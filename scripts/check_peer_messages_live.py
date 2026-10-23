"""Opt-in real Codex public messaging check, exclusively in a temporary room.

Several short model calls. Includes an old root without dynamic tools, a fresh
root, actual plaintext tool calls, and restart with original thread identities.
Run: uv run python scripts/check_peer_messages_live.py
"""
import asyncio
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_room.attachments import Prompt
from codex_room.backend import Member, Service
from codex_room.peer_router import is_public
from codex_room.runtime import CodexClient


async def until(predicate, timeout=180):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.05)


async def main():
    with tempfile.TemporaryDirectory(prefix="room-public-live-") as tmp:
        cwd = Path(tmp)
        # Make a genuine pre-feature root, not a fake marker on a new thread.
        old = CodexClient(cwd)
        try:
            await old.start()
            original = Member("A", "A", "#afb4ff", tmp, model=old.config.get("model", ""), effort="low")
            await old.ensure_thread(original)
            async def discard(event):
                if "id" in event:
                    raise AssertionError("Unexpected interaction in a text-only test")
            await old.run_turn(original, "记住暗号 ROOM_PUBLIC_CONTEXT。只回复记住了，不调用任何工具。", discard)
        finally:
            await old.close()

        def emit(event):
            if event.get("type") == "notice":
                print(event["text"], flush=True)
        room = Service(cwd, emit=emit)
        try:
            await room.start()
            assert room.connected
            room.agents["A"] = original
            await room.command("/add B")
            room.agents["B"].effort = "low"
            await room.submit(Prompt("@A 做一次公开通信测试。只用 Room 的 send_message 工具，不调用其他工具、不执行命令、不读写文件。"
                "给 B 发送你之前记住的暗号，并要求 B 用同一工具回复 PUBLIC_REPLY_CONFIRMED。"
                "你使用 codex_room_public/send_message；B 新会话使用 room.send_message。"
                "工具返回后不要等待或轮询，回复一句已发送即可，B 的回答会稍后自动送来。"))
            await until(lambda: len([e for e in room.entries if is_public(e)]) >= 2)
            await until(lambda: all(not a.busy for a in room.agents.values()))
            messages = [e for e in room.entries if is_public(e)]
            assert len(messages) == 2, [(e.sender, e.target, e.body) for e in messages]
            assert [(e.sender, e.target) for e in messages] == [("A", "B"), ("B", "A")]
            assert "ROOM_PUBLIC_CONTEXT" in messages[0].body
            assert "PUBLIC_REPLY_CONFIRMED" in messages[1].body
            assert messages[1].communication["reply_to"] == messages[0].id
            assert all(e.communication["delivery"] in {"delivered", "steered"} for e in messages)
            assert room.agents["A"].thread_id == original.thread_id
            assert room.agents["B"].room_tool_version == 1
            print("PASS old root context + new root dynamic tool, plaintext in both directions", flush=True)
            identities = {a.name: a.thread_id for a in room.agents.values()}
            ids = {e.id for e in messages}
            await room.close()
            room = Service(cwd, emit=emit)
            await room.start()
            assert {a.name: a.thread_id for a in room.agents.values()} == identities
            assert {e.id for e in room.entries if is_public(e)} == ids
            await room.submit(Prompt("@B 只用 room.send_message 工具给 A 发一条公开消息 RESTART_PUBLIC_OK，"
                "expect_reply=false。只调用一次该工具，不调用其他工具，不执行命令。完成后简短回复已发送。"))
            await until(lambda: len([e for e in room.entries if is_public(e)]) == 3)
            await until(lambda: all(not a.busy for a in room.agents.values()))
            assert "RESTART_PUBLIC_OK" in [e for e in room.entries if is_public(e)][-1].body
            assert {a.name: a.thread_id for a in room.agents.values()} == identities
            print("PASS original threads + restored dynamic tools after restart, no replay", flush=True)
            before_counting = len([e for e in room.entries if is_public(e)])
            await room.submit(Prompt("@A 给 B 发消息，轮流数数，直到数到 11。你先数 1，B 数偶数，你数奇数。"
                "只使用 Room 的公开 send_message 工具，不调用其他工具、不执行命令、不读写文件。"
                "每条公开消息第一行只写本次数字，首条后面说明轮流数到 11 的要求。"
                "每次只发送当前这一个数，不代替对方数。到 11 后结束，不再相互确认。"))
            await until(lambda: all(not a.busy for a in room.agents.values()), timeout=300)
            counted = [e for e in room.entries if is_public(e)][before_counting:]
            assert len(counted) == 11, [(e.sender, e.body) for e in counted]
            assert [int(re.search(r"\d+", e.body.splitlines()[0])[0]) for e in counted] == list(range(1, 12)), [e.body for e in counted]
            assert [e.sender for e in counted] == ["A" if n % 2 else "B" for n in range(1, 12)]
            assert all(e.communication["delivery"] in {"delivered", "steered"} for e in counted)
            assert len({e.communication["chain_id"] for e in counted}) == 1
            assert {a.name: a.thread_id for a in room.agents.values()} == identities
            print("PASS two real Codex members alternate 1 through 11 without acknowledgement loops", flush=True)
        finally:
            await room.close()


if __name__ == "__main__":
    asyncio.run(main())
