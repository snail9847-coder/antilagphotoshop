@echo off
setlocal
cd /d "%~dp0"
where pyw >nul 2>nul
if not errorlevel 1 (
  start "antilagphotoshop" pyw -3 "%~dp0antilagphotoshop_supervisor.pyw" %*
  exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "antilagphotoshop" pythonw "%~dp0antilagphotoshop_supervisor.pyw" %*
  exit /b 0
)
echo Python 3.9 or newer is required. Install Python and add it to PATH.
pause
exit /b 1
