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

function Assert-PathBudget() {
    # Windows 部分原生工具仍受旧式路径上限影响，按视频分析发布阶段的真实最长路径预留空间。
    $probeProject = "dataset-00000000-0000-0000-0000-000000000000"
    $probeJob = "00000000000000000000000000000000"
    $probeJobRoot = Join-Path $workspace "projects\$probeProject\jobs\$probeJob\attempt-1"
    $probeProjectRoot = Join-Path $workspace "projects\$probeProject"
    $probePaths = @(
        (Join-Path $probeJobRoot ".va-00000000\r"),
        (Join-Path $probeJobRoot "v\r\r-0000000000000000\video_analysis_manifest.json"),
        (Join-Path $probeProjectRoot "analysis_artifacts\.pub-00000000\a\02_video_analysis\video_analysis_manifest.json"),
        (Join-Path $probeProjectRoot "analysis_artifacts\va-0000000000000000\02_video_analysis\video_analysis_manifest.json")
    )
    $probePath = $probePaths | Sort-Object { $_.Length } -Descending | Select-Object -First 1
    $maxSafePathLength = 240
    if ($probePath.Length -gt $maxSafePathLength) {
        throw (
            "Windows 路径过长，后续上传任务将无法创建临时目录。" +
            "请把包含启动文件的程序目录直接移动到较短位置，例如 D:\CADScene，然后重新启动。" +
            "当前预计任务路径长度：$($probePath.Length)，安全上限：$maxSafePathLength。"
        )
    }
}

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

function Invoke-WorkspaceMigrationRecord(
    [string]$oldWorkspace,
    [AllowNull()][string]$backupWorkspace
) {
    $recordArguments = @(
        "-m", "cadscene.cli.workspace_migration", "record",
        "--workspace", $workspace,
        "--old-workspace", $oldWorkspace
    )
    if ($backupWorkspace) {
        $recordArguments += @("--backup-workspace", $backupWorkspace)
    }
    $recordOutput = & $python @recordArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "无法写入 workspace 迁移标记：$($recordOutput -join [Environment]::NewLine)"
    }
}

function Invoke-WorkspaceMigration() {
    $planArguments = @(
        "-m", "cadscene.cli.workspace_migration", "plan",
        "--workspace", $workspace
    )
    $planOutput = & $python @planArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "旧 workspace 兼容检查失败：$($planOutput -join [Environment]::NewLine)"
    }
    try {
        $plan = ($planOutput -join [Environment]::NewLine) | ConvertFrom-Json
    }
    catch {
        throw "旧 workspace 兼容检查返回了无效结果：$($planOutput -join [Environment]::NewLine)"
    }
    if ([string]$plan.action -eq "none") {
        return
    }

    $oldWorkspace = [IO.Path]::GetFullPath([string]$plan.old_workspace)
    if ((Split-Path -Leaf $oldWorkspace) -ine "workspace") {
        throw "拒绝迁移非 workspace 目录：$oldWorkspace"
    }
    if ($oldWorkspace -ieq [IO.Path]::GetFullPath($workspace)) {
        throw "旧 workspace 与当前 workspace 相同，拒绝执行迁移。"
    }
    $migrationAction = [string]$plan.action
    if ($migrationAction -eq "record") {
        Invoke-WorkspaceMigrationRecord $oldWorkspace $null
        return
    }
    if ($migrationAction -notin @("migrate", "recover")) {
        throw "未知的 workspace 迁移动作：$($plan.action)"
    }

    $backupWorkspace = [IO.Path]::GetFullPath([string]$plan.backup_workspace)
    if ((Split-Path -Parent $backupWorkspace) -ine (Split-Path -Parent $oldWorkspace)) {
        throw "workspace 备份必须与旧目录位于同一父目录：$backupWorkspace"
    }
    if (-not (Test-Path -LiteralPath $oldWorkspace -PathType Container)) {
        throw "旧 workspace 不存在：$oldWorkspace"
    }
    if ($migrationAction -eq "migrate" -and (Test-Path -LiteralPath $backupWorkspace)) {
        throw "workspace 备份目录已存在：$backupWorkspace"
    }
    if ($migrationAction -eq "recover" -and -not (Test-Path -LiteralPath $backupWorkspace -PathType Container)) {
        throw "用于恢复的 workspace pre 备份不存在：$backupWorkspace"
    }
    $oldItem = Get-Item -LiteralPath $oldWorkspace
    if (($oldItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "旧 workspace 已是链接但未通过兼容校验：$oldWorkspace"
    }

    try {
        $active = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $candidate = $_
            $candidate.CommandLine -and
            $candidate.CommandLine.Contains("cadscene.cli.main") -and
            $candidate.CommandLine.Contains("serve") -and
            $candidate.CommandLine.Contains("--storage-root") -and
            $candidate.CommandLine.Contains($oldWorkspace)
        }
    }
    catch {
        throw "无法检查旧版 CADScene 服务是否仍在运行：$($_.Exception.Message)"
    }
    if ($active) {
        throw "旧版 CADScene 服务仍在使用 $oldWorkspace。请先点击旧版的关闭脚本，再启动新版。"
    }

    if ($migrationAction -eq "recover") {
        $displacedWorkspace = [IO.Path]::GetFullPath([string]$plan.displaced_workspace)
        if ((Split-Path -Parent $displacedWorkspace) -ine (Split-Path -Parent $oldWorkspace)) {
            throw "旧 workspace 空壳备份必须位于原目录旁：$displacedWorkspace"
        }
        if (Test-Path -LiteralPath $displacedWorkspace) {
            throw "旧 workspace 空壳备份已存在：$displacedWorkspace"
        }
        $junctionCreated = $false
        Move-Item -LiteralPath $oldWorkspace -Destination $displacedWorkspace
        try {
            $allowedStaleLease = Join-Path $displacedWorkspace "projects\.serve_viewer.lease"
            $unexpected = Get-ChildItem -LiteralPath $displacedWorkspace -Force -Recurse | Where-Object {
                $isReparsePoint = ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
                $isUnexpectedFile = -not $_.PSIsContainer -and $_.FullName -ine $allowedStaleLease
                $isReparsePoint -or $isUnexpectedFile
            } | Select-Object -First 1
            if ($unexpected) {
                throw "旧 workspace 空壳在恢复期间出现了文件：$($unexpected.FullName)"
            }
            New-Item -ItemType Junction -Path $oldWorkspace -Target $workspace | Out-Null
            $junctionCreated = $true
            Invoke-WorkspaceMigrationRecord $oldWorkspace $backupWorkspace
            Write-Host "旧 workspace 空壳已恢复为兼容 Junction。原 pre 备份保留在：$backupWorkspace"
        }
        catch {
            $recoveryError = $_
            if ($junctionCreated -and (Test-Path -LiteralPath $oldWorkspace)) {
                Remove-Item -LiteralPath $oldWorkspace -Force
            }
            if (
                (Test-Path -LiteralPath $displacedWorkspace -PathType Container) -and
                -not (Test-Path -LiteralPath $oldWorkspace)
            ) {
                Move-Item -LiteralPath $displacedWorkspace -Destination $oldWorkspace
            }
            throw "恢复中断的 workspace 迁移失败，旧空壳已回滚：$($recoveryError.Exception.Message)"
        }
        return
    }

    $junctionCreated = $false
    Move-Item -LiteralPath $oldWorkspace -Destination $backupWorkspace
    try {
        New-Item -ItemType Junction -Path $oldWorkspace -Target $workspace | Out-Null
        $junctionCreated = $true
        Invoke-WorkspaceMigrationRecord $oldWorkspace $backupWorkspace
        Write-Host "旧项目工作空间已兼容迁移。备份保留在：$backupWorkspace"
    }
    catch {
        $migrationError = $_
        if ($junctionCreated -and (Test-Path -LiteralPath $oldWorkspace)) {
            Remove-Item -LiteralPath $oldWorkspace -Force
        }
        if (
            (Test-Path -LiteralPath $backupWorkspace -PathType Container) -and
            -not (Test-Path -LiteralPath $oldWorkspace)
        ) {
            Move-Item -LiteralPath $backupWorkspace -Destination $oldWorkspace
        }
        throw "创建兼容 Junction 失败，旧 workspace 已回滚：$($migrationError.Exception.Message)"
    }
}

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Bundled Python runtime is missing: $python"
    }
    Assert-PathBudget
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
        $unpackScript = Join-Path $runtime "Scripts\conda-unpack-script.py"
        if (-not (Test-Path -LiteralPath $unpackScript -PathType Leaf)) {
            throw "Bundled runtime relocation tool is missing: $unpackScript"
        }
        & $python $unpackScript
        if ($LASTEXITCODE -ne 0) {
            throw "Bundled runtime relocation failed with exit code $LASTEXITCODE."
        }
        New-Item -ItemType File -Force -Path $relocatedMarker | Out-Null
    }

    $runtimeRepairArguments = @(
        "-m", "cadscene.cli.runtime_relocation", "--runtime", $runtime
    )
    $runtimeRepairOutput = & $python @runtimeRepairArguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Bundled OpenCV runtime relocation failed: $($runtimeRepairOutput -join [Environment]::NewLine)"
    }

    Invoke-WorkspaceMigration

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
