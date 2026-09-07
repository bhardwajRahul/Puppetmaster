"""Versioned scalar membership for bounded, read-only keyset traversal.

The entity index is append-only until explicit retention. Each source transaction
publishes its final projection at the journal high-water mark. Readers enumerate
at most max_scan entity keys and perform one indexed version lookup per key.
"""
from __future__ import annotations

# No source bodies, labels, task payloads, or inferred economics belong here.
COLUMNS = ('kind', 'job_id', 'id', 'status', 'sha256', 'revision', 'stamp',
           'task_count', 'artifact_count', 'binding', 'task_id', 'artifact_type', 'scope',
           'goal_preview', 'goal_preview_truncated', 'delivery', 'quality')
SCALAR_BYTES = 4096


def bounded_columns(columns, *, prefix='', max_bytes=SCALAR_BYTES, integer_columns=(), display_supported=True):
    """Guard before SQLite transfers text to Python; identities are never clipped.

    Guard numeric-affinity columns too: legacy SQLite rows may contain text.
    """
    expressions = []
    bad = []
    for column in columns:
        if not display_supported and column in ('goal_preview', 'goal_preview_truncated', 'delivery', 'quality'):
            expressions.append(("'unavailable'" if column in ('delivery', 'quality') else 'NULL') + ' AS ' + column)
            continue
        name = prefix + column
        if column in integer_columns:
            # INTEGER PRIMARY KEY is always a bounded int; preserving its
            # expression lets compound change queries stream the order index.
            expressions.append(f'{name} AS {column}')
            continue
        numeric = column in {'revision', 'task_count', 'artifact_count', 'born', 'deleted', 'at_revision'}
        nullable = column in {'task_count', 'artifact_count'}
        oversized = (f"({name} IS NOT NULL AND (typeof({name})!='integer' OR {name}<0))" if numeric and nullable
                     else f"(typeof({name})!='integer' OR {name}<0)" if numeric
                     else f"({name} IS NOT NULL AND (typeof({name})!='text' OR length(CAST({name} AS BLOB))>{max_bytes}))")
        if column == 'goal_preview_truncated':
            oversized = f"({name} IS NOT NULL AND (typeof({name})!='integer' OR {name} NOT IN (0,1)))"
        if column == 'goal_preview':
            oversized = f"({name} IS NOT NULL AND (typeof({name})!='text' OR length(CAST({name} AS BLOB))>512))"
        if column == 'deleted':
            oversized = f"(typeof({name})!='integer' OR {name} NOT IN (0,1))"
        expressions.append(f'CASE WHEN {oversized} THEN NULL ELSE {name} END AS {column}')
        bad.append(f'COALESCE({oversized},0)')
    expressions.append('(' + ' OR '.join(bad) + ') AS oversized')
    return ','.join(expressions)


def meta_scalar(c, key):
    from puppetmaster.readonly import ReadUnavailable
    bound = 64 if key == 'secret' else 20
    row = c.execute("SELECT CASE WHEN typeof(value)='text' AND length(CAST(value AS BLOB))<=? THEN value END FROM projection_meta WHERE key=?", (bound, key)).fetchone()
    value = row[0] if row else None
    valid = (isinstance(value, str) and
             (len(value) == 64 and all(ch in '0123456789abcdef' for ch in value)
              if key == 'secret' else value.isascii() and value.isdecimal() and int(value) <= 9223372036854775807))
    if not valid:
        raise ReadUnavailable('unable to open metadata: invalid ' + key)
    return value


def integer_scalar(c, sql, args=(), *, default=0):
    """The inner query names its scalar `value`; never transfer affinity text."""
    from puppetmaster.readonly import ReadUnavailable
    row = c.execute("SELECT CASE WHEN typeof(value)='integer' AND value>=0 THEN value END FROM (" + sql + ")", args).fetchone()
    if row is None:
        return default
    if row[0] is None:
        raise ReadUnavailable('unable to open metadata: invalid integer scalar')
    return row[0]


def install(c):
    c.execute("""CREATE TABLE IF NOT EXISTS projection_entities(
        kind TEXT NOT NULL, job_id TEXT NOT NULL, id TEXT NOT NULL, born INTEGER NOT NULL,
        PRIMARY KEY(kind,job_id,id)) WITHOUT ROWID""")
    c.execute("CREATE INDEX IF NOT EXISTS projection_entity_order ON projection_entities(kind,id,job_id)")
    columns = ','.join(COLUMNS)
    c.execute(f"""CREATE TABLE IF NOT EXISTS projection_versions AS
        SELECT {columns}, 0 AS at_revision, 0 AS deleted FROM projection_current WHERE 0""")
    from puppetmaster.job_display import add_columns
    add_columns(c, "projection_versions")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS projection_version_lookup ON projection_versions(kind,job_id,id,at_revision)")
    for op in ('INSERT', 'UPDATE', 'DELETE'):
        name = f'projection_version_{op}'
        c.execute(f'DROP TRIGGER IF EXISTS {name}')
        prefix = 'OLD.' if op == 'DELETE' else 'NEW.'
        clock = '(SELECT COALESCE(MAX(revision),0) FROM projection_changes)'
        values = ','.join(prefix + field for field in COLUMNS)
        c.execute(f"""CREATE TRIGGER {name} AFTER {op} ON projection_current BEGIN
            INSERT INTO projection_entities VALUES({prefix}kind,{prefix}job_id,{prefix}id,{clock}) ON CONFLICT DO NOTHING;
            INSERT INTO projection_versions({columns},at_revision,deleted)
                VALUES({values},{clock},{int(op == 'DELETE')})
                ON CONFLICT(kind,job_id,id,at_revision) DO UPDATE SET
                    {",".join(field+"=excluded."+field for field in COLUMNS if field not in ("kind","job_id","id"))}, deleted=excluded.deleted; END""")
    if not c.execute("SELECT 1 FROM projection_meta WHERE key='snapshot_version'").fetchone():
        c.execute("DELETE FROM projection_versions")
        c.execute("DELETE FROM projection_entities")
        clock = c.execute('SELECT COALESCE(MAX(revision),0) FROM projection_changes').fetchone()[0]
        c.execute('INSERT OR IGNORE INTO projection_entities SELECT kind,job_id,id,? FROM projection_current', (clock,))
        c.execute(f'INSERT OR REPLACE INTO projection_versions({columns},at_revision,deleted) SELECT {columns},?,0 FROM projection_current', (clock,))
        c.execute("INSERT INTO projection_meta VALUES('snapshot_version','1')")
        c.execute("UPDATE projection_meta SET value=CAST(value AS INTEGER)+1 WHERE key='epoch'")
    # Retention is explicit expiry, including manual pruning of either index.
    for table in ('projection_versions', 'projection_entities'):
        c.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_retention AFTER DELETE ON {table} BEGIN
            UPDATE projection_meta SET value=CAST(value AS INTEGER)+1 WHERE key='epoch'; END""")


def high_key(c, kind, job_ref, max_bytes):
    where = 'kind=?'
    args = [kind]
    if job_ref is not None:
        where += ' AND job_id=?'
        args.append(job_ref.job_id)
    row = c.execute('SELECT ' + bounded_columns(('id','job_id'), max_bytes=max_bytes)
                    + f' FROM projection_entities AS entity WHERE {where}'
                    + ' ORDER BY entity.id DESC,entity.job_id DESC LIMIT 1', args).fetchone()
    if row is not None and row['oversized']:
        return None
    return [row['id'], row['job_id']] if row is not None else ['', '']


def rows(c, kind, job_ref, snapshot, last, boundary, count, max_bytes, display_supported=True):
    where = 'kind=? AND (id,job_id)>(?,?) AND (id,job_id)<=(?,?)'
    args = [kind, last[0], last[1], boundary[0], boundary[1]]
    if job_ref is not None:
        where += ' AND job_id=?'
        args.append(job_ref.job_id)
    # Limit candidates before testing birth revision. Otherwise thousands of
    # later insertions can turn a small page into an unbounded SQLite scan.
    keys = c.execute('SELECT ' + bounded_columns(('id','job_id','born'), max_bytes=max_bytes)
                     + f' FROM projection_entities AS entity WHERE {where} ORDER BY entity.id,entity.job_id LIMIT ?', (*args,count)).fetchall()
    result = []
    fields = bounded_columns(COLUMNS + ('deleted',), max_bytes=max_bytes, display_supported=display_supported)
    for key in keys:
        if key['oversized']:
            return None, len(keys)
        if key['born'] > snapshot:
            result.append(dict(id=key['id'], job_id=key['job_id'], deleted=True, oversized=False))
            continue
        row = c.execute(f'''SELECT {fields} FROM projection_versions
            WHERE kind=? AND job_id=? AND id=? AND at_revision<=?
            ORDER BY at_revision DESC LIMIT 1''', (kind,key['job_id'],key['id'],snapshot)).fetchone()
        if row is None:
            return None, len(keys)
        result.append(row)
    return result, len(keys)
