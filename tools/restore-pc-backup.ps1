param([Parameter(Mandatory=$true)][string]$File, [Parameter(Mandatory=$true)][string]$Directory)
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\backup-common.ps1"
$cipher = [IO.File]::ReadAllBytes([IO.Path]::GetFullPath($File))
$bytes = Unprotect-VeiruneBytes $cipher
$manifest = Expand-VeiruneBundle $bytes ([IO.Path]::GetFullPath($Directory))
& python "$PSScriptRoot\..\backup.py" check (Join-Path $Directory 'approvals.sqlite3')
if ($LASTEXITCODE -ne 0) {throw 'Restored database failed its SQLite check'}
Write-Output 'Restored the database and .env into a private directory; the live server was not changed.'
