"""Opt-in real Codex import check. Uses tokens, never resumes the user's live thread."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4

from codex_room.attachments import Prompt
from codex_room.backend import Service
from codex_room.runtime import CodexClient


async def settle(room, name):
    await asyncio.wait_for(room.tasks[name], 150)
    answer = next(e for e in reversed(room.entries) if e.sender == name)
    assert answer.state == 'done', answer.body
    return answer


async def main():
    proof = {}
    with tempfile.TemporaryDirectory(prefix='room-resume-live-') as tmp:
        root = Path(tmp).resolve()
        project, room_path = root/'original project', root/'room'
        project.mkdir()
        room_path.mkdir()
        marker = 'RESUME_' + uuid4().hex[:12]
        original = CodexClient(project)
        await original.start()
        agent = SimpleNamespace(name='external', label='external', cwd=str(project), thread_id=None,
            model='gpt-6.1-sol', effort='medium', permission='read-only', service_tier=None,
            legacy_instructions=False, preserve_thread_title=True)
        try:
            await original.ensure_thread(agent)
            await original.request('thread/name/set', {'threadId': agent.thread_id, 'name': 'Resume integration test'})
            events = []
            async def receive(event):
                events.append(event)
            await asyncio.wait_for(original.run_turn(agent,
                f'这是会话迁移测试。请记住暗号 {marker}。不要调用工具，只回复“已记住”。', receive), 150)
            proof['thread_id'] = agent.thread_id
        finally:
            await original.close()
        messages = []
        room = Service(room_path, emit=messages.append)
        await room.start()
        try:
            assert room.connected
            await room.command(f'/resume {agent.thread_id} 开发')
            m = room.agents['开发']
            assert m.cwd == str(project), m.cwd
            assert m.model == 'gpt-6.1-sol', m.model
            assert m.effort == 'medium', m.effort
            assert m.permission == 'read-only', m.permission
            assert any(e.sender == 'you' and marker in e.body for e in room.entries)
            assert any(e.sender == '开发' and '已记住' in e.body for e in room.entries)
            assert not room.tasks
            proof.update(history=True, same_thread=True, original_settings=True, no_turn_on_import=True)
            await room.command('/add 同事')  # Only a roster entry, no model call.
            await room.submit(Prompt('@开发 回复我之前让你记住的完整暗号，然后调用当前宿主说明中指定的 Room list_members 工具，recent_turns=0，查询现有同事。不要修改文件，不要调用其他工具。'))
            answer = await settle(room, '开发')
            assert marker in answer.body, answer.body
            tools = [b for b in answer.blocks if b.kind == 'tool' and 'list_members' in b.title]
            assert tools and any('同事' in b.text and b.status not in {'failed', 'stopped'} for b in tools), [b.title for b in answer.blocks]
            assert room.agents['开发'].thread_id == agent.thread_id
            proof.update(context_recalled=True, room_tools=True)
            await room.command('/resume ' + agent.thread_id)
            ids = [e.id for e in room.entries]
        finally:
            await room.close()
        room = Service(room_path, emit=messages.append)
        await room.start()
        try:
            assert [e.id for e in room.entries] == ids
            await room.submit(Prompt('@开发 之前的完整暗号是什么？只回复暗号，不调用工具。'))
            answer = await settle(room, '开发')
            assert marker in answer.body, answer.body
            assert room.agents['开发'].thread_id == agent.thread_id
            proof['restart_same_context'] = True
        finally:
            await room.close()
    target = Path('artifacts/resume')
    target.mkdir(parents=True, exist_ok=True)
    (target/'live.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2))
    print(json.dumps(proof, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
