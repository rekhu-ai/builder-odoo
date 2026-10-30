"""The tools this database gives its builder, in the shape the platform hands to the model: a slug, what it does, its
input as JSON Schema, and whether it only reads. A destructive tool on anything but a local database answers
"confirmation_needed" until it is called again with confirmed true, which the builder sends only after the developer
said yes. Every call runs as the superuser on a cursor of its own; a reading tool's cursor is rolled back."""

import json
import logging
import threading

from odoo import SUPERUSER_ID, api

from ..lib import guard, repair, state, workspace
from ..lib.guard import Refusal
from . import actions, assistant, code, data, logs, modules, structure

_logger = logging.getLogger(__name__)

S = {'type': 'string'}
I = {'type': 'integer'}
B = {'type': 'boolean'}
CONFIRMED = {'type': 'boolean', 'description': 'true only after the developer agreed to this exact step'}
DOMAIN = {'type': 'array', 'description': 'an Odoo domain: [["state", "=", "sale"], "|", ["amount_total", ">", 100], ["partner_id.country_id.code", "=", "NL"]]', 'items': {}}
FIELD = {'type': 'object', 'properties': {
    'name': S, 'type': {'type': 'string', 'enum': sorted(modules.FIELD_TYPES)}, 'string': S, 'relation': S, 'inverse': S,
    'choices': {'type': 'array', 'description': 'for a selection: the values, as "value" or "value:Label"', 'items': S}, 'required': B, 'readonly': B, 'index': B, 'translate': B, 'tracking': B, 'help': S, 'ondelete': S, 'default': {}},
    'required': ['name', 'type']}


def obj(properties=None, required=None):
    out = {'type': 'object', 'properties': properties or {}}
    if required:
        out['required'] = required
    return out


def tools():
    """slug → (label, description, input, readonly, destructive, handler)"""
    return {
        'context': ('The database', 'The database as it is now: Odoo version and edition, environment and write mode, the installed apps, the custom modules and their folders, git, Python and PostgreSQL, the log, whether the server must restart, Rekhu Assistant, and the conventions of the custom modules. Read it first.', obj(), True, False, structure.context),
        'modules': ('Modules', 'The installed modules with their state, version, author, dependencies and where they come from (odoo, enterprise or custom). "filter" narrows by name; "custom" true shows only custom modules; "available" true adds the ones not installed.', obj({'filter': S, 'custom': B, 'available': B, 'limit': I}), True, False, structure.module_list),
        'models': ('Models', 'Without "model": the models, narrowed by "filter" or "module". With "model": its whole structure: every field (type, relation, required, compute, related, stored, and the modules that added it), its inheritance and the file of each class in it, its SQL and Python constraints, the record count. "module" with a model shows only the fields that module added. Use it before changing or redesigning a model.', obj({'model': S, 'module': S, 'filter': S, 'limit': I}), True, False, structure.models),
        'model-graph': ('Model relations', 'The relations between models, around one "model" or between the models of a "module": each many2one, one2many and many2many as an edge, and an erDiagram in Mermaid. For explaining or redesigning a data model.', obj({'model': S, 'module': S}), True, False, structure.graph),
        'field-stats': ('Field use', 'How a field is used in the data: how many records have it set, how many distinct values, the commonest values, minimum and maximum. Read it before changing a field\'s type, making it required, or turning a selection into a model: it shows what a migration has to handle.', obj({'model': S, 'field': S}, ['model', 'field']), True, False, data.field_stats),
        'views': ('Views', 'With "view" (id or xml id): its arch, the views that inherit it (with their xml ids and arch), and the combined arch Odoo shows. With "model" and "type" (form, list, search, kanban…): the views of that model and the combined arch. With "model" only: the list of its views.', obj({'view': S, 'model': S, 'type': S, 'combined': B}), True, False, structure.views),
        'menus': ('Menus', 'Menus with their full path, xml id, action and groups; "filter" narrows by path, "model" by the model the action opens.', obj({'filter': S, 'model': S, 'limit': I}), True, False, structure.menus),
        'access': ('Access', 'Who may do what: for a "model" its access rights (per group: read, write, create, delete) and record rules (domain, groups); for a "user" (login) their groups.', obj({'model': S, 'user': S}), True, False, structure.access),
        'crons': ('Scheduled actions', 'The scheduled actions: what they run, how often, the next and last run, and failures. "all" true includes the inactive ones.', obj({'all': B}), True, False, structure.crons),
        'records': ('Records', 'Reads records of a model through the ORM: a "domain", the "fields" to show (default: the main stored fields), "order", "limit" (at most 200), "archived" true to include archived ones. "group_by" a field gives counts per value instead. Personal fields (e-mail, phone, address) are masked unless named in fields; secrets always.', obj({'model': S, 'domain': DOMAIN, 'fields': {'type': 'array', 'items': S}, 'order': S, 'limit': I, 'group_by': S, 'archived': B}, ['model']), True, False, data.records),
        'sql-read': ('SQL read', 'One read-only SQL statement (SELECT, WITH, EXPLAIN or SHOW) in a read-only transaction of at most 15 seconds; at most "limit" rows (default 50, max 200), "params" for %s placeholders. Never for changing data: data changes go into a module and reach the database with module-upgrade.', obj({'sql': S, 'params': {'type': 'array', 'items': {}}, 'limit': I}, ['sql']), True, False, data.sql_read),
        'logs-tail': ('Log', 'The latest warnings and errors of this database, each with its traceback. "source" odoo reads Odoo\'s log file when it has one; by default the builder\'s own file of warnings and errors.', obj({'lines': I, 'source': {'type': 'string', 'enum': ['builder', 'odoo']}}), True, False, logs.tail),
        'logs-search': ('Log search', 'Warnings and errors matching "pattern" (a regular expression, in the message, the logger or the traceback), newest first, and the same ones grouped with a count. "level" narrows to WARNING, ERROR or CRITICAL.', obj({'pattern': S, 'level': S, 'limit': I, 'source': {'type': 'string', 'enum': ['builder', 'odoo']}}), True, False, logs.search),
        'code-list': ('Files', 'Without "path": the custom addons folders and their modules ("all" true adds Odoo\'s and Enterprise\'s). With "path" (a module, or module/folder): its files, "depth" levels deep.', obj({'path': S, 'depth': I, 'all': B}), True, False, code.list_files),
        'code-read': ('Read file', 'A file of any module with line numbers, as module/path ("sale/models/sale_order.py"). "from" and "to" pick lines.', obj({'path': S, 'from': I, 'to': I}, ['path']), True, False, code.read),
        'code-search': ('Search code', 'Lines matching "pattern" (a regular expression) in the custom modules, or in "path" (a module or a file), with file and line. "all" true searches Odoo\'s and Enterprise\'s modules too; "static" true includes static/ and i18n/.', obj({'pattern': S, 'path': S, 'all': B, 'static': B, 'limit': I}, ['pattern']), True, False, code.search),
        'code-write': ('Write file', 'Creates or replaces a whole file in a custom module ("path" as module/path, "content" the whole text); a new module goes into "folder" or the first writable addons folder. Checked at once (Python, XML, manifest, access CSV) and committed on the run\'s branch with a backup. Prefer code-patch inside an existing file.', obj({'path': S, 'content': S, 'message': S, 'folder': S}, ['path', 'content']), False, False, code.write),
        'code-patch': ('Change file', 'Replaces the exact text "find" with "replace" in a file of a custom module (the text must occur once, or set "all"). Checked and committed like code-write.', obj({'path': S, 'find': S, 'replace': S, 'all': B, 'message': S}, ['path', 'find', 'replace']), False, False, code.patch),
        'code-delete': ('Delete file', 'Deletes a file in a custom module, with a backup.', obj({'path': S, 'message': S, 'confirmed': CONFIRMED}, ['path']), False, True, code.delete),
        'module-scaffold': ('New module', 'Makes a new custom module from a description: "models", each with "model" (library.book), "description", "tracking" (chatter) and "fields" (name, type, string, relation, inverse, choices, required…); with list, form and search views, an action and a menu per model, access rights for "group" (default base.group_user), and a test per model. "depends" adds dependencies. Read the result, adjust it, then module-upgrade with install true.', obj({
            'name': S, 'title': S, 'summary': S, 'depends': {'type': 'array', 'items': S}, 'group': S, 'folder': S, 'license': S, 'author': S, 'category': S, 'application': B,
            'models': {'type': 'array', 'items': obj({'model': S, 'description': S, 'tracking': B, 'order': S, 'rec_name': S, 'menu': S, 'fields': {'type': 'array', 'items': FIELD}}, ['model'])}},
            ['name']), False, False, modules.scaffold),
        'module-check': ('Check module', 'Finds what would stop a module from loading, without loading it: Python and XML errors, data files missing from the manifest, folders not imported, dependencies not on the server, models without access rights.', obj({'module': S}, ['module']), True, False, modules.check_module),
        'module-upgrade': ('Install or upgrade', 'Upgrades a custom module so the database matches its files (or installs it, with "install" true). Checked first; a module whose Python changed after the server started is upgraded in a separate Odoo process. On a failure nothing stays half done and the error says the file and line. On staging or production it needs confirmed.', obj({'module': S, 'install': B, 'force': B, 'separate': B, 'confirmed': CONFIRMED}, ['module']), False, True, modules.upgrade),
        'project-file': ('Bring in a file', 'Attaches a file from the project\'s Files panel in the Rekhu account (its "id" from project-uploads) in this database, so a wizard or a button can use it: an export to import, a price list, a document. Answers the attachment id to give to odoo-call. On staging or production it needs confirmed.', obj({'id': S, 'name': S, 'confirmed': CONFIRMED}, ['id']), False, True, actions.project_file),
        'odoo-call': ('Run in Odoo', 'Calls one public method of one model, the way a button in Odoo does, as the superuser: on records by "ids", or on a record made first from "values" (a wizard: its fields, a file as {"attachment": id} from project-file, base64 as most wizards take it, or {"attachment": id, "raw": true} for a field that reads the bytes as they are, such as base_import.import\'s file). "args", "kwargs" and "context" are passed on. Answers what the method returned and the record\'s fields afterwards, where a wizard writes its report. It changes data, which a run\'s undo does not bring back: run an import as a dry run first, show the report, and do the real run only after the developer\'s yes. Not for modules, settings, users or access rights. On staging or production it needs confirmed.', obj({'model': S, 'method': S, 'ids': {'type': 'array', 'items': I}, 'values': {'type': 'object'}, 'args': {'type': 'array', 'items': {}}, 'kwargs': {'type': 'object'}, 'context': {'type': 'object'}, 'confirmed': CONFIRMED}, ['model', 'method']), False, True, actions.call),
        'server-restart': ('Restart Odoo', 'Restarts the Odoo server so it runs the Python now in the files: needed after module-upgrade answers restart_needed, and when context says the server must restart. Odoo\'s own restart: the same database and settings, open pages reconnect, the server is away for a few seconds to half a minute. Waits until the server answers from a fresh process (at most "wait" seconds, default 90). On staging or production it needs confirmed: say the server is briefly unavailable to everyone using it.', obj({'wait': I, 'confirmed': CONFIRMED}), False, True, modules.restart),
        'tests-run': ('Run tests', 'Runs a module\'s tests in a separate Odoo process on a database of their own (<database>_rekhu_test), never on this one. "tags" overrides the test tags (default /module). It starts in the background and waits up to "wait" seconds (default 90); when it is still running, call again with its "job" to wait for the result.', obj({'module': S, 'tags': S, 'job': S, 'wait': I}), True, False, modules.tests),
        'git': ('Git', 'The custom addons repositories, or the one of "module": "status", "diff" (against HEAD), "log" (recent commits) or "branches".', obj({'action': {'type': 'string', 'enum': ['status', 'diff', 'log', 'branches']}, 'module': S}), True, False, modules.git),
        'rollback': ('Undo a run', 'Undoes everything a run changed: the modules it installed are uninstalled, the files are restored and the files it created removed, the repository is back on the branch it started from, and the modules it upgraded are upgraded again to match. "run" is the run id (default this run); "list" true lists the runs. On staging or production it needs confirmed.', obj({'run': S, 'list': B, 'confirmed': CONFIRMED}), False, True, modules.rollback),
        'assistant': ('Rekhu Assistant', 'Rekhu Assistant in this database: "status" shows whether it is installed and on, where it works (the rules), the actions the modules give it (methods marked @tool) and the help topics. "rules" sets where it works: {"sale.order": "change", "res.partner": "read", "crm.lead": "off"}. "help" writes a help topic ("title", "body") it answers from. Changes need confirmed on staging or production.', obj({'action': {'type': 'string', 'enum': ['status', 'rules', 'help']}, 'rules': {'type': 'object'}, 'app': S, 'title': S, 'body': S, 'confirmed': CONFIRMED}), False, False, assistant.assistant),
    }


# changes that need the developer's yes outside a local database, on top of the destructive tools
CONFIRM_ACTIONS = {'assistant': lambda i: (i.get('action') or 'status') != 'status'}
# changes that need writing to be allowed in this database
WRITES = {'code-write', 'code-patch', 'code-delete', 'module-scaffold', 'module-upgrade', 'rollback', 'project-file', 'odoo-call'}


def definitions():
    """What the platform stores and gives the model."""
    return [{'slug': slug, 'label': t[0], 'description': t[1], 'input': t[2], 'readonly': t[3], 'capability': 'read' if t[3] else 'write'} for slug, t in tools().items()]


def run(registry, tool, given, meta=None, dbname=None, broken=None, env=None):
    """Runs one tool for the platform. Never raises: a refusal or a failure comes back as {'error': …}. Without a
    registry (the database does not load: `broken` says why) only the repair tools run, without the ORM."""
    meta = meta or {}
    found = tools().get(tool)
    if not found:
        return {'error': 'this database has no tool "%s" (rekhu_builder %s)' % (tool, state.VERSION)}
    _label, _description, _schema, readonly, destructive, handler = found
    given = given if isinstance(given, dict) else {}
    if registry is None and tool not in repair.TOOLS:
        return {'error': 'the database does not load right now (%s). Until it does, only these tools work: %s. Repair the module, then module-upgrade.' % (guard.clip(broken or 'unknown error', 600), ', '.join(sorted(repair.TOOLS)))}
    threading.current_thread().dbname = registry.db_name if registry is not None else dbname
    try:
        # `env` is given by the signed door (the request's cursor) and by the module's tests (the test's transaction)
        with (_given(env) if env is not None else registry.cursor() if registry is not None else _raw_cursor(dbname)) as cr:
            env = env(su=True) if env is not None else api.Environment(cr, SUPERUSER_ID, {'lang': 'en_US', 'tz': 'UTC'}) if registry is not None else repair.RepairEnv(cr, broken)
            where = state.environment(env)
            if tool in WRITES and state.writes(env) == 'off' and not (tool == 'rollback' and given.get('list')):
                return {'error': 'writing is off for this database (%s): propose the change instead; the developer can allow writes in Settings → Rekhu Builder' % where}
            asks = destructive or (tool in CONFIRM_ACTIONS and CONFIRM_ACTIONS[tool](given))
            if asks and where != 'local' and given.get('confirmed') is not True and not (tool == 'rollback' and given.get('list')):
                return {'confirmation_needed': 'This step changes the %s database. Tell the developer exactly what will happen, wait for a yes, then call again with confirmed true.' % where}
            result = handler(env, given, {'run': workspace.run_id(meta.get('run')), 'user': meta.get('user')})
            if readonly and not isinstance(cr, _GivenCursor):
                cr.rollback()
    except Refusal as refusal:
        return {'error': str(refusal)}
    except Exception as error:  # noqa: BLE001 the builder gets the reason, the log gets the trace
        _logger.exception('rekhu builder: %s failed', tool)
        return {'error': guard.clip('%s: %s' % (type(error).__name__, error), 2000)}
    out = result if isinstance(result, dict) else {'result': result}
    if registry is None:
        out = dict(out, repair_mode='the database does not load: %s' % guard.clip(broken or '', 600))
    text = json.dumps(out, default=str)
    if len(text) > 900000:
        return {'error': 'the answer is too large to send; narrow it down (a filter, a path, fewer lines)'}
    return json.loads(text)


class _raw_cursor:
    """A cursor straight on the database, for repair mode; committed when the block ends without an error."""

    def __init__(self, dbname):
        from odoo.sql_db import db_connect  # noqa: PLC0415
        self.cr = db_connect(dbname).cursor()

    def __enter__(self):
        return self.cr

    def __exit__(self, kind, value, trace):
        if kind is None:
            self.cr.commit()
        else:
            self.cr.rollback()
        self.cr.close()


class _GivenCursor:
    pass


class _given:
    """The cursor of an environment the caller holds: neither committed nor rolled back here."""

    def __init__(self, env):
        self.cr = env.cr

    def __enter__(self):
        return _GivenCursor()

    def __exit__(self, kind, value, trace):
        return False
