@echo off
REM RMK Smart Model Router - operator CLI (read-only).
REM   rmk-router status
REM   rmk-router explain "<task>"
setlocal
set "REPO=%~dp0.."
if exist "%REPO%\venv\Scripts\python.exe" (
  set "PY=%REPO%\venv\Scripts\python.exe"
) else (
  set "PY=python"
)
pushd "%REPO%"
"%PY%" -m agent.routing %*
set "RC=%ERRORLEVEL%"
popd
exit /b %RC%
