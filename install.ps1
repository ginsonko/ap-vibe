[CmdletBinding()]
param(
    [string]$ProjectRoot = $PSScriptRoot,
    [string]$CodexSessionsRoot = (Join-Path $env:USERPROFILE ".codex\sessions"),
    [string]$ConfigDir,
    [string]$DataDir,
    [string]$ProjectId = "ap-vibe-local",
    [int]$Port = 8765,
    [switch]$NoAutostart,
    [switch]$OrganizeRecent,
    [switch]$SkipClaude,
    [switch]$SkipOpen
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$root = (Resolve-Path (Join-Path $PSScriptRoot ".")).Path
$script = Join-Path $root "scripts\ap-vibe.ps1"
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "AP-Vibe 安装入口缺少 scripts/ap-vibe.ps1" }

# A clean clone may not contain the optional frontend build. Prepare it once
# so the same command works for both maintainers and first-time users.
$studioDir = Join-Path $root "apps\studio"
$studioBuild = Join-Path $studioDir "dist\client\index.html"
if (-not (Test-Path -LiteralPath $studioBuild -PathType Leaf)) {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) { throw "发布包的前端构建缺失；重新获取完整发布包，或使用 Node.js 20.19 或更高版本构建。" }
    Push-Location $studioDir
    try {
        & $npm.Source ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "前端依赖安装失败" }
        & $npm.Source run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败" }
    } finally { Pop-Location }
}

$installedConfigDir = if ($ConfigDir) { $ConfigDir } else { Join-Path $env:LOCALAPPDATA 'AP-Vibe' }
$previousConfigPath = Join-Path $installedConfigDir 'config.json'
if (Test-Path -LiteralPath $previousConfigPath -PathType Leaf) {
    $previous = Get-Content -LiteralPath $previousConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($previous.product -eq 'AP-Vibe') {
        if (-not $PSBoundParameters.ContainsKey('ProjectRoot')) { $ProjectRoot = $previous.project_root }
        if (-not $PSBoundParameters.ContainsKey('ProjectId')) { $ProjectId = $previous.project_id }
        if (-not $PSBoundParameters.ContainsKey('Port')) { $Port = [int]$previous.port }
        if (-not $PSBoundParameters.ContainsKey('DataDir')) { $DataDir = $previous.data_dir }
        if (-not $PSBoundParameters.ContainsKey('CodexSessionsRoot')) { $CodexSessionsRoot = $previous.codex_sessions_root }
    }
}
$arguments = @{
    Action = 'install'
    ProjectRoot = $ProjectRoot
    ProjectId = $ProjectId
    Port = [string]$Port
    AutoMonitor = $true
    AutoOnboardWorkspaces = $true
    CodexSessionsRoot = $CodexSessionsRoot
    SkipOpen = $true
}
if ($ConfigDir) { $arguments.ConfigDir = $ConfigDir }
if ($DataDir) { $arguments.DataDir = $DataDir }
if (-not $NoAutostart) { $arguments.RegisterAutostart = $true }

& $script @arguments
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
$installed = Get-Content -LiteralPath (Join-Path $installedConfigDir 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$integrationRoot = $installed.product_root
& $installed.python (Join-Path $integrationRoot 'tools/install_task_context.py') --config (Join-Path $installedConfigDir 'config.json')
if ($LASTEXITCODE -ne 0) { throw '本地服务已安装，但 Codex 项目资料 Skill 安装失败。请根据错误修复后重新安装。' }
& $installed.python (Join-Path $integrationRoot 'tools/trust_hooks.py') --config (Join-Path $installedConfigDir 'config.json')
if ($LASTEXITCODE -ne 0) { Write-Warning 'Codex hook 信任登记未完成；Skill 仍可使用。请在 Codex 的 /hooks 中检查 AP-Vibe 条目。' }
& $installed.python (Join-Path $integrationRoot 'tools/install_mcp.py') --config (Join-Path $installedConfigDir 'config.json')
if ($LASTEXITCODE -ne 0) { throw '本地服务与 Skill 已安装，MCP 注册未完成；可先使用 Skill，按错误说明修复后重试安装。' }
if (-not $SkipClaude) {
    & $installed.python (Join-Path $integrationRoot 'tools/install_claude.py') --if-available --config (Join-Path $installedConfigDir 'config.json')
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Claude 自动接入未完成；原设置和 Codex 接入保持可用。修复提示的问题后可重跑 tools/install_claude.py。' }
}
if ($OrganizeRecent) {
    $localBase = "http://$($installed.host):$($installed.port)/v1/ap-vibe"
    $catalog = Invoke-RestMethod -Uri "$localBase/organization/sessions?scope=recent_unclassified&limit=1" -Method Get
    if ($catalog -is [string]) { $catalog = $catalog | ConvertFrom-Json }
    if ([int]$catalog.total -eq 0) {
        Write-Host '安装已完成；最近 7 天没有可整理的未归类会话。以后可在工作台「项目与记忆」发起整理。'
    } else {
        $body = @{ request_id = "install-organize-$([guid]::NewGuid())"; scope = 'recent_unclassified' } | ConvertTo-Json
        $prepared = Invoke-RestMethod -Uri "$localBase/organization/prepare" -Method Post -ContentType 'application/json' -Body $body
        if ($prepared -is [string]) { $prepared = $prepared | ConvertFrom-Json }
        $dispatchBody = @{ task_id = $prepared.task.task_id } | ConvertTo-Json
        Invoke-RestMethod -Uri "$localBase/organization/dispatch" -Method Post -ContentType 'application/json' -Body $dispatchBody | Out-Null
        Write-Host '已根据授权开始整理最近 7 天活跃的未归类会话，可在工作台查看进展。'
    }
} else {
    Write-Host '推荐：在工作台「项目与记忆」整理最近 7 天活跃会话。安装时也可在用户授权后使用 -OrganizeRecent。'
}
if (-not $SkipOpen) {
    & $script -Action open -ConfigDir $ConfigDir
}
