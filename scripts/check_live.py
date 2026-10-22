"""Opt-in real-Codex check. Uses model tokens and temporary, isolated room state."""
from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from pathlib import Path

from codex_room.app import CodexRoom


async def settle(app: CodexRoom, *names: str) -> None:
    await asyncio.wait_for(asyncio.gather(*(app.tasks[name] for name in names)), 150)
    for name in names:
        answer = app.agents[name].history[-1]
        assert answer.state == "done", (name, answer.body)


async def main() -> None:
    a, b = "ROOM-A-" + uuid.uuid4().hex[:8], "ROOM-B-" + uuid.uuid4().hex[:8]
    proof = {}
    with tempfile.TemporaryDirectory(prefix="codex-room-live-") as tmp:
        state = Path(tmp)
        app = CodexRoom(state_dir=state)
        async with app.run_test(size=(132, 38)) as pilot:
            await asyncio.wait_for(app.connection_task, 45)
            assert app.connected, app.connection_error
            await app.create_member("exp")
            await app.create_member("ops")
            for name in ("exp", "ops"):
                app.agents[name].effort = "low"
            await app.submit(f"@exp 这是独立会话测试。记住暗号 {a}。不要调用工具，只回复：记住了。")
            await app.submit(f"@ops 这是独立会话测试。记住暗号 {b}。不要调用工具，只回复：记住了。")
            await settle(app, "exp", "ops")
            assert app.agents["exp"].thread_id != app.agents["ops"].thread_id
            for name in ("exp", "ops"):
                await app.submit(f"@{name} 你这个会话里刚才的暗号是什么？不要调用工具，只回复暗号。")
            await settle(app, "exp", "ops")
            reply_a, reply_b = (app.agents[name].history[-1].body for name in ("exp", "ops"))
            assert a in reply_a and b not in reply_a, reply_a
            assert b in reply_b and a not in reply_b, reply_b
            proof["parallel_and_isolation"] = True
            print("PASS real parallel replies and independent context", flush=True)

            await app.submit("@ops 这是只读工具测试。请通过 shell 执行 pwd，并回复工具输出。不要修改文件。")
            await settle(app, "ops")
            answer = app.agents["ops"].history[-1]
            tool_reply = answer.body
            assert str(Path.cwd()) in tool_reply, tool_reply
            assert any(block.kind == "command" and str(Path.cwd()) in block.text for block in answer.blocks), ("Missing actual tool execution event", answer.blocks)
            proof["shell_execution"] = True
            print("PASS real shell command, output retained in expandable details", flush=True)
            original = app.agents["exp"].thread_id

        restored = CodexRoom(state_dir=state)
        async with restored.run_test(size=(100, 30)) as pilot:
            await asyncio.wait_for(restored.connection_task, 45)
            assert restored.agents["exp"].thread_id == original
            await restored.submit("@exp 你这个会话里刚才的暗号是什么？不要调用工具，只回复暗号。")
            await settle(restored, "exp")
            assert a in restored.agents["exp"].history[-1].body
            proof["restart_resume"] = True
            print("PASS process restart and context resume", flush=True)

            await restored.submit("@ops 请执行 shell 命令 sleep 30，执行完成后只回复：完成。")
            async with asyncio.timeout(90):
                while restored.agents["ops"].phase != "正在执行命令…":
                    if restored.tasks["ops"].done():
                        raise AssertionError(restored.agents["ops"].history[-1].body)
                    await pilot.pause(0.05)
            await restored.submit("@ops 补充说明：不要修改文件，原定 sleep 任务保持不变。")
            await asyncio.wait_for(restored.stop_agent("ops"), 30)
            assert not restored.agents["ops"].busy
            assert not restored.agents["ops"].queue
            assert not restored.client.active
            proof["interrupt_running_command_after_steering"] = True
            print("PASS interrupt running shell command after steering", flush=True)
        proof["full_access_default"] = all(member.permission == "full" for member in restored.agents.values())
    output = Path("artifacts")
    output.mkdir(exist_ok=True)
    (output / "live-check.json").write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(proof, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
