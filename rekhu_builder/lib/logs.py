"""The warnings and errors of this server, kept where the builder can read them. Odoo in Docker or on a PaaS often
logs to the terminal only, so every process that loads the module (the server, its workers, the cron runner and the
listener) also writes its warnings and errors to <data_dir>/rekhu_builder/warnings.log, rotated at 5 MB."""

import logging
import logging.handlers
import os
import threading

from odoo.tools import config

_installed = []


def path():
    return os.path.join(config['data_dir'], 'rekhu_builder', 'warnings.log')


class _Format(logging.Formatter):
    """Odoo's own line format, with the database and the process."""

    def format(self, record):
        record.dbname = getattr(threading.current_thread(), 'dbname', '?')
        return super().format(record)


def install():
    if _installed:
        return
    try:
        os.makedirs(os.path.dirname(path()), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path(), maxBytes=5 * 1024 * 1024, backupCount=1, encoding='utf-8', delay=True)
    except OSError:
        return
    handler.setLevel(logging.WARNING)
    handler.setFormatter(_Format('%(asctime)s %(process)s %(levelname)s %(dbname)s %(name)s: %(message)s'))
    logging.getLogger().addHandler(handler)
    _installed.append(handler)
