@echo off
REM live_queue (queue assistant) launcher -- just double-click it.
REM
REM NOTE: keep this file ASCII-only. cmd.exe reads .bat files using the OEM code
REM page, so UTF-8 Chinese in comments can be mis-decoded and get executed as a
REM command. All Chinese docs live in README.md instead.
REM
REM It prefers this repo's .venv (which has PySide6, so the window works) and
REM falls back to whatever "python" is on PATH when the folder is copied elsewhere.
REM
REM Useful arguments (just append them):
REM     --ui off             headless: HTTP server only, no window
REM     --port 18080         use another port
REM     --token <secret>     require a key for write operations
REM     --selftest           check config/data only, do not listen

setlocal
set "HERE=%~dp0"
set "VENV_PY=%HERE%..\.venv\Scripts\python.exe"

if exist "%VENV_PY%" (
    "%VENV_PY%" "%HERE%app.py" %*
) else (
    python "%HERE%app.py" %*
)

REM Keep the window open on failure so the error stays readable.
if errorlevel 1 pause
endlocal
