import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_runtime as runtime
from agentflow.accounts import ProviderLimit, AccountChanged
from agentflow.runtime import Engine
from agentflow.safety import LoopStopped
from agentflow.store import DomainError
from agentflow.providers import Providers


class SafetyCommandsUsageTests(unittest.TestCase):
    setUp=runtime.RuntimeTests.setUp
    tearDown=runtime.RuntimeTests.tearDown
    ready=runtime.RuntimeTests.ready

    def run_work(self):
        self.ready()
        return self.engine.chat({'agent_id':'a-lead','message':'Build and verify a local report'})

    def tool(self,run,name,args):
        actor=self.store.get('agents',run['agent_id'])
        self.store.patch('agents',actor['id'],{'tools':list(set(actor.get('tools',[])+[name]))})
        return self.engine.tool(self.store.get('agents',actor['id']),{'name':name,'arguments':args},run)

    def test_varying_text_loop_stops_without_more_inference(self):
        run=self.run_work()
        with patch.object(self.providers,'infer',side_effect=[json.dumps({'reply':f'Update {i}','state':'continue'}) for i in range(30)]) as model:
            for _ in range(5):self.engine.execute(run['id']);self.engine.wake_waiters()
        self.assertEqual(model.call_count,12)
        self.assertTrue(self.store.get('runs',run['id'])['safety_halted'])
        self.assertEqual(len([q for q in self.store.all('decisions') if q.get('safety_request')]),1)

    def test_repeated_operation_with_varying_prose_stops(self):
        run=self.run_work()
        with patch.object(self.providers,'infer',side_effect=[json.dumps({'reply':str(i),'tool_calls':[{'name':'workspace.read','arguments':{'path':'missing.md'}}]}) for i in range(8)]) as model:
            self.engine.execute(run['id']);self.engine.execute(run['id'])
        self.assertEqual(model.call_count,3)
        self.assertTrue(self.store.get('runs',run['id'])['safety_halted'])

    def test_budget_survives_engine_restart_and_owner_can_resume(self):
        run=self.run_work()
        self.store.patch('runs',run['id'],{'inference_attempts':24,'steps':24})
        self.engine.close();self.engine=Engine(self.store,self.providers,self.vault,self.root/'company')
        with patch.object(self.providers,'infer') as model:self.engine.execute(run['id'])
        model.assert_not_called()
        q=next(q for q in self.store.all('decisions') if q.get('safety_request'))
        with self.assertRaises(DomainError):self.engine.team.answer(q['id'],{'answer':'maybe later'})
        self.engine.team.answer(q['id'],{'answer':'Continue','safety_action':'continue'})
        with patch.object(self.providers,'infer',return_value='{"reply":"Finished","state":"complete"}') as model:self.engine.execute(run['id'])
        self.assertEqual(model.call_count,1)
        self.assertEqual(self.store.get('runs',run['id'])['status'],'done')

    def test_root_budget_is_shared_by_children(self):
        run=self.run_work()
        child=self.engine.enqueue('a-code','Work','peer',root_id=run['id'])
        self.store.patch('runs',run['id'],{'family_attempts':64})
        with self.assertRaises(LoopStopped):self.engine.safety.before_infer(child)
        self.assertTrue(self.store.get('runs',run['id'])['safety_halted'])
        with self.assertRaises(LoopStopped):self.engine.enqueue('a-code','More','peer',root_id=run['id'])

    def test_no_self_wait_or_wait_on_current_review(self):
        run=self.run_work()
        with self.assertRaises(DomainError):self.tool(run,'team.wait',{'run_ids':[run['id']]})
        task=self.store.create('tasks',{'title':'Review','status':'review','review_run_id':run['id']})
        with self.assertRaises(DomainError):self.tool(run,'team.wait',{'task_ids':[task['id']]})

    def test_peer_budget_brake_survives_tool_transaction_rollback(self):
        run=self.run_work()
        for i in range(23):self.engine.enqueue('a-code',f'Queued work {i}','peer',root_id=run['id'],project_id=run['project_id'])
        output=json.dumps({'tool_calls':[{'name':'team.message','arguments':{'agent_id':'a-seo','content':'Please check the report','wait':True}}]})
        with patch.object(self.providers,'infer',return_value=output) as model:self.engine.execute(run['id'])
        self.assertEqual(model.call_count,1)
        self.assertTrue(self.store.get('runs',run['id'])['safety_halted'])
        self.engine.wake_waiters()
        self.assertFalse(any(r['status']=='queued' for r in self.store.all('runs') if r.get('root_id')==run['id']))
        self.assertEqual(len([q for q in self.store.all('decisions') if q.get('safety_request')]),1)

    def test_stop_all_cancels_queue_tasks_and_revokes_command_session(self):
        run=self.run_work()
        task=self.store.create('tasks',{'title':'Later','status':'ready','auto_run':True,'agent_id':'a-code'})
        self.engine.commands.sessions[('p','root')]=time.monotonic()+900
        self.engine.safety.stop_all()
        self.assertFalse(self.store.settings()['execution_enabled'])
        self.assertEqual(self.store.get('runs',run['id'])['status'],'cancelled')
        self.assertEqual(self.store.get('tasks',task['id'])['status'],'cancelled')
        self.assertEqual(self.engine.commands.sessions,{})
        with patch.object(self.providers,'infer') as model:self.assertEqual(self.engine.pump(),[])
        model.assert_not_called()

    def test_missing_folder_created_only_after_owner_approves(self):
        run=self.run_work();folder=self.root/'new project'/'nested'
        result=self.tool(run,'files.request_access',{'path':str(folder),'access':'readwrite'})
        q=self.store.get('decisions',result['decision_id'])
        self.assertTrue(q['create_directory']);self.assertFalse(folder.exists())
        self.engine.team.answer(q['id'],{'answer':'Allow','approve':True})
        self.assertTrue(folder.is_dir())
        self.tool(run,'project.mkdir',{'path':'assets/images'})
        self.assertTrue((folder/'assets/images').is_dir())
        with self.assertRaises(DomainError):self.tool(run,'project.mkdir',{'path':'../escape'})

    def command_run(self):
        run=self.run_work();folder=self.root/'project';folder.mkdir()
        self.engine.team.grant_folder(run['project_id'],{'path':str(folder),'access':'readwrite'})
        return run,folder

    def test_dangerous_command_waits_for_atlas_and_cancel_does_not_spawn(self):
        run,folder=self.command_run()
        args={'command':['powershell','-NoProfile','-Command','Remove-Item demo.txt']}
        with patch('agentflow.commands.subprocess.Popen') as launch:
            result=self.tool(run,'workspace.execute',args)
            q=self.store.get('decisions',result['decision_id'])
            self.assertEqual(q['agent_id'],'a-lead');self.assertTrue(q['command_request'])
            self.engine.team.answer(q['id'],{'answer':'Cancel','command_action':'cancel'})
            self.assertTrue(self.tool(run,'workspace.execute',args)['cancelled'])
        launch.assert_not_called()

    def test_powershell_directory_creation_uses_folder_grant_without_shell_approval(self):
        run,folder=self.command_run()
        with patch('agentflow.commands.subprocess.Popen') as launch:
            result=self.tool(run,'workspace.execute',{'command':['powershell','-Command',"New-Item -Path 'assets/new folder' -ItemType Directory -Force"]})
        self.assertTrue(result['created']);self.assertTrue((folder/'assets/new folder').is_dir())
        launch.assert_not_called()
        self.assertFalse(any(q.get('command_request') for q in self.store.all('decisions')))
        with self.assertRaises(DomainError):self.tool(run,'workspace.execute',{'command':['mkdir','../outside']})

    def test_15_minute_command_approval_expires_and_never_bypasses_secret_rules(self):
        run,folder=self.command_run();args={'command':['python','-c','print(1)']}
        result=self.tool(run,'workspace.execute',args);q=self.store.get('decisions',result['decision_id'])
        with patch('agentflow.commands.time.monotonic',return_value=100):
            self.engine.team.answer(q['id'],{'answer':'Session','command_action':'session'})
        self.assertEqual(self.engine.commands.sessions[(run['project_id'],str(folder))],1000)
        with self.assertRaises(DomainError):self.tool(run,'workspace.execute',{'command':['python','-c','open(".env").read()']})
        with patch('agentflow.commands.time.monotonic',return_value=1001):
            result=self.tool(run,'workspace.execute',args)
        self.assertTrue(result['waiting_permission'])

    def test_single_command_approval_is_not_replayed(self):
        run,folder=self.command_run();args={'command':['python','-c','print(1)']}
        result=self.tool(run,'workspace.execute',args)
        self.engine.team.answer(result['decision_id'],{'answer':'Allow','command_action':'allow'})
        with patch.object(self.engine.commands,'sandbox_argv',return_value=['sandbox-runner']),patch('agentflow.commands.subprocess.Popen') as launch:
            launch.return_value.communicate.return_value=('1','');launch.return_value.returncode=0
            self.assertTrue(self.tool(run,'workspace.execute',args)['executed'])
            self.assertTrue(self.tool(run,'workspace.execute',args)['waiting_permission'])
        self.assertEqual(launch.call_count,1)

    def test_routine_command_uses_sandbox_and_missing_sandbox_never_uses_host(self):
        run,folder=self.command_run();self.providers.cli['codex']='codex.exe'
        with patch.object(self.engine.commands,'sandbox_argv',return_value=['sandbox-runner']) as build,patch('agentflow.commands.subprocess.Popen') as launch:
            launch.return_value.communicate.return_value=('test passed','');launch.return_value.returncode=0
            result=self.tool(run,'workspace.execute',{'command':['python','-m','unittest']})
        self.assertEqual(result['exit_code'],0);build.assert_called_once()
        env=launch.call_args.kwargs['env'];self.assertNotIn('OPENAI_API_KEY',env)
        self.providers.cli['codex']=None
        with patch('agentflow.commands.subprocess.Popen') as launch:
            with self.assertRaises(DomainError):self.tool(run,'workspace.execute',{'command':['python','-m','unittest']})
        launch.assert_not_called()

    def test_codex_usage_is_account_bound_and_accepts_multiple_windows(self):
        state=self.providers.accounts.check('codex',force=True)
        payload={'limits':{'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':37,'windowDurationMins':300,'resetsAt':time.time()+1000},'secondary':{'usedPercent':62,'windowDurationMins':10080,'resetsAt':time.time()+2000}}}}}
        with patch.object(self.providers,'_codex_models',return_value=payload):self.providers.usage.refresh_codex(state,force=True)
        public=self.providers.usage.public('codex');self.assertEqual([w['used_percent'] for w in public['windows']],[37,62])
        self.assertNotIn('account_fingerprint',public)
        self.providers.accounts.current['codex']['fingerprint']='different'
        self.assertEqual(self.providers.usage.public('codex')['windows'],[])

    def test_claude_events_use_real_fraction_and_reset_without_guessing(self):
        state=self.providers.accounts.check('claude',force=True)
        records=[{'type':'rate_limit_event','rate_limit_info':{'status':'allowed_warning','rateLimitType':'five_hour','utilization':0.75,'resetsAt':time.time()+1000}}]
        self.providers.usage.claude_events(records,state)
        result=self.providers.usage.public('claude');self.assertEqual(result['windows'][0]['used_percent'],75)
        self.assertEqual(result['source'],'Claude Code rate_limit_event')
        self.assertIsNone(ProviderLimit('claude','limit reached').retry_at)

    def test_usage_account_change_during_fetch_discards_old_data(self):
        state=self.providers.accounts.check('codex',force=True)
        with patch.object(self.providers,'_codex_models',return_value={}),patch.object(self.providers.accounts,'assert_current',side_effect=AccountChanged()):
            with self.assertRaises(AccountChanged):self.providers.usage.refresh_codex(state,force=True)
        self.assertEqual(self.providers.usage.public('codex')['windows'],[])

    def test_claude_partial_events_keep_known_percent_and_use_latest_measurement(self):
        state=self.providers.accounts.check('claude',force=True)
        reset=time.time()+1000
        event=lambda percent:{'type':'rate_limit_event','rate_limit_info':{'rateLimitType':'five_hour','utilization':percent,'resetsAt':reset}}
        self.providers.usage.claude_events([event(.2),event(.49)],state)
        self.providers.usage.claude_events([event(None)],state)
        windows=self.providers.usage.public('claude')['windows']
        self.assertEqual(len(windows),1);self.assertEqual(windows[0]['used_percent'],49)

    def test_usage_keeps_confirmed_data_during_same_account_refresh(self):
        state=self.providers.accounts.check('codex',force=True)
        self.providers.usage.save('codex',state['fingerprint'],[{'id':'codex:primary','used_percent':37}],'test')
        public=self.providers.usage.public('codex',pending=True)
        self.assertEqual(public['windows'][0]['used_percent'],37)
        self.assertTrue(public['updating'])
        with patch.object(self.providers.accounts,'revision',return_value='changed'):
            self.assertEqual(self.providers.usage.public('codex',pending=True)['windows'],[])

    def test_empty_codex_refresh_does_not_erase_confirmed_observation(self):
        state=self.providers.accounts.check('codex',force=True)
        self.providers.usage.save('codex',state['fingerprint'],[{'id':'codex:primary','used_percent':42}],'test')
        with patch.object(self.providers,'_codex_models',return_value={'limits':{}}):
            self.providers.usage.refresh_codex(state,force=True)
        self.assertEqual(self.providers.usage.public('codex')['windows'][0]['used_percent'],42)

    def test_partial_codex_response_retains_weekly_without_claiming_freshness(self):
        state=self.providers.accounts.check('codex',force=True)
        first={'limits':{'rateLimits':{'limitId':'business','primary':{'usedPercent':20,'windowDurationMins':10080,'resetsAt':time.time()+5000}},
            'rateLimitsByLimitId':{'business':{'primary':{'usedPercent':20,'windowDurationMins':10080,'resetsAt':time.time()+5000},'secondary':{'usedPercent':40,'resetsAt':time.time()+6000}}}}}
        with patch.object(self.providers,'_codex_models',return_value=first):self.providers.usage.refresh_codex(state,force=True)
        observed=self.providers.usage.public('codex')
        self.assertEqual(observed['default_bucket'],'business')
        with patch.object(self.providers,'_codex_models',return_value={'limits':{'rateLimits':{'limitId':'business','primary':{'usedPercent':25,'windowDurationMins':10080,'resetsAt':time.time()+5000}}}}):
            self.providers.usage.refresh_codex(state,force=True)
        result={w['id']:w for w in self.providers.usage.public('codex')['windows']}
        self.assertEqual(result['business:primary']['used_percent'],25)
        self.assertEqual(result['business:secondary']['used_percent'],40)
        self.assertTrue(result['business:secondary']['stale'])
        self.assertFalse(result['business:primary']['stale'])

    def test_claude_subscription_percent_is_not_fraction_and_secrets_stay_private(self):
        import io
        with patch.object(self.providers.accounts,'home',return_value=self.root):
            (self.root/'.credentials.json').write_text(json.dumps({'claudeAiOauth':{'accessToken':'private-test-token'}}))
            state=self.providers.accounts.check('claude',force=True)
            opener=Mock()
            opener.open.return_value=io.StringIO(json.dumps({'five_hour':{'utilization':23.5,'resets_at':'2099-10-19T12:00:00Z'},'seven_day':None}))
            with patch('agentflow.usage.urllib.request.build_opener',return_value=opener),patch.object(self.providers,'infer') as inference:
                self.providers.usage.refresh_claude(state,force=True)
            inference.assert_not_called()
            self.assertEqual(opener.open.call_args.args[0].full_url,'https://api.anthropic.com/api/oauth/usage')
            public=self.providers.usage.public('claude')
            self.assertEqual(public['windows'][0]['used_percent'],23.5)
            self.assertNotIn('private-test-token',json.dumps(public)+self.providers.usage.path.read_text())

    def test_gateway_url_is_not_proof_of_connection_or_installation(self):
        self.store.settings({'gateway_url':'http://127.0.0.1:20128/v1'})
        self.providers.verified.add('gateway:old')
        with patch.object(self.providers,'_probe_status'):
            p=next(p for p in self.providers.status() if p['id']=='gateway')
        self.assertTrue(p['configured'])
        self.assertFalse(p['installed']);self.assertFalse(p['connected']);self.assertFalse(p['verified'])
        with patch.object(self.providers,'gateway',side_effect=DomainError('Offline',502)):
            self.providers._probe_status('gateway')
        self.assertFalse(self.providers.gateway_connected)

    def test_gemini_is_a_real_provider_with_chat_only_catalog_and_sign_in_gate(self):
        # Behaviour is covered in depth by tests/test_gemini.py; this keeps the former "disabled" assertions honest.
        self.providers.cli['gemini']='gemini-test.exe'
        fingerprint=self.providers.accounts.check('gemini',force=True)['fingerprint']
        self.providers.models.append({'provider':'gemini','id':'cached','capabilities':['chat'],'account_fingerprint':fingerprint})
        self.providers.models.append({'provider':'gemini','id':'cached-image','capabilities':['image'],'account_fingerprint':fingerprint})
        p=next(p for p in self.providers.status() if p['id']=='gemini')
        self.assertTrue(p['installed']);self.assertNotIn('disabled',p)
        self.assertEqual([m['id'] for m in self.engine.models.catalog() if m['provider']=='gemini'],['cached'])
        with patch.object(self.providers,'read_account',return_value=None):
            with self.assertRaises(DomainError):self.providers.infer({'provider':'gemini','model':'cached'},'Work','signed-out')

    def test_provider_quota_brake_survives_restart(self):
        actor={'provider':'codex','model':'test','name':'Worker'}
        with patch.object(self.providers,'_infer',side_effect=ProviderLimit('codex','unknown reset')) as model:
            with self.assertRaises(ProviderLimit):self.providers.infer(actor,'Work','quota-a')
            with self.assertRaises(ProviderLimit):self.providers.infer(actor,'Work','quota-b')
        self.assertEqual(model.call_count,1)
        reopened=Providers(self.store,self.vault,self.engine.workspace)
        reopened.cli=self.providers.cli.copy()
        with patch.object(reopened,'read_account',return_value={'type':'test','label':'Unit account'}),patch.object(reopened,'_infer') as model:
            with self.assertRaises(ProviderLimit):reopened.infer(actor,'Work','quota-c')
        model.assert_not_called()

    def test_unknown_quota_reset_does_not_blindly_requeue(self):
        run=self.run_work()
        self.store.patch('runs',run['id'],{'status':'waiting_quota','retry_at':None,'quota_blocked_at':1,
            'quota_binding':{'provider':'gateway','model':'observed-test-model'}})
        with patch('agentflow.runtime.time.time',return_value=time.time()+86400):self.engine.wake_waiters()
        self.assertEqual(self.store.get('runs',run['id'])['status'],'waiting_quota')

