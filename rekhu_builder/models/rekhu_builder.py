import logging
from datetime import timedelta

from odoo import api, fields, models

from ..lib import platform, state

_logger = logging.getLogger(__name__)


class RekhuBuilderSeen(models.Model):
    """The signatures of the platform's calls, kept ten minutes: a call that comes twice is refused, in every worker."""

    _name = 'rekhu.builder.seen'
    _description = 'Rekhu Builder: calls already taken'
    _log_access = False

    name = fields.Char(required=True, index=True)
    at = fields.Datetime(required=True, default=fields.Datetime.now)

    if hasattr(models, 'Constraint'):
        _name_unique = models.Constraint('unique(name)', 'A call is taken once.')
    else:
        _sql_constraints = [('name_unique', 'unique(name)', 'A call is taken once.')]

    @api.model
    def _first(self, signature):
        """True the first time a signature is seen."""
        self.env.cr.execute('INSERT INTO rekhu_builder_seen (name, at) VALUES (%s, now() at time zone \'utc\') ON CONFLICT (name) DO NOTHING RETURNING id', (signature,))
        return bool(self.env.cr.fetchone())

    @api.model
    def _rekhu_hourly(self):
        """Forgets old signatures, and tells Rekhu again what this database can do (the tools change with an upgrade)."""
        self.env.cr.execute('DELETE FROM rekhu_builder_seen WHERE at < %s', (fields.Datetime.now() - timedelta(minutes=10),))
        if state.key(self.env) and state.mode(self.env) == 'direct':
            try:
                self._rekhu_report()
            except Exception as error:  # noqa: BLE001 the next hour tries again
                _logger.warning('rekhu builder: reporting the tools failed: %s', error)

    @api.model
    def _rekhu_report(self):
        from ..tools import registry  # noqa: PLC0415
        return platform.report(self.env, registry.definitions())
