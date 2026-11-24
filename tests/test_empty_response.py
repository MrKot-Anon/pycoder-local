import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pycoder.agent import Agent
from pycoder.config import Config
from pycoder.history import History
from pycoder.tools import Tools
from pycoder.transport import Client, EmptyResponse, ModelError, OutputLimit
from tests.support import server, json_response, reply, call


def thinking_response(finish="length", text=None):
    events = [{"choices": [{"delta": {"reasoning_content": "private thinking", "content": text}}]},
              {"choices": [{"delta": {}, "finish_reason": finish}], "usage": {"completion_tokens": 4096}}]
    return 200, "text/event-stream", "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n", {}


class EmptyResponseTests(unittest.TestCase):
    def test_length_requires_decision_without_automatic_growth(self):
        with server(lambda r: thinking_response()) as (url, requests):
            with self.assertRaises(OutputLimit) as error:
                Client(Config(model="test", base_url=url, retries=8), "").complete([])
            self.assertEqual(len(requests), 1)
            self.assertEqual(error.exception.limit, 4096)

    def test_empty_stop_and_whitespace_use_json_fallback(self):
        for text in (None, " \n"):
            with self.subTest(text=text), server(lambda r: thinking_response("stop", text) if len(r) == 1 else json_response(reply("Recovered"))) as (url, requests):
                output = []
                diagnostics = []
                client = Client(Config(model="test", base_url=url), "")
                client.on_diagnostic = diagnostics.append
                with patch.object(Client, "_delay"):
                    client.complete([], on_text=output.append)
                self.assertEqual([r["body"]["stream"] for r in requests], [True, False])
                self.assertEqual(output, ["Recovered"])
                self.assertEqual(len(diagnostics), 1)
                self.assertIsInstance(diagnostics[0], EmptyResponse)
                self.assertEqual(diagnostics[0].finish, "stop")
                self.assertNotIn("--max-output-tokens", str(diagnostics[0]))
                self.assertEqual(requests[0]["body"]["max_tokens"], requests[1]["body"]["max_tokens"])

    def test_persistent_empty_retries_until_cancel_without_reasoning_leak(self):
        from pycoder.events import Cancelled
        with server(lambda r: thinking_response("stop")) as (url, requests):
            client = Client(Config(model="test", base_url=url, retries=2), "")
            errors = []
            client.on_diagnostic = errors.append
            client.on_retry = lambda **data: client.cancel() if data["attempt"] == 5 else None
            with patch.object(client, "_delay"), self.assertRaises(Cancelled):
                client.complete([])
            self.assertEqual(len(requests), 5)
            self.assertIn("finish_reason=stop", str(errors[-1]))
            self.assertIn("reasoning_chars=16", str(errors[-1]))
            self.assertNotIn("private thinking", str(errors[-1]))

    def test_output_limit_is_not_automatically_retried(self):
        with server(lambda r: thinking_response()) as (url, requests):
            with self.assertRaises(OutputLimit):
                Client(Config(model="test", base_url=url, retries=0), "").complete([])
            self.assertEqual(len(requests), 1)

    def test_prior_tool_is_not_reexecuted_after_empty_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            history = History(root / "state", root)
            config = Config(model="test", approval="auto")
            tools = Tools(root, config, history, lambda *a: True, lambda q: "", lambda _: None)
            change = "*** Begin Patch\n*** Add File: done.txt\n+done\n*** End Patch"
            responses = [json_response(reply(None, [call("apply_patch", {"patch": change})])),
                         thinking_response(), json_response(reply("Finished"))]
            try:
                with server(lambda r: responses[len(r) - 1]) as (url, requests), patch.object(Client, "_delay"), patch.object(tools, "execute", wraps=tools.execute) as execute:
                    config.base_url = url
                    agent = Agent(config, Client(config, ""), history, tools, lambda _: None, lambda _: None)
                    agent.on_output_limit = lambda error: ("increase", error.limit * 2)
                    self.assertEqual(agent.run("Create file"), "Finished")
                    self.assertEqual(execute.call_count, 1)
                    self.assertEqual(requests[1]["body"]["messages"], requests[2]["body"]["messages"])
                    self.assertEqual((root / "done.txt").read_text(), "done\n")
            finally:
                tools.close()
                history.close()
