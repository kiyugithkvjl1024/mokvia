param([string]$Destination = (Join-Path $env:LOCALAPPDATA 'mokvia\Backups'))
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Docker
Assert-NoLegacyData
$directory = Assert-OutsideSource -Path $Destination
New-Item -ItemType Directory -Force -Path $directory | Out-Null
# Stop the writer, including the timer loop, before snapshotting. Remains stopped.
& (Join-Path $PSScriptRoot 'Stop.ps1')
$name = 'mokvia-backup-' + [Guid]::NewGuid().ToString('N')
$staged = '/data/.local-state/' + $name + '.tar'
$target = Join-Path $directory ('mokvia-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8) + '.tar')
try {
    # Stage in the data volume: Docker cp cannot read this container tmpfs.
    Invoke-Compose -Arguments @('run', '--no-deps', '-d', '--name', $name, 'app', 'python3', '-c', 'import time; time.sleep(3600)') | Out-Null
    Invoke-Docker -Arguments @('exec', $name, 'python3', '-m', 'local_runtime', 'backup', '--output', $staged) | Out-Host
    # docker cp preserves binary archives on Windows PowerShell 5.
    Invoke-Docker -Arguments @('cp', ($name + ':' + $staged), $target) | Out-Host
    if (-not (Test-Path -LiteralPath $target) -or (Get-Item -LiteralPath $target).Length -eq 0) { throw 'Backup archive was not created.' }
    Write-Host "Backup saved: $target"
    Write-Host 'mokvia remains stopped. Use Start.ps1 when ready.'
} finally {
    Invoke-Docker -Arguments @('exec', $name, 'python3', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)', $staged) | Out-Null
    Invoke-Docker -Arguments @('rm', '-f', $name) | Out-Null
}
