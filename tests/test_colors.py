import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.geometry import Region
from textual.widgets import Input, Select

from codex_room.app import CodexRoom
from codex_room.colors import BACKGROUND, FOREGROUND, COLOR_CHOICES, choose_color, member_colors, normalize_color
from codex_room.runtime import CodexClient
from codex_room.transcript import Entry


ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


class ColorTests(unittest.TestCase):
    def test_allocation_never_wraps_and_aliases_cannot_reuse_a_color(self):
        used = []
        for _ in range(64):
            used.append(choose_color(used))
        self.assertEqual(len(used), len(set(used)))
        self.assertGreater(len(used), len(COLOR_CHOICES))
        self.assertEqual(normalize_color("#ABC"), "#aabbcc")
        self.assertEqual(normalize_color("蓝色"), normalize_color("blue"))
        for requested in ("blue", "蓝色", "#83C8E8"):
            with self.assertRaises(ValueError):
                choose_color(["#83c8e8"], requested)
        with self.assertRaises(ValueError):
            choose_color([], "not-a-color")


class ColorAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = {**os.environ, "COLORTERM": "truecolor", "TERM": "xterm-256color"}
        environment.pop("NO_COLOR", None)
        patcher = patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_command_picker_reply_colors_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(100, 36)) as pilot:
                await app.connection_task
                self.assertTrue(await app.submit("/add a"))
                self.assertTrue(await app.submit("/add b blue"))
                self.assertTrue(await app.submit("/add c"))
                self.assertEqual(app.agents["b"].color, normalize_color("blue"))
                self.assertFalse(await app.submit("/add duplicate 蓝色"))
                self.assertFalse(await app.submit("/add invalid not-a-color"))
                self.assertEqual(len(app.agents), 3)
                self.assertTrue(await app.submit("/add custom #ABC"))
                self.assertEqual(app.agents["custom"].color, "#aabbcc")

                await app.submit("/add")
                await pilot.pause()
                app.screen.query_one("#new-name", Input).value = "pink"
                await pilot.click("#new-color")
                self.assertTrue(app.screen.query_one("#new-color", Select).expanded)
                await pilot.press("down", "down", "enter")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#new-color", Select).value, normalize_color("rose"))
                self.assertEqual(app.screen.query_one("#color-preview").styles.background.hex.lower(),
                                 member_colors(normalize_color("rose")).background)
                self.assertTrue(await pilot.click("#confirm-add"))
                await pilot.pause()
                self.assertEqual(app.agents["pink"].color, normalize_color("rose"))
                expected = {name: agent.color for name, agent in app.agents.items()}
                self.assertEqual(len(expected), len(set(expected.values())))

                markdown = "## 测试标题\n\n这是一条**彩色回复**，带有 `inline`。\n\n```python\nprint('hello')\n```"
                backgrounds = set()
                foregrounds = set()
                markers = set()
                for name in ("a", "b", "pink"):
                    entry = Entry(name, markdown)
                    card = await app.add_entry(entry)
                    await pilot.pause()
                    colors = member_colors(app.agents[name].color)
                    self.assertEqual(card.styles.background.hex.lower(), colors.background)
                    self.assertEqual(card.styles.border_left[0], "solid")
                    self.assertEqual(card.styles.border_left[1].hex.lower(), colors.accent)
                    markers.add(card.styles.border_left[1].hex.lower())
                    backgrounds.add(card.styles.background.hex.lower())
                    strips = card.body.render_lines(Region(0, 0, card.body.size.width, card.body.size.height))
                    segments = [segment for strip in strips for segment in strip]
                    prose = next(segment for segment in segments if "这是一条" in segment.text)
                    foreground = prose.style.color.get_truecolor().hex.lower()
                    self.assertEqual(foreground, colors.text)
                    foregrounds.add(foreground)
                    heading = next(segment for segment in segments if "测试标题" in segment.text)
                    self.assertEqual(heading.style.color.get_truecolor().hex.lower(), FOREGROUND)
                    self.assertTrue(heading.style.bold)
                    self.assertTrue(any(segment.style and segment.style.bgcolor and
                                        segment.style.bgcolor.get_truecolor().hex.lower() == colors.code_background
                                        for segment in segments))
                    self.assertEqual(entry.body, markdown)
                self.assertEqual(backgrounds, {BACKGROUND})
                self.assertEqual(foregrounds, {FOREGROUND})
                self.assertEqual(len(markers), 3)
                self.assertFalse(app.sidebar_visible)
                self.assertEqual(app.query_one("#composer").region.height, 3)

                await pilot.resize_terminal(80, 26)
                await pilot.press("ctrl+n")
                await pilot.pause()
                self.assertTrue(await pilot.click("#confirm-add"))
                self.assertIn("中文或字母", str(app.screen.query_one("#dialog-error").render()))
                await pilot.press("escape")
            restored = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with restored.run_test(size=(100, 36)) as pilot:
                await restored.connection_task
                self.assertEqual({name: agent.color for name, agent in restored.agents.items()}, expected)
                for entry in restored.entries:
                    if entry.sender in restored.agents:
                        self.assertEqual(restored.cards[id(entry)].styles.background.hex.lower(),
                                         member_colors(expected[entry.sender]).background)

    async def test_old_duplicate_colors_are_reassigned_without_changing_threads(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            path = Path(tmp, "state.json")
            path.write_text(json.dumps({"version": 2, "selected": "a", "entries": [], "agents": [
                {"name": "a", "label": "a", "color": "#a9b6ff", "thread_id": "keep-a"},
                {"name": "b", "label": "b", "color": "#A9B6FF", "thread_id": "keep-b"},
            ]}))
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(80, 26)) as pilot:
                await app.connection_task
                self.assertEqual(app.agents["a"].color, "#a9b6ff")
                self.assertNotEqual(app.agents["a"].color, app.agents["b"].color)
                self.assertEqual(app.agents["a"].thread_id, "keep-a")
                self.assertEqual(app.agents["b"].thread_id, "keep-b")
            saved = json.loads(path.read_text())
            self.assertEqual(len({agent["color"] for agent in saved["agents"]}), 2)


if __name__ == "__main__":
    unittest.main()
