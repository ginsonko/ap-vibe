"""Short, atomic idle window for a code update; never discard queued work."""
from contextlib import closing
import time
from .contracts import ContractError


class StudioMaintenance:
    def __init__(self, studio):
        self.studio, self.registry = studio, studio.registry
        with closing(self.registry._connect()) as c:
            c.execute('CREATE TABLE IF NOT EXISTS studio_maintenance(id INTEGER PRIMARY KEY, owner TEXT, expires REAL)')
            c.commit()

    def active(self, c=None):
        if c is None:
            with closing(self.registry._connect()) as connection:
                return self.active(connection)
        row = c.execute('SELECT owner,expires FROM studio_maintenance WHERE id=1').fetchone()
        return bool(row and row['expires'] > time.time())

    def change(self, raw):
        owner = raw.get('owner')
        if not isinstance(owner, str) or not 1 <= len(owner) <= 160:
            raise ContractError('maintenance_owner_required')
        if raw.get('action') not in {'acquire','release'}:
            raise ContractError('maintenance_action_invalid')
        with self.studio._lock, self.registry.transaction():
            c = self.registry._connect()
            row = c.execute('SELECT owner,expires FROM studio_maintenance WHERE id=1').fetchone()
            if row and row['expires'] > time.time() and row['owner'] != owner:
                return {'ok':True,'acquired':False,'reason':'another_update'}
            if raw['action'] == 'release':
                c.execute('DELETE FROM studio_maintenance WHERE id=1 AND owner=?',(owner,))
                return {'ok':True,'released':True}
            runs = c.execute("SELECT COUNT(*) FROM studio_runs WHERE state IN ('starting','running','cancelling')").fetchone()[0]
            images = c.execute("SELECT COUNT(*) FROM studio_image_requests WHERE state IN ('reserved','submitted')").fetchone()[0]
            processes = sum(p.poll() is None for p in self.studio._processes.values())
            if runs or images or processes:
                return {'ok':True,'acquired':False,'reason':'busy','runs':runs,'images':images,'processes':processes}
            expires = time.time() + 120
            c.execute('INSERT OR REPLACE INTO studio_maintenance VALUES (1,?,?)',(owner,expires))
            return {'ok':True,'acquired':True,'owner':owner,'expires':expires}
