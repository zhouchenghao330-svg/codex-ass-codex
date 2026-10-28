"""Private, process-scoped compatibility adapter for pre-existing Codex threads.

No dependencies, global Codex config, persistent credentials, or public listener.
Codex launches this stdio MCP server using a thread-local config override. The
actual issuing thread/turn/call identity comes from Codex's MCP request metadata.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import secrets
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_room.peer_tools import TOOL_VERSION, mcp_tools

LIMIT = 2 * 1024 * 1024


class PeerBridge:
    def __init__(self, handler):
        self.handler, self.server = handler, None
        self.token = secrets.token_urlsafe(32)
        self.port = 0

    async def start(self):
        if self.server:
            return
        self.server = await asyncio.start_server(self.receive, "127.0.0.1", 0, limit=LIMIT)
        self.port = self.server.sockets[0].getsockname()[1]

    def config(self):
        return {"mcp_servers.codex_room_public": {
            "command": sys.executable, "args": ["-u", str(Path(__file__).resolve())],
            "env": {"CODEX_ROOM_PEER_PORT": str(self.port), "CODEX_ROOM_PEER_TOKEN": self.token},
            "startup_timeout_sec": 10, "tool_timeout_sec": 10,
            "required": True,
            "default_tools_approval_mode": "auto",
        }}

    async def receive(self, reader, writer):
        try:
            async with asyncio.timeout(5):
                packet = json.loads(await reader.readline())
                if not isinstance(packet, dict):
                    return
                token = packet.get("token")
                if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self.token.encode()):
                    result = {"accepted": False, "error": "Room connection expired or unauthorized."}
                else:
                    result = self.handler(packet.get("params", {}))
                writer.write((json.dumps(result, ensure_ascii=False) + "\n").encode())
                await writer.drain()
        except (ValueError, OSError, TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None


def call_room(params):
    import socket
    packet = {"token": os.environ["CODEX_ROOM_PEER_TOKEN"], "params": params}
    with socket.create_connection(("127.0.0.1", int(os.environ["CODEX_ROOM_PEER_PORT"])), timeout=8) as connection:
        connection.sendall((json.dumps(packet, ensure_ascii=False) + "\n").encode())
        with connection.makefile("rb") as stream:
            data = stream.readline(LIMIT + 1)
            if len(data) > LIMIT:
                raise ValueError("Room response too large")
            return json.loads(data)


def main():
    for line in sys.stdin.buffer:
        try:
            event = json.loads(line)
            method, params = event.get("method"), event.get("params", {})
            if "id" not in event:
                continue
            if method == "initialize":
                result = {"protocolVersion": params.get("protocolVersion", "2024-11-05"),
                          "capabilities": {"tools": {}}, "serverInfo": {"name": "codex-room-public", "version": f"{TOOL_VERSION}.0"}}
            elif method == "tools/list":
                result = {"tools": mcp_tools()}
            elif method == "ping":
                result = {}
            elif method == "tools/call" and params.get("name") in {"send_message", "list_members"}:
                try:
                    receipt = call_room(params)
                except (OSError, ValueError, KeyError):
                    receipt = {"accepted": False, "error": "Room connection unavailable. Do not retry automatically."}
                result = {"content": [{"type": "text", "text": json.dumps(receipt, ensure_ascii=False)}],
                          "isError": not receipt.get("accepted", False)}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": event["id"], "error": {"code": -32601, "message": "Unsupported method"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": event["id"], "result": result}, ensure_ascii=False), flush=True)
        except (ValueError, KeyError, TypeError):
            continue


if __name__ == "__main__":
    main()
