import asyncio
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


def mouse_event(app, kind, x, y, *, delta_y=0):
    app.screen._forward_event(kind(None, x, y, 0, delta_y, 1, False, False, False))


async def wait_for(pilot, condition):
    async with asyncio.timeout(3):
        while not condition():
            await pilot.pause(0.02)
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

    async def test_drag_and_animation_do_not_repaint_or_reflow_the_whole_screen(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 36)) as pilot:
            await app.create_member("a")
            card = await app.add_entry(Entry("a", "A first line for selecting.\n\nAnother line for selecting."))
            await pilot.pause()
            screen = app.screen
            with patch.object(screen._compositor, "render_full_update",
                              wraps=screen._compositor.render_full_update) as full:
                await pilot.mouse_down(card.body, offset=(2, 0))
                await pilot.hover(card.body, offset=(8, 0))
                await pilot.hover(card.body, offset=(12, 2))
                await pilot.mouse_up(card.body, offset=(12, 2))
                self.assertIn("first line", screen.get_selected_text())
                self.assertEqual(full.call_count, 0)

            await pilot.press("escape")
            answer = Entry("a", "Running.", state="working")
            app.agents["a"].busy = True
            await app.add_entry(answer)
            app.active_entries["a"] = (Entry("you", "work", target="a"), answer)
            app.refresh_chrome()
            await pilot.pause()
            with patch.object(screen, "_refresh_layout", wraps=screen._refresh_layout) as layout:
                app.tick()
                await pilot.pause()
                app.tick()
                await pilot.pause()
                self.assertEqual(layout.call_count, 0)

    async def test_moving_one_endpoint_only_dirties_that_line(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 36)) as pilot:
            await app.create_member("a")
            card = await app.add_entry(Entry("a", "\n\n".join(f"paragraph {i}" for i in range(50))))
            await pilot.pause()
            body = card.body
            original = Selection(Offset(0, 0), Offset(4, 20))
            app.screen.selections = {body: original}
            await pilot.pause()
            with patch.object(body, "refresh", wraps=body.refresh) as refresh:
                body.selection_updated(Selection(Offset(0, 0), Offset(6, 20)))
                regions = refresh.call_args.args
                self.assertEqual(regions, (Region(0, 20, body.size.width, 1),))

    async def test_stationary_pointer_keeps_selecting_during_auto_scroll_both_ways(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            card = await app.add_entry(Entry("a", "\n\n".join(
                f"PARAGRAPH {i:03}: selection text." for i in range(160))))
            await pilot.pause()
            timeline = app.query_one("#timeline")
            timeline.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            x, y = card.body.content_region.x, card.body.content_region.y
            bottom = timeline.content_region.bottom - 1
            mouse_event(app, events.MouseDown, x, y)
            mouse_event(app, events.MouseMove, x + 8, bottom, delta_y=bottom - y)
            await wait_for(pilot, lambda: (timeline.scroll_y > 5
                and card.body in app.screen.selections and app.screen.selections[card.body].end is not None))
            first = app.screen.get_selected_text()
            end = app.screen.selections[card.body].end.y
            scroll = timeline.scroll_y
            # No additional mouse movement: scrolling itself must extend the range.
            await wait_for(pilot, lambda: (timeline.scroll_y > scroll + 8
                and app.screen.selections[card.body].end.y > end + 5))
            selected = app.screen.get_selected_text()
            self.assertTrue(selected.startswith("PARAGRAPH 000:"))
            self.assertGreater(len(selected), len(first))
            mouse_event(app, events.MouseUp, x + 8, bottom)
            await pilot.pause()
            stopped = timeline.scroll_y
            await pilot.pause(0.08)
            self.assertEqual(timeline.scroll_y, stopped)
            await pilot.press("ctrl+c")
            self.assertEqual(app.clipboard, app.screen.get_selected_text())

            app.screen.clear_selection()
            timeline.scroll_to(y=85, animate=False, immediate=True)
            await pilot.pause()
            y = timeline.content_region.y + 5
            anchor = app.screen.get_widget_and_offset_at(x, y)[1]
            mouse_event(app, events.MouseDown, x, y)
            top = timeline.content_region.y
            mouse_event(app, events.MouseMove, x, top, delta_y=top - y)
            await wait_for(pilot, lambda: timeline.scroll_y < 75)
            first_start = app.screen.selections[card.body].start.y
            await wait_for(pilot, lambda: app.screen.selections[card.body].start.y < first_start - 5)
            self.assertEqual(app.screen.selections[card.body].end, anchor)
            mouse_event(app, events.MouseUp, x, top)
            self.assertTrue(app.screen.selections)

    async def test_cross_message_auto_scroll_stays_in_chat_when_pointer_leaves_it(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            await app.create_member("sidebar-only")
            first = None
            for i in range(35):
                card = await app.add_entry(Entry("a", f"MESSAGE {i:02}\n\nBody {i:02}: selection text."))
                first = first or card
            app.action_toggle_sidebar()
            await pilot.pause()
            timeline = app.query_one("#timeline")
            timeline.scroll_home(animate=False, immediate=True)
            await pilot.pause()
            x, y = first.body.content_region.x, first.body.content_region.y
            bottom = timeline.content_region.bottom - 1
            mouse_event(app, events.MouseDown, x, y)
            mouse_event(app, events.MouseMove, x + 8, bottom, delta_y=bottom - y)
            await wait_for(pilot, lambda: first.body.content_region.bottom < timeline.content_region.y)
            selected = app.screen.get_selected_text()
            self.assertTrue(selected.startswith("MESSAGE 00"), selected)
            self.assertIn("MESSAGE 05", selected)
            anchor = app.screen._select_state.start
            background = Entry("sidebar-only", "Background output.", state="working")
            await app.add_entry(background)
            background.body += "\n\nNew streamed paragraph."
            app.refresh_answer(background)
            await pilot.pause()
            self.assertEqual(app.screen._select_state.start, anchor)
            self.assertTrue(app.screen.get_selected_text().startswith("MESSAGE 00"))
            self.assertLess(timeline.scroll_y, timeline.max_scroll_y)
            # Crossing into the member pane must not select or scroll it.
            sidebar_scroll = app.query_one("#members").scroll_y
            mouse_event(app, events.MouseMove, 2, bottom)
            await pilot.pause()
            self.assertNotIn("sidebar-only", app.screen.get_selected_text())
            self.assertEqual(app.query_one("#members").scroll_y, sidebar_scroll)
            self.assertTrue(all(timeline in widget.ancestors for widget in app.screen.selections))
            # The composer/footer below the conversation is also outside its domain.
            outside_y = app.size.height - 1
            mouse_event(app, events.MouseMove, x, outside_y, delta_y=outside_y - bottom)
            await pilot.pause()
            mouse_event(app, events.MouseUp, x, outside_y)
            await pilot.pause()
            selected = app.screen.get_selected_text()
            self.assertTrue(selected.startswith("MESSAGE 00"))
            self.assertNotIn("F1 帮助", selected)
            self.assertTrue(all(timeline in widget.ancestors for widget in app.screen.selections))
            await pilot.press("ctrl+c")
            self.assertEqual(app.clipboard, selected)

    async def test_sidebar_cannot_start_text_selection_and_late_follow_keeps_drag_anchor(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            card = await app.add_entry(Entry("a", "Keep this anchor.\n\nSecond paragraph."))
            app.action_toggle_sidebar()
            await pilot.pause()
            member = app.query_one("#member-a")
            await pilot.mouse_down(member, offset=(2, 0))
            await pilot.hover(card.body, offset=(8, 0))
            await pilot.mouse_up(card.body, offset=(8, 0))
            self.assertFalse(app.screen.selections)
            self.assertFalse(app.screen._selecting)

            await pilot.mouse_down(card.body, offset=(0, 0))
            app.post_message(events.AppFocus())
            await pilot.pause()
            self.assertTrue(app.screen._selecting)
            await app.add_entry(Entry("a", "\n\n".join(f"new output {i}" for i in range(80))))
            await pilot.pause()
            self.assertEqual(app.query_one("#timeline").scroll_y, 0)
            self.assertIs(app.screen._select_state.start.content_widget, card.body)
            await pilot.hover(card.body, offset=(9, 0))
            await pilot.mouse_up(card.body, offset=(9, 0))
            self.assertEqual(app.screen.get_selected_text(), "Keep this")

    async def test_empty_line_in_user_message_is_a_logical_boundary(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(80, 30)) as pilot:
            await app.create_member("a")
            card = await app.add_entry(Entry("you", "First paragraph\n\nSecond paragraph", target="a"))
            await pilot.pause()
            line = card.header.selection_lines()[0]
            column = cell_len(line[:line.index("First")])
            await drag(pilot, card.header, (column, 0), (0, 1))
            self.assertEqual(app.screen.get_selected_text(), "First paragraph")

    async def test_cross_message_endpoint_at_start_does_not_copy_the_whole_reply(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            first = await app.add_entry(Entry("a", "Start here."))
            second = await app.add_entry(Entry("a", "Do not include this paragraph."))
            await pilot.pause()
            await drag(pilot, first.body, (0, 0), (0, 0), second.body)
            selected = app.screen.get_selected_text()
            self.assertTrue(selected.startswith("Start here."))
            self.assertNotIn("Do not include", selected)
