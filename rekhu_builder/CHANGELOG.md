# Changelog

## 1.0.5

- **Start free.** The README and the store page say how to begin: [create a Rekhu account](https://rekhu.com/register?ref=builder-odoo) and it starts with $10 credit, no card needed.
- The key setting and the message for a missing key say where to get one, with a link to sign-up.

## 1.0.4

- module-check reads OWL templates too: a JavaScript global such as Number, String, JSON or parseInt in a template expression is reported with its line, since the screen would only fail in the browser ("ctx.Number is not a function").
- module-check reports an @import of a web address in a module's styles: Odoo cuts it at its first semicolon (a Google Fonts address has several), which leaves the whole backend without styles.
- tests-run lists the tests that were skipped and why (a browser tour skips quietly when the server has no Chrome), so a skipped tour is never reported as a passed one.
- After an install or upgrade the compiled screen assets are rebuilt and every Odoo process forgets the old ones, so the next page load shows the new JavaScript, templates and styles without a restart.

## 1.0.3

- module-check reads which models a module defines from the Python itself; a long class that extends a model without a _name of its own no longer makes it hang.
- The listener stops a tool that does not finish within 280 seconds, answers with the reason and starts afresh, so one stuck call never leaves the database unreachable.

## 1.0.2

- module-check reports what the running Odoo version refuses, with the line: a kanban card named kanban-box on 19, <tree> on 18 and 19, attrs and states since 17, _sql_constraints and a <group> in a search view on 19, <list> and <chatter/> on 17. Several of these break only in the browser, where no test sees them.

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
