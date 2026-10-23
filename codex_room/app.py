from __future__ import annotations

import asyncio
import argparse
import json
import os
import re
import sys
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from rich.cells import cell_len
from rich.console import Group
from rich.style import Style as RichStyle
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as EventMessage
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from .runtime import CodexClient, CodexError, clean_error
from .settings import ModelSettings, PermissionSettings, RuntimeApproval, RuntimeQuestion, PERMISSION_LABELS
from .storage import RoomStore
from .transcript import Entry, ReplyBlock, format_duration, render_reply
from .colors import COLOR_CHOICES, FOREGROUND, MUTED, choose_color, member_colors, normalize_color
from .member_config import parse_add, resolve_directory, resolve_member, validate_name
from .completion import Completion, CompletionEngine, is_room_command
from .selectable import ConversationScreen, SelectableStatic
from .composer import ComposerInput
from .attachments import Attachment, Prompt
from .rendering import ReplyVisual
from .forwarding import ForwardQuote, RecipientPicker, QuotePreview, capture_selection


SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def update_text(widget: Static, content, *, layout: bool = False) -> None:
    """Avoid repainting unchanged chrome or reflowing fixed-height text."""
    if widget.content != content:
        widget.update(content, layout=layout)


def summary_title(summary: str) -> str:
    """Use only the headline of the server's public reasoning summary."""
    first_line = next((line.strip() for line in summary.splitlines() if line.strip()), "")
    return re.sub(r"^[#\s]+|[*`_]", "", first_line).strip()[:120]


def directory_label(directory: str, width: int) -> str:
    """Keep the end of a long path visible in a single reply header."""
    path = Path(directory)
    try:
        relative = path.relative_to(Path.home())
        label = "~" if relative == Path(".") else f"~/{relative}"
    except ValueError:
        label = str(path)
    if width < 1:
        return ""
    if cell_len(label) > width:
        while label and cell_len(label) > width - 1:
            label = label[1:]
        return f"…{label}"
    return label


@dataclass
class Agent:
    name: str
    label: str
    color: str
    busy: bool = False
    phase: str = "等待你的消息"
    queue: deque[Entry] = field(default_factory=deque)
    delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    steered_inputs: list[str] = field(default_factory=list)
    history: list[Entry] = field(default_factory=list)
    thread_id: str | None = None
    model: str = ""
    effort: str = ""
    permission: str = "full"
    service_tier: str | None = None
    legacy_instructions: bool = False
    cwd: str = ""
    parent_name: str = ""
    agent_path: str = ""
    can_accept_direct_input: bool = True
    native_closed: bool = False
    room_tool_version: int = 0

    @property
    def accent(self) -> str:
        return member_colors(self.color).accent

    def config(self) -> dict:
        return {key: getattr(self, key) for key in (
            "name", "label", "color", "thread_id", "model", "effort", "permission", "service_tier", "legacy_instructions", "cwd",
            "parent_name", "agent_path", "can_accept_direct_input", "native_closed", "room_tool_version",
        )}


class Member(Static):
    class Selected(EventMessage):
        def __init__(self, name: str) -> None:
            self.name = name
            super().__init__()

    def __init__(self, agent: Agent) -> None:
        super().__init__(id=self.widget_id(agent.name), classes="member")
        self.agent = agent
        self.tooltip = agent.cwd

    @staticmethod
    def widget_id(name: str) -> str:
        # Rust accepts Unicode internal names. Keep existing ASCII selectors and
        # encode other names into Textual's narrower widget identifier alphabet.
        return f"member-{name}" if name.isascii() else "member--" + name.encode("utf-8").hex()

    def refresh_member(self, selected: bool, frame: str) -> None:
        agent = self.agent
        status = f"{frame} 工作中" if agent.busy else "● 空闲"
        color = "#e8b878" if agent.busy else "#78d8bc"
        text = Text()
        text.append(f"{'▸' if selected else ' '} {agent.label}", style=f"bold {agent.accent}")
        text.append("\n")
        text.append(f"  {status}", style=color)
        if agent.queue:
            text.append(f"  +{len(agent.queue)} 待发送", style="#e8b878")
        if agent.cwd:
            text.append(f"\n  ⌂ {Path(agent.cwd).name or agent.cwd}", style=MUTED)
        self.set_class(selected, "selected")
        layout_key = (agent.label, agent.cwd, agent.busy, len(str(len(agent.queue))) if agent.queue else 0)
        update_text(self, text, layout=layout_key != getattr(self, "_member_layout_key", None))
        self._member_layout_key = layout_key

    def on_click(self) -> None:
        self.post_message(self.Selected(self.agent.name))


class ReplyBody(SelectableStatic):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.auto_links = False
        self._render_nodes = {}

    def update(self, content="", *, layout=True) -> None:
        if isinstance(content, Group):
            visual = ReplyVisual(self, content.renderables, self._render_nodes, getattr(content, "room_sources", ()))
            self._render_nodes = visual.nodes
            content = visual
        super().update(content, layout=layout)

    def action_tools_group(self, index: int) -> None:
        self.parent.cycle_tools_group(index)

    def action_tools_all(self, index: int, level: int) -> None:
        self.parent.set_tools_group(index, level)

    def action_tool_item(self, index: int, part: int) -> None:
        self.parent.toggle_tool_item(index, part)


class ReplyHeader(SelectableStatic):
    def action_toggle_reply(self) -> None:
        self.parent.toggle_reply()

    def action_show_quote(self) -> None:
        self.app.push_screen(QuotePreview(ForwardQuote.load(self.parent.entry.quote)))


class MessageCard(Vertical):
    def __init__(self, entry: Entry, agents: dict[str, Agent]) -> None:
        super().__init__(classes=f"message {entry.sender if entry.sender in ('you', 'system') else 'agent'}")
        self.entry = entry
        self.agents = agents
        self.reply_colors = member_colors(agents[entry.sender].color) if entry.sender in agents else None
        if self.reply_colors:
            self.styles.background = self.reply_colors.background
            self.styles.border_left = ("solid", self.reply_colors.accent)
        self.tools_expanded = False
        self.tool_group_levels: dict[int, int] = {}
        self.tool_item_expanded: dict[tuple[int, int], bool] = {}
        self.started_at = monotonic()
        self.animation_frame = 0
        self._body_key = None
        self._rendered_width = None
        self.header = ReplyHeader(classes="message-header")
        self.preview = ReplyHeader(classes="reply-preview")
        self.preview.display = False
        self.body = ReplyBody(classes="message-body")
        self.progress = SelectableStatic(classes="working-status")
        self.work_summary = SelectableStatic(classes="work-summary")
        self.quote_header = ReplyHeader(classes="quote-header")
        self.refresh_entry()

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.quote_header
        yield self.preview
        yield self.body
        yield self.progress
        yield self.work_summary

    def refresh_completion(self) -> None:
        entry = self.entry
        visible = (entry.sender in self.agents and entry.state in {"done", "stopped", "failed"}
                   and entry.finished_at is not None and entry.duration_seconds is not None)
        self.work_summary.display = visible
        if visible:
            finished = datetime.fromisoformat(entry.finished_at)
            label = {"done": "Worked for", "stopped": "Stopped after", "failed": "Failed after"}[entry.state]
            update_text(self.work_summary, Text(f"{label} {format_duration(entry.duration_seconds)} · {finished:%H:%M}", style=MUTED))
            self.work_summary.tooltip = finished.strftime("%Y-%m-%d %H:%M:%S %z")

    def refresh_working(self, frame: int | None = None) -> None:
        if frame is not None:
            self.animation_frame = frame
        active = self.entry.state == "working" and self.entry.sender in self.agents
        self.progress.display = active
        if not active:
            return
        elapsed = int(monotonic() - self.started_at)
        duration = f"{elapsed // 60}m {elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
        prefix = Text(f"{SPINNER[self.animation_frame % len(SPINNER)]} Working", style=MUTED)
        suffix = f" · {duration}"
        phase = Text(f" · {self.agents[self.entry.sender].phase}", style=self.reply_colors.muted)
        width = self.content_size.width or 80
        phase.truncate(max(0, width - prefix.cell_len - Text(suffix).cell_len), overflow="ellipsis")
        prefix.append_text(phase)
        prefix.append(suffix, style=MUTED)
        update_text(self.progress, prefix)

    def on_resize(self) -> None:
        if self._rendered_width != self.content_size.width:
            self.refresh_entry()

    def cycle_tools_group(self, index: int) -> None:
        current = self.tool_group_levels.get(index, 2 if self.tools_expanded else 0)
        self.set_tools_group(index, 0 if current else 1)

    def set_tools_group(self, index: int, level: int) -> None:
        self.tool_group_levels[index] = level
        end = index
        while end < len(self.entry.blocks) and self.entry.blocks[end].kind != "text":
            end += 1
        self.tool_item_expanded = {key: value for key, value in self.tool_item_expanded.items() if not index <= key[0] < end}
        self.body.screen.clear_selection()
        self.refresh_entry()

    def toggle_tool_item(self, index: int, part: int) -> None:
        start = index
        while start and self.entry.blocks[start - 1].kind != "text":
            start -= 1
        default = self.tool_group_levels.get(start, 2 if self.tools_expanded else 0) == 2
        self.tool_item_expanded[index, part] = not self.tool_item_expanded.get((index, part), default)
        self.body.screen.clear_selection()
        self.refresh_entry()

    def toggle_reply(self) -> None:
        if self.entry.sender not in self.agents:
            return
        self.entry.collapsed = not self.entry.collapsed
        self.screen.clear_selection()
        self.app.dirty = True
        self.refresh_entry()

    def refresh_preview(self) -> None:
        # One readable line, with the complete reply retained for expansion.
        width = self.content_size.width or 80
        first = re.search(r"\S", self.entry.body)
        line = self.entry.body[first.start():first.start() + width * 2].split("\n", 1)[0] if first else ""
        line = re.sub(r"[`*_]", "", line.lstrip("#> -"))
        text = Text(line or ("正在工作…" if self.entry.state == "working" else "工具调用记录"), style=FOREGROUND)
        text.truncate(width, overflow="ellipsis")
        text.stylize(RichStyle(meta={"@click": "toggle_reply"}))
        update_text(self.preview, text)

    def refresh_entry(self) -> None:
        entry = self.entry
        self.quote_header.display = bool(entry.quote)
        if entry.quote:
            quote = ForwardQuote.load(entry.quote)
            label = Text(f"↳ {quote.title} · 点击查看", style=MUTED)
            label.stylize(RichStyle(meta={"@click": "show_quote"}))
            update_text(self.quote_header, label)
        text = Text()
        if entry.sender == "system":
            text.append("◇  ", style="#788294")
            text.append(entry.body, style=MUTED)
        else:
            if entry.sender == "you":
                text.append("› ", style="bold #e8edf5")
                recipients = [name for name in entry.recipients if name in self.agents] or [entry.target]
                for index, name in enumerate(recipients):
                    target = self.agents[name]
                    text.append((" " if index else "") + f"@{target.label}", style=target.accent)
            else:
                agent = self.agents[entry.sender]
                text.append("▸ " if entry.collapsed else "▾ ",
                            style=RichStyle(color=MUTED, meta={"@click": "toggle_reply"}))
                text.append(agent.label, style=f"bold {self.reply_colors.accent}")
                text.append(f"  @{agent.label}", style=self.reply_colors.muted)
            if entry.state == "queued":
                text.append("   ◷ 发送中", style="#e8b878")
            elif entry.state == "steered":
                text.append("   ↳ 已追加", style=MUTED)
            elif entry.state == "stopped":
                text.append("   · 已停止", style=MUTED)
            elif entry.state == "failed":
                text.append("   · 失败", style="#e8b878")
            if entry.sender == "you":
                text.append("  ")
                text.append(entry.display_body if entry.display_body is not None else entry.body, style=FOREGROUND)
            else:
                if agent.cwd:
                    separator = "   · ⌂ "
                    width = self.content_size.width or 80
                    directory = directory_label(agent.cwd, width - text.cell_len - cell_len(separator))
                    if directory:
                        text.append(f"{separator}{directory}", style=MUTED)
                    self.header.tooltip = agent.cwd
                update_text(self.header, text)
                self.set_class(entry.collapsed, "reply-collapsed")
                self.preview.display = entry.collapsed
                if entry.collapsed:
                    self.refresh_preview()
                self.body.display = not entry.collapsed and bool(entry.body or entry.blocks or entry.state != "working")
                width = self.content_size.width or 80
                key = (entry.body, tuple((block.id, block.kind, block.text, block.title, block.status, block.exit_code) for block in entry.blocks),
                       self.tools_expanded, tuple(self.tool_group_levels.items()), tuple(self.tool_item_expanded.items()), width, agent.color)
                if not entry.collapsed and key != self._body_key:
                    self.body.update(render_reply(entry, self.tools_expanded, width, agent.color,
                        group_levels=self.tool_group_levels, expanded_items=self.tool_item_expanded, interactive=True))
                    self._body_key = key
                self._rendered_width = self.content_size.width
                self.refresh_working()
                self.refresh_completion()
                return
        update_text(self.header, text, layout=True)
        self.body.display = False
        self.preview.display = False
        self.progress.display = False
        self.work_summary.display = False


class AddMember(ModalScreen[tuple[str, str, str] | None]):
    BINDINGS = [("escape", "cancel", "返回")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="add-member-dialog"):
            yield Static("[b]拉一个 Codex 进来[/b]", id="dialog-title")
            yield Static("独立会话，从你发给它的第一条消息开始。", classes="muted")
            yield Static("名字 · 用于 @ 点名", classes="field-label")
            yield Input(placeholder="例如 开发笔记 或 a", id="new-name", max_length=32)
            yield Static("工作目录 · 留空沿用群聊目录", classes="field-label")
            yield Input(placeholder=str(self.app.cwd), id="new-cwd")
            yield Static("颜色 · 只显示未占用的颜色", classes="field-label")
            used = {normalize_color(agent.color) for agent in self.app.agents.values()}
            options = [("自动分配", "auto")] + [(Text(f"● {label} · {key}", style=color), color)
                                               for key, label, color in COLOR_CHOICES if color not in used]
            yield Select(options, value="auto", allow_blank=False, id="new-color", compact=True)
            yield Static("", id="color-preview")
            yield Static("", id="dialog-error")
            with Horizontal(id="dialog-actions"):
                yield Button("取消", id="cancel-add")
                yield Button("加入对话", id="confirm-add", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#new-name", Input).focus()
        self.refresh_preview()

    def refresh_preview(self) -> None:
        color = choose_color([agent.color for agent in self.app.agents.values()], str(self.query_one("#new-color", Select).value))
        colors = member_colors(color)
        name = self.query_one("#new-name", Input).value.strip() or "Codex"
        preview = self.query_one("#color-preview", Static)
        preview.styles.background = colors.background
        preview.styles.border_left = ("solid", colors.accent)
        text = Text(f"{name}", style=colors.accent)
        text.append("  ·  正文保持米白色", style=colors.text)
        preview.update(text)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "new-color" and self.is_mounted:
            self.refresh_preview()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "new-name" and self.is_mounted:
            self.refresh_preview()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def confirm(self) -> None:
        name = self.query_one("#new-name", Input).value.strip()
        app = self.app
        try:
            name = validate_name(name, app.agents)
        except ValueError as exc:
            self.query_one("#dialog-error", Static).update(Text(str(exc)))
            return
        try:
            cwd = resolve_directory(self.query_one("#new-cwd", Input).value.strip(), app.cwd)
        except (ValueError, OSError) as exc:
            self.query_one("#dialog-error", Static).update(Text(str(exc)))
            return
        self.dismiss((name, str(self.query_one("#new-color", Select).value), str(cwd)))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.confirm()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-add":
            self.confirm()
        else:
            self.dismiss(None)


class Help(ModalScreen[None]):
    BINDINGS = [("escape", "close", "返回"), ("f1", "close", "返回")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static("[b]一个终端，几位 Codex。[/b]", id="dialog-title")
            yield Static(
                "[b #a9b6ff]@a 你的消息[/]       发给指定成员\n"
                "[b #a9b6ff]@a @b 你的消息[/]    同时交给两人，各自独立处理\n"
                "[b #a9b6ff]直接输入[/]           发给左侧选中的成员\n"
                "[b #a9b6ff]/add 名字 [目录][/]    新建 Codex，省略目录则沿用群聊目录\n"
                "[b #a9b6ff]--color blue[/]        创建时指定颜色\n"
                "[b #a9b6ff]/focus 名字[/]         切到该成员的单人对话\n"
                "[b #a9b6ff]/focus all[/]          回到聊天室的全部消息\n"
                "[b #a9b6ff]/members[/]            展开 / 收起成员列表\n"
                "[b #a9b6ff]/stop 名字[/]          停止回复，取消尚未发出的消息\n"
                "[b #a9b6ff]/del 名字[/]           移除成员，会话记录先归档\n"
                "[b #a9b6ff]/rename 原名 新名[/]   改名，保留原会话；支持中文\n"
                "[b #a9b6ff]/model[/]              选择模型和推理强度\n"
                "[b #a9b6ff]/permissions[/]        选择权限，默认 Full Access\n"
                "[b #a9b6ff]@a /model[/]           调整指定成员的设置\n"
                "[b #a9b6ff]/help[/]               打开此帮助\n\n"
                "[b]Ctrl+1 / 2 / 3[/] 切换成员   [b]Ctrl+L[/] 全部 / 单人视图\n"
                "[b]Ctrl+N[/] 添加成员           [b]@[/] 打开成员选择\n"
                "[b]↑ / ↓[/] 选择候选           [b]Tab / Enter[/] 补全\n"
                "[b]空输入框按 ↑[/] 调出历史，再用 ↑ / ↓ 翻阅\n"
                "[b]Enter[/] 发送   [b]Shift+Enter / Ctrl+J[/] 换行\n"
                "[b]/命令、成员、模型、权限、目录和颜色[/] 均支持补全\n"
                "[b]Ctrl+B[/] 展开 / 收起成员\n"
                "[b]Ctrl+T[/] 展开 / 收起工具调用详情\n"
                "拖选对话后点击底部 [b]转发[/]，或按 [b]Ctrl+F[/]\n"
                "选择现有成员或新建，补一句任务后发送；引用含折叠详情\n"
                "[b]← 返回来源[/] 回到来源最新消息，恢复原视图和草稿\n"
                "点击回复前的 [b]▾ / ▸[/] 收起 / 展开整条回复\n"
                "长粘贴 / 图片显示为块，退格整块删除\n"
                "[b]Alt+Enter[/] 展开光标处的长粘贴原文\n"
                "[b]Ctrl+Q[/] 退出               [b]Esc[/] 回到输入框\n\n"
                "[#788294]默认连接本机 Codex，沿用登录和项目配置。\n"
                "成员会话和配置保存在项目的 .codex-room/ 中。\n"
                "uv run codex-room --demo 可以进入纯界面演示。[/]",
                id="help-copy",
            )
            yield Button("明白了，继续聊", id="close-help", variant="primary")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self) -> None:
        self.dismiss(None)


class CompletionList(OptionList, can_focus=False):
    """A mouse-selectable popup which leaves keyboard focus in the composer."""


class CodexRoom(App):
    TITLE = "Codex Room"
    SUB_TITLE = "一个终端，几位 Codex"
    CSS_PATH = "room.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("ctrl+c", "copy_or_quit", "复制 / 退出", priority=True, show=False),
        Binding("super+c,ctrl+shift+c", "copy_selection", "复制选中文字", priority=True, show=False),
        Binding("ctrl+n", "add_member", "添加成员", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "成员", priority=True),
        Binding("ctrl+l", "toggle_view", "全部 / 单人", priority=True),
        Binding("ctrl+t", "toggle_tools", "工具详情", priority=True),
        Binding("ctrl+f", "forward_selection", "转发选区", priority=True),
        Binding("ctrl+1", "select_index(0)", "成员 1", priority=True, show=False),
        Binding("ctrl+2", "select_index(1)", "成员 2", priority=True, show=False),
        Binding("ctrl+3", "select_index(2)", "成员 3", priority=True, show=False),
        Binding("f1", "help", "帮助", priority=True),
        Binding("escape", "composer", "输入", show=False),
    ]

    def get_default_screen(self) -> ConversationScreen:
        return ConversationScreen(id="_default")

    def __init__(self, *, demo: bool = False, speed: float = 1.0,
                 cwd: Path | None = None, state_dir: Path | None = None) -> None:
        if os.name == "posix":
            from .terminal import RoomDriver
            super().__init__(driver_class=RoomDriver)
        else:
            super().__init__()
        self.scroll_sensitivity_y = 1.0
        self.agents: dict[str, Agent] = {}
        self.selected = ""
        self.show_all = True
        self.sidebar_visible = False
        self.tools_expanded = False
        self.completion_open = False
        self.completion_engine = CompletionEngine()
        self.completion: Completion | None = None
        self.completion_matches: list[str] = []
        self.completion_signature: tuple = ()
        self.dismissed_completion: tuple[str, int] | None = None
        self.entries: list[Entry] = []
        self.batch_heads: dict[str, str] = {}
        self.cards: dict[int, MessageCard] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.active_entries: dict[str, tuple[Entry, Entry]] = {}
        self.reply_boundaries: set[str] = set()
        self.forward_quote: ForwardQuote | None = None
        self.forward_target = ""
        self.forward_origin: dict | None = None
        self.forward_drafts: dict[str, tuple[Prompt, ForwardQuote, dict | None]] = {}
        self.forward_capture: tuple[ForwardQuote, dict] | None = None
        self.pending_interactions: dict[str, tuple[ModalScreen, asyncio.Future]] = {}
        self.showing_interactions: set[str] = set()
        self.frame = 0
        self.speed = speed
        self.demo = demo
        self.cwd = (cwd or Path.cwd()).resolve()
        self.client: CodexClient | None = None
        self.connected = demo
        self.connection_error = ""
        self.connection_task: asyncio.Task | None = None
        self.defaults = {"model": "", "effort": ""}
        self.store = None if demo else RoomStore(state_dir or self.cwd / ".codex-room")
        self.saved = self.store.load() if self.store else {}
        self.input_history: list[str | dict] = self.saved.get("input_history", [
            f"@{entry['target']} {entry['body']}" for entry in self.saved.get("entries", [])
            if entry.get("sender") == "you" and entry.get("target") and entry.get("body")
        ])
        self.dirty = False
        self.shutting_down = False
        self.request_lock = asyncio.Lock()
        self.turn_permissions: dict[str, str] = {}
        if self.saved:
            self.agents = {item["name"]: Agent(**item) for item in self.saved.get("agents", [])}
            used = []
            for agent in self.agents.values():
                cwd = str(resolve_directory(agent.cwd, self.cwd, validate=False))
                self.dirty |= agent.cwd != cwd
                agent.cwd = cwd
                try:
                    color = choose_color(used, agent.color)
                except ValueError:
                    color = choose_color(used)
                self.dirty |= agent.color != color
                agent.color = color
                used.append(color)
            self.selected = self.saved.get("selected", "")
            self.show_all = self.saved.get("show_all", True)
            if self.selected not in self.agents:
                self.selected = next(iter(self.agents), "")

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                with Horizontal(id="members-heading"):
                    yield Static("成员", id="members-label")
                    yield Static(f"{len(self.agents):02d}", id="member-count")
                    yield Button("‹", id="close-sidebar")
                yield Button("全部消息", id="all-messages", tooltip="显示所有成员的对话 · Ctrl+L 切换全部 / 单人")
                with VerticalScroll(id="members"):
                    for agent in self.agents.values():
                        yield Member(agent)
                yield Button("＋ 添加", id="add-member")
            with Vertical(id="main"):
                with VerticalScroll(id="timeline"):
                    yield Static("输入 /add 名字 或按 Ctrl+N 创建 Codex", id="empty-room", classes="muted")
                yield CompletionList(id="completion-list")
                with Horizontal(id="selection-actions"):
                    yield Static("选中的对话", id="selection-label")
                    yield Button("转发 · Ctrl+F", id="forward-selection")
                with Horizontal(id="forward-context"):
                    yield Button("", id="quote-preview")
                    yield Button("整个选区", id="quote-scope")
                    yield Button("× 移除引用", id="quote-remove")
                yield Button("← 返回来源", id="forward-return")
                with Horizontal(id="composer"):
                    yield Static("›", id="prompt")
                    yield ComposerInput(placeholder="说点什么，或 @名字…", id="message-input")
                with Horizontal(id="footer"):
                    yield Button("› 成员", id="toggle-sidebar", tooltip="展开 / 收起成员列表 · Ctrl+B")
                    yield Button("回复请求", id="respond-request")
                    yield Static("", id="recipient")
                    yield Static("F1 帮助", id="connection")

    async def on_mount(self) -> None:
        self.room_screen = self.screen
        self.theme = "textual-dark"
        for item in self.saved.get("entries", []):
            entry = Entry(**item)
            if entry.sender == "system" and not entry.target:
                for name in self.agents:
                    if entry.body.startswith((f"{name} 已加入", f"@{name} ", f"@{name}的")):
                        entry.target = name
                        break
            entry.restore_legacy_tools()
            if entry.state == "continued":
                entry.finish_tools("stopped")
            if entry.state in {"working", "queued"}:
                entry.state = "stopped"
                entry.finish_tools("stopped")
                if not entry.body:
                    entry.body = "上次回复未完成。"
            await self.add_entry(entry)
        self.set_interval(0.15, self.tick)
        self.set_interval(1, self.save_state)
        if not self.demo:
            self.connection_task = asyncio.create_task(self.connect_codex())
        self.refresh_chrome()
        self.query_one("#message-input", ComposerInput).focus()
        self.call_after_refresh(self.scroll_bottom)

    def on_app_focus(self) -> None:
        if (hasattr(self, "room_screen") and self.screen is self.room_screen
                and not self.room_screen.selections and not self.room_screen._selecting):
            self.action_composer()

    def on_click(self, event: events.Click) -> None:
        if self.screen is not getattr(self, "room_screen", None) or event.button != 1:
            return
        widget = event.widget
        if widget and any(isinstance(node, (Input, ComposerInput, Button, Select, OptionList, Member))
                          for node in (widget, *widget.ancestors)):
            return
        if event.style and (event.style.link or event.style.meta.get("@click")):
            return
        if not self.screen.selections:
            self.action_composer()

    def on_key(self, event: events.Key) -> None:
        if self.screen is not getattr(self, "room_screen", None) or not event.is_printable:
            return
        composer = self.room_screen.query_one("#message-input", ComposerInput)
        if not composer.has_focus:
            self.screen.clear_selection()
            composer.focus()
            composer.post_message(event.copy())
            event.prevent_default()
            event.stop()

    def on_paste(self, event: events.Paste) -> None:
        if self.screen is not getattr(self, "room_screen", None):
            return
        composer = self.room_screen.query_one("#message-input", ComposerInput)
        if not composer.has_focus:
            self.screen.clear_selection()
            composer.focus()
            composer.post_message(events.Paste(event.text))
            event.prevent_default()
            event.stop()

    def on_resize(self, event: events.Resize) -> None:
        screen = getattr(self, "room_screen", self.screen)
        screen.set_class(event.size.width < 100, "narrow")
        screen.set_class(event.size.height < 34, "compact")
        if hasattr(self, "room_screen"):
            self.set_timer(0.05, self.reveal_selected)

    def reveal_selected(self) -> None:
        if not self.is_running or self.shutting_down or not self.sidebar_visible or self.selected not in self.agents:
            return
        member = self.room_screen.query_one("#" + Member.widget_id(self.selected))
        self.room_screen.query_one("#members", VerticalScroll).scroll_to_widget(
            member, animate=False, origin_visible=False, immediate=True
        )

    def scroll_bottom(self) -> None:
        if (not self.is_running or self.shutting_down
                or self.room_screen._selecting or self.room_screen.selections):
            return
        # Callers already wait for layout. A second deferred callback would
        # bypass the selection guard if a drag starts before it runs.
        self.room_screen.query_one("#timeline", VerticalScroll).scroll_end(animate=False, immediate=True)

    def tick(self) -> None:
        if not self.is_running or self.shutting_down:
            return
        self.frame += 1
        actions = self.room_screen.query_one("#selection-actions")
        actions.display = bool(self.room_screen.selections or self.forward_capture) and not self.room_screen._selecting
        for _, answer in self.active_entries.values():
            card = self.cards.get(id(answer))
            if card and card.display:
                card.refresh_working(self.frame)
        if self.completion_open:
            self.refresh_completions()
        self.refresh_chrome()

    def close_completions(self) -> None:
        self.completion_open = False
        composer = self.room_screen.query_one("#message-input", ComposerInput)
        self.dismissed_completion = (composer.value, composer.cursor_position)
        self.room_screen.query_one("#completion-list", CompletionList).display = False

    def refresh_completions(self, *, explicit: bool = False) -> None:
        if not self.is_running or not self.is_mounted or self.shutting_down:
            return
        composer = self.room_screen.query_one("#message-input", ComposerInput)
        menu = self.room_screen.query_one("#completion-list", CompletionList)
        position = (composer.value, composer.cursor_position)
        if not composer.has_focus or (position == self.dismissed_completion and not explicit):
            menu.display = False
            self.completion_open = False
            return
        if position != self.dismissed_completion or explicit:
            self.dismissed_completion = None
        context = self.completion_engine.complete(
            composer.completion_text, composer.cursor_position, self.agents, self.cwd,
            self.client.models if self.client else [], explicit=explicit)
        if context is None:
            menu.display = False
            self.completion_open = False
            self.completion = None
            return
        status = tuple((item.member, self.agents[item.member].busy, len(self.agents[item.member].queue))
                       for item in context.candidates if item.member)
        signature = (context.kind, context.start, context.end, context.candidates, status)
        if signature != self.completion_signature or not menu.option_count:
            old_value = None
            if self.completion and menu.highlighted is not None and menu.highlighted < len(self.completion.candidates):
                old_value = self.completion.candidates[menu.highlighted].value
            options = []
            for index, item in enumerate(context.candidates):
                row = Text(no_wrap=True, overflow="ellipsis")
                if item.member:
                    agent = self.agents[item.member]
                    row.append(f"{item.value:<16}", style=agent.accent)
                    row.append("  工作中" if agent.busy else "  空闲", style="#e8b878" if agent.busy else MUTED)
                    if agent.queue:
                        row.append(f" +{len(agent.queue)}", style="#e8b878")
                    row.append(f" · {Path(agent.cwd).name}", style=MUTED)
                else:
                    row.append(f"{item.value:<20}", style="#a9b6ff")
                    row.append(f"  {item.detail}", style=MUTED)
                options.append(Option(row, id=str(index)))
            empty = {"mention": "没有匹配的 Codex · /add 名字 创建", "member": "没有匹配的 Codex",
                     "command": "没有匹配的命令", "directory": "没有匹配的目录 · 可继续输入路径",
                     "model": "没有匹配的模型 · 可直接输入模型名"}.get(context.kind, "没有匹配的选项")
            menu.set_options(options or [Option(empty, disabled=True)])
            values = [item.value for item in context.candidates]
            menu.highlighted = values.index(old_value) if old_value in values else (0 if values else None)
            self.completion_signature = signature
        self.completion = context
        self.completion_matches = [item.member or item.value for item in context.candidates]
        height = min(8, max(1, len(context.candidates)) + 2)
        menu.styles.height = height
        menu.styles.offset = (0, -height)
        menu.display = True
        self.completion_open = True

    def accept_completion(self, index: int | None = None) -> None:
        menu = self.room_screen.query_one("#completion-list", CompletionList)
        context = self.completion
        index = menu.highlighted if index is None else index
        if context is None or index is None or index >= len(context.candidates):
            return
        item = context.candidates[index]
        composer = self.room_screen.query_one("#message-input", ComposerInput)
        value, cursor = context.apply(composer.value, item)
        self.close_completions()
        suffix = composer.value[context.end:]
        replacement_end = len(value) - len(suffix)
        composer.replace(value[context.start:replacement_end],
                         composer.document.get_location_from_index(context.start),
                         composer.document.get_location_from_index(context.end), maintain_selection_offset=False)
        composer.cursor_position = cursor
        if item.directory or (context.kind in {"color", "directory"} and item.value != "--color"):
            # Enter can use this value; another Tab explores the next argument.
            self.dismissed_completion = (value, cursor)
        if context.kind == "mention":
            self.select(item.member)
        else:
            composer.focus()
        self.refresh_completions()
        self.refresh_recipient()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "completion-list" and event.option_id is not None:
            event.stop()
            self.accept_completion(int(event.option_id))

    def refresh_chrome(self) -> None:
        if not self.is_running or not self.is_mounted or self.shutting_down:
            return
        for member in self.room_screen.query(Member):
            member.refresh_member(not self.show_all and member.agent.name == self.selected, SPINNER[self.frame % len(SPINNER)])
        all_messages = self.room_screen.query_one("#all-messages", Button)
        all_messages.label = "▸ 全部消息" if self.show_all else "  全部消息"
        all_messages.set_class(self.show_all, "selected")
        update_text(self.room_screen.query_one("#member-count", Static), f"{len(self.agents):02d}")
        self.room_screen.query_one("#empty-room", Static).display = not self.agents
        self.room_screen.query_one("#toggle-sidebar", Button).label = "‹ 收起" if self.sidebar_visible else "› 成员"
        self.refresh_recipient()
        self.room_screen.query_one("#respond-request").display = self.selected in self.pending_interactions
        state = "演示" if self.demo else ("已连接" if self.connected else ("离线" if self.connection_error else "连接中"))
        update_text(self.room_screen.query_one("#connection", Static), f"F1 帮助 · {state}")

    def refresh_recipient(self) -> None:
        value = self.room_screen.query_one("#message-input", ComposerInput).value
        match = re.match(r"^@([\w-]+)(?:\s|$)", value)
        name = resolve_member(match.group(1), self.agents) if match else self.selected
        recipient = Text()
        if name in self.agents:
            agent = self.agents[name]
            recipient.append(f"@{agent.label}", style=agent.accent)
            recipient.append(" · 追加到当前工作" if agent.busy else " · 空闲", style=MUTED)
            recipient.append(f" · ⌂ {Path(agent.cwd).name or agent.cwd}", style=MUTED)
            self.room_screen.query_one("#recipient", Static).tooltip = f"工作目录：{agent.cwd}"
            if not self.demo:
                recipient.append(f" · {agent.model or '默认模型'} · {agent.effort or '默认强度'} · {PERMISSION_LABELS[agent.permission]}", style=MUTED)
            recipient.append(" · 全部消息" if self.show_all else " · 单人对话", style=MUTED)
            busy = sum(member.busy for member in self.agents.values())
            if busy:
                recipient.append(f" · {busy} 人工作中", style="#e8b878")
        else:
            recipient.append(f"未找到 @{name} · 可以先 /add {name}" if self.agents else "暂无 Codex · /add 名字 或 Ctrl+N 创建", style=MUTED)
        if self.room_screen.selections:
            recipient = Text("Ctrl+C 复制 · Ctrl+F 转发选中对话 · Esc 取消", style=MUTED)
        elif self.completion_open:
            recipient = Text("↑↓ 选择 · Tab / Enter 补全 · Esc 关闭", style=MUTED)
        elif is_room_command(value):
            recipient = Text("/model  /permissions  /add  /focus  /members  /stop  /del  /help", style=MUTED)
        update_text(self.room_screen.query_one("#recipient", Static), recipient)
        label = self.agents[self.selected].label if self.selected in self.agents else ""
        self.room_screen.query_one("#message-input", ComposerInput).placeholder = f"给 @{label} 发消息，或 @名字…" if self.agents else "输入 /add 名字 创建 Codex…"

    async def add_entry(self, entry: Entry) -> MessageCard:
        self.entries.append(entry)
        if entry.sender == "you" and entry.batch_id:
            self.batch_heads.setdefault(entry.batch_id, entry.id)
        self.dirty = True
        if entry.sender == "you":
            self.agents[entry.target].history.append(entry)
        elif entry.sender in self.agents:
            self.agents[entry.sender].history.append(entry)
        card = MessageCard(entry, self.agents)
        card.tools_expanded = self.tools_expanded
        card.refresh_entry()
        self.cards[id(entry)] = card
        card.display = self.entry_visible(entry)
        timeline = self.room_screen.query_one("#timeline", VerticalScroll)
        follow = timeline.is_vertical_scroll_end and not self.room_screen.selections
        await timeline.mount(card)
        if follow or entry.sender == "you":
            self.call_after_refresh(self.scroll_bottom)
        return card

    def entry_visible(self, entry: Entry) -> bool:
        if entry.hidden:
            return False
        if self.show_all and entry.sender == "you" and entry.batch_id:
            return self.batch_heads.get(entry.batch_id) == entry.id
        return (self.show_all or entry.sender == self.selected or entry.target == self.selected
                or (entry.sender == "system" and not entry.target))

    def apply_filter(self) -> None:
        for entry in self.entries:
            self.cards[id(entry)].display = self.entry_visible(entry)
        self.refresh_chrome()
        self.call_after_refresh(self.scroll_bottom)

    def select(self, name: str, *, single: bool = False) -> None:
        if self.forward_quote and name != self.forward_target:
            composer = self.room_screen.query_one("#message-input", ComposerInput)
            self.forward_drafts[self.forward_target] = (composer.prompt, self.forward_quote, self.forward_origin)
            self.forward_quote = None
            composer.value = ""
        self.selected = name
        if single:
            self.show_all = False
        self.dirty = True
        self.apply_filter()
        if single and name in self.forward_drafts and not self.forward_quote:
            prompt, self.forward_quote, self.forward_origin = self.forward_drafts.pop(name)
            self.forward_target = name
            self.room_screen.query_one("#message-input", ComposerInput).load_prompt(prompt)
        self.refresh_forward_context()
        self.set_timer(0.05, self.reveal_selected)
        if not isinstance(self.screen, ModalScreen):
            self.action_composer()

    def on_member_selected(self, event: Member.Selected) -> None:
        if event.name in self.agents:
            self.select(event.name, single=True)

    def action_select_index(self, index: int) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        names = list(self.agents)
        if index < len(names):
            self.select(names[index], single=True)

    def action_composer(self) -> None:
        self.room_screen.clear_selection()
        self.room_screen.query_one("#message-input", ComposerInput).focus()

    async def action_copy_selection(self) -> bool:
        text = self.screen.get_selected_text()
        if not text and isinstance(self.focused, ComposerInput):
            text = self.focused.selected_text
        if not text and isinstance(self.focused, Input) and not self.focused.password:
            text = self.focused.selected_text
        if not text:
            return False
        self.copy_to_clipboard(text)
        if sys.platform == "darwin" and not self.is_headless:
            # Local macOS clipboard also works when an embedded terminal ignores OSC 52.
            process = None
            try:
                process = await asyncio.create_subprocess_exec("pbcopy", stdin=asyncio.subprocess.PIPE)
                await asyncio.wait_for(process.communicate(text.encode("utf-8")), 2)
            except (OSError, asyncio.TimeoutError):
                if process is not None and process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
        return True

    async def action_copy_or_quit(self) -> None:
        if not await self.action_copy_selection():
            await self.action_quit()

    def action_toggle_view(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.show_all = not self.show_all
        self.dirty = True
        self.apply_filter()
        self.action_composer()

    def action_all_messages(self) -> None:
        self.show_all = True
        self.dirty = True
        self.apply_filter()
        self.action_composer()

    def action_toggle_tools(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        timeline = self.room_screen.query_one("#timeline", VerticalScroll)
        follow, scroll_y = timeline.is_vertical_scroll_end, timeline.scroll_y
        self.tools_expanded = not self.tools_expanded
        for card in self.cards.values():
            if card.entry.blocks:
                card.tools_expanded = self.tools_expanded
                card.tool_group_levels.clear()
                card.tool_item_expanded.clear()
                card.refresh_entry()
        if follow:
            self.call_after_refresh(self.scroll_bottom)
        else:
            self.call_after_refresh(lambda: timeline.scroll_to(y=scroll_y, animate=False))

    def action_help(self) -> None:
        if isinstance(self.screen, Help):
            self.screen.dismiss(None)
        elif not isinstance(self.screen, ModalScreen):
            self.push_screen(Help())

    def action_toggle_sidebar(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.sidebar_visible = not self.sidebar_visible
        self.room_screen.query_one("#sidebar").display = self.sidebar_visible
        self.refresh_chrome()
        if self.sidebar_visible:
            self.set_timer(0.05, self.reveal_selected)
        self.action_composer()

    def action_add_member(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.push_screen(AddMember(), self.member_added)

    def prepare_forward_selection(self) -> None:
        quote = capture_selection(self.entries, self.cards, self.room_screen.selections, self.agents)
        if quote:
            composer = self.room_screen.query_one("#message-input", ComposerInput)
            self.forward_capture = (quote, {"selected": self.selected, "show_all": self.show_all,
                "prompt": composer.prompt, "cursor": composer.cursor_position})

    def action_forward_selection(self) -> None:
        if self.screen is not self.room_screen:
            return
        if self.forward_quote:
            self.notify("先发送或移除当前引用，再开始另一份转发。")
            self.forward_capture = None
            return
        if not self.forward_capture:
            self.prepare_forward_selection()
        if not self.forward_capture:
            self.notify("先在对话区拖选需要转发的内容。")
            return
        self.close_completions()
        self.push_screen(RecipientPicker(), self.forward_recipient_chosen)

    def forward_recipient_chosen(self, name: str | None) -> None:
        if not name:
            self.forward_capture = None
            return
        if name == "+new":
            self.push_screen(AddMember(), self.forward_member_added)
        else:
            self.open_forward_draft(name)

    async def forward_member_added(self, result) -> None:
        if result and await self.create_member(*result):
            self.open_forward_draft(self.selected)
        elif result:
            # Keep the snapshot while correcting invalid input or a failed config read.
            self.push_screen(RecipientPicker(), self.forward_recipient_chosen)
        else:
            self.push_screen(RecipientPicker(), self.forward_recipient_chosen)

    def open_forward_draft(self, name: str) -> None:
        if not self.forward_capture or name not in self.agents:
            return
        if name in self.forward_drafts:
            self.notify("这个成员已有未发送的转发草稿，请先处理。")
            self.forward_capture = None
            return
        self.forward_quote, self.forward_origin = self.forward_capture
        self.forward_capture = None
        self.room_screen.query_one("#selection-actions").display = False
        self.forward_target = name
        self.select(name, single=True)
        composer = self.room_screen.query_one("#message-input", ComposerInput)
        composer.value = ""
        composer.focus()
        self.refresh_forward_context()

    def refresh_forward_context(self) -> None:
        if not hasattr(self, "room_screen"):
            return
        bar = self.room_screen.query_one("#forward-context")
        bar.display = self.forward_quote is not None
        self.room_screen.query_one("#forward-return").display = self.forward_origin is not None
        if self.forward_quote:
            self.room_screen.query_one("#quote-preview", Button).label = "▸ " + self.forward_quote.title
            scope = self.room_screen.query_one("#quote-scope", Button)
            scope.display = self.forward_quote.mixed
            scope.label = "仅与来源的对话" if self.forward_quote.scope == "all" else "整个选区"

    def return_to_source(self) -> None:
        if not self.forward_origin:
            return
        origin = self.forward_origin
        composer = self.room_screen.query_one("#message-input", ComposerInput)
        if self.forward_quote:
            self.forward_drafts[self.forward_target] = (composer.prompt, self.forward_quote, self.forward_origin)
            self.forward_quote = None
        self.selected = origin["selected"] if origin["selected"] in self.agents else next(iter(self.agents), "")
        self.show_all = origin["show_all"]
        self.room_screen.clear_selection()
        self.forward_origin = None
        self.refresh_forward_context()
        composer.load_prompt(origin["prompt"])
        composer.cursor_position = origin["cursor"]
        composer.focus()
        composer.resize_to_content()
        # Filter and scroll only after the quote controls and restored draft
        # have their final sizes. Old numeric offsets point into stale content.
        self.apply_filter()

    async def member_added(self, result: tuple[str, str, str] | None) -> None:
        if result:
            await self.create_member(*result)
        self.action_composer()

    async def inherit_defaults(self, agent: Agent) -> None:
        if agent.model and agent.effort:
            return
        if agent.cwd == str(self.cwd):
            defaults = self.defaults
        elif self.client and self.connected:
            result = await self.client.request("config/read", {"cwd": agent.cwd, "includeLayers": False})
            config = result.get("config", {})
            defaults = {"model": config.get("model") or "", "effort": config.get("model_reasoning_effort") or ""}
        else:
            return
        agent.model = agent.model or defaults["model"]
        agent.effort = agent.effort or defaults["effort"]

    async def create_member(self, name: str, color: str | None = None,
                            cwd: str | Path | None = None) -> bool:
        try:
            label = validate_name(name, self.agents)
            name = label.lower() if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", label) else "member-" + uuid4().hex[:12]
            directory = resolve_directory(cwd, self.cwd)
            color = choose_color([agent.color for agent in self.agents.values()], color)
            agent = Agent(name, label, color, cwd=str(directory))
            await self.inherit_defaults(agent)
        except (ValueError, OSError, CodexError) as exc:
            self.notify(str(exc), severity="warning")
            return False
        self.agents[name] = agent
        member = Member(agent)
        await self.room_screen.query_one("#members", VerticalScroll).mount(member)
        self.select(name)
        await self.add_entry(Entry("system", f"{label} 已加入 · 工作目录：{agent.cwd}", target=name))
        self.save_state()
        return True

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "message-input":
            event.text_area.resize_to_content()
            self.refresh_completions()
            self.refresh_recipient()

    async def on_composer_input_submitted(self, event: ComposerInput.Submitted) -> None:
        if event.input.id != "message-input":
            return
        prompt = event.prompt.strip()
        value = prompt.text
        if not value:
            return
        quote = self.forward_quote
        accepted = await self.submit(value, attachments=prompt.attachments, quote=quote)
        if accepted:
            if quote:
                self.forward_quote = None
                self.forward_drafts.pop(self.forward_target, None)
                self.refresh_forward_context()
            record = prompt.dump()
            if not self.input_history or self.input_history[-1] != record:
                self.input_history.append(record)
                self.dirty = True
            event.input.reset_history_navigation()
            if event.input.value.strip() == value:
                event.input.value = ""
            self.save_state()
        self.refresh_chrome()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "respond-request" and self.selected in self.pending_interactions:
            self.run_worker(self.show_interaction(self.selected))
        elif event.button.id == "forward-selection":
            self.action_forward_selection()
        elif event.button.id == "quote-preview" and self.forward_quote:
            self.push_screen(QuotePreview(self.forward_quote), lambda _: self.action_composer())
        elif event.button.id == "quote-scope" and self.forward_quote:
            self.forward_quote.scope = "all" if self.forward_quote.scope == "source" else "source"
            self.refresh_forward_context()
            self.action_composer()
        elif event.button.id == "quote-remove":
            self.forward_quote = None
            self.forward_drafts.pop(self.forward_target, None)
            self.refresh_forward_context()
            self.action_composer()
        elif event.button.id == "forward-return":
            self.return_to_source()
        elif event.button.id == "add-member":
            self.action_add_member()
        elif event.button.id == "all-messages":
            self.action_all_messages()
        elif event.button.id in {"toggle-sidebar", "close-sidebar"}:
            self.action_toggle_sidebar()

    async def submit(self, value: str, *, attachments: list[Attachment] | None = None,
                     quote: ForwardQuote | None = None) -> bool:
        prompt = Prompt(value, attachments or [])
        if not quote and not prompt.attachments and is_room_command(value):
            return await self.command(value)
        names, body = [self.selected], value
        if value.startswith("@"):
            position = 0
            names = []
            while match := re.match(r"@([\w-]+)(?:\s+|$)", value[position:]):
                token = match.group(1)
                name = resolve_member(token, self.agents)
                if not name:
                    self.notify(f"没有 @{token}，请先创建或从补全列表选择。", severity="warning")
                    return False
                if name not in names:
                    names.append(name)
                position += match.end()
            if not names or not value[position:].strip():
                self.notify("写上 @名字 和要发送的内容。", severity="warning")
                return False
            prompt = prompt.slice(position).strip()
            body = prompt.text
        if not names or any(name not in self.agents for name in names):
            self.notify("先用 /add 名字 或 Ctrl+N 创建 Codex。", severity="warning")
            return False
        if quote and names != [self.forward_target]:
            self.notify("这份转发草稿已有接收者，请在当前对话发送。", severity="warning")
            return False
        if not quote and not prompt.attachments and is_room_command(body):
            if len(names) > 1:
                self.notify("设置命令请只指定一个成员。", severity="warning")
                return False
            accepted = await self.command(body, target=names[0])
            if accepted and names[0] in self.agents:
                self.select(names[0])
            return accepted
        if not self.connected:
            self.notify("Codex 尚未连接；连接失败时可用 /reconnect 重试。", severity="warning")
            return False
        if any(self.agents[name].parent_name for name in names):
            self.notify("原生子代理请使用默认 Rust 界面继续对话。", severity="warning")
            return False
        if any(not Path(path).is_file() for path in [*prompt.images, *(quote.images if quote else [])]):
            self.notify("图片文件已不存在，请重新粘贴；输入内容已保留。", severity="warning")
            return False
        batch_id = uuid4().hex
        for name in names:
            agent = self.agents[name]
            entry = Entry("you", prompt.expand(), name, "queued" if agent.busy else "done",
                          display_body=prompt.text if prompt.attachments else None, attachments=prompt.attachments[:],
                          quote=quote.dump() if quote else None, batch_id=batch_id, recipients=names[:])
            await self.add_entry(entry)
            agent.queue.append(entry)
            if agent.busy and name in self.active_entries:
                self.reply_boundaries.add(name)
        if self.selected != names[0]:
            self.select(names[0])
        if len(names) > 1:
            self.show_all = True
            self.apply_filter()
        followups = []
        for name in names:
            if not self.agents[name].busy:
                self.start_agent(name)
            else:
                followups.append(self.deliver_followups(name))
        if followups:
            await asyncio.gather(*followups)
        self.refresh_chrome()
        return True

    async def command(self, value: str, target: str | None = None) -> bool:
        parts = value.split(maxsplit=2)
        command = parts[0].lower()
        explicit_target = target
        target = target or self.selected
        managed = resolve_member(parts[1], self.agents) if command in {"/stop", "/del"} and len(parts) > 1 else target
        if command in {"/model", "/fast", "/permissions", "/permission", "/permisson", "/stop", "/del"} and managed in self.agents and (
                self.agents[managed].parent_name or command == "/del" and any(a.parent_name == managed for a in self.agents.values())):
            self.notify("原生子代理管理请使用默认 Rust 界面。", severity="warning")
            return False
        if command in {"/model", "/fast", "/permissions", "/permission", "/permisson"} and target not in self.agents:
            self.notify("先用 /add 名字 创建 Codex，再调整它的设置。", severity="warning")
            return False
        if command == "/add":
            try:
                options = parse_add(value, self.cwd)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return False
            if options is None:
                self.action_add_member()
            else:
                return await self.create_member(options.name, color=options.color, cwd=options.cwd)
        elif command == "/rename":
            if len(parts) == 3:
                name, label = resolve_member(parts[1], self.agents), parts[2]
            elif len(parts) == 2:
                name, label = target, parts[1]
            else:
                self.notify("用 /rename 原名 新名，或 /rename 新名 重命名当前成员。")
                return False
            if name not in self.agents:
                self.notify("未找到要重命名的成员。", severity="warning")
                return False
            try:
                label = validate_name(label, self.agents, excluding=name)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return False
            self.agents[name].label = label
            for card in self.cards.values():
                if card.entry.sender == name or card.entry.target == name:
                    card.refresh_entry()
            self.completion_signature = ()
            self.dirty = True
            self.refresh_chrome()
            self.save_state()
        elif command == "/del":
            if len(parts) > 2 or (len(parts) == 1 and not explicit_target):
                self.notify("用 /del 名字 移除成员。", severity="warning")
                return False
            name = resolve_member(parts[1], self.agents) if len(parts) > 1 else explicit_target
            if name not in self.agents:
                self.notify(f"没有 @{name}。", severity="warning")
                return False
            return await self.delete_agent(name)
        elif command in {"/focus", "/stop"}:
            if command == "/focus" and len(parts) == 2 and parts[1].casefold() == "all":
                self.action_all_messages()
                return True
            name = resolve_member(parts[1], self.agents) if len(parts) > 1 else target
            if name not in self.agents:
                self.notify(f"没有 @{name}。" if name else "请先创建 Codex。", severity="warning")
                return False
            if command == "/focus":
                self.select(name, single=True)
            else:
                await self.stop_agent(name)
        elif command == "/demo":
            self.notify("演示请用 --demo 启动，再自己创建 Codex。", severity="warning")
            return False
        elif command == "/help":
            self.action_help()
        elif command == "/members":
            self.action_toggle_sidebar()
        elif command == "/model":
            return await self.configure_model(target, parts[1:])
        elif command == "/fast":
            if not self.client or not self.connected:
                self.notify("请先连接 Codex，再切换 Fast 模式", severity="warning")
                return False
            try:
                enabled = await self.client.set_fast_mode(self.agents[target], value.split()[1:])
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return False
            self.notify(f"@{self.agents[target].label} Fast 已{'开启' if enabled else '关闭'} · 下一轮生效")
            self.save_state()
        elif command in {"/permissions", "/permission", "/permisson"}:
            if len(parts) > 1:
                mode = {"full-access": "full", "workspace-write": "workspace"}.get(parts[1], parts[1])
                if mode not in PERMISSION_LABELS:
                    self.notify("权限选项：full / workspace / read-only", severity="warning")
                    return False
                await self.change_permission(target, mode)
            else:
                self.push_screen(PermissionSettings(self.agents[target].label, self.agents[target].permission),
                                 lambda result: self.change_permission(target, result))
        elif command == "/reconnect":
            if any(agent.busy for agent in self.agents.values()):
                self.notify("请先停止工作中的成员，再重新连接。", severity="warning")
                return False
            if self.connection_task and not self.connection_task.done():
                return True
            self.connection_task = asyncio.create_task(self.connect_codex())
        elif command == "/quit":
            self.exit()
        else:
            self.notify("还没有这个指令。输入 /help 查看用法。", severity="warning")
            return False
        return True

    async def configure_model(self, name: str, arguments: list[str]) -> bool:
        if not self.client or not self.connected:
            self.notify("模型列表需要连接真实 Codex 后读取。", severity="warning")
            return False
        if arguments:
            model = arguments[0]
            effort = arguments[1] if len(arguments) > 1 else ""
            return await self.change_model(name, (model, effort))
        if not self.client.models:
            self.notify("Codex 没有返回模型列表，可用 /model 模型名 推理强度 指定。", severity="warning")
            return False
        agent = self.agents[name]
        self.push_screen(ModelSettings(agent.label, self.client.models, agent.model, agent.effort),
                         lambda result: self.change_model(name, result))
        return True

    async def change_model(self, name: str, result: tuple[str, str] | None) -> bool:
        if result is None:
            return True
        model, effort = result
        catalog = next((item for item in self.client.models if item["id"] == model), {}) if self.client else {}
        supported = [item["reasoningEffort"] for item in catalog.get("supportedReasoningEfforts", [])]
        effort = effort or catalog.get("defaultReasoningEffort") or self.agents[name].effort
        if supported and effort not in supported:
            self.notify(f"{model} 支持的推理强度：{', '.join(supported)}", severity="warning")
            return False
        if effort and effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            self.notify("无效的推理强度。", severity="warning")
            return False
        self.agents[name].model, self.agents[name].effort = model, effort
        await self.add_entry(Entry("system", f"@{self.agents[name].label} · {model} · {effort or '默认强度'} · 下一轮生效", target=name))
        self.save_state()
        self.refresh_chrome()
        self.action_composer()
        return True

    async def change_permission(self, name: str, mode: str | None) -> None:
        if mode is None:
            return
        self.agents[name].permission = mode
        await self.add_entry(Entry("system", f"@{self.agents[name].label} · {PERMISSION_LABELS[mode]} · 下一轮生效", target=name))
        self.save_state()
        self.refresh_chrome()
        self.action_composer()

    async def connect_codex(self) -> None:
        self.connected = False
        self.connection_error = ""
        try:
            if self.client:
                await self.client.close()
            self.client = CodexClient(self.cwd)
            await self.client.start()
            self.defaults = {"model": self.client.config.get("model") or "", "effort": self.client.config.get("model_reasoning_effort") or ""}
            for agent in self.agents.values():
                if agent.cwd == str(self.cwd):
                    await self.inherit_defaults(agent)
            self.connected = True
            self.dirty = True
            self.save_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connection_error = clean_error(exc)
            await self.add_entry(Entry("system", f"Codex 连接失败：{clean_error(exc)} · /reconnect 重试"))
        self.refresh_chrome()

    def save_state(self) -> None:
        if not self.store or not self.dirty:
            return
        members = [agent.config() for agent in self.agents.values()]
        try:
            self.store.save({**self.saved, "version": 2, "selected": self.selected, "show_all": self.show_all, "agents": members,
                             "entries": [asdict(entry) for entry in self.entries], "input_history": self.input_history})
            self.dirty = False
        except OSError as exc:
            self.notify(f"保存群聊失败：{exc}", severity="error")

    async def dialog(self, screen):
        future = asyncio.get_running_loop().create_future()
        def completed(result):
            if not future.done():
                future.set_result(result)
        self.push_screen(screen, completed)
        try:
            return await future
        finally:
            if future.cancelled() and screen in self.screen_stack:
                screen.dismiss(None)

    async def server_request(self, name: str, event: dict) -> None:
        assert self.client
        method, params = event["method"], event.get("params", {})
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"}:
            allowed = self.turn_permissions.get(name, self.agents[name].permission) == "full"
            if not allowed:
                allowed = await self.wait_for_interaction(name, RuntimeApproval(self.agents[name].label, params.get("command") or params.get("reason") or json.dumps(params.get("changes", params.get("permissions", {})), ensure_ascii=False)))
            result = {"permissions": params.get("permissions", {}) if allowed else {}, "scope": "turn"} if method == "item/permissions/requestApproval" else {"decision": "accept" if allowed else "decline"}
        elif method in {"item/tool/requestUserInput", "tool/requestUserInput"}:
            result = await self.wait_for_interaction(name, RuntimeQuestion(self.agents[name].label, params.get("questions", []))) or {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "cancel", "content": None}
            await self.add_entry(Entry("system", f"@{self.agents[name].label} 的 MCP 服务需要额外交互，本次请求已取消。", target=name))
        else:
            await self.client.send({"id": event["id"], "error": {"code": -32601, "message": f"Unsupported room request: {method}"}})
            await self.add_entry(Entry("system", f"@{self.agents[name].label} 发出暂不支持的交互：{method}", target=name))
            return
        await self.client.respond(event["id"], result)

    async def wait_for_interaction(self, name: str, screen: ModalScreen):
        future = asyncio.get_running_loop().create_future()
        self.pending_interactions[name] = (screen, future)
        self.agents[name].phase = "等待你回复…"
        self.refresh_chrome()
        try:
            if self.selected == name and not isinstance(self.screen, ModalScreen):
                await self.show_interaction(name)
            else:
                self.notify(f"@{self.agents[name].label} 需要你回复 · 切到该成员后点击「回复请求」", timeout=7)
            return await future
        finally:
            self.pending_interactions.pop(name, None)
            self.refresh_chrome()

    async def show_interaction(self, name: str) -> None:
        if name not in self.pending_interactions or name in self.showing_interactions:
            return
        self.showing_interactions.add(name)
        screen, future = self.pending_interactions[name]
        try:
            async with self.request_lock:
                if not future.done():
                    result = await self.dialog(screen)
                    if not future.done():
                        future.set_result(result)
        finally:
            self.showing_interactions.discard(name)

    def start_agent(self, name: str) -> None:
        self.agents[name].busy = True
        self.tasks[name] = asyncio.create_task(self.run_agent(name))

    def reply_for(self, name: str, body: str) -> str:
        return f"收到：{body}\n\n这是一条界面演示回复。你可以继续输入，测试点名、并行回复和追加消息。\n（未连接真实 Codex，未执行命令）"

    async def deliver_followups(self, name: str) -> None:
        agent = self.agents[name]
        async with agent.delivery_lock:
            while agent.queue and name in self.active_entries:
                request = agent.queue[0]
                answer = self.active_entries[name][1]
                try:
                    if self.demo:
                        agent.steered_inputs.append(request.input_text)
                    elif not self.client or not agent.thread_id or not await self.client.steer_turn(
                            agent.thread_id, request.input_text, images=request.image_paths):
                        break
                except CodexError as exc:
                    # Never silently resend a request whose acceptance is uncertain.
                    if agent.queue and agent.queue[0] is request:
                        agent.queue.popleft()
                    request.state = "failed"
                    self.cards[id(request)].refresh_entry()
                    await self.add_entry(Entry("system", f"@{agent.label} 消息追加未确认：{clean_error(exc)}。未自动重发。", target=name))
                    continue
                if not agent.queue or agent.queue[0] is not request:
                    break
                agent.queue.popleft()
                request.state = "steered"
                request.run_id = answer.run_id
                self.cards[id(request)].refresh_entry()
                # Output already rendered stays in place. Only future output
                # starts a continuation after this user message.
                self.dirty = True
        self.save_state()
        self.refresh_chrome()

    async def continuation(self, name: str) -> Entry:
        request, answer = self.active_entries[name]
        if name in self.reply_boundaries:
            self.reply_boundaries.discard(name)
            if answer.body or answer.blocks:
                answer.state = "continued"
                self.cards[id(answer)].refresh_entry()
                continued = Entry(name, "", state="working", started_at=answer.started_at, run_id=answer.run_id)
                card = await self.add_entry(continued)
                card.started_at = self.cards[id(answer)].started_at
                answer = continued
                self.active_entries[name] = (request, answer)
            else:
                # No output existed yet, so there is nothing to leave behind.
                # Move this empty shell only; it contains no historical text.
                self.entries.remove(answer)
                self.entries.append(answer)
                self.agents[name].history.remove(answer)
                self.agents[name].history.append(answer)
                timeline = self.room_screen.query_one("#timeline", VerticalScroll)
                timeline.move_child(self.cards[id(answer)], after=list(timeline.children)[-1])
        return answer

    def finish_run_tools(self, name: str, answer: Entry, status: str) -> None:
        for segment in self.agents[name].history:
            if segment.sender == name and segment.run_id == answer.run_id:
                segment.finish_tools(status)
                self.refresh_answer(segment)

    async def run_agent(self, name: str) -> None:
        agent = self.agents[name]
        answer = None
        started = None
        try:
            while True:
                async with agent.delivery_lock:
                    if not agent.queue:
                        break
                    request = agent.queue.popleft()
                self.reply_boundaries.discard(name)
                agent.steered_inputs.clear()
                request.state = "done"
                self.cards[id(request)].refresh_entry()
                agent.phase = "正在启动会话…" if not self.demo else "正在思考…"
                started = monotonic()
                answer = Entry(name, "", state="working", started_at=datetime.now().astimezone().isoformat(timespec="seconds"), run_id=uuid4().hex)
                request.run_id = answer.run_id
                self.active_entries[name] = (request, answer)
                card = await self.add_entry(answer)
                card.started_at = started
                if self.demo:
                    await self.deliver_followups(name)
                    await asyncio.sleep(0.7 * self.speed)
                    agent.phase = "正在回复…"
                    index = 0
                    segment_start = 0
                    while True:
                        reply = self.reply_for(name, "\n".join([request.input_text, *agent.steered_inputs]))
                        if index >= len(reply):
                            break
                        current = await self.continuation(name)
                        if current is not answer:
                            segment_start = index
                            answer = current
                        answer.body = reply[segment_start:index + 7]
                        index += 7
                        self.refresh_answer(answer)
                        await asyncio.sleep(0.045 * self.speed)
                    answer.state = "done"
                else:
                    answer = await self.real_reply(name, request.input_text, answer, images=request.image_paths)
                answer.record_finish(monotonic() - started)
                self.refresh_answer(answer)
                self.active_entries.pop(name, None)
                self.reply_boundaries.discard(name)
                agent.phase = "已回复，等待下一条消息"
                self.dirty = True
                self.save_state()
                if not self.show_all and self.selected != name:
                    self.notify(f"@{agent.label} 已完成 · {format_duration(monotonic() - started)}", timeout=5)
        except asyncio.CancelledError:
            if name in self.active_entries:
                answer = self.active_entries[name][1]
            if answer and answer.state == "working":
                answer.state = "stopped"
                self.finish_run_tools(name, answer, "stopped")
                if not answer.body:
                    answer.append_text("回复已停止。")
                answer.record_finish(monotonic() - started)
                self.refresh_answer(answer)
            raise
        except Exception as exc:
            if name in self.active_entries:
                answer = self.active_entries[name][1]
            message = clean_error(exc)
            if answer:
                answer.state = "failed"
                self.finish_run_tools(name, answer, "failed")
                answer.append_text(f"错误：{message}")
                answer.record_finish(monotonic() - started)
                self.refresh_answer(answer)
            agent.phase = "执行失败，等待你的下一条指令"
            while agent.queue:
                queued = agent.queue.popleft()
                queued.state = "stopped"
                self.cards[id(queued)].refresh_entry()
            if self.client and (not self.client.process or self.client.process.returncode is not None):
                self.connected = False
                self.connection_error = message
            self.active_entries.pop(name, None)
        finally:
            self.reply_boundaries.discard(name)
            agent.busy = False
            self.turn_permissions.pop(name, None)
            self.dirty = True
            self.save_state()
            self.refresh_chrome()

    def refresh_answer(self, answer: Entry) -> None:
        self.dirty = True
        if not self.is_running or self.shutting_down:
            return
        follow = (self.room_screen.query_one("#timeline", VerticalScroll).is_vertical_scroll_end
                  and not self.room_screen.selections)
        self.cards[id(answer)].refresh_entry()
        if follow:
            self.call_after_refresh(self.scroll_bottom)

    async def real_reply(self, name: str, text: str, answer: Entry, *, images: list[str] | None = None) -> Entry:
        if not self.client or not self.connected:
            raise CodexError("Codex 连接已断开，使用 /reconnect 重试。")
        agent = self.agents[name]
        settings = replace(agent)
        resolve_directory(settings.cwd, self.cwd)
        await self.inherit_defaults(settings)
        self.turn_permissions[name] = settings.permission
        await self.client.ensure_thread(settings)
        agent.thread_id = settings.thread_id
        agent.legacy_instructions = settings.legacy_instructions
        agent.model = agent.model or settings.model
        agent.effort = agent.effort or settings.effort
        agent.phase = "等待 Codex…"
        self.cards[id(answer)].refresh_working(self.frame)
        self.dirty = True
        self.save_state()
        blocks: dict[str, ReplyBlock] = {}
        owners: dict[int, Entry] = {}
        text_parts: dict[str, list[ReplyBlock]] = {}
        summaries: dict[tuple[str, int], str] = {}

        def block_for(item_id: str, kind: str) -> ReplyBlock:
            if item_id not in blocks:
                blocks[item_id] = ReplyBlock(item_id, kind)
                answer.blocks.append(blocks[item_id])
                owners[id(blocks[item_id])] = answer
            return blocks[item_id]

        async def receive(event: dict) -> None:
            nonlocal answer
            if "id" in event:
                agent.phase = "等待交互…"
                self.cards[id(answer)].refresh_working(self.frame)
                await self.server_request(name, event)
                agent.phase = "正在继续执行…"
                self.cards[id(answer)].refresh_working(self.frame)
                return
            method, params = event["method"], event.get("params", {})
            item = params.get("item", {})
            item_id = params.get("itemId") or item.get("id", "")
            kind = item.get("type")
            known_text = ("".join(part.text for part in text_parts.get(item_id, [blocks[item_id]]))
                          if method == "item/completed" and kind == "agentMessage" and item_id in blocks else None)
            completed_extension = (known_text is not None and item.get("text", "").startswith(known_text)
                                   and item.get("text", "") != known_text)
            visible_new = (method == "item/agentMessage/delta" or
                (method in {"item/started", "item/completed"} and item_id not in blocks
                 and kind not in {None, "reasoning", "contextCompaction", "userMessage"}) or
                method == "error" and not params.get("willRetry") or completed_extension)
            if visible_new:
                current = await self.continuation(name)
                if ((method == "item/agentMessage/delta" or completed_extension) and item_id in blocks
                        and owners[id(blocks[item_id])] is not current):
                    prior = blocks.pop(item_id)
                    text_parts.setdefault(item_id, [prior])
                    prior.status = "completed"
                answer = current
            if method == "item/reasoning/summaryPartAdded":
                summaries[(item_id, params.get("summaryIndex", 0))] = ""
                agent.phase = "正在思考…"
            elif method == "item/reasoning/summaryTextDelta":
                key = (item_id, params.get("summaryIndex", 0))
                summaries[key] = (summaries.get(key, "") + params.get("delta", ""))[:512]
                title = summary_title(summaries[key])
                agent.phase = f"正在思考：{title}" if title else "正在思考…"
            elif method == "item/agentMessage/delta":
                agent.phase = "正在回复…"
                block_for(item_id, "text").text += params.get("delta", "")
                block = blocks[item_id]
                parts = text_parts.setdefault(item_id, [])
                if not any(part is block for part in parts):
                    parts.append(block)
            elif method in {"item/started", "item/completed"}:
                kind = item.get("type")
                if kind == "agentMessage" and item.get("text"):
                    agent.phase = "正在回复…"
                    block = block_for(item_id, "text")
                    prefix = sum(len(part.text) for part in text_parts.get(item_id, []) if part is not block)
                    block.text = item["text"][prefix:]
                    block.status = "inProgress" if method == "item/started" else "completed"
                elif kind == "commandExecution":
                    block = block_for(item_id, "command")
                    block.title = item.get("command", block.title)
                    agent.phase = "正在执行命令…" if method == "item/started" else "正在分析命令结果…"
                    if item.get("aggregatedOutput") is not None:
                        block.text = item["aggregatedOutput"]
                    block.exit_code = item.get("exitCode")
                    block.status = item.get("status") or ("inProgress" if method == "item/started" else "completed")
                    if block.exit_code not in {None, 0}:
                        block.status = "failed"
                elif kind == "fileChange":
                    block = block_for(item_id, "file")
                    paths = [change.get("path", "") for change in item.get("changes", [])]
                    block.title = ", ".join(paths)
                    block.text = json.dumps(item.get("changes", []), ensure_ascii=False, indent=2)
                    block.status = item.get("status") or ("inProgress" if method == "item/started" else "completed")
                    agent.phase = "正在修改文件…"
                elif kind in {"reasoning", "contextCompaction"}:
                    title = summary_title(next((part for part in reversed(item.get("summary", [])) if part.strip()), ""))
                    agent.phase = (f"正在思考：{title}" if title else "正在思考…") if kind == "reasoning" else "正在整理上下文…"
                elif kind and kind not in {"userMessage", "agentMessage"}:
                    block = block_for(item_id, "tool")
                    block.title = "/".join(filter(None, (item.get("server"), item.get("tool")))) or item.get("query") or item.get("path") or kind
                    block.text = json.dumps(item, ensure_ascii=False, indent=2)
                    block.status = item.get("status") or ("inProgress" if method == "item/started" else "completed")
                    if item.get("success") is False or item.get("error"):
                        block.status = "failed"
                    agent.phase = "正在调用工具…" if method == "item/started" else "正在分析工具结果…"
            elif method == "item/commandExecution/outputDelta":
                block_for(item_id, "command").text += params.get("delta", "")
            elif method == "error":
                if params.get("willRetry"):
                    agent.phase = "连接中断，正在重试…"
                else:
                    error = params.get("error") or {}
                    block_for("error", "text").text = "错误：" + clean_error(error.get("message", error))
            if blocks:
                # A command can complete after a followup. Its output belongs
                # to the card where that command began.
                changed = blocks.get(item_id)
                owner = owners.get(id(changed), answer)
                owner.sync_body()
                self.refresh_answer(owner)
                if owner is not answer:
                    self.cards[id(answer)].refresh_working(self.frame)
            else:
                self.cards[id(answer)].refresh_working(self.frame)

        turn = await self.client.run_turn(settings, text, receive, on_started=lambda: self.deliver_followups(name), images=images)
        answer.state = "stopped" if turn.get("status") == "interrupted" else "done"
        self.finish_run_tools(name, answer, "stopped" if answer.state == "stopped" else "completed")
        if not answer.body and not answer.blocks:
            answer.append_text("回复已停止。" if answer.state == "stopped" else "本轮已完成。")
        self.refresh_answer(answer)
        return answer

    async def delete_agent(self, name: str) -> bool:
        agent = self.agents[name]
        task = self.tasks.get(name)
        if agent.busy or agent.queue or (task and not task.done()):
            await self.stop_agent(name)
        else:
            self.tasks.pop(name, None)
        removed = [entry for entry in self.entries if entry.sender == name or entry.target == name]
        if self.store:
            try:
                self.store.archive_member(name, {"version": 2, "archived_at": datetime.now().astimezone().isoformat(),
                                                 "agent": agent.config(), "entries": [asdict(entry) for entry in removed]})
            except OSError as exc:
                self.notify(f"归档失败，已保留 @{agent.label}：{exc}", severity="error")
                return False

        self.close_completions()
        self.room_screen.clear_selection()
        # Keep member metadata until its mounted cards have been removed.
        removed_ids = {id(entry) for entry in removed}
        self.entries = [entry for entry in self.entries if id(entry) not in removed_ids]
        self.batch_heads.clear()
        for entry in self.entries:
            if entry.sender == "you" and entry.batch_id:
                self.batch_heads.setdefault(entry.batch_id, entry.id)
        await self.room_screen.query_one("#timeline", VerticalScroll).remove_children(
            [self.cards.pop(id(entry)) for entry in removed]
        )
        if self.selected == name:
            self.selected = next((other for other in self.agents if other != name), "")
        await self.room_screen.query_one("#" + Member.widget_id(name), Member).remove()
        del self.agents[name]
        self.forward_drafts.pop(name, None)
        if self.forward_target == name:
            self.forward_quote = None
            self.forward_target = ""
        if self.forward_origin and self.forward_origin["selected"] == name:
            self.forward_origin = None
        self.refresh_forward_context()
        if not self.agents:
            self.show_all = True
        if self.client and agent.thread_id:
            self.client.loaded.discard(agent.thread_id)
        self.dirty = True
        self.apply_filter()
        await self.add_entry(Entry("system", f"已移除 @{agent.label}" + (" · 会话记录已归档" if self.store else "")))
        self.save_state()
        self.action_composer()
        self.refresh_completions()
        return True

    async def stop_agent(self, name: str) -> None:
        agent = self.agents[name]
        task = self.tasks.pop(name, None)
        cancelled = len(agent.queue)
        while agent.queue:
            request = agent.queue.popleft()
            request.state = "stopped"
            self.cards[id(request)].refresh_entry()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        active = self.active_entries.pop(name, None)
        if active:
            active[1].state = "stopped"
            active[1].finish_tools("stopped")
            if not active[1].body:
                active[1].append_text("回复已停止。")
            self.cards[id(active[1])].refresh_entry()
        agent.busy = False
        agent.phase = "已停止，等待下一条消息"
        if self.client and self.client.process and self.client.process.returncode is not None:
            self.connected = False
            self.connection_error = "Codex 连接已关闭，可用 /reconnect 重新连接。"
        await self.add_entry(Entry("system", f"@{agent.label} 已停止；取消 {cancelled} 条尚未发出的消息。", target=name))
        self.refresh_chrome()

    async def on_unmount(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        tasks = list(self.tasks.values())
        if self.connection_task:
            tasks.append(self.connection_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.close()
        self.save_state()
        if self.store:
            self.store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="一个终端，多个独立的 Codex 会话")
    parser.add_argument("--demo", action="store_true", help="使用模拟回复预览界面，不连接 Codex")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Codex 工作目录，默认当前目录")
    args = parser.parse_args()
    if not args.cwd.is_dir():
        parser.error("工作目录不存在")
    try:
        app = CodexRoom(demo=args.demo, cwd=args.cwd)
        app.run()
    except (RuntimeError, OSError, ValueError) as exc:
        parser.exit(1, f"codex-room: {exc}\n")


if __name__ == "__main__":
    main()
