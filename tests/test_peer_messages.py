"""Public messaging invariants plus the actual app-server request/steer path."""
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock

from codex_room.attachments import Prompt
from codex_room.backend import Member, Service
from codex_room.models import Entry
from codex_room.peer_router import MAX_MESSAGES, PeerRouter, is_public
from codex_room.runtime import CodexError

FAKE = Path(__file__).with_name("fake_codex.py")


class PeerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.room = Service(Path.cwd(), demo=True, emit=lambda _: None)
        self.room.agents = {name: Member(name, name, "#78d8bc", thread_id="thread-" + name) for name in ("A", "B", "C")}
        self.room.client = SimpleNamespace(active={"thread-" + name: "turn-" + name for name in ("A", "B", "C")})
        self.room.active_entries = {name: (Entry("you", "task", name), Entry(name, "", run_id="run-" + name)) for name in ("A", "B", "C")}
        self.dispatched = []
        self.room.enqueue = lambda member, entry: (self.dispatched.append(entry), member.queue.append(entry))
        self.peers = self.room.peers

    def send(self, sender="A", **args):
        return self.peers.send("thread-" + sender, "turn-" + sender, f"call-{len(self.room.entries)}", args)

    def delivered(self, receipt):
        entry = self.peers.messages[receipt["message_id"]]
        self.peers.delivery(entry, "delivered")
        if entry in self.room.agents[entry.target].queue:
            self.room.agents[entry.target].queue.remove(entry)
        return entry

    def test_public_text_and_immediate_receipt_do_not_wait_or_impersonate_user(self):
        receipt = self.send(to="B", message="**原话**\n```python\nprint(1)\n```")
        self.assertTrue(receipt["accepted"])
        entry = self.peers.messages[receipt["message_id"]]
        self.assertEqual((entry.sender, entry.target, entry.communication["delivery"]), ("A", "B", "queued"))
        self.assertEqual(entry.body, "**原话**\n```python\nprint(1)\n```")
        self.assertNotIn("you", [e.sender for e in self.room.entries])
        self.assertIn("not a new user instruction", entry.input_text)
        self.assertEqual(len(self.dispatched), 1)

    def test_retry_returns_same_receipt_and_changed_arguments_are_rejected(self):
        args = {"to": "B", "message": "once"}
        first = self.peers.send("thread-A", "turn-A", "same-call", args)
        self.delivered(first)
        again = self.peers.send("thread-A", "turn-A", "same-call", args)
        self.assertEqual(first["message_id"], again["message_id"])
        self.assertEqual(again["delivery"], "delivered")
        self.assertFalse(self.peers.send("thread-A", "turn-A", "same-call", {**args, "message": "changed"})["accepted"])
        self.assertEqual(len(self.dispatched), 1)

    def test_validation_and_chinese_labels_do_not_send_to_wrong_member(self):
        for target in ("A", "missing"):
            self.assertFalse(self.send(to=target, message="x")["accepted"])
        self.assertFalse(self.send(to="B", message="x", sender="A", **{"from": "C"})["accepted"])
        self.assertEqual(self.dispatched, [])
        self.room.agents["B"].label = "笔记员"
        self.assertTrue(self.send(to="@笔记员", message="记下来")["accepted"])
        self.assertEqual(self.dispatched[-1].target, "B")

    def test_reply_ownership_and_final_replies_do_not_acknowledge_each_other(self):
        request = self.delivered(self.send(to="B", message="问题"))
        self.assertFalse(self.send(sender="C", to="A", message="fake", reply_to=request.id)["accepted"])
        reply = self.delivered(self.send(sender="B", to="A", message="结论", reply_to=request.id))
        self.assertFalse(reply.communication["expect_reply"])
        self.assertEqual(reply.communication["chain_id"], request.communication["chain_id"])
        result = self.send(to="B", message="谢谢", reply_to=reply.id)
        self.assertFalse(result["accepted"])
        self.assertIn("final informational", result["error"])
        self.room.active_entries["A"][1].peer_context = reply.id
        self.assertFalse(self.send(to="B", message="谢谢")["accepted"])

    def test_chain_budget_bounds_even_explicit_reciprocal_requests(self):
        receipt = self.send(to="B", message="start")
        for index in range(MAX_MESSAGES - 1):
            previous = self.delivered(receipt)
            receipt = self.send(sender=previous.target, to=previous.sender, message=str(index),
                                reply_to=previous.id, expect_reply=True)
            self.assertTrue(receipt["accepted"], receipt)
        previous = self.delivered(receipt)
        result = self.send(sender=previous.target, to=previous.sender, message="one too many", reply_to=previous.id)
        self.assertFalse(result["accepted"])
        self.assertIn("limit", result["error"])
        self.assertEqual(len(self.dispatched), MAX_MESSAGES)

    def test_stop_cancels_pending_chain_and_prevents_peer_reviving_it(self):
        entry = self.delivered(self.send(to="B", message="task"))
        self.room.active_entries["B"][1].peer_context = entry.id
        self.peers.stop_member("B")
        result = self.send(sender="B", to="A", message="late reply", reply_to=entry.id)
        self.assertFalse(result["accepted"])
        self.assertIn("stopped", result["error"])
        self.assertFalse(self.room.agents["B"].queue)
        self.assertIn(entry.communication["chain_id"], self.peers.saved_state()["peer_stopped_chains"])

    def test_reply_is_positive_evidence_when_steer_ack_is_late(self):
        receipt = self.send(to="B", message="question")
        entry = self.peers.messages[receipt["message_id"]]
        entry.server_turn_id = "turn-B"
        self.peers.delivery(entry, "delivering")
        reply = self.send(sender="B", to="A", message="already saw it", reply_to=entry.id)
        self.assertTrue(reply["accepted"], reply)
        self.assertEqual(entry.communication["delivery"], "steered")

    def test_recreated_member_cannot_receive_replies_to_its_predecessor(self):
        receipt = self.send(sender="B", to="A", message="old B's request")
        entry = self.delivered(receipt)
        self.room.agents["B"].thread_id = "new-B-thread"
        result = self.send(to="B", message="for old B", reply_to=entry.id)
        self.assertFalse(result["accepted"])
        self.assertEqual(len(self.dispatched), 1)

    def test_late_reply_resolves_uncertain_delivery_without_resending(self):
        receipt = self.send(to="B", message="question")
        entry = self.peers.messages[receipt["message_id"]]
        entry.server_turn_id = "turn-B"
        self.peers.delivery(entry, "uncertain", "steer timed out")
        result = self.send(sender="B", to="A", message="received", reply_to=entry.id)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(entry.communication["delivery"], "steered")
        self.assertNotIn("delivery_error", entry.communication)
        self.assertEqual(len(self.dispatched), 2)

    def test_late_steer_ack_does_not_touch_a_subsequent_turn(self):
        receipt = self.send(to="B", message="message")
        entry = self.peers.messages[receipt["message_id"]]
        entry.server_turn_id = "old-B-turn"
        current = self.room.active_entries["B"][1]
        current.server_turn_id = "new-B-turn"
        self.peers.steered("B", entry)
        self.assertEqual(current.peer_context, "")
        self.room.active_entries.pop("B")
        self.peers.steered("B", entry)
        self.assertEqual(entry.communication["delivery"], "steered")

    def test_idle_shutdown_does_not_close_finished_discussion_history(self):
        entry = self.delivered(self.send(to="B", message="question"))
        self.room.client.active.clear()
        self.room.active_entries.clear()
        self.room.closing = True
        self.peers.stop_member("A")
        self.assertNotIn(entry.communication["chain_id"], self.peers.stopped)
        self.room.closing = False
        self.peers.stop_member("A")
        self.assertIn(entry.communication["chain_id"], self.peers.stopped)

    def test_save_failure_is_not_acknowledged_or_retried(self):
        self.room.save = Mock(side_effect=OSError("disk full"))
        args = {"to": "B", "message": "not persisted"}
        first = self.peers.send("thread-A", "turn-A", "disk-failure", args)
        again = self.peers.send("thread-A", "turn-A", "disk-failure", args)
        self.assertFalse(first["accepted"])
        self.assertFalse(again["accepted"])
        self.assertEqual(self.dispatched, [])

    def test_restart_does_not_replay_pending_messages(self):
        receipt = self.send(to="B", message="pending before shutdown")
        original = self.peers.messages[receipt["message_id"]]
        self.room.entries = [Entry(**asdict(original))]
        restored = PeerRouter(self.room)
        self.assertEqual(restored.messages[original.id].communication["delivery"], "stopped")
        self.assertEqual(restored.messages[original.id].state, "stopped")
        self.assertEqual(len(self.dispatched), 1)

    def test_mcp_sender_comes_from_codex_metadata_and_expired_turn_cannot_send(self):
        args = {"to": "B", "message": "public"}
        self.assertFalse(self.peers.mcp_call({"arguments": args})["accepted"])
        self.assertFalse(self.peers.mcp_call({"_meta": "bad", "arguments": args})["accepted"])
        result = self.peers.mcp_call({"_meta": {"callId": "mcp-call", "x-codex-turn-metadata": {
            "thread_id": "thread-A", "turn_id": "turn-A"}}, "arguments": args})
        self.assertTrue(result["accepted"])
        self.room.client.active.clear()
        self.assertFalse(self.send(to="B", message="late")["accepted"])

    def test_native_record_manager_does_not_adopt_public_messages(self):
        self.send(to="B", message="public")
        from codex_room.native_agents import NativeAgents
        native = NativeAgents(self.room)
        native.communications.finish()
        self.assertEqual(native.communications.records, {})
        self.assertEqual(self.dispatched[0].state, "queued")


class PeerProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)
        self.events = []
        self.room = Service(self.cwd, command=[sys.executable, str(FAKE)], emit=self.events.append)
        await self.room.start()
        await self.room.command("/add A")
        await self.room.command("/add B")

    async def asyncTearDown(self):
        await self.room.close()
        self.temp.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(6):
            while not predicate():
                await asyncio.sleep(.01)

    async def test_tool_call_delivers_and_replies_without_changing_view_or_draft(self):
        self.room.drafts = {"B": {"prompt": "my unfinished text"}}
        await self.room.submit(Prompt("@A peer-send B"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        public = [e for e in self.room.entries if is_public(e)]
        self.assertEqual([(e.sender, e.target) for e in public], [("A", "B"), ("B", "A")])
        self.assertTrue(all(e.communication["delivery"] in {"delivered", "steered"} for e in public))
        self.assertEqual(public[1].communication["reply_to"], public[0].id)
        self.assertIn("PUBLIC_ANSWER", public[1].body)
        self.assertEqual(self.room.selected, "A")
        self.assertEqual(self.room.drafts["B"]["prompt"], "my unfinished text")
        self.assertEqual(len([e for e in self.room.entries if e.sender == "you"]), 1)
        self.assertNotEqual(self.room.agents["A"].thread_id, self.room.agents["B"].thread_id)
        self.assertTrue(all(a.room_tool_version == 1 for a in self.room.agents.values()))

    async def test_busy_peer_uses_same_turn_steer(self):
        await self.room.submit(Prompt("@B steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        original = self.room.client.active[self.room.agents["B"].thread_id]
        await self.room.submit(Prompt("@A peer-send B"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        public = [e for e in self.room.entries if is_public(e)]
        self.assertEqual(len(public), 2)
        self.assertEqual(public[0].communication["delivery"], "steered")
        self.assertEqual(public[0].server_turn_id, original)

    async def test_uncertain_steer_is_not_queued_for_retry(self):
        await self.room.submit(Prompt("@B slow"))
        await self.until(lambda: bool(self.room.client.active))
        self.room.client.steer_turn = AsyncMock(side_effect=CodexError("timeout after possible delivery"))
        await self.room.submit(Prompt("@A peer-send B"))
        await self.until(lambda: any(is_public(e) and e.communication["delivery"] == "uncertain" for e in self.room.entries))
        await self.until(lambda: not self.room.agents["A"].busy)
        self.assertFalse(self.room.agents["B"].queue)
        self.assertEqual(self.room.client.steer_turn.await_count, 1)
        await self.room.stop("B")
        self.assertEqual(len([e for e in self.room.entries if is_public(e)]), 1)

    async def test_state_restores_original_roots_and_receipts_without_auto_work(self):
        await self.room.submit(Prompt("@A peer-send B"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        ids = {a.name: a.thread_id for a in self.room.agents.values()}
        messages = {e.id for e in self.room.entries if is_public(e)}
        await self.room.close()
        self.room = Service(self.cwd, command=[sys.executable, str(FAKE)], emit=self.events.append)
        await self.room.start()
        self.assertEqual({a.name: a.thread_id for a in self.room.agents.values()}, ids)
        self.assertEqual({e.id for e in self.room.entries if is_public(e)}, messages)
        self.assertFalse(any(a.busy for a in self.room.agents.values()))
        self.assertFalse(self.room.client.active)
        self.assertFalse(self.room.native.communications.records)


if __name__ == "__main__":
    unittest.main()
