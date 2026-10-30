"""The database's calls to the Rekhu platform: checking the key, reporting the tools, and the relay's long poll.
Every call carries the key and says which database it comes from."""

import requests

from . import state


class Refused(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def _headers(env):
    key = state.key(env)
    if not key:
        raise Refused('The Rekhu key is missing or not a Rekhu key (agk_…). Set it under Settings → Rekhu Builder. No Rekhu account yet? Create one at https://rekhu.com/register?ref=builder-odoo: it starts with $10 credit, no card needed.', 400)
    company = env.company
    return {
        'content-type': 'application/json',
        'accept': 'application/json',
        'x-api-key': key['key'],
        'x-agent': key['agent'],
        'x-tenant': env.cr.dbname,
        'x-tenant-name': str(company.name or env.cr.dbname),
        'x-session': 'odoo-builder-%s' % state.VERSION,
        'user-agent': 'rekhu-builder-odoo/%s' % state.VERSION,
    }


def _check(response):
    if response.status_code >= 400:
        try:
            error = response.json().get('error') or {}
            message = error.get('message') if isinstance(error, dict) else str(error)
        except ValueError:
            message = None
        raise Refused('Rekhu answered %s: %s' % (response.status_code, message or 'no reason given'), response.status_code)
    return response


def balance(env, timeout=15):
    """GET /v1/balance: the cheapest call that proves the key works."""
    return _check(requests.get(state.platform(env) + '/v1/balance', headers=_headers(env), timeout=timeout)).json()


def report(env, tools, mode=None):
    """POST /v1/abilities: the tools of this database, how to reach it and the secret its calls are signed with."""
    base = state.base_url(env)
    mode = mode or state.mode(env)
    from odoo import release  # noqa: PLC0415
    body = {
        'abilities': tools,
        'secret': state.secret(env),
        'site_url': base or None,
        'rest_url': base + '/rekhu/builder' if mode == 'direct' and base else None,
        'route': state.ROUTE,
        'mode': mode,
        'kind': 'odoo',
        'environment': state.environment(env),
        'writes': state.writes(env),
        'framework': 'Odoo %s' % release.version,
        'version': state.VERSION,
        'name': str(env.company.name or env.cr.dbname),
    }
    return _check(requests.post(state.platform(env) + '/v1/abilities', headers=_headers(env), json=body, timeout=20)).json()


def next_call(env, wait=25):
    """GET /v1/relay/next: the next call for this database, or None when none came within the wait."""
    response = requests.get(state.platform(env) + '/v1/relay/next', headers=_headers(env), params={'wait': wait}, timeout=wait + 15)
    if response.status_code == 204:
        return None
    return _check(response).json()


def answer(env, call_id, result):
    """POST /v1/relay/<id>/result: the answer to a call this database took."""
    _check(requests.post(state.platform(env) + '/v1/relay/%d/result' % int(call_id), headers=_headers(env), json={'result': result}, timeout=30))


def download(env, file_id, timeout=120):
    """GET /v1/templates/<id>/download: a file from the project's Files panel in the Rekhu account, as bytes, with
    the name it has there."""
    response = _check(requests.get(state.platform(env) + '/v1/templates/%s/download' % file_id, headers=_headers(env), timeout=timeout))
    disposition = response.headers.get('content-disposition', '')
    name = disposition.split('filename="', 1)[1].rstrip('"') if 'filename="' in disposition else None
    return response.content, name
