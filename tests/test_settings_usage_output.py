import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from pycoder.config import Config, load
from pycoder.events import Cancelled
from pycoder.processes import Processes
from pycoder.settings import edit_limits
from pycoder.tool_output import OutputJournal, PAGE_BYTES, read_page
from pycoder.transport import Client, ModelError, OutputLimit
from pycoder.usage import TurnUsage, summary_text
from pycoder.cli import parser
from pycoder.runtime import Runtime
from tests.support import reply, json_response, server


class SettingsTests(unittest.TestCase):
    def test_ui_controls_apply_at_checkpoint_without_chat_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            args = parser().parse_args(["--new", "--no-auth", "--config-dir", str(root / "config")])
            # This is an offline settings test, not Chutes dependency setup.
            cfg = Config(model="test", base_url="http://127.0.0.1:8080/v1", require_token=False)
            runtime = Runtime(args, cfg, root, root / "state")
            runtime.choose = lambda *a, **kw: "continue"
            try:
                runtime._initialize()
                runtime.history.append({"role": "user", "content": "Keep my task"})
                before = list(runtime.history.messages)
                runtime.update_limits({"max_output_tokens": 12345}, save=True)
                self.assertEqual(cfg.max_output_tokens, 4096)
                runtime.agent.check_cancelled()
                self.assertEqual(cfg.max_output_tokens, 12345)
                self.assertEqual(load(args.config_dir).max_output_tokens, 12345)
                self.assertEqual(runtime.history.messages, before)
                self.assertEqual(runtime.view_state["config"]["max_output_tokens"], 12345)
                runtime.submit_action("continue")
                self.assertEqual(runtime.jobs.get_nowait()[1], {"action": "continue"})
                runtime.panel_action = "compact"
                runtime.status("Compaction progress")
                events = []
                while not runtime.bus.queue.empty():
                    events.append(runtime.bus.queue.get_nowait())
                self.assertEqual(events[-1].kind, "action_status")
                self.assertFalse(any(e.kind == "text" for e in events))
                runtime.update_limits({"max_output_tokens": 16000}, save=True)
                runtime.stop()
                runtime.apply_controls(during_shutdown=True)
                self.assertEqual(load(args.config_dir).max_output_tokens, 16000)
            finally:
                if runtime.tools:
                    runtime.tools.close()
                if runtime.history:
                    runtime.history.close()

    def test_invalid_input_then_save_and_next_request_uses_new_limit(self):
        with tempfile.TemporaryDirectory() as tmp, server(lambda r: json_response(reply())) as (url, requests):
            cfg = Config(model="test", base_url=url)
            client = Client(cfg, "")
            choices = iter(["context_window", "max_output_tokens", "save"])
            values = iter(["-4", "8192"])
            statuses = []
            self.assertTrue(edit_limits(cfg, Path(tmp), lambda *a, **kw: next(choices),
                                        lambda q: next(values), statuses.append))
            self.assertEqual(cfg.context_window, 32768)
            self.assertEqual(load(Path(tmp)).max_output_tokens, 8192)
            self.assertEqual(cfg.output_limit_source, "manual")
            self.assertTrue(any("не изменён" in text for text in statuses))
            client.complete([])
            self.assertEqual(requests[0]["body"]["max_tokens"], 8192)

    def test_cancel_does_not_apply_draft_or_write_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(model="test")
            count = 0
            def choose(*args, **kwargs):
                nonlocal count
                count += 1
                if count == 1:
                    return "max_steps"
                raise Cancelled()
            self.assertFalse(edit_limits(cfg, Path(tmp), choose, lambda q: "200", lambda s: None))
            self.assertEqual(cfg.max_steps, 100)
            self.assertFalse((Path(tmp) / "config.json").exists())


class UsageTests(unittest.TestCase):
    def test_retry_and_compaction_all_count_once(self):
        responses = [reply("", usage={"prompt_tokens": 20, "completion_tokens": 15}),
                     reply("done", usage={"prompt_tokens": 25, "completion_tokens": 10}),
                     reply("summary", usage={"prompt_tokens": 35, "completion_tokens": 12})]
        with server(lambda r: json_response(responses[len(r) - 1])) as (url, _):
            client = Client(Config(model="test", base_url=url, retries=1), "")
            usage = TurnUsage()
            client.on_usage = usage.add
            with patch.object(client, "_delay"):
                client.complete([])
            client.complete([], max_tokens=500)
            data = usage.result("done")
            self.assertEqual((data["requests"], data["input_tokens"], data["output_tokens"]), (3, 80, 37))
            self.assertTrue(data["exact"])
            self.assertIn("117 токенов", summary_text(data))

    def test_length_usage_is_counted_even_when_response_is_rejected(self):
        with server(lambda r: json_response(reply("partial", finish="length"))) as (url, _):
            client = Client(Config(model="test", base_url=url), "")
            samples = []
            client.on_usage = lambda **data: samples.append(data)
            with self.assertRaises(OutputLimit):
                client.complete([])
            self.assertEqual(samples, [dict(input_tokens=20, output_tokens=10, total_tokens=30, exact=True, unknown=False)])

    def test_partial_stream_failure_preserves_estimate(self):
        client = Client(Config(model="test", retries=0), "")
        samples = []
        client.on_usage = lambda **data: samples.append(data)
        def stop_retry(**data):
            raise ModelError(data["error"])
        client.on_retry = stop_retry
        response = io.BytesIO(b'data: {"choices":[{"delta":{"content":"abcd"}}]}\n\n')
        with patch.object(client, "_request", side_effect=lambda p, emit: client._stream(response, emit)):
            with self.assertRaises(ModelError):
                client.complete([])
        self.assertEqual(samples[0]["output_tokens"], 1)
        self.assertFalse(samples[0]["exact"])
        self.assertTrue(samples[0]["unknown"])


class OutputTests(unittest.TestCase):
    def test_live_output_arrives_before_process_finishes_and_archive_is_lossless(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            journal = OutputJournal(tmp, "call", lambda kind, **data: events.append((kind, data)))
            processes = Processes()
            try:
                cmd = [sys.executable, "-u", "-c", "print('ready'); input(); print('я'*1100000)"]
                result = processes.start(cmd, Path(tmp), yield_time_ms=200, max_output_tokens=100, journal=journal)
                self.assertTrue(result["running"])
                self.assertTrue(any("ready" in data["tail"] and not data["completed"] for _, data in events))
                result = processes.poll(result["session_id"], "go\n", yield_time_ms=3000, max_output_tokens=100)
                self.assertEqual(result["exit_code"], 0)
                self.assertLessEqual(len(result["output"].encode()), 400)
                self.assertEqual(journal.path.read_text(), "ready\n" + "я" * 1100000 + "\n")
                self.assertLessEqual(len(events[-1][1]["tail"]), 8192)
                self.assertTrue(events[-1][1]["completed"])
                self.assertEqual(journal.path.stat().st_mode & 0o777, 0o600)
            finally:
                processes.close()

    def test_paging_keeps_unicode_at_boundaries_and_last_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out"
            original = "я😀x" * PAGE_BYTES
            path.write_text(original)
            parts, offset = [], 0
            while True:
                text, start, end, size = read_page(path, offset)
                self.assertEqual(start, offset)
                self.assertGreater(end, offset)
                parts.append(text)
                if end == size:
                    break
                offset = end
            self.assertEqual("".join(parts), original)
            text, _, end, size = read_page(path, last=True)
            self.assertEqual(end, size)
            self.assertTrue(original.endswith(text))

    def test_disk_failure_is_reported_without_breaking_tool_output(self):
        with patch("pycoder.tool_output.tempfile.mkstemp", side_effect=OSError("disk full")):
            events = []
            journal = OutputJournal("/tmp", "call", lambda kind, **data: events.append(data))
            journal.append("useful output", final=True)
            self.assertEqual(events[-1]["tail"], "useful output")
            self.assertIn("не сохранён", events[-1]["error"])
