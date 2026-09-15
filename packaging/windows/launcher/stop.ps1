param([switch]$Force)

$ErrorActionPreference = "Stop"
$bundleRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Join-Path $bundleRoot "runtime\python.exe"
$workspace = Join-Path $bundleRoot "workspace"
$statePath = Join-Path $PSScriptRoot "service-state.json"

try {
    if (-not (Test-Path -LiteralPath $statePath -PathType Leaf)) {
        Write-Host "CADScene Workbench is not running."
        exit 0
    }
    $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
    $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $statePath -Force
        Write-Host "CADScene Workbench is not running."
        exit 0
    }

    $expectedPython = [IO.Path]::GetFullPath($python)
    $actualPython = if ($process.Path) { [IO.Path]::GetFullPath($process.Path) } else { "" }
    $expectedStart = [DateTime]::Parse([string]$state.process_start_time).ToUniversalTime()
    $actualStart = $process.StartTime.ToUniversalTime()
    $identityMatches = (
        $actualPython -ieq $expectedPython -and
        [Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -le 1 -and
        [IO.Path]::GetFullPath([string]$state.storage_root) -ieq [IO.Path]::GetFullPath($workspace)
    )
    $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$state.pid)" -ErrorAction SilentlyContinue
    if ($identityMatches -and $null -ne $cim) {
        $command = [string]$cim.CommandLine
        $identityMatches = (
            $cim.ExecutablePath -and
            [IO.Path]::GetFullPath($cim.ExecutablePath) -ieq $expectedPython -and
            $command.Contains("cadscene.cli.main") -and
            $command.Contains("serve") -and
            $command.Contains("--storage-root") -and
            $command.Contains($workspace)
        )
    }
    if (-not $identityMatches) {
        throw "Stored PID does not belong to this CADScene Workbench bundle; refusing to stop it."
    }

    if (-not $Force) {
        $activeSessions = @()
        $projects = Join-Path $workspace "projects"
        if (Test-Path -LiteralPath $projects -PathType Container) {
            Get-ChildItem -LiteralPath $projects -Directory -ErrorAction SilentlyContinue | ForEach-Object {
                $sessionDirectory = Join-Path $_.FullName "workbench_sessions"
                if (-not (Test-Path -LiteralPath $sessionDirectory -PathType Container)) {
                    return
                }
                Get-ChildItem -LiteralPath $sessionDirectory -Filter "*.json" -File -ErrorAction SilentlyContinue | ForEach-Object {
                    try {
                        $session = Get-Content -Raw -LiteralPath $_.FullName | ConvertFrom-Json
                        $expires = [DateTime]::Parse([string]$session.expires_at).ToUniversalTime()
                        if ($session.state -eq "editing" -and $expires -gt [DateTime]::UtcNow) {
                            $activeSessions += "$( [string]$session.project_id ) / $( [string]$session.clip_id )"
                        }
                    }
                    catch {
                        # Corrupt historical session files do not block a verified service stop.
                    }
                }
            }
        }
        if ($activeSessions.Count -gt 0) {
            Write-Warning "Active workbench editing sessions were found: $($activeSessions -join ', ')."
            Write-Warning "Confirmed keyframes already saved as server drafts are safe; unconfirmed camera movement may be lost."
            $answer = Read-Host "Type YES to stop anyway"
            if ($answer -cne "YES") {
                Write-Host "Stop cancelled. Save or close the active workbench first."
                exit 2
            }
        }
    }

    Stop-Process -Id ([int]$state.pid) -Force
    Remove-Item -LiteralPath $statePath -Force
    Write-Host "CADScene Workbench stopped."
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
