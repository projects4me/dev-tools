@echo off
rem Stand-ins for Gaia idea resources and Hermes idea events: see README.md
setlocal
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% "%~dp0sim.py" %*
exit /b %ERRORLEVEL%
