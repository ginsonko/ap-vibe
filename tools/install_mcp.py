"""Register AP-Vibe's stdio tools through the official Codex CLI."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
import tomllib

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
sys.path.insert(0,str(root/'src'))
from ap_mind.codex_cli import codex_command
from tools.installation import custom_config_path


def install(config_path: Path | None = None):
    codex_root=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    config=codex_root/'config.toml'
    backup=codex_root/'ap-vibe-install-backups'/uuid.uuid4().hex
    backup.mkdir(parents=True)
    if config.exists():shutil.copy2(config,backup/'config.toml')
    command=codex_command()+['mcp','add','ap-vibe']
    custom=custom_config_path(config_path)
    if custom:command += ['--env', 'AP_VIBE_CONFIG_PATH='+str(custom)]
    command += ['--',sys.executable,str(root/'tools/ap_vibe_mcp.py')]
    result=subprocess.run(command,capture_output=True,timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        raise RuntimeError('Codex MCP 注册失败；原配置备份保留于 '+str(backup))
    actual=tomllib.loads(config.read_text(encoding='utf-8-sig')).get('mcp_servers',{}).get('ap-vibe',{})
    if (actual.get('command')!=sys.executable or actual.get('args')!=[str(root/'tools/ap_vibe_mcp.py')]
            or actual.get('env',{}).get('AP_VIBE_CONFIG_PATH')!=(str(custom) if custom else None)):
        raise RuntimeError('Codex MCP 注册回读与本次安装不一致；保留配置和备份，请核对目标安装路径。')
    return {'ok':True,'name':'ap-vibe','readback':True,'backup':str(backup),'script':str(root/'tools/ap_vibe_mcp.py')}


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path)
    print(json.dumps(install(parser.parse_args().config),ensure_ascii=False))
