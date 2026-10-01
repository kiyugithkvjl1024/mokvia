param([string]$SourceRoot = (Split-Path $PSScriptRoot -Parent))
$ErrorActionPreference = 'Stop'
$originalLocalAppData = $env:LOCALAPPDATA
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('mokvia-start-test-' + [Guid]::NewGuid().ToString('N'))
$global:Calls = [Collections.Generic.List[string]]::new()
function global:docker {
    $parts = @($args); $command = $parts -join ' '
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
    Write-Host 'Windows startup protection passed (Docker/OS boundaries simulated).'
} finally {
    $env:LOCALAPPDATA = $originalLocalAppData
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}
