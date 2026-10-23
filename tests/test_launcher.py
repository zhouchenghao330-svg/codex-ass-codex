import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Thread
import tomllib
import unittest
from unittest.mock import Mock, patch

from codex_room import launcher, ui_release as release


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.requests = []
        cls.files = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append((self.path, self.headers.get("Authorization")))
                content = cls.files.get(self.path)
                self.send_response(200 if content is not None else 404)
                self.end_headers()
                if content is not None:
                    self.wfile.write(content)

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="Room cache test ")
        self.addCleanup(self.temporary.cleanup)
        self.target = release.current_target()
        self.filename = release.asset_name(self.target)
        self.content = b"test-native-binary-content"
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.requests.clear()
        self.files.clear()
        self.files.update({f"/v0.2.1/{self.filename}": self.content,
                           "/v0.2.1/SHA256SUMS": f"{self.digest}  {self.filename}\n".encode()})
        self.env = patch.dict(os.environ, {"CODEX_ROOM_CACHE_DIR": self.temporary.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.url = patch.object(release, "RELEASE_URL", f"http://127.0.0.1:{self.server.server_port}/v0.2.1")
        self.url.start()
        self.addCleanup(self.url.stop)

    def test_anonymous_download_hash_and_offline_cache(self):
        binary = release.ensure_binary()
        self.assertEqual(binary.read_bytes(), self.content)
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(auth is None for _, auth in self.requests))
        self.assertIn(release.UI_VERSION, str(binary))
        with patch.object(release, "_fetch", side_effect=AssertionError("offline cache must not fetch")):
            self.assertEqual(release.ensure_binary(), binary)
        if sys.platform != "win32":
            self.assertTrue(os.access(binary, os.X_OK))

    def test_corrupted_binary_is_repaired_before_use(self):
        binary = release.ensure_binary()
        binary.write_bytes(b"broken")
        self.assertEqual(release.ensure_binary().read_bytes(), self.content)
        self.assertEqual(len(self.requests), 4)

    def test_corrupted_checksum_is_repaired(self):
        binary = release.ensure_binary()
        (binary.parent / "SHA256SUMS").write_text("invalid checksum")
        self.assertEqual(release.ensure_binary().read_bytes(), self.content)
        self.assertEqual(len(self.requests), 4)

    def test_bad_download_not_installed_and_retry_works(self):
        self.files[f"/v0.2.1/{self.filename}"] = b"incomplete"
        with self.assertRaisesRegex(release.DownloadError, "SHA-256"):
            release.ensure_binary()
        self.assertFalse(list(Path(self.temporary.name).rglob(self.filename)))
        self.assertFalse(list(Path(self.temporary.name).rglob("download-*")))
        self.files[f"/v0.2.1/{self.filename}"] = self.content
        self.assertEqual(release.ensure_binary().read_bytes(), self.content)

    def test_missing_release_has_actionable_error(self):
        self.files.clear()
        with self.assertRaisesRegex(release.DownloadError, "GitHub Releases"):
            release.ensure_binary()

    def test_oversize_download_is_not_installed(self):
        with patch.object(release, "MAX_BINARY_BYTES", 3):
            with self.assertRaisesRegex(release.DownloadError, "大小"):
                release.ensure_binary()
        self.assertFalse(list(Path(self.temporary.name).rglob(self.filename)))

    def test_version_change_uses_separate_cache(self):
        original = release.ensure_binary()
        with patch.object(release, "UI_VERSION", "999.0.0"):
            upgraded = release.ensure_binary()
        self.assertNotEqual(original, upgraded)
        self.assertTrue(original.is_file())
        self.assertEqual(len(self.requests), 4)

    def test_checksum_rejects_missing_duplicate_and_malformed_records(self):
        for manifest in ("", f"invalid  {self.filename}",
                         f"{self.digest}  {self.filename}\n" * 2,
                         f"{self.digest}  ../{self.filename}"):
            with self.subTest(manifest=manifest), self.assertRaises(release.DownloadError):
                release.checksum_for(manifest, self.filename)
        self.assertEqual(release.checksum_for(f"{self.digest.upper()} *{self.filename}", self.filename), self.digest)

    def test_all_supported_platform_architecture_aliases(self):
        for system, architecture, target in (
            ("darwin", "arm64", "aarch64-apple-darwin"), ("darwin", "x86_64", "x86_64-apple-darwin"),
            ("linux", "aarch64", "aarch64-unknown-linux-musl"), ("linux", "x86_64", "x86_64-unknown-linux-musl"),
            ("win32", "ARM64", "aarch64-pc-windows-msvc"), ("win32", "AMD64", "x86_64-pc-windows-msvc"),
        ):
            with self.subTest(system=system, architecture=architecture), patch.object(release.sys, "platform", system), \
                    patch.object(release.platform, "machine", return_value=architecture):
                self.assertEqual(release.current_target(), target)
                self.assertEqual(release.asset_name(target).endswith(".exe"), system == "win32")
        with patch.object(release.platform, "machine", return_value="i686"):
            with self.assertRaisesRegex(release.DownloadError, "--build-ui"):
                release.current_target()

    def test_cache_directories_and_override(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(release.Path, "home", return_value=Path(self.temporary.name)):
            for system, directory in (("darwin", "Library/Caches"), ("linux", ".cache"), ("win32", "AppData/Local")):
                with patch.object(release.sys, "platform", system):
                    self.assertEqual(release.cache_root(), Path(self.temporary.name) / directory / "codex-room/ui")
            with patch.object(release.sys, "platform", "linux"), patch.dict(os.environ, {"XDG_CACHE_HOME": self.temporary.name}):
                self.assertEqual(release.cache_root(), Path(self.temporary.name) / "codex-room/ui")
            with patch.object(release.sys, "platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": self.temporary.name}):
                self.assertEqual(release.cache_root(), Path(self.temporary.name) / "codex-room/ui")

    def test_release_versions_match_python_rust_and_lockfiles(self):
        for filename, package in (("pyproject.toml", "project"), ("terminal-ui/Cargo.toml", "package")):
            data = tomllib.loads((ROOT / filename).read_text())
            self.assertEqual(data[package]["version"], release.UI_VERSION)
        for filename, name in (("uv.lock", "codex-room"), ("terminal-ui/Cargo.lock", "codex-room-ui")):
            data = tomllib.loads((ROOT / filename).read_text())
            self.assertEqual(next(p["version"] for p in data["package"] if p["name"] == name), release.UI_VERSION)


class LauncherTests(unittest.TestCase):
    def launch(self, arguments, environment=None):
        process = Mock()
        process.wait.return_value = 7
        with patch.object(sys, "argv", ["codex_room", *arguments]), \
                patch.dict(os.environ, environment or {}, clear=True), \
                patch.object(launcher, "ensure_binary", return_value=Path("cached ui")) as download, \
                patch.object(launcher, "build_binary", return_value=Path("local ui")) as build, \
                patch.object(launcher.subprocess, "Popen", return_value=process) as spawn:
            with self.assertRaises(SystemExit) as result:
                launcher.main()
            self.assertEqual(result.exception.code, 7)
            return download, build, spawn

    def test_normal_launch_downloads_without_building_and_forwards_arguments(self):
        download, build, spawn = self.launch(["--cwd", "path with spaces", "--demo"])
        download.assert_called_once_with()
        build.assert_not_called()
        self.assertEqual(spawn.call_args.args[0], ["cached ui", "--cwd", "path with spaces", "--demo"])
        self.assertEqual(spawn.call_args.kwargs["env"]["CODEX_ROOM_PYTHON"], sys.executable)
        self.assertEqual(spawn.call_args.kwargs["env"]["CODEX_ROOM_ROOT"], str(ROOT))

    def test_explicit_developer_build_and_binary_override(self):
        for arguments, environment in ((["--build-ui", "--help"], {}),
                                        (["--help"], {"CODEX_ROOM_BUILD_UI": "1"})):
            download, build, spawn = self.launch(arguments, environment)
            download.assert_not_called()
            build.assert_called_once_with(ROOT)
            self.assertEqual(spawn.call_args.args[0], ["local ui", "--help"])
        download, build, spawn = self.launch(["--build-ui"], {"CODEX_ROOM_BINARY": "custom binary"})
        download.assert_not_called()
        build.assert_not_called()
        self.assertEqual(spawn.call_args.args[0], ["custom binary"])

    def test_failed_download_never_starts_child_or_compiler(self):
        with patch.object(sys, "argv", ["codex_room"]), patch.dict(os.environ, {}, clear=True), \
                patch.object(launcher, "ensure_binary", side_effect=release.DownloadError("download failed")), \
                patch.object(launcher, "build_binary") as build, patch.object(launcher.subprocess, "Popen") as spawn:
            with self.assertRaisesRegex(SystemExit, "download failed"):
                launcher.main()
            build.assert_not_called()
            spawn.assert_not_called()

    def test_explicit_build_invokes_cargo_locked_and_reuses_fresh_output(self):
        with tempfile.TemporaryDirectory(prefix="Room source ") as tmp:
            root = Path(tmp)
            manifest = root / "terminal-ui/Cargo.toml"
            manifest.parent.mkdir()
            manifest.write_text("[package]")
            with patch.object(launcher.shutil, "which", return_value="cargo"), \
                    patch.object(launcher.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as build:
                binary = launcher.build_binary(root)
                self.assertEqual(build.call_args.args[0][:4], ["cargo", "build", "--release", "--locked"])
                binary.parent.mkdir(parents=True)
                binary.write_bytes(b"compiled")
                os.utime(binary, (manifest.stat().st_mtime + 10,) * 2)
                launcher.build_binary(root)
                self.assertEqual(build.call_count, 1)


if __name__ == "__main__":
    unittest.main()
