import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from pycoder.config import Config
from pycoder.events import Cancelled
from pycoder import e2ee_setup as s


class SetupTests(unittest.TestCase):
    def test_direct_environment_ignores_proxy_and_pip_configuration(self):
        with patch.dict(os.environ, {'HTTP_PROXY': 'http://secret:password@proxy',
                'https_proxy': 'socks5://proxy', 'ALL_PROXY': 'proxy',
                'PIP_INDEX_URL': 'http://custom', 'PIP_PROXY': 'proxy',
                'PIP_CONFIG_FILE': '/private/pip.conf', 'PATH': '/bin'}, clear=True):
            env = s.direct_environment()
        self.assertNotIn('HTTP_PROXY', env)
        self.assertNotIn('https_proxy', env)
        self.assertNotIn('ALL_PROXY', env)
        self.assertNotIn('PIP_PROXY', env)
        self.assertNotIn('PIP_INDEX_URL', env)
        self.assertEqual(env['PIP_CONFIG_FILE'], os.devnull)
        self.assertEqual(env['NO_PROXY'], '*')
        self.assertEqual(env['PATH'], '/bin')

    def test_present_dependencies_do_not_install_and_custom_provider_is_skipped(self):
        event = Mock()
        with patch.object(s, 'probe', return_value='') as probe, patch.object(s, 'setup') as setup:
            s.ensure(Config(model='m'), Path('/unused'), event=event)
            setup.assert_not_called()
            self.assertEqual(event.call_args.kwargs['state'], 'ready')
            probe.reset_mock()
            s.ensure(Config(base_url='https://example.com'), Path('/unused'))
            probe.assert_not_called()

    def test_unverified_mode_checks_crypto_without_attestation_libraries(self):
        with patch.object(s, 'probe', return_value='') as probe:
            s.ensure(Config(e2ee_verify_attestation=False), Path('/unused'))
        self.assertFalse(probe.call_args.kwargs['attestation'])

    def test_missing_dependencies_install_even_with_proxy_and_enable_before_ready(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            with patch.object(s, 'probe', return_value='ModuleNotFoundError: dcap_qvl'), \
                    patch.object(s, 'setup') as setup, patch.object(s, 'enable') as enable:
                def install(path, emit, check):
                    emit('pip installation output')
                setup.side_effect = install
                s.ensure(Config(proxy_enabled=True), config, event=lambda **data: events.append(data))
                setup.assert_called_once()
                enable.assert_called_once_with(config / 'e2ee-env')
        self.assertIn('installing', [event['state'] for event in events])
        self.assertIn('pip installation output', [event['text'] for event in events])
        self.assertEqual(events[-1]['state'], 'ready')

    def test_failed_install_remains_visible_and_does_not_activate(self):
        events = []
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(s, 'probe', return_value='missing'), \
                patch.object(s, 'setup', side_effect=ValueError('pip HTTP 403')), \
                patch.object(s, 'enable') as enable:
            with self.assertRaisesRegex(ValueError, '403'):
                s.ensure(Config(), Path(directory), event=lambda **data: events.append(data))
            enable.assert_not_called()
        self.assertEqual(events[-1]['state'], 'failed')
        self.assertIn('pip HTTP 403', events[-1]['text'])
        self.assertNotIn('ready', [event['state'] for event in events])

    def test_existing_environment_is_reused_without_download(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            python = s.interpreter(config / 'e2ee-env')
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(s, 'probe', side_effect=['missing', '']), \
                    patch.object(s, 'setup') as setup, patch.object(s, 'enable') as enable:
                s.ensure(Config(), config)
                setup.assert_not_called()
                enable.assert_called_once()

    def test_installer_announces_direct_route_and_writes_marker_only_after_probe(self):
        output = []
        with tempfile.TemporaryDirectory() as directory, patch.object(s, 'run_logged') as run, \
                patch.object(s, 'probe', return_value=''):
            s.setup(Path(directory), output.append)
            self.assertIn('БЕЗ ПРОКСИ', output[0])
            pip = run.call_args_list[1].args[0]
            self.assertIn('--isolated', pip)
            self.assertIn('https://pypi.org/simple', pip)
            self.assertEqual(pip[pip.index('--proxy') + 1], '')
            self.assertTrue((Path(directory) / 'e2ee-env/pycoder-ready.json').is_file())
        with tempfile.TemporaryDirectory() as directory, patch.object(s, 'run_logged'), \
                patch.object(s, 'probe', return_value='broken crypto'):
            with self.assertRaises(ValueError):
                s.setup(Path(directory), output.append)
            self.assertFalse((Path(directory) / 'e2ee-env/pycoder-ready.json').exists())

    def test_log_is_streamed_before_process_finishes_and_cancellation_stops_it(self):
        lines = []
        cancelled = threading.Event()
        def emit(line):
            lines.append(line)
            cancelled.set()
        def check():
            if cancelled.is_set():
                raise Cancelled()
        with self.assertRaises(Cancelled):
            s.run_logged([sys.executable, '-u', '-c',
                "import time; print('installing'); time.sleep(60)"], emit, check)
        self.assertEqual(lines, ['installing'])

    def test_repair_does_not_overwrite_running_venv_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'e2ee-env'
            python = s.interpreter(root)
            python.parent.mkdir(parents=True)
            python.touch()
            (root / 'pyvenv.cfg').touch()
            with patch.object(s, 'run_logged') as run, patch.object(s, 'probe', return_value=''):
                s.setup(Path(directory), lambda text: None)
                first = run.call_args_list[0].args[0]
                self.assertIn('ensurepip', first)
                self.assertNotIn('venv', first)

    def test_nonzero_exit_preserves_log(self):
        lines = []
        with self.assertRaisesRegex(ValueError, '7'):
            s.run_logged([sys.executable, '-u', '-c', "print('package failed'); raise SystemExit(7)"],
                         lines.append, lambda: None)
        self.assertEqual(lines, ['package failed'])

    def test_parallel_start_waits_for_existing_installer_and_reuses_result(self):
        from pycoder.workspace import WorkspaceLock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'e2ee-env'
            root.mkdir()
            lock = WorkspaceLock(root)
            lines = []
            def emit(line):
                lines.append(line)
                lock.close()
            try:
                with patch.object(s, 'probe', return_value=''), patch.object(s, '_setup') as install:
                    s.setup(Path(directory), emit)
                    install.assert_not_called()
            finally:
                lock.close()
            self.assertIn('Ожидаю', lines[0])
