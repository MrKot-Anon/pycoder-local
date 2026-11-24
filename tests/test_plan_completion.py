import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from pycoder.agent import Agent
from pycoder.config import Config
from pycoder.history import History
from pycoder.plans import FINAL_PLAN_CHECK
from pycoder.tools import Tools
from pycoder.transport import Completion
from tests.support import call


def response(content='Done', calls=None):
    message={'role':'assistant','content':content}
    if calls:
        message['tool_calls']=calls
    return Completion(message, {}, 'tool_calls' if calls else 'stop')


class PlanCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.cfg=Config(model='test',approval='auto')
        self.history=History(self.root/'state',self.root)
        self.tools=Tools(self.root,self.cfg,self.history,lambda *a:True,lambda q:'',lambda _:None)
        self.client=Mock()
        self.status=[]
        self.agent=Agent(self.cfg,self.client,self.history,self.tools,self.status.append,lambda _:None)

    def tearDown(self):
        self.tools.close()
        self.history.close()
        self.tmp.cleanup()

    def test_open_nested_plan_is_reconciled_before_completion(self):
        plan=[{'step':'Build','status':'in_progress','children':[
            {'step':'Code','status':'completed'},{'step':'Tests','status':'in_progress'}]}]
        self.history.set_plan(plan)
        done=copy.deepcopy(plan)
        done[0]['status']='completed'
        done[0]['children'][1]['status']='completed'
        self.client.complete.side_effect=[response(), response(None,[call('update_plan',{'plan':done})]),response('Checked and done')]
        self.assertEqual(self.agent.run('Finish'), 'Checked and done')
        self.assertEqual(self.client.complete.call_count,3)
        self.assertEqual(self.history.plan,done)
        check=self.client.complete.call_args_list[1].args[0][-1]['content']
        self.assertTrue(check.startswith(FINAL_PLAN_CHECK))
        self.assertIn('Tests',check)

    def test_ignored_check_is_bounded_and_never_completes_plan_automatically(self):
        plan=[{'step':'Deploy','status':'pending'}]
        self.history.set_plan(plan)
        self.client.complete.return_value=response('Blocked: credentials missing')
        self.assertIn('Blocked',self.agent.run('Continue'))
        self.assertEqual(self.client.complete.call_count,2)
        self.assertEqual(self.history.plan,plan)
        self.assertIn('незавершённые',self.status[-1])

    def test_no_plan_or_completed_plan_needs_no_extra_request(self):
        for plan in ([], [{'step':'Done','status':'completed'}]):
            self.history.set_plan(plan)
            self.client.reset_mock()
            self.client.complete.return_value=response()
            self.agent.run('Question')
            self.assertEqual(self.client.complete.call_count,1)

    def test_plan_check_never_replays_successful_mutations(self):
        self.history.set_plan([{'step':'Create file','status':'in_progress'}])
        change='*** Begin Patch\n*** Add File: done.txt\n+done\n*** End Patch'
        self.client.complete.side_effect=[response(None,[call('apply_patch',{'patch':change})]),response(),response('File done; remaining plan not updated')]
        self.agent.run('Create file')
        self.assertEqual((self.root/'done.txt').read_text(),'done\n')
        self.assertEqual(len([m for m in self.history.messages if m['role']=='tool']),1)

    def test_restored_plan_is_checked_on_continue(self):
        self.history.append({'role':'user','content':'Build project'})
        self.history.set_plan([{'step':'Build','status':'in_progress'}])
        session=self.history.session_id
        self.history.close()
        self.history=History(self.root/'state',self.root,session)
        self.tools.history=self.history
        self.agent.history=self.history
        self.client.complete.return_value=response('Cannot finish yet')
        self.agent.run(None)
        self.assertEqual(self.client.complete.call_count,2)
        self.assertEqual(self.history.plan[0]['status'],'in_progress')


class PlanStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_unfinished_plan_is_visible_after_idle(self):
        from pycoder.tui import CoderApp
        from tests.test_tui import FakeRuntime
        runtime=FakeRuntime()
        app=CoderApp(runtime)
        async with app.run_test(size=(120,35)) as pilot:
            await pilot.pause(.1)
            runtime.bus.emit('busy')
            runtime.bus.emit('plan_unfinished')
            runtime.bus.emit('idle')
            await pilot.pause(.1)
            self.assertIn('План не завершён',app.last_status)
            runtime.bus.emit('busy')
            runtime.bus.emit('idle')
            await pilot.pause(.1)
            self.assertIn('Готов',app.last_status)
