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
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$state.pid)" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Remove-Item -LiteralPath $statePath -Force
        Write-Host "CADScene Workbench is not running."
        exit 0
    }

    $expectedPython = [IO.Path]::GetFullPath($python)
    $actualPython = if ($process.ExecutablePath) { [IO.Path]::GetFullPath($process.ExecutablePath) } else { "" }
    $command = [string]$process.CommandLine
    $identityMatches = (
        $actualPython -ieq $expectedPython -and
        $command.Contains("cadscene.cli.main") -and
        $command.Contains("serve") -and
        $command.Contains("--storage-root") -and
        $command.Contains($workspace)
    )
    if (-not $identityMatches) {
        throw "Stored PID does not belong to this CADScene Workbench bundle; refusing to stop it."
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

