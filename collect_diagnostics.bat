@echo off
setlocal
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
cd /d "%~dp0"
set "SCRIPT=%~dp0antilagphotoshop_diagnostics.py"
where py >nul 2>nul
if not errorlevel 1 goto run_py
where python >nul 2>nul
if not errorlevel 1 goto run_python
echo Python 3.9 or newer was not found.
echo Install Python and add "py" or "python" to PATH.
pause
exit /b 1

:run_py
py -3 "%SCRIPT%" %*
goto finish

:run_python
python "%SCRIPT%" %*

:finish
set "CODE=%ERRORLEVEL%"
echo.
echo Exit code: %CODE%
pause
exit /b %CODE%
