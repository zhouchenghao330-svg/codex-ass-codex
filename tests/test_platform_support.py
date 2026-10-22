import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from codex_room.completion import CompletionEngine, tokens
from codex_room.member_config import parse_add
from codex_room.platform_support import codex_command, lock_file, split_arguments
from codex_room.storage import RoomStore


ROOT = Path(__file__).resolve().parents[1]
ARGS = ["app-server", "--listen", "stdio://"]


class PlatformSupportTests(unittest.TestCase):
    def test_import_does_not_require_posix_modules(self):
        script = """
import sys
class NoPosix:
    def find_spec(self, fullname, *args):
        if fullname in {'fcntl', 'termios', 'tty', 'pty'}:
            raise ImportError('POSIX module unavailable')
sys.meta_path.insert(0, NoPosix())
import codex_room.storage
import codex_room.runtime
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_posix_cli_resolution_and_missing_cli_message(self):
        with patch("codex_room.platform_support.WINDOWS", False), \
                patch("codex_room.platform_support.shutil.which", return_value="/path with spaces/codex"):
            self.assertEqual(codex_command(), ["/path with spaces/codex", *ARGS])
        with patch("codex_room.platform_support.shutil.which", return_value=None):
            with self.assertRaisesRegex(FileNotFoundError, "codex --version"):
                codex_command()

    def test_windows_prefers_native_executable(self):
        def which(name):
            return "C:/Program Files/Codex/codex.exe" if name == "codex.exe" else "C:/npm/codex.cmd"
        with patch("codex_room.platform_support.WINDOWS", True), \
                patch("codex_room.platform_support.shutil.which", side_effect=which):
            self.assertEqual(codex_command(), ["C:/Program Files/Codex/codex.exe", *ARGS])

    def test_npm_windows_shims_resolve_to_native_process_for_both_architectures(self):
        for architecture, machine, target in (("x64", "AMD64", "x86_64-pc-windows-msvc"),
                                               ("arm64", "ARM64", "aarch64-pc-windows-msvc")):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory(prefix="Codex Room ") as tmp:
                root = Path(tmp)
                shim = root / "codex.cmd"
                shim.write_text("@echo off")
                binary = root / "node_modules" / "@openai" / f"codex-win32-{architecture}" / "vendor" / target / "bin" / "codex.exe"
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"fixture")
                with patch("codex_room.platform_support.WINDOWS", True), \
                        patch("codex_room.platform_support.platform.machine", return_value=machine), \
                        patch("codex_room.platform_support.shutil.which", side_effect=lambda name: str(shim) if name == "codex" else None):
                    self.assertEqual(codex_command(), [str(binary), *ARGS])

    def test_legacy_npm_bundle_and_broken_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shim = root / "codex.cmd"
            binary = root / "node_modules" / "@openai" / "codex" / "vendor" / "x86_64-pc-windows-msvc" / "codex" / "codex.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"fixture")
            with patch("codex_room.platform_support.WINDOWS", True), \
                    patch("codex_room.platform_support.platform.machine", return_value="AMD64"), \
                    patch("codex_room.platform_support.shutil.which", side_effect=lambda name: str(shim) if name == "codex" else None):
                self.assertEqual(codex_command(), [str(binary), *ARGS])
                binary.unlink()
                with self.assertRaisesRegex(FileNotFoundError, "codex.exe"):
                    codex_command()

    def test_windows_lock_always_starts_at_byte_zero_without_importing_fcntl(self):
        backend = SimpleNamespace(LK_NBLCK=2, locking=Mock())
        with tempfile.TemporaryFile(mode="w+b") as stream, \
                patch("codex_room.platform_support.WINDOWS", True), \
                patch.dict(sys.modules, {"msvcrt": backend}):
            stream.write(b"existing lock file")
            lock_file(stream)
            backend.locking.assert_called_once_with(stream.fileno(), 2, 1)
            self.assertEqual(stream.tell(), 0)

    def test_windows_paths_quotes_and_completion_preserve_backslashes(self):
        with patch("codex_room.platform_support.WINDOWS", True), \
                patch("codex_room.completion.WINDOWS", True), tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for path in (r"C:\Users\friend\repo", r"D:\My Projects\论文", "D:\\My Projects\\"):
                text = f'/add dev "{path}" --color blue'
                self.assertEqual(split_arguments(text)[2], path)
                self.assertEqual(parse_add(text, base).cwd, path)
                self.assertEqual(tokens(text)[2].value, path)
            self.assertEqual(parse_add(r"/add dev C:\Users\friend\repo", base).cwd, r"C:\Users\friend\repo")
            (base / "My Projects").mkdir()
            (base / "My Projects" / "paper").mkdir()
            options = CompletionEngine().directories("My Projects\\pa", base, refresh=True)
            self.assertEqual([option.value for option in options], ["My Projects/paper/"])
            with self.assertRaises(ValueError):
                parse_add('/add dev "D:\\unclosed', base)

    def test_other_process_cannot_open_the_room_until_owner_closes(self):
        with tempfile.TemporaryDirectory(prefix="房间 with spaces ") as tmp:
            directory = Path(tmp)
            owner = RoomStore(directory)
            script = "from pathlib import Path; from codex_room.storage import RoomStore; import sys; s=RoomStore(Path(sys.argv[1])); s.close()"
            def probe():
                return subprocess.run([sys.executable, "-c", script, tmp], cwd=ROOT,
                                      capture_output=True, timeout=10)
            try:
                self.assertNotEqual(probe().returncode, 0)
            finally:
                owner.close()
            result = probe()
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_process_exit_releases_lock_and_state_still_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = "from pathlib import Path; from codex_room.storage import RoomStore; import sys; s=RoomStore(Path(sys.argv[1])); print('locked',flush=True); sys.stdin.read()"
            process = subprocess.Popen([sys.executable, "-c", script, tmp], cwd=ROOT,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                self.assertEqual(process.stdout.readline().strip(), b"locked")
                process.kill()
                process.communicate(timeout=10)
                store = RoomStore(Path(tmp))
                try:
                    state = {"version": 2, "agents": [], "entries": [{"sender": "system", "body": "中文"}]}
                    store.save(state)
                    self.assertEqual(store.load(), state)
                finally:
                    store.close()
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)


if __name__ == "__main__":
    unittest.main()
