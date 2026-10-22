# 终端技术选型实验

独立实验，**尚未选择或迁移主项目技术栈**。不连接真实 Codex，不访问账号，不读写 `.codex-room`。Rust 使用 Ratatui 0.30.2 + Crossterm 0.29.0；Textual 直接复用当前项目的聊天卡片、Markdown 和选区实现。

## 同一个 Orca 终端里比较

在项目根目录执行：

```sh
uv run python scripts/terminal_lab.py --lines 10000
```

菜单有四组，输入数字启动。实验中 `Ctrl+Q` 返回菜单，然后可启动下一组；输入 `q` 退出菜单。`--lines 1000` 可换成较小负载。

1. **Rust Inline**：正文进入终端历史，Orca 负责滚动、选字与复制；Ratatui 仅绘制底部输入区和正在生成的文字。
2. **Rust Fullscreen**：程序管理全部内容、滚动和选区；支持鼠标折叠、模拟转发预览。
3. **Textual**：当前项目的实际全屏渲染与自绘选区，开启鼠标捕获。
4. **Textual 关闭鼠标捕获**：同一渲染器，只关闭鼠标上报。这并不把它变成 Inline，主要用于分辨鼠标接管带来的影响。

也可以直接启动某一组：

```sh
uv run python scripts/terminal_lab.py rust-inline --lines 10000
uv run python scripts/terminal_lab.py rust-fullscreen --lines 10000
uv run python scripts/terminal_lab.py textual --lines 10000
uv run python scripts/terminal_lab.py textual-no-mouse --lines 10000
```

所有模式读取相同的历史和流式 Markdown，约每 60ms 输出 4 个字符。`1,000 / 10,000` 指 **Markdown 源文本行数**，并非渲染后的物理屏幕行数；标题、换行、表格、代码边框会影响后者。

Rust 首次启动需要 Cargo，会在本目录构建 release 可执行文件。启动器会查找 PATH 和 `~/.cargo/bin/cargo`，无需为了实验改 shell 配置。本机已安装最小 Rust 工具链；其他机器安装方式见 [Rust 官方说明](https://rust-lang.org/tools/install/)。

## 操作

通用：

- `F2` 暂停／继续模拟流式输出；命令行加 `--paused` 可先看静止历史。
- 输入框可输入、粘贴；Enter 提交测试消息，`Alt+Enter` 换行。
- Rust 小样支持中文、方向键、Home/End、退格和 Delete；它没有实现生产版的补全、图片块和完整编辑器。
- `Ctrl+Q` 退出当前组，`/quit` 同样可用。

Rust Inline：

- 用 Orca 原来的滚轮、拖选和复制操作；本程序不获取选区。
- macOS 用宿主的 `Cmd+C`／菜单复制。`Ctrl+C` 仍是程序按键，不能凭空读取终端原生选区。
- 已提交的历史不在每帧重画；正在流式生成的小区域会更新，完整段落完成后再写入历史。
- 历史可回看多远受 Orca 自身的 scrollback 容量限制；10,000 行源文本渲染后可能超过 10,000 屏幕行。

Rust Fullscreen：

- 滚轮一次移动一行，PageUp/PageDown 翻页；`Ctrl+End` 或底部「底部」回到最新。
- 鼠标拖选文字，拖到上下边缘继续滚动；选区按字符边界计算，宽中文的两个终端格映射到同一个字。
- 双击选择连续文字片段，三击选当前行；点高亮外取消选择。
- 有选区时 `Ctrl+C` 复制。macOS 调用 `pbcopy`；Linux 需 `wl-copy` 或 `xclip`，Windows 使用 `clip.exe`，后两者未实机验证。自动检查不会改写剪贴板。
- 点击消息标题前的箭头折叠／展开；`Ctrl+F` 或底部「转发选中」打开**模拟预览，不发送给任何人**，任意键或点击关闭。
- 流式输出期间拖选或滚动旧内容，程序不会自动拉回底部。

两组 Textual：

- 开启捕获时沿用现有文字选择和 `Ctrl+C` 复制；关闭捕获时尝试宿主的原生选择／复制。
- PageUp/PageDown 直接滚动聊天区，两组均可用。
- **关闭捕获后，滚轮无法再作为鼠标事件交给内部聊天滚动区。** 原生选择通常只能覆盖宿主当前可见的全屏内容；这组用于观察差异，不代表已有完整替代方案。

## 建议体验顺序

每组都先按 F2 暂停，再打开流式输出重复测试：

| 检查 | 要留意什么 |
| --- | --- |
| 从一段 query 拖到多条回复末尾 | 是否精确、是否多选字、是否中途失效 |
| 跨过代码块、中文与英文 | 高亮与复制结果是否一致 |
| 保留选区后点别处 | 取消是否立即且自然 |
| 一边输出一边看旧内容 | 是否抢滚动位置、改变旧选区 |
| 拖到窗口上下边缘 | 是否能连续选择，是否突然跳动 |
| 双击、三击与鼠标指针形状 | 与 Orca 原生操作是否一致；语言不会自动统一这些规则 |
| 拖动窗口宽度／改变字号 | 是否错位、丢历史、输入区进入历史或出现重复内容 |
| Fullscreen 折叠、转发预览 | 丰富交互是否值得保留，以及是否影响选字 |
| 退出后普通 shell 输入与退格 | 是否恢复正常 |

Fullscreen 当前在窗口 resize 后会清除旧选区，这是原型的已知限制；是否需要保留应在最终实现中另行验收。Inline 也必须在实际 Orca 中观察缩放时的历史重排；PTY 检查不能证明宿主的所有 resize 行为。

## 结构与检查

```text
scripts/terminal_lab.py            同一份测试数据、四组启动菜单、Textual 对照
experiments/terminal-lab/src/
  main.rs                         输入、模拟流式输出、模式切换点与交互
  history.rs                      Markdown／行缓存、字符坐标和选区
  input.rs                        小输入框的 Unicode 编辑
  terminal.rs                     屏幕／鼠标／raw mode 的设置与恢复
  wide_backend.rs                 insert_before 的中文宽字符兼容处理
scripts/check_terminal_lab.py      实际 PTY 与终端模拟器集成检查
```

源码、Cargo.lock 应进入仓库；`target/`、生成的数据和运行指标放在忽略目录，不提交二进制。

```sh
~/.cargo/bin/cargo test --manifest-path experiments/terminal-lab/Cargo.toml
~/.cargo/bin/cargo build --release --manifest-path experiments/terminal-lab/Cargo.toml
uv run scripts/check_terminal_lab.py
```

本机检查结果：6 项 Rust 单元检查通过；5 项集成检查通过（Textual 对照项分别启动两种鼠标模式）。覆盖宽字符／组合字符编辑与选择、旧行缓存、折叠、10,000 行终端历史保留、流式输出保留草稿、Fullscreen 拖选与转发预览、resize 和正常／panic 退出恢复。

实验确实捕获到 Ratatui 0.30.2 portable `insert_before` 路径把宽字符后面的占位格也交给 Crossterm 绘制，导致中文间多出空格。小型 backend 适配器过滤了已被宽字符覆盖的格子，并有回归检查；未修改系统或 Cargo 缓存中的第三方源码。

运行后生成的 `artifacts/terminal-lab/*-metrics.json` 只有程序内 draw 时间、帧数和行数。它们**不包含终端显示、鼠标事件传输或人的感知延迟**，不能拿来单独证明哪种体验更好。PTY 的 scrollback 检查使用足够容量的终端模拟器，不等于实际 Orca 的主观体验验收。

最终由同一个 Orca 终端中的实际比较决定：先满足自然、跟手的阅读和选字，再选择可承载更多交互的方案。此实验不预设 Inline 必胜，也不预设 Rust Fullscreen 一定更快。
