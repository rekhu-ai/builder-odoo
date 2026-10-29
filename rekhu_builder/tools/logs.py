"""The server's log: Odoo's own log file when it writes one, otherwise the builder's file of warnings and errors
(lib/logs.py). Entries are Odoo's lines with the traceback that follows them."""

import os
import re
from collections import deque

from odoo.tools import config

from ..lib import guard, logs
from ..lib.guard import Refusal

LINE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+) (\d+) (DEBUG|INFO|WARNING|ERROR|CRITICAL) (\S+) (\S+): ?(.*)$')


def _source(i):
    if i.get('source') == 'odoo' or (config['logfile'] and i.get('source') != 'builder'):
        if not config['logfile']:
            raise Refusal('Odoo writes its log to the terminal here (no logfile in its config); the builder\'s own file has the warnings and errors')
        return config['logfile'], 'odoo'
    return logs.path(), 'builder'


def _entries(path, keep=4000):
    """The last entries of a log file, each with its following lines (a traceback)."""
    if not os.path.isfile(path):
        return []
    entries = deque(maxlen=keep)
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            m = LINE.match(line)
            if m:
                entries.append({'at': m.group(1), 'pid': m.group(2), 'level': m.group(3), 'db': m.group(4), 'logger': m.group(5), 'message': m.group(6), 'trace': []})
            elif entries:
                if len(entries[-1]['trace']) < 60:
                    entries[-1]['trace'].append(line)
    return list(entries)


def _shape(e):
    out = {k: v for k, v in e.items() if k != 'trace'}
    if e['trace']:
        out['trace'] = guard.clip('\n'.join(e['trace']), 6000)
    return out


def tail(env, i, ctx):
    path, source = _source(i)
    count = max(1, min(int(i.get('lines') or 60), 400))
    entries = [e for e in _entries(path) if e['db'] in (env.cr.dbname, '?', '-')]
    return {'source': source, 'file': path, 'entries': [_shape(e) for e in entries[-count:]], 'note': None if entries else 'no warnings or errors were logged for this database yet'}


def search(env, i, ctx):
    path, source = _source(i)
    try:
        pattern = re.compile(str(i.get('pattern') or '.'), re.I)
    except re.error as error:
        raise Refusal('not a valid regular expression: %s' % error) from error
    level = str(i.get('level') or '').upper()
    limit = max(1, min(int(i.get('limit') or 30), 200))
    found = []
    for e in reversed(_entries(path, keep=20000)):
        if e['db'] not in (env.cr.dbname, '?', '-'):
            continue
        if level and e['level'] != level:
            continue
        if pattern.search(e['message']) or pattern.search(e['logger']) or any(pattern.search(t) for t in e['trace']):
            found.append(_shape(e))
            if len(found) >= limit:
                break
    # the same error many times: grouped, so the builder sees what is common
    groups = {}
    for e in found:
        key = (e['level'], e['logger'], re.sub(r'\d+', '#', e['message'])[:160])
        groups[key] = groups.get(key, 0) + 1
    return {'source': source, 'entries': found, 'grouped': [{'level': k[0], 'logger': k[1], 'message': k[2], 'count': c} for k, c in sorted(groups.items(), key=lambda kv: -kv[1])][:20]}
