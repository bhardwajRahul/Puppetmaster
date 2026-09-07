"""Scalar historical evidence projections, independent of delivery and task state.

Page coverage describes captured records only, never all provider invocations.
Projection writes occur with source writes; migration may read source bodies,
but readers use only this index. Sequence high-water marks exclude later inserts.
"""
from __future__ import annotations

import json
from puppetmaster.bounded_json import loads as metadata_json_loads
import sqlite3
from dataclasses import dataclass
from typing import Optional, Tuple, Literal

from puppetmaster.contracts import CursorCodec, MAX_BYTES, MAX_PAGE, MAX_SCAN, immutable_digest
from puppetmaster.identity import scope_identity, validate, make_ref
from puppetmaster.models import JobRef, to_jsonable

FIELDS = {
    "attempt": ("attempt_id", "task_id", "run_id", "started_at", "adapter", "model", "provider"),
    "run": ("id", "task_id", "role", "worker_id", "status", "started_at", "completed_at"),
    "observation": ("attempt_id", "observation_id", "source", "observed_at", "usage_state",
                    "tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens",
                    "cost_state", "cost_usd", "cost_basis", "returncode", "timed_out"),
}
FIELDS["outcome"] = FIELDS["observation"]


@dataclass(frozen=True)
class HistoricalRef:
    job_ref: JobRef
    kind: Literal["attempt", "run", "observation", "outcome"]
    sequence: int
    facts: dict


@dataclass(frozen=True)
class HistoricalPage:
    items: Tuple[HistoricalRef, ...] = ()
    outcome: Literal["complete", "partial", "unavailable", "cursor_expired"] = "unavailable"
    next_cursor: Optional[str] = None
    scanned: int = 0
    captured_count: Optional[int] = None
    coverage: Literal["captured", "partial", "unknown"] = "unknown"
    complete_invocation_history: bool = False


def create_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS historical_refs(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
        job_id TEXT NOT NULL, id TEXT NOT NULL, facts TEXT NOT NULL,
        UNIQUE(kind,job_id,id))""")
    c.execute("CREATE INDEX IF NOT EXISTS historical_page ON historical_refs(kind,job_id,sequence)")
    c.execute("""CREATE TABLE IF NOT EXISTS historical_counts(
        kind TEXT NOT NULL, job_id TEXT NOT NULL, count INTEGER NOT NULL,
        PRIMARY KEY(kind,job_id))""")
    c.execute("""CREATE TRIGGER IF NOT EXISTS historical_insert AFTER INSERT ON historical_refs BEGIN
        INSERT INTO historical_counts VALUES(NEW.kind,NEW.job_id,1)
        ON CONFLICT(kind,job_id) DO UPDATE SET count=count+1; END""")
    c.execute("CREATE TABLE IF NOT EXISTS historical_epochs(kind TEXT,job_id TEXT,epoch INTEGER,PRIMARY KEY(kind,job_id))")
    c.execute("DROP TRIGGER IF EXISTS historical_delete")
    c.execute("""CREATE TRIGGER IF NOT EXISTS historical_delete AFTER DELETE ON historical_refs BEGIN
        UPDATE historical_counts SET count=count-1 WHERE kind=OLD.kind AND job_id=OLD.job_id;
        INSERT INTO historical_epochs VALUES(OLD.kind,OLD.job_id,1)
        ON CONFLICT(kind,job_id) DO UPDATE SET epoch=epoch+1; END""")


def _sql_facts(kind, prefix):
    values = []
    for field in FIELDS[kind]:
        value = f"json_extract({prefix}data,'$.{field}')"
        if field == "timed_out":
            value = f"json(CASE json_type({prefix}data,'$.timed_out') WHEN 'true' THEN 'true' WHEN 'false' THEN 'false' ELSE 'null' END)"
        values.extend((f"'{field}'", value))
    return "json_object(" + ",".join(values) + ")"


def install_source_triggers(c):
    create_schema(c)
    seed = not c.execute("SELECT 1 FROM projection_meta WHERE key='history_version'").fetchone()
    for table, kind, key in (("execution_attempts", "attempt", "attempt_id"),
                             ("runs", "run", "id"),
                             ("usage_observations", "observation", "observation_id"),
                             ("usage_observations", "outcome", "observation_id")):
        def identity(prefix):
            return (f"json_array({prefix}attempt_id,{prefix}observation_id)"
                    if table == "usage_observations" else prefix + key)
        condition = ("(json_extract(NEW.data,'$.returncode') IS NOT NULL OR "
                     "json_extract(NEW.data,'$.timed_out') IS NOT NULL)" if kind == "outcome" else "1")
        if seed:
            c.execute(f"""INSERT OR IGNORE INTO historical_refs(kind,job_id,id,facts)
                SELECT '{kind}',job_id,{identity('')},{_sql_facts(kind, '')}
                FROM {table} WHERE {condition.replace('NEW.', '')}""")
        for op in ("INSERT", "UPDATE", "DELETE"):
            name = f"historical_{kind}_{op}"
            c.execute(f"DROP TRIGGER IF EXISTS {name}")
            if op == "DELETE":
                body = f"DELETE FROM historical_refs WHERE kind='{kind}' AND job_id=OLD.job_id AND id={identity('OLD.')};"
            else:
                body = f"""INSERT INTO historical_refs(kind,job_id,id,facts)
                    SELECT '{kind}',NEW.job_id,{identity('NEW.')},{_sql_facts(kind, 'NEW.')} WHERE {condition}
                    ON CONFLICT(kind,job_id,id) DO UPDATE SET facts=excluded.facts;"""
            if op == "UPDATE":
                body = f"""DELETE FROM historical_refs WHERE kind='{kind}' AND job_id=OLD.job_id
                    AND id={identity('OLD.')} AND (OLD.job_id!=NEW.job_id OR {identity('OLD.')}!={identity('NEW.')} OR NOT {condition});""" + body
            c.execute(f"CREATE TRIGGER {name} AFTER {op} ON {table} BEGIN {body} END")
    c.execute("INSERT OR IGNORE INTO projection_meta VALUES('history_version','1')")


def project_file(c, kind, value):
    value = to_jsonable(value)
    if kind == "observation":
        key = json.dumps([value['attempt_id'], value['observation_id']], separators=(',', ':'))
    else:
        key = value['attempt_id' if kind == 'attempt' else 'id']
    kinds = [kind]
    if kind == "observation" and (value.get("returncode") is not None or value.get("timed_out") is not None):
        kinds.append("outcome")
    for item_kind in kinds:
        c.execute("""INSERT INTO historical_refs(kind,job_id,id,facts) VALUES(?,?,?,?)
            ON CONFLICT(kind,job_id,id) DO UPDATE SET facts=excluded.facts""",
            (item_kind, value['job_id'], key, json.dumps({f: value.get(f) for f in FIELDS[item_kind]})))


def page(store, kind, job_ref, *, cursor=None, limit=100, max_bytes=MAX_BYTES,
         max_scan=MAX_SCAN):
    """Bounded snapshot membership; run facts may reflect later run updates.

    Counts include captured records at the first page's high-water mark. Deletion
    expires cursors in the selected job/kind; inserts do not. Observation identity
    uses one exact attempt-metadata lookup per row, never source-table reads.
    """
    from puppetmaster.projections import connection
    if kind not in FIELDS:
        raise ValueError("invalid historical kind")
    for name, value, low, high in (("limit", limit, 1, MAX_PAGE),
                                   ("max_bytes", max_bytes, 1024, MAX_BYTES),
                                   ("max_scan", max_scan, 1, MAX_SCAN)):
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be between {low} and {high}")
    if cursor is not None:
        CursorCodec.inspect(cursor)
    try:
        with connection(store, metadata_only=True) as c:
            if not c.in_transaction:
                c.execute("BEGIN")
            incarnation = scope_identity(store, job_ref, c)
            validate(store, job_ref, c)
            scope = immutable_digest([incarnation, job_ref.as_dict(), kind])
            from puppetmaster.metadata_snapshot import meta_scalar, integer_scalar
            secret = meta_scalar(c, 'secret')
            epoch = meta_scalar(c, 'epoch')
            retention = integer_scalar(c, "SELECT epoch AS value FROM historical_epochs WHERE kind=? AND job_id=?", (kind,job_ref.job_id))
            codec = CursorCodec(bytes.fromhex(secret))
            value = codec.decode(cursor, scope) if cursor else {}
            if cursor and any(type(value.get(key)) is not int or not 0 <= value[key] <= 9223372036854775807
                              for key in ('last', 'high', 'count', 'retention')):
                raise ValueError('invalid historical cursor')
            if value.get('epoch', epoch) != epoch or value.get('retention', retention) != retention:
                return HistoricalPage(outcome="cursor_expired")
            if not c.execute("SELECT 1 FROM projection_meta WHERE key='history_version'").fetchone():
                return HistoricalPage(next_cursor=cursor)
            if c.execute("SELECT 1 FROM projection_pending LIMIT 1").fetchone():
                return HistoricalPage(next_cursor=cursor)
            if not c.execute("SELECT 1 FROM projection_current WHERE kind='job' AND job_id=?", (job_ref.job_id,)).fetchone():
                return HistoricalPage(next_cursor=cursor)
            current_high = integer_scalar(c, "SELECT seq AS value FROM sqlite_sequence WHERE name='historical_refs'")
            high = value.get('high')
            if high is not None and high > current_high:
                return HistoricalPage(outcome="cursor_expired")
            count = value.get('count')
            if high is None:
                high = current_high
                count = integer_scalar(c, "SELECT count AS value FROM historical_counts WHERE kind=? AND job_id=?", (kind,job_ref.job_id))
            last = value.get('last', 0)
            # CASE prevents even a malformed oversized scalar projection being
            # transferred to Python. The unavailable response asks for repair.
            rows = c.execute("""SELECT sequence, CASE WHEN typeof(facts)='text' AND length(CAST(facts AS BLOB))<=? THEN facts END AS facts
                FROM historical_refs WHERE kind=? AND job_id=? AND sequence>? AND sequence<=?
                ORDER BY sequence LIMIT ?""", (min(max_bytes,4096), kind, job_ref.job_id, last, high, min(limit+1,max_scan))).fetchall()
            items = []
            consumed = 0
            token = cursor
            for row in rows:
                if len(items) == limit:
                    break
                if row['facts'] is None:
                    return HistoricalPage(outcome="unavailable", next_cursor=cursor, scanned=len(rows))
                try:
                    raw = metadata_json_loads(row['facts'])
                    if not isinstance(raw, dict):
                        raise ValueError('invalid historical facts')
                    facts = {field: raw.get(field) for field in FIELDS[kind]}
                    if any(isinstance(v, (dict, list)) for v in facts.values()):
                        raise ValueError('invalid historical scalar')
                except (ValueError, TypeError):
                    return HistoricalPage(next_cursor=cursor, scanned=len(rows))
                facts['job_id'] = job_ref.job_id
                try:
                    from puppetmaster.attempts import ExecutionAttempt, UsageObservation
                    if kind == 'attempt':
                        ExecutionAttempt(**facts)
                    elif kind in {'observation', 'outcome'}:
                        UsageObservation(**facts)
                    elif any(not isinstance(v, str) or not v for k, v in facts.items()
                             if k != 'completed_at') or (facts['completed_at'] is not None
                                                         and not isinstance(facts['completed_at'], str)):
                        raise ValueError('invalid run facts')
                except (ValueError, TypeError):
                    return HistoricalPage(next_cursor=cursor, scanned=len(rows))
                if kind in {'observation', 'outcome'}:
                    # The unique (kind, job_id, id) index resolves one attempt,
                    # independent of the consumer's loaded attempt page.
                    attempt = c.execute("""SELECT CASE WHEN typeof(facts)='text' AND length(CAST(facts AS BLOB))<=4096 THEN facts END
                        FROM historical_refs WHERE kind='attempt' AND job_id=? AND id=?""",
                        (job_ref.job_id, facts.get('attempt_id'))).fetchone()
                    try:
                        resolved = metadata_json_loads(attempt[0]) if attempt and attempt[0] is not None else {}
                        if not isinstance(resolved, dict):
                            resolved = {}
                    except (ValueError, TypeError):
                        return HistoricalPage(next_cursor=cursor, scanned=len(rows))
                    facts['task_id'] = resolved.get('task_id')
                    facts['run_id'] = resolved.get('run_id')
                    available = (resolved.get('attempt_id') == facts.get('attempt_id')
                                 and isinstance(facts['task_id'], str) and bool(facts['task_id'])
                                 and isinstance(facts['run_id'], str) and bool(facts['run_id']))
                    if not available:
                        facts['task_id'] = facts['run_id'] = None
                    facts['identity_state'] = 'available' if available else 'unavailable'
                item = HistoricalRef(job_ref if job_ref.version == 1 else make_ref(store.root, job_ref.job_id, incarnation), kind,
                                     row['sequence'], facts)
                candidate_token = codec.encode(dict(v=1,scope=scope,epoch=epoch,retention=retention,high=high,count=count,last=row['sequence']))
                candidate = HistoricalPage(tuple(items+[item]), "partial", candidate_token, len(rows), count, "partial")
                if len(json.dumps(to_jsonable(candidate), ensure_ascii=True).encode()) > max_bytes:
                    break
                items.append(item)
                token = candidate_token
                consumed += 1
            if rows and not consumed:
                return HistoricalPage(scanned=len(rows), next_cursor=cursor)
            more = consumed < len(rows) or len(rows) == min(limit+1,max_scan)
            return HistoricalPage(tuple(items), "partial" if more else "complete", token if more else None,
                                  len(rows), count, "partial" if more else "captured" if count else "unknown")
    except sqlite3.OperationalError as exc:
        if any(message in str(exc) for message in ('locked', 'busy', 'no such table', 'unable to open')):
            return HistoricalPage(next_cursor=cursor)
        raise


@dataclass(frozen=True)
class HistoricalCounts:
    captured_attempts: Optional[int] = None
    captured_runs: Optional[int] = None
    captured_process_outcomes: Optional[int] = None
    captured_observations: Optional[int] = None
    outcome: Literal["available", "unavailable"] = "unavailable"
    coverage: Literal["captured", "partial", "unknown"] = "unknown"
    complete_invocation_history: bool = False


def counts(store, job_ref):
    """Four primary-key lookups; zero counts never assert zero consumption."""
    from puppetmaster.projections import connection
    try:
        with connection(store, metadata_only=True) as c:
            if not c.in_transaction:
                c.execute("BEGIN")
            validate(store, job_ref, c)
            if (not c.execute("SELECT 1 FROM projection_meta WHERE key='history_version'").fetchone()
                    or c.execute("SELECT 1 FROM projection_pending LIMIT 1").fetchone()
                    or not c.execute("SELECT 1 FROM projection_current WHERE kind='job' AND job_id=?", (job_ref.job_id,)).fetchone()):
                return HistoricalCounts()
            values = []
            for kind in ("attempt", "run", "outcome", "observation"):
                from puppetmaster.metadata_snapshot import integer_scalar
                values.append(integer_scalar(c, "SELECT count AS value FROM historical_counts WHERE kind=? AND job_id=?", (kind,job_ref.job_id)))
            return HistoricalCounts(*values, outcome="available", coverage="captured" if any(values) else "unknown")
    except sqlite3.OperationalError as exc:
        if any(message in str(exc) for message in ('locked', 'busy', 'no such table', 'unable to open')):
            return HistoricalCounts()
        raise
