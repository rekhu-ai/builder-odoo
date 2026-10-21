"""The limits every tool works within: which files may be read or written, which values are secrets and how large
an answer may be. A refusal raises Refusal with one sentence the builder can pass on to the developer.

Files are named the Odoo way, by module: "sale_extra/models/sale_order.py". Odoo's own addons and Enterprise can be
read, never written; the custom addons folders are written only when they are writable and allowed in the settings."""

import fnmatch
import os
import re

import odoo
from odoo import release

from . import state

HIDDEN = ('.git', '.env', '.env.*', '*.pem', '*.key', 'id_rsa*', '*.conf', '*.cfg', '.netrc', '__pycache__', '*.pyc', 'node_modules')
# the builder never changes itself or the assistant it builds into the database
OWN = ('rekhu_builder', 'rekhu_assistant')
MODULE = re.compile(r'^[a-z][a-z0-9_]{1,63}$')
# Odoo's own package folder (on 19 odoo is a namespace package without __file__)
ODOO_DIR = os.path.realpath(os.path.dirname(release.__file__))
SECRET = re.compile(r'pass|secret|key|token|salt|private|credential|signature|auth', re.I)


class Refusal(Exception):
    """What the builder is told when something is not possible, in one sentence."""


def clip(text, size=200000):
    text = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', str(text))
    return text if len(text) <= size else text[:size] + '\n… (cut at %d characters)' % size


def kind_of(path):
    """odoo, enterprise or custom, for one addons folder."""
    try:
        names = set(os.listdir(path))
    except OSError:
        return 'missing'
    if 'web_enterprise' in names:
        return 'enterprise'
    if 'web' in names or 'base' in names or os.path.realpath(path).startswith(ODOO_DIR + os.sep):
        return 'odoo'
    return 'custom'


def addons_paths():
    """Every addons folder of this server, once, in Odoo's order."""
    seen, out = set(), []
    for path in list(getattr(odoo.addons, '__path__', [])):
        real = os.path.realpath(path)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            out.append(real)
    return out


def writable_roots(env):
    """The custom addons folders the builder may write in: the ones named in the settings, else every custom folder
    this server's user can write."""
    chosen = [os.path.realpath(p.strip()) for p in (state.param(env, 'addons') or '').split(',') if p.strip()]
    paths = addons_paths()
    if chosen:
        return [p for p in chosen if p in paths and kind_of(p) == 'custom' and os.access(p, os.W_OK)]
    return [p for p in paths if kind_of(p) == 'custom' and os.access(p, os.W_OK)]


def hidden(relative):
    parts = relative.split('/')
    return any(fnmatch.fnmatch(part, pattern) for part in parts for pattern in HIDDEN)


def split(path):
    """"module/rest" → (module, rest), refusing what is not a path inside a module."""
    path = str(path or '').replace('\\', '/').strip().strip('/')
    if not path or '\0' in path:
        raise Refusal('a path inside a module is needed, as module/folder/file')
    parts = path.split('/')
    if any(p in ('..', '.') or p == '' for p in parts):
        raise Refusal('paths outside the modules are not available')
    if not MODULE.match(parts[0]):
        raise Refusal('"%s" is not a module name; paths start with the module, as sale_extra/models/sale_order.py' % parts[0])
    if hidden(path):
        raise Refusal('%s is private (secrets, caches or git); the builder never reads or writes it' % path)
    return parts[0], '/'.join(parts[1:])


def module_root(name):
    """The folder of a module, or None: the first addons folder that has it, as Odoo looks. Read from the disk each
    time, so a module the builder just made is found (Odoo's own lookup is cached)."""
    if not MODULE.match(str(name or '')):
        return None
    for base in addons_paths():
        folder = os.path.join(base, name)
        if os.path.isfile(os.path.join(folder, '__manifest__.py')):
            return os.path.realpath(folder)
    return None


def readable(path):
    """The absolute path of an existing file or folder inside any module."""
    name, rest = split(path)
    root = module_root(name)
    if not root:
        raise Refusal('there is no module %s on this server' % name)
    full = os.path.realpath(os.path.join(root, rest))
    if full != root and not full.startswith(root + os.sep):
        raise Refusal('paths outside the module are not available')
    if not os.path.exists(full):
        raise Refusal('%s does not exist' % path)
    return full


def writable(env, path, root=None):
    """The absolute path to write, refusing Odoo's and Enterprise's addons, the builder itself and folders not allowed.
    A module that does not exist yet goes into `root`, or the first writable folder."""
    name, rest = split(path)
    if name in OWN:
        raise Refusal('%s is Rekhu\'s own module; the builder never changes it' % name)
    roots = writable_roots(env)
    if not roots:
        raise Refusal('there is no custom addons folder the builder may write in: add one to the addons path that this server can write, or name it in Settings → Rekhu Builder')
    existing = module_root(name)
    if existing:
        base = os.path.dirname(existing)
        if base not in roots:
            raise Refusal('%s lives in %s (%s), which the builder does not write; make the change in a custom module of its own that inherits it' % (name, base, kind_of(base)))
        module = existing
    else:
        target = os.path.realpath(root) if root else roots[0]
        if target not in roots:
            raise Refusal('%s is not a folder the builder may write in; one of: %s' % (root, ', '.join(roots)))
        module = os.path.join(target, name)
    full = os.path.realpath(os.path.join(module, rest)) if rest else module
    if full != module and not full.startswith(module + os.sep):
        raise Refusal('paths outside the module are not available')
    return full


def relative(full):
    """An absolute path inside a module back to module/rest."""
    full = os.path.realpath(full)
    for base in addons_paths():
        if full.startswith(base + os.sep):
            return full[len(base) + 1:]
    return full


def redact(value, name=''):
    """Masks secret-looking values, recursively."""
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, name) for v in value]
    if isinstance(value, str) and value and SECRET.search(name):
        return '•••• (set)'
    if isinstance(value, str):
        return re.sub(r'^([a-z][a-z0-9+.-]*://)[^/@\s]+:[^/@\s]+@', r'\1••••@', value, flags=re.I)
    return value
