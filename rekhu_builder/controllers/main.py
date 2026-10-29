"""The platform's door in direct mode:

    POST /rekhu/builder/run     one tool call: {"tool", "input", "run", "user", "nonce"}
    GET  /rekhu/builder/ping    whether the database is connected, signed the same way

Every call is signed with the database's own secret over the time, the method, the route and the exact body (the
scheme of the WordPress plugin and the Laravel package), is refused outside a five-minute window, and is refused when
the same signature comes twice. Never behind a session or CSRF: the signature is the authentication."""

import hashlib
import hmac
import json
import re
import time

from odoo import SUPERUSER_ID, api, http
from odoo.http import request

from ..lib import state
from ..tools import registry as tools

SIGNATURE = re.compile(r'^sha256=[a-f0-9]{64}$')


def _json(data, status=200):
    return request.make_response(json.dumps(data, default=str), headers=[('Content-Type', 'application/json'), ('Cache-Control', 'no-store')], status=status)


class RekhuBuilderController(http.Controller):

    def _env(self):
        """The superuser's environment on the request's own cursor, or None when no database is selected."""
        if not request.db:
            return None
        return api.Environment(request.env.cr, SUPERUSER_ID, {'lang': 'en_US', 'tz': 'UTC'})

    def _verify(self, env, route, body):
        """None when the call is the platform's, otherwise the refusal to send."""
        headers = request.httprequest.headers
        stamp, signature = headers.get('x-tb-time', ''), headers.get('x-tb-signature', '')
        secret = state.param(env, 'secret')
        if not secret or not state.key(env):
            return _json({'message': 'this database is not connected to Rekhu'}, 503)
        if not stamp.isdigit() or abs(time.time() - int(stamp)) > 300 or not SIGNATURE.match(signature):
            return _json({'message': 'signature missing or expired'}, 401)
        method = request.httprequest.method
        expected = 'sha256=' + hmac.new(secret.encode(), ('%s.%s.%s.' % (stamp, method, route)).encode() + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            return _json({'message': 'signature does not match'}, 401)
        if not env['rekhu.builder.seen']._first(signature[7:47]):
            return _json({'message': 'this call was already made once'}, 409)
        return None

    @http.route(state.ROUTE, type='http', auth='none', methods=['POST'], csrf=False, save_session=False, readonly=False)
    def run(self, **_kw):
        env = self._env()
        if env is None:
            return _json({'message': 'no database is selected for this address; set dbfilter so this host opens one database'}, 503)
        body = request.httprequest.get_data()
        refused = self._verify(env, state.ROUTE, body)
        if refused:
            return refused
        try:
            payload = json.loads(body or b'{}')
        except ValueError:
            return _json({'message': 'the body is not JSON'}, 422)
        started = time.time()
        # the tool works on the request's cursor, which Odoo commits when the answer goes out
        result = tools.run(env.registry, str(payload.get('tool') or ''), payload.get('input') or {}, {'run': payload.get('run'), 'user': payload.get('user')}, env=env)
        return _json({'result': result, 'ms': int((time.time() - started) * 1000)})

    @http.route('/rekhu/builder/ping', type='http', auth='none', methods=['GET'], csrf=False, save_session=False, readonly=False)
    def ping(self, **_kw):
        env = self._env()
        if env is None:
            return _json({'message': 'no database is selected for this address'}, 503)
        refused = self._verify(env, '/rekhu/builder/ping', b'')
        if refused:
            return refused
        from odoo import release  # noqa: PLC0415
        return _json({'ok': True, 'module': state.VERSION, 'odoo': release.version})
