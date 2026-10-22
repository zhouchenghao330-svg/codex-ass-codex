"""Compact, clickable tool summaries and readable file-change details."""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from rich.cells import cell_len
from rich.syntax import Syntax

from .colors import FOREGROUND, MUTED

if TYPE_CHECKING:
    from .transcript import ReplyBlock

GREEN = "#78d8bc"
RED = "#e8a0a0"
FAILED = {"failed", "decline", "declined"}


def command_title(command: str) -> str:
    try:
        args = shlex.split(command)
        if len(args) >= 3 and args[0].rsplit("/", 1)[-1] in {"sh", "bash", "zsh", "fish"} and "c" in args[1]:
            command = args[2]
    except ValueError:
        pass
    return " ".join(command.split())


@lru_cache(maxsize=128)
def shell_text(command: str) -> Text:
    text = Syntax("", "bash", theme="ansi_dark").highlight(command)
    text.rstrip()
    text.style = FOREGROUND
    text.highlight_regex(r"(?<!\w)--?[A-Za-z][A-Za-z0-9_-]*", style="#e8b878")
    text.highlight_regex(r"(?:^|[;|&]\s*)[A-Za-z_][\w-]*", style="#83c8e8")
    return text


def payload(block: ReplyBlock):
    try:
        return json.loads(block.text)
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class FileChange:
    path: str
    action: str
    previous: str
    diff: str
    added: int | None
    deleted: int | None


def file_changes(block: ReplyBlock) -> list[FileChange]:
    if block.kind != "file":
        return []
    data = payload(block)
    if isinstance(data, dict):
        data = data.get("changes", [])
    if not isinstance(data, list):
        return []
    changes = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind", {})
        action = kind.get("type", "update") if isinstance(kind, dict) else str(kind)
        move = (kind.get("move_path") or kind.get("movePath")) if isinstance(kind, dict) else None
        path = str(item.get("path", "未知路径"))
        previous = path if move else ""
        diff = str(item.get("diff") or "")
        lines = diff.splitlines()
        unified = any(line.startswith(("@@", "diff --git", "--- ", "+++ ")) for line in lines)
        added = deleted = 0
        in_hunk = False
        for line in lines:
            if line.startswith("@@"):
                in_hunk = True
            elif line.startswith("diff --git"):
                in_hunk = False
            elif line.startswith("+") and (in_hunk or not line.startswith("+++ ")):
                added += 1
            elif line.startswith("-") and (in_hunk or not line.startswith("--- ")):
                deleted += 1
        # Native add/delete items may contain the literal file instead of a unified diff.
        if action == "add" and not unified:
            added = len(lines)
            deleted = 0
        elif action == "delete" and not unified:
            added = 0
            deleted = len(lines)
        elif action == "update" and diff and not unified and not added and not deleted:
            added = deleted = None
        changes.append(FileChange(str(move or path), "rename" if move else action, previous, diff, added, deleted))
    return changes


def counts(change: FileChange) -> str:
    return f"+{change.added} -{change.deleted}" if change.added is not None else "行数未知"


def tool_title(block: ReplyBlock) -> str:
    if block.kind == "command":
        return "命令 · " + command_title(block.title)
    if block.kind == "file":
        return "文件 · " + block.title
    data = payload(block)
    if isinstance(data, dict):
        kind = data.get("type")
        name = "/".join(str(data[key]) for key in ("server", "namespace", "tool") if data.get(key))
        if kind == "mcpToolCall":
            return "MCP · " + (name or block.title)
        if kind == "dynamicToolCall":
            return "工具 · " + (name or block.title)
        if kind == "webSearch":
            action = data.get("action") or {}
            return "搜索 · " + str(data.get("query") or action.get("query") or block.title)
        if kind == "imageView":
            return "查看图片 · " + str(data.get("path") or block.title)
    return "工具 · " + block.title


def status_text(block: ReplyBlock) -> str:
    return {"inProgress": "执行中", "completed": "完成", "failed": "失败", "declined": "拒绝",
            "stopped": "已停止", "cancelled": "已取消"}.get(block.status, block.status)


def action_style(action: str, interactive: bool, color: str = MUTED) -> Style:
    return Style(color=color, meta={"@click": action} if interactive else {})


def tool_summary(blocks: list[ReplyBlock], expanded: bool, width: int, *, level: int | None = None,
                 group_index: int = 0, interactive: bool = False) -> Text:
    level = (2 if expanded else 0) if level is None else level
    commands = sum(block.kind == "command" for block in blocks)
    tools = sum(block.kind not in {"command", "file"} for block in blocks)
    changes = [change for block in blocks for change in file_changes(block)]
    files = len({change.path for change in changes}) or sum(block.kind == "file" for block in blocks)
    if len(blocks) == 1 and not changes:
        label = tool_title(blocks[0])
    elif commands == len(blocks):
        label = f"{commands} 个命令"
    else:
        label = " · ".join(f"{count} {name}" for count, name in ((commands, "命令"), (tools, "工具"), (files, "文件")) if count)
    if changes and all(change.added is not None for change in changes):
        label += f"  +{sum(change.added for change in changes)} -{sum(change.deleted for change in changes)}"
    failed = sum(block.status in FAILED for block in blocks)
    status = "执行中" if any(block.status == "inProgress" for block in blocks) else (
        f"{failed} 项失败" if failed else "已停止" if any(block.status == "stopped" for block in blocks) else "已完成")
    hint = "点击展开 · Ctrl+T 展开" if not level else "点击收起"
    suffix = f" · {status}  ({hint})"
    extra = "  [全部详情]" if level == 1 else "  [仅看摘要]" if level == 2 else ""
    style = action_style(f"tools_group({group_index})", interactive, "#e8b878" if failed else MUTED)
    line = Text(("▾ " if level else "▸ ") + label, style=style, no_wrap=True)
    line.truncate(max(4, width - Text(suffix + extra).cell_len), overflow="ellipsis")
    line.append(suffix, style=style)
    if extra:
        line.append(extra, style=action_style(f"tools_all({group_index},{2 if level == 1 else 1})", interactive, "#83c8e8"))
    return line


def diff_text(change: FileChange) -> Text:
    text = Text()
    lines = change.diff.splitlines()
    has_hunks = any(line.startswith("@@") for line in lines)
    for line in lines:
        color = GREEN if line.startswith("+") else RED if line.startswith("-") else "#83c8e8" if line.startswith("@@") else MUTED
        if change.action == "add" and not has_hunks:
            color = GREEN
        elif change.action == "delete" and not has_hunks:
            color = RED
        text.append(line + "\n", style=color)
    if not text:
        text.append("（未提供 diff）", style=MUTED)
    return text


def tool_content(block: ReplyBlock) -> Text:
    details = Text(style=MUTED)
    if block.kind == "command":
        details.append(f"$ {block.title}\n")
        details.append(block.text.rstrip())
        if block.exit_code is not None:
            details.append(f"\n[退出码 {block.exit_code}]")
        return details
    data = payload(block)
    if isinstance(data, dict) and data.get("type") in {"mcpToolCall", "dynamicToolCall"}:
        fields = (("arguments", "参数"), ("result", "结果"), ("contentItems", "输出"),
                  ("content", "输出"), ("error", "错误"))
        for key, title in fields:
            if key in data and data[key] is not None:
                value = data[key]
                details.append(f"{title}\n", style="bold " + MUTED)
                details.append((value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)) + "\n")
        if details:
            return details
    details.append(block.text or "（暂无输出）")
    return details


def short_path(path: str, width: int) -> str:
    if cell_len(path) <= width:
        return path
    while path and cell_len(path) > max(1, width - 1):
        path = path[1:]
    return "…" + path


def preview_value(value) -> str:
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        value = "\n".join(str(item.get("text", "")) for item in value["content"] if isinstance(item, dict) and item.get("text")) or value
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    lines = text.splitlines()
    return (lines[0] if lines else "") + (f" …（另 {len(lines) - 1} 行）" if len(lines) > 1 else "")


def render_tool_group(blocks: list[ReplyBlock], start: int, level: int, width: int,
                      expanded_items: dict[tuple[int, int], bool], interactive: bool):
    yield tool_summary(blocks, level == 2, width, level=level, group_index=start, interactive=interactive)
    if not level:
        return
    for index, block in enumerate(blocks, start):
        changes = file_changes(block)
        parts = list(enumerate(changes)) if changes else [(-1, None)]
        for part, change in parts:
            expanded = expanded_items.get((index, part), level == 2)
            style = action_style(f"tool_item({index},{part})", interactive, "#e8b878" if block.status in FAILED else MUTED)
            title = Text("  " + ("▾ " if expanded else "▸ "), style=style, no_wrap=block.kind != "command")
            if change:
                verb = {"add": "新增", "delete": "删除", "update": "修改", "rename": "重命名"}.get(change.action, "修改")
                path = f"{change.previous} → {change.path}" if change.previous else change.path
                path = short_path(path, max(8, width - cell_len(f"    {verb} ·   {counts(change)} · {status_text(block)}")))
                title.append(f"{verb} · {path}  {counts(change)}", style=style)
            elif block.kind == "command":
                title.append("命令 · ")
                title.append_text(shell_text(command_title(block.title)).copy())
            else:
                title.append(tool_title(block), style=style)
            title.append(f" · {status_text(block)}", style=style)
            if block.kind != "command":
                title.truncate(max(4, width), overflow="ellipsis")
            yield title
            if expanded:
                if change:
                    yield Text((f"{change.previous} → " if change.previous else "") + change.path + f"  ({counts(change)})", style=MUTED)
                    yield diff_text(change)
                elif block.kind == "command":
                    command = Text("$ ", style=MUTED)
                    command.append_text(shell_text(block.title).copy())
                    yield command
                    if block.text:
                        yield Text(block.text.rstrip(), style=MUTED)
                    if block.exit_code is not None:
                        yield Text(f"[退出码 {block.exit_code}]", style=MUTED)
                else:
                    yield tool_content(block)
            elif not change:
                previews = []
                if block.kind == "command" and block.text:
                    previews.append(("输出", block.text))
                elif block.kind == "tool":
                    data = payload(block)
                    if isinstance(data, dict):
                        previews.extend((title, data[key]) for key, title in (("arguments", "参数"), ("result", "结果"), ("error", "错误"))
                                        if data.get(key) is not None)
                for title, value in previews:
                    line = Text(f"    {title}：" + preview_value(value), style=MUTED, no_wrap=True)
                    line.truncate(max(4, width), overflow="ellipsis")
                    yield line
