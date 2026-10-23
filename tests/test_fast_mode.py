"""Per-member Fast switches must reach Codex, survive resume, and respect turns."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

from codex_room.attachments import Prompt
from codex_room.backend import Service
from codex_room.completion import is_room_command


class FastModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.output = []
        self.command = [sys.executable, str(Path(__file__).with_name("fake_codex.py"))]
        self.service = Service(self.cwd, command=self.command, emit=self.output.append)
        await self.service.start()
        self.service.client.models[0]["serviceTiers"] = [{"id": "priority", "name": "Fast"}]
        await self.service.command("/add 开发")

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    async def idle(self):
        async with asyncio.timeout(5):
            while any(m.busy for m in self.service.agents.values()):
                await asyncio.sleep(.01)

    async def settings(self):
        await self.service.submit(Prompt("@开发 settings"))
        await self.idle()
        return next(e.body for e in reversed(self.service.entries) if e.sender == "开发")

    async def test_toggle_targets_one_member_and_creates_no_model_turn(self):
        await self.service.command("/add 文档")
        self.assertTrue(is_room_command("/fast"))
        self.assertTrue(await self.service.submit(Prompt("@开发 /fast")))
        self.assertEqual(self.service.agents["开发"].service_tier, "priority")
        self.assertIsNone(self.service.agents["文档"].service_tier)
        self.assertFalse(self.service.entries)
        self.assertIsNone(self.service.agents["开发"].thread_id)
        await self.service.submit(Prompt("@开发 /fast"))
        self.assertEqual(self.service.agents["开发"].service_tier, "default")
        await self.service.command("/fast")  # Current selected member is 文档.
        self.assertEqual(self.service.agents["文档"].service_tier, "priority")

    async def test_on_off_reaches_loaded_thread_and_persists_across_restart(self):
        await self.service.command("/fast on")
        self.assertIn('"serviceTier": "priority"', await self.settings())
        identity = self.service.agents["开发"].thread_id
        await self.service.command("/fast off")
        self.assertIn('"serviceTier": "default"', await self.settings())
        self.assertEqual(self.service.agents["开发"].thread_id, identity)
        await self.service.close()
        self.service = Service(self.cwd, command=self.command, emit=self.output.append)
        await self.service.start()
        self.assertEqual(self.service.agents["开发"].service_tier, "default")
        self.assertIn('"serviceTier": "default"', await self.settings())
        self.assertEqual(self.service.agents["开发"].thread_id, identity)

    async def test_first_toggle_inherits_fast_default_but_off_overrides_it(self):
        self.service.client.config["service_tier"] = "fast"
        await self.service.command("/fast")
        self.assertEqual(self.service.agents["开发"].service_tier, "default")
        self.assertIn('"serviceTier": "default"', await self.settings())
        self.assertEqual(self.service.client.config["service_tier"], "fast")

    async def test_busy_toggle_does_not_change_the_current_turn(self):
        requests = []
        original = self.service.client.request
        async def request(method, params, **kwargs):
            requests.append((method, json.loads(json.dumps(params))))
            return await original(method, params, **kwargs)
        self.service.client.request = request
        await self.service.submit(Prompt("@开发 steer-hold"))
        async with asyncio.timeout(5):
            while not self.service.client.active:
                await asyncio.sleep(.01)
        await self.service.command("/fast on")
        turns = [p for m, p in requests if m == "turn/start"]
        self.assertEqual(len(turns), 1)
        self.assertNotIn("serviceTier", turns[0])
        self.assertTrue(self.service.agents["开发"].busy)
        await self.service.stop("开发")
        self.assertIn('"serviceTier": "priority"', await self.settings())

    async def test_unsupported_tier_child_and_invalid_arguments_do_not_mutate(self):
        self.service.client.models[0]["serviceTiers"] = []
        with self.assertRaisesRegex(ValueError, "没有提供 Fast"):
            await self.service.command("/fast on")
        self.assertIsNone(self.service.agents["开发"].service_tier)
        with self.assertRaisesRegex(ValueError, "on"):
            await self.service.command("/fast unknown")
        await self.service.command("/fast off")  # Turning off remains possible.
        member = self.service.agents["开发"]
        member.parent_name = "parent"
        with self.assertRaisesRegex(ValueError, "父会话"):
            await self.service.command("/fast")
        self.assertEqual(member.service_tier, "default")

    async def test_older_catalog_and_project_defaults(self):
        catalog = self.service.client.models[0]
        catalog.pop("serviceTiers")
        catalog["additionalSpeedTiers"] = ["fast"]
        self.assertEqual(self.service.client.fast_service_tier("test-model"), "priority")
        project = self.cwd / "another-project"
        (project / ".codex").mkdir(parents=True)
        # The fake config does not need a speed option: this check ensures the
        # member's directory is queried instead of borrowing the room's default.
        self.service.agents["开发"].cwd = str(project)
        self.service.client.config["service_tier"] = "fast"
        self.assertIsNone(await self.service.client.effective_service_tier(self.service.agents["开发"]))


if __name__ == "__main__":
    unittest.main()
