"""Parse member creation options without changing the process working directory."""
from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from .colors import normalize_color


@dataclass(frozen=True)
class AddOptions:
    name: str
    cwd: str | None = None
    color: str | None = None


def resolve_directory(value: str | Path | None, base: Path, *, validate: bool = True) -> Path:
    path = Path(value).expanduser() if value else base
    if not path.is_absolute():
        path = base / path
    path = path.resolve()
    if validate and not path.is_dir():
        raise ValueError(f"目录不存在或不是文件夹：{path}")
    return path


def parse_add(value: str, base: Path) -> AddOptions | None:
    try:
        tokens = shlex.split(value)[1:]
    except ValueError as exc:
        raise ValueError("路径的引号没有闭合。带空格的路径请用引号括起来。") from exc
    if not tokens:
        return None
    name, *arguments = tokens
    positional = []
    color = None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--color" or argument.startswith("--color="):
            if color is not None:
                raise ValueError("只能指定一次颜色。")
            if argument == "--color":
                index += 1
                if index >= len(arguments):
                    raise ValueError("--color 后面需要颜色，例如 --color blue。")
                color = arguments[index]
            else:
                color = argument.partition("=")[2]
            if not color or color.startswith("--"):
                raise ValueError("--color 后面需要颜色，例如 --color blue。")
        elif argument.startswith("--"):
            raise ValueError("用法：/add 名字 [目录] [--color 颜色]")
        else:
            positional.append(argument)
        index += 1
    if len(positional) > 2 or (len(positional) == 2 and color is not None):
        raise ValueError('用法：/add 名字 [目录] [--color 颜色]；带空格的路径请加引号。')
    cwd = positional[0] if positional else None
    if len(positional) == 2:
        color = positional[1]
    elif cwd is not None and color is None:
        # Preserve /add name blue; ./blue explicitly selects a directory named blue.
        path = resolve_directory(cwd, base, validate=False)
        if not any(char in cwd for char in ("/", "~", ".")) and not path.exists():
            try:
                if cwd.lower() != "auto":
                    normalize_color(cwd)
            except ValueError:
                pass
            else:
                color, cwd = cwd, None
    return AddOptions(name.lstrip("@").lower(), cwd, color)
