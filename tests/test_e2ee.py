import base64
import copy
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from pycoder.attestation import check_report
from pycoder.config import Config
from pycoder.e2ee import ChutesEncryption, DecryptedStream, EncryptionError, Envelope, fallback_question
from pycoder.events import Cancelled
from pycoder.transport import Client, Completion, ModelError


class AttestationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.nonce, self.key = "ab" * 32, "test-public-key"
        binding = hashlib.sha256((self.nonce + self.key).encode()).hexdigest()
        self.report = {"status": "UpToDate", "report": {"TD10": {
            "td_attributes": "00" * 8, "report_data": binding + "00" * 32,
            "mr_td": "12" * 48}}}

    def test_requires_valid_status_debug_off_and_bound_key(self):
        binding, mrtd = check_report(self.report, self.nonce, self.key, ["12" * 48])
        self.assertEqual(mrtd, "12" * 48)
        self.assertEqual(len(binding), 64)
        cases = []
        for field, value in [("td_attributes", "01" + "00" * 7),
                             ("td_attributes", ""), ("report_data", "00" * 64),
                             ("mr_td", "")]:
            bad = copy.deepcopy(self.report)
            bad["report"]["TD10"][field] = value
            cases.append(bad)
        bad = copy.deepcopy(self.report)
        bad["status"] = "OutOfDate"
        cases.append(bad)
        for bad in cases:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_report(bad, self.nonce, self.key)
        for nonce, key, pins in [("cd" * 32, self.key, []),
                                 (self.nonce, "substituted", []),
                                 (self.nonce, self.key, ["34" * 48])]:
            with self.assertRaises(ValueError):
                check_report(self.report, nonce, key, pins)


class EncryptionPolicyTests(unittest.TestCase):
    def test_transient_metadata_errors_retry_without_downgrade(self):
        for status, header, expected in [(429, "17", 1), (429, None, 1), (503, None, 1)]:
            with self.subTest(status=status, header=header):
                client = Client(Config(model="test", retries=1), "secret")
                client.on_encryption_failure = Mock(return_value="plain_once")
                client._encryption = Mock()
                def prepare(*args):
                    client._encryption_metadata("api.chutes.ai", "/e2e/instances/test")
                    return Envelope(b"ciphertext", b"key", {}, "instance")
                client._encryption.prepare.side_effect = prepare
                limited = Mock(status=status)
                limited.read.return_value = b'{}'
                limited.getheader.return_value = header
                success = Mock(status=200)
                success.read.return_value = b'{}'
                connection = Mock()
                connection.getresponse.side_effect = [limited, success]
                with patch("pycoder.transport.http.client.HTTPSConnection", return_value=connection), \
                        patch.object(client, "_delay") as delay, \
                        patch.object(client, "_http_request", return_value=Completion({"content": "ok"}, {}, "stop")) as send:
                    self.assertEqual(client.complete([]).message["content"], "ok")
                    delay.assert_called_once_with(expected)
                    self.assertIsInstance(send.call_args.kwargs["encrypted"], Envelope)
                client.on_encryption_failure.assert_not_called()

    def test_metadata_rate_limit_exhaustion_and_cancel_never_offer_plain(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                client = Client(Config(model="test", retries=1), "secret")
                client.on_encryption_failure = Mock(return_value="plain_once")
                client._encryption = Mock()
                client._encryption.prepare.side_effect = lambda *args: client._encryption_metadata("api.chutes.ai", "/e2e/instances/test")
                response = Mock(status=429)
                response.read.return_value = b'{}'
                response.getheader.return_value = None
                connection = Mock()
                connection.getresponse.return_value = response
                if cancel:
                    client.on_retry = lambda **data: client.cancel()
                with patch("pycoder.transport.http.client.HTTPSConnection", return_value=connection), \
                        patch.object(client, "_http_request") as send:
                    if cancel:
                        with self.assertRaises(Cancelled):
                            client.complete([])
                    else:
                        client.on_retry = lambda **data: client.cancel() if data["attempt"] == 6 else None
                        with patch.object(client, "_delay"), self.assertRaises(Cancelled):
                            client.complete([])
                        self.assertEqual(connection.request.call_count, 6)
                send.assert_not_called()
                client.on_encryption_failure.assert_not_called()

    def test_retry_after_http_date_and_invalid_header(self):
        from pycoder.transport import retry_after_seconds
        with patch("pycoder.transport.time.time", return_value=0):
            self.assertEqual(retry_after_seconds("Thu, 01 Jan 1970 00:02:00 GMT", 60), 120)
        self.assertEqual(retry_after_seconds("invalid", 60), 60)

    def client(self):
        client = Client(Config(model="test", retries=0), "secret")
        client._encryption = Mock()
        client._encryption.prepare.side_effect = EncryptionError("attestation failed")
        return client

    def test_default_failure_never_sends_prompt(self):
        client = self.client()
        states = []
        client.on_security = lambda **data: states.append(data["mode"])
        with patch.object(client, "_http_request") as send, self.assertRaises(ModelError):
            client.complete([{"role": "user", "content": "private"}])
        send.assert_not_called()
        self.assertEqual(states, ["checking", "blocked"])

    def test_downgrade_is_explicit_and_one_request_only(self):
        client = self.client()
        decisions = Mock(side_effect=["plain_once", "stop"])
        client.on_encryption_failure = decisions
        success = Completion({"role": "assistant", "content": "ok"}, {}, "stop")
        with patch.object(client, "_http_request", return_value=success) as send:
            self.assertEqual(client.complete([]).message["content"], "ok")
            with self.assertRaises(ModelError):
                client.complete([])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(decisions.call_count, 2)
        self.assertNotIn("encrypted", send.call_args.kwargs)

    def test_plain_consent_does_not_authorize_automatic_retries(self):
        client = self.client()
        client.config.retries = 3
        client.on_encryption_failure = Mock(return_value="plain_once")
        with patch.object(client, "_http_request", side_effect=ModelError("network", retryable=True)) as send:
            with self.assertRaises(ModelError):
                client.complete([])
        self.assertEqual(send.call_count, 1)

    def test_partial_encrypted_response_never_prompts_to_replay(self):
        client = self.client()
        client._encryption.prepare.side_effect = None
        client.on_encryption_failure = Mock()
        def partial(*args, **kwargs):
            client._progress("partial")
            raise EncryptionError("tampered")
        with patch.object(client, "_http_request", side_effect=partial), self.assertRaises(ModelError):
            client.complete([])
        client.on_encryption_failure.assert_not_called()

    def test_cancel_during_verification_never_falls_back(self):
        client = self.client()
        client._encryption.prepare.side_effect = Cancelled()
        client.on_encryption_failure = Mock()
        with self.assertRaises(Cancelled):
            client.complete([])
        client.on_encryption_failure.assert_not_called()

    def test_question_defaults_to_stop(self):
        choose = Mock(return_value="stop")
        self.assertEqual(fallback_question(choose, "failed"), "stop")
        self.assertEqual(choose.call_args.kwargs["default"], "stop")
        self.assertIn("HTTPS", choose.call_args.kwargs["detail"])

    def test_cancel_terminates_isolated_verifier(self):
        from pycoder.e2ee import verify_hardware
        cancel = Mock(side_effect=Cancelled())
        with self.assertRaises(Cancelled):
            verify_hardware({}, "", "", (), cancel)


class EncryptionSetupTests(unittest.TestCase):
    def test_activate_only_uses_matching_ready_environment(self):
        from pycoder.e2ee_setup import activate, interpreter, REQUIREMENTS
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            directory = config / "e2ee-env"
            python = interpreter(directory)
            python.parent.mkdir(parents=True)
            python.touch()
            marker = directory / "pycoder-ready.json"
            with patch("pycoder.e2ee_setup.os.execv") as execute:
                activate(config)
                execute.assert_not_called()
                marker.write_text(json.dumps({"python": list(sys.version_info[:2]), "requirements": list(REQUIREMENTS)}))
                activate(config)
                self.assertEqual(execute.call_args.args[0], str(python))
                execute.reset_mock()
                with patch("pycoder.e2ee_setup.sys.prefix", str(directory)):
                    activate(config)
                execute.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("chutes_e2ee"), "Install requirements-e2ee.txt")
class CryptoRoundTripTests(unittest.TestCase):
    def setUp(self):
        from chutes_e2ee import crypto
        self.crypto = crypto
        self.pk, self.sk = crypto.mlkem_generate_keypair()
        self.public = base64.b64encode(self.pk).decode()
        self.payload = {"model": "model", "messages": [{"role": "user", "content": "PRIVATE_PROMPT"}], "stream": True}

    def decrypt_request(self, blob):
        c = self.crypto
        ct = blob[:1088]
        key = c.derive_key(c.mlkem_decrypt(self.sk, ct), ct, c.INFO_REQ)
        return json.loads(gzip.decompress(c.chacha_decrypt(key, blob[1088:1100], blob[1100:-16], blob[-16:])))

    def encrypted_response(self, response_pk, streaming):
        c = self.crypto
        ct, secret = c.mlkem_encrypt(base64.b64decode(response_pk))
        key = c.derive_key(secret, ct, c.INFO_STREAM if streaming else c.INFO_RESP)
        if not streaming:
            message = {"choices": [{"message": {"role": "assistant", "content": "PRIVATE_ANSWER"}, "finish_reason": "stop"}]}
            nonce = os.urandom(12)
            body, tag = c.chacha_encrypt(key, nonce, gzip.compress(json.dumps(message).encode()))
            return ct + nonce + body + tag
        events = [{"e2e_init": base64.b64encode(ct).decode()}]
        for delta, finish in [({"reasoning_content": "thinking"}, None), ({"content": "PRIVATE_ANSWER"}, None), ({}, "stop")]:
            text = "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]})
            nonce = os.urandom(12)
            body, tag = c.chacha_encrypt(key, nonce, text.encode())
            events.append({"e2e": base64.b64encode(nonce + body + tag).decode()})
        return ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()

    def test_unverified_real_ciphertext_roundtrip_without_verifier(self):
        def fetch(host, path):
            if path == "/v1/models":
                return {"data": [{"id": "model", "confidential_compute": True, "chute_id": "chute"}]}
            if path.startswith("/e2e/instances/"):
                return {"nonce_expires_in": 60, "instances": [{"instance_id": "instance", "e2e_pubkey": self.public, "nonces": ["one"]}]}
            self.fail("Unverified encryption must not request evidence")
        manager = ChutesEncryption(fetch, lambda: None)
        with patch("pycoder.e2ee.verify_hardware", side_effect=AssertionError("must not verify")):
            envelope = manager.prepare("model", self.payload, verify_attestation=False)
        self.assertNotIn(b"PRIVATE_PROMPT", envelope.body)
        request = self.decrypt_request(envelope.body)
        self.assertEqual(request["messages"], self.payload["messages"])
        response = self.encrypted_response(request["e2e_response_pk"], False)
        from pycoder.e2ee import decrypt_body
        decoded = json.loads(decrypt_body(io.BytesIO(response), envelope.response_key))
        self.assertEqual(decoded["choices"][0]["message"]["content"], "PRIVATE_ANSWER")
        self.assertEqual(manager.verified, {})

    def test_actual_http_envelope_and_streaming_and_json_roundtrip(self):
        for streaming in (True, False):
            payload = {**self.payload, "stream": streaming}
            encrypted = self.crypto.build_e2ee_request(self.public, payload)
            self.assertNotIn(b"PRIVATE_PROMPT", encrypted.blob)
            request = self.decrypt_request(encrypted.blob)
            self.assertEqual(request["messages"], payload["messages"])
            body = self.encrypted_response(request["e2e_response_pk"], streaming)
            self.assertNotIn(b"PRIVATE_ANSWER", body)
            response = io.BytesIO(body)
            response.status = 200
            response.getheader = lambda *args: "text/event-stream" if streaming else "application/octet-stream"
            connection = Mock()
            connection.getresponse.return_value = response
            client = Client(Config(model="model"), "token")
            envelope = Envelope(encrypted.blob, encrypted.response_sk, {"Content-Type": "application/octet-stream"}, "instance")
            fragments = []
            client.on_output = lambda **event: fragments.append(event)
            with patch("pycoder.transport.http.client.HTTPSConnection", return_value=connection) as connect:
                result = client._http_request(payload, lambda _: None, encrypted=envelope)
            self.assertEqual(result.message["content"], "PRIVATE_ANSWER")
            self.assertEqual(connect.call_args.args[0], "api.chutes.ai")
            args = connection.request.call_args.args
            self.assertEqual(args[:2], ("POST", "/e2e/invoke"))
            self.assertEqual(args[2], encrypted.blob)
            self.assertTrue(any(f["channel"] == "content" for f in fragments))

    def test_plaintext_tampering_and_duplicate_chunks_are_rejected(self):
        request = self.crypto.build_e2ee_request(self.public, self.payload)
        decoded = self.decrypt_request(request.blob)
        body = self.encrypted_response(decoded["e2e_response_pk"], True)
        lines = body.splitlines(keepends=True)
        variants = [
            b'data: {"choices":[{"delta":{"content":"injected"}}]}\n\n',
            b'data: {"usage": {}, "choices":[{"delta":{"content":"injected"}}]}\n\n',
            lines[0] + b'\n' + lines[2] + b'\n' + lines[2] + b'\n',
        ]
        event = json.loads(lines[2][5:])
        raw = bytearray(base64.b64decode(event["e2e"]))
        raw[-1] ^= 1
        event["e2e"] = base64.b64encode(raw).decode()
        variants.append(lines[0] + b'\n' + ("data: " + json.dumps(event) + "\n\n").encode())
        for invalid in variants:
            stream = DecryptedStream(io.BytesIO(invalid), request.response_sk, lambda: None)
            with self.subTest(invalid=invalid[:40]), self.assertRaises(EncryptionError):
                while stream.readline():
                    pass

    def test_discovery_checks_flag_attests_key_and_consumes_nonce(self):
        fetched = []
        def fetch(host, path):
            fetched.append((host, path))
            if path == "/v1/models":
                return {"data": [{"id": "model", "confidential_compute": True, "chute_id": "chute"}]}
            if path.startswith("/e2e/instances/"):
                return {"nonce_expires_in": 60, "instances": [{"instance_id": "instance",
                    "e2e_pubkey": self.public, "nonces": ["one", "two", "three"]}]}
            return {"instance_id": "instance", "quote": "proof"}
        manager = ChutesEncryption(fetch, lambda: None)
        with patch("pycoder.e2ee.verify_hardware", return_value={"verified": True}) as verify:
            first = manager.prepare("model", self.payload)
            second = manager.prepare("model", self.payload)
            self.assertEqual(verify.call_count, 1)
            self.assertEqual(verify.call_args.args[2], self.public)
            self.assertEqual(len(verify.call_args.args[1]), 64)
            new_pk, _ = self.crypto.mlkem_generate_keypair()
            manager.pool[0]["e2e_pubkey"] = base64.b64encode(new_pk).decode()
            third = manager.prepare("model", self.payload)
            self.assertEqual(verify.call_count, 2)
            self.assertEqual(third.headers["X-E2E-Nonce"], "three")
        self.assertEqual(first.headers["X-E2E-Nonce"], "one")
        self.assertEqual(second.headers["X-E2E-Nonce"], "two")
        manager.models[0]["confidential_compute"] = False
        with self.assertRaises(EncryptionError):
            manager.prepare("model", self.payload)

    def test_nullable_instance_id_still_requires_bound_hardware_proof(self):
        for evidence in ({"instance_id": None}, {}, {"instance_id": "other"}):
            def fetch(host, path):
                if path == "/v1/models":
                    return {"data": [{"id": "model", "confidential_compute": True, "chute_id": "chute"}]}
                if path.startswith("/e2e/instances/"):
                    return {"nonce_expires_in": 60, "instances": [{"instance_id": "instance",
                        "e2e_pubkey": self.public, "nonces": ["one"]}]}
                return evidence
            with self.subTest(evidence=evidence), patch("pycoder.e2ee.verify_hardware", return_value={"verified": True}) as verify:
                manager = ChutesEncryption(fetch, lambda: None)
                if evidence.get("instance_id") == "other":
                    with self.assertRaisesRegex(EncryptionError, "другому серверу"):
                        manager.prepare("model", self.payload)
                    verify.assert_not_called()
                else:
                    self.assertEqual(manager.prepare("model", self.payload).instance_id, "instance")
                    self.assertEqual(verify.call_args.args[2], self.public)
                    verify.side_effect = EncryptionError("invalid binding")
                    with self.assertRaisesRegex(EncryptionError, "invalid binding"):
                        ChutesEncryption(fetch, lambda: None).prepare("model", self.payload)


class AttestationModeTests(unittest.TestCase):
    def manager(self, config=None):
        public = base64.b64encode(bytes(1184)).decode()
        def fetch(host, path):
            if path == '/v1/models':
                return {'data': [{'id': 'model', 'confidential_compute': True, 'chute_id': 'chute'}]}
            if path.startswith('/e2e/instances/'):
                return {'nonce_expires_in': 60, 'instances': [{'instance_id': 'instance', 'e2e_pubkey': public, 'nonces': ['one', 'two', 'three']}]}
            return {'instance_id': 'instance', 'quote': 'proof'}
        fetch = Mock(side_effect=fetch)
        return ChutesEncryption(fetch, lambda: None, proxy_config=config), fetch

    def test_unverified_still_encrypts_consumes_nonce_and_never_caches_proof(self):
        manager, fetch = self.manager()
        impl = Mock()
        impl.build_e2ee_request.return_value = Mock(blob=b'ciphertext', response_sk=b'response-key')
        with patch('pycoder.e2ee.crypto', return_value=impl), patch('pycoder.e2ee.verify_hardware') as verify:
            first = manager.prepare('model', {'stream': True}, verify_attestation=False)
            self.assertEqual(first.body, b'ciphertext')
            self.assertEqual(first.headers['X-E2E-Nonce'], 'one')
            verify.assert_not_called()
            self.assertFalse(any('/evidence' in c.args[1] for c in fetch.call_args_list))
            self.assertEqual(manager.verified, {})
            # Turning verification back on must not trust an unverified request.
            second = manager.prepare('model', {'stream': True})
            verify.assert_called_once()
            self.assertEqual(second.headers['X-E2E-Nonce'], 'two')

    def test_direct_verifier_routes_are_temporary(self):
        cfg = Config(model='model', proxy_enabled=True)
        manager, _ = self.manager(cfg)
        with patch('pycoder.e2ee.crypto'), patch('pycoder.e2ee.verify_hardware') as verify:
            manager.prepare('model', {}, direct_attestation=True)
            route = verify.call_args.kwargs['proxy_config']
            self.assertTrue(route.proxy_enabled)
            self.assertFalse(route.proxy_intel)
            self.assertFalse(route.proxy_nvidia)
            self.assertTrue(cfg.proxy_intel and cfg.proxy_nvidia and cfg.proxy_chutes)
            manager.prepare('model', {})
            self.assertEqual(verify.call_count, 1)

    def test_one_time_choice_does_not_change_generation_route_or_next_attempt(self):
        cfg = Config(model='model', proxy_enabled=True)
        client = Client(cfg, 'secret')
        client._encryption = Mock()
        observations = []
        envelope = Envelope(b'ciphertext', b'key', {}, 'instance')
        def prepare(model, payload, **options):
            observations.append((client._direct_attestation, options))
            if len(observations) in (1, 3):
                raise EncryptionError('attestation failed')
            return envelope
        client._encryption.prepare.side_effect = prepare
        client.on_encryption_failure = Mock(side_effect=['attestation_direct_once', 'stop'])
        success = Completion({'role': 'assistant', 'content': 'ok'}, {}, 'stop')
        def send(*args, **kwargs):
            self.assertFalse(client._direct_attestation)
            self.assertTrue(cfg.proxy_chutes)
            self.assertIs(kwargs['encrypted'], envelope)
            return success
        with patch.object(client, '_http_request', side_effect=send):
            client._request({}, lambda _: None)
            with self.assertRaises(ModelError):
                client._request({}, lambda _: None)
        self.assertEqual(observations, [(False, {}), (True, {'direct_attestation': True}), (False, {})])
        self.assertFalse(client._direct_attestation)
        self.assertTrue(cfg.proxy_intel and cfg.proxy_nvidia)

    def test_direct_override_applies_to_preparation_metadata(self):
        client = Client(Config(model='model', proxy_enabled=True), 'secret')
        client._direct_attestation = True
        response = Mock(status=200)
        response.read.return_value = b'{}'
        conn = Mock()
        conn.getresponse.return_value = response
        with patch('pycoder.transport.http.client.HTTPSConnection', return_value=conn), patch.object(client, '_proxy_connection') as route:
            for path, direct in [('/instances/id/evidence?nonce=secret-nonce', True), ('/e2e/instances/chute', True), ('/v1/models', True)]:
                client._encryption_metadata('api.chutes.ai', path)
                self.assertEqual(route.call_args.kwargs['direct'], direct)

    def test_metadata_403_identifies_chutes_and_does_not_expose_nonce(self):
        client = Client(Config(model='model'), 'secret')
        response = Mock(status=403)
        response.read.return_value = b'{}'
        conn = Mock()
        conn.getresponse.return_value = response
        with patch('pycoder.transport.http.client.HTTPSConnection', return_value=conn):
            with self.assertRaises(EncryptionError) as error:
                client._encryption_metadata('api.chutes.ai', '/instances/id/evidence?nonce=private-value')
        self.assertIn('Chutes api.chutes.ai/instances/id/evidence', str(error.exception))
        self.assertIn('403', str(error.exception))
        self.assertNotIn('private-value', str(error.exception))

    def test_fourth_choice_only_when_authorized_proxy_mode(self):
        for enabled in (False, True):
            choose = Mock(return_value='stop')
            fallback_question(choose, 'failed', allow_direct=enabled)
            choices = dict(choose.call_args.args[1])
            self.assertEqual('attestation_direct_once' in choices, enabled)
            self.assertEqual(len(choices), 4 if enabled else 3)
            self.assertEqual(choose.call_args.kwargs['default'], 'stop')

    def test_unverified_status_is_not_verified_and_still_sends_envelope(self):
        cfg = Config(model='model', e2ee_verify_attestation=False)
        client = Client(cfg, '')
        client._encryption = Mock()
        client._encryption.prepare.return_value = Envelope(b'ciphertext', b'key', {}, 'instance')
        states = []
        client.on_security = lambda **state: states.append(state)
        with patch.object(client, '_http_request') as send:
            client._request({}, lambda _: None)
        client._encryption.prepare.assert_called_once_with('model', {}, verify_attestation=False)
        self.assertIn('encrypted', send.call_args.kwargs)
        self.assertEqual(states[-1]['mode'], 'unverified')
        self.assertFalse(client.can_retry_attestation_direct)
