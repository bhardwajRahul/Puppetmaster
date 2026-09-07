"""Writer-only goal and lifecycle projection. No body reads on public lookups."""
from __future__ import annotations

MAX_GOAL_PREVIEW_BYTES = 512
FIELDS = ('goal_preview', 'goal_preview_truncated', 'delivery', 'quality')
TYPES = ('TEXT', 'INTEGER CHECK(goal_preview_truncated IN (0,1))',
         "TEXT NOT NULL DEFAULT 'unavailable'", "TEXT NOT NULL DEFAULT 'unavailable'")


def preview(goal):
    if not isinstance(goal, str):
        return None, None
    candidate = goal[:513]
    try:
        candidate.encode('utf-8', errors='strict')
    except UnicodeError:
        return None, None
    used = 0
    for index, char in enumerate(candidate):
        used += len(char.encode('utf-8'))
        if used > MAX_GOAL_PREVIEW_BYTES:
            return candidate[:index], True
    return candidate, len(goal) > len(candidate)


def display(goal, status):
    delivery = ('pending' if status in ('queued', 'running', 'stitching') else
                'blocked' if status in ('failed', 'stalled', 'cancelled') else
                'unverified' if status == 'complete' else 'unavailable')
    return (*preview(goal), delivery, 'unavailable' if delivery == 'unavailable' else 'unverified')


def add_columns(c, table):
    existing = {row[1] for row in c.execute('PRAGMA table_info(' + table + ')')}
    for field, sql_type in zip(FIELDS, TYPES):
        if field not in existing:
            c.execute('ALTER TABLE ' + table + ' ADD COLUMN ' + field + ' ' + sql_type)


def touch_sql(job):
    return f"""
        INSERT INTO projection_changes(kind,job_id,id,status,stamp,task_count,artifact_count)
            SELECT kind,job_id,id,status,'known',task_count,artifact_count
            FROM projection_current WHERE kind='job' AND job_id={job};
        UPDATE projection_current SET revision=last_insert_rowid(),stamp='known'
            WHERE kind='job' AND job_id={job};
    """


def touch(c, job_id):
    cur = c.execute("""INSERT INTO projection_changes(kind,job_id,id,status,stamp,task_count,artifact_count)
        SELECT kind,job_id,id,status,'known',task_count,artifact_count
        FROM projection_current WHERE kind='job' AND job_id=?""", (job_id,))
    c.execute("UPDATE projection_current SET revision=?,stamp='known' WHERE kind='job' AND job_id=?",
              (cur.lastrowid, job_id))


def install(c):
    for table in ('projection_current', 'projection_changes'):
        add_columns(c, table)
    # Every projection replacement (including count-only and history touches)
    # copies the same tuple into its journal entry. Tombstones copy OLD first.
    for op in ('INSERT', 'UPDATE', 'DELETE'):
        c.execute('DROP TRIGGER IF EXISTS display_copy_' + op)
        prefix = 'OLD' if op == 'DELETE' else 'NEW'
        revision = ("(SELECT MAX(revision) FROM projection_changes WHERE kind=OLD.kind AND job_id=OLD.job_id AND id=OLD.id)"
                    if op == 'DELETE' else 'NEW.revision')
        c.execute(f"""CREATE TRIGGER display_copy_{op} AFTER {op} ON projection_current BEGIN
            UPDATE projection_changes SET {','.join(f+'='+prefix+'.'+f for f in FIELDS)}
            WHERE revision={revision}; END""")


def sql_fields(data):
    """Bound candidate transfer/encoding to 513 scalars; SQLite does writer work.

    A recursive scalar walks at most 513 characters, preserving normalization.
    JSON's escaped lone surrogates are rejected by the Unicode range check.
    """
    goal = f"json_extract({data},'$.goal')"
    # Scan a bounded UTF-8 blob, rather than SQLite text length/substr (which
    # stop at NUL). The LIMIT prevents flattening and repeated body extraction.
    candidate = f"substr(CAST({goal} AS BLOB),1,2052)"
    first = "hex(substr(g,bytes+1,1))"
    width = f"CASE WHEN {first}<'80' THEN 1 WHEN {first}<'E0' THEN 2 WHEN {first}<'F0' THEN 3 ELSE 4 END"
    projection = f"""(WITH RECURSIVE input(g,full_length) AS (
        SELECT {candidate},length(CAST({goal} AS BLOB)) LIMIT 1),
        chars(n,bytes,valid) AS (VALUES(0,0,1) UNION ALL
        SELECT n+1,bytes+({width}), valid AND NOT
            ({first}='ED' AND hex(substr(g,bytes+2,1)) BETWEEN 'A0' AND 'BF')
        FROM chars,input WHERE n<513 AND bytes<length(g)),
        result(size,valid) AS (SELECT MAX(CASE WHEN bytes<=512 THEN bytes END),MIN(valid) FROM chars)
        SELECT CASE WHEN json_type({data},'$.goal')='text' AND valid
            THEN json_array(COALESCE(CAST(substr(g,1,size) AS TEXT),''),full_length>size)
            ELSE json_array(NULL,NULL) END FROM input,result)"""
    p = f"json_extract({projection},'$[0]')"
    truncated = f"json_extract({projection},'$[1]')"
    status = f"json_extract({data},'$.status')"
    delivery = f"""CASE WHEN {status} IN ('queued','running','stitching') THEN 'pending'
        WHEN {status} IN ('failed','stalled','cancelled') THEN 'blocked'
        WHEN {status}='complete' THEN 'unverified' ELSE 'unavailable' END"""
    quality = f"CASE WHEN {status} IN ('queued','running','stitching','failed','stalled','cancelled','complete') THEN 'unverified' ELSE 'unavailable' END"
    return p, truncated, delivery, quality


def project(c, job_id, goal, status):
    c.execute('UPDATE projection_current SET ' + ','.join(f+'=?' for f in FIELDS)
              + " WHERE kind='job' AND job_id=?", (*display(goal, status), job_id))


def install_fact_touches(c):
    for table in ('historical_refs', 'completion_receipts'):
        for op in ('INSERT', 'UPDATE', 'DELETE'):
            name = 'summary_' + table + '_' + op
            c.execute('DROP TRIGGER IF EXISTS ' + name)
            job = 'OLD.job_id' if op == 'DELETE' else 'NEW.job_id'
            condition = ''
            if op == 'UPDATE':
                condition = (' WHEN OLD.facts IS NOT NEW.facts' if table == 'historical_refs' else
                             ' WHEN OLD.intent_digest IS NOT NEW.intent_digest OR OLD.outcome IS NOT NEW.outcome')
            c.execute(f'CREATE TRIGGER {name} AFTER {op} ON {table}{condition} BEGIN {touch_sql(job)} END')
