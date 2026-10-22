# /// script
# requires-python = ">=3.11"
# dependencies = ["prompt-toolkit==3.0.52", "rich>=14,<16"]
# ///
"""Check the isolated experiment, including actual POSIX TTY output modes.

Run: uv run scripts/check_native_terminal.py
Does not call models or read/write real room state.
"""
from __future__ import annotations

import asyncio
import io
import os
import select
import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from native_terminal_probe import NativeTerminalProbe


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_output_keeps_draft_and_cursor(self):
        with create_pipe_input() as pipe:
            stream = io.StringIO()
            probe = NativeTerminalProbe(input=pipe, output=DummyOutput(), stream=stream)
            runner = asyncio.create_task(probe.run())
            try:
                await until(lambda: probe.session.app.is_running)
                pipe.send_text("中文 DRAFT_KEEP")
                await until(lambda: probe.session.default_buffer.text == "中文 DRAFT_KEEP")
                pipe.send_bytes(b"\x1b[D")
                await until(lambda: probe.session.default_buffer.cursor_position == len("中文 DRAFT_KEEP") - 1)
                position = probe.session.default_buffer.cursor_position
                await probe.message("B", "BACKGROUND_REPLY")
                await probe.sample(800)
                self.assertEqual(probe.session.default_buffer.text, "中文 DRAFT_KEEP")
                self.assertEqual(probe.session.default_buffer.cursor_position, position)
                self.assertIn("BACKGROUND_REPLY", stream.getvalue())
                pipe.send_bytes(b"\x05\r")  # End, send.
                await until(lambda: probe.submitted == ["中文 DRAFT_KEEP"])
                pipe.send_text("/quit")
                pipe.send_bytes(b"\r")
                await asyncio.wait_for(runner, 5)
            finally:
                if not runner.done():
                    runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)

    async def test_multiline_completion_and_history(self):
        with create_pipe_input() as pipe:
            probe = NativeTerminalProbe(input=pipe, output=DummyOutput(), stream=io.StringIO())
            runner = asyncio.create_task(probe.run())
            try:
                await until(lambda: probe.session.app.is_running)
                pipe.send_text("第一行")
                pipe.send_bytes(b"\x0a")  # Ctrl+J newline, not submit.
                pipe.send_text("第二行")
                await until(lambda: probe.session.default_buffer.text == "第一行\n第二行")
                self.assertFalse(probe.submitted)
                pipe.send_bytes(b"\r")
                await until(lambda: len(probe.submitted) == 1)
                self.assertEqual(probe.submitted[0], "第一行\n第二行")
                await until(lambda: probe.session.app.is_running and not probe.session.default_buffer.text)
                pipe.send_bytes(b"\x1b[A")
                await until(lambda: probe.session.default_buffer.text == "第一行\n第二行")
                pipe.send_bytes(b"\x03")  # Clear this recalled input.
                await until(lambda: probe.session.app.is_running and not probe.session.default_buffer.text)
                pipe.send_text("/qui")
                pipe.send_bytes(b"\t")
                await until(lambda: probe.session.default_buffer.text == "/quit")
                pipe.send_bytes(b"\r\r")  # Accept completion, then send.
                await asyncio.wait_for(runner, 5)
            finally:
                if not runner.done():
                    runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)


@unittest.skipUnless(os.name == "posix", "PTY mode check requires POSIX")
class TTYTests(unittest.TestCase):
    def test_normal_buffer_no_mouse_reporting_and_tty_cleanup(self):
        import fcntl
        import pty
        import termios

        master, slave = pty.openpty()
        process = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 90, 0, 0))
            before = termios.tcgetattr(slave)
            environment = dict(os.environ, TERM="xterm-256color", PROMPT_TOOLKIT_NO_CPR="1")
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("native_terminal_probe.py"))],
                                       stdin=slave, stdout=slave, stderr=slave, env=environment)
            output, sent = bytearray(), False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    output.extend(os.read(master, 65536))
                if not sent and "› ".encode() in output:
                    os.write(master, b"/sample 800\r/quit\r")
                    sent = True
                if process.poll() is not None:
                    while select.select([master], [], [], 0.05)[0]:
                        output.extend(os.read(master, 65536))
                    break
            self.assertIsNotNone(process.poll(), "probe did not exit")
            self.assertEqual(process.returncode, 0, output[-2000:].decode(errors="replace"))
            self.assertIn(b"0800", output)
            for forbidden in (b"\x1b[?1049h", b"\x1b[?1047h", b"\x1b[?47h",
                              b"\x1b[?1000h", b"\x1b[?1002h", b"\x1b[?1003h", b"\x1b[?1006h",
                              b"\x1b[3J"):
                self.assertNotIn(forbidden, output)
            after = termios.tcgetattr(slave)
            # macOS may set the transient PENDIN "retype pending input" bit on
            # returning to canonical mode. Compare actual input/echo/erase modes.
            transient = getattr(termios, "PENDIN", 0)
            after[3] &= ~transient
            before[3] &= ~transient
            self.assertEqual(after, before)
            os.write(master, b"ab\x7fc\n")
            self.assertEqual(os.read(slave, 32), b"ac\n", "shell backspace no longer works")
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main(verbosity=2)
