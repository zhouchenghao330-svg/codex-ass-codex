"""Cache independent reply pieces, keeping completed tool output cheap to repaint."""
from __future__ import annotations

from dataclasses import replace

from rich.markdown import Markdown
from rich.text import Text
from textual.visual import Visual, visualize


class CachedVisual(Visual):
    def __init__(self, source: Visual) -> None:
        self.source = source
        self._height = None
        self._strips = None

    def get_optimal_width(self, rules, container_width):
        return self.source.get_optimal_width(rules, container_width)

    def get_height(self, rules, width):
        if self._height is None or self._height[0] != width:
            self._height = (width, self.source.get_height(rules, width))
        return self._height[1]

    def render_strips(self, width, height, style, options):
        key = (width, style)
        if self._strips is None or self._strips[0] != key:
            self._strips = (key, self.source.render_strips(width, None, style,
                replace(options, selection=None, selection_style=None)))
        return self._strips[1]


class ReplyVisual(Visual):
    def __init__(self, widget, renderables, previous: dict) -> None:
        self.nodes = {}
        self.visuals = []
        self._strips = None
        for item in renderables:
            if isinstance(item, Text):
                key = ("text", item.plain, item.style, tuple(item.spans))
            elif isinstance(item, Markdown):
                key = ("markdown", item.markup, item.style, getattr(item, "code_background", None))
            else:
                key = ("renderable", id(item))
            visual = previous.get(key)
            if visual is None:
                visual = CachedVisual(visualize(widget, item, markup=False))
            self.nodes[key] = visual
            self.visuals.append(visual)

    def get_optimal_width(self, rules, container_width):
        return max((visual.get_optimal_width(rules, container_width) for visual in self.visuals), default=0)

    def get_height(self, rules, width):
        return sum(visual.get_height(rules, width) for visual in self.visuals)

    def render_strips(self, width, height, style, options):
        key = (width, style)
        if self._strips is None or self._strips[0] != key:
            self._strips = (key, [strip for visual in self.visuals
                                 for strip in visual.render_strips(width, None, style, options)])
        strips = self._strips[1]
        return strips[:height] if height is not None and len(strips) > height else strips
