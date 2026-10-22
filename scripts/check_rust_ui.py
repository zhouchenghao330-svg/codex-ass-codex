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

from check_terminal_lab import Session, ROOT

BINARY = ROOT/"terminal-ui/target/release/codex-room-ui"


class RustUITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.previous = {key: os.environ.get(key) for key in ("CODEX_ROOM_ROOT","CODEX_ROOM_PYTHON","CODEX_ROOM_CODEX_COMMAND","NO_COLOR")}
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
