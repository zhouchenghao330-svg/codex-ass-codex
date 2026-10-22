@echo off
setlocal
pushd "%~dp0" || exit /b 1
where uv >nul 2>nul
if errorlevel 1 (
    echo uv was not found. Install uv, then reopen this window.
    echo See WINDOWS.md for setup instructions.
    pause
    popd
    exit /b 1
)
call uv run codex_room %*
set "room_exit_code=%errorlevel%"
if not "%room_exit_code%"=="0" pause
popd
exit /b %room_exit_code%
