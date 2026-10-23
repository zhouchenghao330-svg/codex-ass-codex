# Windows 使用说明

1. 从 GitHub 克隆项目到普通文件夹：`git clone https://github.com/zhouchenghao330-svg/codex-ass-codex.git`。
2. 在 PowerShell 中确认 `codex --version` 能运行，且 Codex 已登录。
3. 确认 `uv --version` 能运行；若没安装，按 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/#installation-methods) 安装。首次安装后重新打开终端。
4. 双击项目目录里的 `start.bat`，或者在此目录打开 PowerShell，运行：

```powershell
uv run codex_room
```

首次启动联网安装 Python 依赖，并从 GitHub Releases 下载 x64 或 ARM64 界面、校验 SHA-256 后缓存。**无需 Rust、Visual Studio 或 GitHub Token**。后续复用缓存，使用你自己的 Codex 登录和配置。

进入后创建成员，指定你电脑上实际存在的项目目录：

```text
/add dev D:/code/project
/add paper "D:\My Projects\paper" --color blue
```

也可以在 PowerShell 启动时指定默认目录：

```powershell
uv run codex_room --cwd "D:\My Projects\paper"
```

文件锁在 Windows 使用系统的字节范围锁；默认界面使用 Ratatui/Crossterm，旧版使用 Textual。兼容 PATH 中的 Codex 独立 `.exe` 和标准 npm `.cmd` 安装，程序会定位 npm 包内实际的 `codex.exe`。Actions 会在 Windows 原生 runner 构建、运行 Rust 单测和 `--help` 检查；完整人工交互仍需实机体验。Codex 安装方式见 [官方 Windows 说明](https://learn.chatgpt.com/docs/enterprise/windows-deployment)。

更新时先退出 Room，运行 `git pull --ff-only`，再执行 `uv run codex_room`。界面版本变化时自动下载新程序，现有聊天和成员保留。网络访问问题、缓存位置及开发者编译见 [界面发行说明](docs/ui-distribution.md)。

克隆不会携带别人的 `.venv/` 或 `.codex-room/`。若此前用过包含别人聊天的 ZIP，先退出 Room，备份并移开旧 `.codex-room`；重新启动会创建自己的房间。
