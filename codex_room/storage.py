"""Small, atomic room state; Codex itself owns the model's conversation history."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .platform_support import lock_file


class RoomStore:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "state.json"
        self.lock = (directory / "room.lock").open("a+b")
        try:
            lock_file(self.lock)
        except OSError:
            self.lock.close()
            raise RuntimeError("这个目录的 Codex Room 已在运行，请回到原来的终端。") from None

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("version") not in {1, 2}:
                raise ValueError("不支持的状态版本")
            if data["version"] == 1:
                backup = self.path.with_name("state.v1.backup.json")
                if not backup.exists():
                    with backup.open("x", encoding="utf-8") as stream:
                        json.dump(data, stream, ensure_ascii=False, indent=2)
                    backup.chmod(0o600)
                presets = {
                    ("exp", "实验", "方案 · 实验 · 结果"),
                    ("ops", "运维", "服务器 · 资源 · 进程"),
                    ("review", "审查", "推敲 · 检查 · 复核"),
                }
                removed = {item["name"] for item in data.get("agents", [])
                           if (item.get("name"), item.get("label"), item.get("role")) in presets}
                members = []
                for item in data.get("agents", []):
                    if item["name"] in removed:
                        continue
                    item.pop("role", None)
                    item["legacy_instructions"] = bool(item.get("thread_id"))
                    members.append(item)
                data["agents"] = members
                data["entries"] = [entry for entry in data.get("entries", [])
                                   if entry.get("sender") not in removed and entry.get("target") not in removed
                                   and not (entry.get("sender") == "system" and
                                            (entry.get("body", "").startswith("Codex 已连接") or
                                             any(f"@{name}" in entry.get("body", "") for name in removed)))]
                names = [item["name"] for item in members]
                if data.get("selected") not in names:
                    data["selected"] = names[0] if names else ""
                data["version"] = 2
                self.save(data)
            return data
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"无法读取群聊状态 {self.path}：{exc}") from exc

    def save(self, data: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def archive_member(self, name: str, data: dict) -> Path:
        directory = self.path.parent / "archives"
        directory.mkdir(mode=0o700, exist_ok=True)
        prefix = f"{datetime.now():%Y%m%d-%H%M%S}-{name}-"
        fd, archive = tempfile.mkstemp(prefix=prefix, suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(archive)
            raise
        return Path(archive)

    def close(self) -> None:
        if not self.lock.closed:
            self.lock.close()
