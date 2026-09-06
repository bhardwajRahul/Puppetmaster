"""Cache pricing and dashboard/canonical economics parity regressions."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from test_cost_report import _routing, _spec, _usage
from puppetmaster.cost import build_cost_report, build_current_registry_cost_report, price_job
from puppetmaster.dashboard import _PAGE_APP_JS, build_job_snapshot
from puppetmaster.models import JobStatus
from puppetmaster.store import SwarmStore
from puppetmaster.usage import aggregate_token_usage, select_usage_records, token_usage


class CacheEconomicsTests(unittest.TestCase):
    def test_sdk_split_cache_is_exclusive_and_preserved(self):
        for keys in [('inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens'),
                     ('input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens')]:
            with self.subTest(keys=keys):
                record = token_usage(sdk_usage=dict(zip(keys, [1000000, 1000000, 2000000, 500000])))
                artifact = _usage('job_x')
                artifact.payload.update(record)
                selected = select_usage_records([artifact])['t1']
                self.assertEqual(selected.get('cache_read_tokens'), 2000000)
                self.assertEqual(selected.get('cache_write_tokens'), 500000)
                # Fresh 2 + read .4 + write 1 + output 3; cache writes use
                # registry input rates because the registry has no write tier.
                cost = price_job([artifact], [_spec('mid', 'mid-v1', 50, 2, 3)])
                self.assertEqual(cost.total_marginal_cost_usd, 6.4)
                self.assertEqual(cost.nominal_usage_cost_usd, 6.4)

    def test_legacy_cached_input_is_inclusive(self):
        artifact = _usage('job_x')
        artifact.payload['tokens_cached'] = 500000
        cost = price_job([artifact], [_spec('mid', 'mid-v1', 50, 2, 3)])
        self.assertEqual(cost.total_marginal_cost_usd, 4.1)

    def test_split_cache_wins_over_legacy_alias(self):
        artifact = _usage('job_x')
        artifact.payload.update(cache_read_tokens=500000, cache_write_tokens=0, tokens_cached=500000)
        self.assertEqual(price_job([artifact], [_spec('mid', 'mid-v1', 50, 2, 3)]).total_marginal_cost_usd, 5.1)

    def test_counterfactual_and_totals_include_split_cache(self):
        cases = [
            ({'cache_read_tokens': 2000000, 'cache_write_tokens': 500000}, 4500000, 10, 6.4),
            ({'cache_read_tokens': 2000000}, 4000000, 9, 5.4),
            ({'cache_write_tokens': 500000}, 2500000, 6, 6),
            ({'cache_read_tokens': 0, 'tokens_cached': 500000}, 2000000, 5, 5),
            ({'tokens_cached': 500000}, 2000000, 5, 4.1),
            ({}, 2000000, 5, 5),
        ]
        for cache, total, naive, selected in cases:
            for billing, reported in [('api', None), ('api', .42), ('plan', None)]:
                with self.subTest(cache=cache, billing=billing, reported=reported):
                    artifact = _usage('job_x', real_cost_usd=reported)
                    artifact.payload.update(cache)
                    registry = [_spec('mid', 'mid-v1', 50, 2, 3, billing=billing)]
                    report = build_current_registry_cost_report('job_x', [artifact], registry)
                    actual = 0 if billing == 'plan' else reported or selected
                    self.assertEqual(report['actual_cost']['total_marginal_cost_usd'], actual)
                    self.assertEqual(report['counterfactual']['naive_cost_usd'], naive)
                    self.assertAlmostEqual(report['counterfactual']['avoided_usd'], naive - actual)
                    self.assertEqual(report['token_usage']['total_tokens'], total)
                    self.assertEqual(price_job([artifact], registry).measured_usage_tokens, total)

    def test_aggregate_split_cache_measured_and_estimated(self):
        measured = _usage('job_x', 'measured')
        measured.payload.update(cache_read_tokens=2000000, cache_write_tokens=500000,
                                tokens_cached=2000000)
        estimated = _usage('job_x', 'estimated')
        estimated.payload.update(cache_write_tokens=300000, tokens_estimated=True)
        legacy = _usage('job_x', 'legacy')
        legacy.payload['tokens_cached'] = 500000
        totals = aggregate_token_usage([measured, estimated, legacy])
        self.assertEqual(totals['total_tokens'], 8800000)
        self.assertEqual(totals['measured_tokens_in'], 2000000)
        self.assertEqual(totals['estimated_tokens_in'], 1000000)
        self.assertEqual(totals['measured_cache_read_tokens'], 2000000)
        self.assertEqual(totals['measured_cache_write_tokens'], 500000)
        self.assertEqual(totals['estimated_cache_read_tokens'], 0)
        self.assertEqual(totals['estimated_cache_write_tokens'], 300000)

    def test_zero_sdk_usage_remains_measured(self):
        self.assertEqual(token_usage(sdk_usage={'inputTokens': 0, 'outputTokens': 0})['tokens_estimated'], False)

    def test_cache_preserves_plan_and_reported_precedence(self):
        artifact = _usage('job_x', real_cost_usd=0.42)
        artifact.payload.update(cache_read_tokens=2000000, cache_write_tokens=500000)
        for billing, expected in [('api', .42), ('plan', 0)]:
            self.assertEqual(price_job([artifact], [_spec('mid', 'mid-v1', 50, 2, 3, billing=billing)]).total_marginal_cost_usd, expected)


class DashboardEconomicsTests(unittest.TestCase):
    def test_missing_usage_is_unknown(self):
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp))
            store.init()
            job = store.create_job('no usage yet')
            with patch('puppetmaster.cost.load_registry', return_value=[]):
                snap = build_job_snapshot(store, job.id)
            self.assertIsNone(snap['cost']['actual_cost']['total_marginal_cost_usd'])


    def test_snapshot_matches_canonical_report(self):
        for status, receipt in [(JobStatus.RUNNING, False), (JobStatus.COMPLETE, False), (JobStatus.COMPLETE, True)]:
            for billing, reported, unknown in [('api', None, False), ('plan', None, False), ('api', .42, False), ('api', None, True)]:
                with self.subTest(status=status, receipt=receipt, billing=billing, reported=reported, unknown=unknown), TemporaryDirectory() as tmp:
                    store = SwarmStore(Path(tmp))
                    store.init()
                    job = store.create_job('economics')
                    artifact = _usage(job.id, real_cost_usd=reported)
                    artifact.payload.update(cache_read_tokens=2000000, cache_write_tokens=500000)
                    store.save_artifact(artifact)
                    store.save_artifact(_routing(job.id, 't1', 'mid', billing=billing, estimated_cost_usd=99))
                    registry = [] if unknown else [_spec('mid', 'mid-v1', 50, 2, 3, billing=billing)]
                    frozen = build_current_registry_cost_report(job.id, [artifact], registry) if receipt else None
                    if frozen:
                        frozen['pricing_source'] = 'terminal_receipt'
                        frozen['token_usage']['total_tokens'] = 123
                    with patch('puppetmaster.cost.load_registry', return_value=[] if receipt else registry):
                        # Saving directly models pre-upgrade final jobs as well.
                        store.save_job(replace(job, status=status, cost_receipt=frozen))
                        expected = build_cost_report(store, job.id)
                        snap = build_job_snapshot(store, job.id)
                    self.assertEqual(snap['cost'], expected)
                    self.assertEqual(snap['tokens_total'], expected['token_usage']['total_tokens'])
                    self.assertEqual(snap['tokens_total'], 123 if receipt else 4500000)

    def test_partial_cost_keeps_unknown_total_and_known_subtotal(self):
        with TemporaryDirectory() as tmp:
            store = SwarmStore(Path(tmp))
            store.init()
            job = store.create_job('partial pricing')
            store.save_artifacts([
                _usage(job.id, 'known', real_cost_usd=.42),
                _usage(job.id, 'unknown', model='missing'),
            ])
            with patch('puppetmaster.cost.load_registry', return_value=[]):
                actual = build_job_snapshot(store, job.id)['cost']['actual_cost']
            self.assertIsNone(actual['total_marginal_cost_usd'])
            self.assertEqual(actual['priced_subtotal_usd'], .42)
            self.assertEqual(actual['unpriced_tasks'], 1)

    @unittest.skipUnless(shutil.which('node'), 'Node needed to execute dashboard formatter')
    def test_render_unknown_zero_and_partial(self):
        start = _PAGE_APP_JS.index('function formatSelectedCost(')
        end = _PAGE_APP_JS.index('\n}', start) + 2
        script = _PAGE_APP_JS[start:end] + '\nconsole.log(JSON.stringify([formatSelectedCost(null), formatSelectedCost({}), formatSelectedCost({total_marginal_cost_usd:null, priced_subtotal_usd:1}), formatSelectedCost({total_marginal_cost_usd:0}), formatSelectedCost({total_marginal_cost_usd:.42})]));'
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), ['unknown', 'unknown', 'unknown', '$0.0000', '$0.4200'])


if __name__ == '__main__':
    unittest.main()
