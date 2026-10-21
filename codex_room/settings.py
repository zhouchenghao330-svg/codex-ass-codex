from __future__ import annotations

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Select, Static

PERMISSION_LABELS = {"full": "Full Access", "workspace": "Workspace", "read-only": "Read-only"}


class ModelSettings(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [("escape", "cancel", "返回")]

    def __init__(self, name: str, models: list[dict], model: str, effort: str) -> None:
        super().__init__()
        self.member = name
        self.models = list(models)
        if model and not any(item["id"] == model for item in self.models):
            self.models.append({"id": model, "displayName": model, "supportedReasoningEfforts": [], "defaultReasoningEffort": effort})
        self.model = model or self.models[0]["id"]
        self.effort = effort

    def efforts(self, model: str) -> tuple[list[str], str]:
        item = next((item for item in self.models if item["id"] == model), {})
        efforts = [option["reasoningEffort"] for option in item.get("supportedReasoningEfforts", [])]
        return efforts or [self.effort or "medium"], item.get("defaultReasoningEffort") or "medium"

    def compose(self) -> ComposeResult:
        efforts, default = self.efforts(self.model)
        current = self.effort if self.effort in efforts else (default if default in efforts else efforts[0])
        with Vertical(id="dialog"):
            yield Static(Text(f"@{self.member} · 模型设置", style="bold"), id="dialog-title")
            yield Static("模型", classes="field-label")
            yield Select([(item.get("displayName", item["id"]), item["id"]) for item in self.models], value=self.model, allow_blank=False, id="model-choice")
            yield Static("推理强度", classes="field-label")
            yield Select([(value, value) for value in efforts], value=current, allow_blank=False, id="effort-choice")
            yield Static("从下一轮开始生效，会话历史继续保留。", classes="muted")
            with Horizontal(id="dialog-actions"):
                yield Button("取消", id="settings-cancel")
                yield Button("保存", id="settings-save", variant="primary")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "model-choice" or not self.is_mounted or event.value is Select.BLANK:
            return
        effort = self.query_one("#effort-choice", Select)
        previous = effort.value
        options, default = self.efforts(str(event.value))
        effort.set_options([(value, value) for value in options])
        effort.value = previous if previous in options else (default if default in options else options[0])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "settings-save":
            self.dismiss((str(self.query_one("#model-choice", Select).value), str(self.query_one("#effort-choice", Select).value)))
        else:
            self.dismiss(None)


class PermissionSettings(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "返回")]

    def __init__(self, name: str, permission: str) -> None:
        super().__init__()
        self.member, self.permission = name, permission

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(f"@{self.member} · 权限设置", style="bold"), id="dialog-title")
            yield Select([(label, mode) for mode, label in PERMISSION_LABELS.items()], value=self.permission, allow_blank=False, id="permission-choice")
            yield Static("Full Access：文件、命令和网络使用完整访问权限。\nWorkspace / Read-only：需要越界时在这里请求批准。\n设置从下一轮开始生效。", classes="muted")
            with Horizontal(id="dialog-actions"):
                yield Button("取消", id="settings-cancel")
                yield Button("保存", id="settings-save", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(str(self.query_one("#permission-choice", Select).value) if event.button.id == "settings-save" else None)


class RuntimeQuestion(ModalScreen[dict | None]):
    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, name: str, questions: list[dict]) -> None:
        super().__init__()
        self.member, self.questions = name, questions

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(f"@{self.member} 需要你补充信息", style="bold"), id="dialog-title")
            with VerticalScroll(id="request-details"):
                for index, question in enumerate(self.questions):
                    yield Static(Text(question.get("question", "请补充信息")), classes="field-question")
                    options = question.get("options") or []
                    if options:
                        yield Static(Text("\n".join(f"· {option.get('label', '')}  {option.get('description', '')}" for option in options)), classes="muted")
                    yield Input(id=f"answer-{index}", placeholder="输入答案或选项名称", password=bool(question.get("isSecret")))
            with Horizontal(id="dialog-actions"):
                yield Button("取消", id="request-cancel")
                yield Button("回复", id="request-accept", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id != "request-accept":
            self.dismiss(None)
            return
        answers = {}
        for index, question in enumerate(self.questions):
            value = self.query_one(f"#answer-{index}", Input).value.strip()
            if not value:
                self.query_one(f"#answer-{index}", Input).focus()
                return
            answers[question["id"]] = {"answers": [value]}
        self.dismiss({"answers": answers})


class RuntimeApproval(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "拒绝")]

    def __init__(self, name: str, details: str) -> None:
        super().__init__()
        self.member, self.details = name, details

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(Text(f"@{self.member} 请求批准", style="bold"), id="dialog-title")
            with VerticalScroll(id="request-details"):
                yield Static(Text(self.details))
            with Horizontal(id="dialog-actions"):
                yield Button("拒绝", id="request-cancel")
                yield Button("允许这次", id="request-accept", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "request-accept")
