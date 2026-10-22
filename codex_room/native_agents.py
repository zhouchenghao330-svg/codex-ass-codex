"""Expose real Codex child threads, preserving their identity and parent tree.

V2 children reject direct input. User messages are relayed by their existing
root with followup_task; this module never impersonates a new independent child.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from uuid import uuid4

from .colors import choose_color
from .models import Entry, ReplyBlock
from .runtime import CodexError, clean_error
from .communications import Communications


def lineage(thread):
    source = thread.get("source")
    sub = source.get("subAgent", {}) if isinstance(source, dict) else {}
    spawn = sub.get("thread_spawn", {}) if isinstance(sub, dict) else {}
    return thread.get("parentThreadId") or spawn.get("parent_thread_id", ""), spawn.get("agent_path", "")


def native_block(item, status="completed"):
    kind, key = item.get("type"), item.get("id", "")
    state = item.get("status") or status
    if kind == "agentMessage":
        return ReplyBlock(key, "text", item.get("text", ""), status=state)
    if kind == "commandExecution":
        code = item.get("exitCode")
        return ReplyBlock(key, "command", item.get("aggregatedOutput") or "", item.get("command", ""),
                          "failed" if code not in {None, 0} else state, code)
    if kind == "fileChange":
        changes = item.get("changes", [])
        return ReplyBlock(key, "file", json.dumps(changes, ensure_ascii=False, indent=2),
                          ", ".join(c.get("path", "") for c in changes), state)
    if kind in {None, "userMessage", "reasoning", "contextCompaction"}:
        return None
    title = "/".join(filter(None, (item.get("server"), item.get("tool")))) or item.get("query") or kind
    if item.get("success") is False or item.get("error"):
        state = "failed"
    return ReplyBlock(key, "tool", json.dumps(item, ensure_ascii=False, indent=2), title, state)


class NativeAgents:
    def __init__(self, service):
        self.app = service
        self.queue = asyncio.Queue()
        self.listener = None
        self.jobs = set()
        self.claimed = set()
        self.records = {(e.native_thread_id, e.server_turn_id): e for e in service.entries
                        if e.native_thread_id and e.server_turn_id and e.sender != "you"}
        self.attached = set()
        self.adopting = {}
        self.streams = {}
        self.finished_items = set()
        self.finished_turns = set()
        self.pending = {}
        self.relay_locks = {}
        self.activities = {}
        self.activity_sequence = 0
        self.seen_activities = {}
        self.suspended = set()
        self.communications = Communications(self)

    def member_for(self, thread_id):
        return next((a for a in self.app.agents.values() if a.thread_id == thread_id), None)

    def root_for(self, member):
        seen = set()
        while member.parent_name:
            if member.name in seen or member.parent_name not in self.app.agents:
                raise ValueError("子代理的父会话尚未恢复，请先 /reconnect")
            seen.add(member.name)
            member = self.app.agents[member.parent_name]
        return member

    def observe(self, event):
        """Called synchronously by the transport, including before resume returns."""
        params = event.get("params", {})
        item = params.get("item", {})
        if event["method"] == "item/completed" and item.get("kind") == "interacted":
            key = params.get("threadId"), params.get("turnId"), item.get("id")
            if key not in self.seen_activities:
                self.activity_sequence += 1
                self.seen_activities[key] = self.activity_sequence
            event["_native_sequence"] = self.seen_activities[key]
        thread = params.get("thread", {})
        parent, _ = lineage(thread)
        thread_id = params.get("threadId") or thread.get("id")
        if parent and (self.member_for(parent) or parent in self.claimed):
            self.claimed.add(thread_id)
        if event["method"] == "transport/error" or parent or self.member_for(thread_id) or thread_id in self.claimed or "id" in event:
            # Root run_turn owns its own interactive requests.
            if "id" not in event or thread_id not in self.app.client.queues:
                self.queue.put_nowait(event)
        return bool("id" in event and thread_id not in self.app.client.queues)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.jobs.add(task)
        def done(task):
            self.jobs.discard(task)
            if not task.cancelled() and task.exception():
                self.app.notify("子代理：" + clean_error(task.exception()))
        task.add_done_callback(done)
        return task

    async def start(self):
        communication_count = len(self.communications.records)
        self.listener = asyncio.create_task(self.listen())
        # Restoring one missing child must not disconnect unrelated roots.
        for member in list(self.app.agents.values()):
            if member.thread_id and not member.parent_name:
                try:
                    await self.discover(member.thread_id)
                except Exception as exc:
                    self.app.notify(f"@{member.label} 恢复：{clean_error(exc)}")
        for member in list(self.app.agents.values()):
            if member.parent_name and member.thread_id not in self.attached:
                try:
                    await self.adopt(member.thread_id)
                except Exception as exc:
                    member.native_closed = True
                    self.app.notify(f"@{member.label} 恢复失败：{clean_error(exc)}")
        backfilled = self.communications.backfill()
        if backfilled or len(self.communications.records) != communication_count:
            from .backend import wire_entry
            self.app.emit({"type": "replace", "entries": [wire_entry(e) for e in self.app.entries]})

    async def close(self):
        tasks = list(self.jobs) + list(self.adopting.values()) + ([self.listener] if self.listener else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.listener = None
        self.jobs.clear()
        for request, wrapper, _ in self.pending.values():
            if request.state == "queued":
                request.state = "stopped"
                self.app.refresh_answer(request)
        self.pending.clear()

    async def discover(self, root_id):
        cursor = None
        while True:
            query = {"ancestorThreadId": root_id, "sourceKinds": ["subAgentThreadSpawn"], "limit": 100}
            if cursor:
                query["cursor"] = cursor
            page = await self.app.client.request("thread/list", query)
            for thread in reversed(page.get("data", [])):
                try:
                    await self.adopt(thread["id"], thread)
                except Exception as exc:
                    self.app.notify("子代理恢复失败：" + clean_error(exc))
            cursor = page.get("nextCursor")
            if not cursor:
                break

    async def adopt(self, thread_id, metadata=None, parent_id="", ancestors=()):
        if thread_id in self.app.ignored_native_threads or thread_id in ancestors:
            return None
        if thread_id in self.attached:
            return self.member_for(thread_id)
        if thread_id in self.adopting:
            return await asyncio.shield(self.adopting[thread_id])
        async def attach():
            client = self.app.client
            thread = metadata or await client.read_native_thread(thread_id)
            parent, path = lineage(thread)
            parent = parent or parent_id
            member = self.member_for(thread_id)
            if member and not member.parent_name:
                return None
            owner = self.member_for(parent)
            if not owner and parent:
                owner = await self.adopt(parent, ancestors=(*ancestors, thread_id))
            if not owner:
                return None
            if owner.parent_name and owner.thread_id not in self.attached:
                owner = await self.adopt(owner.thread_id, ancestors=(*ancestors, thread_id))
                if not owner:
                    return None
            # Only roots with actual children are loaded at startup. Unrelated
            # idle members must not eagerly start their MCP processes.
            await client.ensure_thread(self.root_for(owner))
            if member is None:
                from .backend import Member
                label = path.rsplit("/", 1)[-1] or thread.get("agentNickname") or "子代理"
                label = re.sub(r"[^\w-]", "-", label).strip("-")[:26] or "子代理"
                if not label[0].isalpha():
                    label = "子代理-" + label
                base, i = label, 2
                used = {v.casefold() for a in self.app.agents.values() for v in (a.name, a.label)}
                while label.casefold() in used:
                    label, i = f"{base}-{i}", i + 1
                member = Member("native-" + uuid4().hex[:12], label,
                                choose_color([a.color for a in self.app.agents.values()]),
                                cwd=thread.get("cwd") or owner.cwd, thread_id=thread_id,
                                parent_name=owner.name, agent_path=path, permission=owner.permission)
                self.app.agents[member.name] = member
                self.app.notify(f"@{owner.label} 的子代理 @{label} 已加入")
            member.agent_path = path or member.agent_path
            self.claimed.add(thread_id)
            attached = await client.attach_native_thread(thread_id)
            thread = {**thread, **attached}
            member.can_accept_direct_input = thread.get("canAcceptDirectInput") is True
            member.model = thread.get("model") or member.model or owner.model
            member.effort = thread.get("reasoningEffort") or member.effort or owner.effort
            member.cwd = thread.get("cwd") or member.cwd
            member.native_closed = False
            self.attached.add(thread_id)
            self.communications.resolve(member)
            await self.hydrate(member)
            self.app.members_dirty = self.app.dirty = True
            return member
        task = asyncio.create_task(attach())
        self.adopting[thread_id] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self.adopting.pop(thread_id, None)

    def entry_for(self, member, turn_id, stamp=None):
        key = member.thread_id, turn_id
        if key not in self.records:
            started = datetime.fromtimestamp(stamp).astimezone() if stamp else datetime.now().astimezone()
            self.records[key] = self.app.add_entry(Entry(member.name, "", state="working",
                started_at=started.isoformat(timespec="seconds"), run_id="native-" + turn_id,
                native_thread_id=member.thread_id, server_turn_id=turn_id))
        return self.records[key]

    def merge(self, entry, block, *, authoritative=False):
        old = next((b for b in entry.blocks if b.id == block.id), None)
        if old is None:
            entry.blocks.append(block)
        elif authoritative or old.status == "inProgress" and block.status != "inProgress":
            entry.blocks[entry.blocks.index(old)] = block
        elif block.kind in {"text", "command"} and block.text.startswith(old.text):
            old.text = block.text
            old.title = block.title or old.title
        entry.sync_body()
        self.app.refresh_answer(entry)

    async def hydrate(self, member):
        for turn in await self.app.client.native_turns(member.thread_id):
            self.sync_turn(member, turn)

    def sync_turn(self, member, turn):
        turn_id = turn["id"]
        entry = self.entry_for(member, turn_id, turn.get("startedAt"))
        terminal = turn.get("status") in {"completed", "failed", "interrupted"}
        key = member.thread_id, turn_id
        # A snapshot or start notification can arrive behind a terminal event.
        if key in self.finished_turns and not terminal:
            return
        for item in turn.get("items", []):
            self.communications.record(member, item, turn_id, owner=entry, completed=terminal, stamp=turn.get("startedAt"))
            block = native_block(item, "completed" if terminal else "inProgress")
            if block:
                self.merge(entry, block)
                if terminal:
                    self.finished_items.add((*key, block.id))
        if turn.get("startedAt"):
            entry.started_at = datetime.fromtimestamp(turn["startedAt"]).astimezone().isoformat(timespec="seconds")
        if terminal:
            self.finished_turns.add(key)
            if self.app.client.active.get(member.thread_id) == turn_id:
                self.app.client.active.pop(member.thread_id, None)
            entry.state = {"completed": "done", "failed": "failed", "interrupted": "stopped"}[turn["status"]]
            entry.finish_tools("completed" if entry.state == "done" else entry.state)
            if turn.get("error") and not any(b.id == "native-error" for b in entry.blocks):
                self.merge(entry, ReplyBlock("native-error", "text", "错误：" + clean_error(turn["error"].get("message", turn["error"]))))
            end = turn.get("completedAt")
            if end:
                entry.finished_at = datetime.fromtimestamp(end).astimezone().isoformat(timespec="seconds")
                entry.duration_seconds = turn["durationMs"] / 1000 if turn.get("durationMs") is not None else max(0, end - datetime.fromisoformat(entry.started_at).timestamp())
            else:
                entry.record_finish(datetime.now().timestamp() - datetime.fromisoformat(entry.started_at).timestamp())
        else:
            self.app.client.active[member.thread_id] = turn_id
            # A locally stopped card restored from disk can resume a live turn.
            entry.state, entry.finished_at, entry.duration_seconds = "working", None, None
        member.busy = member.thread_id in self.app.client.active
        member.phase = "正在思考…" if member.busy else "空闲"
        self.app.refresh_answer(entry)

    async def listen(self):
        while True:
            event = await self.queue.get()
            try:
                await self.event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if "id" in event:
                    try:
                        await self.app.client.send({"id": event["id"], "error": {"code": -32603, "message": "Child request unavailable"}})
                    except CodexError:
                        pass
                self.app.notify("子代理同步失败：" + clean_error(exc))
            finally:
                self.queue.task_done()

    async def event(self, event):
        method, params = event["method"], event.get("params", {})
        client = self.app.client
        if method == "transport/error":
            self.app.connected = False
            self.communications.finish()
            for member in self.app.agents.values():
                if member.parent_name:
                    member.busy, member.phase = False, "连接已断开"
                    client.active.pop(member.thread_id, None)
            for entry in self.records.values():
                if entry.state == "working":
                    entry.state = "stopped"
                    entry.finish_tools("stopped")
                    entry.record_finish(datetime.now().timestamp() - datetime.fromisoformat(entry.started_at).timestamp())
                    self.app.refresh_answer(entry)
            return
        if method == "thread/started":
            thread = params.get("thread", {})
            if lineage(thread)[0]:
                await self.adopt(thread["id"], thread)
            return
        thread_id = params.get("threadId", "")
        member = self.member_for(thread_id)
        if not member and "id" in event:
            member = await self.adopt(thread_id)
        if not member:
            if "id" in event:
                await client.send({"id": event["id"], "error": {"code": -32601, "message": "Thread does not belong to this room"}})
            return
        item = params.get("item", {})
        if method == "turn/completed":
            turn = params.get("turn", {})
            state = {"completed": "done", "failed": "failed", "interrupted": "stopped"}.get(turn.get("status"), "stopped")
            self.communications.finish(thread_id, turn.get("id"), state)
        if method in {"item/started", "item/completed"}:
            ids = [item["agentThreadId"]] if item.get("agentThreadId") else []
            if item.get("tool") in {"spawnAgent", "spawn_agent"}:
                ids.extend(item.get("receiverThreadIds", []))
            for child_id in ids:
                child = await self.adopt(child_id, parent_id=thread_id)
                if child and item.get("kind") == "interacted" and method == "item/completed":
                    self.activities[thread_id, params.get("turnId"), child_id] = event.get("_native_sequence", 0)
                if child and item.get("kind") in {"completed", "interrupted"}:
                    await self.hydrate(child)
            self.communications.record(member, item, params.get("turnId", ""), completed=method == "item/completed")
        if thread_id in client.queues:
            return
        if "id" in event:
            if member.name in self.suspended:
                await client.send({"id": event["id"], "error": {"code": -32600, "message": "Child stopped by user"}})
                return
            self.spawn(self.app.server_request(member.name, event))
            return
        if not member.parent_name:
            return
        if method == "thread/closed":
            member.native_closed = True
            client.active.pop(thread_id, None)
            member.busy, member.phase = False, "已关闭"
            for (tid, _), entry in self.records.items():
                if tid == thread_id and entry.state == "working":
                    entry.state = "stopped"
                    entry.finish_tools("stopped")
                    entry.record_finish(datetime.now().timestamp() - datetime.fromisoformat(entry.started_at).timestamp())
                    self.app.refresh_answer(entry)
            self.app.members_dirty = self.app.dirty = True
            return
        turn = params.get("turn", {})
        turn_id = params.get("turnId") or turn.get("id")
        if not turn_id:
            return
        if method in {"turn/started", "turn/completed"}:
            previous = self.records.get((thread_id, turn_id))
            was_active = previous is None or previous.state == "working"
            self.sync_turn(member, turn)
            if method == "turn/started" and member.name in self.suspended:
                self.spawn(self.interrupt(member))
            if method == "turn/completed" and was_active and not self.app.closing:
                self.app.notify(f"@{member.label} 已完成")
            return
        entry = self.entry_for(member, turn_id)
        key = thread_id, turn_id, params.get("itemId") or item.get("id", "")
        if (thread_id, turn_id) in self.finished_turns:
            return
        if method in {"item/agentMessage/delta", "item/commandExecution/outputDelta"}:
            if key not in self.finished_items:
                self.streams[key] = self.streams.get(key, "") + params.get("delta", "")
                self.merge(entry, ReplyBlock(key[2], "text" if method == "item/agentMessage/delta" else "command", self.streams[key]))
            member.phase = "正在回复…" if method == "item/agentMessage/delta" else "正在执行命令…"
        elif method in {"item/started", "item/completed"}:
            if key in self.finished_items:
                return
            block = native_block(item, "inProgress" if method == "item/started" else "completed")
            if block:
                self.merge(entry, block, authoritative=method == "item/completed")
                member.phase = "正在回复…" if block.kind == "text" else "正在调用工具…"
            if method == "item/completed":
                self.finished_items.add(key)
                self.streams.pop(key, None)
        elif method == "item/reasoning/summaryTextDelta":
            # Only the public summary headline, never raw reasoning content.
            from .backend import summary_title
            self.streams[key] = (self.streams.get(key, "") + params.get("delta", ""))[:512]
            member.phase = "正在思考：" + summary_title(self.streams[key])
        self.app.members_dirty = True

    async def relay(self, member, request):
        root = self.root_for(member)
        lock = self.relay_locks.setdefault(root.name, asyncio.Lock())
        wrapper = None
        try:
            async with lock:
                if request.state == "stopped" or member.name not in self.app.agents:
                    return
                payload = {"message_id": request.id, "text": request.input_text, "images": request.image_paths}
                body = ("这是 Codex Room 用户发给现有子代理的消息。请只完成转交，不代替它执行任务。\n"
                        f"目标：{member.agent_path or member.thread_id}\n"
                        "使用 followup_task（旧版 send_input）原样投递下面 JSON 的 text，并把 images 中的路径作为本地图片输入传给同一子代理。"
                        "若它正在运行，追加这条消息。不得创建、替换、关闭代理。转交后结束本轮，用户会在 Room 查看子代理原话。"
                        "如果无法转交，明确说明原因。\n" + json.dumps(payload, ensure_ascii=False))
                wrapper = self.app.add_entry(Entry("you", body, root.name, "queued", hidden=True, relay_for=request.id))
                self.pending[request.id] = request, wrapper, member.name
                floor = self.activity_sequence
                self.app.enqueue(root, wrapper)
                # Wait for the parent turn, including an in-flight steer ACK.
                while wrapper.state == "queued" or (root.name in self.app.active_entries and
                        wrapper.run_id == self.app.active_entries[root.name][1].run_id):
                    if self.app.closing:
                        return
                    await asyncio.sleep(.03)
                # Listener can trail the parent's completion by a few events.
                await self.queue.join()
                if request.state == "stopped":
                    return
                confirmed = self.activities.get((root.thread_id, wrapper.server_turn_id, member.thread_id), 0) > floor
                request.state = "done" if confirmed else "failed"
                if not confirmed:
                    self.app.notify(f"@{member.label} 的转交未确认，请查看父代理回复；未自动重发")
                    for entry in self.app.entries:
                        if entry.relay_for == request.id and entry.sender != "you":
                            entry.hidden = False
                            self.app.refresh_answer(entry)
                self.app.refresh_answer(request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            request.state = "failed"
            self.app.refresh_answer(request)
            self.app.notify("转交未确认：" + clean_error(exc) + "；未自动重发")
        finally:
            self.pending.pop(request.id, None)

    async def interrupt(self, member):
        client = self.app.client
        turn_id = client.active.get(member.thread_id)
        if not turn_id:
            await self.hydrate(member)
            turn_id = client.active.get(member.thread_id)
        if not turn_id:
            return
        try:
            await client.request("turn/interrupt", {"threadId": member.thread_id, "turnId": turn_id}, timeout=10)
        except CodexError as exc:
            if exc.code in {-32600, -32602} and re.search(r"no active turn", str(exc), re.I):
                await self.hydrate(member)
                if member.thread_id not in client.active:
                    return
            raise
        async with asyncio.timeout(10):
            while client.active.get(member.thread_id) == turn_id:
                await asyncio.sleep(.03)

    async def stop(self, member):
        self.suspended.add(member.name)
        for request, wrapper, name in list(self.pending.values()):
            if name == member.name:
                request.state = "stopped"
                self.app.refresh_answer(request)
                root = self.root_for(member)
                if wrapper in root.queue:
                    root.queue.remove(wrapper)
                    wrapper.state = "stopped"
        await self.interrupt(member)
        # Approval tasks are not owned by run_turn for observed children.
        for future, event, name in list(self.app.interactions.values()):
            if name == member.name and not future.done():
                future.cancel()

    async def wait_before_removal(self, names):
        # Do not unsubscribe while an already-delivered parent instruction can
        # still wake this child. Keep observing/intercepting it until drained.
        try:
            async with asyncio.timeout(10):
                while (any(name in names for _, _, name in self.pending.values()) or
                       any(self.app.agents[name].busy for name in names if self.app.agents[name].parent_name)):
                    await asyncio.sleep(.03)
        except TimeoutError as exc:
            raise CodexError("子代理已请求停止，但父会话仍在处理转交。成员已保留；稍后重试删除，或先停止父会话。") from exc
