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

    def command(self,text,finish=None):
        self.session.send(text.encode())
        self.session.wait(lambda:text in "\n".join(self.session.visible.splitlines()[-4:]))
        self.session.send(b"\r")
        for _ in range(5):
            self.session.read(.02)
        if text in "\n".join(self.session.visible.splitlines()[-3:]):
            self.session.send(b"\r") # Enter first accepted an exact completion.
        self.session.wait(lambda:text not in "\n".join(self.session.visible.splitlines()[-3:]) and "正在确认发送" not in self.session.visible)
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
        self.assertEqual(len([e for e in state["entries"] if e["sender"] == "you"]), 1)
        self.session.close(); self.session = None
        self.start(columns=125, rows=42)
        self.session.wait(lambda:"@B → @A" in self.session.visible and "PUBLIC_ANSWER" in self.session.visible)
        self.finish()
        restored = json.loads((self.cwd/".codex-room/state.json").read_text())
        self.assertEqual({e["id"] for e in restored["entries"] if (e.get("communication") or {}).get("transport") == "room"},
                         {e["id"] for e in public})

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
        self.command("settings",finish="sandboxPolicy")
        self.finish()
        self.assertEqual(json.loads((self.cwd/".codex-room/state.json").read_text())["agents"][0]["thread_id"],tid)

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
