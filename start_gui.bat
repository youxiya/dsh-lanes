@echo off
rem ============================================================
rem  DSH multi-version launcher - GUI (new Web frontend)
rem  Pure ASCII + CRLF on purpose: avoids cmd.exe encoding issues.
rem
rem  This starts dsh_lanes_web.py: a tiny local HTTP server plus an
rem  Edge/Chrome --app window (no address bar, no tabs).
rem  Want the classic tkinter window instead? Use start_classic.bat
rem  (or: py dsh_lanes_web.py --classic).
rem ============================================================
title DSH Multi-Version Launcher
cd /d "%~dp0"

rem ------------------------------------------------------------
rem  Kill any leftover launcher web backend from previous runs.
rem  pythonw/pyw runs with no window, so an old server you closed
rem  the window for still lingers (holding its random port and a
rem  now-stale token) and the page can't show new lanes. Remove it
rem  before starting a fresh one.
rem  Matches the launcher script name only; it never touches your
rem  running dsh service instances (those are node.exe processes).
rem ------------------------------------------------------------
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*dsh_lanes_web.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
rem  Give the OS a moment to release the old process and its port.
timeout /t 1 /nobreak >nul

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "dsh_lanes_web.py"
    exit /b
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "dsh_lanes_web.py"
    exit /b
)

rem Fallback: run with a console so errors stay visible
py "dsh_lanes_web.py"
