"""Trust only the exact AP-Vibe hook definitions installed with user authorization."""
from pathlib import Path
import argparse
import json
import os
import shutil
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.codex_rpc import CodexRpc
from tools.installation import hook_commands


def own_hooks(groups, source, expected_commands):
    return [hook for group in groups for hook in group.get('hooks',[])
            if Path(hook.get('sourcePath','')).resolve()==source.resolve()
            and hook.get('handlerType')=='command' and hook.get('command') in expected_commands]


def trust_installed(config_path: Path | None = None):
    root=Path(__file__).resolve().parents[1]
    codex_root=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    source=codex_root/'hooks.json'
    expected=set(hook_commands(root,sys.executable,config_path).values())
    with CodexRpc() as rpc:
        listed=rpc.request('hooks/list',{'cwds':[str(root)]})
        ours=own_hooks(listed.get('data',[]),source,expected)
        if len(ours)!=4 or {h['eventName'] for h in ours}!={'sessionStart','userPromptSubmit','subagentStart','stop'}:
            raise RuntimeError('AP-Vibe hook 与本次安装的准确命令或事件不一致，未变更信任设置')
        edits=[{'keyPath':'hooks.state.'+json.dumps(h['key'])+'.trusted_hash','mergeStrategy':'upsert','value':h['currentHash']}
               for h in ours if h.get('trustStatus')!='trusted']
        backup=None
        if edits:
            backup=codex_root/'ap-vibe-install-backups'/uuid.uuid4().hex
            backup.mkdir(parents=True)
            config=codex_root/'config.toml'
            if config.exists():shutil.copy2(config,backup/'config.toml')
            rpc.request('config/batchWrite',{'edits':edits,'filePath':str(config),'reloadUserConfig':True})
        checked=own_hooks(rpc.request('hooks/list',{'cwds':[str(root)]}).get('data',[]),source,expected)
        if len(checked)!=4 or not all(h.get('trustStatus')=='trusted' for h in checked):raise RuntimeError('AP-Vibe hook 信任记录回读未通过')
        return {'ok':True,'trusted_hooks':[h['eventName'] for h in checked],'backup':str(backup) if backup else None}


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf8')
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path)
    print(json.dumps(trust_installed(parser.parse_args().config),ensure_ascii=False))
