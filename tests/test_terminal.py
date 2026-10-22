"""Exercise the actual terminal driver; headless UI tests don't change TTY modes."""
import os
import select
import struct
import subprocess
import sys
import time
import unittest
from pathlib import Path

if os.name == "posix":
    import fcntl
    import pty
    import termios


ROOT = Path(__file__).resolve().parents[1]
START = "from codex_room.app import CodexRoom; app = CodexRoom(demo=True); app.run()"


@unittest.skipUnless(os.name == "posix", "POSIX terminal driver")
class TerminalTests(unittest.TestCase):
    def run_terminal(self, code, keys=None, *, disable_kitty=False):
        master, slave = pty.openpty()
        process = None
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 90, 0, 0))
            before = termios.tcgetattr(slave)
            environment = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
            environment["TEXTUAL_DISABLE_KITTY_KEY"] = "1" if disable_kitty else "0"
            process = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
                                       stdin=slave, stdout=slave, stderr=slave, env=environment)
            output = bytearray()
            deadline = time.monotonic() + 8
            sent = False
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    output.extend(os.read(master, 65536))
                if keys and not sent and b"F1" in output:
                    os.write(master, keys)
                    sent = True
                if process.poll() is not None:
                    while select.select([master], [], [], 0.05)[0]:
                        output.extend(os.read(master, 65536))
                    break
            self.assertIsNotNone(process.poll(), "TUI didn't exit")
            self.assertEqual(termios.tcgetattr(slave), before, "TTY attributes leaked")
            # In restored canonical mode, the shell's erase character removes text.
            os.write(master, b"ab\x7fc\n")
            self.assertEqual(os.read(slave, 32), b"ac\n")
            return bytes(output), process.returncode
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            os.close(slave)

    def assert_keyboard_restored(self, output, *, disabled=False):
        self.assertIn(b"\x1b[>0u\x1b[?1049h", output)
        self.assertNotIn(b"\x1b[>25u", output)
        if disabled:
            self.assertNotIn(b"\x1b[>1u", output)
            self.assertEqual(output.count(b"\x1b[<u"), 1)
        else:
            self.assertIn(b"\x1b[>1u", output)
            self.assertIn(b"\x1b[=0u\x1b[<u\x1b[?1049l\x1b[<u", output)
            self.assertEqual(output.count(b"\x1b[<u"), 2)
        self.assertIn(b"\x1b[?1049l\x1b[<u", output)

    def test_normal_and_encoded_shortcuts_restore_keyboard_and_backspace(self):
        for keys in (b"\x11", b"\x03", b"\x1b[113;5u", b"/quit\r"):
            with self.subTest(keys=keys):
                output, status = self.run_terminal(START, keys)
                self.assertEqual(status, 0)
                self.assert_keyboard_restored(output)

    def test_disabled_protocol_does_not_pop_an_unsaved_mode(self):
        output, status = self.run_terminal(START, b"\x11", disable_kitty=True)
        self.assertEqual(status, 0)
        self.assert_keyboard_restored(output, disabled=True)

    def test_exception_also_restores_keyboard_and_backspace(self):
        output, _ = self.run_terminal("""
from codex_room.app import CodexRoom
original_mount = CodexRoom.on_mount
async def broken_mount(self):
    await original_mount(self)
    raise RuntimeError('terminal-cleanup-check')
CodexRoom.on_mount = broken_mount
CodexRoom(demo=True).run()
""")
        self.assertIn(b"terminal-cleanup-check", output)
        self.assert_keyboard_restored(output)
