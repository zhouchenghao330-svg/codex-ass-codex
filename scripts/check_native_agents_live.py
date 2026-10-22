"""Opt-in real Codex child test. Temporary room; several short model calls.

Run: uv run python scripts/check_native_agents_live.py
Does not edit project files or copy credentials. Requires Codex login.
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_room.attachments import Prompt
from codex_room.backend import Service


async def until(predicate, timeout=180):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.05)


async def main():
    def emit(event):
        if event.get("type") == "notice":
            print(event["text"], flush=True)
    with tempfile.TemporaryDirectory(prefix="room-native-live-") as tmp:
        room = Service(Path(tmp), emit=emit)
        try:
            await room.start()
            assert room.connected
            await room.command("/add 主代理")
            await room.command("/model " + room.agents["主代理"].model + " low")
            await room.submit(Prompt("创建一个名为 buddy 的原生子代理，告诉它记住暗号 ROOM_NATIVE_CONTEXT，"
                "用 send_message 给 /root 发送一条 ROOM_PEER_NOTICE 消息，然后回复 CHILD_READY。"
                "你等待它完成后只回复 PARENT_READY。请保留它，不关闭。不执行命令、不改文件、不上网。"))
            await until(lambda: any(a.parent_name for a in room.agents.values()))
            child = next(a for a in room.agents.values() if a.parent_name)
            await until(lambda: not room.agents["主代理"].busy and not child.busy and
                any(e.sender == child.name and "CHILD_READY" in e.body for e in room.entries))
            child_id, child_name = child.thread_id, child.name
            print("PASS real child discovered with original output", flush=True)
            await room.native.queue.join()
            pairs = {(e.sender, e.target) for e in room.entries if e.communication}
            assert ("主代理", child_name) in pairs and (child_name, "主代理") in pairs, pairs
            print("PASS real parent/child communication visible in both directions", flush=True)
            await room.submit(Prompt(f"@{child.label} 刚才让你记住的暗号是什么？只回复暗号，不调用工具。"))
            request = next(e for e in reversed(room.entries) if e.sender == "you" and e.target == child_name)
            await until(lambda: request.state not in {"queued"} and not child.busy)
            assert request.state == "done", request.state
            assert any(e.sender == child_name and "ROOM_NATIVE_CONTEXT" in e.body for e in room.entries)
            assert child.thread_id == child_id
            print("PASS direct mention relayed to same native child", flush=True)
            await room.close()
            room = Service(Path(tmp), emit=emit)
            await room.start()
            child = room.agents[child_name]
            assert child.thread_id == child_id and child_id in room.native.attached
            keys = [(e.native_thread_id, e.server_turn_id) for e in room.entries if e.native_thread_id]
            assert len(keys) == len(set(keys))
            print("PASS restart restores original child, no duplicated turns", flush=True)
            await room.submit(Prompt(f"@{child.label} 请执行一次 sleep 20，完成后回复 SLEEP_FINISHED。不要改文件或做其他事情。"))
            await until(lambda: child.busy)
            await room.stop(child_name)
            assert not child.busy
            assert next(e for e in reversed(room.entries) if e.sender == child_name).state == "stopped"
            print("PASS interrupt native child directly", flush=True)
        finally:
            await room.close()


if __name__ == "__main__":
    asyncio.run(main())
