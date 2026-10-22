# /// script
# requires-python = ">=3.11"
# dependencies = ["pyte==0.8.2", "textual==8.2.8"]
# ///
"""PTY integration checks for the isolated Rust/Textual comparison.

Run after building Rust: uv run scripts/check_terminal_lab.py
Synthetic data only. Does not call Codex, send messages, or touch the clipboard.
"""
from __future__ import annotations

import codecs
import json
import os
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import pyte
from wcwidth import wcswidth

from terminal_lab import ROOT, fixture


if os.name == "posix":
    import fcntl
    import pty
    import termios

RUST = ROOT / "experiments/terminal-lab/target/release/codex-room-terminal-lab"


class Session:
    def __init__(self, command, *, columns=90, rows=28):
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.before = termios.tcgetattr(self.slave)
        self.output = bytearray()
        self.queries = 0
        self.screen = pyte.HistoryScreen(columns, rows, history=50000)
        self.stream = pyte.Stream(self.screen)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        environment = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
        self.process = subprocess.Popen(command, stdin=self.slave, stdout=self.slave, stderr=self.slave,
                                        cwd=ROOT, env=environment)

    @property
    def visible(self):
        return "\n".join(self.row_text(self.screen.buffer[y], self.screen.columns)
                         for y in range(self.screen.lines))

    @staticmethod
    def row_text(row, columns):
        # Pyte 0.8 can leave empty wide-cell stubs after cursor-positioned
        # updates; its display property assumes every stub follows a wide glyph.
        parts, column = [], 0
        while column < columns:
            cell = row.get(column)
            text = cell.data if cell is not None and cell.data else " "
            parts.append(text)
            column += max(1, wcswidth(text))
        return "".join(parts).rstrip()

    def read(self, duration=0.05):
        if select.select([self.master], [], [], duration)[0]:
            block = os.read(self.master, 65536)
            self.output.extend(block)
            self.stream.feed(self.decoder.decode(block))
            count = self.output.count(b"\x1b[6n")
            for _ in range(count - self.queries):
                self.send(b"\x1b[1;1R")
            self.queries = count

    def wait(self, predicate, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.read()
            if predicate():
                return
            if self.process.poll() is not None:
                raise AssertionError(f"process exited ({self.process.returncode}):\n{self.visible}")
        raise AssertionError(f"PTY wait timed out:\n{self.visible}")

    def send(self, data):
        os.write(self.master, data)

    def resize(self, columns, rows):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        self.screen.resize(rows, columns)
        self.process.send_signal(signal.SIGWINCH)

    def finish(self, *, send_quit=True):
        if send_quit:
            self.send(b"\x11")
        self.wait(lambda: self.process.poll() is not None, seconds=15)
        while select.select([self.master], [], [], 0.03)[0]:
            self.read(0)
        after = termios.tcgetattr(self.slave)
        before = self.before[:]
        transient = getattr(termios, "PENDIN", 0)
        after[3] &= ~transient
        before[3] &= ~transient
        if after != before:
            raise AssertionError("terminal input/echo modes leaked")
        self.send(b"ab\x7fc\n")
        if not select.select([self.slave], [], [], 2)[0] or os.read(self.slave, 32) != b"ac\n":
            raise AssertionError("shell backspace did not recover")
        return self.process.returncode

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        os.close(self.master)
        os.close(self.slave)


class FixtureTests(unittest.TestCase):
    def test_shared_source_has_exact_requested_size(self):
        for count in (1000, 10000):
            data = fixture(count)
            self.assertEqual(sum(len(message.split("\n")) for message in data["history"]), count)
            self.assertIn("```python", "\n".join(data["history"]))
            self.assertIn("**加粗文字**", "\n".join(data["history"]))


@unittest.skipUnless(os.name == "posix", "requires POSIX PTY")
class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="terminal-lab-check-")
        self.directory = Path(self.tmp.name)
        self.fixture = self.directory / "fixture.json"
        self.fixture.write_text(json.dumps(fixture(10000), ensure_ascii=False))
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.close()
        self.tmp.cleanup()

    def rust(self, mode, *extra):
        self.assertTrue(RUST.is_file(), "build the Rust experiment first")
        metrics = self.directory / f"{mode}.json"
        session = Session([str(RUST), "--mode", mode, "--fixture", str(self.fixture),
                           "--paused", "--metrics", str(metrics), *extra])
        self.sessions.append(session)
        return session, metrics

    def test_inline_preserves_10000_line_history_in_terminal_scrollback(self):
        session, metrics = self.rust("inline")
        session.wait(lambda: "Ctrl+Q" in session.visible)
        self.assertNotIn(b"\x1b[?1049h", session.output)
        self.assertNotIn(b"\x1b[?1000h", session.output)
        self.assertNotIn(b"\x1b[?1003h", session.output)
        self.assertNotIn(b"\x1b[3J", session.output)
        scrollback = "\n".join(session.row_text(row, session.screen.columns)
                              for row in session.screen.history.top)
        self.assertTrue("回合 0001" in scrollback, "oldest history was not retained")
        self.assertTrue("end-of-message" in scrollback.replace("\n", ""), "history text was damaged")
        self.assertGreater(len(session.screen.history.top), 9000)
        session.send("中文草稿".encode())
        session.wait(lambda: "中文草稿" in session.visible)
        session.send(b"\x1bOQ")  # F2: stream while keeping the draft.
        session.wait(lambda: "模拟 Codex · 流式输出" in session.visible)
        self.assertIn("中文草稿", session.visible)
        session.resize(72, 24)
        session.wait(lambda: "已重排当前输入区" in session.visible)
        self.assertEqual(session.finish(), 0)
        self.assertGreater(json.loads(metrics.read_text())["rendered_rows"], 9000)

    def test_fullscreen_drag_forward_resize_and_terminal_cleanup(self):
        session, metrics = self.rust("fullscreen")
        session.wait(lambda: "Ctrl+Q" in session.visible)
        self.assertIn(b"\x1b[?1049h", session.output)
        self.assertIn(b"\x1b[?1000h", session.output)
        session.send(b"\x1b[5~")  # PageUp.
        session.send(b"\x1b[<0;6;5M\x1b[<32;20;9M\x1b[<0;20;9m\x06")
        session.wait(lambda: "模拟转发预览" in session.visible)
        self.assertIn("仅预览，不发送", session.visible)
        session.send(b"\x1b")
        session.wait(lambda: "模拟转发预览" not in session.visible)
        session.send("草稿保留".encode())
        session.wait(lambda: "草稿保留" in session.visible)
        session.send(b"\x1bOQ")
        session.wait(lambda: "流式输出中" in session.visible)
        self.assertIn("草稿保留", session.visible)
        session.resize(74, 25)
        session.wait(lambda: "旧选区已清除" in session.visible)
        self.assertEqual(session.finish(), 0)
        self.assertIn(b"\x1b[?1049l", session.output)
        self.assertIn(b"\x1b[?1000l", session.output)
        self.assertGreater(json.loads(metrics.read_text())["rendered_rows"], 9000)

    def test_fullscreen_panic_also_restores_terminal(self):
        session, _ = self.rust("fullscreen", "--panic-check")
        session.wait(lambda: b"cleanup probe" in session.output)
        self.assertEqual(session.finish(send_quit=False), 101)
        self.assertIn(b"\x1b[?1049l", session.output)
        self.assertIn(b"\x1b[?1000l", session.output)

    def test_textual_control_and_mouse_disabled_use_the_same_real_renderer(self):
        for mode in ("textual", "textual-no-mouse"):
            with self.subTest(mode=mode):
                session = Session([sys.executable, str(ROOT / "scripts/terminal_lab.py"),
                                   mode, "--lines", "1000", "--paused"])
                self.sessions.append(session)
                session.wait(lambda: "F1" in session.visible)
                self.assertIn(b"\x1b[?1049h", session.output)
                if mode == "textual":
                    self.assertIn(b"\x1b[?1003h", session.output)
                else:
                    self.assertNotIn(b"\x1b[?1003h", session.output)
                    self.assertNotIn(b"\x1b[?1000h", session.output)
                session.send(b"\x1b[5~\x1b[6~")
                self.assertEqual(session.finish(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
