"""Version-2 Codex app-server transport, preserving Codex-owned session history."""
from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from pathlib import Path
from typing import Awaitable, Callable

from .platform_support import codex_command
from .peer_tools import TOOL_VERSION


class CodexError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


def clean_error(value: object) -> str:
    text = str(value)
    text = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:1500]


def permission_settings(mode: str, cwd: str) -> tuple[str, dict]:
    if mode == "full":
        return "never", {"type": "dangerFullAccess"}
    if mode == "workspace":
        return "on-request", {"type": "workspaceWrite", "writableRoots": [cwd], "networkAccess": False}
    if mode == "read-only":
        return "on-request", {"type": "readOnly", "networkAccess": False}
    raise CodexError(f"未知权限模式：{mode}")


class CodexClient:
    def __init__(self, cwd: Path, *, command: list[str] | None = None) -> None:
        self.cwd = str(cwd.resolve())
        self.command = command or codex_command()
        self.process: asyncio.subprocess.Process | None = None
        self.reader: asyncio.Task | None = None
        self.stderr_reader: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.queues: dict[str, asyncio.Queue] = {}
        self.loaded: set[str] = set()
        self.load_locks = {}
        self.active: dict[str, str] = {}
        self.sequence = 0
        self.config: dict = {}
        self.models: list[dict] = []
        self.diagnostics: deque[str] = deque(maxlen=8)
        self.closing = False
        # A synchronous observer claims child requests before the reader can
        # reject them. It must never await RPC from inside the reader task.
        self.observer = None
        self.thread_options = None
        self.input_context = None

    async def start(self) -> None:
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command, cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024,
            )
            self.reader = asyncio.create_task(self._read())
            self.stderr_reader = asyncio.create_task(self._read_stderr())
            await self.request("initialize", {"clientInfo": {
                "name": "codex_room", "title": "Codex Room", "version": "0.1.0",
            }, "capabilities": {"experimentalApi": True}})
            await self.send({"method": "initialized"})
            config, catalog = await asyncio.gather(
                self.request("config/read", {"includeLayers": False, "cwd": self.cwd}),
                self.request("model/list", {"limit": 100}),
            )
            self.config = config.get("config", {})
            self.models = catalog.get("data", [])
            while catalog.get("nextCursor"):
                catalog = await self.request("model/list", {"limit": 100, "cursor": catalog["nextCursor"]})
                self.models.extend(catalog.get("data", []))
        except BaseException:
            await self.close()
            raise

    async def send(self, message: dict) -> None:
        if not self.process or self.process.returncode is not None:
            raise CodexError("Codex 连接已关闭。使用 /reconnect 重新连接。")
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method: str, params: dict, *, timeout: float = 45) -> dict:
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError as exc:
            raise CodexError(f"Codex 请求超时：{method}") from exc
        finally:
            self.pending.pop(request_id, None)

    async def respond(self, request_id: int | str, result: dict) -> None:
        await self.send({"id": request_id, "result": result})

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            self.diagnostics.append(clean_error(line.decode(errors="replace").strip()))

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        failure = "Codex app-server 已退出。"
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                method = message.get("method")
                if method:
                    params = message.get("params", {})
                    claimed = self.observer(message) if self.observer else False
                    queue = self.queues.get(params.get("threadId"))
                    if queue is not None:
                        queue.put_nowait(message)
                    elif "id" in message and not claimed:
                        await self.send({"id": message["id"], "error": {
                            "code": -32601, "message": "No active room turn handles this request",
                        }})
                else:
                    future = self.pending.get(message.get("id"))
                    if future is not None and not future.done():
                        if "error" in message:
                            error = message["error"]
                            future.set_exception(CodexError(clean_error(error.get("message", error)), code=error.get("code")))
                        else:
                            future.set_result(message.get("result", {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = clean_error(exc)
        finally:
            if self.diagnostics:
                failure += " " + self.diagnostics[-1]
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(CodexError(failure))
            for queue in self.queues.values():
                queue.put_nowait({"method": "transport/error", "params": {"message": failure}})
            if self.observer:
                self.observer({"method": "transport/error", "params": {"message": failure}})

    async def read_native_thread(self, thread_id: str) -> dict:
        return (await self.request("thread/read", {"threadId": thread_id, "includeTurns": False}))["thread"]

    async def attach_native_thread(self, thread_id: str) -> dict:
        result = await self.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        self.loaded.add(thread_id)
        return result["thread"]

    async def native_turns(self, thread_id: str) -> list[dict]:
        turns, cursor = [], None
        while True:
            params = {"threadId": thread_id, "limit": 100, "itemsView": "full", "sortDirection": "asc"}
            if cursor:
                params["cursor"] = cursor
            page = await self.request("thread/turns/list", params)
            for turn in page.get("data", []):
                if turn.get("itemsView") in {"summary", "notLoaded"}:
                    items, item_cursor = [], None
                    while True:
                        query = {"threadId": thread_id, "turnId": turn["id"], "limit": 100, "sortDirection": "asc"}
                        if item_cursor:
                            query["cursor"] = item_cursor
                        detail = await self.request("thread/items/list", query)
                        items.extend(item["item"] for item in detail.get("data", []))
                        item_cursor = detail.get("nextCursor")
                        if not item_cursor:
                            break
                    turn = {**turn, "items": items, "itemsView": "full"}
                turns.append(turn)
            cursor = page.get("nextCursor")
            if not cursor:
                return turns

    async def ensure_thread(self, agent) -> dict:
        lock = self.load_locks.setdefault(agent.name if hasattr(agent, "name") else agent.thread_id or self.cwd, asyncio.Lock())
        async with lock:
            return await self._ensure_thread(agent)

    async def _ensure_thread(self, agent) -> dict:
        if agent.thread_id in self.loaded:
            return {}
        cwd = agent.cwd or self.cwd
        policy, _ = permission_settings(agent.permission, cwd)
        params = {
            "cwd": cwd, "approvalPolicy": policy,
            "sandbox": {"full": "danger-full-access", "workspace": "workspace-write", "read-only": "read-only"}[agent.permission],
        }
        if agent.model:
            params["model"] = agent.model
        if agent.effort:
            params["config"] = {"model_reasoning_effort": agent.effort}
        if self.thread_options:
            options = self.thread_options(agent)
            params["config"] = {**params.get("config", {}), **options.get("config", {})}
            params.update({k: v for k, v in options.items() if k != "config"})
        new_thread = not agent.thread_id
        if agent.thread_id:
            params["threadId"] = agent.thread_id
            params["excludeTurns"] = True
            if agent.legacy_instructions:
                params["developerInstructions"] = ""
            result = await self.request("thread/resume", params)
        else:
            result = await self.request("thread/start", params)
        agent.legacy_instructions = False
        agent.thread_id = result["thread"]["id"]
        if new_thread and params.get("dynamicTools"):
            agent.room_tool_version = TOOL_VERSION
        self.loaded.add(agent.thread_id)
        if not agent.model:
            agent.model = result.get("model", "")
        if not agent.effort:
            agent.effort = result.get("reasoningEffort") or ""
        return result

    @staticmethod
    def input_items(text: str, images: list[str] | None = None) -> list[dict]:
        return [{"type": "text", "text": text},
                *({"type": "localImage", "path": path} for path in images or [])]

    def turn_input(self, thread_id, text, images=None):
        items = self.input_items(text, images)
        context = self.input_context(thread_id) if self.input_context else ""
        if context:
            # Keep the actual task as its own item, without changing the text
            # stored/displayed as the user's message.
            items.insert(0, {"type": "text", "text": context})
        return items

    async def steer_turn(self, thread_id: str, text: str, *, images: list[str] | None = None) -> bool:
        turn_id = self.active.get(thread_id)
        if not turn_id:
            return False
        try:
            await self.request("turn/steer", {"threadId": thread_id, "expectedTurnId": turn_id,
                                             "input": self.turn_input(thread_id, text, images)})
        except CodexError as exc:
            # Only a definite rejection due to completion may start a fresh turn.
            # Timeouts and transport failures can already have delivered the input.
            if exc.code in {-32600, -32602} and re.search(
                r"no active turn|turn.*(?:mismatch|does not match)|expected.*turn.*(?:match|actual)", str(exc), re.I
            ):
                return False
            raise
        return True

    async def run_turn(self, agent, text: str, on_event: Callable[[dict], Awaitable[None]],
                       *, on_started: Callable[[], Awaitable[None]] | None = None,
                       images: list[str] | None = None) -> dict:
        thread_id = agent.thread_id
        if not thread_id:
            raise CodexError("尚未创建 Codex 会话。")
        queue: asyncio.Queue = asyncio.Queue()
        self.queues[thread_id] = queue
        cwd = agent.cwd or self.cwd
        policy, sandbox = permission_settings(agent.permission, cwd)
        params = {"threadId": thread_id, "input": self.turn_input(thread_id, text, images),
                  "cwd": cwd, "approvalPolicy": policy, "sandboxPolicy": sandbox}
        if agent.model:
            params["model"] = agent.model
        if agent.effort:
            params["effort"] = agent.effort
        starting = asyncio.create_task(self.request("turn/start", params))
        turn_id = None
        try:
            response = await asyncio.shield(starting)
            turn_id = response["turn"]["id"]
            self.active[thread_id] = turn_id
            if on_started:
                await on_started()
            while True:
                event = await queue.get()
                method, data = event["method"], event.get("params", {})
                if method == "transport/error":
                    raise CodexError(data["message"])
                event_turn = data.get("turnId") or data.get("turn", {}).get("id")
                if event_turn and event_turn != turn_id:
                    continue
                await on_event(event)
                if method == "turn/completed":
                    turn = data["turn"]
                    if turn.get("status") == "failed":
                        error = turn.get("error") or {}
                        raise CodexError(clean_error(error.get("message", "Codex 执行失败")))
                    return turn
        except asyncio.CancelledError:
            # A cancelled UI task must not leave a still-running Codex turn behind.
            try:
                if turn_id is None:
                    response = await asyncio.wait_for(asyncio.shield(starting), 45)
                    turn_id = response["turn"]["id"]
                finished = False
                while not queue.empty():
                    event = queue.get_nowait()
                    if event["method"] == "turn/completed" and event.get("params", {}).get("turn", {}).get("id") == turn_id:
                        finished = True
                        break
                    if "id" in event:
                        await self.send({"id": event["id"], "error": {"code": -32000, "message": "Turn interrupted by user"}})
                if finished:
                    raise asyncio.CancelledError
                try:
                    await self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
                except CodexError:
                    # Completion may race with interrupt; its notification still
                    # proves the old turn has stopped and keeps other threads alive.
                    pass
                async with asyncio.timeout(15):
                    while True:
                        event = await queue.get()
                        if event["method"] == "turn/completed" and event.get("params", {}).get("turn", {}).get("id") == turn_id:
                            break
                        if "id" in event:
                            await self.send({"id": event["id"], "error": {"code": -32000, "message": "Turn interrupted by user"}})
            except Exception:
                # If the server cannot acknowledge interruption, terminate it rather
                # than presenting an idle member while its commands keep running.
                await self.close()
            raise
        finally:
            self.active.pop(thread_id, None)
            self.queues.pop(thread_id, None)

    async def close(self) -> None:
        if self.closing:
            return
        self.closing = True
        proc = self.process
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        for task in (self.reader, self.stderr_reader):
            if task and task is not asyncio.current_task():
                task.cancel()
        await asyncio.gather(*(task for task in (self.reader, self.stderr_reader)
                               if task and task is not asyncio.current_task()), return_exceptions=True)
        self.loaded.clear()
