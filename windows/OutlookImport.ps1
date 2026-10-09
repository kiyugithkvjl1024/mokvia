# Run only in the company Windows session. No OAuth, credentials file, or Outlook writes.
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$Folder,
 [ValidateSet('classic','graph')][string]$Source='classic',
 [string]$CalendarId,
 [string]$MailboxScope,
 [Security.SecureString]$AccessToken
)
$ErrorActionPreference='Stop'
$handoff=Get-Item -LiteralPath $Folder
if (!$handoff.PSIsContainer -or ($handoff.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Use an existing dedicated local folder, without reparse points.' }
if ($Source -eq 'graph' -and (!$CalendarId -or !$MailboxScope -or !$AccessToken)) { throw 'Graph requires an already-approved calendar, mailbox scope and in-memory token. This helper does not authorize an account.' }
# COM StartUTC/EndUTC contain UTC wall time even when .NET Kind is Unspecified.
function Utc([datetime]$value) { [datetime]::SpecifyKind($value,[DateTimeKind]::Utc).ToString('o') }
function LocalUtc([datetime]$value) { [datetime]::SpecifyKind($value,[DateTimeKind]::Unspecified).ToUniversalTime().ToString('o') }
function GraphGet([string]$url,[string]$token) {
 if (!$url.StartsWith('https://graph.microsoft.com/v1.0/')) { throw 'Unexpected Graph URL' }
 $response=Invoke-WebRequest -Uri $url -Method Get -Headers @{Authorization=('Bearer '+$token);Prefer='outlook.timezone="UTC", IdType="ImmutableId"'} -MaximumRedirection 0 -TimeoutSec 15 -UseBasicParsing
 if ($response.Content.Length -gt 8388608) { throw 'Response too large' }
 $response.Content | ConvertFrom-Json
}
function GraphTime($value) {
 if ($value.timeZone -ne 'UTC') { throw 'Unexpected Graph timezone' }
 [datetimeoffset]::Parse(($value.dateTime+'Z').Replace('ZZ','Z')).ToUniversalTime().ToString('o')
}
function GetGraph($request) {
 $pointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($AccessToken)
 try {
  $token=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
  $calendar=[Uri]::EscapeDataString($CalendarId)
  $path="/v1.0/me/calendars/$calendar/calendarView"
  $url='https://graph.microsoft.com'+$path+'?startDateTime='+[Uri]::EscapeDataString($request.from)+'&endDateTime='+[Uri]::EscapeDataString($request.to)+'&$top=200&$select=id,iCalUId,seriesMasterId,type,originalStart,subject,start,end,isAllDay,isCancelled'
  $seen=@{};$masters=@{};$events=[Collections.Generic.List[object]]::new()
  while ($url) {
   $uri=[Uri]$url
   if ($uri.Scheme -ne 'https' -or $uri.Authority -ne 'graph.microsoft.com' -or $uri.AbsolutePath -ne $path -or $uri.Fragment -or $seen.ContainsKey($url) -or $seen.Count -ge 100) { throw 'Unsafe pagination' }
   $seen[$url]=$true;$page=GraphGet $url $token
   if ($null -eq $page.value) { throw 'Incomplete page' }
   foreach ($event in $page.value) {
    if ($events.Count -ge 5000) { throw 'Too many events' }
    $uid=$event.iCalUId;$occurrence=''
    if ($event.type -in @('occurrence','exception')) {
     if (!$event.seriesMasterId -or !$event.originalStart) { throw 'Missing recurrence identity' }
     if (!$masters.ContainsKey($event.seriesMasterId)) {
      $master=GraphGet ('https://graph.microsoft.com/v1.0/me/events/'+[Uri]::EscapeDataString($event.seriesMasterId)+'?$select=iCalUId') $token
      $masters[$event.seriesMasterId]=$master.iCalUId
     }
     $uid=$masters[$event.seriesMasterId];$occurrence=$event.originalStart
    } elseif ($event.type -ne 'singleInstance') { throw 'Unexpanded series' }
    $events.Add(@{uid=$uid;occurrence=$occurrence;title=$event.subject;start=(GraphTime $event.start);end=(GraphTime $event.end);all_day=[bool]$event.isAllDay;cancelled=[bool]$event.isCancelled})
   }
   $url=$page.'@odata.nextLink'
  }
  @{version=1;complete=$true;source='graph';scope=$MailboxScope+'/'+$CalendarId;from=$request.from;to=$request.to;events=@($events.ToArray())}
 } finally { $token=$null;[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}
function GetClassic($request) {
 $outlook=$null;$namespace=$null;$calendar=$null;$items=$null;$filtered=$null
 try {
  $outlook=New-Object -ComObject Outlook.Application
  if ($outlook.TimeZones.CurrentTimeZone.ID -ne [TimeZoneInfo]::Local.Id) { throw 'Outlook and Windows timezones must match for original occurrence conversion.' }
  $namespace=$outlook.GetNamespace('MAPI');$calendar=$namespace.GetDefaultFolder(9)
  $scope=$calendar.StoreID+'/'+$calendar.EntryID
  $items=$calendar.Items;$items.Sort('[Start]');$items.IncludeRecurrences=$true
  $first=[datetimeoffset]::Parse($request.from).LocalDateTime;$last=[datetimeoffset]::Parse($request.to).LocalDateTime
  $filter="[Start] < '"+$last.ToString('g')+"' AND [End] > '"+$first.ToString('g')+"'"
  $filtered=$items.Restrict($filter);$events=[Collections.Generic.List[object]]::new()
  foreach ($appointment in $filtered) {
   try {
    if ($events.Count -ge 5000) { throw 'Too many occurrences' }
    $occurrence=''
    if ($appointment.IsRecurring) {
     $occurrence=Utc $appointment.StartUTC
     if ($appointment.RecurrenceState -eq 3) {
      # Modified instances keep the original recurrence date, not the moved date.
      $pattern=$appointment.GetRecurrencePattern();$found=$false
      try {
       for ($i=1;$i -le $pattern.Exceptions.Count;$i++) {
        $exception=$pattern.Exceptions.Item($i)
        if (!$exception.Deleted) {
         $changed=$exception.AppointmentItem
         try {
          if ($changed.EntryID -eq $appointment.EntryID -and $changed.StartUTC -eq $appointment.StartUTC) {
           $occurrence=LocalUtc $exception.OriginalDate;$found=$true;break
          }
         } finally { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($changed) }
        }
       }
      } finally { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($pattern) }
      if (!$found) { throw 'Modified occurrence identity is unverified' }
     }
    }
    $events.Add(@{uid=$appointment.GlobalAppointmentID;occurrence=$occurrence;title=$appointment.Subject;start=(Utc $appointment.StartUTC);end=(Utc $appointment.EndUTC);all_day=[bool]$appointment.AllDayEvent;cancelled=($appointment.MeetingStatus -in @(5,7))})
   } finally { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($appointment) }
  }
  @{version=1;complete=$true;source='classic';scope=$scope;from=$request.from;to=$request.to;events=@($events.ToArray())}
 } finally {
  foreach ($object in @($filtered,$items,$calendar,$namespace,$outlook)) { if ($null -ne $object) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($object) } }
 }
}
Write-Host 'Ready for explicit import requests. No periodic calendar synchronization. Ctrl+C stops the helper.'
$mutexName='Local\mokvia-outlook-'+([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($handoff.FullName.ToLowerInvariant())))).Replace('-','')
$mutex=[Threading.Mutex]::new($false,$mutexName)
if (!$mutex.WaitOne(0)) { throw 'An Outlook helper already owns this folder in this Windows session.' }
$handled=@{}
while ($true) {
 foreach ($file in Get-ChildItem -LiteralPath $handoff.FullName -Filter '*.request.json' -File) {
  if ($file.Name -notmatch '^([0-9a-f]{32})\.request\.json$' -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint)) { continue }
  $id=$Matches[1];if ($handled.ContainsKey($id)) { continue };$handled[$id]=$true
  $result=@{request_id=$id;ok=$false;error='collector_failed'}
  try {
   if ($file.Length -gt 4096) { throw 'Request too large' }
   $request=Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
   if ($request.version -ne 1 -or $request.request_id -ne $id -or $request.source -ne $Source) { throw 'Invalid source/request' }
   $duration=[datetimeoffset]::Parse($request.to)-[datetimeoffset]::Parse($request.from)
   if ($duration.TotalSeconds -le 0 -or $duration.TotalDays -gt 93) { throw 'Invalid range' }
   $snapshot=if ($Source -eq 'graph') { GetGraph $request } else { GetClassic $request }
   $result=@{request_id=$id;ok=$true;snapshot=$snapshot}
  } catch { # No upstream error text: it could contain company metadata or credentials.
   Write-Warning 'Import acquisition failed. Existing mokvia data is unchanged.'
  }
  $temporary=Join-Path $handoff.FullName ($id+'.response.tmp');$destination=Join-Path $handoff.FullName ($id+'.response.json')
  [IO.File]::WriteAllText($temporary,($result|ConvertTo-Json -Depth 12 -Compress),[Text.UTF8Encoding]::new($false))
  Move-Item -LiteralPath $temporary -Destination $destination -Force
 }
 Start-Sleep -Milliseconds 100
}
