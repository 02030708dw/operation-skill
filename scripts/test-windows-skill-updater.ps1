param([Parameter(Mandatory = $true)][string]$Python)

$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'This test creates a disposable standard user and is restricted to Windows GitHub Actions.'
}
$testUser = 'hm-ci-' + [guid]::NewGuid().ToString('N').Substring(0, 10)
$testRoot = Join-Path $env:PUBLIC ('hm-updater-ci-' + [guid]::NewGuid().ToString('N'))
$password = ConvertTo-SecureString ('Hm!9a-' + [guid]::NewGuid().ToString('N')) -AsPlainText -Force
$created = $false
try {
    New-LocalUser -Name $testUser -Password $password -AccountNeverExpires | Out-Null
    $created = $true
    $usersGroup = Get-LocalGroup -SID 'S-1-5-32-545'
    Add-LocalGroupMember -Group $usersGroup -Member $testUser
    New-Item -ItemType Directory -Path (Join-Path $testRoot 'scripts') | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $testRoot 'tests') | Out-Null
    Copy-Item (Join-Path $PSScriptRoot 'operation_skill_updater.py') (Join-Path $testRoot 'scripts')
    Copy-Item (Join-Path $PSScriptRoot 'install-operation-skill-updater.ps1') (Join-Path $testRoot 'scripts')
    $repo = Split-Path $PSScriptRoot -Parent
    Copy-Item (Join-Path $repo 'tests/test_operation_skill_updater.py') (Join-Path $testRoot 'tests')
    Copy-Item (Join-Path $repo 'tests/windows_operation_skill_smoke.py') (Join-Path $testRoot 'tests')
    $grant = $env:COMPUTERNAME + '\' + $testUser + ':(OI)(CI)M'
    & icacls.exe $testRoot /grant $grant | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not grant access to the isolated test directory' }
    $credential = New-Object System.Management.Automation.PSCredential(($env:COMPUTERNAME + '\' + $testUser), $password)
    $smoke = Join-Path $testRoot 'tests/windows_operation_skill_smoke.py'
    $stdout = Join-Path $testRoot 'stdout.log'
    $stderr = Join-Path $testRoot 'stderr.log'
    $process = Start-Process -FilePath $Python -ArgumentList ('"' + $smoke + '"') -Credential $credential -LoadUserProfile -WorkingDirectory $testRoot -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -Wait
    if (Test-Path $stdout) { Get-Content -Raw -Encoding UTF8 $stdout | Write-Output }
    if (Test-Path $stderr) { Get-Content -Raw -Encoding UTF8 $stderr | Write-Output }
    if ($process.ExitCode -ne 0) { throw "Standard-user installer smoke test failed: $($process.ExitCode)" }
} finally {
    if ($created) { Remove-LocalUser -Name $testUser -ErrorAction Continue }
    if (Test-Path -LiteralPath $testRoot) { Remove-Item -LiteralPath $testRoot -Recurse -Force }
}
