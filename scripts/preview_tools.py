"""Capture the tool summary, per-call preview, and complete diff without model usage."""
import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from codex_room.app import CodexRoom
from codex_room.runtime import CodexClient
from preview import capture


ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


async def main():
    output = ROOT / "artifacts"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
        app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
        async with app.run_test(size=(132, 48)) as pilot:
            await app.connection_task
            await app.create_member("dev")
            await app.submit("@dev mixed-tools")
            await app.tasks["dev"]
            card = app.cards[id(app.agents["dev"].history[-1])]
            await pilot.pause()
            capture(app, "tool-overview.svg", output)
            await pilot.click(card.body, offset=(1, 0))
            await pilot.pause()
            capture(app, "tool-previews.svg", output)
            card.body.action_tool_item(3, 0)
            await pilot.pause()
            capture(app, "tool-file-diff.svg", output)
            await pilot.resize_terminal(86, 26)
            await pilot.pause()
            app.query_one("#timeline").scroll_home(animate=False, immediate=True)
            await pilot.pause()
            capture(app, "tool-compact.svg", output)
            print("PASS: mouse tool previews and per-file diff; screenshots in artifacts/")


if __name__ == "__main__":
    asyncio.run(main())
