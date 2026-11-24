import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from pycoder.config import Config
from pycoder.history import History
from pycoder.processes import Processes
from pycoder.tools import Tools


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.processes = Processes()

    def tearDown(self):
        self.processes.close()
        self.tmp.cleanup()

    def test_poll_and_send_input(self):
        command = [sys.executable, "-u", "-c", "print('ready'); print(input())"]
        initial = self.processes.start(command, self.root, yield_time_ms=100)
        self.assertTrue(initial["running"])
        output = self.processes.poll(initial["session_id"], "hello\n", yield_time_ms=2000)
        self.assertEqual(output["exit_code"], 0)
        self.assertIn("hello", output["output"])
        self.assertFalse(output["running"])

    def test_five_process_budget_is_shared_and_finished_slots_are_reusable(self):
        child = Processes()
        child.budget = self.processes.budget
        child.owner = "subagent"
        command = [sys.executable, "-u", "-c", "input()"]
        try:
            first = self.processes.start(command, self.root, yield_time_ms=1)
            for _ in range(4):
                child.start(command, self.root, yield_time_ms=1)
            snapshot = child.budget.snapshot()
            self.assertEqual(snapshot["active_count"], 5)
            self.assertEqual(snapshot["limit"], 5)
            self.assertEqual({p["owner"] for p in snapshot["running"]}, {"Основной агент", "subagent"})
            with self.assertRaisesRegex(ValueError, "5/5"):
                self.processes.start(command, self.root, yield_time_ms=1)
            # Finish without polling: the retained output must not occupy a live slot.
            finished = self.processes.sessions[first["session_id"]]
            finished.write("done\n")
            finished.reader.join(3)
            self.assertTrue(finished.done)
            self.assertEqual(child.budget.snapshot()["active_count"], 4)
            self.assertIn(first["session_id"], self.processes.sessions)
            extra = self.processes.start(command, self.root, yield_time_ms=1)
            self.assertTrue(extra["running"])
            self.assertEqual(child.budget.snapshot()["active_count"], 5)
            child_record = next(p for p in child.budget.snapshot()["running"] if p["owner"] == "subagent")
            child.budget.stop(child_record["id"])
            child.sessions[child_record["session_id"]].reader.join(3)
            self.assertEqual(child.budget.snapshot()["active_count"], 4)
        finally:
            child.close()

    def test_failed_launch_releases_reserved_slot(self):
        with patch("pycoder.processes.Process", side_effect=OSError("launch failed")):
            with self.assertRaises(OSError):
                self.processes.start(["nonexistent"], self.root)
        self.assertEqual(self.processes.budget.snapshot()["active_count"], 0)

    def test_timeout(self):
        command = [sys.executable, "-c", "import time; time.sleep(20)"]
        output = self.processes.start(command, self.root, timeout=1, yield_time_ms=3000)
        self.assertTrue(output["timed_out"])
        self.assertFalse(output["running"])

    def test_bounded_output_and_env_redaction(self):
        command = [sys.executable, "-c", "import os; print(os.getenv('CHUTES_API_KEY')); print(os.getenv('PYCODER_API_KEY')); print('x'*2000000)"]
        with patch.dict(os.environ, {"CHUTES_API_KEY": "secret-a", "PYCODER_API_KEY": "secret-b"}):
            output = self.processes.start(command, self.root, yield_time_ms=3000, max_output_tokens=100)
        self.assertLessEqual(len(output["output"].encode()), 400)
        self.assertNotIn("secret", output["output"])
        self.assertEqual(output["exit_code"], 0)

    @unittest.skipUnless(os.name == "posix", "POSIX PTY")
    def test_pty(self):
        command = [sys.executable, "-c", "import os; print(os.isatty(0))"]
        result = self.processes.start(command, self.root, tty=True, yield_time_ms=2000)
        self.assertIn("True", result["output"])


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.history = History(self.root / "state", self.root)
        self.tools = Tools(self.root, Config(model="test", approval="auto"), self.history,
                           lambda *a: False, lambda q: "answer", lambda text: None)

    def tearDown(self):
        self.tools.close()
        self.history.close()
        self.tmp.cleanup()

    def test_scoped_instructions_before_patch(self):
        (self.root / "src").mkdir()
        (self.root / "src/AGENTS.md").write_text("Use type hints.")
        text = "*** Begin Patch\n*** Add File: src/new.py\n+answer = 42\n*** End Patch"
        first = self.tools.apply_patch(text)
        self.assertFalse(first["applied"])
        self.assertIn("type hints", first["instructions"])
        self.assertFalse((self.root / "src/new.py").exists())
        self.assertEqual(self.tools.apply_patch(text), {"changed": ["src/new.py"]})

    def test_denied_patch_and_command(self):
        self.tools.config.approval = "never"
        self.assertTrue(self.tools.exec_command("touch should-not-exist")["denied"])
        result = self.tools.apply_patch("*** Begin Patch\n*** Add File: no\n+no\n*** End Patch")
        self.assertTrue(result["denied"])
        self.assertFalse((self.root / "no").exists())

    def test_tool_errors_return_to_model(self):
        for name, args in [("read_file", {"path": "../outside"}), ("unknown", {}),
                           ("read_file", {"path": "a", "limit": True}),
                           ("exec_command", {"cmd": "true", "unexpected": 1})]:
            with self.subTest(name=name, args=args):
                self.assertIn("error", json.loads(self.tools.execute(name, json.dumps(args))))

    def test_search_fallback(self):
        (self.root / "x.py").write_text("first\nneedle\nlast")
        with patch("pycoder.tools.shutil.which", return_value=None):
            output = self.tools.search("needle")
        self.assertEqual(output["matches"], ["x.py:2:needle"])
