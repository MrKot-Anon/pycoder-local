import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from pycoder.agent import Agent, FINAL_MARKER, recover_text_tool_call
from pycoder.config import Config
from pycoder.history import History
from pycoder.prompts import COMPACT, SUMMARY_PREFIX
from pycoder.tools import Tools
from pycoder.transport import Client, Completion, ModelError, ProtocolError
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

    def test_textual_minimax_call_after_command_result_continues_cycle(self):
        text_call = ('\n\nПодожду результатов теста:\n\n'
                     '<invoke name="write_stdin">\n'
                     '<parameter name="session_id">1</parameter>\n'
                     '<parameter name="yield_time_ms">300000</parameter>\n'
                     '</invoke>\n</minimax:tool_call>')
        client = Mock()
        client.complete.side_effect = [
            Completion({"role": "assistant", "content": None,
                        "tool_calls": [call("exec_command", {"cmd": "python3 -c 'print(1)'"}, "start")]}, {}, "tool_calls"),
            Completion({"role": "assistant", "content": text_call, "reasoning_content": "Жду результатов."}, {}, "stop"),
            Completion({"role": "assistant", "content": "Тест завершён."}, {}, "stop")]
        self.tools.execute = Mock(side_effect=['{"running": true, "session_id": 1}',
                                               '{"running": false, "exit_code": 0, "output": "PASS"}'])
        self.assertEqual(self.agent(client).run("Run test and wait for it"), "Тест завершён.")
        self.assertEqual(client.complete.call_count, 3)
        self.assertEqual(self.tools.execute.call_args_list[1].args[0], "write_stdin")
        self.assertEqual(json.loads(self.tools.execute.call_args_list[1].args[1]),
                         {"session_id": 1, "yield_time_ms": 300000})
        self.assertEqual(self.history.messages[-3]["tool_calls"][0]["function"]["name"], "write_stdin")

    def test_textual_minimax_call_rejects_malformed_and_ignores_examples(self):
        example = 'Example: <invoke name="write_stdin"></invoke>'
        self.assertEqual(recover_text_tool_call({"content": example}), {"content": example})
        two_calls = ('Checking files. <minimax:tool_call>'
                     '<invoke name="read_file"><parameter name="path">a.py</parameter></invoke>'
                     '<invoke name="read_file"><parameter name="path">b.py</parameter></invoke>'
                     '</minimax:tool_call>')
        recovered = recover_text_tool_call({"content": two_calls})
        self.assertEqual(recovered["content"], "Checking files.")
        self.assertEqual([json.loads(item["function"]["arguments"])["path"]
                          for item in recovered["tool_calls"]], ["a.py", "b.py"])
        command = ('<minimax:tool_call><invoke name="exec_command">'
                   '<parameter name="cmd">printf "a & b" && test 2 < 3</parameter>'
                   '</invoke></minimax:tool_call>')
        recovered = recover_text_tool_call({"content": command})
        self.assertEqual(json.loads(recovered["tool_calls"][0]["function"]["arguments"])["cmd"],
                         'printf "a & b" && test 2 < 3')
        loose = recover_text_tool_call({"content": "<invoke name='write_stdin'>"
                                                  "<parameter name='session_id'>1</invoke>"})
        self.assertEqual(json.loads(loose["tool_calls"][0]["function"]["arguments"]), {"session_id": 1})
        with self.assertRaisesRegex(ProtocolError, "incomplete textual Minimax"):
            recover_text_tool_call({"content": '<invoke name="write_stdin"><parameter name="session_id">1'})

    def test_local_mode_requires_explicit_finish_after_tool_result(self):
        self.cfg.require_explicit_finish = True
        client = Mock()
        client.complete.side_effect = [
            Completion({"role": "assistant", "content": None,
                        "tool_calls": [call("list_files", {}, "files")]}, {}, "tool_calls"),
            Completion({"role": "assistant", "content": "Подожду ещё."}, {}, "stop"),
            Completion({"role": "assistant", "content": "Проверено. " + FINAL_MARKER}, {}, "stop")]
        self.assertEqual(self.agent(client).run("Inspect files"), "Проверено.")
        self.assertEqual(client.complete.call_count, 3)
        self.assertIn("did not explicitly finish", client.complete.call_args.args[0][-1]["content"])

    def test_local_mode_never_reports_done_without_final_marker(self):
        self.cfg.require_explicit_finish = True
        client = Mock()
        client.complete.return_value = Completion({"role": "assistant", "content": "Подожду ещё."}, {}, "stop")
        with self.assertRaisesRegex(ModelError, "without an explicit final marker"):
            self.agent(client).run("Run task")
        self.assertEqual(client.complete.call_count, 3)

    def test_malformed_textual_minimax_call_is_reissued_without_execution(self):
        self.cfg.require_explicit_finish = True
        malformed = '<invoke name="write_stdin"><parameter name="session_id">1'
        client = Mock()
        client.complete.side_effect = [Completion({"role": "assistant", "content": malformed}, {}, "stop"),
                                       Completion({"role": "assistant", "content": FINAL_MARKER}, {}, "stop")]
        self.tools.execute = Mock()
        self.assertEqual(self.agent(client).run("Wait for the test"), "")
        self.tools.execute.assert_not_called()
        self.assertIn("invalid tool-call syntax", client.complete.call_args.args[0][-1]["content"])

    def test_protocol_error_retries_with_feedback(self):
        self.cfg.require_explicit_finish = True
        client = Mock()
        client.complete.side_effect = [ProtocolError("Invalid model response: malformed call"),
                                       Completion({"role": "assistant", "content": "Done " + FINAL_MARKER}, {}, "stop")]
        self.assertEqual(self.agent(client).run("Complete task"), "Done")
        self.assertIn("malformed call", client.complete.call_args.args[0][-1]["content"])

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
