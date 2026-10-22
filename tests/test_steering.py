import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_room.app import CodexRoom
from codex_room.runtime import CodexClient, CodexError


ROOT = Path(__file__).resolve().parents[1]


def client(cwd):
    return CodexClient(cwd, command=[sys.executable, str(ROOT / "tests/fake_codex.py")])


async def wait_until(predicate, pilot):
    async with asyncio.timeout(5):
        while not predicate():
            await pilot.pause(0.01)


class SteeringTests(unittest.IsolatedAsyncioTestCase):
    async def test_followups_share_one_turn_and_late_ack_does_not_duplicate_work(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test() as pilot:
                await app.connection_task
                await app.create_member("a")
                await app.create_member("b")
                await app.submit("@a steer-hold")
                await app.submit("@b slow")
                await wait_until(lambda: len(app.client.active) == 2, pilot)
                task = app.tasks["a"]
                thread = app.agents["a"].thread_id
                turn = app.client.active[thread]
                with patch.object(app.client, "request", wraps=app.client.request) as requests:
                    await app.submit("@a 第一条补充")
                    await app.submit("@a 第二条补充")
                    self.assertIs(app.tasks["a"], task)
                    self.assertFalse(app.agents["a"].queue)
                    answer = next(entry for entry in app.entries if entry.sender == "a")
                    inputs = [entry for entry in app.entries if entry.sender == "you" and entry.target == "a"]
                    self.assertEqual([entry.state for entry in inputs], ["done", "steered", "steered"])
                    self.assertGreater(app.entries.index(answer), app.entries.index(inputs[-1]))
                    timeline = app.query_one("#timeline")
                    self.assertGreater(list(timeline.children).index(app.cards[id(answer)]),
                                       list(timeline.children).index(app.cards[id(inputs[-1])]))
                    # The completion notification arrives before the steer RPC acknowledgment.
                    await app.submit("@a finish-and-ack")
                    await asyncio.wait_for(task, 5)
                    calls = [call for call in requests.call_args_list if call.args[0] == "turn/steer"]
                    self.assertEqual(len(calls), 3)
                    self.assertTrue(all(call.args[1]["expectedTurnId"] == turn for call in calls))
                    self.assertFalse(any(call.args[0] == "turn/start" for call in requests.call_args_list))
                self.assertEqual(len([entry for entry in app.entries if entry.sender == "a"]), 1)
                self.assertIn("第一条补充", answer.body)
                self.assertIn("第二条补充", answer.body)
                self.assertEqual(answer.state, "done")
                self.assertTrue(app.agents["b"].busy)
                await app.stop_agent("b")

    async def test_startup_buffers_are_steered_as_soon_as_turn_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test() as pilot:
                await app.connection_task
                await app.create_member("a")
                gate = asyncio.Event()
                original = app.client.ensure_thread

                async def delayed(agent):
                    await gate.wait()
                    return await original(agent)

                with patch.object(app.client, "ensure_thread", side_effect=delayed):
                    await app.submit("@a steer-hold")
                    await app.submit("@a sent during startup")
                    self.assertTrue(app.agents["a"].queue)
                    gate.set()
                    await wait_until(lambda: bool(app.client.active) and not app.agents["a"].queue, pilot)
                    self.assertEqual(len([entry for entry in app.entries if entry.sender == "a"]), 1)
                    await app.submit("@a finish-steer")
                    await app.tasks["a"]
                self.assertIn("sent during startup", app.agents["a"].history[-1].body)

    async def test_completion_rejection_starts_one_new_turn_for_unsent_input(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test() as pilot:
                await app.connection_task
                await app.create_member("a")
                await app.submit("@a steer-hold")
                await wait_until(lambda: bool(app.client.active), pilot)
                with patch.object(app.client, "request", wraps=app.client.request) as requests:
                    await app.submit("@a race-to-next")
                    await app.tasks["a"]
                    self.assertEqual(sum(call.args[0] == "turn/start" for call in requests.call_args_list), 1)
                answers = [entry for entry in app.entries if entry.sender == "a"]
                self.assertEqual(len(answers), 2)
                self.assertIn("race-to-next", answers[-1].body)
                self.assertFalse(app.agents["a"].queue)

    async def test_rejected_or_uncertain_steers_are_not_silently_replayed(self):
        with tempfile.TemporaryDirectory() as tmp, patch("codex_room.app.CodexClient", client):
            app = CodexRoom(cwd=ROOT, state_dir=Path(tmp))
            async with app.run_test() as pilot:
                await app.connection_task
                await app.create_member("a")
                await app.submit("@a steer-hold")
                await wait_until(lambda: bool(app.client.active), pilot)
                await app.submit("@a reject-steer")
                original = app.client.request

                async def late_timeout(method, params, **kwargs):
                    result = await original(method, params, **kwargs)
                    if method == "turn/steer":
                        raise CodexError("Codex 请求超时：turn/steer")
                    return result

                with patch.object(app.client, "request", side_effect=late_timeout):
                    await app.submit("@a accepted but acknowledgment lost")
                failed = [entry for entry in app.entries if entry.sender == "you" and entry.state == "failed"]
                self.assertEqual(len(failed), 2)
                self.assertFalse(app.agents["a"].queue)
                await app.submit("@a finish-steer")
                await app.tasks["a"]
                answer, = [entry for entry in app.entries if entry.sender == "a"]
                self.assertEqual(answer.body.count("accepted but acknowledgment lost"), 1)
                self.assertNotIn("reject-steer", answer.body)
