import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pycoder.agent import Agent
from pycoder.config import Config
from pycoder.history import History
from pycoder.runtime import Runtime
from pycoder.tools import Tools
from pycoder.transport import Completion
from tests.support import call


class RuntimeSteeringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = Config(model="test", approval="auto")
        self.history = History(self.root / "state/sessions", self.root)
        self.tools = Tools(self.root, self.config, self.history, lambda *args: True,
                           lambda question: "yes", lambda text: None)
        self.client = Mock()
        self.runtime = Runtime(SimpleNamespace(config_dir=self.root / "config"), self.config,
                               self.root, self.root / "state")
        self.agent = Agent(self.config, self.client, self.history, self.tools, lambda text: None,
                           lambda text: None)
        self.runtime.agent = self.agent
        self.runtime.active = True
        self.agent.incoming = self.agent.before_finish = self.runtime.drain_steering

    def tearDown(self):
        self.tools.close()
        self.history.close()
        self.tmp.cleanup()

    def test_message_during_tool_cycle_reaches_next_model_request(self):
        def complete(messages, *args):
            if self.client.complete.call_count == 1:
                self.runtime.submit("Также проверь второй файл")
                return Completion({"role": "assistant", "content": None,
                    "tool_calls": [call("list_files", {}, "files")]}, {}, "tool_calls")
            self.assertEqual(messages[-1], {"role": "user", "content": "Также проверь второй файл"})
            return Completion({"role": "assistant", "content": "Готово"}, {}, "stop")

        self.client.complete.side_effect = complete
        self.assertEqual(self.agent.run("Проверь проект"), "Готово")
        self.assertEqual(self.client.complete.call_count, 2)
        self.assertTrue(self.runtime.jobs.empty())
        self.assertEqual([message["content"] for message in self.history.messages if message["role"] == "user"],
                         ["Проверь проект", "Также проверь второй файл"])

    def test_message_arriving_in_final_response_prevents_finish(self):
        def complete(messages, *args):
            if self.client.complete.call_count == 1:
                self.runtime.submit("Сначала проверь тесты")
                return Completion({"role": "assistant", "content": "Готово"}, {}, "stop")
            self.assertEqual(messages[-1]["content"], "Сначала проверь тесты")
            return Completion({"role": "assistant", "content": "Тесты проверены"}, {}, "stop")

        self.client.complete.side_effect = complete
        self.assertEqual(self.agent.run("Проверь проект"), "Тесты проверены")
        self.assertEqual(self.client.complete.call_count, 2)

    def test_cancel_discards_pending_steering(self):
        self.runtime.submit("Не выполнять после отмены")
        self.runtime.cancel()
        self.assertEqual(self.runtime.drain_steering(), [])
