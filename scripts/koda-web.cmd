@echo off
rem Start the Koda web interface (127.0.0.1 only). Open the link it prints.
rem Close this window or press Ctrl+C to stop it.
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo Koda is not set up yet: see "Getting started" in README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m app.web_ui %*
