"""Keep reply text separate from the tool transcript shown on demand."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime

from rich.console import Group
from rich.markdown import CodeBlock, Heading, Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from .colors import member_colors


MUTED = "#788294"
FAILED = {"failed", "declined"}


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if seconds < 1:
        return "<1s"
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


MARKDOWN_THEME = Theme({
    "markdown.code": "#78d8bc on #2a3128",
    "markdown.h1": "bold #a9b6ff",
    "markdown.h2": "bold #a9b6ff",
    "markdown.h3": "bold #c5cdd9",
    "markdown.h4": "bold #c5cdd9",
    "markdown.h5": "bold #c5cdd9",
    "markdown.h6": "bold #788294",
    "markdown.block_quote": "#788294",
    "markdown.link_url": "underline #83c8e8",
    "markdown.table.border": "#788294",
    "markdown.table.header": "bold #c5cdd9",
})


class ReplyHeading(Heading):
    LEVEL_ALIGN = {**Heading.LEVEL_ALIGN, "h1": "left"}


class ReplyCodeBlock(CodeBlock):
    @classmethod
    def create(cls, markdown, token):
        block = super().create(markdown, token)
        block.background = markdown.code_background
        return block

    def __rich_console__(self, console, options):
        yield Syntax(str(self.text).rstrip("\n"), self.lexer_name, theme=self.theme,
                     background_color=self.background, word_wrap=True, padding=(0, 1))


class ReplyMarkdown(Markdown):
    """Terminal Markdown with compact spacing and the room's existing colors."""

    elements = {**Markdown.elements, "heading_open": ReplyHeading,
                "fence": ReplyCodeBlock, "code_block": ReplyCodeBlock}

    def __init__(self, text: str, color: str | None = None) -> None:
        self.reply_theme = MARKDOWN_THEME
        self.code_background = "#262d24"
        text_color = "#c5cdd9"
        if color:
            colors = member_colors(color)
            text_color = colors.text
            self.code_background = colors.code_background
            self.reply_theme = Theme({**MARKDOWN_THEME.styles,
                **{f"markdown.h{level}": f"bold {colors.accent}" for level in range(1, 7)},
                "markdown.code": f"{colors.text} on {colors.code_background}",
                "markdown.block_quote": colors.muted,
                "markdown.link_url": f"underline {colors.accent}",
                "markdown.item.number": colors.accent,
                "markdown.table.border": colors.muted,
                "markdown.table.header": f"bold {colors.text}",
            })
        super().__init__(text, style=text_color, justify="left")

    def __rich_console__(self, console, options):
        with console.use_theme(self.reply_theme):
            yield from super().__rich_console__(console, options)


@dataclass
class ReplyBlock:
    id: str
    kind: str
    text: str = ""
    title: str = ""
    status: str = "inProgress"
    exit_code: int | None = None


@dataclass
class Entry:
    sender: str
    body: str
    target: str = ""
    state: str = "done"
    time: str = field(default_factory=lambda: datetime.now().strftime("%H:%M"))
    blocks: list[ReplyBlock] = field(default_factory=list)
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        self.blocks = [ReplyBlock(**block) if isinstance(block, dict) else block for block in self.blocks]

    def sync_body(self) -> None:
        self.body = "\n\n".join(block.text for block in self.blocks if block.kind == "text" and block.text)

    def record_finish(self, elapsed: float) -> None:
        if self.finished_at is None:
            self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.duration_seconds = round(max(0, elapsed), 3)

    def append_text(self, text: str) -> None:
        if self.blocks:
            self.blocks.append(ReplyBlock(f"notice-{len(self.blocks)}", "text", text))
            self.sync_body()
        else:
            self.body = (self.body + "\n\n" if self.body else "") + text

    def finish_tools(self, status: str) -> None:
        for block in self.blocks:
            if block.kind != "text" and block.status == "inProgress":
                block.status = status

    def restore_legacy_tools(self) -> None:
        """Recover completed command blocks emitted by the old room renderer."""
        if self.blocks or self.sender in {"you", "system"}:
            return
        pattern = re.compile(
            r"(?:\A|\n\n)\$ (?P<command>[^\n]+)\n"
            r"(?:(?P<output>.*?)\n)?\[退出码 (?P<code>None|-?\d+)\](?=\n\n|\Z)", re.DOTALL
        )
        end = 0
        blocks = []
        for match in pattern.finditer(self.body):
            if text := self.body[end:match.start()].strip():
                blocks.append(ReplyBlock(f"legacy-{len(blocks)}", "text", text))
            code = None if match["code"] == "None" else int(match["code"])
            blocks.append(ReplyBlock(f"legacy-{len(blocks)}", "command", match["output"] or "",
                                     match["command"], "failed" if code not in {0, None} else "completed", code))
            end = match.end()
        if blocks:
            if text := self.body[end:].strip():
                blocks.append(ReplyBlock(f"legacy-{len(blocks)}", "text", text))
            self.blocks = blocks
            self.sync_body()


def command_title(command: str) -> str:
    try:
        args = shlex.split(command)
        if len(args) >= 3 and args[0].rsplit("/", 1)[-1] in {"sh", "bash", "zsh", "fish"} and "c" in args[1]:
            command = args[2]
    except ValueError:
        pass
    return " ".join(command.split())


def tool_summary(blocks: list[ReplyBlock], expanded: bool, width: int) -> Text:
    running = any(block.status == "inProgress" for block in blocks)
    failed = sum(block.status in FAILED for block in blocks)
    stopped = any(block.status == "stopped" for block in blocks)
    if len(blocks) == 1:
        block = blocks[0]
        verb = {"command": "执行命令", "file": "修改文件", "tool": "调用工具"}.get(block.kind, "调用工具")
        label = f"{verb} · {command_title(block.title) if block.kind == 'command' else block.title}"
    else:
        noun = "个命令" if all(block.kind == "command" for block in blocks) else "次工具调用"
        label = f"{len(blocks)} {noun}"
    status = " · 执行中" if running else (f" · {failed} 项失败" if failed else (" · 已停止" if stopped else " · 已完成"))
    hint = "Ctrl+T 收起" if expanded else "Ctrl+T 展开"
    suffix = status + f"  ({hint})"
    line = Text(("▾ " if expanded else "▸ ") + label, style="#e8b878" if failed else MUTED)
    line.truncate(max(4, width - Text(suffix).cell_len), overflow="ellipsis")
    line.append(suffix)
    return line


def render_reply(entry: Entry, expanded: bool, width: int, color: str | None = None) -> Group:
    if not entry.blocks:
        return Group(ReplyMarkdown(entry.body or "正在整理思路…", color))
    rendered = []
    index = 0
    while index < len(entry.blocks):
        block = entry.blocks[index]
        if block.kind == "text":
            if block.text:
                if rendered:
                    rendered.append(Text(""))
                rendered.append(ReplyMarkdown(block.text, color))
            index += 1
            continue
        group = []
        while index < len(entry.blocks) and entry.blocks[index].kind != "text":
            group.append(entry.blocks[index])
            index += 1
        if rendered:
            rendered.append(Text(""))
        details = tool_summary(group, expanded, width)
        if expanded:
            for tool in group:
                title = f"$ {tool.title}" if tool.kind == "command" else tool.title
                details.append(f"\n  {title}", style=MUTED)
                if tool.text:
                    details.append("\n" + tool.text.rstrip(), style=MUTED)
                if tool.kind == "command" and tool.exit_code is not None:
                    details.append(f"\n  [退出码 {tool.exit_code}]", style=MUTED)
        rendered.append(details)
    return Group(*rendered)
