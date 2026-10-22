"""Capture unsent, wrapped prompts in wide and compact terminals."""
import asyncio
from pathlib import Path

from textual import events

from codex_room.app import CodexRoom, ComposerInput
from preview import capture


async def main():
    output = Path("artifacts")
    output.mkdir(exist_ok=True)
    app = CodexRoom(demo=True)
    async with app.run_test(size=(132, 38)) as pilot:
        await app.create_member("mcp_server")
        prompt = ("你帮我看下这个项目是干嘛的？说是一个 mcp_server，可以提供数据库查询的 MCP 工具？"
                  "主要在意它怎么做数据库查询，以及和当前 ai-human 的 query_current_business_data、SQL 工具相比怎么样。"
                  "项目路径：/Users/zhouchenghao/python_file/wj/ai-human。都看完，再给我答案。")
        app.post_message(events.Paste(prompt))
        await pilot.pause()
        editor = app.query_one("#message-input", ComposerInput)
        assert editor.value == prompt and editor.size.height > 1
        assert not any(entry.sender == "you" for entry in app.entries)
        capture(app, "composer-wrapped.svg", output)
        await pilot.resize_terminal(86, 26)
        await pilot.pause()
        assert editor.value == prompt and editor.scroll_x == 0
        capture(app, "composer-compact.svg", output)
        app.action_toggle_sidebar()
        await pilot.pause()
        assert editor.value == prompt and editor.size.height <= 8
        capture(app, "composer-sidebar.svg", output)
        print("PASS: unsent prompts wrap and grow; resize/sidebar preserve text and cursor")


if __name__ == "__main__":
    asyncio.run(main())
