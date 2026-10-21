@echo off
rem Stand-ins for Gaia idea resources and Hermes idea events: see README.md
setlocal
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
rem The runtime copy of dev-tools (p4m runtime) runs main's tooling while this checkout is worked;
rem P4M_RUNTIME=off runs this copy.
set "DIR=%~dp0"
if /i not "%P4M_RUNTIME%"=="off" if exist "%~dp0..\..\.dev-tools-runtime\sim\sim.py" set "DIR=%~dp0..\..\.dev-tools-runtime\sim\"
%PY% "%DIR%sim.py" %*
exit /b %ERRORLEVEL%
