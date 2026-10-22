"""UI-independent room service. JSON lines on stdio, never terminal escape codes.

Rust owns drawing/input/selection. This process owns Codex RPC and the existing
v2 room store. Patches are coalesced; mouse movement never crosses this boundary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import deque
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .attachments import Attachment, Prompt, image_paths
from .colors import choose_color
from .completion import CompletionEngine, EFFORTS, is_room_command
from .quotes import ForwardQuote
from .member_config import parse_add, resolve_directory, resolve_member, validate_name
from .runtime import CodexClient, CodexError, clean_error
from .storage import RoomStore
from .models import Entry, ReplyBlock
from .tool_details import file_changes


def wire_entry(entry):
    data = asdict(entry)
    if entry.started_at:
        data["started_epoch"] = datetime.fromisoformat(entry.started_at).timestamp()
    for raw, block in zip(data["blocks"], entry.blocks):
        if block.kind == "file":
            raw["files"] = [asdict(change) for change in file_changes(block)]
    return data


@dataclass
class Member:
    name: str
    label: str
    color: str
    cwd: str = ""
    thread_id: str | None = None
    model: str = ""
    effort: str = ""
    permission: str = "full"
    legacy_instructions: bool = False
    parent_name: str = ""
    agent_path: str = ""
    can_accept_direct_input: bool = True
    native_closed: bool = False
    busy: bool = False
    phase: str = "空闲"
    queue: deque = field(default_factory=deque)
    delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def config(self):
        return {key: getattr(self, key) for key in (
            "name", "label", "color", "cwd", "thread_id", "model", "effort", "permission", "legacy_instructions",
            "parent_name", "agent_path", "can_accept_direct_input", "native_closed")}


def summary_title(value):
    return re.sub(r"[*_`#]", "", value.split("\n", 1)[0]).strip()[:100]


class Service:
    def __init__(self, cwd: Path, *, demo=False, command=None, emit=None):
        self.cwd, self.demo, self.codex_command = cwd.resolve(), demo, command
        self.emit = emit or self.write
        self.store = None if demo else RoomStore(self.cwd / ".codex-room")
        self.saved = self.store.load() if self.store else {}
        config_keys = {f.name for f in fields(Member)} - {"queue", "delivery_lock", "busy", "phase"}
        self.agents = {m["name"]: Member(**{k: v for k, v in m.items() if k in config_keys})
                       for m in self.saved.get("agents", [])}
        for member in self.agents.values():
            member.cwd = member.cwd or str(self.cwd)
        entry_keys = {f.name for f in fields(Entry)}
        self.entries = [Entry(**{k: v for k, v in e.items() if k in entry_keys}) for e in self.saved.get("entries", [])]
        for entry in self.entries:
            if entry.state in {"working", "queued"}:
                entry.state = "stopped"
                entry.finish_tools("stopped")
            entry.restore_legacy_tools()
        self.selected = self.saved.get("selected", "")
        self.view_revision = 0
        if self.selected not in self.agents:
            self.selected = next(iter(self.agents), "")
        self.show_all = self.saved.get("show_all", True)
        self.input_history = self.saved.get("input_history", [])
        self.drafts = self.saved.get("drafts", {})
        self.client = None
        self.connected = demo
        self.tasks = {}
        self.active_entries = {}
        self.reply_boundaries = set()
        self.changed = {}
        self.members_dirty = False
        self.dirty = False
        self.closing = False
        self.interactions = {}
        self.completions = CompletionEngine()
        self.background = set()
        self.last_submission = None
        self.ignored_native_threads = set(self.saved.get("ignored_native_threads", []))
        from .native_agents import NativeAgents
        self.native = NativeAgents(self)

    @staticmethod
    def write(value):
        print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)

    def notify(self, message):
        self.emit({"type": "notice", "text": clean_error(message)})

    def members(self):
        self.emit({"type": "members", "agents": [{**m.config(), "busy": m.busy, "phase": m.phase}
                   for m in self.agents.values()], "selected": self.selected, "show_all": self.show_all,
                   "connected": self.connected, "demo": self.demo, "view_revision": self.view_revision})
        self.members_dirty = False

    def refresh_answer(self, answer):
        self.changed[answer.id] = answer
        self.dirty = True
        self.members_dirty = True

    def add_entry(self, entry):
        self.entries.append(entry)
        self.refresh_answer(entry)
        return entry

    def flush(self):
        if self.changed:
            self.emit({"type": "patch", "entries": [wire_entry(e) for e in self.changed.values()]})
            self.changed.clear()
        if self.members_dirty:
            self.members()

    def save(self):
        if self.store and self.dirty:
            # Preserve fields owned by a future service rather than discarding
            # them when an older UI is used to open the same room.
            old_members = {a["name"]: a for a in self.saved.get("agents", [])}
            old_entries = {e.get("id"): e for e in self.saved.get("entries", [])}
            self.saved.update(version=2, agents=[{**old_members.get(a.name, {}), **a.config()} for a in self.agents.values()],
                              entries=[{**old_entries.get(e.id, {}), **asdict(e)} for e in self.entries], selected=self.selected,
                              show_all=self.show_all, input_history=self.input_history, drafts=self.drafts,
                              ignored_native_threads=sorted(self.ignored_native_threads))
            self.store.save(self.saved)
        self.dirty = False

    async def start(self):
        self.emit({"type": "init", "entries": [wire_entry(e) for e in self.entries],
                   "input_history": self.input_history, "drafts": self.drafts, "cwd": str(self.cwd)})
        self.members()
        if not self.demo:
            await self.connect()

    async def connect(self):
        self.connected = False
        try:
            if self.client:
                await self.native.close()
                self.client.observer = None
                await self.client.close()
                from .native_agents import NativeAgents
                self.native = NativeAgents(self)
            self.client = CodexClient(self.cwd, command=self.codex_command)
            self.client.observer = self.native.observe
            await self.client.start()
            self.connected = True
            for member in self.agents.values():
                if not member.parent_name:
                    await self.inherit_defaults(member)
            await self.native.start()
            self.notify("Codex 已连接")
            self.dirty = True
        except Exception as exc:
            self.notify(f"Codex 连接失败：{clean_error(exc)} · /reconnect 重试")
        self.members()

    async def inherit_defaults(self, member):
        if not self.client or not self.connected or (member.model and member.effort):
            return
        config = self.client.config
        if member.cwd != str(self.cwd):
            config = (await self.client.request("config/read", {"includeLayers": False, "cwd": member.cwd})).get("config", {})
        member.model = member.model or config.get("model") or ""
        member.effort = member.effort or config.get("model_reasoning_effort") or ""

    async def command(self, value, target=None):
        parts = value.split(maxsplit=2)
        verb = parts[0].lower()
        explicit, target = target, target or self.selected
        if verb == "/add":
            options = parse_add(value, self.cwd)
            if options is None:
                self.emit({"type": "prefill", "text": "/add "})
                return True
            name = validate_name(options.name, self.agents)
            cwd = str(resolve_directory(options.cwd, self.cwd))
            color = choose_color([a.color for a in self.agents.values()], options.color)
            member = Member(name, name, color, cwd)
            await self.inherit_defaults(member)
            self.agents[name] = member
            self.selected, self.show_all = name, False
            self.notify(f"@{name} 已加入 · {cwd}")
        elif verb == "/rename":
            if len(parts) == 3:
                target, label = resolve_member(parts[1], self.agents), parts[2]
            elif len(parts) == 2:
                label = parts[1]
            else:
                raise ValueError("用 /rename 原名 新名，或 /rename 新名")
            if target not in self.agents:
                raise ValueError("没有这个成员")
            self.agents[target].label = validate_name(label, self.agents, excluding=target)
        elif verb in {"/focus", "/stop", "/del"}:
            if verb == "/focus" and len(parts) == 2 and parts[1].casefold() == "all":
                self.show_all = True
            else:
                if verb == "/del" and len(parts) == 1 and not explicit:
                    raise ValueError("用 /del 名字 移除成员")
                name = resolve_member(parts[1], self.agents) if len(parts) > 1 else target
                if name not in self.agents:
                    raise ValueError("没有这个成员")
                if verb == "/focus":
                    self.selected, self.show_all = name, False
                elif verb == "/stop":
                    await self.stop(name)
                else:
                    await self.delete_member(name)
        elif verb in {"/model", "/permissions", "/permission", "/permisson"}:
            if target not in self.agents:
                raise ValueError("请先创建成员")
            member = self.agents[target]
            if member.parent_name:
                raise ValueError("原生子代理的模型和权限由 Codex 父会话管理，请在父会话中调整")
            args = value.split()[1:]
            if verb == "/model":
                models = self.client.models if self.client else []
                if not args:
                    if not models:
                        self.notify("Codex 未返回模型列表，请直接填写 /model 模型名 [推理强度]")
                        self.emit({"type": "prefill", "text": f"@{member.label} /model "})
                        return True
                    self.emit({"type": "choose", "title": "选择模型", "prefix": f"@{member.label} /model ",
                               "values": [{"value": m["id"], "detail": ", ".join(e["reasoningEffort"] for e in m.get("supportedReasoningEfforts", []))} for m in models]})
                    return True
                catalog = next((m for m in models if m["id"] == args[0]), {})
                effort = (args[1] if len(args) > 1 else "") or catalog.get("defaultReasoningEffort") or member.effort
                supported = [e["reasoningEffort"] for e in catalog.get("supportedReasoningEfforts", [])]
                if effort and (effort not in EFFORTS or (supported and effort not in supported)):
                    raise ValueError(f"不支持该推理强度，可用：{', '.join(supported or EFFORTS)}")
                member.model, member.effort = args[0], effort
            else:
                if not args:
                    self.emit({"type": "choose", "title": "选择权限", "prefix": f"@{member.label} /permissions ",
                               "values": [{"value": v} for v in ("full", "workspace", "read-only")]})
                    return True
                mode = {"full-access": "full", "workspace-write": "workspace"}.get(args[0], args[0])
                if mode not in {"full", "workspace", "read-only"}:
                    raise ValueError("权限：full / workspace / read-only")
                member.permission = mode
            self.notify(f"@{member.label} 设置已保存，下一轮生效")
        elif verb == "/reconnect":
            if any(a.busy for a in self.agents.values()):
                raise ValueError("先停止正在工作的成员，再重连")
            await self.connect()
        elif verb in {"/help", "/members"}:
            self.emit({"type": "action", "action": verb[1:]})
        elif verb == "/quit":
            self.emit({"type": "action", "action": "quit"})
        else:
            raise ValueError("演示请用 --demo 启动")
        self.dirty = True
        self.members()
        return True

    async def submit(self, prompt, quote=None):
        if not prompt.attachments and not quote and is_room_command(prompt.text):
            return await self.command(prompt.text)
        names = [self.selected]
        position = 0
        if prompt.text.startswith("@"):
            names = []
            while match := re.match(r"@([\w-]+)(?:\s+|$)", prompt.text[position:]):
                name = resolve_member(match.group(1), self.agents)
                if not name:
                    raise ValueError(f"没有 @{match.group(1)}，输入内容已保留")
                if name not in names:
                    names.append(name)
                position += match.end()
            prompt = prompt.slice(position).strip()
        if not names or any(n not in self.agents for n in names):
            raise ValueError("先用 /add 名字 创建 Codex")
        if not prompt.text.strip() and not quote:
            raise ValueError("请输入消息")
        if quote and names != [self.selected]:
            raise ValueError("转发草稿请发送给当前接收者")
        if not quote and not prompt.attachments and is_room_command(prompt.text):
            if len(names) != 1:
                raise ValueError("设置命令只指定一位成员")
            return await self.command(prompt.text, names[0])
        if not self.connected:
            raise ValueError("Codex 尚未连接，/reconnect 重试")
        images = [*prompt.images, *(ForwardQuote.load(quote).images if quote else [])]
        if any(not Path(path).is_file() for path in images):
            raise ValueError("图片文件已不存在，请重新粘贴；输入内容已保留")
        for name in names:
            member = self.agents[name]
            if member.parent_name:
                if member.native_closed or member.thread_id not in self.native.attached:
                    raise ValueError(f"@{member.label} 尚未恢复或已关闭，请先 /reconnect")
                self.native.root_for(member)
        batch = uuid4().hex
        for name in names:
            agent = self.agents[name]
            entry = self.add_entry(Entry("you", prompt.expand(), name, "queued" if agent.busy else "done",
                display_body=prompt.text if prompt.attachments else None, attachments=prompt.attachments[:],
                quote=quote, batch_id=batch, recipients=names[:]))
            if agent.parent_name and not agent.can_accept_direct_input:
                entry.state = "queued"
                self.native.suspended.discard(name)
                self.native.spawn(self.native.relay(agent, entry))
            else:
                self.enqueue(agent, entry)
        self.selected = names[0]
        if len(names) > 1:
            self.show_all = True
        self.members()
        return True

    def enqueue(self, agent, entry):
        agent.queue.append(entry)
        if agent.busy and agent.name in self.active_entries:
            if not entry.hidden:
                self.reply_boundaries.add(agent.name)
        if not agent.busy:
            agent.busy = True
            self.tasks[agent.name] = asyncio.create_task(self.run_agent(agent.name))
        else:
            self.spawn(self.deliver_followups(agent.name))

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def deliver_followups(self, name):
        agent = self.agents[name]
        async with agent.delivery_lock:
            while agent.queue and name in self.active_entries:
                request = agent.queue[0]
                answer = self.active_entries[name][1]
                try:
                    request.server_turn_id = self.client.active.get(agent.thread_id, "") if self.client else ""
                    if self.demo:
                        # The demo loop consumes this list in the same turn.
                        self.demo_followups.setdefault(name, []).append(request.input_text)
                    elif not await self.client.steer_turn(agent.thread_id, request.input_text, images=request.image_paths):
                        break
                except Exception as exc:
                    if agent.queue and agent.queue[0] is request:
                        agent.queue.popleft()
                    request.state = "failed"
                    self.refresh_answer(request)
                    self.notify(f"追加未确认：{clean_error(exc)}。未自动重发")
                    continue
                if not agent.queue or agent.queue[0] is not request:
                    break
                agent.queue.popleft()
                request.state, request.run_id = "steered", answer.run_id
                self.refresh_answer(request)

    async def continuation(self, name):
        request, answer = self.active_entries[name]
        if name in self.reply_boundaries:
            self.reply_boundaries.discard(name)
            answer.state = "continued"
            self.refresh_answer(answer)
            answer = self.add_entry(Entry(name, "", state="working", started_at=answer.started_at, run_id=answer.run_id,
                                         hidden=answer.hidden, relay_for=answer.relay_for))
            self.active_entries[name] = request, answer
        return answer

    def finish_run_tools(self, name, answer, status):
        for segment in self.entries:
            if segment.sender == name and segment.run_id == answer.run_id:
                segment.finish_tools(status)
                self.refresh_answer(segment)

    async def run_agent(self, name):
        agent, answer, started = self.agents[name], None, monotonic()
        try:
            while agent.queue:
                async with agent.delivery_lock:
                    request = agent.queue.popleft()
                request.state = "done"
                self.refresh_answer(request)
                self.reply_boundaries.discard(name)
                started = monotonic()
                answer = self.add_entry(Entry(name, "", state="working", started_at=datetime.now().astimezone().isoformat(timespec="seconds"), run_id=uuid4().hex,
                    hidden=request.hidden, relay_for=request.relay_for))
                request.run_id = answer.run_id
                self.active_entries[name] = request, answer
                agent.phase = "正在启动会话…"
                if self.demo:
                    self.demo_followups = getattr(self, "demo_followups", {})
                    self.demo_followups[name] = []
                    await self.deliver_followups(name)
                    await asyncio.sleep(0.15)
                    text = f"收到：{request.body}\n\n这是一条 **Rust 界面演示** 回复。\n\n```python\nprint('你好，Codex Room')\n```\n\n未连接真实 Codex，未执行命令。"
                    for i in range(0, len(text), 4):
                        answer = await self.continuation(name)
                        answer.body += text[i:i+4]
                        agent.phase = "正在回复…"
                        self.refresh_answer(answer)
                        await asyncio.sleep(0.04)
                    for followup in self.demo_followups[name]:
                        answer = await self.continuation(name)
                        answer.body += f"\n\n已采纳追加：{followup}"
                    answer.state = "done"
                else:
                    answer = await self.real_reply(name, request.input_text, answer, images=request.image_paths)
                answer.record_finish(monotonic() - started)
                self.refresh_answer(answer)
                self.active_entries.pop(name, None)
                if not request.hidden:
                    self.notify(f"@{agent.label} 已完成")
        except asyncio.CancelledError:
            answer = self.active_entries.get(name, (None, answer))[1]
            if answer:
                answer.state = "stopped"
                answer.record_finish(monotonic() - started)
                self.finish_run_tools(name, answer, "stopped")
                self.refresh_answer(answer)
            raise
        except Exception as exc:
            answer = self.active_entries.get(name, (None, answer))[1]
            if answer:
                answer.state = "failed"
                answer.append_text(f"错误：{clean_error(exc)}")
                answer.record_finish(monotonic() - started)
                self.finish_run_tools(name, answer, "failed")
                self.refresh_answer(answer)
            self.notify(clean_error(exc))
            if self.client and (not self.client.process or self.client.process.returncode is not None):
                self.connected = False
        finally:
            for pending in agent.queue:
                pending.state = "stopped"
                self.refresh_answer(pending)
            agent.queue.clear()
            self.active_entries.pop(name, None)
            self.reply_boundaries.discard(name)
            agent.busy, agent.phase = False, "空闲"
            self.members_dirty = True
            self.dirty = True
            self.save()

    async def real_reply(self, name: str, text: str, answer: Entry, *, images: list[str] | None = None) -> Entry:
        if not self.client or not self.connected:
            raise CodexError("Codex 连接已断开，使用 /reconnect 重试。")
        agent = self.agents[name]
        settings = replace(agent)
        resolve_directory(settings.cwd, self.cwd)
        if settings.parent_name:
            await self.client.attach_native_thread(settings.thread_id)
        else:
            await self.inherit_defaults(settings)
            await self.client.ensure_thread(settings)
        agent.thread_id = settings.thread_id
        agent.legacy_instructions = settings.legacy_instructions
        agent.model = agent.model or settings.model
        agent.effort = agent.effort or settings.effort
        agent.phase = "等待 Codex…"
        self.members_dirty = True
        self.dirty = True
        self.save()
        blocks: dict[str, ReplyBlock] = {}
        owners: dict[int, Entry] = {}
        text_parts: dict[str, list[ReplyBlock]] = {}
        summaries: dict[tuple[str, int], str] = {}

        def block_for(item_id: str, kind: str) -> ReplyBlock:
            if item_id not in blocks:
                blocks[item_id] = ReplyBlock(item_id, kind)
                answer.blocks.append(blocks[item_id])
                owners[id(blocks[item_id])] = answer
            return blocks[item_id]

        async def receive(event: dict) -> None:
            nonlocal answer
            if "id" in event:
                agent.phase = "等待交互…"
                self.members_dirty = True
                await self.server_request(name, event)
                agent.phase = "正在继续执行…"
                self.members_dirty = True
                return
            method, params = event["method"], event.get("params", {})
            turn_id = params.get("turnId") or params.get("turn", {}).get("id")
            if turn_id:
                answer.server_turn_id = turn_id
                self.active_entries[name][0].server_turn_id = turn_id
                if agent.parent_name:
                    answer.native_thread_id = agent.thread_id
                    self.native.records[agent.thread_id, turn_id] = answer
            item = params.get("item", {})
            item_id = params.get("itemId") or item.get("id", "")
            kind = item.get("type")
            known_text = ("".join(part.text for part in text_parts.get(item_id, [blocks[item_id]]))
                          if method == "item/completed" and kind == "agentMessage" and item_id in blocks else None)
            completed_extension = (known_text is not None and item.get("text", "").startswith(known_text)
                                   and item.get("text", "") != known_text)
            visible_new = (method == "item/agentMessage/delta" or
                (method in {"item/started", "item/completed"} and item_id not in blocks
                 and kind not in {None, "reasoning", "contextCompaction", "userMessage"}) or
                method == "error" and not params.get("willRetry") or completed_extension)
            if visible_new:
                current = await self.continuation(name)
                if ((method == "item/agentMessage/delta" or completed_extension) and item_id in blocks
                        and owners[id(blocks[item_id])] is not current):
                    prior = blocks.pop(item_id)
                    text_parts.setdefault(item_id, [prior])
                    prior.status = "completed"
                answer = current
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
                block = blocks[item_id]
                parts = text_parts.setdefault(item_id, [])
                if not any(part is block for part in parts):
                    parts.append(block)
            elif method in {"item/started", "item/completed"}:
                kind = item.get("type")
                if kind == "agentMessage" and item.get("text"):
                    agent.phase = "正在回复…"
                    block = block_for(item_id, "text")
                    prefix = sum(len(part.text) for part in text_parts.get(item_id, []) if part is not block)
                    block.text = item["text"][prefix:]
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
                elif kind and kind not in {"userMessage", "agentMessage"}:
                    block = block_for(item_id, "tool")
                    block.title = "/".join(filter(None, (item.get("server"), item.get("tool")))) or item.get("query") or item.get("path") or kind
                    block.text = json.dumps(item, ensure_ascii=False, indent=2)
                    block.status = item.get("status") or ("inProgress" if method == "item/started" else "completed")
                    if item.get("success") is False or item.get("error"):
                        block.status = "failed"
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
                # A command can complete after a followup. Its output belongs
                # to the card where that command began.
                changed = blocks.get(item_id)
                owner = owners.get(id(changed), answer)
                owner.sync_body()
                self.refresh_answer(owner)
                if owner is not answer:
                    self.members_dirty = True
            else:
                self.members_dirty = True

        async def on_started():
            answer.server_turn_id = self.client.active.get(settings.thread_id, "")
            self.active_entries[name][0].server_turn_id = answer.server_turn_id
            await self.deliver_followups(name)
        turn = await self.client.run_turn(settings, text, receive, on_started=on_started, images=images)
        answer.state = "stopped" if turn.get("status") == "interrupted" else "done"
        self.finish_run_tools(name, answer, "stopped" if answer.state == "stopped" else "completed")
        if not answer.body and not answer.blocks:
            answer.append_text("回复已停止。" if answer.state == "stopped" else "本轮已完成。")
        self.refresh_answer(answer)
        return answer

    async def server_request(self, name, event):
        method = event["method"]
        if method == "mcpServer/elicitation/request":
            await self.client.respond(event["id"], {"action": "cancel"})
            self.notify(f"@{name} 的 MCP 表单暂不支持，已取消")
            return
        if method not in {"item/tool/requestUserInput", "tool/requestUserInput", "item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval"}:
            await self.client.send({"id": event["id"], "error": {"code": -32601, "message": "Unsupported room interaction"}})
            self.notify(f"不支持的交互：{method}")
            return
        key = str(event["id"])
        agent = self.agents.get(name)
        if agent:
            agent.phase = "等待你回复…"
            self.members_dirty = True
        future = asyncio.get_running_loop().create_future()
        self.interactions[key] = (future, event, name)
        self.emit({"type": "interaction", "member": name, "event": event})
        try:
            result = await future
            await self.client.respond(event["id"], result)
        finally:
            self.interactions.pop(key, None)
            self.emit({"type": "interaction_done", "id": key})
            if agent and agent.busy:
                agent.phase = "正在继续执行…"
                self.members_dirty = True

    async def stop(self, name):
        agent = self.agents[name]
        if agent.parent_name and not agent.can_accept_direct_input:
            for entry in self.entries:
                if entry.sender == "you" and entry.target == name and entry.state == "queued":
                    entry.state = "stopped"
                    self.refresh_answer(entry)
            await self.native.stop(agent)
            self.members_dirty = True
            return
        for request in agent.queue:
            request.state = "stopped"
            self.refresh_answer(request)
        agent.queue.clear()
        task = self.tasks.get(name)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.members_dirty = True

    async def delete_member(self, name):
        names = [name]
        for parent in names:
            names.extend(a.name for a in self.agents.values() if a.parent_name == parent and a.name not in names)
        # Stop every descendant before removing any records. A failure leaves
        # the tree available to inspect/retry instead of silently orphaning it.
        for child in reversed(names):
            await self.stop(child)
        await self.native.wait_before_removal(names)
        if self.store:
            for child in names:
                self.store.archive_member(child, {"version": 2, "agent": self.agents[child].config(),
                    "entries": [asdict(e) for e in self.entries if e.sender == child or e.target == child],
                    "draft": self.drafts.get(child)})
        for child in reversed(names):
            member = self.agents.pop(child)
            if member.parent_name:
                self.ignored_native_threads.add(member.thread_id)
                self.native.attached.discard(member.thread_id)
                try:
                    await self.client.request("thread/unsubscribe", {"threadId": member.thread_id}, timeout=5)
                except CodexError as exc:
                    self.notify("子代理订阅清理：" + clean_error(exc))
            self.drafts.pop(child, None)
        removed_ids = {e.id for e in self.entries if e.sender in names or e.target in names}
        self.entries = [e for e in self.entries if e.id not in removed_ids and e.relay_for not in removed_ids]
        self.native.records = {key: e for key, e in self.native.records.items() if e.sender not in names}
        entry_ids = {e.id for e in self.entries}
        self.native.communications.records = {key: e for key, e in self.native.communications.records.items() if e.id in entry_ids}
        self.selected = self.selected if self.selected in self.agents else next(iter(self.agents), "")
        self.show_all = True
        self.emit({"type": "replace", "entries": [wire_entry(e) for e in self.entries]})
        self.changed = {k: e for k, e in self.changed.items() if e in self.entries}

    async def handle(self, message):
        kind = message.get("type")
        if kind == "submit":
            accepted = False
            try:
                accepted = await self.submit(Prompt.load(message["prompt"]), message.get("quote"))
                if accepted:
                    prompt = message["prompt"]
                    if not self.input_history or self.input_history[-1] != prompt:
                        self.input_history.append(prompt)
                    self.dirty = True
            except (ValueError, OSError, CodexError) as exc:
                self.notify(exc)
            self.last_submission = (message.get("id"), accepted)
            self.emit({"type": "submitted", "id": message.get("id"), "accepted": accepted})
        elif kind == "complete":
            text, cursor = message["text"], message["cursor"]
            completion = self.completions.complete(text, cursor, self.agents, self.cwd,
                self.client.models if self.client else [], explicit=message.get("explicit", False))
            candidates = []
            if completion:
                for item in completion.candidates:
                    result, position = completion.apply(text, item)
                    suffix_length = len(text) - completion.end
                    replacement = result[completion.start:len(result)-suffix_length if suffix_length else len(result)]
                    candidates.append({**asdict(item), "text": result, "cursor": position,
                                       "start": completion.start, "end": completion.end, "replacement": replacement})
            self.emit({"type": "completion", "id": message.get("id"), "text": text, "cursor": cursor, "candidates": candidates})
        elif kind == "select":
            self.view_revision = message.get("id", self.view_revision)
            name = message.get("name")
            if name in self.agents:
                self.selected = name
            self.show_all = message.get("show_all", False)
            self.dirty = True
            self.members()
        elif kind == "collapse":
            entry = next((e for e in self.entries if e.id == message["id"]), None)
            if entry:
                entry.collapsed = message["collapsed"]
                self.refresh_answer(entry)
        elif kind == "drafts":
            self.drafts = message["drafts"]
            pending = message.get("pending")
            if pending and self.last_submission == (pending["id"], False):
                # Quit may arrive before the UI reads a rejection. Preserve the
                # original payload with anything typed after pressing Enter.
                view = pending["view"]
                original = Prompt.load(pending["prompt"])
                current = self.drafts.get(view, {})
                newer = Prompt.load(current.get("prompt", ""))
                offset = len(original.text) + (1 if original.text and newer.text else 0)
                text = original.text + ("\n" if original.text and newer.text else "") + newer.text
                attachments = original.attachments + [replace(a, start=a.start+offset, end=a.end+offset) for a in newer.attachments]
                self.drafts[view] = {"prompt": Prompt(text, attachments).dump(), "quote": pending.get("quote") or current.get("quote")}
            self.dirty = True
        elif kind == "answer":
            interaction = self.interactions.get(str(message["id"]))
            if interaction and not interaction[0].done():
                interaction[0].set_result(message["result"])
        elif kind == "paste":
            text = message["text"]
            paths = image_paths(text, self.cwd)
            self.emit({"type": "paste", "text": text, "images": paths, "id": message.get("id")})
        else:
            raise ValueError(f"未知界面消息：{kind}")
        self.flush()

    async def close(self):
        self.closing = True
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        try:
            async with asyncio.timeout(25):
                await asyncio.gather(*(self.stop(name) for name in list(self.agents)), return_exceptions=True)
        except TimeoutError:
            self.notify("停止确认超时，正在关闭本地 Codex 连接")
        finally:
            await self.native.close()
            if self.client:
                self.client.observer = None
                await self.client.close()
        for entry in self.entries:
            if entry.state in {"working", "queued"}:
                entry.state = "stopped"
                entry.finish_tools("stopped")
        self.dirty = True
        try:
            self.save()
        finally:
            if self.store:
                self.store.close()


async def serve(options):
    command = json.loads(os.environ["CODEX_ROOM_CODEX_COMMAND"]) if os.environ.get("CODEX_ROOM_CODEX_COMMAND") else None
    service = Service(Path(options.cwd), demo=options.demo, command=command)
    start = asyncio.create_task(service.start())

    async def ticker():
        last_save = monotonic()
        while True:
            await asyncio.sleep(0.033)
            service.flush()
            if monotonic() - last_save > 1:
                try:
                    service.save()
                except OSError as exc:
                    service.notify(f"保存失败：{clean_error(exc)}")
                last_save = monotonic()

    ticks = asyncio.create_task(ticker())
    try:
        while line := await asyncio.to_thread(sys.stdin.buffer.readline):
            message = json.loads(line)
            if message.get("type") == "shutdown":
                break
            try:
                await service.handle(message)
            except Exception as exc:
                service.notify(clean_error(exc))
    finally:
        start.cancel()
        ticks.cancel()
        await asyncio.gather(start, ticks, return_exceptions=True)
        await service.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cwd", default=str(Path.cwd()))
    parser.add_argument("--demo", action="store_true")
    options = parser.parse_args()
    try:
        asyncio.run(serve(options))
    except Exception as exc:
        print(json.dumps({"type": "fatal", "text": clean_error(exc)}), flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
