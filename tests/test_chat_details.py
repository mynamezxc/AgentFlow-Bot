import base64
import copy
import io
import json
import struct
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

import test_runtime as runtime
from agentflow.store import DomainError
from agentflow.identity import FACES, face_for
from agentflow.models import ModelUnavailable


class ChatDetailsTests(unittest.TestCase):
    setUp=runtime.RuntimeTests.setUp
    tearDown=runtime.RuntimeTests.tearDown
    ready=runtime.RuntimeTests.ready

    def upload(self,name,raw):
        return self.engine.attachments.upload({'name':name,'data':base64.b64encode(raw).decode()})

    def newrun(self,**extra):
        self.ready()
        return self.engine.chat({'message':'QA project','agent_id':'a-lead','channel':'dm-a-lead',**extra})

    def image(self):
        def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
        raw=b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',2,2,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\x00'+b'\x00\xff\x00'*2+b'\x00'+b'\x00\xff\x00'*2))+chunk(b'IEND',b'')
        return self.upload('input.png',raw)

    def test_upload_rejects_path_binary_and_mismatched_image(self):
        for name,raw in [('../input.txt',b'x'),('x.exe',b'x'),('x.png',b'not PNG'),('x.txt',b'a\0b'),('x.txt',b'\xff')]:
            with self.subTest(name=name),self.assertRaises(DomainError): self.upload(name,raw)
        with self.assertRaises(DomainError): self.engine.attachments.upload({'name':'a.txt','data':'%%%bad'})

    def test_actual_attachment_text_and_image_forwarded_and_inherited(self):
        text=self.upload('facts.txt','Chỉ số chính: 7319'.encode())
        image=self.image()
        run=self.newrun(attachment_ids=[text['id'],image['id']])
        self.assertIn('Chỉ số chính: 7319',self.engine.prompt(self.store.get('agents','a-lead'),run))
        pictures=self.engine.attachments.images(run)
        self.assertEqual(Path(pictures[0]['path']).read_bytes(),self.engine.attachments.path(image).read_bytes())
        child=self.engine.enqueue('a-content','Review','peer',root_id=run['id'],project_id=run['project_id'])
        self.assertEqual(child['attachment_ids'],run['attachment_ids'])
        follow=self.engine.chat({'message':'Nêu lại chỉ số','agent_id':'a-lead','channel':run['channel'],'project_id':run['project_id']})
        self.assertEqual(follow['attachment_ids'],run['attachment_ids'])
        self.assertNotIn('text',next(i for i in self.store.snapshot()['attachments'] if i['id']==text['id']))

    def test_attachment_scope_and_integrity(self):
        text=self.upload('facts.txt',b'confidential project A')
        run=self.newrun(attachment_ids=[text['id']])
        other=self.store.create('projects',{'title':'B','status':'active'})
        with self.assertRaises(DomainError):self.engine.attachments.bind([text['id']],other['id'])
        with self.assertRaises(DomainError):self.engine.attachments.context({**run,'project_id':other['id']})
        self.engine.attachments.path(text).write_bytes(b'changed')
        with self.assertRaises(DomainError):self.engine.attachments.images({**run,'attachment_ids':[text['id']]}) if text['is_image'] else self.engine.attachments.bind([text['id']],run['project_id'])

    def test_docx_and_xlsx_extract_actual_text_not_filename(self):
        def archive(entries):
            out=io.BytesIO()
            with zipfile.ZipFile(out,'w') as z:
                for name,value in entries:z.writestr(name,value)
            return out.getvalue()
        doc=self.upload('brief.docx',archive([('word/document.xml','<document><p><t>Campaign 7319</t></p></document>')]))
        self.assertIn('Campaign 7319',doc['text'])
        sheet=self.upload('budget.xlsx',archive([('xl/worksheets/sheet1.xml','<worksheet><row><c r="A1" t="s"><v>0</v></c></row></worksheet>'),('xl/sharedStrings.xml','<sst><si><t>Revenue 7319</t></si></sst>')]))
        self.assertIn('A1: Revenue 7319',sheet['text'])

    def test_diff_history_is_real_immutable_and_snapshot_bounded(self):
        run=self.newrun();actor=self.store.get('agents','a-lead')
        first=self.engine.files.write({'path':'plan.md','content':'before\nkeep\n'},actor,run)
        second=self.engine.files.write({'path':'plan.md','content':'after\nkeep\nnew\n'},actor,run)
        third=self.engine.files.write({'path':'plan.md','content':'after\nkeep\nnew\n'},actor,run)
        self.assertIsNone(third['change_id'])
        change=self.engine.files.change(second['change_id'])
        self.assertEqual((change['before'],change['after']),('before\nkeep\n','after\nkeep\nnew\n'))
        self.assertEqual((change['added'],change['deleted']),(2,1))
        self.assertTrue(any(l['kind']=='delete' and l['text']=='before' for l in change['diff']))
        self.assertIsNone(self.engine.files.change(first['change_id'])['before'])
        self.assertNotIn('after',self.store.snapshot()['file_changes'][0])
        self.assertIn(str(self.engine.workspace),self.store.get('projects',run['project_id'])['context_paths'])

    def test_checklist_edit_order_preserves_people_and_follows_task(self):
        run=self.newrun();actor=self.store.get('agents','a-lead')
        task=self.store.create('tasks',{'title':'Write','agent_id':'a-content','project_id':run['project_id'],'status':'ready'})
        derived=self.engine.progress.items(run['project_id'])[0]
        self.assertEqual(derived['status'],'todo')
        self.assertTrue(derived['derived'])
        result=self.engine.progress.set({'items':[{'title':'Research','agent_ids':['a-content'],'position':8}]},actor,run)
        item=result['items'][0]
        self.engine.progress.set({'items':[{'id':item['id'],'status':'doing'}]},actor,run)
        saved=self.store.get('checklist',item['id'])
        self.assertEqual((saved['agent_ids'],saved['position']),(['a-content'],8))
        linked=self.service.create('checklist',{'title':'Write draft','task_id':task['id'],'project_id':run['project_id']})
        self.store.patch('tasks',task['id'],{'status':'done'})
        self.assertEqual(next(c for c in self.engine.progress.items(run['project_id']) if c['id']==linked['id'])['status'],'done')
        self.service.update('checklist',linked['id'],{'archived':True,'version':linked['version']})
        self.assertFalse(any(c.get('task_id')==task['id'] for c in self.engine.progress.items(run['project_id'])))

    def test_checklist_cannot_cross_projects_or_use_unknown_people(self):
        run=self.newrun();actor=self.store.get('agents','a-lead')
        other=self.store.create('projects',{'title':'B'})
        task=self.store.create('tasks',{'title':'B','project_id':other['id'],'status':'ready','agent_id':'a-content'})
        with self.assertRaises(DomainError): self.engine.progress.set({'items':[{'title':'Bad','task_id':task['id']}]},actor,run)
        with self.assertRaises(DomainError): self.engine.progress.set({'items':[{'title':'Bad','agent_ids':['unknown']}]},actor,run)

    def test_private_peer_messages_do_not_notify_owner_telegram(self):
        run=self.newrun();peer={**run,'kind':'peer','outbound':{'channel':'telegram','chat_id':'fake'}}
        with patch.object(self.engine,'queue_reply') as outbound:
            private=self.engine.team.emit('a-content','Internal draft',peer)
            self.assertEqual(private['audience'],'internal');outbound.assert_not_called()
            report=self.engine.team.invoke('chat.send',{'content':'Report @{a-content}','audience':'owner'},self.store.get('agents','a-content'),peer)
            self.assertEqual(self.store.get('messages',report['message_id'])['audience'],'owner')
            outbound.assert_called_once()

    def test_faces_are_stable_validated_and_discoverable(self):
        run=self.newrun();actor=self.store.get('agents','a-lead')
        item=self.service.create('agents',{'name':'A','role':'QA','provider':'codex','color':'mint','face':'cat'})
        self.assertEqual((item['face'],item['color']),('cat','mint'))
        self.assertIn(face_for(item['id']),FACES)
        with self.assertRaises(DomainError):self.service.update('agents',item['id'],{'face':'<img>','version':item['version']})
        team=self.engine.context(actor,run=run)['team']
        self.assertEqual(next(a for a in team if a['id']==item['id'])['face'],'cat')

    def test_gateway_sends_pixels_in_multimodal_request(self):
        image=self.image();run=self.newrun(attachment_ids=[image['id']])
        actor={**self.store.get('agents','a-lead'),'input_images':self.engine.attachments.images(run)}
        session=self.providers.accounts.check('gateway',force=True)
        self.providers.models=[{'id':actor['model'],'provider':'gateway','account_fingerprint':session['fingerprint']}]
        with patch.object(self.providers,'gateway',return_value={'choices':[{'message':{'content':'Seen'}}]}) as gateway:
            self.providers.infer(actor,'Describe pixels',run['id'])
            content=gateway.call_args.args[1]['messages'][0]['content']
            self.assertEqual(content[0],{'type':'text','text':'Describe pixels'})
            self.assertTrue(content[1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_native_clis_receive_actual_image_payloads(self):
        image=self.image();run=self.newrun(attachment_ids=[image['id']]);pictures=self.engine.attachments.images(run)
        for provider in ('codex','claude'):
            with self.subTest(provider=provider):
                actor={**self.store.get('agents','a-lead'),'provider':provider,'input_images':pictures}
                session=self.providers.accounts.check(provider,force=True)
                self.providers.models=[{'id':actor['model'],'provider':provider,'account_fingerprint':session['fingerprint']}];self.providers.cli[provider]='native.exe'
                process=Mock(returncode=0)
                result={'type':'result','result':'Seen','is_error':False}
                output=json.dumps(result) if provider=='claude' else json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Seen'}})
                process.communicate.return_value=(output,'')
                with patch('agentflow.providers.subprocess.Popen',return_value=process) as launch:
                    self.providers.infer(actor,'Describe pixels',run['id'])
                    args=launch.call_args.args[0]
                    if provider=='codex':self.assertEqual(args[args.index('--image')+1],pictures[0]['path'])
                    else:
                        payload=json.loads(process.communicate.call_args.args[0]);content=payload['message']['content']
                        self.assertEqual(content[1]['type'],'image');self.assertEqual(base64.b64decode(content[1]['source']['data']),Path(pictures[0]['path']).read_bytes())
                        self.assertIn('stream-json',args)

    def test_generated_image_is_listed_as_real_output(self):
        image=self.image();run=self.newrun();actor=self.store.get('agents','a-lead')
        self.store.settings({'image_model':'test-image'})
        raw=self.engine.attachments.path(image).read_bytes()
        with patch.object(self.providers,'gateway',return_value={'data':[{'b64_json':base64.b64encode(raw).decode()}]}):
            result=self.engine.tool(actor,{'name':'image.generate','arguments':{'prompt':'QA'}},run)
        artifact=self.store.get('artifacts',result['artifact_id'])
        self.assertTrue(artifact['is_image']);self.assertEqual(artifact['project_id'],run['project_id'])
        self.assertEqual(Path(artifact['absolute_path']).read_bytes(),raw)


if __name__=='__main__':unittest.main()
