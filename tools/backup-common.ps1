$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Security
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
function Protect-VeiruneBytes([byte[]]$Bytes) {
    [Security.Cryptography.ProtectedData]::Protect($Bytes, [Text.Encoding]::UTF8.GetBytes('Veirune backup v1'), [Security.Cryptography.DataProtectionScope]::CurrentUser)
}
function Unprotect-VeiruneBytes([byte[]]$Bytes) {
    [Security.Cryptography.ProtectedData]::Unprotect($Bytes, [Text.Encoding]::UTF8.GetBytes('Veirune backup v1'), [Security.Cryptography.DataProtectionScope]::CurrentUser)
}
function Set-VeirunePrivateDirectory([string]$Path) {
    [void][IO.Directory]::CreateDirectory($Path)
    $owner = [Security.Principal.WindowsIdentity]::GetCurrent().User
    # Read only the existing ordinary ACL; a fresh security descriptor can
    # make Windows PowerShell attempt to replace auditing without permission.
    $acl = Get-Acl -LiteralPath $Path
    foreach ($rule in @($acl.GetAccessRules($true,$false,[Security.Principal.SecurityIdentifier]))) {
        $acl.RemoveAccessRuleSpecific($rule)
    }
    $acl.SetOwner($owner)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($owner, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    # Persist only changed sections; Set-Acl can request SACL privileges on an
    # existing protected directory in Windows PowerShell 5.1.
    if ([IO.Directory].GetMethods().Name -contains 'SetAccessControl') {
        [IO.Directory]::SetAccessControl($Path,$acl)
    } else {
        [IO.FileSystemAclExtensions]::SetAccessControl([IO.DirectoryInfo]::new($Path),$acl)
    }
}
function Expand-VeiruneBundle([byte[]]$Bytes, [string]$Directory) {
    if (Test-Path -LiteralPath $Directory) {throw 'Restore into a new directory; existing files are never overwritten.'}
    $stream = [IO.MemoryStream]::new($Bytes, $false)
    $archive = [IO.Compression.ZipArchive]::new($stream, [IO.Compression.ZipArchiveMode]::Read)
    try {
        $allowed = @('approvals.sqlite3', '.env', 'manifest.json')
        if ($archive.Entries.Count -ne 3 -or @($archive.Entries | Where-Object {$_.FullName -notin $allowed}).Count) {throw 'Unexpected backup archive contents'}
        foreach ($name in $allowed) {if (@($archive.Entries | Where-Object {$_.FullName -eq $name}).Count -ne 1) {throw 'Duplicate or missing backup entry'}}
        $total = ($archive.Entries | Measure-Object -Property Length -Sum).Sum
        if ($total -gt 536870912) {throw 'Backup exceeds the recovery size limit'}
        Set-VeirunePrivateDirectory $Directory
        foreach ($entry in $archive.Entries) {
            $input = $entry.Open()
            $output = [IO.File]::Open((Join-Path $Directory $entry.FullName), [IO.FileMode]::CreateNew)
            try {$input.CopyTo($output)} finally {$output.Dispose(); $input.Dispose()}
        }
        $manifest = Get-Content -LiteralPath (Join-Path $Directory 'manifest.json') -Raw | ConvertFrom-Json
        $hash = (Get-FileHash -LiteralPath (Join-Path $Directory 'approvals.sqlite3') -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($manifest.schema -ne 1 -or $hash -ne $manifest.databaseSha256) {throw 'Restored database hash mismatch'}
        $manifest
    } finally {$archive.Dispose(); $stream.Dispose()}
}
function Remove-VeiruneTemporaryDirectory([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $prefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\veirune-backup-'
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {throw 'Refusing to remove an unrelated directory'}
    if (Test-Path -LiteralPath $resolved) {Remove-Item -LiteralPath $resolved -Recurse -Force}
}
