import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from pycoder.agent import Agent
from pycoder.config import Config
from pycoder.history import History
from pycoder.prompts import COMPACT, SUMMARY_PREFIX
from pycoder.tools import Tools
from pycoder.transport import Client, Completion, ModelError
from tests.support import call, json_response, reply, server


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = Config(model="test", context_window=16384, max_output_tokens=1024, approval="auto", retries=0)
        self.history = History(self.root / "state", self.root)
        self.tools = Tools(self.root, self.cfg, self.history, lambda *a: True, lambda q: "yes", lambda t: None)

    def tearDown(self):
        self.tools.close()
        self.history.close()
        self.tmp.cleanup()

    def agent(self, client):
        return Agent(self.cfg, client, self.history, self.tools, lambda t: None, lambda t: None)

    def test_complete_coding_loop_with_real_http_and_tools(self):
        (self.root / "calc.py").write_text("answer = 1\n")
        patch_text = "*** Begin Patch\n*** Update File: calc.py\n@@\n-answer = 1\n+answer = 42\n*** End Patch"
        responses = [reply(None, [call("read_file", {"path": "calc.py"}, "read")]),
                     reply(None, [call("apply_patch", {"patch": patch_text}, "patch")]),
                     reply(None, [call("shell", {"command": [sys.executable, "-c", "from calc import answer; assert answer == 42; print('PASS')"]}, "test")]),
                     reply("Fixed and tested.")]
        with server(lambda requests: json_response(responses[len(requests) - 1])) as (url, requests):
            self.cfg.base_url = url
            answer = self.agent(Client(self.cfg, "")).run("Fix answer to be 42 and test it")
            self.assertEqual(answer, "Fixed and tested.")
            self.assertEqual(len(requests), 4)
            self.assertIn("PASS", requests[-1]["body"]["messages"][-1]["content"])
            for request in requests:
                self.assertEqual(request["path"], "/v1/chat/completions")
        self.assertEqual((self.root / "calc.py").read_text(), "answer = 42\n")

    def test_same_tool_error_with_different_arguments_stops(self):
        client = Mock()
        client.complete.side_effect = [Completion({"role": "assistant", "content": "",
            "tool_calls": [call("read_file", {"path": "missing", "offset": i}, str(i))]}, {}, "tool_calls")
            for i in range(3)]
        with self.assertRaisesRegex(ModelError, "три раза подряд"):
            self.agent(client).run("Read file")
        self.assertEqual(client.complete.call_count, 3)
        self.assertTrue(any("same tool error occurred twice" in str(m.get("content"))
                            for m in self.history.messages))
        self.assertEqual(sum(m["role"] == "tool" for m in self.history.messages), 3)

    def test_partial_retry_never_executes_aborted_tools_or_saves_partial_text(self):
        partial = 'data: ' + json.dumps({"choices": [{"delta": {"content": "aborted text",
            "tool_calls": [{"index": 0, "id": "aborted", "type": "function", "function": {
                "name": "apply_patch", "arguments": json.dumps({"patch":
                    "*** Begin Patch\n*** Add File: must-not-exist\n+bad\n*** End Patch"})}}]}}]}) + '\n\n'
        responses = [json_response(reply(None, [call("list_files", {}, "previous")])),
                     (200, "text/event-stream", partial, {}), json_response(reply("Recovered"))]
        self.cfg.retries = 1
        with server(lambda r: responses[len(r) - 1]) as (url, requests), patch.object(Client, "_delay"), \
                patch.object(self.tools, "execute", wraps=self.tools.execute) as execute:
            self.cfg.base_url = url
            self.assertEqual(self.agent(Client(self.cfg, "")).run("Inspect"), "Recovered")
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(requests[1]["body"]["messages"], requests[2]["body"]["messages"])
        self.assertFalse((self.root / "must-not-exist").exists())
        self.assertFalse(any(m.get("content") == "aborted text" for m in self.history.messages))

    def test_tool_success_resets_error_streak(self):
        client = Mock()
        calls = [call("read_file", {"path": "missing", "offset": 1}, "1"),
                 call("read_file", {"path": "missing", "offset": 2}, "2"),
                 call("list_files", {}, "3"),
                 call("read_file", {"path": "missing", "offset": 3}, "4")]
        client.complete.side_effect = [Completion({"role": "assistant", "content": "", "tool_calls": [c]}, {}, "tool_calls")
                                       for c in calls] + [Completion({"role": "assistant", "content": "done"}, {}, "stop")]
        self.assertEqual(self.agent(client).run("Inspect"), "done")

    def test_text_after_list_files_with_null_tool_calls(self):
        calls = [call("list_files", {}, "listing")]
        events = [{"choices": [{"delta": {"content": c, "tool_calls": None}}]}
                  for c in "Работа завершена"]
        events.append({"choices": [{"delta": None, "finish_reason": "stop"}], "usage": None})
        sse = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        def respond(requests):
            if len(requests) == 1:
                return json_response(reply(None, calls))
            return 200, "text/event-stream", sse, {}
        with server(respond) as (url, requests):
            self.cfg.base_url = url
            result = self.agent(Client(self.cfg, "")).run("Посмотри список файлов")
            self.assertEqual(result, "Работа завершена")
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[1]["body"]["messages"][-1]["role"], "tool")
            self.assertEqual(self.history.messages[-1]["content"], "Работа завершена")

    def test_compaction_preserves_user_constraints_and_journal(self):
        self.history.append({"role": "user", "content": "Do not change public API"})
        for i in range(8):
            self.history.append({"role": "assistant", "content": "detail " * 2000})
        self.history.append({"role": "user", "content": "Now fix the tests"})
        client = Mock()
        client.complete.return_value = Completion({"role": "assistant", "content": "API preserved; fix test x next."}, {}, "stop")
        agent = self.agent(client)
        before = agent.estimate()
        agent.compact()
        self.assertLess(agent.estimate(), before)
        self.assertEqual(self.history.messages[0]["content"], "Do not change public API")
        self.assertTrue(self.history.messages[-1]["content"].startswith(SUMMARY_PREFIX))
        self.assertIn("detail ", self.history.path.read_text())
        request = client.complete.call_args.args[0]
        self.assertEqual(request[-1]["content"], COMPACT)
        self.assertNotIn("tools", client.complete.call_args.kwargs)

    def test_compaction_failure_preserves_history(self):
        self.history.append({"role": "user", "content": "task"})
        self.history.append({"role": "assistant", "content": "data " * 3000})
        original = list(self.history.messages)
        client = Mock()
        client.complete.return_value = Completion({"role": "assistant", "content": "cut off"}, {}, "length")
        with self.assertRaises(ModelError):
            self.agent(client).compact()
        self.assertEqual(original, self.history.messages)

    def test_compaction_overflow_removes_whole_tool_groups(self):
        self.history.append({"role": "user", "content": "old task"})
        self.history.append({"role": "assistant", "content": None, "tool_calls": [call("read_file", {"path": "a"})]})
        self.history.append({"role": "tool", "tool_call_id": "call_1", "content": "long " * 1000})
        self.history.append({"role": "user", "content": "latest constraint"})
        client = Mock()
        client.complete.side_effect = [ModelError("context", context_overflow=True),
                                      ModelError("context", context_overflow=True),
                                      Completion({"role": "assistant", "content": "summary"}, {}, "stop")]
        self.agent(client).compact()
        request = client.complete.call_args.args[0]
        self.assertFalse(any(m["role"] == "tool" for m in request))
        self.assertIn("latest constraint", str(request))

    def test_interrupt_repairs_unfinished_tool_calls(self):
        client = Mock()
        client.complete.return_value = Completion({"role": "assistant", "content": None,
                                                   "tool_calls": [call("exec_command", {"cmd": "true"})]}, {}, "tool_calls")
        self.tools.execute = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.agent(client).run("task")
        self.assertEqual(self.history.messages[-1]["role"], "tool")
        self.assertIn("may have run", self.history.messages[-1]["content"])

    def test_image_is_attached_after_tool_results_and_can_be_compacted(self):
        (self.root / "image.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        responses = [reply(None, [call("view_image", {"path": "image.png"})]), reply("Image received")]
        with server(lambda r: json_response(responses[len(r) - 1])) as (url, requests):
            self.cfg.base_url = url
            agent = self.agent(Client(self.cfg, ""))
            agent.run("Inspect image")
            messages = requests[-1]["body"]["messages"]
            self.assertEqual(messages[-2]["role"], "tool")
            self.assertEqual(messages[-1]["content"][-1]["type"], "image_url")
        self.history.append({"role": "assistant", "content": "detail " * 3000})
        client = Mock()
        client.complete.return_value = Completion({"role": "assistant", "content": "Inspected local image."}, {}, "stop")
        self.agent(client).compact()
        self.assertFalse(any(isinstance(m.get("content"), list) for m in self.history.messages))

    def test_cli_one_shot_custom_url_without_credentials(self):
        with server(lambda r: json_response(reply("CLI works"))) as (url, requests):
            env = {k: v for k, v in os.environ.items() if k not in {"CHUTES_API_KEY", "PYCODER_API_KEY"}}
            result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "run.py"),
                                     "--workspace", str(self.root), "--config-dir", str(self.root / "config"),
                                     "--state-dir", str(self.root / "cli-state"), "--base-url", url,
                                     "--model", "test", "--max-output-tokens", "4096", "--no-auth", "hello"],
                                    env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CLI works", result.stdout)
            self.assertEqual(len(requests), 1)
            self.assertNotIn("Authorization", requests[0]["headers"])
