"""Frozen selected-result facts, independent of captured attempt consumption.

Only the terminal writer inspects selected usage. Public reads touch a single
bounded materialization and job revision in the identity-validated snapshot.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import sqlite3
from typing import Literal, Optional, Union

from puppetmaster.models import JobRef

MAX_INTEGER = 2**53 - 1
MAX_USD = 10**12
MAX_PAYLOAD_BYTES = 4096
Number = Union[int, float]
MetricState = Literal['unknown', 'partial', 'measured', 'estimated']
Reason = Literal['no_terminal_receipt', 'legacy_provenance_unknown', 'projection_missing',
                 'projection_pending', 'selection_changed', 'metadata_invalid',
                 'numeric_limit', 'read_snapshot_unavailable']


@dataclass(frozen=True)
class SelectedMetric:
    total: Optional[Number]
    state: MetricState
    known_selected: Optional[int]
    unknown_selected: Optional[int]
    estimated_selected: Optional[int]
    conflicting_selected: Optional[int]


@dataclass(frozen=True)
class SelectedTotals:
    tokens_in: SelectedMetric
    tokens_out: SelectedMetric
    cache_read_tokens: SelectedMetric
    cache_write_tokens: SelectedMetric
    api_cost_usd: SelectedMetric
    plan_marginal_cost_usd: SelectedMetric
    api_equivalent_cost_usd: SelectedMetric


@dataclass(frozen=True)
class SelectedEconomics:
    job_ref: JobRef
    outcome: Literal['available', 'unavailable'] = 'unavailable'
    summary_revision: Optional[int] = None
    receipt_digest: Optional[str] = None
    source: Literal['terminal_receipt', 'unavailable'] = 'unavailable'
    coverage: Literal['selected_receipt', 'unknown'] = 'unknown'
    selected_count: Optional[int] = None
    totals: Optional[SelectedTotals] = None
    reason: Optional[Reason] = None
    retry_after_ms: Optional[int] = None


def encoded(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(',', ':'))


def valid_number(value, money=False):
    return (type(value) in ((int, float) if money else (int,))
            and 0 <= value <= (MAX_USD if money else MAX_INTEGER)
            and (type(value) is int or math.isfinite(value)))


def unknown_payload(reason='no_terminal_receipt'):
    return dict(version=1, receipt_digest=None, selected_count=None, totals=None, reason=reason)


def validate_payload(value):
    if not isinstance(value, dict) or len(value) != 5 or set(value) != {'version', 'receipt_digest', 'selected_count', 'totals', 'reason'}:
        raise ValueError('invalid projection fields')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('unsupported projection version')
    digest = value['receipt_digest']
    if digest is not None and (type(digest) is not str or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
        raise ValueError('invalid receipt digest')
    count, totals, reason = value['selected_count'], value['totals'], value['reason']
    if totals is None:
        if (count is not None or reason not in ('no_terminal_receipt', 'legacy_provenance_unknown', 'numeric_limit', 'metadata_invalid')
                or (reason in ('no_terminal_receipt', 'legacy_provenance_unknown') and digest is not None)):
            raise ValueError('invalid unavailable projection')
        return None
    if reason is not None or digest is None or not valid_number(count) or count == 0:
        raise ValueError('invalid selected cardinality')
    if not isinstance(totals, dict) or len(totals) != 7 or set(totals) != set(SelectedTotals.__dataclass_fields__):
        raise ValueError('invalid totals')
    result = {}
    for field, metric in totals.items():
        if not isinstance(metric, dict) or len(metric) != 6 or set(metric) != set(SelectedMetric.__dataclass_fields__):
            raise ValueError('invalid metric')
        total, state = metric['total'], metric['state']
        known, unknown, estimated, conflicts = (metric[k] for k in
            ('known_selected', 'unknown_selected', 'estimated_selected', 'conflicting_selected'))
        if not all(valid_number(n) for n in (known, unknown, estimated, conflicts)):
            raise ValueError('invalid metric counts')
        if known + unknown != count or estimated > known or conflicts > unknown:
            raise ValueError('inconsistent metric counts')
        expected = 'unknown' if not known else 'partial' if unknown else 'estimated' if estimated else 'measured'
        if state != expected or ((total is None) != (bool(unknown) or not known)):
            raise ValueError('inconsistent metric state')
        if total is not None and not valid_number(total, field.endswith('_usd')):
            raise ValueError('numeric_limit')
        if field == 'api_equivalent_cost_usd' and known != estimated:
            raise ValueError('API-equivalent must be estimated')
        result[field] = SelectedMetric(**metric)
    return SelectedTotals(**result)


def project(receipt, legacy=False):
    if not isinstance(receipt, dict):
        return encoded(unknown_payload('legacy_provenance_unknown' if legacy else 'no_terminal_receipt'))
    member = receipt.get('bounded_economics')
    if not isinstance(member, dict):
        return encoded(unknown_payload('legacy_provenance_unknown'))
    # Validate shape before encoding so corrupt nested or huge source data is
    # never serialized merely to find out it exceeds the projection budget.
    try:
        validate_payload(member)
        raw = encoded(member)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError('oversized projection')
        return raw
    except (ValueError, TypeError, OverflowError):
        return encoded(unknown_payload('metadata_invalid'))


def freeze(receipt, artifacts):
    """Called once with the terminal writer's already-selected source inputs."""
    from puppetmaster.cost import _usage_records, execution_billing_artifacts
    records = _usage_records(artifacts)
    routes = execution_billing_artifacts(artifacts)
    count = len(records)
    digest = hashlib.sha256(encoded({k: v for k, v in receipt.items() if k != 'bounded_economics'}).encode('ascii')).hexdigest()
    if not count:
        return unknown_payload()
    buckets = {field: [] for field in SelectedTotals.__dataclass_fields__}
    priced_rows = {row["task_id"]: row for row in receipt.get("actual_cost", {}).get("tasks", [])}
    overflow = not valid_number(count)
    for task_id, record in records.items():
        # Versioned presence facts avoid legacy SDK default-zero ambiguity.
        facts = record.get('selected_facts') or {}
        state = 'estimated' if record['tokens_estimated'] else 'measured'
        for field in ('tokens_in', 'tokens_out', 'cache_read_tokens', 'cache_write_tokens'):
            value = facts.get(field)
            if value is not None and not valid_number(value):
                overflow = True
            buckets[field].append((value, state))
        cost = facts.get('real_cost_usd')
        if cost is not None and not valid_number(cost, True):
            overflow = True
        route = routes.get(task_id)
        billing = (route.payload or {}).get('billing') if route is not None else None
        priced = priced_rows.get(task_id)
        if billing not in ('api', 'plan', 'unknown'):
            billing = priced.get('billing') if priced else None
        buckets['api_cost_usd'].append((cost if billing == 'api' else None, 'measured'))
        buckets['plan_marginal_cost_usd'].append((0 if billing == 'plan' else None, 'estimated'))
        # Registry valuation is always an estimate, never an API charge.
        equivalent = (priced.get('api_equivalent_cost_usd',
                                 priced.get('marginal_cost_usd') if billing == 'api' else None) if priced
                      and billing != 'plan' and (cost is None or billing != 'api')
                      and facts.get('tokens_in') is not None and facts.get('tokens_out') is not None else None)
        buckets['api_equivalent_cost_usd'].append((equivalent, 'estimated'))
    totals = {}
    for field, rows in buckets.items():
        known = [(n, state) for n, state in rows if n is not None]
        if any(not valid_number(n, field.endswith('_usd')) for n, _ in known):
            overflow = True
            continue
        unknown = count - len(known)
        estimated = sum(state == 'estimated' for _, state in known)
        total = sum(n for n, _ in known) if known and not unknown else None
        if total is not None and not valid_number(total, field.endswith('_usd')):
            overflow = True
        state = 'unknown' if not known else 'partial' if unknown else 'estimated' if estimated else 'measured'
        totals[field] = asdict(SelectedMetric(total, state, len(known), unknown, estimated, 0))
    if overflow:
        return dict(unknown_payload('numeric_limit'), receipt_digest=digest)
    result = dict(version=1, receipt_digest=digest, selected_count=count, totals=totals, reason=None)
    validate_payload(result)
    if len(encoded(result)) > MAX_PAYLOAD_BYTES:
        return dict(unknown_payload('metadata_invalid'), receipt_digest=digest)
    return result


def create_schema(c):
    c.execute("""CREATE TABLE IF NOT EXISTS selected_economics_current(
        job_id TEXT PRIMARY KEY, receipt_digest TEXT,
        payload TEXT NOT NULL CHECK(length(CAST(payload AS BLOB))<=4096))""")


def source_sql(prefix):
    receipt = f"json_extract({prefix}.data,'$.cost_receipt')"
    member = f"json_extract({prefix}.data,'$.cost_receipt.bounded_economics')"
    matches = f"json_extract({prefix}.data,'$.cost_receipt.job_id')={prefix}.id AND json_extract({prefix}.data,'$.cost_receipt.pricing_source')='terminal_receipt'"
    raw = f"CASE WHEN NOT COALESCE(({matches}),0) THEN '{encoded(unknown_payload('metadata_invalid'))}' WHEN json_type({prefix}.data,'$.cost_receipt.bounded_economics')='object' AND length(CAST({member} AS BLOB))<=4096 THEN {member} ELSE '{project({}, legacy=True)}' END"
    active = f"COALESCE(json_extract({prefix}.data,'$.status') IN ('complete','failed','cancelled'),0)=0"
    return f"""
        DELETE FROM selected_economics_current WHERE job_id={prefix}.id
            AND ({receipt} IS NULL OR {active} OR json_extract(payload,'$.reason')='no_terminal_receipt');
        INSERT OR IGNORE INTO selected_economics_current(job_id,receipt_digest,payload)
            VALUES({prefix}.id,CASE WHEN {receipt} IS NOT NULL AND NOT ({active})
                THEN CASE WHEN typeof(json_extract({member},'$.receipt_digest'))='text' AND length(CAST(json_extract({member},'$.receipt_digest') AS BLOB))=64 THEN json_extract({member},'$.receipt_digest') END END,
                CASE WHEN {receipt} IS NULL OR {active} THEN '{project(None)}' ELSE {raw} END);
    """


def project_file(c, job_id, value):
    receipt = value.get('cost_receipt')
    if value.get('status') not in ('complete', 'failed', 'cancelled'):
        receipt = None
    c.execute("DELETE FROM selected_economics_current WHERE job_id=? AND (? OR json_extract(payload,'$.reason')='no_terminal_receipt')",
              (job_id, receipt is None))
    payload = (encoded(unknown_payload('metadata_invalid')) if receipt is not None and
               (not isinstance(receipt, dict) or receipt.get('job_id') != job_id or receipt.get('pricing_source') != 'terminal_receipt')
               else project(receipt))
    digest = json.loads(payload)['receipt_digest']
    c.execute('INSERT OR IGNORE INTO selected_economics_current(job_id,receipt_digest,payload) VALUES(?,?,?)',
              (job_id, digest, payload))


def read(store, job_ref, expected_summary_revision=None):
    from puppetmaster.identity import validate, StoreIdentityError
    from puppetmaster.projections import connection
    from puppetmaster.bounded_json import loads
    if not isinstance(job_ref, JobRef) or job_ref.version != 2:
        raise StoreIdentityError('selected economics requires v2 JobRef; rebind')
    if expected_summary_revision is not None and not valid_number(expected_summary_revision):
        raise ValueError('invalid expected_summary_revision')
    from puppetmaster.readonly import selection
    selected = selection(store)
    revision = None
    try:
        with connection(store, metadata_only=True) as c:
            if not c.in_transaction:
                c.execute('BEGIN')
            validate(store, job_ref, c, strict=True)
            row = c.execute("SELECT CASE WHEN typeof(revision)='integer' AND revision BETWEEN 0 AND ? THEN revision END FROM projection_current WHERE kind='job' AND job_id=? AND id=?",
                            (MAX_INTEGER, job_ref.job_id, job_ref.job_id)).fetchone()
            if row is None:
                raise KeyError(job_ref.job_id)
            revision = row[0]
            if revision is None:
                return SelectedEconomics(job_ref, reason='numeric_limit')
            if expected_summary_revision is not None and revision != expected_summary_revision:
                return SelectedEconomics(job_ref, summary_revision=revision, reason='selection_changed')
            if not c.execute("SELECT 1 FROM projection_meta WHERE key='display_economics_version' AND value='1'").fetchone():
                return SelectedEconomics(job_ref, summary_revision=revision, reason='projection_missing')
            if c.execute('SELECT 1 FROM projection_pending LIMIT 1').fetchone():
                return SelectedEconomics(job_ref, summary_revision=revision, reason='projection_pending')
            row = c.execute("SELECT CASE WHEN typeof(payload)='text' AND length(CAST(payload AS BLOB))<=4096 THEN payload END FROM selected_economics_current WHERE job_id=?", (job_ref.job_id,)).fetchone()
            if row is None:
                return SelectedEconomics(job_ref, summary_revision=revision, reason='projection_missing')
            try:
                value = loads(row[0])
                totals = validate_payload(value)
            except (ValueError, TypeError, OverflowError):
                return SelectedEconomics(job_ref, summary_revision=revision, reason='metadata_invalid')
            return SelectedEconomics(job_ref, 'available' if totals is not None else 'unavailable',
                revision, value['receipt_digest'], 'terminal_receipt' if value['receipt_digest'] else 'unavailable',
                'selected_receipt' if totals is not None else 'unknown', value['selected_count'], totals, value['reason'])
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc) or 'no such column' in str(exc):
            return SelectedEconomics(job_ref, summary_revision=revision, reason='projection_missing')
        if any(s in str(exc) for s in ('locked', 'busy', 'unable to open')):
            return SelectedEconomics(job_ref, reason='read_snapshot_unavailable', retry_after_ms=100)
        raise
    finally:
        if selection(store) != selected:
            raise StoreIdentityError('store replaced during selected economics lookup; explicitly rebind')


def check_receipt_replacement(old, new):
    """Writer-only CAS: a frozen receipt cannot be replaced while terminal."""
    from puppetmaster.contracts import ContractConflict
    if (old.get('cost_receipt') is not None and new.get('cost_receipt') is not None
            and old.get('status') in ('complete', 'failed', 'cancelled')
            and new.get('status') in ('complete', 'failed', 'cancelled')
            and old['cost_receipt'] != new['cost_receipt']):
        raise ContractConflict('conflicting frozen terminal receipt; reopen before refreezing')
