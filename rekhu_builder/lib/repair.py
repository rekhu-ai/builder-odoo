"""Repair mode: the database does not load (a module's Python or XML is broken halfway through a change), so there is
no ORM. The builder still reaches the files, git, the checks, the log, the upgrade in a separate process and the undo:
enough to repair the change it was making. These tools see a small stand-in for the environment that reads and
writes the system parameters with plain SQL."""

from types import SimpleNamespace

from .guard import Refusal

# the tools that work without the ORM
TOOLS = {'context', 'code-list', 'code-read', 'code-search', 'code-write', 'code-patch', 'code-delete', 'module-check', 'module-upgrade', 'server-restart', 'git', 'rollback', 'logs-tail', 'logs-search'}


class _Params:
    def __init__(self, cr):
        self.cr = cr

    def sudo(self):
        return self

    def get_param(self, name, default=False):
        self.cr.execute('SELECT value FROM ir_config_parameter WHERE key = %s', (name,))
        row = self.cr.fetchone()
        return row[0] if row else default

    def set_param(self, name, value):
        self.cr.execute('UPDATE ir_config_parameter SET value = %s WHERE key = %s', (value, name))
        if not self.cr.rowcount:
            self.cr.execute('INSERT INTO ir_config_parameter (key, value) VALUES (%s, %s)', (name, value))


class RepairEnv:
    """What the file, git and module tools use of an environment, while the database does not load."""

    repair = True

    def __init__(self, cr, error):
        self.cr = cr
        self.error = error
        cr.execute('SELECT name FROM res_company ORDER BY id LIMIT 1')
        row = cr.fetchone()
        name = row[0] if row else cr.dbname
        self.company = SimpleNamespace(name=name.get('en_US', next(iter(name.values()), '')) if isinstance(name, dict) else name)

    def __getitem__(self, model):
        if model == 'ir.config_parameter':
            return _Params(self.cr)
        raise Refusal('the database does not load right now, so %s cannot be read: repair the module with the file tools, module-check and module-upgrade first (the error is in context)' % model)

    def __contains__(self, model):
        return False


def module_state(cr, name):
    cr.execute('SELECT state FROM ir_module_module WHERE name = %s', (name,))
    row = cr.fetchone()
    return row[0] if row else None
