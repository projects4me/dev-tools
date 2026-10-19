@echo off
rem Talos: dispatch Projects4Me issues to headless Claude Code agents. See README.md
setlocal
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% "%~dp0talos.py" %*
exit /b %ERRORLEVEL%
