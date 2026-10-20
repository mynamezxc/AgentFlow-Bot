import http.client
import json
import socket
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import test_runtime as runtime
from agentflow.browser import (HostResolver, ResolveError, classify, covers, domain_pattern, host_map_rules, origin,
                               registrable)
from agentflow.research import public_page
from agentflow.store import DomainError
from server import make_handler


def gai(table):
    """getaddrinfo stand-in: host -> address (or missing -> gaierror)."""
    def lookup(host, port, *args, **kwargs):
        if host not in table:raise socket.gaierror(-2,'Name or service not known')
        return [(None,None,None,None,(table[host],port))]
    return lookup


class FakeRequest:
    def __init__(self,url,method='GET',navigation=False):
        self.url,self.method,self._nav=url,method,navigation
    def is_navigation_request(self):return self._nav


class FakeRoute:
    def __init__(self,request):self.request,self.action=request,None
    def continue_(self):self.action='continue'
    def abort(self):self.action='abort'


class DomainHelperTests(unittest.TestCase):
    def test_registrable_domain(self):
        for host,expected in [('shop.example','shop.example'),('demo-banle.shop.example','shop.example'),('a.b.example.com','example.com'),
                              ('shop.example.com.vn','example.com.vn'),('example.com.vn','example.com.vn'),('www.bbc.co.uk','bbc.co.uk'),
                              ('x.y.com.au','y.com.au'),('127.0.0.1',None),('::1',None),('localhost',None),('com.vn',None),('intranet',None)]:
            self.assertEqual(registrable(host),expected,host)

    def test_domain_pattern_rejects_ips_and_public_suffixes(self):
        self.assertEqual(domain_pattern('*.shop.example'),'*.shop.example');self.assertEqual(domain_pattern('SHOP.example'),'*.shop.example')
        self.assertEqual(domain_pattern('*.shop.shop.example'),'*.shop.shop.example')
        for bad in ('*.com','*.com.vn','*.127.0.0.1','*.localhost','','*.'):
            with self.assertRaises(DomainError):domain_pattern(bad)

    def test_covers_exact_and_wildcard(self):
        grants=[{'origin':'https://exact.test','access':'read'},{'domain':'*.shop.example','origin':'*.shop.example','access':'interact','schemes':['https','http']}]
        self.assertIsNotNone(covers(grants,'https://exact.test/x','read'))
        self.assertIsNone(covers(grants,'https://exact.test','interact'))
        self.assertIsNone(covers(grants,'https://exact.test:8443','read'))
        for url in ('https://shop.example','https://demo-banle.shop.example/a?b=1','http://a.b.shop.example'):
            self.assertIsNotNone(covers(grants,url,'interact'),url)
        for url in ('https://evilshop.example','https://shop.example.evil.com','https://demo.shop.example:8443','ftp://shop.example','https://127.0.0.1','https://bad host'):
            self.assertIsNone(covers(grants,url,'read'),url)
        self.assertIsNone(covers([{'domain':'*.shop.example','access':'interact','schemes':['https']}],'http://x.shop.example','read'))
        # A wildcard never covers an IP literal or localhost, even if mis-stored as such.
        self.assertIsNone(covers([{'domain':'*.0.0.1','access':'read'}],'http://127.0.0.1','read'))
        self.assertIsNone(covers([{'domain':'*.localhost','access':'read'}],'http://localhost','read'))

    def test_classify_addresses(self):
        self.assertEqual(classify('93.184.216.34'),'public');self.assertEqual(classify('127.0.0.1'),'private')
        self.assertEqual(classify('192.168.1.5'),'private');self.assertEqual(classify('::1'),'private')
        for blocked in ('169.254.169.254','0.0.0.0','224.0.0.1','fe80::1%eth0','::ffff:169.254.1.1','nonsense'):
            self.assertEqual(classify(blocked),'blocked',blocked)

    def test_host_map_rules_for_chromium(self):
        self.assertEqual(host_map_rules({'*.shop.example':'127.0.0.1','api.test':'::1'}),'MAP *.shop.example 127.0.0.1,MAP shop.example 127.0.0.1,MAP api.test [::1]')
        self.assertEqual(host_map_rules({}),'')


class ResolverTests(unittest.TestCase):
    def make(self,mapping=None):
        return HostResolver(lambda:{'browser_host_map':mapping or {}})

    def test_caches_for_ttl_and_then_refreshes(self):
        resolver=self.make()
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai({'a.test':'93.184.216.34'})) as lookup,patch('agentflow.browser.time.monotonic',return_value=100):
            self.assertEqual(resolver.resolve('A.test.',443),['93.184.216.34']);resolver.resolve('a.test',443)
            self.assertEqual(lookup.call_count,1)
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai({'a.test':'93.184.216.35'})) as lookup,patch('agentflow.browser.time.monotonic',return_value=161):
            self.assertEqual(resolver.resolve('a.test',443),['93.184.216.35'])

    def test_dns_failure_is_explicit_with_host_map_hint(self):
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai({})):
            with self.assertRaises(ResolveError) as ctx:self.make().resolve('demo-banle.shop.example',443)
        self.assertEqual(str(ctx.exception),'Cannot resolve demo-banle.shop.example. If this is a local site, map it in Browser host map (e.g. *.shop.example → 127.0.0.1) or add it to the hosts file.')
        self.assertEqual(ctx.exception.kind,'dns')

    def test_invalid_hosts_and_scope_ids_do_not_masquerade_as_dns(self):
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=ValueError('bad')):
            with self.assertRaises(ResolveError) as ctx:self.make().resolve('weird.test',443)
        self.assertEqual(ctx.exception.kind,'invalid')
        with self.assertRaises(ResolveError):self.make().resolve('fe80::1%eth0',443)
        self.assertEqual(self.make().resolve('::1',443),['::1'])
        with patch('agentflow.browser.socket.getaddrinfo',return_value=[(None,None,None,None,('fe80::1%12',443,0,12))]):
            self.assertEqual(self.make().resolve('scoped.test',443),['fe80::1'])

    def test_host_map_wins_and_most_specific_pattern_applies(self):
        resolver=self.make({'*.shop.example':'127.0.0.1','demo.shop.example':'10.0.0.5','*.shop.shop.example':'::1'})
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=AssertionError('OS resolver must not be used')):
            self.assertEqual(resolver.resolve('demo-banle.shop.example'),['127.0.0.1']);self.assertEqual(resolver.resolve('shop.example'),['127.0.0.1'])
            self.assertEqual(resolver.resolve('demo.shop.example'),['10.0.0.5']);self.assertEqual(resolver.resolve('a.shop.shop.example'),['::1'])


class BrowserApprovalTests(unittest.TestCase):
    setUp=runtime.RuntimeTests.setUp
    tearDown=runtime.RuntimeTests.tearDown
    ready=runtime.RuntimeTests.ready

    def newrun(self,text='Verify the demo shops'):
        self.ready();return self.engine.chat({'message':text,'agent_id':'a-lead','channel':'dm-a-lead'})

    def agent(self,agent_id='a-content'):return self.store.get('agents',agent_id)

    def grants(self,run,grants):
        return self.store.patch('projects',run['project_id'],{'browser_grants':grants})

    def project(self,run):return self.store.get('projects',run['project_id'])

    # ---- grant matching & decisions ------------------------------------------------------

    def test_existing_wildcard_grant_covers_request_open_and_check(self):
        run=self.newrun();self.grants(run,[{'domain':'*.shop.example','origin':'*.shop.example','access':'interact','schemes':['https','http']}])
        result=self.engine.browser.request({'url':'https://demo-banle.shop.example/login','access':'interact'},self.agent(),run)
        self.assertEqual((result['granted'],result['covered_by']),(True,'*.shop.example'))
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai({'demo-banle.shop.example':'93.184.216.34'})):
            self.assertTrue(self.engine.browser._allowed('https://demo-banle.shop.example/login',self.project(run),'interact'))
            self.assertFalse(self.engine.browser._allowed('https://other.vn',self.project(run)))
        self.assertTrue(self.engine.browser.covers_url(run,'https://x.shop.example/api'));self.assertFalse(self.engine.browser.covers_url(run,'https://x.evil.vn/api'))

    def test_subdomain_request_offers_domain_first_and_allow_domain_grants_wildcard(self):
        run=self.newrun()
        asked=self.engine.browser.request({'url':'https://demo-banle.shop.example','access':'interact','reason':'Check the shop'},self.agent(),run)
        q=self.store.get('decisions',asked['decision_id'])
        self.assertEqual([a['value'] for a in q['actions']],['allow_domain','allow_site','deny'])
        self.assertEqual([a['label'] for a in q['actions']],['Allow all of *.shop.example','Allow https://demo-banle.shop.example','Deny'])
        self.assertEqual((q['action_field'],q['action_handler'],q['actions'][0]['style']),('browser_action','browser','primary'))
        self.assertEqual(q['options'][0]['label'],'Allow all of *.shop.example')
        with self.assertRaises(DomainError):self.engine.team.answer(q['id'],{'answer':'sure, whatever'})
        self.engine.team.answer(q['id'],{'answer':'Allow all of *.shop.example'})  # typed label (e.g. Telegram) maps to allow_domain
        grant=self.project(run)['browser_grants'][0]
        self.assertEqual((grant['domain'],grant['access'],grant['schemes']),('*.shop.example','interact',['https','http']))
        self.assertTrue(self.engine.browser.request({'url':'https://demo-nhahang.shop.example','access':'interact'},self.agent(),run)['granted'])
        self.assertTrue(self.engine.browser.request({'url':'https://shop.example','access':'read'},self.agent(),run)['granted'])

    def test_apex_request_without_siblings_prefers_exact_origin_and_allow_site_is_exact(self):
        run=self.newrun()
        asked=self.engine.browser.request({'url':'https://shop.example','access':'interact'},self.agent(),run);q=self.store.get('decisions',asked['decision_id'])
        self.assertEqual([a['value'] for a in q['actions']],['allow_site','allow_domain','deny']);self.assertEqual(q['actions'][0]['style'],'primary')
        self.engine.team.answer(q['id'],{'answer':'Allow https://shop.example','browser_action':'allow_site'})
        self.assertEqual(self.project(run)['browser_grants'][0]['origin'],'https://shop.example');self.assertNotIn('domain',self.project(run)['browser_grants'][0])
        self.assertNotIn('granted',self.engine.browser.request({'url':'https://demo.shop.example','access':'interact'},self.agent(),run))
        again=self.engine.browser.request({'url':'https://shop2.shop.example','access':'interact'},self.agent('a-seo'),run)
        # A project already holding a grant on shop.example leads with the wildcard for any further host on it.
        self.assertEqual(self.store.get('decisions',again['decision_id'])['actions'][0]['value'],'allow_domain')

    def test_open_requests_are_deduplicated_by_registrable_domain(self):
        run=self.newrun()
        first=self.engine.browser.request({'url':'https://demo-banle.shop.example','access':'interact'},self.agent('a-content'),run)
        other_run=self.store.get('runs',self.engine.enqueue('a-seo','Check the second shop','peer',root_id=run['id'],project_id=run['project_id'])['id'])
        second=self.engine.browser.request({'url':'https://demo-nhahang.shop.example','access':'interact'},self.agent('a-seo'),other_run)
        self.assertEqual(first['decision_id'],second['decision_id'])
        q=self.store.get('decisions',first['decision_id']);self.assertEqual({r['agent_id'] for r in q['requesters']},{'a-content','a-seo'})
        self.assertEqual(len([d for d in self.store.all('decisions') if d.get('browser_origin')]),1)
        # A different registrable domain is a separate question.
        other=self.engine.browser.request({'url':'https://example.org','access':'interact'},self.agent(),run)
        self.assertNotEqual(other['decision_id'],first['decision_id'])

    def test_deny_and_legacy_answers_still_work(self):
        run=self.newrun();actor=self.agent()
        asked=self.engine.browser.request({'url':'https://demo.shop.example','access':'interact'},actor,run)
        self.engine.team.answer(asked['decision_id'],{'answer':'Deny','browser_action':'deny'})
        self.assertEqual(self.project(run).get('browser_grants',[]),[])
        with self.assertRaises(DomainError):self.engine.browser.request({'url':'https://demo.shop.example','access':'interact'},actor,run)
        # A decision created before action buttons existed (no actions) is answered by its label.
        q=self.store.create('decisions',{'question':'Allow?','context':'','options':[],'agent_id':'a-lead','run_id':run['id'],'project_id':run['project_id'],'channel':'dm-a-lead','root_id':run['id'],
            'kind':'approval','status':'open','blocking':False,'browser_origin':'https://legacy.test','browser_access':'read','browser_allow_answer':'Allow viewing','requesters':[]})
        self.engine.team.answer(q['id'],{'answer':'Allow viewing'})
        self.assertEqual(self.project(run)['browser_grants'][0]['origin'],'https://legacy.test')

    def test_revoke_wildcard_and_covered_origin_message(self):
        run=self.newrun();project=self.grants(run,[{'domain':'*.shop.example','origin':'*.shop.example','access':'interact'},{'origin':'https://keep.test','access':'read'}])
        with self.assertRaises(DomainError) as ctx:self.engine.browser.revoke(project['id'],{'origin':'https://demo.shop.example','version':project['version']})
        self.assertIn('*.shop.example',str(ctx.exception))
        project=self.project(run)
        updated=self.service.action('projects',project['id'],'browser-revoke',{'origin':'*.shop.example','version':project['version']})
        self.assertEqual([g['origin'] for g in updated['browser_grants']],['https://keep.test'])

    # ---- precise errors, redirects and the route guard ----------------------------------------

    def test_check_distinguishes_not_granted_dns_and_blocked_address(self):
        run=self.newrun();self.grants(run,[{'domain':'*.shop.example','origin':'*.shop.example','access':'interact'},{'origin':'https://meta.test','access':'read'}]);project=self.project(run)
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai({'meta.test':'169.254.169.254'})):
            with self.assertRaises(DomainError) as ctx:self.engine.browser._check('https://stranger.com/x',project)
            self.assertIn('is not granted',str(ctx.exception));self.assertIn('browser.request_access',str(ctx.exception))
            with self.assertRaises(ResolveError) as ctx:self.engine.browser._check('https://demo-banle.shop.example',project)
            self.assertIn('Cannot resolve demo-banle.shop.example',str(ctx.exception))
            with self.assertRaises(DomainError) as ctx:self.engine.browser._check('https://meta.test',project)
            self.assertIn('never a browser target',str(ctx.exception))

    def test_guard_policy_navigation_assets_and_private_addresses(self):
        run=self.newrun();self.grants(run,[{'domain':'*.shop.example','origin':'*.shop.example','access':'read'},{'origin':'https://rw.test','access':'interact'}]);b=self.engine.browser
        table={'demo.shop.example':'127.0.0.1','cdn.example.net':'93.184.216.34','intranet.corp':'10.1.2.3','rw.test':'93.184.216.35','meta.test':'169.254.169.254','zero.test':'0.0.0.0'}
        def decide(url,method='GET',nav=False):
            route=FakeRoute(FakeRequest(url,method,nav));state={'blocked':[]}
            b.guard_route(route,run['project_id'],state);return route.action,state['blocked']
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai(table)):
            self.assertEqual(decide('https://demo.shop.example/',nav=True)[0],'continue')              # granted navigation (loopback ok via grant)
            self.assertEqual(decide('https://cdn.example.net/app.js')[0],'continue')              # public subresource
            self.assertEqual(decide('https://cdn.example.net/',nav=True),('abort',[{'site':'https://cdn.example.net','reason':'grant','url':'https://cdn.example.net/'}]))
            self.assertEqual(decide('https://cdn.example.net/track','POST')[0],'abort')           # writes need a grant
            self.assertEqual(decide('https://demo.shop.example/api','POST')[0],'abort')                # read-only grant
            self.assertEqual(decide('https://rw.test/api','POST')[0],'continue')                  # interact grant
            self.assertEqual(decide('http://intranet.corp/x')[0],'abort')                         # private, ungranted
            self.assertEqual(decide('https://meta.test/latest')[0],'abort');self.assertEqual(decide('https://zero.test/')[0],'abort')
            self.assertEqual(decide('https://unresolvable.example/x')[0],'abort')
            self.assertEqual(decide('http://localhost:8765/api')[0],'abort')
            self.engine.browser.control_ports.add(9000);self.grants(run,[{'origin':'http://127.0.0.1:9000','access':'interact'}])
            self.assertEqual(decide('http://127.0.0.1:9000/',nav=True)[0],'abort')                # control plane stays blocked
            self.store.settings({'browser_public_assets':False})
            self.assertEqual(decide('https://cdn.example.net/app.js')[0],'abort')                 # setting turns the relaxation off

    def test_redirect_to_uncovered_origin_names_it(self):
        run=self.newrun();self.grants(run,[{'origin':'https://app.test','access':'read'}]);b=self.engine.browser
        error=b._blocked_error({'site':'https://login.thirdparty.com','reason':'grant'})
        self.assertIn('https://login.thirdparty.com',str(error));self.assertIn('browser.request_access',str(error));self.assertIn('*.thirdparty.com',str(error))

    # ---- host map ----------------------------------------------------------------------------

    def test_host_map_setting_validation_and_cache_reset(self):
        run=self.newrun();b=self.engine.browser
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai({})):
            with self.assertRaises(ResolveError):b.resolver.resolve('demo.shop.example',443)
            b.set_host_map('*.shop.example','127.0.0.1')
            self.assertEqual(b.resolver.resolve('demo.shop.example',443),['127.0.0.1'])
        self.assertEqual(self.store.settings()['browser_host_map'],{'*.shop.example':'127.0.0.1'});self.assertEqual(b._rules(),'MAP *.shop.example 127.0.0.1,MAP shop.example 127.0.0.1')
        for pattern,address in [('*.com','127.0.0.1'),('127.0.0.1','127.0.0.1'),('localhost','127.0.0.1'),('*.shop.example','not-an-ip'),('*.shop.example','169.254.169.254'),('*.shop.example','0.0.0.0'),('bad host','127.0.0.1')]:
            with self.assertRaises(DomainError):b.set_host_map(pattern,address)
        self.assertEqual(b.set_host_map('*.shop.example',None),{})

    def test_hostmap_suggestion_asks_owner_then_maps(self):
        run=self.newrun();actor=self.agent();b=self.engine.browser
        self.grants(run,[{'origin':'https://shop.example','access':'interact'},{'origin':'https://demo-banle.shop.example','access':'interact'}])
        url='https://demo-banle.shop.example';table={'shop.example':'127.0.0.1'}
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai(table)):
            waiting=b._open_checked(url,self.project(run),actor,run)
            q=self.store.get('decisions',waiting['decision_id'])
            self.assertEqual((q['action_field'],q['action_handler'],q['hostmap_pattern'],q['hostmap_address']),('hostmap_action','browser_hostmap','*.shop.example','127.0.0.1'))
            self.assertEqual([a['value'] for a in q['actions']],['map','skip']);self.assertEqual(q['actions'][0]['label'],'Map *.shop.example → 127.0.0.1 for the team browser')
            self.assertEqual(b._open_checked(url,self.project(run),self.agent('a-seo'),run)['decision_id'],q['id'])   # deduplicated
            self.engine.team.answer(q['id'],{'answer':q['actions'][0]['label'],'hostmap_action':'map'})
            self.assertEqual(self.store.settings()['browser_host_map'],{'*.shop.example':'127.0.0.1'})
            self.assertEqual(b._open_checked(url,self.project(run),actor,run),['127.0.0.1'])

    def test_hostmap_skip_is_remembered_and_public_apex_is_not_suggested(self):
        run=self.newrun();actor=self.agent();b=self.engine.browser
        self.grants(run,[{'domain':'*.shop.example','origin':'*.shop.example','access':'interact'},{'domain':'*.pub.com','origin':'*.pub.com','access':'interact'}])
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai({'shop.example':'192.168.1.20','pub.com':'93.184.216.34'})):
            q=self.store.get('decisions',b._open_checked('https://a.shop.example',self.project(run),actor,run)['decision_id'])
            self.engine.team.answer(q['id'],{'answer':'Skip','hostmap_action':'skip'})
            with self.assertRaises(ResolveError):b._open_checked('https://a.shop.example',self.project(run),actor,run)
            with self.assertRaises(ResolveError):b._open_checked('https://missing.pub.com',self.project(run),actor,run)
        self.assertEqual(self.store.settings().get('browser_host_map',{}),{})

    # ---- approval mode ------------------------------------------------------------------------

    def test_mode_resolution_and_validation(self):
        run=self.newrun();modes=self.engine.approvals
        self.assertEqual(modes.mode_for(run),'ask')
        self.assertEqual(modes.set('dm-a-lead','auto'),{'channel':'dm-a-lead','mode':'auto'});self.assertEqual(self.store.settings()['approval_modes'],{'dm-a-lead':'auto'})
        self.assertEqual(modes.mode_for(run),'auto')
        project=self.store.patch('projects',run['project_id'],{'channel':'dm-other'})
        self.assertEqual(modes.mode_for(run),'ask')                                    # project.channel wins
        modes.set('dm-other','auto');self.assertEqual(modes.mode_for(run),'auto')
        self.store.patch('projects',run['project_id'],{'channel':None})
        self.assertEqual(modes.mode_for({'owner_channel':'dm-x','channel':'dm-y'}),'ask');modes.set('dm-x','auto')
        self.assertEqual(modes.mode_for({'owner_channel':'dm-x','channel':'dm-y'}),'auto');self.assertEqual(modes.mode_for({'channel':'dm-x'}),'auto')
        modes.set('dm-x','ask');self.assertEqual(modes.mode_for({'channel':'dm-x'}),'ask')
        for channel,mode in [('','auto'),('dm-a','always'),(None,'ask')]:
            with self.assertRaises(DomainError):modes.set(channel,mode)

    def info(self):
        # Auto-approvals are collapsed action records (kind 'action', audience 'activity'); flatten for text checks.
        return [{**m,'content':m['content']+'\n'+'\n'.join(a['label']+' '+a['detail']+' '+a.get('cwd','') for a in m.get('actions',[]))}
                for m in self.store.all('messages') if m.get('kind')=='action']

    def test_auto_mode_grants_browser_access_and_coalesces_messages(self):
        run=self.newrun();self.engine.approvals.set('dm-a-lead','auto');b=self.engine.browser;actor=self.agent()
        first=b.request({'url':'https://demo-banle.shop.example','access':'interact'},actor,run)
        self.assertEqual((first['granted'],first['auto_approved'],first['covered_by']),(True,True,'*.shop.example'))
        self.assertTrue(b.request({'url':'https://demo-nhahang.shop.example','access':'interact'},actor,run)['granted'])         # covered, no new message
        local=b.request({'url':'http://localhost:4567','access':'interact'},actor,run);self.assertEqual(local['covered_by'],'http://localhost:4567')
        self.assertEqual([g.get('domain') or g['origin'] for g in self.project(run)['browser_grants']],['*.shop.example','http://localhost:4567'])
        self.assertFalse([q for q in self.store.all('decisions') if q.get('browser_origin')])
        messages=self.info();self.assertEqual(len(messages),1)
        self.assertEqual((messages[0]['channel'],messages[0]['audience']),('dm-a-lead','activity'))
        self.assertIn('Auto-approved',messages[0]['content']);self.assertIn('*.shop.example',messages[0]['content']);self.assertIn('http://localhost:4567',messages[0]['content'])
        with patch('agentflow.approvals.time.monotonic',return_value=time.monotonic()+61):
            b.request({'url':'https://example.org','access':'read'},actor,run)
        self.assertEqual(len(self.info()),2)
        self.assertTrue(self.info()[1]['content'].startswith('Auto-approved: Browser access to https://example.org'))

    def test_auto_mode_never_approves_the_control_plane_and_prefers_apex_origin(self):
        run=self.newrun();self.engine.approvals.set('dm-a-lead','auto');self.engine.browser.denied_origins.add('http://127.0.0.1:8765')
        with self.assertRaises(DomainError):self.engine.browser.request({'url':'http://127.0.0.1:8765/api/state'},self.agent(),run)
        self.assertEqual(self.engine.browser.request({'url':'https://shop.example','access':'interact'},self.agent(),run)['covered_by'],'https://shop.example')

    def test_auto_mode_applies_host_map_without_asking(self):
        run=self.newrun();self.engine.approvals.set('dm-a-lead','auto');b=self.engine.browser
        self.grants(run,[{'origin':'https://shop.example','access':'interact'},{'origin':'https://demo-banle.shop.example','access':'interact'}])
        with patch('agentflow.browser.socket.getaddrinfo',side_effect=gai({'shop.example':'127.0.0.1'})):
            self.assertEqual(b._open_checked('https://demo-banle.shop.example',self.project(run),self.agent(),run),['127.0.0.1'])
        self.assertEqual(self.store.settings()['browser_host_map'],{'*.shop.example':'127.0.0.1'})
        self.assertIn('Auto-approved: Map *.shop.example → 127.0.0.1 for the team browser',self.info()[0]['content'])
        self.assertFalse([q for q in self.store.all('decisions') if q.get('hostmap_pattern')])

    def test_browser_restarts_when_host_map_changes(self):
        b=self.engine.browser
        class Closeable:
            closed=0
            def close(self):Closeable.closed+=1
        b.browser=Closeable();b.sessions={'p':{'context':Closeable()}};b.launched_rules=''
        b._sync_rules();self.assertIsNotNone(b.browser)
        b.set_host_map('*.shop.example','127.0.0.1');b._sync_rules()
        self.assertIsNone(b.browser);self.assertEqual((b.sessions,Closeable.closed),({},2))

    # ---- commands -----------------------------------------------------------------------------------

    def command_run(self):
        run=self.newrun('Build and verify a local report');folder=self.root/'project';folder.mkdir()
        self.engine.team.grant_folder(run['project_id'],{'path':str(folder),'access':'readwrite'});return run,folder

    def tool(self,run,name,args):
        actor=self.store.get('agents',run['agent_id']);self.store.patch('agents',actor['id'],{'tools':list(set(actor.get('tools',[])+[name]))})
        return self.engine.tool(self.store.get('agents',actor['id']),{'name':name,'arguments':args},run)

    def execute(self,run,args):
        with patch.object(self.engine.commands,'sandbox_argv',return_value=['sandbox-runner']),patch('agentflow.commands.subprocess.Popen') as launch:
            launch.return_value.communicate.return_value=('ok','');launch.return_value.returncode=0
            return self.tool(run,'workspace.execute',args)

    def test_command_decision_has_four_generic_actions(self):
        run,folder=self.command_run();result=self.tool(run,'workspace.execute',{'command':['python','-c','print(1)']})
        q=self.store.get('decisions',result['decision_id'])
        self.assertEqual([a['value'] for a in q['actions']],['cancel','allow','session','task'])
        self.assertEqual((q['action_field'],q['action_handler']),('command_action','command'))
        self.assertEqual([a['label'] for a in q['actions']][2:],['Allow all commands for 15 minutes',"Allow all commands for this task until it's done"])
        self.assertTrue(q['command_request'])

    def test_task_scoped_command_approval_lasts_until_root_run_finishes(self):
        run,folder=self.command_run();args={'command':['python','-c','print(1)']}
        q=self.store.get('decisions',self.tool(run,'workspace.execute',args)['decision_id'])
        self.engine.team.answer(q['id'],{'answer':'task','command_action':'task'})
        self.assertTrue(self.execute(run,args)['executed']);self.assertTrue(self.execute(run,{'command':['python','-c','print(2)']})['executed'])
        child=self.engine.enqueue('a-code','Work','peer',root_id=run['id'],project_id=run['project_id'])
        self.assertTrue(self.execute(self.store.get('runs',child['id']),{'command':['python','-c','print(3)']})['executed'])   # same root run
        with self.assertRaises(DomainError):self.execute(run,{'command':['python','-c','open(".env").read()']})                    # secrets stay blocked
        self.store.patch('runs',run['id'],{'status':'done'})
        self.assertTrue(self.execute(run,{'command':['python','-c','print(4)']})['waiting_permission'])

    def test_auto_mode_approves_commands_but_keeps_protections(self):
        run,folder=self.command_run();self.engine.approvals.set('dm-a-lead','auto')
        self.assertTrue(self.execute(run,{'command':['python','-c','print(1)']})['executed'])
        self.assertFalse([q for q in self.store.all('decisions') if q.get('command_request')])
        self.assertEqual(len(self.info()),1);self.assertTrue(self.info()[0]['content'].startswith('Auto-approved: Run python -c print(1)'));self.assertEqual(self.info()[0]['actions'][0]['type'],'command');self.assertIn(str(folder),self.info()[0]['content'])
        self.execute(run,{'command':['python','-c','print(2)']});self.assertEqual(len(self.info()),1);self.assertIn('print(2)',self.info()[0]['content'])   # coalesced
        with self.assertRaises(DomainError):self.execute(run,{'command':['python','-c','open(".env").read()']})
        with self.assertRaises(DomainError):self.execute(run,{'command':['python','-c','print(1)'],'cwd':'../outside'})
        self.engine.approvals.set('dm-a-lead','ask')
        self.assertTrue(self.execute(run,{'command':['python','-c','print(9)']})['waiting_permission'])

    def test_folder_access_is_never_auto_approved(self):
        run=self.newrun();self.engine.approvals.set('dm-a-lead','auto')
        result=self.tool(run,'files.request_access',{'path':str(self.root/'secret-data'),'access':'readwrite'})
        self.assertEqual(self.store.get('decisions',result['decision_id'])['status'],'open')

    # ---- web.fetch ----------------------------------------------------------------------------------------

    def test_web_fetch_allows_private_targets_only_with_a_covering_grant(self):
        run=self.newrun();self.grants(run,[{'domain':'*.shop.example','origin':'*.shop.example','access':'read'}]);b=self.engine.browser
        self.store.settings({'browser_host_map':{'*.shop.example':'127.0.0.1'}})
        class Response:
            status=200
            def getheader(self,name,default=''):return 'text/plain' if name=='Content-Type' else default
            def read(self,n):return b'hello local'
        class Conn:
            instances=[]
            def __init__(self,host,address,port=80):Conn.instances.append((host,address,port))
            def request(self,*a,**k):pass
            def getresponse(self):return Response()
            def close(self):pass
        allow=lambda u:b.covers_url(run,u)
        with patch('agentflow.research.PinnedPlainConnection',Conn),patch('agentflow.research.PinnedConnection',Conn),patch('agentflow.research.socket.getaddrinfo',side_effect=gai({})):
            with self.assertRaises(DomainError):public_page('https://demo.shop.example/',allow=lambda u:False,resolve=b.resolver.resolve)       # private, not granted
            page=public_page('http://demo.shop.example/health',allow=allow,resolve=b.resolver.resolve)
            self.assertEqual((page['text'],Conn.instances[-1]),('hello local',('demo.shop.example','127.0.0.1',80)))
            with self.assertRaises(DomainError):public_page('http://demo.shop.example:8080/health',allow=allow,resolve=b.resolver.resolve)   # wildcard covers default ports only
            self.grants(run,[{'origin':'http://demo.shop.example:8080','access':'read'}])
            self.assertEqual(public_page('http://demo.shop.example:8080/health',allow=allow,resolve=b.resolver.resolve)['status'],200)
            self.assertEqual(Conn.instances[-1],('demo.shop.example','127.0.0.1',8080))
            with self.assertRaises(DomainError):public_page('http://demo.stranger.com/',allow=allow,resolve=b.resolver.resolve)           # not covered: http refused
            with self.assertRaises(DomainError):public_page('https://demo.shop.example/',resolve=gai_resolve({'demo.shop.example':'169.254.169.254'}))
            with self.assertRaises(DomainError):public_page('https://demo.shop.example/',allow=allow,resolve=gai_resolve({'demo.shop.example':'169.254.169.254'}))  # link-local never
            public=public_page('https://example.com/',resolve=gai_resolve({'example.com':'93.184.216.34'}))
            self.assertEqual((public['status'],Conn.instances[-1]),(200,('example.com','93.184.216.34',443)))

    # ---- HTTP routes ----------------------------------------------------------------------------------------

    def test_http_routes_for_approval_mode_and_host_map(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.service,'session',0));port=server.server_port
        server.RequestHandlerClass=make_handler(self.service,'session',port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();conn=http.client.HTTPConnection('127.0.0.1',port)
        headers={'Cookie':'af_session=session','Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}'}
        def post(path,body):
            conn.request('POST',path,json.dumps(body),headers);res=conn.getresponse();return res.status,json.loads(res.read())
        try:
            self.assertEqual(post('/api/approval-mode',{'channel':'dm-a-lead','mode':'auto'}),(200,{'channel':'dm-a-lead','mode':'auto'}))
            self.assertEqual(self.store.settings()['approval_modes'],{'dm-a-lead':'auto'});self.assertEqual(post('/api/approval-mode',{'channel':'dm-a-lead','mode':'bogus'})[0],400)
            status,body=post('/api/browser/host-map',{'pattern':'*.shop.example','address':'127.0.0.1'})
            self.assertEqual((status,body['host_map']),(200,{'*.shop.example':'127.0.0.1'}))
            self.assertEqual(post('/api/browser/host-map',{'pattern':'*.com','address':'127.0.0.1'})[0],400)
            status,body=post('/api/browser/host-map',{'pattern':'*.shop.example','address':None});self.assertEqual((status,body['host_map']),(200,{}))
        finally:conn.close();server.shutdown();server.server_close();thread.join(timeout=2)


def gai_resolve(table):
    def resolve(host,port):
        if host not in table:raise socket.gaierror('missing')
        return [table[host]]
    return resolve


if __name__=='__main__':unittest.main()
