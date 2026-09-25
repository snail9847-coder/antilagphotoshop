@echo off
setlocal
cd /d "%~dp0"
set "SCRIPT=%~dp0antilagphotoshop_diagnostics.py"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%SCRIPT%" %*
    set "CODE=%ERRORLEVEL%"
    echo.
    echo Exit code: %CODE%
    pause
    exit /b %CODE%
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%SCRIPT%" %*
    set "CODE=%ERRORLEVEL%"
    echo.
    echo Exit code: %CODE%
    pause
    exit /b %CODE%
)
echo Python 3.9 or newer was not found.
echo Install Python and add "py" or "python" to PATH.
pause
exit /b 1
