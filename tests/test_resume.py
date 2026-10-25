import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

from codex_room.attachments import Attachment, Prompt
from codex_room.backend import Service
from codex_room.resume import parse_resume

FAKE = Path(__file__).with_name('fake_resume_codex.py')


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name).resolve()
        self.project = self.cwd / 'original project'
        self.project.mkdir()
        self.tid = str(uuid4())
        self.policy = {'type': 'workspaceWrite', 'writableRoots': [str(self.project)],
                       'networkAccess': True, 'excludeTmpdirEnvVar': True}
        thread = {'id': self.tid, 'name': '原项目开发', 'cwd': str(self.project),
                  'model': 'ORIGINAL_MODEL', 'reasoningEffort': 'medium', 'serviceTier': 'fast',
                  'approvalPolicy': 'untrusted', 'sandbox': self.policy, 'memory': 'RETAINED_TOKEN',
                  'status': {'type': 'notLoaded'}, 'canAcceptDirectInput': True}
        self.turns = [
            {'id': 'old-1', 'status': 'completed', 'startedAt': 1700000000, 'completedAt': 1700000003, 'items': [
                {'id': 'env', 'type': 'userMessage', 'content': [{'type': 'text', 'text': '<environment_context>\nHOST\n</environment_context>'}]},
                {'id': 'q1', 'type': 'userMessage', 'content': [{'type': 'text', 'text': '[Codex Room host context; metadata]\nHIDE_HOST'},
                    {'type': 'text', 'text': '原问题中文'}, {'type': 'localImage', 'path': str(self.project/'image.png')}]},
                {'id': 'r', 'type': 'reasoning', 'text': 'HIDE_RAW_REASONING'},
                {'id': 'a1', 'type': 'agentMessage', 'text': '准备调查'},
                {'id': 'cmd', 'type': 'commandExecution', 'command': 'pwd', 'aggregatedOutput': str(self.project), 'exitCode': 0, 'status': 'completed'},
                {'id': 'q2', 'type': 'userMessage', 'content': [{'type': 'text', 'text': '用户中途补充'}]},
                {'id': 'a2', 'type': 'agentMessage', 'text': '原回答 **Markdown**'},
            ]},
            {'id': 'old-2', 'status': 'completed', 'startedAt': 1700000004, 'items': [
                {'id': 'q3', 'type': 'userMessage', 'content': [{'type': 'text', 'text': '第二轮问题'}]},
                {'id': 'a3', 'type': 'agentMessage', 'text': '第二轮回答'},
            ]},
        ]
        (self.cwd/'resume-fixture.json').write_text(json.dumps({'threads': {self.tid: thread}, 'turns': {self.tid: self.turns}}))
        self.events = []
        self.room = self.new_room()
        await self.room.start()

    def new_room(self):
        return Service(self.cwd, command=[sys.executable, str(FAKE)], emit=self.events.append)

    async def asyncTearDown(self):
        await self.room.close()
        self.tmp.cleanup()

    async def state(self):
        return await self.room.client.request('fixture/state', {})

    async def test_restores_same_thread_history_settings_and_order_without_starting_a_turn(self):
        await self.room.submit(Prompt('/resume ' + self.tid))
        m = self.room.agents['原项目开发']
        self.assertEqual((m.cwd, m.model, m.effort, m.service_tier), (str(self.project), 'ORIGINAL_MODEL', 'medium', 'fast'))
        self.assertEqual(m.resume_policy, {'approvalPolicy': 'untrusted', 'sandboxPolicy': self.policy})
        self.assertEqual([e.body for e in self.room.entries], ['原问题中文\n[Image #1]', '准备调查', '用户中途补充', '原回答 **Markdown**', '第二轮问题', '第二轮回答'])
        self.assertEqual(self.room.entries[0].image_paths, [str(self.project/'image.png')])
        self.assertEqual(self.room.entries[1].blocks[1].kind, 'command')
        self.assertNotIn('HIDE_', json.dumps([e.body for e in self.room.entries]))
        calls = (await self.state())['calls']
        self.assertFalse(any(c['method'] in {'thread/start', 'turn/start', 'thread/name/set'} for c in calls))
        resume = next(c['params'] for c in calls if c['method'] == 'thread/resume')
        self.assertEqual(set(resume), {'threadId', 'excludeTurns', 'config'})
        self.assertIn('mcp_servers.codex_room_public', resume['config'])
        self.assertEqual(self.room.selected, m.name)
        self.assertFalse(self.room.show_all)

    async def test_repeated_import_selects_existing_member_and_never_duplicates_history(self):
        await self.room.command('/resume ' + self.tid + ' 开发')
        ids = [e.id for e in self.room.entries]
        await self.room.command('/focus all')
        await self.room.command('/resume ' + self.tid)
        self.assertEqual(list(self.room.agents), ['开发'])
        self.assertEqual([e.id for e in self.room.entries], ids)
        self.assertEqual(self.room.selected, '开发')

    async def test_context_and_exact_permissions_survive_restart_and_explicit_change(self):
        await self.room.command('/resume ' + self.tid + ' 开发')
        await self.room.close()
        self.room = self.new_room()
        await self.room.start()
        await self.room.submit(Prompt('recall'))
        async with asyncio.timeout(5):
            while self.room.agents['开发'].busy:
                await asyncio.sleep(.01)
        self.assertEqual(self.room.entries[-1].body, 'RETAINED_TOKEN')
        self.assertEqual(self.room.agents['开发'].thread_id, self.tid)
        calls = (await self.state())['calls']
        turn = next(c['params'] for c in calls if c['method'] == 'turn/start')
        self.assertEqual(turn['approvalPolicy'], 'untrusted')
        self.assertEqual(turn['sandboxPolicy'], self.policy)
        self.assertEqual(turn['cwd'], str(self.project))
        self.assertEqual(turn['model'], 'ORIGINAL_MODEL')
        self.assertEqual(turn['effort'], 'medium')
        await self.room.command('/permissions read-only')
        self.assertIsNone(self.room.agents['开发'].resume_policy)

    async def test_room_titles_are_short_and_rename_syncs_but_external_title_is_preserved(self):
        await self.room.command('/resume ' + self.tid)
        await self.room.command('/rename 原项目开发 审查')
        self.assertEqual((await self.state())['state']['threads'][self.tid]['name'], '原项目开发')
        await self.room.command('/add 新成员')
        await self.room.submit(Prompt('hello'))
        async with asyncio.timeout(5):
            while self.room.agents['新成员'].busy:
                await asyncio.sleep(.01)
        tid = self.room.agents['新成员'].thread_id
        self.assertEqual((await self.state())['state']['threads'][tid]['name'], 'Room · 新成员')
        await self.room.command('/rename 新成员 环境排查')
        self.assertEqual((await self.state())['state']['threads'][tid]['name'], 'Room · 环境排查')
        self.room.client.thread_names.clear()
        await self.room.sync_title(self.room.agents['新成员'])
        self.assertEqual((await self.state())['state']['threads'][tid]['name'], 'Room · 环境排查')

    async def test_invalid_missing_and_child_threads_do_not_create_members_or_turns(self):
        for value in ('not-an-id', str(uuid4())):
            with self.assertRaises(Exception):
                await self.room.command('/resume ' + value)
        with self.assertRaises(ValueError):
            await self.room.command('/resume ' + self.tid + ' 12invalid')
        self.assertFalse(self.room.agents)
        self.assertFalse(self.room.entries)
        self.assertFalse(any(c['method'] == 'turn/start' for c in (await self.state())['calls']))

    async def test_busy_child_missing_directory_and_unsupported_policy_are_rejected(self):
        for update, fragment in (({'status': {'type': 'active'}}, '正在运行'),
                                 ({'parentThreadId': 'parent'}, '子代理'),
                                 ({'cwd': str(self.cwd/'missing')}, '目录不存在'),
                                 ({'sandbox': {'type': 'externalSandbox'}}, '权限类型')):
            old = (await self.state())['state']['threads'][self.tid]
            await self.room.client.request('fixture/update', {'threadId': self.tid, 'thread': {**old, **update}})
            with self.assertRaisesRegex(ValueError, fragment):
                await self.room.command('/resume ' + self.tid)
            self.assertFalse(self.room.agents)
            self.assertFalse(self.room.entries)
            await self.room.client.request('fixture/update', {'threadId': self.tid, 'thread': old})

    async def test_unfinished_snapshot_does_not_commit_a_member(self):
        await self.room.client.request('fixture/unfinished', {'threadId': self.tid})
        with self.assertRaisesRegex(ValueError, '尚未结束'):
            await self.room.command('/resume ' + self.tid)
        self.assertFalse(self.room.agents)

    async def test_copyable_command_and_atomic_paste_are_local_inputs(self):
        copied = f"cd '/ignored/path' && env -u CODEX_HOME -u ORCA_CODEX_HOME codex '--dangerously-bypass-approvals-and-sandbox' '-c' 'tui.resume_cwd=current' 'resume' '{self.tid}'"
        label = '[Pasted Content]'
        prompt = Prompt('/resume ' + label, [Attachment(8, 8+len(label), 'paste', copied, label)])
        await self.room.submit(prompt)
        self.assertEqual(next(iter(self.room.agents.values())).cwd, str(self.project))
        self.assertFalse(any(c['method'] == 'turn/start' for c in (await self.state())['calls']))


class ResumeParserTests(unittest.TestCase):
    def test_strict_ids_quotes_and_command_not_shell_execution(self):
        tid = str(uuid4())
        self.assertEqual(parse_resume(f'/resume "{tid}" 开发'), (tid, '开发'))
        self.assertEqual(parse_resume(f'/RESUME\t{tid}'), (tid, None))
        self.assertIsNone(parse_resume('/resume'))
        for value in (f'codex resume {tid}; touch /tmp/should-not-run', f'env CODEX_HOME=/other codex resume {tid}',
                      f'codex resume {tid} && echo unexpected', f'cd /tmp ; codex resume {tid}'):
            with self.assertRaises(ValueError):
                parse_resume('/resume ' + value)
