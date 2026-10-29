"""Building modules: a new module from a description of its models, a check of a module's files before it is loaded,
installing or upgrading it, its tests on a database of their own, git, and undoing a run."""

import ast
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

from odoo.tools import config

from ..lib import guard, repair, state, workspace
from ..lib.guard import Refusal
from . import code

FIELD_TYPES = {'char': 'Char', 'text': 'Text', 'html': 'Html', 'integer': 'Integer', 'float': 'Float', 'monetary': 'Monetary', 'boolean': 'Boolean',
               'date': 'Date', 'datetime': 'Datetime', 'selection': 'Selection', 'many2one': 'Many2one', 'one2many': 'One2many', 'many2many': 'Many2many', 'binary': 'Binary'}
PY_NAME = re.compile(r'^[a-z_][a-z0-9_]*$')
MODEL_NAME = re.compile(r'^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$')


# -- a new module ---------------------------------------------------------------------------------------------------

def _class_name(model):
    return ''.join(p.capitalize() for p in re.split(r'[._]', model))


def _choice(c):
    """One selection choice from what the model gives: "value", "value:Label", [value, label] or {value, label}."""
    if isinstance(c, dict):
        value = str(c.get('value') or c.get('key') or '')
        return (value, str(c.get('label') or value.replace('_', ' ').capitalize()))
    if isinstance(c, (list, tuple)) and c:
        return (str(c[0]), str(c[1]) if len(c) > 1 else str(c[0]).replace('_', ' ').capitalize())
    value, _sep, label = str(c).partition(':')
    value = value.strip()
    return (value, label.strip() or value.replace('_', ' ').capitalize())


def _field_line(f):
    name, kind = f.get('name', ''), str(f.get('type', '')).lower()
    if not PY_NAME.match(name) or kind not in FIELD_TYPES:
        raise Refusal('every field needs a name (snake_case) and a type, one of %s' % ', '.join(FIELD_TYPES))
    args = []
    if kind in ('many2one', 'one2many', 'many2many'):
        if not f.get('relation'):
            raise Refusal('%s is %s: it needs the related model as relation' % (name, kind))
        args.append(repr(f['relation']))
        if kind == 'one2many':
            if not f.get('inverse'):
                raise Refusal('%s is one2many: it needs inverse, the many2one field on %s that points back' % (name, f['relation']))
            args.append(repr(f['inverse']))
    if kind == 'selection':
        choices = f.get('choices') or []
        if not choices:
            raise Refusal('%s is a selection: it needs choices' % name)
        args.append(repr([_choice(c) for c in choices]))
    args.append(repr(f.get('string') or name.replace('_id', '').replace('_ids', '').replace('_', ' ').capitalize()))
    for attr in ('required', 'readonly', 'index', 'translate', 'tracking'):
        if f.get(attr):
            args.append('%s=True' % attr)
    if f.get('default_code'):
        args.append('default=%s' % f['default_code'])
    elif f.get('default') is not None:
        args.append('default=%r' % f['default'])
    if kind == 'many2one' and f.get('ondelete'):
        args.append('ondelete=%r' % f['ondelete'])
    if f.get('help'):
        args.append('help=%r' % f['help'])
    return '    %s = fields.%s(%s)' % (name, FIELD_TYPES[kind], ', '.join(args))


def _model_py(m, tracking):
    fields_ = list(m.get('fields') or [])
    if any(str(f.get('type')).lower() == 'monetary' for f in fields_) and not any(f.get('name') == 'currency_id' for f in fields_):
        fields_.append({'name': 'currency_id', 'type': 'many2one', 'relation': 'res.currency', 'string': 'Currency', 'default_code': 'lambda self: self.env.company.currency_id'})
    if not any(f.get('name') == 'name' for f in fields_) and not m.get('rec_name'):
        # a record is shown by its name: without a name field, by its first text or relation (a rental by its
        # equipment); a required name only when there is nothing else to show it by
        first = next((f['name'] for f in fields_ if str(f.get('type')).lower() in ('char', 'many2one')), None)
        if first:
            m = dict(m, rec_name=first)
        else:
            fields_.insert(0, {'name': 'name', 'type': 'char', 'required': True, 'string': 'Name'})
    inherit = ["'mail.thread'", "'mail.activity.mixin'"] if tracking else []
    lines = ['from odoo import fields, models', '', '', 'class %s(models.Model):' % _class_name(m['model']),
             '    _name = %r' % m['model'], '    _description = %r' % (m.get('description') or m['model'].replace('.', ' ').capitalize())]
    if inherit:
        lines.append('    _inherit = [%s]' % ', '.join(inherit))
    if m.get('order'):
        lines.append('    _order = %r' % m['order'])
    if m.get('rec_name'):
        lines.append('    _rec_name = %r' % m['rec_name'])
    lines.append('')
    lines += [_field_line(f) for f in fields_]
    return '\n'.join(lines) + '\n', fields_


def _views_xml(module, m, fields_, major):
    """List, form and search views and an action, laid out as Odoo's own: the name as the form's title, a "state"
    selection as the status bar, the other fields in two columns, lines in a tab, amounts with their currency."""
    lst = 'list' if major >= 18 else 'tree'
    xid = m['model'].replace('.', '_')
    names = {f['name'] for f in fields_}
    money = 'currency_id' in names
    shown = [f for f in fields_ if f['type'] not in ('one2many', 'many2many', 'html', 'text', 'binary') and f['name'] != 'currency_id'][:7]
    header = [f for f in fields_ if f['name'] == 'state' and f['type'] == 'selection']
    title = [f for f in fields_ if f['name'] == 'name' and f['type'] == 'char']
    body = [f for f in fields_ if f['type'] not in ('one2many', 'html', 'text') and f['name'] not in ('currency_id', 'state', 'name')]
    long_ = [f for f in fields_ if f['type'] in ('html', 'text')]
    lines = [f for f in fields_ if f['type'] == 'one2many']
    half = (len(body) + 1) // 2

    def field(f, indent):
        extra = ' widget="many2many_tags"' if f['type'] == 'many2many' else ''
        return '%s<field name="%s"%s/>' % (' ' * indent, f['name'], extra)

    out = ['<?xml version="1.0" encoding="utf-8"?>', '<odoo>',
           '    <record id="%s_view_%s" model="ir.ui.view">' % (xid, lst), '        <field name="name">%s.%s</field>' % (m['model'], lst),
           '        <field name="model">%s</field>' % m['model'], '        <field name="arch" type="xml">', '            <%s>' % lst]
    out += [field(f, 16) for f in shown]
    if money:
        out.append('                <field name="currency_id" column_invisible="1"/>' if major >= 17 else '                <field name="currency_id" invisible="1"/>')
    out += ['            </%s>' % lst, '        </field>', '    </record>', '',
            '    <record id="%s_view_form" model="ir.ui.view">' % xid, '        <field name="name">%s.form</field>' % m['model'],
            '        <field name="model">%s</field>' % m['model'], '        <field name="arch" type="xml">', '            <form>']
    if header:
        out += ['                <header>', '                    <field name="state" widget="statusbar" options="{\'clickable\': \'1\'}"/>', '                </header>']
    out.append('                <sheet>')
    if title:
        out += ['                    <div class="oe_title">', '                        <h1><field name="name" placeholder="%s"/></h1>' % (title[0].get('string') or 'Name'), '                    </div>']
    if body:
        out += ['                    <group>', '                        <group>'] + [field(f, 28) for f in body[:half]] + ['                        </group>', '                        <group>'] + [field(f, 28) for f in body[half:]]
        if money:
            out.append('                            <field name="currency_id" invisible="1"/>')
        out += ['                        </group>', '                    </group>']
    if lines or long_:
        out.append('                    <notebook>')
        for f in lines + long_:
            out += ['                        <page string="%s" name="%s">' % (f.get('string') or f['name'].replace('_', ' ').capitalize(), f['name']), field(f, 28), '                        </page>']
        out.append('                    </notebook>')
    out.append('                </sheet>')
    if m.get('tracking'):
        out.append('                <chatter/>' if major >= 18 else '                <div class="oe_chatter"><field name="message_follower_ids"/><field name="activity_ids"/><field name="message_ids"/></div>')
    out += ['            </form>', '        </field>', '    </record>', '',
            '    <record id="%s_view_search" model="ir.ui.view">' % xid, '        <field name="name">%s.search</field>' % m['model'],
            '        <field name="model">%s</field>' % m['model'], '        <field name="arch" type="xml">', '            <search>']
    out += [field(f, 16) for f in shown if f['type'] in ('char', 'many2one')][:4]
    groupable = [f for f in shown if f['type'] in ('many2one', 'selection')][:3]
    if groupable:
        # group-by filters straight in the search view: Odoo 19 no longer takes a <group> there, 17 and 18 take both
        out.append('                <separator/>')
        out += ['                <filter string="%s" name="group_%s" context="{\'group_by\': \'%s\'}"/>' % (f.get('string') or f['name'], f['name'], f['name']) for f in groupable]
    out += ['            </search>', '        </field>', '    </record>', '',
            '    <record id="%s_action" model="ir.actions.act_window">' % xid, '        <field name="name">%s</field>' % _plural(m.get('description') or m['model']),
            '        <field name="res_model">%s</field>' % m['model'], '        <field name="view_mode">%s,form</field>' % lst, '    </record>', '</odoo>']
    return '\n'.join(out) + '\n'


def scaffold(env, i, ctx):
    """A new module from a description: models with their fields, list, form and search views, an action and a menu
    per model, access rights, and a test that creates a record of each model."""
    name = str(i.get('name') or '')
    if not re.match(r'^[a-z][a-z0-9_]{2,63}$', name):
        raise Refusal('the module name is snake_case, at least three characters, as sale_extra')
    if guard.module_root(name):
        raise Refusal('a module %s exists already; change it with code-write and code-patch' % name)
    models_ = i.get('models') or []
    for m in models_:
        if not MODEL_NAME.match(str(m.get('model') or '')):
            raise Refusal('every model needs a dotted name, as library.book')
    depends = list(dict.fromkeys(['base'] + list(i.get('depends') or []) + (['mail'] if any(m.get('tracking') for m in models_) else [])))
    missing = [d for d in depends if not guard.module_root(d)]
    if missing:
        raise Refusal('these dependencies are not on this server: %s' % ', '.join(missing))
    major = state.odoo_major()
    group = i.get('group') or 'base.group_user'
    files = {}
    # Odoo 20 keeps access rights in ir.access (operation "crud"); 17 to 19 in ir.model.access
    access_file = 'security/ir.access.csv' if major >= 20 else 'security/ir.model.access.csv'
    data = [access_file]
    acl = ['id,name,model_id,group_id/id,operation,domain'] if major >= 20 else ['id,name,model_id:id,group_id:id,perm_read,perm_write,perm_create,perm_unlink']
    menus = []
    test_lines = []
    built = {m['model']: _model_py(m, m.get('tracking'))[1] for m in models_}
    for m in models_:
        py, fields_ = _model_py(m, m.get('tracking'))
        stem = m['model'].replace('.', '_')
        files['models/%s.py' % stem] = py
        files['views/%s_views.xml' % stem] = _views_xml(name, m, fields_, major)
        data.append('views/%s_views.xml' % stem)
        acl.append('access_%s_user,%s user,%s,%s,crud,' % (stem, m['model'], m['model'], group) if major >= 20 else 'access_%s_user,%s user,model_%s,%s,1,1,1,1' % (stem, m['model'], stem, group))
        menus.append('    <menuitem id="%s_menu" name="%s" parent="%s_menu_root" action="%s_action" sequence="%d"/>' % (stem, m.get('menu') or _plural(m.get('description') or m['model'].split('.')[-1].capitalize()), name, stem, 10 * (len(menus) + 1)))
        values = _values(fields_, built)
        test_lines += ['    def test_create_%s(self):' % stem, '        record = self.env[%r].create({%s})' % (m['model'], values), '        self.assertTrue(record.exists())', '']
    files['models/__init__.py'] = ''.join('from . import %s\n' % m['model'].replace('.', '_') for m in models_)
    files[access_file] = '\n'.join(acl) + '\n'
    if models_:
        files['views/menus.xml'] = '<?xml version="1.0" encoding="utf-8"?>\n<odoo>\n    <menuitem id="%s_menu_root" name="%s" sequence="90"/>\n%s\n</odoo>\n' % (name, i.get('title') or name.replace('_', ' ').title(), '\n'.join(menus))
        data.append('views/menus.xml')
        files['tests/__init__.py'] = 'from . import test_%s\n' % name
        files['tests/test_%s.py' % name] = 'from odoo.tests import TransactionCase, tagged\n\n\n@tagged(\'post_install\', \'-at_install\')\nclass Test%s(TransactionCase):\n\n%s' % (_class_name(name), '\n'.join(test_lines).rstrip() + '\n')
    files['__init__.py'] = 'from . import models\n' if models_ else ''
    # Odoo writes compiled Python next to the code; it never belongs in the repository
    files['.gitignore'] = '__pycache__/\n*.pyc\n'
    manifest = {'name': i.get('title') or name.replace('_', ' ').title(), 'summary': i.get('summary') or '', 'version': '%s.1.0.0' % state.series() if i.get('series_version') else '1.0.0',
                'category': i.get('category') or 'Uncategorized', 'author': i.get('author') or env.company.name or 'Odoo developer', 'license': i.get('license') or 'LGPL-3',
                'depends': depends, 'data': data, 'application': bool(i.get('application', bool(models_))), 'installable': True}
    files['__manifest__.py'] = _manifest_text(manifest)
    run = ctx['run']
    written = []
    for rel in sorted(files, key=lambda r: (r != '__manifest__.py', r)):
        full = guard.writable(env, '%s/%s' % (name, rel), i.get('folder'))
        workspace.begin(env, run, full)
        workspace.backup(env, run, full)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as f:
            f.write(files[rel])
        written.append(full)
    problems = check_module(env, {'module': name}, ctx)['problems']
    commit = workspace.commit(env, run, written, 'Add module %s' % name)
    return {'module': name, 'files': [guard.relative(f) for f in written], 'commit': commit, 'branch': workspace.branch_of(env, run, written[0]) if written else None, 'problems': problems,
            'next': 'read the files, adjust them, then module-upgrade with install true; after that tests-run'}


def _plural(word):
    """Menus name the records in the plural, as Odoo's do: Books, Categories."""
    if word.endswith('y') and word[-2:-1] not in 'aeiou':
        return word[:-1] + 'ies'
    return word if word.endswith('s') else word + 's'


def _values(fields_, built, depth=0):
    """The values of a test record, as Python source: every required field, and a record of the module's own model
    for a required relation to it."""
    out = []
    for f in fields_:
        if not f.get('required'):
            continue
        if f['type'] == 'many2one' and f.get('relation') in built and depth < 2:
            out.append('%r: self.env[%r].create({%s}).id' % (f['name'], f['relation'], _values(built[f['relation']], built, depth + 1)))
            continue
        value = _sample(f)
        if value is not None:
            out.append('%r: %s' % (f['name'], value))
    return ', '.join(out)


def _sample(f):
    """A value a test can create a record with, as Python source; None when the field needs none."""
    kind = f['type']
    if kind == 'selection':
        return repr(_choice(f['choices'][0])[0])
    if kind == 'many2one':
        # the first record there is; partners, users and companies always exist
        return "self.env[%r].search([], limit=1).id" % f['relation']
    return {'char': repr('Test'), 'text': repr('Test'), 'html': repr('<p>Test</p>'), 'integer': '1', 'float': '1.0', 'monetary': '1.0',
            'boolean': 'True', 'date': "'2026-01-01'", 'datetime': "'2026-01-01 10:00:00'"}.get(kind)


def _manifest_text(m):
    lines = ['{']
    for k, v in m.items():
        if isinstance(v, list) and v:
            lines.append('    %r: [' % k)
            lines += ['        %r,' % x for x in v]
            lines.append('    ],')
        else:
            lines.append('    %r: %r,' % (k, v))
    lines.append('}')
    return '\n'.join(lines) + '\n'


# -- checking a module ----------------------------------------------------------------------------------------------

# what one Odoo series refuses or no longer shows, often only in the browser: (since, before, file kind, pattern, what to do)
VERSION_TRAPS = [
    (19, None, '.xml', r't-name=["\']kanban-box["\']', 'Odoo 19 names the kanban card template t-name="card"; kanban-box is gone and the board fails in the browser'),
    (19, None, '.py', r'^\s*_sql_constraints\s*=', 'Odoo 19 has no _sql_constraints: use models.Constraint(\'…\', \'message\') attributes'),
    (18, None, '.xml', r'<tree[\s>]', 'Odoo 18 and 19 call list views <list>, not <tree>'),
    (18, None, '.xml', r'view_mode["\']?>[^<]*\btree\b', 'Odoo 18 and 19 name the list view mode "list", not "tree"'),
    (17, None, '.xml', r'\sattrs=|\sstates=', 'Odoo 17 and later have no attrs or states: write invisible, readonly and required as Python expressions'),
    (None, 18, '.xml', r'<list[\s>]', 'Odoo 17 calls list views <tree>, not <list>'),
    (None, 18, '.xml', r'<chatter\s*/>', 'Odoo 17 has no <chatter/>: use a div class="oe_chatter" with the mail fields'),
]


# JavaScript names an OWL template cannot use: OWL looks every other name up on the component, so Number(x) in a template
# becomes this.Number(x) and the screen fails in the browser ("ctx.Number is not a function"). Math, Date, Array, Object,
# console and window are the exceptions OWL allows.
OWL_GLOBALS = ('Number', 'String', 'Boolean', 'JSON', 'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'Intl', 'Symbol',
               'Promise', 'encodeURIComponent', 'decodeURIComponent', 'setTimeout', 'setInterval', 'document', 'location',
               'navigator', 'localStorage', 'sessionStorage', 'Map', 'Set', 'structuredClone')
OWL_GLOBAL_USE = re.compile(r'(?<![\w.$])(%s)\s*[.(]' % '|'.join(OWL_GLOBALS))


def _owl_globals(path, text):
    """Template expressions in an OWL .xml file under static/ that call a JavaScript global the template cannot see."""
    from lxml import etree  # noqa: PLC0415
    try:
        root = etree.fromstring(text.encode())
    except etree.XMLSyntaxError:
        return []
    out = []
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        for attr, value in node.attrib.items():
            if not attr.startswith('t-') or attr in ('t-name', 't-inherit', 't-inherit-mode', 't-ref', 't-key'):
                continue
            # quoted text inside an expression is not code
            code_only = re.sub(r"'[^']*'|\"[^\"]*\"|`[^`]*`", "''", value)
            m = OWL_GLOBAL_USE.search(code_only)
            if m:
                out.append('%s, line %d: %s="%s" uses %s, which an OWL template cannot call (it becomes this.%s and the screen fails in the browser): compute it in the component and use the result' % (
                    guard.relative(path), node.sourceline or 0, attr, guard.clip(value, 80), m.group(1), m.group(1)))
    return out


def _style_imports(path, text):
    """An @import of a web address in a module's styles: Odoo moves it to the top of the whole bundle and cuts it at its
    first semicolon, so an address with one (Google Fonts: wght@400;600) leaves an open url( and the browser drops
    every style of the backend, not only the module's."""
    out = []
    for m in re.finditer(r'@import\s+url\(\s*["\']?(https?:[^"\')]*)', text):
        advice = 'an @import of a web address in a module\'s styles is hoisted into every page\'s bundle'
        if ';' in m.group(1):
            advice = 'the address has a ";", where Odoo cuts the @import: the backend loses all its styles. Write the semicolons as %3B, or better, load the font with a <link> in a template or ship the font files in static/'
        out.append('%s, line %d: %s' % (guard.relative(path), text.count('\n', 0, m.start()) + 1, advice))
    return out


def _models_in(text):
    """The models a Python file defines: classes on models.Model or TransientModel with a _name, read from the code
    itself (not with a pattern, which a long class without a _name makes very slow)."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {b.attr for b in node.bases if isinstance(b, ast.Attribute)}
        if not bases & {'Model', 'TransientModel'}:
            continue
        for item in node.body:
            if isinstance(item, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_name' for t in item.targets) \
                    and isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                out.add(item.value.value)
    return out


def _search_groups(text):
    """The line of the first <group> inside a <search> view, or None."""
    from lxml import etree  # noqa: PLC0415
    try:
        root = etree.fromstring(text.encode())
    except etree.XMLSyntaxError:
        return None
    for group in root.iter('group'):
        if any(a.tag == 'search' for a in group.iterancestors()):
            return group.sourceline
    return None


def _traps(path, text):
    major = state.odoo_major()
    out = []
    if major >= 19 and path.endswith('.xml'):
        line = _search_groups(text)
        if line:
            out.append('%s, line %d: Odoo 19 takes no <group> in a search view: put the group-by filters straight in it' % (guard.relative(path), line))
    for since, before, kind, pattern, advice in VERSION_TRAPS:
        if (since and major < since) or (before and major >= before) or not path.endswith(kind):
            continue
        m = re.search(pattern, text, re.M)
        if m:
            out.append('%s, line %d: %s' % (guard.relative(path), text.count('\n', 0, m.start()) + 1, advice))
    return out


def check_module(env, i, ctx):
    """Everything that would stop the module from loading, found before it is loaded."""
    name = str(i.get('module') or '')
    root = guard.module_root(name)
    if not root:
        raise Refusal('there is no module %s on this server' % name)
    problems = []
    manifest_path = os.path.join(root, '__manifest__.py')
    try:
        with open(manifest_path, encoding='utf-8') as f:
            manifest = ast.literal_eval(f.read())
    except (OSError, ValueError, SyntaxError) as error:
        return {'module': name, 'problems': ['the manifest does not read: %s' % error]}
    for dep in manifest.get('depends', []):
        if not guard.module_root(dep):
            problems.append('depends on %s, which is not on this server' % dep)
    py_models, acl_models = set(), set()
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not guard.hidden(d) and d != 'static']
        for f in files:
            full = os.path.join(folder, f)
            if f.endswith(('.py', '.xml', '.csv')):
                problem = code.check(full)
                if problem:
                    problems.append('%s: %s' % (guard.relative(full), problem))
                elif f.endswith(('.py', '.xml')):
                    with open(full, encoding='utf-8', errors='replace') as fh:
                        problems += _traps(full, fh.read())
            if f.endswith('.py'):
                with open(full, encoding='utf-8', errors='replace') as fh:
                    text = fh.read()
                # every model with a _name of its own, abstract ones aside, needs access rights (transient ones too)
                py_models |= _models_in(text)
            if f == 'ir.model.access.csv':
                with open(full, encoding='utf-8') as fh:
                    acl_models |= {r.split(',')[2].strip().split('.')[-1] for r in fh.read().splitlines()[1:] if r.count(',') >= 3}
            elif f == 'ir.access.csv':
                # Odoo 20 names the model by its technical name (sale.order) or by the external id of its ir.model record
                with open(full, encoding='utf-8') as fh:
                    for r in fh.read().splitlines()[1:]:
                        if r.count(',') >= 3:
                            ref = r.split(',')[2].strip()
                            acl_models.add(ref.split('.')[-1] if ref.split('.')[-1].startswith('model_') else 'model_' + ref.replace('.', '_'))
    # OWL templates and styles are only read by the browser, so their mistakes show nowhere else
    static = os.path.join(root, 'static')
    for folder, dirs, files in os.walk(static):
        dirs[:] = [d for d in dirs if not guard.hidden(d) and d != 'lib']
        for f in files:
            full = os.path.join(folder, f)
            if f.endswith('.xml'):
                with open(full, encoding='utf-8', errors='replace') as fh:
                    problems += _owl_globals(full, fh.read())
            elif f.endswith(('.scss', '.css')):
                with open(full, encoding='utf-8', errors='replace') as fh:
                    problems += _style_imports(full, fh.read())
    # every folder with Python is imported by its parent's __init__.py
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not guard.hidden(d) and d not in ('static', 'tests', 'migrations', 'cli')]
        if folder == root:
            continue
        pys = [f[:-3] for f in files if f.endswith('.py') and f != '__init__.py']
        init = os.path.join(folder, '__init__.py')
        if pys and not os.path.isfile(init):
            problems.append('%s has Python files but no __init__.py' % guard.relative(folder))
        elif pys:
            with open(init, encoding='utf-8') as fh:
                text = fh.read()
            problems += ['%s is not imported in %s' % (p, guard.relative(init)) for p in pys if not re.search(r'\b%s\b' % re.escape(p), text)]
    for m in sorted(py_models):
        if 'model_' + m.replace('.', '_') not in acl_models and not _extends(env, m, name):
            problems.append('the model %s has no line in security/%s: only the superuser could use it' % (m, 'ir.access.csv' if 'ir.access' in env else 'ir.model.access.csv'))
    return {'module': name, 'version': manifest.get('version'), 'problems': problems, 'ok': not problems}


def _extends(env, model, module):
    """True when the model belongs to another module and this one only extends it (_name and _inherit the same)."""
    if getattr(env, 'repair', False):
        env.cr.execute("SELECT d.module FROM ir_model m JOIN ir_model_data d ON d.model = 'ir.model' AND d.res_id = m.id WHERE m.model = %s ORDER BY d.id LIMIT 1", (model,))
        row = env.cr.fetchone()
        return bool(row and row[0] != module)
    found = env['ir.model'].sudo().search([('model', '=', model)], limit=1)
    first = (found.modules or '').split(',')[0].strip() if found else ''
    return bool(first and first != module)


# -- loading it -----------------------------------------------------------------------------------------------------

def _odoo_command():
    for candidate in (shutil.which('odoo'), shutil.which('odoo-bin'), sys.argv[0] if os.path.basename(sys.argv[0]).startswith('odoo') else None):
        if candidate and os.access(candidate, os.X_OK):
            return [candidate]
    return None


def _server_args():
    """The options a second Odoo process needs to reach the same server: config file, addons, database host."""
    args = []
    if config.rcfile and os.path.isfile(config.rcfile):
        args += ['-c', config.rcfile]
    args.append('--addons-path=%s' % ','.join(guard.addons_paths()))
    for option in ('db_host', 'db_port', 'db_user'):
        if config.get(option):
            args.append('--%s=%s' % (option, config[option]))
    args.append('--data-dir=%s' % config['data_dir'])
    return args


def _env_vars():
    out = dict(os.environ)
    if config.get('db_password'):
        out['PGPASSWORD'] = str(config['db_password'])
    return out


def _python_changed(name):
    """True when the module's Python changed after this process loaded it: then the upgrade runs in a new process."""
    from .structure import _started  # noqa: PLC0415
    root = guard.module_root(name)
    loaded = 'odoo.addons.%s' % name in sys.modules
    if not root or not loaded:
        return False
    started = _started()
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not guard.hidden(d) and d != 'static']
        if any(f.endswith('.py') and os.path.getmtime(os.path.join(folder, f)) > started for f in files):
            return True
    return False


def upgrade(env, i, ctx):
    """Installs or upgrades a module. Checked first; on a failure the module states are put back as they were."""
    name = str(i.get('module') or '')
    if name in guard.OWN:
        raise Refusal('%s is Rekhu\'s own module; the developer upgrades it' % name)
    if not guard.module_root(name):
        raise Refusal('there is no module %s on this server' % name)
    checked = check_module(env, {'module': name}, ctx)
    if checked['problems'] and not i.get('force'):
        return {'module': name, 'upgraded': False, 'problems': checked['problems'], 'next': 'fix these first (or force true when one is wrong)'}
    if getattr(env, 'repair', False):
        # no ORM: the upgrade runs in a new Odoo process, which is also the test of whether the database loads again
        current = repair.module_state(env.cr, name)
        if current in (None, 'uninstalled', 'to install') and not i.get('install'):
            return {'module': name, 'upgraded': False, 'state': current, 'next': 'the module is not installed: call again with install true'}
        workspace.note_module(env, ctx['run'], name, installed=current in (None, 'uninstalled', 'to install'))
        return _upgrade_apart(env, name, current in (None, 'uninstalled', 'to install'), time.time())
    Module = env['ir.module.module'].sudo()
    Module.update_list()
    module = Module.search([('name', '=', name)], limit=1)
    if not module:
        raise Refusal('Odoo does not list %s after updating the apps list: is its folder in the addons path?' % name)
    install = module.state in ('uninstalled', 'to install')
    if install and not i.get('install'):
        return {'module': name, 'upgraded': False, 'state': module.state, 'next': 'the module is not installed: call again with install true'}
    workspace.note_module(env, ctx['run'], name, installed=install)
    started = time.time()
    if _python_changed(name) or i.get('separate'):
        return _upgrade_apart(env, name, install, started)
    env.cr.commit()
    try:
        (module.button_immediate_install if install else module.button_immediate_upgrade)()
    except Exception as error:  # noqa: BLE001 the loading error is what the developer needs, the database is put back
        env.cr.rollback()
        _reset_states(env)
        return {'module': name, 'upgraded': False, 'error': guard.clip(_explain(error), 6000), 'next': 'fix it, then module-upgrade again'}
    _refresh_assets(env)
    return {'module': name, 'upgraded': True, 'action': 'installed' if install else 'upgraded', 'seconds': round(time.time() - started, 1)}


def _upgrade_apart(env, name, install, started):
    """The upgrade in a new Odoo process, which reads the module's Python afresh; the running server must restart to
    use the new Python, which --dev=reload does by itself."""
    command = _odoo_command()
    if not command:
        return {'module': name, 'upgraded': False, 'error': 'the module\'s Python changed after this server started, and odoo-bin is not reachable from here to upgrade it apart',
                'next': 'restart the Odoo server, then module-upgrade again'}
    env.cr.commit()
    args = command + _server_args() + ['-d', env.cr.dbname, '--db-filter=.*', '--no-http', '--stop-after-init', '--log-level=warn', ('-i' if install else '-u'), name]
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=600, env=_env_vars(), check=False)
    except subprocess.TimeoutExpired:
        return {'module': name, 'upgraded': False, 'error': 'the upgrade took longer than 10 minutes'}
    out = (done.stdout or '') + (done.stderr or '')
    failed = done.returncode != 0 or re.search(r' (ERROR|CRITICAL) ', out)
    if failed:
        _reset_states(env)
    else:
        _refresh_assets(env)
    dev = 'reload' in (config.get('dev_mode') or [])
    return {'module': name, 'upgraded': not failed, 'seconds': round(time.time() - started, 1), 'apart': True,
            'log': guard.clip('\n'.join(l for l in out.splitlines() if re.search(r' (WARNING|ERROR|CRITICAL) |Traceback|Error', l) or l.startswith(' ')), 8000) or None,
            'restart_needed': not dev and not failed,
            'next': None if failed else ('the server reloads by itself (--dev=reload)' if dev else 'restart the Odoo server so it runs the new Python')}


def _refresh_assets(env):
    """After an upgrade the browser must get the module's new JavaScript, templates and styles: the compiled bundles are
    dropped and every Odoo process is told to forget the ones it holds, so the next page load builds them afresh."""
    try:
        if getattr(env, 'repair', False):
            env.cr.execute("DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%%'")
            return
        with env.registry.cursor() as cr:
            cr.execute("DELETE FROM ir_attachment WHERE url LIKE '/web/assets/%%'")
        env.registry.clear_cache('assets')
        env.registry.signal_changes()
    except Exception:  # noqa: BLE001 a stale bundle is a nuisance, never a reason to fail an upgrade that worked
        pass


def _reset_states(env):
    def reset(cr):
        cr.execute("UPDATE ir_module_module SET state = 'installed' WHERE state IN ('to upgrade', 'to remove')")
        cr.execute("UPDATE ir_module_module SET state = 'uninstalled' WHERE state = 'to install'")
    if getattr(env, 'repair', False):
        reset(env.cr)
        return
    with env.registry.cursor() as cr:
        reset(cr)


def _explain(error):
    import traceback  # noqa: PLC0415
    text = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    # the end of the trace says what broke; Odoo's ParseError carries the file and the line of the XML
    return text[-6000:]


# -- tests ----------------------------------------------------------------------------------------------------------

def _jobs(env):
    folder = os.path.join(workspace.home(env), 'tests')
    os.makedirs(folder, exist_ok=True)
    return folder


def _free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def tests(env, i, ctx):
    """Runs a module's tests in a new Odoo process on a database of their own (<database>_rekhu_test), so the
    developer's data stays as it is. Starts in the background; the same call with the job waits for the result."""
    folder = _jobs(env)
    job = str(i.get('job') or '')
    if not job:
        name = str(i.get('module') or '')
        if not guard.module_root(name):
            raise Refusal('there is no module %s on this server' % name)
        command = _odoo_command()
        if not command:
            raise Refusal('odoo-bin is not reachable from this server process; run the tests in CI, or on the server: odoo -d test_db -i %s --test-tags /%s --stop-after-init' % (name, name))
        for other in os.listdir(folder):
            if other.endswith('.json'):
                with open(os.path.join(folder, other), encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('state') == 'running' and _alive(data.get('pid')):
                    raise Refusal('tests of %s are still running (job %s); wait for them with that job' % (data['module'], data['job']))
        job = uuid.uuid4().hex[:12]
        tags = str(i.get('tags') or '/%s' % name)
        database = '%s_rekhu_test' % env.cr.dbname
        log = os.path.join(folder, job + '.log')
        args = command + _server_args() + ['-d', database, '--db-filter=.*', '-i', name, '-u', name, '--test-tags', tags, '--stop-after-init',
                                           '--http-port=%d' % _free_port(), '--log-level=test']
        with open(log, 'w', encoding='utf-8') as out:
            process = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, env=_env_vars(), start_new_session=True)  # noqa: S603 our own odoo-bin with our own arguments
        data = {'job': job, 'module': name, 'tags': tags, 'database': database, 'pid': process.pid, 'state': 'running', 'started': time.time()}
        with open(os.path.join(folder, job + '.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f)
    wait = max(0, min(int(i.get('wait') if i.get('wait') is not None else 90), 100))
    until = time.time() + wait
    while True:
        result = _job_state(folder, job)
        if result['state'] != 'running' or time.time() >= until:
            return result
        time.sleep(2)


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:
        done, _status = os.waitpid(int(pid), os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def _job_state(folder, job):
    path = os.path.join(folder, re.sub(r'[^a-f0-9]', '', job) + '.json')
    if not os.path.isfile(path):
        raise Refusal('there is no test job %s' % job)
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
    with open(os.path.join(folder, data['job'] + '.log'), encoding='utf-8', errors='replace') as f:
        log = f.read()
    running = data['state'] == 'running' and _alive(data['pid'])
    if running and time.time() - data['started'] > 1800:
        try:
            os.killpg(int(data['pid']), signal.SIGTERM)
        except OSError:
            pass
        running = False
        log += '\nstopped after 30 minutes'
    out = {'job': data['job'], 'module': data['module'], 'tags': data['tags'], 'database': data['database'], 'seconds': round(time.time() - data['started'])}
    if running:
        out.update(state='running', last=guard.clip('\n'.join(log.splitlines()[-5:]), 2000), next='call tests-run again with this job to wait for the result')
        return out
    summary = re.findall(r'odoo\.tests\.result: (\d+) failed, (\d+) error\(s\) of (\d+) tests', log)
    failures = re.findall(r'(?:^|\n)\S+ \S+ \d+ (?:ERROR|CRITICAL) \S+ \S+: (?:FAIL|ERROR): (.+?)(?=\n\S+ \S+ \d+ (?:INFO|WARNING|ERROR|CRITICAL|RUNBOT) |\Z)', log, re.S)
    errors = [l for l in log.splitlines() if re.search(r' (ERROR|CRITICAL) ', l)]
    if summary:
        failed, errored, total = map(int, summary[-1])
        out.update(state='passed' if not failed and not errored and total else 'failed', tests=total, failed=failed, errors=errored)
    else:
        out.update(state='failed', tests=0, note='no test result in the log: the module did not load, or it has no tests for these tags')
    # a skipped test is not a passed one: browser tours skip quietly when the server has no Chrome
    skipped = re.findall(r' skipped (\S+)\s*:\s*(.+?)\s*$', log, re.M)
    if skipped:
        out['skipped'] = [{'test': t, 'reason': guard.clip(r, 200)} for t, r in skipped[:20]]
        out['note'] = ('%d test(s) did not run (skipped), so they prove nothing: %s. Tell the developer which ones and why, '
                       'and never report a screen as tested when its tour was skipped.') % (len(skipped), '; '.join(sorted({r for _, r in skipped}))[:300])
    if failures:
        out['failures'] = [guard.clip(f.strip(), 4000) for f in failures[:10]]
    elif errors and out['state'] == 'failed':
        out['log'] = guard.clip('\n'.join(errors[-30:]), 8000)
    data['state'] = out['state']
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f)
    return out


# -- git and undo ---------------------------------------------------------------------------------------------------

def git(env, i, ctx):
    action = i.get('action') or 'status'
    tops = sorted({t for t in (workspace.toplevel(r) for r in guard.writable_roots(env)) if t})
    if i.get('module'):
        root = guard.module_root(i['module'])
        top = workspace.toplevel(root) if root else None
        if not top:
            raise Refusal('%s is not in a git working copy' % i['module'])
        tops = [top]
    if not tops:
        return {'git': 'the custom addons folders are not git working copies (or git is not installed on this server)'}
    args = {'diff': ['diff', 'HEAD', '--stat', '--patch'], 'log': ['log', '-20', '--format=%h %ad %an: %s', '--date=short'],
            'branches': ['branch', '--list', '--sort=-committerdate', '--format=%(refname:short) %(committerdate:relative)']}.get(action, ['status', '--short', '--branch'])
    out = {}
    for top in tops:
        ok, text = workspace.git(args, top)
        out[top] = guard.clip(text.strip(), 150000)
    return {'action': action, 'repositories': out}


def rollback(env, i, ctx):
    """Undoes a run: the modules it installed are uninstalled first (their files are about to go), the files are put
    back, and the modules it upgraded are upgraded again so the database matches the files."""
    if i.get('list'):
        return {'runs': workspace.runs(env)}
    run = workspace.run_id(i.get('run')) if i.get('run') else ctx['run']
    data = workspace.record(env, run)
    uninstalled = []
    for name in ([] if getattr(env, 'repair', False) else data.get('installed', [])):
        module = env['ir.module.module'].sudo().search([('name', '=', name), ('state', '=', 'installed')], limit=1)
        if module:
            env.cr.commit()
            module.button_immediate_uninstall()
            uninstalled.append(name)
    done = workspace.rollback(env, run)
    upgraded = []
    for name in done['modules']:
        installed = repair.module_state(env.cr, name) == 'installed'
        if guard.module_root(name) and installed:
            result = upgrade(env, {'module': name, 'force': True}, ctx)
            upgraded.append({'module': name, 'upgraded': result.get('upgraded'), 'error': result.get('error'), 'restart_needed': result.get('restart_needed')})
    return dict(done, run=run, uninstalled=uninstalled, upgraded=upgraded)
