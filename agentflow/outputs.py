"""Registered deliverables, immutable reads and owner-initiated desktop opening."""
import difflib
import hashlib
import mimetypes
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from .files import TEXT_EXTENSIONS
from .store import DomainError, uid

def _epoch(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except ValueError:return None


DOCUMENTS={'.pdf','.docx','.xlsx','.pptx'}
EDIT_LIMIT=2*1024*1024
# Files that a desktop app would run or script (Windows Script Host, interpreters, browsers): never opened natively.
NO_NATIVE_OPEN={'.js','.py','.ps1','.sh','.html','.svg'}
IMAGES={'.png','.jpg','.jpeg','.webp'}


class Outputs:
    def __init__(self,engine):self.engine,self.store=engine,engine.store

    def register(self,args,actor,run):
        suffix=Path(str(args.get('path',''))).suffix.lower()
        if suffix not in TEXT_EXTENSIONS|DOCUMENTS|IMAGES:raise DomainError('Unsupported output file type.',415)
        # Registration does not grant access; only files in the company workspace.
        path=self.engine.files.path(args.get('path',''),extensions=TEXT_EXTENSIONS|DOCUMENTS|IMAGES)
        return self._register(path,self.engine.workspace,'workspace',args,actor,run,{'deliverable':True} if args.get('deliverable') is True else None)

    def _register(self,path,root,source,args,actor,run,extra=None,keep_author=False):
        """Verify and (re)register one readable file; `root` is the company workspace or a granted project folder."""
        suffix=path.suffix.lower()
        if not path.is_file() or path.stat().st_size>20*1024*1024:raise DomainError('Output is missing or exceeds 20 MB.')
        raw=path.read_bytes()
        if suffix=='.pdf' and not raw.startswith(b'%PDF-'):raise DomainError('Invalid PDF file.')
        if suffix in DOCUMENTS-{'.pdf'}:
            import io,zipfile
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    needed={'.docx':'word/document.xml','.xlsx':'xl/workbook.xml','.pptx':'ppt/presentation.xml'}[suffix]
                    if needed not in z.namelist() or any('vbaproject' in n.lower() for n in z.namelist()):raise ValueError()
            except (ValueError,zipfile.BadZipFile):raise DomainError('Expected a valid macro-free Office document.') from None
        previous=next((a for a in self.store.all('artifacts') if a.get('absolute_path')==str(path)),None)
        doc={'path':str(path.relative_to(root)), 'absolute_path':str(path),
            'title':str(args.get('title') or path.name)[:250],'project_id':run.get('project_id'),
            'agent_id':actor['id'],'run_id':run['id'],'source':source,'bytes':len(raw),
            'type':suffix[1:],'sha256':hashlib.sha256(raw).hexdigest(),'is_image':suffix in IMAGES,
            'kind':'canvas' if suffix=='.html' and (args.get('kind')=='canvas' or (previous or {}).get('kind')=='canvas') else 'file',**(extra or {})}
        if previous and keep_author:
            # Attaching refreshes the hash; it must not change who authored the file.
            for key in ('agent_id','run_id','project_id','external'):
                if previous.get(key) is not None:doc[key]=previous[key]
            if not args.get('title'):doc['title']=previous.get('title') or doc['title']
        item=self.store.patch('artifacts',previous['id'],doc) if previous else self.store.create('artifacts',doc)
        return {k:v for k,v in item.items() if k!='absolute_path'}

    # ---- attachments (envelope `files`, chat.send `files`) --------------------------------------
    def locate(self,raw,run):
        """(path, root, source, extra) for a company- or project-relative file the agent may attach."""
        text=str(raw or '').strip().replace('\\','/')
        if not text or len(text)>500:raise DomainError('File path is required.')
        files=self.engine.files
        grants=[]
        if run.get('project_id'):
            try:grants=self.store.get('projects',run['project_id']).get('folder_grants',[])
            except DomainError:grants=[]
        exts=TEXT_EXTENSIONS|DOCUMENTS|IMAGES
        workspace=True
        if Path(text).is_absolute():
            path=self.engine.access.normalized(text)
            if path.is_relative_to(self.engine.workspace):text=str(path.relative_to(self.engine.workspace)).replace('\\','/')
            else:
                grant=self.engine.access.covered({'folder_grants':grants},path,'read')
                if not grant:raise DomainError('Outside the company workspace and granted project folders.',403)
                text=str(path.relative_to(Path(grant['path']).resolve())).replace('\\','/');grants=[grant];workspace=False
        if workspace:
            candidate=files.path(text,extensions=exts)
            if candidate.is_file():return candidate,self.engine.workspace,'workspace',{}
        for grant in grants:
            args={'project_id':run['project_id'],'grant_id':grant.get('id'),'path':text}
            try:
                root=files.root(args);rel=args.get('path',text)
                candidate=files.path(rel,root,extensions=exts)
            except DomainError:continue
            if candidate.is_file():return candidate,root,'project',{'grant_id':grant.get('id'),'source_project':run['project_id']}
        raise DomainError('File not found in the company workspace or granted project folders.',404)

    def attach(self,paths,actor,run):
        """Register existing readable files as owner-visible attachments: {'artifact_ids':[..],'notes':[..]}.

        Missing, denied or unsupported files become notes; nothing here raises."""
        if isinstance(paths,str):paths=[paths]
        if not isinstance(paths,list):paths=[]
        ids,notes=[],[]
        for raw in paths[:10]:
            try:
                path,root,source,extra=self.locate(raw,run)
                previous=next((a for a in self.store.all('artifacts') if a.get('absolute_path')==str(path)),None)
                known=previous or any(c.get('path')==str(path.relative_to(root)) and c.get('run_id') for c in self.store.all('file_changes') if c.get('project_id')==run.get('project_id'))
                started=_epoch(run.get('created_at'))
                # A file that predates this run and was never registered is not agent-authored.
                if not known and started and path.stat().st_mtime<started-2:extra={**extra,'external':True}
                # Files an agent explicitly attaches (envelope `files`, chat.send `files`) are for the owner to review.
                if not extra.get('external'):extra={**extra,'deliverable':True}
                item=self._register(path,root,source,{},actor,run,extra,keep_author=True)
                if item['id'] not in ids:ids.append(item['id'])
            except DomainError as exc:notes.append(str(raw)[:200]+': '+str(exc))
            except OSError:notes.append(str(raw)[:200]+': the file could not be read.')
        if len(paths)>10:notes.append('Only the first 10 files were attached.')
        return {'artifact_ids':ids,'notes':notes}

    def canvas(self,args,actor,run):
        if Path(str(args.get('path',''))).suffix.lower()!='.html':raise DomainError('Canvas needs an .html path.')
        result=self.engine.files.write(args,actor,run)
        return {**self.register({**args,'kind':'canvas'},actor,run),'change_id':result['change_id']}

    def view_image(self,args,run):
        item,path=self.resolve('artifact',args.get('artifact_id',''))
        if item.get('project_id')!=run.get('project_id') or not item.get('is_image'):raise DomainError('Choose a real image from the current project.',403)
        current=self.store.get('runs',run['id'])
        self.store.patch('runs',run['id'],{'visual_artifact_ids':list(dict.fromkeys(current.get('visual_artifact_ids',[])+[item['id']]))[-3:]})
        return {'artifact_id':item['id'],'path':item['path'],'image_available':True,'note':'Actual pixels will be attached to your next model turn.'}

    def images(self,run):
        explicit=run.get('visual_artifact_ids',[])
        own=[a['id'] for a in self.store.all('artifacts') if a.get('run_id')==run['id'] and a.get('is_image')]
        result=[]
        for record_id in list(dict.fromkeys(own+explicit))[-3:]:
            item,path=self.resolve('artifact',record_id)
            if item.get('project_id')!=run.get('project_id') or not item.get('is_image'):raise DomainError('Image is not part of this project.',403)
            result.append({'path':str(path),'mime':mimetypes.guess_type(str(path))[0] or 'image/png','name':item['title']})
        return result

    def resolve(self,source,record_id,verify=True):
        if source=='attachment':
            item=self.store.get('attachments',record_id);path=self.engine.attachments.path(item)
        elif source=='artifact':
            item=self.store.get('artifacts',record_id)
            args={'path':item['path']}
            if item.get('source')=='project':args.update(project_id=item.get('source_project') or item['project_id'],grant_id=item.get('grant_id'))
            root=self.engine.files.root(args) if args.get('project_id') else self.engine.workspace
            path=self.engine.files.path(args['path'],root,extensions=TEXT_EXTENSIONS|DOCUMENTS|IMAGES)
            if not path.is_file():raise DomainError('The file is no longer available.',404)
            if verify and item.get('sha256') and hashlib.sha256(path.read_bytes()).hexdigest()!=item['sha256']:
                raise DomainError('File changed after registration. Register it again before opening.',409)
        else:raise DomainError('Unknown file source.',404)
        if path.stat().st_size>20*1024*1024:raise DomainError('File exceeds preview limit.',413)
        return item,path

    def preview(self,source,record_id):
        item,path=self.resolve(source,record_id,verify=False);suffix=path.suffix.lower()  # viewing a file edited elsewhere is fine; opening it natively stays integrity-checked
        public={k:v for k,v in item.items() if k not in ('absolute_path','text')}
        mode='image' if suffix in IMAGES else 'document' if suffix in DOCUMENTS else 'canvas' if item.get('kind')=='canvas' else 'text'
        if mode in ('text','canvas') and path.stat().st_size>3000000:raise DomainError('Text preview exceeds 3 MB; split the file.',413)
        content=self.engine.vault.redact(path.read_text(encoding='utf-8-sig',errors='replace')) if mode in ('text','canvas') else None
        editable,reason=self.editable(source,item,path)
        return {'file':public,'mode':mode,'suffix':suffix,'content':content,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'editable':editable,'readonly_reason':reason,'can_open':suffix not in NO_NATIVE_OPEN}

    # ---- owner edits (preview pane) --------------------------------------------------------------
    def grant_for(self,item):
        project=self.store.get('projects',item.get('source_project') or item.get('project_id'))
        grants=project.get('folder_grants',[]);grant_id=item.get('grant_id')
        return next((g for g in grants if g.get('id')==grant_id),None) if grant_id else (grants[0] if grants else None)

    def editable(self,source,item,path):
        """(bool, reason): may the owner edit this file in the preview pane?"""
        if source!='artifact':return False,'Attachments are read-only.'
        if path.suffix.lower() not in TEXT_EXTENSIONS:return False,'Only text files can be edited here.'
        if path.stat().st_size>EDIT_LIMIT:return False,'Files over 2 MB cannot be edited here.'
        if item.get('source')=='project':
            try:grant=self.grant_for(item)
            except DomainError:grant=None
            if not grant or grant.get('access')!='readwrite':return False,'Folder is read-only for agents and edits.'
        elif not path.resolve().is_relative_to(self.engine.workspace.resolve()):return False,'This file is outside the company workspace.'
        raw=path.read_bytes()
        try:text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:return False,'The file is not UTF-8 text.'
        if '\x00' in text:return False,'Binary files cannot be edited.'
        if self.engine.vault.redact(text)!=text:return False,'This file contains secrets that are hidden in the preview; edit it outside AgentFlow.'
        return True,''

    def save(self,source,record_id,data):
        """Owner-only save from the preview pane: conflict-checked, atomic, line endings kept, recorded as a file change."""
        if source!='artifact':raise DomainError('Attachments are read-only.',403)
        content=data.get('content');base=str(data.get('base_sha256') or '')
        if not isinstance(content,str):raise DomainError('Send the file content as text.')
        if len(content.encode('utf-8'))>EDIT_LIMIT:raise DomainError('Files over 2 MB cannot be edited here.',413)
        if '\x00' in content:raise DomainError('Binary content cannot be saved.')
        item,path=self.resolve('artifact',record_id,verify=False)
        editable,reason=self.editable('artifact',item,path)
        if not editable:raise DomainError(reason or 'This file cannot be edited.',403)
        raw=path.read_bytes();current=hashlib.sha256(raw).hexdigest()
        if not base or base!=current:
            error=DomainError('The file changed since you opened it. Reload it before saving so no work is overwritten.',409)
            error.extra={'current_sha256':current}
            raise error
        bom=raw.startswith(b'\xef\xbb\xbf');before=raw[3:].decode('utf-8') if bom else raw.decode('utf-8')
        crlf=raw.count(b'\r\n')*2>=raw.count(b'\n') and b'\r\n' in raw
        text=content.replace('\r\n','\n').replace('\r','\n')
        payload=(b'\xef\xbb\xbf' if bom else b'')+(text.replace('\n','\r\n') if crlf else text).encode('utf-8')
        if payload==raw:return {**self.preview('artifact',record_id),'change_id':None}
        temporary=path.with_name(path.name+'.agentflow-tmp')
        try:
            temporary.write_bytes(payload);os.replace(temporary,path)
        finally:
            if temporary.exists():temporary.unlink()
        shown_before,shown_after=before.replace('\r\n','\n'),text
        added=deleted=0
        for tag,i,j,k,l in difflib.SequenceMatcher(None,shown_before.splitlines(),shown_after.splitlines()).get_opcodes():
            if tag in ('insert','replace'):added+=l-k
            if tag in ('delete','replace'):deleted+=j-i
        with self.store.transaction():
            saved=self.store.patch('artifacts',item['id'],{'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload),'mtime':path.stat().st_mtime,'edited_by':'owner'})
            change=self.store.create('file_changes',{'artifact_id':saved['id'],'project_id':saved.get('project_id'),'run_id':None,'agent_id':'owner',
                'path':saved['path'],'title':saved.get('title') or saved['path'],'kind':'modified','before':shown_before,'after':shown_after,
                'added':added,'deleted':deleted,'before_sha256':hashlib.sha256(shown_before.encode()).hexdigest(),'after_sha256':hashlib.sha256(shown_after.encode()).hexdigest()})
            project=None
            if saved.get('project_id'):
                try:project=self.store.get('projects',saved['project_id'])
                except DomainError:project=None
            self.store.create('messages',{'agent_id':'a-lead','role':'assistant','content':'Owner edited '+saved['path'],'kind':'progress','audience':'activity',
                'channel':(project or {}).get('channel') or 'dm-a-lead','project_id':saved.get('project_id'),'run_id':None})
        return {**self.preview('artifact',record_id),'change_id':change['id']}

    def open_native(self,source,record_id):
        item,path=self.resolve(source,record_id)
        if path.suffix.lower() in NO_NATIVE_OPEN:raise DomainError('Scripts and web files are not opened in a desktop app, because they could run code. Download it or edit it in the preview pane.',403)
        # Open a verified copy, never a user-controlled executable/URI or shell string.
        raw=path.read_bytes()
        if path.suffix.lower()=='.pdf' and not raw.startswith(b'%PDF-'):raise DomainError('Invalid PDF file.')
        if path.suffix.lower() in DOCUMENTS-{'.pdf'}:
            import io,zipfile
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    required={'.docx':'word/document.xml','.xlsx':'xl/workbook.xml','.pptx':'ppt/presentation.xml'}[path.suffix.lower()]
                    if required not in z.namelist() or any('vbaproject' in n.lower() for n in z.namelist()):raise ValueError()
            except (ValueError,zipfile.BadZipFile):raise DomainError('Only valid macro-free Office documents can open in a desktop app.') from None
        folder=self.store.directory/'open-files';folder.mkdir(exist_ok=True)
        copy=folder/(uid('open')+'-'+path.name);copy.write_bytes(raw)
        try:
            if os.name=='nt':os.startfile(str(copy))
            elif sys.platform=='darwin':subprocess.Popen(['open',str(copy)])
            else:subprocess.Popen(['xdg-open',str(copy)])
        except OSError:raise DomainError('No desktop application could open this file. Download it and choose an application.',502) from None
        return {'opened':True,'name':item.get('name') or path.name}
