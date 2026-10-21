@echo off
rem The Nestor UI (brainstorm ideas with Agent Nestor, who runs in Talos): see README.md
setlocal
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
rem The runtime copy of dev-tools (p4m runtime) runs main's tooling while this checkout is worked;
rem P4M_RUNTIME=off runs this copy.
set "DIR=%~dp0"
if /i not "%P4M_RUNTIME%"=="off" if exist "%~dp0..\..\.dev-tools-runtime\nestor\nestor.py" set "DIR=%~dp0..\..\.dev-tools-runtime\nestor\"
%PY% "%DIR%nestor.py" %*
exit /b %ERRORLEVEL%
