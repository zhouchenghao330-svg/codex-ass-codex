# Codex Room

一个终端，多个独立的真实 Codex 会话。通过本机 `codex app-server` 接入，沿用现有 Codex 登录、模型服务和项目配置。

默认权限为 **Full Access**：`danger-full-access` + `approval_policy = "never"`。每位成员可独立设置模型、推理强度和权限，修改从下一轮开始生效。

## 启动

需要 [uv](https://docs.astral.sh/uv/) 和已登录、可正常运行的 Codex CLI（本机验证版本：0.163.0）。首次运行会安装 Python 依赖。

```sh
uv run codex-room
```

也支持 `uv run codex_room` 和 `uv run python -m codex_room`。用 `--cwd /path/to/project` 指定 Codex 的工作目录。

默认真实调用模型并允许执行任务。纯界面演示使用 `uv run codex-room --demo`，演示模式不连接 Codex。

默认只有消息、三行输入区和一行状态，成员侧栏默认收起。支持鼠标滚动消息和中文输入。

点击聊天区或重新激活窗口即可直接输入；在消息区开始打字或粘贴也会自动回到输入框。选中文本和弹窗中的输入保持正常。

Codex 回复支持 Markdown：标题、加粗、列表、引用、链接、表格、行内代码和带语法高亮的代码块。流式回复和恢复的历史消息使用相同的渲染方式；工具日志保留原始文本。

运行中的回复下方显示带旋转动效的 `Working`、当前阶段和耗时。收到 Codex 的公开思考摘要时显示摘要标题；没有摘要时显示实际运行阶段。完成、失败或停止后动效自动消失。

每条回复顶部显示成员名字、`@名字` 和工作目录。长路径自动缩略，悬停可查看完整路径。回复最底部显示 `Worked for 11m 32s · 20:25`：耗时从该轮实际开始计算，包含会话启动和工具执行，不包含排队等待；时间为本地结束时间。停止和失败也会保留对应耗时和结束时间，重启后可以继续查看。旧记录没有计时数据时不会补造耗时。

新房间初始没有成员。`/add 名字` 创建普通 Codex 会话，名字只用于显示和 `@` 点名，应用不附加身份或职责提示词。

用 `/add 名字 路径` 为成员单独指定工作目录，例如 `/add a ~/code/project-a`。支持绝对路径、相对路径和 `~`；相对路径以群聊的工作目录为基准，带空格的路径请加引号。目录必须已存在，省略时沿用群聊目录。目录会传入 Codex 会话及每轮请求，并随成员保存；不同成员可以在不同项目中并行工作。

每位 Codex 的回复使用各自的文字颜色和深色背景。`/add` 或 `Ctrl+N` 可在创建窗口中填写工作目录、选择颜色并预览；命令可写 `/add a ~/code/project-a --color blue`。仍兼容 `/add a blue` 和 `/add a #83c8e8`；如果相对目录与颜色同名，使用 `./blue` 指定目录。省略颜色时自动分配未占用的颜色，预设颜色用完后继续生成新颜色。手动选择已占用的颜色会提示更换，配色会随成员一起保存。

## 试试这些操作

```text
/add a
/add b
/add c blue
/add dev ~/code/project --color mint
/add paper "/Users/me/My Paper"
@a 你好
@b 查看当前目录
/focus b
/members
/model
@b /model
@b /model gpt-6.1-sol low
/permissions
@b /permissions workspace
/stop a
/reconnect
/help
```

- `Ctrl+B` 或点击底部「成员」展开 / 收起侧栏；点击成员切到与该成员的单人对话，同时切换默认接收者。
- 点击侧栏顶部「全部消息」恢复所有成员的对话；当前视图会高亮显示。默认接收者保持不变，仍可从输入框底部查看。
- 从 `@` 列表选中成员，或成功发送 `@名字 消息` 后，该成员会成为默认接收者；下一条直接输入继续发给它。
- `Ctrl+1/2/3` 或 `/focus 名字` 切到对应成员的单人对话；`Ctrl+N` 添加成员。
- `/add 名字 [目录] [--color 颜色]` 指定工作目录和配色；颜色支持 blue、purple、mint、rose、中文颜色名及十六进制色值。
- `Ctrl+L` 切换全部消息 / 当前成员视图。
- 输入 `@` 弹出成员列表，继续输入名字过滤；`↑/↓` 选择，`Tab` 或 `Enter` 确认，也可以鼠标点击。`Esc` 关闭。
- 确认成员会填入 `@名字 ` 并切换默认接收者，写好消息后再按 `Enter` 发送。
- `/model` 打开当前成员的模型和推理强度选择，也可用 `@名字 /model` 指定成员。
- `/permissions` 打开权限选择；可直接指定 `full`、`workspace`、`read-only`，兼容 `/permission`。
- 同一成员忙时，新消息排队；不同成员可以同时工作。
- 工具调用默认折叠成一行摘要；`Ctrl+T` 展开 / 收起命令、输出和其他工具详情，回复正文保持可见。
- `/stop 名字` 停止该成员当前回复，并取消其所有排队消息。
- `F1` 查看帮助，`Ctrl+Q` 或 `Ctrl+C` 退出。

每个成员使用独立的 Codex thread。模型上下文由 Codex 自己维护；后续消息只发送新增内容，界面的全部消息不会广播给其他成员。

群聊显示记录、成员配置和 thread ID 保存在工作目录的 `.codex-room/state.json`，已加入 Git 忽略。重启后恢复会话，尚未完成或排队的请求不会自动重发。同一工作目录只允许打开一个真实群聊，避免并发覆盖状态。群聊内调整保存在成员配置中，日常 Codex 全局配置保持原值。

普通审批和 Codex 的补充问题会在 TUI 中弹出。MCP 的任意表单交互暂未实现，遇到时会明确取消并提示。`/reconnect` 可重连失败或退出的 app-server。

## 预览与验证

```sh
uv run python scripts/preview.py
uv run python -m unittest discover -s tests -v
```

界面检查只用模拟回复，协议与交互测试使用本地测试服务。生成的截图在 `artifacts/` 中。

终端测试会在独立 PTY 中检查 Ctrl+Q、Ctrl+C、`/quit` 和异常退出后的键盘模式及退格恢复。界面只启用基本扩展键盘支持，退出时恢复调用方的键盘模式。

如果旧版本退出后退格变成了 `7u` / `127u`，可在受影响的终端运行以下命令，清除遗留的扩展键盘模式：

```sh
printf '\033[=0u'
```

真实端到端验证（会调用模型，包含只读 `pwd` 和可中断的 `sleep 30`，群聊状态放在临时目录）：

```sh
uv run python scripts/check_live.py
```

参考：[Codex app-server 官方协议](https://learn.chatgpt.com/docs/app-server)。
