"""A wrapping prompt editor with completion and submitted-input history."""
from __future__ import annotations

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import OptionList, TextArea


class ComposerInput(TextArea):
    BINDINGS = [Binding("ctrl+shift+a,super+a", "select_all", show=False)]

    class Submitted(Message):
        def __init__(self, editor: ComposerInput) -> None:
            super().__init__()
            self.input = editor
            self.value = editor.value

    def __init__(self, **kwargs) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False, **kwargs)
        self.reset_history_navigation()

    @property
    def value(self) -> str:
        return self.text

    @value.setter
    def value(self, value: str) -> None:
        self.load_text(value)

    @property
    def cursor_position(self) -> int:
        return self.document.get_index_from_location(self.cursor_location)

    @cursor_position.setter
    def cursor_position(self, position: int) -> None:
        self.move_cursor(self.document.get_location_from_index(max(0, min(len(self.text), position))))

    def resize_to_content(self) -> None:
        if not self.is_mounted or self.parent is None:
            return
        rows = max(1, min(self.wrapped_document.height, 8, max(1, self.screen.size.height // 3)))
        if self.styles.height is None or self.styles.height.value != rows:
            self.styles.height = rows
            self.parent.styles.height = rows + 2
            self.call_after_refresh(self.scroll_cursor_visible)

    def on_mount(self) -> None:
        self.call_after_refresh(self.resize_to_content)

    def on_resize(self) -> None:
        self.call_after_refresh(self.resize_to_content)

    def reset_history_navigation(self) -> None:
        self._history_index: int | None = None
        self._history_values: list[str] = []
        self._history_drafts: dict[int, tuple[str, int]] = {}

    def action_cursor_up(self, select: bool = False) -> None:
        if select or self.wrapped_document.location_to_offset(self.cursor_location).y > 0:
            super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        if select or self.wrapped_document.location_to_offset(self.cursor_location).y < self.wrapped_document.height - 1:
            super().action_cursor_down(select)

    def recall_history(self, direction: int) -> bool:
        if self._history_index is None:
            if direction > 0 or self.value or not self.app.input_history:
                return False
            self._history_values = self.app.input_history[:]
            self._history_index = len(self._history_values)
        else:
            row = self.wrapped_document.location_to_offset(self.cursor_location).y
            if (direction < 0 and row > 0) or (direction > 0 and row < self.wrapped_document.height - 1):
                return False
        self._history_drafts[self._history_index] = (self.value, self.cursor_position)
        index = max(0, min(len(self._history_values), self._history_index + direction))
        original = self._history_values[index] if index < len(self._history_values) else ""
        value, cursor = self._history_drafts.get(index, (original, len(original)))
        self._history_index = index
        self.value = value
        self.cursor_position = cursor
        self.app.close_completions()
        self.app.refresh_recipient()
        return True

    async def _on_key(self, event: events.Key) -> None:
        app = self.app
        if event.key in {"enter", "shift+enter", "ctrl+j"}:
            event.prevent_default()
            event.stop()
            if event.key == "enter":
                await self.action_submit()
            else:
                self._replace_via_keyboard("\n", *self.selection)
            return
        if (event.key in {"up", "down"} and (not self.value or not app.completion_open)
                and self.recall_history(-1 if event.key == "up" else 1)):
            event.prevent_default()
            event.stop()
            return
        if event.key in {"up", "down", "tab", "shift+tab", "escape"}:
            app.refresh_completions(explicit=event.key != "escape")
        if app.completion_open and event.key in {"up", "down", "tab", "shift+tab", "escape"}:
            menu = app.room_screen.query_one("#completion-list", OptionList)
            if event.key in {"up", "shift+tab"}:
                menu.action_cursor_up()
            elif event.key == "down":
                menu.action_cursor_down()
            elif event.key == "tab":
                app.accept_completion()
            else:
                app.close_completions()
                app.refresh_recipient()
            event.prevent_default()
            event.stop()
            return
        if event.key in {"tab", "shift+tab"}:
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)

    async def action_submit(self) -> None:
        self.app.refresh_completions()
        if self.app.completion_open:
            if self.app.completion.candidates:
                self.app.accept_completion()
                return
            if self.app.completion.kind in {"mention", "command"}:
                return
        self.post_message(self.Submitted(self))

    def on_blur(self) -> None:
        if hasattr(self.app, "room_screen"):
            self.app.close_completions()

    async def _on_paste(self, event: events.Paste) -> None:
        await super()._on_paste(event)
        event.prevent_default()
        event.stop()

    def _watch_selection(self, previous, selection) -> None:
        super()._watch_selection(previous, selection)
        if self.is_mounted and hasattr(self.app, "room_screen"):
            self.call_after_refresh(self.app.refresh_completions)
