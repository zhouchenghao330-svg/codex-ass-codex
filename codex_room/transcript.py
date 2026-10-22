"""Keep reply text separate from the tool transcript shown on demand."""
from __future__ import annotations


from rich.console import Group
from rich.markdown import CodeBlock, Heading, Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

from .colors import CODE_BACKGROUND, FOREGROUND, LINK, MUTED, member_colors
from .models import Entry, ReplyBlock
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
