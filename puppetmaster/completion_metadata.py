"""Receipt materialization. Intent bodies are read only by writers and migration."""
from __future__ import annotations

import sqlite3

from puppetmaster.contracts import CompletionReceipt
from puppetmaster.identity import validate

OUTCOMES = ('pending_publication', 'published', 'stale_lease', 'invalidated', 'legacy_unknown')


def create_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS completion_receipts(
        job_id TEXT NOT NULL, run_id TEXT NOT NULL, intent_digest TEXT,
        outcome TEXT NOT NULL, PRIMARY KEY(job_id,run_id))""")


def install_source_triggers(c):
    create_schema(c)
    # SQLite evaluates these expressions at publication/migration, never at read.
    digest = "json_extract(data,'$.intent_digest')"
    outcome = "COALESCE(json_extract(data,'$.publication'),'legacy_unknown')"
    valid_digest = f"({digest} IS NULL OR (typeof({digest})='text' AND length(CAST({digest} AS BLOB))=64))"
    allowed = ','.join(repr(v) for v in OUTCOMES)
    fields = f"CASE WHEN {valid_digest} THEN {digest} END, CASE WHEN {valid_digest} AND {outcome} IN ({allowed}) THEN {outcome} ELSE 'unavailable' END"
    if not c.execute("SELECT 1 FROM projection_meta WHERE key='completion_version'").fetchone():
        c.execute(f"INSERT OR REPLACE INTO completion_receipts SELECT job_id,id,{fields} FROM completions")
    for op in ('INSERT', 'UPDATE', 'DELETE'):
        c.execute(f"DROP TRIGGER IF EXISTS completion_receipt_{op}")
        body = "DELETE FROM completion_receipts WHERE job_id=OLD.job_id AND run_id=OLD.id;" if op != 'INSERT' else ''
        if op != 'DELETE':
            body += f"INSERT INTO completion_receipts SELECT NEW.job_id,NEW.id,{fields.replace('data,', 'NEW.data,')} ON CONFLICT(job_id,run_id) DO UPDATE SET intent_digest=excluded.intent_digest,outcome=excluded.outcome;"
        c.execute(f"CREATE TRIGGER completion_receipt_{op} AFTER {op} ON completions BEGIN {body} END")
    c.execute("INSERT OR REPLACE INTO projection_meta VALUES('completion_version','1')")


def project_file(c, value):
    digest = value.get('intent_digest')
    outcome = value.get('publication', 'legacy_unknown')
    if (digest is not None and (type(digest) is not str or len(digest) != 64 or len(digest.encode()) != 64)) or outcome not in OUTCOMES:
        digest, outcome = None, 'unavailable'
    c.execute("INSERT INTO completion_receipts(job_id,run_id,intent_digest,outcome) VALUES(?,?,?,?) "
              "ON CONFLICT(job_id,run_id) DO UPDATE SET intent_digest=excluded.intent_digest,outcome=excluded.outcome",
              (value['run']['job_id'], value['run']['id'], digest, outcome))


def read(store, job_ref, run_id):
    from puppetmaster.projections import connection
    if type(run_id) is not str or not run_id or len(run_id) > 256 or len(run_id.encode()) > 256 or store._safe_key(run_id) != run_id:
        raise ValueError('invalid completion run id')
    try:
        with connection(store, metadata_only=True) as c:
            validate(store, job_ref, c)
            if not c.execute("SELECT 1 FROM projection_current WHERE kind='job' AND job_id=?", (job_ref.job_id,)).fetchone():
                raise KeyError(job_ref.job_id)
            if (not c.execute("SELECT 1 FROM projection_meta WHERE key='completion_version'").fetchone()
                    or c.execute("SELECT 1 FROM projection_pending LIMIT 1").fetchone()):
                return CompletionReceipt(job_ref, run_id, None, 'unavailable')
            row = c.execute("""SELECT CASE WHEN typeof(intent_digest)='text' AND length(CAST(intent_digest AS BLOB))=64 THEN intent_digest END,
                CASE WHEN (intent_digest IS NULL OR (typeof(intent_digest)='text' AND length(CAST(intent_digest AS BLOB))=64))
                    AND outcome IN ('pending_publication','published','stale_lease','invalidated','legacy_unknown')
                    THEN outcome ELSE 'unavailable' END
                FROM completion_receipts WHERE job_id=? AND run_id=?""", (job_ref.job_id, run_id)).fetchone()
            return CompletionReceipt(job_ref, run_id, row[0] if row else None, row[1] if row else 'legacy_unknown')
    except sqlite3.OperationalError as exc:
        if any(message in str(exc) for message in ('locked', 'busy', 'no such table', 'unable to open')):
            return CompletionReceipt(job_ref, run_id, None, 'unavailable')
        raise
