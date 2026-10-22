import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from rich.console import Console
from textual.geometry import Region
from textual.widgets import Input, Select

from codex_room.app import Agent, CodexRoom
from codex_room.runtime import CodexClient, CodexError
from codex_room.settings import ModelSettings, RuntimeApproval, RuntimeQuestion
from codex_room.storage import RoomStore
from codex_room.transcript import Entry, ReplyBlock, render_reply

ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


async def wait_until(predicate, pilot=None):
    async with asyncio.timeout(5):
        while not predicate():
            if pilot:
                await pilot.pause(0.02)
            else:
                await asyncio.sleep(0.01)


def reply_text(entry, expanded=False, width=96):
    console = Console(width=width, color_system=None)
    with console.capture() as captured:
        console.print(render_reply(entry, expanded, width), end="")
    return captured.get()


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = client(ROOT)
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.close()

    async def run_text(self, agent, text):
        output = []
        async def event(message):
            if message["method"] == "item/agentMessage/delta":
                output.append(message["params"]["delta"])
        await self.client.run_turn(agent, text, event)
        return "".join(output)

    async def test_parallel_threads_and_context_isolation(self):
        a, b = Agent("a", "a", "white"), Agent("b", "b", "white")
        await asyncio.gather(self.client.ensure_thread(a), self.client.ensure_thread(b))
        self.assertNotEqual(a.thread_id, b.thread_id)
        await asyncio.gather(self.run_text(a, "remember alpha"), self.run_text(b, "remember beta"))
        answers = await asyncio.gather(self.run_text(a, "recall"), self.run_text(b, "recall"))
        self.assertIn("alpha", answers[0])
        self.assertNotIn("beta", answers[0])
        self.assertIn("beta", answers[1])

    async def test_model_permission_overrides_and_resume(self):
        agent = Agent("a", "a", "white", model="test-model", effort="low")
        await self.client.ensure_thread(agent)
        thread = agent.thread_id
        full = await self.run_text(agent, "settings")
        self.assertIn('"approvalPolicy": "never"', full)
        self.assertIn('"type": "dangerFullAccess"', full)
        self.client.loaded.clear()
        agent.permission, agent.effort = "workspace", "high"
        await self.client.ensure_thread(agent)
        self.assertEqual(agent.thread_id, thread)
        scoped = await self.run_text(agent, "settings")
        self.assertIn('"effort": "high"', scoped)
        self.assertIn('"type": "workspaceWrite"', scoped)

    async def test_plain_codex_has_no_app_identity_prompt(self):
        agent = Agent("sam", "sam", "white")
        response = await self.client.ensure_thread(agent)
        self.assertFalse(response["hasDeveloperInstructions"])
        self.client.loaded.clear()
        agent.legacy_instructions = True
        response = await self.client.ensure_thread(agent)
        self.assertEqual(response["developerInstructionsValue"], "")
        self.assertFalse(agent.legacy_instructions)

    async def test_stop_interrupts_runtime_and_can_start_next_turn(self):
        agent = Agent("a", "a", "white")
        await self.client.ensure_thread(agent)
        task = asyncio.create_task(self.run_text(agent, "slow"))
        await wait_until(lambda: bool(self.client.active))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.client.active)
        self.assertIn("next", await self.run_text(agent, "next"))

    async def test_failed_turn_and_process_exit_are_visible(self):
        agent = Agent("a", "a", "white")
        await self.client.ensure_thread(agent)
        with self.assertRaisesRegex(CodexError, "test failure"):
            await self.run_text(agent, "fail")
        task = asyncio.create_task(self.run_text(agent, "slow"))
        await wait_until(lambda: bool(self.client.active))
        self.client.process.kill()
        with self.assertRaises(CodexError):
            await asyncio.wait_for(task, 3)


class AppTests(unittest.IsolatedAsyncioTestCase):
    async def test_mentions_change_default_recipient_for_followup_messages(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(100, 36)) as pilot:
                await app.connection_task
                await app.submit('/add a')
                await app.submit('/add b')
                app.select('a')
                composer = app.query_one('#message-input', Input)
                count = len(app.entries)
                composer.value = '@b'
                composer.cursor_position = len(composer.value)
                await pilot.press('enter')
                self.assertEqual(app.selected, 'b')
                self.assertEqual(composer.value, '@b ')
                self.assertEqual(len(app.entries), count)
                self.assertEqual(app.agents['b'].history, [])
                composer.value = '@a slow'
                composer.cursor_position = len(composer.value)
                await pilot.press('enter')
                self.assertEqual(app.selected, 'a')
                self.assertIn('@a', composer.placeholder)
                composer.value = 'follow up without a mention'
                composer.cursor_position = len(composer.value)
                await pilot.press('enter')
                followup = next(entry for entry in app.agents['a'].history if entry.sender == 'you' and entry.body == 'follow up without a mention')
                self.assertEqual(followup.state, 'steered')
                self.assertFalse(app.agents['a'].queue)
                self.assertEqual(app.agents['b'].history, [])
                composer.value = '@missing preserve this draft'
                composer.cursor_position = len(composer.value)
                await pilot.press('enter')
                self.assertEqual(app.selected, 'a')
                self.assertEqual(composer.value, '@missing preserve this draft')
                app.show_all = False
                app.apply_filter()
                composer.value = '@b'
                composer.cursor_position = len(composer.value)
                await pilot.press('tab')
                self.assertEqual(app.selected, 'b')
                self.assertFalse(app.cards[id(app.agents['a'].history[-1])].display)
                composer.value = '@b hello'
                composer.cursor_position = len(composer.value)
                await pilot.press('enter')
                await asyncio.wait_for(app.tasks['b'], 5)
                composer.value = 'continue here'
                composer.cursor_position = len(composer.value)
                await pilot.press('enter')
                await asyncio.wait_for(app.tasks['b'], 5)
                self.assertIn('continue here', app.agents['b'].history[-1].body)
                await app.stop_agent('a')
                app.connected = False
                self.assertFalse(await app.submit('@a cannot send'))
                self.assertEqual(app.selected, 'b')
                app.connected = True
                self.assertTrue(await app.submit('@a /permissions read-only'))
                self.assertEqual(app.selected, 'a')
            restored = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with restored.run_test(size=(80, 26)) as pilot:
                await restored.connection_task
                self.assertEqual(restored.selected, 'a')

    async def test_working_animation_summary_phases_and_terminal_states(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(100, 36)) as pilot:
                await app.connection_task
                await app.submit("/add a")
                await app.submit("/add b")
                await app.submit("@a thinking-slow")
                await app.submit("@b slow")
                await wait_until(lambda: "检查项目结构" in app.agents["a"].phase, pilot)
                await pilot.pause()
                answer = app.agents["a"].history[-1]
                card = app.cards[id(answer)]

                def status_text():
                    return "".join(strip.text for strip in card.progress.render_lines(
                        Region(0, 0, card.progress.size.width, 1)))

                self.assertTrue(card.progress.display)
                self.assertFalse(card.body.display)
                self.assertFalse(app.sidebar_visible)
                self.assertIn("Working", status_text())
                self.assertIn("正在思考：检查项目结构", status_text())
                self.assertNotIn("**", status_text())
                self.assertNotIn("PUBLIC_SUMMARY_BODY", status_text())
                self.assertEqual(answer.body, "")
                self.assertEqual(answer.blocks, [])
                first_frame = status_text()[0]
                await wait_until(lambda: status_text()[0] != first_frame, pilot)
                card.started_at = monotonic() - 65
                await pilot.pause(0.2)
                self.assertRegex(status_text(), r"1m 0[5-6]s")
                self.assertNotIn("检查项目结构", app.agents["b"].phase)
                composer = app.query_one("#message-input", Input)
                composer.value = "a pending draft"
                composer.cursor_position = len(composer.value)
                composer.cursor_position = 3
                await pilot.pause(0.2)
                self.assertEqual(composer.value, "a pending draft")
                self.assertEqual(composer.cursor_position, 3)
                self.assertTrue(composer.has_focus)
                await app.stop_agent("a")
                self.assertFalse(card.progress.display)
                self.assertTrue(app.agents["b"].busy)
                await app.stop_agent("b")

                await app.submit("@a thinking")
                await wait_until(lambda: app.agents["a"].phase == "正在执行命令…", pilot)
                current = app.agents["a"].history[-1]
                current_card = app.cards[id(current)]
                await pilot.pause()
                self.assertTrue(current_card.progress.display)
                self.assertIn("正在执行命令", "".join(strip.text for strip in current_card.progress.render_lines(
                    Region(0, 0, current_card.progress.size.width, 1))))
                await wait_until(lambda: "检查完成" in current.body, pilot)
                self.assertEqual(app.agents["a"].phase, "正在回复…")
                await asyncio.wait_for(app.tasks["a"], 5)
                self.assertEqual(current.state, "done")
                self.assertFalse(current_card.progress.display)

                await app.submit("@a thinking-fail")
                await asyncio.wait_for(app.tasks["a"], 5)
                failed = app.agents["a"].history[-1]
                self.assertEqual(failed.state, "failed")
                self.assertFalse(app.cards[id(failed)].progress.display)
                self.assertIn("thinking test failure", failed.body)
            saved = Path(tmp, "state.json").read_text()
            self.assertNotIn("RAW_REASONING_SENTINEL", saved)
            self.assertNotIn("PUBLIC_SUMMARY_BODY", saved)
            self.assertNotIn("Working", saved)
            restored = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with restored.run_test(size=(80, 26)) as pilot:
                await restored.connection_task
                self.assertFalse(any(card.progress.display for card in restored.cards.values()))

    async def test_markdown_reply_styles_code_streaming_and_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(100, 36)) as pilot:
                await app.connection_task
                await app.submit("/add test")
                await app.submit("@test markdown")
                await asyncio.wait_for(app.tasks["test"], 5)
                await pilot.pause()
                answer = app.agents["test"].history[-1]
                card = app.cards[id(answer)]
                raw = answer.body
                strips = card.body.render_lines(Region(0, 0, card.body.size.width, card.body.size.height))
                visible = "\n".join(strip.text for strip in strips)
                self.assertIn("当前环境", visible)
                self.assertNotIn("# 当前环境", visible)
                self.assertIn("身份", visible)
                self.assertNotIn("**身份**", visible)
                self.assertIn("/tmp/project", visible)
                self.assertNotIn("`/tmp/project`", visible)
                self.assertIn('print("**literal**")', visible)
                self.assertNotIn("```", visible)
                self.assertIn("ready", visible)
                segments = [segment for strip in strips for segment in strip]
                self.assertTrue(any("身份" in segment.text and segment.style.bold for segment in segments if segment.style))
                self.assertTrue(any(segment.style.link == "https://example.com/docs" for segment in segments if segment.style))

                # Incomplete Markdown is normal while the model streams tokens.
                stream = Entry("test", "", state="working", blocks=[ReplyBlock("stream", "text")])
                await app.add_entry(stream)
                for chunk in ("**状态", "**：处理中\n\n```python\n", 'print("ok")\n', "```"):
                    stream.blocks[0].text += chunk
                    stream.sync_body()
                    app.refresh_answer(stream)
                    await pilot.pause()
                stream.state = "done"
                app.refresh_answer(stream)
                self.assertIn('print("ok")', reply_text(stream))
                self.assertNotIn("```", reply_text(stream))
                await pilot.resize_terminal(66, 26)
                await pilot.pause()
                self.assertLessEqual(app.query_one("#message-input").region.bottom, 26)
                app.save_state()
            saved = json.loads(Path(tmp, "state.json").read_text())
            self.assertEqual(next(entry["body"] for entry in saved["entries"] if entry["body"] == raw), raw)
            restored = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with restored.run_test(size=(100, 36)) as pilot:
                await restored.connection_task
                old_reply = next(entry for entry in restored.entries if entry.body == raw)
                self.assertNotIn("**身份**", reply_text(old_reply))
                self.assertIn('print("**literal**")', reply_text(old_reply))

    async def test_tools_collapse_stream_toggle_interrupt_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            state = Path(tmp)
            app = CodexRoom(cwd=ROOT, state_dir=state)
            async with app.run_test(size=(100, 30)) as pilot:
                await app.connection_task
                await app.submit("/add test")
                await app.submit("@test tools")
                await asyncio.wait_for(app.tasks["test"], 5)
                answer = app.agents["test"].history[-1]
                card = app.cards[id(answer)]
                self.assertNotIn("TOOL OUTPUT", answer.body)
                collapsed = reply_text(answer, card.tools_expanded, 96)
                self.assertIn("Checking the project.", collapsed)
                self.assertIn("Project checked.", collapsed)
                self.assertIn("3 个命令", collapsed)
                self.assertEqual(collapsed.count("Ctrl+T 展开"), 1)
                self.assertNotIn("Skill instructions", collapsed)
                composer = app.query_one("#message-input", Input)
                composer.value = "draft @test"
                composer.cursor_position = len(composer.value)
                composer.cursor_position = 5
                await pilot.pause()
                collapsed_height = card.size.height
                await pilot.press("ctrl+t")
                await pilot.pause()
                expanded = reply_text(answer, card.tools_expanded, 96)
                self.assertIn("TOOL OUTPUT", expanded)
                self.assertEqual(expanded.count("[退出码 0]"), 3)
                self.assertLess(expanded.index("Checking the project."), expanded.index("$ /bin/zsh"))
                self.assertLess(expanded.index("[退出码 0]"), expanded.index("Project checked."))
                self.assertGreater(card.size.height, collapsed_height)
                self.assertEqual(composer.value, "draft @test")
                self.assertEqual(composer.cursor_position, 5)
                self.assertTrue(composer.has_focus)
                await pilot.press("ctrl+t")
                await pilot.pause()
                self.assertEqual(card.size.height, collapsed_height)

                await app.submit("@test tool-failure")
                await asyncio.wait_for(app.tasks["test"], 5)
                failed = app.agents["test"].history[-1]
                self.assertIn("1 项失败", reply_text(failed, False, 96))
                self.assertIn("[退出码 9]", reply_text(failed, True, 96))

                await app.submit("@test tool-slow")
                await wait_until(lambda: any(block.kind == "command" for block in app.agents["test"].history[-1].blocks), pilot)
                active = app.agents["test"].history[-1]
                self.assertIn("执行中", reply_text(active, False, 96))
                await pilot.press("ctrl+t")
                await pilot.pause()
                self.assertIn("TOOL OUTPUT", reply_text(active, app.tools_expanded, 96))
                await app.stop_agent("test")
                self.assertEqual(active.state, "stopped")
                self.assertFalse(any(block.status == "inProgress" for block in active.blocks if block.kind != "text"))
            restored = CodexRoom(cwd=ROOT, state_dir=state)
            async with restored.run_test(size=(86, 26)) as pilot:
                await restored.connection_task
                saved_answer = next(entry for entry in restored.entries if entry.blocks)
                self.assertFalse(restored.tools_expanded)
                self.assertNotIn("TOOL OUTPUT", reply_text(saved_answer, False, 82))
                self.assertIn("TOOL OUTPUT", reply_text(saved_answer, True, 82))

    async def test_settings_requests_persistence_and_room_lock(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            state = Path(tmp)
            app = CodexRoom(cwd=ROOT, state_dir=state)
            async with app.run_test(size=(132, 38)) as pilot:
                await app.connection_task
                self.assertTrue(app.connected)
                self.assertEqual(app.agents, {})
                self.assertEqual(app.entries, [])
                self.assertEqual(app.selected, "")
                self.assertFalse(await app.submit("hello"))
                self.assertFalse(await app.submit("/model"))
                self.assertFalse(await app.submit("/permissions"))
                await app.submit("/add exp")
                await app.submit("/add ops")
                self.assertEqual(app.agents["exp"].label, "exp")
                self.assertFalse(hasattr(app.agents["exp"], "role"))
                with self.assertRaises(RuntimeError):
                    RoomStore(state)
                await app.submit("@ops /model")
                await pilot.pause()
                self.assertIsInstance(app.screen, ModelSettings)
                app.screen.query_one("#effort-choice", Select).value = "low"
                await pilot.click("#settings-save")
                self.assertEqual(app.agents["ops"].effort, "low")
                self.assertEqual(app.agents["exp"].effort, "high")
                await app.submit("@ops /permissions workspace")
                await app.submit("@ops approval")
                await wait_until(lambda: isinstance(app.screen, RuntimeApproval), pilot)
                await pilot.pause()
                self.assertTrue(await pilot.click("#request-cancel"))
                await asyncio.wait_for(app.tasks["ops"], 5)
                self.assertIn("decline", app.agents["ops"].history[-1].body)
                await app.submit("@ops interactive")
                await wait_until(lambda: isinstance(app.screen, RuntimeQuestion), pilot)
                await pilot.pause()
                app.screen.query_one("#answer-0", Input).value = "blue"
                await pilot.click("#request-accept")
                await asyncio.wait_for(app.tasks["ops"], 5)
                self.assertIn("blue", app.agents["ops"].history[-1].body)
                old_thread = app.agents["ops"].thread_id
                await app.submit("@exp slow")
                await wait_until(lambda: bool(app.client.active), pilot)
                await app.submit("@exp queued task")
                await app.stop_agent("exp")
                self.assertFalse(app.agents["exp"].busy)
                self.assertFalse(app.agents["exp"].queue)
            saved = json.loads((state / "state.json").read_text())
            self.assertEqual(saved["agents"][1]["permission"], "workspace")
            restored = CodexRoom(cwd=ROOT, state_dir=state)
            async with restored.run_test(size=(100, 30)) as pilot:
                await restored.connection_task
                self.assertEqual(restored.agents["ops"].thread_id, old_thread)
                self.assertEqual(restored.agents["ops"].effort, "low")
                self.assertFalse(any(member.busy or member.queue for member in restored.agents.values()))
                await restored.submit("@ops settings")
                await asyncio.wait_for(restored.tasks["ops"], 5)
                self.assertEqual(restored.agents["ops"].thread_id, old_thread)
                self.assertIn("workspaceWrite", restored.agents["ops"].history[-1].body)


class MigrationTests(unittest.TestCase):
    def test_legacy_tool_output_can_be_collapsed_without_hiding_reply_text(self):
        entry = Entry("test", "First reply.\n\n$ /bin/zsh -lc pwd\n/a/path\n[退出码 0]\n\n"
                      "$ cat /a/skill\nLong instructions\n\nMore instructions\n[退出码 0]\n\nFinal reply.")
        entry.restore_legacy_tools()
        self.assertEqual(entry.body, "First reply.\n\nFinal reply.")
        self.assertIn("2 个命令", reply_text(entry, False, 80))
        self.assertNotIn("Long instructions", reply_text(entry, False, 80))
        self.assertIn("More instructions", reply_text(entry, True, 80))
        self.assertIn("Final reply.", reply_text(entry, False, 80))

    def test_remove_old_presets_and_preserve_user_created_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RoomStore(Path(tmp))
            store.save({"version": 1, "selected": "exp", "agents": [
                {"name": "exp", "label": "实验", "role": "方案 · 实验 · 结果", "color": "white", "thread_id": "old-exp"},
                {"name": "ops", "label": "运维", "role": "服务器 · 资源 · 进程", "color": "white"},
                {"name": "review", "label": "审查", "role": "推敲 · 检查 · 复核", "color": "white"},
                {"name": "sam", "label": "sam", "role": "old instruction", "color": "white", "thread_id": "keep-sam"},
            ], "entries": [
                {"sender": "system", "body": "Codex 已连接。"},
                {"sender": "you", "target": "exp", "body": "hello"},
                {"sender": "exp", "body": "我是实验成员"},
                {"sender": "sam", "body": "keep this reply"},
            ]})
            data = store.load()
            self.assertEqual(data["version"], 2)
            self.assertEqual([item["name"] for item in data["agents"]], ["sam"])
            self.assertNotIn("role", data["agents"][0])
            self.assertTrue(data["agents"][0]["legacy_instructions"])
            self.assertEqual(data["agents"][0]["thread_id"], "keep-sam")
            self.assertEqual(data["entries"], [{"sender": "sam", "body": "keep this reply"}])
            self.assertEqual(data["selected"], "sam")
            self.assertTrue(Path(tmp, "state.v1.backup.json").exists())
            self.assertEqual(store.load(), data)
            store.close()


if __name__ == "__main__":
    unittest.main()
