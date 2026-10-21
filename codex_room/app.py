from __future__ import annotations

import asyncio
import argparse
import json
import os
import re
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from time import monotonic

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as EventMessage
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Select, Static
from textual.widgets.option_list import Option

from .runtime import CodexClient, CodexError, clean_error
from .settings import ModelSettings, PermissionSettings, RuntimeApproval, RuntimeQuestion, PERMISSION_LABELS
from .storage import RoomStore
from .transcript import Entry, ReplyBlock, format_duration, render_reply
from .colors import COLOR_CHOICES, choose_color, member_colors, normalize_color
from .member_config import parse_add, resolve_directory


MUTED = "#788294"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def summary_title(summary: str) -> str:
    """Use only the headline of the server's public reasoning summary."""
    first_line = next((line.strip() for line in summary.splitlines() if line.strip()), "")
    return re.sub(r"^[#\s]+|[*`_]", "", first_line).strip()[:120]


def directory_label(directory: str, width: int) -> str:
    """Keep the end of a long path visible in a single reply header."""
    path = Path(directory)
    try:
        relative = path.relative_to(Path.home())
        label = "~" if relative == Path(".") else f"~/{relative}"
    except ValueError:
        label = str(path)
    if width < 1:
        return ""
    if cell_len(label) > width:
        while label and cell_len(label) > width - 1:
            label = label[1:]
        return f"…{label}"
    return label


@dataclass
class Agent:
    name: str
    label: str
    color: str
    busy: bool = False
    phase: str = "等待你的消息"
    queue: deque[Entry] = field(default_factory=deque)
    history: list[Entry] = field(default_factory=list)
    thread_id: str | None = None
    model: str = ""
    effort: str = ""
    permission: str = "full"
    legacy_instructions: bool = False
    cwd: str = ""

    @property
    def accent(self) -> str:
        return member_colors(self.color).accent


class Member(Static):
    class Selected(EventMessage):
        def __init__(self, name: str) -> None:
            self.name = name
            super().__init__()

    def __init__(self, agent: Agent) -> None:
        super().__init__(id=f"member-{agent.name}", classes="member")
        self.agent = agent
        self.tooltip = agent.cwd

    def refresh_member(self, selected: bool, frame: str) -> None:
        agent = self.agent
        status = f"{frame} 工作中" if agent.busy else "● 空闲"
        color = "#e8b878" if agent.busy else "#78d8bc"
        text = Text()
        text.append(f"{'▸' if selected else ' '} {agent.label}", style=f"bold {agent.accent}")
        text.append(f"  @{agent.name}\n", style=MUTED)
        text.append(f"  {status}", style=color)
        if agent.queue:
            text.append(f"  +{len(agent.queue)} 待处理", style="#e8b878")
        if agent.cwd:
            text.append(f"\n  ⌂ {Path(agent.cwd).name or agent.cwd}", style=MUTED)
        self.set_class(selected, "selected")
        self.update(text)

    def on_click(self) -> None:
        self.post_message(self.Selected(self.agent.name))


class MessageCard(Vertical):
    def __init__(self, entry: Entry, agents: dict[str, Agent]) -> None:
        super().__init__(classes=f"message {entry.sender if entry.sender in ('you', 'system') else 'agent'}")
        self.entry = entry
        self.agents = agents
        self.reply_colors = member_colors(agents[entry.sender].color) if entry.sender in agents else None
        if self.reply_colors:
            self.styles.background = self.reply_colors.background
        self.tools_expanded = False
        self.started_at = monotonic()
        self.animation_frame = 0
        self.header = Static(classes="message-header")
        self.body = Static(classes="message-body")
        self.progress = Static(classes="working-status")
        self.work_summary = Static(classes="work-summary")
        self.refresh_entry()

    def compose(self) -> ComposeResult:
        yield self.header
        yield self.body
        yield self.progress
        yield self.work_summary

    def refresh_completion(self) -> None:
        entry = self.entry
        visible = (entry.sender in self.agents and entry.state in {"done", "stopped", "failed"}
                   and entry.finished_at is not None and entry.duration_seconds is not None)
        self.work_summary.display = visible
        if visible:
            finished = datetime.fromisoformat(entry.finished_at)
            label = {"done": "Worked for", "stopped": "Stopped after", "failed": "Failed after"}[entry.state]
            self.work_summary.update(Text(f"{label} {format_duration(entry.duration_seconds)} · {finished:%H:%M}", style=MUTED))
            self.work_summary.tooltip = finished.strftime("%Y-%m-%d %H:%M:%S %z")

    def refresh_working(self, frame: int | None = None) -> None:
        if frame is not None:
            self.animation_frame = frame
        active = self.entry.state == "working" and self.entry.sender in self.agents
        self.progress.display = active
        if not active:
            return
        elapsed = int(monotonic() - self.started_at)
        duration = f"{elapsed // 60}m {elapsed % 60:02d}s" if elapsed >= 60 else f"{elapsed}s"
        prefix = Text(f"{SPINNER[self.animation_frame % len(SPINNER)]} Working", style=self.reply_colors.accent)
        suffix = f" · {duration}"
        phase = Text(f" · {self.agents[self.entry.sender].phase}", style=self.reply_colors.muted)
        width = self.content_size.width or 80
        phase.truncate(max(0, width - prefix.cell_len - Text(suffix).cell_len), overflow="ellipsis")
        prefix.append_text(phase)
        prefix.append(suffix, style=MUTED)
        self.progress.update(prefix)

    def on_resize(self) -> None:
        self.refresh_entry()

    def refresh_entry(self) -> None:
        entry = self.entry
        text = Text()
        if entry.sender == "system":
            text.append("◇  ", style="#788294")
            text.append(entry.body, style=MUTED)
        else:
            if entry.sender == "you":
                text.append("› ", style="bold #e8edf5")
                target = self.agents[entry.target]
                text.append(f"@{target.name}", style=target.accent)
            else:
                agent = self.agents[entry.sender]
                text.append(agent.label, style=f"bold {self.reply_colors.accent}")
                text.append(f"  @{agent.name}", style=self.reply_colors.muted)
            if entry.state == "queued":
                text.append("   ◷ 待处理", style="#e8b878")
            elif entry.state == "stopped":
                text.append("   · 已停止", style=MUTED)
            elif entry.state == "failed":
                text.append("   · 失败", style="#e8b878")
            if entry.sender == "you":
                text.append("  ")
                text.append(entry.body, style="#c5cdd9")
            else:
                if agent.cwd:
                    separator = "   · ⌂ "
                    width = self.content_size.width or 80
                    directory = directory_label(agent.cwd, width - text.cell_len - cell_len(separator))
                    if directory:
                        text.append(f"{separator}{directory}", style=MUTED)
                    self.header.tooltip = agent.cwd
                self.header.update(text)
                self.body.display = bool(entry.body or entry.blocks or entry.state != "working")
                self.body.update(render_reply(entry, self.tools_expanded, self.content_size.width or 80, agent.color))
                self.refresh_working()
                self.refresh_completion()
                return
        self.header.update(text)
        self.body.display = False
        self.progress.display = False
        self.work_summary.display = False


class AddMember(ModalScreen[tuple[str, str, str] | None]):
    BINDINGS = [("escape", "cancel", "返回")]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="add-member-dialog"):
            yield Static("[b]拉一个 Codex 进来[/b]", id="dialog-title")
            yield Static("独立会话，从你发给它的第一条消息开始。", classes="muted")
            yield Static("名字 · 用于 @ 点名", classes="field-label")
            yield Input(placeholder="例如 a 或 codex1", id="new-name", max_length=20)
            yield Static("工作目录 · 留空沿用群聊目录", classes="field-label")
            yield Input(placeholder=str(self.app.cwd), id="new-cwd")
            yield Static("颜色 · 只显示未占用的颜色", classes="field-label")
            used = {normalize_color(agent.color) for agent in self.app.agents.values()}
            options = [("自动分配", "auto")] + [(Text(f"● {label} · {key}", style=color), color)
                                               for key, label, color in COLOR_CHOICES if color not in used]
            yield Select(options, value="auto", allow_blank=False, id="new-color", compact=True)
            yield Static("", id="color-preview")
            yield Static("", id="dialog-error")
            with Horizontal(id="dialog-actions"):
                yield Button("取消", id="cancel-add")
                yield Button("加入对话", id="confirm-add", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#new-name", Input).focus()
        self.refresh_preview()

    def refresh_preview(self) -> None:
        color = choose_color([agent.color for agent in self.app.agents.values()], str(self.query_one("#new-color", Select).value))
        colors = member_colors(color)
        name = self.query_one("#new-name", Input).value.strip() or "Codex"
        preview = self.query_one("#color-preview", Static)
        preview.styles.background = colors.background
        preview.update(Text(f"● {name}  ·  回复颜色预览", style=colors.text))

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "new-color" and self.is_mounted:
            self.refresh_preview()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "new-name" and self.is_mounted:
            self.refresh_preview()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def confirm(self) -> None:
        name = self.query_one("#new-name", Input).value.strip()
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,19}", name):
            self.query_one("#dialog-error", Static).update("请用英文字母开头，可包含数字、_ 和 -。")
            return
        app = self.app
        if name.lower() in app.agents or name.lower() in {"all", "you", "system"}:
            self.query_one("#dialog-error", Static).update("这个名字已经被占用，换一个试试。")
            return
        try:
            cwd = resolve_directory(self.query_one("#new-cwd", Input).value.strip(), app.cwd)
        except (ValueError, OSError) as exc:
            self.query_one("#dialog-error", Static).update(Text(str(exc)))
            return
        self.dismiss((name.lower(), str(self.query_one("#new-color", Select).value), str(cwd)))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.confirm()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-add":
            self.confirm()
        else:
            self.dismiss(None)


class Help(ModalScreen[None]):
    BINDINGS = [("escape", "close", "返回"), ("f1", "close", "返回")]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog"):
            yield Static("[b]一个终端，几位 Codex。[/b]", id="dialog-title")
            yield Static(
                "[b #a9b6ff]@a 你的消息[/]       发给指定成员\n"
                "[b #a9b6ff]直接输入[/]           发给左侧选中的成员\n"
                "[b #a9b6ff]/add 名字 [目录][/]    新建 Codex，省略目录则沿用群聊目录\n"
                "[b #a9b6ff]--color blue[/]        创建时指定颜色\n"
                "[b #a9b6ff]/focus 名字[/]         切到该成员的单人对话\n"
                "[b #a9b6ff]/members[/]            展开 / 收起成员列表\n"
                "[b #a9b6ff]/stop 名字[/]          停止回复，并取消该成员的排队消息\n"
                "[b #a9b6ff]/model[/]              选择模型和推理强度\n"
                "[b #a9b6ff]/permissions[/]        选择权限，默认 Full Access\n"
                "[b #a9b6ff]@a /model[/]           调整指定成员的设置\n"
                "[b #a9b6ff]/help[/]               打开此帮助\n\n"
                "[b]Ctrl+1 / 2 / 3[/] 切换成员   [b]Ctrl+L[/] 全部 / 单人视图\n"
                "[b]Ctrl+N[/] 添加成员           [b]@[/] 打开成员选择\n"
                "[b]↑ / ↓[/] 选择成员           [b]Tab / Enter[/] 确认\n"
                "[b]Ctrl+B[/] 展开 / 收起成员\n"
                "[b]Ctrl+T[/] 展开 / 收起工具调用详情\n"
                "[b]Ctrl+Q[/] 退出               [b]Esc[/] 回到输入框\n\n"
                "[#788294]默认连接本机 Codex，沿用登录和项目配置。\n"
                "成员会话和配置保存在项目的 .codex-room/ 中。\n"
                "uv run codex-room --demo 可以进入纯界面演示。[/]",
                id="help-copy",
            )
            yield Button("明白了，继续聊", id="close-help", variant="primary")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self) -> None:
        self.dismiss(None)


class MentionList(OptionList, can_focus=False):
    """A mouse-selectable popup which leaves keyboard focus in the composer."""


class ComposerInput(Input):
    """Navigate mentions while retaining normal input behavior outside the popup."""

    async def _on_key(self, event: events.Key) -> None:
        app = self.app
        if event.key in {"up", "down", "tab", "escape"}:
            app.refresh_mentions()
        if app.mention_open and event.key in {"up", "down", "tab", "escape"}:
            menu = app.room_screen.query_one("#mention-list", MentionList)
            if event.key == "up":
                menu.action_cursor_up()
            elif event.key == "down":
                menu.action_cursor_down()
            elif event.key == "tab":
                app.accept_mention()
            else:
                app.close_mentions()
                app.refresh_recipient()
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)

    async def action_submit(self) -> None:
        self.app.refresh_mentions()
        if self.app.mention_open:
            self.app.accept_mention()
        else:
            await super().action_submit()

    def _on_blur(self, event: events.Blur) -> None:
        super()._on_blur(event)
        if hasattr(self.app, "room_screen"):
            self.app.close_mentions()


class CodexRoom(App):
    TITLE = "Codex Room"
    SUB_TITLE = "一个终端，几位 Codex"
    CSS_PATH = "room.tcss"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("ctrl+c", "quit", "退出", priority=True, show=False),
        Binding("ctrl+n", "add_member", "添加成员", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "成员", priority=True),
        Binding("ctrl+l", "toggle_view", "全部 / 单人", priority=True),
        Binding("ctrl+t", "toggle_tools", "工具详情", priority=True),
        Binding("ctrl+1", "select_index(0)", "成员 1", priority=True, show=False),
        Binding("ctrl+2", "select_index(1)", "成员 2", priority=True, show=False),
        Binding("ctrl+3", "select_index(2)", "成员 3", priority=True, show=False),
        Binding("f1", "help", "帮助", priority=True),
        Binding("escape", "composer", "输入", show=False),
    ]

    def __init__(self, *, demo: bool = False, speed: float = 1.0,
                 cwd: Path | None = None, state_dir: Path | None = None) -> None:
        if os.name == "posix":
            from .terminal import RoomDriver
            super().__init__(driver_class=RoomDriver)
        else:
            super().__init__()
        self.agents: dict[str, Agent] = {}
        self.selected = ""
        self.show_all = True
        self.sidebar_visible = False
        self.tools_expanded = False
        self.mention_open = False
        self.mention_matches: list[str] = []
        self.mention_signature: tuple = ()
        self.dismissed_mention: str | None = None
        self.entries: list[Entry] = []
        self.cards: dict[int, MessageCard] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.active_entries: dict[str, tuple[Entry, Entry]] = {}
        self.frame = 0
        self.speed = speed
        self.demo = demo
        self.cwd = (cwd or Path.cwd()).resolve()
        self.client: CodexClient | None = None
        self.connected = demo
        self.connection_error = ""
        self.connection_task: asyncio.Task | None = None
        self.defaults = {"model": "", "effort": ""}
        self.store = None if demo else RoomStore(state_dir or self.cwd / ".codex-room")
        self.saved = self.store.load() if self.store else {}
        self.dirty = False
        self.shutting_down = False
        self.request_lock = asyncio.Lock()
        self.turn_permissions: dict[str, str] = {}
        if self.saved:
            self.agents = {item["name"]: Agent(**item) for item in self.saved.get("agents", [])}
            used = []
            for agent in self.agents.values():
                cwd = str(resolve_directory(agent.cwd, self.cwd, validate=False))
                self.dirty |= agent.cwd != cwd
                agent.cwd = cwd
                try:
                    color = choose_color(used, agent.color)
                except ValueError:
                    color = choose_color(used)
                self.dirty |= agent.color != color
                agent.color = color
                used.append(color)
            self.selected = self.saved.get("selected", "")
            self.show_all = self.saved.get("show_all", True)
            if self.selected not in self.agents:
                self.selected = next(iter(self.agents), "")

    def compose(self) -> ComposeResult:
        with Horizontal(id="workspace"):
            with Vertical(id="sidebar"):
                with Horizontal(id="members-heading"):
                    yield Static("成员", id="members-label")
                    yield Static(f"{len(self.agents):02d}", id="member-count")
                    yield Button("‹", id="close-sidebar")
                yield Button("全部消息", id="all-messages", tooltip="显示所有成员的对话 · Ctrl+L 切换全部 / 单人")
                with VerticalScroll(id="members"):
                    for agent in self.agents.values():
                        yield Member(agent)
                yield Button("＋ 添加", id="add-member")
            with Vertical(id="main"):
                with VerticalScroll(id="timeline"):
                    yield Static("输入 /add 名字 或按 Ctrl+N 创建 Codex", id="empty-room", classes="muted")
                yield MentionList(id="mention-list")
                with Horizontal(id="composer"):
                    yield Static("›", id="prompt")
                    yield ComposerInput(placeholder="说点什么，或 @名字…", id="message-input", select_on_focus=False)
                with Horizontal(id="footer"):
                    yield Static("", id="recipient")
                    yield Button("成员", id="toggle-sidebar")
                    yield Static("Ctrl+B · F1 帮助", id="connection")

    async def on_mount(self) -> None:
        self.room_screen = self.screen
        self.theme = "textual-dark"
        for item in self.saved.get("entries", []):
            entry = Entry(**item)
            if entry.sender == "system" and not entry.target:
                for name in self.agents:
                    if entry.body.startswith((f"{name} 已加入", f"@{name} ", f"@{name}的")):
                        entry.target = name
                        break
            entry.restore_legacy_tools()
            if entry.state in {"working", "queued"}:
                entry.state = "stopped"
                entry.finish_tools("stopped")
                if not entry.body:
                    entry.body = "上次回复未完成。"
            await self.add_entry(entry)
        self.set_interval(0.15, self.tick)
        self.set_interval(1, self.save_state)
        if not self.demo:
            self.connection_task = asyncio.create_task(self.connect_codex())
        self.refresh_chrome()
        self.query_one("#message-input", Input).focus()
        self.call_after_refresh(self.scroll_bottom)

    def on_app_focus(self) -> None:
        if hasattr(self, "room_screen") and self.screen is self.room_screen:
            self.action_composer()

    def on_click(self, event: events.Click) -> None:
        if self.screen is not getattr(self, "room_screen", None) or event.button != 1:
            return
        widget = event.widget
        if widget and any(isinstance(node, (Input, Button, Select, OptionList, Member))
                          for node in (widget, *widget.ancestors)):
            return
        if event.style and (event.style.link or event.style.meta.get("@click")):
            return
        if not self.screen.get_selected_text():
            self.action_composer()

    def on_key(self, event: events.Key) -> None:
        if self.screen is not getattr(self, "room_screen", None) or not event.is_printable:
            return
        composer = self.room_screen.query_one("#message-input", Input)
        if not composer.has_focus:
            self.screen.clear_selection()
            composer.focus()
            composer.post_message(event.copy())
            event.prevent_default()
            event.stop()

    def on_paste(self, event: events.Paste) -> None:
        if self.screen is not getattr(self, "room_screen", None):
            return
        composer = self.room_screen.query_one("#message-input", Input)
        if not composer.has_focus:
            self.screen.clear_selection()
            composer.focus()
            composer.post_message(events.Paste(event.text))
            event.prevent_default()
            event.stop()

    def on_resize(self, event: events.Resize) -> None:
        screen = getattr(self, "room_screen", self.screen)
        screen.set_class(event.size.width < 100, "narrow")
        screen.set_class(event.size.height < 34, "compact")
        if hasattr(self, "room_screen"):
            self.set_timer(0.05, self.reveal_selected)

    def reveal_selected(self) -> None:
        if not self.sidebar_visible or self.selected not in self.agents:
            return
        member = self.room_screen.query_one(f"#member-{self.selected}")
        self.room_screen.query_one("#members", VerticalScroll).scroll_to_widget(
            member, animate=False, origin_visible=False, immediate=True
        )

    def scroll_bottom(self) -> None:
        self.room_screen.query_one("#timeline", VerticalScroll).scroll_end(animate=False)

    def tick(self) -> None:
        if not self.is_running or self.shutting_down:
            return
        self.frame += 1
        for _, answer in self.active_entries.values():
            card = self.cards.get(id(answer))
            if card and card.display:
                card.refresh_working(self.frame)
        if self.mention_open:
            self.refresh_mentions()
        self.refresh_chrome()

    def close_mentions(self) -> None:
        self.mention_open = False
        self.dismissed_mention = self.room_screen.query_one("#message-input", Input).value
        self.room_screen.query_one("#mention-list", MentionList).display = False

    def refresh_mentions(self) -> None:
        composer = self.room_screen.query_one("#message-input", Input)
        menu = self.room_screen.query_one("#mention-list", MentionList)
        match = re.fullmatch(r"@([^\s]*)", composer.value)
        if not match or not composer.has_focus or composer.value == self.dismissed_mention:
            menu.display = False
            self.mention_open = False
            if not match:
                self.dismissed_mention = None
            return
        query = match.group(1).casefold()
        matches = [
            name for name, agent in self.agents.items()
            if name.casefold().startswith(query) or agent.label.casefold().startswith(query)
        ]
        signature = tuple((name, self.agents[name].busy, len(self.agents[name].queue)) for name in matches)
        if signature != self.mention_signature or not menu.option_count:
            old_name = None
            if menu.highlighted is not None and menu.highlighted < len(self.mention_matches):
                old_name = self.mention_matches[menu.highlighted]
            options = []
            for name in matches:
                agent = self.agents[name]
                row = Text(f"@{name:<12}", style=agent.accent)
                if agent.label != name:
                    row.append(f"  {agent.label}", style="#d1d4cd")
                row.append("  工作中" if agent.busy else "  空闲", style="#e8b878" if agent.busy else MUTED)
                if agent.queue:
                    row.append(f" +{len(agent.queue)}", style="#e8b878")
                options.append(Option(row, id=name))
            menu.set_options(options or [Option("没有匹配的 Codex" if self.agents else "先用 /add 名字 创建 Codex", disabled=True)])
            menu.highlighted = matches.index(old_name) if old_name in matches else (0 if matches else None)
            self.mention_signature = signature
        self.mention_matches = matches
        height = min(8, max(1, len(matches)) + 2)
        menu.styles.height = height
        menu.styles.offset = (0, -height)
        menu.display = True
        self.mention_open = True

    def accept_mention(self, name: str | None = None) -> None:
        menu = self.room_screen.query_one("#mention-list", MentionList)
        if name is None:
            if menu.highlighted is None or not self.mention_matches:
                return
            name = self.mention_matches[menu.highlighted]
        composer = self.room_screen.query_one("#message-input", Input)
        self.close_mentions()
        composer.value = f"@{name} "
        composer.cursor_position = len(composer.value)
        self.select(name)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id == "mention-list" and event.option_id:
            event.stop()
            self.accept_mention(event.option_id)

    def refresh_chrome(self) -> None:
        if not self.is_running or not self.is_mounted or self.shutting_down:
            return
        for member in self.room_screen.query(Member):
            member.refresh_member(not self.show_all and member.agent.name == self.selected, SPINNER[self.frame % len(SPINNER)])
        all_messages = self.room_screen.query_one("#all-messages", Button)
        all_messages.label = "▸ 全部消息" if self.show_all else "  全部消息"
        all_messages.set_class(self.show_all, "selected")
        self.room_screen.query_one("#member-count", Static).update(f"{len(self.agents):02d}")
        self.room_screen.query_one("#empty-room", Static).display = not self.agents
        self.refresh_recipient()
        state = "演示" if self.demo else ("已连接" if self.connected else ("离线" if self.connection_error else "连接中"))
        self.room_screen.query_one("#connection", Static).update(f"Ctrl+B · F1 帮助 · {state}")

    def refresh_recipient(self) -> None:
        value = self.room_screen.query_one("#message-input", Input).value
        match = re.match(r"^@([\w-]+)(?:\s|$)", value)
        name = match.group(1).lower() if match else self.selected
        recipient = Text()
        if name in self.agents:
            agent = self.agents[name]
            recipient.append(f"@{name}", style=agent.accent)
            recipient.append(" · 排队发送" if agent.busy else " · 空闲", style=MUTED)
            recipient.append(f" · ⌂ {Path(agent.cwd).name or agent.cwd}", style=MUTED)
            self.room_screen.query_one("#recipient", Static).tooltip = f"工作目录：{agent.cwd}"
            if not self.demo:
                recipient.append(f" · {agent.model or '默认模型'} · {agent.effort or '默认强度'} · {PERMISSION_LABELS[agent.permission]}", style=MUTED)
            recipient.append(" · 全部消息" if self.show_all else " · 单人对话", style=MUTED)
            busy = sum(member.busy for member in self.agents.values())
            if busy:
                recipient.append(f" · {busy} 人工作中", style="#e8b878")
        else:
            recipient.append(f"未找到 @{name} · 可以先 /add {name}" if self.agents else "暂无 Codex · /add 名字 或 Ctrl+N 创建", style=MUTED)
        if value.startswith("/"):
            recipient = Text("/model  /permissions  /add  /focus  /members  /stop  /help", style=MUTED)
        elif self.mention_open:
            recipient = Text("↑↓ 选择 · Tab / Enter 确认 · Esc 关闭", style=MUTED)
        self.room_screen.query_one("#recipient", Static).update(recipient)
        self.room_screen.query_one("#message-input", Input).placeholder = f"给 @{self.selected} 发消息，或 @名字…" if self.agents else "输入 /add 名字 创建 Codex…"

    async def add_entry(self, entry: Entry) -> MessageCard:
        self.entries.append(entry)
        self.dirty = True
        if entry.sender == "you":
            self.agents[entry.target].history.append(entry)
        elif entry.sender in self.agents:
            self.agents[entry.sender].history.append(entry)
        card = MessageCard(entry, self.agents)
        card.tools_expanded = self.tools_expanded
        card.refresh_entry()
        self.cards[id(entry)] = card
        card.display = self.entry_visible(entry)
        timeline = self.room_screen.query_one("#timeline", VerticalScroll)
        follow = timeline.is_vertical_scroll_end
        await timeline.mount(card)
        if follow or entry.sender == "you":
            self.call_after_refresh(self.scroll_bottom)
        return card

    def entry_visible(self, entry: Entry) -> bool:
        return (self.show_all or entry.sender == self.selected or entry.target == self.selected
                or (entry.sender == "system" and not entry.target))

    def apply_filter(self) -> None:
        for entry in self.entries:
            self.cards[id(entry)].display = self.entry_visible(entry)
        self.refresh_chrome()
        self.call_after_refresh(self.scroll_bottom)

    def select(self, name: str, *, single: bool = False) -> None:
        self.selected = name
        if single:
            self.show_all = False
        self.dirty = True
        self.apply_filter()
        self.set_timer(0.05, self.reveal_selected)
        if not isinstance(self.screen, ModalScreen):
            self.action_composer()

    def on_member_selected(self, event: Member.Selected) -> None:
        self.select(event.name, single=True)

    def action_select_index(self, index: int) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        names = list(self.agents)
        if index < len(names):
            self.select(names[index], single=True)

    def action_composer(self) -> None:
        self.room_screen.query_one("#message-input", Input).focus()

    def action_toggle_view(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.show_all = not self.show_all
        self.dirty = True
        self.apply_filter()
        self.action_composer()

    def action_all_messages(self) -> None:
        self.show_all = True
        self.dirty = True
        self.apply_filter()
        self.action_composer()

    def action_toggle_tools(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        timeline = self.room_screen.query_one("#timeline", VerticalScroll)
        follow, scroll_y = timeline.is_vertical_scroll_end, timeline.scroll_y
        self.tools_expanded = not self.tools_expanded
        for card in self.cards.values():
            if card.entry.blocks:
                card.tools_expanded = self.tools_expanded
                card.refresh_entry()
        if follow:
            self.call_after_refresh(self.scroll_bottom)
        else:
            self.call_after_refresh(lambda: timeline.scroll_to(y=scroll_y, animate=False))

    def action_help(self) -> None:
        if isinstance(self.screen, Help):
            self.screen.dismiss(None)
        elif not isinstance(self.screen, ModalScreen):
            self.push_screen(Help())

    def action_toggle_sidebar(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.sidebar_visible = not self.sidebar_visible
        self.room_screen.query_one("#sidebar").display = self.sidebar_visible
        if self.sidebar_visible:
            self.set_timer(0.05, self.reveal_selected)
        self.action_composer()

    def action_add_member(self) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.push_screen(AddMember(), self.member_added)

    async def member_added(self, result: tuple[str, str, str] | None) -> None:
        if result:
            await self.create_member(*result)
        self.action_composer()

    async def inherit_defaults(self, agent: Agent) -> None:
        if agent.model and agent.effort:
            return
        if agent.cwd == str(self.cwd):
            defaults = self.defaults
        elif self.client and self.connected:
            result = await self.client.request("config/read", {"cwd": agent.cwd, "includeLayers": False})
            config = result.get("config", {})
            defaults = {"model": config.get("model") or "", "effort": config.get("model_reasoning_effort") or ""}
        else:
            return
        agent.model = agent.model or defaults["model"]
        agent.effort = agent.effort or defaults["effort"]

    async def create_member(self, name: str, color: str | None = None,
                            cwd: str | Path | None = None) -> bool:
        try:
            directory = resolve_directory(cwd, self.cwd)
            color = choose_color([agent.color for agent in self.agents.values()], color)
            agent = Agent(name, name, color, cwd=str(directory))
            await self.inherit_defaults(agent)
        except (ValueError, OSError, CodexError) as exc:
            self.notify(str(exc), severity="warning")
            return False
        self.agents[name] = agent
        member = Member(agent)
        await self.room_screen.query_one("#members", VerticalScroll).mount(member)
        self.select(name)
        await self.add_entry(Entry("system", f"{name} 已加入 · 工作目录：{agent.cwd}", target=name))
        self.save_state()
        return True

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "message-input":
            self.refresh_mentions()
            self.refresh_recipient()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "message-input":
            return
        value = event.value.strip()
        if not value:
            return
        accepted = await self.submit(value)
        if accepted:
            event.input.value = ""
        self.refresh_chrome()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-member":
            self.action_add_member()
        elif event.button.id == "all-messages":
            self.action_all_messages()
        elif event.button.id in {"toggle-sidebar", "close-sidebar"}:
            self.action_toggle_sidebar()

    async def submit(self, value: str) -> bool:
        if value.startswith("/"):
            return await self.command(value)
        name, body = self.selected, value
        if value.startswith("@"):
            match = re.match(r"^@([a-zA-Z0-9_-]+)\s+(.+)$", value, re.DOTALL)
            if not match:
                self.notify("写上 @名字 和要发送的内容。", severity="warning")
                return False
            name, body = match.group(1).lower(), match.group(2).strip()
        if name not in self.agents:
            self.notify(f"没有 @{name}，可以用 /add {name} 创建。" if name else "先用 /add 名字 或 Ctrl+N 创建 Codex。", severity="warning")
            return False
        if body.startswith("/"):
            accepted = await self.command(body, target=name)
            if accepted:
                self.select(name)
            return accepted
        if not self.connected:
            self.notify("Codex 尚未连接；连接失败时可用 /reconnect 重试。", severity="warning")
            return False
        # Routing is explicit: mentioning another member later in a message is just text.
        agent = self.agents[name]
        entry = Entry("you", body, name, "queued" if agent.busy else "done")
        await self.add_entry(entry)
        agent.queue.append(entry)
        if self.selected != name:
            self.select(name)
        if not agent.busy:
            self.start_agent(name)
        self.refresh_chrome()
        return True

    async def command(self, value: str, target: str | None = None) -> bool:
        parts = value.split(maxsplit=2)
        command = parts[0].lower()
        target = target or self.selected
        if command in {"/model", "/permissions", "/permission", "/permisson"} and target not in self.agents:
            self.notify("先用 /add 名字 创建 Codex，再调整它的设置。", severity="warning")
            return False
        if command == "/add":
            try:
                options = parse_add(value, self.cwd)
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return False
            if options is None:
                self.action_add_member()
            else:
                name = options.name
                if not re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", name) or name in self.agents or name in {"all", "you", "system"}:
                    self.notify("名字需要唯一，使用英文字母、数字、_ 或 -，以字母开头，最多 20 位。", severity="warning")
                    return False
                return await self.create_member(name, color=options.color, cwd=options.cwd)
        elif command in {"/focus", "/stop"}:
            name = parts[1].lstrip("@").lower() if len(parts) > 1 else target
            if name not in self.agents:
                self.notify(f"没有 @{name}。" if name else "请先创建 Codex。", severity="warning")
                return False
            if command == "/focus":
                self.select(name, single=True)
            else:
                await self.stop_agent(name)
        elif command == "/demo":
            self.notify("演示请用 --demo 启动，再自己创建 Codex。", severity="warning")
            return False
        elif command == "/help":
            self.action_help()
        elif command == "/members":
            self.action_toggle_sidebar()
        elif command == "/model":
            return await self.configure_model(target, parts[1:])
        elif command in {"/permissions", "/permission", "/permisson"}:
            if len(parts) > 1:
                mode = {"full-access": "full", "workspace-write": "workspace"}.get(parts[1], parts[1])
                if mode not in PERMISSION_LABELS:
                    self.notify("权限选项：full / workspace / read-only", severity="warning")
                    return False
                await self.change_permission(target, mode)
            else:
                self.push_screen(PermissionSettings(target, self.agents[target].permission),
                                 lambda result: self.change_permission(target, result))
        elif command == "/reconnect":
            if any(agent.busy for agent in self.agents.values()):
                self.notify("请先停止工作中的成员，再重新连接。", severity="warning")
                return False
            if self.connection_task and not self.connection_task.done():
                return True
            self.connection_task = asyncio.create_task(self.connect_codex())
        elif command == "/quit":
            self.exit()
        else:
            self.notify("还没有这个指令。输入 /help 查看用法。", severity="warning")
            return False
        return True

    async def configure_model(self, name: str, arguments: list[str]) -> bool:
        if not self.client or not self.connected:
            self.notify("模型列表需要连接真实 Codex 后读取。", severity="warning")
            return False
        if arguments:
            model = arguments[0]
            effort = arguments[1] if len(arguments) > 1 else ""
            return await self.change_model(name, (model, effort))
        if not self.client.models:
            self.notify("Codex 没有返回模型列表，可用 /model 模型名 推理强度 指定。", severity="warning")
            return False
        agent = self.agents[name]
        self.push_screen(ModelSettings(name, self.client.models, agent.model, agent.effort),
                         lambda result: self.change_model(name, result))
        return True

    async def change_model(self, name: str, result: tuple[str, str] | None) -> bool:
        if result is None:
            return True
        model, effort = result
        catalog = next((item for item in self.client.models if item["id"] == model), {}) if self.client else {}
        supported = [item["reasoningEffort"] for item in catalog.get("supportedReasoningEfforts", [])]
        effort = effort or catalog.get("defaultReasoningEffort") or self.agents[name].effort
        if supported and effort not in supported:
            self.notify(f"{model} 支持的推理强度：{', '.join(supported)}", severity="warning")
            return False
        if effort and effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            self.notify("无效的推理强度。", severity="warning")
            return False
        self.agents[name].model, self.agents[name].effort = model, effort
        await self.add_entry(Entry("system", f"@{name} · {model} · {effort or '默认强度'} · 下一轮生效", target=name))
        self.save_state()
        self.refresh_chrome()
        self.action_composer()
        return True

    async def change_permission(self, name: str, mode: str | None) -> None:
        if mode is None:
            return
        self.agents[name].permission = mode
        await self.add_entry(Entry("system", f"@{name} · {PERMISSION_LABELS[mode]} · 下一轮生效", target=name))
        self.save_state()
        self.refresh_chrome()
        self.action_composer()

    async def connect_codex(self) -> None:
        self.connected = False
        self.connection_error = ""
        try:
            if self.client:
                await self.client.close()
            self.client = CodexClient(self.cwd)
            await self.client.start()
            self.defaults = {"model": self.client.config.get("model") or "", "effort": self.client.config.get("model_reasoning_effort") or ""}
            for agent in self.agents.values():
                if agent.cwd == str(self.cwd):
                    await self.inherit_defaults(agent)
            self.connected = True
            self.dirty = True
            self.save_state()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.connection_error = clean_error(exc)
            await self.add_entry(Entry("system", f"Codex 连接失败：{clean_error(exc)} · /reconnect 重试"))
        self.refresh_chrome()

    def save_state(self) -> None:
        if not self.store or not self.dirty:
            return
        members = []
        for agent in self.agents.values():
            members.append({key: getattr(agent, key) for key in (
                "name", "label", "color", "thread_id", "model", "effort", "permission", "legacy_instructions", "cwd",
            )})
        try:
            self.store.save({"version": 2, "selected": self.selected, "show_all": self.show_all, "agents": members,
                             "entries": [asdict(entry) for entry in self.entries]})
            self.dirty = False
        except OSError as exc:
            self.notify(f"保存群聊失败：{exc}", severity="error")

    async def dialog(self, screen):
        future = asyncio.get_running_loop().create_future()
        def completed(result):
            if not future.done():
                future.set_result(result)
        self.push_screen(screen, completed)
        try:
            return await future
        finally:
            if future.cancelled() and screen in self.screen_stack:
                screen.dismiss(None)

    async def server_request(self, name: str, event: dict) -> None:
        assert self.client
        method, params = event["method"], event.get("params", {})
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"}:
            allowed = self.turn_permissions.get(name, self.agents[name].permission) == "full"
            if not allowed:
                async with self.request_lock:
                    allowed = await self.dialog(RuntimeApproval(name, params.get("command") or params.get("reason") or json.dumps(params.get("changes", params.get("permissions", {})), ensure_ascii=False)))
            result = {"permissions": params.get("permissions", {}) if allowed else {}, "scope": "turn"} if method == "item/permissions/requestApproval" else {"decision": "accept" if allowed else "decline"}
        elif method in {"item/tool/requestUserInput", "tool/requestUserInput"}:
            async with self.request_lock:
                result = await self.dialog(RuntimeQuestion(name, params.get("questions", []))) or {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "cancel", "content": None}
            await self.add_entry(Entry("system", f"@{name} 的 MCP 服务需要额外交互，本次请求已取消。", target=name))
        else:
            await self.client.send({"id": event["id"], "error": {"code": -32601, "message": f"Unsupported room request: {method}"}})
            await self.add_entry(Entry("system", f"@{name} 发出暂不支持的交互：{method}", target=name))
            return
        await self.client.respond(event["id"], result)

    def start_agent(self, name: str) -> None:
        self.agents[name].busy = True
        self.tasks[name] = asyncio.create_task(self.run_agent(name))

    def reply_for(self, name: str, body: str) -> str:
        return f"收到：{body}\n\n这是一条界面演示回复。你可以继续输入，测试点名、并行回复和排队。\n（未连接真实 Codex，未执行命令）"

    async def run_agent(self, name: str) -> None:
        agent = self.agents[name]
        answer = None
        started = None
        try:
            while agent.queue:
                request = agent.queue.popleft()
                request.state = "done"
                self.cards[id(request)].refresh_entry()
                agent.phase = "正在启动会话…" if not self.demo else "正在思考…"
                started = monotonic()
                answer = Entry(name, "", state="working", started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
                self.active_entries[name] = (request, answer)
                card = await self.add_entry(answer)
                card.started_at = started
                if self.demo:
                    await asyncio.sleep(0.7 * self.speed)
                    reply = self.reply_for(name, request.body)
                    agent.phase = "正在回复…"
                    for index in range(0, len(reply), 7):
                        answer.body = reply[:index + 7]
                        self.refresh_answer(answer)
                        await asyncio.sleep(0.045 * self.speed)
                    answer.state = "done"
                else:
                    await self.real_reply(name, request.body, answer)
                answer.record_finish(monotonic() - started)
                self.refresh_answer(answer)
                self.active_entries.pop(name, None)
                agent.phase = "已回复，等待下一条消息"
                self.dirty = True
                self.save_state()
        except asyncio.CancelledError:
            if answer and answer.state == "working":
                answer.state = "stopped"
                answer.finish_tools("stopped")
                if not answer.body:
                    answer.append_text("回复已停止。")
                answer.record_finish(monotonic() - started)
                self.refresh_answer(answer)
            raise
        except Exception as exc:
            message = clean_error(exc)
            if answer:
                answer.state = "failed"
                answer.finish_tools("failed")
                answer.append_text(f"错误：{message}")
                answer.record_finish(monotonic() - started)
                self.refresh_answer(answer)
            agent.phase = "执行失败，等待你的下一条指令"
            while agent.queue:
                queued = agent.queue.popleft()
                queued.state = "stopped"
                self.cards[id(queued)].refresh_entry()
            if self.client and (not self.client.process or self.client.process.returncode is not None):
                self.connected = False
                self.connection_error = message
            self.active_entries.pop(name, None)
        finally:
            agent.busy = False
            self.turn_permissions.pop(name, None)
            self.dirty = True
            self.save_state()
            self.refresh_chrome()

    def refresh_answer(self, answer: Entry) -> None:
        self.dirty = True
        if not self.is_running or self.shutting_down:
            return
        follow = self.room_screen.query_one("#timeline", VerticalScroll).is_vertical_scroll_end
        self.cards[id(answer)].refresh_entry()
        if follow:
            self.call_after_refresh(self.scroll_bottom)

    async def real_reply(self, name: str, text: str, answer: Entry) -> None:
        if not self.client or not self.connected:
            raise CodexError("Codex 连接已断开，使用 /reconnect 重试。")
        agent = self.agents[name]
        settings = replace(agent)
        resolve_directory(settings.cwd, self.cwd)
        await self.inherit_defaults(settings)
        self.turn_permissions[name] = settings.permission
        await self.client.ensure_thread(settings)
        agent.thread_id = settings.thread_id
        agent.legacy_instructions = settings.legacy_instructions
        agent.model = agent.model or settings.model
        agent.effort = agent.effort or settings.effort
        agent.phase = "等待 Codex…"
        self.cards[id(answer)].refresh_working(self.frame)
        self.dirty = True
        self.save_state()
        blocks: dict[str, ReplyBlock] = {}
        summaries: dict[tuple[str, int], str] = {}

        def block_for(item_id: str, kind: str) -> ReplyBlock:
            if item_id not in blocks:
                blocks[item_id] = ReplyBlock(item_id, kind)
                answer.blocks.append(blocks[item_id])
            return blocks[item_id]

        async def receive(event: dict) -> None:
            if "id" in event:
                agent.phase = "等待交互…"
                self.cards[id(answer)].refresh_working(self.frame)
                await self.server_request(name, event)
                agent.phase = "正在继续执行…"
                self.cards[id(answer)].refresh_working(self.frame)
                return
            method, params = event["method"], event.get("params", {})
            item = params.get("item", {})
            item_id = params.get("itemId") or item.get("id", "")
            if method == "item/reasoning/summaryPartAdded":
                summaries[(item_id, params.get("summaryIndex", 0))] = ""
                agent.phase = "正在思考…"
            elif method == "item/reasoning/summaryTextDelta":
                key = (item_id, params.get("summaryIndex", 0))
                summaries[key] = (summaries.get(key, "") + params.get("delta", ""))[:512]
                title = summary_title(summaries[key])
                agent.phase = f"正在思考：{title}" if title else "正在思考…"
            elif method == "item/agentMessage/delta":
                agent.phase = "正在回复…"
                block_for(item_id, "text").text += params.get("delta", "")
            elif method in {"item/started", "item/completed"}:
                kind = item.get("type")
                if kind == "agentMessage" and item.get("text"):
                    agent.phase = "正在回复…"
                    block = block_for(item_id, "text")
                    block.text = item["text"]
                    block.status = "inProgress" if method == "item/started" else "completed"
                elif kind == "commandExecution":
                    block = block_for(item_id, "command")
                    block.title = item.get("command", block.title)
                    agent.phase = "正在执行命令…" if method == "item/started" else "正在分析命令结果…"
                    if item.get("aggregatedOutput") is not None:
                        block.text = item["aggregatedOutput"]
                    block.exit_code = item.get("exitCode")
                    block.status = item.get("status") or ("inProgress" if method == "item/started" else "completed")
                    if block.exit_code not in {None, 0}:
                        block.status = "failed"
                elif kind == "fileChange":
                    block = block_for(item_id, "file")
                    paths = [change.get("path", "") for change in item.get("changes", [])]
                    block.title = ", ".join(paths)
                    block.text = json.dumps(item.get("changes", []), ensure_ascii=False, indent=2)
                    block.status = item.get("status") or ("inProgress" if method == "item/started" else "completed")
                    agent.phase = "正在修改文件…"
                elif kind in {"reasoning", "contextCompaction"}:
                    title = summary_title(next((part for part in reversed(item.get("summary", [])) if part.strip()), ""))
                    agent.phase = (f"正在思考：{title}" if title else "正在思考…") if kind == "reasoning" else "正在整理上下文…"
                elif kind in {"mcpToolCall", "dynamicToolCall", "webSearch", "imageView"}:
                    block = block_for(item_id, "tool")
                    block.title = "/".join(filter(None, (item.get("server"), item.get("tool")))) or item.get("query") or item.get("path") or kind
                    block.text = json.dumps(item, ensure_ascii=False, indent=2)
                    block.status = item.get("status") or ("inProgress" if method == "item/started" else "completed")
                    agent.phase = "正在调用工具…" if method == "item/started" else "正在分析工具结果…"
            elif method == "item/commandExecution/outputDelta":
                block_for(item_id, "command").text += params.get("delta", "")
            elif method == "error":
                if params.get("willRetry"):
                    agent.phase = "连接中断，正在重试…"
                else:
                    error = params.get("error") or {}
                    block_for("error", "text").text = "错误：" + clean_error(error.get("message", error))
            if blocks:
                answer.sync_body()
                self.refresh_answer(answer)
            else:
                self.cards[id(answer)].refresh_working(self.frame)

        turn = await self.client.run_turn(settings, text, receive)
        answer.state = "stopped" if turn.get("status") == "interrupted" else "done"
        answer.finish_tools("stopped" if answer.state == "stopped" else "completed")
        if not answer.body and not answer.blocks:
            answer.append_text("回复已停止。" if answer.state == "stopped" else "本轮已完成。")
        self.refresh_answer(answer)

    async def stop_agent(self, name: str) -> None:
        agent = self.agents[name]
        task = self.tasks.pop(name, None)
        cancelled = len(agent.queue)
        while agent.queue:
            request = agent.queue.popleft()
            request.state = "stopped"
            self.cards[id(request)].refresh_entry()
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        active = self.active_entries.pop(name, None)
        if active:
            active[1].state = "stopped"
            active[1].finish_tools("stopped")
            if not active[1].body:
                active[1].append_text("回复已停止。")
            self.cards[id(active[1])].refresh_entry()
        agent.busy = False
        agent.phase = "已停止，等待下一条消息"
        if self.client and self.client.process and self.client.process.returncode is not None:
            self.connected = False
            self.connection_error = "Codex 连接已关闭，可用 /reconnect 重新连接。"
        await self.add_entry(Entry("system", f"@{name} 已停止；取消 {cancelled} 条排队消息。", target=name))
        self.refresh_chrome()

    async def on_unmount(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        tasks = list(self.tasks.values())
        if self.connection_task:
            tasks.append(self.connection_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client:
            await self.client.close()
        self.save_state()
        if self.store:
            self.store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="一个终端，多个独立的 Codex 会话")
    parser.add_argument("--demo", action="store_true", help="使用模拟回复预览界面，不连接 Codex")
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Codex 工作目录，默认当前目录")
    args = parser.parse_args()
    if not args.cwd.is_dir():
        parser.error("工作目录不存在")
    try:
        app = CodexRoom(demo=args.demo, cwd=args.cwd)
        app.run()
    except (RuntimeError, OSError, ValueError) as exc:
        parser.exit(1, f"codex-room: {exc}\n")


if __name__ == "__main__":
    main()
