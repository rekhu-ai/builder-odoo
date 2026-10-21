"""Rekhu Assistant in this database, as the builder sets it up: where it works (the scope rules), the actions the
developer's modules give it (methods marked with @tool) and the company's help topics it answers from."""

import inspect

from ..lib.guard import Refusal

MODES = ('off', 'read', 'change')


def _installed(env):
    return bool(env['ir.module.module'].sudo().search_count([('name', '=', 'rekhu_assistant'), ('state', '=', 'installed')])) and 'rekhu.assistant.scope' in env


def _actions(env):
    out = []
    for name in list(env.registry):
        cls = env.registry[name]
        for attr, method in inspect.getmembers(cls, predicate=callable):
            spec = getattr(method, '_rekhu_tool', None)
            if spec:
                try:
                    source = inspect.getsourcefile(method)
                except TypeError:
                    source = None
                out.append({'model': name, 'method': attr, 'description': spec['description'], 'readonly': spec['readonly'], 'confirm': spec['confirm'],
                            'groups': spec['groups'], 'file': source})
    return out


def assistant(env, i, ctx):
    action = i.get('action') or 'status'
    if not _installed(env):
        return {'installed': False, 'next': 'Rekhu Assistant is not installed here. It is on the Odoo Apps store as rekhu_assistant; once installed, the builder can set it up.'}
    Scope = env['rekhu.assistant.scope'].sudo()
    Help = env['rekhu.assistant.help'].sudo()
    params = env['ir.config_parameter'].sudo()
    if action == 'rules':
        rules = i.get('rules') or {}
        if not isinstance(rules, dict) or not rules:
            raise Refusal('rules is {model: "off" | "read" | "change"}')
        done = []
        for model, mode in rules.items():
            if mode not in MODES:
                raise Refusal('%s: the mode is off, read or change' % model)
            if model not in env or env[model]._abstract or env[model]._transient:
                raise Refusal('%s is not a model that keeps records' % model)
            existing = Scope.search([('model_name', '=', model)], limit=1)
            if mode == 'off':
                existing.unlink()
            elif existing:
                existing.mode = mode
            else:
                Scope.create({'model_id': env['ir.model']._get_id(model), 'mode': mode, 'app': i.get('app') or ''})
            done.append('%s: %s' % (model, mode))
        return {'changed': done, 'rules': Scope._rekhu_rules()}
    if action == 'help':
        title, body = str(i.get('title') or '').strip(), str(i.get('body') or '').strip()
        if not title or not body:
            raise Refusal('a help topic needs a title and a body')
        topic = Help.with_context(active_test=False).search([('name', '=', title)], limit=1)
        existed = bool(topic)
        if existed:
            topic.write({'body': body, 'active': True})
        else:
            topic = Help.create({'name': title, 'body': body})
        return {'topic': topic.id, 'title': topic.name, 'action': 'updated' if existed else 'created'}
    return {
        'installed': True,
        'enabled': params.get_param('rekhu_assistant.enabled') in ('True', '1', 'true'),
        'connected': bool(params.get_param('rekhu_assistant.secret_key')),
        'rules': [{'model': r.model_name, 'mode': r.mode, 'app': r.app} for r in Scope.search([])],
        'actions': _actions(env),
        'help_topics': Help.search([]).mapped('name'),
        'how_to_add_an_action': 'a method on a model in a custom module, marked with @tool from odoo.addons.rekhu_assistant.tools (the module depends on rekhu_assistant); see the decorator\'s docstring',
    }
