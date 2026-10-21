"""The data, read only: records through the ORM, counts and spreads of a field before it is redesigned, and plain SQL
in a read-only transaction. Data is never changed here: changes go into a module (data files or a migration script)
and reach the database with module-upgrade.

Personal and secret values are masked: contact details unless the developer asks for that field by name, secrets
(passwords, tokens, keys) always."""

import re

from psycopg2 import sql as pgsql

from ..lib import guard
from ..lib.guard import Refusal

SECRET = re.compile(r'password|passwd|token|secret|api_?key|totp|signature|private|oauth|access_key', re.I)
PERSONAL = re.compile(r'email|phone|mobile|street|zip|city|vat|iban|acc_number|birth|ssn|passport|identification|ip_address|login', re.I)
OPERATORS = {'=', '!=', '>', '>=', '<', '<=', 'like', 'ilike', 'not like', 'not ilike', 'in', 'not in', 'child_of', 'parent_of', '=like', '=ilike', 'any', 'not any'}
MAX_ROWS = 200


def _mask(name, value, asked=()):
    if value in (None, False, '') or isinstance(value, bool):
        return value
    if SECRET.search(name):
        return '•••• (set)'
    if PERSONAL.search(name) and name not in asked:
        return '•••• (personal; ask for the field by name to see it)'
    return value


def _domain(model, given):
    out = []
    for part in given or []:
        if part in ('&', '|', '!'):
            out.append(part)
            continue
        if not isinstance(part, (list, tuple)) or len(part) != 3:
            raise Refusal('a domain is a list of [field, operator, value] and "&", "|", "!"')
        field, operator, value = part
        if not isinstance(field, str) or field.split('.')[0] not in model._fields:
            raise Refusal('%s has no field "%s"' % (model._name, field))
        if operator not in OPERATORS:
            raise Refusal('the operator "%s" is not allowed' % operator)
        out.append((field, operator, value))
    return out


def _plain(value):
    if isinstance(value, tuple) and len(value) == 2:
        return {'id': value[0], 'name': value[1]}
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return value


def records(env, i, ctx):
    name = (i.get('model') or '').strip()
    if name not in env:
        raise Refusal('there is no model %s' % name)
    model = env[name].sudo().with_context(active_test=not i.get('archived'))
    domain = _domain(model, i.get('domain'))
    if i.get('group_by'):
        group = str(i['group_by'])
        if group.split(':')[0] not in model._fields:
            raise Refusal('%s has no field "%s"' % (name, group))
        rows = model._read_group(domain, groupby=[group], aggregates=['__count'], limit=MAX_ROWS)
        return {'model': name, 'group_by': group, 'groups': [{'value': _mask(group, getattr(g, 'display_name', g) if hasattr(g, '_name') else _plain(g)), 'count': c} for g, c in rows]}
    asked = [f for f in (i.get('fields') or []) if f in model._fields]
    names = [f for f in asked if model._fields[f].type != 'binary'] or [n for n, f in model._fields.items() if f.store and f.type not in ('binary', 'one2many', 'many2many', 'html', 'properties', 'properties_definition', 'json') and not n.startswith(('message_', 'activity_', 'website_message'))][:15]
    order = i.get('order')
    if order and not all(p.strip().split(' ')[0] in model._fields for p in str(order).split(',')):
        raise Refusal('the order names a field the model does not have')
    limit = max(1, min(int(i.get('limit') or 20), MAX_ROWS))
    total = model.search_count(domain)
    rows = model.search_read(domain, names, limit=limit, order=order or None)
    return {'model': name, 'total': total, 'shown': len(rows), 'records': [{k: _mask(k, _plain(v), asked) for k, v in r.items()} for r in rows]}


def field_stats(env, i, ctx):
    """How a field is used: how many records have it set, how many distinct values, the commonest. What a redesign
    (a new type, required, a selection turned into a model) has to migrate."""
    name, field_name = (i.get('model') or '').strip(), (i.get('field') or '').strip()
    if name not in env or field_name not in env[name]._fields:
        raise Refusal('name a model and one of its fields')
    model = env[name].sudo()
    field = model._fields[field_name]
    if model._abstract or model._transient:
        raise Refusal('%s keeps no records' % name)
    out = {'model': name, 'field': field_name, 'type': field.type, 'stored': bool(field.store)}
    cr = env.cr
    if field.type == 'many2many' and field.store:
        cr.execute(pgsql.SQL('SELECT count(DISTINCT {a}), count(*), count(DISTINCT {b}) FROM {t}').format(a=pgsql.Identifier(field.column1), b=pgsql.Identifier(field.column2), t=pgsql.Identifier(field.relation)))
        records_set, links, distinct = cr.fetchone()
        out.update(records=model.with_context(active_test=False).search_count([]), records_set=records_set, links=links, distinct=distinct)
        return out
    if not field.store or field.type in ('one2many', 'binary'):
        out['records_set'] = model.with_context(active_test=False).search_count([(field_name, '!=', False)])
        out['records'] = model.with_context(active_test=False).search_count([])
        out['note'] = 'not a column: counted through the ORM; no spread of values'
        return out
    column, table = pgsql.Identifier(field_name), pgsql.Identifier(model._table)
    cr.execute(pgsql.SQL('SELECT count(*), count({c}), count(DISTINCT {c}) FROM {t}').format(c=column, t=table))
    total, set_, distinct = cr.fetchone()
    out.update(records=total, records_set=set_, empty=total - set_, distinct=distinct)
    if field.type not in ('text', 'html') and distinct:
        cr.execute(pgsql.SQL('SELECT {c}, count(*) FROM {t} WHERE {c} IS NOT NULL GROUP BY {c} ORDER BY count(*) DESC LIMIT 12').format(c=column, t=table))
        top = cr.fetchall()
        if field.type == 'many2one':
            names = dict(env[field.comodel_name].sudo().browse([v for v, _c in top]).exists().mapped(lambda r: (r.id, r.display_name)))
            top = [({'id': v, 'name': names.get(v)}, c) for v, c in top]
        elif field.translate and top and isinstance(top[0][0], dict):
            top = [(v.get('en_US') or next(iter(v.values()), None), c) for v, c in top]
        out['commonest'] = [{'value': _mask(field_name, _plain(v)), 'count': c} for v, c in top]
    if field.type in ('integer', 'float', 'monetary', 'date', 'datetime'):
        cr.execute(pgsql.SQL('SELECT min({c}), max({c}) FROM {t}').format(c=column, t=table))
        low, high = cr.fetchone()
        out.update(min=_plain(low), max=_plain(high))
    return out


def sql_read(env, i, ctx):
    """One read-only statement in a read-only transaction of its own, at most 15 seconds."""
    query = str(i.get('sql') or '').strip().rstrip(';').strip()
    if not re.match(r'^(select|with|explain|show)\b', query, re.I) or ';' in query:
        raise Refusal('one read-only statement: SELECT, WITH, EXPLAIN or SHOW, without a semicolon inside')
    limit = max(1, min(int(i.get('limit') or 50), MAX_ROWS))
    with env.registry.cursor() as cr:
        try:
            cr.execute('SET TRANSACTION READ ONLY')
            cr.execute('SET LOCAL statement_timeout = 15000')
            cr.execute(query, tuple(i.get('params') or ()))  # noqa: SQL the transaction is read only and time-limited
            columns = [d[0] for d in cr.description or []]
            rows = cr.fetchmany(limit + 1)
        except Exception as error:  # noqa: BLE001 the database's own message is what the builder needs
            raise Refusal('the database refused it: %s' % guard.clip(str(error), 1000)) from error
        finally:
            cr.rollback()
    return {'columns': columns, 'rows': [[_mask(c, _plain(v)) for c, v in zip(columns, r)] for r in rows[:limit]], 'more': len(rows) > limit}
