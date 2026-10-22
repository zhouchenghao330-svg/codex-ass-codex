"""Keep reply text separate from the tool transcript shown on demand."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from rich.console import Group
from rich.markdown import CodeBlock, Heading, Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from .colors import CODE_BACKGROUND, FOREGROUND, LINK, MUTED, member_colors
from .attachments import Attachment
from .tool_details import command_title, render_tool_group, tool_summary


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
    "markdown.code": f"{FOREGROUND} on {CODE_BACKGROUND}",
    **{f"markdown.h{level}": f"bold {FOREGROUND}" for level in range(1, 7)},
    "markdown.block_quote": MUTED,
    "markdown.link": f"underline {LINK}",
    "markdown.link_url": f"underline {LINK}",
    "markdown.item.number": FOREGROUND,
    "markdown.table.border": MUTED,
    "markdown.table.header": f"bold {FOREGROUND}",
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
        self.code_background = CODE_BACKGROUND
        text_color = FOREGROUND
        if color:
            colors = member_colors(color)
            text_color = colors.text
            self.code_background = colors.code_background
            self.reply_theme = Theme({**MARKDOWN_THEME.styles,
                "markdown.code": f"{colors.text} on {colors.code_background}",
                "markdown.block_quote": colors.muted,
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
    display_body: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    collapsed: bool = False
    id: str = field(default_factory=lambda: uuid4().hex)
    run_id: str = ""
    batch_id: str = ""
    quote: dict | None = None
    recipients: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.blocks = [ReplyBlock(**block) if isinstance(block, dict) else block for block in self.blocks]
        self.attachments = [Attachment(**item) if isinstance(item, dict) else item for item in self.attachments]

    @property
    def image_paths(self) -> list[str]:
        paths = [item.content for item in self.attachments if item.kind == "image"]
        if self.quote:
            from .forwarding import ForwardQuote
            paths.extend(ForwardQuote.load(self.quote).images)
        return list(dict.fromkeys(paths))

    @property
    def input_text(self) -> str:
        if not self.quote:
            return self.body
        from .forwarding import ForwardQuote
        return ForwardQuote.load(self.quote).with_instruction(self.body)

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


def render_reply(entry: Entry, expanded: bool, width: int, color: str | None = None, *,
                 group_levels: dict[int, int] | None = None,
                 expanded_items: dict[tuple[int, int], bool] | None = None, interactive: bool = False) -> Group:
    if not entry.blocks:
        return Group(ReplyMarkdown(entry.body or "正在整理思路…", color))
    rendered = []
    sources = []
    index = 0
    while index < len(entry.blocks):
        block = entry.blocks[index]
        if block.kind == "text":
            if block.text:
                if rendered:
                    rendered.append(Text(""))
                    sources.append(())
                prose = ReplyMarkdown(block.text, color)
                rendered.append(prose)
                sources.append((index,))
            index += 1
            continue
        group = []
        start = index
        while index < len(entry.blocks) and entry.blocks[index].kind != "text":
            group.append(entry.blocks[index])
            index += 1
        if rendered:
            rendered.append(Text(""))
            sources.append(())
        level = (group_levels or {}).get(start, 2 if expanded else 0)
        tool_sources = []
        tools = list(render_tool_group(group, start, level, width, expanded_items or {}, interactive, tool_sources))
        rendered.extend(tools)
        sources.extend(tool_sources)
    group = Group(*rendered)
    group.room_sources = sources
    return group
