from dataclasses import replace
import contextlib
import io
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from pycoder.agent import Agent
from pycoder.config import Config, load
from pycoder.events import Cancelled
from pycoder.history import History
from pycoder.settings import edit_sandbox, network_modes, set_execution_mode
from pycoder.tools import Tools
from pycoder.sandbox import supported
from tests.support import call, json_response, reply, server


class ExecutionModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.cfg = Config(model="test", base_url="http://127.0.0.1:8080/v1")
        self.history = History(self.base / "state", self.root)
        self.addCleanup(self.history.close)
        self.questions = []
        self.tools = Tools(self.root, self.cfg, self.history,
                           lambda *args: self.questions.append(args) or False, lambda _: "", lambda _: None)
        self.addCleanup(self.tools.close)

    def test_full_access_executes_outside_workspace_without_approval_even_with_proxy(self):
        set_execution_mode(self.cfg, "full")
        self.cfg.proxy_enabled = True
        with server(lambda _: json_response({"ok": True})) as (url, requests):
            code = ("from pathlib import Path; import http.client; "
                    f"c=http.client.HTTPConnection('127.0.0.1',{urlsplit(url).port}); "
                    "c.request('GET','/'); assert c.getresponse().status==200; "
                    "Path('outside.txt').write_text('FULL_ACCESS_OK'); print('FULL_ACCESS_OK')")
            result = self.tools.exec_command(shlex.join([sys.executable, "-c", code]),
                workdir=str(self.base), sandbox_permissions="require_escalated", yield_time_ms=30000)
        self.assertEqual(result["exit_code"], 0, result)
        self.assertTrue(result["sandbox"]["full_access"])
        self.assertEqual((self.base / "outside.txt").read_text(), "FULL_ACCESS_OK")
        self.assertEqual(len(requests), 1)
        self.assertTrue(self.tools.request_network_access("localhost:1234", "")["allowed"])
        self.assertEqual(self.questions, [])

    def test_full_access_file_tools_read_outside_workspace_and_revoke(self):
        outside = self.base / "outside.txt"
        outside.write_text("GLOBAL_READ_OK\n", encoding="utf-8")
        image = self.base / "outside.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
        with self.assertRaisesRegex(ValueError, "outside the workspace"):
            self.tools.read_file(str(outside))
        set_execution_mode(self.cfg, "full")
        self.assertIn("GLOBAL_READ_OK", self.tools.read_file(str(outside))["content"])
        self.assertIn(str(outside), self.tools.list_files(str(outside))["files"])
        self.assertTrue(any("GLOBAL_READ_OK" in line for line in self.tools.search("GLOBAL_READ_OK", str(outside))["matches"]))
        self.assertEqual(self.tools.view_image(str(image))["attached"], str(image))
        set_execution_mode(self.cfg, "sandbox")
        with self.assertRaisesRegex(ValueError, "outside the workspace"):
            self.tools.read_file(str(outside))

    def test_confirm_mode_denies_command_when_not_approved_and_full_mode_can_be_revoked(self):
        set_execution_mode(self.cfg, "full")
        set_execution_mode(self.cfg, "confirm")
        self.cfg.proxy_enabled = True
        result = self.tools.exec_command("printf test")
        self.assertTrue(result["denied"])
        self.assertEqual(self.questions[0][0], "command")
        self.assertFalse(self.cfg.full_access)
        self.assertFalse(self.cfg.sandbox_enabled)

    def test_full_mode_preserves_cancellation_and_readonly(self):
        set_execution_mode(self.cfg, "full")
        self.cfg.approval = "never"
        self.assertTrue(self.tools.exec_command("echo no")["denied"])
        self.assertTrue(self.tools.request_network_access("localhost:1234", "")["denied"])
        self.cfg.approval = "auto"
        self.tools.cancelled.set()
        with self.assertRaises(Cancelled):
            self.tools.exec_command("echo no")

    def test_cli_readonly_overrides_saved_full_access_without_saving_override(self):
        from dataclasses import asdict
        from pycoder.cli import main
        from pycoder.util import private_json
        set_execution_mode(self.cfg, "full")
        directory = self.base / "config"
        private_json(directory / "config.json", asdict(self.cfg))
        responses = [json_response(reply(None, [call("exec_command", {"cmd": "printf must-not-run"})])),
                     json_response(reply("Done [[PYCODER_DONE]]"))]
        with server(lambda requests: responses[len(requests) - 1]) as (url, requests), \
                patch("sys.stdin.isatty", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = main(["--local", url, "--model", "test", "--new", "--read-only",
                           "--config-dir", str(directory), "--state-dir", str(self.base / "cli-state"),
                           "-C", str(self.root), "Check read-only"])
        self.assertEqual(result, 0)
        self.assertIn('"denied": true', requests[-1]["body"]["messages"][-1]["content"])
        self.assertTrue(load(directory).full_access)

    def test_running_child_observes_parent_mode_changes(self):
        parent = replace(self.cfg)
        self.tools.sandbox_config = lambda: parent
        set_execution_mode(parent, "full")
        result = self.tools.shell([sys.executable, "-c", "print('CHILD_OK')"],
                                  sandbox_permissions="require_escalated")
        self.assertEqual(result["exit_code"], 0, result)
        self.assertFalse(self.questions)
        agent = Agent(self.cfg, None, self.history, self.tools, lambda _: None, lambda _: None)
        self.assertIn("FULL ACCESS", agent.effective_system())
        self.assertIn('"network": "unrestricted"', agent.effective_system())
        self.assertNotIn('"network": "none"', agent.effective_system())
        set_execution_mode(parent, "confirm")
        self.assertTrue(self.tools.exec_command("printf no")["denied"])
        self.assertNotIn("FULL ACCESS", agent.effective_system())

    def test_proxy_status_reports_effective_full_access_network(self):
        from pycoder.proxy import label
        self.cfg.proxy_enabled = True
        self.cfg.sandbox_network = "none"
        set_execution_mode(self.cfg, "full")
        status = label(self.cfg)
        self.assertIn("сеть без ограничений PyCoder", status)
        self.assertNotIn("сеть запрещена", status)

    def test_plain_warning_cancel_and_persistence(self):
        answers = iter(["sandbox_enabled", "full", "no", "apply"])
        edit_sandbox(self.cfg, self.base / "config", lambda *a, **kw: next(answers), lambda _: "", lambda _: None)
        self.assertFalse(self.cfg.full_access)
        seen = []
        answers = iter(["sandbox_enabled", "full", "yes", "save"])
        def choose(*args, **kwargs):
            seen.append((args, kwargs))
            return next(answers)
        edit_sandbox(self.cfg, self.base / "config", choose, lambda _: "", lambda _: None)
        saved = load(self.base / "config")
        self.assertTrue(saved.full_access)
        self.assertFalse(saved.sandbox_enabled)
        self.assertEqual(saved.approval, "auto")
        self.assertIn("потере данных", seen[2][1]["detail"])
        self.cfg.validate()
        with self.assertRaises(ValueError):
            replace(self.cfg, sandbox_enabled=True).validate()
        with self.assertRaises(ValueError):
            replace(self.cfg, approval="ask").validate()

    @unittest.skipUnless(supported(), "Native sandbox requires macOS/Linux")
    def test_all_network_reaches_local_server_and_keeps_filesystem_sandbox(self):
        set_execution_mode(self.cfg, "sandbox")
        self.cfg.proxy_enabled = True
        self.cfg.sandbox_network = "direct"
        self.assertIn("direct", dict(network_modes(self.cfg)))
        self.assertFalse(self.cfg.sandbox_localhost)
        self.assertFalse(self.cfg.sandbox_lan)
        with server(lambda _: json_response({"ok": True})) as (url, requests):
            # Use a directory outside system temp (which the existing sandbox permits).
            with tempfile.TemporaryDirectory(prefix="pycoder-network-check-", dir=Path.home()) as outside:
                target = str(Path(outside) / "must-not-write")
                code = ("import http.client; from pathlib import Path; "
                        f"c=http.client.HTTPConnection('127.0.0.1',{urlsplit(url).port}); "
                        "c.request('GET','/'); assert c.getresponse().status==200; "
                        "print('NETWORK_OK',flush=True); "
                        f"Path({target!r}).write_text('bad')")
                result = self.tools.shell([sys.executable, "-c", code])
                self.assertIn("NETWORK_OK", result["output"])
                self.assertNotEqual(result["exit_code"], 0)
                self.assertFalse(Path(target).exists())
                self.assertEqual(len(requests), 1)
                self.assertFalse(self.questions)


class ExecutionModeUITests(unittest.IsolatedAsyncioTestCase):
    async def test_three_modes_warning_cancel_apply_and_revoke(self):
        from pycoder.tui import CoderApp, SandboxScreen, QuestionScreen
        from textual.widgets import OptionList
        from tests.test_tui import FakeRuntime
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(120, 48)) as pilot:
            await pilot.pause()
            screen = SandboxScreen(runtime.view_state["config"])
            app.push_screen(screen)
            await pilot.pause()
            await pilot.press("enter")
            self.assertEqual([key for key, _ in app.screen.options], ["sandbox", "confirm", "full"])
            await pilot.press("down", "down", "enter")
            self.assertIsInstance(app.screen, QuestionScreen)
            self.assertFalse(screen.draft.full_access)
            await pilot.press("escape")
            self.assertFalse(screen.draft.full_access)
            await pilot.press("enter", "down", "down", "enter", "down", "enter")
            self.assertTrue(screen.draft.full_access)
            self.assertFalse(runtime.view_state["config"]["full_access"])
            screen.query_one(OptionList).highlighted = next(i for i, item in enumerate(screen.options) if item[0] == "apply")
            await pilot.press("enter")
            self.assertTrue(runtime.view_state["config"]["full_access"])
            screen.query_one(OptionList).highlighted = 0
            await pilot.press("enter", "up", "enter")
            self.assertFalse(screen.draft.full_access)
            self.assertFalse(screen.draft.sandbox_enabled)
            self.assertEqual(screen.draft.approval, "ask")
