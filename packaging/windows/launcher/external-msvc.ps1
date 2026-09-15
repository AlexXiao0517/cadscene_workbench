$script:ExternalMsvcHelpUrl = "https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist"
$script:ExternalMsvcMinimumVersion = [Version]"14.51.36231.0"
$script:ExternalMsvcRequiredDlls = @(
    "vcruntime140.dll", "vcruntime140_1.dll", "msvcp140.dll", "msvcp140_1.dll",
    "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
    "concrt140.dll", "vcomp140.dll", "vcamp140.dll", "vccorlib140.dll",
    "vcruntime140_threads.dll"
)
$script:ExternalMsvcAliases = @{
    "runtime/Lib/site-packages/pycolmap.libs/msvcp140-a4c2229bdc2a2a630acdc095b4d86008.dll" = "msvcp140.dll"
    "runtime/Lib/site-packages/pycolmap.libs/msvcp140_2-fe68f6b61d8de0f75e5717671051586a.dll" = "msvcp140_2.dll"
    "runtime/Lib/site-packages/pycolmap.libs/vcomp140-f96f3a14d88d8846f31f3ab38a490304.dll" = "vcomp140.dll"
    "runtime/Lib/site-packages/pyproj.libs/msvcp140-d76d4b45e040cbc263297f5a5893a46c.dll" = "msvcp140.dll"
}

if (-not (Test-Path Function:\Get-ExternalMsvcSystemDirectory)) {
    function Get-ExternalMsvcSystemDirectory() { return [Environment]::SystemDirectory }
}
if (-not (Test-Path Function:\Get-ExternalMsvcFileVersion)) {
    function Get-ExternalMsvcFileVersion([string]$Path) {
        return [Diagnostics.FileVersionInfo]::GetVersionInfo($Path).FileVersion
    }
}
if (-not (Test-Path Function:\Get-ExternalMsvcMachine)) {
    function Get-ExternalMsvcMachine([string]$Path) {
        $stream = [IO.File]::OpenRead($Path)
        try {
            $reader = New-Object IO.BinaryReader($stream)
            if ($reader.ReadUInt16() -ne 0x5A4D) { return "invalid" }
            $stream.Position = 0x3C
            $peOffset = $reader.ReadUInt32()
            if ($peOffset -gt ($stream.Length - 6)) { return "invalid" }
            $stream.Position = $peOffset
            if ($reader.ReadUInt32() -ne 0x00004550) { return "invalid" }
            if ($reader.ReadUInt16() -eq 0x8664) { return "x64" }
            return "not-x64"
        }
        finally { $stream.Dispose() }
    }
}
if (-not (Test-Path Function:\Test-ExternalMsvcMicrosoftSignature)) {
    function Test-ExternalMsvcMicrosoftSignature([string]$Path) {
        $signature = Get-AuthenticodeSignature -LiteralPath $Path
        return (
            $signature.Status -eq [Management.Automation.SignatureStatus]::Valid -and
            $null -ne $signature.SignerCertificate -and
            $signature.SignerCertificate.Subject -match '(^|, )O=Microsoft Corporation(,|$)'
        )
    }
}

function Throw-ExternalMsvcError([string]$Message) {
    throw "$Message`n请从微软官方页面安装或修复最新的 Microsoft Visual C++ x64 运行库：$script:ExternalMsvcHelpUrl"
}

function Get-ExternalMsvcSha256([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "") }
    finally { $sha256.Dispose(); $stream.Dispose() }
}

function Assert-ExternalMsvcPlainPath([string]$Root, [string]$Path) {
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $pathFull = [IO.Path]::GetFullPath($Path)
    if (-not $pathFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
        throw "external-msvc.json 包含越界目标路径：$Path"
    }
    $cursor = [IO.Path]::GetFullPath($Root)
    if (Test-Path -LiteralPath $cursor) {
        if (((Get-Item -Force -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "拒绝通过链接目录写入本地运行库别名：$cursor"
        }
    }
    $relative = $pathFull.Substring($rootFull.Length)
    foreach ($part in $relative.Split('\')) {
        $cursor = Join-Path $cursor $part
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -Force -LiteralPath $cursor
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "拒绝通过链接或重解析点写入本地运行库别名：$cursor"
            }
        }
    }
}

function Initialize-ExternalMsvc([Parameter(Mandatory=$true)][string]$BundleRoot) {
    $root = [IO.Path]::GetFullPath($BundleRoot)
    $manifestPath = Join-Path $root "external-msvc.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return }

    try { $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json }
    catch { throw "external-msvc.json 不是有效 JSON：$($_.Exception.Message)" }
    $allowedProperties = @("schema_version", "minimum_version", "required_dlls", "aliases", "excluded")
    $actualProperties = @($manifest.PSObject.Properties.Name)
    if (@($actualProperties | Where-Object { $_ -notin $allowedProperties }).Count -or
        @($allowedProperties | Where-Object { $_ -notin $actualProperties }).Count) {
        throw "external-msvc.json 字段不符合 schema version 1。"
    }
    if ($manifest.schema_version -ne 1) { throw "不支持的 external-msvc.json schema_version。" }
    $minimumText = [string]$manifest.minimum_version
    if ($minimumText -notmatch '^\d+(\.\d+){3}$') { throw "external-msvc.json minimum_version 无效。" }
    try { $minimum = [Version]$minimumText }
    catch { throw "external-msvc.json minimum_version 无效。" }
    if ($minimum -lt $script:ExternalMsvcMinimumVersion) { throw "external-msvc.json minimum_version 低于支持下限。" }
    $required = @($manifest.required_dlls)
    if ($required.Count -ne $script:ExternalMsvcRequiredDlls.Count -or
        @($required | Where-Object { $_ -cnotin $script:ExternalMsvcRequiredDlls }).Count -or
        @($script:ExternalMsvcRequiredDlls | Where-Object { $_ -cnotin $required }).Count) {
        throw "external-msvc.json required_dlls 不符合允许列表。"
    }
    $seenExcluded = @{}
    foreach ($excluded in @($manifest.excluded)) {
        if (@($excluded.PSObject.Properties.Name).Count -ne 2 -or
            "path" -notin $excluded.PSObject.Properties.Name -or "sha256" -notin $excluded.PSObject.Properties.Name) {
            throw "external-msvc.json excluded 字段无效。"
        }
        $excludedPath = [string]$excluded.path
        $parts = @($excludedPath.Split('/'))
        $parent = if ($parts.Count -gt 1) { ($parts[0..($parts.Count - 2)] -join '/').ToLowerInvariant() } else { "" }
        $basename = if ($parts.Count) { $parts[-1].ToLowerInvariant() } else { "" }
        $allowedParents = @(
            "runtime", "runtime/library/bin",
            "runtime/lib/site-packages/pycolmap.libs",
            "runtime/lib/site-packages/pyproj.libs", "colmap/bin",
            "pure_rotation_backend/outputs/build_opengv_cli"
        )
        $knownBasename = (
            $basename -cin $script:ExternalMsvcRequiredDlls -or
            $basename -ceq "ucrtbase.dll" -or
            $basename -match '^api-ms-win-(core|crt)-[a-z0-9-]+\.dll$' -or
            @($script:ExternalMsvcAliases.Keys | Where-Object {
                ([IO.Path]::GetFileName($_)).ToLowerInvariant() -ceq $basename -and
                $_.ToLowerInvariant() -ceq $excludedPath.ToLowerInvariant()
            }).Count -eq 1
        )
        if ($excludedPath.Contains('\') -or $excludedPath.Contains(':') -or
            [IO.Path]::IsPathRooted($excludedPath) -or $parts -contains "" -or
            $parts -contains "." -or $parts -contains ".." -or
            $parent -notin $allowedParents -or -not $knownBasename -or
            [string]$excluded.sha256 -notmatch '^[0-9a-fA-F]{64}$') {
            throw "external-msvc.json excluded 路径或 SHA-256 无效。"
        }
        if ($seenExcluded.ContainsKey($excludedPath)) { throw "external-msvc.json 包含重复 excluded 路径。" }
        $seenExcluded[$excludedPath] = $true
    }

    $plans = @()
    $seenTargets = @{}
    foreach ($alias in @($manifest.aliases)) {
        if (@($alias.PSObject.Properties.Name).Count -ne 2 -or
            "source" -notin $alias.PSObject.Properties.Name -or "target" -notin $alias.PSObject.Properties.Name) {
            throw "external-msvc.json aliases 字段无效。"
        }
        $sourceName = [string]$alias.source
        $targetRelative = ([string]$alias.target).Replace('\', '/')
        if ([IO.Path]::GetFileName($sourceName) -cne $sourceName -or $sourceName -cnotin $script:ExternalMsvcRequiredDlls) {
            throw "external-msvc.json 包含未知 source：$sourceName"
        }
        if (-not (@($script:ExternalMsvcAliases.Keys) -ccontains $targetRelative) -or
            $script:ExternalMsvcAliases[$targetRelative] -cne $sourceName) {
            throw "external-msvc.json 包含未知 alias target：$targetRelative"
        }
        if ($seenTargets.ContainsKey($targetRelative)) { throw "external-msvc.json 包含重复 alias target。" }
        $seenTargets[$targetRelative] = $true
        $target = Join-Path $root ($targetRelative.Replace('/', '\'))
        Assert-ExternalMsvcPlainPath $root $target
        if ((Test-Path -LiteralPath $target) -and -not (Test-Path -LiteralPath $target -PathType Leaf)) {
            throw "本地运行库别名目标不是普通文件：$target"
        }
        $plans += [pscustomobject]@{ SourceName=$sourceName; Target=$target }
    }
    if ($plans.Count -ne $script:ExternalMsvcAliases.Count) { throw "external-msvc.json 必须声明全部本地运行库别名。" }

    if (-not [Environment]::Is64BitOperatingSystem -or -not [Environment]::Is64BitProcess) {
        Throw-ExternalMsvcError "本程序只支持 Windows x64，并且必须由 64 位 PowerShell 启动。"
    }
    $systemDirectory = [IO.Path]::GetFullPath((Get-ExternalMsvcSystemDirectory))
    $sources = @{}
    foreach ($dll in $script:ExternalMsvcRequiredDlls) {
        $source = Join-Path $systemDirectory $dll
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { Throw-ExternalMsvcError "系统缺少必需运行库文件：$dll" }
        try { $version = [Version]([string](Get-ExternalMsvcFileVersion $source)) }
        catch { Throw-ExternalMsvcError "无法读取运行库版本：$dll" }
        if ($version -lt $minimum) { Throw-ExternalMsvcError "运行库版本过旧：$dll（$version，最低要求 $minimum）" }
        if ((Get-ExternalMsvcMachine $source) -ne "x64") { Throw-ExternalMsvcError "运行库不是受支持的 x64 文件：$dll" }
        if (-not (Test-ExternalMsvcMicrosoftSignature $source)) { Throw-ExternalMsvcError "运行库未通过 Microsoft 数字签名验证：$dll" }
        $sources[$dll] = $source
    }

    foreach ($plan in $plans) {
        $parent = Split-Path -Parent $plan.Target
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
        Assert-ExternalMsvcPlainPath $root $plan.Target
        $source = $sources[$plan.SourceName]
        if ((Test-Path -LiteralPath $plan.Target -PathType Leaf) -and
            (Get-ExternalMsvcSha256 $source) -eq (Get-ExternalMsvcSha256 $plan.Target)) {
            continue
        }
        $temporary = Join-Path $parent ("." + [IO.Path]::GetFileName($plan.Target) + "." + [Guid]::NewGuid().ToString("N") + ".tmp")
        $backup = Join-Path $parent ("." + [IO.Path]::GetFileName($plan.Target) + "." + [Guid]::NewGuid().ToString("N") + ".bak")
        try {
            Copy-Item -LiteralPath $source -Destination $temporary
            if (Test-Path -LiteralPath $plan.Target -PathType Leaf) {
                [IO.File]::Replace($temporary, $plan.Target, $backup)
            } else {
                [IO.File]::Move($temporary, $plan.Target)
            }
        }
        finally {
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
            if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Force }
        }
    }
    Write-Host "已从本机 Microsoft x64 运行库创建必需的本地别名；这些文件不得重新分发或重新打包。"
}
