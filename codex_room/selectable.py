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
from textual.selection import SELECT_ALL, SelectEnd, Selection, SelectState
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
        if end is None:
            # Native Content uses logical line offsets, including empty lines
            # between wrapped paragraphs in the user's messages.
            for previous_y in range(y - 1, -1, -1):
                for segment in self.render_line(previous_y):
                    previous = segment.style.meta.get("offset") if segment.style is not None else None
                    if previous is not None and previous[1] is not None:
                        return Offset(0, previous[1] + y - previous_y)
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
        start = selection.start if selection.start is not None else Offset(0, 0)
        end = selection.end if selection.end is not None else Offset(len(lines[-1]) if lines else 0, len(lines) - 1)
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

    def on_mount(self) -> None:
        self.screen_layout_refresh_signal.subscribe(self, self._update_select)

    @staticmethod
    def _timeline_for(widget: Widget | None) -> Widget | None:
        if widget is not None:
            return next((node for node in widget.ancestors_with_self if node.id == "timeline"), None)
        return None

    @staticmethod
    def _chat_widgets(timeline: Widget) -> list[SelectableStatic]:
        # Displayed means part of the conversation, including content scrolled
        # out of view. Hidden member conversations and collapsed tools stay out.
        return [widget for widget in timeline.query(SelectableStatic)
                if widget.display and widget.visible and widget.size.height
                and all(parent.display for parent in widget.ancestors)]

    def _clamp_to_chat(self, timeline: Widget, x: int, y: int) -> Offset:
        region = timeline.content_region.intersection(self.region)
        return Offset(max(region.x, min(region.right - 1, x)),
                      max(region.y, min(region.bottom - 1, y)))

    @staticmethod
    def _text_end(widget: SelectableStatic) -> Offset:
        lines = widget.selection_lines()
        return Offset(len(lines[-1]), len(lines) - 1) if lines else Offset(0, 0)

    def get_widget_and_offset_at(self, x: int, y: int):
        timeline = getattr(self, "_drag_timeline", None) if self._selecting else None
        if timeline is not None:
            x, y = self._clamp_to_chat(timeline, x, y)
        widget, offset = super().get_widget_and_offset_at(x, y)
        timeline = timeline or self._timeline_for(widget)
        if timeline is None:
            return widget, offset
        if isinstance(widget, SelectableStatic):
            offset = widget.character_offset_at(x, y)
            if offset is not None:
                return widget, offset
        # Borders, message gaps and the scrollbar are boundaries between chat
        # text, rather than invitations to select their whole parent container.
        previous = None
        for candidate in self._chat_widgets(timeline):
            region = candidate.content_region
            if y < region.y:
                return ((previous, self._text_end(previous)) if previous is not None
                        else (candidate, Offset(0, 0)))
            if y < region.bottom:
                offset = candidate.character_offset_at(max(region.x, min(region.right - 1, x)), y)
                return candidate, offset or Offset(0, 0)
            previous = candidate
        return (previous, self._text_end(previous)) if previous is not None else (None, None)

    def _select_all_in_widget(self, widget: Widget) -> None:
        if self._timeline_for(widget) is not None:
            super()._select_all_in_widget(widget)

    def _check_auto_scroll(self, select_widget, mouse_coordinate, delta_y) -> None:
        timeline = getattr(self, "_drag_timeline", None)
        if timeline is not None:
            pointer = self._clamp_to_chat(timeline, *map(int, mouse_coordinate))
            super()._check_auto_scroll(timeline, tuple(pointer), delta_y)

    def _update_select(self, _screen=None) -> None:
        # Scrolling changes which content is under a stationary pointer. Wait
        # for the new layout before resolving its endpoint; reusing the old
        # SelectEnd freezes a selection within a long reply.
        timeline = getattr(self, "_drag_timeline", None)
        if (self._selecting and timeline is not None and self._select_state is not None
                and (self._select_state.end is not None or timeline.scroll_offset != self._press_scroll)
                and not getattr(self, "_selection_refresh_pending", False)):
            self._selection_refresh_pending = True
            self.call_after_refresh(self._refresh_drag_endpoint)

    def _refresh_drag_endpoint(self) -> None:
        self._selection_refresh_pending = False
        pointer = getattr(self, "_drag_pointer", None)
        if not self.is_current or not self._selecting or self._select_state is None or pointer is None:
            return
        widget, offset = self.get_widget_and_offset_at(*pointer)
        if widget is not None and offset is not None:
            self._select_state = self._select_state.update_end(
                self._clamp_to_chat(self._drag_timeline, *pointer),
                SelectEnd(widget.parent, widget, offset))

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
                widget, _ = super().get_widget_and_offset_at(event.screen_x, event.screen_y)
                if widget is not None and any(node.id == "forward-selection" for node in widget.ancestors_with_self):
                    self.app.prepare_forward_selection()
                self._drag_timeline = None
                self._selection_press = None
                if self.selections:
                    if self._inside_selection(event.screen_x, event.screen_y):
                        self._selection_press = (event.screen_offset, self.selections.copy())
                    else:
                        self.clear_selection()
                self._drag_pointer = event.screen_offset
                widget, _ = super().get_widget_and_offset_at(event.screen_x, event.screen_y)
                timeline = self._timeline_for(widget)
                self._press_scroll = timeline.scroll_offset if timeline is not None else None
            elif isinstance(event, events.MouseMove) and self._selecting:
                self._drag_pointer = event.screen_offset
            elif isinstance(event, events.MouseUp) and event.button == 1:
                press = getattr(self, "_selection_press", None)
                self._selection_press = None
                timeline = getattr(self, "_drag_timeline", None)
                scrolled = timeline is not None and timeline.scroll_offset != self._press_scroll
                if self._selecting and self._select_state is not None and self._select_state.end is not None:
                    self._drag_pointer = event.screen_offset
                    self._refresh_drag_endpoint()
                if scrolled:
                    self._mouse_down_offset = None
                if press is not None and press[0] == event.screen_offset and not scrolled:
                    self.selections = press[1]
                    self._mouse_down_offset = None
        super()._forward_event(event)
        if isinstance(event, events.MouseUp) and event.button == 1:
            self._drag_timeline = None
            self._drag_pointer = None

    def _watch__select_state(self, state: SelectState | None) -> None:
        if state is None:
            self._selecting = False
            self._drag_timeline = None
            return
        timeline = self._timeline_for(state.start.content_widget)
        if timeline is None or not state.is_attached_to_dom():
            self._select_state = None
            return
        self._drag_timeline = timeline
        self._selecting = True
        if state.end is None:
            return
        if state.is_single_content_widget:
            start, end = state.content_offsets
            self.selections = ({state.start.content_widget: Selection(start, end)}
                               if start != end else {})
            return
        # Select in transcript order, keeping the anchor when it has scrolled
        # offscreen and never traversing the sidebar, footer or composer.
        widgets = self._chat_widgets(timeline)
        start_widget, end_widget = state.start.content_widget, state.end.content_widget
        if start_widget not in widgets or end_widget not in widgets:
            return
        first, last = widgets.index(start_widget), widgets.index(end_widget)
        start, end = state.start.content_offset, state.end.content_offset
        if first > last:
            first, last = last, first
            start, end = end, start
        selections = {widget: SELECT_ALL for widget in widgets[first:last + 1]}
        selections[widgets[first]] = Selection(start, None)
        selections[widgets[last]] = Selection(None, end)
        self.selections = selections
