import hashlib
import hmac
import json
import time

from odoo.tests import HttpCase, tagged

from ..lib import params as settings
from ..lib import state


@tagged('post_install', '-at_install', 'rekhu_builder')
class TestDoor(HttpCase):
    """The platform's signed door: only a fresh, correctly signed call that was not made before runs a tool."""

    def setUp(self):
        super().setUp()
        settings.put(self.env, 'rekhu_builder.key', 'agk_test-customer_odoo-builder_abcdefghijklmnop')
        settings.put(self.env, 'rekhu_builder.environment', 'local')
        self.secret = state.secret(self.env)
        # a server with one database finds it by its dbfilter; the test server has several, so the session names it
        self.authenticate(None, None)

    def post(self, body, stamp=None, secret=None):
        raw = json.dumps(body).encode()
        stamp = str(stamp or int(time.time()))
        signature = 'sha256=' + hmac.new((secret or self.secret).encode(), ('%s.POST.%s.' % (stamp, state.ROUTE)).encode() + raw, hashlib.sha256).hexdigest()
        return self.url_open(state.ROUTE, data=raw, headers={'Content-Type': 'application/json', 'x-tb-time': stamp, 'x-tb-signature': signature})

    def test_a_signed_call_runs(self):
        response = self.post({'tool': 'context', 'input': {}, 'nonce': 'one'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result']['database'], self.env.cr.dbname)

    def test_unsigned_and_wrong_calls_are_refused(self):
        self.assertEqual(self.url_open(state.ROUTE, data=b'{}', headers={'Content-Type': 'application/json'}).status_code, 401)
        self.assertEqual(self.post({'tool': 'context', 'nonce': 'two'}, secret='wrong' * 10).status_code, 401)
        self.assertEqual(self.post({'tool': 'context', 'nonce': 'three'}, stamp=int(time.time()) - 600).status_code, 401)

    def test_a_call_runs_once(self):
        body = {'tool': 'context', 'input': {}, 'nonce': 'four'}
        stamp = int(time.time())
        self.assertEqual(self.post(body, stamp=stamp).status_code, 200)
        self.assertEqual(self.post(body, stamp=stamp).status_code, 409)
