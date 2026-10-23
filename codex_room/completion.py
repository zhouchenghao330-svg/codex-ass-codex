"""Context-aware composer completion, without dispatching commands or messages."""
from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Mapping

from .colors import COLOR_CHOICES, normalize_color
from .platform_support import WINDOWS
from .member_config import resolve_member


COMMANDS = (
    ("/add", "创建 Codex · 名字 [目录] [--color 颜色]"),
    ("/focus", "切到单人对话或聊天室 · 名字 / all"),
    ("/stop", "停止成员并取消尚未发出的消息 · 名字"),
    ("/del", "移除成员，先归档会话记录 · 名字"),
    ("/rename", "重命名成员，保留会话 · [原名] 新名"),
    ("/model", "选择模型 · [模型] [推理强度]"),
    ("/fast", "切换当前成员的 Fast 模式 · [on / off]"),
    ("/adhd", "切换正文窄栏居中 · [on / off]"),
    ("/permissions", "选择权限 · [full / workspace / read-only]"),
    ("/members", "展开 / 收起成员列表"),
    ("/help", "查看帮助"),
    ("/reconnect", "重新连接 Codex"),
    ("/quit", "退出"),
)
ALIASES = {"/permission": "/permissions", "/permisson": "/permissions"}
COMMAND_NAMES = frozenset(name for name, _ in COMMANDS) | ALIASES.keys() | {"/demo"}
PERMISSIONS = (("full", "Full Access"), ("workspace", "工作区读写"), ("read-only", "只读"))
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def is_room_command(text: str) -> bool:
    """Only complete, registered command names are local commands; paths are text."""
    words = text.split(maxsplit=1)
    return bool(words and words[0].lower() in COMMAND_NAMES)


@dataclass(frozen=True)
class Token:
    start: int
    end: int
    value: str


def tokens(text: str) -> list[Token]:
    """A forgiving shlex-style lexer, including unfinished quotes and escapes."""
    result = []
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        start = position
        value = ""
        quote = ""
        while position < len(text):
            char = text[position]
            if not quote and char.isspace():
                break
            if char == quote:
                quote = ""
            elif not quote and char in "\"'":
                quote = char
            elif char == "\\" and not WINDOWS and quote != "'" and position + 1 < len(text):
                following = text[position + 1]
                if not quote or following in '\\"$`\n':
                    position += 1
                    value += following
                else:
                    value += char
            else:
                value += char
            position += 1
        result.append(Token(start, position, value))
    return result


@dataclass(frozen=True)
class Candidate:
    value: str
    detail: str = ""
    member: str = ""
    directory: bool = False


@dataclass(frozen=True)
class Completion:
    kind: str
    start: int
    end: int
    query: str
    candidates: tuple[Candidate, ...]

    def apply(self, text: str, candidate: Candidate) -> tuple[str, int]:
        value = shlex.quote(candidate.value) if candidate.directory else candidate.value
        suffix = text[self.end:]
        cursor = self.start + len(value)
        if not candidate.directory:
            if suffix[:1].isspace():
                cursor += 1
            else:
                value += " "
                cursor += 1
        return text[:self.start] + value + suffix, cursor


class CompletionEngine:
    def __init__(self) -> None:
        self._directory_cache: dict[Path, tuple[float, tuple[str, ...]]] = {}

    def directories(self, query: str, cwd: Path, *, refresh: bool) -> list[Candidate]:
        if WINDOWS:
            query = query.replace("\\", "/")
        if query == "~":
            return [Candidate("~/", "主目录", directory=True)]
        parent_text, _, prefix = query.rpartition("/")
        base = (parent_text + "/") if "/" in query else ""
        try:
            parent = Path(base).expanduser() if base else cwd
            if not parent.is_absolute():
                parent = cwd / parent
            cached = self._directory_cache.get(parent)
            if refresh or cached is None or monotonic() - cached[0] > 1:
                with os.scandir(parent) as entries:
                    names = tuple(sorted(entry.name for entry in entries if entry.is_dir()))
                self._directory_cache[parent] = (monotonic(), names)
                if len(self._directory_cache) > 32:
                    del self._directory_cache[next(iter(self._directory_cache))]
            else:
                names = cached[1]
        except (OSError, ValueError, RuntimeError):
            return []
        matches = [Candidate(base + name + "/", "目录 · Tab 继续补全", directory=True)
                   for name in names if name.startswith(prefix) and (not name.startswith(".") or prefix.startswith("."))]
        if not base:
            matches = [Candidate(value, description, directory=True) for value, description in
                       (("./", "当前目录"), ("../", "上一级目录"), ("~/", "主目录"))
                       if value.startswith(query)] + matches
        return matches[:100]

    def complete(self, text: str, cursor: int, agents: Mapping, cwd: Path,
                 models: list[dict], *, explicit: bool = False) -> Completion | None:
        cursor = min(len(text), max(0, cursor))
        words = tokens(text)
        active = next((word for word in words if word.start <= cursor <= word.end), Token(cursor, cursor, ""))
        before = [word.value for word in words if word.end <= active.start and word is not active]
        partial = tokens(text[active.start:cursor])
        query = partial[0].value if partial else ""
        kind = ""
        candidates = []

        mentions = [word for word in before if word.startswith("@")]
        leading_mentions = len(mentions) == len(before) and all(resolve_member(word, agents) for word in mentions)
        if leading_mentions and query.startswith("@"):
            kind = "mention"
            chosen = {resolve_member(word, agents) for word in mentions}
            candidates = [Candidate("@" + agent.label, member=name) for name, agent in agents.items()
                          if name not in chosen and (name.casefold().startswith(query[1:].casefold()) or agent.label.casefold().startswith(query[1:].casefold()))]
        else:
            # Commands may also follow an explicit @recipient. Prose is untouched.
            if before and before[0].startswith("@"):
                if not resolve_member(before[0], agents):
                    return None
                before = before[1:]
            if not before and query.startswith("/"):
                kind = "command"
                prefix = ALIASES.get(query.lower(), query.lower())
                candidates = [Candidate(name, detail) for name, detail in COMMANDS if name.startswith(prefix)]
                if not candidates:
                    return None
            elif before:
                command, *arguments = before
                command = ALIASES.get(command.lower(), command.lower())
                if command in {"/focus", "/stop", "/del", "/rename"} and not arguments:
                    kind = "member"
                    candidates = [Candidate(("@" if query.startswith("@") else "") + agent.label, member=name)
                                  for name, agent in agents.items() if name.startswith(query.lstrip("@").casefold())
                                  or agent.label.casefold().startswith(query.lstrip("@").casefold())]
                    if command == "/focus" and "all".startswith(query.casefold()):
                        candidates.append(Candidate("all", "聊天室 · 显示全部消息"))
                elif command == "/adhd" and not arguments:
                    kind = "reading"
                    candidates = [Candidate(value, detail) for value, detail in
                                  (("on", "开启窄栏居中"), ("off", "关闭窄栏居中")) if value.startswith(query.lower())]
                elif command == "/permissions" and not arguments:
                    kind = "permission"
                    candidates = [Candidate(value, detail) for value, detail in PERMISSIONS if value.startswith(query.lower())]
                elif command == "/model" and len(arguments) <= 1:
                    if not arguments:
                        kind = "model"
                        known = {item["id"]: item.get("displayName", "") for item in models}
                        for agent in agents.values():
                            if agent.model:
                                known.setdefault(agent.model, "当前成员使用的模型")
                        candidates = [Candidate(value, detail) for value, detail in known.items()
                                      if value.lower().startswith(query.lower())]
                    else:
                        kind = "effort"
                        model = next((item for item in models if item["id"] == arguments[0]), {})
                        efforts = [item["reasoningEffort"] for item in model.get("supportedReasoningEfforts", [])] or EFFORTS
                        candidates = [Candidate(value, "推理强度") for value in efforts if value.startswith(query.lower())]
                elif command == "/add" and arguments:
                    used = {normalize_color(agent.color) for agent in agents.values()}
                    colors = [Candidate("auto", "自动分配未使用的颜色")] + [
                        Candidate(key, label) for key, label, color in COLOR_CHOICES if color not in used]
                    if arguments[-1] == "--color":
                        kind = "color"
                        candidates = [item for item in colors if item.value.startswith(query.lower())]
                    elif query.startswith("--color="):
                        kind = "color"
                        candidates = [Candidate("--color=" + item.value, item.detail) for item in colors
                                      if item.value.startswith(query.partition("=")[2].lower())]
                    else:
                        positional = []
                        color_given = False
                        skip = False
                        for argument in arguments:
                            if skip:
                                skip = False
                            elif argument == "--color":
                                color_given = True
                                skip = True
                            elif argument.startswith("--color="):
                                color_given = True
                            else:
                                positional.append(argument)
                        if len(positional) in {1, 2}:
                            kind = "directory" if len(positional) == 1 else "color"
                            if len(positional) == 1 and not query.startswith("--"):
                                candidates = self.directories(query, cwd, refresh=explicit)
                            if not color_given:
                                if "--color".startswith(query):
                                    candidates.append(Candidate("--color", "指定成员颜色"))
                                if len(positional) == 2:
                                    candidates.extend(item for item in colors if item.value.startswith(query.lower()))
                                elif query and not any(char in query for char in "/.~"):
                                    for item in colors:
                                        if item.value.startswith(query.lower()):
                                            # An existing file/folder takes precedence in /add's legacy syntax.
                                            value = "--color=" + item.value if (cwd / item.value).exists() else item.value
                                            candidates.append(Candidate(value, item.detail))

        if not kind:
            return None
        if not explicit and kind != "mention" and len(candidates) == 1 and candidates[0].value == query and not candidates[0].directory:
            return None
        return Completion(kind, active.start, active.end, query, tuple(candidates))
