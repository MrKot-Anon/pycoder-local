import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import threading
import time
import unittest
from unittest.mock import Mock, patch

from pycoder.background_attestation import BackgroundAttestation, AttestationDecision
from pycoder.config import Config
from pycoder.e2ee import ChutesEncryption, EncryptionError, background_question
from pycoder.events import EventBus, Cancelled
from pycoder.transport import Client


class BackgroundAttestationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000.0
        self.config = Config(model='model')
        self.transport = Client(self.config, 'token')
        self.key = base64.b64encode(bytes(1184)).decode()
        self.scope = ('instance', self.key, ())
        self.bus = EventBus()
        self.manager = BackgroundAttestation('model', self.transport,
            lambda reason, recovered, stopped: background_question(self.bus.request, reason, recovered, stopped),
            clock=lambda: self.now)
        self.manager.current = self.scope
        self.addCleanup(self.manager.close)

    def wait_for(self, condition):
        deadline = time.monotonic() + 3
        while not condition():
            self.assertLess(time.monotonic(), deadline, 'background operation did not finish')
            time.sleep(0.01)

    def test_five_minute_renewal_and_eight_minute_trust_are_separate(self):
        self.manager.successes[self.scope] = self.now
        self.now += 300
        self.assertTrue(self.manager.valid(self.scope))
        self.assertFalse(self.manager._warning_due())
        self.now += 179.99
        self.assertTrue(self.manager.valid(self.scope))
        self.now += 0.01
        self.assertFalse(self.manager.valid(self.scope))
        self.assertTrue(self.manager._warning_due())

    def test_unknown_key_never_inherits_grace_period(self):
        self.manager.successes[self.scope] = self.now
        other = ('instance', base64.b64encode(b'x' * 1184).decode(), ())
        self.assertFalse(self.manager.valid(other))
        cancel = threading.Event()
        def check():
            if cancel.is_set():
                raise Cancelled()
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.manager.require, other, check)
            self.wait_for(lambda: self.manager.current == other)
            self.assertFalse(pending.done())
            cancel.set()
            with self.assertRaises(Cancelled):
                pending.result(timeout=2)
        self.assertFalse(self.manager.stopped.is_set())

    def test_unanswered_warning_disappears_and_request_resumes(self):
        self.manager.successes[self.scope] = self.now - 481
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.manager.require, self.scope, lambda: None)
            event = self.bus.queue.get(timeout=2)
            self.assertEqual(event.kind, 'question')
            self.assertIn('8 минут', event.data['detail'])
            self.assertFalse(pending.done())
            with self.manager.lock:
                self.manager.successes[self.scope] = self.now
            pending.result(timeout=2)
            self.assertTrue(event.data['reply'].cancelled())

    def test_explicit_stop_is_not_overridden_by_recovery(self):
        self.manager.successes[self.scope] = self.now - 481
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.manager.require, self.scope, lambda: None)
            event = self.bus.queue.get(timeout=2)
            event.data['reply'].set_result('stop')
            with self.manager.lock:
                self.manager.successes[self.scope] = self.now
            with self.assertRaises(AttestationDecision) as raised:
                pending.result(timeout=2)
            self.assertEqual(raised.exception.action, 'stop')

    def test_initial_attempt_gets_three_minutes(self):
        self.now += 179
        self.assertFalse(self.manager._warning_due())
        self.now += 1
        self.assertTrue(self.manager._warning_due())
        self.assertFalse(self.manager.valid(self.scope))

    def test_idle_session_warns_and_success_closes_question(self):
        self.manager.proactive = True
        self.manager.successes[self.scope] = self.now - 481
        self.manager.next_attempt = self.now + 100
        self.manager.start()
        event = self.bus.queue.get(timeout=2)
        self.assertEqual(event.kind, 'question')
        with self.manager.lock:
            self.manager.successes[self.scope] = self.now
        self.wait_for(event.data['reply'].cancelled)

    def test_worker_renews_and_retries_during_grace(self):
        self.manager.target_requested = True
        attempt = Mock()
        self.manager.engine.attest = attempt
        self.manager.start()
        self.wait_for(lambda: self.manager.valid(self.scope))
        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(self.manager.next_attempt, self.now + 300)
        attempt.side_effect = EncryptionError('Intel HTTP 403')
        self.manager.target_requested = True
        self.now += 300
        self.manager.wake.set()
        self.wait_for(lambda: bool(self.manager.error))
        self.assertTrue(self.manager.valid(self.scope))
        self.assertEqual(self.manager.next_attempt, self.now + 15)
        self.assertTrue(self.bus.queue.empty())
        attempt.side_effect = None
        self.now += 15
        self.manager.wake.set()
        self.wait_for(lambda: self.manager.successes[self.scope] == self.now)
        self.assertEqual(self.manager.next_attempt, self.now + 300)

    def test_worker_fetches_evidence_but_never_builds_a_prompt(self):
        self.manager.current = None
        def fetch(host, path):
            if path == '/v1/models':
                return {'data': [{'id': 'model', 'confidential_compute': True, 'chute_id': 'chute'}]}
            if path.startswith('/e2e/instances/'):
                return {'nonce_expires_in': 60, 'instances': [{'instance_id': 'instance',
                    'e2e_pubkey': self.key, 'nonces': ['one']}]}
            return {'instance_id': 'instance', 'quote': 'proof'}
        self.manager.engine.fetch = Mock(side_effect=fetch)
        with patch('pycoder.e2ee.verify_hardware') as verify, patch('pycoder.e2ee.crypto') as crypto:
            self.manager.start()
            self.wait_for(lambda: self.manager.valid(self.scope))
            self.manager.close()
            verify.assert_called_once()
            self.assertEqual(verify.call_args.args[2], self.key)
            crypto.assert_not_called()
            self.assertEqual(self.manager.engine.pool[0]['nonces'], ['one'])

    def test_direct_retry_is_worker_local_and_only_one_attempt(self):
        self.manager.target_requested = True
        self.manager.direct_once = True
        routes = []
        def attest(scope, **kwargs):
            routes.append((self.transport._direct_attestation, kwargs['direct_attestation']))
            raise EncryptionError('failed')
        self.manager.engine.attest = attest
        self.manager.start()
        self.wait_for(lambda: len(routes) == 1)
        self.wait_for(lambda: not self.transport._direct_attestation)
        self.now += 15
        self.manager.wake.set()
        self.wait_for(lambda: len(routes) == 2)
        self.assertEqual(routes, [(True, True), (False, False)])

    def test_close_cancels_pending_question_and_verifier(self):
        self.manager.proactive = True
        self.manager.successes[self.scope] = self.now - 481
        self.manager.target_requested = True
        entered = threading.Event()
        def attest(*args, **kwargs):
            entered.set()
            self.manager.stopped.wait(2)
            self.manager.check()
        self.manager.engine.attest = attest
        self.manager.start()
        self.assertTrue(entered.wait(2))
        event = self.bus.queue.get(timeout=2)
        self.manager.close()
        self.assertFalse(self.manager.worker.is_alive())
        self.assertFalse(self.manager.monitor.is_alive())
        self.assertTrue(event.data['reply'].cancelled())

    def test_unverified_and_custom_provider_do_not_start_worker(self):
        for cfg in (replace(self.config, e2ee_verify_attestation=False),
                    replace(self.config, base_url='https://example.com/v1')):
            client = Client(cfg, 'token')
            client.start_attestation()
            self.assertIsNone(client._attestation)

    def test_encryption_continues_while_renewal_is_running(self):
        self.manager.successes[self.scope] = self.now - 301
        self.manager.target_requested = True
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def attest(*args, **kwargs):
            entered.set()
            release.wait(3)
        self.manager.engine.attest = attest
        self.manager.start()
        self.assertTrue(entered.wait(2))
        engine = ChutesEncryption(lambda *args: self.fail('metadata already cached'), lambda: None)
        engine.background = self.manager
        engine.models = [{'id': 'model', 'confidential_compute': True, 'chute_id': 'chute'}]
        engine.models_until = time.monotonic() + 300
        engine.pool_scope = 'chute'
        engine.pool_until = time.monotonic() + 50
        engine.pool = [{'instance_id': 'instance', 'e2e_pubkey': self.key, 'nonces': ['nonce']}]
        impl = Mock()
        impl.build_e2ee_request.return_value = Mock(blob=b'ciphertext', response_sk=b'key')
        with patch('pycoder.e2ee.crypto', return_value=impl):
            result = engine.prepare('model', {'messages': ['private']})
        self.assertEqual(result.body, b'ciphertext')
        self.assertFalse(release.is_set())
        release.set()

    def test_transport_owns_separate_proxy_config_and_cancellation(self):
        cfg = replace(self.config, proxy_enabled=True, proxy_intel=False, proxy_nvidia=True)
        client = Client(cfg, 'token')
        with patch.object(BackgroundAttestation, 'start'):
            client.start_attestation()
        self.addCleanup(client.close)
        background = client._attestation.transport
        self.assertIsNot(background.config, cfg)
        self.assertTrue(background.config.proxy_enabled)
        self.assertFalse(background.config.proxy_intel)
        self.assertTrue(background.config.proxy_nvidia)
        client.cancel()
        self.assertFalse(background.cancelled.is_set())
        background._direct_attestation = True
        self.assertFalse(client._direct_attestation)

    def test_other_question_does_not_block_auto_recovery(self):
        self.bus.question_lock.acquire()
        self.addCleanup(self.bus.question_lock.release)
        recovered = threading.Event()
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.bus.request, 'attestation', auto_resolve=recovered.is_set)
            recovered.set()
            self.assertEqual(pending.result(timeout=2), 'recovered')
        self.assertTrue(self.bus.queue.empty())
