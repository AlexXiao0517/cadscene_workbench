param()

$ErrorActionPreference = "Stop"
$bundleRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runtime = Join-Path $bundleRoot "runtime"
$python = Join-Path $bundleRoot "runtime\python.exe"
$launcher = Join-Path $bundleRoot "launcher\start.ps1"
$statePath = Join-Path $bundleRoot "launcher\service-state.json"
$payloadRoot = Join-Path $PSScriptRoot "payload"

try {
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "没有找到整合包 Python：$python。请把补丁 ZIP 直接解压到 CADScene-0.1.3 程序根目录。"
    }
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
        throw "没有找到启动文件：$launcher。请检查补丁解压位置。"
    }
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        $state = Get-Content -Raw -LiteralPath $statePath | ConvertFrom-Json
        $process = Get-Process -Id ([int]$state.pid) -ErrorAction SilentlyContinue
        if ($process) {
            throw "CADScene 服务仍在运行，请先双击关闭脚本。"
        }
    }

    $relativeFiles = @(
        "launcher\start.ps1",
        "runtime\Lib\site-packages\cadscene\cli\workspace_migration.py",
        "runtime\Lib\site-packages\cadscene\cli\runtime_relocation.py"
    )
    foreach ($relative in $relativeFiles) {
        $sourceFile = Join-Path $payloadRoot $relative
        $destinationFile = Join-Path $bundleRoot $relative
        if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
            throw "补丁文件缺失：$sourceFile"
        }
        if (-not (Test-Path -LiteralPath $destinationFile -PathType Leaf)) {
            throw "整合包目标文件缺失：$destinationFile"
        }
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupRoot = Join-Path $bundleRoot "launcher\hotfix-backup-$timestamp"
    foreach ($relative in $relativeFiles) {
        $destinationFile = Join-Path $bundleRoot $relative
        $backupFile = Join-Path $backupRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backupFile) | Out-Null
        Copy-Item -LiteralPath $destinationFile -Destination $backupFile
    }

    try {
        foreach ($relative in $relativeFiles) {
            $sourceFile = Join-Path $payloadRoot $relative
            $destinationFile = Join-Path $bundleRoot $relative
            Copy-Item -LiteralPath $sourceFile -Destination $destinationFile -Force
        }
        $repairArguments = @(
            "-m", "cadscene.cli.runtime_relocation", "--runtime", $runtime
        )
        $repairOutput = & $python @repairArguments 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "OpenCV 路径修复失败：$($repairOutput -join [Environment]::NewLine)"
        }
    }
    catch {
        foreach ($relative in $relativeFiles) {
            $backupFile = Join-Path $backupRoot $relative
            $destinationFile = Join-Path $bundleRoot $relative
            if (Test-Path -LiteralPath $backupFile -PathType Leaf) {
                Copy-Item -LiteralPath $backupFile -Destination $destinationFile -Force
            }
        }
        throw
    }

    Write-Host "CADScene 0.1.3 补丁应用完成。原文件备份在：$backupRoot"
    Write-Host "现在可以双击启动 CAD 视频工作台。"
    exit 0
}
catch {
    Write-Error $_
    exit 1
}
