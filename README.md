# tgbot_vpn

Telegram bot for a single 3x-ui VPN panel: registration, manual bank-transfer
payments with an admin decision step, bans, support relay and broadcasts. The
panel remains the enforcer of expiry and traffic; the bot owns identity, access
status, roles, payments, audit and settings in a local SQLite database
(`data/bot.db` by default).

---

## ⚠️ Migrating from the legacy bot? Read this first

The bot decides from its **own database**. `/broadcast`, `/ban` and `/pay` never
read the panel's client list, and the legacy JSON ban middleware
(`banned_users.json` + `middlewares.py`) no longer exists. The old bot kept the
customer list on the panel only, so on an existing deployment you **must** run the
import once, *before* the first start:

```bash
python -m app.cli import-legacy --dry-run   # preview only: changes nothing
python -m app.cli import-legacy             # apply (idempotent, safe to re-run)
```

What the import does:

* every panel client whose `email` is a Telegram id becomes a `users` row
  (`approved`, `panel_client_uuid` set, `@username` parsed from `comment`);
* ids listed in `BANNED_USERS_FILE` become `blocked` and their panel client is
  disabled;
* legacy clients created **disabled with `expiry_time = 0`** (never activated) get
  an explicit `now` expiry, so they can no longer be mistaken for lifetime
  ("unlimited") subscriptions;
* `settings.bank_details` is seeded from `BANK_ACCOUNT_DETAILS` when it is unset.

The service additionally **refuses to start** when the `users` table is empty
**while** the panel still carries clients — that combination silently breaks
broadcasts (nobody is reached), bans (they do not block anything) and `/pay`
(foreign-key violation). Set `AUTO_IMPORT_LEGACY=true` to let the startup do the
import automatically instead of failing with the instruction.

Skipping this step is the most common migration mistake: `/ban <id>` answers
"unknown user" and banned users keep their proxy access.

---

## Requirements

* Python 3.12+
* a reachable 3x-ui panel and its API token
* a Telegram bot token (BotFather) and at least one owner Telegram id

## Setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in the real values
```

### Environment variables (`.env`)

| Variable | Required | Meaning |
|---|---|---|
| `BOT_TOKEN` | yes | Telegram bot token. |
| `VPN_TOKEN` | yes | 3x-ui API token (token auth; `login()` is never called). |
| `DOMAIN` | yes | Panel base URL, e.g. `https://panel.example.com:2053/`. |
| `SUB_URL_BASE` | yes | Subscription link base, e.g. `https://panel.example.com:2053/sub/`. |
| `INBOUND_ID` | yes | Inbound that carries every client. |
| `OWNER_IDS` | yes | JSON list or comma list (`[123]` / `123,456`); at least one. |
| `ADMIN_IDS` | deprecated | Legacy alias for `OWNER_IDS`. |
| `DATABASE_URL` | no | Defaults to `sqlite+aiosqlite:///data/bot.db`. |
| `TIMEZONE` | no | Display/digest timezone, default `UTC`. |
| `DIGEST_HOUR` | no | Local hour of the daily digest, default `9`. |
| `LOG_LEVEL` | no | Default `INFO`; secrets are always masked. |
| `AUTO_IMPORT_LEGACY` | no | `true` = import the legacy deployment automatically when the DB is empty. |
| `BANK_ACCOUNT_DETAILS` | no | Seeds `settings.bank_details` when unset. |
| `BANNED_USERS_FILE` | no | Legacy JSON ban list, default `banned_users.json`. |

## Running

```bash
python main.py
```

Startup creates the data directory, applies Alembic migrations, seeds tariffs and
settings, checks the legacy import, then starts long polling.

## Backup and restore

```bash
sqlite3 data/bot.db ".backup 'backup.db'"   # consistent copy, safe while running
```

Copy `backup.db` next to `data/bot.db` (it is the whole bot state) and restore by
replacing `data/bot.db` while the bot is stopped.

## Upgrading

```bash
git pull
pip install -r requirements.txt
python main.py      # pending migrations are applied automatically on start
```

## Development / quality gate

```bash
ruff check . && ruff format --check . && mypy app && pytest -q
```
