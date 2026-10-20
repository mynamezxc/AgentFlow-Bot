import base64
import io
import json
import socket
import http.client
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock,patch
import test_runtime as runtime
from agentflow.browser import origin
from agentflow.contract import SYSTEM_PROMPT,TOOL_SCHEMAS
from agentflow.language import owner_language,english_authoring,VIETNAMESE
from agentflow.store import DomainError
from server import make_handler
from http.server import ThreadingHTTPServer


class RichBrowserTests(unittest.TestCase):
    setUp=runtime.RuntimeTests.setUp
    tearDown=runtime.RuntimeTests.tearDown
    ready=runtime.RuntimeTests.ready
    def newrun(self,text='Build an interactive report'):
        self.ready();return self.engine.chat({'message':text,'agent_id':'a-lead','channel':'dm-a-lead'})
    def call(self,name,args,run):return self.engine.tool(self.store.get('agents','a-lead'),{'name':name,'arguments':args},run)

    def test_language_policy_defaults_and_explicit_requests(self):
        self.assertEqual(owner_language('Build the report'),'en')
        self.assertEqual(owner_language('Tạo báo cáo giúp tôi'),'vi')
        self.assertEqual(owner_language('Tạo báo cáo, trả lời bằng tiếng Anh'),'en')
        self.assertEqual(owner_language('Reply in Vietnamese'),'vi')
        run=self.newrun('Tạo báo cáo giúp tôi')
        child=self.engine.enqueue('a-content','Review the report','peer',root_id=run['id'],project_id=run['project_id'])
        self.assertEqual(child['owner_language'],'vi')
        prompt=self.engine.prompt(self.store.get('agents','a-lead'),run)
        self.assertIn('Owner reply language: vi',prompt)
        self.assertIn('runtime_guides',prompt)
        self.assertFalse(VIETNAMESE.search(SYSTEM_PROMPT))
        self.assertFalse(VIETNAMESE.search(json.dumps(TOOL_SCHEMAS,ensure_ascii=False)))

    def test_authored_skills_tools_require_english_but_preserve_data(self):
        run=self.newrun()
        with self.assertRaises(DomainError):self.call('skills.create',{'name':'Báo cáo','content':'# Báo cáo\nKiểm tra dữ liệu.'},run)
        skill=self.call('skills.create',{'name':'Report verification','content':'# Report verification\nRead sources, verify all values, write evidence.'},run)
        self.assertEqual(self.store.get('skills',skill['id'])['description'],'Reusable operating guidance authored by the team.')
        with self.assertRaises(DomainError):self.call('tools.create',{'name':'report','description':'Tạo báo cáo','inputs':{},'steps':[{'name':'workspace.read','arguments':{'path':'brief.md'}}]},run)
        tool=self.call('tools.create',{'name':'report','description':'Write a localized customer report.','inputs':{},'steps':[{'name':'workspace.write','arguments':{'path':'localized.md','content':'Dữ liệu gốc tiếng Việt'}}]},run)
        self.call('tools.invoke',{'tool_id':tool['id'],'arguments':{}},run)
        self.assertIn('Dữ liệu gốc',(self.engine.workspace/'localized.md').read_text(encoding='utf-8'))

    def test_canvas_registration_preview_and_path_restrictions(self):
        run=self.newrun()
        result=self.call('outputs.canvas',{'path':'reports/canvas.html','title':'Budget explorer','content':'<button>Calculate</button><script>let x=1;</script>'},run)
        preview=self.engine.outputs.preview('artifact',result['id'])
        self.assertEqual(preview['mode'],'canvas');self.assertIn('<script>',preview['content'])
        self.assertNotIn('absolute_path',preview['file'])
        with self.assertRaises(DomainError):self.engine.outputs.open_native('artifact',result['id'])
        with self.assertRaises(DomainError):self.call('outputs.register',{'path':'../secret.pdf'},run)
        with self.assertRaises(DomainError):self.call('outputs.canvas',{'path':'canvas.exe','content':'malicious'},run)

    def test_native_open_is_explicit_safe_copy_and_checks_integrity(self):
        run=self.newrun();path=self.engine.workspace/'report.pdf';path.write_bytes(b'%PDF-1.4\nQA fixture')
        item=self.call('outputs.register',{'path':'report.pdf'},run)
        self.assertEqual(self.engine.outputs.preview('artifact',item['id'])['mode'],'document')
        with patch('agentflow.outputs.os.startfile',create=True) as launch:
            self.engine.outputs.open_native('artifact',item['id'])
            opened=Path(launch.call_args.args[0]);self.assertTrue(opened.is_relative_to(self.store.directory/'open-files'))
            self.assertEqual(opened.read_bytes(),path.read_bytes())
        path.write_bytes(b'%PDF-modified')
        with self.assertRaises(DomainError):self.engine.outputs.open_native('artifact',item['id'])

    def test_office_macros_and_renamed_files_cannot_open(self):
        run=self.newrun()
        for name,raw in [('fake.pdf',b'not a PDF'),('fake.docx',b'not Office')]:
            (self.engine.workspace/name).write_bytes(raw)
            with self.assertRaises(DomainError):self.call('outputs.register',{'path':name},run)
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr('word/document.xml','<document/>');z.writestr('word/VBAPROJECT.bin','macro')
        (self.engine.workspace/'macro.docx').write_bytes(stream.getvalue())
        with self.assertRaises(DomainError):self.call('outputs.register',{'path':'macro.docx'},run)

    def test_browser_access_is_atlas_owned_and_owner_answered(self):
        run=self.newrun('Kiểm tra giao diện website')
        request=self.engine.browser.request({'url':'http://localhost:4567/demo','access':'interact','reason':'Verify UI controls.'},self.store.get('agents','a-content'),run)
        self.assertTrue(request['waiting_permission']);q=self.store.get('decisions',request['decision_id'])
        self.assertEqual((q['agent_id'],q['channel']),('a-lead','dm-a-lead'))
        self.assertEqual(q['browser_origin'],'http://localhost:4567')
        again=self.engine.browser.request({'url':'http://localhost:4567/other','access':'interact'},self.store.get('agents','a-content'),run)
        self.assertEqual(again['decision_id'],q['id'])
        with self.assertRaises(DomainError):self.call('decisions.record_answer',{'decision_id':q['id']},run)
        self.engine.team.answer(q['id'],{'answer':q['browser_allow_answer'],'browser_action':'allow_site'})
        project=self.store.get('projects',run['project_id'])
        self.assertEqual(project['browser_grants'][0]['access'],'interact')
        self.assertTrue(self.engine.browser.request({'url':'http://localhost:4567','access':'interact'},self.store.get('agents','a-content'),run)['granted'])

    def test_browser_origin_network_and_control_plane_boundaries(self):
        for url in ['file:///C:/secret.txt','http://user:password@host','javascript:alert(1)','https://host:bad']:
            with self.assertRaises(DomainError):origin(url)
        self.assertEqual(origin('HTTPS://example.com:443/path'),'https://example.com')
        project={'browser_grants':[{'origin':'https://example.com','access':'read'}]}
        with patch('agentflow.browser.socket.getaddrinfo',return_value=[(None,None,None,None,('93.184.216.34',443))]):
            self.assertTrue(self.engine.browser._allowed('https://example.com/page',project))
            self.assertFalse(self.engine.browser._allowed('https://other.com',project))
        self.engine.browser.resolver.clear()  # lookups are cached for 60 s
        with patch('agentflow.browser.socket.getaddrinfo',return_value=[(None,None,None,None,('169.254.169.254',443))]):self.assertFalse(self.engine.browser._allowed('https://example.com',project))
        self.engine.browser.control_ports.add(8765)
        with self.assertRaises(DomainError):self.engine.browser.request({'url':'http://127.1:8765'},self.store.get('agents','a-lead'),self.newrun())

    def test_browser_read_only_and_stale_snapshot_prevent_actions(self):
        run=self.newrun();actor=self.store.get('agents','a-lead');page=Mock();page.url='https://example.com';session={'page':page,'version':4,'observed':True}
        self.store.patch('projects',run['project_id'],{'browser_grants':[{'origin':'https://example.com','access':'read'}]})
        with patch.object(self.engine.browser,'_session',return_value=session),patch.object(self.engine.browser,'_allowed',return_value=True):
            with self.assertRaises(DomainError):self.engine.browser._invoke('browser.act',{'snapshot_id':4,'action':'click'},actor,run)
            self.store.patch('projects',run['project_id'],{'browser_grants':[{'origin':'https://example.com','access':'interact'}]})
            with self.assertRaises(DomainError):self.engine.browser._invoke('browser.act',{'snapshot_id':2,'action':'click'},actor,run)
        page.get_by_role.assert_not_called()

    def test_owner_can_revoke_browser_access_without_model_authority(self):
        run=self.newrun();site='https://example.com'
        project=self.store.patch('projects',run['project_id'],{'browser_grants':[{'origin':site,'access':'interact'}]})
        with self.assertRaises(DomainError):self.engine.browser.revoke(project['id'],{'origin':site,'version':0})
        updated=self.service.action('projects',project['id'],'browser-revoke',{'origin':site,'version':project['version']})
        self.assertEqual(updated['browser_grants'],[])
        self.assertNotIn('browser.revoke',TOOL_SCHEMAS)

    def test_browser_images_are_real_model_inputs(self):
        run=self.newrun();actor=self.store.get('agents','a-lead')
        page=Mock();page.url='https://example.com';page.screenshot.return_value=b'actual screenshot bytes'
        with patch.object(self.engine.browser,'_session',return_value={'page':page,'canvas':True}):
            result=self.engine.browser._invoke('browser.screenshot',{'title':'Before'},actor,run)
        images=self.engine.browser.images(run)
        self.assertEqual(Path(images[0]['path']).read_bytes(),b'actual screenshot bytes')
        self.assertEqual(self.store.get('artifacts',result['artifact_id'])['kind'],'screenshot')
        child=self.engine.enqueue('a-content','Review the screenshot','peer',root_id=run['id'],project_id=run['project_id'])
        self.engine.outputs.view_image({'artifact_id':result['artifact_id']},child)
        child=self.store.get('runs',child['id'])
        self.assertEqual(self.engine.outputs.images(child)[0]['path'],images[0]['path'])
        other=self.store.create('projects',{'title':'Other'})
        with self.assertRaises(DomainError):self.engine.outputs.view_image({'artifact_id':result['artifact_id']},{**child,'project_id':other['id']})

    def test_canvas_http_auth_csp_and_owner_only_native_open(self):
        run=self.newrun();item=self.call('outputs.canvas',{'path':'c.html','content':'<button>Test</button><script>let x=1;</script>'},run)
        (self.engine.workspace/'qa.pdf').write_bytes(b'%PDF-1.4\nFixture')
        pdf=self.call('outputs.register',{'path':'qa.pdf'},run)
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.service,'session',0));port=server.server_port
        server.RequestHandlerClass=make_handler(self.service,'session',port)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();conn=http.client.HTTPConnection('127.0.0.1',port)
        try:
            conn.request('GET','/api/canvas/'+item['id']);res=conn.getresponse();self.assertEqual(res.status,401);res.read()
            conn.request('GET','/api/canvas/'+item['id'],headers={'Cookie':'af_session=session'});res=conn.getresponse()
            self.assertEqual(res.status,200);csp=res.getheader('Content-Security-Policy');self.assertIn('sandbox allow-scripts',csp);self.assertIn("connect-src 'none'",csp);self.assertIn('<script>',res.read().decode())
            with patch('agentflow.outputs.os.startfile',create=True) as launch:
                url='/api/files/artifact/'+pdf['id']+'/open'
                conn.request('POST',url,'{}',{'Cookie':'af_session=session','Content-Type':'application/json','Origin':'https://evil.example'})
                res=conn.getresponse();self.assertEqual(res.status,403);res.read();launch.assert_not_called()
                conn.request('POST',url,'{}',{'Cookie':'af_session=session','Content-Type':'application/json','Origin':f'http://127.0.0.1:{port}'})
                res=conn.getresponse();self.assertEqual(res.status,200);self.assertTrue(json.loads(res.read())['opened']);launch.assert_called_once()
        finally:conn.close();server.shutdown();server.server_close();thread.join(timeout=2)


if __name__=='__main__':unittest.main()
