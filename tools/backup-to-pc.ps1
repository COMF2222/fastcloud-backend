param([string]$Server = 'root@80.47.238.200', [string]$Destination = 'D:\VeiruneBackups')
$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\backup-common.ps1"
if ($Server -notmatch '^[a-z_][a-z0-9_-]*@[a-z0-9][a-z0-9.-]+$') {throw 'Invalid SSH destination'}
$destinationPath = [IO.Path]::GetFullPath($Destination)
Set-VeirunePrivateDirectory $destinationPath
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('veirune-backup-' + [guid]::NewGuid().ToString('N'))
Set-VeirunePrivateDirectory $temporary
try {
    $raw = & ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 $Server 'python3 /root/fastcloud-backend/tools/export_backup.py'
    if ($LASTEXITCODE -ne 0) {throw 'Could not create the remote backup. Check SSH and the deployed backend.'}
    $report = ($raw -join "`n") | ConvertFrom-Json
    if ($report.file -notmatch '^veirune-\d{8}T\d{6}Z-[a-f0-9]{8}\.zip$' -or $report.sha256 -notmatch '^[a-f0-9]{64}$') {throw 'Invalid backup response'}
    $zip = Join-Path $temporary 'input.zip'
    & scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=15 "${Server}:/root/.local/share/veirune/exports/$($report.file)" $zip
    if ($LASTEXITCODE -ne 0) {throw 'Could not download the checked backup'}
    if ((Get-Item -LiteralPath $zip).Length -gt 536870912 -or (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $report.sha256) {throw 'Downloaded backup hash mismatch'}
    $bytes = [IO.File]::ReadAllBytes($zip)
    $drill = Join-Path $temporary 'restore-drill'
    $manifest = Expand-VeiruneBundle $bytes $drill
    & python "$PSScriptRoot\..\backup.py" check (Join-Path $drill 'approvals.sqlite3') | Out-Null
    if ($LASTEXITCODE -ne 0) {throw 'Local SQLite integrity check failed'}
    & python "$PSScriptRoot\..\backup.py" restore (Join-Path $drill 'approvals.sqlite3') --target (Join-Path $drill 'trial.sqlite3') | Out-Null
    if ($LASTEXITCODE -ne 0) {throw 'Local restore drill failed'}
    $cipher = Protect-VeiruneBytes $bytes
    if ([Convert]::ToBase64String($bytes) -cne [Convert]::ToBase64String((Unprotect-VeiruneBytes $cipher))) {throw 'Encrypted backup recovery failed'}
    $name = [IO.Path]::GetFileNameWithoutExtension($report.file) + '.vbackup'
    $target = Join-Path $destinationPath $name
    $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew)
    try {$output.Write($cipher,0,$cipher.Length)} finally {$output.Dispose()}
    # Keep eight recent copies plus daily recovery points for thirty days, within 2 GiB.
    $files = @(Get-ChildItem -LiteralPath $destinationPath -File -Filter 'veirune-*.vbackup' | Sort-Object LastWriteTimeUtc -Descending)
    $dates = @{}; $total = 0L; $kept = 0
    for ($index=0; $index -lt $files.Count; $index++) {
        $file = $files[$index]; $day = $file.LastWriteTimeUtc.ToString('yyyy-MM-dd')
        $wanted = $index -lt 8 -or ($file.LastWriteTimeUtc -gt [DateTime]::UtcNow.AddDays(-30) -and -not $dates.ContainsKey($day))
        if ($index -eq 0 -or ($wanted -and $total + $file.Length -le 2147483648)) {$total += $file.Length; $kept++; $dates[$day] = $true}
        else {
            if (-not $file.FullName.StartsWith($destinationPath.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)) {throw 'Unexpected retention path'}
            Remove-Item -LiteralPath $file.FullName
        }
    }
    [ordered]@{savedAt=[DateTime]::UtcNow.ToString('o');file=$name;server=$Server;retained=$kept;counts=$manifest.counts} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destinationPath 'last-success.json') -Encoding UTF8
    Write-Output "Backup saved and restored successfully: $target"
} catch {
    Set-Content -LiteralPath (Join-Path $destinationPath 'last-error.txt') -Value ([DateTime]::UtcNow.ToString('o') + ' Backup failed; check SSH, backend health and Python availability.') -Encoding UTF8
    throw
} finally {Remove-VeiruneTemporaryDirectory $temporary}
