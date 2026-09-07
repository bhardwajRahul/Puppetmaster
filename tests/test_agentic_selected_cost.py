"""Provider cost presence survives Agentic aggregation and terminal freeze."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from puppetmaster.adapters.agentic import AgenticAdapter
from puppetmaster.invocation import execution_scope
from puppetmaster.models import ArtifactType, JobStatus, Task
from puppetmaster.providers import AssistantTurn, _openai_usage_fields
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore
from puppetmaster.usage import select_usage_records
from tests.test_cost_report import _registry


class AgenticSelectedCostTests(unittest.TestCase):
    def check_costs(self, costs, forced):
        for store_type in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(backend=store_type.__name__, forced=forced, costs=costs):
                with TemporaryDirectory() as tmp:
                    store = store_type(Path(tmp) / 'state')
                    job = store.create_job('provider cost provenance')
                    task = Task(job_id=job.id, role='explore', instruction='inspect',
                                adapter='agentic', payload={
                                    'cwd': tmp, 'provider': 'openrouter', 'model': 'mid-v1',
                                    'disable_codegraph': True, 'stream_deltas': False,
                                    'max_turns': 1 if forced else len(costs),
                                })
                    store.save_task(task)
                    turns = []
                    for index, cost in enumerate(costs):
                        raw = dict(prompt_tokens=5, completion_tokens=2, total_tokens=7)
                        if cost is not None:
                            raw['cost'] = cost
                        submit = index == len(costs) - 1
                        turns.append(AssistantTurn(
                            tool_calls=[dict(id=str(index),
                                name='submit_findings' if submit else 'list_dir',
                                arguments={'artifacts': []} if submit else {'path': '.'})],
                            usage=_openai_usage_fields(raw), accounting_usage=raw))
                    with execution_scope(store, SimpleNamespace(id='run'), task), \
                            patch('puppetmaster.adapters.agentic.provider_chat', side_effect=turns) as call, \
                            patch.object(AgenticAdapter, '_compose_delta_sink', return_value=None), \
                            patch('puppetmaster.adapters.agentic.get_provider_circuit_breaker'), \
                            patch('puppetmaster.rate_limit_state.admit_or_raise'):
                        artifacts = AgenticAdapter().run(task, task.instruction, 'worker')
                    self.assertEqual(call.call_count, len(costs))
                    payload = next(a.payload for a in artifacts if a.type == ArtifactType.VERIFICATION)
                    self.assertEqual(payload['result'], 'passed')
                    self.assertEqual(payload['submit_forced_max_turns'], forced)
                    expected = sum(costs) if all(c is not None for c in costs) else None
                    self.assertEqual(payload.get('real_cost_usd'), expected)
                    self.assertEqual('real_cost_usd' in payload, expected is not None)
                    facts = select_usage_records(artifacts)[task.id]['selected_facts']
                    self.assertEqual(facts.get('real_cost_usd'), expected)
                    self.assertEqual(payload['tokens_in'], 5 * len(costs))
                    # All provider calls still retain independent consumption observations.
                    self.assertEqual(len(store.list_attempts(job.id)), len(costs))
                    observations = store.list_usage_observations(job.id)
                    self.assertEqual(len(observations), len(costs))
                    self.assertEqual(sorted(o.cost_usd for o in observations if o.cost_usd is not None),
                                     sorted(c for c in costs if c is not None))
                    for artifact in artifacts:
                        store.save_artifact(artifact)
                    with patch('puppetmaster.cost.load_registry', return_value=_registry()):
                        store.update_job_status(job.id, JobStatus.COMPLETE)
                    result = store.get_selected_economics(store.job_ref(job.id))
                    metric = result.totals.api_cost_usd
                    self.assertEqual(metric.total, expected)
                    self.assertEqual(metric.unknown_selected, int(expected is None))
                    self.assertEqual(metric.known_selected, int(expected is not None))
                    self.assertEqual(metric.state, 'unknown' if expected is None else 'measured')

    def test_explicit_zero(self):
        for forced in (False, True):
            self.check_costs([0, 0], forced)

    def test_mixed_known_and_missing(self):
        for forced in (False, True):
            for costs in ([0.25, None], [None, 0.25]):
                self.check_costs(costs, forced)

    def test_all_known(self):
        for forced in (False, True):
            self.check_costs([0, 0.25], forced)
