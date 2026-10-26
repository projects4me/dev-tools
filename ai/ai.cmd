@echo off
setlocal
rem Projects4Me workflow automation: runs ai.py from PowerShell or cmd (Git Bash uses the sh wrapper `ai`).
set "AI_DIR=%~dp0"
set "PY="
rem Never the Microsoft Store python3 stub: py -3, else python 3.8+.
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [ai] Python 3 not found. Install it from https://www.python.org/downloads/ and retry.
  exit /b 1
)
rem The runtime copy of dev-tools (p4m runtime) runs main's tooling while this checkout is worked;
rem P4M_RUNTIME=off runs this copy.
if /i not "%P4M_RUNTIME%"=="off" if exist "%~dp0..\..\.dev-tools-runtime\ai\ai.py" set "AI_DIR=%~dp0..\..\.dev-tools-runtime\ai\"
%PY% "%AI_DIR%ai.py" %*
exit /b %ERRORLEVEL%
