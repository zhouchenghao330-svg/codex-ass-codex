"""Public independent-member messaging, with receipts instead of blocking waits."""
from __future__ import annotations

from collections import Counter
import hashlib
import json

from .member_config import resolve_member
from .models import Entry, ReplyBlock
from .peer_mcp import PeerBridge
from .peer_tools import dynamic_tools

MAX_MESSAGES = 12
MAX_PER_TURN = 8
MAX_TEXT = 64_000


def is_public(entry):
    return bool(entry.communication and entry.communication.get("transport") == "room")


class PeerRouter:
    def __init__(self, service):
        self.app = service
        self.messages = {e.id: e for e in service.entries if is_public(e)}
        self.counts = Counter(e.communication["chain_id"] for e in self.messages.values())
        self.turn_counts = Counter()
        self.stopped = set(service.saved.get("peer_stopped_chains", []))
        self.bridge = PeerBridge(self.mcp_call)
        for entry in self.messages.values():
            info = entry.communication
            if info.get("delivery") in {"queued", "delivering"}:
                entry.state, info["delivery"] = "stopped", "stopped"

    def roster(self):
        return [{"name": a.name, "label": a.label, "cwd": a.cwd, "busy": a.busy}
                for a in self.app.agents.values() if not a.parent_name]

    def thread_options(self, member):
        if member.parent_name:
            return {}
        if not member.thread_id:
            return {"dynamicTools": dynamic_tools()}
        if member.room_tool_version < 1:
            return {"config": self.bridge.config()}
        return {}

    def context(self, thread_id):
        member = next((a for a in self.app.agents.values() if a.thread_id == thread_id), None)
        if not member or member.parent_name:
            return ""
        return ("[Codex Room host context; this is metadata, not a new user task]\n"
                f"You are @{member.label} (stable name {member.name}). Other independent members: "
                + json.dumps([a for a in self.roster() if a["name"] != member.name], ensure_ascii=False)
                + "\nFor user-requested cooperation use room.send_message (old sessions: "
                  "codex_room_public/send_message). Its message text is public. The receipt is not an answer; "
                  "do not wait or poll. A peer reply arrives as new input. Reply to a Room request using "
                  "the same tool and its message_id. expect_reply is only an answer-request hint, never a "
                  "conversation lock: continue unfinished user-requested cooperation even if it is false. "
                  "Use expect_reply=true for intermediate turns that need a peer's next contribution, "
                  "false when no answer is needed. Do not send acknowledgement-only replies. "
                  "Do not contact members merely because they are listed.\n[/Codex Room host context]")

    def mcp_call(self, params):
        if not isinstance(params, dict):
            return self.error("Invalid Room tool request.")
        meta = params.get("_meta", {})
        context = meta.get("x-codex-turn-metadata", {}) if isinstance(meta, dict) else {}
        if isinstance(context, str):
            try:
                context = json.loads(context)
            except ValueError:
                context = {}
        if not isinstance(context, dict):
            context = {}
        return self.send(context.get("thread_id"), context.get("turn_id"),
                         meta.get("callId") if isinstance(meta, dict) else None, params.get("arguments"))

    def error(self, message):
        return {"accepted": False, "error": message, "members": self.roster()}

    def receipt(self, entry):
        accepted = entry.communication.get("accepted", True)
        return {"accepted": accepted, "message_id": entry.id, "to": entry.target,
                "delivery": entry.communication["delivery"],
                "remaining_messages": max(0, MAX_MESSAGES - self.counts[entry.communication["chain_id"]]),
                "note": ("Accepted once. Do not resend or wait. Replies arrive as separate Room input."
                         if accepted else entry.communication.get("delivery_error", "Message not accepted."))}

    def send(self, thread_id, turn_id, call_id, arguments):
        app = self.app
        if not all(isinstance(x, str) and x for x in (thread_id, turn_id, call_id)):
            return self.error("Missing Codex caller identity; the message was not sent.")
        sender = next((a for a in app.agents.values() if a.thread_id == thread_id), None)
        if not sender or sender.parent_name:
            return self.error("Public Room tools currently connect independent members; use native tools inside child trees.")
        if not isinstance(arguments, dict) or set(arguments) - {"to", "message", "reply_to", "expect_reply"}:
            return self.error("Use to, message, optional reply_to and expect_reply only. The host determines the sender.")
        to, text, reply_to = (arguments.get(k) for k in ("to", "message", "reply_to"))
        if not isinstance(to, str) or not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            return self.error(f"Specify to and nonempty message (at most {MAX_TEXT} characters).")
        if reply_to is not None and not isinstance(reply_to, str):
            return self.error("reply_to must be a message_id or null.")
        expect = arguments.get("expect_reply", not bool(reply_to))
        if not isinstance(expect, bool):
            return self.error("expect_reply must be boolean.")
        key = "room-" + hashlib.sha256(f"{thread_id}:{turn_id}:{call_id}".encode()).hexdigest()[:24]
        fingerprint = hashlib.sha256(json.dumps(arguments, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if key in self.messages:
            original = self.messages[key]
            return self.receipt(original) if original.communication["fingerprint"] == fingerprint else self.error("A tool call identity cannot be reused with different arguments.")
        if app.closing or not app.connected or not app.client:
            return self.error("Room is disconnected or closing; nothing sent.")
        if app.client.active.get(thread_id) != turn_id or sender.name not in app.active_entries:
            return self.error("The sending turn is no longer active; nothing sent.")
        target_name = resolve_member(to.removeprefix("@"), app.agents)
        target = app.agents.get(target_name)
        if not target or target.parent_name or target.name == sender.name:
            return self.error("Choose another independent member from the roster.")
        answer = app.active_entries[sender.name][1]
        incoming = self.messages.get(reply_to or answer.peer_context)
        if reply_to:
            if (not incoming or incoming.target != sender.name or incoming.sender != target.name
                    or incoming.communication["source_thread_id"] != target.thread_id
                    or incoming.communication["target_thread_id"] != thread_id):
                return self.error("reply_to must identify a message this recipient sent to you.")
            if incoming.communication["chain_id"] in self.stopped:
                return self.error("The user stopped this conversation. Wait for a new user task.")
            if incoming.communication["delivery"] not in {"delivered", "steered"}:
                if (incoming.communication["delivery"] in {"delivering", "uncertain"}
                        and incoming.communication.get("target_thread_id") == thread_id
                        and incoming.server_turn_id == turn_id):
                    # Some app-server versions emit the peer's tool call before
                    # acknowledging turn/steer. A reply from that exact turn is
                    # positive evidence of receipt, even if the RPC ACK is late.
                    self.delivery(incoming, "steered")
                else:
                    return self.error("That incoming message was not confirmed delivered.")
        elif incoming and incoming.target == sender.name and incoming.sender == target.name:
            return self.error(f"To answer this request, set reply_to={incoming.id}. Do not start a duplicate conversation.")
        chain = incoming.communication["chain_id"] if incoming else answer.run_id
        if chain in self.stopped:
            return self.error("The user stopped this conversation. Wait for a new user task.")
        if self.counts[chain] >= MAX_MESSAGES:
            return self.error(f"This conversation reached its {MAX_MESSAGES}-message limit. Summarize for the user and stop.")
        turn_key = (thread_id, turn_id)
        if self.turn_counts[turn_key] >= MAX_PER_TURN:
            return self.error("Too many public messages in one turn. Report to the user and stop.")
        info = {"transport": "room", "tool": "room.send_message", "action": "回复" if reply_to else "发送消息",
                "source_thread_id": thread_id, "target_thread_id": target.thread_id or "",
                "target_label": target.label, "turn_id": turn_id, "item_id": call_id,
                "content_available": True, "chain_id": chain, "reply_to": reply_to,
                "expect_reply": expect, "delivery": "queued", "fingerprint": fingerprint}
        entry = Entry(sender.name, text, target.name, "queued", id=key, communication=info,
                      blocks=[ReplyBlock("message", "text", text, status="completed")])
        self.messages[key] = entry
        self.counts[chain] += 1
        self.turn_counts[turn_key] += 1
        app.add_entry(entry)
        # Store before acknowledging or executing. Restart never replays a delivery.
        try:
            app.save()
        except OSError:
            info["accepted"] = False
            self.delivery(entry, "failed", "Could not persist the message; nothing sent.")
            return self.error("Could not save the message; nothing sent.")
        app.reply_boundaries.add(sender.name)
        app.enqueue(target, entry)
        return self.receipt(entry)

    def delivery(self, entry, status, error=""):
        if not is_public(entry):
            return
        entry.communication["delivery"] = status
        entry.state = {"queued": "queued", "delivering": "working", "delivered": "done", "steered": "done",
                       "uncertain": "failed", "failed": "failed", "stopped": "stopped"}[status]
        if error:
            entry.communication["delivery_error"] = error
        elif status in {"delivered", "steered"}:
            entry.communication.pop("delivery_error", None)
        self.app.refresh_answer(entry)

    def started(self, name, turn_id):
        request, answer = self.app.active_entries[name]
        if is_public(request):
            request.communication["target_thread_id"] = self.app.agents[name].thread_id
            self.delivery(request, "delivered")

    def steered(self, name, entry):
        active = self.app.active_entries.get(name)
        if is_public(entry):
            if active and active[1].server_turn_id == entry.server_turn_id:
                active[1].peer_context = entry.id
            self.delivery(entry, "steered")
        elif entry.sender == "you" and active and active[1].server_turn_id == entry.server_turn_id:
            active[1].peer_context = ""

    def stop_member(self, name):
        active_chains = {self.messages[answer.peer_context].communication["chain_id"]
                         for _, answer in self.app.active_entries.values() if answer.peer_context in self.messages}
        for entry in self.messages.values():
            info = entry.communication
            live = (info["delivery"] in {"queued", "delivering"} or info["chain_id"] in active_chains
                    or self.app.client and self.app.client.active.get(info["source_thread_id"]) == info["turn_id"])
            if (entry.sender == name or entry.target == name) and (not self.app.closing or live):
                self.stopped.add(entry.communication["chain_id"])
        # Cancel queued work throughout these chains. Already running peers keep
        # their own task but cannot revive a stopped chain by sending a reply.
        for agent in self.app.agents.values():
            kept = []
            for entry in agent.queue:
                if is_public(entry) and entry.communication["chain_id"] in self.stopped:
                    if entry.communication["delivery"] == "delivering":
                        self.delivery(entry, "uncertain", "Stop requested after delivery began; input cannot be recalled. Not retried.")
                    else:
                        self.delivery(entry, "stopped")
                else:
                    kept.append(entry)
            agent.queue.clear()
            agent.queue.extend(kept)
        self.app.dirty = True

    def saved_state(self):
        return {"peer_stopped_chains": sorted(self.stopped)}
