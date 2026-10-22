"""Add selection coordinates and highlighting to rendered Markdown/tool output."""
from __future__ import annotations

from rich.segment import Segment
from rich.style import Style as RichStyle
from textual import events
from textual.geometry import Offset
from textual.selection import Selection
from textual.strip import Strip
from textual.visual import RichVisual, Visual
from textual.widgets import Static
from .rendering import ReplyVisual


# Match the default word separators in Orca's bundled xterm terminal.
WORD_SEPARATORS = frozenset(" ()[]{}',\"`")


def word_span(line: str, column: int) -> tuple[int, int]:
    if not line:
        return 0, 0
    start = end = min(max(0, column), len(line) - 1)
    if line[start] == " ":
        while start > 0 and line[start - 1] == " ":
            start -= 1
        while end + 1 < len(line) and line[end + 1] == " ":
            end += 1
    else:
        while start > 0 and line[start - 1] not in WORD_SEPARATORS:
            start -= 1
        while end + 1 < len(line) and line[end + 1] not in WORD_SEPARATORS:
            end += 1
    return start, end + 1


def highlight(strip: Strip, start: int, end: int, style: RichStyle) -> Strip:
    """Selection offsets count Unicode characters, not terminal cells."""
    segments = []
    position = 0
    for segment in strip:
        text, original, control = segment
        cuts = sorted({0, len(text), max(0, min(len(text), start - position)),
                       max(0, min(len(text), end - position))})
        for left, right in zip(cuts, cuts[1:]):
            selected = start <= position + left < end
            selected_style = (original or RichStyle.null()) + style if selected else original
            segments.append(Segment(text[left:right], selected_style, control))
        position += len(text)
    return Strip(segments, strip.cell_length)


class SelectableVisual(Visual):
    def __init__(self, source: Visual) -> None:
        self.source = source
        self.lines: tuple[str, ...] = ()
        self._raw_strips = None
        self._offset_strips = None

    def get_optimal_width(self, rules, container_width):
        return self.source.get_optimal_width(rules, container_width)

    def get_height(self, rules, width):
        return self.source.get_height(rules, width)

    def render_strips(self, width, height, style, options):
        strips = self.source.render_strips(width, height, style, options)
        if strips is not self._raw_strips:
            self._raw_strips = strips
            self.lines = tuple(strip.text.rstrip() for strip in strips)
            self._offset_strips = [strip.apply_offsets(0, y) for y, strip in enumerate(strips)]
        if options.selection is None:
            return self._offset_strips
        result = []
        for y, strip in enumerate(strips):
            if options.selection is not None and options.selection_style is not None:
                span = options.selection.get_span(y)
                if span is not None:
                    start, end = span
                    end = len(strip.text) if end < 0 else end
                    strip = highlight(strip, start, end, options.selection_style.rich_style)
            result.append(strip.apply_offsets(0, y))
        return result


class SelectableStatic(Static):
    """Use the displayed text for selection, retaining Rich colors and links."""

    def render(self):
        visual = super().render()
        return SelectableVisual(visual) if isinstance(visual, (RichVisual, ReplyVisual)) else visual

    def selection_lines(self) -> tuple[str, ...]:
        visual = self._render()
        if isinstance(visual, SelectableVisual):
            if not visual.lines:
                Visual.to_strips(self, visual, self.size.width, None, self.visual_style, apply_selection=False)
            return visual.lines
        return tuple(str(visual).splitlines())

    async def _on_click(self, event: events.Click) -> None:
        if event.widget is not self or event.button != 1 or event.chain not in {2, 3}:
            return
        # Textual's default double/triple click selects a widget or its entire
        # container. Handle these here so neither can select a whole reply.
        event.prevent_default()
        event.stop()
        widget, offset = self.screen.get_widget_and_offset_at(*event.screen_offset)
        lines = self.selection_lines()
        self.screen.clear_selection()
        if widget is not self or offset is None or offset.y >= len(lines):
            return
        line = lines[offset.y]
        if event.chain == 2 and offset.x >= len(line):
            return
        start, end = word_span(line, offset.x) if event.chain == 2 else (0, len(line))
        if start < end:
            self.screen.selections = {self: Selection(Offset(start, offset.y), Offset(end, offset.y))}

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if not self.display:
            return None
        visual = self._render()
        if not isinstance(visual, SelectableVisual):
            return super().get_selection(selection)
        return selection.extract("\n".join(self.selection_lines())), "\n"
