@echo off
REM live_queue (queue assistant) standalone launcher -- safe to double-click.
REM
REM NOTE: keep this file ASCII-only. cmd.exe reads .bat with the OEM code page,
REM so UTF-8 Chinese comments can be mis-decoded and executed as commands.
REM Chinese docs live in README.md.
REM
REM It prefers this repo .venv (which has PySide6, so the window works) and
REM falls back to whatever "python" is on PATH when the folder is copied elsewhere.
REM
REM If a live_queue is already serving on the configured port, this only opens
REM the control panel instead of starting a second instance.

setlocal
set "HERE=%~dp0"
set "VENV_PY=%HERE%..\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%HERE%start_standalone.py" %*
) else (
    python "%HERE%start_standalone.py" %*
)

set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [live_queue] exit code %RC%
    pause
)
endlocal & exit /b %RC%
