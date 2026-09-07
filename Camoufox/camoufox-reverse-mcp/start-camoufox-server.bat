@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "CAMOUFOX_EXECUTABLE_PATH=%~dp0..\camoufox.exe"
set "CAMOUFOX_BROWSER_ROOT=%~dp0.."
set "CAMOUFOX_DATA_DIR=%~dp0..\.camoufox-data"
set "CAMOUFOX_REVERSE_RUNTIME_DIR=%~dp0..\.camoufox-runtime"
python "%~dp0scripts\start_camoufox_server.py" %*
exit /b %ERRORLEVEL%
