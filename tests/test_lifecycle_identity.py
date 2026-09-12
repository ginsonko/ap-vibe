"""Windows receipt fallback must not mistake a recycled PID for the daemon."""
from pathlib import Path
import shutil
import subprocess
import sys
import pytest


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows lifecycle')
def test_receipt_continuity_with_missing_commandline(tmp_path):
    root=Path(__file__).resolve().parents[1]
    script=tmp_path/'identity.ps1'
    script.write_text(r'''
param($Root)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
. (Join-Path $Root 'scripts/process-identity.ps1')
$tokens=$null;$parseErrors=$null
$tree=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root 'scripts/ap-vibe.ps1'),[ref]$tokens,[ref]$parseErrors)
if($parseErrors.Count){throw 'parse errors'}
foreach($name in @('Convert-ToFullPath','Normalize-Text','Test-SamePath')){
 $node=$tree.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)
 Invoke-Expression $node.Extent.Text
}
$cfg=[pscustomobject]@{python='C:\python.exe';data_dir='C:\data';project_root='C:\repo';product_root=$Root;port=8765;project_id='project';host='127.0.0.1'}
$record=[pscustomobject]@{pid=1234;path='C:\python.exe';command_line='';start_time='2026-09-11T06:18:16.3413579Z'}
$receipt=[pscustomobject]@{product='AP-Vibe';state='running';pid=1234;process_path='C:\python.exe';started_at=$record.start_time;identity=$cfg}
if(-not(Test-ReceiptProcessIdentity $cfg $record $receipt 1234)){throw 'string dates'}
$receipt.started_at=[DateTime]::Parse($record.start_time).ToUniversalTime()
if(-not(Test-ReceiptProcessIdentity $cfg $record $receipt 1234)){throw 'typed JSON date'}
$receipt.started_at=$receipt.started_at.AddSeconds(1)
if(Test-ReceiptProcessIdentity $cfg $record $receipt 1234){throw 'reused PID accepted'}
$receipt.started_at=$record.start_time
$record.command_line='python other.py'
if(Test-ReceiptProcessIdentity $cfg $record $receipt 1234){throw 'different command accepted'}
$record.command_line='';$record.path='C:\other.exe'
if(Test-ReceiptProcessIdentity $cfg $record $receipt 1234){throw 'different image accepted'}
$record.path='C:\python.exe'
if(Test-ReceiptProcessIdentity $cfg $record $receipt 5678){throw 'different listener accepted'}
$receipt.identity=[pscustomobject]@{data_dir='C:\other';project_root='C:\repo';port=8765;project_id='project';host='127.0.0.1'}
if(Test-ReceiptProcessIdentity $cfg $record $receipt 1234){throw 'different data accepted'}
$receipt.identity=$cfg;$record.start_time=''
if(Test-ReceiptProcessIdentity $cfg $record $receipt 1234){throw 'missing time accepted'}
Write-Output 'identity checks passed'
''',encoding='utf-8-sig')
    shells=[p for p in (shutil.which('pwsh'),shutil.which('powershell')) if p]
    assert shells
    for shell in shells:
        result=subprocess.run([shell,'-NoProfile','-File',str(script),str(root)],capture_output=True,text=True,timeout=30)
        assert result.returncode==0,result.stdout+result.stderr
        assert 'identity checks passed' in result.stdout


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows lifecycle')
def test_start_and_stop_reconcile_stale_receipt_only_for_owned_listener(tmp_path):
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / 'reconcile.ps1'
    script.write_text(r'''
param($Root)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$tokens=$null;$errors=$null
$tree=[System.Management.Automation.Language.Parser]::ParseFile((Join-Path $Root 'scripts/ap-vibe.ps1'),[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'parse errors'}
foreach($name in @('New-Receipt','Sync-OwnedReceipt','Start-ManagedService','Invoke-Action','New-ResultError','Set-PropertyValue')){
 $node=$tree.Find({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name},$true)
 Invoke-Expression $node.Extent.Text
}
$script:ConfigSchema='ap-vibe.lifecycle.v1'
$script:InvocationParameters=@{}
$cfg=[pscustomobject]@{python='C:\python.exe';data_dir='C:\data';project_root='C:\repo';product_root=$Root;port=8765;project_id='project';host='127.0.0.1'}
$script:listenerPid=200
$script:owned=$true
$script:saved=[pscustomobject]@{pid=100;state='running';started_at='old';stdout_path='old.log';stderr_path='old-error.log'}
$script:stoppedPid=0
function Get-ExistingConfig { return $cfg }
function Assert-ConfigIdentity { param($Config) }
function Select-AvailableServicePort { param($Config) }
function Get-ConfigPath { return 'config.json' }
function Get-ReceiptPath { return 'receipt.json' }
function Read-JsonFile { param($Path) return $script:saved }
function Write-AtomicJson { param($Path,$Value) if($Path -eq 'receipt.json'){$script:saved=$Value} }
function Get-ListeningPid { param($Port) if($script:listenerPid){return $script:listenerPid} return $null }
function Test-OwnedProcess { param($Config,$ProcessId)
 if($script:owned){return [pscustomobject]@{pid=$ProcessId;path='C:\python.exe';command_line='owned';start_time='new'}}
 return $null
}
function Invoke-Health { param($Config,$Timeout) return [pscustomobject]@{reachable=$true;http_status=200;body=[pscustomobject]@{status='ok'}} }
function Stop-ExactOwnedProcess { param($Config,$ProcessId)
 $script:stoppedPid=$ProcessId;$script:listenerPid=0
 return [pscustomobject]@{status='stopped'}
}
$start=Start-ManagedService $cfg
if($start.pid -ne 200 -or $start.receipt.pid -ne 200 -or $script:saved.pid -ne 200){throw 'start retained stale receipt'}
if($start.receipt.stdout_path -ne ''){throw 'old logs misattributed'}
$script:saved.stdout_path='current.log'
$again=Start-ManagedService $cfg
if($again.receipt.stdout_path -ne 'current.log'){throw 'current logs lost'}
$Action='stop'
$script:saved=[pscustomobject]@{pid=100;state='running';started_at='old'}
$stop=Invoke-Action
if(-not $stop.stopped -or $script:stoppedPid -ne 200 -or -not $stop.port_released){throw 'stale stop failed'}
$script:saved=$null;$script:listenerPid=201
$stop=Invoke-Action
if($script:stoppedPid -ne 201 -or -not $stop.stopped){throw 'missing receipt not recovered'}
$script:listenerPid=999;$script:owned=$false;$script:stoppedPid=0
$stop=Invoke-Action
if($stop.ok -or $script:stoppedPid -ne 0){throw 'unrelated process stopped'}
Write-Output 'receipt reconciliation passed'
''', encoding='utf-8-sig')
    for shell in [p for p in (shutil.which('pwsh'), shutil.which('powershell')) if p]:
        result = subprocess.run([shell, '-NoProfile', '-File', str(script), str(root)],
                                capture_output=True, text=True, errors='replace', timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'receipt reconciliation passed' in result.stdout
