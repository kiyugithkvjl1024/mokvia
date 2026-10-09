Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$script:RepoRoot = Split-Path $PSScriptRoot -Parent
$script:BaseUrl = 'http://localhost:24873'
$script:StateDir = Join-Path $env:LOCALAPPDATA 'mokvia'
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
    $files = @('-f', $script:ComposeFile)
    # stop/ps/logs/exec act on existing project containers and need no host bind.
    # Only explicit backup/restore commands may create a maintenance container
    # without capture; a normal run/up must validate and bind the configured folder.
    $maintenance = $Arguments.Count -gt 0 -and $Arguments[0] -in @('stop', 'ps', 'logs', 'exec')
    if ($Arguments.Count -gt 0 -and $Arguments[0] -eq 'run') {
        $serviceIndex = [Array]::IndexOf($Arguments, 'app')
        if ($serviceIndex -ge 1 -and $Arguments.Count -gt ($serviceIndex + 1)) {
            $command = @($Arguments[($serviceIndex + 1)..($Arguments.Count - 1)])
            $maintenance = ($command.Count -eq 3 -and $command[0] -ceq 'python3' -and $command[1] -ceq '-c' -and $command[2] -ceq 'import time; time.sleep(3600)') -or
                ($command.Count -ge 4 -and $command[0] -ceq 'python3' -and $command[1] -ceq '-m' -and $command[2] -ceq 'local_runtime' -and $command[3] -cin @('backup', 'restore'))
        }
    }
    $captureOverride = Get-CaptureComposeOverride -Maintenance:$maintenance
    if ($captureOverride) { $files += @('-f', $captureOverride) }
    $outlookOverride = Get-OutlookComposeOverride -Maintenance:$maintenance -WithCapture:([bool]$captureOverride)
    if ($outlookOverride) { $files += @('-f', $outlookOverride) }
    $nxOverride = Get-TimeTrackerComposeOverride -Maintenance:$maintenance -WithCapture:([bool]$captureOverride) -WithOutlook:([bool]$outlookOverride)
    if ($nxOverride) { $files += @('-f', $nxOverride) }
    Invoke-Docker -Arguments (@('compose', '-p', 'mokvia') + $files + $Arguments)
}

# Store a non-secret profile path only; never read or save the credential value.
function Assert-TimeTrackerProfile {
    param([string]$File)
    $full=[IO.Path]::GetFullPath($File)
    $folder=Assert-CaptureFolder -Folder (Split-Path $full -Parent)
    if ($full -match '(?i)OneDrive' -or !(Test-Path -LiteralPath $full -PathType Leaf)) { throw 'Use an existing unsynced company-local NX profile outside source/data.' }
    $item=Get-Item -LiteralPath $full -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -gt 65536) { throw 'Unsafe NX profile file.' }
    $profile=Get-Content -LiteralPath $full -Raw | ConvertFrom-Json
    $expected=@('version','api_base','allowed_host','user_id','timezone','granularity','required_categories','auth_mode','credential_env','company_approved')
    $names=@($profile.PSObject.Properties.Name)
    if ($names.Count -ne $expected.Count -or @($names|Where-Object {$_ -notin $expected}).Count) { throw 'NX profile must contain only non-secret configuration fields.' }
    $uri=[Uri]$profile.api_base
    if ($profile.version -ne 1 -or $profile.company_approved -isnot [bool] -or !$profile.company_approved -or
        $uri.Scheme -cne 'https' -or $uri.Port -ne 443 -or $uri.UserInfo -or $uri.Query -or $uri.Fragment -or
        $uri.Host -cne $profile.allowed_host -or !$uri.AbsolutePath.EndsWith('/api') -or
        $profile.user_id -cnotmatch '^[0-9]+$' -or $profile.credential_env -cnotmatch '^MOKVIA_NX_[A-Z0-9_]{1,64}$' -or
        $profile.auth_mode -cnotin @('api_key','bearer') -or $profile.granularity -notin @(5,6,10,15)) { throw 'Invalid or unapproved NX configuration.' }
    return @{file=$full;profile=$profile}
}
function Set-TimeTrackerConfiguration {
    param([string]$File,[switch]$Disable)
    $config=if ($Disable) { @{version=1;enabled=$false} } else { @{version=1;enabled=$true;file=(Assert-TimeTrackerProfile -File $File).file} }
    $path=Join-Path $script:StateDir 'timetracker-reference.json';$temporary=$path+'.tmp'
    [IO.File]::WriteAllText($temporary,($config|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $path -Force
}
function Get-TimeTrackerComposeOverride {
    param([switch]$Maintenance,[switch]$WithCapture,[switch]$WithOutlook)
    if ($Maintenance) { return $null }
    $path=Join-Path $script:StateDir 'timetracker-reference.json'
    if (!(Test-Path -LiteralPath $path)) { return $null }
    $config=Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $names=@($config.PSObject.Properties.Name)
    if ($config.version -ne 1 -or $config.enabled -isnot [bool] -or @($names|Where-Object {$_ -notin @('version','enabled','file')}).Count) { throw 'Invalid NX profile reference.' }
    if (!$config.enabled) { return $null }
    $checked=Assert-TimeTrackerProfile -File $config.file
    $command=@('python3','-m','local_runtime','serve','--container','--timetracker-config','/nx-config/profile.json')
    if ($WithCapture) { $command+=@('--capture-folder','/capture-inbox') }
    if ($WithOutlook) { $command+=@('--outlook-folder','/outlook-handoff') }
    $environment=@{};$name=$checked.profile.credential_env
    $environment[$name]='${'+$name+'-}' # Compose inherits the process environment; file contains only a variable reference.
    $override=@{services=@{app=@{command=$command;environment=$environment;volumes=@(@{type='bind';source=$checked.file.Replace('$','$$');target='/nx-config/profile.json';read_only=$true;bind=@{create_host_path=$false}})}}}
    $output=Join-Path $script:StateDir 'compose.timetracker.json'
    [IO.File]::WriteAllText($output,($override|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
    return $output
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

function Assert-NoLegacyData {
    $volumes = @(Invoke-Docker -Arguments @('volume', 'ls', '--format', '{{.Name}}'))
    $legacy = @($volumes | Where-Object { $_ -match '(^gtd-local_|_gtd_data$)' })
    if ($legacy.Count -gt 0) {
        throw ('Existing legacy data volume(s): ' + ($legacy -join ', ') + '. Startup stopped; no data was copied or deleted. Review these volumes before a clean mokvia install.')
    }
    $legacyState = Join-Path $env:LOCALAPPDATA 'GtdLocal'
    if (Test-Path -LiteralPath $legacyState) {
        throw ('Existing legacy Windows state: ' + $legacyState + '. Startup stopped; keep its data and backups and review it before a clean mokvia install.')
    }
}

# Capture settings live only in the user's external state directory, never in ZIP source.
function Write-AtomicCaptureJson {
    param([string]$Path, [object]$Value)
    Assert-OutsideSource -Path $script:StateDir | Out-Null
    $temporary = Join-Path $script:StateDir ('capture-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
        # PowerShell 5.1 converts $null to an empty string for a .NET string argument.
        # Pass a true null backup path so replacing existing settings stays atomic.
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, [System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}
function Get-CaptureReparseTag {
    param([string]$Path)
    # Windows cloud placeholders are reparse points too. Inspect the tag instead of
    # rejecting every OneDrive directory or permitting arbitrary junctions.
    if (-not ('MokviaCaptureNative' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class MokviaCaptureNative {
    [StructLayout(LayoutKind.Sequential)] public struct TagInfo { public uint Attributes; public uint Tag; }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern SafeFileHandle CreateFileW(string path, uint access, uint share, IntPtr security, uint creation, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetFileInformationByHandleEx(SafeFileHandle file, int infoClass, out TagInfo info, uint size);
    public static uint GetTag(string path) {
        using (SafeFileHandle file = CreateFileW(path, 0x80, 7, IntPtr.Zero, 3, 0x02200000, IntPtr.Zero)) {
            if (file.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error());
            TagInfo info;
            if (!GetFileInformationByHandleEx(file, 9, out info, 8)) throw new Win32Exception(Marshal.GetLastWin32Error());
            return info.Tag;
        }
    }
}
'@
    }
    return [MokviaCaptureNative]::GetTag($Path)
}
function Assert-CaptureFolder {
    param([string]$Folder, [switch]$ShapeOnly)
    if ([string]::IsNullOrWhiteSpace($Folder) -or -not [IO.Path]::IsPathRooted($Folder)) {
        throw 'CaptureFolder must be an explicit absolute existing directory.'
    }
    # Company OneDrive must be a local drive path, never a UNC/network source.
    if ($Folder.StartsWith('\\') -or $Folder.StartsWith('//')) { throw 'Network and device paths are not allowed for CaptureFolder.' }
    if ([IO.Path]::DirectorySeparatorChar -eq '\' -and $Folder -notmatch '^[A-Za-z]:[\\/]') {
        throw 'CaptureFolder must include its local drive.'
    }
    if ($Folder -match '^\\\\[?.]\\') { throw 'Device paths are not allowed for CaptureFolder.' }
    if ([IO.Path]::DirectorySeparatorChar -eq '\') {
        $drive = New-Object IO.DriveInfo([IO.Path]::GetPathRoot($Folder))
        if ($drive.DriveType -eq [IO.DriveType]::Network) { throw 'Mapped network drives are not allowed for CaptureFolder.' }
    }
    $full = [IO.Path]::GetFullPath($Folder).TrimEnd('\', '/')
    $pathRoot = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Folder)).TrimEnd('\', '/')
    if ($full.Equals($pathRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'CaptureFolder must not be a drive or share root.' }
    $source = [IO.Path]::GetFullPath($script:RepoRoot).TrimEnd('\', '/')
    $separator = [IO.Path]::DirectorySeparatorChar
    if ($full.Equals($source, [StringComparison]::OrdinalIgnoreCase) -or
        $full.StartsWith($source + $separator, [StringComparison]::OrdinalIgnoreCase) -or
        $source.StartsWith($full + $separator, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'CaptureFolder must not overlap the source directory.'
    }
    if ($ShapeOnly) { return $full }
    if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw 'CaptureFolder must already exist; no directory is created automatically.' }
    # Inspect only the explicitly chosen directory and its ancestors; never walk children.
    $current = Get-Item -LiteralPath $full -Force
    while ($current) {
        $linkProperty = $current.PSObject.Properties['LinkType']
        if ($linkProperty -and $linkProperty.Value) { throw 'CaptureFolder and its ancestors must not be symlinks or junctions.' }
        if (($current.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            $tag = Get-CaptureReparseTag -Path $current.FullName
            # IO_REPARSE_TAG_CLOUD and CLOUD_1 ... CLOUD_F share this masked value.
            if (([uint32]$tag -band [uint32]4294905855) -ne [uint32]2415919130) {
                throw 'CaptureFolder has an unsupported reparse point. Only Windows cloud placeholders are allowed.'
            }
        }
        $current = $current.Parent
    }
    return $full
}
function Set-CaptureConfiguration {
    param([string]$Folder, [switch]$Disable)
    if ($Disable) { $config = [ordered]@{ version = 1; enabled = $false } }
    else { $config = [ordered]@{ version = 1; enabled = $true; folder = (Assert-CaptureFolder -Folder $Folder) } }
    Write-AtomicCaptureJson -Path (Join-Path $script:StateDir 'capture-config.json') -Value $config
}
function Get-CaptureComposeOverride {
    param([switch]$Maintenance)
    $configPath = Join-Path $script:StateDir 'capture-config.json'
    $overridePath = Join-Path $script:StateDir 'capture-compose.json'
    if (-not (Test-Path -LiteralPath $configPath)) {
        # A surviving override without its authority must not silently enable a mount.
        if (Test-Path -LiteralPath $overridePath) { throw 'Capture override exists without its configuration. Review external mokvia state.' }
        return $null
    }
    Assert-OutsideSource -Path $script:StateDir | Out-Null
    try { $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { throw 'Invalid capture configuration JSON. Review external mokvia state.' }
    if ($null -eq $config) { throw 'Invalid capture configuration.' }
    $names = @($config.PSObject.Properties.Name)
    if ($names -notcontains 'version' -or $names -notcontains 'enabled' -or
        ($config.version -isnot [int] -and $config.version -isnot [long]) -or $config.version -ne 1 -or $config.enabled -isnot [bool]) {
        throw 'Invalid capture configuration version or enabled value.'
    }
    if (@($names | Where-Object { $_ -notin @('version', 'enabled', 'folder') }).Count -gt 0) { throw 'Unexpected capture configuration fields.' }
    if (-not $config.enabled) {
        if ($names -contains 'folder') { throw 'Disabled capture configuration must not contain a folder.' }
        if (Test-Path -LiteralPath $overridePath) { Remove-Item -LiteralPath $overridePath -Force }
        return $null
    }
    if ($names -notcontains 'folder' -or $config.folder -isnot [string]) { throw 'Enabled capture configuration requires a folder.' }
    $folder = Assert-CaptureFolder -Folder $config.folder -ShapeOnly:$Maintenance
    # The same project/service identity comes from the base Compose file. A
    # maintenance command must never require or mount the handoff filesystem.
    if ($Maintenance) { return $null }
    # JSON is valid YAML; serialization prevents YAML quoting/injection. Compose
    # interpolates dollar signs even in JSON strings, so escape them explicitly.
    $override = [ordered]@{
        services = [ordered]@{
            app = [ordered]@{
                command = @('python3', '-m', 'local_runtime', 'serve', '--container', '--capture-folder', '/capture-inbox')
                volumes = @([ordered]@{
                    type = 'bind'; source = $folder.Replace('$', '$$'); target = '/capture-inbox'; read_only = $true
                    bind = [ordered]@{ create_host_path = $false }
                })
            }
        }
    }
    Write-AtomicCaptureJson -Path $overridePath -Value $override
    return $overridePath
}

# Non-secret local folder setting only. No account identity, token, or OAuth setup.
function Set-OutlookConfiguration {
    param([string]$Folder, [switch]$Disable)
    $config = if ($Disable) { @{version=1;enabled=$false} } else { @{version=1;enabled=$true;folder=(Assert-CaptureFolder -Folder $Folder)} }
    if (!$Disable -and $config.folder -match '(?i)OneDrive') { throw 'Outlook handoff must be an unsynced company-local folder.' }
    $path=Join-Path $script:StateDir 'outlook-import.json'
    $temporary=$path+'.tmp'
    [IO.File]::WriteAllText($temporary,($config|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $path -Force
}
function Get-OutlookComposeOverride {
    param([switch]$Maintenance, [switch]$WithCapture)
    $path=Join-Path $script:StateDir 'outlook-import.json'
    if (!(Test-Path -LiteralPath $path)) { return $null }
    $config=Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
    $names=@($config.PSObject.Properties.Name)
    if ($config.version -ne 1 -or $config.enabled -isnot [bool] -or @($names|Where-Object {$_ -notin @('version','enabled','folder')}).Count) { throw 'Invalid Outlook folder configuration.' }
    if (!$config.enabled -or $Maintenance) { return $null }
    $folder=Assert-CaptureFolder -Folder $config.folder
    if ($folder -match '(?i)OneDrive') { throw 'Use an unsynced local Outlook folder.' }
    $command=@('python3','-m','local_runtime','serve','--container','--outlook-folder','/outlook-handoff')
    if ($WithCapture) { $command+=@('--capture-folder','/capture-inbox') }
    $override=@{services=@{app=@{command=$command;volumes=@(@{type='bind';source=$folder.Replace('$','$$');target='/outlook-handoff';read_only=$false;bind=@{create_host_path=$false}})}}}
    $output=Join-Path $script:StateDir 'compose.outlook.json'
    [IO.File]::WriteAllText($output,($override|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
    return $output
}
