"""Public cumulative budgets survive CLI/MCP transport and durable replay."""
import contextlib
import io
import unittest
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from puppetmaster.budget import (
    BUDGET_FIELDS, BudgetPolicy, budget_cli_flags, budget_policy_from_inputs,
)
from puppetmaster.cli import build_parser
from puppetmaster.cli._dispatch import _main
from puppetmaster.cli.commands_jobs import read_job_state
from puppetmaster import mcp_server as mcp
from puppetmaster.dashboard import build_job_snapshot
from puppetmaster.orchestrator import Orchestrator
from puppetmaster.sqlite_store import SQLiteSwarmStore
from puppetmaster.store import SwarmStore
from puppetmaster.swarm_launch import detach_analysis_swarm


POLICY = BudgetPolicy(0.25, 10000, 2000, 2, 60.5)
INPUTS = {'budget_' + key: value for key, value in asdict(POLICY).items()}


from puppetmaster.identity import make_ref


def _test_reference(root, job_id, *, expected_incarnation=None, launch_binding=False):
    assert launch_binding is True
    return make_ref(root, job_id, "00000000-0000-4000-8000-000000000001")


class PublicBudgetTests(unittest.TestCase):
    def test_validation_and_omission(self):
        self.assertIsNone(budget_policy_from_inputs({'max_cost_usd': 0.1}))
        self.assertEqual(budget_policy_from_inputs(INPUTS), POLICY)
        for field, kind in BUDGET_FIELDS.items():
            self.assertIsNotNone(budget_policy_from_inputs({'budget_' + field: 1}))
            for value in (0, -1, float('nan'), float('inf'), True, '2', [], 10**400):
                with self.subTest(field=field, value=str(value)[:20]):
                    with self.assertRaisesRegex(ValueError, 'budget_' + field):
                        budget_policy_from_inputs({'budget_' + field: value})
            if kind is int:
                with self.assertRaises(ValueError):
                    budget_policy_from_inputs({'budget_' + field: 1.5})
        self.assertEqual(BudgetPolicy(max_attempts=0).max_attempts, 0)

    def test_all_launch_parsers(self):
        parser = build_parser()
        for verb in ('run', 'cursor', 'claude', 'openai', 'codex', 'hermes',
                     'antigravity', 'agentic', 'edit', 'prewalk', 'swarm', 'review', 'browser', 'rerun'):
            with self.subTest(verb=verb):
                args = parser.parse_args([verb, 'test', *budget_cli_flags(POLICY)])
                self.assertEqual(budget_policy_from_inputs(vars(args)), POLICY)
                legacy = parser.parse_args([verb, 'test'])
                self.assertIsNone(budget_policy_from_inputs(vars(legacy)))
        args = parser.parse_args(['codex', 'test', '--max-cost-usd', '9',
                                  '--budget-max-usd', '1'])
        self.assertEqual(args.max_cost_usd, 9)
        self.assertEqual(budget_policy_from_inputs(vars(args)).max_usd, 1)

    def test_cli_rejects_before_store_creation(self):
        with patch('puppetmaster.cli._dispatch.create_store') as create:
            for verb in ('codex', 'rerun'):
                for value in ('0', '-1', 'nan', 'inf', 'invalid'):
                    with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                        _main([verb, 'test', '--budget-max-usd', value])
            create.assert_not_called()

    def test_schemas_and_builders(self):
        for name in ('swarm_schema', 'review_schema', 'cursor_swarm_schema',
                     'codex_schema', 'claude_schema', 'cursor_implement_schema',
                     'implement_schema', 'edit_schema', 'browser_swarm_schema',
                     'prewalk_schema', 'agentic_schema', 'openai_schema'):
            props = getattr(mcp, name)()['properties']
            for key in INPUTS:
                self.assertEqual(props[key]['exclusiveMinimum'], 0)
        args = dict(INPUTS, goal='test', instruction='test', tasks=['test'], cwd='.')
        for name in ('cursor_command', 'codex_command', 'hermes_command',
                     'antigravity_command', 'edit_command', 'browser_swarm_command',
                     'prewalk_command', 'agentic_command', 'claude_command', 'openai_command'):
            with self.subTest(builder=name):
                command = getattr(mcp, name)(args)
                parsed = build_parser().parse_args(command)
                self.assertEqual(budget_policy_from_inputs(vars(parsed)), POLICY)
                self.assertNotIn('--max-cost-usd', command)
        command = mcp.codex_command(dict(args, max_cost_usd=7))
        self.assertEqual(command[command.index('--max-cost-usd') + 1], '7')
        self.assertEqual(command[command.index('--budget-max-usd') + 1], '0.25')

    def test_cli_dispatch_to_orchestrator(self):
        with TemporaryDirectory() as root, patch.dict('os.environ', {'PUPPETMASTER_WORKER': '0'}), patch.object(
            Orchestrator, 'run', side_effect=InterruptedError('captured')
        ) as run:
            with self.assertRaises(InterruptedError):
                _main(['--state-dir', root, 'run', 'test', *budget_cli_flags(POLICY)])
            self.assertEqual(run.call_args.kwargs['budget_policy'], POLICY)
            with self.assertRaises(InterruptedError):
                _main(['--state-dir', root, 'run', 'test'])
            self.assertIsNone(run.call_args.kwargs['budget_policy'])

    def test_rerun_inherits_and_persists_policy(self):
        import json

        def stop(job):
            raise InterruptedError(job.id)

        original_run = Orchestrator.run
        for store_type in (SwarmStore, SQLiteSwarmStore):
            for configured in (False, True):
                with self.subTest(store=store_type.__name__, config=configured), TemporaryDirectory() as root:
                    store = store_type(Path(root))
                    config = Path(root) / 'workflow.json'
                    config.write_text(json.dumps({'workers': [
                        {'role': 'explore', 'instruction': 'test', 'adapter': 'local',
                         'payload': {'max_cost_usd': 9}}
                    ]}))
                    suffix = ['--config', str(config)] if configured else []
                    with patch.dict('os.environ', {'PUPPETMASTER_WORKER': '0',
                                                  'PUPPETMASTER_LAUNCH_KEY': ''}):
                        for source_policy in (POLICY, BudgetPolicy(max_attempts=0), None):
                            with self.assertRaises(InterruptedError) as source:
                                original_run(Orchestrator(store), 'test', budget_policy=source_policy,
                                             on_job_created=stop)
                            source_id = str(source.exception)
                            # Exercise dispatch and real durable creation, stopping before workers.
                            def capture(instance, *args, **kwargs):
                                return original_run(instance, *args, **kwargs, on_job_created=stop)
                            with patch('puppetmaster.cli._dispatch.create_store', return_value=store), patch.object(
                                Orchestrator, 'run', autospec=True, side_effect=capture
                            ):
                                command = ['rerun', source_id, *suffix]
                                with self.assertRaises(InterruptedError) as rerun:
                                    _main(command)
                                reopened = store_type(Path(root))
                                self.assertEqual(reopened.get_job(str(rerun.exception)).budget_policy, source_policy)
                                self.assertNotEqual(str(rerun.exception), source_id)
                                if source_policy == POLICY:
                                    # A partial matching flag cannot discard the other four caps.
                                    with self.assertRaises(InterruptedError) as matching:
                                        _main([*command, '--budget-max-usd', '0.25'])
                                    self.assertEqual(store.get_job(str(matching.exception)).budget_policy, POLICY)
                                    for field in BUDGET_FIELDS:
                                        with self.assertRaisesRegex(ValueError, 'preserves the source budget'):
                                            _main([*command, '--budget-' + field.replace('_', '-'), '99999'])
                                if source_policy is None:
                                    with self.assertRaises(InterruptedError) as explicit:
                                        _main([*command, *budget_cli_flags(POLICY)])
                                    self.assertEqual(store.get_job(str(explicit.exception)).budget_policy, POLICY)
                                # A launch-key replay stays idempotent; conflicting persisted policy fails closed.
                                with patch.dict('os.environ', {'PUPPETMASTER_LAUNCH_KEY': 'rerun-' + source_id}):
                                    with self.assertRaises(InterruptedError) as keyed:
                                        _main(command)
                                    with self.assertRaises(InterruptedError) as replay:
                                        _main(command)
                                    self.assertEqual(str(replay.exception), str(keyed.exception))
                                    if source_policy is None:
                                        with self.assertRaises(ValueError):
                                            _main([*command, *budget_cli_flags(POLICY)])
                                    with self.assertRaises(ValueError):
                                        original_run(Orchestrator(store), 'test', budget_policy=(
                                            None if source_policy is not None else POLICY))
                                    self.assertEqual(store.get_job(str(keyed.exception)).budget_policy, source_policy)

    def test_mcp_handlers_propagate_policy(self):
        args = dict(INPUTS, goal='test', cwd='.', adapter='local')
        with patch.object(mcp, '_platform_lock_preflight', return_value=None), patch.object(
            mcp, 'start_cli', return_value={}
        ) as start:
            mcp.start_codex(args)
            self.assertEqual(budget_policy_from_inputs(vars(build_parser().parse_args(
                start.call_args.args[0]))), POLICY)
            with patch.object(mcp, 'write_generated_swarm_config', return_value=Path('/tmp/config.json')):
                mcp.start_swarm(args)
            self.assertEqual(budget_policy_from_inputs(vars(build_parser().parse_args(
                start.call_args.args[0]))), POLICY)

    def test_transport_validates_without_spawning(self):
        with patch.object(mcp.subprocess, 'Popen') as spawn, patch.object(mcp.subprocess, 'run') as run:
            for launch in (mcp.start_cli, mcp.run_cli):
                with self.assertRaisesRegex(ValueError, 'budget_max_attempts'):
                    launch(['run', 'test'], {'budget_max_attempts': 0})
            spawn.assert_not_called()
            run.assert_not_called()

    @patch("puppetmaster.identity.reference_at", new=_test_reference)
    def test_detached_swarm_transports_policy(self):
        with TemporaryDirectory() as root, patch('puppetmaster.swarm_launch.subprocess.Popen') as spawn, patch(
            'puppetmaster.swarm_launch.wait_for_job_id', return_value='job_test'
        ), patch('puppetmaster.swarm_launch.write_analysis_swarm_config', return_value=Path(root) / 'config.json'):
            spawn.return_value.pid = 123
            detach_analysis_swarm(goal='test', roles=['explore'], adapter='local',
                                  state_dir=Path(root), cwd=root, budget_policy=POLICY)
            command = spawn.call_args.args[0]
            parsed = build_parser().parse_args(command[4:])
            self.assertEqual(budget_policy_from_inputs(vars(parsed)), POLICY)

    def test_orchestrator_persistence_and_replay(self):
        for store_type in (SwarmStore, SQLiteSwarmStore):
            with self.subTest(store=store_type.__name__), TemporaryDirectory() as root:
                store = store_type(Path(root))
                # Stop immediately after durable creation, before any worker dispatch.
                def stop(job):
                    raise InterruptedError(job.id)
                orchestrator = Orchestrator(store)
                with self.assertRaises(InterruptedError) as caught:
                    orchestrator.run('test', launch_key='public-budget', budget_policy=POLICY,
                                     on_job_created=stop)
                job_id = str(caught.exception)
                reopened = store_type(Path(root))
                self.assertEqual(reopened.get_job(job_id).budget_policy, POLICY)
                self.assertEqual(reopened.status_snapshot(job_id, compact=True)['job']['budget_policy'], asdict(POLICY))
                self.assertEqual(read_job_state(reopened, job_id)['budget_policy'], asdict(POLICY))
                self.assertEqual(build_job_snapshot(reopened, job_id)['budget']['policy'], asdict(POLICY))
                replay = Orchestrator(reopened).run('test', launch_key='public-budget', budget_policy=POLICY)
                self.assertEqual(replay.job.id, job_id)
                for other in (None, BudgetPolicy(max_attempts=3)):
                    with self.assertRaises(ValueError):
                        Orchestrator(reopened).run('test', launch_key='public-budget', budget_policy=other)
                with self.assertRaises(InterruptedError) as legacy:
                    orchestrator.run('legacy', on_job_created=stop)
                self.assertIsNone(reopened.get_job(str(legacy.exception)).budget_policy)


if __name__ == '__main__':
    unittest.main()
