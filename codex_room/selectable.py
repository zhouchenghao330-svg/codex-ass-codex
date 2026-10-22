"""Add selection coordinates and highlighting to rendered Markdown/tool output."""
from __future__ import annotations

from rich.segment import Segment
from rich.style import Style as RichStyle
from textual.selection import Selection
from textual.strip import Strip
from textual.visual import RichVisual, Visual
from textual.widgets import Static


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

    def get_optimal_width(self, rules, container_width):
        return self.source.get_optimal_width(rules, container_width)

    def get_height(self, rules, width):
        return self.source.get_height(rules, width)

    def render_strips(self, width, height, style, options):
        strips = self.source.render_strips(width, height, style, options)
        self.lines = tuple(strip.text.rstrip() for strip in strips)
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
        return SelectableVisual(visual) if isinstance(visual, RichVisual) else visual

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if not self.display:
            return None
        visual = self._render()
        if not isinstance(visual, SelectableVisual):
            return super().get_selection(selection)
        if not visual.lines:
            Visual.to_strips(self, visual, self.size.width, None, self.visual_style, apply_selection=False)
        return selection.extract("\n".join(visual.lines)), "\n"
