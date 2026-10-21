"""Readable member colors and allocation without reusing an occupied color."""
from __future__ import annotations

import colorsys
import re
from dataclasses import dataclass
from itertools import count
from functools import lru_cache

from rich.color import Color


# Keep the original first five colors so existing rooms retain their appearance.
COLOR_CHOICES = (
    ("purple", "紫色", "#a9b6ff"),
    ("mint", "薄荷绿", "#78d8bc"),
    ("amber", "琥珀色", "#e8b878"),
    ("rose", "玫瑰色", "#e79ab5"),
    ("blue", "蓝色", "#83c8e8"),
    ("peach", "蜜桃色", "#efb19b"),
    ("lime", "青柠色", "#c3d787"),
    ("violet", "藤紫色", "#c7a5ec"),
    ("cyan", "青色", "#8ad8d9"),
    ("green", "绿色", "#9ed3a1"),
    ("coral", "珊瑚色", "#ed9f9a"),
    ("sand", "沙色", "#dacc9c"),
)
COLOR_NAMES = {alias: color for key, label, color in COLOR_CHOICES for alias in (key, label)}
COLOR_NAMES.update({"紫": "#a9b6ff", "蓝": "#83c8e8", "绿": "#9ed3a1", "粉": "#e79ab5", "pink": "#e79ab5"})


@lru_cache(maxsize=256)
def normalize_color(value: str) -> str:
    value = COLOR_NAMES.get(value.strip().lower(), value.strip().lower())
    if re.fullmatch(r"#[0-9a-f]{3}", value):
        value = "#" + "".join(char * 2 for char in value[1:])
    try:
        rgb = Color.parse(value).get_truecolor()
    except Exception as exc:
        raise ValueError("无效颜色，可使用 blue、purple、mint、rose 等颜色名，或 #RRGGBB。") from exc
    return f"#{rgb.red:02x}{rgb.green:02x}{rgb.blue:02x}"


def choose_color(used: list[str], requested: str | None = None) -> str:
    occupied = {normalize_color(color) for color in used}
    if requested and requested.lower() != "auto":
        color = normalize_color(requested)
        if color in occupied:
            raise ValueError("这个颜色已被其他 Codex 使用，请选择其他颜色，或留空自动分配。")
        return color
    for _, _, color in COLOR_CHOICES:
        if color not in occupied:
            return color
    # Continue with generated pastel colors after the named palette is exhausted.
    for index in count():
        rgb = colorsys.hls_to_rgb((index * 0.61803398875) % 1, 0.73, 0.6)
        color = "#" + "".join(f"{round(channel * 255):02x}" for channel in rgb)
        if color not in occupied:
            return color
    raise AssertionError("unreachable")


def mix(base: str, tint: str, amount: float) -> str:
    left, right = normalize_color(base), normalize_color(tint)
    return "#" + "".join(f"{round(int(left[i:i+2], 16) * (1 - amount) + int(right[i:i+2], 16) * amount):02x}"
                         for i in (1, 3, 5))


@dataclass(frozen=True)
class MemberColors:
    accent: str
    text: str
    background: str
    code_background: str
    muted: str


@lru_cache(maxsize=256)
def member_colors(color: str) -> MemberColors:
    # Lift dark custom colors so labels and prose remain readable on the dark UI.
    accent = normalize_color(color)
    rgb = [int(accent[i:i+2], 16) / 255 for i in (1, 3, 5)]
    hue, lightness, saturation = colorsys.rgb_to_hls(*rgb)
    if lightness < 0.65:
        accent = "#" + "".join(f"{round(channel * 255):02x}" for channel in colorsys.hls_to_rgb(hue, 0.72, saturation))
    background = mix("#151917", accent, 0.16)
    return MemberColors(accent, mix(accent, "#eef0e9", 0.25), background,
                        mix(background, "#000000", 0.14), mix(accent, "#788294", 0.55))
