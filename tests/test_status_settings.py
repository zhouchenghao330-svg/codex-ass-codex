"""Composer settings use the member's catalog/config and never start a model turn."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from codex_room.backend import Service


class StatusSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.events = []
        self.room = Service(self.cwd, command=[sys.executable, str(Path(__file__).with_name('fake_codex.py'))], emit=self.events.append)
        await self.room.start()
        self.room.client.models[0]['serviceTiers'] = [{'id': 'priority', 'name': 'Fast'}]
        await self.room.command('/add A')
        await self.room.command('/add B')

    async def asyncTearDown(self):
        await self.room.close()
        self.tmp.cleanup()

    async def test_model_and_direct_effort_requests_preserve_draft_and_apply_only_after_confirmation(self):
        a = self.room.agents['A']
        self.room.drafts = {'A': {'prompt': '未发送的草稿'}}
        await self.room.handle({'type': 'member-settings', 'member': 'A', 'setting': 'effort', 'id': 7})
        choice = next(e for e in reversed(self.events) if e['type'] == 'choose')
        self.assertEqual((choice['placement'], choice['request_id'], choice['action']), ('status', 7, 'member-effort'))
        self.assertEqual(choice['data'], {'member': 'A', 'model': 'test-model'})
        self.room.client.models.append({'id': 'other', 'defaultReasoningEffort': 'low',
                                       'supportedReasoningEfforts': [{'reasoningEffort': 'low'}]})
        await self.room.handle({'type': 'member-model', 'member': 'A', 'model': 'other', 'placement': 'status', 'request_id': 8})
        choice = next(e for e in reversed(self.events) if e['type'] == 'choose')
        self.assertEqual((choice['placement'], choice['request_id'], choice['selected']), ('status', 8, 'low'))
        self.assertEqual((a.model, a.effort), ('test-model', 'high'))
        await self.room.command('/rename A 新显示名')
        await self.room.handle({'type': 'member-effort', 'member': 'A', 'model': 'other', 'effort': 'low'})
        self.assertEqual((a.model, a.effort), ('other', 'low'))
        self.assertEqual(self.room.agents['B'].model, 'test-model')
        self.assertEqual(self.room.selected, 'B')
        self.assertEqual(self.room.drafts['A']['prompt'], '未发送的草稿')
        self.assertIsNone(a.thread_id)
        self.assertFalse(self.room.entries)

    async def test_fast_display_resolves_inherited_project_config_and_does_not_persist_derived_state(self):
        a = self.room.agents['A']
        project = self.cwd / 'another-project'
        (project / '.codex').mkdir(parents=True)
        (project / '.codex/config.toml').write_text('service_tier = "fast"\n')
        a.cwd = str(project)
        await self.room.refresh_fast_state(a)
        self.room.members()
        latest = next(e for e in reversed(self.events) if e['type'] == 'members')
        state = next(m for m in latest['agents'] if m['name'] == 'A')
        self.assertTrue(state['fast_enabled'])
        self.assertTrue(state['fast_supported'])
        self.assertIsNone(a.service_tier)
        await self.room.handle({'type': 'member-fast', 'member': 'A', 'enabled': False})
        self.assertFalse(a.fast_enabled)
        self.assertEqual(a.service_tier, 'default')
        self.assertIsNone(self.room.agents['B'].service_tier)
        await self.room.handle({'type': 'member-fast', 'member': 'A', 'enabled': True})
        self.assertTrue(a.fast_enabled)
        self.room.save()
        saved = json.loads((self.cwd / '.codex-room/state.json').read_text())
        for member in saved['agents']:
            self.assertNotIn('fast_enabled', member)
            self.assertNotIn('fast_supported', member)
        self.assertIsNone(a.thread_id)

    async def test_deleted_child_and_invalid_settings_return_scoped_errors(self):
        await self.room.handle({'type': 'member-settings', 'member': 'missing', 'setting': 'model', 'id': 10})
        self.assertEqual(self.events[-1]['type'], 'setting-error')
        self.assertEqual(self.events[-1]['request_id'], 10)
        a = self.room.agents['A']
        a.parent_name = 'B'
        await self.room.handle({'type': 'member-settings', 'member': 'A', 'setting': 'effort', 'id': 11})
        self.assertIn('父会话', next(e for e in reversed(self.events) if e['type'] == 'setting-error')['text'])
        with self.assertRaises(ValueError):
            await self.room.handle({'type': 'member-fast', 'member': 'A', 'enabled': True})
        a.parent_name = ''
        self.room.client.models[0]['serviceTiers'] = []
        with self.assertRaises(ValueError):
            await self.room.handle({'type': 'member-fast', 'member': 'A', 'enabled': True})
        self.assertIsNone(a.service_tier)
        self.assertIsNone(a.thread_id)


if __name__ == '__main__':
    unittest.main()
