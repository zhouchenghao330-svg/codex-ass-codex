import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input

from codex_room.app import AddMember, CodexRoom
from codex_room.member_config import parse_add, resolve_directory
from codex_room.runtime import CodexClient


ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


class AddCommandTests(unittest.TestCase):
    def test_paths_quotes_colors_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            (base / "blue").mkdir()
            self.assertEqual(parse_add('/add a "./My Project" --color blue', base).cwd, "./My Project")
            self.assertEqual(parse_add('/add a "./My Project" --color blue', base).color, "blue")
            self.assertEqual(parse_add('/add a blue', base).cwd, "blue")
            self.assertEqual(parse_add('/add a rose', base).color, "rose")
            self.assertEqual(parse_add('/add a ./blue #ABC', base).color, "#ABC")
            self.assertEqual(parse_add('/add a --color=blue', base).color, "blue")
            self.assertEqual(resolve_directory('blue', base), base / 'blue')
            self.assertEqual(resolve_directory('~', base), Path.home().resolve())
            self.assertEqual(resolve_directory(None, base), base)
            self.assertIsNone(parse_add('/add', base))
            for text in ('/add a "unterminated', '/add a --color', '/add a --unknown x',
                         '/add a . blue --color rose', '/add a . extra arguments'):
                with self.subTest(text=text), self.assertRaises(ValueError):
                    parse_add(text, base)
            (base / "file.txt").write_text("file")
            for path in ('missing', 'file.txt'):
                with self.assertRaises(ValueError):
                    resolve_directory(path, base)


class MemberDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_members_use_and_restore_their_own_directories_and_configs(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            base = Path(tmp).resolve()
            room = base / "room"
            room.mkdir()
            a, b = room / "project-a", base / "Project B"
            for directory, marker, model in ((a, "alpha", "project-model-a"), (b, "beta", "project-model-b")):
                directory.mkdir()
                (directory / "marker.txt").write_text(marker)
                (directory / ".codex").mkdir()
                (directory / ".codex/config.toml").write_text(f'model = "{model}"\nmodel_reasoning_effort = "low"\n')
            original_cwd = Path.cwd()
            state = base / "state"
            app = CodexRoom(cwd=room, state_dir=state)
            async with app.run_test(size=(110, 36)) as pilot:
                await app.connection_task
                self.assertTrue(await app.submit('/add a ./project-a --color blue'))
                self.assertTrue(await app.submit(f'/add b "{b}" --color rose'))
                self.assertTrue(await app.submit('/add root'))
                self.assertEqual(app.agents['a'].cwd, str(a))
                self.assertEqual(app.agents['b'].cwd, str(b))
                self.assertEqual(app.agents['root'].cwd, str(room))
                self.assertEqual(app.agents['a'].model, 'project-model-a')
                self.assertEqual(app.agents['b'].model, 'project-model-b')
                self.assertEqual(app.agents['a'].effort, 'low')
                composer = app.query_one('#message-input', Input)
                composer.value = '/add missing ./not-here'
                await pilot.press('enter')
                self.assertEqual(composer.value, '/add missing ./not-here')
                self.assertNotIn('missing', app.agents)
                app.agents['a'].permission = 'workspace'
                await app.submit('@a cwd-probe')
                await app.submit('@b cwd-probe')
                await asyncio.wait_for(asyncio.gather(app.tasks['a'], app.tasks['b']), 5)
                for name, directory, marker in (('a', a, 'alpha'), ('b', b, 'beta')):
                    reply = json.loads(app.agents[name].history[-1].body.split(': ', 1)[1])
                    self.assertEqual(reply['cwd'], str(directory))
                    self.assertEqual(reply['threadCwd'], str(directory))
                    self.assertEqual(reply['marker'], marker)
                reply_a = json.loads(app.agents['a'].history[-1].body.split(': ', 1)[1])
                self.assertEqual(reply_a['sandbox']['writableRoots'], [str(a)])
                self.assertEqual(app.cwd, room)
                self.assertEqual(Path.cwd(), original_cwd)
                threads = {name: app.agents[name].thread_id for name in ('a', 'b')}

                await pilot.resize_terminal(80, 26)
                await pilot.press('ctrl+n')
                await pilot.pause()
                self.assertIsInstance(app.screen, AddMember)
                app.screen.query_one('#new-name', Input).value = 'dialog'
                app.screen.query_one('#new-cwd', Input).value = './missing'
                self.assertTrue(await pilot.click('#confirm-add'))
                self.assertIsInstance(app.screen, AddMember)
                self.assertNotIn('dialog', app.agents)
                app.screen.query_one('#new-cwd', Input).value = '../Project B'
                app.screen.query_one('#new-cwd', Input).focus()
                await pilot.press('enter')
                async with asyncio.timeout(3):
                    while 'dialog' not in app.agents:
                        await pilot.pause(0.02)
                self.assertEqual(app.agents['dialog'].cwd, str(b))
            restored = CodexRoom(cwd=room, state_dir=state)
            async with restored.run_test(size=(100, 32)) as pilot:
                await restored.connection_task
                for name, directory in (('a', a), ('b', b)):
                    self.assertEqual(restored.agents[name].cwd, str(directory))
                    self.assertEqual(restored.agents[name].thread_id, threads[name])
                    await restored.submit(f'@{name} cwd-probe')
                await asyncio.wait_for(asyncio.gather(restored.tasks['a'], restored.tasks['b']), 5)
                for name, marker in (('a', 'alpha'), ('b', 'beta')):
                    self.assertIn(marker, restored.agents[name].history[-1].body)
                    self.assertEqual(restored.agents[name].thread_id, threads[name])

    async def test_legacy_members_inherit_the_room_directory(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            path = Path(tmp, 'state.json')
            path.write_text(json.dumps({'version': 2, 'selected': 'old', 'entries': [], 'agents': [
                {'name': 'old', 'label': 'old', 'color': '#a9b6ff', 'thread_id': 'keep-thread'},
            ]}))
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(80, 26)) as pilot:
                await app.connection_task
                self.assertEqual(app.agents['old'].cwd, str(ROOT))
                self.assertEqual(app.agents['old'].thread_id, 'keep-thread')
            self.assertEqual(json.loads(path.read_text())['agents'][0]['cwd'], str(ROOT))
