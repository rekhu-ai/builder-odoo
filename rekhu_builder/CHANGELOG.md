# Changelog

## 1.0.1

- The listener restarts itself only after an upgrade or an undo that loaded new Python, no longer after every look at the database.
- Whether the server must restart for new Python is judged from when this process loaded the module, so it clears once the new code runs.

## 1.0.0

The first release of Rekhu Builder for Odoo 17, 18 and 19.

- Reads the database as it runs: modules and their origin, models with their fields and the module behind each, the relations between models, how a field is used in the data, views with their inheritance, menus, access rights and record rules, scheduled actions, the log, and the code.
- Builds custom modules: a new module from a description of its models (views, menu, access rights and a test included), and changes to existing ones. Every file is checked as it is written.
- Checks a module before it loads, installs or upgrades it (in a separate process when its Python changed), and runs its tests on a database of their own.
- Writes on a git branch of its own, or with a backup of every file; every run can be undone, a module it installed is uninstalled.
- Reaches Rekhu directly, or through the listener `odoo rekhu_listen` for a laptop, Docker or a server behind a firewall. The listener keeps a repair mode for when a change stops the database from loading.
- Local, staging and production: changes wait for the developer's yes outside local; production reads and proposes only, unless allowed.
- Sets up Rekhu Assistant: where it works, its help topics, and the actions of the custom modules.
