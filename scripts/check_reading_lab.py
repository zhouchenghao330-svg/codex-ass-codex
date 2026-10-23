# /// script
# requires-python = ">=3.11"
# dependencies = ["pyte==0.8.2", "textual==8.2.8"]
# ///
"""Independent reading-demo PTY checks; no Codex or real clipboard access."""
import os
from pathlib import Path
import tempfile
import unittest
from wcwidth import wcswidth

from check_terminal_lab import ROOT, Session

BINARY = ROOT / "experiments/terminal-lab/target/release/codex-room-reading-demo"
OUTPUT = ROOT / "artifacts/reading-demo"


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="reading-demo-check-")
        self.cwd = Path(self.tmp.name)
        self.previous = dict(os.environ)
        os.environ.pop("NO_COLOR", None)
        self.clipboard = self.cwd / "clipboard.txt"
        self.clipboard.write_text("")
        helper = self.cwd / "pbcopy"
        helper.write_text('#!/bin/sh\n/bin/cat > "$READING_DEMO_CLIPBOARD"\n')
        helper.chmod(0o755)
        os.environ.update(PATH=str(self.cwd)+os.pathsep+os.environ["PATH"], READING_DEMO_CLIPBOARD=str(self.clipboard))
        self.session = Session([str(BINARY)], columns=150, rows=42)
        self.session.wait(lambda:"段落＋行" in self.session.visible and "我们已经有一层" in self.session.visible)

    def tearDown(self):
        try:
            if self.session.process.poll() is None:
                self.assertEqual(self.session.finish(), 0)
        finally:
            self.session.close()
            os.environ.clear(); os.environ.update(self.previous)
            self.tmp.cleanup()

    def row(self, text):
        return next(y for y, line in enumerate(self.session.visible.splitlines()) if text in line)

    def move(self, x, y):
        self.session.send(f"\x1b[<35;{x+1};{y+1}M".encode())

    def wait_mode(self, text):
        self.session.wait(lambda:text in self.session.visible.splitlines()[-3])

    def test_modes_color_styles_and_strength(self):
        y = self.row("我们已经有一层")
        self.session.send(b"0"); self.wait_mode("原样")
        normal = self.session.screen.buffer[y][3].bg
        self.move(4, y)
        for mode, name in ((1,"行阅读尺"),(2,"段落聚焦"),(3,"段落＋行")):
            self.session.send(str(mode).encode()); self.wait_mode(name)
            if mode != 2:
                self.session.wait(lambda:self.session.screen.buffer[y][3].bg != normal)
            OUTPUT.mkdir(parents=True, exist_ok=True)
            (OUTPUT/f"mode-{mode}-screen.txt").write_text(self.session.visible)
        self.session.send(b"d")
        self.session.wait(lambda:"只加浅底色" in self.session.visible)
        self.session.send(b"d+")
        self.session.wait(lambda:"压暗 40%" in self.session.visible)

    def test_narrow_layout_and_resize(self):
        y = self.row("开发  @开发")
        before = self.session.visible.splitlines()[y].index("开发")
        self.session.send(b"4"); self.wait_mode("窄栏＋聚焦")
        self.session.wait(lambda:self.session.visible.splitlines()[self.row("开发  @开发")].index("开发") > before+10)
        (OUTPUT/"mode-4-screen.txt").write_text(self.session.visible)
        self.session.resize(80, 34)
        self.session.wait(lambda:"0–4 切方案" in self.session.visible)
        self.session.send(b"0"); self.wait_mode("原样")
        self.session.send(b"3"); self.wait_mode("段落＋行")

    def test_lock_scroll_and_stream_do_not_follow_tail(self):
        y = self.row("我们已经有一层")
        self.move(4,y)
        self.session.wait(lambda:self.session.screen.buffer[y][3].bg == "303831")
        self.session.send(b" ")
        self.session.wait(lambda:"已锁定" in self.session.visible)
        self.move(4, self.row("真正需要先弄清楚"))
        self.session.read(.1)
        self.assertEqual(self.session.screen.buffer[y][3].bg, "303831")
        before = "\n".join(self.session.visible.splitlines()[4:12])
        self.session.send(b"\x1b[12~")
        self.session.wait(lambda:"流式模拟中" in self.session.visible)
        self.session.read(.2)
        self.assertEqual("\n".join(self.session.visible.splitlines()[4:12]), before)
        self.session.send(b"\x1b[4~")
        self.session.wait(lambda:"新内容会追加在这里" in self.session.visible)
        self.session.send(b"\x1b[12~\x1b")
        self.session.wait(lambda:"鼠标跟随" in self.session.visible)

    def test_drag_selection_overrides_hover_and_command_copy_is_exact(self):
        self.session.send(b"\x1b[12~")
        self.session.wait(lambda:"流式模拟中" in self.session.visible)
        y = self.row("dedup")
        text = self.session.visible.splitlines()[y]
        x = wcswidth(text[:text.index("dedup")])
        self.session.send(f"\x1b[<0;{x+1};{y+1}M".encode())
        self.session.read(.03)
        self.session.send(f"\x1b[<32;{x+6};{y+1}M\x1b[<0;{x+6};{y+1}m".encode())
        self.session.wait(lambda:any(cell.bg=="415a74" for row in self.session.screen.buffer.values() for cell in row.values()))
        self.move(4,self.row("真正需要先弄清楚"))
        self.session.read(.1)
        self.session.send(b"\x1b[99;9u")
        self.session.wait(lambda:"已复制" in self.session.visible)
        self.assertEqual(self.clipboard.read_text(),"dedup")
        self.session.send(b"\x1b")
        self.session.wait(lambda:not any(cell.bg=="415a74" for row in self.session.screen.buffer.values() for cell in row.values()))


if __name__ == "__main__":
    OUTPUT.mkdir(parents=True, exist_ok=True)
    unittest.main(verbosity=2)
