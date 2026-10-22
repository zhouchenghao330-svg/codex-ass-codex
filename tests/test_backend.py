"""The Rust UI's service contract, tested against a deterministic Codex peer."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from codex_room.attachments import Prompt
from codex_room.backend import Service, wire_entry
from codex_room.forwarding import ForwardQuote

FAKE = Path(__file__).with_name("fake_codex.py")


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temp.name)
        self.output = []
        self.service = Service(self.cwd, command=[sys.executable, str(FAKE)], emit=self.output.append)
        await self.service.start()
        await self.service.command("/add 开发")

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def until(self, predicate, timeout=5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.01)

    async def send_done(self, text, name="开发"):
        await self.service.submit(Prompt(f"@{name} {text}"))
        await self.until(lambda: not self.service.agents[name].busy)
        return next(e for e in reversed(self.service.entries) if e.sender == name)

    async def test_thread_identity_and_context_persist(self):
        answer = await self.send_done("remember retained-context")
        member = self.service.agents["开发"]
        tid = member.thread_id
        self.assertTrue(tid)
        answer = await self.send_done("recall")
        self.assertIn("retained-context", answer.body)
        self.assertEqual(member.thread_id, tid)
        state = json.loads((self.cwd/".codex-room/state.json").read_text())
        self.assertEqual(state["agents"][0]["thread_id"], tid)
        self.assertEqual(state["version"], 2)

    async def test_multi_recipient_is_validated_before_any_dispatch(self):
        await self.service.command("/add 笔记")
        with self.assertRaises(ValueError):
            await self.service.submit(Prompt("@开发 @不存在 test"))
        self.assertEqual(self.service.entries, [])
        await self.service.submit(Prompt("@开发 @笔记 @开发 parallel"))
        await self.until(lambda: all(not a.busy for a in self.service.agents.values()))
        queries = [e for e in self.service.entries if e.sender=="you"]
        self.assertEqual(len(queries), 2)
        self.assertEqual(len({e.batch_id for e in queries}), 1)
        self.assertNotEqual(self.service.agents["开发"].thread_id, self.service.agents["笔记"].thread_id)

    async def test_steer_splits_output_and_keeps_old_tool_owner(self):
        await self.service.submit(Prompt("steer-stream"))
        await self.until(lambda: any(e.blocks and any(b.kind=="command" for b in e.blocks) for e in self.service.entries))
        await self.service.submit(Prompt("FOLLOWUP"))
        await self.service.submit(Prompt("finish-steer"))
        await self.until(lambda:not self.service.agents["开发"].busy)
        entries = self.service.entries
        query = next(i for i,e in enumerate(entries) if e.sender=="you" and e.body=="FOLLOWUP")
        self.assertEqual(entries[query].state, "steered")
        before = entries[query-1]
        after = next(e for e in entries[query+1:] if e.sender=="开发")
        self.assertEqual(before.state,"continued")
        self.assertIn("BEFORE_FOLLOWUP",before.body)
        self.assertIn("OUTPUT_AFTER_FOLLOWUP",next(b.text for b in before.blocks if b.kind=="command"))
        self.assertNotIn("BEFORE_FOLLOWUP",after.body)
        self.assertIn("FOLLOWUP",after.body)
        self.assertEqual(before.run_id,after.run_id)
        self.assertIsNone(before.duration_seconds)
        self.assertIsNotNone(after.duration_seconds)

    async def test_tools_files_and_public_reasoning(self):
        answer = await self.send_done("mixed-tools")
        data = wire_entry(answer)
        file = next(b for b in data["blocks"] if b["kind"]=="file")
        self.assertEqual(len(file["files"]),4)
        self.assertEqual(file["files"][0]["added"],2)
        self.assertEqual(file["files"][0]["deleted"],1)
        self.assertEqual(file["files"][3]["action"],"rename")
        answer = await self.send_done("thinking")
        self.assertNotIn("RAW_REASONING_SENTINEL",json.dumps(wire_entry(answer)))
        self.assertNotIn("PUBLIC_SUMMARY_BODY",json.dumps(wire_entry(answer)))

    async def test_stop_delete_archives_and_new_member_gets_new_thread(self):
        self.service.drafts["开发"]={"prompt":"old draft"}
        await self.service.submit(Prompt("slow"))
        await self.until(lambda:bool(self.service.client.active))
        await self.service.command("/del 开发")
        self.assertNotIn("开发",self.service.agents)
        archives = list((self.cwd/".codex-room/archives").glob("*.json"))
        self.assertEqual(len(archives),1)
        archived = json.loads(archives[0].read_text())
        self.assertTrue(archived)
        self.assertEqual(self.service.client.active,{})
        self.assertNotIn("开发",self.service.drafts)
        await self.service.command("/add 开发")
        self.assertIsNone(self.service.agents["开发"].thread_id)

    async def test_approval_and_multi_question_contract(self):
        await self.service.submit(Prompt("approval"))
        await self.until(lambda:bool(self.service.interactions))
        key = next(iter(self.service.interactions))
        await self.service.handle({"type":"answer","id":key,"result":{"decision":"accept"}})
        await self.until(lambda:not self.service.agents["开发"].busy)
        self.assertIn("accept",self.service.entries[-1].body)
        await self.service.submit(Prompt("interactive"))
        await self.until(lambda:bool(self.service.interactions))
        key=next(iter(self.service.interactions))
        await self.service.handle({"type":"answer","id":key,"result":{"answers":{"color":{"answers":["blue"]}}}})
        await self.until(lambda:not self.service.agents["开发"].busy)
        self.assertIn("blue",self.service.entries[-1].body)

    async def test_paths_are_messages_and_completion_is_unicode_safe(self):
        answer=await self.send_done("/Users/some/path")
        self.assertIn("/Users/some/path",answer.body)
        await self.service.handle({"type":"complete","id":1,"text":"/mem","cursor":4})
        event=next(e for e in reversed(self.output) if e["type"]=="completion")
        self.assertEqual(event["type"],"completion")
        self.assertEqual(event["candidates"][0]["text"],"/members ")
        await self.service.command("/rename 开发 环境排查")
        await self.service.handle({"type":"complete","id":2,"text":"@环","cursor":2})
        event=next(e for e in reversed(self.output) if e["type"]=="completion")
        self.assertEqual(event["candidates"][0]["value"],"@环境排查")

    async def test_forwarding_keeps_source_quotes_and_full_tools(self):
        await self.service.command("/add 笔记")
        self.service.selected="笔记"
        quote=ForwardQuote(messages=[{"id":"source","member":"开发","speaker":"开发","recipient":"开发",
            "cwd":str(self.cwd),"time":"12:00","text":"结论","blocks":[{"id":"tool","kind":"command","title":"pwd","text":"FULL_OUTPUT"}],"images":[]}],
            source="开发",source_label="开发").dump()
        await self.service.submit(Prompt("写进笔记"),quote)
        await self.until(lambda:not self.service.agents["笔记"].busy)
        query=next(e for e in self.service.entries if e.sender=="you")
        self.assertEqual(query.quote,quote)
        self.assertIn("FULL_OUTPUT",query.input_text)
        self.assertIn("写进笔记",query.input_text)
        self.assertFalse(self.service.agents["开发"].thread_id)

    async def test_migration_keeps_drafts_unknown_fields_and_does_not_replay(self):
        await self.service.close()
        path=self.cwd/".codex-room/state.json"
        state=json.loads(path.read_text())
        state["future_global"]={"keep":True}
        state["agents"][0]["future_member"]="keep"
        state["entries"]=[{"sender":"开发","body":"old unfinished","state":"working","id":"old","future_entry":"keep"}]
        state["drafts"]={"开发":{"prompt":{"text":"[Pasted Content 6000 chars]","attachments":[
            {"start":0,"end":27,"label":"[Pasted Content 6000 chars]","kind":"paste","content":"x"*6000}]}}}
        path.write_text(json.dumps(state))
        self.service=Service(self.cwd,command=[sys.executable,str(FAKE)],emit=self.output.append)
        await self.service.start()
        self.assertEqual(self.service.entries[0].state,"stopped")
        self.assertFalse(self.service.agents["开发"].busy)
        self.service.dirty=True
        self.service.save()
        saved=json.loads(path.read_text())
        self.assertEqual(saved["future_global"],{"keep":True})
        self.assertEqual(saved["agents"][0]["future_member"],"keep")
        self.assertEqual(saved["entries"][0]["future_entry"],"keep")
        self.assertEqual(saved["drafts"],state["drafts"])

    async def test_rejected_submission_survives_quit_before_ui_reads_ack(self):
        await self.service.handle({"type":"submit","id":42,"prompt":"@不存在 original"})
        await self.service.handle({"type":"drafts","drafts":{"开发":{"prompt":"new typing"}},"pending":{
            "id":42,"view":"开发","prompt":"@不存在 original","quote":None}})
        self.service.save()
        saved=json.loads((self.cwd/".codex-room/state.json").read_text())
        self.assertEqual(saved["drafts"]["开发"]["prompt"],"@不存在 original\nnew typing")
        self.assertEqual(saved["entries"],[])

    async def test_legacy_fallback_preserves_rust_drafts_and_thread(self):
        from unittest.mock import patch
        from codex_room.app import CodexRoom
        from codex_room.runtime import CodexClient
        await self.send_done("remember SAME_THREAD")
        thread=self.service.agents["开发"].thread_id
        self.service.drafts={"开发":{"prompt":"尚未发送的草稿","quote":None}}
        await self.service.close()
        with patch("codex_room.app.CodexClient",lambda cwd:CodexClient(cwd,command=[sys.executable,str(FAKE)])):
            legacy=CodexRoom(cwd=self.cwd)
            async with legacy.run_test(size=(100,30)):
                await legacy.connection_task
                legacy.dirty=True
                legacy.save_state()
        state=json.loads((self.cwd/".codex-room/state.json").read_text())
        self.assertEqual(state["drafts"]["开发"]["prompt"],"尚未发送的草稿")
        self.assertEqual(state["agents"][0]["thread_id"],thread)
        self.service=Service(self.cwd,command=[sys.executable,str(FAKE)],emit=self.output.append)
