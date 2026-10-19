@echo off
setlocal
rem Projects4Me CLI bootstrap: finds (or installs) Python 3, then runs p4m.py.
set "P4M_DIR=%~dp0"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [p4m] Python 3 not found - installing with winget...
  winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
  if errorlevel 1 (
    echo [p4m] Could not install Python automatically. Install it from https://www.python.org/downloads/ and retry.
    exit /b 1
  )
  echo [p4m] Python installed. Open a NEW terminal and run the command again.
  exit /b 0
)
%PY% "%P4M_DIR%p4m.py" %*
exit /b %ERRORLEVEL%
