#!/usr/bin/env python3
"""Focused process-group cleanup regressions for the release validator."""
import errno
import importlib.util
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location("release_validator", Path(__file__).with_name("test-release.py"))
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class CleanupTests(unittest.TestCase):
    def process(self):
        process = mock.Mock()
        process.pid = 12345
        return process

    def test_transient_permission_error_requires_confirmed_disappearance(self):
        process = self.process()
        with mock.patch.object(validator.os, "killpg", side_effect=[
            PermissionError(errno.EPERM, "group is exiting"),
            ProcessLookupError(errno.ESRCH, "group is gone"),
            ProcessLookupError(errno.ESRCH, "group is gone"),
        ]) as kill, mock.patch.object(validator.time, "sleep"):
            validator.stop_group(process)
        self.assertEqual(kill.call_args_list, [
            mock.call(process.pid, signal.SIGTERM),
            mock.call(process.pid, signal.SIGTERM),
            mock.call(process.pid, signal.SIGKILL),
        ])
        process.wait.assert_called_with(timeout=2)

    def test_permanent_permission_error_is_not_ignored(self):
        process = self.process()
        denied = PermissionError(errno.EPERM, "live group is inaccessible")
        with mock.patch.object(validator.os, "killpg", side_effect=denied), \
             mock.patch.object(validator.time, "monotonic", side_effect=[0, 0, 3]), \
             mock.patch.object(validator.time, "sleep"):
            with self.assertRaises(PermissionError) as raised:
                validator.stop_group(process)
        self.assertIs(raised.exception, denied)

    def test_cleanup_reaches_child_after_leader_exits(self):
        # Wait for the child to install its SIGTERM handler before cleanup.
        # The leader exits without waiting, leaving stdout owned by its child.
        child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(300)"
        parent = "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1]])"
        process = subprocess.Popen([sys.executable, "-c", parent, child],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "child did not start")
            self.assertEqual(process.stdout.readline(), b"ready\n")
            self.assertEqual(process.wait(timeout=5), 0)
            validator.stop_group(process)
            self.assertTrue(select.select([process.stdout], [], [], 5)[0], "child still owns stdout")
            self.assertEqual(os.read(process.stdout.fileno(), 1), b"")
        finally:
            try:
                validator.stop_group(process)
            finally:
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
