# 预编译界面与发布

普通使用者安装 uv 与 Codex CLI，使用自己的 Codex 登录，在克隆目录运行 `uv run codex_room`。不需要 Rust 或 GitHub Token。

## 下载和缓存

`codex_room/ui_release.py` 的 `UI_VERSION` 固定选择一个 Release，例如 `0.2.3` 对应 `v0.2.3`，不使用 `latest`。按当前 Python 进程的系统和架构匹配程序：

| 系统 | x64 | ARM64 |
|---|---|---|
| macOS | x86_64-apple-darwin | aarch64-apple-darwin |
| Windows | x86_64-pc-windows-msvc | aarch64-pc-windows-msvc |
| Linux | x86_64-unknown-linux-musl | aarch64-unknown-linux-musl |

匿名取得 `SHA256SUMS` 和 `codex-room-ui-目标架构`（Windows 加 `.exe`），下载到临时文件，校验后原子安装。系统文件锁防止多个房间同时安装；进程退出后锁自动释放。每次启动根据缓存校验清单重新核对程序，缓存损坏时重新下载，不运行校验失败的下载结果。清单与程序来自同一 GitHub Release，SHA-256 用于检测损坏，不是独立的签名信任机制。

缓存按版本与目标架构隔离：

- macOS：`~/Library/Caches/codex-room/ui/版本/目标架构/`
- Windows：`%LOCALAPPDATA%/codex-room/ui/版本/目标架构/`
- Linux：`$XDG_CACHE_HOME/codex-room/ui/版本/目标架构/`，默认 `~/.cache/`
- `CODEX_ROOM_CACHE_DIR` 可指定缓存根目录。

同一版本有已校验缓存后，不再联网获取界面；Codex 自身仍需其服务连接。更新代码后，只有指定界面版本变化才下载新程序，房间数据不在缓存中。

首次需要访问 GitHub Releases；遵循标准代理环境设置，例如 `HTTPS_PROXY`。失败会显示地址和缓存目录，重新运行可重试；不会自动安装 Rust、编译或退回其他版本。暂时无法下载可显式用 `--legacy-ui`。`CODEX_ROOM_BINARY` 可指定自己准备的程序，此开发覆盖绕过自动下载与校验。

## 本地开发

修改 Rust 源码后运行 `uv run codex_room --build-ui`，需要 Rust 1.88+ 及对应平台编译工具。也可设置 `CODEX_ROOM_BUILD_UI=1`。源码时间变化才重建；普通启动始终使用指定 Release。`CODEX_ROOM_BINARY` 优先级高于本地构建。

新版 Python 协议配套新版 UI 时，维护者必须先发布对应版本，不能让普通使用者自动跟随未发布的 Rust 源码。

## 发布新版本

1. 同步修改 `ui_release.py` 的 `UI_VERSION`、`pyproject.toml`、`uv.lock`、`terminal-ui/Cargo.toml` 和 `Cargo.lock` 中当前项目的版本。
2. 新建 `docs/releases/v版本.md`，运行 Python/Rust 检查并提交。
3. 给提交创建 `v版本` tag，先仅推送 tag，触发 `.github/workflows/release-ui.yml`。它验证版本和 Python 后端，在六个平台分别执行启动器检查、Rust 单测和 Release 构建。
4. 全部成功后汇集六个程序，核对资产集合、生成 `SHA256SUMS`，上传为 draft，完成后才公开发布。不含本地房间、聊天、登录配置或虚拟环境。
5. 在该 tag 的源码目录使用空缓存运行 `uv run codex_room --help`，再断网重复验证缓存。完整 UI 可用 `--demo` 在 PTY 验证，不操作真实房间。验证后再推送 main，让普通用户拉到新版本时，对应成品已经可下载。

Release notes 按 tag 自动选择。已公开的版本不覆盖程序；更改时增加版本并创建新 tag。平台失败不会发布缺件版本。上传失败后，先检查并清理同一 tag 未完成的 draft 再重跑，不覆盖已公开资产。

Runner 标签参考 [GitHub 官方 runner 列表](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)，Actions 引用固定 commit。各平台构建及帮助检查不等同完整人工交互验收。
