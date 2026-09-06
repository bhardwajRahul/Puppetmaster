"""Eval budgets reach independent durable jobs without launching a provider."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from puppetmaster.budget import (
    BUDGET_FIELDS, BudgetAdmissionError, BudgetPolicy, budget_cli_flags,
    budget_policy_from_inputs,
)
from puppetmaster.cli._dispatch import _main
from puppetmaster.cli._parser import build_parser
from puppetmaster.eval_harness import adapter_apply_fn, builtin_cases, EvalReport
from puppetmaster.invocation import execution_scope, invocation
from puppetmaster.orchestrator import Orchestrator


POLICY = BudgetPolicy(max_usd=2.5, max_tokens_in=100, max_tokens_out=50,
                      max_attempts=1, max_elapsed_seconds=30)


class EvalBudgetTests(unittest.TestCase):
    def test_parser_fields_and_legacy(self):
        parser = build_parser()
        for policy in [POLICY, None]:
            args = parser.parse_args(['eval', *budget_cli_flags(policy)])
            self.assertEqual(budget_policy_from_inputs(vars(args)), policy)
        for field in BUDGET_FIELDS:
            with self.subTest(field=field):
                args = parser.parse_args(['eval', '--budget-' + field.replace('_', '-'), '2'])
                self.assertEqual(budget_policy_from_inputs(vars(args)), BudgetPolicy(**{field: 2}))

    def test_invalid_limits_fail_before_launch(self):
        with mock.patch('puppetmaster.eval_harness.adapter_apply_fn') as launch:
            for field, kind in BUDGET_FIELDS.items():
                for value in ['0', '-1', 'nan', 'inf', '-inf', 'oops'] + (['1.5'] if kind is int else []):
                    with self.subTest(field=field, value=value), contextlib.redirect_stderr(io.StringIO()):
                        with self.assertRaises(SystemExit) as error:
                            _main(['eval', '--budget-' + field.replace('_', '-') + '=' + value])
                        self.assertEqual(error.exception.code, 2)
            launch.assert_not_called()

    def test_cli_passes_policy_to_harness(self):
        for policy in [POLICY, None]:
            with self.subTest(policy=policy), tempfile.TemporaryDirectory() as root, \
                    mock.patch('puppetmaster.eval_harness.adapter_apply_fn') as factory, \
                    mock.patch('puppetmaster.eval_harness.run_eval', return_value=EvalReport(
                        adapter='agentic', model=None, results=[])), \
                    contextlib.redirect_stdout(io.StringIO()):
                _main(['--state-dir', root, 'eval', *budget_cli_flags(policy)])
                self.assertEqual(factory.call_args.kwargs['budget_policy'], policy)

    def test_demo_launches_validate_and_propagate(self):
        for verb, method in [('demo', 'run'), ('crash-demo', 'run_crash_recovery_demo')]:
            for policy in [POLICY, None]:
                with self.subTest(verb=verb, policy=policy), tempfile.TemporaryDirectory() as root, \
                        mock.patch.object(Orchestrator, method, side_effect=InterruptedError) as run:
                    with self.assertRaises(InterruptedError):
                        _main(['--state-dir', root, verb, *budget_cli_flags(policy)])
                    self.assertEqual(run.call_args.kwargs['budget_policy'], policy)
            for field in BUDGET_FIELDS:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    _main([verb, '--budget-' + field.replace('_', '-'), '0'])

    def test_crash_demo_persists_budget_before_workers(self):
        from puppetmaster.store_factory import create_store
        for policy in [POLICY, None]:
            with tempfile.TemporaryDirectory() as root:
                store = create_store('sqlite', root)
                with mock.patch('puppetmaster.orchestrator._tag_job_effort', side_effect=InterruptedError):
                    with self.assertRaises(InterruptedError):
                        Orchestrator(store).run_crash_recovery_demo('test', budget_policy=policy)
                jobs = store.list_jobs()
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0].budget_policy, policy)

    def test_harness_preserves_adapter_options_and_policy(self):
        for policy in [POLICY, None]:
            with mock.patch.object(Orchestrator, 'run') as run:
                adapter_apply_fn(adapter='agentic', model='test-model', provider='test-provider',
                                 budget_policy=policy)(Path('/unused'), builtin_cases()[0])
                self.assertEqual(run.call_args.kwargs['budget_policy'], policy)
                payload = run.call_args.kwargs['specs'][0].payload
                self.assertEqual(payload['model'], 'test-model')
                self.assertEqual(payload['provider'], 'test-provider')
                self.assertIn('verify_command', payload)
                self.assertNotIn('max_cost_usd', payload)
                self.assertFalse(any(key.startswith('budget_') for key in payload))

    def test_real_job_persistence_and_admission_are_independent_per_case(self):
        original_run = Orchestrator.run
        for policy in [POLICY, None]:
            jobs = []
            class Captured(Exception):
                pass

            def run(instance, *args, **kwargs):
                def created(job):
                    jobs.append(job.id)
                    self.assertEqual(instance.store.get_job(job.id).budget_policy, policy)
                    task = SimpleNamespace(job_id=job.id, id='eval-task', adapter='test', payload={
                        'billing': 'plan', 'budget_allowance': {
                            'tokens_in': 1, 'tokens_out': 1, 'elapsed_seconds': 1}})
                    with execution_scope(instance.store, SimpleNamespace(id='eval-run'), task):
                        with invocation():
                            pass
                        if policy is not None:
                            with self.assertRaises(BudgetAdmissionError), invocation():
                                self.fail('second dispatch exceeded per-case attempt cap')
                        else:
                            with invocation():
                                pass
                    raise Captured()
                return original_run(instance, *args, **kwargs, on_job_created=created)

            apply = adapter_apply_fn(budget_policy=policy)
            with mock.patch.object(Orchestrator, 'run', autospec=True, side_effect=run):
                for case in builtin_cases()[:2]:
                    with self.assertRaises(Captured):
                        apply(Path('/unused'), case)
            self.assertEqual(len(set(jobs)), 2)


if __name__ == '__main__':
    unittest.main()
