"""Doing things in the database, beside building modules: a file from the project's Files panel brought into Odoo as
an attachment, and one method of one model called, the way a button in Odoo calls it. An import wizard the builder
built is run this way: the file attached, the wizard made with its settings, its button pressed, first as a dry run.

Every call changes data, so it waits for the developer's yes outside a local database, and it is refused where the
builder may not change anything (production, unless allowed). Data changes are not part of a run's undo: a wizard's
dry run comes first, its report goes to the developer, and only after their yes the real run."""

import base64
import json
import mimetypes
import re

from ..lib import guard, platform
from ..lib.guard import Refusal

# models whose changes go another way (modules through module-upgrade) or that hold secrets and users' access
CLOSED = {'ir.module.module', 'ir.config_parameter', 'res.users', 'res.users.apikeys', 'ir.attachment', 'ir.rule', 'ir.model.access',
          'res.groups', 'ir.cron', 'ir.actions.server', 'ir.ui.view', 'base.module.upgrade', 'base.module.uninstall'}
# what never runs through a call: deleting records, and switching the user or environment the call runs in
CLOSED_METHODS = {'unlink', 'sudo', 'with_user', 'with_env', 'browse', 'invalidate_cache', 'flush', 'flush_model', 'init'}
METHOD = re.compile(r'^[a-z][a-z0-9_]{1,80}$')
UUID = re.compile(r'^[0-9a-f-]{36}$', re.I)
TEXT_LIMIT = 20000


def project_file(env, i, ctx):
    """A file of the project's Files panel (its id from project-uploads) attached in Odoo, to give to a wizard."""
    file_id = str(i.get('id') or '').strip()
    if not UUID.match(file_id):
        raise Refusal('give the id of a file in the project\'s Files panel, as project-uploads lists it')
    try:
        data, name = platform.download(env, file_id)
    except platform.Refused as refused:
        raise Refusal('that file could not be fetched from the Rekhu account: %s' % refused) from refused
    name = str(i.get('name') or name or file_id)[:200]
    attachment = env['ir.attachment'].sudo().create({
        'name': name,
        'datas': base64.b64encode(data),
        'mimetype': mimetypes.guess_type(name)[0] or 'application/octet-stream',
        'description': 'From the Rekhu project files, for the builder',
    })
    return {'attachment': attachment.id, 'name': name, 'size': len(data),
            'use': 'give it to a binary field of odoo-call "values" as {"attachment": %d}' % attachment.id}


def call(env, i, ctx):
    """One public method of one model: on records by "ids", or on a record made first from "values" (a wizard)."""
    name = str(i.get('model') or '').strip()
    method = str(i.get('method') or '').strip()
    if name not in env:
        raise Refusal('there is no model %s' % name)
    if name in CLOSED or name.startswith('rekhu.'):
        raise Refusal('%s is not changed through a call: modules go through module-upgrade, settings and access rights into a module' % name)
    if not METHOD.match(method) or method.startswith('_') or method in CLOSED_METHODS:
        raise Refusal('"%s" cannot be called: only public methods of a model, the ones its buttons and wizards call' % method)
    model = env[name].sudo().with_context(**(i.get('context') or {}))
    if not callable(getattr(type(model), method, None)):
        raise Refusal('%s has no method %s' % (name, method))
    values = i.get('values')
    if values is not None and not isinstance(values, dict):
        raise Refusal('"values" is an object of field names and values')
    if values is not None:
        records = model.create(_values(env, model, values))
    elif i.get('ids'):
        records = model.browse([int(x) for x in i['ids']]).exists()
        if not records:
            raise Refusal('none of those %s records exist' % name)
    else:
        records = model
    args, kwargs = i.get('args') or [], i.get('kwargs') or {}
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise Refusal('"args" is a list and "kwargs" an object')
    returned = getattr(records, method)(*args, **kwargs)
    records.env.flush_all()
    out = {'model': name, 'method': method, 'ids': records.ids, 'returned': _shape(returned)}
    if records:
        # a wizard writes its report into its own fields: the developer reads it from here
        out['records'] = _read(records)
    return out


def _values(env, model, values):
    """The values for a new record, with a file given as {"attachment": id} read into its binary field: base64, as
    Odoo's binary fields and most wizards take it, or the file's own bytes with "raw" true, for a field that reads them
    as they are (Odoo's own import, base_import.import)."""
    out = {}
    for field, value in values.items():
        if field not in model._fields:
            raise Refusal('%s has no field "%s"' % (model._name, field))
        if isinstance(value, dict) and 'attachment' in value:
            attachment = env['ir.attachment'].sudo().browse(int(value['attachment'])).exists()
            if not attachment:
                raise Refusal('there is no attachment %s; bring the file in with project-file first' % value['attachment'])
            value = attachment.raw if value.get('raw') else attachment.datas
        out[field] = value
    return out


def _read(records):
    """The records' own fields after the call, files and long texts left out or cut, secrets masked."""
    skip = ('create_uid', 'write_uid', 'create_date', 'write_date', '__last_update')
    fields = [f for f, d in records._fields.items() if d.store and d.type not in ('binary', 'image', 'one2many', 'many2many') and f not in skip]
    rows = []
    for row in records[:20].read(fields):
        clean = {}
        for key, value in row.items():
            if isinstance(value, str) and guard.SECRET.search(key):
                value = '•••• (set)'
            elif isinstance(value, str) and len(value) > TEXT_LIMIT:
                value = value[:TEXT_LIMIT] + ' … (cut)'
            elif isinstance(value, tuple) and len(value) == 2:
                value = {'id': value[0], 'name': value[1]}
            clean[key] = value
        rows.append(clean)
    return rows


def _shape(value):
    """What a method returned, as the builder can read it: a record set by its records, an action by what it opens."""
    if hasattr(value, '_name') and hasattr(value, 'ids'):
        return {'model': value._name, 'ids': value.ids[:200], 'names': [r.display_name for r in value[:20]]}
    if isinstance(value, dict) and value.get('type', '').startswith('ir.actions'):
        keep = ('type', 'name', 'res_model', 'res_id', 'view_mode', 'target', 'tag', 'url', 'params')
        return {k: v for k, v in value.items() if k in keep}
    try:
        json.dumps(value, default=str)
        return value
    except (TypeError, ValueError):
        return str(value)[:2000]
