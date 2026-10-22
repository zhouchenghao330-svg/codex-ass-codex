"""Exercise real TUI interactions and capture representative terminal screens."""
from __future__ import annotations

import asyncio
import os
import xml.etree.ElementTree as ET
from pathlib import Path

os.environ.pop("NO_COLOR", None)
os.environ["COLORTERM"] = "truecolor"
os.environ["TERM"] = "xterm-256color"

from rich.cells import cell_len
from textual.widgets import Input

from codex_room.app import CodexRoom


def capture(app: CodexRoom, name: str, output: Path) -> None:
    """Correct Rich 15's SVG textLength for wide glyphs; terminal rendering is unaffected."""
    svg = app.export_screenshot()
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    root = ET.fromstring(svg)
    for text in root.iter("{http://www.w3.org/2000/svg}text"):
        content = "".join(text.itertext())
        if "textLength" in text.attrib and content:
            cells = cell_len(content)
            cell_width = float(text.attrib["textLength"]) / len(content)
            text.set("textLength", str(cells * cell_width))
            text.set("lengthAdjust", "spacingAndGlyphs")
    (output / name).write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")


async def preview() -> None:
    output = Path("artifacts")
    output.mkdir(exist_ok=True)
    app = CodexRoom(demo=True, speed=1.5)
    async with app.run_test(size=(132, 43)) as pilot:
        await pilot.pause()
        assert not app.query_one("#sidebar").display
        assert app.query_one("#timeline").region.y == 1
        assert app.query_one("#composer").region.height == 3
        capture(app, "preview.svg", output)

        # These members are explicit test fixtures, never startup defaults.
        await app.create_member("exp")
        await app.create_member("ops")
        await app.create_member("review")
        app.select("exp")

        # Picking a mention must never submit a message or resize the timeline.
        composer = app.query_one("#message-input", Input)
        entry_count = len(app.entries)
        timeline_region = app.query_one("#timeline").region
        composer.value = "@"
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        assert app.completion_open and app.completion_matches == ["exp", "ops", "review"]
        assert app.query_one("#timeline").region == timeline_region
        assert app.query_one("#completion-list").region.bottom <= app.query_one("#composer").region.y
        await pilot.press("down")
        assert app.query_one("#completion-list").highlighted == 1
        capture(app, "mentions.svg", output)
        await pilot.press("enter")
        assert composer.value == "@ops " and not app.completion_open
        assert composer.has_focus and len(app.entries) == entry_count

        composer.value = "@REV"
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        assert app.completion_matches == ["review"]
        await pilot.press("tab")
        assert composer.value == "@review "
        composer.value = "@"
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not app.completion_open and composer.value == "@"
        composer.value = "@zzzz"
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        assert app.completion_open and app.completion_matches == []
        await pilot.press("enter")
        assert len(app.entries) == entry_count and composer.value == "@zzzz"
        composer.value = "@o"
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        await pilot.click("#completion-list", offset=(3, 1))
        assert composer.value == "@ops " and not app.completion_open
        assert composer.has_focus and len(app.entries) == entry_count
        composer.value = ""
        composer.cursor_position = len(composer.value)

        composer.value = "/mem"
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        capture(app, "command-completion.svg", output)
        await pilot.press("tab")
        assert composer.value == "/members " and not app.sidebar_visible
        composer.value = "/focus "
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        capture(app, "argument-completion.svg", output)
        await pilot.press("down", "tab")
        assert composer.value == "/focus ops "
        composer.value = ""
        composer.cursor_position = 0

        # Use the actual composer and sidebar rather than bypassing their events.
        await pilot.click("#toggle-sidebar")
        await pilot.pause(0.1)
        assert app.query_one("#sidebar").display
        assert str(app.query_one("#toggle-sidebar").label) == "‹ 收起"
        capture(app, "sidebar.svg", output)
        await pilot.click("#member-ops")
        assert app.selected == "ops"
        assert not app.show_all
        await pilot.click("#all-messages")
        assert app.show_all
        await pilot.click("#toggle-sidebar")
        assert not app.query_one("#sidebar").display
        composer = app.query_one("#message-input", Input)
        composer.value = "检查 GPU 和实验进程"
        composer.cursor_position = len(composer.value)
        await pilot.press("enter")
        assert app.agents["ops"].busy
        assert not any(entry.body == "检查 GPU 和实验进程" for entry in app.agents["exp"].history)

        composer.value = "@exp 准备消融实验"
        composer.cursor_position = len(composer.value)
        await pilot.press("enter")
        assert app.agents["exp"].busy and app.agents["ops"].busy
        composer.value = "@exp 完成后再检查日志"
        composer.cursor_position = len(composer.value)
        await pilot.press("enter")
        steered = next(entry for entry in reversed(app.entries) if entry.sender == "you" and entry.target == "exp")
        assert steered.state == "steered"
        assert not app.agents["exp"].queue
        await pilot.pause(0.08)
        capture(app, "parallel.svg", output)

        composer.value = "/stop exp"
        composer.cursor_position = len(composer.value)
        await pilot.press("enter")
        assert not app.agents["exp"].busy
        assert not app.agents["exp"].queue
        assert steered.state == "steered"

        # An invalid recipient must preserve the draft, without dispatching it.
        composer.value = "@missing 这条消息不能丢失"
        composer.cursor_position = len(composer.value)
        await pilot.press("enter")
        assert composer.value == "@missing 这条消息不能丢失"
        composer.value = "@rev"
        composer.cursor_position = len(composer.value)
        await pilot.press("tab")
        assert composer.value == "@review "
        composer.value = ""
        composer.cursor_position = len(composer.value)

        await pilot.press("ctrl+n")
        await pilot.pause()
        app.screen.query_one("#new-name", Input).value = "writer"
        capture(app, "add-member.svg", output)
        await pilot.click("#confirm-add")
        await pilot.pause()
        assert app.selected == "writer"
        assert app.agents["writer"].history == []
        composer.value = "@wri"
        composer.cursor_position = len(composer.value)
        await pilot.pause()
        assert app.completion_matches == ["writer"]
        await pilot.press("enter")
        assert composer.value == "@writer "
        composer.value = ""
        composer.cursor_position = len(composer.value)

        await pilot.press("ctrl+l")
        assert not app.show_all
        for entry in app.entries:
            if entry.sender in {"exp", "ops"}:
                assert not app.cards[id(entry)].display

        await pilot.press("f1")
        await pilot.pause()
        await pilot.press("escape")

        # Stop + restart must not resend instructions already steered into the old turn.
        composer.value = "@exp 重新看一下结果"
        composer.cursor_position = len(composer.value)
        await pilot.press("enter")
        await asyncio.gather(*list(app.tasks.values()))
        assert not any(agent.busy for agent in app.agents.values())
        assert not any(entry.state == "working" for entry in app.entries)
        await pilot.pause()
        last_reply = next(entry for entry in reversed(app.entries) if entry.sender == "exp")
        assert "完成后再检查日志" not in last_reply.body
        assert app.cards[id(last_reply)].work_summary.display
        capture(app, "completed.svg", output)
        body = app.cards[id(last_reply)].body
        await pilot.mouse_down(body, offset=(0, 0))
        await pilot.hover(body, offset=(16, 0))
        await pilot.mouse_up(body, offset=(16, 0))
        await pilot.pause()
        selected = app.screen.get_selected_text()
        assert selected
        capture(app, "text-selection.svg", output)
        await pilot.press("ctrl+c")
        assert app.clipboard == selected and app.is_running
        await pilot.press("escape")
        assert composer.has_focus and not app.screen.get_selected_text()
        await pilot.double_click(body, offset=(6, 0))
        selected_word = app.screen.get_selected_text()
        assert selected_word and "\n" not in selected_word and "界面演示" not in selected_word
        capture(app, "word-selection.svg", output)
        await pilot.click(body, offset=(2, 2))
        assert composer.has_focus and not app.screen.get_selected_text()

        await pilot.resize_terminal(86, 26)
        await pilot.pause(0.1)
        app.scroll_bottom()
        await pilot.pause()
        assert app.query_one("#message-input").region.bottom <= 26
        assert app.query_one("#message-input").region.width > 75
        capture(app, "compact.svg", output)
        await pilot.click("#toggle-sidebar")
        await pilot.pause(0.1)
        app.scroll_bottom()
        await pilot.pause()
        member = app.query_one("#member-writer")
        assert app.query_one("#members").can_view_entire(member)
        capture(app, "compact-sidebar.svg", output)
        await pilot.click("#close-sidebar")
        assert not app.sidebar_visible
        print("PASS: routing, parallel replies, steer, stop/restart, completion, add-member, filters, selection/copy, help, resize, sidebar")
        print(f"Screenshots: {output.resolve()}")


if __name__ == "__main__":
    asyncio.run(preview())
