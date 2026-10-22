"""What the module knows about itself and the database it lives in: the key, how the platform reaches it, which
environment this is and what the builder may change here. Everything is a system parameter, set in Settings →
Rekhu Builder; nothing is read from the request."""

import os
import re
import secrets
from urllib.parse import urlparse

from odoo import release

VERSION = '1.0.2'
KEY = re.compile(r'^agk_([a-z0-9-]+)_([a-z0-9-]+)_[A-Za-z0-9_-]+$')
PARAM = 'rekhu_builder.'
ROUTE = '/rekhu/builder/run'
ENVIRONMENTS = ('local', 'staging', 'production')


def param(env, name, default=None):
    return env['ir.config_parameter'].sudo().get_param(PARAM + name, default)


def key(env):
    """The key's parts: agk_<customer>_<agent>_<secret>. None when there is no usable key."""
    m = KEY.match(str(param(env, 'key') or '').strip())
    return {'key': m.group(0), 'customer': m.group(1), 'agent': m.group(2)} if m else None


def secret(env):
    """The secret the platform signs its calls with; made once, on first use, and handed over when the tools are reported."""
    value = param(env, 'secret')
    if not value:
        value = secrets.token_urlsafe(48)
        env['ir.config_parameter'].sudo().set_param(PARAM + 'secret', value)
    return value


def platform(env):
    return (param(env, 'platform') or 'https://api.rekhu.ai').rstrip('/')


def base_url(env):
    return (env['ir.config_parameter'].sudo().get_param('web.base.url') or '').rstrip('/')


def environment(env):
    """local, staging or production. The setting wins; on Odoo.sh the build's stage; otherwise production, the safe guess."""
    chosen = param(env, 'environment')
    if chosen in ENVIRONMENTS:
        return chosen
    stage = os.environ.get('ODOO_STAGE')
    if stage:
        return {'dev': 'local', 'staging': 'staging', 'production': 'production'}.get(stage, 'staging')
    return 'production'


def mode(env):
    """relay or direct: how the platform reaches this database. Automatic: direct only for a public https address."""
    chosen = param(env, 'mode') or 'auto'
    if chosen in ('relay', 'direct'):
        return chosen
    url = urlparse(base_url(env))
    host = url.hostname or ''
    private = (not host or host == 'localhost' or host.endswith(('.test', '.local', '.localhost', '.internal'))
               or re.match(r'^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)', host) is not None or '.' not in host)
    return 'relay' if private or url.scheme != 'https' else 'direct'


def writes(env):
    """off, branch or on: what the builder may change, after the production rule."""
    if environment(env) == 'production' and param(env, 'production_writes') not in ('1', 'True', 'true'):
        return 'off'
    from . import workspace  # noqa: PLC0415 git is looked up only when it matters
    chosen = param(env, 'writes') or 'auto'
    git = workspace.has_git(env)
    if chosen in ('off', 'on'):
        return chosen
    if chosen == 'branch':
        return 'branch' if git else 'on'
    return 'branch' if git else 'on'


def series():
    return release.serie


def odoo_major():
    return int(str(release.serie).split('.')[0].replace('saas~', '') or 0)
