# Run from a normal logged-in Windows session. Never changes execution policy.
param([string]$CaptureFolder, [switch]$DisableCapture, [string]$OutlookFolder, [switch]$DisableOutlook, [string]$TimeTrackerConfigFile, [switch]$DisableTimeTracker)
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Docker
Assert-NoLegacyData
if ($PSBoundParameters.ContainsKey('CaptureFolder') -and $DisableCapture) {
    throw 'Use CaptureFolder or DisableCapture, not both.'
}
if ($PSBoundParameters.ContainsKey('CaptureFolder')) { Set-CaptureConfiguration -Folder $CaptureFolder }
elseif ($DisableCapture) { Set-CaptureConfiguration -Disable }
if ($PSBoundParameters.ContainsKey('OutlookFolder') -and $DisableOutlook) { throw 'Use OutlookFolder or DisableOutlook, not both.' }
if ($PSBoundParameters.ContainsKey('OutlookFolder')) { Set-OutlookConfiguration -Folder $OutlookFolder }
elseif ($DisableOutlook) { Set-OutlookConfiguration -Disable }
if ($PSBoundParameters.ContainsKey('TimeTrackerConfigFile') -and $DisableTimeTracker) { throw 'Use TimeTrackerConfigFile or DisableTimeTracker, not both.' }
if ($PSBoundParameters.ContainsKey('TimeTrackerConfigFile')) { Set-TimeTrackerConfiguration -File $TimeTrackerConfigFile }
elseif ($DisableTimeTracker) { Set-TimeTrackerConfiguration -Disable }
Get-CaptureComposeOverride | Out-Null
Get-OutlookComposeOverride | Out-Null
Get-TimeTrackerComposeOverride | Out-Null
Assert-PortOwnership
Invoke-Docker -Arguments @('volume', 'create', 'mokvia_data') | Out-Null
Invoke-Compose -Arguments @('up', '-d', '--build') | Out-Host
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri ($script:BaseUrl + '/api/v1/health') -TimeoutSec 2
        if ($health.status -eq 'ok' -and $health.distribution -eq 'mokvia') {
            $ready = $true
            break
        }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) { throw 'mokvia did not become healthy. Inspect docker compose logs; no unrelated process was stopped.' }
# A helper blocked by policy produces no heartbeat: the open browser takes over.
$notifyScript = Join-Path $PSScriptRoot 'Notify.ps1'
try {
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-WindowStyle', 'Hidden', '-File', ('"' + $notifyScript + '"')) -ErrorAction Stop | Out-Null
} catch { Write-Warning 'Native notification helper could not start. Use the open browser notification fallback.' }
Start-Process $script:BaseUrl
Write-Host 'mokvia is ready. Allow browser notifications when prompted. Keep the tab open if native notifications are unavailable.'
