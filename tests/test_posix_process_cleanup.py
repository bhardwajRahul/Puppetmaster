"""POSIX containment must not depend on visible descendant environments."""
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from puppetmaster.win_process import cleanup_owned_process, popen_owned


@unittest.skipUnless(os.name == "posix", "POSIX process groups required")
class PosixProcessCleanupTests(unittest.TestCase):
    def test_group_cleanup_without_environment_discovery(self):
        parent = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],env={}); "
            "print('ready',flush=True); time.sleep(60)"
        )
        process = popen_owned([sys.executable, "-c", parent],
                              start_new_session=True, stdout=subprocess.PIPE, text=True)
        reader = None
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            with patch("puppetmaster.win_process._owned_posix_pids", side_effect=OSError("unavailable")):
                cleanup_owned_process(process, "nonce", time.monotonic() + 2)
            reader = threading.Thread(target=process.stdout.read, daemon=True)
            reader.start()
            reader.join(timeout=2)
            self.assertFalse(reader.is_alive(), "descendant still holds stdout open")
        finally:
            # This dedicated test group still has a live member if cleanup failed.
            if process.returncode is None or (reader is not None and reader.is_alive()):
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    pass
            process.wait(timeout=3)
            process.stdout.close()

    def test_reaped_leader_never_authorizes_group_signal(self):
        process = popen_owned([sys.executable, "-c", "pass"], start_new_session=True)
        process.wait(timeout=3)
        with patch("puppetmaster.win_process.os.killpg") as killpg:
            cleanup_owned_process(process, "", time.monotonic() + 1)
        killpg.assert_not_called()

    def test_shared_session_never_authorizes_group_signal(self):
        process = popen_owned([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            with patch("puppetmaster.win_process.os.killpg") as killpg:
                cleanup_owned_process(process, "", time.monotonic() + 1)
            killpg.assert_not_called()
        finally:
            process.kill()
            process.wait(timeout=3)

    def test_group_signal_holds_reaping_lock(self):
        process = popen_owned([sys.executable, "-c", "import time; time.sleep(60)"],
                              start_new_session=True)
        killpg = os.killpg

        def checked_killpg(pid, sig):
            self.assertTrue(process._waitpid_lock.locked())
            self.assertIsNone(process.returncode)
            killpg(pid, sig)

        try:
            with patch("puppetmaster.win_process.os.killpg", side_effect=checked_killpg) as signal_group:
                cleanup_owned_process(process, "", time.monotonic() + 1)
            signal_group.assert_called_once()
        finally:
            process.kill()
            process.wait(timeout=3)
