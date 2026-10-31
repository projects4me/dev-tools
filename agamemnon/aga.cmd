@echo off
rem aga: the guarded tool an Agamemnon session acts with (talos\aga.py). See talos\README.md
setlocal
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
rem The runtime copy of dev-tools (p4m runtime) runs main's tooling while this checkout is worked;
rem P4M_RUNTIME=off runs this copy.
set "DIR=%~dp0..\talos\"
if /i not "%P4M_RUNTIME%"=="off" if exist "%~dp0..\..\.dev-tools-runtime\talos\aga.py" set "DIR=%~dp0..\..\.dev-tools-runtime\talos\"
%PY% "%DIR%aga.py" %*
exit /b %ERRORLEVEL%
