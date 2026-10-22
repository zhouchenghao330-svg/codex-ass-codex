# Windows ZIP 使用说明

1. 把整个 ZIP 解压到普通文件夹，不要在压缩包预览里直接运行。
2. 在 PowerShell 中确认 `codex --version` 能运行，且 Codex 已登录。
3. 确认 `uv --version` 能运行；若没安装，按 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/#installation-methods) 安装。首次安装后重新打开终端。
4. 新版界面需要 [Rust](https://rustup.rs/)；按安装器要求准备本机编译环境，并重新打开终端。如果暂时不安装 Rust，可运行 `uv run codex_room --legacy-ui` 使用旧 Textual 界面。
5. 双击解压目录里的 `start.bat`，或者在此目录打开 PowerShell，运行：

```powershell
uv run codex_room
```

首次启动会联网安装 Python 依赖并构建 Rust 界面，使用你自己的 Codex 登录和配置。后续会复用构建结果。

进入后创建成员，指定你电脑上实际存在的项目目录：

```text
/add dev D:/code/project
/add paper "D:\My Projects\paper" --color blue
```

也可以在 PowerShell 启动时指定默认目录：

```powershell
uv run codex_room --cwd "D:\My Projects\paper"
```

文件锁在 Windows 使用系统的字节范围锁；默认界面使用 Ratatui/Crossterm，旧版使用 Textual。兼容 PATH 中的 Codex 独立 `.exe` 和标准 npm `.cmd` 安装，程序会定位 npm 包内实际的 `codex.exe`。此次 Rust 界面尚未在 Windows 实机验证。Codex 安装方式见 [官方 Windows 说明](https://learn.chatgpt.com/docs/enterprise/windows-deployment)。

这是源码 ZIP，不是 macOS 安装包；macOS/Linux 仍使用同一份代码和 `uv run codex_room`。

如果 ZIP 中带有别人的 `.venv/` 或 `.codex-room/`，请重新索取干净的源码包。干净源码包不会携带虚拟环境、登录信息和聊天记录。
