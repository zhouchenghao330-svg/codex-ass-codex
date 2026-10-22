import asyncio
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input

from codex_room.app import CodexRoom
from codex_room.runtime import CodexClient


ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


async def wait_until(predicate, pilot):
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause(0.02)


class MemberTests(unittest.IsolatedAsyncioTestCase):
    async def test_delete_running_member_archives_cancelled_queue_and_preserves_other_work(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            state_dir = Path(tmp) / "state"
            app = CodexRoom(cwd=ROOT, state_dir=state_dir)
            async with app.run_test(size=(100, 32)) as pilot:
                await app.connection_task
                await app.submit("/add a")
                await app.submit("/add b")
                await app.submit("@a slow")
                await app.submit("@a queued request")
                await app.submit("@b slow")
                await wait_until(lambda: len(app.client.active) == 2, pilot)
                old_thread = app.agents["a"].thread_id
                other_thread = app.agents["b"].thread_id
                other_task = app.tasks["b"]
                app.select("a", single=True)
                composer = app.query_one("#message-input", Input)
                composer.value = "/del "
                composer.cursor_position = len(composer.value)
                await pilot.pause()
                self.assertEqual(app.completion_matches, ["a", "b"])
                await pilot.press("tab")
                self.assertEqual(composer.value, "/del a ")
                self.assertIn("a", app.agents)
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(composer.value, "")
                self.assertEqual(list(app.agents), ["b"])
                self.assertEqual(app.selected, "b")
                self.assertFalse(app.show_all)
                self.assertNotIn("a", app.tasks)
                self.assertNotIn("a", app.active_entries)
                self.assertNotIn(old_thread, app.client.active)
                self.assertNotIn(old_thread, app.client.loaded)
                self.assertIn(other_thread, app.client.active)
                self.assertIs(app.tasks["b"], other_task)
                self.assertTrue(app.agents["b"].busy)
                self.assertFalse(app.query("#member-a"))
                self.assertFalse(any(entry.sender == "a" or entry.target == "a" for entry in app.entries))
                self.assertEqual(set(app.cards), {id(entry) for entry in app.entries})

                archive_path, = (state_dir / "archives").glob("*.json")
                archive = json.loads(archive_path.read_text())
                self.assertEqual(archive["agent"]["thread_id"], old_thread)
                self.assertEqual(archive["agent"]["cwd"], str(ROOT))
                self.assertTrue(any(entry["body"] == "queued request" and entry["state"] == "stopped"
                                    for entry in archive["entries"]))
                answer = next(entry for entry in archive["entries"] if entry["sender"] == "a")
                self.assertEqual(answer["state"], "stopped")
                self.assertIsNotNone(answer["duration_seconds"])
                self.assertEqual(stat.S_IMODE(archive_path.stat().st_mode), 0o600)
                composer.value = "@"
                composer.cursor_position = 1
                await pilot.pause()
                self.assertEqual(app.completion_matches, ["b"])
                await app.stop_agent("b")

            restored = CodexRoom(cwd=ROOT, state_dir=state_dir)
            async with restored.run_test() as pilot:
                await restored.connection_task
                self.assertEqual(list(restored.agents), ["b"])
                self.assertEqual(restored.selected, "b")
                self.assertFalse(restored.show_all)
                self.assertFalse(any(entry.sender == "a" or entry.target == "a" for entry in restored.entries))

    async def test_archive_failure_preserves_member_and_reusing_name_starts_new_session(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            project = Path(tmp) / "project"
            project.mkdir()
            marker = project / "keep.txt"
            marker.write_text("keep project files")
            state_dir = Path(tmp) / "state"
            app = CodexRoom(cwd=project, state_dir=state_dir)
            async with app.run_test() as pilot:
                await app.connection_task
                await app.submit("/add a")
                await app.submit("@a remember old context")
                await app.tasks["a"]
                old_thread = app.agents["a"].thread_id
                old_color = app.agents["a"].color
                old_entries = list(app.entries)
                self.assertFalse(await app.submit("/del"))
                self.assertFalse(await app.submit("/del missing"))
                self.assertFalse(await app.submit("/del a extra"))
                with patch.object(app.store, "archive_member", side_effect=OSError("disk unavailable")):
                    self.assertFalse(await app.submit("/del a"))
                self.assertIn("a", app.agents)
                self.assertEqual(app.selected, "a")
                self.assertTrue(all(entry in app.entries for entry in old_entries))
                self.assertEqual(app.agents["a"].thread_id, old_thread)
                self.assertTrue(await app.submit("@a /del"))
                self.assertEqual(app.selected, "")
                self.assertEqual(app.agents, {})
                self.assertTrue(app.show_all)
                self.assertTrue(app.query_one("#empty-room").display)
                self.assertEqual(json.loads(app.store.path.read_text())["agents"], [])
                self.assertEqual(marker.read_text(), "keep project files")

                self.assertTrue(await app.submit("/add a"))
                self.assertEqual(app.agents["a"].color, old_color)
                self.assertEqual(app.agents["a"].history, [])
                self.assertIsNone(app.agents["a"].thread_id)
                await app.submit("@a recall")
                await app.tasks["a"]
                self.assertIn("NO_MEMORY", app.agents["a"].history[-1].body)
                self.assertNotEqual(app.agents["a"].thread_id, old_thread)
                self.assertFalse(any(entry.body == "remember old context" for entry in app.entries))

    async def test_sidebar_has_permanent_mouse_toggle_at_left_of_footer(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(86, 26)) as pilot:
            await pilot.pause()
            toggle = app.query_one("#toggle-sidebar", Button)
            self.assertTrue(toggle.visible)
            self.assertEqual(toggle.region.x, app.query_one("#composer").region.x)
            self.assertLess(toggle.region.right, app.query_one("#recipient").region.x)
            self.assertFalse(app.sidebar_visible)
            self.assertEqual(str(toggle.label), "› 成员")
            self.assertIn("成员", toggle.render_line(0).text)
            await pilot.click(toggle)
            self.assertTrue(app.sidebar_visible)
            self.assertTrue(app.query_one("#sidebar").display)
            self.assertEqual(str(toggle.label), "‹ 收起")
            await pilot.pause(0.4)
            self.assertIn("收起", toggle.render_line(0).text)
            await pilot.click(toggle)
            self.assertFalse(app.sidebar_visible)
            self.assertFalse(app.query_one("#sidebar").display)
            self.assertEqual(str(toggle.label), "› 成员")
            self.assertTrue(app.query_one("#message-input", Input).has_focus)
