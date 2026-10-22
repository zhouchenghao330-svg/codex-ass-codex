import json
import unittest
from pathlib import Path

from codex_room.backend import Service, Member
from codex_room.models import Entry, ReplyBlock
from codex_room.quotes import ForwardQuote


class CommunicationTests(unittest.TestCase):
    def setUp(self):
        self.room = Service(Path.cwd(), demo=True, emit=lambda event: None)
        self.a = Member("A", "开发", "#a9b6ff", thread_id="root")
        self.b = Member("B", "审查", "#78d8bc", thread_id="child", parent_name="A")
        self.room.agents.update(A=self.a, B=self.b)
        self.log = self.room.native.communications

    def test_v2_records_both_directions_without_fabricating_message_text(self):
        self.log.record(self.a, {"id": "out", "type": "subAgentActivity", "kind": "interacted", "agentThreadId": "child"}, "t1")
        self.log.record(self.b, {"id": "in", "type": "subAgentActivity", "kind": "interacted", "agentThreadId": "root"}, "t2")
        self.assertEqual([(e.sender, e.target) for e in self.room.entries], [("A", "B"), ("B", "A")])
        for entry in self.room.entries:
            self.assertEqual(entry.body, "")
            self.assertFalse(entry.communication["content_available"])
            self.assertEqual(entry.communication["action"], "发送消息")

    def test_plaintext_prompt_is_preserved_and_deduplicated_across_updates(self):
        item = {"id": "send", "type": "collabAgentToolCall", "tool": "sendInput", "senderThreadId": "root",
            "receiverThreadIds": ["child"], "prompt": "**完整原文**\n\n```python\nprint('hi')\n```"}
        self.log.record(self.a, item, "t", completed=False)
        self.log.record(self.a, item, "t", completed=True)
        self.log.record(self.a, {**item, "prompt": None}, "t", completed=True)
        self.log.record(self.a, {**item, "prompt": None}, "t", completed=False)
        self.assertEqual(len(self.room.entries), 1)
        entry = self.room.entries[0]
        self.assertEqual(entry.body, item["prompt"])
        self.assertEqual(entry.blocks[0].text, item["prompt"])
        self.assertTrue(entry.communication["content_available"])
        self.assertEqual(entry.state, "done")

    def test_backfill_preserves_message_order_and_never_duplicates(self):
        owner = self.room.add_entry(Entry("A", "Existing reply", server_turn_id="t"))
        for number in range(2):
            item = {"id": str(number), "type": "collabAgentToolCall", "tool": "sendMessage",
                "receiverThreadIds": ["child"], "prompt": f"message {number}"}
            owner.blocks.append(ReplyBlock(str(number), "tool", json.dumps(item), status="completed"))
        self.assertTrue(self.log.backfill())
        self.assertEqual([e.body for e in self.room.entries], ["Existing reply", "message 0", "message 1"])
        self.assertFalse(self.log.backfill())
        self.assertEqual(len(self.room.entries), 3)

    def test_deleted_receivers_do_not_reappear_and_late_identity_is_resolved(self):
        del self.room.agents["B"]
        item = {"id": "one", "type": "subAgentActivity", "kind": "interacted", "agentThreadId": "child", "agentPath": "/root/reviewer"}
        self.log.record(self.a, item, "t")
        entry = self.room.entries[0]
        self.assertFalse(entry.target)
        self.room.agents["B"] = self.b
        self.log.resolve(self.b)
        self.assertEqual(entry.target, "B")
        self.room.ignored_native_threads.add("child")
        self.log.record(self.a, {**item, "id": "two"}, "t")
        self.assertEqual(len(self.room.entries), 1)

    def test_forwarding_preserves_direction_and_explicit_missing_content(self):
        quote = ForwardQuote([{"member": "B", "speaker": "开发", "recipient": "审查", "cwd": "/example",
            "text": "", "blocks": [], "images": [], "communication": {"action": "发送消息", "content_available": False}}], "B", "审查")
        self.assertIn("@开发 → @审查", quote.readable())
        self.assertIn("未提供", quote.readable())
        self.assertFalse(json.loads(quote.payload())[0]["communication"]["content_available"])

    def test_interrupted_sender_does_not_leave_message_permanently_sending(self):
        self.log.record(self.a, {"id": "out", "type": "subAgentActivity", "kind": "interacted", "agentThreadId": "child"}, "t", completed=False)
        self.assertEqual(self.room.entries[0].state, "working")
        self.log.finish("root", "t", "stopped")
        self.assertEqual(self.room.entries[0].state, "stopped")
