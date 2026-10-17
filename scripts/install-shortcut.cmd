@echo off
rem Add "Koda" to your Desktop and Start menu. It opens Koda in its own window;
rem closing the window stops Koda. Run again after moving this folder.
cd /d "%~dp0.."
if not exist ".venv\Scripts\pythonw.exe" (
  echo Koda is not set up yet: see "Getting started" in README.md.
  pause
  exit /b 1
)
set "KODA_ROOT=%CD%"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = $env:KODA_ROOT; $shell = New-Object -ComObject WScript.Shell;" ^
  "foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {" ^
  "  $link = $shell.CreateShortcut((Join-Path $dir 'Koda.lnk'));" ^
  "  $link.TargetPath = Join-Path $root '.venv\Scripts\pythonw.exe';" ^
  "  $link.Arguments = '-m app.web_ui --app';" ^
  "  $link.WorkingDirectory = $root;" ^
  "  $link.IconLocation = (Join-Path $root 'scripts\koda.ico') + ',0';" ^
  "  $link.Description = 'Koda, your personal AI agent';" ^
  "  $link.Save(); Write-Output ('Created ' + $link.FullName) }"
