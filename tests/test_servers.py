import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from pycoder.config import Config
from pycoder.events import Cancelled
from pycoder.servers import Servers, ServerSelectionError
from pycoder.transport import Client, Completion, ModelError


def instances(*ids):
    return [{"instance_id": identifier, "e2e_pubkey": base64.b64encode(bytes(1184)).decode(),
             "nonces": [identifier + str(i) for i in range(30)]} for identifier in ids]


class ServerTests(unittest.TestCase):
    def client(self, *ids):
        client = Client(Config(model="test", e2ee_verify_attestation=False), "")
        def metadata(host, path):
            if path == "/v1/models":
                return {"data": [{"id": "test", "chute_id": "chute", "confidential_compute": True}]}
            return {"instances": instances(*ids), "nonce_expires_in": 60}
        client._encryption_metadata = metadata
        crypto = patch("pycoder.e2ee.crypto", return_value=SimpleNamespace(
            build_e2ee_request=lambda key, payload: SimpleNamespace(blob=b"ciphertext", response_sk=b"secret")))
        crypto.start()
        self.addCleanup(crypto.stop)
        delay = patch.object(client, "_delay")
        self.delay = delay.start()
        self.addCleanup(delay.stop)
        return client

    def test_manual_selection_statistics_and_persistence_without_secrets(self):
        client = self.client("a", "b")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stats.json"
            client.servers = Servers(path)
            client.servers.publish("chute", instances("a", "b"), time.monotonic() + 50)
            client.servers.choose("b")
            send = Mock(return_value=Completion({"content": "ok"}, {}, "stop"))
            with patch.object(client, "_http_request", send):
                client.complete([])
            self.assertEqual(send.call_args.kwargs["encrypted"].instance_id, "b")
            rows = {r["id"]: r for r in client.servers.snapshot()["rows"]}
            self.assertEqual(rows["b"]["success"], 1)
            self.assertEqual(rows["b"]["attempts"], 1)
            self.assertNotIn("attempts", rows["a"])
            saved = path.read_text()
            for secret in ("nonces", "pubkey", "secret"):
                self.assertNotIn(secret, saved)
            restored = Servers(path).snapshot()
            self.assertEqual(next(r for r in restored["rows"] if r["id"] == "b")["success"], 1)
            self.assertFalse(any(r["available"] for r in restored["rows"]))

    def test_partial_actions_include_reasoning_and_tool_fragments(self):
        for action in ("stop", "stop_switch", "switch_retry"):
            with self.subTest(action=action):
                client = self.client("a", "b")
                client.servers.set_option("partial_action", action)
                used, usage = [], []
                client.on_usage = lambda **data: usage.append(data)
                def send(payload, emit, **kwargs):
                    used.append(kwargs["encrypted"].instance_id)
                    if len(used) == 1:
                        data = {"choices": [{"delta": {"reasoning_content": "thinking",
                            "tool_calls": [{"index": 0, "function": {"name": "shell", "arguments": "{}"}}]}}]}
                        return client._stream(io.BytesIO(("data: " + json.dumps(data) + "\n\n").encode()), emit)
                    return Completion({"content": "ok"}, {}, "stop")
                with patch.object(client, "_http_request", side_effect=send):
                    if action == "switch_retry":
                        self.assertEqual(client.complete([]).message["content"], "ok")
                        self.assertEqual(used, ["a", "b"])
                    else:
                        with self.assertRaisesRegex(ModelError, "неполный ответ"):
                            client.complete([])
                        self.assertEqual(used, ["a"])
                rows = {r["id"]: r for r in client.servers.snapshot()["rows"]}
                self.assertEqual(rows["a"]["errors"], 1)
                self.assertEqual(rows["a"]["interrupted"], 1)
                self.assertGreater(usage[0]["output_tokens"], 0)
                self.assertTrue(usage[0]["unknown"])
                chosen = client.servers.order(instances("a", "b"))[0]["instance_id"]
                self.assertEqual(chosen, "a" if action == "stop" else "b")

    def test_per_server_and_total_retry_limits(self):
        client = self.client("a", "b", "c", "d", "e", "f")
        used = []
        def send(payload, emit, **kwargs):
            used.append(kwargs["encrypted"].instance_id)
            raise ModelError("capacity", retryable=True)
        with patch.object(client, "_http_request", side_effect=send):
            with self.assertRaisesRegex(ModelError, "общий лимит повторов: 10"):
                client.complete([])
        self.assertEqual(used, ["a", "a", "b", "b", "c", "c", "d", "d", "e", "e", "f"])
        self.assertEqual(self.delay.call_count, 10)
        self.assertEqual(sum(r.get("errors", 0) for r in client.servers.snapshot()["rows"]), 11)

    def test_cycle_restarts_per_server_allowance_but_not_total_budget(self):
        client = self.client("a", "b")
        client.servers.set_option("exhausted_action", "cycle")
        used = []
        def send(payload, emit, **kwargs):
            used.append(kwargs["encrypted"].instance_id)
            raise ModelError("capacity", retryable=True)
        with patch.object(client, "_http_request", side_effect=send):
            with self.assertRaisesRegex(ModelError, "общий лимит повторов: 10"):
                client.complete([])
        self.assertEqual(used, ["a", "a", "b", "b", "a", "a", "b", "b", "a", "a", "b"])
        self.assertEqual(self.delay.call_count, 10)

    def test_cycle_can_recover_after_all_servers_fail(self):
        client = self.client("a", "b")
        client.servers.set_option("exhausted_action", "cycle")
        client.servers.set_option("retries_per_server", 0)
        used = []
        def send(payload, emit, **kwargs):
            used.append(kwargs["encrypted"].instance_id)
            if len(used) < 3:
                raise ModelError("offline", retryable=True)
            return Completion({"content": "recovered"}, {}, "stop")
        with patch.object(client, "_http_request", side_effect=send):
            self.assertEqual(client.complete([]).message["content"], "recovered")
        self.assertEqual(used, ["a", "b", "a"])
        self.assertEqual(sum(r.get("errors", 0) for r in client.servers.snapshot()["rows"]), 2)

    def test_cycle_handles_existing_cooldown_but_not_absent_servers(self):
        registry = Servers()
        registry.set_option("exhausted_action", "cycle")
        registry.exclude("a")
        self.assertEqual(registry.order(instances("a"))[0]["instance_id"], "a")
        with self.assertRaises(ServerSelectionError):
            registry.order([])
        registry.publish("chute", instances("a"), time.monotonic() + 50)
        registry.choose("a")
        with self.assertRaisesRegex(ServerSelectionError, "Выбранный сервер отсутствует"):
            registry.order(instances("b"))

    def test_cycle_does_not_override_partial_stop_or_disabled_switch(self):
        for action in ("stop", "stop_switch"):
            client = self.client("a")
            client.servers.set_option("exhausted_action", "cycle")
            client.servers.set_option("partial_action", action)
            def send(payload, emit, **kwargs):
                client._progress("partial")
                raise ModelError("incomplete", retryable=True)
            with patch.object(client, "_http_request", side_effect=send) as request:
                with self.assertRaisesRegex(ModelError, "неполный ответ"):
                    client.complete([])
                self.assertEqual(request.call_count, 1)
        client = self.client("a", "b")
        client.servers.set_option("exhausted_action", "cycle")
        client.servers.set_option("auto_switch", False)
        with patch.object(client, "_http_request", side_effect=ModelError("offline", retryable=True)) as request:
            with self.assertRaisesRegex(ModelError, "автоматическая смена выключена"):
                client.complete([])
            self.assertEqual(request.call_count, 2)

    def test_disabled_switch_retries_same_server_only_and_zero_total(self):
        for total, expected in ((10, 2), (0, 1)):
            client = self.client("a", "b")
            client.servers.set_option("auto_switch", False)
            client.servers.set_option("total_retries", total)
            with patch.object(client, "_http_request", side_effect=ModelError("offline", retryable=True)) as send:
                with self.assertRaises(ModelError):
                    client.complete([])
            self.assertEqual(send.call_count, expected)
            self.assertEqual({c.kwargs["encrypted"].instance_id for c in send.call_args_list}, {"a"})

    def test_cancel_is_not_server_error(self):
        client = self.client("a")
        with patch.object(client, "_http_request", side_effect=Cancelled()):
            with self.assertRaises(Cancelled):
                client.complete([])
        row = client.servers.snapshot()["rows"][0]
        self.assertEqual(row["cancelled"], 1)
        self.assertEqual(row["active"], 0)
        self.assertNotIn("errors", row)

    def test_shared_nonce_pool_never_reuses_tokens(self):
        registry = Servers()
        item = instances("a")[0]
        tokens = []
        def consume():
            try:
                tokens.append(registry.nonce(item))
            except ServerSelectionError:
                pass
        threads = [threading.Thread(target=consume) for _ in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(tokens), 30)
        self.assertEqual(len(set(tokens)), 30)

    def test_refreshed_pool_used_and_missing_selection_stops(self):
        client = self.client("a")
        client.servers.publish("chute", instances("b"), time.monotonic() + 50)
        client.servers.choose("b")
        with patch.object(client, "_http_request", return_value=Completion({"content": "ok"}, {}, "stop")) as send:
            client.complete([])
            self.assertEqual(send.call_args.kwargs["encrypted"].instance_id, "b")
            client.servers.publish("chute", instances("c"), time.monotonic() + 50)
            with self.assertRaisesRegex(ModelError, "Выбранный сервер отсутствует"):
                client.complete([])
            self.assertEqual(send.call_count, 1)

    def test_refresh_uses_separate_transport_and_does_not_generate(self):
        from pycoder.runtime import Runtime
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = Config(model="test", proxy_enabled=True)
            runtime = Runtime(SimpleNamespace(config_dir=root), cfg, root, root)
            runtime.client = Client(cfg, "secret")
            runtime.agent = object()
            old = runtime.client
            old._encryption = object()  # Represents a concurrent generation's crypto state.
            entered, release = threading.Event(), threading.Event()
            seen = []
            def metadata(client, host, path):
                seen.append((client, host, path))
                self.assertTrue(client.config.proxy_enabled)
                self.assertIsNot(client, old)
                if path == "/v1/models":
                    entered.set()
                    release.wait(3)
                    return {"data": [{"id": "test", "chute_id": "chute", "confidential_compute": True}]}
                return {"instances": instances("new"), "nonce_expires_in": 60}
            with patch.object(Client, "_encryption_metadata", metadata), patch.object(Client, "_http_request") as generation:
                runtime.refresh_servers()
                self.assertTrue(entered.wait(2))
                self.assertTrue(runtime.server_state()["refreshing"])
                runtime.refresh_servers()  # Duplicate clicks must not issue a second refresh.
                release.set()
                deadline = time.monotonic() + 3
                while runtime.server_state()["refreshing"] and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(runtime.server_state()["refreshing"])
                generation.assert_not_called()
            self.assertEqual(len(seen), 2)
            self.assertIs(runtime.client, old)
            self.assertFalse(old.cancelled.is_set())
            self.assertEqual(runtime.server_state()["rows"][0]["id"], "new")

    def test_retry_exclusion_survives_cooldown_expiry(self):
        client = self.client("a", "b")
        client.servers.set_option("retries_per_server", 0)
        used = []
        def send(payload, emit, **kwargs):
            used.append(kwargs["encrypted"].instance_id)
            # Simulate a long attestation between attempts: cooldown alone is insufficient.
            for row in client.servers.rows.values():
                row["failed_until"] = 0
            raise ModelError("offline", retryable=True)
        with patch.object(client, "_http_request", side_effect=send):
            with self.assertRaisesRegex(ModelError, "Нет доступного сервера"):
                client.complete([])
        self.assertEqual(used, ["a", "b"])

    def test_metadata_retries_have_same_total_budget(self):
        client = self.client("a")
        client.servers.set_option("total_retries", 2)
        client._encryption_metadata = Mock(side_effect=ModelError("metadata offline", retryable=True))
        with self.assertRaisesRegex(ModelError, "общий лимит повторов: 2"):
            client.complete([])
        self.assertEqual(client._encryption_metadata.call_count, 3)
        self.assertEqual(client.servers.snapshot()["rows"], [])

    def test_options_validate_and_persist(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Servers(Path(directory) / "stats.json")
            for name, value in (("total_retries", -1), ("retries_per_server", True), ("partial_action", "bad"), ("exhausted_action", "bad")):
                with self.assertRaises(ValueError):
                    registry.set_option(name, value)
            registry.set_option("partial_action", "switch_retry")
            registry.set_option("exhausted_action", "cycle")
            registry.set_option("total_retries", 7)
            registry.set_option("retries_per_server", 2)
            registry.set_option("auto_switch", False)
            state = Servers(registry.path).snapshot()
            self.assertEqual(state["total_retries"], 7)
            self.assertEqual(state["retries_per_server"], 2)
            self.assertEqual(state["partial_action"], "switch_retry")
            self.assertEqual(state["exhausted_action"], "cycle")
            self.assertFalse(state["auto_switch"])
