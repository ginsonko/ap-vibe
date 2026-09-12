[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("install", "start", "status", "open", "launch", "install-desktop-launcher", "stop", "uninstall-autostart", "apply-update")]
    [string]$Action,

    [string]$ConfigDir,
    [string]$DataDir,
    [string]$ProjectRoot,
    [string]$ProjectId = "ap-vibe-local",
    [string]$Port = "8765",
    [string]$BindHost = "127.0.0.1",
    [string]$Python,
    [string]$StudioDir,
    [string]$CandidateRoot,
    [string]$CodexSessionsRoot,
    [switch]$AutoMonitor,
    [switch]$AutoOnboardWorkspaces,
    [switch]$RegisterAutostart,
    [switch]$SkipOpen,
    [string]$StartupDir,
    [int]$HealthTimeoutSeconds = 20
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$script:InvocationParameters = @{} + $PSBoundParameters
Set-StrictMode -Version 2.0

$script:ProductRoot = (Resolve-Path (Join-Path $PSScriptRoot ".." )).Path
$script:PackageRoot = $script:ProductRoot
$script:ScriptPath = (Resolve-Path $MyInvocation.MyCommand.Path).Path
$script:ConfigSchema = "ap-vibe.lifecycle.v1"
$script:AutostartMarker = "AP-VIBE-AUTOSTART-V1"
. (Join-Path $PSScriptRoot 'process-identity.ps1')

function Convert-ToFullPath {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "path_empty" }
    $expanded = [Environment]::ExpandEnvironmentVariables($Value.Trim())
    try {
        return [IO.Path]::GetFullPath($expanded)
    } catch {
        throw "path_invalid"
    }
}

function Get-DefaultConfigDir {
    if (-not [string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
        return (Join-Path $env:LOCALAPPDATA "AP-Vibe")
    }
    return (Join-Path $env:USERPROFILE "AppData\Local\AP-Vibe")
}

function Get-ResolvedConfigDir {
    if ([string]::IsNullOrWhiteSpace($ConfigDir)) { return (Convert-ToFullPath (Get-DefaultConfigDir)) }
    return (Convert-ToFullPath $ConfigDir)
}

function Get-ConfigPath { return (Join-Path (Get-ResolvedConfigDir) "config.json") }
function Get-ReceiptPath { return (Join-Path (Get-ResolvedConfigDir) "runtime-receipt.json") }
function Get-LogDir { return (Join-Path (Get-ResolvedConfigDir) "logs") }
function Get-AutostartDir {
    if (-not [string]::IsNullOrWhiteSpace($StartupDir)) { return (Convert-ToFullPath $StartupDir) }
    return [Environment]::GetFolderPath("Startup")
}
function Get-AutostartPath { return (Join-Path (Get-AutostartDir) "AP-Vibe-Autostart.cmd") }

function Get-DesktopDir {
    $desktop = [Environment]::GetFolderPath("Desktop")
    if ([string]::IsNullOrWhiteSpace($desktop)) { $desktop = Join-Path $env:USERPROFILE "Desktop" }
    return (Convert-ToFullPath $desktop)
}

function Get-DesktopShortcutPath { return (Join-Path (Get-DesktopDir) "AP-Vibe.lnk") }

function Get-LauncherIconPath {
    $candidate = Join-Path $script:ProductRoot "assets\ap-vibe.ico"
    if (Test-Path -LiteralPath $candidate -PathType Leaf) { return (Convert-ToFullPath $candidate) }
    return $null
}

function Get-PowerShellExecutable {
    $preferred = @(
        (Join-Path $PSHOME "powershell.exe"),
        (Join-Path $PSHOME "pwsh.exe"),
        (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe")
    )
    foreach ($candidate in ($preferred | Select-Object -Unique)) {
        if (-not [string]::IsNullOrWhiteSpace($candidate) -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return (Convert-ToFullPath $candidate) }
    }
    $command = Get-Command powershell.exe -ErrorAction SilentlyContinue
    if ($command) { return (Convert-ToFullPath $command.Source) }
    $command = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if ($command) { return (Convert-ToFullPath $command.Source) }
    throw "powershell_executable_not_found"
}

function Write-AtomicJson {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $tmp = "$Path.tmp.$PID.$([guid]::NewGuid().ToString('N'))"
    $json = $Value | ConvertTo-Json -Depth 24
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Read-JsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    try {
        return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        throw "json_read_failed:$Path"
    }
}

function Set-PropertyValue {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [AllowNull()]$Value
    )
    if ($null -ne $Object.PSObject.Properties[$Name]) {
        $Object.PSObject.Properties[$Name].Value = $Value
    } else {
        $Object | Add-Member -NotePropertyName $Name -NotePropertyValue $Value -Force
    }
}

function Normalize-Text {
    param([AllowNull()][string]$Value)
    if ($null -eq $Value) { return "" }
    return (($Value -replace '"', '' -replace '/', '\').Trim().ToLowerInvariant())
}

function Test-SamePath {
    param([AllowNull()][string]$Left, [AllowNull()][string]$Right)
    if ([string]::IsNullOrWhiteSpace($Left) -or [string]::IsNullOrWhiteSpace($Right)) { return $false }
    return ((Normalize-Text (Convert-ToFullPath $Left)) -eq (Normalize-Text (Convert-ToFullPath $Right)))
}

function Get-ProcessRecord {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return $null }
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) { return $null }
    $cim = Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId) -ErrorAction SilentlyContinue
    $imagePath = if ($cim) { [string]$cim.ExecutablePath } else { [string]$process.Path }
    if ([string]::IsNullOrWhiteSpace($imagePath)) { $imagePath = Get-LimitedProcessImage $ProcessId }
    [pscustomobject]@{
        pid = $ProcessId
        name = [string]$process.ProcessName
        path = $imagePath
        command_line = if ($cim) { [string]$cim.CommandLine } else { "" }
        start_time = try { $process.StartTime.ToUniversalTime().ToString("o") } catch { $null }
    }
}

function Get-ListeningPid {
    param([int]$PortNumber)
    try {
        $connections = Get-NetTCPConnection -State Listen -LocalPort $PortNumber -ErrorAction Stop
        $candidate = $connections | Where-Object { $_.LocalAddress -in @("127.0.0.1", "::1", "0.0.0.0", "::") } | Select-Object -First 1
        if ($candidate) { return [int]$candidate.OwningProcess }
    } catch {
        # Windows PowerShell installations without NetTCPConnection use netstat.
    }
    try {
        $lines = netstat.exe -ano -p tcp 2>$null
        foreach ($line in $lines) {
            if ($line -match "LISTENING\s+(\d+)\s*$" -and $line -match ":$PortNumber\s") {
                return [int]$matches[1]
            }
        }
    } catch { }
    return $null
}

function Get-EffectivePort {
    try { return [int]$Port } catch { throw "port_invalid" }
}

function Select-AvailableServicePort {
    param([Parameter(Mandatory = $true)]$Config)
    $ownerPid = Get-ListeningPid ([int]$Config.port)
    if (-not $ownerPid -or (Test-OwnedProcess $Config $ownerPid)) { return }
    $address = if ($Config.host -eq "::1") { [Net.IPAddress]::IPv6Loopback } else { [Net.IPAddress]::Loopback }
    $listener = [Net.Sockets.TcpListener]::new($address, 0)
    try {
        $listener.Start()
        $availablePort = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    } finally { $listener.Stop() }
    if ($null -eq $Config.PSObject.Properties["requested_port"]) {
        Set-PropertyValue $Config "requested_port" ([int]$Config.port)
    }
    $Config.port = $availablePort
}

function Get-PythonLauncher {
    param([AllowNull()][string]$Requested)
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($Requested)) { $candidates += $Requested }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) { $candidates += $command.Source }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { $candidates += $py.Source }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        try {
            $resolved = (Get-Command $candidate -ErrorAction Stop).Source
            $prefix = @()
            if ([IO.Path]::GetFileName($resolved).ToLowerInvariant() -eq "py.exe") { $prefix = @("-3") }
            $version = & $resolved @prefix --version 2>&1 | Out-String
            if ($version -match "Python\s+(\d+)\.(\d+)") {
                $major = [int]$matches[1]; $minor = [int]$matches[2]
                if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) {
                    return [pscustomobject]@{ path = $resolved; prefix = $prefix; version = $version.Trim() }
                }
            }
        } catch { }
    }
    throw "python_311_not_found"
}

function Get-ExistingConfig {
    $path = Get-ConfigPath
    return (Read-JsonFile $path)
}

function New-Configuration {
    $portNumber = Get-EffectivePort
    if ($portNumber -lt 1024 -or $portNumber -gt 65535) { throw "port_out_of_bounds" }
    $projectRootValue = if ([string]::IsNullOrWhiteSpace($ProjectRoot)) { $script:ProductRoot } else { $ProjectRoot }
    $projectRootValue = Convert-ToFullPath $projectRootValue
    if (-not (Test-Path -LiteralPath $projectRootValue -PathType Container)) { throw "project_root_not_found" }
    $dataValue = if ([string]::IsNullOrWhiteSpace($DataDir)) { Join-Path (Get-ResolvedConfigDir) "data" } else { $DataDir }
    $dataValue = Convert-ToFullPath $dataValue
    $studioValue = if ([string]::IsNullOrWhiteSpace($StudioDir)) { Join-Path $script:ProductRoot "apps\studio\dist\client" } else { $StudioDir }
    $studioValue = Convert-ToFullPath $studioValue
    if (-not (Test-Path -LiteralPath (Join-Path $studioValue "index.html") -PathType Leaf)) { throw "studio_build_not_found" }
    $python = Get-PythonLauncher $Python
    $sessionsValue = $null
    if (-not [string]::IsNullOrWhiteSpace($CodexSessionsRoot)) {
        $sessionsValue = Convert-ToFullPath $CodexSessionsRoot
        if (-not (Test-Path -LiteralPath $sessionsValue -PathType Container)) { throw "codex_sessions_root_not_found" }
    }
    $releaseVersion = $null
    $versionFile = Join-Path $script:ProductRoot 'ap-vibe-version.json'
    if (Test-Path -LiteralPath $versionFile -PathType Leaf) {
        $releaseMetadata = Read-JsonFile $versionFile
        if ($releaseMetadata.product -eq 'AP-Vibe' -and $releaseMetadata.version -match '^v\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?$') { $releaseVersion = [string]$releaseMetadata.version }
    }
    [pscustomobject]@{
        schema = $script:ConfigSchema
        product = "AP-Vibe"
        installed_version = $releaseVersion
        product_root = $script:ProductRoot
        script_path = $script:ScriptPath
        python = $python.path
        python_prefix = @($python.prefix)
        python_version = $python.version
        host = $BindHost
        port = $portNumber
        data_dir = $dataValue
        project_root = $projectRootValue
        project_id = $ProjectId
        studio_dir = $studioValue
        codex_sessions_root = $sessionsValue
        auto_monitor = [bool]$AutoMonitor
        auto_onboard_workspaces = [bool]$AutoOnboardWorkspaces
        config_dir = (Get-ResolvedConfigDir)
        state = "configured"
        updated_at = [DateTime]::UtcNow.ToString("o")
    }
}

function Assert-ConfigIdentity {
    param([Parameter(Mandatory = $true)]$Config)
    if ([string]$Config.schema -ne $script:ConfigSchema -or [string]$Config.product -ne "AP-Vibe") { throw "config_schema_mismatch" }
    if (-not (Test-SamePath ([string]$Config.config_dir) (Get-ResolvedConfigDir))) { throw "config_directory_mismatch" }
    if ([string]$Config.host -notin @("127.0.0.1", "localhost", "::1")) { throw "host_not_loopback" }
    if ([int]$Config.port -lt 1024 -or [int]$Config.port -gt 65535) { throw "port_out_of_bounds" }
}

function Test-OwnedProcess {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][int]$ProcessId
    )
    $record = Get-ProcessRecord $ProcessId
    if ($null -eq $record) { return $null }
    $cmd = Normalize-Text $record.command_line
    $module = "ap_mind.studio_server"
    $portToken = "--port $([int]$Config.port)"
    $projectToken = "--codex-project-id $(Normalize-Text ([string]$Config.project_id))"
    $dataToken = Normalize-Text ([string]$Config.data_dir)
    $rootToken = Normalize-Text ([string]$Config.project_root)
    $moduleOk = $cmd.Contains($module)
    $portOk = $cmd.Contains((Normalize-Text $portToken))
    $projectOk = $cmd.Contains($projectToken)
    $dataOk = $cmd.Contains($dataToken)
    $rootOk = $cmd.Contains($rootToken)
    if ($moduleOk -and $portOk -and $projectOk -and $dataOk -and $rootOk) {
        return $record
    }
    if ([string]::IsNullOrWhiteSpace($cmd)) {
        $receipt = Read-JsonFile (Get-ReceiptPath)
        $listener = Get-ListeningPid ([int]$Config.port)
        if ($listener -and (Test-ReceiptProcessIdentity $Config $record $receipt $listener)) { return $record }
    }
    return $null
}

function Invoke-Health {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [int]$TimeoutSec = 2
    )
    $url = "http://$([string]$Config.host):$([int]$Config.port)/v1/health"
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec $TimeoutSec -ErrorAction Stop
        $body = $response.Content | ConvertFrom-Json
        return [pscustomobject]@{ reachable = $true; http_status = [int]$response.StatusCode; body = $body; error = $null }
    } catch {
        $status = $null
        $body = $null
        try {
            if ($_.Exception.Response) {
                $status = [int]$_.Exception.Response.StatusCode
                $reader = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
                $text = $reader.ReadToEnd(); $reader.Dispose()
                if ($text) { $body = $text | ConvertFrom-Json }
            }
        } catch { }
        return [pscustomobject]@{ reachable = $false; http_status = $status; body = $body; error = $_.Exception.Message }
    }
}

function Wait-Healthy {
    param([Parameter(Mandatory = $true)]$Config, [int]$TimeoutSec)
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, [Math]::Min($TimeoutSec, 120)))
    $last = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        $last = Invoke-Health $Config 2
        if ($last.reachable -and $last.body.status -eq "ok") { return $last }
        Start-Sleep -Milliseconds 400
    }
    return $last
}

function New-CommandArguments {
    param([Parameter(Mandatory = $true)]$Config)
    $args = @()
    $args += @($Config.python_prefix)
    $args += @("-m", "ap_mind.studio_server", "--host", [string]$Config.host, "--port", [string]$Config.port)
    $args += @("--data-dir", [string]$Config.data_dir, "--studio-dir", [string]$Config.studio_dir)
    $args += @("--codex-project-id", [string]$Config.project_id, "--project-root", [string]$Config.project_root, "--logic-root", [string]$Config.project_root)
    if (-not [string]::IsNullOrWhiteSpace([string]$Config.codex_sessions_root)) { $args += @("--codex-sessions-root", [string]$Config.codex_sessions_root) }
    if ([bool]$Config.auto_monitor) { $args += "--auto-monitor" }
    if ($null -ne $Config.PSObject.Properties['auto_onboard_workspaces'] -and [bool]$Config.auto_onboard_workspaces) { $args += "--auto-onboard-workspaces" }
    return $args
}

function New-Receipt {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)]$Health,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$StdoutPath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$StderrPath
    )
    [pscustomobject]@{
        schema = $script:ConfigSchema
        product = "AP-Vibe"
        state = "running"
        pid = [int]$Process.pid
        process_path = [string]$Process.path
        command_line = [string]$Process.command_line
        started_at = [string]$Process.start_time
        url = "http://$([string]$Config.host):$([int]$Config.port)/"
        health = $Health.body
        health_http_status = [int]$Health.http_status
        stdout_path = $StdoutPath
        stderr_path = $StderrPath
        identity = [pscustomobject]@{
            host = [string]$Config.host
            port = [int]$Config.port
            data_dir = [string]$Config.data_dir
            project_root = [string]$Config.project_root
            project_id = [string]$Config.project_id
        }
        updated_at = [DateTime]::UtcNow.ToString("o")
    }
}

function Sync-OwnedReceipt {
    param($Config, $Process, $Health)
    # Call only after Test-OwnedProcess matched this installation. A healthy
    # daemon may have been started outside the wrapper after the receipt died.
    $previous = Read-JsonFile (Get-ReceiptPath)
    $stdout = ''; $stderr = ''
    if ($previous -and [int]$previous.pid -eq [int]$Process.pid -and
        [string]$previous.started_at -eq [string]$Process.start_time) {
        if ($previous.PSObject.Properties['stdout_path']) { $stdout = [string]$previous.stdout_path }
        if ($previous.PSObject.Properties['stderr_path']) { $stderr = [string]$previous.stderr_path }
    }
    $receipt = New-Receipt $Config $Process $Health $stdout $stderr
    Write-AtomicJson (Get-ReceiptPath) $receipt
    return $receipt
}

function Stop-ExactOwnedProcess {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [int]$WaitSeconds = 8
    )
    if ($ProcessId -le 0) {
        return [pscustomobject]@{ status = "absent"; pid = $ProcessId; owner = $null; error = $null }
    }
    $record = Get-ProcessRecord $ProcessId
    if ($null -eq $record) {
        return [pscustomobject]@{ status = "already_exited"; pid = $ProcessId; owner = $null; error = $null }
    }
    $owned = Test-OwnedProcess $Config $ProcessId
    if ($null -eq $owned) {
        # The process may exit between the first read and identity check.
        $recordAfterRace = Get-ProcessRecord $ProcessId
        if ($null -eq $recordAfterRace) {
            return [pscustomobject]@{ status = "already_exited"; pid = $ProcessId; owner = $null; error = $null }
        }
        return [pscustomobject]@{ status = "identity_mismatch"; pid = $ProcessId; owner = $recordAfterRace; error = $null }
    }
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
    } catch {
        return [pscustomobject]@{ status = "stop_failed"; pid = $ProcessId; owner = $owned; error = $_.Exception.Message }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, [Math]::Min($WaitSeconds, 30)))
    while ((Get-ProcessRecord $ProcessId) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 200 }
    $remaining = Get-ProcessRecord $ProcessId
    if ($null -eq $remaining) {
        return [pscustomobject]@{ status = "stopped"; pid = $ProcessId; owner = $owned; error = $null }
    }
    return [pscustomobject]@{ status = "stop_timeout"; pid = $ProcessId; owner = $remaining; error = "process_still_running" }
}

function Start-ManagedService {
    param([Parameter(Mandatory = $true)]$Config)
    Assert-ConfigIdentity $Config
    Select-AvailableServicePort $Config
    Write-AtomicJson (Get-ConfigPath) $Config
    $existingPid = Get-ListeningPid ([int]$Config.port)
    if ($existingPid) {
        $owned = Test-OwnedProcess $Config $existingPid
        if ($owned) {
            $health = Invoke-Health $Config 2
            if ($health.reachable -and $health.body.status -eq "ok") {
                $receipt = Sync-OwnedReceipt $Config $owned $health
                return [pscustomobject]@{ ok = $true; replayed = $true; stage = "already_running"; pid = $existingPid; health = $health.body; receipt = $receipt }
            }
            $staleStop = Stop-ExactOwnedProcess $Config $existingPid
            if ($staleStop.status -notin @("stopped", "already_exited", "absent")) {
                return [pscustomobject]@{ ok = $false; code = "existing_service_stop_failed"; stage = "preflight"; retryable = $true; solution = "已有 AP-Vibe 实例不健康且未能安全停止；查看 status/receipt 后重试，不启动第二个实例。"; owner_pid = $existingPid; owner = $staleStop.owner; stop = $staleStop }
            }
            Start-Sleep -Milliseconds 300
        } else {
            return [pscustomobject]@{ ok = $false; code = "port_in_use"; stage = "preflight"; retryable = $false; solution = "换用未占用的端口后重新 install；没有停止或接管占用该端口的其它进程。"; port = [int]$Config.port; owner_pid = $existingPid; owner = (Get-ProcessRecord $existingPid) }
        }
    }
    $configDir = Get-ResolvedConfigDir
    $logDir = Get-LogDir
    New-Item -ItemType Directory -Force -Path $configDir, $Config.data_dir, $logDir | Out-Null
    $stamp = [DateTime]::UtcNow.ToString("yyyyMMdd-HHmmss-fff")
    $stdout = Join-Path $logDir "daemon-$stamp.stdout.log"
    $stderr = Join-Path $logDir "daemon-$stamp.stderr.log"
    $srcRoot = Join-Path $script:ProductRoot "src"
    $oldPythonPath = $env:PYTHONPATH
    $oldApConfigPath = $env:AP_VIBE_CONFIG_PATH
    try {
        $env:AP_VIBE_CONFIG_PATH = Get-ConfigPath
        if ([string]::IsNullOrWhiteSpace($oldPythonPath)) { $env:PYTHONPATH = $srcRoot } else { $env:PYTHONPATH = "$srcRoot$([IO.Path]::PathSeparator)$oldPythonPath" }
        $helper = Join-Path $script:ProductRoot "tools\launch_daemon.py"
        $prefix = @($Config.python_prefix)
        $daemonArguments = @(New-CommandArguments $Config)
        $launchResult = & ([string]$Config.python) @prefix $helper --executable ([string]$Config.python) --cwd $script:ProductRoot --stdout $stdout --stderr $stderr -- @daemonArguments
        if ($LASTEXITCODE -ne 0) { throw "daemon_launch_failed" }
        $launched = $launchResult | ConvertFrom-Json
        Set-PropertyValue $Config "launch_mode" ([string]$launched.launch_mode)
        $launch = [pscustomobject]@{ Id = [int]$launched.pid }
    } finally {
        $env:PYTHONPATH = $oldPythonPath
        $env:AP_VIBE_CONFIG_PATH = $oldApConfigPath
    }
    $health = Wait-Healthy $Config $HealthTimeoutSeconds
    if (-not ($health -and $health.reachable -and $health.body.status -eq "ok")) {
        $ownerPid = Get-ListeningPid ([int]$Config.port)
        $owner = if ($ownerPid) { Get-ProcessRecord $ownerPid } else { $null }
        $cleanupResults = @()
        foreach ($cleanupPid in (@([int]$launch.Id) + @($ownerPid) | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)) {
            $cleanupResults += Stop-ExactOwnedProcess $Config ([int]$cleanupPid)
        }
        $remainingOwnerPid = Get-ListeningPid ([int]$Config.port)
        $launchStillRunning = ($null -ne (Get-ProcessRecord ([int]$launch.Id)))
        $cleanupComplete = ($null -eq $remainingOwnerPid -and -not $launchStillRunning)
        return [pscustomobject]@{
            ok = $false
            code = "health_timeout"
            stage = "health_readback"
            retryable = $true
            solution = "查看 stderr 日志，修复 Python、构建目录或 data 目录问题后再次 start；服务未写入已安装成功状态。若 cleanup_complete=false，只按 receipt/命令行核对后手动处理。"
            launch_pid = [int]$launch.Id
            owner_pid = $ownerPid
            owner = $owner
            health = if ($health) { $health.body } else { $null }
            stdout_path = $stdout
            stderr_path = $stderr
            cleanup = [pscustomobject]@{
                attempted = $true
                complete = $cleanupComplete
                candidates = @($cleanupResults)
                remaining_owner_pid = $remainingOwnerPid
                launch_still_running = $launchStillRunning
            }
        }
    }
    $ownerPid = Get-ListeningPid ([int]$Config.port)
    $process = if ($ownerPid) { Test-OwnedProcess $Config $ownerPid } else { $null }
    if ($null -eq $process) { $process = Test-OwnedProcess $Config ([int]$launch.Id) }
    if ($null -eq $process) {
        return [pscustomobject]@{ ok = $false; code = "process_identity_unreadable"; stage = "receipt"; retryable = $true; solution = "保留日志并检查服务命令行；不要按进程名停止其它 Python。"; launch_pid = [int]$launch.Id; owner_pid = $ownerPid; stdout_path = $stdout; stderr_path = $stderr }
    }
    $receipt = New-Receipt $Config $process $health $stdout $stderr
    Write-AtomicJson (Get-ReceiptPath) $receipt
    return [pscustomobject]@{ ok = $true; replayed = $false; stage = "healthy"; pid = [int]$process.pid; health = $health.body; receipt = $receipt }
}

function Get-ManagedStatus {
    $config = Get-ExistingConfig
    $receipt = Read-JsonFile (Get-ReceiptPath)
    if ($null -eq $config) {
        return [pscustomobject]@{ ok = $true; status = "not_installed"; installed = $false; running = $false; config = $null; receipt = $receipt }
    }
    Assert-ConfigIdentity $config
    $portPid = Get-ListeningPid ([int]$config.port)
    $owned = if ($portPid) { Test-OwnedProcess $config $portPid } else { $null }
    $health = Invoke-Health $config 2
    if (
        $receipt -and
        [string]$receipt.state -eq "running" -and
        [int]$receipt.pid -gt 0 -and
        $null -eq $owned -and
        $null -eq $portPid -and
        $null -eq (Get-ProcessRecord ([int]$receipt.pid))
    ) {
        # A daemon can exit outside this wrapper (crash, logoff, or external
        # termination). Reconcile only the provably absent owned process when
        # the port is also free; an occupied port remains fail-closed.
        Set-PropertyValue $receipt "state" "stopped"
        Set-PropertyValue $receipt "stopped_at" ([DateTime]::UtcNow.ToString("o"))
        Set-PropertyValue $receipt "stop_reason" "process_not_running"
        $receipt.updated_at = [DateTime]::UtcNow.ToString("o")
        Write-AtomicJson (Get-ReceiptPath) $receipt
    }
    $state = "stopped"
    if ($owned -and $health.reachable -and $health.body.status -eq "ok") { $state = "running" }
    elseif ($portPid -and -not $owned) { $state = "port_conflict" }
    elseif ($owned) { $state = "degraded" }
    [pscustomobject]@{
        ok = $true
        status = $state
        installed = $true
        running = ($state -eq "running")
        pid = if ($owned) { [int]$owned.pid } else { $null }
        port = [int]$config.port
        url = "http://$([string]$config.host):$([int]$config.port)/"
        config = $config
        receipt = $receipt
        health = if ($health) { $health.body } else { $null }
        health_http_status = if ($health) { $health.http_status } else { $null }
        owner_pid = $portPid
        owner = if ($portPid) { Get-ProcessRecord $portPid } else { $null }
    }
}

function Register-Autostart {
    param([Parameter(Mandatory = $true)]$Config)
    $dir = Get-AutostartDir
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $path = Get-AutostartPath
    $scriptPath = (Convert-ToFullPath $script:ScriptPath)
    $configPath = (Convert-ToFullPath (Get-ConfigPath))
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $existing = [IO.File]::ReadAllText($path)
        $existingNormalized = Normalize-Text $existing
        $ownedExisting = $existingNormalized.Contains((Normalize-Text $script:AutostartMarker)) -and $existingNormalized.Contains((Normalize-Text $configPath)) -and $existingNormalized.Contains((Normalize-Text $scriptPath))
        if (-not $ownedExisting) { throw "autostart_identity_mismatch" }
    }
    $ps = Get-PowerShellExecutable
    $lines = @(
        "@echo off",
        "rem $($script:AutostartMarker)",
        "rem AP-VIBE-CONFIG: $configPath",
        "rem AP-VIBE-SCRIPT: $scriptPath",
        "`"$ps`" -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Action start -ConfigDir `"$(Get-ResolvedConfigDir)`" -SkipOpen"
    )
    $content = ($lines -join "`r`n") + "`r`n"
    [IO.File]::WriteAllText($path, $content, (New-Object Text.UTF8Encoding($false)))
    $readback = [IO.File]::ReadAllText($path)
    if (-not ($readback.Contains($script:AutostartMarker) -and $readback.Contains($configPath) -and $readback.Contains($scriptPath))) {
        throw "autostart_readback_failed"
    }
    return [pscustomobject]@{ registered = $true; path = $path; marker = $script:AutostartMarker; readback = $true }
}

function Install-DesktopLauncher {
    param([Parameter(Mandatory = $true)]$Config)
    Assert-ConfigIdentity $Config
    $status = Get-ManagedStatus
    $started = $false
    if (-not $status.running) {
        $startedResult = Start-ManagedService $Config
        if (-not $startedResult.ok) { return $startedResult }
        $Config.state = "installed"
        $Config.updated_at = [DateTime]::UtcNow.ToString("o")
        Write-AtomicJson (Get-ConfigPath) $Config
        $status = Get-ManagedStatus
        $started = -not [bool]$startedResult.replayed
    }
    if (-not $status.running) {
        return (New-ResultError "service_not_healthy" "desktop_launcher" "服务尚未通过 health=ok；没有创建可能指向错误端口的快捷方式。" $true)
    }
    $desktop = Get-DesktopDir
    New-Item -ItemType Directory -Force -Path $desktop | Out-Null
    $shortcutPath = Get-DesktopShortcutPath
    $scriptPath = Convert-ToFullPath $script:ScriptPath
    $configDir = Convert-ToFullPath (Get-ResolvedConfigDir)
    $powershell = Get-PowerShellExecutable
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`" -Action launch -ConfigDir `"$configDir`""
    $icon = Get-LauncherIconPath
    $iconLocation = if ($icon) { "$icon,0" } else { "$powershell,0" }
    $shell = $null
    $created = -not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)
    try {
        $shell = New-Object -ComObject WScript.Shell
        if (-not $created) {
            $existing = $shell.CreateShortcut($shortcutPath)
            $existingTarget = [string]$existing.TargetPath
            $existingArgs = Normalize-Text ([string]$existing.Arguments)
            $owned = (Test-SamePath $existingTarget $powershell) -and
                $existingArgs.Contains((Normalize-Text $scriptPath)) -and
                $existingArgs.Contains("-action launch") -and
                $existingArgs.Contains((Normalize-Text $configDir))
            if (-not $owned) { return (New-ResultError "desktop_launcher_identity_mismatch" "desktop_launcher" "桌面上已有同名快捷方式但不属于当前 AP-Vibe；未覆盖它。请改名或手动核对后重试。") }
        }
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $powershell
        $shortcut.Arguments = $arguments
        $shortcut.WorkingDirectory = $script:ProductRoot
        $shortcut.IconLocation = $iconLocation
        $shortcut.Description = "AP-Vibe 本地项目心智工作台"
        $shortcut.Save()
        $readback = $shell.CreateShortcut($shortcutPath)
        $targetOk = Test-SamePath ([string]$readback.TargetPath) $powershell
        $argsNormalized = Normalize-Text ([string]$readback.Arguments)
        $readbackOk = $targetOk -and $argsNormalized.Contains((Normalize-Text $scriptPath)) -and $argsNormalized.Contains("-action launch") -and $argsNormalized.Contains((Normalize-Text $configDir))
        if (-not $readbackOk) { return (New-ResultError "desktop_launcher_readback_failed" "desktop_launcher" "快捷方式已经写入但属性核对失败；为避免误启动，先不要使用它并查看桌面文件属性。" $true) }
        return [pscustomobject]@{ ok = $true; status = "desktop_launcher_installed"; shortcut_path = $shortcutPath; current_url = $status.url; url = $status.url; created = $created; updated = -not $created; service_started = $started; icon_path = $icon; readback = [pscustomobject]@{ target = [string]$readback.TargetPath; arguments = [string]$readback.Arguments; working_directory = [string]$readback.WorkingDirectory; icon_location = [string]$readback.IconLocation } }
    } catch {
        return (New-ResultError "desktop_launcher_unavailable" "desktop_launcher" "Windows 快捷方式组件不可用或桌面没有写入权限；没有修改其它启动项。" $true)
    }
}

function Uninstall-Autostart {
    $path = Get-AutostartPath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        return [pscustomobject]@{ ok = $true; status = "absent"; removed = $false; path = $path }
    }
    $content = [IO.File]::ReadAllText($path)
    $configPath = Normalize-Text (Get-ConfigPath)
    $scriptPath = Normalize-Text $script:ScriptPath
    $normalized = Normalize-Text $content
    if (-not ($normalized.Contains((Normalize-Text $script:AutostartMarker)) -and $normalized.Contains($configPath) -and $normalized.Contains($scriptPath))) {
        return [pscustomobject]@{ ok = $false; code = "autostart_identity_mismatch"; status = "refused"; retryable = $false; solution = "该启动项不是由 AP-Vibe 当前入口创建的，未删除；请在系统启动项中手动核对后再处理。"; path = $path }
    }
    Remove-Item -LiteralPath $path -Force
    [pscustomobject]@{ ok = $true; status = "removed"; removed = $true; path = $path }
}

function New-ResultError {
    param([string]$Code, [string]$Stage, [string]$Solution, [bool]$Retryable = $false)
    [pscustomobject]@{ ok = $false; status = "failed"; code = $Code; stage = $Stage; retryable = $Retryable; solution = $Solution }
}

function Invoke-Action {
    if ($Action -ne 'install') {
        $activeConfig = Get-ExistingConfig
        if ($activeConfig -and (Test-Path -LiteralPath (Join-Path $activeConfig.product_root 'src\ap_mind\studio_server.py'))) {
            $script:ProductRoot = [string]$activeConfig.product_root
            $script:ScriptPath = Join-Path $script:ProductRoot 'scripts\ap-vibe.ps1'
        }
    }
    switch ($Action) {
        "apply-update" {
            $config = Get-ExistingConfig
            Assert-ConfigIdentity $config
            $candidate = Convert-ToFullPath $CandidateRoot
            $versions = Convert-ToFullPath (Join-Path (Get-ResolvedConfigDir) 'versions')
            if (-not $candidate.StartsWith($versions + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'update_candidate_outside_versions' }
            # The first update may start from a version without update_client.py.
            # Use this verified invocation package's helper, while keeping the
            # original installation root for process identity and rollback.
            $check = & ([string]$config.python) (Join-Path $script:PackageRoot 'tools\update_client.py') verify --root $candidate
            if ($LASTEXITCODE -ne 0) { throw 'update_package_verification_failed' }
            $verified = $check | ConvertFrom-Json
            $oldRoot = $script:ProductRoot
            $original = $config | ConvertTo-Json -Depth 32 | ConvertFrom-Json
            $owner = 'update-' + [Guid]::NewGuid().ToString('N')
            $maintenanceUrl = "http://$($config.host):$($config.port)/v1/ap-vibe/studio/maintenance"
            $lease = $false
            $stopped = $false
            $started = $null
            try {
                $current = Get-ManagedStatus
                if ($current.running) {
                    $acquired = Invoke-RestMethod -Uri $maintenanceUrl -Method Post -ContentType 'application/json' -Body (@{owner=$owner;action='acquire'} | ConvertTo-Json) -TimeoutSec 5
                    if (-not $acquired.acquired) { return [pscustomobject]@{ok=$true;updated=$false;status='busy';reason=$acquired.reason} }
                    $lease = $true
                    $stop = Stop-ExactOwnedProcess $config ([int]$current.pid)
                    if ($stop.status -notin @('stopped','already_exited','absent')) { throw 'update_stop_not_confirmed' }
                    $stopped = $true
                } elseif ($current.status -ne 'stopped') { throw 'update_runtime_identity_unavailable' }
                $backupJson = & ([string]$config.python) (Join-Path $script:PackageRoot 'tools\update_client.py') checkpoint --config (Get-ConfigPath) --root $candidate
                if ($LASTEXITCODE -ne 0) { throw 'update_backup_failed' }
                $backup = $backupJson | ConvertFrom-Json
                $config.product_root = $candidate
                $config.script_path = Join-Path $candidate 'scripts\ap-vibe.ps1'
                $config.studio_dir = Join-Path $candidate 'apps\studio\dist\client'
                Set-PropertyValue $config 'installed_version' ([string]$verified.version)
                Set-PropertyValue $config 'previous_product_root' $oldRoot
                Write-AtomicJson (Get-ConfigPath) $config
                $script:ProductRoot = $candidate
                $script:ScriptPath = [string]$config.script_path
                $started = Start-ManagedService $config
                if (-not $started.ok) { throw 'update_new_runtime_unhealthy' }
                # Keep private config and original data identity; only sync integration files.
                $integrationIssues = @()
                if (Test-SamePath (Get-ResolvedConfigDir) (Get-DefaultConfigDir)) {
                    $integrationResults = @()
                    foreach ($installer in @('install_dependencies.py','install_task_context.py','install_mcp.py','install_claude.py','install_harness.py','install_native_extras.py','trust_hooks.py')) {
                        $syncArgs = @('--config', (Get-ConfigPath))
                        if ($installer -eq 'install_claude.py') { $syncArgs += '--if-available' }
                        $previousPreference = $ErrorActionPreference
                        try {
                            # Windows PowerShell treats native stderr as error
                            # records. Capture the full diagnostic before handling
                            # the exit status, rather than losing it to "Traceback".
                            $ErrorActionPreference = 'Continue'
                            $sync = & ([string]$config.python) (Join-Path $candidate ('tools\' + $installer)) @syncArgs 2>&1
                            $syncExit = $LASTEXITCODE
                        } finally { $ErrorActionPreference = $previousPreference }
                        $integrationResults += [pscustomobject]@{ installer = $installer; exit_code = $syncExit; output = (($sync | ForEach-Object { [string]$_ }) -join "`n") }
                        if ($syncExit -ne 0) { $integrationIssues += $installer }
                    }
                    Write-AtomicJson (Join-Path (Get-ResolvedConfigDir) 'update-integration.json') ([pscustomobject]@{version=$verified.version;results=$integrationResults})
                }
                return [pscustomobject]@{ok=$true;updated=$true;status='installed';version=$verified.version;pid=$started.pid;url=$started.receipt.url;backup=$backup.backup;integration_issues=$integrationIssues}
            } catch {
                $failure = $_.Exception.Message
                $cleanupComplete = $true
                if ($script:ProductRoot -eq $candidate) {
                    $candidatePids = @()
                    if ($null -ne $started) {
                        foreach ($field in @('pid','launch_pid','owner_pid')) {
                            if ($started.PSObject.Properties.Name -contains $field -and $started.$field) { $candidatePids += [int]$started.$field }
                        }
                    }
                    $candidatePids += Get-ListeningPid ([int]$config.port)
                    foreach ($candidatePid in ($candidatePids | Where-Object { $_ -and $_ -gt 0 } | Select-Object -Unique)) {
                        $cleanupResult = Stop-ExactOwnedProcess $config ([int]$candidatePid)
                        if ($cleanupResult.status -notin @('stopped','already_exited','absent')) { $cleanupComplete = $false }
                    }
                }
                $script:ProductRoot = $oldRoot
                $script:ScriptPath = [string]$original.script_path
                Write-AtomicJson (Get-ConfigPath) $original
                if (-not $cleanupComplete) {
                    return [pscustomobject]@{ok=$false;updated=$false;status='recovery_pending';code=$failure;recovered=$false;data_restored=$false;solution='旧版入口已恢复，但候选进程尚未确认退出；保留数据并等待进程核对，未启动第二份服务。'}
                }
                $recovered = Start-ManagedService $original
                return [pscustomobject]@{ok=$false;updated=$false;status='rolled_back';code=$failure;recovered=[bool]$recovered.ok;data_restored=$false;solution='已恢复旧代码入口，保留同一数据库和所有新写入。'}
            } finally {
                if ($lease) {
                    try { $released = Invoke-RestMethod -Uri $maintenanceUrl -Method Post -ContentType 'application/json' -Body (@{owner=$owner;action='release'} | ConvertTo-Json) -TimeoutSec 5 } catch { }
                }
            }
        }
        "status" {
            return (Get-ManagedStatus)
        }
        "uninstall-autostart" {
            return (Uninstall-Autostart)
        }
        "install" {
            $existing = Get-ExistingConfig
            if ($existing) {
                Assert-ConfigIdentity $existing
                if (-not $script:InvocationParameters.ContainsKey('ProjectRoot')) { $ProjectRoot = $existing.project_root }
                if (-not $script:InvocationParameters.ContainsKey('ProjectId')) { $ProjectId = $existing.project_id }
                if (-not $script:InvocationParameters.ContainsKey('Port')) { $Port = [string]$existing.port }
                if (-not $script:InvocationParameters.ContainsKey('DataDir')) { $DataDir = $existing.data_dir }
                if (-not $script:InvocationParameters.ContainsKey('CodexSessionsRoot')) { $CodexSessionsRoot = $existing.codex_sessions_root }
            }
            $config = New-Configuration
            if ($existing) {
                Assert-ConfigIdentity $existing
                if ($null -ne $existing.PSObject.Properties["requested_port"] -and [int]$existing.requested_port -eq [int]$config.port) {
                    Set-PropertyValue $config "requested_port" ([int]$existing.requested_port)
                    $config.port = [int]$existing.port
                }
                if (-not (Test-SamePath ([string]$existing.project_root) ([string]$config.project_root)) -or [int]$existing.port -ne [int]$config.port -or [string]$existing.project_id -ne [string]$config.project_id) {
                    return (New-ResultError "config_identity_conflict" "preflight" "现有配置属于另一个项目或端口；使用对应的 ConfigDir，或明确选择新的隔离配置目录。")
                }
                if (-not (Test-SamePath ([string]$existing.data_dir) ([string]$config.data_dir))) {
                    return (New-ResultError 'config_data_directory_conflict' 'preflight' '已保留原资料目录。重复安装不会迁移数据；继续使用原DataDir，迁移请单独备份并明确安排。')
                }
                # Repair integration around the existing instance. Version
                # switching belongs to apply-update, preserving unknown fields.
                $config = $existing
            }
            Select-AvailableServicePort $config
            New-Item -ItemType Directory -Force -Path (Get-ResolvedConfigDir), $config.data_dir | Out-Null
            $config.state = "starting"
            Write-AtomicJson (Get-ConfigPath) $config
            $started = Start-ManagedService $config
            if (-not $started.ok) {
                $config.state = "failed"
                Set-PropertyValue $config "last_failure" $started
                $config.updated_at = [DateTime]::UtcNow.ToString("o")
                Write-AtomicJson (Get-ConfigPath) $config
                return $started
            }
            $config.state = "installed"
            $config.updated_at = [DateTime]::UtcNow.ToString("o")
            $autostart = $null
            $autostartFailure = $null
            if ($RegisterAutostart) {
                try {
                    $autostart = Register-Autostart $config
                } catch {
                    $autostartFailure = $_.Exception.Message
                    $cleanup = $null
                    try { $cleanup = Uninstall-Autostart } catch { $cleanup = [pscustomobject]@{ ok = $false; status = "cleanup_failed"; error = $_.Exception.Message } }
                    $autostart = [pscustomobject]@{
                        registered = $false
                        status = "failed"
                        code = (($autostartFailure -split ":")[0])
                        error = $autostartFailure
                        path = (Get-AutostartPath)
                        cleanup = $cleanup
                        solution = "服务本身已健康运行；请修复 Startup 目录权限或占用后重新执行 install -RegisterAutostart，或继续使用 open/start。"
                    }
                    Set-PropertyValue $config "last_autostart_failure" $autostart
                    # An earlier successful registration must not survive a
                    # later failed attempt as if it were still active.
                    if ($null -ne $config.PSObject.Properties["autostart"]) { $config.PSObject.Properties.Remove("autostart") }
                }
            }
            Write-AtomicJson (Get-ConfigPath) $config
            if ($null -ne $autostart -and $null -eq $autostartFailure) { Set-PropertyValue $config "autostart" $autostart; Write-AtomicJson (Get-ConfigPath) $config }
            $partialInstall = ($null -ne $autostartFailure)
            $result = [pscustomobject]@{ ok = $true; status = if ($partialInstall) { "installed_partial" } else { "installed" }; service_status = "healthy"; autostart_registration_failed = $partialInstall; replayed = [bool]$started.replayed; stage = "health_readback"; pid = $started.pid; url = $started.receipt.url; health = $started.health; config_path = (Get-ConfigPath); receipt_path = (Get-ReceiptPath); autostart = $autostart }
            if (-not $SkipOpen) {
                try { Start-Process -FilePath $result.url | Out-Null; $result | Add-Member -NotePropertyName opened -NotePropertyValue $true }
                catch { $result | Add-Member -NotePropertyName opened -NotePropertyValue $false }
            }
            return $result
        }
        "start" {
            $config = Get-ExistingConfig
            if ($null -eq $config) { return (New-ResultError "not_installed" "load_config" "先运行 install，或使用正确的 ConfigDir。") }
            Assert-ConfigIdentity $config
            $started = Start-ManagedService $config
            if (-not $started.ok) { return $started }
            $config.state = "installed"; $config.updated_at = [DateTime]::UtcNow.ToString("o"); Write-AtomicJson (Get-ConfigPath) $config
            return [pscustomobject]@{ ok = $true; status = "running"; replayed = [bool]$started.replayed; stage = $started.stage; pid = $started.pid; url = $started.receipt.url; health = $started.health; receipt_path = (Get-ReceiptPath) }
        }
        "open" {
            $status = Get-ManagedStatus
            if (-not $status.running) { return (New-ResultError "service_not_healthy" "open" "先运行 start；只有 health status=ok 的本地服务才会打开。" $true) }
            if (-not $SkipOpen) { Start-Process -FilePath $status.url | Out-Null }
            return [pscustomobject]@{ ok = $true; status = "opened"; url = $status.url; pid = $status.pid }
        }
        "launch" {
            $config = Get-ExistingConfig
            if ($null -eq $config) { return (New-ResultError "not_installed" "launch" "先运行 install，或使用正确的 ConfigDir。") }
            Assert-ConfigIdentity $config
            $status = Get-ManagedStatus
            $started = $false
            if (-not $status.running) {
                $startedResult = Start-ManagedService $config
                if (-not $startedResult.ok) { return $startedResult }
                $config.state = "installed"
                $config.updated_at = [DateTime]::UtcNow.ToString("o")
                Write-AtomicJson (Get-ConfigPath) $config
                $status = Get-ManagedStatus
                $started = -not [bool]$startedResult.replayed
            }
            if (-not $status.running) { return (New-ResultError "service_not_healthy" "launch" "服务未通过 health=ok；没有打开页面。" $true) }
            Start-Process -FilePath $status.url | Out-Null
            return [pscustomobject]@{ ok = $true; status = "launched"; url = $status.url; current_url = $status.url; pid = $status.pid; service_started = $started }
        }
        "install-desktop-launcher" {
            if ($null -eq (Get-ExistingConfig)) { return (New-ResultError "not_installed" "desktop_launcher" "先运行 install，或使用正确的 ConfigDir。") }
            return (Install-DesktopLauncher (Get-ExistingConfig))
        }
        "stop" {
            $config = Get-ExistingConfig
            if ($null -eq $config) { return [pscustomobject]@{ ok = $true; status = "not_installed"; stopped = $false } }
            Assert-ConfigIdentity $config
            $receipt = Read-JsonFile (Get-ReceiptPath)
            $listenerPid = Get-ListeningPid ([int]$config.port)
            if ($listenerPid) {
                $listener = Test-OwnedProcess $config $listenerPid
                if (-not $listener) { return (New-ResultError "port_conflict" "stop" "配置端口上的进程不属于当前安装，未停止任何进程。请查看 status 核对配置。") }
                $receipt = Sync-OwnedReceipt $config $listener (Invoke-Health $config 2)
            }
            $receiptPid = if ($receipt) { [int]$receipt.pid } else { 0 }
            if ($receiptPid -le 0) {
                $portOwnerPid = Get-ListeningPid ([int]$config.port)
                if ($portOwnerPid) {
                    $portOwner = Test-OwnedProcess $config $portOwnerPid
                    if ($portOwner) { return (New-ResultError "receipt_missing_for_owned_process" "stop" "发现本配置端口仍由 AP-Vibe 占用，但没有可验证 receipt PID；为避免误杀未执行停止。请先恢复 receipt 或手动核对 owner。" $true) }
                    return (New-ResultError "port_conflict" "stop" "配置端口被其它进程占用，且没有可验证 receipt；未停止任何进程。请查看 owner 后改用正确 ConfigDir/端口。")
                }
                return [pscustomobject]@{ ok = $true; status = "stopped"; stopped = $false; reason = "no_receipt_pid"; port_released = $true }
            }
            $stopResult = Stop-ExactOwnedProcess $config $receiptPid
            if ($stopResult.status -eq "already_exited" -or $stopResult.status -eq "absent") {
                $portOwnerPid = Get-ListeningPid ([int]$config.port)
                $receipt.state = "stopped"; Set-PropertyValue $receipt "stopped_at" ([DateTime]::UtcNow.ToString("o")); Set-PropertyValue $receipt "stop_reason" "process_already_exited"; $receipt.updated_at = [DateTime]::UtcNow.ToString("o"); Write-AtomicJson (Get-ReceiptPath) $receipt
                return [pscustomobject]@{ ok = $true; status = "stopped"; stopped = $false; reason = "process_already_exited"; pid = $receiptPid; port_released = ($null -eq $portOwnerPid); owner_pid = $portOwnerPid; receipt_path = (Get-ReceiptPath) }
            }
            if ($stopResult.status -eq "identity_mismatch") { return (New-ResultError "stop_refused_identity_mismatch" "stop" "暂时无法确认 receipt 中的 PID 仍属于本服务；未停止任何进程。请先查看 status 核对身份，不代表已经确认被其它命令占用。") }
            if ($stopResult.status -eq "stop_failed") { return [pscustomobject]@{ ok = $false; status = "failed"; code = "stop_failed"; stage = "stop"; retryable = $true; error = $stopResult.error; owner = $stopResult.owner; solution = "确认当前用户仍有权停止该 AP-Vibe 进程后重试；未按进程名停止其它服务。" } }
            if ($stopResult.status -eq "stop_timeout") { return [pscustomobject]@{ ok = $false; status = "failed"; code = "stop_timeout"; stage = "stop"; retryable = $true; pid = $receiptPid; owner = $stopResult.owner; solution = "服务未在有限时间内退出；查看 status/日志后再重试，不要按进程名批量停止。" } }
            $receipt.state = "stopped"; Set-PropertyValue $receipt "stopped_at" ([DateTime]::UtcNow.ToString("o")); $receipt.updated_at = [DateTime]::UtcNow.ToString("o"); Write-AtomicJson (Get-ReceiptPath) $receipt
            return [pscustomobject]@{ ok = $true; status = "stopped"; stopped = $true; pid = $receiptPid; port_released = ($null -eq (Get-ListeningPid ([int]$config.port))); receipt_path = (Get-ReceiptPath) }
        }
    }
}

$result = $null
$lifecycleMutex = $null
$ownsLifecycleMutex = $false
try {
    if ($Action -in @("install", "start", "launch", "stop", "apply-update")) {
        $sha = [Security.Cryptography.SHA256]::Create()
        try { $lockHash = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes((Get-ResolvedConfigDir).ToLowerInvariant()))).Replace("-", "") }
        finally { $sha.Dispose() }
        $lifecycleMutex = [Threading.Mutex]::new($false, "Local\APVibe-" + $lockHash)
        try { $ownsLifecycleMutex = $lifecycleMutex.WaitOne(30000) }
        catch [Threading.AbandonedMutexException] { $ownsLifecycleMutex = $true }
        if (-not $ownsLifecycleMutex) { throw "lifecycle_operation_in_progress" }
    }
    $result = Invoke-Action
} catch {
    $result = New-ResultError (($_.Exception.Message -split ":")[0]) "script" "保留当前 config/receipt 和错误阶段，按 solution 修复后重试；脚本没有按进程名清理其它服务。" $true
} finally {
    if ($ownsLifecycleMutex) { $lifecycleMutex.ReleaseMutex() }
    if ($null -ne $lifecycleMutex) { $lifecycleMutex.Dispose() }
}
$result | ConvertTo-Json -Depth 24
if (-not $result.ok) { exit 1 }
