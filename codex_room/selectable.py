"""Add selection coordinates and highlighting to rendered Markdown/tool output."""
from __future__ import annotations

from dataclasses import replace

from rich.cells import get_character_cell_size
from rich.segment import Segment
from rich.style import Style as RichStyle
from textual import events
from textual.geometry import Offset, Region
from textual.reactive import var
from textual.screen import Screen
from textual.selection import Selection, SelectState
from textual.strip import Strip
from textual.visual import RichVisual, Visual
from textual.widget import Widget
from textual.widgets import Static
from .rendering import CachedVisual, ReplyVisual


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
        self.source = CachedVisual(source) if isinstance(source, RichVisual) else source
        self.lines: tuple[str, ...] = ()
        self._raw_strips = None
        self._offset_strips = None
        self._highlighted_lines = {}

    def get_optimal_width(self, rules, container_width):
        return self.source.get_optimal_width(rules, container_width)

    def get_height(self, rules, width):
        return self.source.get_height(rules, width)

    def render_strips(self, width, height, style, options):
        strips = self.source.render_strips(width, height, style,
            replace(options, selection=None, selection_style=None))
        if strips is not self._raw_strips:
            self._raw_strips = strips
            self.lines = tuple(strip.text.rstrip() for strip in strips)
            self._offset_strips = [strip.apply_offsets(0, y) for y, strip in enumerate(strips)]
            self._highlighted_lines.clear()
        if options.selection is None or options.selection_style is None:
            return self._offset_strips
        # A pointer movement must not recreate offsets for thousands of untouched
        # output lines. Keep the base strips and only replace the selected rows.
        result = self._offset_strips.copy()
        selection = options.selection
        first = max(0, selection.start.y) if selection.start is not None else 0
        last = min(len(strips), selection.end.y + 1) if selection.end is not None else len(strips)
        rich_style = options.selection_style.rich_style
        for y in range(first, last):
            start, end = selection.get_span(y)
            end = len(strips[y].text) if end < 0 else end
            key = (start, end, rich_style)
            cached = self._highlighted_lines.get(y)
            if cached is None or cached[0] != key:
                cached = (key, highlight(strips[y], start, end, rich_style).apply_offsets(0, y))
                self._highlighted_lines[y] = cached
            result[y] = cached[1]
        return result


class SelectableStatic(Static):
    """Use the displayed text for selection, retaining Rich colors and links."""

    def render(self):
        visual = super().render()
        if not isinstance(visual, (RichVisual, ReplyVisual)):
            return visual
        cached = getattr(self, "_selectable_visual", None)
        if cached is None or cached[0] is not visual:
            cached = self._selectable_visual = (visual, SelectableVisual(visual))
        return cached[1]

    def selection_updated(self, selection: Selection | None) -> None:
        previous = getattr(self, "_last_selection", None)
        self._last_selection = selection
        if previous == selection:
            return
        if not isinstance(self._render(), SelectableVisual):
            # Textual Content keeps logical offsets across wrapped lines; its
            # selected rows need not have the same y as their displayed rows.
            super().selection_updated(selection)
            return
        boundaries = {0, self.size.height}
        for selected in (previous, selection):
            if selected is None:
                continue
            for offset in selected:
                if offset is not None:
                    boundaries.update(max(0, min(self.size.height, y)) for y in (offset.y, offset.y + 1))
        boundaries = sorted(boundaries)
        regions = []
        # Spans are constant between their start/end rows. A small endpoint
        # movement should not dirty the unchanged middle of a long selection.
        for first, last in zip(boundaries, boundaries[1:]):
            before = previous.get_span(first) if previous is not None else None
            after = selection.get_span(first) if selection is not None else None
            if before != after:
                regions.append(Region(0, first, self.size.width, last - first))
        if regions:
            self.refresh(*regions)

    def character_offset_at(self, x: int, y: int) -> Offset | None:
        """Map both cells of a wide glyph to that glyph, without rounding up."""
        x -= self.content_region.x
        y -= self.content_region.y
        if x < 0 or y < 0 or y >= self.size.height:
            return None
        cell = 0
        end = None
        for segment in self.render_line(y):
            offset = segment.style.meta.get("offset") if segment.style is not None else None
            for index, character in enumerate(segment.text):
                next_cell = cell + get_character_cell_size(character)
                if offset is not None and offset[1] is not None:
                    end = Offset(offset[0] + index + 1, offset[1])
                    if cell <= x < next_cell:
                        return Offset(offset[0] + index, offset[1])
                cell = next_cell
        if end is None and isinstance(self._render(), SelectableVisual):
            # An empty rendered row is still a precise endpoint. Returning None
            # makes Textual fall back to selecting the whole content widget.
            return Offset(0, y)
        return end

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
        lines = self.selection_lines()
        start = selection.start or Offset(0, 0)
        end = selection.end or Offset(len(lines[-1]) if lines else 0, len(lines) - 1)
        selected = list(lines[start.y:end.y + 1])
        if not selected:
            return "", "\n"
        if len(selected) == 1:
            return selected[0][start.x:end.x] if start.y == end.y else selected[0][start.x:], "\n"
        selected[0] = selected[0][start.x:]
        if end.y < len(lines):
            selected[-1] = selected[-1][:end.x]
        return "\n".join(selected), "\n"


class ConversationScreen(Screen):
    """Use character boundaries for dragging, and clear on an outside press."""

    # Screen's default reactive repaints the entire terminal for every pointer
    # movement. The selection watcher already refreshes the affected text rows.
    _select_state: var[SelectState | None] = var(None)
    # Focus styles and cursor visibility already repaint their own widgets.
    # Moving focus out of the composer must not redraw the whole transcript.
    focused: var[Widget | None] = var(None)

    def get_widget_and_offset_at(self, x: int, y: int):
        widget, offset = super().get_widget_and_offset_at(x, y)
        if isinstance(widget, SelectableStatic):
            offset = widget.character_offset_at(x, y)
        return widget, offset

    def _inside_selection(self, x: int, y: int) -> bool:
        widget, offset = self.get_widget_and_offset_at(x, y)
        selection = self.selections.get(widget)
        if (selection is None or offset is None
                or Offset(x, y) not in widget.content_region):
            return False
        span = selection.get_span(offset.y)
        if span is None:
            return False
        start, end = span
        if end < 0:
            # A click in the blank space after the text is outside the highlight.
            lines = widget.selection_lines() if isinstance(widget, SelectableStatic) else ()
            end = len(lines[offset.y]) if offset.y < len(lines) else 0
        return start <= offset.x < end

    def _forward_event(self, event: events.Event) -> None:
        if not event.is_forwarded:
            if isinstance(event, events.MouseDown) and event.button == 1:
                self._selection_press = None
                if self.selections:
                    if self._inside_selection(event.screen_x, event.screen_y):
                        self._selection_press = (event.screen_offset, self.selections.copy())
                    else:
                        self.clear_selection()
            elif isinstance(event, events.MouseUp) and event.button == 1:
                press = getattr(self, "_selection_press", None)
                self._selection_press = None
                if press is not None and press[0] == event.screen_offset:
                    self.selections = press[1]
                    self._mouse_down_offset = None
        super()._forward_event(event)

    def _watch__select_state(self, state: SelectState | None) -> None:
        if state is None:
            self._selecting = False
            return
        if (state is not None and state.end is not None
                and state.is_single_content_widget and state.is_attached_to_dom()):
            self._selecting = True
            start, end = state.content_offsets
            self.selections = ({state.start.content_widget: Selection(start, end)}
                               if start != end else {})
            return
        super()._watch__select_state(state)
