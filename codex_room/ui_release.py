"""Pinned, anonymous UI downloads. No Rust toolchain on the ordinary launch path."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import platform
import re
import sys
import tempfile
import time
from urllib.request import Request, urlopen

from .platform_support import lock_file, print_status


UI_VERSION = "0.2.2"
REPOSITORY = "zhouchenghao330-svg/codex-ass-codex"
RELEASE_URL = f"https://github.com/{REPOSITORY}/releases/download/v{UI_VERSION}"
TARGETS = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "arm64"): "aarch64-unknown-linux-musl",
    ("linux", "x86_64"): "x86_64-unknown-linux-musl",
    ("win32", "arm64"): "aarch64-pc-windows-msvc",
    ("win32", "x86_64"): "x86_64-pc-windows-msvc",
}
MAX_BINARY_BYTES = 128 * 1024 * 1024


class DownloadError(RuntimeError):
    pass


def asset_name(target: str) -> str:
    return f"codex-room-ui-{target}" + (".exe" if "windows" in target else "")


def current_target() -> str:
    machine = platform.machine().lower()
    architecture = {"aarch64": "arm64", "amd64": "x86_64", "x64": "x86_64"}.get(machine, machine)
    try:
        return TARGETS[sys.platform, architecture]
    except KeyError:
        raise DownloadError(
            f"暂无 {sys.platform}/{machine} 的预编译界面。"
            "开发者可安装 Rust 1.88+ 后用 uv run codex_room --build-ui 构建，"
            "或用 --legacy-ui 启动旧界面。"
        ) from None


def cache_root() -> Path:
    override = os.environ.get("CODEX_ROOM_CACHE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "codex-room" / "ui"


def checksum_for(manifest: str, filename: str) -> str:
    matches = []
    for line in manifest.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64}) [ *](\S+)", line)
        if match and match[2] == filename:
            matches.append(match[1].lower())
    if len(matches) != 1:
        raise DownloadError(f"SHA256SUMS 中缺少或重复记录：{filename}")
    return matches[0]


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _fetch(url: str, destination: Path, limit: int) -> None:
    request = Request(url, headers={"User-Agent": f"codex-room/{UI_VERSION}", "Accept": "application/octet-stream"})
    # urllib respects the user's standard HTTPS_PROXY / HTTP_PROXY settings.
    with urlopen(request, timeout=30) as response, destination.open("wb") as output:
        size = 0
        deadline = time.monotonic() + 180
        while chunk := response.read1(128 * 1024):
            size += len(chunk)
            if size > limit or time.monotonic() > deadline:
                raise DownloadError("下载超过大小或时间限制，请检查网络后重试。")
            output.write(chunk)


@contextmanager
def _installation_lock(directory: Path):
    # An OS lock is released on process exit; a leftover lock file cannot block
    # future launches. Serializing installation also avoids replacing a running
    # .exe on Windows when two different rooms first start together.
    with (directory / "install.lock").open("a+b") as stream:
        deadline = time.monotonic() + 240
        while True:
            try:
                lock_file(stream)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise DownloadError("等待另一进程下载界面超时，请稍后重试。") from None
                time.sleep(0.1)
        yield


def _valid_cache(executable: Path, manifest: Path, filename: str) -> bool:
    try:
        expected = checksum_for(manifest.read_text(encoding="utf-8"), filename)
        return executable.is_file() and sha256(executable) == expected
    except (OSError, UnicodeError, DownloadError):
        return False


def ensure_binary() -> Path:
    target = current_target()
    filename = asset_name(target)
    directory = cache_root() / UI_VERSION / target
    executable = directory / filename
    manifest = directory / "SHA256SUMS"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with _installation_lock(directory):
            if not _valid_cache(executable, manifest, filename):
                print_status(f"正在下载 Codex Room 界面 v{UI_VERSION}（{target}，仅首次需要）…")
                with tempfile.TemporaryDirectory(prefix="download-", dir=directory) as temporary:
                    staged_manifest = Path(temporary) / "SHA256SUMS"
                    staged_binary = Path(temporary) / filename
                    _fetch(f"{RELEASE_URL}/SHA256SUMS", staged_manifest, 64 * 1024)
                    expected = checksum_for(staged_manifest.read_text(encoding="utf-8"), filename)
                    _fetch(f"{RELEASE_URL}/{filename}", staged_binary, MAX_BINARY_BYTES)
                    if sha256(staged_binary) != expected:
                        raise DownloadError("界面 SHA-256 校验失败，没有运行下载的文件；请重试。")
                    if sys.platform != "win32":
                        staged_binary.chmod(0o755)
                    os.replace(staged_binary, executable)
                    os.replace(staged_manifest, manifest)
            if sys.platform != "win32":
                executable.chmod(0o755)
            return executable
    except (OSError, UnicodeError, DownloadError) as exc:
        raise DownloadError(
            f"无法准备界面 v{UI_VERSION}：{exc}\n"
            f"下载地址：{RELEASE_URL}\n"
            f"缓存目录：{directory}\n"
            "请确认网络可访问 GitHub Releases，稍后重新运行。"
            "已校验的缓存可离线使用；也可加 --legacy-ui 使用旧界面。"
        ) from exc
