"""User-visible update controls over the existing detached release worker."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from .contracts import ContractError
from .organization_runner import _write_json


class StudioUpdates:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.next_poll = 0

    def tick(self):
        # A busy update must resume when work ends even if no new prompt
        # arrives. No model calls or repeated network polling are involved.
        if time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + 30
        status = self.status()
        if (status.get('mode') == 'automatic' and status.get('state') in {'busy','ready','pending'}
                and status.get('next_check_at',0) <= time.time()):
            try:
                self.action({'action':'check'})
            except (OSError, ContractError):
                pass

    def installation(self):
        default = Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'AP-Vibe/config.json'
        path = Path(os.environ.get('AP_VIBE_CONFIG_PATH') or default).resolve()
        try:
            config = json.loads(path.read_text(encoding='utf-8-sig'))
            if config.get('product') != 'AP-Vibe' or Path(config['data_dir']).resolve() != self.service.data_dir.resolve():
                raise ValueError('different installation')
            return path, config
        except (OSError, ValueError, KeyError, TypeError):
            raise ContractError('update_installation_unavailable')

    def status(self):
        try:
            path, config = self.installation()
        except ContractError:
            return {'ok':True, 'available':False, 'state':'unmanaged', 'message':'此服务尚未使用安装器管理，自动更新不可用。'}
        options = dict(config.get('updates') or {})
        status = {}
        for filename, target in [('update-options.json', options), ('update-status.json', status)]:
            try:
                target.update(json.loads((path.parent / filename).read_text(encoding='utf-8-sig')))
            except (OSError, ValueError, TypeError):
                pass
        mode = 'off' if options.get('enabled') is False else 'check' if options.get('auto_install') is False else 'automatic'
        return {'ok':True, 'available':True, 'mode':mode, 'channel':options.get('channel','beta'),
                'current_version':config.get('installed_version'),
                'state':'disabled' if mode == 'off' else status.get('state','not_checked'),
                **{k:status[k] for k in ('available_version','checked_at','next_check_at','message') if k in status},
                'integration_issues':(status.get('result') or {}).get('integration_issues', [])}

    def action(self, raw):
        with self.lock:
            path, config = self.installation()
            if raw.get('action') == 'configure':
                if raw.get('mode') not in {'automatic','check','off'} or raw.get('channel','beta') not in {'beta','stable'}:
                    raise ContractError('update_settings_invalid')
                # A separate preferences file cannot overwrite a concurrent
                # installer's config/product_root switch.
                _write_json(path.parent/'update-options.json', {'enabled':raw['mode']!='off',
                    'auto_install':raw['mode']=='automatic','channel':raw.get('channel','beta')})
            elif raw.get('action') == 'check':
                if self.status()['mode'] == 'off':
                    return self.status()
                script = Path(config['product_root'])/'tools/update_client.py'
                if not script.is_file():
                    raise ContractError('update_worker_missing')
                subprocess.Popen([config['python'], '-X', 'utf8', str(script), 'check','--config',str(path),'--force'],
                    cwd=config['product_root'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)
                return {**self.status(),'requested':True}
            else:
                raise ContractError('update_action_invalid')
            return self.status()
