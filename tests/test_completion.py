import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Input

from codex_room.app import ComposerInput, Agent, CodexRoom
from codex_room.completion import CompletionEngine, is_room_command
from codex_room.member_config import parse_add
from codex_room.runtime import CodexClient


ROOT = Path(__file__).resolve().parents[1]
MODELS = [{"id": "test-model", "displayName": "Test Model", "supportedReasoningEfforts": [
    {"reasoningEffort": "low"}, {"reasoningEffort": "high"}]}]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


class CompletionTests(unittest.TestCase):
    def test_file_paths_are_text_and_only_exact_registered_names_are_commands(self):
        engine = CompletionEngine()
        agents = {"a": Agent("a", "a", "purple")}
        for text in ("/Users/me/project/doc.md", "/model/config.toml", "/tmp", "/unknown"):
            with self.subTest(text=text):
                self.assertFalse(is_room_command(text))
                for prefix in ("", "@a "):
                    value = prefix + text
                    self.assertIsNone(engine.complete(value, len(value), agents, ROOT, []))
                    self.assertIsNone(engine.complete(value, len(value), agents, ROOT, [], explicit=True))
        for text in ("/model", "/MODEL test-model", "/permission full", "/permisson full", "/del a"):
            self.assertTrue(is_room_command(text))
        for text in ("", "ordinary /model text", "/mem"):
            self.assertFalse(is_room_command(text))

    def test_command_contexts_use_members_catalog_permissions_and_unoccupied_colors(self):
        engine = CompletionEngine()
        agents = {"a": Agent("a", "a", "purple"), "b": Agent("b", "b", "mint", model="custom-model")}
        cases = {
            "/adh": ["/adhd"], "/adhd ": ["on", "off"], "/adhd of": ["off"],
            "/mem": ["/members"], "@a /mod": ["/model"], "/permisson": ["/permissions"],
            "/focus ": ["a", "b", "all"], "/focus al": ["all"], "@a /stop @": ["@a", "@b"],
            "/d": ["/del"], "/del ": ["a", "b"], "@a /del @": ["@a", "@b"],
            "/model te": ["test-model"], "/model cu": ["custom-model"],
            "/model test-model ": ["low", "high"], "/permissions w": ["workspace"],
            "/add new --co": ["--color"], "/add new --color p": ["peach"],
            "/add new bl": ["blue"],
            "/add new . --color=bl": ["--color=blue"],
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                result = engine.complete(text, len(text), agents, ROOT, MODELS)
                self.assertEqual([item.value for item in result.candidates], expected)
        for text in ("hello /mem", "please @a", "/add new", "@a 你好", "/members"):
            with self.subTest(prose=text):
                self.assertIsNone(engine.complete(text, len(text), agents, ROOT, MODELS))

    def test_mid_token_and_quoted_directory_completion_preserve_other_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "My Project").mkdir()
            (base / "My File").write_text("not a directory")
            engine = CompletionEngine()
            text = '/add new "./My P" --color blue'
            cursor = text.index('P"') + 1
            result = engine.complete(text, cursor, {}, base, [])
            self.assertEqual([item.value for item in result.candidates], ["./My Project/"])
            completed, position = result.apply(text, result.candidates[0])
            self.assertEqual(shlex.split(completed), ["/add", "new", "./My Project/", "--color", "blue"])
            self.assertEqual(parse_add(completed, base).cwd, "./My Project/")
            self.assertEqual(completed[position:], " --color blue")
            for text in ('/add new "./My P', r'/add new ./My\ P'):
                result = engine.complete(text, len(text), {}, base, [])
                completed, _ = result.apply(text, result.candidates[0])
                self.assertEqual(parse_add(completed, base).cwd, "./My Project/")
            text = "/foc a"
            result = engine.complete(text, 4, {}, base, [])
            self.assertEqual(result.apply(text, result.candidates[0]), ("/focus a", 7))
            (base / "blue").mkdir()
            result = engine.complete("/add new bl", 11, {}, base, [], explicit=True)
            self.assertEqual([item.value for item in result.candidates], ["blue/", "--color=blue"])
            text = "/add new ~codex-room-nonexistent-user/"
            result = engine.complete(text, len(text), {}, base, [])
            self.assertEqual(result.candidates, ())


class CompletionUITests(unittest.IsolatedAsyncioTestCase):
    async def test_absolute_paths_with_and_without_mention_reach_codex_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(100, 36)) as pilot:
                await app.connection_task
                await app.submit("/add a")
                composer = app.query_one("#message-input", ComposerInput)
                messages = ("/Users/me/orca/build-mcp/docs/experiment.md 你看下这个内容，和 query_sql_workflow 相比？",
                            "@a /Users/me/My Project/experiment.md 请读这份文件",
                            "/tmp", "@a /model/config.toml 请读这个配置")
                for message in messages:
                    composer.value = message
                    composer.cursor_position = len(message)
                    await pilot.pause()
                    self.assertFalse(app.completion_open)
                    await pilot.press("enter")
                    await app.tasks["a"]
                    expected = message.removeprefix("@a ")
                    own, reply = app.agents["a"].history[-2:]
                    self.assertEqual(own.body, expected)
                    self.assertIn(expected, reply.body)
                    self.assertEqual(composer.value, "")
                self.assertTrue(await app.submit("@a /permissions read-only"))
                self.assertEqual(app.agents["a"].permission, "read-only")

    async def test_tab_completion_member_selection_and_settings_do_not_execute_early(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(110, 36)) as pilot:
                await app.connection_task
                await app.submit("/add a")
                await app.submit("/add b")
                app.select("a")
                composer = app.query_one("#message-input", ComposerInput)

                async def draft(value, cursor=None):
                    composer.value = value
                    composer.cursor_position = len(value) if cursor is None else cursor
                    await pilot.pause()

                await draft("")
                await pilot.press("/", "m", "e", "m", "tab")
                self.assertEqual(composer.value, "/members ")
                self.assertFalse(app.sidebar_visible)
                await pilot.press("enter")
                self.assertTrue(app.sidebar_visible)
                await draft("/focus ")
                self.assertEqual(app.completion_matches, ["a", "b", "all"])
                await pilot.press("down", "tab")
                self.assertEqual(composer.value, "/focus b ")
                self.assertEqual(app.selected, "a")
                await pilot.press("enter")
                self.assertEqual(app.selected, "b")
                self.assertFalse(app.show_all)

                await app.submit("@a slow")
                app.select("b")
                await draft("/focus al")
                await pilot.press("tab")
                self.assertEqual(composer.value, "/focus all ")
                self.assertFalse(app.show_all)
                await pilot.press("enter")
                self.assertTrue(app.show_all)
                self.assertEqual(app.selected, "b")
                self.assertTrue(all(card.display for card in app.cards.values()))
                self.assertTrue(app.agents["a"].busy)
                self.assertFalse(any(entry.sender == "you" and "/focus" in entry.body for entry in app.entries))
                await app.submit("/focus b")
                await draft("/stop ")
                await pilot.press("enter")
                self.assertEqual(composer.value, "/stop a ")
                self.assertTrue(app.agents["a"].busy)
                self.assertEqual(app.selected, "b")
                await pilot.press("enter")
                self.assertFalse(app.agents["a"].busy)

                await draft("@b /per")
                await pilot.press("tab")
                self.assertEqual(composer.value, "@b /permissions ")
                self.assertEqual(app.completion_matches, ["full", "workspace", "read-only"])
                await pilot.press("down", "tab")
                self.assertEqual(app.agents["b"].permission, "full")
                await pilot.press("enter")
                self.assertEqual(app.agents["b"].permission, "workspace")

                await draft("/model te")
                await pilot.press("tab")
                self.assertEqual(app.completion_matches, ["low", "high"])
                await pilot.press("tab")
                self.assertEqual(composer.value, "/model test-model low ")
                self.assertEqual(app.agents["b"].effort, "high")
                await pilot.press("enter")
                self.assertEqual(app.agents["b"].effort, "low")
                await draft("/model custom-unlisted")
                await pilot.press("enter")
                self.assertEqual(app.agents["b"].model, "custom-unlisted")

    async def test_popup_escape_mouse_cursor_editing_and_plain_text_tab(self):
        app = CodexRoom(demo=True)
        async with app.run_test(size=(80, 26)) as pilot:
            await app.create_member("alice")
            await app.create_member("bob")
            composer = app.query_one("#message-input", ComposerInput)
            composer.value = "/focus "
            composer.cursor_position = len(composer.value)
            await pilot.pause()
            before = len(app.entries)
            await pilot.click("#completion-list", offset=(3, 2))
            self.assertEqual(composer.value, "/focus bob ")
            self.assertEqual(len(app.entries), before)
            self.assertTrue(composer.has_focus)
            composer.value = "/foc alice"
            composer.cursor_position = 4
            await pilot.press("tab")
            self.assertEqual(composer.value, "/focus alice")
            self.assertEqual(composer.cursor_position, 7)
            composer.value = "/"
            composer.cursor_position = 1
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause(0.2)
            self.assertFalse(app.completion_open)
            await pilot.press("tab")
            self.assertEqual(composer.value, "/add ")
            composer.value = "ordinary text @bob /mem"
            composer.cursor_position = len(composer.value)
            await pilot.press("tab")
            self.assertEqual(composer.value, "ordinary text @bob /mem")
            self.assertTrue(composer.has_focus)

    async def test_add_directory_spaces_nested_tab_and_colors(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            base = Path(tmp).resolve()
            room = base / "room"
            room.mkdir()
            project = room / "My Project"
            project.mkdir()
            (project / "child").mkdir()
            app = CodexRoom(cwd=room, state_dir=base / "state")
            async with app.run_test(size=(100, 32)) as pilot:
                await app.connection_task
                composer = app.query_one("#message-input", ComposerInput)
                composer.value = '/add new "./My P'
                composer.cursor_position = len(composer.value)
                await pilot.press("tab")
                self.assertEqual(parse_add(composer.value, room).cwd, "./My Project/")
                self.assertFalse(app.completion_open)
                self.assertNotIn("new", app.agents)
                await pilot.press("tab")
                self.assertEqual(parse_add(composer.value, room).cwd, "./My Project/child/")
                await pilot.press("enter")
                self.assertEqual(app.agents["new"].cwd, str(project / "child"))
                composer.value = "/add other --color=bl"
                composer.cursor_position = len(composer.value)
                await pilot.press("tab")
                self.assertEqual(parse_add(composer.value, room).color, "blue")
                await pilot.press("enter")
                self.assertEqual(app.agents["other"].color, "#83c8e8")
                composer.value = "/add third . --color "
                composer.cursor_position = len(composer.value)
                await pilot.pause()
                self.assertNotIn("purple", app.completion_matches)
                self.assertNotIn("blue", app.completion_matches)
                self.assertIn("auto", app.completion_matches)
