@echo off
REM live_queue (queue assistant) standalone Bilibili danmaku bridge.
REM
REM NOTE: keep this file ASCII-only. cmd.exe reads .bat with the OEM code page,
REM so UTF-8 Chinese comments can be mis-decoded and executed as commands.
REM Chinese docs live in README.md.
REM
REM This connects to the Bilibili live open platform long connection and posts
REM danmaku queue requests to live_queue. It does NOT need the MoFox bot.
REM
REM If the bot is running, the platform room session is locked (code 7002) and
REM this bridge stays on standby, so nothing is queued twice.

setlocal
set "HERE=%~dp0"
set "VENV_PY=%HERE%..\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%HERE%bilibili_bridge.py" %*
) else (
    python "%HERE%bilibili_bridge.py" %*
)

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [live_queue bridge] exit code %RC%
    pause
)
endlocal & exit /b %RC%
