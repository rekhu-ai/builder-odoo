<p align="center"><img src="rekhu_builder/static/description/icon.png" width="72" alt="Rekhu"></p>

# Rekhu Builder for Odoo

The AI agent for Odoo developers. Install this module on your development or staging database, add the key from your
[Rekhu](https://rekhu.com) account, and the agent in your project chat reads the database as it runs. It then designs,
builds, tests and upgrades your custom modules, on a git branch of its own.

> **Start free.** [Create a Rekhu account](https://rekhu.com/register?ref=builder-odoo) and get **$10 credit** to try it, no card needed. After that you top up from $10 under **Billing**: no subscription, and unused credit never expires. [rekhu.com](https://rekhu.com) · [Docs](https://rekhu.com/docs) · [Pricing](https://rekhu.com/pricing)

- **Reads** the installed modules and where they come from, and every model with its fields and the module that added each. It also reads views and their inheritance, menus, access rights and record rules, scheduled actions and their failures, the server log, and the code.
- **Designs models with you:** relations as a diagram, and how a field is used in the data before you change it (how many records have it set, its values, empty ones).
- **Builds:** new modules from a description of their models, plus changes to existing custom modules: Python, XML views, security, data and migration scripts. Every file is checked as it is written.
- **Loads and tests:** checks the module, upgrades it (in a separate process when its Python changed), and runs its tests on a database of their own, never on yours.
- **Undoes:** every run can be rolled back. Files are restored, a module the run installed is uninstalled, and the repository goes back to the branch it started from.
- **Sets up [Rekhu Assistant](https://apps.odoo.com/apps/modules/19.0/rekhu_assistant):** where it works, its help topics, and actions of your own.

Odoo 17, 18, 19 and 20, Community and Enterprise. It doesn't work on Odoo Online, which does not take custom modules.

## Install

1. [Create a Rekhu account](https://rekhu.com/register?ref=builder-odoo) (it starts with $10 credit, no card needed), then **Projects → New project → Odoo**, then **Connection → Make a key** under Rekhu Builder.
2. Put `rekhu_builder` in your addons path (or install it from the Odoo Apps store) and install **Rekhu Builder** from Apps.
3. In Odoo open **Settings → Rekhu Builder**, paste the key, say what this database is (local, staging or production), and press **Connect**.

On a laptop, in Docker or behind a firewall, nothing has to be opened inbound. Keep the listener running instead:

```bash
odoo rekhu_listen -c /etc/odoo/odoo.conf -d mydb
```

Run it as a process or container of its own, beside the server, so a restart of the server does not stop it. When a
change breaks the loading of the database, the listener keeps working in repair mode: files, git, checks, upgrade and
undo, enough for the builder to repair its change.

A server with a public https address can be called directly instead (Settings → Rekhu Builder → Connection: Direct).
It needs a `dbfilter` that opens one database for its address.

## Safe by default

| | Local | Staging | Production |
|---|---|---|---|
| Reads | yes | yes | yes |
| Changes the custom modules | yes, on a branch | after your yes in the chat | no, it proposes a diff; unless you allow changes |
| Upgrades, deletes, uninstalls | yes | after your yes | no |

- It only ever writes in the custom addons folders: never Odoo's own modules, Enterprise, or itself. Name the folders in the settings, or it takes every custom folder this server can write.
- With git, every change goes on a branch `rekhu/<run>`, one commit per change, and you merge. Without git, every file is backed up before it changes.
- It works as the superuser for reading the structure. Personal fields (e-mail, phone, address) are masked unless asked for by name, and secrets always are. SQL runs read only, at most 15 seconds.
- It never reads the Odoo config file, passwords or keys. Every call from Rekhu is signed with this database's own secret and can be used once.

## Settings

| Setting | What it does |
|---|---|
| Key | The key from your Rekhu project (agk_…) |
| How Rekhu reaches this server | Automatic, Direct or Relay (the listener) |
| This database is | Local, staging or production: decides what needs your yes |
| Allow changes on production | Off by default |
| Changes to the modules | On a git branch, in the files with a backup, or never |
| Folders it may write | Custom addons folders, separated by commas |

The warnings and errors of every process that loads the module also go to `<data_dir>/rekhu_builder/warnings.log`, so
the builder can read them when Odoo logs to the terminal only.

## Licence

Licensed, not sold: see LICENSE.
Questions: hello@rekhu.com
