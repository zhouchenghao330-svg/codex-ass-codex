"""Read-only discovery, bounded user dialogue and old-thread tool upgrades."""
from dataclasses import asdict
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from codex_room.attachments import Attachment
from codex_room.backend import Member, Service
from codex_room.models import Entry, ReplyBlock
from codex_room.peer_tools import TOOL_VERSION, dynamic_tools, mcp_tools


class MemberDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.room = Service(Path.cwd(), demo=True, emit=lambda _: None)
        self.room.agents = {name: Member(name, name, "#78d8bc", "/work/" + name,
                                       thread_id="thread-" + name) for name in ("A", "B", "C")}
        self.room.client = SimpleNamespace(active={"thread-A": "turn-A"})
        self.room.active_entries = {"A": (Entry("you", "inspect", "A"), Entry("A", "", run_id="run-A"))}

    def query(self, **args):
        return self.room.peers.call("list_members", "thread-A", "turn-A", "query", args)

    def pair(self, text, reply, index, name="B"):
        run = f"run-{name}-{index}"
        question = Entry("you", text, name, run_id=run)
        answer = Entry(name, reply, run_id=run)
        self.room.entries.extend((question, answer))
        return question, answer

    def test_roster_history_is_read_only_and_excludes_native_children_and_self(self):
        self.room.agents["B"].label = "环境排查"
        self.room.agents["B"].busy = True
        self.room.agents["B"].queue.append(Entry("you", "queued", "B"))
        self.room.agents["child"] = Member("child", "child", "#78d8bc", parent_name="A")
        for index in range(6):
            self.pair(f"用户问题 {index}", f"调查结论 {index}\n第二行\n第三行\n第四行不展示", index)
        before = [asdict(e) for e in self.room.entries]
        result = self.query()
        self.assertTrue(result["accepted"])
        self.assertEqual([m["name"] for m in result["members"]], ["B", "C"])
        member = result["members"][0]
        self.assertEqual((member["label"], member["cwd"], member["busy"], member["queued_messages"]),
                         ("环境排查", "/work/B", True, 1))
        self.assertEqual([d["user"]["text"] for d in member["recent_dialogue"]], [f"用户问题 {i}" for i in (3, 4, 5)])
        self.assertEqual(member["recent_dialogue"][-1]["codex_preview"]["text"], "调查结论 5\n第二行\n第三行")
        self.assertTrue(member["recent_dialogue"][-1]["codex_preview"]["truncated"])
        self.assertEqual(before, [asdict(e) for e in self.room.entries])
        self.assertFalse(self.room.peers.messages)
        self.assertFalse(self.room.peers.counts)
        self.assertEqual(list(self.room.agents["B"].queue)[0].body, "queued")

    def test_member_filter_pagination_and_zero_history(self):
        self.room.agents["B"].label = "环境排查"
        for index in range(6):
            self.pair(str(index), "answer", index)
        result = self.query(member="@环境排查", recent_turns=5)
        self.assertEqual(result["total_members"], 1)
        self.assertEqual(len(result["members"][0]["recent_dialogue"]), 5)
        first = self.query(limit=1, recent_turns=0)
        self.assertEqual(first["members"][0]["recent_dialogue"], [])
        self.assertEqual(first["next_cursor"], 1)
        second = self.query(limit=1, cursor=first["next_cursor"])
        self.assertEqual(second["members"][0]["name"], "C")
        self.assertIsNone(second["next_cursor"])

    def test_self_only_returns_current_identity_and_paged_original_pending_requests(self):
        self.room.agents["A"].label = "开发"
        self.room.agents["A"].permission = "read-only"
        bodies = ["原文\n\n```json\n{\"a\":1}\n```\n" + "中文🙂 " * 8000, "第二条原文\n" + "x" * 40000]
        for index, body in enumerate(bodies):
            request = Entry("B", body, "A", communication={
                "source_thread_id": "thread-B", "target_thread_id": "thread-A", "expect_reply": True,
                "delivery": "delivered", "chain_id": f"request-{index}"})
            self.room.peers.messages[request.id] = request
        before = [asdict(e) for e in self.room.entries]
        result = self.query(self_only=True)
        self.assertEqual(result["members"], [])
        self.assertEqual(result["total_members"], 0)
        self.assertEqual((result["self"]["name"], result["self"]["label"], result["self"]["permission"]),
                         ("A", "开发", "read-only"))
        pending = result["self"]["pending_replies"]
        self.assertEqual(pending["total"], 2)
        self.assertEqual([p["message"] for p in pending["requests"]], bodies[:1])
        second = self.query(self_only=True, pending_cursor=pending["next_cursor"])["self"]["pending_replies"]
        self.assertEqual([p["message"] for p in second["requests"]], bodies[1:])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(before, [asdict(e) for e in self.room.entries])
        self.assertFalse(any(m.queue for m in self.room.agents.values()))
        for invalid in ({"self_only": 1}, {"self_only": True, "member": "B"},
                        {"pending_cursor": -1}, {"pending_cursor": True}, {"pending_cursor": "1"}):
            self.assertFalse(self.query(**invalid)["accepted"], invalid)

    def test_hidden_inputs_peer_turns_and_tools_are_not_user_dialogue(self):
        q1, a1 = self.pair("第一问", "第一答", 1)
        q2 = Entry("you", "追问", "B", state="steered", run_id=q1.run_id)
        a2 = Entry("B", "追问的答复", run_id=q1.run_id)
        a2.blocks = [ReplyBlock("text", "text", a2.body), ReplyBlock("tool", "command", "TOOL_SECRET")]
        self.room.entries.extend((q2, a2,
            Entry("B", "PEER_ONLY_RESPONSE", run_id="peer-run"),
            Entry("you", "INTERNAL_RELAY", "B", hidden=True),
            Entry("C", "PUBLIC_MESSAGE", "B", communication={"transport": "room"})))
        q3 = Entry("you", "尚未开始的问题", "B", state="queued")
        self.room.entries.append(q3)
        result = self.query(member="B")
        history = result["members"][0]["recent_dialogue"]
        self.assertEqual([d["codex_preview"]["text"] if d["codex_preview"] else None for d in history],
                         [a1.body, a2.body, None])
        encoded = json.dumps(result)
        for private in ("TOOL_SECRET", "PEER_ONLY_RESPONSE", "INTERNAL_RELAY", "PUBLIC_MESSAGE"):
            self.assertNotIn(private, encoded)

    def test_queued_users_and_long_materials_do_not_misattribute_or_expand_previews(self):
        q1 = Entry("you", "问题一", "B", run_id="one")
        q2 = Entry("you", "问题二", "B", run_id="two")
        self.room.entries.extend((q1, q2, Entry("B", "答一", run_id="one"), Entry("B", "答二", run_id="two")))
        history = self.query(member="B")["members"][0]["recent_dialogue"]
        self.assertEqual([h["codex_preview"]["text"] for h in history], ["答一", "答二"])
        q3, a3 = self.pair("x" * 3000, "y" * 1000, 3)
        self.room.drafts["B"] = {"prompt": "DRAFT_SECRET"}
        q3.attachments = [Attachment(start=0, end=1, kind="paste", content="PASTE_SECRET", label="[Pasted Content 3000 chars]")]
        q3.display_body = "请分析 [Pasted Content 3000 chars]"
        q3.quote = {"messages": [{"text": "FORWARDED_SECRET"}]}
        result = self.query(member="B")
        last = result["members"][0]["recent_dialogue"][-1]
        self.assertEqual(last["user"]["text"], q3.display_body)
        self.assertTrue(last["user"]["has_forwarded_material"])
        self.assertEqual(len(last["codex_preview"]["text"]), 600)
        self.assertTrue(last["codex_preview"]["truncated"])
        for private in ("DRAFT_SECRET", "PASTE_SECRET", "FORWARDED_SECRET"):
            self.assertNotIn(private, json.dumps(result))
        q3.display_body = None
        user = self.query(member="B")["members"][0]["recent_dialogue"][-1]["user"]
        self.assertEqual(len(user["text"]), 2000)
        self.assertTrue(user["truncated"])

    def test_validation_and_mcp_identity_are_checked_without_waking_a_member(self):
        for args in ({"recent_turns": 6}, {"recent_turns": True}, {"limit": 11}, {"cursor": -1},
                     {"member": []}, {"member": "missing"}, {"unknown": True}):
            self.assertFalse(self.query(**args)["accepted"], args)
        params = {"name": "list_members", "arguments": {"member": "B"},
                  "_meta": {"callId": "query", "x-codex-turn-metadata": {"thread_id": "thread-A", "turn_id": "turn-A"}}}
        self.assertTrue(self.room.peers.mcp_call(params)["accepted"])
        self.room.client.active.clear()
        result = self.room.peers.mcp_call(params)
        self.assertFalse(result["accepted"])
        self.assertNotIn("recent_dialogue", json.dumps(result))

    def test_tools_are_available_in_new_and_old_threads_without_replacing_the_thread(self):
        member = self.room.agents["A"]
        old_id = member.thread_id
        for version in range(TOOL_VERSION):
            member.room_tool_version = version
            options = self.room.peers.thread_options(member)
            self.assertIn("config", options)
            self.assertNotIn("dynamicTools", options)
            self.assertEqual(member.thread_id, old_id)
            self.assertIn("codex_room_public/list_members", self.room.peers.context(old_id))
        member.room_tool_version = TOOL_VERSION
        self.assertEqual(self.room.peers.thread_options(member), {})
        self.assertIn("room.list_members", self.room.peers.context(old_id))
        self.assertEqual([t["name"] for t in dynamic_tools()[0]["tools"]], ["send_message", "list_members"])
        self.assertTrue(next(t for t in mcp_tools() if t["name"] == "list_members")["annotations"]["readOnlyHint"])


if __name__ == "__main__":
    unittest.main()
