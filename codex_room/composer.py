"""A wrapping prompt editor with completion and submitted-input history."""
from __future__ import annotations

from dataclasses import replace

from textual import events
from textual.binding import Binding
from textual.document._document import Selection
from textual.document._edit import Edit
from textual.message import Message
from textual.widgets import OptionList, TextArea

from .attachments import Attachment, PASTE_CHAR_LIMIT, PASTE_LINE_LIMIT, Prompt, image_paths


class PromptEdit(Edit):
    """Undo and redo the text and its hidden payloads as one edit."""

    def __init__(self, edit: Edit, before: list[Attachment], after: list[Attachment]):
        super().__init__(edit.text, edit.from_location, edit.to_location, edit.maintain_selection_offset)
        self.before, self.after_attachments = before, after

    def do(self, text_area, record_selection=True):
        result = super().do(text_area, record_selection)
        text_area.attachments = self.after_attachments[:]
        return result

    def undo(self, text_area):
        result = super().undo(text_area)
        text_area.attachments = self.before[:]
        return result


class ComposerInput(TextArea):
    BINDINGS = [Binding("ctrl+shift+a,super+a", "select_all", show=False),
                Binding("alt+enter", "expand_paste", show=False)]

    class Submitted(Message):
        def __init__(self, editor: ComposerInput) -> None:
            super().__init__()
            self.input = editor
            self.value = editor.value
            self.prompt = editor.prompt

    def __init__(self, **kwargs) -> None:
        self.attachments: list[Attachment] = []
        super().__init__(soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False, **kwargs)
        self.reset_history_navigation()

    @property
    def prompt(self) -> Prompt:
        return Prompt(self.text, self.attachments[:])

    @property
    def completion_text(self) -> str:
        text = self.text
        for item in reversed(self.attachments):
            text = text[:item.start] + " " * (item.end - item.start) + text[item.end:]
        return text

    def load_text(self, text: str) -> None:
        self.attachments = []
        super().load_text(text)

    def load_prompt(self, prompt: Prompt) -> None:
        self.load_text(prompt.text)
        self.attachments = prompt.attachments[:]
        self.refresh()

    def edit(self, edit: Edit):
        start, end = sorted(self.document.get_index_from_location(location)
                            for location in (edit.from_location, edit.to_location))
        start, end = self.prompt.atomic_range(start, end)
        edit.from_location = self.document.get_location_from_index(start)
        edit.to_location = self.document.get_location_from_index(end)
        delta = len(edit.text) - (end - start)
        after = [replace(item, start=item.start + delta, end=item.end + delta) if item.start >= end else item
                 for item in self.attachments if item.end <= start or item.start >= end]
        for item in getattr(self, "_inserting_attachments", []):
            after.append(replace(item, start=item.start + start, end=item.end + start))
        return super().edit(PromptEdit(edit, self.attachments[:], sorted(after, key=lambda item: item.start)))

    def paste_text(self, text: str) -> None:
        if self.read_only:
            return
        images = image_paths(text, self.app.cwd)
        parts = []
        labels = {item.label for item in self.attachments}
        def block(kind, content):
            number = 1
            while True:
                label = (f"[Image #{number}]" if kind == "image" else
                         f"[Pasted Content{' #' + str(number) if number > 1 else ''} {len(content)} chars]")
                if label not in labels:
                    break
                number += 1
            labels.add(label)
            start = sum(len(part.label) + 1 for part in parts)
            parts.append(Attachment(start, start + len(label), kind, content, label))
        if images:
            for path in images:
                block("image", path)
        elif len(text) >= PASTE_CHAR_LIMIT or text.count("\n") >= PASTE_LINE_LIMIT - 1:
            block("paste", text)
        self._inserting_attachments = parts
        try:
            self._replace_via_keyboard(" ".join(item.label for item in parts) if parts else text, *self.selection)
        finally:
            self._inserting_attachments = []
        self.focus()

    def action_paste(self) -> None:
        self.paste_text(self.app.clipboard)

    def action_expand_paste(self) -> None:
        for item in self.attachments:
            if item.kind == "paste" and item.start <= self.cursor_position <= item.end:
                self.replace(item.content, self.document.get_location_from_index(item.start),
                             self.document.get_location_from_index(item.end), maintain_selection_offset=False)
                return

    @property
    def selected_text(self) -> str:
        start, end = self.prompt.atomic_range(*(self.document.get_index_from_location(location)
                                               for location in self.selection))
        return self.prompt.slice(start, end).expand(copy_images=True) if start != end else ""

    def action_cut(self) -> None:
        if self.read_only:
            return
        if not self.attachments:
            super().action_cut()
            return
        if self.selection.is_empty:
            self.action_select_line()
        text = self.selected_text
        if text:
            self._delete_via_keyboard(*self.selection)
            self.app.copy_to_clipboard(text)

    def get_cursor_left_location(self):
        position = self.cursor_position
        for item in self.attachments:
            if item.start < position <= item.end:
                return self.document.get_location_from_index(item.start)
        return super().get_cursor_left_location()

    def get_cursor_right_location(self):
        position = self.cursor_position
        for item in self.attachments:
            if item.start <= position < item.end:
                return self.document.get_location_from_index(item.end)
        return super().get_cursor_right_location()

    def get_line(self, line_index):
        line = super().get_line(line_index)
        start = self.document.get_index_from_location((line_index, 0))
        for item in self.attachments:
            if start <= item.start < start + len(line):
                line.stylize("#94c9bf", item.start - start, item.end - start)
        return line

    def _refresh_size(self) -> None:
        # TextArea's undo updates scrollbars before restoring its selection.
        # A multiline block collapsed back to one row must not retain row 8.
        clamped = Selection(*(self.clamp_visitable(location) for location in self.selection))
        if clamped != self.selection:
            self.selection = clamped
        super()._refresh_size()

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
        self._history_values: list[str | dict] = []
        self._history_drafts: dict[int, tuple[Prompt, int]] = {}

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
        self._history_drafts[self._history_index] = (self.prompt, self.cursor_position)
        index = max(0, min(len(self._history_values), self._history_index + direction))
        original = Prompt.load(self._history_values[index]) if index < len(self._history_values) else Prompt("")
        value, cursor = self._history_drafts.get(index, (original, len(original.text)))
        self._history_index = index
        self.load_prompt(value)
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
        self.paste_text(event.text)
        event.prevent_default()
        event.stop()

    def _watch_selection(self, previous, selection) -> None:
        if selection.is_empty:
            position = self.document.get_index_from_location(selection.end)
            for item in self.attachments:
                if item.start < position < item.end:
                    edge = item.start if position - item.start < item.end - position else item.end
                    self.selection = Selection.cursor(self.document.get_location_from_index(edge))
                    return
        super()._watch_selection(previous, selection)
        if self.is_mounted and hasattr(self.app, "room_screen"):
            self.call_after_refresh(self.app.refresh_completions)
