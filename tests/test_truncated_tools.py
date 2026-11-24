import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pycoder.agent import Agent
from pycoder.config import Config
from pycoder.history import History
from pycoder.tools import Tools
from pycoder.transport import Client, TruncatedTool
from tests.support import server, json_response, reply, call
from tests.test_tui import FakeRuntime
from pycoder.tui import CoderApp


def truncated(stream):
    calls = [call('apply_patch', {'patch': '*** Begin Patch\n*** Add File: forbidden.txt\n+bad\n*** End Patch'}, 'first'),
             call('apply_patch', {}, 'second')]
    calls[1]['function']['arguments'] = '{"patch":"unfinished'
    if not stream:
        return json_response(reply('Creating files', calls, finish='length'))
    for i, c in enumerate(calls):
        c['index'] = i
    event = {'choices': [{'delta': {'content': 'Creating files', 'tool_calls': calls}, 'finish_reason': 'length'}]}
    return 200, 'text/event-stream', 'data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n', {}


class TruncatedToolTests(unittest.TestCase):
    def test_regeneration_preserves_prior_results_and_never_executes_partial_batch(self):
        for stream in (True, False):
            with self.subTest(stream=stream), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                history = History(root / 'state', root)
                config = Config(model='test', approval='auto', max_output_tokens=1024)
                tools = Tools(root, config, history, lambda *a: True, lambda q: '', lambda _: None)
                def create(name):
                    return json_response(reply(None, [call('apply_patch', {'patch': f'*** Begin Patch\n*** Add File: {name}\n+ok\n*** End Patch'})]))
                responses = [create('a.txt'), truncated(stream), create('b.txt'), json_response(reply('Done'))]
                try:
                    with server(lambda r: responses[len(r)-1]) as (url, requests), patch.object(Client, '_delay'), patch.object(tools, 'execute', wraps=tools.execute) as execute:
                        config.base_url = url
                        client = Client(config, '')
                        restarts = []
                        agent = Agent(config, client, history, tools, lambda _: None, lambda _: None)
                        agent.on_output_limit = lambda error: (restarts.append(True) or ("increase", error.limit * 2))
                        self.assertEqual(agent.run('Create files'), 'Done')
                        self.assertEqual(execute.call_count, 2)
                        self.assertFalse((root / 'forbidden.txt').exists())
                        self.assertTrue((root / 'a.txt').exists() and (root / 'b.txt').exists())
                        self.assertEqual(requests[1]['body']['messages'], requests[2]['body']['messages'])
                        self.assertEqual([r['body']['max_tokens'] for r in requests], [1024, 1024, 2048, 2048])
                        self.assertEqual(restarts, [True])
                        self.assertEqual(config.max_output_tokens, 2048)
                        self.assertNotIn('unfinished', str(history.messages))
                finally:
                    tools.close()
                    history.close()

    def test_transport_never_retries_length_without_user_decision(self):
        for retries, initial, expected in [(0, 4096, [4096]), (8, 4096, [4096]), (8, 40000, [40000])]:
            with self.subTest(retries=retries, initial=initial), server(lambda r: truncated(True)) as (url, requests), patch.object(Client, '_delay'):
                client = Client(Config(model='test', base_url=url, context_window=1048576, max_output_tokens=initial, retries=retries), '')
                with self.assertRaises(TruncatedTool):
                    client.complete([])
                self.assertEqual([r['body']['max_tokens'] for r in requests], expected)

    def test_context_capacity_is_attached_to_limit_error(self):
        from pycoder.util import tokens
        messages = [{"role": "user", "content": "x" * 40000}]
        capacity = 16384 * 95 // 100 - tokens(messages) - tokens([]) - 512
        with server(lambda r: truncated(True)) as (url, requests), patch.object(Client, '_delay'):
            with self.assertRaises(TruncatedTool) as caught:
                Client(Config(model='test', base_url=url, context_window=16384, retries=8), '').complete(messages)
            self.assertEqual(caught.exception.capacity, capacity)
            self.assertEqual([r['body']['max_tokens'] for r in requests], [4096])


class RestartUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_prose_is_marked_and_new_attempt_is_separate(self):
        runtime = FakeRuntime()
        app = CoderApp(runtime)
        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause(.1)
            for kind, data in [('busy', {}), ('model_start', {}), ('text', {'text': 'First attempt'})]:
                runtime.bus.emit(kind, **data)
            await pilot.pause(.1)
            first = app.response
            runtime.bus.emit('model_restart')
            runtime.bus.emit('text', text='Second attempt')
            await pilot.pause(.1)
            self.assertIsNot(first, app.response)
            self.assertIn('инструмент не выполнен', first.content.markup)
            self.assertEqual(app.response_text, 'Second attempt')
            self.assertIsNotNone(app.model_started)
