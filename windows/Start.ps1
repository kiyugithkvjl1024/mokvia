# Run from a normal logged-in Windows session. Never changes execution policy.
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Docker
Assert-PortOwnership
Invoke-Compose -Arguments @('up', '-d', '--build') | Out-Host
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri ($script:BaseUrl + '/api/v1/health') -TimeoutSec 2
        if ($health.status -eq 'ok' -and $health.distribution -eq 'gtd-local') {
            $ready = $true
            break
        }
    } catch { Start-Sleep -Seconds 1 }
}
if (-not $ready) { throw 'GTD did not become healthy. Inspect docker compose logs; no unrelated process was stopped.' }
# A helper blocked by policy produces no heartbeat: the open browser takes over.
$notifyScript = Join-Path $PSScriptRoot 'Notify.ps1'
try {
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile', '-WindowStyle', 'Hidden', '-File', ('"' + $notifyScript + '"')) -ErrorAction Stop | Out-Null
} catch { Write-Warning 'Native notification helper could not start. Use the open browser notification fallback.' }
Start-Process $script:BaseUrl
Write-Host 'GTD is ready. Allow browser notifications when prompted. Keep the tab open if native notifications are unavailable.'
