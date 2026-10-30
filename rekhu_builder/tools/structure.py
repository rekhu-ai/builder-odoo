"""The database as it is built: the server, the modules and where they come from, the models with their fields and
the module behind each, the views and their inheritance, the menus, the access rules and the scheduled actions.
All of it is read as the superuser: the builder works for the developer, who sees the whole database."""

import ast
import inspect
import os
import sys
import time

from odoo import release
from odoo.tools import config

from ..lib import guard, state, workspace
from ..lib.guard import Refusal

APP_LIMIT = 300


def _module_path(name):
    return guard.module_root(name)


def _where(name):
    root = _module_path(name)
    return guard.kind_of(os.path.dirname(root)) if root else 'missing'


def models_of(env, module):
    """The models a module makes or adds fields to, from the external ids it owns."""
    data = env['ir.model.data'].sudo().search([('module', '=', module), ('model', 'in', ('ir.model', 'ir.model.fields'))])
    made = env['ir.model'].sudo().browse(data.filtered(lambda d: d.model == 'ir.model').mapped('res_id')).exists()
    fields_ = env['ir.model.fields'].sudo().browse(data.filtered(lambda d: d.model == 'ir.model.fields').mapped('res_id')).exists()
    return set(made.mapped('model')) | set(fields_.mapped('model'))


def _installed(env):
    return env['ir.module.module'].sudo().search([('state', 'in', ('installed', 'to upgrade'))])


def context(env, i, ctx):
    """The database as it is now, in one call: what the builder reads first in every turn."""
    if getattr(env, 'repair', False):
        return repair_context(env)
    mods = _installed(env)
    paths = []
    for p in guard.addons_paths():
        kind = guard.kind_of(p)
        try:
            count = sum(1 for n in os.listdir(p) if os.path.isfile(os.path.join(p, n, '__manifest__.py')))
        except OSError:
            count = 0
        paths.append({'path': p, 'kind': kind, 'modules': count, 'writable': p in guard.writable_roots(env), 'git': workspace.toplevel(p)})
    custom, rekhu = [], []
    for m in mods:
        root = _module_path(m.name)
        if root and guard.kind_of(os.path.dirname(root)) == 'custom':
            row = {'name': m.name, 'title': m.shortdesc, 'version': m.latest_version or m.installed_version, 'author': m.author, 'folder': os.path.dirname(root)}
            # Rekhu's own modules sit in a custom folder too, but they are not the developer's: never a model for theirs
            (rekhu if m.name.startswith('rekhu_') else custom).append(row)
    git = {}
    for p in paths:
        if p['git'] and p['git'] not in git:
            ok, branch = workspace.git(['rev-parse', '--abbrev-ref', 'HEAD'], p['git'])
            _ok, dirty = workspace.git(['status', '--porcelain'], p['git'])
            git[p['git']] = {'branch': branch.strip() if ok else None, 'changed_files': len([x for x in dirty.splitlines() if x.strip()])}
    env.cr.execute('SHOW server_version')
    postgres = env.cr.fetchone()[0]
    assistant = env['ir.module.module'].sudo().search([('name', '=', 'rekhu_assistant'), ('state', '=', 'installed')], limit=1)
    return {
        'odoo': release.version,
        'series': release.serie,
        'edition': 'enterprise' if mods.filtered(lambda m: m.name == 'web_enterprise') else 'community',
        'database': env.cr.dbname,
        'environment': state.environment(env),
        'writes': state.writes(env),
        'mode': state.mode(env),
        'url': state.base_url(env),
        'python': sys.version.split()[0],
        'postgres': postgres,
        'workers': config['workers'],
        'dev_mode': config.get('dev_mode') or [],
        'log': config['logfile'] or 'stdout (the builder keeps warnings and errors in its own file: logs-tail and logs-search read it)',
        'companies': env['res.company'].sudo().search_count([]),
        'languages': env['res.lang'].sudo().search([('active', '=', True)]).mapped('code'),
        'apps': [{'name': m.name, 'title': m.shortdesc} for m in mods.filtered('application')],
        'modules_installed': len(mods),
        'custom_modules': custom,
        'rekhu_modules': [r['name'] for r in rekhu],
        'addons_paths': paths,
        'git': git,
        'restart_needed': server_behind(env),
        'assistant': {'installed': bool(assistant), 'version': assistant.latest_version if assistant else None},
        'conventions': conventions(custom),
        'builder': state.VERSION,
    }


def repair_context(env):
    """What can be told while the database does not load: why, and where the files are."""
    paths = [{'path': p, 'kind': guard.kind_of(p), 'writable': p in guard.writable_roots(env), 'git': workspace.toplevel(p)} for p in guard.addons_paths()]
    return {'repair_mode': True, 'database': env.cr.dbname, 'loads': False, 'load_error': guard.clip(env.error or '', 4000), 'odoo': release.version, 'series': release.serie,
            'environment': state.environment(env), 'writes': state.writes(env), 'addons_paths': paths, 'builder': state.VERSION,
            'next': 'load_error says which module and file break the loading: read it with code-read, fix it, module-check, then module-upgrade; or rollback the run that broke it'}


def conventions(custom):
    """What the custom modules agree on, read from their manifests and folders: the builder follows it."""
    if not custom:
        return 'no custom modules yet: use Odoo\'s own conventions (models/, views/, security/ir.model.access.csv, or security/ir.access.csv on Odoo 20, tests/, LGPL-3 or the licence the developer names)'
    licences, authors, tests, versions = {}, {}, 0, set()
    for c in custom[:40]:
        root = _module_path(c['name'])
        try:
            with open(os.path.join(root, '__manifest__.py'), encoding='utf-8') as f:
                manifest = ast.literal_eval(f.read())
        except (OSError, ValueError, SyntaxError):
            continue
        licences[manifest.get('license', 'LGPL-3')] = licences.get(manifest.get('license', 'LGPL-3'), 0) + 1
        if manifest.get('author'):
            authors[manifest['author']] = authors.get(manifest['author'], 0) + 1
        tests += os.path.isdir(os.path.join(root, 'tests'))
        versions.add(str(manifest.get('version', '')).count('.'))
    top = lambda d: max(d, key=d.get) if d else None  # noqa: E731
    return {'licence': top(licences), 'author': top(authors), 'modules_with_tests': '%d of %d' % (tests, len(custom[:40])),
            'version_style': 'series-prefixed (%s.x.y.z)' % release.serie if 4 in versions else 'x.y.z (Odoo adds the series)'}


def server_behind(env):
    """Whether the Odoo server runs older Python than its files. The listener is a process of its own, so it asks the
    server; None when the server cannot be reached from here (server-restart says where it looked)."""
    from ..lib import server  # noqa: PLC0415
    if server.inside():
        return restart_needed()
    try:
        _url, answer = server.call(env, 'GET', server.PING, timeout=3)
    except RuntimeError:
        return None
    return bool(answer.get('restart_needed')) if answer else None


def restart_needed():
    """True when Python code of a loaded module changed after this process started: Odoo only reads Python at start."""
    started = _started()
    for name, mod in list(sys.modules.items()):
        if name.startswith('odoo.addons.') and getattr(mod, '__file__', None):
            try:
                if os.path.getmtime(mod.__file__) > started:
                    return True
            except OSError:
                continue
    return False


LOADED = time.time()


def _started():
    """When this process loaded the module's Python: the moment code changes after it are not running."""
    return LOADED


def module_list(env, i, ctx):
    """Modules with their state and origin."""
    domain = []
    if not i.get('available'):
        domain.append(('state', 'in', ('installed', 'to upgrade', 'to install')))
    if i.get('filter'):
        domain += ['|', ('name', 'ilike', i['filter']), ('shortdesc', 'ilike', i['filter'])]
    mods = env['ir.module.module'].sudo().search(domain, order='name', limit=int(i.get('limit') or 400))
    out = []
    for m in mods:
        where = _where(m.name)
        if i.get('custom') and where != 'custom':
            continue
        out.append({'name': m.name, 'title': m.shortdesc, 'state': m.state, 'version': m.latest_version or m.installed_version, 'author': m.author,
                    'where': where, 'application': m.application, 'depends': m.dependencies_id.mapped('name')})
    return {'modules': out, 'shown': len(out)}


def _field_row(model, name, field, defined):
    row = {'name': name, 'type': field.type, 'label': field.string}
    for attr in ('relation', 'comodel_name'):
        if getattr(field, attr, None):
            row['relation'] = getattr(field, attr)
            break
    if field.type == 'one2many':
        row['inverse'] = field.inverse_name
    if field.type == 'selection' and isinstance(field.selection, list):
        row['choices'] = [s[0] for s in field.selection][:30]
    for attr in ('required', 'readonly', 'index', 'translate', 'company_dependent'):
        if getattr(field, attr, False):
            row[attr] = True if getattr(field, attr) is True else str(getattr(field, attr))
    if not field.store:
        row['stored'] = False
    if field.compute:
        row['compute'] = field.compute if isinstance(field.compute, str) else getattr(field.compute, '__name__', 'yes')
    if field.related:
        row['related'] = '.'.join(field.related) if isinstance(field.related, (list, tuple)) else field.related
    if defined.get(name):
        row['modules'] = defined[name]
    return row


def models(env, i, ctx):
    """Without a model: the models, narrowed by text or module. With one: its whole structure."""
    name = (i.get('model') or '').strip()
    if not name:
        domain = []
        if i.get('filter'):
            domain += ['|', ('model', 'ilike', i['filter']), ('name', 'ilike', i['filter'])]
        if i.get('module'):
            domain.append(('model', 'in', list(models_of(env, i['module']))))
        rows = env['ir.model'].sudo().search(domain, order='model', limit=int(i.get('limit') or 300))
        return {'models': [{'model': r.model, 'name': r.name, 'transient': r.transient, 'modules': r.modules} for r in rows if r.model in env], 'shown': len(rows)}
    if name not in env:
        if env['ir.model'].sudo().search_count([('model', '=', name)]):
            raise Refusal('%s is in the database, but this server process has not loaded its Python yet: the Odoo server must restart (with --dev=reload it does so by itself)' % name)
        raise Refusal('there is no model %s in this database (or its module is not installed); models with filter finds it' % name)
    model = env[name].sudo()
    ir_fields = env['ir.model.fields'].sudo().search([('model', '=', name)])
    defined = {f.name: f.modules for f in ir_fields if f.modules}
    fields_out = [_field_row(model, n, f, defined) for n, f in sorted(model._fields.items()) if n not in ('id',) and not (i.get('module') and i['module'] not in (defined.get(n) or ''))]
    classes = []
    for cls in type(model).mro():
        module = getattr(cls, '_module', None)
        if module and cls.__module__.startswith('odoo.addons.'):
            try:
                source = guard.relative(inspect.getsourcefile(cls))
            except (TypeError, OSError):
                source = None
            classes.append({'module': module, 'file': source, 'where': _where(module)})
    env.cr.execute('SELECT conname, pg_get_constraintdef(c.oid) FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = %s AND c.contype IN (\'u\', \'c\')', (model._table,))
    constraints = [{'name': r[0], 'definition': r[1]} for r in env.cr.fetchall()]
    python_constraints = sorted({getattr(m, '__name__', str(m)) for m in model._constraint_methods})
    out = {
        'model': name,
        'description': model._description,
        'table': model._table if not model._abstract else None,
        'abstract': model._abstract,
        'transient': model._transient,
        'inherit': _parents(model),
        'inherits': dict(model._inherits),
        'rec_name': model._rec_name,
        'order': model._order,
        'records': model.with_context(active_test=False).search_count([]) if not model._abstract and not model._transient else None,
        'defined_in': classes,
        'fields': fields_out[: int(i.get('limit') or 400)],
        'sql_constraints': constraints,
        'python_constraints': python_constraints,
        'xml_id': env['ir.model.data'].sudo().search([('model', '=', 'ir.model'), ('res_id', '=', env['ir.model'].sudo()._get_id(name))], limit=1).complete_name,
    }
    return out


def graph(env, i, ctx):
    """The relations between models: of one module, or around one model, as edges. Mermaid text for a diagram."""
    names = set()
    if i.get('model'):
        if i['model'] not in env:
            raise Refusal('there is no model %s' % i['model'])
        names.add(i['model'])
        for f in env[i['model']]._fields.values():
            if f.type in ('many2one', 'one2many', 'many2many') and f.comodel_name:
                names.add(f.comodel_name)
    if i.get('module'):
        names |= models_of(env, i['module'])
    if not names:
        raise Refusal('name a model or a module')
    names = {n for n in names if n in env}
    edges, seen = [], set()
    for n in sorted(names):
        for fname, f in env[n]._fields.items():
            if f.type in ('many2one', 'many2many', 'one2many') and f.comodel_name in names and fname not in ('create_uid', 'write_uid'):
                key = (n, fname)
                if key not in seen:
                    seen.add(key)
                    edges.append({'from': n, 'field': fname, 'type': f.type, 'to': f.comodel_name, 'stored': bool(f.store)})
    arrows = {'many2one': '}o--||', 'one2many': '||--o{', 'many2many': '}o--o{'}
    mermaid = 'erDiagram\n' + '\n'.join('    %s %s %s : %s' % (e['from'].replace('.', '_'), arrows[e['type']], e['to'].replace('.', '_'), e['field']) for e in edges if e['type'] != 'one2many')
    return {'models': sorted(names), 'edges': edges[:600], 'mermaid': mermaid[:20000]}


def views(env, i, ctx):
    """One view with its inheritance, or the views of a model; "combined" gives the arch Odoo really shows."""
    View = env['ir.ui.view'].sudo()
    if i.get('view'):
        ref = str(i['view'])
        view = View.browse(int(ref)) if ref.isdigit() else env.ref(ref, raise_if_not_found=False)
        if not view or not view.exists() or view._name != 'ir.ui.view':
            raise Refusal('there is no view %s' % ref)

        def tree(v, depth=0):
            node = {'id': v.id, 'xml_id': v.xml_id, 'name': v.name, 'mode': v.mode, 'priority': v.priority, 'active': v.active, 'arch': guard.clip(v.arch_db or '', 20000)}
            if depth < 6:
                node['inherited_by'] = [tree(c, depth + 1) for c in v.with_context(active_test=False).inherit_children_ids]
            return node

        out = {'view': tree(view), 'model': view.model, 'type': view.type, 'inherits': view.inherit_id.xml_id or None}
        if i.get('combined', True) and view.model in env:
            root = view
            while root.inherit_id and root.mode == 'extension':
                root = root.inherit_id
            try:
                out['combined'] = guard.clip(_arch_text(root._get_combined_arch()), 60000)
            except Exception as error:  # noqa: BLE001 a broken inheritance is exactly what the developer asks about
                out['combined_error'] = str(error)
        return out
    model = (i.get('model') or '').strip()
    if not model:
        raise Refusal('name a model or a view')
    domain = [('model', '=', model)]
    if i.get('type'):
        domain.append(('type', '=', i['type']))
    rows = View.with_context(active_test=False).search(domain, order='type, priority, id')
    out = {'model': model, 'views': [{'id': v.id, 'xml_id': v.xml_id, 'name': v.name, 'type': v.type, 'mode': v.mode, 'priority': v.priority, 'inherits': v.inherit_id.xml_id or None, 'active': v.active} for v in rows[:300]]}
    if i.get('type') and model in env:
        try:
            got = env[model].sudo().get_views([(False, i['type'])])
            out['combined'] = guard.clip(got['views'][i['type']]['arch'], 60000)
        except Exception as error:  # noqa: BLE001 a broken view is exactly what the developer asks about
            out['combined_error'] = str(error)
    return out


def _arch_text(arch):
    from lxml import etree  # noqa: PLC0415
    return etree.tostring(arch, encoding='unicode', pretty_print=True)


def menus(env, i, ctx):
    """Menus with their action, by text, app or model."""
    rows = env['ir.ui.menu'].sudo().with_context(active_test=False).search([])
    wanted = str(i.get('filter') or '').lower()
    out = []
    for m in rows:
        if len(out) >= int(i.get('limit') or 300):
            break
        if wanted and wanted not in (m.complete_name or '').lower():
            continue
        action = m.action
        res_model = getattr(action, 'res_model', None) if action else None
        if i.get('model') and res_model != i['model']:
            continue
        out.append({'id': m.id, 'xml_id': m.get_external_id().get(m.id) or None, 'path': m.complete_name, 'action': '%s,%s' % (action._name, action.id) if action else None,
                    'action_xml_id': action.get_external_id().get(action.id) if action else None, 'model': res_model,
                    'groups': m.groups_id.mapped('full_name') if 'groups_id' in m._fields else m.group_ids.mapped('full_name'), 'active': m.active})
    return {'menus': out, 'shown': len(out)}


def access(env, i, ctx):
    """Who may do what on a model: the access rights and the record rules; or the groups of a user."""
    out = {}
    if i.get('user'):
        user = env['res.users'].sudo().with_context(active_test=False).search([('login', '=', i['user'])], limit=1)
        if not user:
            raise Refusal('there is no user with login %s' % i['user'])
        groups = user.all_group_ids if 'all_group_ids' in user._fields else user.groups_id
        out['user'] = {'login': user.login, 'active': user.active, 'share': user.share, 'groups': sorted(groups.mapped('full_name'))}
    if i.get('model'):
        model_id = env['ir.model'].sudo()._get_id(i['model'])
        if 'ir.access' in env:
            # Odoo 20: one model for access rights and record rules; a row with a domain is a rule
            acl = env['ir.access'].sudo().with_context(active_test=False).search([('model_id', '=', model_id)])
            out['access'] = [{'name': a.name, 'xml_id': a.get_external_id().get(a.id) or None, 'group': a.group_id.full_name or 'everyone',
                              'operation': a.operation, 'domain': a.domain or None, 'active': a.active} for a in acl]
            if not acl and i['model'] in env and not env[i['model']]._abstract:
                out['warning'] = 'this model has no access rights: only the superuser can use it, and Odoo logs a warning at start'
            return out
        acl = env['ir.model.access'].sudo().with_context(active_test=False).search([('model_id', '=', model_id)])
        out['access'] = [{'name': a.name, 'xml_id': a.get_external_id().get(a.id) or None, 'group': a.group_id.full_name or 'everyone',
                          'read': a.perm_read, 'write': a.perm_write, 'create': a.perm_create, 'unlink': a.perm_unlink, 'active': a.active} for a in acl]
        rules = env['ir.rule'].sudo().with_context(active_test=False).search([('model_id', '=', model_id)])
        group_field = 'groups' if 'groups' in env['ir.rule']._fields else 'group_ids'
        out['rules'] = [{'name': r.name, 'xml_id': r.get_external_id().get(r.id) or None, 'domain': r.domain_force, 'groups': r[group_field].mapped('full_name') or 'global',
                         'read': r.perm_read, 'write': r.perm_write, 'create': r.perm_create, 'unlink': r.perm_unlink, 'active': r.active} for r in rules]
        if not acl and i['model'] in env and not env[i['model']]._abstract:
            out['warning'] = 'this model has no access rights: only the superuser can use it, and Odoo logs a warning at start'
    if not out:
        raise Refusal('name a model, a user (login) or both')
    return out


def crons(env, i, ctx):
    """The scheduled actions: when they run and whether they fail."""
    Cron = env['ir.cron'].sudo().with_context(active_test=False)
    rows = Cron.search([] if i.get('all') else [('active', '=', True)], order='nextcall')
    out = []
    for c in rows[:200]:
        row = {'id': c.id, 'xml_id': c.get_external_id().get(c.id) or None, 'name': c.cron_name if 'cron_name' in c._fields else c.name, 'active': c.active,
               'model': c.model_id.model, 'code': guard.clip(c.code or '', 600), 'every': '%s %s' % (c.interval_number, c.interval_type),
               'nextcall': str(c.nextcall), 'lastcall': str(c.lastcall) if 'lastcall' in c._fields and c.lastcall else None}
        for extra in ('failure_count', 'first_failure_date'):
            if extra in c._fields and c[extra]:
                row[extra] = str(c[extra])
        out.append(row)
    failing = [r for r in out if r.get('failure_count')]
    return {'crons': out, 'failing': len(failing)}


def _parents(model):
    """The models a model inherits from. Odoo 17 to 19 have _inherit on the model's class; Odoo 20 refuses it there and
    keeps it, as _inherit__, on the classes the model was put together from."""
    try:
        own = model._inherit
        return list(own) if isinstance(own, (list, tuple)) else ([own] if own else [])
    except AttributeError:
        names = []
        for cls in type(model).__mro__:
            value = cls.__dict__.get('_inherit__', cls.__dict__.get('_inherit'))
            for parent in ([value] if isinstance(value, str) else value or ()):
                if isinstance(parent, str) and parent not in names:
                    names.append(parent)
        return names
