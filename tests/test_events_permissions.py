import json
from pathlib import Path
import tempfile
import threading
import shlex
import sys
import time
from types import SimpleNamespace
import unittest

from pycoder.config import Config
from pycoder.events import EventBus, Cancelled
from pycoder.history import History
from pycoder.permissions import CommandPolicy
from pycoder.tools import Tools
from pycoder.runtime import Runtime


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.history = History(self.root / "state", self.root)
        self.decisions = []
        self.tools = Tools(self.root, Config(model="test", sandbox_enabled=False), self.history,
                           lambda *args: self.decisions.append(args) or "remember", lambda q: "A", lambda _: None)
        self.tools.policy = CommandPolicy(self.root, self.root / "config")

    def tearDown(self):
        self.tools.close()
        self.history.close()
        self.tmp.cleanup()

    def test_workspace_patch_is_trusted_but_escape_and_symlink_are_rejected(self):
        self.tools.apply_patch("*** Begin Patch\n*** Add File: x\n+hello\n*** End Patch")
        self.assertEqual(self.decisions, [])
        self.assertEqual((self.root / "x").read_text(), "hello\n")
        with self.assertRaises(ValueError):
            self.tools.apply_patch("*** Begin Patch\n*** Add File: ../outside\n+no\n*** End Patch")
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.tools.read_file("link/outside")

    def test_remember_exact_command_scope_and_revoke(self):
        self.tools.exec_command("printf hello")
        self.tools.policy = CommandPolicy(self.root, self.root / "config")
        self.tools.exec_command("printf hello")
        self.assertEqual(len(self.decisions), 1)
        self.tools.exec_command("printf hello; printf other")
        self.assertEqual(len(self.decisions), 2)
        (self.root / "sub").mkdir()
        self.tools.exec_command("printf hello", workdir="sub")
        self.assertEqual(len(self.decisions), 3)
        other = CommandPolicy(self.root / "sub", self.root / "config")
        self.assertFalse(other.saved)
        self.tools.policy.clear()
        self.tools.exec_command("printf hello")
        self.assertEqual(len(self.decisions), 4)

    def test_session_permission_not_persistent_and_readonly_wins(self):
        self.tools.approve = lambda *a: "session"
        self.tools.exec_command("printf once")
        self.assertTrue(self.tools.policy.session)
        self.assertFalse(CommandPolicy(self.root, self.root / "config").session)
        self.tools.config.approval = "never"
        self.assertTrue(self.tools.exec_command("printf once")["denied"])

    def test_structured_questions_and_validation(self):
        result = self.tools.request_user_input(questions=[{"id": "choice", "question": "Which?", "options": [{"label": "A", "description": "First"}]}])
        self.assertEqual(result, {"answers": {"choice": {"answers": ["A"]}}})
        result = self.tools.execute("request_user_input", json.dumps({"questions": [{"id": "bad"}]}))
        self.assertIn("error", json.loads(result))

    def test_sleep_interrupt_and_new_context(self):
        self.tools.activity.set()
        self.assertTrue(self.tools.sleep(20000)["interrupted"])
        self.tools.cancelled.set()
        with self.assertRaises(Cancelled):
            self.tools.sleep(20000)
        self.assertTrue(self.tools.new_context()["scheduled"])

    def test_image_attachment_and_reject_external_path(self):
        (self.root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        self.assertEqual(self.tools.view_image("image.png")["mime_type"], "image/png")
        self.assertTrue(self.tools.pending_images[-1]["image_url"]["url"].startswith("data:image/png;base64,"))
        with self.assertRaises(ValueError):
            self.tools.view_image("../outside.png")

    def test_long_process_wait_can_be_cancelled(self):
        self.tools.processes.cancelled = self.tools.cancelled
        self.tools.config.approval = "auto"
        result = []
        def run():
            try:
                self.tools.exec_command(shlex.join([sys.executable, "-c", "import time; time.sleep(20)"]), yield_time_ms=30000)
            except Cancelled:
                result.append("cancelled")
        thread = threading.Thread(target=run)
        thread.start()
        time.sleep(0.2)
        self.tools.cancelled.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["cancelled"])


class EventTests(unittest.TestCase):
    def test_cancel_queued_task_does_not_cancel_next_submission(self):
        runtime = Runtime(None, None, None, None)
        runtime.tools = SimpleNamespace(compaction_requested=False, close=lambda: None)
        runtime._initialize = lambda: None
        result = []
        done = threading.Event()
        runtime._job = lambda text: (result.append(text), done.set())
        runtime.submit("old")
        runtime.cancel()
        runtime.start()
        try:
            runtime.submit("new")
            self.assertTrue(done.wait(2))
            self.assertEqual(result, ["new"])
        finally:
            runtime.stop()
            runtime.thread.join(2)
        self.assertFalse(runtime.thread.is_alive())

    def test_request_response(self):
        bus = EventBus()
        result = []
        thread = threading.Thread(target=lambda: result.append(bus.request("Choose", [("a", "A")])) )
        thread.start()
        event = bus.queue.get(timeout=1)
        self.assertEqual(event.kind, "question")
        event.data["reply"].set_result("a")
        thread.join(1)
        self.assertEqual(result, ["a"])

    def test_cancel_unblocks_question_without_approval(self):
        bus = EventBus()
        result = []
        def ask():
            try:
                bus.request("Permit?")
            except Cancelled:
                result.append("cancelled")
        thread = threading.Thread(target=ask)
        thread.start()
        event = bus.queue.get(timeout=1)
        bus.cancelled.set()
        thread.join(1)
        self.assertEqual(result, ["cancelled"])
        self.assertTrue(event.data["reply"].cancelled())
