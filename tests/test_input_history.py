import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input

from codex_room.app import ComposerInput, CodexRoom
from codex_room.runtime import CodexClient


ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


async def send(app, pilot, value):
    composer = app.query_one("#message-input", ComposerInput)
    composer.value = value
    composer.cursor_position = len(value)
    await pilot.press("enter")


class InputHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_input_browses_messages_and_commands_without_dispatching(self):
        app = CodexRoom(demo=True, speed=0.01)
        async with app.run_test(size=(100, 32)) as pilot:
            composer = app.query_one("#message-input", ComposerInput)
            await pilot.press("up", "down")
            self.assertEqual(composer.value, "")
            await app.create_member("a")
            await app.create_member("b")
            await send(app, pilot, "第一条消息")
            await send(app, pilot, "@a 第二条消息")
            await send(app, pilot, "/focus b")
            await asyncio.gather(*app.tasks.values())
            before = len(app.entries)
            self.assertEqual(app.input_history, ["第一条消息", "@a 第二条消息", "/focus b"])
            for key, expected in (("up", "/focus b"), ("up", "@a 第二条消息"),
                                  ("up", "第一条消息"), ("up", "第一条消息"),
                                  ("down", "@a 第二条消息"), ("down", "/focus b"),
                                  ("down", ""), ("down", "")):
                await pilot.press(key)
                self.assertEqual(composer.value, expected)
                self.assertFalse(app.completion_open)
                self.assertEqual(app.selected, "b")
            self.assertEqual(len(app.entries), before)

            # Each browsed entry keeps temporary edits and its cursor, without changing history.
            await pilot.press("up", "up", "!")
            edited = "@a 第二条消息!"
            composer.cursor_position = 4
            await pilot.press("up", "down")
            self.assertEqual(composer.value, edited)
            self.assertEqual(composer.cursor_position, 4)
            self.assertEqual(app.input_history[1], "@a 第二条消息")
            await pilot.press("down", "down")
            composer.value = "new draft"
            composer.cursor_position = 3
            await pilot.press("up", "down")
            self.assertEqual(composer.value, "new draft")
            self.assertEqual(composer.cursor_position, 3)
            await pilot.press("enter")
            self.assertEqual(app.input_history[-1], "new draft")
            await pilot.press("up")
            self.assertEqual(composer.value, "new draft")

    async def test_drafts_completion_and_failed_submissions_do_not_get_overwritten(self):
        app = CodexRoom(demo=True, speed=0.01)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("alice")
            await app.create_member("bob")
            composer = app.query_one("#message-input", ComposerInput)
            await send(app, pilot, "sent message")
            await send(app, pilot, "sent message")
            self.assertEqual(app.input_history, ["sent message"])
            composer.value = "unsent draft"
            composer.cursor_position = 3
            await pilot.press("up", "down")
            self.assertEqual(composer.value, "unsent draft")
            self.assertEqual(composer.cursor_position, 3)
            await send(app, pilot, "@missing keep this draft")
            self.assertEqual(composer.value, "@missing keep this draft")
            await pilot.press("up", "down")
            self.assertEqual(composer.value, "@missing keep this draft")
            self.assertEqual(app.input_history, ["sent message"])
            composer.value = ""
            composer.cursor_position = 0
            await pilot.press("up")
            self.assertEqual(composer.value, "sent message")
            composer.value = "@"
            composer.cursor_position = 1
            await pilot.pause()
            self.assertTrue(app.completion_open)
            await pilot.press("down")
            self.assertEqual(composer.value, "@")
            self.assertEqual(app.query_one("#completion-list").highlighted, 1)
            await pilot.press("up", "tab")
            self.assertEqual(composer.value, "@alice ")
            self.assertEqual(app.input_history, ["sent message"])

    async def test_history_survives_restart_and_old_rooms_recover_user_messages(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            state_dir = Path(tmp)
            app = CodexRoom(cwd=ROOT, state_dir=state_dir)
            async with app.run_test() as pilot:
                await app.connection_task
                await send(app, pilot, "/add a")
                await send(app, pilot, "@a 你好")
                await app.tasks["a"]
                await send(app, pilot, "/permissions read-only")
            restored = CodexRoom(cwd=ROOT, state_dir=state_dir)
            async with restored.run_test() as pilot:
                await restored.connection_task
                composer = restored.query_one("#message-input", ComposerInput)
                self.assertEqual(restored.input_history, ["/add a", "@a 你好", "/permissions read-only"])
                await pilot.press("up", "up")
                self.assertEqual(composer.value, "@a 你好")
                self.assertEqual(restored.agents["a"].permission, "read-only")

            # Older state files only contain conversation entries, without raw command history.
            state_file = state_dir / "state.json"
            data = json.loads(state_file.read_text())
            data.pop("input_history")
            state_file.write_text(json.dumps(data))
            legacy = CodexRoom(cwd=ROOT, state_dir=state_dir)
            async with legacy.run_test() as pilot:
                await legacy.connection_task
                self.assertEqual(legacy.input_history, ["@a 你好"])
                await pilot.press("up")
                self.assertEqual(legacy.query_one("#message-input", ComposerInput).value, "@a 你好")
