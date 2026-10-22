"""Lossless compact prompt blocks, independent of the terminal editor."""
from __future__ import annotations

import shlex
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import unquote, urlparse


PASTE_CHAR_LIMIT = 1000
PASTE_LINE_LIMIT = 8
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@dataclass(frozen=True)
class Attachment:
    start: int
    end: int
    kind: str
    content: str
    label: str


@dataclass
class Prompt:
    text: str
    attachments: list[Attachment] = field(default_factory=list)

    @classmethod
    def load(cls, value: str | dict) -> Prompt:
        if isinstance(value, str):
            return cls(value)
        return cls(value["text"], [Attachment(**item) for item in value.get("attachments", [])])

    def dump(self) -> str | dict:
        return asdict(self) if self.attachments else self.text

    def atomic_range(self, start: int, end: int) -> tuple[int, int]:
        start, end = sorted((start, end))
        for item in self.attachments:
            if (start < item.end and end > item.start) or item.start < start == end < item.end:
                start, end = min(start, item.start), max(end, item.end)
        return start, end

    def slice(self, start: int, end: int | None = None) -> Prompt:
        end = len(self.text) if end is None else end
        return Prompt(self.text[start:end], [replace(item, start=item.start - start, end=item.end - start)
            for item in self.attachments if start <= item.start and item.end <= end])

    def strip(self) -> Prompt:
        return self.slice(len(self.text) - len(self.text.lstrip()), len(self.text.rstrip()))

    def expand(self, *, copy_images: bool = False) -> str:
        text = self.text
        for item in reversed(self.attachments):
            if item.kind == "paste" or copy_images:
                content = shlex.quote(item.content) if item.kind == "image" else item.content
                text = text[:item.start] + content + text[item.end:]
        return text

    @property
    def images(self) -> list[str]:
        return [item.content for item in self.attachments if item.kind == "image"]


def image_paths(text: str, cwd: Path) -> list[str]:
    """Only a paste consisting entirely of existing image paths is an attachment."""
    def resolve(value: str) -> Path:
        if value.startswith("file://"):
            url = urlparse(value)
            if url.netloc not in {"", "localhost"}:
                raise ValueError("Remote file URL")
            value = unquote(url.path)
        path = Path(value).expanduser()
        return (path if path.is_absolute() else cwd / path).resolve()

    try:
        # An unquoted single filename may itself contain spaces.
        candidates = [text.strip()]
        single = resolve(candidates[0])
        if not single.is_file():
            candidates = shlex.split(text)
        paths = [resolve(value) for value in candidates]
        if paths and all(path.suffix.lower() in IMAGE_SUFFIXES and path.is_file() for path in paths):
            return [str(path) for path in paths]
    except (OSError, ValueError, RuntimeError):
        pass
    return []
