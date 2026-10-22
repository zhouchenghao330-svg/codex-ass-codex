import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from codex_room.attachments import Prompt
from codex_room.backend import Service, wire_entry
from codex_room.models import Entry
from codex_room.quotes import ForwardQuote
from codex_room.runtime import CodexError

PEER = Path(__file__).with_name("fake_native_codex.py")


class NativeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)
        self.events = []
        self.room = self.new_room()
        await self.room.start()
        await self.room.command("/add A")

    def new_room(self):
        return Service(self.cwd, command=[sys.executable, str(PEER)], emit=self.events.append)

    async def asyncTearDown(self):
        await self.room.close()
        self.temp.cleanup()

    async def until(self, predicate, timeout=6):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.01)

    async def child(self, message="spawn"):
        await self.room.submit(Prompt("@A " + message))
        await self.until(lambda: any(a.parent_name for a in self.room.agents.values()))
        child = next(a for a in self.room.agents.values() if a.parent_name)
        await self.until(lambda: child.thread_id in self.room.native.attached)
        return child

    async def settled(self):
        await self.until(lambda: not any(a.busy for a in self.room.agents.values()) and not self.room.native.jobs)
        await self.room.native.queue.join()

    async def send_child(self, child, text):
        await self.room.submit(Prompt(f"@{child.label} {text}"))
        request = next(e for e in reversed(self.room.entries) if e.sender == "you" and e.target == child.name)
        await self.until(lambda: request.state != "queued")
        await self.settled()
        return request

    async def test_native_identity_multiround_restore_and_tool_paging(self):
        child = await self.child()
        await self.settled()
        self.assertFalse(child.can_accept_direct_input)
        self.assertEqual(child.model, "child-model")
        tid, name = child.thread_id, child.name
        self.assertIn("CHILD:ORIGINAL_CONTEXT", next(e.body for e in self.room.entries if e.sender == name))
        await self.send_child(child, "remember retained-native-context")
        request = await self.send_child(child, "recall")
        self.assertEqual(request.state, "done")
        self.assertEqual(child.thread_id, tid)
        self.assertIn("retained-native-context", next(e.body for e in reversed(self.room.entries) if e.sender == name))
        await self.send_child(child, "tools")
        answer = next(e for e in reversed(self.room.entries) if e.sender == name)
        data = wire_entry(answer)
        self.assertEqual(next(b for b in data["blocks"] if b["kind"] == "command")["status"], "failed")
        self.assertEqual(next(b for b in data["blocks"] if b["kind"] == "file")["files"][0]["added"], 1)
        self.assertEqual(answer.duration_seconds, .123)
        await self.room.close()
        self.room = self.new_room()
        await self.room.start()
        self.assertTrue(self.room.connected)
        self.assertEqual(self.room.agents[name].thread_id, tid)
        entries = [e for e in self.room.entries if e.native_thread_id]
        self.assertEqual(len(entries), len({(e.native_thread_id, e.server_turn_id) for e in entries}))
        request = await self.send_child(self.room.agents[name], "recall")
        self.assertEqual(request.state, "done")
        self.assertIn("retained-native-context", next(e.body for e in reversed(self.room.entries) if e.sender == name))

    async def test_approval_before_attach_finishes_is_presented(self):
        child = await self.child("spawn-approval")
        await self.until(lambda: self.room.interactions)
        key, (_, _, member) = next(iter(self.room.interactions.items()))
        self.assertEqual(member, child.name)
        await self.room.handle({"type": "answer", "id": key, "result": {"decision": "accept"}})
        await self.settled()
        self.assertIn('"decision": "accept"', next(e.body for e in reversed(self.room.entries) if e.sender == child.name))
        self.assertFalse(self.room.interactions)

    async def test_failed_relay_exposes_parent_reason_and_never_resends(self):
        child = await self.child()
        await self.settled()
        request = await self.send_child(child, "no-relay")
        self.assertEqual(request.state, "failed")
        root_replies = [e for e in self.room.entries if e.relay_for == request.id and e.sender == "A"]
        self.assertTrue(root_replies and not root_replies[-1].hidden)
        self.assertEqual(len([e for e in self.room.entries if e.sender == "you" and e.relay_for == request.id]), 1)

    async def test_busy_parent_activity_before_steer_ack_and_stop(self):
        child = await self.child()
        await self.settled()
        await self.room.submit(Prompt("@A hold"))
        await self.until(lambda: self.room.client.active.get(self.room.agents["A"].thread_id))
        await self.room.submit(Prompt(f"@{child.label} recall"))
        await self.until(lambda: any(e.sender == child.name and "ORIGINAL_CONTEXT" in e.body and e.server_turn_id != "t-2" for e in self.room.entries))
        await self.room.stop("A")
        await self.settled()
        request = next(e for e in reversed(self.room.entries) if e.sender == "you" and e.target == child.name)
        self.assertEqual(request.state, "done")

    async def test_stop_cancels_waiting_relay_and_delete_suppresses_rediscovery(self):
        child = await self.child()
        await self.settled()
        await self.room.submit(Prompt(f"@{child.label} slow"))
        await self.until(lambda: child.busy)
        await self.room.submit(Prompt(f"@{child.label} must-not-restart"))
        await self.room.stop(child.name)
        await self.settled()
        self.assertFalse(child.busy)
        self.assertFalse(any(e.sender == child.name and "must-not-restart" in e.body for e in self.room.entries))
        tid, name = child.thread_id, child.name
        await self.room.command("/del " + child.label)
        self.assertNotIn(name, self.room.agents)
        await self.room.native.discover(self.room.agents["A"].thread_id)
        self.assertIsNone(self.room.native.member_for(tid))
        self.assertIn(tid, self.room.ignored_native_threads)
        self.assertTrue(list((self.cwd / ".codex-room/archives").glob("*.json")))

    async def test_repeated_and_stale_stream_notifications_do_not_duplicate_or_regress(self):
        child = await self.child()
        await self.settled()
        entry = next(e for e in self.room.entries if e.sender == child.name)
        old = entry.body
        await self.room.native.event({"method": "turn/started", "params": {"threadId": child.thread_id,
            "turn": {"id": entry.server_turn_id, "status": "inProgress", "items": []}}})
        await self.room.native.event({"method": "item/agentMessage/delta", "params": {"threadId": child.thread_id,
            "turnId": entry.server_turn_id, "itemId": "msg", "delta": "DUPLICATE"}})
        await self.room.native.hydrate(child)
        self.assertEqual(entry.body, old)
        self.assertEqual(entry.state, "done")
        self.assertFalse(child.busy)
        self.room.native.sync_turn(child, {"id": "newer-live-turn", "status": "inProgress", "items": []})
        self.room.native.sync_turn(child, {"id": entry.server_turn_id, "status": "completed", "items": []})
        self.assertEqual(self.room.client.active[child.thread_id], "newer-live-turn")
        self.assertTrue(child.busy)
        self.room.native.sync_turn(child, {"id": "newer-live-turn", "status": "completed", "items": []})

    async def test_missing_old_child_does_not_disconnect_other_roots(self):
        child = await self.child()
        await self.settled()
        await self.room.close()
        fixture = json.loads((self.cwd / "native-fixture.json").read_text())
        fixture["threads"].pop(child.thread_id)
        (self.cwd / "native-fixture.json").write_text(json.dumps(fixture))
        self.room = self.new_room()
        await self.room.start()
        self.assertTrue(self.room.connected)
        self.assertTrue(self.room.agents[child.name].native_closed)
        with self.assertRaises(ValueError):
            await self.room.submit(Prompt(f"@{child.label} do not clone"))
        await self.room.submit(Prompt("@A still-alive"))
        await self.settled()
        self.assertIn("still-alive", next(e.body for e in reversed(self.room.entries) if e.sender == "A"))

    async def test_child_settings_are_not_falsely_applied(self):
        child = await self.child()
        await self.settled()
        with self.assertRaises(ValueError):
            await self.room.command("/model imaginary low", child.name)
        with self.assertRaises(ValueError):
            await self.room.command("/permissions full", child.name)
        self.assertEqual(child.model, "child-model")

    async def test_stopping_pending_child_approval_cleans_ui_request(self):
        child = await self.child("spawn-approval")
        await self.until(lambda: self.room.interactions)
        await self.room.stop(child.name)
        await self.until(lambda: not self.room.interactions)
        self.assertFalse(child.busy)

    async def test_uncertain_steer_is_not_retried_as_fresh_turn(self):
        child = await self.child()
        await self.settled()
        await self.room.submit(Prompt("@A hold"))
        await self.until(lambda: self.room.client.active.get(self.room.agents["A"].thread_id))
        async def uncertain(*args, **kwargs):
            raise CodexError("fixture delivery unknown")
        self.room.client.steer_turn = uncertain
        await self.room.submit(Prompt(f"@{child.label} uncertain-message"))
        request = self.room.entries[-1]
        await self.until(lambda: request.state == "failed")
        self.assertEqual(len([e for e in self.room.entries if e.sender == "you" and e.relay_for == request.id]), 1)
        self.assertFalse(any(e.sender == child.name and "uncertain-message" in e.body for e in self.room.entries))
        await self.room.stop("A")

    async def test_recursive_delete_and_failed_interrupt_keep_tree_reviewable(self):
        child = await self.child()
        await self.settled()
        metadata = await self.room.client.request("fixture/addChild", {"parentThreadId": child.thread_id})
        nested = await self.room.native.adopt(metadata["id"], metadata)
        self.assertEqual(nested.parent_name, child.name)
        self.assertEqual(self.room.native.root_for(nested).name, "A")
        await self.room.submit(Prompt(f"@{child.label} slow"))
        await self.until(lambda: child.busy)
        request = self.room.client.request
        async def fail_interrupt(method, params, **kwargs):
            if method == "turn/interrupt":
                raise CodexError("fixture interrupt unconfirmed")
            return await request(method, params, **kwargs)
        self.room.client.request = fail_interrupt
        with self.assertRaises(CodexError):
            await self.room.command("/del A")
        self.assertEqual(len(self.room.agents), 3)
        self.assertTrue(child.busy)
        self.room.client.request = request
        await self.room.command("/del A")
        self.assertEqual(self.room.agents, {})
        self.assertIn(nested.thread_id, self.room.ignored_native_threads)

    async def test_unrelated_native_tree_is_never_attached(self):
        original = self.room.client.request
        async def read_foreign(method, params, **kwargs):
            if method == "thread/read":
                return {"thread": {"id": params["threadId"], "parentThreadId": "foreign-root" if params["threadId"] == "foreign-child" else None}}
            return await original(method, params, **kwargs)
        self.room.client.request = read_foreign
        self.assertIsNone(await self.room.native.adopt("foreign-child"))
        self.assertEqual(list(self.room.agents), ["A"])

    async def test_idle_roots_without_children_are_not_eagerly_loaded(self):
        await self.room.submit(Prompt("@A ordinary-root-task"))
        await self.settled()
        tid = self.room.agents["A"].thread_id
        await self.room.close()
        self.room = self.new_room()
        await self.room.start()
        self.assertTrue(self.room.connected)
        self.assertNotIn(tid, self.room.client.loaded)

    async def test_delete_keeps_subscription_until_inflight_parent_relay_finishes(self):
        child = await self.child()
        await self.settled()
        await self.room.submit(Prompt("@A hold"))
        await self.until(lambda: self.room.client.active.get(self.room.agents["A"].thread_id))
        await self.room.submit(Prompt(f"@{child.label} slow"))
        await self.until(lambda: child.busy)
        removing = asyncio.create_task(self.room.command("/del " + child.label))
        await asyncio.sleep(.08)
        self.assertIn(child.name, self.room.agents)
        self.assertIn(child.thread_id, self.room.native.attached)
        await self.room.stop("A")
        await removing
        self.assertNotIn(child.name, self.room.agents)

    async def test_snapshot_and_stream_merge_keeps_exact_prose_and_full_forward(self):
        child = await self.child()
        await self.settled()
        turn_id = "synthetic"
        self.room.native.sync_turn(child, {"id": turn_id, "status": "inProgress", "items": [
            {"id": "m", "type": "agentMessage", "text": "hello "}]})
        for delta in ("hello ", "world"):
            await self.room.native.event({"method": "item/agentMessage/delta", "params": {
                "threadId": child.thread_id, "turnId": turn_id, "itemId": "m", "delta": delta}})
        entry = self.room.native.records[child.thread_id, turn_id]
        self.assertEqual(entry.body, "hello world")
        self.room.native.sync_turn(child, {"id": turn_id, "status": "completed", "items": []})
        quote = ForwardQuote([{"id": entry.id, "text": entry.body, "member": child.name,
            "blocks": [asdict(b) for b in entry.blocks]}], child.name, child.label)
        self.assertIn("hello world", quote.with_instruction("review"))


if __name__ == "__main__":
    unittest.main()
