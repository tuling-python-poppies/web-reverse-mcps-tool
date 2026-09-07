@echo off
set "CAMOUFOX_EXECUTABLE_PATH=%~dp0..\camoufox.exe"
set "CAMOUFOX_BROWSER_ROOT=%~dp0.."
set "CAMOUFOX_DATA_DIR=%~dp0..\.camoufox-data"
set "CAMOUFOX_REVERSE_RUNTIME_DIR=%~dp0..\.camoufox-runtime"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0..\.camoufox-runtime\playwright-browsers"
set "TEMP=%~dp0..\.camoufox-runtime\temp"
set "TMP=%~dp0..\.camoufox-runtime\temp"
python -m camoufox_reverse_mcp %*
