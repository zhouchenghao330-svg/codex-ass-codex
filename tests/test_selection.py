import os
import unittest
from unittest.mock import patch

from textual import events
from textual.geometry import Region
from textual.widgets import Input

from codex_room.app import CodexRoom
from codex_room.transcript import Entry, ReplyBlock


async def drag(pilot, widget, start, end, end_widget=None):
    await pilot.mouse_down(widget, offset=start)
    await pilot.hover(end_widget or widget, offset=end)
    await pilot.mouse_up(end_widget or widget, offset=end)
    await pilot.pause()


class SelectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        environment.pop("NO_COLOR", None)
        self.environment = patch.dict(os.environ, environment, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    async def test_drag_unicode_markdown_highlight_copy_and_return_to_composer(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            entry = Entry("a", '你好，**可以选择**文字。\n\nEnglish words for selection\n\n'
                               '[文档](https://example.com/docs)\n\n```python\nprint("**literal**")\n```')
            card = await app.add_entry(entry)
            composer = app.query_one("#message-input", Input)
            composer.value = "draft"
            composer.cursor_position = 2
            await pilot.pause()
            await drag(pilot, card.body, (0, 0), (7, 0))
            self.assertEqual(app.screen.get_selected_text(), "你好，可以")
            self.assertFalse(composer.has_focus)
            strips = card.body.render_lines(Region(0, 0, card.body.size.width, 1))
            self.assertTrue(any(segment.style and segment.style.bgcolor
                                and segment.style.bgcolor.get_truecolor() == (233, 223, 184)
                                for strip in strips for segment in strip))
            await pilot.press("ctrl+c")
            self.assertEqual(app.clipboard, "你好，可以")
            self.assertTrue(app.is_running)
            self.assertEqual(composer.value, "draft")
            self.assertEqual(composer.cursor_position, 2)
            app.post_message(events.AppFocus())
            await pilot.pause()
            self.assertEqual(app.screen.get_selected_text(), "你好，可以")

            card.body.text_select_all()
            await pilot.pause()
            selected = app.screen.get_selected_text()
            self.assertIn("可以选择", selected)
            self.assertNotIn("**可以选择**", selected)
            self.assertIn('print("**literal**")', selected)
            self.assertNotIn("```", selected)
            self.assertIn("文档", selected)
            all_lines = card.body.render_lines(Region(0, 0, card.body.size.width, card.body.size.height))
            self.assertTrue(any(segment.style and segment.style.link == "https://example.com/docs"
                                for strip in all_lines for segment in strip))
            await pilot.press("super+c")
            self.assertEqual(app.clipboard, selected)
            await pilot.press("escape")
            self.assertFalse(app.screen.get_selected_text())
            self.assertTrue(composer.has_focus)
            await pilot.press("X")
            self.assertEqual(composer.value, "drXaft")

    async def test_cross_message_reverse_drag_resize_and_visible_tool_output(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(90, 36)) as pilot:
            await app.create_member("a")
            first = await app.add_entry(Entry("a", "First paragraph"))
            second = await app.add_entry(Entry("a", "Second paragraph"))
            await pilot.pause()
            await drag(pilot, first.body, (6, 0), (6, 0), second.body)
            selected = app.screen.get_selected_text()
            self.assertTrue(selected.startswith("paragraph"), selected)
            self.assertTrue(selected.endswith("Second"), selected)
            await pilot.press("ctrl+shift+c")
            self.assertEqual(app.clipboard, selected)
            app.screen.clear_selection()
            await drag(pilot, first.body, (10, 0), (6, 0))
            self.assertEqual(app.screen.get_selected_text(), "parag")

            app.screen.clear_selection()
            entry = Entry("a", "", blocks=[ReplyBlock("intro", "text", "Before tools."),
                ReplyBlock("tool", "command", 'COMMAND **literal**\n第二行输出', "cat file.txt", "completed", 0),
                ReplyBlock("end", "text", "After tools.")])
            entry.sync_body()
            card = await app.add_entry(entry)
            await pilot.pause()
            card.body.text_select_all()
            await pilot.pause()
            self.assertIn("Before tools.", app.screen.get_selected_text())
            self.assertIn("After tools.", app.screen.get_selected_text())
            self.assertNotIn("COMMAND", app.screen.get_selected_text())
            app.screen.clear_selection()
            app.action_toggle_tools()
            await pilot.pause()
            card.body.text_select_all()
            await pilot.pause()
            self.assertIn("COMMAND **literal**", app.screen.get_selected_text())
            self.assertIn("第二行输出", app.screen.get_selected_text())
            await pilot.resize_terminal(64, 26)
            await pilot.press("ctrl+c")
            self.assertIn("第二行输出", app.clipboard)
            self.assertNotIn("First paragraph", app.clipboard)

    async def test_copy_input_selection_then_ctrl_c_without_selection_exits(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(80, 26)) as pilot:
            composer = app.query_one("#message-input", Input)
            composer.value = "selected draft"
            await pilot.press("ctrl+shift+a", "ctrl+c")
            self.assertTrue(app.is_running)
            self.assertEqual(app.clipboard, "selected draft")
            await pilot.press("right", "ctrl+c")
            self.assertFalse(app.is_running)

    async def test_wheel_over_a_reply_moves_one_line_in_each_direction(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(80, 26)) as pilot:
            await app.create_member("a")
            await app.add_entry(Entry("a", "```text\n" + "\n".join(f"line {index}" for index in range(100)) + "\n```"))
            await pilot.pause()
            timeline = app.query_one("#timeline")
            timeline.scroll_to(y=20, animate=False, immediate=True)
            await pilot.pause()
            x, y = timeline.region.x + 5, timeline.region.y + 5
            for event_type, expected in ((events.MouseScrollDown, 21), (events.MouseScrollUp, 20)):
                app.post_message(event_type(None, x, y, 0, 0, 0, False, False, False, screen_x=x, screen_y=y))
                await pilot.pause()
                self.assertEqual(timeline.scroll_y, expected)
