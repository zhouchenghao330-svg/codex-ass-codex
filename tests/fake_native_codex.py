"""Deterministic parent-owned V2 peer, including early child approvals."""
import asyncio
import json
import sys
import time
from pathlib import Path

store = Path("native-fixture.json")
state = json.loads(store.read_text()) if store.exists() else {"threads": {}, "turns": {}, "seq": 0}
tasks, requests = {}, {}


def send(message):
    params = message.get("params", {})
    tid = params.get("threadId")
    turn = params.get("turn")
    if turn:
        old = state["turns"].setdefault(tid, {}).get(turn["id"], {})
        state["turns"][tid][turn["id"]] = {**old, **turn, "items": old.get("items", [])}
    if message.get("method") == "item/completed":
        state["turns"].setdefault(tid, {}).setdefault(params["turnId"], {"id": params["turnId"], "items": []})["items"].append(params["item"])
    store.write_text(json.dumps(state))
    print(json.dumps(message), flush=True)


def turn_start(tid):
    state["seq"] += 1
    turn = {"id": f"t-{state['seq']}", "status": "inProgress", "items": [], "startedAt": int(time.time())}
    send({"method": "turn/started", "params": {"threadId": tid, "turn": turn}})
    return turn


def completed(tid, turn, status="completed"):
    send({"method": "turn/completed", "params": {"threadId": tid, "turn": {
        **turn, "status": status, "completedAt": int(time.time()), "durationMs": 123,
        "error": {"message": "child failed"} if status == "failed" else None}}})


def item(tid, turn, value):
    send({"method": "item/completed", "params": {"threadId": tid, "turnId": turn["id"], "item": value}})


async def child_run(tid, turn, text, approval=False):
    try:
        if approval:
            rid = "approval-" + turn["id"]
            future = requests[rid] = asyncio.get_running_loop().create_future()
            send({"id": rid, "method": "item/commandExecution/requestApproval", "params": {
                "threadId": tid, "turnId": turn["id"], "command": "printf fixture"}})
            text = "APPROVED:" + json.dumps(await future)
        if text == "slow":
            await asyncio.sleep(30)
        if text == "recall":
            text = state["threads"][tid].get("memory", "ORIGINAL_CONTEXT")
        if text.startswith("remember "):
            state["threads"][tid]["memory"] = text[9:]
        if text == "tools":
            item(tid, turn, {"id": "cmd", "type": "commandExecution", "command": "false", "exitCode": 1, "status": "completed", "aggregatedOutput": "FAILED_TOOL"})
            item(tid, turn, {"id": "file", "type": "fileChange", "changes": [{"path": "a.py", "kind": {"type": "update"}, "diff": "@@ -1 +1 @@\n-old\n+new"}]})
        for part in ("CHILD:", text):
            send({"method": "item/agentMessage/delta", "params": {"threadId": tid, "turnId": turn["id"], "itemId": "msg", "delta": part}})
            await asyncio.sleep(.03)
        item(tid, turn, {"id": "msg", "type": "agentMessage", "text": "CHILD:" + text})
        completed(tid, turn, "failed" if text == "fail" else "completed")
    except asyncio.CancelledError:
        completed(tid, turn, "interrupted")


async def relay(tid, turn, text):
    payload = json.loads(text.splitlines()[-1])
    child = next(t for t in state["threads"].values() if t.get("parentThreadId") == tid)
    if payload["text"] == "no-relay":
        return
    child_turn = turn_start(child["id"])
    tasks[child_turn["id"]] = asyncio.create_task(child_run(child["id"], child_turn, payload["text"]))
    item(tid, turn, {"id": "relay-" + child_turn["id"], "type": "subAgentActivity", "kind": "interacted",
                     "agentThreadId": child["id"], "agentPath": "/root/buddy"})


async def root_run(tid, turn, text):
    try:
        if text.startswith("spawn"):
            child_id = tid + "-child"
            meta = {"id": child_id, "parentThreadId": tid, "cwd": state["threads"][tid]["cwd"],
                    "canAcceptDirectInput": False, "model": "child-model", "reasoningEffort": "low",
                    "source": {"subAgent": {"thread_spawn": {"parent_thread_id": tid, "agent_path": "/root/buddy"}}}}
            state["threads"][child_id] = meta
            send({"method": "thread/started", "params": {"thread": meta}})
            ct = turn_start(child_id)
            tasks[ct["id"]] = asyncio.create_task(child_run(child_id, ct, "ORIGINAL_CONTEXT", text == "spawn-approval"))
            item(tid, turn, {"id": "spawn", "type": "subAgentActivity", "kind": "started", "agentThreadId": child_id, "agentPath": "/root/buddy"})
        elif text.startswith("这是 Codex Room"):
            await relay(tid, turn, text)
        elif text == "hold":
            await asyncio.sleep(30)
        await asyncio.sleep(.05)
        item(tid, turn, {"id": "root-msg", "type": "agentMessage", "text": "ROOT:" + ("RELAY_DONE" if text.startswith("这是 Codex Room") else text)})
        completed(tid, turn)
    except asyncio.CancelledError:
        completed(tid, turn, "interrupted")


async def main():
    while line := await asyncio.to_thread(sys.stdin.readline):
        msg = json.loads(line)
        method, params = msg.get("method"), msg.get("params", {})
        if not method:
            future = requests.pop(msg.get("id"), None)
            if future and not future.done():
                future.set_result(msg.get("result", msg.get("error")))
            continue
        if "id" not in msg:
            continue
        result = {}
        try:
            if method == "config/read":
                result = {"config": {"model": "root-model", "model_reasoning_effort": "low"}}
            elif method == "fixture/addChild":
                tid = params["parentThreadId"] + "-nested"
                result = state["threads"][tid] = {"id": tid, "parentThreadId": params["parentThreadId"],
                    "cwd": str(Path.cwd()), "canAcceptDirectInput": False,
                    "source": {"subAgent": {"thread_spawn": {"parent_thread_id": params["parentThreadId"], "agent_path": "/root/buddy/nested"}}}}
            elif method == "thread/start":
                tid = f"root-{len(state['threads'])}"
                meta = state["threads"][tid] = {"id": tid, "cwd": params["cwd"], "canAcceptDirectInput": True}
                result = {"thread": meta}
            elif method in {"thread/read", "thread/resume"}:
                result = {"thread": state["threads"][params["threadId"]]}
                if method == "thread/resume" and result["thread"].get("parentThreadId"):
                    assert set(params) == {"threadId", "excludeTurns"}
            elif method == "thread/list":
                result = {"data": [t for t in state["threads"].values() if t.get("parentThreadId") == params.get("ancestorThreadId")]}
            elif method == "thread/turns/list":
                # Exercise the separate item paging path, never final-only data.
                turns = list(state["turns"].get(params["threadId"], {}).values())
                start = int(params.get("cursor", 0))
                result = {"data": [{**t, "items": [], "itemsView": "summary"} for t in turns[start:start+1]],
                          "nextCursor": str(start+1) if start+1 < len(turns) else None}
            elif method == "thread/items/list":
                values = state["turns"][params["threadId"]][params["turnId"]]["items"]
                start = int(params.get("cursor", 0))
                result = {"data": [{"item": i} for i in values[start:start+1]],
                          "nextCursor": str(start+1) if start+1 < len(values) else None}
            elif method == "turn/start":
                tid = params["threadId"]
                assert not state["threads"][tid].get("parentThreadId"), "V2 child direct input forbidden"
                turn = turn_start(tid)
                tasks[turn["id"]] = asyncio.create_task(root_run(tid, turn, params["input"][0]["text"]))
                result = {"turn": turn}
            elif method == "turn/steer":
                tid, turn_id = params["threadId"], params["expectedTurnId"]
                assert not state["threads"][tid].get("parentThreadId")
                turn = state["turns"][tid][turn_id]
                if tasks[turn_id].done():
                    send({"id": msg["id"], "error": {"code": -32600, "message": "no active turn"}})
                    continue
                # Notification precedes RPC ACK to reproduce the delivery race.
                await relay(tid, turn, params["input"][0]["text"])
            elif method == "turn/interrupt":
                task = tasks.get(params["turnId"])
                if task:
                    task.cancel()
                    await task
            send({"id": msg["id"], "result": result})
        except Exception as exc:
            send({"id": msg["id"], "error": {"code": -32602, "message": str(exc)}})


asyncio.run(main())
