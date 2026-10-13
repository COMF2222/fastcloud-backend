# Windows-only restore drill using a fixture, without SSH or task registration.
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\backup-common.ps1"
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('veirune-backup-' + [guid]::NewGuid().ToString('N'))
Set-VeirunePrivateDirectory $temporary
try {
    $fixture = @'
import hashlib,importlib.util,json,sqlite3,sys,zipfile
from pathlib import Path
root,checkout=map(Path,sys.argv[1:])
sys.path.insert(0,str(checkout))
spec=importlib.util.spec_from_file_location('export',checkout/'tools/export_backup.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
db=root/'fixture.sqlite3'
connection=sqlite3.connect(db)
connection.executescript('CREATE TABLE users (id INTEGER PRIMARY KEY); CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT); INSERT INTO users VALUES(42);')
connection.close()
env=root/'.env';env.write_text('SOUNDCLOUD_CLIENT_SECRET=fixture-only\n')
report=module.bundle(db,env,root/'exports')
(root/'report.json').write_text(json.dumps(report))
with zipfile.ZipFile(root/'malformed.zip','w') as z:
    z.writestr('../outside.txt','must never be extracted')
    z.writestr('.env','fixture')
    z.writestr('manifest.json','{}')
'@
    $fixture | & python - $temporary ([IO.Path]::GetFullPath("$PSScriptRoot\.."))
    if ($LASTEXITCODE -ne 0) {throw 'Could not prepare restore fixture'}
    $global:VeiruneBackupFixtureReport = Get-Content -LiteralPath (Join-Path $temporary 'report.json') -Raw
    $report = $global:VeiruneBackupFixtureReport | ConvertFrom-Json
    $global:VeiruneBackupFixtureArchive = Join-Path (Join-Path $temporary 'exports') $report.file
    function global:ssh {$global:LASTEXITCODE = 0; $global:VeiruneBackupFixtureReport}
    function global:scp {Copy-Item -LiteralPath $global:VeiruneBackupFixtureArchive -Destination $args[-1]; $global:LASTEXITCODE = 0}
    $destination = Join-Path $temporary 'offsite'
    & "$PSScriptRoot\backup-to-pc.ps1" -Destination $destination
    $copy = @(Get-ChildItem -LiteralPath $destination -Filter '*.vbackup')
    if ($copy.Count -ne 1) {throw 'Backup file was not published'}
    $cipher = [IO.File]::ReadAllBytes($copy[0].FullName)
    if ([Convert]::ToBase64String($cipher) -eq [Convert]::ToBase64String([IO.File]::ReadAllBytes($global:VeiruneBackupFixtureArchive))) {throw 'Archive was not encrypted'}
    $restore = Join-Path $temporary 'recovered'
    & "$PSScriptRoot\restore-pc-backup.ps1" -File $copy[0].FullName -Directory $restore
    if ((Get-Content -LiteralPath (Join-Path $restore '.env') -Raw) -notmatch 'fixture-only') {throw 'Private configuration did not restore'}
    $cipher[20] = $cipher[20] -bxor 1
    $rejected = $false
    try {Unprotect-VeiruneBytes $cipher | Out-Null} catch {$rejected = $true}
    if (-not $rejected) {throw 'Damaged encrypted backup was accepted'}
    $rejected = $false
    try {Expand-VeiruneBundle ([IO.File]::ReadAllBytes((Join-Path $temporary 'malformed.zip'))) (Join-Path $temporary 'invalid') | Out-Null} catch {$rejected = $true}
    if (-not $rejected -or (Test-Path -LiteralPath (Join-Path $temporary 'outside.txt'))) {throw 'Unsafe archive was extracted'}
    'PASS: encrypted pull, database restore, private config restore, tamper detection and path traversal rejection.'
} finally {
    Remove-Item Function:\ssh,Function:\scp -ErrorAction SilentlyContinue
    Remove-Variable VeiruneBackupFixtureReport,VeiruneBackupFixtureArchive -Scope Global -ErrorAction SilentlyContinue
    Remove-VeiruneTemporaryDirectory $temporary
}
