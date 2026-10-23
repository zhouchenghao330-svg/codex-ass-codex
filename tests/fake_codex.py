"""Small protocol peer for deterministic transport, cancellation and UI checks."""
import asyncio
import json
import sys
import tomllib
from pathlib import Path

threads = {}
turns = {}
thread_turns = {}
steering = {}
steer_ready = {}
requests = {}
sequence = 0


def input_text(params):
    return "\n".join(item["text"] for item in params["input"]
                     if item["type"] == "text" and not item["text"].startswith("[Codex Room host context;"))


async def public_call(thread, turn, arguments):
    global sequence
    sequence += 1
    rid = f"public-{sequence}"
    future = asyncio.get_running_loop().create_future()
    requests[rid] = future
    item = {"id": rid, "type": "dynamicToolCall", "namespace": "room", "tool": "send_message",
            "arguments": arguments, "status": "inProgress"}
    send({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": item}})
    send({"id": rid, "method": "item/tool/call", "params": {"threadId": thread, "turnId": turn,
          "callId": rid, "namespace": "room", "tool": "send_message", "arguments": arguments}})
    result = await future
    send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn,
          "item": {**item, **result, "status": "completed" if result.get("success") else "failed"}}})
    return json.loads(result["contentItems"][0]["text"])


async def peer_input(thread, turn, text):
    lines = text.splitlines()
    info = json.loads(lines[1])
    if info["expect_reply"]:
        bounce = "BOUNCE_TEST" in text
        receipt = await public_call(thread, turn, {"to": info["from"],
            "message": "BOUNCE_TEST" if bounce else "**PUBLIC_ANSWER**\n\n完整的公开回复。",
            "reply_to": info["message_id"], "expect_reply": bounce})
        return "PEER_REPLIED:" + str(receipt.get("accepted"))
    if threads[thread].get("force_peer_ack"):
        receipt = await public_call(thread, turn, {"to": info["from"], "message": "谢谢",
            "reply_to": info["message_id"], "expect_reply": False})
        return "PEER_ACK_BLOCKED:" + str(not receipt.get("accepted"))
    return "FINAL_PEER_RESPONSE_RECEIVED"


def config_for(cwd):
    config = {"model": "test-model", "model_reasoning_effort": "high"}
    path = Path(cwd) / ".codex/config.toml"
    if path.is_file():
        local = tomllib.loads(path.read_text())
        config.update({key: local[key] for key in config if key in local})
    return config


def send(message):
    print(json.dumps(message), flush=True)


async def finish(thread, turn, text, params):
    global sequence
    try:
        if text.startswith(("peer-send ", "peer-send-hold ", "peer-force-ack ", "peer-bounce ")):
            target = text.split(maxsplit=1)[1]
            threads[thread]["force_peer_ack"] = text.startswith("peer-force-ack ")
            hold, bounce = text.startswith("peer-send-hold "), text.startswith("peer-bounce ")
            receipt = await public_call(thread, turn, {"to": target,
                "message": "BOUNCE_TEST" if bounce else "**PUBLIC_REQUEST**\n\n请给出你的建议。", "expect_reply": True})
            text = "PUBLIC_RECEIPT:" + str(receipt.get("accepted"))
            if hold:
                await steer_ready[turn].wait()
            else:
                # Exercise a reply arriving while the sender is still processing.
                await asyncio.sleep(.15)
        elif text.startswith("[Codex Room public peer message;"):
            text = await peer_input(thread, turn, text)
        if text == "continuous-stream":
            body = ""
            for index in range(120):
                delta = f"stream line {index:03d}\n\n"
                body += delta
                send({"method":"item/agentMessage/delta","params":{"threadId":thread,"turnId":turn,"itemId":"continuous","delta":delta}})
                await asyncio.sleep(.05)
            send({"method":"item/completed","params":{"threadId":thread,"turnId":turn,"item":{"id":"continuous","type":"agentMessage","text":body}}})
            send({"method":"turn/completed","params":{"threadId":thread,"turn":{"id":turn,"status":"completed"}}})
            return
        markdown = text == "markdown"
        stream_prefix = ""
        if text in {"steer-stream", "steer-complete"}:
            stream_prefix = "BEFORE_FOLLOWUP\n"
            send({"method": "item/agentMessage/delta", "params": {"threadId": thread, "turnId": turn, "itemId": "message-1", "delta": stream_prefix}})
            send({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": {
                "id": "early-command", "type": "commandExecution", "command": "pwd", "status": "inProgress"}}})
            await steer_ready[turn].wait()
            send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": {
                "id": "early-command", "type": "commandExecution", "command": "pwd", "status": "completed",
                "aggregatedOutput": "OUTPUT_AFTER_FOLLOWUP", "exitCode": 0}}})
        elif text == "steer-hold":
            await steer_ready[turn].wait()
        elif text == "slow":
            await asyncio.sleep(30)
        else:
            await asyncio.sleep(0.03)
        if text in {"thinking", "thinking-slow", "thinking-fail"}:
            reasoning = {"id": "reasoning-1", "type": "reasoning", "summary": [], "content": ["RAW_REASONING_SENTINEL"]}
            send({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": reasoning}})
            summary_params = {"threadId": thread, "turnId": turn, "itemId": "reasoning-1", "summaryIndex": 0}
            send({"method": "item/reasoning/summaryPartAdded", "params": summary_params})
            for delta in ("**检查项目", "结构**\n\nPUBLIC_SUMMARY_BODY"):
                send({"method": "item/reasoning/summaryTextDelta", "params": {**summary_params, "delta": delta}})
            send({"method": "item/reasoning/textDelta", "params": {**summary_params, "contentIndex": 0, "delta": "RAW_REASONING_SENTINEL"}})
            await asyncio.sleep(30 if text == "thinking-slow" else 0.7)
            reasoning["summary"] = ["**检查项目结构**\n\nPUBLIC_SUMMARY_BODY"]
            send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": reasoning}})
            if text == "thinking-fail":
                send({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "failed", "error": {"message": "thinking test failure"}}}})
                return
            item = {"id": "command-think", "type": "commandExecution", "command": "pwd", "status": "inProgress"}
            send({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": item}})
            await asyncio.sleep(0.7)
            item.update(status="completed", exitCode=0, aggregatedOutput="/tmp/project")
            send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": item}})
            send({"method": "item/agentMessage/delta", "params": {"threadId": thread, "turnId": turn, "itemId": "message-1", "delta": "检查完成，正在回复。"}})
            await asyncio.sleep(0.7)
        if text in {"tools", "tool-failure", "tool-slow"}:
            send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": {
                "id": "commentary-1", "type": "agentMessage", "text": "Checking the project."
            }}})
            for index in range(3 if text == "tools" else 1):
                item = {"id": f"command-{index}", "type": "commandExecution", "command": f"/bin/zsh -lc 'cat fixture-{index}.txt'", "status": "inProgress"}
                send({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": item}})
                output = "TOOL OUTPUT\n" + "Skill instructions stay in the details.\n" * 20
                for delta in (output[:10], output[10:]):
                    send({"method": "item/commandExecution/outputDelta", "params": {"threadId": thread, "turnId": turn, "itemId": item["id"], "delta": delta}})
                if text == "tool-slow":
                    await asyncio.sleep(30)
                await asyncio.sleep(0.05)
                item.update(status="completed", exitCode=9 if text == "tool-failure" else 0, aggregatedOutput=output)
                send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": item}})
            text = "Project checked."
        if text in {"interactive", "approval"}:
            sequence += 1
            rid = f"server-{sequence}"
            future = asyncio.get_running_loop().create_future()
            requests[rid] = future
            method = "item/tool/requestUserInput" if text == "interactive" else "item/commandExecution/requestApproval"
            details = {"threadId": thread, "turnId": turn, "itemId": "item-1"}
            if text == "interactive":
                details.update(isBlocking=True, questions=[{"id": "color", "question": "Which color?", "options": []}])
            else:
                details["command"] = "printf test"
            send({"id": rid, "method": method, "params": details})
            answer = await future
            text = json.dumps(answer)
        if text == "mixed-tools":
            items = [
                {"id": "native-command", "type": "commandExecution", "command": "printf fixture", "exitCode": 0,
                 "aggregatedOutput": "command preview\nCOMMAND_OUTPUT **literal**"},
                {"id": "mcp-search", "type": "mcpToolCall", "server": "fixture", "tool": "search",
                 "arguments": {"query": "lookup something"}, "result": {"content": [{"type": "text", "text": "MCP preview\nMCP_RESULT"}]}},
                {"id": "dynamic-tool", "type": "dynamicToolCall", "namespace": "functions", "tool": "exec_command",
                 "arguments": {"cmd": "pwd"}, "contentItems": [{"type": "text", "text": "DYNAMIC_OUTPUT"}], "success": True},
                {"id": "file-edits", "type": "fileChange", "changes": [
                    {"path": "src/app.py", "kind": {"type": "update", "move_path": None},
                     "diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1,2 +1,3 @@\n-old_value\n+new_value\n+added_value\n keep"},
                    {"path": "src/new.py", "kind": {"type": "add"}, "diff": "new file\nsecond line\n"},
                    {"path": "src/deleted.py", "kind": {"type": "delete"}, "diff": "old file\n"},
                    {"path": "src/old_name.py", "kind": {"type": "update", "move_path": "src/renamed.py"}, "diff": ""},
                 ]},
            ]
            for item in items:
                send({"method": "item/started", "params": {"threadId": thread, "turnId": turn, "item": {**item, "status": "inProgress"}}})
                send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": {**item, "status": "completed"}}})
            text = "Tools finished."
        if text.startswith("remember "):
            threads[thread]["memory"] = text[9:]
        elif text == "recall":
            text = threads[thread].get("memory", "NO_MEMORY")
        elif text == "settings":
            text = json.dumps({key: params.get(key) for key in ("model", "effort", "approvalPolicy", "sandboxPolicy", "cwd")})
        elif text == "cwd-probe":
            cwd = Path(params["cwd"])
            text = json.dumps({"cwd": str(cwd), "threadCwd": threads[thread]["cwd"],
                               "marker": (cwd / "marker.txt").read_text(), "model": params.get("model"),
                               "sandbox": params["sandboxPolicy"]})
        if steering.get(turn):
            displayed = []
            for followup in steering[turn]:
                if followup.startswith("[Codex Room public peer message;"):
                    displayed.append(await peer_input(thread, turn, followup))
                else:
                    displayed.append(followup)
            text += "\nSTEERED: " + json.dumps(displayed, ensure_ascii=False)
        if text == "fail":
            send({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "failed", "error": {"message": "test failure"}}}})
            return
        body = ("# 当前环境\n\n- **身份**：Codex\n- **目录**：`/tmp/project`\n\n"
                "> 引用内容\n\n[文档](https://example.com/docs)\n\n"
                "```python\nprint(\"**literal**\")\n```\n\n"
                "| 模型 | 状态 |\n| --- | --- |\n| test | ready |") if markdown else f"{thread}: {text}"
        if not text.startswith("steer-complete"):
            for chunk in (body[:4], body[4:]):
                send({"method": "item/agentMessage/delta", "params": {"threadId": thread, "turnId": turn, "itemId": "message-1", "delta": chunk}})
        send({"method": "item/completed", "params": {"threadId": thread, "turnId": turn, "item": {"type": "agentMessage", "id": "message-1", "text": stream_prefix + body}}})
        send({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}}})
    except asyncio.CancelledError:
        send({"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "interrupted"}}})


async def main():
    global sequence
    while line := await asyncio.to_thread(sys.stdin.readline):
        message = json.loads(line)
        method, params = message.get("method"), message.get("params", {})
        if not method:
            future = requests.pop(message.get("id"), None)
            if future and not future.done():
                future.set_result(message.get("result", {}))
            continue
        if "id" not in message:
            continue
        result = {}
        if method == "config/read":
            result = {"config": config_for(params["cwd"])}
        elif method == "model/list":
            result = {"data": [{"id": "test-model", "displayName": "Test Model", "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}, {"reasoningEffort": "high"}]}]}
        elif method in {"thread/start", "thread/resume"}:
            thread = params.get("threadId") or f"thread-{len(threads) + 1}"
            threads.setdefault(thread, {})
            threads[thread]["cwd"] = params["cwd"]
            config = config_for(params["cwd"])
            result = {"thread": {"id": thread}, "cwd": params["cwd"], "model": params.get("model", config["model"]),
                      "reasoningEffort": params.get("config", {}).get("model_reasoning_effort", config["model_reasoning_effort"]),
                      "approvalPolicy": params["approvalPolicy"], "sandbox": {"type": "dangerFullAccess"}}
            result["hasDeveloperInstructions"] = "developerInstructions" in params
            result["developerInstructionsValue"] = params.get("developerInstructions")
        elif method == "turn/start":
            sequence += 1
            turn = f"turn-{sequence}"
            thread = params["threadId"]
            thread_turns[thread] = turn
            steering[turn] = []
            steer_ready[turn] = asyncio.Event()
            turns[turn] = asyncio.create_task(finish(thread, turn, input_text(params), params))
            result = {"turn": {"id": turn, "status": "inProgress"}}
        elif method == "turn/steer":
            thread = params["threadId"]
            turn = thread_turns.get(thread)
            text = input_text(params)
            if text == "reject-steer":
                send({"id": message["id"], "error": {"code": -32602, "message": "steering rejected by fixture"}})
                continue
            if text == "race-to-next" and turn and not turns[turn].done():
                turns[turn].cancel()
                await turns[turn]
            if not turn or turns[turn].done():
                send({"id": message["id"], "error": {"code": -32600, "message": "no active turn to steer"}})
                continue
            if params["expectedTurnId"] != turn:
                send({"id": message["id"], "error": {"code": -32600, "message": "turn id mismatch"}})
                continue
            steering[turn].append(text)
            if text in {"finish-steer", "finish-and-ack"} or text.startswith("[Codex Room public peer message;"):
                steer_ready[turn].set()
            if text == "finish-and-ack":
                await turns[turn]
                await asyncio.sleep(0.05)
            result = {"turnId": turn}
        elif method == "turn/interrupt":
            task = turns.get(params["turnId"])
            if task:
                task.cancel()
        send({"id": message["id"], "result": result})


asyncio.run(main())
