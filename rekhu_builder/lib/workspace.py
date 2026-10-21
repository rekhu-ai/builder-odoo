"""Where the builder's changes go and how they are undone. Every run that writes gets a record in
<data_dir>/rekhu_builder/<database>/runs/<run>.json and a backup of each file before its first change, so a rollback
puts everything back. With write mode "branch" the changes also go to a git branch rekhu/<run> of the repository the
module lives in, and are committed there: only the files the builder changed, never the developer's own work."""

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone

from odoo.tools import config

from .guard import Refusal, clip, writable_roots

GIT_USER = ['-c', 'user.name=Rekhu', '-c', 'user.email=agent@rekhu.com']


def git(args, cwd, timeout=30):
    """Runs git; returns (ok, output)."""
    try:
        done = subprocess.run(['git', *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    return done.returncode == 0, (done.stdout or '') + (done.stderr if done.returncode else '')


def toplevel(path):
    """The repository a folder belongs to, or None (no git, or not a working copy)."""
    if not shutil.which('git'):
        return None
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    while folder and not os.path.isdir(folder):
        folder = os.path.dirname(folder)
    ok, out = git(['rev-parse', '--show-toplevel'], folder)
    return os.path.realpath(out.strip()) if ok and out.strip() else None


def has_git(env):
    return any(toplevel(root) for root in writable_roots(env))


def run_id(run):
    run = re.sub(r'[^A-Za-z0-9-]', '', str(run or ''))[:36]
    return run or 'manual-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')


def home(env):
    folder = os.path.join(config['data_dir'], 'rekhu_builder', env.cr.dbname)
    os.makedirs(os.path.join(folder, 'runs'), exist_ok=True)
    return folder


def _file(env, run):
    return os.path.join(home(env), 'runs', run + '.json')


def record(env, run):
    path = _file(env, run)
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {'run': run, 'files': [], 'created': [], 'branches': {}, 'modules': [], 'started': datetime.now(timezone.utc).isoformat()}


def save(env, data):
    with open(_file(env, data['run']), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def runs(env, limit=30):
    """The runs that changed something, newest first."""
    folder = os.path.join(home(env), 'runs')
    out = []
    for name in sorted(os.listdir(folder), key=lambda n: os.path.getmtime(os.path.join(folder, n)), reverse=True)[:limit]:
        if name.endswith('.json'):
            with open(os.path.join(folder, name), encoding='utf-8') as f:
                data = json.load(f)
            out.append({'run': data['run'], 'started': data.get('started'), 'files': len(data['files']), 'created': len(data['created']),
                        'branches': [b['branch'] for b in data['branches'].values()], 'modules': data.get('modules', []), 'rolled_back': data.get('rolled_back')})
    return out


def begin(env, run, full):
    """Refuses when writing is off; in branch mode switches the file's repository to the run's branch."""
    from . import state  # noqa: PLC0415
    mode = state.writes(env)
    if mode == 'off':
        raise Refusal('writing is off for this database (%s). Propose the change as a diff instead; the developer can allow writes in Settings → Rekhu Builder.' % state.environment(env))
    data = record(env, run)
    top = toplevel(full) if mode == 'branch' else None
    if top and top not in data['branches']:
        branch = 'rekhu/' + run
        ok, current = git(['rev-parse', '--abbrev-ref', 'HEAD'], top)
        exists, _out = git(['rev-parse', '--verify', '--quiet', 'refs/heads/' + branch], top)
        ok, out = git(['checkout', branch] if exists else ['checkout', '-b', branch], top)
        if not ok:
            raise Refusal('could not switch %s to branch %s: %s' % (top, branch, clip(out, 400)))
        data['branches'][top] = {'branch': branch, 'from': current.strip()}
        save(env, data)
    return data


def backup(env, run, full):
    """Keeps the file as it was before the run's first change to it, or notes that it did not exist."""
    data = record(env, run)
    if full in [f['path'] for f in data['files']] or full in data['created']:
        return
    if os.path.isfile(full):
        copy = os.path.join(home(env), 'runs', run, str(len(data['files'])))
        os.makedirs(os.path.dirname(copy), exist_ok=True)
        shutil.copy2(full, copy)
        data['files'].append({'path': full, 'backup': copy})
    else:
        data['created'].append(full)
    save(env, data)


def commit(env, run, fulls, message):
    """Commits the run's changed files on its branch (branch mode only); returns the short commit id."""
    data = record(env, run)
    done = None
    by_top = {}
    for full in fulls:
        top = next((t for t in data['branches'] if full.startswith(t + os.sep)), None)
        if top:
            by_top.setdefault(top, []).append(os.path.relpath(full, top))
    for top, paths in by_top.items():
        git(['add', '-A', '--', *paths], top)
        ok, _out = git([*GIT_USER, 'commit', '-m', str(message or 'Rekhu Builder')[:200], '--', *paths], top)
        if ok:
            done = git(['rev-parse', '--short', 'HEAD'], top)[1].strip()
    return done


def branch_of(env, run, full):
    """The branch the run's changes to this file are on, or None (no git, or write mode "on")."""
    for top, b in record(env, run)['branches'].items():
        if full.startswith(top + os.sep):
            return b['branch']
    return None


def note_module(env, run, name, installed=False):
    """A module the run upgraded, or installed for the first time (an undo uninstalls that one)."""
    data = record(env, run)
    if name not in data['modules']:
        data['modules'].append(name)
    if installed and name not in data.setdefault('installed', []):
        data['installed'].append(name)
    save(env, data)


def rollback(env, run):
    """Puts everything the run changed back: files restored, created files removed, the branches left."""
    data = record(env, run)
    if not data['files'] and not data['created'] and not data['branches']:
        raise Refusal('there is nothing to undo for run %s' % run)
    done = {'restored': [], 'removed': [], 'branches': [], 'modules': [m for m in data.get('modules', []) if m not in data.get('installed', [])]}
    for top, b in data['branches'].items():
        # the run's changes are commits on its own branch: back on the branch it started from, they are gone
        git(['checkout', '--', '.'], top)
        ok, out = git(['checkout', b['from']], top)
        if not ok:
            raise Refusal('could not go back to %s in %s: %s' % (b['from'], top, clip(out, 400)))
        done['branches'].append('back on %s; branch %s is kept for reference (git branch -D %s removes it)' % (b['from'], b['branch'], b['branch']))
    in_branch = tuple(t + os.sep for t in data['branches'])
    for f in data['files']:
        if not f['path'].startswith(in_branch) and os.path.isfile(f['backup']):
            shutil.copy2(f['backup'], f['path'])
            done['restored'].append(f['path'])
    # created files: on a branch, checking out the starting branch took the committed ones away; what is left over
    # (never committed) goes too, as do the folders the run made and left empty
    for full in sorted(data['created'], key=len, reverse=True):
        if os.path.isfile(full) and not (full.startswith(in_branch) and _tracked(full)):
            os.remove(full)
            done['removed'].append(full)
    for full in sorted({os.path.dirname(c) for c in data['created']}, key=len, reverse=True):
        while os.path.isdir(full) and not os.listdir(full) and full not in writable_roots(env):
            os.rmdir(full)
            full = os.path.dirname(full)
    data['rolled_back'] = datetime.now(timezone.utc).isoformat()
    save(env, data)
    return done


def _tracked(full):
    top = toplevel(full)
    return bool(top and git(['ls-files', '--error-unmatch', os.path.relpath(full, top)], top)[0])
