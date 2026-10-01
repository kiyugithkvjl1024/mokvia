. (Join-Path $PSScriptRoot 'Common.ps1')
# Only our helper reads this signal; never terminate a process by a recycled PID.
Set-Content -LiteralPath (Join-Path $script:StateDir 'notify-stop') -Value 'stop' -Encoding UTF8
$probe = New-Object System.Threading.Mutex($false, 'Local\mokviaNotify')
try {
    $acquired = $false
    try { $acquired = $probe.WaitOne(15000) }
    catch [System.Threading.AbandonedMutexException] { $acquired = $true }
    if ($acquired) { $probe.ReleaseMutex() }
    else { throw 'Notification helper did not stop within 15 seconds. Wait before restarting.' }
} finally { $probe.Dispose() }
Invoke-Compose -Arguments @('stop', 'app') | Out-Host
Write-Host 'mokvia stopped. The data volume is retained.'
