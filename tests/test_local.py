import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pycoder.cli import main, parser
from pycoder.config import Config, load, limit_scope
from pycoder.local import local_url, prepare_local
from tests.support import json_response, reply, server


class LocalTests(unittest.TestCase):
    def test_address_forms(self):
        for address, expected in [
            ("127.0.0.1:8080", "http://127.0.0.1:8080/v1"),
            ("model-host.example", "http://model-host.example:8080/v1"),
            ("localhost:9000/v1/", "http://localhost:9000/v1"),
            ("[::1]", "http://[::1]:8080/v1"),
            ("http://localhost", "http://localhost/v1"),
            ("https://local.test/custom/v1/chat/completions", "https://local.test/custom/v1"),
        ]:
            with self.subTest(address=address):
                self.assertEqual(local_url(address), expected)
        for address in ("", "проверь проект", "http://user:key@localhost", "localhost:99999", "http://localhost?key=x"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                local_url(address)

    def test_flags_and_prompt_separator(self):
        for flag in ("--локал", "--local"):
            args = parser().parse_args([flag, "--", "Проверь проект"])
            self.assertEqual(args.local, "127.0.0.1:8080")
            self.assertEqual(args.prompt, "Проверь проект")

    def test_local_cli_ignores_cloud_proxy_keys_limits_and_does_not_save(self):
        def respond(requests):
            if requests[-1]["method"] == "GET":
                return json_response({"data": [{"id": "minimax-m2.5", "meta": {"n_ctx": 49152}}]})
            return json_response(reply("LOCAL_OK"))
        with tempfile.TemporaryDirectory() as d, server(respond) as (url, requests):
            root = Path(d)
            config = root / "config"
            config.mkdir()
            original = json.dumps({"model": "cloud-model", "proxy_enabled": True,
                "proxy_host": "invalid-proxy", "max_output_tokens": 65000,
                "context_window": 1048576, "output_limit_source": "catalog"})
            (config / "config.json").write_text(original)
            (config / "proxy.json").write_text("broken cloud proxy configuration")
            with patch.dict(os.environ, {"PYCODER_API_KEY": "must-not-send", "CHUTES_API_KEY": "cloud-key",
                                        "HTTP_PROXY": "http://127.0.0.1:1"}), \
                    patch("sys.stdin.isatty", return_value=False), \
                    contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
                code = main(["--локал", url, "--plain", "--new", "--config-dir", str(config),
                             "--state-dir", str(root / "state"), "-C", str(root), "Скажи LOCAL_OK"])
            self.assertEqual(code, 0)
            self.assertIn("LOCAL_OK", output.getvalue())
            self.assertEqual([r["method"] for r in requests], ["GET", "POST"])
            self.assertTrue(all("Authorization" not in r["headers"] for r in requests))
            self.assertEqual(requests[-1]["body"]["model"], "minimax-m2.5")
            self.assertLessEqual(requests[-1]["body"]["max_tokens"], 4096)
            self.assertEqual((config / "config.json").read_text(), original)
            self.assertFalse((config / "credentials.json").exists())
            with self.assertRaises(ValueError):
                load(config)  # Ordinary mode still rejects a broken proxy file.

    def test_catalog_context_and_explicit_overrides(self):
        with server(lambda _: json_response({"data": [{"id": "minimax-m2.5", "meta": {"n_ctx": 49152}}]})) as (url, _):
            args = parser().parse_args(["--local", url])
            cfg = Config(model="old")
            prepare_local(cfg, args, lambda _: None)
            self.assertEqual(cfg.context_window, 49152)
            self.assertEqual(cfg.model, "minimax-m2.5")
        args = parser().parse_args(["--local", "--model", "explicit", "--context-window", "16384",
                                    "--max-output-tokens", "8192"])
        with patch("pycoder.local.Client.list_models", side_effect=AssertionError("No discovery with explicit model")):
            prepare_local(cfg, args, lambda _: None)
        self.assertEqual((cfg.model, cfg.context_window, cfg.max_output_tokens), ("explicit", 16384, 8192))

    def test_local_saved_output_limit_survives_restart_for_same_model(self):
        from dataclasses import asdict
        from pycoder.util import private_json
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            args = parser().parse_args(["--local", "127.0.0.1:8080", "--model", "test"])
            config = Config()
            prepare_local(config, args, lambda _: None)
            config.max_output_tokens = 12000
            config.output_limit_scope = limit_scope(config)
            private_json(directory / "config.json", asdict(config))
            restarted = load(directory, include_proxy=False)
            prepare_local(restarted, args, lambda _: None)
            self.assertEqual(restarted.max_output_tokens, 12000)
            different = parser().parse_args(["--local", "127.0.0.1:8080", "--model", "other"])
            prepare_local(restarted, different, lambda _: None)
            self.assertEqual(restarted.max_output_tokens, Config().max_output_tokens)

    def test_ambiguous_catalog_requires_explicit_model(self):
        with server(lambda _: json_response({"data": [{"id": "a"}, {"id": "b"}]})) as (url, _):
            with self.assertRaisesRegex(ValueError, "--model"):
                prepare_local(Config(), parser().parse_args(["--local", url]), lambda _: None)

    def test_conflicting_setup_options_fail_before_network(self):
        for extra in (["--base-url", "http://other"], ["--configure"]):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                prepare_local(Config(), parser().parse_args(["--local", *extra]), lambda _: None)

    def test_local_tui_initialization_skips_model_and_token_questions(self):
        from pycoder.runtime import Runtime
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve()
            args = parser().parse_args(["--local", "--model", "minimax-m2.5", "--new",
                                        "--config-dir", str(root / "config")])
            cfg = Config()
            prepare_local(cfg, args, lambda _: None)
            runtime = Runtime(args, cfg, root, root / "state")
            try:
                with patch.object(runtime, "choose", side_effect=AssertionError("Unexpected startup question")), \
                        patch.dict(os.environ, {"PYCODER_API_KEY": "must-not-send"}):
                    runtime._initialize()
                self.assertEqual(runtime.client.token, "")
                self.assertEqual(runtime.client.config.model, "minimax-m2.5")
                self.assertFalse((root / "config" / "config.json").exists())
            finally:
                if runtime.team:
                    runtime.team.close()
                if runtime.tools:
                    runtime.tools.close()
                if runtime.client:
                    runtime.client.close()
                if runtime.history:
                    runtime.history.close()
