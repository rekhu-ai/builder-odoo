"""The running Odoo server, as the builder reaches it. In direct mode the tools run inside the server itself; the
listener (relay mode) is a process of its own, often in another container, so it reaches the server over HTTP on
the module's signed door, with the same secret and scheme as the platform's calls.

Odoo only reads Python when it starts. A restart is Odoo's own (odoo.service.server.restart): the server process
starts afresh with the same arguments and the same database; nothing is lost, open pages reconnect."""

import hashlib
import hmac
import threading
import time

import requests
from odoo.tools import config

from . import state

PING = '/rekhu/builder/ping'
RESTART = '/rekhu/builder/restart'
# after the answer has gone out: a restart that starts at once cuts its own answer off
DELAY = 1.5


def inside():
    """True in the Odoo server process that serves the web (the signed door, its threads, workers and crons); False in
    the listener, a shell or a test run, which may hold a server object that never serves HTTP."""
    from odoo.service import server  # noqa: PLC0415
    running = getattr(server, 'server', None)
    # threaded and gevent servers keep their HTTP server in httpd, the prefork master and its workers the socket
    return running is not None and (getattr(running, 'httpd', None) is not None or getattr(running, 'socket', None) is not None)


def restart_soon():
    """Odoo's own restart, a moment from now, so the answer to this call still reaches the caller."""
    from odoo.service import server  # noqa: PLC0415
    threading.Timer(DELAY, server.restart).start()


def addresses(env):
    """Where the listener may find the server: the address set in the settings, the server's own port on this host
    (the same pod or machine), then the database's web address."""
    chosen = (state.param(env, 'server_url') or '').strip().rstrip('/')
    found = [chosen, 'http://127.0.0.1:%s' % (config.get('http_port') or 8069), state.base_url(env)]
    return [url for n, url in enumerate(found) if url and url not in found[:n]]


_last = {'stamp': 0}
_lock = threading.Lock()


def _stamp():
    """A second no earlier call from this process was signed in: the door refuses a signature it saw before, and two
    calls on the same route in one second would carry the same one."""
    with _lock:
        while int(time.time()) <= _last['stamp']:
            time.sleep(0.05)
        _last['stamp'] = int(time.time())
        return str(_last['stamp'])


def _headers(env, method, route, body=b''):
    stamp = _stamp()
    signature = hmac.new(state.secret(env).encode(), ('%s.%s.%s.' % (stamp, method, route)).encode() + body, hashlib.sha256).hexdigest()
    return {'x-tb-time': stamp, 'x-tb-signature': 'sha256=' + signature, 'accept': 'application/json'}


def call(env, method, route, timeout=8, only=None):
    """(address, answer) from the first address that answers; answer None when none did. A refusal by the server
    (wrong secret, another database) raises, since another address would not change it."""
    for url in ([only] if only else addresses(env)):
        try:
            response = requests.request(method, url + route, params={'db': env.cr.dbname}, headers=_headers(env, method, route), timeout=timeout)
        except requests.RequestException:
            continue
        if response.status_code in (502, 503, 504) and 'json' not in response.headers.get('content-type', ''):
            continue  # a proxy in front of a server that is not up
        try:
            body = response.json()
        except ValueError:
            continue  # not this module's door: another service on that address
        if response.status_code >= 400:
            raise RuntimeError('the Odoo server at %s refused the call: %s' % (url, body.get('message') or response.status_code))
        return url, body
    return None, None


def ping(env, only=None):
    """What the running server says about itself: its version and whether its Python is behind the files."""
    return call(env, 'GET', PING, only=only)
