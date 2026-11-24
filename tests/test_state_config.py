import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pycoder.config import Config, endpoint, get_token, load, wizard
from pycoder.history import History, groups
from pycoder.util import private_json, tokens, truncate


class ConfigTests(unittest.TestCase):
    def test_cli_unverified_override_does_not_persist_during_catalog_migration(self):
        from dataclasses import asdict
        from pycoder.cli import main
        from pycoder.transport import Client, Completion
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            private_json(config_dir / "config.json", asdict(Config(model="model", require_token=False, output_limit_source="unset")))
            observed = []
            def complete(client, *args, **kwargs):
                observed.append(client.config.e2ee_verify_attestation)
                return Completion({"role": "assistant", "content": "done"}, {}, "stop")
            with patch.object(Client, "list_models", return_value=[{"id": "model", "max_output_length": 4096}]), patch.object(Client, "complete", complete), patch("sys.stdin.isatty", return_value=False), patch("pycoder.e2ee_setup.ensure"):
                result = main(["test", "--new", "--no-auth", "--e2ee-unverified", "--config-dir", str(config_dir), "--state-dir", str(root / "state"), "--workspace", str(root)])
            self.assertEqual(result, 0)
            self.assertEqual(observed, [False])
            self.assertTrue(load(config_dir).e2ee_verify_attestation)

    def test_wizard_token_scope_and_permissions(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            directory = Path(d)
            responses = iter(["1", "1", "8192"])
            cfg = wizard(Config(), directory, input_fn=lambda _: next(responses), secret_fn=lambda _: "hidden-token",
                         fetch_models=lambda cfg, key: [{"id": "my-model", "context_length": 65536}])
            self.assertEqual(load(directory).model, "my-model")
            self.assertEqual(get_token(cfg, directory), "hidden-token")
            self.assertNotIn("hidden-token", (directory / "config.json").read_text())
            self.assertEqual((directory / "credentials.json").stat().st_mode & 0o777, 0o600)
            cfg.base_url = "https://different.example/v1"
            self.assertEqual(get_token(cfg, directory), "")

    def test_chutes_environment_token_not_sent_to_custom_server(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {"CHUTES_API_KEY": "secret"}, clear=True):
            self.assertEqual(get_token(Config(), Path(d)), "secret")
            self.assertEqual(get_token(Config(base_url="http://localhost:8000"), Path(d)), "")

    def test_custom_no_auth_wizard(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            answers = iter(["2", "http://localhost:1234/v1", "local-model", "", ""])
            cfg = wizard(Config(), Path(d), input_fn=lambda _: next(answers), secret_fn=lambda _: "-", fetch_models=lambda *a: [{"id": "local-model"}])
            self.assertFalse(cfg.require_token)
            self.assertEqual(get_token(cfg, Path(d)), "")

    def test_bounded_unicode_output(self):
        result = truncate("abcdef😀" * 1000, 100)
        self.assertLessEqual(tokens(result), 100)
        self.assertTrue(result.startswith("abcdef"))
        self.assertTrue(result.endswith("😀"))


class HistoryTests(unittest.TestCase):
    def test_resume_repair_and_compaction_journal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            history = History(root / "sessions", root)
            history.append({"role": "user", "content": "task"})
            history.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": "one", "type": "function", "function": {"name": "apply_patch", "arguments": "{}"}},
                {"id": "two", "type": "function", "function": {"name": "exec_command", "arguments": "{}"}}]})
            history.append({"role": "tool", "tool_call_id": "one", "content": "done"})
            session_id = history.session_id
            history.close()
            history = History(root / "sessions", root, session_id)
            self.assertEqual(history.messages[-1]["tool_call_id"], "two")
            self.assertIn("may have run", history.messages[-1]["content"])
            self.assertEqual(len(groups(history.messages)), 2)
            history.replace([{"role": "user", "content": "summary"}], "test compaction")
            path = history.path
            history.close()
            self.assertIn('"content": "task"', path.read_text())
            history = History(root / "sessions", root, session_id)
            self.assertEqual(history.messages, [{"role": "user", "content": "summary"}])
            history.close()

    def test_incomplete_final_record_is_discarded(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            history = History(root, root)
            session_id, path = history.session_id, history.path
            history.append({"role": "user", "content": "persisted"})
            history.close()
            with path.open("ab") as stream:
                stream.write(b'{"type":"message","data":"\xf0\x9f')
            history = History(root, root, session_id)
            self.assertEqual(history.messages[0]["content"], "persisted")
            history.append({"role": "assistant", "content": "new"})
            history.close()
            for line in path.read_text().splitlines():
                json.loads(line)

    def test_session_lock_and_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            history = History(root, root)
            session_id = history.session_id
            with self.assertRaisesRegex(ValueError, "already open"):
                History(root, root, session_id)
            history.close()
            with self.assertRaisesRegex(ValueError, "original"):
                History(root, root / "different", session_id)
