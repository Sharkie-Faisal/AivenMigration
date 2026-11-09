# aiven-migrate

A [Claude Code](https://claude.com/claude-code) skill (and standalone CLI) that copies **everything** from one
Aiven **PostgreSQL** and/or **MySQL** service to another: all databases, schemas, tables, data, views,
sequences, indexes, constraints, functions, procedures, triggers and events. After copying, it checks every table.

It's built for the monthly "my Aiven free trial is ending, move everything to a new account" chore. You save
your current service once; after that you only paste the new service's connection details.

Nothing is hardcoded. It finds every database and table on the source automatically, and connection
details are always supplied by you.

## Install

```bash
# Claude Code personal skill (macOS / Linux / Git Bash)
git clone https://github.com/<you>/aiven-migrate-skill ~/.claude/skills/aiven-migrate
python -m pip install -r ~/.claude/skills/aiven-migrate/requirements.txt
```

```powershell
# Windows PowerShell
git clone https://github.com/<you>/aiven-migrate-skill "$HOME\.claude\skills\aiven-migrate"
python -m pip install -r "$HOME\.claude\skills\aiven-migrate\requirements.txt"
```

Requirements:
- Python 3.10+
- For Postgres, `pg_dump`/`pg_restore` at the **same or a newer major version** than your server:
  - **Windows**: downloaded automatically (portable, one time, about 330 MB) into `~/.claude/tools/pgsql<major>/`.
  - **macOS**: `brew install postgresql@18`, then put it on your PATH.
  - **Linux**: `postgresql-client-18` from the PGDG repository.
- MySQL needs no extra tools; it's copied with pure Python.

## Use with Claude Code

Open Claude Code and say something like:

> migrate aiven: new postgres is `postgres://avnadmin:...@new-host:12345/defaultdb?sslmode=require`

The first time, Claude asks for your **current** (source) connection too. Claude then tests both connections, does a
read-only dry run, migrates, verifies, and shows a summary.

## Use as a plain CLI

```bash
M=~/.claude/skills/aiven-migrate/migrate.py

# 1. one time: save the current (source) services
python $M set --uri "postgres://avnadmin:PASS@old-pg-host:PORT/defaultdb?sslmode=require"
python $M set --uri "mysql://avnadmin:PASS@old-mysql-host:PORT/defaultdb"
python $M show                                   # passwords masked

# 2. each month: preview, then migrate to the new services
python $M run --dry-run --to-pg "postgres://avnadmin:PASS@new-pg:PORT/defaultdb?sslmode=require" \
                        --to-mysql "mysql://avnadmin:PASS@new-mysql:PORT/defaultdb"
python $M run           --to-pg "..." --to-mysql "..."
```

Copy the URI from the Aiven console (**Service URI**, after revealing the password). If your password contains
`@ : / ? #` or `%`, URL-encode it.

| Option | Meaning |
|---|---|
| `--to-pg URI` / `--to-mysql URI` | target service(s); give one or both |
| `--from-pg URI` / `--from-mysql URI` | use this source just this once instead of the saved one |
| `--only a,b` | only these MySQL databases / Postgres schemas |
| `--jobs N` | parallelism (default 4) |
| `--overwrite` | replace existing objects in the **target** (otherwise it refuses if the target isn't empty) |
| `--no-promote` | don't make the target the new saved source |
| `--dry-run` | list what would be migrated; writes nothing |

Other commands: `show`, `set --uri`, `test --uri`, `inventory --engine pg|mysql [--uri]`.

Exit codes: `0` everything verified, `2` some table or object didn't match (see `report.json`), `1` error.

## What gets migrated

| | PostgreSQL | MySQL |
|---|---|---|
| Databases | all non-template DBs (except Aiven's internal `_aiven`) | all DBs except `mysql`, `sys`, `performance_schema`, `information_schema` |
| Schemas | all, except Aiven's internal `_avnadmin_monitor` | n/a |
| Tables + data | yes (`pg_dump -Fc` → `pg_restore -j N`) | yes (`SHOW CREATE TABLE` + streamed multi-row INSERTs, values passed through untouched) |
| Views / materialized views | yes | views yes |
| Sequences, AUTO_INCREMENT values | yes | yes |
| Indexes, PK/FK/unique/check constraints | yes | yes |
| Functions / procedures / triggers / events | yes | yes (DEFINER removed; recreated under their original `sql_mode`) |
| Extensions, types, domains, large objects, comments | yes | n/a |
| Users / roles / grants | **no**: the target keeps its own `avnadmin` | **no** |
| Aiven service settings, connection pools, integrations | **no** | **no** |

MySQL data is copied without conversion losses, including floats, decimals, datetimes and timestamps (copied in UTC), BIT,
geometry with SRID, JSON, binary/blob, ENUM/SET, invisible columns and generated columns. This was tested
with `CHECKSUM TABLE` matching between source and target.

## Verification

- Exact `COUNT(*)` of every table, source vs target.
- Postgres: counts of tables, views, materialized views, indexes, constraints, sequences and functions per schema.
- MySQL: table and view counts, plus any errors creating views, routines, triggers or events.
- A report is written to `~/.claude/aiven-migrate/backups/<timestamp>/report.json`.

## Safety

- The **source is only read**, never written to.
- It refuses if source and target are the same server. That's detected by hostname, resolved IP, MySQL
  `server_uuid` or the Postgres system identifier.
- It refuses to write into a target that already has tables, unless you pass `--overwrite`.
  With `--overwrite`, MySQL target databases are dropped and recreated, and Postgres uses `pg_restore --clean`.
- The Postgres dump is kept as a backup of the old service (`~/.claude/aiven-migrate/backups/`, last 2 runs).

## Where your data lives

| Path | Contents |
|---|---|
| `~/.claude/aiven-migrate/connections.json` | saved connections, **including passwords in plain text**. Local only, never commit it |
| `~/.claude/aiven-migrate/backups/` | Postgres dumps + reports |
| `~/.claude/tools/pgsql<N>/` | portable pg_dump/pg_restore (Windows) |

None of these are inside the repo.

## Limitations

- Downtime-style copy: writes made to the source *during* the migration may not be copied. Stop your app
  while it runs.
- The target Postgres major version should be the same or newer than the source.
- If a run is interrupted, re-run with `--overwrite`; the target is rebuilt from scratch.
- It's built and tested against Aiven, but it works with any reachable Postgres/MySQL over TLS.

## License

MIT
