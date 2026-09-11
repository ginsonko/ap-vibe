# Windows can omit WMI CommandLine even for a process the current user owns.
# QUERY_LIMITED_INFORMATION can still provide its executable path.
function Get-LimitedProcessImage {
    param([int]$ProcessId)
    try {
        if (-not ('APVibe.ProcessImage' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
namespace APVibe {
    public static class ProcessImage {
        [DllImport("kernel32.dll", SetLastError=true)]
        static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder path, ref uint size);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
        public static string Read(int pid) {
            var handle=OpenProcess(0x1000, false, pid);
            if (handle==IntPtr.Zero) return null;
            try { uint size=32768; var path=new StringBuilder((int)size);
                return QueryFullProcessImageName(handle,0,path,ref size)?path.ToString():null;
            } finally { CloseHandle(handle); }
        }
    }
}
'@
        }
        return [APVibe.ProcessImage]::Read($ProcessId)
    } catch { return $null }
}

function Test-ReceiptProcessIdentity {
    param($Config, $Record, $Receipt, [int]$ListeningPid)
    # Only use continuity evidence if live command-line visibility is absent.
    # A nonempty, different command line must never be accepted via a receipt.
    try {
        if (-not [string]::IsNullOrWhiteSpace([string]$Record.command_line)) { return $false }
        if ($null -eq $Receipt -or $Receipt.product -ne 'AP-Vibe' -or $Receipt.state -ne 'running') { return $false }
        if ([int]$Record.pid -ne [int]$Receipt.pid -or [int]$Record.pid -ne $ListeningPid) { return $false }
        if ([string]::IsNullOrWhiteSpace($Record.start_time) -or [string]::IsNullOrWhiteSpace($Receipt.started_at)) { return $false }
        # PowerShell 7 may decode JSON ISO dates to DateTime; do not convert
        # those through a localized string that drops fractions and timezone.
        $recordTime = if ($Record.start_time -is [DateTime]) { [DateTimeOffset]$Record.start_time } else { [DateTimeOffset]::Parse([string]$Record.start_time) }
        $receiptTime = if ($Receipt.started_at -is [DateTime]) { [DateTimeOffset]$Receipt.started_at } else { [DateTimeOffset]::Parse([string]$Receipt.started_at) }
        $delta=($recordTime-$receiptTime).Duration()
        if ($delta.TotalMilliseconds -gt 1) { return $false }
        if (-not (Test-SamePath $Record.path $Receipt.process_path) -or -not (Test-SamePath $Record.path $Config.python)) { return $false }
        foreach ($field in @('data_dir','project_root')) {
            if (-not (Test-SamePath $Config.$field $Receipt.identity.$field)) { return $false }
        }
        if ([int]$Config.port -ne [int]$Receipt.identity.port -or
            [string]$Config.project_id -ne [string]$Receipt.identity.project_id -or
            [string]$Config.host -ne [string]$Receipt.identity.host) { return $false }
        return $true
    } catch { return $false }
}
