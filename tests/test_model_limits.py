import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pycoder.agent import Agent
from pycoder.config import Config, ensure_output_limit, load, limit_scope
from pycoder.history import History
from pycoder.tools import Tools
from pycoder.transport import Client, Completion, TruncatedTool
from pycoder.output_limits import resolve_output_limit
from tests.support import call


class ModelLimitTests(unittest.TestCase):
    def test_migrate_legacy_catalog_limit_and_scope_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            (path/'config.json').write_text(json.dumps({'model': 'kimi', 'max_output_tokens': 4096}))
            cfg = load(path)
            catalog = Mock(return_value=[{'id': 'kimi', 'context_length': 1048576, 'max_output_length': 65535}])
            self.assertTrue(ensure_output_limit(cfg, 'key', fetch_models=catalog, print_fn=lambda _: None))
            self.assertEqual(cfg.max_output_tokens, 65535)
            self.assertEqual(cfg.output_limit_scope, limit_scope(cfg))
            self.assertFalse(ensure_output_limit(cfg, 'key', fetch_models=catalog))
            catalog.assert_called_once()
            cfg.model = 'other'
            answers = Mock(return_value='12345')
            self.assertTrue(ensure_output_limit(cfg, 'key', input_fn=answers, fetch_models=catalog, print_fn=lambda _: None))
            self.assertEqual(cfg.max_output_tokens, 12345)
            answers.assert_called_once()

    def test_missing_invalid_catalog_limit_prompts_and_noninteractive_requires_explicit_value(self):
        for missing in [None, 0, -1, True, '65535']:
            cfg = Config(model='test', output_limit_source='unset')
            catalog = lambda *a: [{'id':'test','max_output_length':missing}]
            with self.subTest(value=missing):
                with self.assertRaisesRegex(ValueError, '--max-output-tokens'):
                    ensure_output_limit(cfg, '', fetch_models=catalog, print_fn=lambda _: None)
                answers = iter(['invalid', '-1', '90000'])
                ensure_output_limit(cfg, '', fetch_models=catalog, input_fn=lambda _: next(answers), print_fn=lambda _: None)
                self.assertEqual(cfg.max_output_tokens, 90000)
                cfg.validate()  # output cap may exceed context; each request fits actual input.

    def test_stream_is_not_cut_off_by_declared_token_count_or_intermediate_length(self):
        client = Client(Config(model='test', max_output_tokens=1), '')
        events = [{'choices':[{'delta':{'content':'abc'*100},'finish_reason':'length'}]},
                  {'choices':[{'delta':{'content':'tail'},'finish_reason':'stop'}], 'usage':{'completion_tokens':1000}}]
        body = ''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n'
        with patch.object(client, '_request', side_effect=lambda p, emit: client._stream(io.BytesIO(body.encode()), emit)):
            result = client.complete([])
        self.assertTrue(result.message['content'].endswith('tail'))
        self.assertEqual(result.usage['completion_tokens'], 1000)

    def test_fragment_choice_discards_all_truncated_calls_preserves_plan_and_prior_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = Config(model='test')
            history = History(root/'state', root)
            tools = Tools(root,cfg,history,lambda *a: True,lambda q:'',lambda _:None)
            history.set_plan([{'step':'Build', 'status':'in_progress'}])
            error = TruncatedTool({'role':'assistant','content':'Creating file','tool_calls':[call('apply_patch',{})]})
            error.limit, error.capacity = 4096, 100000
            client=Mock()
            client.complete.side_effect=[error,Completion({'role':'assistant','content':'Fragmented'}, {}, 'stop'),
                                         Completion({'role':'assistant','content':'Remaining work is blocked'}, {}, 'stop')]
            try:
                agent=Agent(cfg,client,history,tools,lambda _:None,lambda _:None)
                agent.on_output_limit=Mock(return_value=('fragment',None))
                with patch.object(tools,'execute') as execute:
                    agent.run('Work')
                    execute.assert_not_called()
                agent.on_output_limit.assert_called_once_with(error)
                self.assertEqual(cfg.max_output_tokens,4096)
                self.assertNotIn('tool_calls',str(history.messages))
                self.assertTrue(any('smaller steps' in (m.get('content') or '') for m in history.messages))
                self.assertEqual(history.plan[0]['status'],'in_progress')
            finally:
                tools.close(); history.close()

    def test_choice_of_larger_limit_validates_input_and_stop_is_no_action(self):
        error=TruncatedTool({})
        error.limit,error.capacity=65535,100000
        answers=iter(['65535','100001','80000'])
        choice=Mock(return_value='increase')
        self.assertEqual(resolve_output_limit(error,choice,lambda _:next(answers)),('increase',80000))
        self.assertEqual([o[0] for o in choice.call_args.args[1]], ['fragment','increase','stop'])
        self.assertIsNone(resolve_output_limit(error,lambda *a,**k:'stop',lambda _: self.fail()))

    def test_compaction_limit_can_be_increased_without_replacing_history_early(self):
        from pycoder.transport import OutputLimit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            cfg=Config(model='test',context_window=32768)
            history=History(root/'state',root)
            tools=Tools(root,cfg,history,lambda *a:True,lambda q:'',lambda _:None)
            history.append({'role':'user','content':'Keep API stable'})
            history.append({'role':'assistant','content':'details ' * 4000})
            original=list(history.messages)
            error=OutputLimit({'role':'assistant','content':'partial summary'})
            error.limit,error.capacity=4096,10000
            client=Mock()
            client.complete.side_effect=[error,Completion({'role':'assistant','content':'API stable; continue tests.'},{},'stop')]
            try:
                agent=Agent(cfg,client,history,tools,lambda _:None,lambda _:None)
                def decide(e):
                    self.assertEqual(history.messages,original)
                    self.assertEqual(e.operation,'compaction')
                    return 'increase',8192
                agent.on_output_limit=decide
                agent.compact()
                self.assertEqual(client.complete.call_args.kwargs['max_tokens'],8192)
                self.assertNotEqual(history.messages,original)
            finally:
                tools.close()
                history.close()


class LimitDialogTests(unittest.IsolatedAsyncioTestCase):
    async def test_tui_fragment_and_increase_choices_wait_for_user(self):
        import asyncio
        from pycoder.cli import parser
        from pycoder.runtime import Runtime
        from pycoder.tui import CoderApp, Composer, QuestionScreen
        from tests.support import server, json_response, reply
        for increase in (False, True):
            with self.subTest(increase=increase), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                responses=[reply('Partial', finish='length'), reply('Done')]
                with server(lambda r: json_response(responses[len(r)-1])) as (url, requests):
                    args=parser().parse_args(['--new','--no-auth','--config-dir',str(root/'config')])
                    runtime=Runtime(args, Config(model='test',base_url=url,require_token=False),root,root/'state')
                    app=CoderApp(runtime)
                    async def until(condition):
                        for _ in range(150):
                            if condition(): return
                            await asyncio.sleep(.03)
                        self.fail('UI condition not reached')
                    async with app.run_test(size=(120,40)) as pilot:
                        await until(lambda:isinstance(app.screen,QuestionScreen))
                        await pilot.press('enter')
                        await until(lambda:app.ready)
                        app.query_one(Composer).load_text('Work')
                        await pilot.press('enter')
                        await until(lambda:isinstance(app.screen,QuestionScreen))
                        self.assertIn('оборвала',app.screen.data['title'])
                        await pilot.pause(.1)
                        self.assertEqual(len(requests),1)
                        if increase:
                            await pilot.press('down','enter')
                            await until(lambda:isinstance(app.screen,QuestionScreen) and app.screen.data.get('allow_text'))
                            await pilot.press('enter')
                        else:
                            await pilot.press('enter')
                        await until(lambda:len(requests)==2 and not app.busy)
                        self.assertEqual(requests[1]['body']['max_tokens'],8192 if increase else 4096)
                        if increase:
                            self.assertEqual(requests[0]['body']['messages'],requests[1]['body']['messages'])
                        else:
                            self.assertIn('smaller steps',requests[1]['body']['messages'][-1]['content'])
                    runtime.stop()
                    await asyncio.to_thread(runtime.thread.join,3)
