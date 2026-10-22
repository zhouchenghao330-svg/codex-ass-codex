"""Small OS boundaries for file locking, local paths and CLI launchers."""
from __future__ import annotations

import shlex
import shutil
import sys
import platform
from pathlib import Path
from typing import BinaryIO


WINDOWS = sys.platform == "win32"


def lock_file(stream: BinaryIO) -> None:
    """Take a non-blocking process lock, released automatically on close/exit."""
    if WINDOWS:
        import msvcrt

        # Lock the same byte in every process, independent of append position.
        # Byte-range locks may extend past EOF, so the file need not be written.
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def split_arguments(text: str) -> list[str]:
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    if WINDOWS:
        # These are TUI arguments, not a POSIX shell: C:\Users is a path.
        lexer.escape = ""
    return list(lexer)


def codex_command() -> list[str]:
    """Resolve a native executable or bypass npm's Windows batch wrapper."""
    executable = shutil.which("codex.exe") if WINDOWS else None
    executable = executable or shutil.which("codex")
    if executable is None:
        raise FileNotFoundError("找不到 Codex CLI。请先在同一个终端确认 codex --version 可以运行。")
    arguments = ["app-server", "--listen", "stdio://"]
    if WINDOWS and Path(executable).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        # npm puts its shim beside node_modules. Launch its native binary
        # directly: terminating a batch/Node wrapper can leave Codex running.
        package = Path(executable).parent / "node_modules" / "@openai" / "codex"
        architectures = ["arm64", "x64"] if platform.machine().lower() in {"arm64", "aarch64"} else ["x64"]
        for architecture in architectures:
            target = "aarch64-pc-windows-msvc" if architecture == "arm64" else "x86_64-pc-windows-msvc"
            for root in (package.parent / f"codex-win32-{architecture}",
                         package / "node_modules" / "@openai" / f"codex-win32-{architecture}", package):
                for directory in ("bin", "codex"):
                    binary = root / "vendor" / target / directory / "codex.exe"
                    if binary.is_file():
                        return [str(binary), *arguments]
        raise FileNotFoundError(
            "发现 Codex 的 Windows 脚本启动器，但找不到对应的 codex.exe。"
            "请修复 npm 的 Codex 安装，或使用 Codex 官方 Windows 独立安装器。"
        )
    return [executable, *arguments]
