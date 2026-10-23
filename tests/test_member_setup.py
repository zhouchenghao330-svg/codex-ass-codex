"""Member model setup uses the live catalog without starting a Codex turn."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from codex_room.backend import Service


class MemberSetupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name).resolve()
        self.events = []
        self.service = Service(self.cwd, command=[sys.executable, str(Path(__file__).with_name("fake_codex.py"))],
                               emit=self.events.append)
        await self.service.start()

    async def asyncTearDown(self):
        await self.service.close()
        self.tmp.cleanup()

    def choice(self):
        return next(e for e in reversed(self.events) if e["type"] == "choose")

    async def test_add_inherits_project_defaults_and_saves_only_after_both_choices(self):
        project = self.cwd / "Project A"
        (project / ".codex").mkdir(parents=True)
        (project / ".codex/config.toml").write_text('model = "test-model"\nmodel_reasoning_effort = "high"\n')
        await self.service.command(f'/add 新同事 "{project}"')
        member = self.service.agents["新同事"]
        model_choice = self.choice()
        self.assertEqual(model_choice["action"], "member-model")
        self.assertEqual(model_choice["data"]["member"], member.name)
        self.assertEqual(model_choice["selected"], "test-model")
        self.assertEqual(member.cwd, str(project))
        await self.service.handle({"type": "member-model", "member": member.name, "model": "test-model"})
        self.assertEqual(self.choice()["selected"], "high")  # Project config wins over model's low default.
        self.assertEqual([v["value"] for v in self.choice()["values"]], ["low", "high"])
        self.assertEqual((member.model, member.effort), ("test-model", "high"))
        self.assertIsNone(member.thread_id)
        self.assertEqual(self.service.entries, [])
        await self.service.handle({"type": "member-effort", "member": member.name,
                                   "model": "test-model", "effort": "low"})
        self.service.save()
        state = json.loads((self.cwd / ".codex-room/state.json").read_text())
        self.assertEqual((state["agents"][0]["model"], state["agents"][0]["effort"]), ("test-model", "low"))
        self.assertIsNone(member.thread_id)

    async def test_model_change_uses_supported_efforts_and_cancel_keeps_original(self):
        self.service.client.models.append({"id": "review-model", "displayName": "Review Model",
            "defaultReasoningEffort": "low", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]})
        await self.service.command("/add A")
        member = self.service.agents["A"]
        await self.service.handle({"type": "member-model", "member": "A", "model": "review-model"})
        choice = self.choice()
        self.assertEqual(choice["selected"], "low")
        self.assertEqual([v["value"] for v in choice["values"]], ["low"])
        self.assertEqual((member.model, member.effort), ("test-model", "high"))
        with self.assertRaisesRegex(ValueError, "不支持"):
            await self.service.handle({"type": "member-effort", "member": "A",
                                       "model": "review-model", "effort": "high"})
        self.assertEqual((member.model, member.effort), ("test-model", "high"))
        # The UI can dismiss the picker without sending another message.
        await self.service.command("/model")
        self.assertEqual(self.choice()["selected"], "test-model")
        await self.service.handle({"type": "member-effort", "member": "A",
                                   "model": "review-model", "effort": "low"})
        self.assertEqual((member.model, member.effort), ("review-model", "low"))

    async def test_picker_targets_stable_member_even_after_rename_or_view_change(self):
        await self.service.command("/add A")
        await self.service.handle({"type": "member-model", "member": "A", "model": "test-model"})
        await self.service.command("/rename A 已改名")
        await self.service.command("/add B")
        await self.service.handle({"type": "member-effort", "member": "A", "model": "test-model", "effort": "low"})
        self.assertEqual(self.service.agents["A"].effort, "low")
        self.assertEqual(self.service.agents["B"].effort, "high")
        self.assertEqual(self.service.selected, "B")
        await self.service.command("/del A")
        with self.assertRaisesRegex(ValueError, "不存在"):
            await self.service.handle({"type": "member-effort", "member": "A", "model": "test-model", "effort": "high"})

    async def test_custom_models_and_missing_catalog_keep_working_defaults(self):
        self.service.client.models = []
        await self.service.command("/add Custom")
        self.assertEqual([v["value"] for v in self.choice()["values"]], ["test-model"])
        await self.service.handle({"type": "member-model", "member": "Custom", "model": "test-model"})
        self.assertEqual([v["value"] for v in self.choice()["values"]], ["high"])
        await self.service.handle({"type": "member-effort", "member": "Custom", "model": "test-model", "effort": "high"})
        with self.assertRaisesRegex(ValueError, "模型列表"):
            await self.service.handle({"type": "member-model", "member": "Custom", "model": "unknown-model"})
        self.service.client.config = {}
        await self.service.command("/add Default")
        self.assertEqual(self.choice()["values"][0]["label"], "沿用项目默认")
        await self.service.handle({"type": "member-model", "member": "Default", "model": ""})
        self.assertEqual(self.choice()["values"][0]["label"], "模型默认")
        await self.service.handle({"type": "member-effort", "member": "Default", "model": "", "effort": ""})

    async def test_model_without_reasoning_options_does_not_inherit_unsupported_effort(self):
        self.service.client.models.append({"id": "no-reasoning", "supportedReasoningEfforts": []})
        await self.service.command("/add A")
        await self.service.handle({"type": "member-model", "member": "A", "model": "no-reasoning"})
        self.assertEqual([v["value"] for v in self.choice()["values"]], [""])
        with self.assertRaisesRegex(ValueError, "模型默认"):
            await self.service.handle({"type": "member-effort", "member": "A", "model": "no-reasoning", "effort": "high"})
        await self.service.handle({"type": "member-effort", "member": "A", "model": "no-reasoning", "effort": ""})
        self.assertEqual((self.service.agents["A"].model, self.service.agents["A"].effort), ("no-reasoning", ""))
        await self.service.inherit_defaults(self.service.agents["A"])
        self.assertEqual(self.service.agents["A"].effort, "")


if __name__ == "__main__":
    unittest.main()
