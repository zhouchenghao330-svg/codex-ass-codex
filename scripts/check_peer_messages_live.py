"""Opt-in real Codex public messaging check, exclusively in a temporary room.

Several short model calls plus a 20-second terminal wait. Includes a root with
v1 persisted tool definitions, v2 tools, discovery and explicit busy-peer steer.
Run: uv run python scripts/check_peer_messages_live.py
"""
import asyncio
from copy import deepcopy
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_room.attachments import Prompt
from codex_room.backend import Member, Service
from codex_room.peer_router import is_public
from codex_room.peer_tools import TOOL_VERSION, dynamic_tools
from codex_room.runtime import CodexClient


async def until(predicate, timeout=180):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.05)


async def main():
    with tempfile.TemporaryDirectory(prefix="room-public-live-") as tmp:
        cwd = Path(tmp)
        # Persist the real v1 shape: one send tool without delivery_mode. The
        # same native thread must later gain v2 tools through the MCP adapter.
        old = CodexClient(cwd)
        try:
            await old.start()
            legacy = deepcopy(dynamic_tools())
            legacy[0]["tools"] = legacy[0]["tools"][:1]
            legacy[0]["tools"][0]["inputSchema"]["properties"].pop("delivery_mode")
            old.thread_options = lambda _: {"dynamicTools": legacy}
            original = Member("A", "A", "#afb4ff", tmp, model=old.config.get("model", ""), effort="low")
            await old.ensure_thread(original)
            original.room_tool_version = 1
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
            assert room.agents["B"].room_tool_version == TOOL_VERSION
            print(f"PASS v1 root upgrade + v{TOOL_VERSION} dynamic tool, original context and public text", flush=True)
            await room.submit(Prompt("@B 这是一个给成员查询准备的用户问题 DISCOVERY_RECENT_USER。"
                "只回复 DISCOVERY_SHORT_REPLY，不调用任何工具。"))
            await until(lambda: not room.agents["B"].busy)
            for member, interface, target in (("A", "codex_room_public/list_members", "B"), ("B", "room.list_members", "A")):
                start = len(room.entries)
                await room.submit(Prompt(f"@{member} 只调用一次 {interface}，member={target}、recent_turns=3。"
                    "查看同事最近对话。不要发送公开消息，不使用其他工具。之后只回复已查看。"))
                await until(lambda: not room.agents[member].busy)
                tools = [b for e in room.entries[start:] for b in e.blocks if b.kind == "tool" and "list_members" in b.title]
                assert tools and all(b.status == "completed" for b in tools), [b.text for b in tools]
                assert "recent_dialogue" in "".join(b.text for b in tools)
                if member == "A":
                    assert "DISCOVERY_RECENT_USER" in "".join(b.text for b in tools)
                    assert "DISCOVERY_SHORT_REPLY" in "".join(b.text for b in tools)
            assert len(room.peers.messages) == 2
            print("PASS recent dialogue discovery via old-thread MCP and new dynamic tool, no peer wakeup", flush=True)
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
            assert [e.communication["expect_reply"] for e in counted] == [True] * 10 + [False]
            assert {a.name: a.thread_id for a in room.agents.values()} == identities
            print("PASS two real Codex members alternate 1 through 11 without acknowledgement loops", flush=True)
            await room.submit(Prompt("@B 测试忙时追加。只用终端执行 sleep 20 等待二十秒；不要读写文件、不要发公开消息。"
                "等待结束后向用户简短汇报即可。"))
            await until(lambda: room.agents["B"].phase == "正在执行命令…")
            active = room.client.active[room.agents["B"].thread_id]
            before = len(room.peers.messages)
            await room.submit(Prompt("@A 只调用一次宿主当前指定的 send_message，to=B，message=LIVE_STEER_CHECK，"
                "expect_reply=false，delivery_mode=steer。不要调用其他工具，发完结束本轮。"))
            await until(lambda: all(not a.busy for a in room.agents.values()))
            extra = list(room.peers.messages.values())[before:]
            assert len(extra) == 1, [(e.sender, e.body) for e in extra]
            assert extra[0].communication["delivery_mode"] == "steer"
            assert extra[0].communication["delivery"] == "steered", extra[0].communication
            assert extra[0].server_turn_id == active
            print("PASS explicit steer enters a real busy turn; false prevents acknowledgement messages", flush=True)
        finally:
            await room.close()


if __name__ == "__main__":
    asyncio.run(main())
