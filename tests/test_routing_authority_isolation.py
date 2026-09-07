"""Registry loader patches must not outlive their context through lazy imports."""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


class RoutingAuthorityIsolationTests(unittest.TestCase):
    def run_fresh_python(self, source, *args):
        result = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(source), *args],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_allowed_routing_then_billing_discovery(self):
        self.check_discovery_order(
            "test_allowed_model_routing.py", "test_billing_provenance.py"
        )

    def test_billing_then_allowed_routing_discovery(self):
        self.check_discovery_order(
            "test_billing_provenance.py", "test_allowed_model_routing.py"
        )

    def check_discovery_order(self, *patterns):
        self.run_fresh_python("""
            import sys
            import unittest

            loader = unittest.TestLoader()
            for pattern in sys.argv[1:]:
                suite = loader.discover('tests', pattern=pattern)
                result = unittest.TextTestRunner(verbosity=1).run(suite)
                if not result.wasSuccessful():
                    raise SystemExit(1)
        """, *patterns)

    def test_import_under_patch_and_later_patches_restore(self):
        self.run_fresh_python("""
            import sys
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from unittest.mock import patch
            from puppetmaster import model_registry

            assert 'puppetmaster.routing_authority' not in sys.modules
            original = model_registry.load_registry
            with TemporaryDirectory() as tmp:
                path = Path(tmp) / 'models.json'
                real = [model_registry.ModelSpec(
                    id='codex/real', adapter='codex', adapter_model_name='real')]
                fake = [model_registry.ModelSpec(
                    id='codex/fake', adapter='codex', adapter_model_name='fake')]
                model_registry.save_registry(real, path)

                def check(authority, specs):
                    payload = authority.bind_registry_authority({}, path, specs)
                    assert authority.load_bound_registry(payload)[1] == specs
                    bound = authority.resolve_and_bind_explicit_pin(
                        {'model': specs[0].adapter_model_name},
                        adapter='codex', registry_path=path)
                    assert bound['pinned_model'] == specs[0].id
                    assert bound['registry_digest'] == payload['registry_digest']

                with patch.object(model_registry, 'load_registry', return_value=fake) as loader:
                    from puppetmaster import routing_authority
                    check(routing_authority, fake)
                    assert loader.call_count == 2
                assert model_registry.load_registry is original
                check(routing_authority, real)

                with patch.object(model_registry, 'load_registry', return_value=fake) as loader:
                    check(routing_authority, fake)
                    assert loader.call_count == 2
                assert model_registry.load_registry is original
                check(routing_authority, real)
        """)
