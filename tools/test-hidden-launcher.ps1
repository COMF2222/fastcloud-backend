$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\backup-common.ps1"
$temporary = Join-Path ([IO.Path]::GetTempPath()) ('veirune-backup-' + [guid]::NewGuid().ToString('N'))
Set-VeirunePrivateDirectory $temporary
try {
    $fixture = Join-Path $temporary 'fixture script.ps1'
    $marker = Join-Path $temporary 'result.json'
    @'
param([string]$Server,[string]$Destination)
[IO.File]::WriteAllText($Destination,(@{server=$Server;nonInteractive=[Environment]::GetCommandLineArgs() -contains '-NonInteractive';hidden=[Environment]::GetCommandLineArgs() -contains 'Hidden'} | ConvertTo-Json))
exit 0
'@ | Set-Content -LiteralPath $fixture -Encoding UTF8
    $executable = (Get-Process -Id $PID).Path
    $hostExecutable = Join-Path $env:SystemRoot 'System32\wscript.exe'
    $argsText = '//B //NoLogo "' + "$PSScriptRoot\run-backup-hidden.vbs" + '" "' + $executable + '" "' + $fixture + '" "root@80.47.238.200" "' + $marker + '"'
    $process = Start-Process -FilePath $hostExecutable -ArgumentList $argsText -WindowStyle Hidden -Wait -PassThru
    if ($process.ExitCode -ne 0) {throw 'Hidden launcher failed'}
    $result = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($result.server -ne 'root@80.47.238.200' -or -not $result.nonInteractive -or -not $result.hidden) {throw 'Hidden launcher changed the backup arguments'}
    'PASS: windowless host starts hidden, non-interactive PowerShell and preserves Unicode/space paths.'
} finally {Remove-VeiruneTemporaryDirectory $temporary}
