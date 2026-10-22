# Rust Fullscreen 迁移

用户在同一 Orca 终端里实测后选择 Ratatui Fullscreen：Inline 最跟手，Rust Fullscreen 接近，两个 Textual 方案仍明显不顺。正式方向确定为 Fullscreen，Inline 保留为实验基线。

## 代码边界

- `terminal-ui/`：Rust + Ratatui。输入编辑、Markdown 排版、工具展开、成员视图、拖选、滚动、转发草稿均在此进程处理。
- `codex_room/backend.py`：无界面的 Python 服务，复用经过验证的 `runtime.py` 与 `storage.py`，负责 Codex RPC、steer、取消、交互请求和 v2 存储。正常运行不加载 Textual。
- `models.py`、`quotes.py`：新旧界面共享的消息与引用结构，不依赖界面框架。
- `peer_router.py`、`peer_tools.py`：独立成员公开通信的协议与路由。新会话使用动态工具，已有会话由 `peer_mcp.py` 临时适配，保持原 thread。
- `launcher.py`：构建并启动 Rust；`--legacy-ui` 显式启动旧 Textual。

前后端通过子进程 stdio 的 JSON 行通信。UI 命令与 Codex RPC 是两条独立通道。鼠标事件、选区更新和输入编辑不会跨进程等待 Python。接收消息通过独立读线程进入 Rust，写消息也不会阻塞绘制循环。后端以约 33ms 合并消息补丁；状态定期原子保存，完成和退出时落盘。旧会话公开消息适配器另使用带进程随机令牌的本机回环端口，退出关闭，不对外网监听。

主界面使用 Rust，**项目没有全部改写为纯 Rust**。保留 Python 通信层减少了同时替换两个状态机的风险。原生子代理现通过该服务接入新界面，父子关系、历史恢复、继续讨论和生命周期边界见 [功能交接](next-version-handoff.md)。

## 体验与性能

稳定历史按消息缓存 Markdown 和排版，流式更新只重排受影响的消息。实际绘制只遍历可见行；拖选和滚动不解析 Markdown、不复制全段历史。工具组标题只切换摘要展开与收起，全部详情独立控制；展开时固定当前位置。

拖选范围只属于聊天正文。拖出上下边缘会自动滚动，起点保持在原行；双击连续片段，三击行，点击高亮外取消。保留选区时暂缓合入会改变正文行位置的更新，后台任务继续；取消选区后应用缓存的新输出。窗口宽度变化会重新排版并清除旧选区。

输入按 grapheme 编辑；图片和长粘贴为可撤销的原子块。Rust 内部使用 UTF-8 字节偏移，存储与 Python 通信时转换成原来的 Unicode 字符偏移。图片粘贴在本地同步识别，保证“粘贴后立刻 Enter”的顺序，不等后端往返。

转发保留选区边界正文与跨过的折叠详情，不自动摘要。切换接收者保留草稿，返回来源恢复原草稿并滚至最新。视图请求带递增版本，后台旧状态不会把刚选择的成员切回去。

## 兼容与边界

沿用 `.codex-room/state.json` v2、成员的原始 `thread_id`、名称、颜色、路径、历史、附件和计时。旧的未完成请求标记停止，不自动重发。新界面还会保存各视图草稿；旧 Textual 可以读取主要会话数据，但不负责新 UI 的草稿交互。保存时保留已存在的未知字段。

真实模式持有原有房间锁。删除成员先停止任务，再归档记录，不删除工作目录或 Codex 原生历史。退出先恢复终端，再等待后端取消；无法确认时关闭 app-server。Unix 全屏界面启用最小增强键盘标志，接收 macOS ⌘C 等修饰键；不启用全键转义和按键释放上报。正常退出和 panic 都先弹出键盘模式，再离开全屏，恢复普通输入与退格。Windows 保留原生键盘输入路径。

⌘C、Ctrl+C、Ctrl+Shift+C 和底部「复制」读取同一个当前选区。⌘C 没有选区时提示，不退出或插入字母 c；普通 Ctrl+C 无选区时仍退出。宿主若把 ⌘C 截作自身复制操作，程序无法取得该按键，可使用明确的「复制」入口。

当前创建成员由输入区的 `/add` 完成，按钮和 Ctrl+N 会预填命令；目录和颜色仍可补全。鼠标指针样式由宿主终端决定，未承诺与 Orca 原生选字完全相同。Shift+Enter 依赖终端是否单独上报，Ctrl+J 始终可换行。窄于 65 列时不绘制侧栏，可以通过 @ 补全或 `/focus` 切换。

原生子代理已在迁移后接入；V2 child 的直接输入限制、真实停止与完整历史合流见 [交接文档](next-version-handoff.md)。独立成员公开通信不依赖子代理，见 [公开通信说明](public-messages.md)。

## 验证

```sh
cargo test --locked --manifest-path terminal-ui/Cargo.toml
cargo build --release --locked --manifest-path terminal-ui/Cargo.toml
uv run python -m unittest discover -s tests -v
uv run scripts/check_rust_ui.py
```

Rust 单测覆盖 Unicode/原子块、选区边界、折叠详情、多人状态、过期视图状态、发送确认以及一万条历史的缓存。PTY 检查把真实 Rust UI 接到本地假 app-server，验证工具展开、输入历史、恢复、转发、返回最新位置、拖选自动滚动、缩放和退出后的退格恢复。不操作真实剪贴板或用户房间。

以下检查会调用真实模型两次，在临时目录创建独立房间：

```sh
uv run scripts/check_rust_ui_live.py
```

比较实验仍可用 `uv run python scripts/terminal_lab.py --lines 10000`。机器内绘制或缓存检查不能代替 Orca 中主观拖选体验，后续以真实使用反馈继续优化。
