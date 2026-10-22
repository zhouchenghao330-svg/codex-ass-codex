# /// script
# requires-python = ">=3.11"
# dependencies = ["prompt-toolkit==3.0.52", "rich>=14,<16"]
# ///
"""Isolated scrollback experiment. No Codex calls, room state, or mouse capture.

Run: uv run scripts/native_terminal_probe.py
This tests a presentation approach; it does not replace the application.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from time import monotonic

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.application.current import set_app
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import has_completions
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.text import Text


COMMANDS = ["/sample", "/background", "/help", "/quit", "@A", "@B"]
ACCENTS = {"A": "#a9b6ff", "B": "#78d8bc"}
HELP = """终端原生呈现小样（模拟内容，不调用 Codex）
直接拖选、双击、滚轮和复制交给当前终端；本程序不处理鼠标选区。
/sample 120   输出长记录，测试跨屏拖选
/background   模拟后台成员陆续回复；期间可以继续输入或拖选
@A 消息       模拟异步回复；@B 同样可用
Enter 发送 · Ctrl+J / Alt+Enter 换行 · Tab 补全 · ↑↓ 历史
/quit 或 Ctrl+D 退出；Ctrl+C 清空当前输入，空输入时退出
"""


class NativeTerminalProbe:
    def __init__(self, *, input=None, output=None, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.jobs: set[asyncio.Task] = set()
        self.writer = asyncio.Lock()
        self.submitted: list[str] = []
        self.sequence = 0
        bindings = KeyBindings()

        @bindings.add("enter")
        def accept(event):
            if has_completions():
                event.current_buffer.complete_state = None
            else:
                event.current_buffer.validate_and_handle()

        @bindings.add("c-j")
        @bindings.add("escape", "enter")
        def newline(event):
            event.current_buffer.insert_text("\n")

        @bindings.add("c-c")
        def cancel(event):
            if event.current_buffer.text:
                event.current_buffer.reset()
            else:
                event.app.exit(exception=EOFError())

        self.session = PromptSession(
            message="› ", multiline=True, wrap_lines=True, mouse_support=False,
            history=InMemoryHistory(), key_bindings=bindings,
            completer=WordCompleter(COMMANDS, WORD=True), complete_while_typing=False,
            style=Style.from_dict({"prompt": "#8d9488", "": "#d9d9cc"}),
            input=input, output=output,
        )
        # PromptSession builds an inline Application (full_screen=False).
        # Finished text is printed once; no retained transcript or custom selection.

    async def emit(self, renderable):
        async with self.writer:
            def write():
                # Construct at write time so terminal width follows a resize.
                console = Console(file=self.stream, force_terminal=self.stream.isatty(),
                                  color_system="truecolor" if self.stream.isatty() else None)
                console.print(renderable)

            # Temporarily erase/redraw only the active prompt; preserve its Buffer.
            # Unlike a stdout proxy, there is no default 200ms batching delay.
            # Background tasks start between prompt_async calls. Bind the same
            # Application explicitly so output still goes above the active input.
            with set_app(self.session.app):
                await run_in_terminal(write)

    async def message(self, member, body, *, seconds=None):
        self.sequence += 1
        header = Text(f"{member}  @{member}  ·  m{self.sequence:03d}", style=ACCENTS[member])
        parts = [header, Markdown(body)]
        if seconds is not None:
            parts.append(Text(f"Worked for {seconds:.1f}s · {datetime.now():%H:%M}", style="#8d9488"))
        parts.append(Text(""))
        await self.emit(Group(*parts))

    async def sample(self, count=120):
        count = min(2000, max(1, count))
        # One output transaction: a long transcript shouldn't become 120 redraws.
        rows = [Text("长记录：请用当前终端的原生拖选跨行、跨屏选择。", style="#8d9488")]
        for index in range(1, count + 1):
            member = "A" if index % 2 else "B"
            line = Text(f"{index:04d}  @{member}  ", style=ACCENTS[member])
            line.append("这里是稳定的历史正文。中文、English、路径 /some/project/src/example.py 均可选择。",
                        style="#d9d9cc")
            rows.append(line)
        await self.emit(Group(*rows))

    async def reply(self, member, text, delay=0.8):
        started = monotonic()
        await asyncio.sleep(delay)
        await self.message(member, "模拟收到：\n\n" + text +
                           "\n\n这段回复进入终端历史；之后打字只更新输入区。",
                           seconds=monotonic() - started)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)
        task.add_done_callback(self.jobs.discard)
        return task

    async def handle(self, text):
        if text == "/quit":
            return False
        if text == "/help":
            await self.emit(Text(HELP))
        elif text == "/background":
            for member, delay in (("A", 1), ("B", 3), ("A", 5)):
                self.spawn(self.reply(member, "后台回复，观察你的输入草稿和选区是否受影响。", delay))
        elif text.split(maxsplit=1)[0:1] == ["/sample"]:
            try:
                await self.sample(int(text.split(maxsplit=1)[1]) if " " in text else 120)
            except ValueError:
                await self.emit(Text("用 /sample 120 指定行数。"))
        elif text:
            member, body = "A", text
            if text.startswith(("@A ", "@B ")):
                member, body = text[1], text[3:]
            self.spawn(self.reply(member, body))
        return True

    async def run(self):
        await self.emit(Text(HELP))
        try:
            while True:
                try:
                    text = await self.session.prompt_async()
                except KeyboardInterrupt:
                    if not self.session.default_buffer.text:
                        break
                    continue
                except EOFError:
                    break
                self.submitted.append(text)
                if not await self.handle(text.strip()):
                    break
        finally:
            tasks = list(self.jobs)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise SystemExit("请在交互终端运行：uv run scripts/native_terminal_probe.py")
    # Preserve the caller's exact TTY state even when queued type-ahead makes
    # macOS set PENDIN during a cooked/raw transition above the prompt.
    saved = None
    if sys.platform != "win32":
        import termios
        saved = termios.tcgetattr(sys.stdin.fileno())
    try:
        asyncio.run(NativeTerminalProbe().run())
    finally:
        if saved is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, saved)
