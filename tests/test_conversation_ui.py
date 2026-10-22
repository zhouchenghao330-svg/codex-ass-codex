import asyncio
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from textual import events
from textual.geometry import Region
from textual.widgets import Input

from codex_room.app import AddMember, CodexRoom
from codex_room.runtime import CodexClient
from codex_room.transcript import Entry


ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


def visible_text(widget):
    return "\n".join(strip.text for strip in widget.render_lines(
        Region(0, 0, widget.size.width, widget.size.height)))


class ConversationUITests(unittest.IsolatedAsyncioTestCase):
    async def test_click_typing_paste_and_window_focus_keep_composer_ready(self):
        app = CodexRoom(demo=True, cwd=ROOT)
        async with app.run_test(size=(100, 32)) as pilot:
            await app.create_member("a")
            card = await app.add_entry(Entry("a", "This is a selectable reply."))
            await pilot.pause()
            composer = app.query_one("#message-input", Input)
            timeline = app.query_one("#timeline")

            timeline.focus()
            await pilot.click(card.body)
            self.assertTrue(composer.has_focus)
            await pilot.press("h", "i")
            self.assertEqual(composer.value, "hi")

            # The first key and pasted text survive even if a scroll area took focus.
            timeline.focus()
            await pilot.press("!")
            self.assertTrue(composer.has_focus)
            self.assertEqual(composer.value, "hi!")
            timeline.focus()
            app.post_message(events.Paste(" pasted"))
            await pilot.pause()
            self.assertEqual(composer.value, "hi! pasted")
            timeline.focus()
            app.post_message(events.AppFocus())
            await pilot.pause()
            self.assertTrue(composer.has_focus)

            # Selecting a reply for copy must remain possible.
            timeline.focus()
            card.header.text_select_all()
            await pilot.pause()
            self.assertIn("@a", app.screen.get_selected_text())
            app.on_click(events.Click(card.header, 0, 0, 0, 0, 1, False, False, False))
            self.assertFalse(composer.has_focus)
            await pilot.press("x")
            self.assertEqual(composer.value, "hi! pastedx")
            self.assertFalse(app.screen.get_selected_text())

            # Focus events and typing stay inside an open settings/add dialog.
            await pilot.press("ctrl+n")
            self.assertIsInstance(app.screen, AddMember)
            field = app.screen.query_one("#new-name", Input)
            app.post_message(events.AppFocus())
            await pilot.press("n", "e", "w")
            self.assertTrue(field.has_focus)
            self.assertEqual(field.value, "new")
            self.assertEqual(composer.value, "hi! pastedx")
            await pilot.press("escape")
            self.assertTrue(composer.has_focus)

    async def test_sidebar_filters_one_conversation_without_stopping_others_and_restores_view(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            state = Path(tmp)
            app = CodexRoom(cwd=ROOT, state_dir=state)
            async with app.run_test(size=(110, 36)) as pilot:
                await app.connection_task
                await app.submit("/add a")
                await app.submit("/add b")
                await app.submit("@a hello-a")
                await app.tasks["a"]
                await app.submit("@b slow")
                await app.submit("@b /model test-model low")
                await pilot.press("ctrl+b")
                await pilot.click("#member-a")
                self.assertEqual(app.selected, "a")
                self.assertFalse(app.show_all)
                self.assertTrue(app.agents["b"].busy)
                self.assertTrue(app.query_one("#message-input", Input).has_focus)
                for entry in app.entries:
                    if entry.sender == "a" or entry.target == "a":
                        self.assertTrue(app.cards[id(entry)].display)
                    elif entry.sender == "b" or entry.target == "b":
                        self.assertFalse(app.cards[id(entry)].display)
                composer = app.query_one("#message-input", Input)
                composer.value = "draft"
                await pilot.click("#all-messages")
                self.assertTrue(app.show_all)
                self.assertTrue(all(card.display for card in app.cards.values()))
                self.assertTrue(app.query_one("#all-messages").has_class("selected"))
                self.assertFalse(app.query_one("#member-a").has_class("selected"))
                self.assertEqual(app.selected, "a")
                self.assertEqual(composer.value, "draft")
                self.assertTrue(composer.has_focus)
                self.assertTrue(app.agents["b"].busy)
                await pilot.pause(0.3)
                await pilot.click("#all-messages")
                self.assertTrue(app.show_all)
                await pilot.click("#member-b")
                self.assertFalse(app.show_all)
                self.assertFalse(app.query_one("#all-messages").has_class("selected"))
                self.assertTrue(app.query_one("#member-b").has_class("selected"))
                await app.stop_agent("b")
                self.assertEqual(app.entries[-1].target, "b")
                self.assertTrue(app.cards[id(app.entries[-1])].display)
            restored = CodexRoom(cwd=ROOT, state_dir=state)
            async with restored.run_test(size=(100, 30)) as pilot:
                await restored.connection_task
                self.assertEqual(restored.selected, "b")
                self.assertFalse(restored.show_all)
                self.assertTrue(all(not restored.cards[id(entry)].display for entry in restored.entries
                                    if entry.sender == "a" or entry.target == "a"))

    async def test_reply_header_directory_bottom_timing_and_history(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            state = Path(tmp)
            app = CodexRoom(cwd=ROOT, state_dir=state)
            async with app.run_test(size=(100, 36)) as pilot:
                await app.connection_task
                await app.submit("/add a")
                await app.submit("@a thinking")
                await asyncio.wait_for(app.tasks["a"], 5)
                # A fresh turn gets its own clock rather than reusing the previous duration.
                await app.submit("@a next")
                await asyncio.wait_for(app.tasks["a"], 5)
                await pilot.pause()
                answers = [entry for entry in app.agents["a"].history if entry.sender == "a"]
                first, second = answers
                self.assertGreaterEqual(first.duration_seconds, 2)
                self.assertLess(second.duration_seconds, first.duration_seconds / 2)
                for entry in answers:
                    self.assertGreaterEqual(datetime.fromisoformat(entry.finished_at), datetime.fromisoformat(entry.started_at))
                    card = app.cards[id(entry)]
                    self.assertIn("codex-ass-codex", visible_text(card.header))
                    self.assertEqual(card.header.tooltip, str(ROOT))
                    self.assertEqual(card.header.size.height, 1)
                    self.assertIn("Worked for", visible_text(card.work_summary))
                    self.assertIn(datetime.fromisoformat(entry.finished_at).strftime("%H:%M"), visible_text(card.work_summary))
                    self.assertGreaterEqual(card.work_summary.virtual_region.y, card.body.virtual_region.bottom)
                    self.assertEqual(list(card.children)[-1], card.work_summary)
                    self.assertFalse(card.progress.display)

                await pilot.resize_terminal(40, 26)
                await pilot.pause()
                header = app.cards[id(second)].header
                self.assertEqual(header.size.height, 1)
                self.assertIn("…", visible_text(header))
                self.assertIn("codex-ass-codex", visible_text(header))

                await app.submit("@a fail")
                await app.tasks["a"]
                failed = app.agents["a"].history[-1]
                await app.submit("@a slow")
                async with asyncio.timeout(3):
                    while not app.client.active:
                        await pilot.pause(0.02)
                await app.stop_agent("a")
                stopped = app.agents["a"].history[-1]
                await pilot.pause()
                self.assertIn("Failed after", visible_text(app.cards[id(failed)].work_summary))
                self.assertIn("Stopped after", visible_text(app.cards[id(stopped)].work_summary))
                # A long reply must follow through to its newly added completion line.
                await pilot.resize_terminal(60, 18)
                app.scroll_bottom()
                await app.submit("@a markdown")
                await app.tasks["a"]
                await pilot.pause()
                long_reply = app.agents["a"].history[-1]
                self.assertTrue(app.query_one("#timeline").can_view_entire(app.cards[id(long_reply)].work_summary))
                metadata = [(entry.body, entry.finished_at, entry.duration_seconds) for entry in (*answers, failed, stopped, long_reply)]
                legacy = Entry("a", "Legacy reply without timing")
                await app.add_entry(legacy)
                self.assertFalse(app.cards[id(legacy)].work_summary.display)
            restored = CodexRoom(cwd=ROOT, state_dir=state)
            async with restored.run_test(size=(100, 30)) as pilot:
                await restored.connection_task
                for body, finished, duration in metadata:
                    entry = next(item for item in restored.entries if item.sender == "a" and item.body == body)
                    self.assertEqual(entry.finished_at, finished)
                    self.assertEqual(entry.duration_seconds, duration)
                    self.assertTrue(restored.cards[id(entry)].work_summary.display)
                old = next(item for item in restored.entries if item.body == legacy.body)
                self.assertFalse(restored.cards[id(old)].work_summary.display)
