# /// script
# requires-python = ">=3.11"
# dependencies = ["pyte==0.8.2", "textual==8.2.8"]
# ///
"""Opt-in real Codex + Rust launcher check. Uses two short model calls."""
import json
import os
import tempfile
from pathlib import Path

from check_terminal_lab import ROOT, Session


def send(session,text):
    session.send(text.encode())
    session.wait(lambda:text in "\n".join(session.visible.splitlines()[-5:]))
    session.send(b"\r")
    for _ in range(5):
        session.read(.02)
    if text in "\n".join(session.visible.splitlines()[-3:]):
        session.send(b"\r")
    session.wait(lambda:"正在确认发送" not in session.visible)
    if text.startswith("/add "):
        session.wait(lambda:"选择模型 ·" in session.visible)
        session.send(b"\r")
        session.wait(lambda:"选择思考强度 ·" in session.visible)
        session.send(b"\r")
        session.wait(lambda:"已设置 ·" in session.visible and "选择思考强度 ·" not in session.visible)


def main():
    os.environ.pop("CODEX_ROOM_CODEX_COMMAND",None)
    proof={}
    with tempfile.TemporaryDirectory(prefix="rust-room-live-") as tmp:
        cwd=Path(tmp)
        command=[str(ROOT/".venv/bin/python"),"-m","codex_room","--cwd",tmp]
        for round in range(2):
            session=Session(command,columns=125,rows=34)
            try:
                session.wait(lambda:"已连接" in session.visible,seconds=60)
                if round==0:
                    send(session,"/add 验证")
                    session.wait(lambda:"@验证" in session.visible)
                    send(session,"请记住暗号 RUST_ROOM_LIVE_OK。本轮只回复这个暗号，不调用工具。")
                else:
                    send(session,"刚才让你记住的暗号是什么？只回复暗号，不调用工具。")
                session.wait(lambda:"Worked for" in session.visible and "RUST_ROOM_LIVE_OK" in session.visible,seconds=120)
                # The first completed footer can already exist after restart.
                if round:
                    session.wait(lambda:session.visible.count("Worked for")>=2,seconds=120)
                assert session.finish()==0
                state=json.loads((cwd/".codex-room/state.json").read_text())
                answer=next(e for e in reversed(state["entries"]) if e["sender"]=="验证")
                assert answer["state"]=="done",answer
                assert "RUST_ROOM_LIVE_OK" in answer["body"],answer["body"]
                tid=state["agents"][0]["thread_id"]
                if round:
                    assert tid==proof["thread_id"]
                proof.update(thread_id=tid,**{("resume" if round else "real_reply"):True})
                print("PASS real Rust UI "+("context resume" if round else "streaming reply"),flush=True)
            finally:
                session.close()
    (ROOT/"artifacts/rust-ui-live.json").write_text(json.dumps(proof,indent=2))


if __name__=="__main__":
    main()
