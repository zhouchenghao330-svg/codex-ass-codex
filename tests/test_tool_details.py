import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from rich.cells import cell_len
from textual.widgets import Input

from codex_room.app import ComposerInput, CodexRoom
from codex_room.runtime import CodexClient
from codex_room.tool_details import file_changes
from codex_room.transcript import Entry, ReplyBlock, render_reply


ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


def rendered(entry, expanded):
    console = Console(width=132, color_system=None)
    with console.capture() as captured:
        console.print(render_reply(entry, expanded, 130), end="")
    return captured.get()


class FileChangeTests(unittest.TestCase):
    def test_diff_counts_exclude_headers_and_handle_literal_added_deleted_files(self):
        block = ReplyBlock("files", "file", json.dumps([
            {"path": "app.py", "kind": {"type": "update"}, "diff": "--- a/app.py\n+++ b/app.py\n@@ -1 +1,2 @@\n-old\n+new\n+more"},
            {"path": "added.py", "kind": {"type": "add"}, "diff": "first\nsecond\n"},
            {"path": "removed.py", "kind": {"type": "delete"}, "diff": "first\n"},
            {"path": "before.py", "kind": {"type": "update", "movePath": "after.py"}, "diff": ""},
        ]))
        changes = file_changes(block)
        self.assertEqual([(change.added, change.deleted) for change in changes], [(2, 1), (2, 0), (0, 1), (0, 0)])
        self.assertEqual((changes[-1].action, changes[-1].previous, changes[-1].path), ("rename", "before.py", "after.py"))
        old = ReplyBlock("legacy", "file", "raw legacy output", "app.py", "completed")
        self.assertEqual(file_changes(old), [])
        self.assertIn("raw legacy output", rendered(Entry("a", "", blocks=[old]), True))


class ToolDetailsUITests(unittest.IsolatedAsyncioTestCase):
    async def test_click_summary_individual_file_and_full_transcript_then_restore(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test(size=(132, 80)) as pilot:
                await app.connection_task
                await app.create_member("a")
                await app.submit("@a mixed-tools")
                await app.tasks["a"]
                answer = app.agents["a"].history[-1]
                card = app.cards[id(answer)]
                body = card.body
                composer = app.query_one("#message-input", ComposerInput)
                composer.value = "preserved draft"
                composer.cursor_position = 3
                await pilot.pause()

                async def click_line(needle):
                    lines = body.selection_lines()
                    y = next(i for i, line in enumerate(lines) if needle in line)
                    x = cell_len(lines[y][:lines[y].index(needle)]) + 1
                    await pilot.click(body, offset=(x, y))
                    await pilot.pause()

                collapsed = "\n".join(body.selection_lines())
                self.assertIn("1 命令 · 2 工具 · 4 文件", collapsed)
                self.assertIn("+4 -2", collapsed)
                self.assertNotIn("COMMAND_OUTPUT", collapsed)
                await click_line("点击展开")
                compact = "\n".join(body.selection_lines())
                self.assertIn("命令 · printf fixture", compact)
                self.assertIn("MCP · fixture/search", compact)
                self.assertIn("工具 · functions/exec_command", compact)
                self.assertIn("修改 · src/app.py  +2 -1", compact)
                self.assertIn("新增 · src/new.py  +2 -0", compact)
                self.assertIn("删除 · src/deleted.py  +0 -1", compact)
                self.assertIn("重命名 · src/old_name.py → src/renamed.py", compact)
                self.assertIn("lookup something", compact)
                self.assertIn("command preview", compact)
                self.assertIn("MCP preview", compact)
                self.assertNotIn("MCP_RESULT", compact)
                self.assertNotIn("COMMAND_OUTPUT", compact)
                self.assertNotIn("+new_value", compact)

                await click_line("修改 · src/app.py")
                expanded_file = "\n".join(body.selection_lines())
                self.assertIn("+new_value", expanded_file)
                self.assertIn("-old_value", expanded_file)
                self.assertNotIn("MCP_RESULT", expanded_file)
                self.assertNotIn("COMMAND_OUTPUT", expanded_file)
                body.text_select_all()
                await pilot.pause()
                await pilot.press("ctrl+c")
                self.assertIn("+new_value", app.clipboard)
                self.assertNotIn("MCP_RESULT", app.clipboard)
                await pilot.press("escape")
                await click_line("修改 · src/app.py")
                self.assertNotIn("+new_value", "\n".join(body.selection_lines()))
                await click_line("点击收起")
                self.assertNotIn("MCP preview", "\n".join(body.selection_lines()))
                self.assertNotIn("COMMAND_OUTPUT", "\n".join(body.selection_lines()))
                await click_line("点击展开")
                await click_line("全部详情")
                full = "\n".join(body.selection_lines())
                self.assertIn("MCP_RESULT", full)
                self.assertIn("DYNAMIC_OUTPUT", full)
                self.assertIn("COMMAND_OUTPUT **literal**", full)
                self.assertIn("[退出码 0]", full)
                self.assertIn("+new_value", full)
                self.assertIn("new file", full)
                self.assertIn("old file", full)
                self.assertEqual(composer.value, "preserved draft")
                self.assertEqual(composer.cursor_position, 3)
                await click_line("MCP · fixture/search")
                self.assertNotIn("MCP_RESULT", "\n".join(body.selection_lines()))
                self.assertIn("+new_value", "\n".join(body.selection_lines()))
                await click_line("MCP · fixture/search")
                self.assertIn("MCP_RESULT", "\n".join(body.selection_lines()))
                await click_line("点击收起")
                self.assertNotIn("COMMAND_OUTPUT", "\n".join(body.selection_lines()))
                await pilot.press("ctrl+t")
                await pilot.pause()
                self.assertIn("MCP_RESULT", "\n".join(body.selection_lines()))
                await pilot.press("ctrl+t")
                await pilot.resize_terminal(86, 26)
                await pilot.pause()
                self.assertNotIn("COMMAND_OUTPUT", "\n".join(body.selection_lines()))

            restored = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with restored.run_test() as pilot:
                await restored.connection_task
                entry = next(entry for entry in restored.entries if any(block.kind == "file" for block in entry.blocks))
                self.assertIn("+4 -2", rendered(entry, False))
                self.assertNotIn("new_value", rendered(entry, False))
                self.assertIn("new_value", rendered(entry, True))
                self.assertIn("MCP_RESULT", rendered(entry, True))
