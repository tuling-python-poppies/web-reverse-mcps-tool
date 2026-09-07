param(
  [string]$Candidate,
  [switch]$UseEnvironmentCandidate,
  [switch]$Persist,
  [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

function Test-LocalPathChain {
  param([string]$Path, [switch]$RequireDirectory, [switch]$RequireFile)
  try {
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if ($resolved.StartsWith('\\')) { return $false }
    $root = [System.IO.Path]::GetPathRoot($resolved)
    if ([string]::IsNullOrWhiteSpace($root)) { return $false }
    if ((New-Object System.IO.DriveInfo($root)).DriveType -ne [System.IO.DriveType]::Fixed) {
      return $false
    }
    $current = $root
    foreach ($segment in $resolved.Substring($root.Length) -split '[\\/]') {
      if ([string]::IsNullOrWhiteSpace($segment)) { continue }
      $current = Join-Path $current $segment
      $item = Get-Item -LiteralPath $current -Force
      if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        return $false
      }
    }
    $leaf = Get-Item -LiteralPath $resolved -Force
    if ($RequireDirectory -and -not $leaf.PSIsContainer) { return $false }
    if ($RequireFile -and $leaf.PSIsContainer) { return $false }
    return $true
  } catch {
    return $false
  }
}

function Write-JsonAtomically {
  param([string]$Path, $Value)
  $tempPath = "$Path.$PID.tmp"
  $stream = $null
  $writer = $null
  try {
    $stream = [System.IO.File]::Open(
      $tempPath,
      [System.IO.FileMode]::CreateNew,
      [System.IO.FileAccess]::Write,
      [System.IO.FileShare]::None
    )
    $writer = New-Object System.IO.StreamWriter(
      $stream,
      (New-Object System.Text.UTF8Encoding($true))
    )
    $writer.Write(($Value | ConvertTo-Json))
    $writer.Flush()
    $stream.Flush($true)
  } finally {
    if ($null -ne $writer) {
      $writer.Dispose()
    } elseif ($null -ne $stream) {
      $stream.Dispose()
    }
  }
  $backupPath = $null
  $replaceSucceeded = $false
  try {
    if ([System.IO.File]::Exists($Path)) {
      $backupPath = "$Path.$PID.$([Guid]::NewGuid().ToString('N')).bak"
      [System.IO.File]::Replace($tempPath, $Path, $backupPath, $true)
    } else {
      [System.IO.File]::Move($tempPath, $Path)
    }
    $replaceSucceeded = $true
  } catch {
    $replaceError = $_.Exception.Message
    if (-not [System.IO.File]::Exists($Path) -and
        $null -ne $backupPath -and
        [System.IO.File]::Exists($backupPath)) {
      try {
        [System.IO.File]::Move($backupPath, $Path)
      } catch {}
    }
    $destinationPresent = [System.IO.File]::Exists($Path)
    $backupPresent = $null -ne $backupPath -and [System.IO.File]::Exists($backupPath)
    $replacementPresent = [System.IO.File]::Exists($tempPath)
    throw "Atomic JSON replacement failed. destinationPresent=$destinationPresent; backupPresent=$backupPresent; replacementPresent=$replacementPresent; backup=$backupPath; replacement=$tempPath. Error: $replaceError"
  } finally {
    if ($replaceSucceeded) {
      Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
    }
    if ($replaceSucceeded -and $null -ne $backupPath) {
      Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    }
  }
}

function Resolve-MiniappMcpRoot {
  try {
    if (-not (Test-LocalPathChain $PSScriptRoot -RequireDirectory)) {
      return $null
    }
    $configDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
    $root = Split-Path -Parent $configDirectory
    if (-not (Test-LocalPathChain $root -RequireDirectory)) { return $null }
    $pyprojectPath = Join-Path $root 'pyproject.toml'
    $launcherPath = Join-Path $root 'run_mcp_server.py'
    $serverPath = Join-Path $root 'src\miniapp_cdp\server.py'
    if (-not (Test-LocalPathChain $pyprojectPath -RequireFile) -or
        -not (Test-LocalPathChain $launcherPath -RequireFile) -or
        -not (Test-LocalPathChain $serverPath -RequireFile)) {
      return $null
    }
    $pyproject = Get-Content -LiteralPath $pyprojectPath -Raw
    $launcher = Get-Content -LiteralPath $launcherPath -Raw
    if ($pyproject -notmatch '(?m)^\s*name\s*=\s*"miniapp-reverse-mcp"\s*$' -or
        $launcher -notmatch 'from\s+miniapp_cdp\.server\s+import\s+main') {
      return $null
    }
    return $root
  } catch {
    return $null
  }
}

$mcpRoot = Resolve-MiniappMcpRoot
if ($null -eq $mcpRoot) {
  [Console]::Error.WriteLine('The lifecycle scripts must run from a verified miniapp-reverse-mcp\config directory.')
  exit 4
}
$configDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$configPath = Join-Path $configDirectory 'wmpf-root.json'

function Resolve-WmpfRoot {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return $null }

  try {
    $resolved = (Resolve-Path -LiteralPath $Path).Path
    if (-not (Test-LocalPathChain $resolved -RequireDirectory)) { return $null }
    $packagePath = Join-Path $resolved 'package.json'
    $sourcePath = Join-Path $resolved 'src\index.ts'
    $tsNodePath = Join-Path $resolved 'node_modules\.bin\ts-node.cmd'
    if (-not (Test-LocalPathChain $packagePath -RequireFile) -or
        -not (Test-LocalPathChain $sourcePath -RequireFile) -or
        -not (Test-LocalPathChain $tsNodePath -RequireFile)) {
      return $null
    }

    $package = Get-Content -LiteralPath $packagePath -Raw | ConvertFrom-Json
    $main = [string]$package.main -replace '\\', '/'
    $dependencyNames = @($package.dependencies.PSObject.Properties.Name)
    $devDependencyNames = @($package.devDependencies.PSObject.Properties.Name)
    if ([string]$package.name -ine 'WMPFDebugger' -or
        $main -ne 'src/index.ts' -or
        'frida' -notin $dependencyNames -or
        'protobufjs' -notin $dependencyNames -or
        'ws' -notin $dependencyNames -or
        'ts-node' -notin $devDependencyNames) {
      return $null
    }

    $source = Get-Content -LiteralPath $sourcePath -Raw
    if ($source -notmatch '\bparse_cli_options\b' -or
        $source -notmatch '\bWebSocketServer\b') {
      return $null
    }

    return $resolved
  } catch {
    return $null
  }
}

if ($UseEnvironmentCandidate) {
  $Candidate = $env:WMPF_ROOT_CANDIDATE
}

$resolvedRoot = $null
if (-not [string]::IsNullOrWhiteSpace($Candidate)) {
  $resolvedRoot = Resolve-WmpfRoot $Candidate
  if ($null -eq $resolvedRoot) {
    [Console]::Error.WriteLine('The supplied path is not a valid WMPFDebugger root.')
    exit 2
  }
} else {
  if (Test-Path -LiteralPath $configPath) {
    try {
      $configItem = Get-Item -LiteralPath $configPath -Force
    } catch {
      [Console]::Error.WriteLine("Persisted WMPFDebugger root configuration is unreadable: $configPath")
      exit 4
    }
    if ($configItem.PSIsContainer -or
        (($configItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
      [Console]::Error.WriteLine("Persisted WMPFDebugger root configuration is not a regular file: $configPath")
      exit 4
    }
    try {
      $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
      $resolvedRoot = Resolve-WmpfRoot $config.wmpfDebuggerRoot
    } catch {
      [Console]::Error.WriteLine("Persisted WMPFDebugger root configuration is unreadable: $configPath")
      exit 4
    }
    if ($null -eq $resolvedRoot) {
      [Console]::Error.WriteLine("Persisted WMPFDebugger root configuration is invalid: $configPath")
      exit 4
    }
  }

  if ($null -eq $resolvedRoot) {
    $cwd = (Get-Location).Path
    $parent = Split-Path -Parent $cwd
    $candidates = @($cwd, $parent)
    if (-not [string]::IsNullOrWhiteSpace($parent)) {
      $candidates += Join-Path $parent 'WMPFDebugger'
    }

    $seen = @{}
    $validRoots = @{}
    foreach ($path in $candidates) {
      if ([string]::IsNullOrWhiteSpace($path) -or $seen.ContainsKey($path)) { continue }
      $seen[$path] = $true
      $matchedRoot = Resolve-WmpfRoot $path
      if ($null -ne $matchedRoot) {
        $validRoots[$matchedRoot.ToLowerInvariant()] = $matchedRoot
      }
    }
    $matches = @($validRoots.Values)
    if ($matches.Count -gt 1) {
      [Console]::Error.WriteLine("Multiple valid WMPFDebugger roots were discovered: $($matches -join ', ')")
      exit 5
    }
    if ($matches.Count -eq 1) {
      $resolvedRoot = [string]$matches[0]
    }
  }
}

if ($null -eq $resolvedRoot) {
  exit 3
}

if ($Persist) {
  if (Test-Path -LiteralPath $configPath) {
    $existingConfig = Get-Item -LiteralPath $configPath -Force
    if ($existingConfig.PSIsContainer -or
        (($existingConfig.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
      [Console]::Error.WriteLine("Refusing to replace a non-regular WMPFDebugger root configuration: $configPath")
      exit 4
    }
  }
  Write-JsonAtomically $configPath ([ordered]@{ wmpfDebuggerRoot = $resolvedRoot })
}

if (-not $Quiet) {
  Write-Output $resolvedRoot
}
exit 0
