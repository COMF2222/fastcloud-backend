param([string]$Server = 'root@80.47.238.200')
$ErrorActionPreference = 'Stop'
if ($Server -notmatch '^root@([0-9]{1,3}\.){3}[0-9]{1,3}$') {throw 'Use root@public-ip'}
$origin = 'https://' + $Server.Split('@')[1]
$command = @'
cd /root/fastcloud-backend && docker compose -f compose.ip.yaml exec -T fastcloud printenv FASTCLOUD_RELEASE_NOTIFY_TOKEN
'@
$token = & ssh -o BatchMode=yes -o StrictHostKeyChecking=yes $Server $command
if ($LASTEXITCODE -ne 0 -or ($token -join '').Trim() -notmatch '^[a-f0-9]{64}$') {throw 'Could not read the configured notification token'}
$gh = (Get-Command gh -ErrorAction SilentlyContinue).Source
if (-not $gh) {$gh = Join-Path $env:ProgramFiles 'GitHub CLI\gh.exe'}
if (-not (Test-Path -LiteralPath $gh)) {throw 'Install GitHub CLI and run gh auth login'}
$token | & $gh secret set FASTCLOUD_RELEASE_NOTIFY_TOKEN --repo COMF2222/veirune
if ($LASTEXITCODE -ne 0) {throw 'GitHub notification secret update failed'}
$origin | & $gh secret set FASTCLOUD_SERVER_URL --repo COMF2222/veirune
if ($LASTEXITCODE -ne 0) {throw 'GitHub server origin update failed'}
$token = $null
'Updated desktop CI server origin and notification secret without printing credentials.'
