@echo off
setlocal
cd /d "%~dp0"
set "RECOVERY_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
set "RECOVERY_PYTHON_ARGS="
if exist "%RECOVERY_PYTHON%" goto run
where py.exe >nul 2>nul
if not errorlevel 1 (
  set "RECOVERY_PYTHON=py.exe"
  set "RECOVERY_PYTHON_ARGS=-3"
  goto run
)
where python.exe >nul 2>nul
if not errorlevel 1 (
  set "RECOVERY_PYTHON=python.exe"
  goto run
)
echo Python 3.11 or newer was not found. Install Python before using this tool.
pause
exit /b 1

:run
"%RECOVERY_PYTHON%" %RECOVERY_PYTHON_ARGS% -X utf8 "%~dp0codex_project_recovery.py" recover
set "RECOVERY_EXIT=%errorlevel%"
echo.
if not "%RECOVERY_EXIT%"=="0" (
  echo Recovery stopped. Keep the error and all backup folders for investigation.
) else (
  echo You may close this window and reopen ChatGPT now.
)
pause
exit /b %RECOVERY_EXIT%
