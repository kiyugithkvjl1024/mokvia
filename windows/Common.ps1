Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:RepoRoot = Split-Path $PSScriptRoot -Parent
$script:BaseUrl = 'http://localhost:24873'
$script:StateDir = Join-Path $env:LOCALAPPDATA 'GtdLocal'
$script:ComposeFile = Join-Path $script:RepoRoot 'compose.yaml'
New-Item -ItemType Directory -Force -Path $script:StateDir | Out-Null
function Invoke-Docker {
    param([string[]]$Arguments)
    $result = & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed (exit $LASTEXITCODE)." }
    return $result
}
function Invoke-Compose {
    param([string[]]$Arguments)
    Invoke-Docker -Arguments (@('compose', '-p', 'gtd-local', '-f', $script:ComposeFile) + $Arguments)
}
function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Install company-approved Docker with Linux containers first.' }
    Invoke-Docker -Arguments @('info', '--format', '{{.OSType}}') | ForEach-Object {
        if ($_ -ne 'linux') { throw 'Docker must be using Linux containers.' }
    }
    Invoke-Docker -Arguments @('compose', 'version') | Out-Null
}
function Assert-PortOwnership {
    $listener = @(Get-NetTCPConnection -LocalPort 24873 -State Listen -ErrorAction SilentlyContinue)
    if ($listener.Count -eq 0) { return }
    $container = @(Invoke-Compose -Arguments @('ps', '-q', 'app'))
    if ($container.Count -ne 1 -or -not $container[0]) { throw 'Port 24873 is occupied. Leave the other process running and resolve the conflict.' }
    $mapping = Invoke-Docker -Arguments @('inspect', '--format', '{{json .NetworkSettings.Ports}}', $container[0])
    if ($mapping -notmatch '"HostIp":"127.0.0.1","HostPort":"24873"') {
        throw 'Port 24873 ownership or loopback binding could not be verified.'
    }
}
function Assert-OutsideSource {
    param([string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetFullPath($script:RepoRoot).TrimEnd('\', '/')
    if ($full.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or $full.StartsWith($root + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Keep company data and backups outside the source directory.'
    }
    return $full
}
