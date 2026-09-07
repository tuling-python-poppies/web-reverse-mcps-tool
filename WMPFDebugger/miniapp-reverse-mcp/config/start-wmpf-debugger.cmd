@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
set "INPUT_ROOT=%~1"
set "LEASE_TOKEN=%~2"
set "WMPF_SERVICE_SCRIPT=%SCRIPT_DIR%wmpf-debugger-service.ps1"

powershell -NoProfile -Command "$p=$env:WMPF_SERVICE_SCRIPT; try { $r=(Resolve-Path -LiteralPath $p).Path; if($r.StartsWith('\\')){exit 4}; $root=[IO.Path]::GetPathRoot($r); if((New-Object IO.DriveInfo($root)).DriveType -ne [IO.DriveType]::Fixed){exit 4}; $current=$root; foreach($part in $r.Substring($root.Length) -split '[\\/]') { if([string]::IsNullOrWhiteSpace($part)){continue}; $current=Join-Path $current $part; $item=Get-Item -LiteralPath $current -Force; if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){exit 4} }; if((Get-Item -LiteralPath $r -Force).PSIsContainer){exit 4}; exit 0 } catch { exit 4 }"
if errorlevel 1 exit /b 4

if defined INPUT_ROOT (
  set "WMPF_ROOT_CANDIDATE=%INPUT_ROOT%"
  powershell -NoProfile -ExecutionPolicy Bypass -File "%WMPF_SERVICE_SCRIPT%" -Action Start -UseEnvironmentRoot -LeaseToken "%LEASE_TOKEN%"
) else (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%WMPF_SERVICE_SCRIPT%" -Action Start -LeaseToken "%LEASE_TOKEN%"
)
set "SERVICE_EXIT=%ERRORLEVEL%"
set "WMPF_ROOT_CANDIDATE="
exit /b %SERVICE_EXIT%
