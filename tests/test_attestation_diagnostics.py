import asyncio
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from pycoder import attestation as a
from pycoder.e2ee import verification_failure, verify_hardware, EncryptionError


class AttestationDiagnosticsTests(unittest.TestCase):
    def test_native_crash_preserves_stderr_and_redacts_secrets(self):
        process = Mock(returncode=134)
        process.poll.return_value = 134
        process.communicate.return_value = ('', None)
        def start(*args, **kwargs):
            kwargs['stderr'].write(b'panic: failed to create UnixStream; nonce-sensitive public-sensitive Bearer secret-token\nSIGABRT (6)')
            kwargs['stderr'].flush()
            Path(kwargs['env']['PYCODER_ATTESTATION_PROGRESS']).write_text(json.dumps({
                'stage': 'Intel TDX', 'step': 'Загрузка проверочных данных Intel PCS'}))
            return process
        with patch('pycoder.e2ee.subprocess.Popen', side_effect=start), self.assertRaises(EncryptionError) as raised:
            verify_hardware({}, 'nonce-sensitive', 'public-sensitive', (), lambda: None)
        text = str(raised.exception)
        self.assertIn('134', text)
        self.assertIn('UnixStream', text)
        self.assertIn('SIGABRT', text)
        self.assertIn('HTTP-запрос не подтверждён', text)
        for secret in ('nonce-sensitive', 'public-sensitive', 'secret-token'):
            self.assertNotIn(secret, text)

    def setUp(self):
        a.HTTP_EVENTS.clear()
        a.CHECK_FAILURES.clear()
        a.SECRETS = []
        a.STAGE, a.STEP = 'Intel TDX', 'test'
        self.report = {'status': 'UpToDate', 'report': {'TD10': {
            'td_attributes': '00' * 8,
            'report_data': hashlib.sha256(b'noncekey').hexdigest() + '00' * 32,
            'mr_td': '12' * 48}}}

    def test_field_failures_show_expected_and_actual(self):
        cases = [('status', 'OutOfDate', 'UpToDate', 'OutOfDate'),
                 ('td_attributes', '01' + '00' * 7, 'ожидалось 0', 'получено 1'),
                 ('report_data', '00' * 64, hashlib.sha256(b'noncekey').hexdigest(), '00' * 32),
                 ('mr_td', '', '48 байт', '0')]
        for key, value, expected, actual in cases:
            report = copy.deepcopy(self.report)
            (report if key == 'status' else report['report']['TD10'])[key] = value
            with self.assertRaises(ValueError) as failure:
                a.check_report(report, 'nonce', 'key')
            self.assertIn(expected, str(failure.exception))
            self.assertIn(actual, str(failure.exception))
        with self.assertRaises(ValueError) as failure:
            a.check_report(self.report, 'nonce', 'key', ['34' * 48])
        self.assertIn('12' * 48, str(failure.exception))
        self.assertIn('34' * 48, str(failure.exception))

    def test_native_intel_http_failure_reaches_user_with_url_and_status(self):
        a.STEP = 'Загрузка проверочных данных Intel PCS'
        error = RuntimeError('Failed to fetch https://api.trustedservices.intel.com/tdx/certification/v4/tcb?fmspc=123456&update=standard: HTTP 403')
        text = verification_failure(a.failure_result(error))
        self.assertIn('Этап: Загрузка проверочных данных Intel PCS', text)
        self.assertIn('Запрос: GET https://api.trustedservices.intel.com/tdx/certification/v4/tcb', text)
        self.assertIn('HTTP 403', text)
        self.assertIn('fmspc=123456', text)

    def test_sensitive_url_fields_tokens_and_terminal_controls_are_hidden(self):
        a.SECRETS = ['private-nonce', 'private-public-key']
        result = a.safe_detail('GET https://user:password@host/path?token=my-token&nonce=private-nonce&fmspc=123#private-fragment Bearer api-secret private-public-key\x1b[31m')
        for secret in ('password', 'my-token', 'private-nonce', 'private-fragment', 'api-secret', 'private-public-key', '\x1b'):
            self.assertNotIn(secret, result)
        self.assertIn('fmspc=123', result)
        self.assertIn('https://host/path', result)

    def test_native_verification_exception_chain_is_preserved_without_invented_url(self):
        a.STEP = 'Проверка подписи TDX, цепочки сертификатов и сроков действия'
        try:
            try:
                raise ValueError('certificate expired')
            except ValueError as inner:
                raise RuntimeError('quote signature chain invalid') from inner
        except RuntimeError as error:
            text = verification_failure(a.failure_result(error))
        self.assertIn('certificate expired', text)
        self.assertIn('quote signature chain invalid', text)
        self.assertNotIn('Запрос:', text)

    def test_stage_is_preserved_when_native_fetch_fails(self):
        native = types.SimpleNamespace(parse_quote=lambda raw: types.SimpleNamespace(quote_type=lambda: 'TDX'),
            get_collateral_from_pcs=AsyncMock(side_effect=RuntimeError('DNS resolution failed')), verify=Mock())
        with patch.dict('sys.modules', {'dcap_qvl': native}):
            with self.assertRaises(RuntimeError) as failure:
                asyncio.run(a.verify({'evidence': {'quote': base64.b64encode(bytes(632)).decode()}}))
        result = a.failure_result(failure.exception)
        self.assertEqual(result['step'], 'Загрузка проверочных данных Intel PCS')
        text = verification_failure(result)
        self.assertIn('DNS resolution failed', text)
        self.assertIn('не сообщил точный URL', text)
        native.verify.assert_not_called()

    def test_claims_include_mismatch_and_missing_fields(self):
        details = a.claim_mismatches({'secboot': False, 'dbgstat': 'enabled', 'eat_nonce': 'wrong'},
                                    {'secboot': True, 'dbgstat': 'disabled', 'eat_nonce': 'expected', 'required': True})
        text = '\n'.join(details)
        for word in ('secboot', 'False', 'True', 'dbgstat', 'enabled', 'disabled', 'wrong', 'expected', 'required', 'отсутствует'):
            self.assertIn(word, text)

    def test_parent_transports_detailed_child_failure(self):
        result = {'verified': False, 'stage': 'Intel TDX', 'step': 'Проверка полей TDX',
                  'error': 'ValueError', 'reason': 'status: ожидалось UpToDate; получено OutOfDate'}
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps(result), None)
        with patch('pycoder.e2ee.subprocess.Popen', return_value=process), self.assertRaises(EncryptionError) as error:
            verify_hardware({}, 'nonce', 'key', (), lambda: None)
        self.assertIn('OutOfDate', str(error.exception))
        self.assertIn('Этап: Проверка полей TDX', str(error.exception))

    def test_last_step_is_written_for_timeout_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'progress.json'
            with patch.dict(os.environ, {'PYCODER_ATTESTATION_PROGRESS': str(path)}):
                a.set_step('Запрос NRAS', 'NVIDIA GPU')
                a.record_http('POST', 'https://nras.attestation.nvidia.com/v3/attest/gpu?token=hidden')
            result = json.loads(path.read_text())
            self.assertEqual(result['step'], 'Запрос NRAS')
            self.assertNotIn('hidden', path.read_text())
            self.assertIsNone(result['requests'][0]['status'])

    def test_http_hook_records_failed_request_even_if_sdk_catches_exception(self):
        session_class = type('Session', (), {'send': Mock(side_effect=OSError('connection reset'))})
        requests = types.SimpleNamespace(sessions=types.SimpleNamespace(Session=session_class))
        request = types.SimpleNamespace(method='GET', url='https://nras.attestation.nvidia.com/.well-known/jwks.json')
        original = session_class.send
        with patch.dict('sys.modules', {'requests': requests}), a.observe_http():
            with self.assertRaises(OSError):
                session_class().send(request)
        self.assertIs(session_class.send, original)
        text = verification_failure(a.failure_result(ValueError('SDK returned False')))
        self.assertIn('Запрос: GET https://nras.attestation.nvidia.com/.well-known/jwks.json', text)
        self.assertIn('connection reset', text)
        self.assertIn('ответ HTTP не получен', text)

    def test_parent_timeout_reports_last_step_and_pending_request(self):
        process = Mock(returncode=None)
        process.communicate.return_value = ('', None)
        process.poll.return_value = None
        def start(*args, **kwargs):
            Path(kwargs['env']['PYCODER_ATTESTATION_PROGRESS']).write_text(json.dumps({
                'stage': 'NVIDIA GPU', 'step': 'Запрос NRAS', 'requests': [
                    {'method': 'POST', 'url': 'https://nras.attestation.nvidia.com/v3/attest/gpu', 'status': None}]}))
            return process
        with patch('pycoder.e2ee.subprocess.Popen', side_effect=start), patch('pycoder.e2ee.time.monotonic', side_effect=[0, 91]), self.assertRaises(EncryptionError) as error:
            verify_hardware({}, 'nonce', 'key', (), lambda: None)
        text = str(error.exception)
        self.assertIn('90 с', text)
        self.assertIn('Этап: Запрос NRAS', text)
        self.assertIn('POST https://nras.attestation.nvidia.com/v3/attest/gpu', text)
        process.kill.assert_called_once()

    def test_http_status_and_invalid_json_are_recorded(self):
        response = types.SimpleNamespace(status_code=403, json=Mock(side_effect=ValueError('not JSON')))
        session_class = type('Session', (), {'send': Mock(return_value=response)})
        requests = types.SimpleNamespace(sessions=types.SimpleNamespace(Session=session_class))
        request = types.SimpleNamespace(method='POST', url='https://nras.attestation.nvidia.com/v3/attest/gpu')
        with patch.dict('sys.modules', {'requests': requests}), a.observe_http():
            result = session_class().send(request)
            with self.assertRaises(ValueError):
                result.json()
        text = verification_failure(a.failure_result(ValueError('SDK returned False')))
        self.assertIn('HTTP 403', text)
        self.assertIn('не удалось разобрать как JSON', text)


@unittest.skipUnless(importlib.util.find_spec("nv_attestation_sdk"), "NVIDIA SDK not installed")
class NvidiaDiagnosticHooksTests(unittest.TestCase):
    def test_swallowed_sdk_signature_and_claim_failures_are_retained(self):
        from nv_attestation_sdk.utils import claim_utils, nras_utils
        a.CHECK_FAILURES.clear()
        a.SECRETS = []
        original_claims = claim_utils.validate_claims
        with patch.object(nras_utils, "decode_jwt_token", side_effect=ValueError("invalid signature")):
            with a.observe_nvidia():
                self.assertFalse(claim_utils.validate_claims({"secboot": False}, {"secboot": True}))
                self.assertIsNone(nras_utils.get_matching_key({"keys": []}, "missing-key"))
                with self.assertRaises(ValueError):
                    nras_utils.decode_jwt_token("token", None)
        self.assertIs(claim_utils.validate_claims, original_claims)
        text = "\n".join(a.CHECK_FAILURES)
        self.assertIn("secboot: ожидалось True; получено False", text)
        self.assertIn("missing-key", text)
        self.assertIn("invalid signature", text)
