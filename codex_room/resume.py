"""Import an existing local root thread without executing a pasted command."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
import shlex
from uuid import UUID

from .attachments import Attachment
from .member_config import validate_name
from .models import Entry
from .native_agents import lineage, native_block


USAGE = "用 /resume 会话ID [成员名]，也可在 /resume 后粘贴 Orca 的完整恢复命令"


def parse_resume(value):
    try:
        tail = value.split(maxsplit=1)
        words = shlex.split(tail[1] if len(tail) == 2 else "")
    except ValueError as exc:
        raise ValueError("恢复命令的引号没有闭合") from exc
    if not words:
        return None
    name = None
    if len(words) <= 2:
        thread_id = words[0]
        name = words[1] if len(words) == 2 else None
    else:
        # Accept the copyable Orca command as data, never run a shell. Custom
        # CODEX_HOME assignments need a separate connection and are not guessed.
        if words[0] == "cd":
            if len(words) < 4 or words[2] != "&&":
                raise ValueError(USAGE)
            words = words[3:]
        if words and words[0] == "env":
            words = words[1:]
            while len(words) >= 2 and words[0] == "-u" and words[1] in {"CODEX_HOME", "ORCA_CODEX_HOME"}:
                words = words[2:]
        if (len(words) < 3 or Path(words[0]).name not in {"codex", "codex.exe"}
                or words[-2] != "resume" or any(w in {";", "&&", "||", "|", ">", "<"} for w in words)):
            raise ValueError(USAGE)
        thread_id = words[-1]
    try:
        thread_id = str(UUID(thread_id))
    except ValueError as exc:
        raise ValueError("会话 ID 应是完整的 UUID；可从 Orca 的「复制会话 ID」取得") from exc
    return thread_id, name


def member_name(thread, agents, requested=None):
    if requested:
        return validate_name(requested, agents)
    title = thread.get("name") or ""
    if len(title) > 32 or title.startswith("[Codex Room"):
        title = ""
    candidate = re.sub(r"[^\w-]+", "-", title).strip("-_") if title else ""
    if not candidate or not re.match(r"[^\W\d_]", candidate):
        candidate = re.sub(r"[^\w-]+", "-", Path(thread["cwd"]).name).strip("-_")
    if not candidate or not re.match(r"[^\W\d_]", candidate):
        candidate = "恢复会话"
    candidate = candidate[:24]
    for index in range(1, len(agents) + 3):
        name = candidate if index == 1 else f"{candidate}-{index}"
        try:
            return validate_name(name, agents)
        except ValueError:
            continue
    raise ValueError("无法自动分配成员名，请在会话 ID 后指定成员名")


def timestamp(value):
    return datetime.fromtimestamp(value).astimezone().isoformat(timespec="seconds") if value else None


def user_content(item):
    """Hide known host-only input parts in the view, leaving stored Codex data intact."""
    text, attachments = "", []
    for part in item.get("content", []):
        kind = part.get("type")
        if kind == "text":
            value = part.get("text", "")
            if (value.startswith(("[Codex Room host context;", "# AGENTS.md instructions for "))
                    or any(value.startswith(f"<{tag}>") and value.rstrip().endswith(f"</{tag}>")
                           for tag in ("environment_context", "user_instructions"))):
                continue
        elif kind == "localImage":
            value = f"[Image #{len(attachments) + 1}]"
        elif kind == "image":
            value = "[远程图片]"
        elif kind in {"skill", "mention"}:
            value = "$" + part.get("name", "") if kind == "skill" else "@" + part.get("name", "")
        else:
            continue
        if not value:
            continue
        if text:
            text += "\n"
        start = len(text)
        text += value
        if kind == "localImage":
            attachments.append(Attachment(start, len(text), "image", part["path"], value))
    return text, attachments


def history_entries(member, turns):
    """Keep user/assistant order, including user steers within one native turn."""
    result = []
    for turn in turns:
        turn_id = turn["id"]
        status = {"completed": "done", "failed": "failed", "interrupted": "stopped"}.get(turn.get("status"), "stopped")
        run_id = f"resume-{member.thread_id}-{turn_id}"
        started = timestamp(turn.get("startedAt"))
        common = dict(run_id=run_id, native_thread_id=member.thread_id, server_turn_id=turn_id,
                      started_at=started)
        answer = None
        seen = set()
        for item in turn.get("items", []):
            item_id = item.get("id") or str(len(seen))
            if item_id in seen:
                continue
            seen.add(item_id)
            if item.get("type") == "userMessage":
                text, attachments = user_content(item)
                if text:
                    result.append(Entry("you", text, member.name, attachments=attachments,
                        id=f"{run_id}-user-{item_id}", **common))
                    answer = None
                continue
            block = native_block(item, "completed" if status == "done" else status)
            if not block:
                continue
            if answer is None:
                answer = Entry(member.name, "", state=status, id=f"{run_id}-answer-{item_id}", **common)
                result.append(answer)
            answer.blocks.append(block)
            answer.sync_body()
        if answer:
            answer.finished_at = timestamp(turn.get("completedAt"))
            if turn.get("durationMs") is not None:
                answer.duration_seconds = turn["durationMs"] / 1000
            answer.finish_tools("completed" if status == "done" else status)
            if turn.get("error"):
                answer.append_text("错误：" + str(turn["error"].get("message", "执行失败")))
    return result


async def resume_member(service, value):
    parsed = parse_resume(value)
    if parsed is None:
        service.emit({"type": "prefill", "text": "/resume "})
        service.notify(USAGE)
        return
    thread_id, requested = parsed
    existing = next((m for m in service.agents.values() if m.thread_id == thread_id), None)
    if existing:
        service.selected, service.show_all = existing.name, False
        service.emit({"type": "action", "action": "bottom"})
        service.notify(f"已切到 @{existing.label} · 这段会话已在 Room 中")
        return
    if not service.connected or not service.client or service.demo:
        raise ValueError("请先连接 Codex，再恢复本机会话；/reconnect 重试")
    client = service.client
    try:
        thread = await client.read_native_thread(thread_id)
    except Exception as exc:
        from .runtime import CodexError, clean_error
        raise CodexError(f"无法读取会话 {thread_id}：{clean_error(exc)}。请确认它位于当前 Codex 数据目录中") from exc
    if lineage(thread)[0] or thread.get("canAcceptDirectInput") is False:
        raise ValueError("这是原生子代理会话，请恢复它的父会话，子代理会随父会话加入")
    if thread.get("status", {}).get("type") == "active" or thread_id in client.active:
        raise ValueError("这段会话正在运行，请等当前任务结束后再接入")
    from .member_config import resolve_directory
    cwd = str(resolve_directory(thread.get("cwd"), service.cwd)) if thread.get("cwd") else ""
    if not cwd:
        raise ValueError("原会话没有工作目录，无法安全恢复")
    name = member_name(thread, service.agents, requested)
    service.notify("正在读取会话历史…请在原窗口结束当前任务，接入后选择一端继续对话")
    # No cwd/model/sandbox overrides: let Codex restore the original settings.
    restored = await client.request("thread/resume", {"threadId": thread_id, "excludeTurns": True,
                                                    "config": service.peers.bridge.config()})
    cwd = str(resolve_directory(restored.get("cwd") or cwd, service.cwd))
    sandbox = restored.get("sandbox", {})
    mode = {"dangerFullAccess": "full", "workspaceWrite": "workspace", "readOnly": "read-only"}.get(sandbox.get("type"))
    if mode is None or "approvalPolicy" not in restored:
        raise ValueError("这段会话的权限类型暂不支持恢复；未改动其权限或发送新任务")
    from .backend import Member
    from .colors import choose_color
    member = Member(name, name, choose_color([a.color for a in service.agents.values()]),
        cwd=cwd, thread_id=thread_id, model=restored.get("model") or "", effort=restored.get("reasoningEffort") or "",
        permission=mode, service_tier=restored.get("serviceTier"), preserve_thread_title=True,
        resume_policy={"approvalPolicy": restored["approvalPolicy"], "sandboxPolicy": sandbox})
    turns = await client.native_turns(thread_id, page_size=20)
    if any(t.get("status") == "inProgress" for t in turns):
        raise ValueError("会话历史包含尚未结束的运行，请先在原窗口结束任务后重试")
    entries = history_entries(member, turns)
    # Commit to Room only after all metadata/history reads have succeeded.
    service.agents[name] = member
    client.loaded.add(thread_id)
    service.ignored_native_threads.discard(thread_id)
    for entry in entries:
        service.add_entry(entry)
    service.selected, service.show_all = name, False
    service.dirty = True
    service.flush()
    service.save()
    try:
        await service.native.discover(thread_id)
    except Exception as exc:
        from .runtime import clean_error
        service.notify("主会话已接入，子代理恢复失败：" + clean_error(exc))
    service.notify(f"已恢复 @{name} · {cwd} · {len(turns)} 轮历史 · 可用 /rename 改成员名")
