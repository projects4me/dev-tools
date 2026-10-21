@echo off
rem The Nestor UI (brainstorm ideas with Agent Nestor, who runs in Talos): see README.md
setlocal
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% "%~dp0nestor.py" %*
exit /b %ERRORLEVEL%
