import asyncio
import unittest

from textual import events
from textual.geometry import Region

from codex_room.app import CodexRoom, ComposerInput
from codex_room.transcript import Entry, ReplyBlock


class ComposerTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_chinese_prompt_wraps_before_sending_and_reflows_on_resize(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(86, 26)) as pilot:
            editor = app.query_one("#message-input", ComposerInput)
            prompt = ("你帮我看下这个项目是干嘛的？主要在意它怎么做数据库查询工具。"
                      "还要和当前 ai-human 的工具比较：/Users/zhouchenghao/python_file/wj/ai-human。"
                      "请都看完，再给我答案。")
            app.post_message(events.Paste(prompt))
            await pilot.pause()
            self.assertEqual(editor.value, prompt)
            self.assertGreater(editor.region.height, 1)
            self.assertEqual(editor.scroll_x, 0)
            self.assertLessEqual(editor.region.height, 8)
            self.assertLessEqual(app.query_one("#footer").region.bottom, 26)
            visible = "".join(strip.text.strip() for strip in editor.render_lines(Region(0, 0, editor.size.width, editor.size.height)))
            self.assertIn("你帮我看下", visible)
            self.assertIn("再给我答案", visible)
            self.assertEqual(app.entries, [])
            original_height = editor.region.height
            cursor = editor.cursor_position
            app.action_toggle_sidebar()
            await pilot.pause()
            self.assertGreaterEqual(editor.region.height, original_height)
            self.assertEqual(editor.cursor_position, cursor)
            self.assertEqual(editor.value, prompt)
            await pilot.resize_terminal(132, 40)
            await pilot.pause()
            self.assertEqual(editor.value, prompt)
            self.assertEqual(editor.scroll_x, 0)
            editor.value = ""
            await pilot.pause()
            self.assertEqual(editor.region.height, 1)
            self.assertEqual(app.query_one("#composer").region.height, 3)

    async def test_manual_newlines_paste_copy_send_and_multiline_history(self):
        app = CodexRoom(demo=True, speed=0.001)
        async with app.run_test(size=(86, 26)) as pilot:
            await app.create_member("a")
            editor = app.query_one("#message-input", ComposerInput)
            app.post_message(events.Paste("第一行\n第二行"))
            await pilot.pause()
            await pilot.press("shift+enter", "x")
            self.assertEqual(editor.value, "第一行\n第二行\nx")
            await pilot.press("ctrl+shift+a", "ctrl+c")
            self.assertEqual(app.clipboard, "第一行\n第二行\nx")
            await pilot.press("right", "enter")
            await asyncio.gather(*app.tasks.values())
            own = [entry for entry in app.entries if entry.sender == "you"]
            self.assertEqual(len(own), 1)
            self.assertEqual(own[0].body, "第一行\n第二行\nx")
            self.assertEqual(editor.value, "")
            self.assertEqual(editor.region.height, 1)
            await pilot.press("up")
            self.assertEqual(editor.value, own[0].body)
            self.assertEqual(editor.cursor_location[0], 2)
            await pilot.press("up")
            self.assertEqual(editor.cursor_location[0], 1)
            self.assertEqual(editor.value, own[0].body)
            editor.cursor_position = 0
            await pilot.press("X")
            self.assertEqual(editor.value, "X第一行\n第二行\nx")

    async def test_long_pastes_fold_and_completion_still_works(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(86, 26)) as pilot:
            await app.create_member("alice")
            await app.create_member("bob")
            editor = app.query_one("#message-input", ComposerInput)
            path = "/very-long-component" * 100
            app.post_message(events.Paste(path))
            await pilot.pause()
            self.assertEqual(editor.value, "[Pasted Content 2000 chars]")
            self.assertEqual(editor.prompt.expand(), path)
            self.assertEqual(editor.region.height, 1)
            self.assertEqual(editor.scroll_y, 0)
            self.assertEqual(editor.scroll_x, 0)
            editor.value = "@"
            editor.cursor_position = 1
            await pilot.pause()
            await pilot.press("down", "tab")
            self.assertEqual(editor.value, "@bob ")
            self.assertEqual(app.selected, "bob")
            self.assertEqual(editor.region.height, 1)
            self.assertFalse(any(entry.sender == "you" for entry in app.entries))

    async def test_command_syntax_is_colored_and_wraps_without_truncating_the_command(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(86, 32)) as pilot:
            await app.create_member("a")
            command = "git status --short --branch; sed -n '12,40p' " + "/long/path/component" * 6 + "/file.py"
            card = await app.add_entry(Entry("a", "", blocks=[ReplyBlock("cmd", "command", "output", command, "completed", 0)]))
            card.set_tools_group(0, 1)
            await pilot.pause()
            lines = card.body.selection_lines()
            self.assertGreater(len(lines), 3)
            self.assertTrue(any("/file.py" in line for line in lines))
            strips = card.body.render_lines(Region(0, 0, card.body.size.width, card.body.size.height))
            colors = {segment.style.color for strip in strips for segment in strip if segment.style and segment.style.color}
            self.assertGreaterEqual(len(colors), 3)
            card.cycle_tools_group(0)
            await pilot.pause()
            self.assertEqual(card.tool_group_levels[0], 0)
            self.assertNotIn("输出：", "\n".join(card.body.selection_lines()))
