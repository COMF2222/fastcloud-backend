param([string]$Server = 'root@80.47.238.200', [string]$Destination = 'D:\VeiruneBackups')
$ErrorActionPreference = 'Stop'
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {throw 'Python is required for database restore validation'}
& "$PSScriptRoot\backup-to-pc.ps1" -Server $Server -Destination $Destination
if ($LASTEXITCODE -ne 0) {throw 'First backup failed; no scheduled task was installed'}
$runtime = Join-Path $env:LOCALAPPDATA 'Veirune\BackupTools'
. "$PSScriptRoot\backup-common.ps1"
Set-VeirunePrivateDirectory $runtime
Copy-Item -LiteralPath "$PSScriptRoot\..\backup.py" -Destination (Join-Path $runtime 'backup.py') -Force
$scripts = Join-Path $runtime 'tools'
Set-VeirunePrivateDirectory $scripts
foreach ($name in @('backup-common.ps1','backup-to-pc.ps1','restore-pc-backup.ps1','run-backup-hidden.vbs')) {Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $scripts $name) -Force}
if ($Server -notmatch '^[a-z_][a-z0-9_-]*@[a-z0-9][a-z0-9.-]+$' -or $Destination.Contains('"')) {throw 'Invalid scheduled task arguments'}
$executable = (Get-Process -Id $PID).Path
$argument = '//B //NoLogo "' + (Join-Path $scripts 'run-backup-hidden.vbs') + '" "' + $executable + '" "' + (Join-Path $scripts 'backup-to-pc.ps1') + '" "' + $Server + '" "' + [IO.Path]::GetFullPath($Destination) + '"'
$hostExecutable = Join-Path $env:SystemRoot 'System32\wscript.exe'
if (-not (Test-Path -LiteralPath $hostExecutable)) {throw 'Windows Script Host is required for the hidden backup launcher'}
$action = New-ScheduledTaskAction -Execute $hostExecutable -Argument $argument
$daily = New-ScheduledTaskTrigger -Daily -At '00:05'
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 1)
$daily.Repetition = $repeat.Repetition
$logon = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'Veirune database backup' -Action $action -Trigger @($daily,$logon) -Principal $principal -Settings $settings -Force | Out-Null
'Installed hourly backups while this Windows account is signed in, plus a backup at sign-in. Missed runs start when available.'
