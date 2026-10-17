@echo off
rem Start the Koda chat in this window, using the project's virtual environment.
rem Double-click it, or pin a shortcut to it on the taskbar or Start menu.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Koda is not set up yet: see "Getting started" in README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m app.main %*
