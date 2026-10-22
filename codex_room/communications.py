"""Visible native collaboration records, without inventing hidden payloads."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .models import Entry, ReplyBlock


class Communications:
    def __init__(self, native):
        self.native = native
        self.app = native.app
        self.records = {e.id: e for e in self.app.entries if e.communication and e.communication.get("transport") != "room"}

    def record(self, sender, item, turn_id, *, owner=None, completed=True, stamp=None):
        if item.get("type") == "subAgentActivity":
            activity = item.get("kind")
            if activity not in {"started", "interacted"}:
                return
            targets = [item.get("agentThreadId")]
            action = "分配任务" if activity == "started" else "发送消息"
            # V2 activity has no message field. The opaque arguments must not
            # be decoded, inferred from the reply, or labeled as user text.
            body, tool = None, "spawn_agent" if activity == "started" else "native"
        elif item.get("type") == "collabAgentToolCall":
            tool = item.get("tool", "")
            if tool not in {"spawnAgent", "sendInput", "sendMessage", "followupTask"}:
                return
            targets = item.get("receiverThreadIds", [])
            action = {"spawnAgent": "分配任务", "followupTask": "继续任务"}.get(tool, "发送消息")
            sender = self.native.member_for(item.get("senderThreadId")) or sender
            body = item.get("prompt")
            if not isinstance(body, str):
                body = None
        else:
            return
        for target_id in targets:
            if not target_id or target_id in self.app.ignored_native_threads:
                continue
            target = self.native.member_for(target_id)
            identity = f"{sender.thread_id}:{turn_id}:{item.get('id')}:{target_id}"
            key = "communication-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            entry = self.records.get(key)
            if entry is None:
                entry = Entry(sender.name, "", target.name if target else "", id=key)
                if stamp:
                    entry.time = datetime.fromtimestamp(stamp).astimezone().strftime("%H:%M")
                self.records[key] = entry
                self.app.add_entry(entry)
                if owner is not None:
                    self.app.entries.remove(entry)
                    index = self.app.entries.index(owner) + 1
                    while index < len(self.app.entries):
                        other = self.app.entries[index].communication or {}
                        if other.get("source_thread_id") != sender.thread_id or other.get("turn_id") != turn_id:
                            break
                        index += 1
                    self.app.entries.insert(index, entry)
                if sender.name in self.app.active_entries and not self.app.active_entries[sender.name][0].hidden:
                    self.app.reply_boundaries.add(sender.name)
            terminal = completed or item.get("status") in {"completed", "failed"}
            if entry.communication and entry.state != "working" and not terminal:
                continue
            entry.state = "failed" if item.get("status") == "failed" else "done" if terminal else "working"
            entry.target = target.name if target else entry.target
            available = body is not None or bool(entry.communication and entry.communication.get("content_available"))
            entry.communication = {"action": action, "tool": tool, "source_thread_id": sender.thread_id,
                "target_thread_id": target_id, "target_label": target.label if target else item.get("agentPath") or target_id,
                "turn_id": turn_id, "item_id": item.get("id"), "content_available": available}
            if body is not None:
                entry.body = body
                entry.blocks = [ReplyBlock("message", "text", body, status="completed")]
            self.app.refresh_answer(entry)

    def resolve(self, member):
        for entry in self.records.values():
            if entry.communication["target_thread_id"] == member.thread_id:
                entry.target = member.name
                entry.communication["target_label"] = member.label
                self.app.refresh_answer(entry)

    def finish(self, thread_id=None, turn_id=None, state="stopped"):
        for entry in self.records.values():
            info = entry.communication
            if entry.state == "working" and (thread_id is None or info["source_thread_id"] == thread_id) and (
                    turn_id is None or info["turn_id"] == turn_id):
                entry.state = state
                self.app.refresh_answer(entry)

    def backfill(self):
        """Recover protocol records already saved inside folded tool blocks."""
        before = len(self.records)
        for entry in list(self.app.entries):
            sender = self.app.agents.get(entry.sender)
            if not sender or entry.communication:
                continue
            for block in entry.blocks:
                if block.kind != "tool":
                    continue
                try:
                    item = json.loads(block.text)
                except (ValueError, TypeError):
                    continue
                if isinstance(item, dict):
                    if block.status != "inProgress" and item.get("status") in {None, "inProgress"}:
                        item["status"] = block.status
                    stamp = datetime.fromisoformat(entry.started_at).timestamp() if entry.started_at else None
                    self.record(sender, item, entry.server_turn_id or entry.run_id or entry.id,
                                owner=entry, completed=block.status != "inProgress", stamp=stamp)
        return len(self.records) != before
