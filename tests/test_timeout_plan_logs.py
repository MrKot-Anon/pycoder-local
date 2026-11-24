import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from pycoder.config import Config
from pycoder.transport import Client, ModelError
from pycoder.plans import validate_plan, plan_lines
from pycoder.diagnostics import Diagnostics
from pycoder.tui import CoderApp, Composer, LogPane
from pycoder.logview import follow
from tests.test_tui import FakeRuntime


@contextmanager
def slow_server(mode):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            try:
                if mode == 'headers':
                    time.sleep(.25)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                if mode == 'buffered_tool':
                    self.wfile.write(b'data: {"choices":[{"delta":{"content":"Creating file"}}]}\n\n')
                    self.wfile.flush()
                    time.sleep(.25)
                    event = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {
                        "name": "apply_patch", "arguments": json.dumps({"patch": "x" * 24000})}}]}, "finish_reason": "tool_calls"}]}
                    self.wfile.write(('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode())
                    return
                if mode == 'stall':
                    time.sleep(.4)
                else:
                    for _ in range(4):
                        self.wfile.write(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
                        self.wfile.flush()
                        time.sleep(.08)
                self.wfile.write(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            except (BrokenPipeError, ConnectionResetError):
                pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{httpd.server_port}/v1'
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class TimeoutTests(unittest.TestCase):
    def test_headers_and_generation_have_separate_idle_timeout(self):
        with slow_server('headers') as url:
            client = Client(Config(model='test', base_url=url, timeout_seconds=.05,
                                   generation_timeout_seconds=.4, retries=0), '')
            # Total > .4s; each network gap < .4s. Header wait > connect timeout.
            result = client.complete([])
            self.assertEqual(result.message['content'], 'hello' * 4)

    def test_stalled_stream_records_phase_without_secrets(self):
        with slow_server('stall') as url:
            client = Client(Config(model='test', base_url=url, timeout_seconds=.2,
                                   generation_timeout_seconds=.08, retries=0), 'secret-value')
            errors = []
            client.on_diagnostic = errors.append
            # Custom endpoints retry indefinitely; this test inspects one attempt.
            def stop_retry(**data):
                raise ModelError(data['error'])
            client.on_retry = stop_retry
            with self.assertRaisesRegex(ModelError, 'phase=SSE stream') as caught:
                client.complete([])
            self.assertNotIn('secret-value', str(caught.exception))
            self.assertIn('received=0 bytes', str(caught.exception))
            self.assertEqual(len(errors), 1)

    def test_buffered_tool_after_prose_outlives_legacy_timeout(self):
        for idle, succeeds in [(.08, False), (.6, True)]:
            with self.subTest(idle=idle), slow_server('buffered_tool') as url:
                client = Client(Config(model='test', base_url=url, timeout_seconds=.2,
                                       generation_timeout_seconds=idle, retries=0), '')
                output = []
                # Stop after the first timeout; retry policy is tested separately.
                def stop_retry(**data):
                    raise ModelError(data['error'])
                client.on_retry = stop_retry
                if succeeds:
                    result = client.complete([], on_text=output.append)
                    tool = result.message['tool_calls'][0]
                    self.assertEqual(len(json.loads(tool['function']['arguments'])['patch']), 24000)
                else:
                    with self.assertRaisesRegex(ModelError, 'phase=SSE stream'):
                        client.complete([], on_text=output.append)
                self.assertEqual(output, ['Creating file'])

    def test_cli_log_view_does_not_load_model_or_lock_workspace(self):
        from pycoder.cli import main
        with tempfile.TemporaryDirectory() as tmp, patch('pycoder.cli.load', side_effect=AssertionError), patch('pycoder.cli.WorkspaceLock', side_effect=AssertionError), patch('pycoder.logview.follow', return_value=0) as view:
            self.assertEqual(main(['--logs', '--state-dir', tmp]), 0)
            view.assert_called_once_with(Path(tmp).resolve() / 'errors.log')

    def test_external_terminal_gets_quoted_command_without_credentials(self):
        from pycoder.logview import open_terminal
        with patch('pycoder.logview.sys.platform', 'darwin'), patch('pycoder.logview.sys.argv', ['/tmp/a b/pycoder']), patch('pycoder.logview.subprocess.run') as run:
            open_terminal(Path('/tmp/cfg $(ignored)'), Path('/tmp/state'))
            import shlex
            argv = shlex.split(run.call_args.args[0][-1])
            self.assertEqual(argv[1:], [str(Path('/tmp/a b/pycoder').resolve()), '--logs', '--config-dir', '/tmp/cfg $(ignored)', '--state-dir', '/tmp/state'])

    def test_nested_plan_active_branch_and_rendering(self):
        plan = [{'step': 'Implement', 'status': 'in_progress', 'children': [
            {'step': 'API', 'status': 'completed'}, {'step': 'UI', 'status': 'in_progress'}]}]
        validate_plan(plan)
        self.assertEqual(list(plan_lines(plan)), ['◉ Implement', '  ✓ API', '  ◉ UI'])
        plan[0]['status'] = 'completed'
        with self.assertRaises(ValueError):
            validate_plan(plan)
        plan[0]['status'] = 'in_progress'
        plan[0]['children'][0]['status'] = 'in_progress'
        with self.assertRaises(ValueError):
            validate_plan(plan)

    def test_log_follow_rotation_and_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'errors.log'
            path.write_text('first\n')
            def tick(_):
                if path.read_text() == 'first\n':
                    path.rename(path.with_suffix('.old'))
                    path.write_text('second\n')
                else:
                    raise KeyboardInterrupt
            output = io.StringIO()
            with patch('pycoder.logview.time.sleep', side_effect=tick), patch('sys.stdout', output):
                self.assertEqual(follow(path), 0)
            self.assertIn('first\nsecond\n', output.getvalue())


class LiveLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_logs_update_while_model_runs_close_without_cancel_and_elapsed_ticks(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = FakeRuntime()
            runtime.diagnostics = Diagnostics(Path(tmp))
            app = CoderApp(runtime)
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause(.1)
                runtime.bus.emit('busy')
                runtime.bus.emit('model_start')
                await pilot.pause(.1)
                app.model_started -= 65
                await pilot.press('f3')
                await pilot.pause(.1)
                self.assertTrue(app.query_one(LogPane).display)
                runtime.diagnostics.record(ValueError('new error'))
                runtime.bus.emit('text', text='still running')
                await pilot.pause(1.2)
                self.assertIn('new error', str(app.query_one('#log-content').content))
                self.assertIn('still running', app.response_text)
                self.assertIn('модель 01:', app.last_status)
                self.assertGreaterEqual(time.monotonic() - app.model_started, 65)
                await pilot.press('escape')
                await pilot.pause(.1)
                self.assertFalse(app.query_one(LogPane).display)
                self.assertFalse(runtime.bus.cancelled.is_set())
                self.assertEqual(runtime.submitted, [])
                runtime.bus.emit('error', text='stalled')
                runtime.bus.emit('idle')
                await pilot.pause(.1)
                self.assertIn('Ошибка', app.last_status)
                duration = app.model_elapsed
                await pilot.pause(.1)
                self.assertEqual(duration, app.model_elapsed)
                self.assertNotIn('new error', '\n'.join(str(w.content) for w in app.query('.bubble')))
