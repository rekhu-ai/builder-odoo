"""The modules' code: list, read and search in every module on the server; write only in the custom modules, with a
backup and, in branch mode, a commit on the run's branch. Every written file is checked at once (Python compiles,
XML parses, the manifest reads, the access CSV has its columns), so a typo is found before the module is upgraded."""

import ast
import csv
import io
import os
import re

from ..lib import guard, workspace
from ..lib.guard import Refusal

TEXT = ('.py', '.xml', '.csv', '.js', '.scss', '.css', '.md', '.rst', '.txt', '.po', '.pot', '.json', '.yml', '.yaml', '.html', '.toml', '.cfg', '.ini')


def check(full, text=None):
    """None when the file is fine, otherwise what is wrong with it."""
    if text is None:
        with open(full, encoding='utf-8', errors='replace') as f:
            text = f.read()
    name = os.path.basename(full)
    try:
        if name == '__manifest__.py':
            manifest = ast.literal_eval(text)
            if not isinstance(manifest, dict) or not manifest.get('name'):
                return 'the manifest is not a dict with at least a name'
            root = os.path.dirname(full)
            missing = [d for d in manifest.get('data', []) + manifest.get('demo', []) if not os.path.isfile(os.path.join(root, d))]
            if missing:
                return 'the manifest names data files that do not exist: %s' % ', '.join(missing)
        elif full.endswith('.py'):
            compile(text, full, 'exec')
        elif full.endswith('.xml'):
            from lxml import etree  # noqa: PLC0415
            etree.fromstring(text.encode())
        elif name == 'ir.model.access.csv':
            rows = list(csv.reader(io.StringIO(text)))
            need = ['id', 'name', 'model_id:id', 'group_id:id', 'perm_read', 'perm_write', 'perm_create', 'perm_unlink']
            if not rows or [c.strip() for c in rows[0]] != need:
                return 'the first line of ir.model.access.csv must be: %s' % ','.join(need)
            bad = [str(n + 1) for n, r in enumerate(rows[1:], 1) if r and len(r) != len(need)]
            if bad:
                return 'ir.model.access.csv lines with the wrong number of columns: %s' % ', '.join(bad)
        elif name == 'ir.access.csv':
            # Odoo 20: access rights and record rules in one model; operation is a subset of "crud"
            rows = list(csv.reader(io.StringIO(text)))
            head = [c.strip() for c in rows[0]] if rows else []
            if not head or head[:2] != ['id', 'name'] or 'operation' not in head or not {'model_id', 'model_id:id', 'model_id/id'} & set(head):
                return 'the first line of ir.access.csv must be: id,name,model_id,group_id/id,operation,domain'
            bad = [str(n + 1) for n, r in enumerate(rows[1:], 1) if r and len(r) != len(head)]
            if bad:
                return 'ir.access.csv lines with the wrong number of columns: %s' % ', '.join(bad)
            op = head.index('operation')
            # a selection: the letters in the order c, r, u, d (crud, cru, rd, r …)
            wrong = [str(n + 1) for n, r in enumerate(rows[1:], 1) if r and (not r[op].strip() or r[op].strip() != ''.join(ch for ch in 'crud' if ch in r[op]))]
            if wrong:
                return 'ir.access.csv lines whose operation is not crud or a part of it in that order (cru, rd, r): %s' % ', '.join(wrong)
    except SyntaxError as error:
        return 'Python syntax error, line %s: %s' % (error.lineno, error.msg)
    except ValueError as error:
        return 'the manifest is not a plain Python dict: %s' % error
    except Exception as error:  # noqa: BLE001 lxml's XMLSyntaxError and its kin
        return 'not valid: %s' % error
    return None


def list_files(env, i, ctx):
    path = str(i.get('path') or '').strip('/')
    if not path:
        roots = []
        for root in guard.addons_paths():
            kind = guard.kind_of(root)
            if kind != 'custom' and not i.get('all'):
                continue
            names = sorted(n for n in os.listdir(root) if os.path.isfile(os.path.join(root, n, '__manifest__.py')))
            roots.append({'folder': root, 'kind': kind, 'writable': root in guard.writable_roots(env), 'modules': names[:400]})
        return {'folders': roots, 'note': 'custom folders only; all true adds Odoo\'s and Enterprise\'s' if not i.get('all') else None}
    full = guard.readable(path)
    if os.path.isfile(full):
        raise Refusal('%s is a file; use code-read' % path)
    depth = max(1, min(int(i.get('depth') or 3), 6))
    out = []
    start = full.count(os.sep)
    for folder, dirs, files in os.walk(full):
        dirs[:] = sorted(d for d in dirs if not guard.hidden(d))
        level = folder.count(os.sep) - start
        if level >= depth:
            dirs[:] = []
        rel = guard.relative(folder)
        for f in sorted(files):
            if not guard.hidden(f):
                out.append('%s/%s (%d B)' % (rel, f, os.path.getsize(os.path.join(folder, f))))
        if len(out) > 1500:
            break
    return {'path': path, 'files': out[:1500]}


def read(env, i, ctx):
    path = str(i.get('path') or '')
    full = guard.readable(path)
    if os.path.isdir(full):
        raise Refusal('%s is a folder; use code-list' % path)
    if os.path.getsize(full) > 2_000_000:
        raise Refusal('%s is larger than 2 MB; read part of it with from and to, or search in it' % path)
    with open(full, encoding='utf-8', errors='replace') as f:
        lines = f.read().split('\n')
    start = max(1, int(i.get('from') or 1))
    end = min(len(lines), int(i.get('to') or start + 799))
    text = '\n'.join('%5d  %s' % (n, lines[n - 1]) for n in range(start, end + 1))
    return {'path': path, 'lines': len(lines), 'from': start, 'to': end, 'text': guard.clip(text, 300000)}


def search(env, i, ctx):
    try:
        pattern = re.compile(str(i.get('pattern') or ''))
    except re.error as error:
        raise Refusal('a valid regular expression is needed as pattern: %s' % error) from error
    if not i.get('pattern'):
        raise Refusal('a pattern is needed')
    limit = max(1, min(int(i.get('limit') or 100), 400))
    if i.get('path'):
        starts = [guard.readable(i['path'])]
    else:
        # the custom modules by default; "all" searches Odoo's too, which is large
        starts = [os.path.join(r, n) for r in guard.addons_paths() if i.get('all') or guard.kind_of(r) == 'custom' for n in os.listdir(r) if os.path.isfile(os.path.join(r, n, '__manifest__.py'))]
    hits = []
    for start in starts:
        walker = [(os.path.dirname(start), [], [os.path.basename(start)])] if os.path.isfile(start) else os.walk(start)
        for folder, dirs, files in walker:
            dirs[:] = [d for d in dirs if not guard.hidden(d) and (i.get('static') or d not in ('static', 'i18n'))]
            for f in files:
                if guard.hidden(f) or not f.endswith(TEXT):
                    continue
                full = os.path.join(folder, f)
                try:
                    with open(full, encoding='utf-8', errors='replace') as fh:
                        for n, line in enumerate(fh, 1):
                            if pattern.search(line):
                                hits.append('%s:%d: %s' % (guard.relative(full), n, line.rstrip()[:300]))
                                if len(hits) >= limit:
                                    return {'hits': hits, 'more': True}
                except OSError:
                    continue
    return {'hits': hits, 'more': False}


def _write(env, ctx, path, text, message, root=None):
    full = guard.writable(env, path, root)
    if os.path.isdir(full):
        raise Refusal('%s is a folder' % path)
    run = ctx['run']
    workspace.begin(env, run, full)
    problem = check(full, text)
    created = not os.path.exists(full)
    workspace.backup(env, run, full)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w', encoding='utf-8') as f:
        f.write(text)
    commit = workspace.commit(env, run, [full], message or '%s %s' % ('Add' if created else 'Change', path))
    return {'path': guard.relative(full), 'created': created, 'bytes': len(text.encode()), 'commit': commit, 'branch': workspace.branch_of(env, run, full), 'check': problem or 'ok',
            'next': 'module-upgrade to load it' + ('; Python changed, so the server must restart first (or run with --dev=reload)' if full.endswith('.py') else '')}


def write(env, i, ctx):
    if 'content' not in i:
        raise Refusal('the whole file is needed as content')
    return _write(env, ctx, i.get('path'), str(i['content']), i.get('message'), i.get('folder'))


def patch(env, i, ctx):
    path = str(i.get('path') or '')
    full = guard.writable(env, path)
    if not os.path.isfile(full):
        raise Refusal('%s does not exist; use code-write to create it' % path)
    with open(full, encoding='utf-8') as f:
        text = f.read()
    find, replace = str(i.get('find') or ''), str(i.get('replace') or '')
    if not find:
        raise Refusal('the exact text to find is needed')
    count = text.count(find)
    if not count:
        raise Refusal('the text to find is not in %s (read the file again: it must match exactly, spaces included)' % path)
    if count > 1 and not i.get('all'):
        raise Refusal('the text occurs %d times in %s; give more of the surrounding text, or set all' % (count, path))
    return _write(env, ctx, path, text.replace(find, replace), i.get('message'))


def delete(env, i, ctx):
    path = str(i.get('path') or '')
    full = guard.writable(env, path)
    if not os.path.isfile(full):
        raise Refusal('%s is not a file' % path)
    run = ctx['run']
    workspace.begin(env, run, full)
    workspace.backup(env, run, full)
    os.remove(full)
    commit = workspace.commit(env, run, [full], i.get('message') or 'Remove %s' % path)
    return {'path': path, 'deleted': True, 'commit': commit, 'branch': workspace.branch_of(env, run, full), 'note': 'if the manifest or an __init__.py names it, change that too'}
