import os
import unittest
from unittest.mock import patch

from rich.cells import cell_len
from textual import events
from textual.geometry import Offset, Region
from textual.selection import Selection
from textual.widgets import Input

from codex_room.app import ComposerInput, CodexRoom
from codex_room.selectable import highlight
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

    async def test_double_click_phrase_triple_click_line_and_click_elsewhere_clears(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(110, 36)) as pilot:
            await app.create_member("a")
            card = await app.add_entry(Entry("a", "第一段保持未选中。\n\n"
                "- `Ctrl+C` 复制选中文字，`Esc` 取消；没有选区时 `Ctrl+C` 仍退出。\n\n"
                "最后一段也保持未选中。"))
            composer = app.query_one("#message-input", ComposerInput)
            composer.value = "draft"
            await pilot.pause()
            lines = card.body.selection_lines()
            y = next(index for index, line in enumerate(lines) if "没有" in line)
            x = cell_len(lines[y][:lines[y].index("没有")])
            await pilot.double_click(card.body, offset=(x, y))
            self.assertEqual(app.screen.get_selected_text(), "取消；没有选区时")
            await pilot.press("ctrl+c")
            self.assertEqual(app.clipboard, "取消；没有选区时")
            await pilot.click(card.body, offset=(1, 0))
            self.assertFalse(app.screen.get_selected_text())
            self.assertTrue(composer.has_focus)
            self.assertEqual(composer.value, "draft")

            await pilot.triple_click(card.body, offset=(x, y))
            self.assertEqual(app.screen.get_selected_text(), lines[y])
            self.assertNotIn("第一段", app.screen.get_selected_text())
            timeline = app.query_one("#timeline")
            await pilot.click(timeline, offset=(3, timeline.size.height - 2))
            self.assertFalse(app.screen.get_selected_text())

            await pilot.double_click(card.body, offset=(card.body.size.width - 2, 0))
            self.assertFalse(app.screen.get_selected_text())
            await drag(pilot, card.body, (0, 0), (4, 0))
            self.assertEqual(app.screen.get_selected_text(), "第一")

    async def test_double_click_code_and_user_message_selects_word_not_whole_widget(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            own = await app.add_entry(Entry("you", "hello other_text goodbye", target="a"))
            code = await app.add_entry(Entry("a", "Before.\n\n```text\nalpha beta_gamma omega\nsecond line\n```\n\nAfter."))
            await pilot.pause()
            lines = own.header.selection_lines()
            column = cell_len(lines[0][:lines[0].index("other_text")])
            await pilot.double_click(own.header, offset=(column, 0))
            self.assertEqual(app.screen.get_selected_text(), "other_text")
            lines = code.body.selection_lines()
            y = next(index for index, line in enumerate(lines) if "beta_gamma" in line)
            x = cell_len(lines[y][:lines[y].index("beta_gamma")])
            await pilot.double_click(code.body, offset=(x, y))
            self.assertEqual(app.screen.get_selected_text(), "beta_gamma")
            await pilot.press("escape")
            await pilot.triple_click(code.body, offset=(x, y))
            self.assertEqual(app.screen.get_selected_text(), lines[y])
            self.assertNotIn("second line", app.screen.get_selected_text())

    async def test_drag_unicode_markdown_highlight_copy_and_return_to_composer(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            entry = Entry("a", '你好，**可以选择**文字。\n\nEnglish words for selection\n\n'
                               '[文档](https://example.com/docs)\n\n```python\nprint("**literal**")\n```')
            card = await app.add_entry(entry)
            composer = app.query_one("#message-input", ComposerInput)
            composer.value = "draft"
            composer.cursor_position = 2
            await pilot.pause()
            await drag(pilot, card.body, (0, 0), (10, 0))
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
            await drag(pilot, first.body, (11, 0), (6, 0))
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
            composer = app.query_one("#message-input", ComposerInput)
            composer.value = "selected draft"
            await pilot.press("ctrl+shift+a", "ctrl+c")
            self.assertTrue(app.is_running)
            self.assertEqual(app.clipboard, "selected draft")
            await pilot.press("right", "ctrl+c")
            self.assertFalse(app.is_running)

    async def test_drag_has_exact_end_boundary_in_both_directions_and_wide_cells(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            card = await app.add_entry(Entry("a", "alpha beta gamma\n\n甲乙丙丁 ABC"))
            await pilot.pause()
            for start, end in (((6, 0), (10, 0)), ((10, 0), (6, 0))):
                await drag(pilot, card.body, start, end)
                self.assertEqual(app.screen.get_selected_text(), "beta")
            lines = card.body.selection_lines()
            y = next(i for i, line in enumerate(lines) if "甲乙" in line)
            await drag(pilot, card.body, (6, 0), (0, y - 1))
            self.assertEqual(app.screen.get_selected_text(), "beta gamma")
            await drag(pilot, card.body, (0, y - 1), (4, y))
            self.assertEqual(app.screen.get_selected_text(), "\n甲乙")
            # Both cells of 乙 map to 乙, rather than rounding up to 丙.
            for end in (2, 3):
                await drag(pilot, card.body, (0, y), (end, y))
                self.assertEqual(app.screen.get_selected_text(), "甲")
            await drag(pilot, card.body, (4, y), (0, y))
            self.assertEqual(app.screen.get_selected_text(), "甲乙")
            await pilot.double_click(card.body, offset=(1, y))
            self.assertEqual(app.screen.get_selected_text(), "甲乙丙丁")

    async def test_outside_press_clears_immediately_and_inside_click_preserves_selection(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            card = await app.add_entry(Entry("a", "alpha beta gamma\n\n甲乙丙丁 ABC"))
            composer = app.query_one("#message-input", ComposerInput)
            composer.value = "keep draft"
            composer.cursor_position = 4
            await pilot.pause()
            await drag(pilot, card.body, (6, 0), (10, 0))
            await pilot.click(card.body, offset=(8, 0))
            self.assertEqual(app.screen.get_selected_text(), "beta")
            # Same row and widget; just one character outside the highlighted span.
            await pilot.mouse_down(card.body, offset=(10, 0))
            self.assertFalse(app.screen.selections)
            await pilot._post_mouse_events([events.MouseUp, events.Click], card.body, (10, 0), button=1)
            await pilot.pause()
            self.assertTrue(composer.has_focus)
            self.assertEqual((composer.value, composer.cursor_position), ("keep draft", 4))

            lines = card.body.selection_lines()
            y = next(i for i, line in enumerate(lines) if "甲乙" in line)
            await drag(pilot, card.body, (0, y), (4, y))
            await pilot.click(card.body, offset=(3, y))
            self.assertEqual(app.screen.get_selected_text(), "甲乙")
            await pilot.mouse_down(card.body, offset=(4, y))
            self.assertFalse(app.screen.selections)
            await pilot.mouse_up(card.body, offset=(4, y))

            for target, offset in ((card.body, (60, 0)), (card.header, (2, 0)),
                                   ("#toggle-sidebar", (2, 0))):
                card.body.text_select_all()
                await pilot.pause()
                await pilot.mouse_down(target, offset=offset)
                self.assertFalse(app.screen.selections)
                await pilot._post_mouse_events([events.MouseUp, events.Click], target, offset, button=1)
                await pilot.pause()
            self.assertTrue(app.sidebar_visible)

    async def test_small_selection_reuses_unselected_rows_of_long_output(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            entry = Entry("a", "", blocks=[ReplyBlock("tool", "command",
                "\n".join(f"line {i}: example output" for i in range(2000)), "cat output", "completed", 0)])
            card = await app.add_entry(entry)
            card.tools_expanded = True
            card.refresh_entry()
            await pilot.pause()
            body = card.body
            visual = body._render()
            body.render_line(0)
            untouched = visual._offset_strips[1000]
            with patch("codex_room.selectable.highlight", wraps=highlight) as paint:
                for end in (3, 4, 5):
                    selection = Selection(Offset(0, 0), Offset(end, 0))
                    app.screen.selections = {body: selection}
                    body.selection_updated(selection)
                    body.render_line(0)
                    self.assertIs(body._render(), visual)
                    self.assertIs(body._render_cache.lines[1000], untouched)
                self.assertEqual(paint.call_count, 3)
            app.screen.clear_selection()
            body.selection_updated(None)
            body.render_line(0)
            self.assertIs(body._render_cache.lines[0], visual._offset_strips[0])

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
