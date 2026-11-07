@echo off
rem MCP-Manager installer for Windows. On Linux and macOS, run install.sh.
rem The work is in scripts\install.ps1; the options are listed at the top of it.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
set code=%errorlevel%
rem started by a double click: keep the window open long enough to read it
if "%~1"=="" pause
exit /b %code%
