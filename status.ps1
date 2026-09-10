[CmdletBinding()]
param([string]$ConfigDir)
$root = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$script = Join-Path $root "scripts\ap-vibe.ps1"
if ($ConfigDir) { & $script -Action status -ConfigDir $ConfigDir } else { & $script -Action status }
exit $LASTEXITCODE
