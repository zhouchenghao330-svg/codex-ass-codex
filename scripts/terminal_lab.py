"""Run controlled terminal experiments; never connects to Codex or room state.

uv run python scripts/terminal_lab.py rust-inline --lines 10000
uv run python scripts/terminal_lab.py rust-fullscreen --lines 10000
uv run python scripts/terminal_lab.py textual --lines 10000
uv run python scripts/terminal_lab.py textual-no-mouse --lines 10000
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from time import monotonic


ROOT = Path(__file__).resolve().parents[1]


def fixture(lines: int) -> dict:
    template = [
        "### 回合 {turn:04d}：终端阅读实验",
        "",
        "这里是**加粗文字**和普通中文。拖动选择应该准确停在字符边界。",
        "English words, punctuation; and 中文 mixed together.",
        "",
        "- 观察滚动、拖选和复制，而不是只看语言名称。",
        "- 路径：`/some/project/src/example.py`。",
        "- 引用范围应该由用户自己决定。",
        "",
        "> 这是一段引用：先验证体验，再决定是否迁移。",
        "",
        "```python",
        "def deduplicate(messages):",
        "    # 中文注释：按 ID 保留第一次出现的消息",
        "    return list(dict.fromkeys(messages))",
        "```",
        "",
        "| 项目 | 状态 |",
        "| --- | --- |",
        "| 原生滚动 | 待体验 |",
        "| 选字复制 | 待体验 |",
        "",
        "这里的内容在所有模式中完全相同，不调用任何模型或工具。",
        "末尾测试：没有、取消；你好，世界。end-of-message",
    ]
    history, remaining, turn = [], lines, 1
    while remaining:
        block = [row.replace("{turn:04d}", f"{turn:04d}") for row in template[:remaining]]
        history.append("\n".join(block))
        remaining -= len(block)
        turn += 1
    stream = [
        "我正在**模拟流式回复**。你可以同时输入，或者拖选之前的历史内容。\n\n",
        "### 当前实现\n\n先保留稳定历史，再更新正在输出的这小块内容。\n\n",
        "- 第一项：中文和 English 混排。\n- 第二项：观察选区是否被追加内容打断。\n\n",
        "```rust\nfn main() {\n    println!(\"你好，终端！\");\n}\n```\n\n",
        "> 此处只是模拟输出，没有创建成员，也没有连接真实 Codex。\n\n",
        "| 实验 | 检查 |\n| --- | --- |\n| 拖选 | 精确性与跟手感 |\n| 流式输出 | 是否干扰选区 |\n\n",
    ]
    return {"history": history, "stream_blocks": stream, "source_lines": lines}


def textual_run(data: dict, mouse: bool, paused=False) -> None:
    # Reuse the actual current renderer/selection implementation, not a new
    # stripped-down Textual app that would hide the project's real costs.
    sys.path.insert(0, str(ROOT))
    from textual.binding import Binding
    from textual.containers import VerticalScroll
    from codex_room.app import Agent, CodexRoom
    from codex_room.transcript import Entry, ReplyBlock

    class ExperimentRoom(CodexRoom):
        CSS_PATH = ROOT / "codex_room/room.tcss"
        BINDINGS = [Binding("f2", "pause_stream", "暂停／继续", priority=True),
                    Binding("pageup", "history_up", show=False, priority=True),
                    Binding("pagedown", "history_down", show=False, priority=True)]

        def __init__(self):
            super().__init__(demo=True, cwd=ROOT)
            self.agents = {"probe": Agent("probe", "模拟Codex", "#a9b6ff", cwd=str(ROOT))}
            self.selected = "probe"
            self.saved = {"entries": [Entry("probe", text).__dict__ for text in data["history"]]}
            self.stream_paused = paused
            self.stream_block = 0
            self.stream_position = 0
            self.stream_entry = None
            self.load_started = monotonic()

        async def on_mount(self):
            await super().on_mount()
            self.notify(f"Textual · {'应用捕获鼠标' if mouse else '关闭鼠标捕获'} · "
                        f"{data['source_lines']} 行源文本 · 初始化 {monotonic()-self.load_started:.2f}s\n"
                        "F2 暂停／继续流式输出 · PageUp/Down 滚动记录 · Ctrl+Q 退出", timeout=12)
            self.set_interval(0.06, self.stream_tick)

        async def stream_tick(self):
            if self.stream_paused:
                return
            if self.stream_entry is None:
                self.stream_entry = Entry("probe", "", state="working", blocks=[ReplyBlock("stream", "text")],
                                          started_at=datetime.now().astimezone().isoformat(timespec="seconds"))
                await self.add_entry(self.stream_entry)
                self.agents["probe"].busy = True
                self.agents["probe"].phase = "模拟流式输出 · F2 暂停"
            block = data["stream_blocks"][self.stream_block % len(data["stream_blocks"])]
            self.stream_position = min(len(block), self.stream_position + 4)
            self.stream_entry.blocks[0].text = block[:self.stream_position]
            self.stream_entry.sync_body()
            self.refresh_answer(self.stream_entry)
            if self.stream_position == len(block):
                self.stream_entry.blocks[0].status = "completed"
                self.stream_entry.state = "done"
                self.stream_entry.record_finish(0.06 * (len(block) / 4))
                self.refresh_answer(self.stream_entry)
                self.stream_entry = None
                self.stream_position = 0
                self.stream_block += 1

        def action_pause_stream(self):
            self.stream_paused = not self.stream_paused
            self.agents["probe"].busy = not self.stream_paused
            if self.stream_entry is not None:
                self.stream_entry.state = "paused" if self.stream_paused else "working"
                self.refresh_answer(self.stream_entry)
            self.notify("流式输出已暂停" if self.stream_paused else "流式输出已继续")

        def action_history_up(self):
            self.room_screen.query_one("#timeline", VerticalScroll).scroll_page_up(animate=False)

        def action_history_down(self):
            self.room_screen.query_one("#timeline", VerticalScroll).scroll_page_down(animate=False)

        async def submit(self, value, **kwargs):
            if value.strip() == "/quit":
                self.exit()
            elif value.strip() == "/pause":
                self.action_pause_stream()
            else:
                await self.add_entry(Entry("you", value, "probe"))
            return True

    ExperimentRoom().run(mouse=mouse)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    modes = ["rust-inline", "rust-fullscreen", "textual", "textual-no-mouse"]
    parser.add_argument("mode", nargs="?", choices=modes)
    parser.add_argument("--lines", type=int, choices=[1000, 10000], default=1000)
    parser.add_argument("--paused", action="store_true", help="先暂停流式输出，F2 开始")
    args = parser.parse_args()
    if args.mode is None:
        while True:
            print(f"\n终端技术选型实验 · {args.lines:,} 行相同源文本 · 不连接模型")
            for index, mode in enumerate(modes, 1):
                print(f"  {index}. {mode}")
            try:
                choice = input("选择 1～4，q 退出；实验中 Ctrl+Q 返回菜单：").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if choice.lower() == "q":
                break
            if choice not in {"1", "2", "3", "4"}:
                continue
            command = [sys.executable, str(Path(__file__).resolve()), modes[int(choice)-1], "--lines", str(args.lines)]
            if args.paused:
                command.append("--paused")
            subprocess.call(command, cwd=ROOT)
        return
    data = fixture(args.lines)
    if args.mode.startswith("rust-"):
        cargo = shutil.which("cargo") or str(Path.home() / ".cargo/bin/cargo")
        if not Path(cargo).is_file():
            parser.error("找不到 Cargo。请先安装 Rust：https://rust-lang.org/tools/install/")
        directory = ROOT / "artifacts/terminal-lab"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"fixture-{args.lines}.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        command = [cargo, "run", "--release", "--manifest-path", str(ROOT / "experiments/terminal-lab/Cargo.toml"),
                   "--", "--mode", args.mode.removeprefix("rust-"), "--fixture", str(path),
                   "--metrics", str(directory / f"{args.mode}-{args.lines}-metrics.json")]
        if args.paused:
            command.append("--paused")
        raise SystemExit(subprocess.call(command, cwd=ROOT))
    textual_run(data, mouse=args.mode == "textual", paused=args.paused)


if __name__ == "__main__":
    main()
