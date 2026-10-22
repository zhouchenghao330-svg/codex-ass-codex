"""Opt-in real-Codex steering check: one turn, a short sleep, and no project edits."""
import asyncio
import tempfile
import uuid
from pathlib import Path

from codex_room.app import Agent
from codex_room.runtime import CodexClient


async def main():
    marker = "STEER-" + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="codex-room-steer-") as tmp:
        client = CodexClient(Path(tmp))
        task = None
        try:
            await client.start()
            agent = Agent("probe", "probe", "mint", cwd=tmp, model=client.config.get("model") or "", effort="low")
            await client.ensure_thread(agent)
            running = asyncio.Event()
            output = []

            async def receive(event):
                params = event.get("params", {})
                if event["method"] == "item/started" and params.get("item", {}).get("type") == "commandExecution":
                    running.set()
                if event["method"] == "item/agentMessage/delta":
                    output.append(params.get("delta", ""))

            task = asyncio.create_task(client.run_turn(agent,
                "这是消息投递测试。只执行一次 shell 命令 sleep 3，不读取或修改文件，不调用其他工具，然后简短回复完成。", receive))
            async with asyncio.timeout(90):
                while not running.is_set():
                    if task.done():
                        raise AssertionError("No shell execution event: " + "".join(output))
                    await asyncio.sleep(0.05)
                turn = client.active[agent.thread_id]
                assert await client.steer_turn(agent.thread_id, f"追加指令：工具结束后，只回复 {marker}。")
                assert client.active.get(agent.thread_id) == turn
                result = await task
            assert result["id"] == turn and result["status"] == "completed", result
            assert marker in "".join(output), "".join(output)
            print("PASS: real turn/steer accepted during shell execution; same turn consumed the new instruction.")
        finally:
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await client.close()


if __name__ == "__main__":
    asyncio.run(main())
