[CmdletBinding()]
param([string]$ConfigDir)
$root = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$script = Join-Path $root "scripts\ap-vibe.ps1"
if ($ConfigDir) { & $script -Action stop -ConfigDir $ConfigDir } else { & $script -Action stop }
exit $LASTEXITCODE
