"""Invocation consumption, separate from selected-result usage and pricing.

Observations are overlapping snapshots, never deltas. Equal values reconcile;
different known values conflict rather than implying an ordering or a sum.
No artifact, retry counter, or current model price participates in this report.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable, Literal, Optional, Protocol, Tuple, Union

from puppetmaster.attempts import ExecutionAttempt, UsageObservation

Number = Union[int, float]
ConsumptionStatus = Literal["unknown", "partial", "measured", "estimated"]


class ConsumptionStore(Protocol):
    def list_attempts(self, job_id: str) -> list[ExecutionAttempt]: ...

    def list_usage_observations(self, job_id: str) -> list[UsageObservation]: ...


@dataclass(frozen=True)
class ConsumptionMetric:
    """A total exists only when every recorded invocation has a known value.

    Subtotals include only reconciled values. A zero subtotal with no known
    attempts is an empty sum, not evidence of measured zero consumption.
    """
    total: Optional[Number]
    known_subtotal: Number
    status: ConsumptionStatus
    known_attempts: int
    unknown_attempts: int
    estimated_attempts: int
    conflicting_attempts: int


@dataclass(frozen=True)
class ConsumptionTotals:
    tokens_in: ConsumptionMetric
    tokens_out: ConsumptionMetric
    cache_read_tokens: ConsumptionMetric
    cache_write_tokens: ConsumptionMetric
    api_cost_usd: ConsumptionMetric
    plan_marginal_cost_usd: ConsumptionMetric
    api_equivalent_cost_usd: ConsumptionMetric


@dataclass(frozen=True)
class AttemptConsumption:
    attempt: ExecutionAttempt
    observation_ids: Tuple[str, ...]
    totals: ConsumptionTotals
    process_outcomes: Tuple[UsageObservation, ...] = ()


@dataclass(frozen=True)
class AttemptConsumptionReport:
    job_id: str
    attempt_count: int
    attempts: Tuple[AttemptConsumption, ...]
    totals: ConsumptionTotals

    def to_dict(self) -> dict:
        """Detached, JSON-serializable fields, with unknowns preserved as null."""
        return asdict(self)


def _reconcile(values: Iterable[Tuple[Number, str]]) -> ConsumptionMetric:
    values = tuple(values)
    distinct = {value for value, _ in values}
    if len(distinct) != 1:
        return ConsumptionMetric(None, 0, "unknown", 0, 1, 0, int(len(distinct) > 1))
    value = next(iter(distinct))
    estimated = all(state == "estimated" for _, state in values)
    return ConsumptionMetric(value, value, "estimated" if estimated else "measured",
                             1, 0, int(estimated), 0)


def _attempt_totals(observations: list[UsageObservation]) -> ConsumptionTotals:
    metrics = {}
    for field in ("tokens_in", "tokens_out", "cache_read_tokens", "cache_write_tokens"):
        metrics[field] = _reconcile(
            (getattr(obs, field), obs.usage_state) for obs in observations
            if getattr(obs, field) is not None
        )
    for field, basis in (("api_cost_usd", "api"),
                         ("plan_marginal_cost_usd", "plan_marginal"),
                         ("api_equivalent_cost_usd", "api_equivalent")):
        metrics[field] = _reconcile(
            (obs.cost_usd, obs.cost_state) for obs in observations
            if obs.cost_basis == basis and obs.cost_usd is not None
        )
    return ConsumptionTotals(**metrics)


def _sum_metrics(metrics: Iterable[ConsumptionMetric]) -> ConsumptionMetric:
    metrics = tuple(metrics)
    known = sum(metric.known_attempts for metric in metrics)
    unknown = sum(metric.unknown_attempts for metric in metrics)
    estimated = sum(metric.estimated_attempts for metric in metrics)
    conflicts = sum(metric.conflicting_attempts for metric in metrics)
    subtotal = sum(metric.known_subtotal for metric in metrics)
    status: ConsumptionStatus = (
        "unknown" if not known else "partial" if unknown else
        "estimated" if estimated else "measured"
    )
    return ConsumptionMetric(subtotal if known and not unknown else None,
                             subtotal, status, known, unknown, estimated, conflicts)


def build_attempt_consumption_report(
    store: ConsumptionStore, job_id: str,
) -> AttemptConsumptionReport:
    """Read both ledger backends without changing selected-result economics.

    Results describe captured attempts, not proof of complete telemetry. Reads
    are not snapshot-isolated during live writes. Missing history stays empty
    with unknown totals. Each cost basis requires explicit evidence per attempt;
    absence of a basis never implies a zero charge for that basis.
    """
    attempts = sorted(store.list_attempts(job_id),
                      key=lambda attempt: (attempt.started_at, attempt.attempt_id))
    observations = {}
    for observation in store.list_usage_observations(job_id):
        observations.setdefault(observation.attempt_id, []).append(observation)
    rows = []
    for attempt in attempts:
        captured = sorted(observations.get(attempt.attempt_id, []),
                          key=lambda observation: observation.observation_id)
        rows.append(AttemptConsumption(attempt,
                                       tuple(obs.observation_id for obs in captured),
                                       _attempt_totals(captured),
                                       tuple(obs for obs in captured if
                                             obs.returncode is not None or
                                             obs.timed_out is not None)))
    totals = ConsumptionTotals(**{
        field: _sum_metrics(getattr(row.totals, field) for row in rows)
        for field in ConsumptionTotals.__dataclass_fields__
    })
    return AttemptConsumptionReport(job_id, len(rows), tuple(rows), totals)
