param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('Start', 'Stop', 'Status')]
  [string]$Action,
  [string]$Root,
  [switch]$UseEnvironmentRoot,
  [string]$LeaseToken
)

$ErrorActionPreference = 'Stop'
$managedPorts = @(9421, 62000)
$stateDirectory = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$statePath = Join-Path $stateDirectory 'wmpf-debugger-state.json'

function Write-Failure {
  param([string]$Message, [int]$Code)
  [Console]::Error.WriteLine($Message)
  exit $Code
}

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
    $writer.Write(($Value | ConvertTo-Json -Depth 4))
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

function Test-WmpfRootFingerprint {
  param([string]$Root)
  try {
    if (-not (Test-LocalPathChain $Root -RequireDirectory)) { return $false }
    $packagePath = Join-Path $Root 'package.json'
    $sourcePath = Join-Path $Root 'src\index.ts'
    $tsNodePath = Join-Path $Root 'node_modules\.bin\ts-node.cmd'
    if (-not (Test-LocalPathChain $packagePath -RequireFile) -or
        -not (Test-LocalPathChain $sourcePath -RequireFile) -or
        -not (Test-LocalPathChain $tsNodePath -RequireFile)) {
      return $false
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
      return $false
    }
    $source = Get-Content -LiteralPath $sourcePath -Raw
    return $source -match '\bparse_cli_options\b' -and $source -match '\bWebSocketServer\b'
  } catch {
    return $false
  }
}

function Test-ManagedStateSchema {
  param($State)
  if ($null -eq $State -or
      ($State.version -isnot [int] -and $State.version -isnot [long]) -or
      [long]$State.version -ne 2) {
    return $false
  }
  if ($State.root -isnot [string] -or [string]::IsNullOrWhiteSpace($State.root)) { return $false }
  try {
    $resolvedRoot = (Resolve-Path -LiteralPath ([string]$State.root)).Path.TrimEnd('\')
  } catch {
    return $false
  }
  if (-not [StringComparer]::OrdinalIgnoreCase.Equals(
      ([string]$State.root).TrimEnd('\'),
      $resolvedRoot
    ) -or -not (Test-WmpfRootFingerprint $resolvedRoot)) {
    return $false
  }
  if (($State.launcherPid -isnot [int] -and $State.launcherPid -isnot [long]) -or
      [long]$State.launcherPid -le 0 -or
      ($State.launcherStartUtcTicks -isnot [int] -and $State.launcherStartUtcTicks -isnot [long]) -or
      [long]$State.launcherStartUtcTicks -le 0) {
    return $false
  }
  $created = [DateTimeOffset]::MinValue
  if ($State.createdUtc -isnot [string] -or
      -not [DateTimeOffset]::TryParse([string]$State.createdUtc, [ref]$created)) {
    return $false
  }
  $leases = @($State.leases)
  if ($leases.Count -eq 0) { return $false }
  $seenLeases = @{}
  foreach ($lease in $leases) {
    if ($lease -isnot [string]) { return $false }
    $parsed = [Guid]::Empty
    if (-not [Guid]::TryParse([string]$lease, [ref]$parsed)) { return $false }
    $key = $parsed.ToString('D')
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals([string]$lease, $key)) { return $false }
    if ($seenLeases.ContainsKey($key)) { return $false }
    $seenLeases[$key] = $true
  }
  return $true
}

function Test-MiniappMcpRoot {
  param([string]$Root)
  try {
    if (-not (Test-LocalPathChain $Root -RequireDirectory)) { return $false }
    $pyprojectPath = Join-Path $Root 'pyproject.toml'
    $launcherPath = Join-Path $Root 'run_mcp_server.py'
    $serverPath = Join-Path $Root 'src\miniapp_cdp\server.py'
    if (-not (Test-LocalPathChain $pyprojectPath -RequireFile) -or
        -not (Test-LocalPathChain $launcherPath -RequireFile) -or
        -not (Test-LocalPathChain $serverPath -RequireFile)) {
      return $false
    }
    $pyproject = Get-Content -LiteralPath $pyprojectPath -Raw
    $launcher = Get-Content -LiteralPath $launcherPath -Raw
    return $pyproject -match '(?m)^\s*name\s*=\s*"miniapp-reverse-mcp"\s*$' -and
      $launcher -match 'from\s+miniapp_cdp\.server\s+import\s+main'
  } catch {
    return $false
  }
}

if (-not (Test-LocalPathChain $PSScriptRoot -RequireDirectory)) {
  Write-Failure 'The lifecycle scripts require a local, non-reparse miniapp-reverse-mcp\config directory.' 4
}
$mcpRoot = Split-Path -Parent $stateDirectory
if (-not (Test-MiniappMcpRoot $mcpRoot)) {
  Write-Failure 'The lifecycle scripts are not inside a verified miniapp-reverse-mcp project.' 4
}

function Get-ManagedState {
  if (-not (Test-Path -LiteralPath $statePath)) { return $null }
  try {
    $stateItem = Get-Item -LiteralPath $statePath -Force
    if ($stateItem.PSIsContainer -or
        (($stateItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
      Write-Failure "Managed WMPFDebugger state is not a regular file: $statePath" 4
    }
    $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if (-not (Test-ManagedStateSchema $state)) {
      Write-Failure "Managed WMPFDebugger state has an invalid schema or root fingerprint: $statePath" 4
    }
    return $state
  } catch {
    Write-Failure "Managed WMPFDebugger state is unreadable: $statePath" 4
  }
}

function Remove-ManagedState {
  if (-not (Test-Path -LiteralPath $statePath)) { return }
  $stateItem = Get-Item -LiteralPath $statePath -Force
  if ($stateItem.PSIsContainer -or
      (($stateItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
    throw "Refusing to remove non-regular WMPFDebugger state: $statePath"
  }
  Remove-Item -LiteralPath $statePath -Force -ErrorAction Stop
  if (Test-Path -LiteralPath $statePath) {
    throw "WMPFDebugger state cleanup did not remove: $statePath"
  }
}

function Test-ProcessIdentity {
  param($State)
  if ($null -eq $State -or $null -eq $State.launcherPid -or
      $null -eq $State.launcherStartUtcTicks) {
    return $false
  }

  try {
    $process = Get-Process -Id ([int]$State.launcherPid)
    $actualTicks = $process.StartTime.ToUniversalTime().Ticks
    if ($actualTicks -ne [long]$State.launcherStartUtcTicks) { return $false }
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$State.launcherPid)" -ErrorAction Stop
    if ($null -eq $cim) { return $false }
    $executablePath = [string]$cim.ExecutablePath
    if ([string]::IsNullOrWhiteSpace($executablePath) -or
        -not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
      return $false
    }
    $name = [System.IO.Path]::GetFileName($executablePath)
    if ($name -ine 'powershell.exe' -and $name -ine 'pwsh.exe' -and $name -ine 'node.exe') {
      return $false
    }
    $commandLine = [string]$cim.CommandLine
    $rootPath = (Resolve-Path -LiteralPath ([string]$State.root)).Path.TrimEnd('\')
    $rootNeedles = @("'$rootPath'", "`"$rootPath`"", "$rootPath\", "$rootPath/")
    $hasRootBoundary = $false
    foreach ($needle in $rootNeedles) {
      if ($commandLine.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        $hasRootBoundary = $true
        break
      }
    }
    return $hasRootBoundary -and
      ($commandLine -match 'ts-node' -or $commandLine -match 'src[\\/]index\.ts')
  } catch {
    return $false
  }
}

function Get-RecordedProcessPresence {
  param($State)
  try {
    $process = [System.Diagnostics.Process]::GetProcessById([int]$State.launcherPid)
    $actualTicks = $process.StartTime.ToUniversalTime().Ticks
    if ($actualTicks -eq [long]$State.launcherStartUtcTicks) {
      return 'same'
    }
    return 'reused'
  } catch [System.ArgumentException] {
    return 'missing'
  } catch {
    return 'unknown'
  }
}

function Get-ProcessStartTicks {
  param([int]$ProcessId)
  try {
    return (Get-Process -Id $ProcessId).StartTime.ToUniversalTime().Ticks
  } catch {
    return $null
  }
}

function Get-CimProcessStartTicks {
  param($Process)
  try {
    if ($Process.CreationDate -is [DateTime]) {
      return $Process.CreationDate.ToUniversalTime().Ticks
    }
    return [System.Management.ManagementDateTimeConverter]::ToDateTime(
      [string]$Process.CreationDate
    ).ToUniversalTime().Ticks
  } catch {
    return $null
  }
}

function Test-SameProcessStartTicks {
  param([long]$Left, [long]$Right)
  return [Math]::Abs($Left - $Right) -le [TimeSpan]::TicksPerMillisecond
}

function Get-ListenerProcessIds {
  param([int]$Port)
  $listeners = @()
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
      $listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
      break
    } catch {
      if ($attempt -ge 3) {
        Write-Failure "Unable to enumerate TCP listeners while checking port $Port after $attempt attempts: $($_.Exception.Message)" 4
      }
      Start-Sleep -Milliseconds 200
    }
  }
  return @(
    $listeners |
      Where-Object { [int]$_.LocalPort -eq $Port } |
      ForEach-Object { [int]$_.OwningProcess } |
      Where-Object { $_ -gt 0 } |
      Sort-Object -Unique
  )
}

function Test-IsDescendantProcess {
  param(
    [int]$ProcessId,
    [int]$AncestorProcessId,
    [long]$AncestorStartUtcTicks
  )
  $currentId = $ProcessId
  $currentTicks = Get-ProcessStartTicks $currentId
  if ($null -eq $currentTicks -or $currentTicks -lt $AncestorStartUtcTicks) { return $false }
  $seen = @{}
  for ($depth = 0; $depth -lt 32; $depth++) {
    if ($currentId -eq $AncestorProcessId) {
      return $currentTicks -eq $AncestorStartUtcTicks
    }
    if ($currentId -le 0 -or $seen.ContainsKey($currentId)) { return $false }
    $seen[$currentId] = $true
    $process = $null
    for ($attempt = 1; $attempt -le 2; $attempt++) {
      try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $currentId" -ErrorAction SilentlyContinue
        break
      } catch {
        if ($attempt -ge 2) { return $false }
        Start-Sleep -Milliseconds 150
      }
    }
    if ($null -eq $process) { return $false }
    $parentId = [int]$process.ParentProcessId
    $parentTicks = Get-ProcessStartTicks $parentId
    if ($null -eq $parentTicks -or $parentTicks -gt $currentTicks) { return $false }
    $currentId = $parentId
    $currentTicks = $parentTicks
  }
  return $false
}

function Get-AllListenerProcessIds {
  $ids = foreach ($port in $managedPorts) {
    Get-ListenerProcessIds $port
  }
  return @($ids | Sort-Object -Unique)
}

function Get-UnmanagedListenerProcessIds {
  param($State)
  if ($null -eq $State -or $null -eq $State.launcherPid) {
    return @(Get-AllListenerProcessIds)
  }

  $ancestorId = [int]$State.launcherPid
  $ancestorTicks = [long]$State.launcherStartUtcTicks
  return @(
    Get-AllListenerProcessIds |
      Where-Object { -not (Test-IsDescendantProcess $_ $ancestorId $ancestorTicks) }
  )
}

function Test-WmpfNodeIdentity {
  param([int]$ProcessId, $State)
  try {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId"
    if ($null -eq $process -or [string]$process.Name -ine 'node.exe') { return $false }
    $commandLine = [string]$process.CommandLine
    $rootPrefix = ([string]$State.root).TrimEnd('\') + '\'
    return $commandLine.IndexOf($rootPrefix, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
      ($commandLine -match 'ts-node' -or $commandLine -match 'src[\\/]index\.ts')
  } catch {
    return $false
  }
}

function Get-CimProcessSnapshot {
  for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
      return @(Get-CimInstance Win32_Process -ErrorAction Stop)
    } catch {
      if ($attempt -ge 3) {
        throw "Unable to enumerate processes via CIM after $attempt attempts: $($_.Exception.Message)"
      }
      Start-Sleep -Milliseconds 200
    }
  }
}

function Get-ManagedDescendantIdentities {
  param($State, [object[]]$KnownIdentities = @())
  $snapshot = @(Get-CimProcessSnapshot)
  $childrenByParent = @{}
  foreach ($process in $snapshot) {
    $parentKey = [string][int]$process.ParentProcessId
    if (-not $childrenByParent.ContainsKey($parentKey)) {
      $childrenByParent[$parentKey] = @()
    }
    $childrenByParent[$parentKey] += $process
  }

  $queue = New-Object System.Collections.Queue
  $queue.Enqueue([pscustomobject]@{
    Id = [int]$State.launcherPid
    StartTicks = [long]$State.launcherStartUtcTicks
    Depth = 0
  })
  $seen = @{}
  $seen[[string][int]$State.launcherPid] = $true
  foreach ($identity in $KnownIdentities) {
    $identityKey = [string][int]$identity.Id
    if ($seen.ContainsKey($identityKey)) { continue }
    $seen[$identityKey] = $true
    $queue.Enqueue($identity)
  }
  $result = @()

  while ($queue.Count -gt 0) {
    $parent = $queue.Dequeue()
    $liveParentTicks = Get-ProcessStartTicks $parent.Id
    if ($null -ne $liveParentTicks -and $liveParentTicks -ne [long]$parent.StartTicks) {
      continue
    }
    $parentKey = [string]$parent.Id
    if (-not $childrenByParent.ContainsKey($parentKey)) { continue }
    foreach ($child in $childrenByParent[$parentKey]) {
      $childId = [int]$child.ProcessId
      $childKey = [string]$childId
      if ($childId -le 0 -or $seen.ContainsKey($childKey)) { continue }
      $snapshotTicks = Get-CimProcessStartTicks $child
      $childTicks = Get-ProcessStartTicks $childId
      if ($null -eq $snapshotTicks -or $null -eq $childTicks -or
          -not (Test-SameProcessStartTicks $snapshotTicks $childTicks) -or
          $childTicks -lt $parent.StartTicks) {
        continue
      }
      $identity = [pscustomobject]@{
        Id = $childId
        StartTicks = [long]$childTicks
        Depth = $parent.Depth + 1
      }
      $seen[$childKey] = $true
      $result += $identity
      $queue.Enqueue($identity)
    }
  }
  return @($result)
}

function Stop-ExactProcess {
  param($Identity)
  $process = Get-Process -Id ([int]$Identity.Id) -ErrorAction SilentlyContinue
  if ($null -eq $process) { return $true }
  $startTicks = $null
  try {
    $startTicks = $process.StartTime.ToUniversalTime().Ticks
  } catch {
    $stillAlive = Get-Process -Id ([int]$Identity.Id) -ErrorAction SilentlyContinue
    return ($null -eq $stillAlive)
  }
  if ($null -eq $startTicks -or $startTicks -ne [long]$Identity.StartTicks) {
    return $false
  }
  Stop-Process -InputObject $process -Force -ErrorAction SilentlyContinue
  try {
    $process.WaitForExit(2000) | Out-Null
  } catch {}
  try {
    return $process.HasExited
  } catch {
    return $false
  }
}

function Stop-ManagedProcessTree {
  param($State)
  $launcher = [pscustomobject]@{
    Id = [int]$State.launcherPid
    StartTicks = [long]$State.launcherStartUtcTicks
    Depth = 0
  }
  $known = @()
  $refused = @()
  for ($pass = 0; $pass -lt 5; $pass++) {
    $discovered = @(Get-ManagedDescendantIdentities $State $known)
    $identityMap = @{}
    foreach ($identity in @($known + $discovered)) {
      $identityMap["$($identity.Id):$($identity.StartTicks)"] = $identity
    }
    $known = @($identityMap.Values)
    $active = @(
      $known | Where-Object {
        (Get-ProcessStartTicks $_.Id) -eq [long]$_.StartTicks
      }
    )
    if ($active.Count -eq 0) { break }
    foreach ($identity in @($active | Sort-Object Depth -Descending)) {
      if (-not (Stop-ExactProcess $identity)) {
        $refused += [int]$identity.Id
      }
    }
    if ($refused.Count -gt 0) { return @($refused | Sort-Object -Unique) }
    Start-Sleep -Milliseconds 100
  }

  $stillActive = @(
    $known | Where-Object {
      (Get-ProcessStartTicks $_.Id) -eq [long]$_.StartTicks
    }
  )
  if ($stillActive.Count -gt 0) {
    return @($stillActive | ForEach-Object { [int]$_.Id } | Sort-Object -Unique)
  }

  if (-not (Stop-ExactProcess $launcher)) {
    return @([int]$State.launcherPid)
  }

  for ($pass = 0; $pass -lt 3; $pass++) {
    $late = @(Get-ManagedDescendantIdentities $State $known)
    if ($late.Count -eq 0) { break }
    foreach ($identity in @($late | Sort-Object Depth -Descending)) {
      if (-not (Stop-ExactProcess $identity)) {
        $refused += [int]$identity.Id
      }
      $known += $identity
    }
    if ($refused.Count -gt 0) { break }
    Start-Sleep -Milliseconds 100
  }
  $final = @(Get-ManagedDescendantIdentities $State $known)
  $finalMap = @{}
  foreach ($identity in @($known + $final)) {
    $finalMap["$($identity.Id):$($identity.StartTicks)"] = $identity
  }
  $finalActive = @(
    @($finalMap.Values) | Where-Object {
      (Get-ProcessStartTicks $_.Id) -eq [long]$_.StartTicks
    }
  )
  $refused += @($finalActive | ForEach-Object { [int]$_.Id })
  return @($refused | Sort-Object -Unique)
}

function Save-ManagedState {
  param($State)
  if (-not (Test-ManagedStateSchema $State)) {
    throw 'Refusing to persist invalid WMPFDebugger state.'
  }
  if (Test-Path -LiteralPath $statePath) {
    $existingState = Get-Item -LiteralPath $statePath -Force
    if ($existingState.PSIsContainer -or
        (($existingState.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) {
      throw "Refusing to replace non-regular WMPFDebugger state: $statePath"
    }
  }
  Write-JsonAtomically $statePath $State
}

function Test-CustodyStateMatches {
  param($Expected)
  try {
    if (-not (Test-LocalPathChain $statePath -RequireFile)) { return $false }
    $actual = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
    if (-not (Test-ManagedStateSchema $actual)) { return $false }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals([string]$actual.root, [string]$Expected.root) -or
        [long]$actual.launcherPid -ne [long]$Expected.launcherPid -or
        [long]$actual.launcherStartUtcTicks -ne [long]$Expected.launcherStartUtcTicks) {
      return $false
    }
    $actualLeases = @($actual.leases | Sort-Object)
    $expectedLeases = @($Expected.leases | Sort-Object)
    return $actualLeases.Count -eq $expectedLeases.Count -and
      [StringComparer]::OrdinalIgnoreCase.Equals(
        ($actualLeases -join ','),
        ($expectedLeases -join ',')
      )
  } catch {
    return $false
  }
}

function Resolve-LeaseToken {
  param([string]$Token, [switch]$Generate)
  if ([string]::IsNullOrWhiteSpace($Token)) {
    if ($Generate) { return [Guid]::NewGuid().ToString('D') }
    Write-Failure 'A lease token from start-wmpf-debugger.cmd is required to stop this service.' 7
  }
  $parsed = [Guid]::Empty
  if (-not [Guid]::TryParse($Token, [ref]$parsed)) {
    Write-Failure 'The WMPFDebugger lease token is invalid.' 7
  }
  return $parsed.ToString('D')
}

function Get-StateLeases {
  param($State)
  return @(
    @($State.leases) |
      ForEach-Object { [string]$_ } |
      Where-Object { -not [string]::IsNullOrWhiteSpace($_) } |
      Sort-Object -Unique
  )
}

function Repair-StaleManagedState {
  param($State)
  if ($null -eq $State) { return $false }
  $descendants = @(Get-ManagedDescendantIdentities $State)
  $nodeCandidates = @(
    $descendants | Where-Object { Test-WmpfNodeIdentity $_.Id $State }
  )
  if ($nodeCandidates.Count -ne 1) { return $false }

  $candidate = $nodeCandidates[0]
  $candidateTicks = Get-ProcessStartTicks $candidate.Id
  if ($null -eq $candidateTicks -or $candidateTicks -ne [long]$candidate.StartTicks) {
    return $false
  }
  $State.launcherPid = [int]$candidate.Id
  $State.launcherStartUtcTicks = [long]$candidateTicks
  Save-ManagedState $State
  return $true
}

function Resolve-StartRoot {
  $resolverPath = Join-Path $PSScriptRoot 'resolve-wmpf-root.ps1'
  if (-not (Test-LocalPathChain $resolverPath -RequireFile)) {
    Write-Failure 'The WMPFDebugger root resolver is missing, non-regular, or behind a reparse point.' 4
  }
  $resolverArgs = @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $resolverPath
  )
  if ($UseEnvironmentRoot) {
    $resolverArgs += '-UseEnvironmentCandidate'
  } elseif (-not [string]::IsNullOrWhiteSpace($Root)) {
    $resolverArgs += @('-Candidate', $Root)
  }
  $output = @(& powershell.exe @resolverArgs)
  $resolverExit = $LASTEXITCODE
  if ($resolverExit -ne 0 -or $output.Count -eq 0) {
    if ($resolverExit -le 0) { $resolverExit = 1 }
    Write-Failure 'Unable to resolve a valid WMPFDebugger root.' $resolverExit
  }
  return [string]$output[-1]
}

# The global lock protects machine-wide ports across login sessions. The legacy
# local lock keeps same-session released scripts on the same critical section.
$serviceMutexes = @()
$serviceMutexNames = @(
  'Global\OpenCode.WebProtocolRecovery.WechatMiniapp.WmpfDebuggerService',
  'Local\OpenCode.WechatMiniappReverse.WmpfDebuggerService'
)
foreach ($mutexName in $serviceMutexNames) {
  try {
    $serviceMutex = [System.Threading.Mutex]::new($false, $mutexName)
    try {
      $lockTaken = $serviceMutex.WaitOne(5000)
    } catch [System.Threading.AbandonedMutexException] {
      $lockTaken = $true
    }
  } catch {
    Write-Failure "Unable to acquire WMPFDebugger lifecycle mutex $mutexName." 6
  }
  if (-not $lockTaken) {
    Write-Failure "Timed out waiting for WMPFDebugger lifecycle mutex $mutexName." 6
  }
  $serviceMutexes += $serviceMutex
}

try {
  if ($Action -eq 'Status') {
    $state = Get-ManagedState
    $listeners = @(Get-AllListenerProcessIds)
    if ($null -ne $state -and -not (Test-ProcessIdentity $state)) {
      if (Repair-StaleManagedState $state) {
        $state = Get-ManagedState
      } else {
        $descendants = @(Get-ManagedDescendantIdentities $state)
        if ($listeners.Count -gt 0 -or $descendants.Count -gt 0) {
          Write-Failure 'Managed custody state exists, but no unique WMPFDebugger Node process can be promoted yet.' 4
        }
      }
    }
    if (Test-ProcessIdentity $state) {
      $unmanaged = @(Get-UnmanagedListenerProcessIds $state)
      if ($unmanaged.Count -gt 0) {
        Write-Failure "Ports 9421/62000 include unmanaged listener PID(s): $($unmanaged -join ', ')." 4
      }
      if ((Get-ListenerProcessIds 62000).Count -gt 0) {
        Write-Output "Managed WMPFDebugger is ready (launcher PID $($state.launcherPid))."
      } else {
        Write-Output "Managed WMPFDebugger is starting (launcher PID $($state.launcherPid))."
      }
      exit 0
    }
    if ($listeners.Count -gt 0) {
      Write-Failure "Ports 9421/62000 are occupied by unmanaged listener PID(s): $($listeners -join ', ')." 4
    }
    Write-Output 'Managed WMPFDebugger is not running.'
    exit 3
  }

  if ($Action -eq 'Start') {
    $lease = Resolve-LeaseToken $LeaseToken -Generate
    $state = Get-ManagedState
    if ($null -ne $state -and -not (Test-ProcessIdentity $state)) {
      if (-not (Repair-StaleManagedState $state)) {
        $listeners = @(Get-AllListenerProcessIds)
        $descendants = @(Get-ManagedDescendantIdentities $state)
        $presence = Get-RecordedProcessPresence $state
        if ($listeners.Count -gt 0 -or $descendants.Count -gt 0 -or
            $presence -ne 'missing') {
          Write-Failure "State still has processes or an unverifiable recorded PID (presence=$presence); preserve custody for manual inspection and do not kill by port." 4
        }
        Remove-ManagedState
        $state = $null
      }
    }
    if (Test-ProcessIdentity $state) {
      if ($UseEnvironmentRoot -or -not [string]::IsNullOrWhiteSpace($Root)) {
        $requestedRoot = Resolve-StartRoot
        if (-not [StringComparer]::OrdinalIgnoreCase.Equals([string]$state.root, $requestedRoot)) {
          Write-Failure 'A managed WMPFDebugger process from a different root is already active.' 5
        }
      }
      $unmanaged = @(Get-UnmanagedListenerProcessIds $state)
      if ($unmanaged.Count -gt 0) {
        Write-Failure "Refusing to start: ports 9421/62000 include unmanaged listener PID(s): $($unmanaged -join ', ')." 4
      }
      $leases = @(Get-StateLeases $state)
      if ($lease -notin $leases) {
        $leases += $lease
        $state | Add-Member -MemberType NoteProperty -Name leases -Value @($leases) -Force
        Save-ManagedState $state
      }
      if ((Get-ListenerProcessIds 62000).Count -gt 0) {
        Write-Output "Managed WMPFDebugger is already listening on 62000 (launcher PID $($state.launcherPid))."
      } else {
        Write-Output "Managed WMPFDebugger startup is already pending (launcher PID $($state.launcherPid))."
      }
      Write-Output "WMPFDebugger lease token: $lease"
      exit 0
    }

    $rootPath = Resolve-StartRoot
    $listeners = @(Get-AllListenerProcessIds)
    if ($listeners.Count -gt 0) {
      Write-Failure "Refusing to start: ports 9421/62000 are occupied by unmanaged listener PID(s): $($listeners -join ', ')." 4
    }

    $serviceProcess = Get-Process -Id $PID
    $createdUtc = [DateTime]::UtcNow.ToString('o')
    $provisionalState = [pscustomobject][ordered]@{
      version = 2
      root = $rootPath
      launcherPid = $PID
      launcherStartUtcTicks = $serviceProcess.StartTime.ToUniversalTime().Ticks
      createdUtc = $createdUtc
      leases = @($lease)
    }
    Save-ManagedState $provisionalState

    try {
      $escapedRoot = $rootPath.Replace("'", "''")
      $commandLine = 'call "node_modules\.bin\ts-node.cmd" "src\index.ts" >> "wmpf-debugger.log" 2>&1'
      $escapedCommand = $commandLine.Replace("'", "''")
      $hiddenScript = "Set-Location -LiteralPath '$escapedRoot'; & `$env:ComSpec /d /s /c '$escapedCommand'"
      $launcher = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
        '-Command', $hiddenScript
      ) -WorkingDirectory $rootPath -WindowStyle Hidden -PassThru
    } catch {
      Remove-ManagedState
      throw
    }
    $newState = [pscustomobject][ordered]@{
      version = 2
      root = $rootPath
      launcherPid = $launcher.Id
      launcherStartUtcTicks = $launcher.StartTime.ToUniversalTime().Ticks
      createdUtc = $createdUtc
      leases = @($lease)
    }
    try {
      Save-ManagedState $newState
    } catch {
      $saveError = $_.Exception.Message
      if (-not (Test-CustodyStateMatches $provisionalState)) {
        try {
          Save-ManagedState $provisionalState
        } catch {}
      }
      if (Test-CustodyStateMatches $provisionalState) {
        Write-Failure "WMPFDebugger final state persistence failed; provisional custody is retained. Retry Status or Stop with lease token: $lease. Error: $saveError" 4
      }
      Write-Failure "WMPFDebugger final state persistence failed and provisional custody could not be confirmed. Launcher PID: $($launcher.Id); lease token: $lease. Error: $saveError" 4
    }

    Start-Sleep -Milliseconds 200
    $launcher.Refresh()
    if ($launcher.HasExited) {
      $refused = @(Stop-ManagedProcessTree $newState)
      $remaining = @(Get-AllListenerProcessIds)
      if ($refused.Count -gt 0 -or $remaining.Count -gt 0) {
        Write-Failure 'WMPFDebugger exited during startup and managed descendants remain; state was retained for safe recovery.' 1
      }
      Remove-ManagedState
      Write-Failure 'WMPFDebugger exited during startup; inspect wmpf-debugger.log.' 1
    }

    Write-Output "WMPFDebugger spawn requested (managed launcher PID $($launcher.Id))."
    Write-Output "WMPFDebugger lease token: $lease"
    exit 0
  }

  $state = Get-ManagedState
  $listeners = @(Get-AllListenerProcessIds)
  if ($null -eq $state) {
    if ($listeners.Count -gt 0) {
      Write-Failure "Refusing to stop unmanaged listener PID(s): $($listeners -join ', ')." 4
    }
    Write-Output 'No managed WMPFDebugger process is recorded.'
    exit 0
  }

  if (-not (Test-ProcessIdentity $state)) {
    if (-not (Repair-StaleManagedState $state)) {
      $descendants = @(Get-ManagedDescendantIdentities $state)
      $presence = Get-RecordedProcessPresence $state
      if ($listeners.Count -gt 0 -or $descendants.Count -gt 0 -or
          $presence -ne 'missing') {
        Write-Failure "Stale state could not promote one proven WMPFDebugger Node process and the recorded PID is not safely absent (presence=$presence); automatic cleanup is refused." 4
      }
      Remove-ManagedState
      Write-Output 'Removed stale WMPFDebugger state; no managed process is running.'
      exit 0
    }
  }

  $unmanaged = @(Get-UnmanagedListenerProcessIds $state)
  if ($unmanaged.Count -gt 0) {
    Write-Failure "Refusing to stop: ports 9421/62000 include unmanaged listener PID(s): $($unmanaged -join ', ')." 4
  }

  $lease = Resolve-LeaseToken $LeaseToken
  $leases = @(Get-StateLeases $state)
  if ($lease -notin $leases) {
    Write-Failure 'The lease token does not own this managed WMPFDebugger service.' 7
  }
  $remainingLeases = @($leases | Where-Object { $_ -ne $lease })
  if ($remainingLeases.Count -gt 0) {
    $state | Add-Member -MemberType NoteProperty -Name leases -Value @($remainingLeases) -Force
    Save-ManagedState $state
    Write-Output "Released WMPFDebugger lease $lease; service remains active for $($remainingLeases.Count) lease(s)."
    exit 0
  }

  $refused = @(Stop-ManagedProcessTree $state)
  Start-Sleep -Milliseconds 200
  if ($refused.Count -gt 0) {
    Write-Failure "Refused to stop PID(s) whose start time no longer matched state: $($refused -join ', ')." 4
  }

  $remaining = @(Get-AllListenerProcessIds)
  if ($remaining.Count -gt 0) {
    Write-Failure "Managed process stopped, but ports 9421/62000 now have listener PID(s): $($remaining -join ', ')." 4
  }
  Remove-ManagedState
  Write-Output "Stopped managed WMPFDebugger launcher PID $($state.launcherPid) and its process tree."
  exit 0
} catch {
  [Console]::Error.WriteLine("WMPFDebugger lifecycle failure: $($_.Exception.Message)")
  [Console]::Error.WriteLine("Type: $($_.Exception.GetType().FullName); Line: $($_.InvocationInfo.ScriptLineNumber); Command: $($_.InvocationInfo.MyCommand)")
  [Console]::Error.WriteLine("Stack: $($_.ScriptStackTrace)")
  try {
    if ($Action -eq 'Stop' -and $null -ne $state) {
      $presence = Get-RecordedProcessPresence $state
      $descendants = @(Get-ManagedDescendantIdentities $state)
      $liveListeners = @(Get-AllListenerProcessIds)
      if ($presence -eq 'missing' -and $descendants.Count -eq 0 -and $liveListeners.Count -eq 0) {
        Remove-ManagedState
        [Console]::Error.WriteLine('Recovered: removed stale managed state; no managed process remains.')
      }
    }
  } catch {
    [Console]::Error.WriteLine("Post-failure recovery check failed: $($_.Exception.Message)")
  }
  exit 1
}
