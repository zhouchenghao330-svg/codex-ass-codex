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
from codex_room.peer_router import MAX_MESSAGES, MAX_PENDING_REMINDERS, PeerRouter, is_public
from codex_room.peer_tools import SCHEMA, TOOL_VERSION
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
        args.setdefault("expect_reply", True)
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
        args = {"to": "B", "message": "once", "expect_reply": True}
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

    def test_expect_reply_is_required_in_schema_and_router_without_any_default(self):
        self.assertIn("expect_reply", SCHEMA["required"])
        for index, invalid in enumerate(({}, {"expect_reply": None}, {"expect_reply": 0}, {"expect_reply": "false"})):
            result = self.peers.send("thread-A", "turn-A", f"invalid-{index}",
                                     {"to": "B", "message": "x", **invalid})
            self.assertFalse(result["accepted"], result)
            self.assertIn("There is no default", result["error"])
        self.assertEqual(self.dispatched, [])
        self.assertFalse(self.peers.messages)

    def test_delivery_mode_defaults_to_queue_and_explicit_steer_is_recorded(self):
        queued = self.send(to="B", message="new help")
        self.assertEqual(queued["delivery_mode"], "queue")
        receipt = self.send(to="C", message="current correction", delivery_mode="steer")
        self.assertTrue(receipt["accepted"])
        self.assertEqual(receipt["delivery_mode"], "steer")
        self.assertEqual(self.peers.messages[receipt["message_id"]].communication["delivery_mode"], "steer")
        for mode in (None, True, "immediate", []):
            self.assertFalse(self.send(to="B", message="invalid", delivery_mode=mode)["accepted"])

    def test_steer_cannot_bypass_false_after_another_peer_message_in_same_user_scope(self):
        old = self.delivered(self.send(to="B", message="earlier question"))
        final = self.delivered(self.send(to="B", message="finished", expect_reply=False))
        final.communication["recipient_scope"] = "B-user-task"
        other = self.delivered(self.send(sender="C", to="B", message="another peer", expect_reply=True))
        answer = self.room.active_entries["B"][1]
        answer.task_id, answer.peer_context = "B-user-task", other.id
        for reply_to in (final.id, old.id, None):
            result = self.send(sender="B", to="A", message="thanks", reply_to=reply_to, delivery_mode="steer")
            self.assertFalse(result["accepted"], result)
            self.assertIn("expect_reply=false", result["error"])
        # An old queued true request can start a later turn after false has
        # already arrived via steer. It still cannot reopen that same chain.
        answer.task_id, answer.peer_context = old.communication["chain_id"], old.id
        self.assertFalse(self.send(sender="B", to="A", message="late old reply", reply_to=old.id)["accepted"])
        answer.task_id, answer.peer_context = "new-user-input", ""
        self.assertTrue(self.send(sender="B", to="A", message="new task", delivery_mode="steer")["accepted"])

    def test_reply_ownership_and_false_block_before_creating_or_dispatching_message(self):
        request = self.delivered(self.send(to="B", message="问题"))
        self.assertFalse(self.send(sender="C", to="A", message="fake", reply_to=request.id)["accepted"])
        reply = self.delivered(self.send(sender="B", to="A", message="结论", reply_to=request.id, expect_reply=False))
        self.assertFalse(reply.communication["expect_reply"])
        self.assertEqual(reply.communication["chain_id"], request.communication["chain_id"])
        self.assertIn("blocked by the host", reply.input_text)
        result = self.send(to="B", message="谢谢", reply_to=reply.id, expect_reply=True)
        self.assertFalse(result["accepted"], result)
        self.assertIn("expect_reply=false", result["error"])
        self.room.active_entries["A"][1].peer_context = reply.id
        self.assertFalse(self.send(to="B", message="谢谢")["accepted"])
        self.assertEqual(len(self.dispatched), 2)
        self.assertEqual(len(self.room.entries), 2)
        self.assertEqual(len(self.peers.messages), 2)
        self.assertFalse(self.room.agents["B"].queue)

    def test_counting_to_eleven_then_false_blocks_replies_and_older_message_bypass(self):
        receipt = self.send(to="B", message="1", expect_reply=True)
        for number in range(2, 12):
            previous = self.delivered(receipt)
            receipt = self.send(sender=previous.target, to=previous.sender, message=str(number),
                                reply_to=previous.id, expect_reply=number < 11)
            self.assertTrue(receipt["accepted"], receipt)
        final = self.delivered(receipt)
        self.assertEqual([e.body for e in self.dispatched], [str(n) for n in range(1, 12)])
        self.assertEqual(len({e.communication["chain_id"] for e in self.dispatched}), 1)
        self.assertFalse(final.communication["expect_reply"])
        self.room.active_entries["B"][1].peer_context = final.id
        earlier_true = self.dispatched[-3]
        self.assertTrue(earlier_true.communication["expect_reply"])
        for reply_to in (final.id, earlier_true.id, None):
            result = self.send(sender="B", to="A", message="收到，谢谢", reply_to=reply_to)
            self.assertFalse(result["accepted"], result)
            self.assertIn("expect_reply=false", result["error"])
        self.assertEqual(len(self.dispatched), 11)
        self.assertEqual(len(self.room.entries), 11)
        self.assertFalse(self.room.agents["A"].queue)

    def test_new_user_task_can_contact_the_same_member_after_a_false_message(self):
        final = self.delivered(self.send(sender="B", to="A", message="finished", expect_reply=False))
        self.room.active_entries["A"][1].peer_context = final.id
        self.assertFalse(self.send(to="B", message="thanks")["accepted"])
        self.room.active_entries["A"] = (Entry("you", "new task", "A"), Entry("A", "", run_id="new-user-run"))
        receipt = self.send(to="B", message="a new user-requested task")
        self.assertTrue(receipt["accepted"], receipt)
        self.assertEqual(self.peers.messages[receipt["message_id"]].communication["chain_id"], "new-user-run")

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

    def test_shutdown_keeps_all_peer_chains_in_an_active_mixed_turn_stopped(self):
        first = self.delivered(self.send(to="B", message="first request", delivery_mode="steer"))
        second = self.delivered(self.send(sender="C", to="B", message="second request", delivery_mode="steer"))
        answer = self.room.active_entries["B"][1]
        first.run_id = second.run_id = answer.run_id
        answer.peer_context = second.id
        self.room.active_entries = {"B": self.room.active_entries["B"]}
        self.room.client.active = {"thread-B": "turn-B"}
        self.room.closing = True
        self.peers.stop_member("A")
        self.assertIn(first.communication["chain_id"], self.peers.stopped)

    def test_save_failure_is_not_acknowledged_or_retried(self):
        self.room.save = Mock(side_effect=OSError("disk full"))
        args = {"to": "B", "message": "not persisted", "expect_reply": True}
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
        args = {"to": "B", "message": "public", "expect_reply": True}
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

    def test_reply_reminder_survives_restart_and_clears_on_accepted_reply_even_if_uncertain(self):
        request = self.delivered(self.send(to="B", message="先调研，再回答我的问题"))
        pending = self.peers.pending_replies(self.room.agents["B"])
        self.assertEqual(pending["total"], 1)
        self.assertEqual(pending["requests"][0]["message_id"], request.id)
        restored = PeerRouter(self.room)
        self.assertEqual(restored.pending_replies(self.room.agents["B"]), pending)
        reply = self.send(sender="B", to="A", message="已查明原因", reply_to=request.id, expect_reply=False)
        self.peers.delivery(self.peers.messages[reply["message_id"]], "uncertain")
        self.assertEqual(self.peers.pending_replies(self.room.agents["B"])["total"], 0)

    def test_reminders_exclude_queued_false_stopped_and_recreated_members(self):
        self.send(to="B", message="not delivered yet")
        self.delivered(self.send(to="B", message="no reply needed", expect_reply=False))
        self.assertEqual(self.peers.pending_replies(self.room.agents["B"])["total"], 0)
        request = self.delivered(self.send(to="B", message="question"))
        self.room.agents["A"].thread_id = "replacement-A"
        self.assertEqual(self.peers.pending_replies(self.room.agents["B"])["total"], 0)
        self.room.agents["A"].thread_id = "thread-A"
        self.room.agents["B"].thread_id = "replacement-B"
        self.assertEqual(self.peers.pending_replies(self.room.agents["B"])["total"], 0)
        self.room.agents["B"].thread_id = "thread-B"
        self.peers.stopped.add(request.communication["chain_id"])
        self.assertEqual(self.peers.pending_replies(self.room.agents["B"])["total"], 0)

    def test_context_refreshes_identity_permissions_and_bounded_outstanding_requests(self):
        for number in range(MAX_PENDING_REMINDERS):
            self.delivered(self.send(to="B", message=f"question {number}: " + "x" * 1000))
        self.room.client.active["thread-A"] = "next-turn-A"
        extra = self.peers.send("thread-A", "next-turn-A", "extra", {"to": "B", "message": "one more", "expect_reply": True})
        self.delivered(extra)
        self.room.agents["A"].label = "改名后的成员"
        self.room.agents["B"].label = "笔记"
        self.room.agents["B"].permission = "read-only"
        context = self.peers.context("thread-B")
        self.assertIn("@笔记 (stable name B)", context)
        self.assertIn("mode=read-only", context)
        self.assertIn("改名后的成员", context)
        pending = self.peers.pending_replies(self.room.agents["B"])
        self.assertEqual(pending["total"], MAX_PENDING_REMINDERS + 1)
        self.assertEqual(len(pending["requests"]), MAX_PENDING_REMINDERS)
        self.assertTrue(all(len(p["message"]) > 1000 for p in pending["requests"]))
        self.assertEqual(pending["next_cursor"], MAX_PENDING_REMINDERS)


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
        self.assertTrue(all(e.communication["delivery"] == "delivered" for e in public))
        self.assertEqual(public[1].communication["reply_to"], public[0].id)
        self.assertIn("PUBLIC_ANSWER", public[1].body)
        self.assertEqual(self.room.selected, "A")
        self.assertEqual(self.room.drafts["B"]["prompt"], "my unfinished text")
        self.assertEqual(len([e for e in self.room.entries if e.sender == "you"]), 1)
        self.assertNotEqual(self.room.agents["A"].thread_id, self.room.agents["B"].thread_id)
        self.assertTrue(all(a.room_tool_version == TOOL_VERSION for a in self.room.agents.values()))

    async def test_busy_peer_queues_while_user_steers_past_it_then_peer_starts_new_turn(self):
        requests = AsyncMock(wraps=self.room.client.request)
        self.room.client.request = requests
        await self.room.submit(Prompt("@B steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        original = self.room.client.active[self.room.agents["B"].thread_id]
        await self.room.submit(Prompt("@A peer-send B"))
        await self.until(lambda: not self.room.agents["A"].busy)
        public = [e for e in self.room.entries if is_public(e)]
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0].communication["delivery"], "queued")
        self.assertEqual(self.room.client.active[self.room.agents["B"].thread_id], original)
        self.assertNotIn("B", self.room.reply_boundaries)
        self.assertFalse(any(call.args[0] == "turn/steer" for call in requests.call_args_list))
        await self.room.submit(Prompt("@B USER_FOLLOWUP"))
        user = self.room.entries[-1]
        await self.until(lambda: user.state == "steered")
        self.assertEqual(user.server_turn_id, original)
        self.assertEqual(list(self.room.agents["B"].queue), public)
        await self.room.submit(Prompt("@B finish-steer"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        public = [e for e in self.room.entries if is_public(e)]
        self.assertEqual(len(public), 2)
        self.assertTrue(all(e.communication["delivery"] == "delivered" for e in public))
        self.assertNotEqual(public[0].server_turn_id, original)
        steers = [call for call in requests.call_args_list if call.args[0] == "turn/steer"]
        self.assertEqual(len(steers), 2)
        self.assertTrue(all(not any("[Codex Room public peer message;" in item.get("text", "")
                          for item in call.args[1]["input"]) for call in steers))

    async def test_busy_sender_also_queues_peer_reply(self):
        await self.room.submit(Prompt("@A peer-send-hold B"))
        await self.until(lambda: len(self.room.peers.messages) == 2)
        reply = list(self.room.peers.messages.values())[-1]
        original = self.room.client.active[self.room.agents["A"].thread_id]
        self.assertEqual(reply.communication["delivery"], "queued")
        self.assertEqual(list(self.room.agents["A"].queue), [reply])
        await self.room.submit(Prompt("@A finish-steer"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        self.assertEqual(reply.communication["delivery"], "delivered")
        self.assertNotEqual(reply.server_turn_id, original)

    async def test_explicit_peer_steer_reaches_busy_peer_in_same_turn(self):
        await self.room.submit(Prompt("@B steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        original = self.room.client.active[self.room.agents["B"].thread_id]
        await self.room.submit(Prompt("@A peer-steer B"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        request, reply = list(self.room.peers.messages.values())
        self.assertEqual((request.communication["delivery_mode"], request.communication["delivery"]), ("steer", "steered"))
        self.assertEqual(request.server_turn_id, original)
        self.assertEqual(reply.communication["reply_to"], request.id)
        self.assertFalse(reply.communication["expect_reply"])

    async def test_user_steers_before_peer_steer_and_queued_peer_does_not_block_either(self):
        await self.room.command("/add C")
        requests = AsyncMock(wraps=self.room.client.request)
        self.room.client.request = requests
        await self.room.submit(Prompt("@B steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        await self.room.submit(Prompt("@A peer-send B"))
        await self.until(lambda: not self.room.agents["A"].busy)
        async with self.room.agents["B"].delivery_lock:
            await self.room.submit(Prompt("@C peer-steer B"))
            await self.until(lambda: len(self.room.peers.messages) == 2)
            await self.room.submit(Prompt("@B USER_PRIORITY"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        steers = [call.args[1]["input"][-1]["text"] for call in requests.call_args_list if call.args[0] == "turn/steer"]
        self.assertEqual(steers[0], "USER_PRIORITY")
        self.assertTrue(steers[1].startswith("[Codex Room public peer message;"))
        first = next(iter(self.room.peers.messages.values()))
        self.assertEqual(first.communication["delivery_mode"], "queue")
        self.assertEqual(first.communication["delivery"], "delivered")

    async def test_peer_steer_falls_back_to_one_new_turn_after_definite_completion(self):
        await self.room.submit(Prompt("@B steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        original = self.room.client.active[self.room.agents["B"].thread_id]
        await self.room.submit(Prompt("@A peer-steer-race B"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        messages = list(self.room.peers.messages.values())
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0].communication["delivery"], "delivered")
        self.assertNotEqual(messages[0].server_turn_id, original)

    async def test_peer_reply_before_late_steer_ack_is_not_resent_or_attached_to_new_turn(self):
        await self.room.submit(Prompt("@B steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        original = self.room.client.active[self.room.agents["B"].thread_id]
        await self.room.submit(Prompt("@A peer-steer-late-ack B"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        request, reply = list(self.room.peers.messages.values())
        self.assertEqual(request.communication["delivery"], "steered")
        self.assertEqual(request.server_turn_id, original)
        self.assertEqual(reply.communication["reply_to"], request.id)

    async def test_uncertain_peer_steer_is_not_retried(self):
        await self.room.submit(Prompt("@B slow"))
        await self.until(lambda: bool(self.room.client.active))
        self.room.client.steer_turn = AsyncMock(side_effect=CodexError("timeout after possible delivery"))
        await self.room.submit(Prompt("@A peer-steer B"))
        await self.until(lambda: not self.room.agents["A"].busy)
        public = next(iter(self.room.peers.messages.values()))
        self.assertEqual(public.communication["delivery"], "uncertain")
        self.assertFalse(self.room.agents["B"].queue)
        self.assertEqual(self.room.client.steer_turn.await_count, 1)
        await self.room.stop("B")

    async def test_list_members_tool_returns_actual_recent_dialogue_without_public_delivery(self):
        await self.room.submit(Prompt("@B DISCOVERY_QUESTION"))
        await self.until(lambda: not self.room.agents["B"].busy)
        await self.room.submit(Prompt("@A list-members B"))
        await self.until(lambda: not self.room.agents["A"].busy)
        body = next(e.body for e in reversed(self.room.entries) if e.sender == "A")
        self.assertIn("MEMBER_DISCOVERY:", body)
        result = json.loads(body.split("MEMBER_DISCOVERY:", 1)[1])
        dialogue = result["members"][0]["recent_dialogue"][0]
        self.assertEqual(dialogue["user"]["text"], "DISCOVERY_QUESTION")
        self.assertIn("DISCOVERY_QUESTION", dialogue["codex_preview"]["text"])
        self.assertFalse(self.room.peers.messages)

    async def test_user_still_precedes_queued_peer_when_old_turn_ends_before_steer(self):
        requests = AsyncMock(wraps=self.room.client.request)
        self.room.client.request = requests
        await self.room.submit(Prompt("@B steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        thread = self.room.agents["B"].thread_id
        await self.room.submit(Prompt("@A peer-send B"))
        await self.until(lambda: not self.room.agents["A"].busy)
        await self.room.submit(Prompt("@B race-to-next"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        starts = [call.args[1] for call in requests.call_args_list
                  if call.args[0] == "turn/start" and call.args[1]["threadId"] == thread]
        tasks = [params["input"][-1]["text"] for params in starts]
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks[:2], ["steer-hold", "race-to-next"])
        self.assertTrue(tasks[2].startswith("[Codex Room public peer message;"))
        self.assertEqual(len(self.room.peers.messages), 2)

    async def test_actual_tool_call_that_ignores_false_gets_error_without_delivering(self):
        await self.room.submit(Prompt("@A peer-force-ack B"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        public = [e for e in self.room.entries if is_public(e)]
        self.assertEqual([(e.sender, e.target) for e in public], [("A", "B"), ("B", "A")])
        self.assertFalse(public[-1].communication["expect_reply"])
        self.assertTrue(any("PEER_ACK_BLOCKED:True" in e.body for e in self.room.entries))
        self.assertTrue(any(b.kind == "tool" and b.status == "failed" for e in self.room.entries for b in e.blocks))
        self.assertFalse(any(member.queue for member in self.room.agents.values()))

    async def test_uncertain_user_steer_is_not_retried_or_discarding_queued_peer(self):
        await self.room.submit(Prompt("@B slow"))
        await self.until(lambda: bool(self.room.client.active))
        self.room.client.steer_turn = AsyncMock(side_effect=CodexError("timeout after possible delivery"))
        await self.room.submit(Prompt("@A peer-send B"))
        await self.until(lambda: not self.room.agents["A"].busy)
        self.room.client.steer_turn.assert_not_awaited()
        await self.room.submit(Prompt("@B USER_FOLLOWUP"))
        user = self.room.entries[-1]
        await self.until(lambda: user.state == "failed")
        queued = list(self.room.agents["B"].queue)
        self.assertEqual(len(queued), 1)
        self.assertTrue(is_public(queued[0]))
        self.assertEqual(queued[0].communication["delivery"], "queued")
        self.assertEqual(self.room.client.steer_turn.await_count, 1)
        await self.room.stop("B")
        self.assertEqual(len([e for e in self.room.entries if is_public(e)]), 1)

    async def test_endless_peer_requests_are_stopped_at_chain_budget_without_model_cooperation(self):
        await self.room.submit(Prompt("@A peer-bounce B"))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))
        messages = list(self.room.peers.messages.values())
        self.assertEqual(len(messages), MAX_MESSAGES)
        self.assertEqual(len({e.task_id for e in messages}), 1)
        self.assertTrue(all(e.communication["expect_reply"] for e in messages))
        self.assertTrue(any(b.status == "failed" and "limit" in b.text
                            for e in self.room.entries for b in e.blocks))
        self.assertFalse(any(member.queue for member in self.room.agents.values()))
        self.assertTrue(all(self.room.peers.pending_replies(member)["total"] == 0
                            for member in self.room.agents.values()))

    async def test_stop_removes_queued_peer_request_without_waking_recipient(self):
        await self.room.submit(Prompt("@B steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        original = self.room.client.active[self.room.agents["B"].thread_id]
        await self.room.submit(Prompt("@A peer-send B"))
        await self.until(lambda: not self.room.agents["A"].busy)
        message = next(iter(self.room.peers.messages.values()))
        await self.room.stop("A")
        self.assertEqual(message.communication["delivery"], "stopped")
        self.assertFalse(self.room.agents["B"].queue)
        self.assertEqual(self.room.client.active[self.room.agents["B"].thread_id], original)
        await self.room.submit(Prompt("@B finish-steer"))
        await self.until(lambda: not self.room.agents["B"].busy)
        self.assertEqual(len(self.room.peers.messages), 1)

    async def test_new_user_input_separates_chain_before_steer_ack_and_old_stop(self):
        await self.room.command("/add C")
        for name, text in (("A", "slow"), ("C", "slow"), ("B", "steer-hold")):
            await self.room.submit(Prompt(f"@{name} {text}"))
        await self.until(lambda: len(self.room.client.active) == 3)
        thread = self.room.agents["B"].thread_id
        turn = self.room.client.active[thread]
        old = self.room.peers.send(thread, turn, "old-task", {"to": "A", "message": "old request", "expect_reply": True})
        old_chain = self.room.peers.messages[old["message_id"]].communication["chain_id"]
        accepted, release = asyncio.Event(), asyncio.Event()
        original = self.room.client.steer_turn
        async def late_ack(*args, **kwargs):
            result = await original(*args, **kwargs)
            accepted.set()
            await release.wait()
            return result
        self.room.client.steer_turn = late_ack
        try:
            await self.room.submit(Prompt("@B independent-user-task"))
            user = self.room.entries[-1]
            await asyncio.wait_for(accepted.wait(), 2)
            self.assertEqual(user.state, "queued")  # ACK is still pending.
            receipt = self.room.peers.send(thread, turn, "new-task", {"to": "C", "message": "new request", "expect_reply": True})
            self.assertTrue(receipt["accepted"], receipt)
            new = self.room.peers.messages[receipt["message_id"]]
            self.assertEqual(new.communication["chain_id"], user.id)
            self.assertNotEqual(new.communication["chain_id"], old_chain)
        finally:
            release.set()
        await self.until(lambda: user.state == "steered")
        self.assertEqual(user.server_turn_id, turn)
        await self.room.stop("A")
        self.assertIn(old_chain, self.room.peers.stopped)
        self.assertNotIn(new.communication["chain_id"], self.room.peers.stopped)
        self.assertEqual(new.communication["delivery"], "queued")
        self.assertIn(new, self.room.agents["C"].queue)

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
