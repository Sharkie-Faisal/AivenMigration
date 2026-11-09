#!/usr/bin/env python3
"""
Aiven database migrator (PostgreSQL + MySQL) - move everything from one Aiven
service to another (e.g. when a free trial expires).

Saved connections live in ~/.claude/aiven-migrate/connections.json (local only).

Commands
  show                                   list saved connections (passwords masked)
  set --engine pg|mysql --uri URI        save/replace the "current" (source) connection
  inventory --engine pg|mysql [--uri U]  list schemas/tables/sizes (defaults to current)
  test --uri URI                         check that a connection works
  run [--to-pg URI] [--to-mysql URI]     migrate current -> new, verify, then promote new to current
      [--from-pg URI] [--from-mysql URI] override the saved source
      [--only a,b]                       only these databases (mysql) / schemas (pg)
      [--overwrite]                      replace objects that already exist in the target
      [--jobs N] [--no-promote] [--dry-run]

URIs:  postgres://user:pass@host:port/db?sslmode=require
       mysql://user:pass@host:port/db?ssl-mode=REQUIRED
"""
import argparse, datetime as dt, json, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HOME = Path.home() / ".claude" / "aiven-migrate"
CONF = HOME / "connections.json"
BACKUPS = HOME / "backups"
TOOLS = Path.home() / ".claude" / "tools"
KEEP_BACKUP_RUNS = 2

MYSQL_SYSTEM_DBS = {"mysql", "sys", "performance_schema", "information_schema"}
PG_SKIP_DBS = {"_aiven"}
PG_SKIP_SCHEMAS = ("_avnadmin_monitor", "_aiven")


def log(msg=""):
    print(msg, flush=True)


def die(msg):
    log(f"ERROR: {msg}")
    sys.exit(1)


# --------------------------------------------------------------------------- config
def parse_uri(uri):
    u = urllib.parse.urlparse(uri.strip())
    scheme = u.scheme.lower()
    engine = {"postgres": "pg", "postgresql": "pg", "pg": "pg", "mysql": "mysql"}.get(scheme)
    if not engine:
        die(f"unsupported URI scheme '{u.scheme}' (use postgres:// or mysql://)")
    if not (u.hostname and u.port and u.username and u.password):
        die("URI must contain user, password, host and port: scheme://user:pass@host:port/db")
    q = urllib.parse.parse_qs(u.query)
    return {
        "engine": engine,
        "host": u.hostname,
        "port": u.port,
        "user": urllib.parse.unquote(u.username),
        "password": urllib.parse.unquote(u.password),
        "database": (u.path or "/").lstrip("/") or "defaultdb",
        "sslmode": (q.get("sslmode") or ["require"])[0] if engine == "pg" else "REQUIRED",
    }


def mask(c):
    return f"{c['engine']}://{c['user']}:****@{c['host']}:{c['port']}/{c['database']}"


def load_conf():
    if CONF.exists():
        return json.loads(CONF.read_text(encoding="utf-8"))
    return {"pg": {"current": None, "history": []}, "mysql": {"current": None, "history": []}}


def save_conf(conf):
    HOME.mkdir(parents=True, exist_ok=True)
    CONF.write_text(json.dumps(conf, indent=2), encoding="utf-8")


def promote(conf, engine, new):
    slot = conf.setdefault(engine, {"current": None, "history": []})
    if slot.get("current"):
        old = dict(slot["current"])
        old["retired_at"] = dt.datetime.now().isoformat(timespec="seconds")
        slot.setdefault("history", []).insert(0, old)
        slot["history"] = slot["history"][:6]
    new = dict(new)
    new["since"] = dt.datetime.now().isoformat(timespec="seconds")
    slot["current"] = new


# --------------------------------------------------------------------------- postgres
def pg_connect(c, dbname=None):
    import psycopg
    return psycopg.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"],
                           dbname=dbname or c["database"], sslmode=c.get("sslmode", "require"),
                           connect_timeout=20, autocommit=True)


def pg_databases(c):
    with pg_connect(c) as cn:
        rows = cn.execute("select datname from pg_database where not datistemplate order by 1").fetchall()
    return [r[0] for r in rows if r[0] not in PG_SKIP_DBS]


PG_USER_NS = ("n.nspname not in ('pg_catalog','information_schema','pg_toast') and n.nspname not like 'pg\\_temp%%' "
              "and n.nspname not like 'pg\\_toast\\_temp%%' and n.nspname not in ('_avnadmin_monitor','_aiven')")


def pg_tables(cn):
    return cn.execute(f"""select n.nspname, c.relname, pg_total_relation_size(c.oid)
        from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind in ('r','p') and not c.relispartition and {PG_USER_NS} order by 1,2""").fetchall()


def pg_object_counts(cn):
    rows = cn.execute(f"""select n.nspname, c.relkind, count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
        where c.relkind in ('r','p','v','m','S','i') and {PG_USER_NS} group by 1,2""").fetchall()
    out = {}
    names = {"r": "tables", "p": "tables", "v": "views", "m": "matviews", "S": "sequences", "i": "indexes"}
    for ns, k, n in rows:
        d = out.setdefault(ns, {})
        d[names[k]] = d.get(names[k], 0) + n
    for ns, n in cn.execute(f"""select n.nspname, count(*) from pg_constraint x join pg_namespace n on n.oid=x.connamespace
            where {PG_USER_NS} group by 1""").fetchall():
        out.setdefault(ns, {})["constraints"] = n
    for ns, n in cn.execute(f"""select n.nspname, count(*) from pg_proc p join pg_namespace n on n.oid=p.pronamespace
            where {PG_USER_NS} group by 1""").fetchall():
        out.setdefault(ns, {})["functions"] = n
    return out


def pg_server_major(c):
    with pg_connect(c) as cn:
        return int(cn.execute("show server_version_num").fetchone()[0]) // 10000


def ensure_pg_tools(major):
    bindir = TOOLS / f"pgsql{major}" / "bin"
    if (bindir / "pg_dump.exe").exists() or (bindir / "pg_dump").exists():
        return bindir
    sysdump = shutil.which("pg_dump")
    if sysdump:
        out = subprocess.run([sysdump, "--version"], capture_output=True, text=True).stdout
        m = re.search(r"(\d+)\.", out)
        if m and int(m.group(1)) >= major:
            return Path(sysdump).parent
    if os.name != "nt":
        die(f"pg_dump {major}+ not found on PATH. Install it and re-run:\n"
            f"  macOS:  brew install postgresql@{major}  (then add $(brew --prefix postgresql@{major})/bin to PATH)\n"
            f"  Debian/Ubuntu:  sudo apt install postgresql-client-{major}  (PGDG apt repo)\n"
            f"  RHEL/Fedora:    sudo dnf install postgresql{major}")
    url = None
    for minor in range(12, -1, -1):
        cand = f"https://get.enterprisedb.com/postgresql/postgresql-{major}.{minor}-1-windows-x64-binaries.zip"
        try:
            with urllib.request.urlopen(urllib.request.Request(cand, method="HEAD"), timeout=20) as r:
                if r.status == 200:
                    url = cand
                    break
        except Exception:
            continue
    if not url:
        die(f"could not find PostgreSQL {major} Windows binaries on get.enterprisedb.com")
    TOOLS.mkdir(parents=True, exist_ok=True)
    zpath = TOOLS / f"pgsql{major}.zip"
    log(f"Downloading portable PostgreSQL {major} client tools (one-time, ~330 MB): {url}")
    with urllib.request.urlopen(url, timeout=60) as r, open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done, last = 0, 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total and done - last > total / 10:
                last = done
                log(f"  {done * 100 // total}%")
    log("Extracting bin/ ...")
    with zipfile.ZipFile(zpath) as z:
        for m in z.namelist():
            if m.startswith("pgsql/bin/") and not m.endswith("/"):
                target = TOOLS / f"pgsql{major}" / m[len("pgsql/"):]
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(m) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
    zpath.unlink(missing_ok=True)
    return bindir


def pg_env(c):
    env = dict(os.environ)
    env["PGPASSWORD"] = c["password"]
    env["PGSSLMODE"] = c.get("sslmode", "require")
    env["PGCONNECT_TIMEOUT"] = "20"
    return env


def pg_conn_args(c, db):
    return ["--host", c["host"], "--port", str(c["port"]), "--username", c["user"], "--dbname", db]


def pg_inventory(c):
    report = {}
    for db in pg_databases(c):
        with pg_connect(c, db) as cn:
            tabs = pg_tables(cn)
            report[db] = {"tables": len(tabs), "bytes": sum(t[2] for t in tabs),
                          "schemas": sorted({t[0] for t in tabs}), "objects": pg_object_counts(cn)}
    return report


def pg_count_rows(c, db, only):
    out = {}
    with pg_connect(c, db) as cn:
        for ns, rel, _ in pg_tables(cn):
            if only and ns not in only:
                continue
            q = f'select count(*) from "{ns.replace(chr(34), chr(34) * 2)}"."{rel.replace(chr(34), chr(34) * 2)}"'
            out[f"{ns}.{rel}"] = cn.execute(q).fetchone()[0]
    return out


def migrate_pg(src, dst, args, run_dir):
    result = {"engine": "pg", "source": mask(src), "target": mask(dst), "databases": {}, "ok": True}
    only = set(args.only.split(",")) if args.only else None
    smaj, dmaj = pg_server_major(src), pg_server_major(dst)
    log(f"[pg] source v{smaj} {mask(src)}  ->  target v{dmaj} {mask(dst)}")
    if dmaj < smaj:
        log(f"[pg] WARNING: target major ({dmaj}) is older than source ({smaj}); restore may hit incompatibilities.")
    dbs = pg_databases(src)
    tgt_dbs = set(pg_databases(dst))
    # preflight: target must be empty unless --overwrite
    for db in dbs:
        if db in tgt_dbs:
            with pg_connect(dst, db) as cn:
                existing = [f"{t[0]}.{t[1]}" for t in pg_tables(cn) if not only or t[0] in only]
            if existing and not args.overwrite:
                die(f"[pg] target database '{db}' already has {len(existing)} tables (e.g. {existing[:3]}). "
                    f"Re-run with --overwrite to replace them.")
    for db in dbs:
        with pg_connect(src, db) as cn:
            n = len([t for t in pg_tables(cn) if not only or t[0] in only])
        log(f"[pg] database {db}: {n} tables")
    if args.dry_run:
        return result

    bindir = ensure_pg_tools(max(smaj, dmaj))
    exe = lambda name: str(bindir / (name + (".exe" if os.name == "nt" else "")))

    for db in dbs:
        t0 = time.time()
        if db not in tgt_dbs:
            with pg_connect(dst) as cn:
                cn.execute(f'create database "{db}"')
            log(f"[pg] created database {db} on target")
        dump = run_dir / f"pg_{db}.dump"
        cmd = [exe("pg_dump"), *pg_conn_args(src, db), "-Fc", "-Z", "6", "-b", "--no-owner", "--no-acl",
               "--no-publications", "--no-subscriptions", "--no-security-labels", "-f", str(dump)]
        for s in PG_SKIP_SCHEMAS:
            cmd += ["--exclude-schema", s]
        for s in sorted(only or []):
            cmd += ["--schema", s]
        log(f"[pg] dumping {db} -> {dump}")
        p = subprocess.run(cmd, env=pg_env(src), capture_output=True, text=True)
        if p.returncode != 0:
            log(p.stderr)
            die(f"[pg] pg_dump failed for {db}")
        log(f"[pg] dump size {dump.stat().st_size / 1048576:.1f} MB; restoring with {args.jobs} jobs ...")
        cmd = [exe("pg_restore"), *pg_conn_args(dst, db), "--no-owner", "--no-acl", "-j", str(args.jobs)]
        if args.overwrite:
            cmd += ["--clean", "--if-exists"]
        p = subprocess.run(cmd + [str(dump)], env=pg_env(dst), capture_output=True, text=True)
        errors = [l for l in p.stderr.splitlines() if "error" in l.lower()]
        # harmless: public schema already exists / comment on public schema owned by someone else
        real = [e for e in errors if not re.search(r'schema "public" already exists|must be owner of schema public', e)]
        if real:
            log(f"[pg] pg_restore reported {len(real)} error(s):")
            for e in real[:30]:
                log("   " + e)
        log(f"[pg] verifying row counts for {db} ...")
        s_rows, d_rows = pg_count_rows(src, db, only), pg_count_rows(dst, db, only)
        mism = {k: (v, d_rows.get(k)) for k, v in s_rows.items() if d_rows.get(k) != v}
        with pg_connect(src, db) as a, pg_connect(dst, db) as b:
            so, do = pg_object_counts(a), pg_object_counts(b)
        if only:
            so = {k: v for k, v in so.items() if k in only}
            do = {k: v for k, v in do.items() if k in only}
        obj_diff = {ns: {"source": so.get(ns), "target": do.get(ns)} for ns in so if so.get(ns) != do.get(ns)}
        ok = not mism and not obj_diff
        result["ok"] &= ok
        result["databases"][db] = {"tables": len(s_rows), "rows": sum(s_rows.values()), "row_mismatches": mism,
                                   "object_count_diffs": obj_diff, "restore_errors": real[:50],
                                   "seconds": round(time.time() - t0)}
        log(f"[pg] {db}: {len(s_rows)} tables, {sum(s_rows.values()):,} rows, "
            f"{'VERIFIED' if ok else 'MISMATCH'} ({round(time.time() - t0)}s)")
        for k, (a, b) in list(mism.items())[:20]:
            log(f"     rows differ {k}: source={a} target={b}")
        for ns, d in obj_diff.items():
            log(f"     objects differ in schema {ns}: source={d['source']} target={d['target']}")
    return result


# --------------------------------------------------------------------------- mysql
def my_connect(c, db=None, stream=False):
    import pymysql
    from pymysql.converters import conversions
    from pymysql.cursors import SSCursor, Cursor
    # keep encoders (python -> SQL literal) but drop decoders, so values arrive as the exact text/bytes
    # MySQL sent and are written back verbatim (no float/datetime/decimal round-trip loss)
    enc_only = {k: v for k, v in conversions.items() if not isinstance(k, int)}
    cn = pymysql.connect(host=c["host"], port=c["port"], user=c["user"], password=c["password"],
                         database=db, ssl={"ssl": {}}, charset="utf8mb4", autocommit=True,
                         connect_timeout=20, read_timeout=3600, write_timeout=3600,
                         conv=enc_only, cursorclass=SSCursor if stream else Cursor)
    # Aiven defaults to sql_mode=ANSI (double-quoted identifiers). Use one neutral mode on every
    # connection so SHOW CREATE emits backticks and the target accepts it; UTC so TIMESTAMPs copy exactly.
    with cn.cursor() as cur:
        cur.execute(f"SET SESSION sql_mode='{NEUTRAL_SQL_MODE}', time_zone='+00:00'")
    return cn


NEUTRAL_SQL_MODE = "NO_AUTO_VALUE_ON_ZERO,NO_ENGINE_SUBSTITUTION"


def qi(name):
    return "`" + name.replace("`", "``") + "`"


DEFINER_RE = re.compile(r"\s*DEFINER\s*=\s*(`[^`]*`|'[^']*'|\S+)@(`[^`]*`|'[^']*'|\S+)", re.I)


def strip_definer(sql):
    return DEFINER_RE.sub("", sql, count=1)


def my_databases(cn):
    with cn.cursor() as cur:
        cur.execute("show databases")
        return [r[0] for r in cur.fetchall() if r[0] not in MYSQL_SYSTEM_DBS]


def my_tables(cn, db):
    with cn.cursor() as cur:
        cur.execute("select table_name, table_type, coalesce(data_length,0)+coalesce(index_length,0) "
                    "from information_schema.tables where table_schema=%s order by 1", (db,))
        return [(r[0], r[1], int(r[2])) for r in cur.fetchall()]


def my_inventory(c):
    cn = my_connect(c)
    out = {}
    for db in my_databases(cn):
        t = my_tables(cn, db)
        out[db] = {"tables": sum(1 for x in t if x[1] == "BASE TABLE"),
                   "views": sum(1 for x in t if x[1] == "VIEW"), "bytes": sum(x[2] for x in t)}
    cn.close()
    return out


def my_session(cn, target):
    with cn.cursor() as cur:
        if target:
            cur.execute("SET FOREIGN_KEY_CHECKS=0")
            cur.execute("SET UNIQUE_CHECKS=0")
            for stmt in ("SET SESSION sql_require_primary_key=0", "SET SESSION innodb_lock_wait_timeout=600"):
                try:
                    cur.execute(stmt)
                except Exception:
                    pass


def my_copy_table(src, dst, db, table, max_stmt):
    t0 = time.time()
    s = my_connect(src, db)
    with s.cursor() as cur:
        cur.execute("select column_name, data_type, generation_expression from information_schema.columns "
                    "where table_schema=%s and table_name=%s order by ordinal_position", (db, table))
        cols = [(r[0], r[1].lower()) for r in cur.fetchall() if not (r[2] or "").strip()]
    s.close()
    sel, kinds = [], []
    for name, dtype in cols:
        if dtype == "bit":
            sel.append(f"CAST({qi(name)} AS UNSIGNED)"); kinds.append("raw")
        elif dtype in ("geometry", "point", "linestring", "polygon", "multipoint", "multilinestring",
                       "multipolygon", "geomcollection", "geometrycollection"):
            sel.append(f"HEX({qi(name)})"); kinds.append("hex")
        else:
            sel.append(qi(name)); kinds.append("lit")
    s = my_connect(src, db, stream=True)
    d = my_connect(dst, db)
    my_session(s, False)
    my_session(d, True)
    head = f"INSERT INTO {qi(table)} ({','.join(qi(c[0]) for c in cols)}) VALUES "
    rows = 0
    with s.cursor() as sc, d.cursor() as dc:
        sc.execute(f"SELECT {','.join(sel)} FROM {qi(table)}")
        buf, size = [], len(head)
        lit = d.literal
        while True:
            batch = sc.fetchmany(2000)
            if not batch:
                break
            for row in batch:
                parts = []
                for v, k in zip(row, kinds):
                    if v is None:
                        parts.append("NULL")
                    elif k == "raw":
                        parts.append(v if isinstance(v, str) else v.decode())
                    elif k == "hex":
                        parts.append("X'" + (v if isinstance(v, str) else v.decode()) + "'")
                    else:
                        parts.append(lit(v))
                tup = "(" + ",".join(parts) + ")"
                if buf and size + len(tup) + 1 > max_stmt:
                    dc.execute(head + ",".join(buf))
                    buf, size = [], len(head)
                buf.append(tup)
                size += len(tup) + 1
                rows += 1
        if buf:
            dc.execute(head + ",".join(buf))
    s.close()
    d.close()
    return table, rows, time.time() - t0


def my_count(c, db, tables):
    cn = my_connect(c, db)
    out = {}
    with cn.cursor() as cur:
        for t in tables:
            cur.execute(f"select count(*) from {qi(t)}")
            out[t] = int(cur.fetchone()[0])
    cn.close()
    return out


def migrate_mysql(src, dst, args, run_dir):
    result = {"engine": "mysql", "source": mask(src), "target": mask(dst), "databases": {}, "ok": True}
    only = set(args.only.split(",")) if args.only else None
    s, d = my_connect(src), my_connect(dst)
    with s.cursor() as c1, d.cursor() as c2:
        c1.execute("select version()"); sv = c1.fetchone()[0]
        c2.execute("select version(), @@max_allowed_packet"); dv, packet = c2.fetchone()
    log(f"[mysql] source {sv} {mask(src)}  ->  target {dv} {mask(dst)}")
    max_stmt = max(256 * 1024, min(8 * 1024 * 1024, int(packet) // 2))
    dbs = [x for x in my_databases(s) if not only or x in only]
    tgt_dbs = set(my_databases(d))
    for db in dbs:
        if db in tgt_dbs and my_tables(d, db) and not args.overwrite:
            die(f"[mysql] target database '{db}' already has objects. Re-run with --overwrite to replace it.")
    for db in dbs:
        t = my_tables(s, db)
        log(f"[mysql] database {db}: {sum(1 for x in t if x[1] == 'BASE TABLE')} tables, "
            f"{sum(x[2] for x in t) / 1048576:.1f} MB")
    if args.dry_run:
        s.close(); d.close()
        return result

    for db in dbs:
        t0 = time.time()
        with s.cursor() as cur:
            cur.execute(f"SHOW CREATE DATABASE {qi(db)}")
            create_db = cur.fetchone()[1]
        my_session(d, True)
        with d.cursor() as cur:
            if args.overwrite:
                cur.execute(f"DROP DATABASE IF EXISTS {qi(db)}")
            cur.execute(create_db.replace("CREATE DATABASE", "CREATE DATABASE IF NOT EXISTS", 1)
                        if "IF NOT EXISTS" not in create_db else create_db)
            cur.execute(f"USE {qi(db)}")
        objs = my_tables(s, db)
        base = sorted([o for o in objs if o[1] == "BASE TABLE"], key=lambda o: -o[2])
        views = [o[0] for o in objs if o[1] == "VIEW"]
        errors = []
        # 1. tables (DDL)
        with s.cursor() as sc, d.cursor() as dc:
            sc.execute(f"USE {qi(db)}")
            for name, _, _ in base:
                sc.execute(f"SHOW CREATE TABLE {qi(name)}")
                ddl = sc.fetchone()[1]
                try:
                    dc.execute(ddl)
                except Exception as e:
                    errors.append(f"create table {name}: {e}")
        log(f"[mysql] {db}: created {len(base)} tables, copying data with {args.jobs} workers ...")
        # 2. data
        done = 0
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futs = {ex.submit(my_copy_table, src, dst, db, name, max_stmt): name for name, _, _ in base}
            for f in as_completed(futs):
                done += 1
                try:
                    t, n, secs = f.result()
                    log(f"[mysql]   ({done}/{len(base)}) {db}.{t}: {n:,} rows in {secs:.0f}s")
                except Exception as e:
                    errors.append(f"copy {futs[f]}: {e}")
                    log(f"[mysql]   ({done}/{len(base)}) {db}.{futs[f]}: FAILED {e}")
        # 3. views (retry loop for view-on-view dependencies)
        with s.cursor() as sc:
            sc.execute(f"USE {qi(db)}")
            view_ddl = {}
            for v in views:
                sc.execute(f"SHOW CREATE VIEW {qi(v)}")
                view_ddl[v] = strip_definer(sc.fetchone()[1])
        pending, last_err = dict(view_ddl), {}
        while pending:
            progressed = False
            with d.cursor() as dc:
                for v, ddl in list(pending.items()):
                    try:
                        dc.execute(ddl); pending.pop(v); progressed = True
                    except Exception as e:
                        last_err[v] = str(e)
            if not progressed:
                errors += [f"create view {v}: {last_err[v]}" for v in pending]
                break
        # 4. routines, triggers, events
        with s.cursor() as sc:
            sc.execute(f"USE {qi(db)}")
            sc.execute("select routine_type, routine_name from information_schema.routines where routine_schema=%s", (db,))
            routines = sc.fetchall()
            sc.execute("select trigger_name from information_schema.triggers where trigger_schema=%s", (db,))
            triggers = [r[0] for r in sc.fetchall()]
            sc.execute("select event_name from information_schema.events where event_schema=%s", (db,))
            events = [r[0] for r in sc.fetchall()]
            extra = []
            # bodies are stored as written, so recreate each under its original sql_mode
            for rtype, rname in routines:
                sc.execute(f"SHOW CREATE {rtype} {qi(rname)}")
                r = sc.fetchone()
                extra.append((f"{rtype.lower()} {rname}", r[1], r[2]))
            for t in triggers:
                sc.execute(f"SHOW CREATE TRIGGER {qi(t)}")
                r = sc.fetchone()
                extra.append((f"trigger {t}", r[1], r[2]))
            for e in events:
                sc.execute(f"SHOW CREATE EVENT {qi(e)}")
                r = sc.fetchone()
                extra.append((f"event {e}", r[1], r[3]))
        with d.cursor() as dc:
            for label, mode, ddl in extra:
                try:
                    dc.execute("SET SESSION sql_mode=%s", (mode or "",))
                    dc.execute(strip_definer(ddl))
                except Exception as e:
                    errors.append(f"create {label}: {e}")
            dc.execute(f"SET SESSION sql_mode='{NEUTRAL_SQL_MODE}'")
        # 5. verify
        names = [b[0] for b in base]
        sc_, dc_ = my_count(src, db, names), my_count(dst, db, names)
        mism = {k: (v, dc_.get(k)) for k, v in sc_.items() if dc_.get(k) != v}
        tgt_objs = my_tables(d, db)
        obj_ok = (len([o for o in tgt_objs if o[1] == "BASE TABLE"]) == len(base)
                  and len([o for o in tgt_objs if o[1] == "VIEW"]) == len(views))
        ok = not mism and not errors and obj_ok
        result["ok"] &= ok
        result["databases"][db] = {"tables": len(base), "views": len(views), "routines": len(routines),
                                   "triggers": len(triggers), "events": len(events), "rows": sum(sc_.values()),
                                   "row_mismatches": mism, "errors": errors, "seconds": round(time.time() - t0)}
        log(f"[mysql] {db}: {len(base)} tables, {sum(sc_.values()):,} rows, "
            f"{'VERIFIED' if ok else 'PROBLEMS'} ({round(time.time() - t0)}s)")
        for k, (a, b) in list(mism.items())[:20]:
            log(f"     rows differ {k}: source={a} target={b}")
        for e in errors[:30]:
            log(f"     {e}")
    s.close(); d.close()
    return result


# --------------------------------------------------------------------------- commands
def get_source(conf, engine, override):
    if override:
        return parse_uri(override)
    cur = (conf.get(engine) or {}).get("current")
    if not cur:
        die(f"no saved {engine} source. Save one with: set --engine {engine} --uri URI (or pass --from-{engine})")
    return cur


def server_identity(c):
    try:
        if c["engine"] == "pg":
            with pg_connect(c) as cn:
                try:
                    return "pg:" + str(cn.execute("select system_identifier from pg_control_system()").fetchone()[0])
                except Exception:
                    return None
        cn = my_connect(c)
        with cn.cursor() as cur:
            cur.execute("select @@server_uuid")
            v = cur.fetchone()[0]
        cn.close()
        return "mysql:" + v
    except Exception:
        return None


def same_service(a, b):
    import socket
    if int(a["port"]) == int(b["port"]):
        try:
            ia = {x[4][0] for x in socket.getaddrinfo(a["host"], a["port"])}
            ib = {x[4][0] for x in socket.getaddrinfo(b["host"], b["port"])}
            if a["host"].lower() == b["host"].lower() or ia & ib:
                return True
        except OSError:
            if a["host"].lower() == b["host"].lower():
                return True
    ida, idb = server_identity(a), server_identity(b)
    return bool(ida and ida == idb)


def test_conn(c):
    if c["engine"] == "pg":
        with pg_connect(c) as cn:
            return "PostgreSQL " + cn.execute("show server_version").fetchone()[0]
    cn = my_connect(c)
    with cn.cursor() as cur:
        cur.execute("select version()")
        v = cur.fetchone()[0]
    cn.close()
    return "MySQL " + v


def cmd_show(args):
    conf = load_conf()
    for eng in ("pg", "mysql"):
        slot = conf.get(eng) or {}
        cur = slot.get("current")
        log(f"{eng:5s} current: {mask(cur) + '  (since ' + cur.get('since', '?') + ')' if cur else '-'}")
        for h in slot.get("history", []):
            log(f"      retired {h.get('retired_at', '?')}: {mask(h)}")
    log(f"\nconfig: {CONF}")


def cmd_set(args):
    c = parse_uri(args.uri)
    if args.engine and c["engine"] != args.engine:
        die(f"URI is {c['engine']} but --engine {args.engine}")
    log(f"testing {mask(c)} ... {test_conn(c)}")
    conf = load_conf()
    promote(conf, c["engine"], c)
    save_conf(conf)
    log(f"saved as current {c['engine']} connection")


def cmd_test(args):
    c = parse_uri(args.uri)
    log(f"{mask(c)} -> {test_conn(c)}")


def cmd_inventory(args):
    conf = load_conf()
    c = parse_uri(args.uri) if args.uri else get_source(conf, args.engine, None)
    log(f"{mask(c)}")
    inv = pg_inventory(c) if c["engine"] == "pg" else my_inventory(c)
    log(json.dumps(inv, indent=2, default=str))


def cmd_run(args):
    conf = load_conf()
    plans = []
    for eng, to, frm in (("pg", args.to_pg, args.from_pg), ("mysql", args.to_mysql, args.from_mysql)):
        if not to:
            continue
        dst = parse_uri(to)
        if dst["engine"] != eng:
            die(f"--to-{eng} must be a {eng} URI")
        src = get_source(conf, eng, frm)
        if same_service(src, dst):
            die(f"[{eng}] source and target are the same service ({dst['host']}:{dst['port']}) - refusing")
        plans.append((eng, src, dst))
    if not plans:
        die("nothing to do: pass --to-pg and/or --to-mysql")
    for eng, src, dst in plans:
        log(f"[{eng}] source check: {test_conn(src)}")
        log(f"[{eng}] target check: {test_conn(dst)}")
    run_dir = BACKUPS / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for eng, src, dst in plans:
        res = (migrate_pg if eng == "pg" else migrate_mysql)(src, dst, args, run_dir)
        results.append(res)
        if not args.dry_run and res["ok"] and not args.no_promote:
            promote(conf, eng, dst)
            save_conf(conf)
            log(f"[{eng}] target promoted to 'current' - next month only the new target is needed")
    (run_dir / "report.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    # prune old backup runs
    runs = sorted([p for p in BACKUPS.iterdir() if p.is_dir()], reverse=True)
    for old in runs[KEEP_BACKUP_RUNS:]:
        shutil.rmtree(old, ignore_errors=True)
    log("\n================ SUMMARY ================")
    for r in results:
        status = "dry-run" if args.dry_run else ("OK - verified" if r["ok"] else "NEEDS ATTENTION")
        log(f"{r['engine']:5s} {status}:  {r['source']}  ->  {r['target']}")
        for db, d in r["databases"].items():
            log(f"      {db}: {d.get('tables')} tables, {d.get('rows', 0):,} rows, {d.get('seconds')}s")
    log(f"report: {run_dir / 'report.json'}")
    if not all(r["ok"] for r in results):
        sys.exit(2)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    p = sub.add_parser("set"); p.add_argument("--engine", choices=["pg", "mysql"]); p.add_argument("--uri", required=True)
    p = sub.add_parser("test"); p.add_argument("--uri", required=True)
    p = sub.add_parser("inventory"); p.add_argument("--engine", choices=["pg", "mysql"], default="pg"); p.add_argument("--uri")
    p = sub.add_parser("run")
    p.add_argument("--to-pg"); p.add_argument("--to-mysql")
    p.add_argument("--from-pg"); p.add_argument("--from-mysql")
    p.add_argument("--only", help="comma list: mysql databases / pg schemas")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--no-promote", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        import psycopg, pymysql  # noqa
    except ImportError:
        die('missing drivers: python -m pip install "psycopg[binary]" pymysql')
    {"show": cmd_show, "set": cmd_set, "test": cmd_test, "inventory": cmd_inventory, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
