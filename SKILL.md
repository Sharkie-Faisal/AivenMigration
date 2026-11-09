---
name: aiven-migrate
description: Migrate all schemas, tables, data, views, sequences, indexes, routines and triggers from one Aiven PostgreSQL and/or MySQL service to another (e.g. the monthly move when an Aiven free trial expires). Use when the user says "migrate aiven", "move my databases to the new aiven account", "trial expired", "transfer tables/schemas to new connection", or gives new Aiven connection details. The current source connections are saved; the user only supplies the new target(s).
---

# Aiven migrate

The migrator script is `~/.claude/skills/aiven-migrate/migrate.py` (Python 3.10+). Saved connections live in
`~/.claude/aiven-migrate/connections.json`. That file is local only and holds passwords, so never print it or copy it into a repo or into memory.

- **Postgres**: uses `pg_dump -Fc` and `pg_restore -j N` (`--no-owner --no-acl`, and the Aiven-internal schemas `_avnadmin_monitor` and `_aiven` are skipped). If the right pg_dump version isn't installed, a portable one is downloaded once into `~/.claude/tools/pgsql<major>/`. The dump is also kept as a backup in `~/.claude/aiven-migrate/backups/<timestamp>/` (last 2 runs kept).
- **MySQL**: copies with pure Python. Table DDL comes from `SHOW CREATE` and data is streamed with multi-row INSERTs, using N tables in parallel. Then views, procedures, functions, triggers and events are recreated, with DEFINER stripped. Values are passed through as raw text/bytes, so floats, decimals, datetimes, BIT, geometry, JSON and blobs copy losslessly (verified with `CHECKSUM TABLE`). System DBs (`mysql`, `sys`, `performance_schema`, `information_schema`) are skipped.
- **Verification**: exact `COUNT(*)` for every table, source vs target. For Postgres, the counts of tables, views, indexes, constraints, sequences and functions are also compared per schema.
- **Promote**: when verification passes, the target becomes the saved "current" source and the old one moves to history. Next month, only the new target is needed.
- **Safety**: it refuses if source and target are the same server (hostname, resolved IP, server_uuid or system id). It also refuses to write into a target that already has tables, unless `--overwrite` is given (Postgres: `pg_restore --clean`; MySQL: `DROP DATABASE` and recreate on the **target** only). The source is never written to.

## Workflow

1. **Prereqs** (once per machine): `python -m pip install "psycopg[binary]" pymysql`
2. **Show the saved source**: `python ~/.claude/skills/aiven-migrate/migrate.py show`
   If nothing is saved or it's wrong, run `migrate.py set --uri "<uri>"`.
3. **Get the new target(s) from the user.** Ask for whichever of Postgres and MySQL they're moving. Accept either:
   - the full Aiven "Service URI" with the password revealed, or
   - the separate fields (host, port, user, password, database). Build the URI yourself and **URL-encode the password** (`urllib.parse.quote(pw, safe="")`):
     `postgres://USER:PASS@HOST:PORT/DB?sslmode=require` and `mysql://USER:PASS@HOST:PORT/DB`
   Check each one first: `migrate.py test --uri "<uri>"`.
4. **Dry run** (read-only): it lists what will be moved and fails early if the target isn't empty.
   `migrate.py run --dry-run --to-pg "<pg uri>" --to-mysql "<mysql uri>"`
   Give only the engines being migrated. Show the user the list.
5. **Migrate**: run the same command without `--dry-run`. Run it in the background (it takes minutes for about 1 GB) with a long timeout, and relay the progress lines.
   Options: `--only a,b` (MySQL DBs / Postgres schemas), `--jobs 4` (parallelism), `--overwrite` (only after the user confirms it's OK to replace what's in the target), `--no-promote`, and `--from-pg/--from-mysql URI` (one-off source override).
6. **Report** the SUMMARY block. Exit code 0 means everything was verified. Exit code 2 means some table or object didn't match; show the mismatch lines from the output or from `report.json` and investigate before telling the user it's done.
   On success, remind the user to:
   - update app configs and env vars to the new host/port/password (search the project for the old hostname)
   - delete the old trial service only after checking the app works on the new one

## Notes / limits
- Database users/roles, grants and Aiven service settings aren't copied. The target uses its own `avnadmin`. Mention this if the source had extra users.
- Postgres extensions are recreated by the dump. If the target can't create one, the restore error is shown.
- If the target's Postgres major version is older than the source's, the restore can fail. Create the new service with the same or a newer version.
- If the connection drops halfway, re-run with `--overwrite`. It's idempotent because the target is rebuilt from scratch.
- Aiven MCP (`Aiven-Open/mcp-aiven`, or hosted at mcp.aiven.live) can list, create and manage services, but it has no tool that copies data between services. Only use it if the user wants Claude to also create the new service. That needs an Aiven API token from the new account.
