import contextlib
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from puppetmaster import setup_readiness as readiness
from puppetmaster.model_registry import ModelSpec, save_registry, discovery_meta_path


class SetupReadinessTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(TemporaryDirectory()))
        self.registry = self.home / 'models.json'
        self.stack.enter_context(patch.dict(os.environ, {
            'PUPPETMASTER_MODELS_PATH': str(self.registry),
            'CODEX_HOME': str(self.home / '.codex'),
        }, clear=True))
        self.stack.enter_context(patch('pathlib.Path.home', return_value=self.home))
        self.stack.enter_context(patch('puppetmaster.model_registry.default_registry_path', return_value=self.registry))
        self.stack.enter_context(patch('puppetmaster.platform_lock.is_adapter_enabled', return_value=True))
        self.stack.enter_context(patch('puppetmaster.diagnostics._codex_cli_installed', return_value=True))
        # Any unexpected generation or network request fails the test.
        self.live = self.stack.enter_context(patch('puppetmaster.preflight.live_probe', side_effect=AssertionError('live probe')))
        self.network = self.stack.enter_context(patch('urllib.request.urlopen', side_effect=AssertionError('network')))
        self.stack.enter_context(patch('puppetmaster.platform_billing._default_runner', return_value=(0, 'Logged in using ChatGPT', '')))
        self.write_model()

    def write_model(self, adapter='codex', enabled=True, provider=None, retired=False):
        self.spec = ModelSpec(
            id=adapter + '/gpt-6-astra', adapter=adapter, adapter_model_name='gpt-6-astra',
            enabled=enabled and not retired, retired=retired,
            retirement_reason="test retirement" if retired else "",
            retirement_authority="test" if retired else "",
            payload_defaults={'provider': provider} if provider else {},
        )
        save_registry([self.spec], self.registry)
        self.catalog(adapter)

    def catalog(self, source='codex', ids=None, old=False):
        discovery_meta_path(self.registry).write_text(json.dumps({source: {
            'model_ids': ['gpt-6-astra'] if ids is None else ids,
            'refreshed_at': '2000-01-01T00:00:00Z' if old else datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'origin': 'live',
        }}))

    def collect(self, adapter='codex', install='installed', rc=0):
        return readiness.collect_setup_readiness({adapter}, installation_results={adapter: install}, installation_rc=rc)

    def test_successful_static_readiness_and_no_live_calls(self):
        result = self.collect()
        row = result['targets'][0]
        for name in ('installed', 'credentials_ready', 'model_available', 'preflight_passed'):
            self.assertEqual(row[name]['status'], 'pass', row)
        self.assertEqual(row['first_run_verified']['status'], 'skipped')
        self.live.assert_not_called()
        self.network.assert_not_called()
        json.dumps(result)

    def test_missing_keys(self):
        self.write_model('openai')
        row = self.collect('openai')['targets'][0]
        self.assertEqual(row['credentials_ready']['status'], 'fail')
        self.assertEqual(row['preflight_passed']['status'], 'fail')

    def test_missing_codex_cli_with_valid_auth(self):
        with patch('puppetmaster.diagnostics._codex_cli_installed', return_value=False):
            row = self.collect()['targets'][0]
        self.assertEqual(row['installed']['status'], 'fail')
        self.assertEqual(row['credentials_ready']['status'], 'pass')
        self.assertEqual(row['preflight_passed']['status'], 'fail')

    def test_absent_disabled_retired_and_stale_astra(self):
        for case, expected in [('absent', 'fail'), ('disabled', 'fail'), ('retired', 'fail'), ('stale', 'unknown'), ('no_catalog', 'unknown')]:
            with self.subTest(case=case):
                self.write_model(enabled=case != 'disabled', retired=case == 'retired')
                if case == 'absent':
                    self.catalog(ids=['other'])
                elif case == 'stale':
                    self.catalog(old=True)
                elif case == 'no_catalog':
                    discovery_meta_path(self.registry).unlink()
                row = self.collect()['targets'][0]
                self.assertEqual(row['model_available']['status'], expected)
                self.assertTrue(row['model_available']['remedy'])

    def test_absent_registry_model(self):
        save_registry([], self.registry)
        self.assertEqual(self.collect()['targets'][0]['model_available']['status'], 'fail')

    def test_installation_errors_and_dry_run(self):
        result = self.collect(install='error', rc=1)
        self.assertEqual(result['installation_rc'], 1)
        self.assertEqual(result['targets'][0]['installed']['status'], 'fail')
        self.assertEqual(self.collect(install='would_install')['targets'][0]['installed']['status'], 'skipped')

    def test_host_only_setup(self):
        result = readiness.collect_setup_readiness(set(), host_pilots={'pi'}, installation_results={'pi': 'installed'})
        row = result['targets'][0]
        self.assertTrue(row['host_only'])
        self.assertEqual(row['installed']['status'], 'pass')
        self.assertEqual(row['model_available']['status'], 'skipped')
        self.assertEqual(row['first_run_verified']['status'], 'skipped')

    def test_unknown_adapter_and_provider(self):
        row = self.collect('mystery')['targets'][0]
        self.assertEqual(row['credentials_ready']['status'], 'fail')
        self.assertEqual(row['preflight_passed']['status'], 'fail')
        self.write_model('agentic', provider='unknown-provider')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-test-key'}):
            row = self.collect('agentic')['targets'][0]
        self.assertEqual(row['credentials_ready']['status'], 'unknown')
        self.assertEqual(row['preflight_passed']['status'], 'fail')

    def test_other_provider_key_does_not_satisfy_selected_provider(self):
        self.write_model('agentic', provider='anthropic')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-test-key'}):
            row = self.collect('agentic')['targets'][0]
        self.assertEqual(row['credentials_ready']['status'], 'fail')

    def test_unreadable_registry_is_unknown(self):
        self.registry.write_text('{broken json')
        row = self.collect()['targets'][0]
        self.assertEqual(row['model_available']['status'], 'unknown')
        self.assertEqual(row['preflight_passed']['status'], 'fail')

    def test_missing_codex_login(self):
        with patch('puppetmaster.platform_billing._default_runner', return_value=(1, 'Not logged in', '')):
            row = self.collect()['targets'][0]
        self.assertEqual(row['credentials_ready']['status'], 'fail')

    def test_platform_lock_blocks_preflight(self):
        with patch('puppetmaster.platform_lock.is_adapter_enabled', return_value=False):
            row = self.collect()['targets'][0]
        self.assertEqual(row['preflight_passed']['status'], 'fail')

    def test_setup_preserves_return_code_and_handoff(self):
        from puppetmaster.cli import commands_install as install
        import puppetmaster.cli as cli
        for status, expected in [('installed', 0), ('error', 1)]:
            with self.subTest(status=status), contextlib.ExitStack() as stack:
                args = SimpleNamespace(state_dir=None, skip_doctor=True, skip_models=True,
                                       skip_rules=True, skip_hooks=True, platforms='codex')
                stack.enter_context(patch.object(install, '_setup_platform_step', return_value=0))
                stack.enter_context(patch('puppetmaster.platform_lock.is_configured', return_value=True))
                stack.enter_context(patch('puppetmaster.platform_lock.enabled_adapters', return_value={'codex'}))
                stack.enter_context(patch('shutil.which', side_effect=lambda name: '/fake/codex' if name == 'codex' else None))
                stack.enter_context(patch.object(cli, 'install_codex_mcp', return_value=SimpleNamespace(status=status, messages=[])))
                output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertEqual(install._run_setup(args), expected)
                self.assertEqual(args.setup_readiness['installation_rc'], expected)
                self.assertIn('start a new `codex` session', output.getvalue())
                self.assertIn('first run', output.getvalue())
                self.assertNotIn('Setup complete.', output.getvalue())
                self.live.assert_not_called()
                self.network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
