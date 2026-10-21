import logging

from odoo import _, fields, models
from odoo.exceptions import UserError

from ..lib import guard, platform, state

_logger = logging.getLogger(__name__)

ENVIRONMENTS = [('local', 'Local'), ('staging', 'Staging'), ('production', 'Production')]
WRITES = [('auto', 'Automatic'), ('branch', 'On a git branch'), ('on', 'In the files, with a backup'), ('off', 'Never, propose only')]
MODES = [('auto', 'Automatic'), ('direct', 'Direct'), ('relay', 'Relay (the listener)')]


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    rekhu_builder_key = fields.Char('Key', config_parameter='rekhu_builder.key', help='The key (agk_…) from the Connection tab of your Odoo project in your Rekhu account.')
    rekhu_builder_platform = fields.Char('Rekhu address for the builder', config_parameter='rekhu_builder.platform', default='https://api.rekhu.ai')
    rekhu_builder_environment = fields.Selection(ENVIRONMENTS, 'This database is', config_parameter='rekhu_builder.environment', help='On production the builder reads and proposes only, unless writes are allowed below. On staging and production every change waits for your yes in the chat.')
    rekhu_builder_writes = fields.Selection(WRITES, 'Changes to the modules', config_parameter='rekhu_builder.writes', default='auto')
    rekhu_builder_production_writes = fields.Boolean('Allow changes on production', config_parameter='rekhu_builder.production_writes')
    rekhu_builder_mode = fields.Selection(MODES, 'Connection', config_parameter='rekhu_builder.mode', default='auto')
    rekhu_builder_addons = fields.Char('Folders it may write', config_parameter='rekhu_builder.addons', help='Custom addons folders, separated by commas. Empty: every custom folder of the addons path this server can write.')

    rekhu_builder_status = fields.Text('Now', compute='_compute_rekhu_builder_status')

    def _compute_rekhu_builder_status(self):
        env = self.env
        roots = guard.writable_roots(env)
        lines = [
            _('Environment: %s · changes: %s · connection: %s', state.environment(env), state.writes(env), state.mode(env)),
            _('Writes in: %s', ', '.join(roots) if roots else _('no custom addons folder this server can write')),
        ]
        for settings in self:
            settings.rekhu_builder_status = '\n'.join(lines)

    def set_values(self):
        super().set_values()
        if state.key(self.env) and state.mode(self.env) == 'direct':
            try:
                self.env['rekhu.builder.seen']._rekhu_report()
            except Exception as error:  # noqa: BLE001 saving the settings never fails on the network; Connect says why
                _logger.warning('rekhu builder: reporting the tools failed: %s', error)

    def action_rekhu_builder_connect(self):
        """Checks the key and tells Rekhu what this database can do and how to reach it."""
        self.ensure_one()
        self.set_values()
        try:
            stored = self.env['rekhu.builder.seen']._rekhu_report()
        except platform.Refused as error:
            raise UserError(str(error)) from error
        except Exception as error:  # noqa: BLE001 the network, a proxy, a wrong address
            raise UserError(_('Rekhu could not be reached at %(address)s: %(error)s', address=state.platform(self.env), error=error)) from error
        relay = state.mode(self.env) == 'relay'
        message = _('Connected: %s tools reported.', stored.get('stored', 0))
        if relay:
            message += ' ' + _('This server is not reachable from the internet, so keep the listener running: odoo rekhu_listen -d %s', self.env.cr.dbname)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {'type': 'success', 'title': _('Rekhu Builder'), 'message': message, 'sticky': relay}}
