"""Launch the Rust UI; the Python Codex transport remains a private subprocess."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main() -> None:
    arguments = sys.argv[1:]
    if "--legacy-ui" in arguments:
        sys.argv.remove("--legacy-ui")
        from .app import main as legacy_main
        legacy_main()
        return
    root = Path(__file__).resolve().parent.parent
    manifest = root / "terminal-ui/Cargo.toml"
    if not manifest.is_file():
        manifest = Path(__file__).resolve().parent / "rust_ui/Cargo.toml"
    binary = os.environ.get("CODEX_ROOM_BINARY")
    if not binary:
        if not manifest.is_file():
            raise SystemExit("缺少 Rust 界面源码，请使用完整源码包或加 --legacy-ui 启动旧版。")
        suffix = ".exe" if os.name == "nt" else ""
        target = manifest.parent / "target"
        executable = target / f"release/codex-room-ui{suffix}"
        sources = [manifest, *manifest.parent.glob("Cargo.lock"), *manifest.parent.glob("src/*.rs")]
        if not executable.is_file() or any(p.stat().st_mtime > executable.stat().st_mtime for p in sources):
            cargo = shutil.which("cargo")
            fallback = Path.home() / f".cargo/bin/cargo{suffix}"
            cargo = cargo or (str(fallback) if fallback.is_file() else None)
            if not cargo:
                raise SystemExit("Rust 界面首次启动需要 Rust：请从 https://rustup.rs 安装，或加 --legacy-ui 使用旧版。")
            print("正在构建 Rust 界面（首次启动需要一点时间）…", flush=True)
            result = subprocess.run([cargo, "build", "--release", "--locked", "--manifest-path", str(manifest),
                                     "--target-dir", str(target)], cwd=manifest.parent)
            if result.returncode:
                raise SystemExit(result.returncode)
        binary = str(executable)
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
