"""Versioned metadata projection persistence; never read bodies on page paths.

SQLite source triggers make projections transactional. File stores use a local
SQLite metadata index with a durable pending-write marker. A crash between the
file rename and projection commit makes reads unavailable until explicit repair.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from puppetmaster.contracts import (
    CursorCodec, MAX_BYTES, MAX_PAGE, MAX_SCAN, MetadataPage, MetadataRef, TaskBinding,
    immutable_digest,
)
from puppetmaster.models import JobRef, to_jsonable
from puppetmaster.state import state_identity


def create_schema(c):
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    # Generated journal triggers must not reference a changing schema.
    c.execute("DROP TRIGGER IF EXISTS projection_previous_status")
    c.execute("DROP TRIGGER IF EXISTS projection_scope")
    c.execute("CREATE TABLE IF NOT EXISTS projection_meta(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT OR IGNORE INTO projection_meta VALUES('secret', ?)", (secrets.token_hex(32),))
    c.execute("INSERT OR IGNORE INTO projection_meta VALUES('version', '1')")
    c.execute("INSERT OR IGNORE INTO projection_meta VALUES('epoch', '0')")
    c.execute("""CREATE TABLE IF NOT EXISTS projection_changes(
        revision INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        job_id TEXT NOT NULL, id TEXT NOT NULL, status TEXT, sha256 TEXT,
        stamp TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
        task_count INTEGER, artifact_count INTEGER, binding TEXT, task_id TEXT, artifact_type TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS projection_current(
        kind TEXT NOT NULL, job_id TEXT NOT NULL, id TEXT NOT NULL,
        status TEXT, sha256 TEXT, revision INTEGER NOT NULL, stamp TEXT NOT NULL,
        task_count INTEGER, artifact_count INTEGER, binding TEXT, task_id TEXT, artifact_type TEXT,
        PRIMARY KEY(kind, job_id, id))""")
    c.execute("CREATE INDEX IF NOT EXISTS projection_filter ON projection_current(kind,status,id)")
    c.execute("CREATE INDEX IF NOT EXISTS projection_scoped_filter ON projection_current(kind,job_id,status,id)")
    c.execute("CREATE INDEX IF NOT EXISTS projection_change_filter ON projection_changes(kind,status,revision)")
    c.execute("CREATE INDEX IF NOT EXISTS projection_order ON projection_current(kind,id)")
    c.execute("CREATE INDEX IF NOT EXISTS projection_feed ON projection_changes(kind, revision)")
    c.execute("CREATE INDEX IF NOT EXISTS projection_scoped_feed ON projection_changes(kind,job_id,revision)")
    c.execute("CREATE INDEX IF NOT EXISTS projection_entity_history ON projection_changes(kind,job_id,id,revision)")
    if "previous_status" not in {row[1] for row in c.execute("PRAGMA table_info(projection_changes)")}:
        c.execute("ALTER TABLE projection_changes ADD COLUMN previous_status TEXT")
        c.execute("""UPDATE projection_changes SET previous_status=(
            SELECT CASE WHEN prior.deleted=0 THEN prior.status END FROM projection_changes AS prior
            WHERE prior.kind=projection_changes.kind AND prior.job_id=projection_changes.job_id
              AND prior.id=projection_changes.id AND prior.revision<projection_changes.revision
            ORDER BY prior.revision DESC LIMIT 1)""")
    c.execute("CREATE INDEX IF NOT EXISTS projection_previous_filter ON projection_changes(kind,previous_status,revision)")
    c.execute("CREATE INDEX IF NOT EXISTS projection_change_scoped_filter ON projection_changes(kind,job_id,status,revision)")
    c.execute("CREATE INDEX IF NOT EXISTS projection_previous_scoped_filter ON projection_changes(kind,job_id,previous_status,revision)")
    # Both backends append the change before replacing the current projection.
    c.execute("""CREATE TRIGGER IF NOT EXISTS projection_previous_status
        AFTER INSERT ON projection_changes BEGIN
        UPDATE projection_changes SET previous_status=(
            SELECT status FROM projection_current
            WHERE kind=NEW.kind AND job_id=NEW.job_id AND id=NEW.id)
        WHERE revision=NEW.revision;
        END""")
    for table, columns in (("projection_current", ("scope",)),
                           ("projection_changes", ("scope", "previous_scope"))):
        existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
        for column in columns:
            if column not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    c.execute("""CREATE TRIGGER IF NOT EXISTS projection_scope
        AFTER INSERT ON projection_changes BEGIN
        UPDATE projection_changes SET
            scope=COALESCE(NEW.scope, (SELECT scope FROM projection_current
                WHERE kind=NEW.kind AND job_id=NEW.job_id AND id=NEW.id)),
            previous_scope=(SELECT scope FROM projection_current
                WHERE kind=NEW.kind AND job_id=NEW.job_id AND id=NEW.id)
        WHERE revision=NEW.revision;
        END""")
    c.execute("CREATE TABLE IF NOT EXISTS projection_pending(path TEXT PRIMARY KEY)")
    c.execute("""CREATE TABLE IF NOT EXISTS contract_receipts(
        kind TEXT NOT NULL, job_id TEXT NOT NULL, id TEXT NOT NULL,
        data TEXT NOT NULL, PRIMARY KEY(kind,job_id,id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS cancellation_targets(
        job_id TEXT, task_id TEXT, generation INTEGER, lease_id TEXT, owner TEXT,
        request_id TEXT, binding_digest TEXT, observed INTEGER,
        PRIMARY KEY(job_id,task_id,binding_digest,request_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS cancellation_request ON cancellation_targets(job_id,request_id,observed)")


def install_source_triggers(c):
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    # Drop every dependent source trigger before ALTER TABLE validates them.
    # Rebuild in the same transaction, including on already-v5 databases.
    for table in ("jobs", "tasks", "artifacts"):
        for operation in ("INSERT", "UPDATE", "DELETE"):
            c.execute(f"DROP TRIGGER IF EXISTS projection_{table}_{operation}")
    create_schema(c)
    for table, kind in (("jobs", "job"), ("tasks", "task"), ("artifacts", "artifact")):
        job = "NEW.id" if kind == "job" else "NEW.job_id"
        old_job = "OLD.id" if kind == "job" else "OLD.job_id"
        status_key = "execution_status" if kind == "artifact" else "status"
        status = f"json_extract(NEW.data, '$.{status_key}')"
        binding = ("json_object('task_id',NEW.id,'generation',json_extract(NEW.data,'$.generation'),"
                   "'lease_id',json_extract(NEW.data,'$.lease_id'),'owner',json_extract(NEW.data,'$.lease_owner'))"
                   if kind == "task" else "NULL")
        task_id = "NEW.task_id" if kind == "artifact" else "NULL"
        artifact_type = "NEW.type" if kind == "artifact" else "NULL"
        sha = "json_extract(NEW.data, '$.sha256')" if kind == "artifact" else "NULL"
        legacy_sha = sha.replace("NEW.", "")
        scope = ("json_object('origin',json_extract(NEW.data,'$.origin'),"
                 "'project_id',json_extract(NEW.data,'$.project_id'),"
                 "'session_id',json_extract(NEW.data,'$.session_id'))" if kind == "job" else "NULL")
        # Seed missing projections without replacing existing revision provenance.
        c.execute(f"""INSERT OR IGNORE INTO projection_current
            (kind,job_id,id,status,sha256,revision,stamp,task_count,artifact_count,
             binding,task_id,artifact_type,scope)
            SELECT ?, {'id' if kind == 'job' else 'job_id'}, id,
                   json_extract(data, '$.{status_key}'),
                   {legacy_sha},
                   0, 'legacy_unknown', NULL, NULL,
                   {binding.replace('NEW.data','data').replace('NEW.id','id')},
                   {task_id.replace('NEW.','')}, {artifact_type.replace('NEW.','')},
                   {scope.replace('NEW.', '')} FROM {table}""", (kind,))
        if kind == "job":
            c.execute("""UPDATE projection_current SET scope=(SELECT
                json_object('origin',json_extract(data,'$.origin'),
                            'project_id',json_extract(data,'$.project_id'),
                            'session_id',json_extract(data,'$.session_id'))
                FROM jobs WHERE jobs.id=projection_current.id)
                WHERE kind='job' AND scope IS NULL""")
        for operation in ("INSERT", "UPDATE"):
            child_touch = ""
            child_move = ""
            if kind != "job":
                column = "task_count" if kind == "task" else "artifact_count"
                delta = "1" if operation == "INSERT" else "(OLD.job_id != NEW.job_id)"
                if operation == "UPDATE":
                    child_move = f"""
                        INSERT INTO projection_changes(kind,job_id,id,status,sha256,stamp,deleted,binding,task_id,artifact_type)
                            SELECT kind,job_id,id,status,sha256,'known',1,binding,task_id,artifact_type
                            FROM projection_current WHERE kind='{kind}' AND job_id=OLD.job_id
                            AND id=OLD.id AND OLD.job_id != NEW.job_id;
                        DELETE FROM projection_current WHERE kind='{kind}' AND job_id=OLD.job_id
                            AND id=OLD.id AND OLD.job_id != NEW.job_id;
                        UPDATE projection_current SET {column}=MAX(0,COALESCE({column},0)-1)
                            WHERE kind='job' AND job_id=OLD.job_id AND OLD.job_id != NEW.job_id;
                        INSERT INTO projection_changes(kind,job_id,id,status,stamp,task_count,artifact_count)
                            SELECT kind,job_id,id,status,'known',task_count,artifact_count
                            FROM projection_current WHERE kind='job' AND job_id=OLD.job_id
                            AND OLD.job_id != NEW.job_id;
                        UPDATE projection_current SET revision=last_insert_rowid(), stamp='known'
                            WHERE kind='job' AND job_id=OLD.job_id AND OLD.job_id != NEW.job_id;
                    """
                child_touch = f"""
                    UPDATE projection_current SET {column}=COALESCE({column},0)+{delta}
                        WHERE kind='job' AND job_id={job};
                    INSERT INTO projection_changes(kind,job_id,id,status,stamp,task_count,artifact_count)
                        SELECT 'job',job_id,id,status,'known',task_count,artifact_count
                        FROM projection_current WHERE kind='job' AND job_id={job};
                    UPDATE projection_current SET revision=last_insert_rowid(), stamp='known'
                        WHERE kind='job' AND job_id={job};
                """
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS projection_{table}_{operation}
                AFTER {operation} ON {table} BEGIN
                {child_move}
                INSERT INTO projection_changes(kind,job_id,id,status,sha256,stamp,scope)
                VALUES('{kind}',{job},NEW.id,{status},{sha},'known',{scope});
                INSERT INTO projection_current
                    (kind,job_id,id,status,sha256,revision,stamp,task_count,artifact_count,
                     binding,task_id,artifact_type,scope) VALUES(
                    '{kind}',{job},NEW.id,{status},{sha},last_insert_rowid(),'known',
                    {'0,0' if kind == 'job' else 'NULL,NULL'}, {binding}, {task_id}, {artifact_type}, {scope})
                ON CONFLICT(kind,job_id,id) DO UPDATE SET status=excluded.status,
                    sha256=excluded.sha256, revision=excluded.revision, stamp=excluded.stamp,
                    binding=excluded.binding, task_id=excluded.task_id, artifact_type=excluded.artifact_type,
                    scope=excluded.scope;
                UPDATE projection_changes SET
                    binding={binding}, task_id={task_id}, artifact_type={artifact_type},
                    task_count=(SELECT task_count FROM projection_current WHERE kind='{kind}' AND job_id={job} AND id=NEW.id),
                    artifact_count=(SELECT artifact_count FROM projection_current WHERE kind='{kind}' AND job_id={job} AND id=NEW.id)
                    WHERE revision=last_insert_rowid();
                {child_touch}
                END""")
        child_delete = ""
        if kind != "job":
            column = "task_count" if kind == "task" else "artifact_count"
            child_delete = f"""
                UPDATE projection_current SET {column}=MAX(0,COALESCE({column},0)-1)
                    WHERE kind='job' AND job_id={old_job};
                INSERT INTO projection_changes(kind,job_id,id,status,stamp,task_count,artifact_count)
                    SELECT 'job',job_id,id,status,'known',task_count,artifact_count
                    FROM projection_current WHERE kind='job' AND job_id={old_job};
                UPDATE projection_current SET revision=last_insert_rowid(), stamp='known'
                    WHERE kind='job' AND job_id={old_job};
            """
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS projection_{table}_DELETE
            AFTER DELETE ON {table} BEGIN
            INSERT INTO projection_changes(kind,job_id,id,status,sha256,stamp,deleted,task_count,artifact_count,binding,task_id,artifact_type)
            SELECT kind,job_id,id,status,sha256,'known',1,task_count,artifact_count,binding,task_id,artifact_type
            FROM projection_current WHERE kind='{kind}' AND job_id={old_job} AND id=OLD.id;
            DELETE FROM projection_current WHERE kind='{kind}' AND job_id={old_job} AND id=OLD.id;
            {child_delete}
            END""")
    c.execute("""UPDATE projection_current SET
        task_count=(SELECT COUNT(*) FROM tasks WHERE tasks.job_id=projection_current.job_id),
        artifact_count=(SELECT COUNT(*) FROM artifacts WHERE artifacts.job_id=projection_current.job_id)
        WHERE kind='job'""")


@contextmanager
def connection(store, *, metadata_only=False):
    if metadata_only:
        path = store.root / ("state.sqlite3" if store.backend_name == "sqlite" else "metadata.sqlite3")
        c = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()
    elif store.backend_name == "sqlite":
        store._ensure_attached()
        with store._session() as c:
            yield c
    else:
        c = sqlite3.connect(str(store.root / "metadata.sqlite3"), timeout=5)
        c.row_factory = sqlite3.Row
        try:
            with c:
                yield c
        finally:
            c.close()


def initialize_file(store):
    with connection(store) as c:
        create_schema(c)
        if c.execute("SELECT 1 FROM projection_meta WHERE key='initialized'").fetchone():
            return
        # Explicit initialization only, never performed by a metadata query.
        for path in store.jobs_dir.glob("*/job.json"):
            project_file(c, path, store.read_json(path), legacy=True)
            for directory in ("tasks", "artifacts"):
                for child in (path.parent / directory).glob("*.json"):
                    project_file(c, child, store.read_json(child), legacy=True)
        c.execute("INSERT INTO projection_meta VALUES('initialized', '1')")


def project_file(c, path, value, legacy=False):
    kind = file_kind(path)
    if kind is None:
        return
    value = to_jsonable(value)
    jid = value["id"] if kind == "job" else value["job_id"]
    stamp = "legacy_unknown" if legacy else "known"
    args = (kind, jid, value["id"], value.get("execution_status" if kind == "artifact" else "status"), value.get("sha256"), stamp)
    scope = json.dumps({key: value.get(key) for key in ("origin", "project_id", "session_id")}) if kind == "job" else None
    cur = c.execute("""INSERT INTO projection_changes(kind,job_id,id,status,sha256,stamp,scope)
                       VALUES(?,?,?,?,?,?,?)""", (*args, scope))
    exists = c.execute("SELECT 1 FROM projection_current WHERE kind=? AND job_id=? AND id=?",
                       (kind, jid, value["id"])).fetchone()
    c.execute("""INSERT INTO projection_current
        (kind,job_id,id,status,sha256,revision,stamp,task_count,artifact_count,
         binding,task_id,artifact_type,scope) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(kind,job_id,id) DO UPDATE SET status=excluded.status,
        sha256=excluded.sha256, revision=excluded.revision, stamp=excluded.stamp,
        binding=excluded.binding, task_id=excluded.task_id, artifact_type=excluded.artifact_type,
        scope=excluded.scope""",
              (*args[:5], cur.lastrowid, stamp, 0 if kind == "job" else None, 0 if kind == "job" else None,
               json.dumps({"task_id": value["id"], "generation": value.get("generation"),
                           "lease_id": value.get("lease_id"), "owner": value.get("lease_owner")}) if kind == "task" else None,
               value.get("task_id") if kind == "artifact" else None,
               value.get("type") if kind == "artifact" else None, scope))
    if kind != "job":
        column = "task_count" if kind == "task" else "artifact_count"
        c.execute(f"UPDATE projection_current SET {column}=COALESCE({column},0)+? WHERE kind='job' AND job_id=?",
                  (0 if exists else 1, jid))
        cur = c.execute("""INSERT INTO projection_changes(kind,job_id,id,status,stamp,task_count,artifact_count)
            SELECT 'job',job_id,id,status,?,task_count,artifact_count
            FROM projection_current WHERE kind='job' AND job_id=?""", (stamp, jid))
        c.execute("UPDATE projection_current SET revision=?, stamp=? WHERE kind='job' AND job_id=?",
                  (cur.lastrowid, stamp, jid))
    else:
        c.execute("""UPDATE projection_changes SET
            task_count=(SELECT task_count FROM projection_current WHERE kind='job' AND job_id=?),
            artifact_count=(SELECT artifact_count FROM projection_current WHERE kind='job' AND job_id=?)
            WHERE revision=?""", (jid, jid, cur.lastrowid))


def file_kind(path):
    if path.name == "job.json" and path.parent.parent.name == "jobs":
        return "job"
    if path.parent.name in {"tasks", "artifacts"} and path.parent.parent.parent.name == "jobs":
        return "task" if path.parent.name == "tasks" else "artifact"
    return None


def page(store, kind, job_ref=None, *, cursor=None, limit=100, max_bytes=MAX_BYTES,
         max_scan=MAX_SCAN, changes=False, after_revision=0, status=None,
         origin=None, project_id=None, session_id=None):
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError("limit must be between 1 and 200")
    if type(max_bytes) is not int or not 1024 <= max_bytes <= MAX_BYTES:
        raise ValueError("max_bytes must be between 1024 and 262144")
    if type(max_scan) is not int or not 1 <= max_scan <= MAX_SCAN:
        raise ValueError("max_scan must be between 1 and 1000")
    if type(after_revision) is not int or after_revision < 0:
        raise ValueError("invalid revision")
    if job_ref is not None:
        if not isinstance(job_ref, JobRef) or job_ref.state_id != state_identity(store.root):
            raise ValueError("job_ref.state_id does not match this store")
        store._assert_safe_job_dir(job_ref.job_id)
    if status is not None and (not isinstance(status, str) or len(status) > 64):
        raise ValueError("invalid status filter")
    filters = {"origin": origin, "project_id": project_id, "session_id": session_id}
    for name, value in filters.items():
        if value is not None and (kind != "job" or not isinstance(value, str) or not value or len(value) > 256):
            raise ValueError(f"invalid {name} filter")
    sid = state_identity(store.root)
    scope = immutable_digest([sid, kind, to_jsonable(job_ref), changes, after_revision, status, filters])
    try:
        database = store.root / ("state.sqlite3" if store.backend_name == "sqlite" else "metadata.sqlite3")
        if not database.exists():
            return MetadataPage((), "unavailable", 0)
        with connection(store, metadata_only=True) as c:
            if not c.in_transaction:
                c.execute("BEGIN")
            if c.execute("SELECT 1 FROM projection_pending LIMIT 1").fetchone():
                return MetadataPage((), "unavailable", 0)
            rev = c.execute("SELECT COALESCE(MAX(revision),0) FROM projection_changes").fetchone()[0]
            secret = c.execute("SELECT value FROM projection_meta WHERE key='secret'").fetchone()[0]
            codec = CursorCodec(bytes.fromhex(secret))
            epoch = c.execute("SELECT value FROM projection_meta WHERE key='epoch'").fetchone()[0]
            value = codec.decode(cursor, scope) if cursor else {}
            if value.get("epoch", epoch) != epoch:
                return MetadataPage((), "cursor_expired", rev)
            if job_ref and not changes and not c.execute("SELECT 1 FROM projection_current WHERE kind='job' AND job_id=? LIMIT 1",
                                         (job_ref.job_id,)).fetchone():
                return MetadataPage((), "unavailable", rev)
            snapshot = value.get("revision", rev)
            if after_revision > rev or snapshot > rev or (not changes and snapshot != rev):
                return MetadataPage((), "cursor_expired", rev)
            last = value.get("last", after_revision if changes else "")
            args = [kind]
            where = "kind=?"
            if status is not None:
                where += " AND status=?"
                args.append(status)
            if job_ref:
                where += " AND job_id=?"
                args.append(job_ref.job_id)
            table = "projection_changes" if changes else "projection_current"
            key = "revision" if changes else "id"
            where += f" AND {key}>?"
            args.append(last)
            if changes:
                where += " AND revision<=?"
                args.append(snapshot)
            query = f"SELECT * FROM {table} WHERE {where}"
            if changes and status is not None:
                # UNION's ordered merge streams the two status indexes and
                # deduplicates unchanged membership without sorting the journal.
                previous_where = where.replace("status=?", "previous_status=?")
                suffix = "scoped_filter" if job_ref else "filter"
                query = f"SELECT * FROM {table} INDEXED BY projection_change_{suffix} WHERE {where}"
                query += f" UNION SELECT * FROM {table} INDEXED BY projection_previous_{suffix} WHERE {previous_where}"
                args += args
            rows = c.execute(query + f" ORDER BY {key} LIMIT ?",
                             (*args, min(limit + 1, max_scan))).fetchall()
            items = []
            consumed = 0
            for row in rows:
                if len(items) == limit:
                    break
                current_scope = json.loads(row["scope"] or "{}")
                previous_scope = json.loads(row["previous_scope"] or "{}") if changes else {}
                current_matches = (status is None or row["status"] == status) and all(
                    value is None or current_scope.get(name) == value for name, value in filters.items())
                previous_matches = changes and (status is None or row["previous_status"] == status) and all(
                    value is None or previous_scope.get(name) == value for name, value in filters.items())
                if not current_matches and not previous_matches:
                    last = row[key]
                    consumed += 1
                    continue
                item = MetadataRef(JobRef(row["job_id"], sid), row["id"], kind,
                    row["status"], row["sha256"], row["revision"], row["stamp"],
                    (bool(row["deleted"]) or not current_matches) if changes else False,
                    row["task_count"], row["artifact_count"],
                    TaskBinding(**json.loads(row["binding"])) if row["binding"] else None,
                    row["task_id"], row["artifact_type"],
                    current_scope.get("origin"), current_scope.get("project_id"), current_scope.get("session_id"))
                candidate_token = codec.encode({"v": 1, "scope": scope, "revision": snapshot, "last": row[key], "epoch": epoch})
                candidate = MetadataPage(tuple(items + [item]), "partial", snapshot, candidate_token, len(rows))
                if len(json.dumps(to_jsonable(candidate), ensure_ascii=True).encode()) > max_bytes:
                    break
                items.append(item)
                consumed += 1
                last = row[key]
            if rows and not consumed:
                return MetadataPage((), "unavailable", snapshot, scanned=len(rows))
            more = consumed < len(rows) or len(rows) == min(limit + 1, max_scan)
            token = codec.encode({"v": 1, "scope": scope, "revision": snapshot, "last": last, "epoch": epoch}) if more else None
            return MetadataPage(tuple(items), "partial" if more else "complete", snapshot, token, len(rows))
    except sqlite3.OperationalError as exc:
        code = getattr(exc, "sqlite_errorcode", None)
        # Python 3.9 has no sqlite_errorcode; match SQLite's lock messages only.
        locked = ((code & 0xff) in (5, 6)) if code is not None else str(exc) in {
            "database is locked", "database table is locked", "database schema is locked",
        } or str(exc).startswith(("database table is locked: ", "database schema is locked: "))
        if locked:
            return MetadataPage((), "unavailable", 0, next_cursor=cursor)
        if "no such table" in str(exc):
            return MetadataPage((), "unavailable", 0)
        raise
