import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.geometry import Offset
from textual.selection import Selection
from textual.widgets import Input, OptionList

from codex_room.app import CodexRoom, ComposerInput, AddMember
from codex_room.attachments import Attachment
from codex_room.forwarding import ForwardQuote, RecipientPicker, QuotePreview, capture_selection
from codex_room.member_config import resolve_member
from codex_room.runtime import CodexClient
from codex_room.transcript import Entry, ReplyBlock

ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


async def wait_until(predicate, pilot):
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause(.01)


class ForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def source(self, app, pilot):
        await app.create_member("a")
        await app.create_member("b")
        query = await app.add_entry(Entry("you", "不是有 dedup 吗？", "a"))
        reply = await app.add_entry(Entry("a", "调研后发现没有接入。", blocks=[
            ReplyBlock("text", "text", "调研后发现没有接入。"),
            ReplyBlock("cmd", "command", "FULL_HIDDEN_TOOL_OUTPUT", "rg dedup", "completed", 0),
            ReplyBlock("diff", "file", '[{"path":"dedup.py","diff":"+wire_up()"}]', "dedup.py", "completed")]))
        app.select("a", single=True)
        await pilot.pause()
        app.room_screen.selections = {query.header: Selection(None, None), reply.body: Selection(None, None)}
        return query, reply

    async def test_forward_existing_preview_send_return_and_restore_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            state = Path(tmp)
            app = CodexRoom(cwd=ROOT, state_dir=state)
            async with app.run_test(size=(120, 40)) as pilot:
                await app.connection_task
                query, reply = await self.source(app, pilot)
                editor = app.query_one(ComposerInput)
                editor.paste_text("keep original " * 500)
                original = editor.prompt.dump()
                app.room_screen.selections = {query.header: Selection(None, None), reply.body: Selection(None, None)}
                with patch.object(app.client, "request", wraps=app.client.request) as calls:
                    await pilot.press("ctrl+f")
                    self.assertIsInstance(app.screen, RecipientPicker)
                    app.screen.query_one(Input).value = "b"
                    await pilot.pause()
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(app.selected, "b")
                    self.assertFalse(app.show_all)
                    self.assertIsNotNone(app.forward_quote)
                    self.assertEqual(editor.value, "")
                    await pilot.click("#quote-preview")
                    self.assertIsInstance(app.screen, QuotePreview)
                    self.assertIn("FULL_HIDDEN_TOOL_OUTPUT", app.screen.query_one("#quote-detail").text)
                    await pilot.press("escape")
                    reply.entry.blocks[1].text = "CHANGED AFTER CAPTURE"
                    editor.value = "写入 /tmp/vault/开发经验，讲清为什么需要 dedup"
                    await pilot.press("enter")
                    await app.tasks["b"]
                    sent = [call.args[1] for call in calls.call_args_list if call.args[0] == "turn/start"]
                    self.assertEqual(len(sent), 1)
                    payload = sent[0]["input"][0]["text"]
                    self.assertIn("FULL_HIDDEN_TOOL_OUTPUT", payload)
                    self.assertIn("wire_up()", payload)
                    self.assertNotIn("CHANGED AFTER CAPTURE", payload)
                    self.assertIn("用户本次任务", payload)
                    request = next(entry for entry in app.entries if entry.target == "b" and entry.sender == "you")
                    snapshot_id, request_id = request.quote["id"], request.id
                    self.assertEqual(request.body, "写入 /tmp/vault/开发经验，讲清为什么需要 dedup")
                    self.assertIsNone(app.forward_quote)
                    await pilot.click("#forward-return")
                    self.assertEqual(app.selected, "a")
                    self.assertFalse(app.show_all)
                    self.assertEqual(editor.prompt.dump(), original)
                    self.assertTrue(editor.has_focus)
                    app.save_state()
            restored = CodexRoom(cwd=ROOT, state_dir=state)
            async with restored.run_test() as pilot:
                await restored.connection_task
                request = next(entry for entry in restored.entries if entry.id == request_id)
                self.assertEqual(request.quote["id"], snapshot_id)
                self.assertIn("FULL_HIDDEN_TOOL_OUTPUT", request.input_text)

    async def test_mouse_forward_new_chinese_member_cancel_and_resume_draft(self):
        app = CodexRoom(demo=True, cwd=ROOT, speed=.01)
        async with app.run_test(size=(120, 40)) as pilot:
            query, reply = await self.source(app, pilot)
            await pilot.pause(.2)
            await pilot.click("#forward-selection")
            self.assertIsInstance(app.screen, RecipientPicker)
            menu = app.screen.query_one(OptionList)
            menu.highlighted = menu.option_count - 1
            await pilot.press("enter")
            self.assertIsInstance(app.screen, AddMember)
            app.screen.query_one("#new-name", Input).value = "开发笔记"
            await pilot.click("#confirm-add")
            await pilot.pause()
            name = resolve_member("开发笔记", app.agents)
            self.assertEqual(app.selected, name)
            self.assertIsNotNone(app.forward_quote)
            editor = app.query_one(ComposerInput)
            editor.value = "整理到我的笔记"
            await pilot.click("#forward-return")
            self.assertEqual(app.selected, "a")
            app.select(name, single=True)
            await pilot.pause()
            self.assertEqual(editor.value, "整理到我的笔记")
            self.assertIsNotNone(app.forward_quote)
            await pilot.click("#quote-remove")
            self.assertIsNone(app.forward_quote)
            self.assertEqual(editor.value, "整理到我的笔记")

    async def test_scope_collapsed_details_partial_text_and_attachments(self):
        app = CodexRoom(demo=True, cwd=ROOT)
        async with app.run_test(size=(120, 40)) as pilot:
            await app.create_member("a")
            await app.create_member("b")
            label = "[Pasted Content 6000 chars]"
            image = "[Image #1]"
            text = "X" * 6000
            query = await app.add_entry(Entry("you", f"{text} {image}", "a", display_body=f"{label} {image}",
                attachments=[Attachment(0, len(label), "paste", text, label),
                             Attachment(len(label)+1, len(label)+1+len(image), "image", "/tmp/image.png", image)]))
            other = await app.add_entry(Entry("b", "OTHER MEMBER"))
            reply = await app.add_entry(Entry("a", "HIDDEN REPLY", collapsed=True, blocks=[ReplyBlock("cmd", "command", "INTERNAL", "pwd")]))
            await pilot.pause()
            selections = {query.header: Selection(None, None), reply.preview: Selection(None, None)}
            quote = capture_selection(app.entries, app.cards, selections, app.agents)
            self.assertTrue(quote.mixed)
            self.assertEqual(len(quote.selected), 2)
            self.assertEqual(quote.images, ["/tmp/image.png"])
            self.assertIn(text, quote.payload())
            self.assertIn("INTERNAL", quote.payload())
            self.assertNotIn("OTHER MEMBER", quote.payload())
            quote.scope = "all"
            self.assertEqual(len(quote.selected), 3)
            self.assertIn("OTHER MEMBER", quote.payload())
            partial = await app.add_entry(Entry("a", "start keep end"))
            await pilot.pause()
            quote = capture_selection(app.entries, app.cards, {partial.body: Selection(Offset(6, 0), Offset(10, 0))}, app.agents)
            self.assertEqual(quote.selected[0]["text"], "keep")
            self.assertNotIn("start", quote.payload())
            tool_card = await app.add_entry(Entry("a", "", blocks=[
                ReplyBlock("one", "command", "FIRST_TOOL", "printf first", "completed", 0),
                ReplyBlock("two", "command", "SECOND_TOOL", "printf second", "completed", 0)]))
            tool_card.set_tools_group(0, 1)
            await pilot.pause()
            lines = tool_card.body.selection_lines()
            row = next(index for index, line in enumerate(lines) if "printf first" in line)
            quote = capture_selection(app.entries, app.cards,
                {tool_card.body: Selection(Offset(0, row), Offset(len(lines[row]), row))}, app.agents)
            self.assertIn("FIRST_TOOL", quote.payload())
            self.assertNotIn("SECOND_TOOL", quote.payload())

    async def test_background_question_notifies_without_stealing_focus(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(100, 36)) as pilot:
                await app.connection_task
                await app.create_member("a")
                await app.create_member("b")
                await app.submit("@b interactive")
                app.select("a", single=True)
                editor = app.query_one(ComposerInput)
                editor.value = "keep typing"
                await wait_until(lambda: "b" in app.pending_interactions, pilot)
                self.assertIs(app.screen, app.room_screen)
                self.assertTrue(editor.has_focus)
                self.assertEqual(editor.value, "keep typing")
                app.select("b", single=True)
                await pilot.pause()
                await pilot.click("#respond-request")
                await pilot.pause()
                app.screen.query_one("#answer-0", Input).value = "blue"
                await pilot.click("#request-accept")
                await app.tasks["b"]
                self.assertNotIn("b", app.pending_interactions)
                self.assertIn("blue", app.agents["b"].history[-1].body)

    async def test_multi_mentions_independent_runs_chinese_rename_preserves_identity(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(120, 40)) as pilot:
                await app.connection_task
                await app.submit("/add 排查")
                a = resolve_member("排查", app.agents)
                await app.submit("/add 笔记")
                b = resolve_member("笔记", app.agents)
                editor = app.query_one(ComposerInput)
                editor.value = "@排查 @"
                editor.cursor_position = len(editor.value)
                await pilot.pause()
                self.assertEqual(app.completion_matches, [b])
                await pilot.press("tab")
                self.assertEqual(editor.value, "@排查 @笔记 ")
                with patch.object(app.client, "request", wraps=app.client.request) as calls:
                    self.assertTrue(await app.submit("@排查 @笔记 @排查 slow"))
                    await wait_until(lambda: len(app.client.active) == 2, pilot)
                    self.assertTrue(app.show_all)
                    self.assertNotEqual(app.agents[a].thread_id, app.agents[b].thread_id)
                    thread, task = app.agents[a].thread_id, app.tasks[a]
                    self.assertTrue(await app.submit("/rename 排查 环境排查"))
                    self.assertEqual(app.agents[a].thread_id, thread)
                    self.assertIs(app.tasks[a], task)
                    self.assertEqual(resolve_member("环境排查", app.agents), a)
                    self.assertFalse(await app.submit("/rename 环境排查 笔记"))
                    self.assertFalse(await app.submit("@环境排查 @不存在 should not send"))
                    starts = [call for call in calls.call_args_list if call.args[0] == "turn/start"]
                    self.assertEqual(len(starts), 2)
                    self.assertTrue(all(call.args[1]["input"][0]["text"] == "slow" for call in starts))
                    inputs = [entry for entry in app.entries if entry.sender == "you"]
                    self.assertEqual(len(inputs), 2)
                    self.assertEqual(inputs[0].batch_id, inputs[1].batch_id)
                    self.assertEqual(sum(app.cards[id(entry)].display for entry in inputs), 1)
                    app.select(b, single=True)
                    self.assertFalse(app.cards[id(inputs[0])].display)
                    self.assertTrue(app.cards[id(inputs[1])].display)
                    await app.stop_agent(a)
                    await app.stop_agent(b)
                    self.assertEqual(len({entry.id for entry in app.entries}), len(app.entries))
