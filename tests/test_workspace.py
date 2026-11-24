import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from pycoder.history import History
from pycoder.workspace import WorkspaceLock, latest_session

PROJECT = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.sessions = self.root / "state/sessions"

    def tearDown(self):
        self.tmp.cleanup()

    def conversation(self, root=None, text="previous task"):
        history = History(self.sessions, root or self.workspace)
        if text:
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": "previous answer"})
            history.set_plan([{"step": "test changes", "status": "pending"}])
        session_id = history.session_id
        history.close()
        return session_id

    def test_discovery_ignores_empty_and_other_workspace_and_orders_by_activity(self):
        old = self.conversation()
        newest = self.conversation(text="newer")
        self.conversation(text="")
        self.conversation(root=self.root, text="another project")
        (self.sessions / "broken.jsonl").write_bytes(b"{bad")
        os.utime(self.sessions / (newest + ".jsonl"), ns=(1000000000, 1000000000))
        os.utime(self.sessions / (old + ".jsonl"), ns=(2000000000, 2000000000))
        self.assertEqual(latest_session(self.sessions, self.workspace).session_id, old)

    def test_lock_blocks_alias_and_releases(self):
        alias = self.root / "alias"
        alias.symlink_to(self.workspace, target_is_directory=True)
        lock = WorkspaceLock(self.workspace)
        try:
            with self.assertRaisesRegex(ValueError, "уже запущен"):
                WorkspaceLock(alias)
            other = WorkspaceLock(self.root)
            other.close()
        finally:
            lock.close()
        lock = WorkspaceLock(alias)
        lock.close()

    def test_second_process_is_rejected_before_configuration_even_with_other_state_dir(self):
        lock = WorkspaceLock(self.workspace)
        try:
            result = subprocess.run([sys.executable, str(PROJECT / "run.py"),
                                     "--workspace", str(self.workspace),
                                     "--state-dir", str(self.root / "other-state"),
                                     "--config-dir", str(self.root / "no-config")],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("уже запущен", result.stderr)
            self.assertNotIn("Missing token", result.stderr)
        finally:
            lock.close()

    def test_kernel_releases_lock_after_abrupt_process_exit(self):
        source = "from pathlib import Path; from pycoder.workspace import WorkspaceLock; import os, sys; lock=WorkspaceLock(Path(sys.argv[1])); os._exit(0)"
        result = subprocess.run([sys.executable, "-c", source, str(self.workspace)],
                                cwd=PROJECT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        lock = WorkspaceLock(self.workspace)
        lock.close()

    def launch_interactive(self, answers, *args):
        config = self.root / "config"
        config.mkdir(exist_ok=True)
        (config / "config.json").write_text(json.dumps({"model": "test", "require_token": False, "output_limit_source": "manual"}))
        env = {k: v for k, v in os.environ.items() if k not in {"PYCODER_API_KEY", "CHUTES_API_KEY"}}
        env["PYCODER_CONFIG_DIR"] = str(config)
        env["PYCODER_STATE_DIR"] = str(self.root / "state")
        harness = '''import io, sys
sys.path.insert(0, sys.argv[1])
from pycoder.cli import main
class Terminal(io.StringIO):
    def isatty(self): return True
sys.stdin = Terminal(sys.argv[2])
raise SystemExit(main(sys.argv[3:]))
'''
        return subprocess.run([sys.executable, "-c", harness, str(PROJECT), answers, *args],
                              cwd=self.workspace, env=env, capture_output=True, text=True, timeout=10)

    def test_restore_yes_keeps_messages_plan_and_same_journal(self):
        session_id = self.conversation()
        result = self.launch_interactive("\ny\n/plan\n/exit\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Восстановить контекст", result.stdout)
        self.assertIn("Контекст восстановлен", result.stderr)
        self.assertIn("test changes", result.stderr)
        self.assertEqual(len(list(self.sessions.glob("*.jsonl"))), 1)
        history = History(self.sessions, self.workspace, session_id)
        self.assertEqual(history.messages[0]["content"], "previous task")
        history.close()

    def test_restore_no_keeps_old_history_and_starts_new(self):
        session_id = self.conversation()
        old = (self.sessions / (session_id + ".jsonl")).read_bytes()
        result = self.launch_interactive("\nn\n/exit\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(list(self.sessions.glob("*.jsonl"))), 2)
        self.assertEqual(old, (self.sessions / (session_id + ".jsonl")).read_bytes())

    def test_explicit_new_skips_question(self):
        self.conversation()
        result = self.launch_interactive("\n/exit\n", "--new")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Восстановить контекст", result.stdout)
        self.assertEqual(len(list(self.sessions.glob("*.jsonl"))), 2)
