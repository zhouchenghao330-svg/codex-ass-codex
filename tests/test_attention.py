"""Completed-result attention survives restart and rejects stale read receipts."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest

from codex_room.backend import Member, Service
from codex_room.models import Entry


class AttentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.events = []
        self.room = Service(self.cwd, emit=self.events.append)
        self.addCleanup(lambda: self.room.store.close())
        self.room.agents = {name: Member(name, name, "#78d8bc", str(self.cwd)) for name in ("A", "B")}

    def reopen(self):
        self.room.store.close()
        self.room = Service(self.cwd, emit=self.events.append)
        return self.room

    def complete(self, name="B", **kwargs):
        entry = self.room.add_entry(Entry(name, "result", state="working", **kwargs))
        self.assertNotIn(entry.id, self.room.attention.pending.values())
        entry.state = "done"
        entry.record_finish(1)
        self.room.refresh_answer(entry)
        return entry

    async def test_completion_restart_read_and_late_receipt_are_consistent(self):
        first = self.complete()
        self.room.refresh_answer(first)
        self.room.save()
        restored = self.reopen()
        self.assertEqual(restored.attention.pending, {"B": first.id})
        newer = self.complete()
        await self.room.handle({"type": "read-result", "member": "B", "entry_id": first.id})
        self.assertEqual(self.room.attention.pending, {"B": newer.id})
        await self.room.handle({"type": "read-result", "member": "A", "entry_id": newer.id})
        self.assertEqual(self.room.attention.pending, {"B": newer.id})
        await self.room.handle({"type": "read-result", "member": "B", "entry_id": newer.id})
        self.assertFalse(self.room.attention.pending)
        self.assertFalse(self.reopen().attention.pending)
        self.room.refresh_answer(newer)
        self.assertFalse(self.room.attention.pending)

    async def test_history_import_and_hidden_or_public_transport_cards_are_not_new_results(self):
        old = Entry("B", "old", state="done", finished_at=(datetime.now().astimezone() - timedelta(days=1)).isoformat())
        self.room.add_entry(old)
        self.complete(hidden=True)
        self.complete(communication={"transport": "room"})
        self.complete(name="you")
        self.assertFalse(self.room.attention.pending)
        latest = self.complete()
        self.room.agents["B"].busy = True  # A next task doesn't erase the previous unread result.
        self.room.members()
        member = next(m for m in self.events[-1]["agents"] if m["name"] == "B")
        self.assertEqual(member["unread_entry_id"], latest.id)
        self.assertTrue(member["busy"])
        del self.room.agents["B"]
        self.room.members()
        self.assertFalse(self.room.attention.pending)

    async def test_failure_is_pending_and_existing_legacy_history_starts_read(self):
        entry = self.complete()
        entry.state = "failed"
        self.room.refresh_answer(entry)
        self.room.save()
        self.assertEqual(self.reopen().attention.pending, {"B": entry.id})
        self.room.saved.pop("unread_results", None)
        self.room.store.save(self.room.saved)
        legacy = self.reopen()
        legacy.refresh_answer(legacy.entries[0])
        self.assertFalse(legacy.attention.pending)
