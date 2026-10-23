"""Launch the Rust UI; the Python Codex transport remains a private subprocess."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .ui_release import DownloadError, ensure_binary
from .platform_support import print_status, utf8_stdio


def build_binary(root: Path) -> Path:
    """Explicit developer path; ordinary launches never invoke Cargo."""
    manifest = root / "terminal-ui/Cargo.toml"
    if not manifest.is_file():
        manifest = Path(__file__).resolve().parent / "rust_ui/Cargo.toml"
    if not manifest.is_file():
        raise SystemExit("缺少 Rust 界面源码，请从 GitHub 克隆完整项目后构建。")
    suffix = ".exe" if os.name == "nt" else ""
    target = manifest.parent / "target"
    executable = target / f"release/codex-room-ui{suffix}"
    sources = [manifest, *manifest.parent.glob("Cargo.lock"), *manifest.parent.glob("src/**/*.rs")]
    if not executable.is_file() or any(p.stat().st_mtime > executable.stat().st_mtime for p in sources):
        cargo = shutil.which("cargo")
        fallback = Path.home() / f".cargo/bin/cargo{suffix}"
        cargo = cargo or (str(fallback) if fallback.is_file() else None)
        if not cargo:
            raise SystemExit("--build-ui 是开发者模式，需要 Rust 1.88+：https://rustup.rs 。普通启动请去掉 --build-ui。")
        print_status("正在构建本地 Rust 界面…")
        result = subprocess.run([cargo, "build", "--release", "--locked", "--manifest-path", str(manifest),
                                 "--target-dir", str(target)], cwd=manifest.parent)
        if result.returncode:
            raise SystemExit(result.returncode)
    return executable


def main() -> None:
    utf8_stdio()
    arguments = sys.argv[1:]
    if "--legacy-ui" in arguments:
        sys.argv.remove("--legacy-ui")
        from .app import main as legacy_main
        legacy_main()
        return
    root = Path(__file__).resolve().parent.parent
    build = "--build-ui" in arguments or os.environ.get("CODEX_ROOM_BUILD_UI") == "1"
    arguments = [argument for argument in arguments if argument != "--build-ui"]
    binary = os.environ.get("CODEX_ROOM_BINARY")
    if not binary:
        try:
            binary = str(build_binary(root) if build else ensure_binary())
        except DownloadError as exc:
            raise SystemExit(str(exc)) from exc
    environment = {**os.environ, "CODEX_ROOM_PYTHON": sys.executable, "CODEX_ROOM_ROOT": str(root)}
    try:
        # Do not exec: the launcher also works on Windows, and the Rust guard owns
        # terminal restoration before this process returns to the shell.
        process = subprocess.Popen([binary, *arguments], env=environment)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            code = process.wait()
        raise SystemExit(code)
    except OSError as exc:
        raise SystemExit(f"无法启动 Rust 界面：{exc}") from exc
