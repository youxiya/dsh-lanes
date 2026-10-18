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
