# /// script
# requires-python = ">=3.11"
# dependencies = ["pyte==0.8.2", "textual==8.2.8"]
# ///
"""Real Rust frontend + local Codex protocol peer, in an isolated PTY.

No model calls, no real room state, and no clipboard mutations.
Build first: cargo build --release --locked --manifest-path terminal-ui/Cargo.toml
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from wcwidth import wcswidth

from check_terminal_lab import Session, ROOT

BINARY = ROOT/"terminal-ui/target/release/codex-room-ui"


class RustUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.previous = {key: os.environ.get(key) for key in ("CODEX_ROOM_ROOT","CODEX_ROOM_PYTHON","CODEX_ROOM_CODEX_COMMAND","NO_COLOR","PATH","CODEX_ROOM_COPY_CAPTURE")}
        os.environ.pop("NO_COLOR",None)
        os.environ.update(CODEX_ROOM_ROOT=str(ROOT),CODEX_ROOM_PYTHON=str(ROOT/".venv/bin/python"),
                          CODEX_ROOM_CODEX_COMMAND=json.dumps([str(ROOT/".venv/bin/python"),str(ROOT/"tests/fake_codex.py")]))
        self.session = None

    def tearDown(self):
        if self.session:
            self.session.close()
        for key,value in self.previous.items():
            if value is None:
                os.environ.pop(key,None)
            else:
                os.environ[key]=value
        self.tmp.cleanup()

    def start(self, **kwargs):
        self.session = Session([str(BINARY),"--cwd",str(self.cwd)],**kwargs)
        self.session.wait(lambda:"已连接" in self.session.visible)

    def command(self,text,finish=None,setup=True):
        self.session.send(text.encode())
        self.session.wait(lambda:text in "\n".join(self.session.visible.splitlines()[-4:]))
        self.session.send(b"\r")
        for _ in range(5):
            self.session.read(.02)
        if text in "\n".join(self.session.visible.splitlines()[-3:]):
            self.session.send(b"\r") # Enter first accepted an exact completion.
        self.session.wait(lambda:text not in "\n".join(self.session.visible.splitlines()[-3:]) and "正在确认发送" not in self.session.visible)
        if setup and text.startswith("/add "):
            # The completion-accepting Enter above may already have confirmed
            # step 1 if the backend created the member before the next redraw.
            self.session.wait(lambda:"选择模型 ·" in self.session.visible or "选择思考强度 ·" in self.session.visible)
            if "选择模型 ·" in self.session.visible:
                self.session.send(b"\r")
            self.session.wait(lambda:"选择思考强度 ·" in self.session.visible)
            self.session.send(b"\r")
            self.session.wait(lambda:"已设置 ·" in self.session.visible and "选择思考强度 ·" not in self.session.visible)
        if finish:
            self.session.wait(lambda:finish in self.session.visible)

    def paste(self,text):
        self.session.send(b"\x1b[200~"+text.encode()+b"\x1b[201~")

    def click(self,x,y):
        self.session.send(f"\x1b[<0;{x+1};{y+1}M\x1b[<0;{x+1};{y+1}m".encode())

    def row(self,text):
        return next(i for i,line in enumerate(self.session.visible.splitlines()) if text in line)

    def finish(self):
        self.assertEqual(self.session.finish(),0)
        self.assert_keyboard_restored()

    def assert_keyboard_restored(self):
        output = self.session.output
        self.assertEqual(output.count(b"\x1b[>1u"), 1)
        self.assertEqual(output.count(b"\x1b[<1u"), 1)
        self.assertLess(output.index(b"\x1b[<1u"), output.index(b"\x1b[?1049l"))
        self.assertNotIn(b"\x1b[>9u", output)  # Do not encode every ordinary key.

    def test_macos_command_copy_replaces_stale_text_and_button_copies_current_selection(self):
        # Capture helper stdin instead of touching the user's actual clipboard.
        helpers = self.cwd/"copy-helpers"
        helpers.mkdir()
        captured = self.cwd/"copied.txt"
        captured.write_text("Room 公开消息工具")
        for name in ("pbcopy", "wl-copy", "xclip", "clip.exe"):
            helper = helpers/name
            helper.write_text('#!/bin/sh\n/bin/cat > "$CODEX_ROOM_COPY_CAPTURE"\n')
            helper.chmod(0o755)
        os.environ["PATH"] = str(helpers) + os.pathsep + os.environ["PATH"]
        os.environ["CODEX_ROOM_COPY_CAPTURE"] = str(captured)
        directory = self.cwd/".codex-room"
        directory.mkdir()
        first, second = "FIRST_中文_Alpha", "SECOND_中文_Beta"
        entries = [{"id":str(n), "sender":"A", "body":text, "state":"done"}
                   for n, text in enumerate(("Room 公开消息工具", first, second))]
        (directory/"state.json").write_text(json.dumps({"version":2,"agents":[{
            "name":"A","label":"A","color":"#afb4ff","cwd":str(self.cwd)}],
            "entries":entries,"selected":"A","show_all":False}))
        self.start(columns=110, rows=34)
        self.session.wait(lambda:second in self.session.visible)

        def select(text):
            row = self.row(text)
            line = self.session.visible.splitlines()[row]
            start = wcswidth(line[:line.index(text)])
            end = start + wcswidth(text)
            self.session.send(f"\x1b[<0;{start+1};{row+1}M\x1b[<32;{end+1};{row+1}M\x1b[<0;{end+1};{row+1}m".encode())
            self.session.read(.04)

        def copied(text):
            self.session.wait(lambda:captured.read_text() == text)

        select(first)
        self.session.send(b"\x1b[99;9u")  # Cmd+C, emitted by a Kitty-compatible macOS terminal.
        copied(first)
        select(second)
        self.session.send(b"\x1b[99;9u")
        copied(second)
        select("中文")
        self.click(67, 33)  # Footer Copy must not dismiss the text selection.
        copied("中文")
        select(first)
        self.session.send(b"\x03")  # Existing Ctrl+C continues to copy.
        copied(first)
        select(second)
        self.session.send(b"\x1b[67;6u")  # Ctrl+Shift+C.
        copied(second)
        self.session.send(b"\x1b")
        self.session.read(.05)
        self.session.send(b"\x1b[99;9u")
        self.session.wait(lambda:"先拖选" in self.session.visible)
        self.assertIsNone(self.session.process.poll())
        self.assertEqual(captured.read_text(), second)
        self.session.send(b"\x03")  # Only plain Ctrl+C with no selection exits.
        self.assertEqual(self.session.finish(send_quit=False), 0)
        self.assert_keyboard_restored()

    def test_add_model_and_effort_picker_mouse_keyboard_cancel_and_restore(self):
        self.start(columns=110, rows=34)
        self.command("/add 配置测试",setup=False)
        self.session.wait(lambda:"选择模型 · @配置测试 · 1/2" in self.session.visible)
        self.assertIn("Esc 保留原配置",self.session.visible)
        self.session.send(b"\r")
        self.session.wait(lambda:"选择思考强度 · @配置测试 · 2/2" in self.session.visible and "› high" in self.session.visible)
        # The inherited high effort is selected, even though the model default is low.
        self.assertTrue(any("› high" in row for row in self.session.visible.splitlines()))
        self.session.send(b"\x1b[A\r")
        self.session.wait(lambda:"已设置 · test-model · low" in self.session.visible)
        self.command("/model",setup=False)
        self.session.wait(lambda:"选择模型 ·" in self.session.visible and "Test Model" in self.session.visible)
        self.click(15,self.row("Test Model"))
        self.session.wait(lambda:"选择思考强度 ·" in self.session.visible and " high" in self.session.visible)
        self.click(15,self.row(" high"))
        self.session.wait(lambda:"已设置 · test-model · high" in self.session.visible)
        self.command("/add 保留默认",setup=False)
        self.session.wait(lambda:"选择模型 · @保留默认" in self.session.visible)
        self.session.send(b"\x1b")
        self.session.wait(lambda:"选择模型 ·" not in self.session.visible)
        self.command("/focus 配置测试")
        self.command("/model",setup=False)
        self.session.wait(lambda:"选择模型 ·" in self.session.visible)
        self.session.send(b"\r")
        self.session.wait(lambda:"选择思考强度 ·" in self.session.visible)
        self.session.send(b"\x1b[A\x1b")  # Highlight low, then cancel: keep high.
        self.session.wait(lambda:"选择思考强度 ·" not in self.session.visible)
        self.finish()
        state=json.loads((self.cwd/".codex-room/state.json").read_text())
        for member in state["agents"]:
            self.assertEqual((member["model"],member["effort"]),("test-model","high"))
            self.assertIsNone(member["thread_id"])
        self.assertEqual(state["entries"],[])
        self.session.close();self.session=None
        self.start(columns=110,rows=34)
        self.command("/model",setup=False)
        self.session.wait(lambda:"选择模型 ·" in self.session.visible)
        self.session.send(b"\r")
        self.session.wait(lambda:"› high" in self.session.visible)
        self.session.send(b"\x1b")
        self.finish()

    def test_centered_reading_toggle_copy_fold_scroll_and_restart(self):
        helpers = self.cwd/"copy-helpers"
        helpers.mkdir()
        captured = self.cwd/"copied.txt"
        captured.write_text("")
        for name in ("pbcopy", "wl-copy", "xclip", "clip.exe"):
            helper = helpers/name
            helper.write_text('#!/bin/sh\n/bin/cat > "$CODEX_ROOM_COPY_CAPTURE"\n')
            helper.chmod(0o755)
        os.environ["PATH"] = str(helpers) + os.pathsep + os.environ["PATH"]
        os.environ["CODEX_ROOM_COPY_CAPTURE"] = str(captured)
        directory = self.cwd/".codex-room"
        directory.mkdir()
        entries = [{"id":str(n), "sender":"A", "body":f"HISTORY_{n:02d}\n\n" + "阅读位置保持测试。"*20,
                    "state":"done"} for n in range(25)]
        entries.append({"id":"latest", "sender":"A", "body":"CENTER_SELECT\n\nFOLD_DETAIL", "state":"done"})
        (directory/"state.json").write_text(json.dumps({"version":2,"agents":[{
            "name":"A","label":"A","color":"#afb4ff","cwd":str(self.cwd)}],
            "entries":entries,"selected":"A","show_all":False}))
        self.start(columns=150, rows=40)
        self.session.wait(lambda:"CENTER_SELECT" in self.session.visible)

        def position(text):
            y = self.row(text)
            line = self.session.visible.splitlines()[y]
            return wcswidth(line[:line.index(text)]), y

        original_x, _ = position("CENTER_SELECT")
        self.command("/adhd", finish="居中阅读已开启")
        x, y = position("CENTER_SELECT")
        self.assertEqual(x-original_x, 27)
        end = x + len("CENTER_SELECT")
        self.session.send(f"\x1b[<0;{x+1};{y+1}M\x1b[<32;{end+1};{y+1}M\x1b[<0;{end+1};{y+1}m\x1b[99;9u".encode())
        self.session.wait(lambda:captured.read_text() == "CENTER_SELECT")
        self.click(1, y)  # Centered margin clears the selection, not the card.
        header_y = next(i for i,line in enumerate(self.session.visible.splitlines()) if "▾ A" in line and i < y and i > y-4)
        self.click(x, header_y)
        self.session.wait(lambda:"FOLD_DETAIL" not in self.session.visible)
        collapsed_y = self.row("▸ A")
        self.click(x, collapsed_y)
        self.session.wait(lambda:"▾ A" in self.session.visible.splitlines()[collapsed_y])
        self.session.send(b"\x1b[1;5F")  # Expanded tail can extend below the current viewport.
        self.session.wait(lambda:"FOLD_DETAIL" in self.session.visible)
        self.session.send(b"\x1b[<64;70;12M" * 50)
        self.session.wait(lambda:"CENTER_SELECT" not in self.session.visible)
        self.command("/adhd off", finish="居中阅读已关闭")
        self.assertNotIn("CENTER_SELECT", self.session.visible)
        self.command("/adhd on", finish="居中阅读已开启")
        self.assertNotIn("CENTER_SELECT", self.session.visible)
        self.finish()
        saved = json.loads((directory/"state.json").read_text())
        self.assertTrue(saved["reading_centered"])
        self.assertEqual(len(saved["entries"]), len(entries))
        self.assertIsNone(saved["agents"][0]["thread_id"])
        self.session.close(); self.session = None
        self.start(columns=150, rows=40)
        self.session.wait(lambda:"CENTER_SELECT" in self.session.visible)
        self.assertEqual(position("CENTER_SELECT")[0], x)
        self.command("/adhd off", finish="居中阅读已关闭")
        self.assertEqual(position("CENTER_SELECT")[0], original_x)
        self.finish()
        self.assertFalse(json.loads((directory/"state.json").read_text())["reading_centered"])

    def test_markdown_table_columns_reflow_with_sidebar_and_terminal_width(self):
        source = (ROOT/"tests/fixtures/markdown_table.md").read_text()
        directory = self.cwd/".codex-room"
        directory.mkdir()
        (directory/"state.json").write_text(json.dumps({"version":2,"agents":[{
            "name":"A","label":"A","color":"#a9b6ff","cwd":str(self.cwd)}],"selected":"A","show_all":False,
            "entries":[{"id":"table","sender":"A","body":source,"state":"done",
                        "blocks":[{"id":"text","kind":"text","text":source}]}]}))
        self.start(columns=110, rows=60)

        def assert_table(columns, sidebar):
            left = (24 if sidebar else 0) + 2
            right = columns - 2

            def table_rows():
                return [row for row in self.session.screen.buffer.values()
                        if row.get(left) and row[left].data in ("┌", "│", "├", "└")]

            self.session.wait(lambda:any(row[left].data == "└" and row.get(right)
                                         and row[right].data == "┘" for row in table_rows()))
            rows = table_rows()
            self.assertGreater(len(rows), 6)
            borders = "│┌┬┐├┼┤└┴┘"
            positions = lambda row: [x for x in range(left, columns-1)
                                     if row.get(x) and row[x].data and row[x].data in borders]
            expected = positions(next(row for row in rows if row[left].data == "┌"))
            self.assertEqual(len(expected), 4)
            self.assertEqual(expected[0], left)
            self.assertEqual(expected[-1], right)
            for row in rows:
                self.assertEqual(positions(row), expected)

        assert_table(110, False)
        self.click(1, 59)
        assert_table(110, True)
        self.session.resize(80, 60)
        assert_table(80, True)
        self.click(25, 59)
        assert_table(80, False)
        self.session.resize(160, 60)
        assert_table(160, False)
        self.finish()
        restored = json.loads((directory/"state.json").read_text())
        table = next(e for e in restored["entries"] if e["id"] == "table")
        self.assertEqual(table["body"], source)
        self.assertEqual(table["blocks"][0]["text"], source)

    def test_footer_member_switch_reaches_latest_after_scrolling_and_reselect(self):
        agents = [{"name": name, "label": name, "color": "#78d8bc", "cwd": str(self.cwd)} for name in ("A", "B", "C")]
        entries = [{"id": f"{name}-{n}", "sender": name, "body": f"{name} history {n}\n" * 3,
            "target": "", "state": "done"} for name in ("A", "B", "C") for n in range(60)]
        for name in ("A", "B", "C"):
            index = next(i for i in reversed(range(len(entries))) if entries[i]["sender"] == name)
            entries[index]["body"] += "LATEST_" + name
        (self.cwd/".codex-room").mkdir()
        (self.cwd/".codex-room/state.json").write_text(json.dumps({"version":2,"agents":agents,"entries":entries,"selected":"A","show_all":True}))
        self.start(columns=115, rows=32)
        self.session.wait(lambda:"LATEST_C" in self.session.visible)
        self.session.send(b"\x1b[<64;90;12M" * 24)
        self.session.wait(lambda:"LATEST_C" not in self.session.visible)
        self.click(1,31)
        self.session.wait(lambda:"成员 · 3" in self.session.visible)
        self.click(5,11)  # Last visible member C, via the sidebar.
        self.session.wait(lambda:"LATEST_C" in self.session.visible and "@C" in self.session.visible)
        self.session.send(b"\x1b[<64;90;12M" * 24)
        self.session.wait(lambda:"LATEST_C" not in self.session.visible)
        self.click(5,11)  # Selecting the already active member must also go down.
        self.session.wait(lambda:"LATEST_C" in self.session.visible)
        self.click(5,3)
        self.session.wait(lambda:"LATEST_A" in self.session.visible and "LATEST_C" not in self.session.visible)
        self.click(25,31)  # Close sidebar and reflow the conversation.
        self.session.wait(lambda:"成员 · 3" not in self.session.visible)
        self.assertIn("LATEST_A", self.session.visible)
        self.finish()

    def test_peer_communication_is_visible_with_original_markdown_or_missing_notice(self):
        os.environ["CODEX_ROOM_CODEX_COMMAND"] = json.dumps([str(ROOT/".venv/bin/python"), str(ROOT/"tests/fake_native_codex.py")])
        self.start(columns=125, rows=42)
        self.command("/add A", finish="@A")
        self.command("spawn-plaintext", finish="PEER_ORIGINAL_TEXT")
        self.session.wait(lambda:"@A → @buddy" in self.session.visible)
        self.assertNotIn("**PEER_ORIGINAL_TEXT**", self.session.visible)
        self.assertIn("Codex 未提供这条通信的正文", self.session.visible)
        self.command("/focus buddy", finish="PEER_ORIGINAL_TEXT")
        self.session.wait(lambda:"CHILD:ORIGINAL_CONTEXT" in self.session.visible)
        self.finish()
        state = json.loads((self.cwd/".codex-room/state.json").read_text())
        messages = [e for e in state["entries"] if e.get("communication")]
        self.assertEqual(len(messages), 2)
        self.session.close(); self.session = None
        self.start(columns=125, rows=42)
        self.session.wait(lambda:"PEER_ORIGINAL_TEXT" in self.session.visible)
        self.finish()
        state = json.loads((self.cwd/".codex-room/state.json").read_text())
        restored = [e for e in state["entries"] if e.get("communication")]
        self.assertEqual({e["id"] for e in restored}, {e["id"] for e in messages})
        self.assertEqual(len(restored), 2)

    def test_independent_public_messages_are_markdown_foldable_and_restored(self):
        self.start(columns=125, rows=42)
        self.command("/add A", finish="@A")
        self.command("/add B", finish="@B")
        self.command("@A peer-send B", finish="PUBLIC_ANSWER")
        self.session.wait(lambda:"@A → @B" in self.session.visible and "@B → @A" in self.session.visible)
        self.assertIn("PUBLIC_REQUEST", self.session.visible)
        self.assertNotIn("**PUBLIC_REQUEST**", self.session.visible)
        self.assertNotIn("**PUBLIC_ANSWER**", self.session.visible)
        self.assertNotIn("Codex 未提供这条通信的正文", self.session.visible)
        self.assertIn("已送达", self.session.visible)
        self.assertIn("Queue", self.session.visible)
        row = self.row("@A → @B")
        self.click(4, row)
        self.session.wait(lambda:"请给出你的建议" not in self.session.visible)
        self.assertNotIn("**PUBLIC_REQUEST**", self.session.visible)
        self.click(4, self.row("@A → @B"))
        self.session.wait(lambda:"请给出你的建议" in self.session.visible)
        self.command("/focus B", finish="PUBLIC_REQUEST")
        self.session.wait(lambda:"@B → @A" in self.session.visible)
        self.command("/focus all", finish="PUBLIC_ANSWER")
        self.finish()
        state = json.loads((self.cwd/".codex-room/state.json").read_text())
        public = [e for e in state["entries"] if (e.get("communication") or {}).get("transport") == "room"]
        self.assertEqual([(e["sender"],e["target"]) for e in public], [("A","B"),("B","A")])
        self.assertEqual(public[1]["communication"]["reply_to"], public[0]["id"])
        self.assertEqual(public[0]["communication"]["delivery_mode"], "queue")
        self.assertEqual(len([e for e in state["entries"] if e["sender"] == "you"]), 1)
        self.session.close(); self.session = None
        self.start(columns=125, rows=42)
        self.session.wait(lambda:"@B → @A" in self.session.visible and "PUBLIC_ANSWER" in self.session.visible)
        self.finish()
        restored = json.loads((self.cwd/".codex-room/state.json").read_text())
        self.assertEqual({e["id"] for e in restored["entries"] if (e.get("communication") or {}).get("transport") == "room"},
                         {e["id"] for e in public})

    def test_member_discovery_shows_recent_user_question_without_sending_peer_messages(self):
        self.start(columns=125, rows=42)
        self.command("/add A", finish="@A")
        self.command("/add B", finish="@B")
        self.command("@B DISCOVERY_CONTEXT", finish="DISCOVERY_CONTEXT")
        self.command("@A list-members B", finish="MEMBER_DISCOVERY:")
        self.session.wait(lambda:"recent_dialogue" in self.session.visible)
        self.finish()
        state = json.loads((self.cwd/".codex-room/state.json").read_text())
        self.assertFalse(any((e.get("communication") or {}).get("transport") == "room" for e in state["entries"]))
        answer = next(e for e in reversed(state["entries"]) if e["sender"] == "A")
        result = json.loads(answer["body"].split("MEMBER_DISCOVERY:", 1)[1])
        self.assertEqual(result["members"][0]["recent_dialogue"][0]["user"]["text"], "DISCOVERY_CONTEXT")

    def test_native_child_original_output_multiround_and_restart(self):
        os.environ["CODEX_ROOM_CODEX_COMMAND"] = json.dumps([str(ROOT/".venv/bin/python"), str(ROOT/"tests/fake_native_codex.py")])
        self.start(columns=120, rows=36)
        self.command("/add A", finish="@A")
        self.command("spawn")
        self.session.wait(lambda:"buddy" in self.session.visible)
        self.command("/focus buddy", finish="CHILD:ORIGINAL_CONTEXT")
        self.session.wait(lambda:"Worked for" in self.session.visible)
        self.command("remember PTY_NATIVE_CONTEXT", finish="CHILD:remember PTY_NATIVE_CONTEXT")
        self.command("recall", finish="CHILD:PTY_NATIVE_CONTEXT")
        self.command("/focus all")
        self.assertNotIn("这是 Codex Room 用户", self.session.visible)
        self.session.send(b"\x02")
        self.session.wait(lambda:"子代理 · @A" in self.session.visible)
        self.finish()
        state = json.loads((self.cwd/".codex-room/state.json").read_text())
        child = next(a for a in state["agents"] if a.get("parent_name"))
        keys = [(e["native_thread_id"], e["server_turn_id"]) for e in state["entries"] if e.get("native_thread_id")]
        self.assertEqual(len(keys), len(set(keys)))
        self.session.close(); self.session = None
        self.start(columns=120, rows=36)
        self.command("/focus buddy", finish="CHILD:PTY_NATIVE_CONTEXT")
        self.command("recall")
        self.session.wait(lambda:self.session.visible.count("CHILD:PTY_NATIVE_CONTEXT") >= 2)
        self.finish()
        restored = json.loads((self.cwd/".codex-room/state.json").read_text())
        self.assertEqual(next(a for a in restored["agents"] if a.get("parent_name"))["thread_id"], child["thread_id"])

    def test_real_stream_tools_input_history_and_restore(self):
        self.start(columns=110,rows=34)
        self.command("/add 开发",finish="@开发")
        self.command("mixed-tools",finish="Worked for")
        self.assertIn("2 工具",self.session.visible)
        self.assertIn("4 文件",self.session.visible)
        self.assertNotIn("COMMAND_OUTPUT",self.session.visible)
        y=self.row("命令")
        self.click(8,y)
        self.session.wait(lambda:"全部详情" in self.session.visible)
        self.assertIn("src/app.py",self.session.visible)
        self.click(8,self.row("全部详情"))
        self.session.wait(lambda:"COMMAND_OUTPUT" in self.session.visible)
        self.session.send(b"\x1b[1;5F") # Ctrl+End
        self.session.send(b"\x1b[A") # previous submitted input
        self.session.wait(lambda:"mixed-tools" in self.session.visible.splitlines()[-3])
        self.session.send(b"\x15") # clear composer
        self.paste("中"*6000)
        self.session.wait(lambda:"Pasted Content 6000 chars" in self.session.visible)
        self.session.send(b"\x7f")
        self.session.wait(lambda:"Pasted Content" not in self.session.visible)
        self.command("remember KEEP_THREAD",finish="KEEP_THREAD")
        self.session.wait(lambda:"Worked for" in self.session.visible)
        self.finish()
        state=json.loads((self.cwd/".codex-room/state.json").read_text())
        tid=state["agents"][0]["thread_id"]
        self.assertTrue(tid)
        self.assertTrue(any("KEEP_THREAD" in e["body"] for e in state["entries"]))
        self.session.close();self.session=None
        self.start(columns=110,rows=34)
        self.assertIn("KEEP_THREAD",self.session.visible)
        # The added serviceTier field can put 'sandboxPolicy' across a visual
        # wrap. Wait for a visible value, then verify the original JSON below.
        self.command("settings",finish="dangerFullAccess")
        self.finish()
        restored = json.loads((self.cwd/".codex-room/state.json").read_text())
        self.assertEqual(restored["agents"][0]["thread_id"],tid)
        settings = json.loads(next(e["body"] for e in reversed(restored["entries"]) if e["sender"]=="开发").split(": ",1)[1])
        self.assertEqual(settings["sandboxPolicy"]["type"],"dangerFullAccess")

    def test_selection_filter_forward_return_latest_and_resize(self):
        self.start(columns=110,rows=34)
        self.command("/add A",finish="@A")
        self.command("remember SOURCE",finish="SOURCE")
        self.session.wait(lambda:"Worked for" in self.session.visible)
        self.command("/add B",finish="@B")
        self.command("/focus A",finish="@A")
        y=self.row("thread-1: remember SOURCE")
        self.session.send(f"\x1b[<0;4;{y+1}M\x1b[<32;100;{y+1}M\x1b[<0;100;{y+1}m".encode())
        self.session.send(b"\x06")
        self.session.wait(lambda:"转发给" in self.session.visible)
        self.session.send(b"B\r")
        self.session.wait(lambda:"引用：" in self.session.visible)
        self.command("写成笔记")
        self.session.wait(lambda:"Worked for" in self.session.visible)
        self.click(31,33) # return source
        self.session.wait(lambda:"thread-1: remember SOURCE" in self.session.visible)
        self.assertNotIn("写成笔记",self.session.visible)
        self.command("/focus all",finish="全部消息")
        self.session.resize(72,24)
        self.session.wait(lambda:"Rust" in self.session.visible)
        self.finish()
        state=json.loads((self.cwd/".codex-room/state.json").read_text())
        quoted=[e for e in state["entries"] if e.get("quote")]
        self.assertEqual(len(quoted),1)
        self.assertEqual(quoted[0]["target"],"B")
        self.assertIn("SOURCE",json.dumps(quoted[0]["quote"]))

    def test_forward_new_recipient_retains_source_draft_and_sidebar_button(self):
        self.start(columns=110,rows=34)
        self.command("/add A",finish="@A")
        self.command("remember SOURCE_DRAFT",finish="SOURCE_DRAFT")
        self.session.wait(lambda:"Worked for" in self.session.visible)
        self.click(2,33)
        self.session.wait(lambda:"成员 · 1" in self.session.visible)
        self.click(25,33) # member button now starts after the open sidebar
        self.session.wait(lambda:"成员 · 1" not in self.session.visible)
        self.session.send("留在来源的草稿".encode())
        self.session.wait(lambda:"留在来源的草稿" in self.session.visible.splitlines()[-3])
        y=self.row("thread-1: remember SOURCE_DRAFT")
        self.session.send(f"\x1b[<0;3;{y+1}M\x1b[<32;100;{y+1}M\x1b[<0;100;{y+1}m\x06".encode())
        self.session.wait(lambda:"转发给" in self.session.visible)
        self.session.send("新建\r".encode())
        self.session.wait(lambda:"/add " in self.session.visible.splitlines()[-3])
        self.session.send(b"\x15")
        self.command("/add 新笔记",finish="引用：")
        self.command("写成笔记")
        self.session.wait(lambda:"Worked for" in self.session.visible)
        self.click(31,33)
        self.session.wait(lambda:"留在来源的草稿" in self.session.visible.splitlines()[-3])
        self.assertIn("thread-1: remember SOURCE_DRAFT",self.session.visible)
        self.finish()
        state=json.loads((self.cwd/".codex-room/state.json").read_text())
        quoted=[e for e in state["entries"] if e.get("quote")]
        self.assertEqual(len(quoted),1)
        self.assertEqual(quoted[0]["target"],"新笔记")
        self.assertEqual(state["drafts"]["A"]["prompt"],"留在来源的草稿")

    def test_text_selection_preserves_live_working_phase_and_timer(self):
        self.start(columns=110,rows=34)
        self.command("/add A")
        self.command("thinking-slow")
        self.session.wait(lambda:"Working · 正在思考：检查项目结构 ·" in self.session.visible)
        def clock():
            line=next((line for line in self.session.visible.splitlines() if "Working · 正在思考：检查项目结构 ·" in line),"")
            return line.rsplit("·",1)[-1].strip() if line else ""
        before=clock()
        row=self.row("thinking-slow")
        line=self.session.visible.splitlines()[row]
        x=wcswidth(line[:line.index("thinking-slow")])
        self.session.send(f"\x1b[<0;{x+1};{row+1}M\x1b[<32;{x+9};{row+1}M\x1b[<0;{x+9};{row+1}m".encode())
        self.session.wait(lambda:any(cell.bg=="415a74" for cells in self.session.screen.buffer.values() for cell in cells.values()))
        self.session.wait(lambda:bool(clock()) and clock()!=before)
        self.assertTrue(any(cell.bg=="415a74" for cells in self.session.screen.buffer.values() for cell in cells.values()))
        self.session.send(b"\x1b")
        self.session.wait(lambda:not any(cell.bg=="415a74" for cells in self.session.screen.buffer.values() for cell in cells.values()))
        self.assertIn("Working · 正在思考：检查项目结构 ·",self.session.visible)
        self.finish()

    def test_large_history_drag_keeps_anchor_and_background_stream(self):
        room=self.cwd/".codex-room";room.mkdir()
        entries=[{"id":f"m{i}","sender":"A","body":f"ROW_{i:05d} "+"中文选择"*8,"state":"done"} for i in range(10000)]
        (room/"state.json").write_text(json.dumps({"version":2,"selected":"A","show_all":False,
            "agents":[{"name":"A","label":"A","color":"#a9b6ff","cwd":str(self.cwd)}],"entries":entries}))
        self.start(columns=110,rows=34)
        self.session.wait(lambda:"ROW_09999" in self.session.visible)
        self.command("continuous-stream",finish="stream line")
        self.session.send(b"\x1b[5~")
        self.session.wait(lambda:"ROW_09999" not in self.session.visible)
        self.session.send(b"\x1b[<0;4;5M")
        started=time.monotonic()
        for y in range(6,27):
            self.session.send(f"\x1b[<32;28;{y}M".encode())
            self.session.read(.001)
        self.session.send(b"\x1b[<32;28;28M")
        for _ in range(15):
            self.session.read(.03)
        self.session.send(b"\x1b[<0;28;28m\x06")
        self.session.wait(lambda:"转发给" in self.session.visible,seconds=3)
        self.assertLess(time.monotonic()-started,3)
        self.session.send(b"\x1b")
        self.session.resize(80,25)
        self.session.wait(lambda:"Rust" in self.session.visible)
        self.finish()


if __name__=="__main__":
    unittest.main(verbosity=2)
