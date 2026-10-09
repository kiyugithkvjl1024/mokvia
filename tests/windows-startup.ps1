param([string]$SourceRoot = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
$originalLocalAppData = $env:LOCALAPPDATA
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('mokvia-start-test-' + [Guid]::NewGuid().ToString('N'))
$global:Calls = [Collections.Generic.List[string]]::new()
$global:DockerArguments = [Collections.Generic.List[object]]::new()
function global:docker {
    $parts = @($args); $command = $parts -join ' '
    $global:DockerArguments.Add($parts)
    $global:Calls.Add($command); $global:LASTEXITCODE = 0
    if ($command -eq 'info --format {{.OSType}}') { return 'linux' }
    if ($command -eq 'volume ls --format {{.Name}}') { return $global:FakeVolumes }
    if ($command -match '^volume create ') { return $parts[-1] }
    if ($command -match 'compose .* up ') { $global:Started = $true }
}
function global:Get-NetTCPConnection { return }
function global:Invoke-RestMethod { return @{ status = 'ok'; distribution = 'mokvia' } }
function global:Start-Process { param([string]$FilePath, [string[]]$ArgumentList); $global:Calls.Add('Start-Process ' + $FilePath + ' ' + ($ArgumentList -join ' ')) }
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw $Message } }
try {
    New-Item -ItemType Directory -Path $tempRoot | Out-Null
    $env:LOCALAPPDATA = Join-Path $tempRoot 'old-volume'
    $global:FakeVolumes = @('gtd-local_gtd_data'); $global:Started = $false; $global:Calls.Clear()
    $caught = ''
    try { & (Join-Path $SourceRoot 'windows/Start.ps1') } catch { $caught = $_.Exception.Message }
    Assert-True (-not $global:Started) 'Legacy data must stop startup before compose up.'
    Assert-True ($caught -match 'gtd-local_gtd_data') 'Legacy refusal must identify the existing volume.'
    Assert-True (-not ($global:Calls -match '^volume create ')) 'Legacy detection must not create an empty new volume.'

    $env:LOCALAPPDATA = Join-Path $tempRoot 'old-state'
    New-Item -ItemType Directory -Force -Path (Join-Path $env:LOCALAPPDATA 'GtdLocal') | Out-Null
    $global:FakeVolumes = @(); $global:Started = $false; $global:Calls.Clear(); $caught = ''
    try { & (Join-Path $SourceRoot 'windows/Start.ps1') } catch { $caught = $_.Exception.Message }
    Assert-True (-not $global:Started) 'Legacy Windows state must stop startup.'
    Assert-True ($caught -match 'GtdLocal') 'Legacy state refusal must identify the directory.'

    $env:LOCALAPPDATA = Join-Path $tempRoot 'fresh'
    $global:FakeVolumes = @(); $global:Started = $false; $global:Calls.Clear()
    & (Join-Path $SourceRoot 'windows/Start.ps1')
    Assert-True $global:Started 'Fresh install must start.'
    Assert-True ([bool]($global:Calls -match '^volume create mokvia_data$')) 'Fresh install must explicitly provision the mokvia volume.'
    Assert-True ([bool]($global:Calls -match 'compose -p mokvia .* up ')) 'Fresh install must use the mokvia project.'
    Assert-True ([bool]($global:Calls -match 'windows[/\\]Notify.ps1')) 'Fresh install must launch the packaged notification helper.'
    Assert-True (Test-Path (Join-Path $env:LOCALAPPDATA 'mokvia')) 'Windows state must use the mokvia directory.'
    $stateDir = Join-Path $env:LOCALAPPDATA 'mokvia'
    Assert-True (-not (Test-Path (Join-Path $stateDir 'capture-config.json'))) 'Default startup must leave capture disabled.'
    Assert-True (-not [bool]($global:Calls -match 'capture-compose.json')) 'Disabled capture must not add an override.'

    $capture = Join-Path $tempRoot 'Incoming space $literal # test'
    New-Item -ItemType Directory -Path $capture | Out-Null
    $global:Calls.Clear()
    & (Join-Path $SourceRoot 'windows/Start.ps1') -CaptureFolder $capture
    $configFile = Join-Path $stateDir 'capture-config.json'
    $overrideFile = Join-Path $stateDir 'capture-compose.json'
    $config = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
    $override = Get-Content -LiteralPath $overrideFile -Raw | ConvertFrom-Json
    Assert-True ($config.enabled -eq $true -and $config.folder -eq $capture) 'Explicit folder must persist outside the source.'
    $mounts = @($override.services.app.volumes)
    Assert-True ($mounts.Count -eq 1) 'Override must expose only one dedicated capture mount.'
    Assert-True ($mounts[0].target -eq '/capture-inbox' -and $mounts[0].read_only -eq $true) 'Capture mount must be read-only and dedicated.'
    Assert-True ($mounts[0].type -eq 'bind' -and $mounts[0].bind.create_host_path -eq $false) 'Missing host directory must never be auto-created.'
    Assert-True ($mounts[0].source -eq $capture.Replace('$', '$$')) 'Literal dollar paths must escape Compose interpolation.'
    Assert-True (($override.services.app.command -join ' ') -eq 'python3 -m local_runtime serve --container --capture-folder /capture-inbox') 'Capture command must use the explicit container folder.'
    Assert-True ([bool]($global:Calls -match 'capture-compose.json.* up ')) 'Configured startup must use its override.'
    $lastDocker = $global:DockerArguments[$global:DockerArguments.Count - 1]
    Assert-True ($lastDocker[6] -eq $overrideFile) 'Compose override path must be passed as one argument.'
    $global:Calls.Clear()
    & (Join-Path $SourceRoot 'windows/Start.ps1')
    Assert-True ([bool]($global:Calls -match 'capture-compose.json.* up ')) 'Restart without parameters must reuse capture settings.'
    . (Join-Path $SourceRoot 'windows/Common.ps1')
    # Maintenance must still address mokvia/app after OneDrive disappears, without a bind.
    Remove-Item -LiteralPath $capture -Force
    foreach ($operation in @('stop', 'ps', 'logs', 'exec')) {
        $global:Calls.Clear()
        Invoke-Compose -Arguments @($operation, 'app')
        Assert-True (-not [bool]($global:Calls -match 'capture-compose.json')) 'Existing-container maintenance must omit the unavailable capture mount.'
        Assert-True ([bool]($global:Calls -match 'compose -p mokvia .* app$')) 'Maintenance must preserve the same mokvia project/service identity.'
    }
    foreach ($command in @('backup', 'restore')) {
        $global:Calls.Clear()
        Invoke-Compose -Arguments @('run', '--no-deps', 'app', 'python3', '-m', 'local_runtime', $command)
        Assert-True (-not [bool]($global:Calls -match 'capture-compose.json')) 'Explicit backup/restore run must not bind the handoff folder.'
    }
    $global:Calls.Clear()
    Invoke-Compose -Arguments @('run', '--no-deps', '-d', '--name', 'synthetic-maintenance', 'app', 'python3', '-c', 'import time; time.sleep(3600)')
    Assert-True (-not [bool]($global:Calls -match 'capture-compose.json')) 'Packaged backup/restore temporary container must not bind capture.'
    foreach ($arguments in @(@('up', '-d'), @('run', 'app', 'python3', '-m', 'local_runtime', 'serve', '--container'))) {
        $global:Calls.Clear(); $caught = ''
        try { Invoke-Compose -Arguments $arguments | Out-Null } catch { $caught = $_.Exception.Message }
        Assert-True ([bool]$caught -and $global:Calls.Count -eq 0) 'Normal startup/run must refuse a missing configured handoff folder before Docker.'
    }
    New-Item -ItemType Directory -Path $capture | Out-Null
    # Simulate reparse metadata without creating junctions or requiring admin rights.
    $nativeTagFunction = ${function:Get-CaptureReparseTag}
    $global:CaptureItem = [pscustomobject]@{ FullName = $capture; Parent = $null; Attributes = [IO.FileAttributes]::ReparsePoint; LinkType = $null }
    function Get-Item { param([string]$LiteralPath, [switch]$Force); return $global:CaptureItem }
    function Get-CaptureReparseTag { param([string]$Path); return $global:CaptureTag }
    try {
        $global:CaptureTag = [uint32]2415919130
        Assert-True ((Assert-CaptureFolder -Folder $capture) -eq $capture) 'OneDrive cloud reparse points must be allowed.'
        $global:CaptureTag = [uint32]2684354563
        $caught = ''
        try { Assert-CaptureFolder -Folder $capture | Out-Null } catch { $caught = $_.Exception.Message }
        Assert-True ([bool]$caught) 'Junction reparse tags must fail closed.'
        $global:Calls.Clear()
        Invoke-Compose -Arguments @('stop', 'app')
        Assert-True (-not [bool]($global:Calls -match 'capture-compose.json')) 'Stopping must remain safe after the handoff becomes a junction.'
        $global:CaptureTag = [uint32]2415919130
        $global:CaptureItem.LinkType = 'SymbolicLink'
        $caught = ''
        try { Assert-CaptureFolder -Folder $capture | Out-Null } catch { $caught = $_.Exception.Message }
        Assert-True ([bool]$caught) 'Explicit symlink metadata must be rejected.'
    } finally {
        Remove-Item Function:Get-Item
        Set-Item Function:Get-CaptureReparseTag -Value $nativeTagFunction
    }
    foreach ($bad in @($SourceRoot, (Split-Path $SourceRoot -Parent), (Join-Path $tempRoot 'missing'), '.', [IO.Path]::GetPathRoot($capture), '\\synthetic-server\share\Incoming', '//synthetic-server/share/Incoming')) {
        $global:Started = $false; $caught = ''
        try { & (Join-Path $SourceRoot 'windows/Start.ps1') -CaptureFolder $bad } catch { $caught = $_.Exception.Message }
        Assert-True (-not $global:Started -and [bool]$caught) ('Unsafe capture path must fail closed: ' + $bad)
    }
    $caught = ''; $global:Started = $false
    try { & (Join-Path $SourceRoot 'windows/Start.ps1') -CaptureFolder $capture -DisableCapture } catch { $caught = $_.Exception.Message }
    Assert-True (-not $global:Started -and [bool]$caught) 'Contradictory capture parameters must stop startup.'
    $savedConfig = Get-Content -LiteralPath $configFile -Raw
    [IO.File]::WriteAllText($configFile, '{"version":1,"enabled":"yes","folder":"unsafe"}')
    $caught = ''; $global:Started = $false
    try { & (Join-Path $SourceRoot 'windows/Start.ps1') } catch { $caught = $_.Exception.Message }
    Assert-True (-not $global:Started -and [bool]$caught) 'Invalid saved configuration must fail closed.'
    [IO.File]::WriteAllText($configFile, $savedConfig)
    $global:Calls.Clear()
    & (Join-Path $SourceRoot 'windows/Start.ps1') -DisableCapture
    $config = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
    Assert-True ($config.enabled -eq $false) 'Explicit disable must persist.'
    Assert-True (-not (Test-Path -LiteralPath $overrideFile)) 'Disable must remove the stale capture override.'
    Assert-True (-not [bool]($global:Calls -match 'capture-compose.json')) 'Disabled startup must use the base Compose command.'
    $global:Calls.Clear()
    & (Join-Path $SourceRoot 'windows/Start.ps1')
    Assert-True (-not [bool]($global:Calls -match 'capture-compose.json')) 'Disable must survive restart.'
    Write-Host 'Windows startup and capture configuration protection passed (Docker/OS boundaries simulated).'

    # Future NX startup configuration: only synthetic profile + environment references.
    Set-CaptureConfiguration -Disable
    Set-OutlookConfiguration -Disable
    $nxDir=Join-Path $tempRoot 'NXLocal';New-Item -ItemType Directory -Path $nxDir | Out-Null
    $nxFile=Join-Path $nxDir 'profile.json'
    $nxProfile=@{version=1;api_base='https://example.invalid/nx/api';allowed_host='example.invalid';user_id='21';timezone='Asia/Tokyo';granularity=5;required_categories=@();auth_mode='api_key';credential_env='MOKVIA_NX_TEST_CREDENTIAL';company_approved=$true}
    [IO.File]::WriteAllText($nxFile,($nxProfile|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    Set-TimeTrackerConfiguration -File $nxFile
    $nxOverride=Get-TimeTrackerComposeOverride
    $nx=Get-Content -LiteralPath $nxOverride -Raw | ConvertFrom-Json
    Assert-True ($nx.services.app.volumes[0].read_only -eq $true -and $nx.services.app.volumes[0].bind.create_host_path -eq $false) 'NX config mount must be read-only, with no creation.'
    Assert-True ($nx.services.app.environment.MOKVIA_NX_TEST_CREDENTIAL -ceq '${MOKVIA_NX_TEST_CREDENTIAL-}') 'Compose file stores only an environment reference.'
    Assert-True (($nx.services.app.command -join ' ') -match '--timetracker-config /nx-config/profile.json') 'Future NX configuration reaches the explicit runtime argument.'
    Assert-True ($null -eq (Get-TimeTrackerComposeOverride -Maintenance)) 'Backup/restore never injects NX credentials or mounts profile.'
    $combined=Get-Content -LiteralPath (Get-TimeTrackerComposeOverride -WithCapture -WithOutlook) -Raw | ConvertFrom-Json
    Assert-True (($combined.services.app.command -join ' ') -match '--capture-folder /capture-inbox --outlook-folder /outlook-handoff') 'NX override preserves both existing input sources.'
    $nxProfile.api_key='synthetic-forbidden-profile-value'
    [IO.File]::WriteAllText($nxFile,($nxProfile|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    $rejected=$false;try { Get-TimeTrackerComposeOverride | Out-Null } catch { $rejected=$true }
    Assert-True $rejected 'Secret-like profile fields must be refused before Docker.'
    Set-TimeTrackerConfiguration -Disable
    Assert-True ($null -eq (Get-TimeTrackerComposeOverride)) 'Disabling NX startup preserves data and omits credential injection.'
} finally {
    $env:LOCALAPPDATA = $originalLocalAppData
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
