import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pycoder.config import Config, DEFAULT_CHUTES_MODEL, get_token, wizard
from pycoder.transport import Client, ModelError
from tests.support import json_response, server


class OnboardingTests(unittest.TestCase):
    def test_provider_key_catalog_model_order_and_kimi_default(self):
        events = []
        def answer(prompt):
            events.append("provider" if "Ваш выбор" in prompt else "model")
            return ""
        def secret(prompt):
            events.append("key")
            self.assertIn("Chutes", prompt)
            return "test-secret"
        def catalog(config, key):
            events.append("catalog")
            self.assertEqual(key, "test-secret")
            return [{"id": "other"}, {"id": DEFAULT_CHUTES_MODEL,
                    "context_length": 1048576, "max_output_length": 65535}]
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            config = wizard(Config(), Path(d), input_fn=answer, secret_fn=secret, fetch_models=catalog)
            self.assertEqual(config.model, DEFAULT_CHUTES_MODEL)
            self.assertEqual(config.context_window, 1048576)
            self.assertEqual(config.max_output_tokens, 65535)
            self.assertEqual(get_token(config, Path(d)), "test-secret")
        self.assertEqual(events, ["provider", "key", "catalog", "model"])

    def test_missing_kimi_never_silently_selects_another_model(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "Выберите модель"):
                wizard(Config(), Path(d), input_fn=lambda _: "", secret_fn=lambda _: "secret",
                       fetch_models=lambda *a: [{"id": "Kimi-K2"}])
            self.assertFalse((Path(d) / "config.json").exists())
            answers = iter(["1", "1", "8192"])
            cfg = wizard(Config(), Path(d), input_fn=lambda _: next(answers), secret_fn=lambda _: "secret",
                         fetch_models=lambda *a: [{"id": "other", "context_length": 65536}])
            self.assertEqual(cfg.model, "other")

    def test_custom_provider_missing_limit_asks_user(self):
        answers = iter(["2", "http://localhost:8000/v1", "local-model", "32768", "4096"])
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            cfg = wizard(Config(), Path(d), input_fn=lambda _: next(answers), secret_fn=lambda _: "-",
                         fetch_models=lambda *a: [{"id": "local-model"}])
            self.assertEqual(cfg.model, "local-model")
            self.assertFalse(cfg.require_token)

    def test_failed_catalog_does_not_replace_saved_settings(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ, {}, clear=True):
            path = Path(d) / "config.json"
            path.write_text('{"model":"old"}')
            def fail(*args):
                raise ModelError("HTTP 401")
            with self.assertRaises(ModelError):
                wizard(Config(), Path(d), input_fn=lambda _: "1", secret_fn=lambda _: "bad", fetch_models=fail)
            self.assertEqual(path.read_text(), '{"model":"old"}')

    def test_models_http_auth_and_redirect_policy(self):
        with server(lambda r: json_response({"data": [{"id": DEFAULT_CHUTES_MODEL}]})) as (url, requests):
            client = Client(Config(base_url=url, model="", retries=0), "secret")
            self.assertEqual(client.list_models()[0]["id"], DEFAULT_CHUTES_MODEL)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["method"], "GET")
            self.assertEqual(requests[0]["path"], "/v1/models")
            self.assertEqual(requests[0]["headers"]["Authorization"], "Bearer secret")
        with server(lambda r: (307, "text/plain", "redirect", {"Location": "https://example.org"})) as (url, requests):
            with self.assertRaisesRegex(ModelError, "307"):
                Client(Config(base_url=url, retries=0), "secret").list_models()
            self.assertEqual(len(requests), 1)

    def test_bad_catalog_and_unauthorized_key_fail_cleanly(self):
        for body in ({"data": []}, {"data": [{"id": "bad\nname"}]}, {"data": "invalid"}):
            with self.subTest(body=body), server(lambda r: json_response(body)) as (url, _):
                with self.assertRaises(ModelError):
                    Client(Config(base_url=url, retries=0), "secret").list_models()
        with server(lambda r: (401, "text/plain", "bad secret", {})) as (url, requests):
            with self.assertRaises(ModelError) as error:
                Client(Config(base_url=url), "secret").list_models()
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(len(requests), 1)
