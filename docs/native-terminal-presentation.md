# 终端原生呈现调研与验证

更新：2026-10-22。范围：调研和独立小样，**尚未替换 Codex Room 的启动入口**。

后续决定：先不迁移正式项目，使用相同数据比较 Rust Inline、Rust Fullscreen、当前 Textual 与关闭捕获的 Textual。四组实验已经独立落在 [terminal-lab](../experiments/terminal-lab/README.md)，入口为 `uv run python scripts/terminal_lab.py --lines 10000`。下文保留此前 Python Inline 研究；正式选型以同一 Orca 终端中的体验结果为依据。

## 结论

可以在 Python 中做出“正文写进终端历史、终端负责拖选与复制、程序维护输入区”的呈现方式。无需先把项目改成 Rust，也无需修改 Orca。

本次用 `prompt_toolkit` 的 inline 输入区与 Rich 输出制作了独立小样，验证普通终端缓冲区、后台输出和输入草稿可以共存。下一步应先在 Orca 中亲自体验它的选择和滚动，再决定正式迁移。

这不意味着把现有 Textual 的鼠标关闭就完成了，也不意味着当前所有点击功能都能原样保留。

## 先试小样

在项目目录运行：

```sh
uv run scripts/native_terminal_probe.py
```

依赖通过脚本内的 uv 元数据隔离安装；没有修改项目的 `pyproject.toml` 或 `uv.lock`。小样只用模拟内容，不连接模型、不创建 Codex 成员、不读取或保存真实房间状态。

建议按这个顺序体验：

1. 输入 `/sample 120`，从上向下跨屏拖选、反向拖选、双击、取消选择、复制。
2. 输入 `/background`，随即输入一段草稿，移动光标到中间，等模拟的 A/B 回复到来，检查草稿和光标。
3. 让后台消息继续产生，同时尝试滚动和选择旧文字，观察是否被拉回底部或取消选区。
4. 粘贴中文和长路径；用 `Ctrl+J` 或 `Alt+Enter` 换行，Enter 发送。
5. 用 `Tab` 补全 `/sample`、`/quit` 等；空输入时用方向键翻历史。
6. `/quit` 退出后，检查终端里普通输入和退格。

小样暂不提供：真实 Codex、消息过滤、可点击折叠、原生转发、原子长粘贴块／图片块、完整运行状态区、按 token 流式 Markdown。A/B 是小样的固定测试标签，不会加入真实房间。

## 已执行的验证

```sh
uv run scripts/check_native_terminal.py
```

3 项检查通过：

- 正在编辑中文草稿且光标位于中间时，后台回复和 800 行历史输出保留草稿与光标。
- 多行输入、Enter 提交、Ctrl+C 清空、方向键历史和 Tab 补全。
- 实际 POSIX PTY 中输出 800 行后退出；未启用 alternate screen、未开启鼠标上报、未清空终端 scrollback；恢复原输入模式，shell 退格正常。macOS 的临时 `PENDIN` 标志不作为功能模式泄漏。

这些检查证明了控制序列和输入状态，不证明人眼感知的拖选延迟。没有自动测量 Orca 的鼠标指针、双击语义、剪贴板和拖到边缘自动滚动；这些由宿主终端提供，仍需实际体验。Windows 和 tmux 下也尚未实机验证。

## 为什么现有界面与终端原生选择不一样

当前主程序采用 Textual 全屏模式：

```text
终端鼠标事件 → Python/Textual → 解析文字坐标 → 计算选区 → 重绘高亮 → 终端显示
```

代码位置：

- `codex_room/terminal.py`：当前全屏驱动和键盘模式恢复。
- `codex_room/app.py`：`VerticalScroll` 聊天区、卡片、输入框和侧栏。
- `codex_room/selectable.py`：自己维护字符位置、跨消息选区、高亮和拖选滚动。

现有选区实现做过缓存和局部重画，但仍需要应用处理鼠标移动。普通终端路径可以直接去掉这一段往返：

```text
Codex 事件 → 格式化文字 → 终端历史
鼠标拖选／高亮／复制 → 由 Orca 等宿主终端处理
键盘输入 → 程序维护的 inline 输入区 → Codex
```

Python 仍处理任务、输入与输出；终端自己处理正文选择。使用 Rust 的高效自绘选区也是另一种可行方案，不能把“全屏”直接等同于“必然卡顿”。此次优先验证的是让宿主终端承担正文选择的方式。

## Codex 的实现可借鉴什么

本机版本 `codex-cli 0.163.0`；参考的本地源码 checkout 为 `a9b08151`。源码与安装包不是已证明完全一致的构建，下面分别记录代码证据和 CLI 能力。

官方文档描述了 scrollback/fullscreen 两种终端模式，以及 `--no-alt-screen` 和 `tui.alternate_screen` 配置。本机 `codex --help` 也明确说明 `--no-alt-screen` 保留终端滚动历史。[CLI 参数](https://learn.chatgpt.com/docs/cli/reference)、[终端模式说明](https://learn.chatgpt.com/docs/developer-commands)、[高级配置](https://learn.chatgpt.com/docs/config-file/config-advanced)。

因此，之前把“Codex 的选择体验”全部归结为“它一直使用终端历史”过于笼统：本地源码同时具有自己维护正文与选区的 owned 模式。

关键源码路径均相对于 Codex 的 `codex-rs/tui/src/`：

| 路径 | 借鉴点 |
| --- | --- |
| `tui.rs`，`insert_history_hyperlink_lines_with_wrap_policy` | 已稳定的历史批量提交，输入／活动区域与历史分开维护 |
| `insert_history.rs` | 在输入区上方插入历史，保存／恢复光标，处理 ANSI 样式和换行 |
| `tui/scrollback.rs` | 区分普通终端、Zellij、Windows Terminal 的滚动方式；不能假定所有宿主的部分滚动区域行为相同 |
| `transcript_mode.rs` | 明确区分 Owned 和 Terminal 两种正文所有权 |
| `tui/alternate_screen.rs` | 临时界面进入／退出时配对恢复鼠标、键盘和屏幕状态 |
| `app/owned_transcript.rs` | Codex 另一条自绘正文、滚动、选择路径，不应误称它都是终端原生选择 |

值得复用的是这些职责划分和行为，不是直接在 Python 中调用 Rust 的私有 TUI 模块。

## 小样如何实现

采用 `PromptSession`，输入界面为 `full_screen=False`，并设 `mouse_support=False`：不接管正文鼠标，不构造可滚动的消息组件树。

完整回复以 Rich Markdown 转 ANSI 后一次写出。写出时通过 `run_in_terminal` 暂时让出输入区域，完成后恢复相同 Buffer 和光标。后台任务必须显式绑定到相同输入 Application，避免直接打印覆盖正在输入的文字。

小样没有使用默认会批处理 stdout 的代理，也没有为了指针形状而发宿主不一定支持的鼠标形状覆盖序列。它依赖宿主的正常文本选择行为。

参考：[prompt_toolkit 输入、补全、多行和 asyncio 文档](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)、[asyncio 集成](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/advanced_topics/asyncio.html)。

## 为什么不是给 Textual 加 `inline=True`

本项目安装的 Textual 确实支持 `App.run(inline=True, mouse=False)`，这是一个候选方案。但它只改变界面占用终端的方式：

- 聊天记录仍在 `VerticalScroll` 内时，滚出组件视口的内容不会自动变成宿主的 scrollback。
- 仍须把稳定历史从组件树中拆出，设计在输入区上方安全输出的通道。
- 当前 inline driver 在 resize 时会重画界面；需要验证不会破坏选区或历史。
- 本地 Textual 的 inline driver 选择还受平台限制，Windows 不能照搬 POSIX 路径。

可以进一步比较“保留 Textual 做小输入区”和“prompt_toolkit 做输入区”的成本。这次小样证明后者可行，尚未选定正式框架。

## 已有功能怎么承接

| 功能 | 迁移判断 |
| --- | --- |
| 多 Codex、独立 thread、工作目录、模型、权限、steer | 后端能力可保留；先从依赖卡片和组件的控制逻辑中拆出 |
| 名字颜色、米白正文、Markdown、代码高亮、结束时间 | 可继续格式化为终端文字，无需自绘选区 |
| 补全、多行输入、历史 | 小样验证基础能力；正式版仍要承接现有参数补全与输入行为 |
| 长粘贴／图片块、原子删除、撤销 | 数据结构可复用，但编辑器交互要移植，基础 PromptSession 不会自动提供 |
| Working 动效／公开摘要 | 在活动区小范围更新；不能靠重画整个历史来做动画 |
| 多成员同时流式输出 | 统一输出队列，在段落／事件边界提交稳定内容；临时 token 尾部留在活动区。要避免 A/B token 交错和反复写入同一段 |
| 常驻可点击侧栏 | 与整屏终端历史难以保持同样交互；候选为临时成员选择器，尚未决定删除或替代 |
| `/focus` 全部／单人视图 | 原生 scrollback 不是可直接过滤的数据表；切换需要从持久记录重建显示，或临时查看器。清理范围、历史和返回位置要明确设计 |
| 点击历史回复／工具原地折叠 | 已进入 scrollback 的任意旧行不能按原组件方式安全地持续重排；候选为临时详情查看器／按消息 ID 操作，或显式重建当前视图 |
| 鼠标链接 | 普通 URL／路径检测可依赖宿主；不能假定终端超链接能任意回调应用函数 |
| 原生子代理成员 | 协议工作继续有用；当前草稿直接 mount Member、刷新卡片，接展示层前需要解耦 |

这些是待验证的交互承接方式，不是已经批准减少用户功能。

## 转发是需要专门设计的一处

现有转发不仅复制可见文本，还依据消息 ID 带上跨过的折叠正文、工具输出和 diff。它依赖程序知道选区对应哪些消息。

普通终端会在宿主内部维护选区，不通过通用 TTY API 把选区坐标／文本通知 Python。把鼠标捕获关掉后，Ctrl+F 不能凭空知道用户刚才选了什么；读取剪贴板也不能证明它就是当前选区。

可评估的方式：

1. 用户用终端复制，然后明确“转发剪贴板”，保留熟悉的原生选字；要处理来源匹配、重复文本、部分选中和缺少隐藏工具详情，不能冒充现有无损转发。
2. 提供按消息范围选择的临时转发界面，利用稳定消息 ID 保留完整详情；它与正文的原生拖选复制是两条操作路径。
3. 如果要同时保留“宿主原生拖选”和“应用立刻知道精确选区”，需要宿主集成。不能把某个 Orca 特有能力当成所有终端都有。

当前不自动改成其中一种，先验证基础阅读体验，再结合真实转发习惯定。

## 建议的迁移步骤

1. 在 Orca 体验小样，重点判断拖选是否自然、输出期间选区是否稳定、是否需要固定输入区；不必先连接模型。
2. 如果手感符合目标，再从当前 `CodexRoom` 抽出与 UI 无关的成员、消息、派发和状态事件。`CodexClient`、原 thread ID、Entry 数据、引用快照与持久状态继续保留。
3. 新增可切换的 inline 入口，先接一个真实成员的流式回复、工具与停止，再接多成员；默认入口先保留，避免一边迁移一边覆盖现有工作方式。
4. 移植长粘贴／图片与参数补全，验证重启、宽度变化、输入法和 shell 终端恢复。
5. 单独确定成员选择、全部／单人视图、工具详情和无损转发的交互。
6. 再恢复原生子代理成员功能的接入与验证，完成后按已有授权提交、推送并公开仓库。

待恢复的功能和当前草稿清单见 [下一版功能交接](next-version-handoff.md)。
