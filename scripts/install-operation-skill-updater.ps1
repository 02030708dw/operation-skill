param(
    [string]$BaseUrl = $env:OPERATION_SKILL_BASE_URL,
    [switch]$InstallCore,
    [switch]$AdoptExistingCore,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$HermesHome = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $HOME ".hermes" }
$UpdaterHome = Join-Path $HermesHome "operation-skill-updater"
$UpdaterPath = Join-Path $UpdaterHome "operation_skill_updater.py"

function Find-Python {
    $candidate = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe"
    if (Test-Path $candidate) { return $candidate }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw "未找到 Hermes Python 或 python.exe，请先安装 Hermes。"
}

function Assert-AllowedUri {
    param(
        [Uri]$Uri,
        [string]$Label
    )
    $Allowed = $Uri.IsAbsoluteUri -and $Uri.Scheme -eq "https"
    if ($env:OPERATION_SKILL_UPDATER_ALLOW_FILE_URL -eq "1" -and $Uri.IsAbsoluteUri -and $Uri.Scheme -eq "file") {
        $Allowed = $true
    }
    if (-not $Allowed) { throw "$Label 只允许 HTTPS 地址。" }
}

function Get-FinalResponseUri {
    param($Response)
    if ($Response -and $Response.BaseResponse) {
        $ResponseUri = $Response.BaseResponse.PSObject.Properties["ResponseUri"]
        if ($ResponseUri -and $ResponseUri.Value) { return [Uri]$ResponseUri.Value }
        $RequestMessage = $Response.BaseResponse.PSObject.Properties["RequestMessage"]
        if ($RequestMessage -and $RequestMessage.Value -and $RequestMessage.Value.RequestUri) {
            return [Uri]$RequestMessage.Value.RequestUri
        }
    }
    throw "无法确认 HTTPS 下载的最终地址。"
}

function Invoke-CheckedDownload {
    param(
        [Uri]$Uri,
        [string]$OutFile,
        [long]$MaximumBytes,
        [string]$Label
    )
    Assert-AllowedUri $Uri $Label
    if ($Uri.Scheme -eq "file") {
        Copy-Item -LiteralPath $Uri.LocalPath -Destination $OutFile -Force
        $FinalUri = $Uri
    }
    else {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $OutFile -PassThru
        $FinalUri = Get-FinalResponseUri $Response
    }
    Assert-AllowedUri $FinalUri "$Label 的最终下载地址"
    $DownloadedBytes = (Get-Item -LiteralPath $OutFile).Length
    if ($DownloadedBytes -gt $MaximumBytes) {
        throw "$Label 超过允许大小 $MaximumBytes 字节。"
    }
    return $DownloadedBytes
}

$Python = Find-Python
if ($InstallCore -and $AdoptExistingCore) {
    throw "-InstallCore 与 -AdoptExistingCore 不能同时使用。"
}
if ($Uninstall) {
    if ($InstallCore -or $AdoptExistingCore) {
        throw "-Uninstall 不能与安装或采用参数同时使用。"
    }
    if (Test-Path $UpdaterPath) {
        & $Python $UpdaterPath --hermes-home $HermesHome uninstall-schedule
        $UninstallExitCode = $LASTEXITCODE
        if ($UninstallExitCode -ne 0) { exit $UninstallExitCode }
    }
    Write-Host "已移除自动更新计划；日志、备份和 Skill 保持不变。"
    exit 0
}

if (-not $BaseUrl) {
    throw "请设置 OPERATION_SKILL_BASE_URL，例如 https://downloads.example.com/operation-skills/stable"
}
$BaseUrl = $BaseUrl.TrimEnd("/")
$BaseUri = [Uri]$BaseUrl
Assert-AllowedUri $BaseUri "OPERATION_SKILL_BASE_URL"
$Temporary = Join-Path ([IO.Path]::GetTempPath()) ("operation-skill-updater-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Temporary | Out-Null
$InstallerExitCode = 0
try {
    $ManifestPath = Join-Path $Temporary "manifest.json"
    $DownloadedUpdater = Join-Path $Temporary "operation_skill_updater.py"
    $ManifestUri = [Uri]"$BaseUrl/manifest.json"
    Invoke-CheckedDownload $ManifestUri $ManifestPath 1048576 "manifest" | Out-Null
    $Manifest = Get-Content -Raw -Encoding UTF8 $ManifestPath | ConvertFrom-Json
    if ($null -eq $Manifest -or $Manifest -isnot [pscustomobject]) { throw "manifest 根节点必须是对象。" }
    if ([string]$Manifest.schemaVersion -cne "1") { throw "manifest schema 无效。" }
    if ([string]$Manifest.repository -cne "02030708dw/operation-skill" -or [string]$Manifest.channel -cne "main") {
        throw "manifest 来源或通道无效。"
    }
    if ([string]$Manifest.commit -cnotmatch "^[0-9a-f]{40}$") { throw "manifest commit 无效。" }
    $ReleaseSequenceValue = $Manifest.releaseSequence
    $IntegerTypeCodes = @(
        [TypeCode]::Byte, [TypeCode]::SByte, [TypeCode]::Int16, [TypeCode]::UInt16,
        [TypeCode]::Int32, [TypeCode]::UInt32, [TypeCode]::Int64, [TypeCode]::UInt64
    )
    if ($null -eq $ReleaseSequenceValue -or $IntegerTypeCodes -notcontains [Type]::GetTypeCode($ReleaseSequenceValue.GetType())) {
        throw "manifest releaseSequence 无效。"
    }
    $ReleaseSequence = [string]$ReleaseSequenceValue
    if ($ReleaseSequence -notmatch "^[1-9][0-9]*$") { throw "manifest releaseSequence 无效。" }
    $UpdaterUrl = [string]$Manifest.updater.url
    $UpdaterUri = [Uri]$UpdaterUrl
    Assert-AllowedUri $UpdaterUri "更新器下载地址"
    $Expected = [string]$Manifest.updater.sha256
    if ($Expected -cnotmatch "^[0-9a-f]{64}$") { throw "manifest updater SHA-256 无效。" }
    $UpdaterSizeValue = $Manifest.updater.size
    if ($null -eq $UpdaterSizeValue -or $IntegerTypeCodes -notcontains [Type]::GetTypeCode($UpdaterSizeValue.GetType())) {
        throw "manifest updater 大小无效。"
    }
    $ExpectedSize = [long]$UpdaterSizeValue
    if ($ExpectedSize -le 0 -or $ExpectedSize -gt 2097152) { throw "manifest updater 大小无效。" }
    $ActualSize = Invoke-CheckedDownload $UpdaterUri $DownloadedUpdater 2097152 "更新器"
    if ($ActualSize -ne $ExpectedSize) { throw "更新器大小校验失败。" }
    $Actual = (Get-FileHash -Algorithm SHA256 $DownloadedUpdater).Hash.ToLowerInvariant()
    if ($Expected -cne $Actual) { throw "更新器 SHA-256 校验失败。" }

    $BootstrapArguments = @(
        $DownloadedUpdater,
        "--hermes-home", $HermesHome,
        "--manifest-url", "$BaseUrl/manifest.json",
        "bootstrap-install",
        "--manifest-file", $ManifestPath
    )
    if ($InstallCore -or $AdoptExistingCore) {
        $BootstrapArguments += "--manage-core"
    }
    & $Python @BootstrapArguments
    $BootstrapExitCode = $LASTEXITCODE
    if ($BootstrapExitCode -ne 0) {
        throw "更新器原子安装失败，退出码 $BootstrapExitCode。"
    }

    $RunArguments = @($UpdaterPath, "--hermes-home", $HermesHome, "--idle-timeout", "0", "run")
    if ($InstallCore) { $RunArguments += "--install-core" }
    if ($AdoptExistingCore) {
        $RunArguments += @("--adopt-existing-core", "--adopt-release-sequence", $ReleaseSequence)
    }
    & $Python @RunArguments
    $RunExitCode = $LASTEXITCODE
    if ($RunExitCode -ne 0) {
        [Console]::Error.WriteLine("首次更新返回退出码 $RunExitCode；仍将注册自动重试计划。")
    }

    & $Python $UpdaterPath --hermes-home $HermesHome install-schedule
    $ScheduleExitCode = $LASTEXITCODE
    if ($ScheduleExitCode -ne 0) {
        [Console]::Error.WriteLine("自动更新计划注册失败，退出码 $ScheduleExitCode。")
    }

    if ($ScheduleExitCode -ne 0) {
        $InstallerExitCode = $ScheduleExitCode
    }
    elseif ($RunExitCode -ne 0) {
        $InstallerExitCode = $RunExitCode
    }
    else {
        Write-Host "运营 Skill 自动更新已安装。"
    }
}
finally {
    Remove-Item -Recurse -Force $Temporary -ErrorAction SilentlyContinue
}
if ($InstallerExitCode -ne 0) { exit $InstallerExitCode }
