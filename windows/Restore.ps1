param([Parameter(Mandatory=$true)][string]$Archive, [string]$BackupDirectory = (Join-Path $env:LOCALAPPDATA 'mokvia\Backups'))
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-Docker
Assert-NoLegacyData
$inputFile = Assert-OutsideSource -Path $Archive
if (-not (Test-Path -LiteralPath $inputFile -PathType Leaf)) { throw 'Archive file not found.' }
& (Join-Path $PSScriptRoot 'Stop.ps1')
# Preserve current data outside the volume before attempting any restore.
& (Join-Path $PSScriptRoot 'Backup.ps1') -Destination $BackupDirectory
$name = 'mokvia-restore-' + [Guid]::NewGuid().ToString('N')
$staged = '/data/.local-state/' + $name + '.tar'
try {
    Invoke-Compose -Arguments @('run', '--no-deps', '-d', '--name', $name, 'app', 'python3', '-c', 'import time; time.sleep(3600)') | Out-Null
    Invoke-Docker -Arguments @('cp', $inputFile, ($name + ':' + $staged)) | Out-Host
    Invoke-Docker -Arguments @('exec', '--user', '0', $name, 'chown', '10001:10001', $staged) | Out-Null
    Invoke-Docker -Arguments @('exec', '--user', '0', $name, 'chmod', '600', $staged) | Out-Null
    Invoke-Docker -Arguments @('exec', $name, 'python3', '-m', 'local_runtime', 'restore', '--input', $staged) | Out-Host
    Write-Host 'Restore validated and completed. mokvia remains stopped. Start and verify records before continuing.'
} finally {
    Invoke-Docker -Arguments @('exec', $name, 'python3', '-c', 'import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)', $staged) | Out-Null
    Invoke-Docker -Arguments @('rm', '-f', $name) | Out-Null
}
