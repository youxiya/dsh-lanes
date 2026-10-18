@echo off
rem ============================================================
rem  DSH multi-version launcher - GUI (classic tkinter window)
rem  Pure ASCII + CRLF on purpose: avoids cmd.exe encoding issues.
rem
rem  Same launcher, same lanes.json, same core functions as the new
rem  Web frontend (start_gui.bat) - this one just draws the window
rem  with tkinter. It still has the entries that have not been ported
rem  yet: create version, add profile, plugin library, credentials,
rem  plugin market, chat backup, clone, upgrade, delete.
rem ============================================================
title DSH Multi-Version Launcher (classic)
cd /d "%~dp0"

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw "dsh_lanes_gui.py"
    exit /b
)

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "dsh_lanes_gui.py"
    exit /b
)

rem Fallback: run with a console so errors stay visible
py "dsh_lanes_gui.py"
