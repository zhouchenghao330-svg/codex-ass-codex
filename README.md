# Codex Room

一个终端，多个独立的 Codex 会话。正式界面使用 **Rust + Ratatui Fullscreen**：统一深色背景和米白正文，通过彩色名字与细竖线区分成员。Codex 连接和兼容存储继续由本地 Python 服务负责，沿用本机的 Codex 登录与项目配置。

## 启动

源码运行需要 [uv](https://docs.astral.sh/uv/)、[Rust 1.88+](https://rustup.rs/) 和已登录、可正常运行的 Codex CLI。首次启动会安装 Python 依赖并构建 Rust 界面，后续直接使用已构建的程序；Rust 源码更新后会自动重新构建。

```sh
uv run codex_room
```

同样支持 `uv run codex-room`、`uv run python -m codex_room`。通过 `--cwd /path/to/project` 指定房间目录。纯界面演示用 `--demo`，不连接 Codex、不写真实房间数据。

旧 Textual 界面保留为显式回退，无需 Rust：

```sh
uv run codex_room --legacy-ui
```

Windows ZIP 可完整解压后运行 `start.bat`；首次构建需具备 Rust 的本机编译环境，步骤见 [Windows 说明](WINDOWS.md)。当前 Rust 界面已在 macOS 的 PTY 和真实 Codex 中验证，Windows 尚待实际运行验证。

## 成员与对话

新房间没有预设成员。名字只用于显示和点名，不会注入角色提示词。

```text
/add 开发 ~/code/project
/add 笔记 "/path/to/My Notes" --color mint
@开发 看看这个项目
@开发 @笔记 分别评价这个方案
/rename 开发 环境排查
/focus 笔记
/focus all
/model
@环境排查 /model 模型名 low
/permissions workspace
/stop 环境排查
/del 环境排查
/reconnect
/help
```

`/add 名字 [目录] [--color 颜色]` 支持中文名、绝对/相对路径、~ 和带引号的路径。省略目录沿用房间目录；省略颜色自动分配未占用的颜色。颜色支持预设名称和十六进制值。Ctrl+N 或「添加」预填创建命令，Tab 可补全目录、颜色等参数。

侧栏默认收起，底部「成员」或 Ctrl+B 可展开。点击成员进入单人对话并切换默认接收者；「全部消息」、`/focus all` 或 Ctrl+L 可回到聊天室。名字也出现在 @ 补全中，侧栏可以一直收起。

开头连续 @ 多位成员时，各自收到同一任务、使用独立 thread 并行处理；不存在的成员会在任何投递前报错。正文普通提及不会转发给别人。忙时新消息使用 `turn/steer` 追加到当前轮；启动期间先暂存，拿到 turn ID 后追加。不对超时或不确定的投递盲目重发。追加的用户消息位于对话中间，新输出接在它后面，旧工具结果保持原位。

默认权限为 **Full Access**（danger-full-access / never）。模型、强度和权限按成员保存，调整从下一轮生效。后台完成或请求交互只做轻量提示；可切到成员后点「回复请求」处理。MCP 任意表单暂不支持，会明确取消。`/del 名字` 先停止并归档该成员，不删除项目目录或 Codex 原生历史。

## 阅读、输入与工具

- 直接打字即可输入。Enter 发送，Ctrl+J 换行；Shift+Enter 取决于终端是否单独上报。输入自动折行，最多显示 8 行；↑/↓ 在空输入框中翻历史，在多行草稿中移动光标。
- `/mem` + Tab 补成 `/members`；@、命令、成员、模型、推理强度、权限、目录和颜色均支持补全。↑/↓、Tab/Enter 或鼠标选择候选，选中只填入文本，再按 Enter 执行。普通路径不会误认为命令。
- 一次粘贴达到 1000 字符或 8 行，显示 `[Pasted Content N chars]`；原文完整发送。本地图片路径显示 `[Image #1]`，以 localImage 发送。左右跨过整个块，Backspace/Delete 整块删除，Ctrl+Z/Y 撤销/重做，Alt+Enter 展开长文本；选中长粘贴块复制时还原完整原文。图片文件需仍然存在；终端需把图片粘贴/拖入转换为本地路径。
- 拖选只覆盖正文，可跨消息；双击片段、三击行，点高亮外或 Esc 取消。Ctrl+C 复制选区，没有选区时退出；Ctrl+Q 始终退出。滚轮每次 1 行，PageUp/Down 翻页，Ctrl+End 或底部「底部」回到最新。
- 拖到正文上下边缘自动滚动。保留选区时正文排版暂时固定，后台继续工作；取消后合入新输出，避免锚点移动。调整终端宽度会清除旧选区并重排。
- Markdown、代码语法和 diff 使用各自的颜色。点击回复标题折叠整条回复；工具标题切换摘要展开/收起，单项可独立查看详情，「全部详情」单独控制。命令、MCP/Codex 工具、文件新增/修改/删除/重命名分别显示，文件提供 +新增/-删除和完整 diff；计数是工具记录的变更，不是仓库最终净差异。
- 工作中显示动效、阶段与耗时，只展示公开思考摘要的标题。回复顶部是成员与目录，底部保留 `Worked for … · 结束时间`，失败或停止也保留计时。

## 转发

拖选连续对话 → Ctrl+F 或底部「转发」→ 搜索已有成员或新建 → 补一句任务后发送。引用可预览、移除，或从默认的「与来源成员的对话」切换到「整个选区」。

边界只带所选正文；跨过的折叠回复、工具摘要会带上内部详情，包括完整输出和文件 diff。长粘贴保留原文，图片随引用发送。快照保留原发言者、目录、消息 ID，不自动摘要，也不自动联系来源成员。接收者的现有草稿保留，可以继续聊几轮。

「返回来源」恢复原视图和原草稿，滚到最新消息；目标的未发送草稿留在那里。适合把排查讨论转给另一个 Codex 记笔记、调研，再回来继续讨论。当前不包含原生子代理聊天室；后续范围见 [功能交接](docs/next-version-handoff.md)。

## 数据与维护

`.codex-room/state.json` 沿用 v2 格式，保留成员 thread、配置、历史、附件、计时，并保存各视图草稿。重启恢复原会话，未完成请求不自动重发。同一房间只允许一个真实实例。登录信息由 Codex 自己管理，不进入 Room 状态或仓库。

界面与通信边界、缓存和当前限制见 [Rust 迁移说明](docs/rust-fullscreen.md)；旧界面操作见 [Textual 旧版说明](docs/textual-legacy.md)。

```sh
cargo test --locked --manifest-path terminal-ui/Cargo.toml
uv run python -m unittest discover -s tests -v
uv run scripts/check_rust_ui.py
```

PTY 检查只使用隔离的模拟服务，不读写用户房间或剪贴板。真实检查会调用模型两次：`uv run scripts/check_rust_ui_live.py`。

四组对照实验保留：`uv run python scripts/terminal_lab.py --lines 10000`，见 [实验说明](experiments/terminal-lab/README.md)。

协议参考：[Codex app-server 官方文档](https://learn.chatgpt.com/docs/app-server)。
