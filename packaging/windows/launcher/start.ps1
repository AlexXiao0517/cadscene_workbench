param(
    [int]$PreferredPort = 8300,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
$bundleRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtime = Join-Path $bundleRoot "runtime"
$python = Join-Path $bundleRoot "runtime\python.exe"
$workspace = Join-Path $bundleRoot "workspace"
$backend = Join-Path $bundleRoot "pure_rotation_backend"
$logs = Join-Path $bundleRoot "logs"
$statePath = Join-Path $PSScriptRoot "service-state.json"
$relocatedMarker = Join-Path $runtime ".cadscene-relocated"

function Test-ManagedService([object]$state) {
    if ($null -eq $state -or $null -eq $state.pid -or $null -eq $state.process_start_time) {
        return $false
    }
    $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }
    $expectedPython = [IO.Path]::GetFullPath($python)
    $actualPython = if ($process.Path) { [IO.Path]::GetFullPath($process.Path) } else { "" }
    $expectedStart = [DateTime]::Parse([string]$state.process_start_time).ToUniversalTime()
    $actualStart = $process.StartTime.ToUniversalTime()
    if (
        $actualPython -ieq $expectedPython -and
        [Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -le 1 -and
        [IO.Path]::GetFullPath([string]$state.storage_root) -ieq [IO.Path]::GetFullPath($workspace)
    ) {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$state.pid)" -ErrorAction SilentlyContinue
        if ($null -ne $cim) {
            $command = [string]$cim.CommandLine
            return (
                $cim.ExecutablePath -and
                [IO.Path]::GetFullPath($cim.ExecutablePath) -ieq $expectedPython -and
                $command.Contains("cadscene.cli.main") -and
                $command.Contains("serve") -and
                $command.Contains("--storage-root") -and
                $command.Contains($workspace)
            )
        }
        return $true
    }
    return $false
}

function Get-FreePort([int]$startPort) {
    foreach ($candidate in $startPort..($startPort + 99)) {
        $listener = $null
        try {
            $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $candidate)
            $listener.Start()
            return $candidate
        }
        catch {
            continue
        }
        finally {
            if ($null -ne $listener) {
                $listener.Stop()
            }
        }
    }
    throw "No free localhost port was found between $startPort and $($startPort + 99)."
}

function Quote-ProcessArgument([string]$value) {
    return '"' + $value.Replace('"', '\"') + '"'
}

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Bundled Python runtime is missing: $python"
    }
    New-Item -ItemType Directory -Force -Path $workspace, $logs | Out-Null

    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        $existingState = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
        if (Test-ManagedService $existingState) {
            $existingUrl = "http://127.0.0.1:$($existingState.port)/apps/project_library/"
            if (-not $NoBrowser) {
                Start-Process $existingUrl
            }
            Write-Host "CADScene Workbench is already running: $existingUrl"
            exit 0
        }
        Remove-Item -LiteralPath $statePath -Force
    }

    $env:PATH = "$runtime;$runtime\Scripts;$runtime\Library\bin;$env:PATH"
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    Set-Location -LiteralPath $bundleRoot
    if (-not (Test-Path -LiteralPath $relocatedMarker -PathType Leaf)) {
        $unpacker = Join-Path $runtime "Scripts\conda-unpack.exe"
        if (-not (Test-Path -LiteralPath $unpacker -PathType Leaf)) {
            throw "Bundled runtime relocation tool is missing: $unpacker"
        }
        & $unpacker
        if ($LASTEXITCODE -ne 0) {
            throw "Bundled runtime relocation failed with exit code $LASTEXITCODE."
        }
        New-Item -ItemType File -Force -Path $relocatedMarker | Out-Null
    }

    $doctorArguments = @(
        "-m", "cadscene.cli.main", "doctor",
        "--storage-root", $workspace,
        "--pure-rotation-backend-root", $backend,
        "--pure-rotation-python", $python
    )
    $doctorOutput = & $python @doctorArguments 2>&1
    $doctorOutput | Out-File -LiteralPath (Join-Path $logs "doctor.txt") -Encoding utf8
    if ($LASTEXITCODE -ne 0) {
        throw "Environment self-check failed. See logs\doctor.txt."
    }

    $port = Get-FreePort $PreferredPort
    $stdoutPath = Join-Path $logs "service-stdout.log"
    $stderrPath = Join-Path $logs "service-stderr.log"
    $serviceArguments = @(
        "-m", "cadscene.cli.main", "serve",
        "--bind", "127.0.0.1",
        "--port", [string]$port,
        "--storage-root", $workspace,
        "--pure-rotation-backend-root", $backend,
        "--pure-rotation-python", $python
    )
    $argumentLine = ($serviceArguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join " "
    $processParameters = @{
        FilePath = $python
        ArgumentList = $argumentLine
        WorkingDirectory = $bundleRoot
        RedirectStandardOutput = $stdoutPath
        RedirectStandardError = $stderrPath
        WindowStyle = "Hidden"
        PassThru = $true
    }
    $process = Start-Process @processParameters
    $url = "http://127.0.0.1:$port/apps/project_library/"
    $ready = $false
    foreach ($attempt in 1..120) {
        Start-Sleep -Milliseconds 250
        if ($process.HasExited) {
            throw "Service exited during startup. See logs\service-stderr.log."
        }
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 2
            if ($response.StatusCode -eq 200) {
                $ready = $true
                break
            }
        }
        catch {
            continue
        }
    }
    if (-not $ready) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        throw "Service did not become ready within 30 seconds."
    }

    [pscustomobject]@{
        pid = $process.Id
        port = $port
        python = [IO.Path]::GetFullPath($python)
        storage_root = [IO.Path]::GetFullPath($workspace)
        process_start_time = $process.StartTime.ToUniversalTime().ToString("o")
        started_at = [DateTime]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding utf8

    if (-not $NoBrowser) {
        Start-Process $url
    }
    Write-Host "CADScene Workbench is ready: $url"
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
