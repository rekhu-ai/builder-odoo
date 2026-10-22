"""odoo rekhu_listen -d <database>: the relay. Keeps one outgoing connection to Rekhu, takes the builder's tool calls
for this database, runs them here and sends the results back. Nothing has to be opened inbound, so it works on a
laptop, in Docker or behind a firewall, and it reconnects by itself.

    odoo rekhu_listen -c /etc/odoo/odoo.conf -d mydb
    docker compose exec odoo odoo rekhu_listen -d mydb

Odoo 19 loads this file on its own, outside the module, so everything else is imported inside run()."""

import logging
import os
import sys
import threading
import time

from odoo.cli import Command

_logger = logging.getLogger('rekhu_builder.listen')


class RekhuListen(Command):
    """Connect this database to its Rekhu Builder (no inbound connection needed)"""

    name = 'rekhu_listen'

    def run(self, args):
        from odoo import SUPERUSER_ID, api  # noqa: PLC0415
        from odoo.service import server  # noqa: PLC0415
        from odoo.modules.registry import Registry  # noqa: PLC0415
        from odoo.tools import config  # noqa: PLC0415

        once = '--once' in args
        args = [a for a in args if a != '--once']
        try:
            config.parse_config(args, setup_logging=True)
        except TypeError:
            config.parse_config(args)
        names = config['db_name']
        dbname = (names[0] if isinstance(names, (list, tuple)) else str(names or '').split(',')[0]).strip()
        if not dbname:
            sys.exit('Name the database: odoo rekhu_listen -d <database>')
        server.load_server_wide_modules()
        threading.current_thread().dbname = dbname
        from odoo.addons.rekhu_builder.lib import platform, repair, state  # noqa: PLC0415
        from odoo.addons.rekhu_builder.tools import registry as tools  # noqa: PLC0415

        def load():
            """The database's registry, or None and why it does not load: then the listener works in repair mode."""
            try:
                return Registry(dbname), None
            except Exception as error:  # noqa: BLE001 a broken module must not keep the builder from repairing it
                return None, '%s: %s' % (type(error).__name__, str(error)[:1500])

        registry, broken = load()

        def env_do(fn):
            if registry is not None:
                with registry.cursor() as cr:
                    return fn(api.Environment(cr, SUPERUSER_ID, {}))
            with tools._raw_cursor(dbname) as cr:
                return fn(repair.RepairEnv(cr, broken))

        if not env_do(state.key):
            sys.exit('This database has no Rekhu key yet: Settings → Rekhu Builder, then run this again.')
        say = lambda text: print(text, flush=True)  # noqa: E731
        say('rekhu listening for the builder · %s · %s · changes %s' % (dbname, env_do(state.environment), env_do(state.writes)))
        if broken:
            say('  the database does not load (%s): repair mode, the file, git, check and upgrade tools work' % broken[:200])
        pause, reported = 1, False
        while True:
            try:
                if not reported:
                    # the listener takes the calls, so Rekhu queues them for it whatever the setting says
                    stored = env_do(lambda env: platform.report(env, tools.definitions(), mode='relay'))
                    reported = True
                    say('  connected · %s tools reported' % stored.get('stored', 0))
                call = env_do(lambda env: platform.next_call(env, 25))
                pause = 1
                if not call:
                    continue
                tool, started = str(call.get('tool') or ''), time.time()
                say('  → %s' % tool)
                result = tools.run(registry, tool, call.get('input') or {}, {'run': call.get('run'), 'user': call.get('user')}, dbname=dbname, broken=broken)
                env_do(lambda env: platform.answer(env, call['id'], result))
                # an upgrade made a new registry, or a repair made the database load again: the next calls use it
                registry, now_broken = load()
                if broken and not now_broken:
                    say('  the database loads again')
                broken = now_broken
                ok = 'error' not in result
                say('  ← %s %s · %d ms' % (tool, 'ok' if ok else str(result['error'])[:120], (time.time() - started) * 1000))
                if once:
                    return
                if tool in ('module-upgrade', 'rollback') and (result.get('restart_needed') or result.get('apart')):
                    # the database now runs Python this process has not loaded: start afresh, as --dev=reload does
                    say('  restarting to load the new Python')
                    os.execv(sys.executable, [sys.executable, *sys.argv])
            except KeyboardInterrupt:
                return
            except Exception as error:  # noqa: BLE001 the network comes and goes; the listener stays
                say('  connection lost: %s · retrying in %d s' % (str(error)[:160], pause))
                time.sleep(pause)
                pause = min(30, pause * 2)
                reported = False

