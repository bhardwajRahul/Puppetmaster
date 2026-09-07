"""The launch receipt uses the child handshake, never a quiescent-store read."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))
import hermetic_env  # noqa: F401
from readonly_fixtures import file_bytes

from puppetmaster import identity, mcp_server, readonly
from puppetmaster.models import JobRef
from puppetmaster.state import state_identity
from puppetmaster.store_factory import create_store


def fingerprint(root):
    return {str(p.relative_to(root)): (p.stat().st_ino, p.stat().st_mtime_ns,
            hashlib.sha256(file_bytes(p)).hexdigest())
            for p in root.rglob('*') if p.is_file()}


class McpLaunchIdentityTests(unittest.TestCase):
    def test_writing_child_returns_exact_receipt_without_observation(self):
        for backend in ('file', 'sqlite'):
            with self.subTest(backend=backend), TemporaryDirectory() as tmp:
                root = Path(tmp) / 'state'
                state = {}
                launcher = Mock(pid=987654)
                launcher.poll.return_value = None
                unavailable = patch.object(readonly, 'connect', side_effect=readonly.ReadUnavailable(
                    'unable to open database: source changed'))

                def spawn(command, **kwargs):
                    incarnation = command[command.index('--store-incarnation') + 1]
                    store = create_store(backend, root)
                    store._incarnation = incarnation
                    if backend == 'sqlite':
                        store._open_mode = 'attach'
                    job = store.create_job('launched job', launch_key='one-launch')
                    self.assertEqual(store._incarnation, incarnation)
                    state.update(store=store, job=job, incarnation=incarnation)
                    return launcher

                def reported(*args, **kwargs):
                    # Commit real source changes after launch and keep a writer
                    # open throughout receipt construction. No timing lottery.
                    path = root / ('state.sqlite3' if backend == 'sqlite' else 'metadata.sqlite3')
                    writer = sqlite3.connect(str(path))
                    state['writer'] = writer
                    writer.execute('CREATE TABLE launch_race_probe(value INTEGER)')
                    writer.execute('INSERT INTO launch_race_probe VALUES(1)')
                    writer.commit()
                    writer.execute('INSERT INTO launch_race_probe VALUES(2)')
                    state['before'] = fingerprint(root)
                    state['reader'] = unavailable.start()
                    state['started'] = time.monotonic()
                    return state['job'].id

                try:
                    with patch.object(mcp_server.subprocess, 'Popen', side_effect=spawn) as popen, \
                            patch.object(mcp_server, '_track_async_process'), \
                            patch.object(mcp_server, 'wait_for_job_id', side_effect=reported), \
                            patch.object(mcp_server, '_terminate_launcher_tree') as terminate:
                        result = mcp_server.start_cli(['review', 'goal'], dict(
                            cwd=tmp, state_dir=str(root), backend=backend, launch_key='one-launch'))
                        self.assertLess(time.monotonic() - state['started'], 1)
                        body = json.loads(result['content'][0]['text'])
                        expected = dict(version=2, job_id=state['job'].id,
                            state_id=state_identity(root), incarnation=state['incarnation'])
                        self.assertEqual(body['job_ref'], expected)
                        self.assertEqual(body['monitor_with']['arguments']['job_ref'], expected)
                        self.assertEqual(body['monitor_with']['backend'], backend)
                        popen.assert_called_once()
                        self.assertEqual(popen.call_args.args[0].count('--launch-key'), 1)
                        terminate.assert_not_called()
                        state['reader'].assert_not_called()
                        self.assertEqual(fingerprint(root), state['before'])
                        self.assertIsNone(launcher.poll())
                finally:
                    unavailable.stop()
                    if 'writer' in state:
                        state['writer'].rollback()
                        state['writer'].close()
                store = state['store']
                ref = JobRef(**body['job_ref'])
                before = fingerprint(root)
                store.validate_job_ref(ref, strict=True)
                self.assertEqual(fingerprint(root), before)
                self.assertEqual(store.get_job(ref.job_id).launch_key, 'one-launch')
                # A later replacement cannot silently rebind the launch receipt.
                root.rename(root.with_name('old'))
                replacement = create_store(backend, root)
                replacement.init()
                with self.assertRaises(identity.StoreIdentityError):
                    replacement.validate_job_ref(ref, strict=True)


if __name__ == '__main__':
    unittest.main()
