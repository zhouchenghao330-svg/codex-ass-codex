import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual import events
from textual.document._document import Selection

from codex_room.app import CodexRoom, ComposerInput
from codex_room.attachments import Prompt, image_paths
from codex_room.runtime import CodexClient


ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


async def paste(app, pilot, text):
    app.post_message(events.Paste(text))
    await pilot.pause()


class AttachmentTests(unittest.TestCase):
    def test_image_paths_handle_spaces_shell_quotes_urls_and_ignore_normal_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            first = cwd / "中文 image.png"
            second = cwd / "other.jpg"
            first.touch()
            second.touch()
            for value in (str(first), f"'{first}'", str(first).replace(" ", "\\ "), first.as_uri()):
                self.assertEqual(image_paths(value, cwd), [str(first)])
            self.assertEqual(image_paths(f"'{first}' {second}", cwd), [str(first), str(second)])
            self.assertEqual(image_paths(f"please read {first}", cwd), [])
            self.assertEqual(image_paths("missing.png", cwd), [])
            self.assertEqual(image_paths("file://other-host/tmp/file.png", cwd), [])


class AttachmentUITests(unittest.IsolatedAsyncioTestCase):
    async def test_large_paste_is_one_delete_and_undo_redo_preserve_payload(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(86, 26)) as pilot:
            editor = app.query_one("#message-input", ComposerInput)
            original = "中文段落\n" * 1000
            editor.value = "请检查："
            editor.cursor_position = len(editor.value)
            await paste(app, pilot, original)
            self.assertEqual(editor.value, "请检查：[Pasted Content 5000 chars]")
            self.assertEqual(editor.prompt.expand(), "请检查：" + original)
            self.assertEqual(editor.region.height, 1)
            await pilot.press("backspace")
            self.assertEqual(editor.value, "请检查：")
            self.assertEqual(editor.attachments, [])
            await pilot.press("ctrl+z")
            self.assertEqual(editor.prompt.expand(), "请检查：" + original)
            self.assertEqual(len(editor.attachments), 1)
            await pilot.press("ctrl+y")
            self.assertEqual(editor.value, "请检查：")
            self.assertFalse(editor.attachments)
            await pilot.press("ctrl+z", "ctrl+shift+a", "ctrl+c")
            self.assertEqual(app.clipboard, "请检查：" + original)
            self.assertTrue(app.is_running)
            await pilot.press("ctrl+x")
            self.assertEqual(editor.value, "")
            self.assertEqual(app.clipboard, "请检查：" + original)
            await pilot.press("ctrl+v")
            self.assertEqual(editor.prompt.expand(), "请检查：" + original)
            self.assertEqual(len(editor.attachments), 1)

    async def test_blocks_are_atomic_for_arrows_partial_selection_and_multiple_pastes(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            editor = app.query_one("#message-input", ComposerInput)
            await paste(app, pilot, "x" * 6000)
            await pilot.press("left")
            self.assertEqual(editor.cursor_position, 0)
            await pilot.press("right")
            self.assertEqual(editor.cursor_position, len(editor.value))
            await pilot.press("space")
            await paste(app, pilot, "x" * 6000)
            self.assertEqual(len(editor.attachments), 2)
            self.assertEqual(len({item.label for item in editor.attachments}), 2)
            second = editor.attachments[-1]
            editor.selection = Selection(editor.document.get_location_from_index(second.start + 3),
                                         editor.document.get_location_from_index(second.end - 2))
            await pilot.press("backspace")
            self.assertEqual(len(editor.attachments), 1)
            self.assertEqual(editor.prompt.expand(), "x" * 6000 + " ")
            editor.cursor_position = 0
            await pilot.press("delete")
            self.assertEqual(editor.value, " ")
            self.assertFalse(editor.attachments)

    async def test_expand_for_editing_is_undoable_and_small_paste_stays_plain(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(100, 32)) as pilot:
            editor = app.query_one("#message-input", ComposerInput)
            await paste(app, pilot, "small\npaste")
            self.assertEqual(editor.value, "small\npaste")
            self.assertFalse(editor.attachments)
            editor.value = ""
            original = "多行资料\n" * 8
            await paste(app, pilot, original)
            label = editor.value
            await pilot.press("alt+enter")
            self.assertEqual(editor.value, original)
            self.assertFalse(editor.attachments)
            await pilot.press("ctrl+z")
            self.assertEqual(editor.value, label)
            self.assertEqual(editor.prompt.expand(), original)

    async def test_mention_completion_keeps_payload_and_pasted_commands_are_text(self):
        app = CodexRoom(demo=True, speed=0.001)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            editor = app.query_one("#message-input", ComposerInput)
            raw = "/del a\n" + "example\n" * 200
            await paste(app, pilot, raw)
            editor.cursor_position = 0
            await pilot.press("@", "a", "tab")
            self.assertTrue(editor.value.startswith("@a "))
            self.assertEqual(len(editor.attachments), 1)
            await pilot.press("enter")
            await asyncio.gather(*app.tasks.values())
            self.assertIn("a", app.agents)
            own = next(entry for entry in app.entries if entry.sender == "you")
            self.assertEqual(own.body, raw)
            self.assertEqual(own.display_body, "[Pasted Content 1607 chars]")
            self.assertEqual(editor.value, "")
            self.assertFalse(editor.attachments)
            await pilot.press("up")
            self.assertTrue(editor.value.startswith("@a [Pasted Content"))
            self.assertEqual(editor.prompt.expand(), "@a " + raw)

    async def test_image_blocks_and_large_paste_reach_start_steer_and_restart_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            directory = Path(tmp).resolve()
            image = directory / "first image.png"
            second = directory / "second.png"
            image.touch()
            second.touch()
            state_dir = directory / "room"
            app = CodexRoom(cwd=ROOT, state_dir=state_dir)
            async with app.run_test(size=(100, 32)) as pilot:
                await app.connection_task
                await app.create_member("a")
                editor = app.query_one("#message-input", ComposerInput)
                editor.value = "steer-hold "
                editor.cursor_position = len(editor.value)
                await paste(app, pilot, str(image))
                self.assertEqual(editor.value, "steer-hold [Image #1]")
                editor.selection = Selection((0, len("steer-hold ")), (0, len(editor.value)))
                await pilot.press("ctrl+c")
                self.assertEqual(image_paths(app.clipboard, ROOT), [str(image)])
                await pilot.press("right")
                with patch.object(app.client, "request", wraps=app.client.request) as requests:
                    await pilot.press("enter")
                    await asyncio.gather(*app.tasks.values())
                    start = next(call.args[1] for call in requests.call_args_list if call.args[0] == "turn/start")
                    self.assertEqual(start["input"], [{"type": "text", "text": "steer-hold [Image #1]"},
                                                     {"type": "localImage", "path": str(image)}])
                    await app.submit("steer-hold")
                    async with asyncio.timeout(5):
                        while not app.client.active:
                            await pilot.pause(0.01)
                    raw = "資料\n" * 2000
                    await paste(app, pilot, raw)
                    await pilot.press("space")
                    await paste(app, pilot, f"'{image}' '{second}'")
                    self.assertEqual(len(editor.attachments), 3)
                    await pilot.press("enter")
                    steered = [call.args[1] for call in requests.call_args_list if call.args[0] == "turn/steer"][-1]
                    self.assertEqual(steered["input"], [{"type": "text", "text": raw + " [Image #1] [Image #2]"},
                        {"type": "localImage", "path": str(image)}, {"type": "localImage", "path": str(second)}])
                    await app.submit("finish-steer")
                    await app.tasks["a"]
                await pilot.press("up")
                self.assertEqual(editor.prompt.expand(), raw + " [Image #1] [Image #2]")
            restored = CodexRoom(cwd=ROOT, state_dir=state_dir)
            async with restored.run_test(size=(100, 32)) as pilot:
                await restored.connection_task
                editor = restored.query_one("#message-input", ComposerInput)
                await pilot.press("up")
                self.assertEqual(len(editor.attachments), 3)
                self.assertEqual(editor.prompt.expand(), raw + " [Image #1] [Image #2]")
                self.assertEqual(editor.prompt.images, [str(image), str(second)])
                self.assertEqual(editor.region.height, 1)
                image.unlink()
                before = len(restored.entries)
                await pilot.press("enter")
                self.assertEqual(len(restored.entries), before)
                self.assertEqual(len(editor.attachments), 3)
                await pilot.press("backspace")
                self.assertEqual(editor.prompt.images, [str(image)])
                # Merely typing a label never creates an image attachment.
                editor.value = "literal [Image #1]"
                self.assertFalse(editor.attachments)
