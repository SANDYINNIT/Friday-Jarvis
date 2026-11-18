"""Regression tests for the two fixes made on 2026-09-30.

1. The local edge-tts server was spawned with NO port of its own, so it
   inherited FRIDAY's own `PORT` (start_friday.cmd does `set "PORT=%1"` = 10101)
   and bound to FRIDAY's port. It was also DETACHED, so it survived FRIDAY being
   closed and kept squatting on 10101. Both are fixed in tts_bootstrap.

2. There was no pytest configuration at all, so a bare `pytest` recursed into
   .venv and the vendored interpreter and hung. pyproject.toml now sets
   testpaths + norecursedirs.
"""

import os
import sys
import tomllib
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from source.server import tts_bootstrap as tb  # noqa: E402


class TtsSpawnIsolationTests(unittest.TestCase):
    """The spawned TTS server must never take FRIDAY's port, nor outlive her."""

    def test_child_port_comes_from_base_url_not_inherited_env(self):
        # The exact failure: parent env carries PORT=10101 (FRIDAY's own port).
        with mock.patch.dict(os.environ, {"PORT": "10101"}, clear=False):
            with mock.patch("subprocess.Popen") as popen:
                popen.return_value.poll.return_value = None
                tb._spawn_server("C:/proj", "C:/py/python.exe", base_url="http://localhost:5050/v1")
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["PORT"], "5050", "child must not inherit FRIDAY's PORT")
        self.assertNotEqual(env["PORT"], "10101")

    def test_child_is_not_detached(self):
        """DETACHED_PROCESS let the server outlive FRIDAY and hold the port."""
        with mock.patch.dict(os.environ, {"PORT": "10101"}, clear=False):
            with mock.patch("subprocess.Popen") as popen:
                popen.return_value.poll.return_value = None
                tb._spawn_server("C:/proj", "C:/py/python.exe", base_url="http://localhost:5050/v1")
        flags = popen.call_args.kwargs["creationflags"]
        self.assertFalse(
            flags & getattr(__import__("subprocess"), "DETACHED_PROCESS"),
            "the TTS server must NOT be detached - it must die with FRIDAY",
        )

    def test_children_are_registered_for_cleanup(self):
        with mock.patch.dict(os.environ, {"PORT": "10101"}, clear=False):
            with mock.patch("subprocess.Popen") as popen:
                popen.return_value.poll.return_value = None
                tb._spawn_server("C:/proj", "C:/py/python.exe", base_url="http://localhost:5050/v1")
        self.assertTrue(tb._CHILDREN, "spawned servers must be tracked for atexit cleanup")

    def test_terminate_children_terminates_live_processes(self):
        class FakeProc:
            def __init__(self):
                self.terminated = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

        proc = FakeProc()
        tb._CHILDREN.append(proc)
        tb._terminate_children()
        self.assertTrue(proc.terminated, "live children must be terminated on exit")
        self.assertEqual(tb._CHILDREN, [])

    def test_spawn_accepts_an_explicit_base_url(self):
        import inspect

        params = inspect.signature(tb._spawn_server).parameters
        self.assertIn("base_url", params, "_spawn_server must accept an explicit port source")


class PytestConfigTests(unittest.TestCase):
    """`pytest` from the project root must not recurse into .venv."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
            cls.options = tomllib.load(handle)["tool"]["pytest"]["ini_options"]

    def test_testpaths_do_not_use_a_bare_dot(self):
        # A bare "." does not reliably collect the root test files.
        self.assertNotIn(".", self.options["testpaths"])

    def test_root_tests_are_collected(self):
        self.assertIn("test_*.py", self.options["testpaths"])
        self.assertIn("source/server/tests", self.options["testpaths"])

    def test_venv_and_vendored_trees_are_excluded(self):
        excluded = set(self.options["norecursedirs"])
        for required in (".venv", "tmp-oi", "openinterpreter", "site-packages"):
            self.assertIn(required, excluded, f"{required} must be excluded from collection")

    def test_configured_paths_actually_contain_tests(self):
        import glob

        self.assertTrue(glob.glob(os.path.join(ROOT, "test_*.py")),
                        "testpaths points at test_*.py but none exist")


if __name__ == "__main__":
    unittest.main()
    print("PASS: tts spawn isolation + pytest configuration")
