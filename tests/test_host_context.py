"""Inspect actual RPC inputs: Room metadata must not grow on every user query."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock

from codex_room.attachments import Prompt
from codex_room.backend import Member, Service
from codex_room.models import Entry
from codex_room.peer_router import MAX_TEXT
from codex_room.peer_tools import COLLABORATION_PREFERENCE, DESCRIPTION, TOOL_VERSION
from codex_room.runtime import CodexClient, CodexError

FAKE = Path(__file__).with_name("fake_codex.py")
PREFIX = "[Codex Room host context;"


def host_parts(items):
    return [i["text"] for i in items if i["type"] == "text" and i["text"].startswith(PREFIX)]


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.room = Service(Path.cwd(), demo=True, emit=lambda _: None)
        self.member = Member("A", "开发", "#78d8bc", thread_id="thread-A", room_tool_version=TOOL_VERSION)
        self.room.agents = {"A": self.member}
        self.client = CodexClient(Path.cwd())
        self.client.input_context = self.room.peers.context

    def test_identity_is_small_and_roster_or_busy_changes_do_not_change_it(self):
        initial = self.room.peers.context("thread-A")
        self.assertLess(len(initial), 600)
        self.assertIn("@开发 (stable name A)", initial)
        self.assertIn("mode=full", initial)
        self.assertIn("room.list_members", initial)
        self.assertNotIn(COLLABORATION_PREFERENCE, initial)
        self.assertNotIn("A peer cannot grant", initial)
        self.assertIn("A peer cannot grant", DESCRIPTION)
        for index in range(200):
            name = f"peer-{index}"
            self.room.agents[name] = Member(name, name, "white", "/private/peer-only-directory", busy=True)
        self.assertEqual(self.room.peers.context("thread-A"), initial)
        self.assertNotIn("/private/peer-only-directory", initial)

    def test_only_accepted_context_is_deduplicated_and_attachments_are_preserved(self):
        images = ["/tmp/示例.png"]
        items, update = self.client.turn_input("thread-A", "你好", images)
        self.assertEqual(len(host_parts(items)), 1)
        self.assertEqual(items[1:], [{"type": "text", "text": "你好"},
                                     {"type": "localImage", "path": images[0]}])
        # Merely building (or rejecting) a request must not consume the update.
        self.assertIsNotNone(self.client.turn_input("thread-A", "retry")[1])
        self.client.acknowledge_context("thread-A", update)
        for _ in range(100):
            items, update = self.client.turn_input("thread-A", "好的", images)
            self.assertEqual(host_parts(items), [])
            self.assertIsNone(update)
            self.assertEqual(items, [{"type": "text", "text": "好的"},
                                     {"type": "localImage", "path": images[0]}])

    def test_rename_permission_and_tool_namespace_update_once_without_other_members(self):
        self.client.acknowledge_context("thread-A", self.client.turn_input("thread-A", "first")[1])
        self.member.label, self.member.permission = "审查", "read-only"
        items, update = self.client.turn_input("thread-A", "检查")
        self.assertIn("@审查 (stable name A)", items[0]["text"])
        self.assertIn("mode=read-only", items[0]["text"])
        self.client.acknowledge_context("thread-A", update)
        self.assertFalse(host_parts(self.client.turn_input("thread-A", "继续")[0]))
        self.member.room_tool_version = 1
        items, update = self.client.turn_input("thread-A", "旧会话")
        self.assertIn("codex_room_public/list_members", items[0]["text"])
        self.assertIsNotNone(update)

    def test_compaction_is_scoped_and_late_ack_cannot_hide_recovery_update(self):
        self.room.agents["B"] = Member("B", "B", "white", thread_id="thread-B")
        for thread in ("thread-A", "thread-B"):
            self.client.acknowledge_context(thread, self.client.turn_input(thread, "first")[1])
        self.client.observe_context("item/started", {"threadId": "thread-A", "item": {"type": "contextCompaction"}})
        self.assertIsNone(self.client.turn_input("thread-A", "still retained")[1])
        self.client.observe_context("item/completed", {"threadId": "thread-A", "turnId": "turn-A",
                                    "item": {"id": "compact-1", "type": "contextCompaction"}})
        _, old_update = self.client.turn_input("thread-A", "in flight")
        self.client.observe_context("item/completed", {"threadId": "thread-A", "turnId": "turn-A",
                                    "item": {"id": "compact-2", "type": "contextCompaction"}})
        self.client.acknowledge_context("thread-A", old_update)
        items, update = self.client.turn_input("thread-A", "after compaction")
        self.assertEqual(len(host_parts(items)), 1)
        self.client.acknowledge_context("thread-A", update)
        self.assertIsNone(self.client.turn_input("thread-A", "next")[1])
        self.assertIsNone(self.client.turn_input("thread-B", "unaffected")[1])

    def test_native_child_and_unknown_thread_have_no_room_metadata(self):
        self.member.parent_name = "parent"
        for thread in ("thread-A", "unknown"):
            items, update = self.client.turn_input(thread, "hello")
            self.assertEqual(items, [{"type": "text", "text": "hello"}])
            self.assertIsNone(update)

    def test_pending_reminders_are_sent_only_when_the_ledger_changes(self):
        self.room.agents["B"] = Member("B", "资料员", "white", thread_id="thread-B")
        self.client.acknowledge_context("thread-A", self.client.turn_input("thread-A", "first")[1])
        request = Entry("B", "请先调研再回复", "A", communication={
            "transport": "room", "source_thread_id": "thread-B", "target_thread_id": "thread-A",
            "expect_reply": True, "delivery": "delivered", "chain_id": "chain-1"})
        self.room.peers.messages[request.id] = request
        items, update = self.client.turn_input("thread-A", "continue")
        self.assertIn(request.id, host_parts(items)[0])
        self.assertIn("请先调研再回复", host_parts(items)[0])
        self.client.acknowledge_context("thread-A", update)
        self.assertFalse(host_parts(self.client.turn_input("thread-A", "unchanged")[0]))
        reply = Entry("A", "已完成", "B", communication={
            "source_thread_id": "thread-A", "reply_to": request.id})
        self.room.peers.messages[reply.id] = reply
        items, update = self.client.turn_input("thread-A", "finished")
        self.assertIn('"total":0', host_parts(items)[0])
        self.assertNotIn(request.id, host_parts(items)[0])
        self.client.acknowledge_context("thread-A", update)
        self.assertFalse(host_parts(self.client.turn_input("thread-A", "unchanged again")[0]))

    def test_original_messages_are_not_summarized_or_split_and_pages_are_bounded(self):
        self.room.agents["B"] = Member("B", "B", "white", thread_id="thread-B")
        bodies = ["标题\n\n```json\n{\"保留\": true}\n```\n" + "原文🙂 " * 8000, "第二份\n\n" + "x" * 40000]
        self.assertTrue(all(len(body) <= MAX_TEXT for body in bodies))
        ids = []
        for index, body in enumerate(bodies):
            request = Entry("B", body, "A", communication={
                "transport": "room", "source_thread_id": "thread-B", "target_thread_id": "thread-A",
                "expect_reply": True, "delivery": "delivered", "chain_id": f"chain-{index}"})
            self.room.peers.messages[request.id] = request
            ids.append(request.id)
        first = self.room.peers.pending_replies(self.member)
        self.assertEqual(first["total"], 2)
        self.assertEqual([p["message"] for p in first["requests"]], bodies[:1])
        self.assertEqual(first["next_cursor"], 1)
        second = self.room.peers.pending_replies(self.member, cursor=first["next_cursor"])
        self.assertEqual([p["message"] for p in second["requests"]], bodies[1:])
        self.assertIsNone(second["next_cursor"])
        self.assertNotIn("excerpt", first["requests"][0])
        reply = Entry("A", "第一份已回复", "B", communication={"source_thread_id": "thread-A", "reply_to": ids[0]})
        self.room.peers.messages[reply.id] = reply
        after_reply = self.room.peers.pending_replies(self.member, cursor=first["next_cursor"])
        self.assertEqual(after_reply["total"], 1)
        self.assertEqual([p["message"] for p in after_reply["requests"]], bodies[1:])

    def test_canonical_and_legacy_duplicates_are_paired_in_either_order(self):
        for turn, legacy_first in (("turn-1", False), ("turn-2", True)):
            canonical = "item/completed", {"threadId": "thread-A", "turnId": turn,
                         "item": {"id": f"compact-{turn}", "type": "contextCompaction"}}
            legacy = "thread/compacted", {"threadId": "thread-A", "turnId": turn}
            before = self.client.context_versions.get("thread-A", 0)
            for method, params in ([legacy, canonical, legacy, canonical] if legacy_first else
                                   [canonical, legacy, canonical, legacy]):
                self.client.observe_context(method, params)
            self.assertEqual(self.client.context_versions["thread-A"], before + 1)


class ContextProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)
        self.room = Service(self.cwd, command=[sys.executable, str(FAKE)], emit=lambda _: None)
        await self.room.start()
        await self.room.command("/add A")
        await self.room.command("/add B")
        self.requests = AsyncMock(wraps=self.room.client.request)
        self.room.client.request = self.requests

    async def asyncTearDown(self):
        await self.room.close()
        self.temp.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(6):
            while not predicate():
                await asyncio.sleep(.01)

    async def send(self, text):
        await self.room.submit(Prompt(text))
        await self.until(lambda: all(not a.busy for a in self.room.agents.values()))

    def inputs(self, method="turn/start"):
        return [call.args[1]["input"] for call in self.requests.call_args_list if call.args[0] == method]

    def recoveries(self):
        return [c.args[1] for c in self.requests.call_args_list
                if c.args[0] == "turn/steer" and len(c.args[1]["input"]) == 1 and host_parts(c.args[1]["input"])]

    async def pending_request(self, body):
        for member in self.room.agents.values():
            await self.room.client.ensure_thread(member)
        request = Entry("B", body, "A", communication={
            "transport": "room", "source_thread_id": self.room.agents["B"].thread_id,
            "target_thread_id": self.room.agents["A"].thread_id,
            "expect_reply": True, "delivery": "delivered", "chain_id": "pending-test"})
        self.room.peers.messages[request.id] = request
        self.room.add_entry(request)
        return request

    async def test_new_turns_and_user_steer_only_send_changed_metadata(self):
        await self.send("@A hello")
        await self.send("@A 好的")
        self.assertEqual([len(host_parts(i)) for i in self.inputs()], [1, 0])
        await self.room.submit(Prompt("@A steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        await self.send("@A finish-steer")
        self.assertEqual([len(host_parts(i)) for i in self.inputs()], [1, 0, 0])
        self.assertEqual(self.inputs("turn/steer"), [[{"type": "text", "text": "finish-steer"}]])
        await self.room.command("/rename A 开发")
        await self.send("@开发 next")
        self.assertIn("@开发 (stable name A)", host_parts(self.inputs()[-1])[0])
        await self.send("@开发 next again")
        self.assertFalse(host_parts(self.inputs()[-1]))
        # Existing native developer instructions remain untouched.
        loads = [c.args[1] for c in self.requests.call_args_list if c.args[0] in {"thread/start", "thread/resume"}]
        self.assertTrue(loads)
        self.assertTrue(all("developerInstructions" not in p for p in loads))

    async def test_rejected_start_and_steer_do_not_consume_identity_update(self):
        await self.send("@A first")
        member, client = self.room.agents["A"], self.room.client
        await self.room.command("/rename A 新名")
        original = client.request
        client.request = AsyncMock(side_effect=CodexError("rejected", code=-32602))
        with self.assertRaises(CodexError):
            await client.run_turn(member, "reject", AsyncMock())
        self.assertIsNotNone(client.turn_input(member.thread_id, "retry")[1])
        client.active[member.thread_id] = "test-turn"
        with self.assertRaises(CodexError):
            await client.steer_turn(member.thread_id, "reject")
        self.assertIsNotNone(client.turn_input(member.thread_id, "retry")[1])
        client.request = AsyncMock(side_effect=CodexError("no active turn", code=-32600))
        self.assertFalse(await client.steer_turn(member.thread_id, "race"))
        self.assertIsNotNone(client.turn_input(member.thread_id, "retry")[1])
        client.active.clear()
        client.request = original
        await self.send("@新名 accepted")
        self.assertIn("@新名", host_parts(self.inputs()[-1])[0])
        await self.send("@新名 next")
        self.assertFalse(host_parts(self.inputs()[-1]))

    async def test_reminders_change_once_and_public_roundtrip_still_works(self):
        await self.send("@A first")
        await self.send("@B first")
        # The incoming request is not yet in the pending ledger when its turn
        # starts. It is then answered and must not cause permanent re-injection.
        await self.send("@A peer-send B")
        public = list(self.room.peers.messages.values())
        self.assertEqual([(e.sender, e.target) for e in public], [("A", "B"), ("B", "A")])
        self.assertTrue(all(e.communication["delivery"] == "delivered" for e in public))
        await self.send("@A next")
        await self.send("@B next")
        self.assertFalse(host_parts(self.inputs()[-2]))
        self.assertFalse(host_parts(self.inputs()[-1]))

    async def test_reconnect_sends_one_small_update_and_preserves_thread(self):
        await self.send("@A first")
        thread_id = self.room.agents["A"].thread_id
        await self.room.command("/reconnect")
        self.requests = AsyncMock(wraps=self.room.client.request)
        self.room.client.request = self.requests
        await self.send("@A after reconnect")
        await self.send("@A next")
        self.assertEqual(self.room.agents["A"].thread_id, thread_id)
        self.assertEqual([len(host_parts(i)) for i in self.inputs()], [1, 0])
        self.assertLess(len(host_parts(self.inputs()[0])[0]), 600)

    async def test_compaction_notification_refreshes_metadata_once_on_next_input(self):
        await self.send("@A first")
        await self.send("@A context-compact")
        self.assertFalse(host_parts(self.inputs()[-1]))
        await self.send("@A after compaction")
        self.assertEqual(len(host_parts(self.inputs()[-1])), 1)
        await self.send("@A next")
        self.assertFalse(host_parts(self.inputs()[-1]))

    async def test_mid_turn_compaction_restores_original_request_without_any_new_message(self):
        body = "B 的原始请求\n\n```json\n{\"a\": \"原样\"}\n```\n" + "长消息🙂 " * 5000
        request = await self.pending_request(body)
        await self.send("@A compact-recover")
        restored = self.recoveries()
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["expectedTurnId"], self.room.client.finished_turns[self.room.agents["A"].thread_id])
        text = restored[0]["input"][0]["text"]
        pending = json.loads(next(line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("Outstanding replies")))
        self.assertEqual(pending["requests"][0]["message_id"], request.id)
        self.assertEqual(pending["requests"][0]["message"], body)
        self.assertEqual(len([e for e in self.room.entries if e.sender == "you"]), 1)
        self.assertEqual(len(self.inputs()), 1)
        self.assertTrue(any("RECOVERY_SEEN:1" in e.body for e in self.room.entries))
        await self.send("@A next")
        self.assertFalse(host_parts(self.inputs()[-1]))

    async def test_repeated_notifications_restore_once_and_later_compaction_restores_again(self):
        await self.send("@A compact-recover-duplicates")
        self.assertEqual(len(self.recoveries()), 1)
        await self.send("@A compact-recover-twice")
        self.assertEqual(len(self.recoveries()), 3)
        self.assertTrue(any("RECOVERY_SEEN:2" in e.body for e in self.room.entries))

    async def test_queued_user_input_restores_state_without_an_extra_recovery_rpc(self):
        await self.room.submit(Prompt("@A steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        client, member = self.room.client, self.room.agents["A"]
        client.observe_context("item/completed", {"threadId": member.thread_id,
            "turnId": client.active[member.thread_id], "item": {"id": "compact-user", "type": "contextCompaction"}})
        await self.send("@A finish-steer")
        self.assertEqual(len(self.inputs("turn/steer")), 1)
        self.assertEqual(self.inputs("turn/steer")[0][-1]["text"], "finish-steer")
        self.assertEqual(len(host_parts(self.inputs("turn/steer")[0])), 1)
        self.assertFalse(self.recoveries())

    async def test_stop_cancels_pending_recovery_and_does_not_start_a_new_turn(self):
        await self.room.submit(Prompt("@A slow"))
        await self.until(lambda: bool(self.room.client.active))
        client, member = self.room.client, self.room.agents["A"]
        turn = client.active[member.thread_id]
        client.observe_context("item/completed", {"threadId": member.thread_id,
            "turnId": turn, "item": {"id": "compact-stop", "type": "contextCompaction"}})
        await self.room.stop("A")
        self.assertFalse(self.recoveries())
        self.assertFalse(client.context_recoveries)
        self.assertEqual(len(self.inputs()), 1)
        await self.send("@A next")
        self.assertEqual(len(self.inputs()), 2)
        self.assertEqual(len(host_parts(self.inputs()[-1])), 1)

    async def test_failed_recovery_remains_dirty_and_is_not_automatically_retried(self):
        await self.room.submit(Prompt("@A steer-hold"))
        await self.until(lambda: bool(self.room.client.active))
        client, member = self.room.client, self.room.agents["A"]
        original = client.request
        client.request = AsyncMock(side_effect=CodexError("unconfirmed recovery timeout"))
        client.observe_context("item/completed", {"threadId": member.thread_id,
            "turnId": client.active[member.thread_id], "item": {"id": "compact-fail", "type": "contextCompaction"}})
        task = client.context_recoveries[member.thread_id]
        await task
        client.request.assert_awaited_once()
        self.assertIsNotNone(client.turn_input(member.thread_id, "next")[1])
        client.request = original
        await self.send("@A finish-steer")
        self.assertEqual(len(host_parts(self.inputs("turn/steer")[-1])), 1)

    async def test_late_old_compaction_does_not_touch_a_new_task(self):
        await self.send("@A first")
        client, member = self.room.client, self.room.agents["A"]
        old_turn = client.finished_turns[member.thread_id]
        await self.room.submit(Prompt("@A steer-hold"))
        await self.until(lambda: bool(client.active))
        version = client.context_versions[member.thread_id]
        client.observe_context("item/completed", {"threadId": member.thread_id,
            "turnId": old_turn, "item": {"id": "old-compact", "type": "contextCompaction"}})
        self.assertEqual(client.context_versions[member.thread_id], version)
        self.assertFalse(client.context_recoveries)
        await self.send("@A finish-steer")
        self.assertFalse(host_parts(self.inputs("turn/steer")[-1]))

    async def test_self_state_tool_returns_original_pending_message_without_waking_peer(self):
        body = "来自 B 的原文\n\n```python\nprint('保持格式')\n```"
        request = await self.pending_request(body)
        await self.send("@A self-state")
        answer = next(e.body for e in reversed(self.room.entries) if e.sender == "A" and "SELF_STATE:" in e.body)
        state = json.loads(answer.split("SELF_STATE:", 1)[1])
        self.assertEqual(state["members"], [])
        self.assertEqual(state["self"]["name"], "A")
        self.assertEqual(state["self"]["pending_replies"]["requests"][0]["message_id"], request.id)
        self.assertEqual(state["self"]["pending_replies"]["requests"][0]["message"], body)
        self.assertEqual(len(self.inputs()), 1)
        self.assertEqual(len(self.room.peers.messages), 1)

    async def test_slow_recovery_ack_does_not_block_user_followup_or_revive_finished_turn(self):
        client = self.room.client
        original = client.request
        accepted, release = asyncio.Event(), asyncio.Event()
        async def delayed(method, params, **kwargs):
            result = await original(method, params, **kwargs)
            if method == "turn/steer" and len(params["input"]) == 1 and host_parts(params["input"]):
                accepted.set()
                await release.wait()
            return result
        client.request = AsyncMock(side_effect=delayed)
        await self.room.submit(Prompt("@A compact-recover-hold"))
        await asyncio.wait_for(accepted.wait(), 3)
        self.assertTrue(client.context_recoveries)
        await asyncio.wait_for(self.send("@A finish-steer"), 2)
        self.assertFalse(client.context_recoveries)
        self.assertEqual(len(self.inputs()), 1)
        release.set()
        await self.send("@A next")
        self.assertEqual(len(self.inputs()), 2)


if __name__ == "__main__":
    unittest.main()
