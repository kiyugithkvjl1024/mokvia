. (Join-Path $PSScriptRoot 'Common.ps1')
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\GtdLocalNotify', [ref]$created)
if (-not $created) { $mutex.Dispose(); exit 0 }
$stopFile = Join-Path $script:StateDir 'notify-stop'
$idFile = Join-Path $script:StateDir 'last-notification-id'
$icon = $null
try {
    Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Text = 'mokvia'
    $icon.Visible = $true
    $lastId = ''
    if (Test-Path -LiteralPath $idFile) { $lastId = (Get-Content -LiteralPath $idFile -Raw).Trim() }
    $headers = @{ 'Origin' = $script:BaseUrl; 'X-GTD-Web' = '1' }
    while (-not (Test-Path -LiteralPath $stopFile)) {
        try {
            Invoke-RestMethod -Method Post -Uri ($script:BaseUrl + '/api/v1/local-notifications/heartbeat') -Headers $headers -ContentType 'application/json' -Body '{}' -TimeoutSec 3 | Out-Null
            $notice = Invoke-RestMethod -Uri ($script:BaseUrl + '/api/v1/local-notifications') -TimeoutSec 3
            if ($notice.id -and $notice.text -and ([string]$notice.id -ne $lastId)) {
                # Persist before display: unknown display result must not trigger a burst.
                $lastId = [string]$notice.id
                Set-Content -LiteralPath $idFile -Value $lastId -Encoding UTF8
                $icon.ShowBalloonTip(10000, 'mokvia', [string]$notice.text, [System.Windows.Forms.ToolTipIcon]::Info)
            }
        } catch {
            # No heartbeat while server is unavailable; browser fallback can recover.
        }
        for ($tick = 0; $tick -lt 100 -and -not (Test-Path -LiteralPath $stopFile); $tick++) {
            [System.Windows.Forms.Application]::DoEvents()
            Start-Sleep -Milliseconds 100
        }
    }
} finally {
    if ($null -ne $icon) { $icon.Visible = $false; $icon.Dispose() }
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
