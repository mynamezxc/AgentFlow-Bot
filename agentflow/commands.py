"""Folder-scoped command execution with owner approvals and an OS sandbox."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .contract import TERMINAL
from .store import DomainError, now


class Commands:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.sessions = {}  # Deliberately lost on restart; no model can extend it.
        self.lock = threading.RLock()

    def normalize(self, args, run, scope=None):
        command = args.get('command')
        if not isinstance(command, list) or not 1 <= len(command) <= 80 or not all(isinstance(s,str) and 0 < len(s) < 8000 and '\x00' not in s for s in command):
            raise DomainError('Use an argv command array without shell expressions in the program name.')
        command=list(command)
        if Path(command[0]).stem.casefold() in ('powershell','pwsh'):
            for flag in ('-NoProfile','-NonInteractive'):
                if flag.casefold() not in [s.casefold() for s in command[1:]]:command.insert(1,flag)
        scoped = {**args}
        company = scope == 'company'  # Python-level only (agent script tools run in the company workspace, never from model args)
        if run.get('project_id') and not company:
            if args.get('project_id') not in (None, run['project_id']):
                raise DomainError('Cannot execute commands in another project.',403)
            scoped['project_id'] = run['project_id']
        root = self.engine.files.root(scoped) if scoped.get('project_id') else self.engine.workspace
        if scoped.get('_file_scope'):
            raise DomainError('Running commands requires folder access; a single-file grant is insufficient.',403)
        cwd = self.engine.files.path(args.get('cwd',''),root)
        if not cwd.is_dir():
            raise DomainError('The working directory does not exist. Use project.mkdir or workspace.mkdir first.',404)
        # Validate path-shaped arguments as well as enforcing the OS boundary below.
        for token in command:
            if re.search(r'(?i)(?:\.ssh|\.aws|\.codex|\.claude|\.agentflow|credentials\.(?:json|bin)|auth\.json|\.env(?:\W|$))',token):
                raise DomainError('Commands cannot access account stores or secrets.',403)
        return list(command), root, cwd, scoped

    @staticmethod
    def directory_command(command):
        if command[0].casefold() in ('mkdir','md') and len(command)==2:return command[1]
        if Path(command[0]).stem.casefold() not in ('powershell','pwsh'):return None
        if '-command' not in [t.casefold() for t in command[1:-1]]:return None
        script=command[-1]
        if re.search(r'[$;|&<>`(){}\r\n]',script):return None
        tokens=re.findall(r'"[^"]*"|\x27[^\x27]*\x27|\S+',script)
        tokens=[t[1:-1] if len(t)>1 and t[0]==t[-1] and t[0] in '\x27"' else t for t in tokens]
        if len(tokens)==2 and tokens[0].casefold() in ('mkdir','md'):return tokens[1]
        if not tokens or tokens[0].casefold()!='new-item':return None
        options={};i=1
        while i<len(tokens):
            flag=tokens[i].casefold()
            if flag=='-force':i+=1;continue
            if flag not in ('-itemtype','-path','-literalpath') or i+1>=len(tokens):return None
            if flag in options:return None
            options[flag]=tokens[i+1];i+=2
        if options.get('-itemtype','').casefold()!='directory':return None
        return options.get('-literalpath') or options.get('-path')

    @staticmethod
    def routine(command):
        exe=Path(command[0]).stem.casefold()
        tail=command[1:]
        if exe in ('rg','ripgrep','tree','ls','pwd','where','whereis'):
            return not any(s.startswith(('--pre','--pre-glob')) for s in tail)
        if exe=='git':
            if tail and tail[0]=='branch':
                return all(v in ('--list','--show-current','-a','-r','-v','-vv') for v in tail[1:])
            return bool(tail) and tail[0] in ('status','diff','log','show','ls-files','rev-parse','branch') and not any(s.startswith(('--exec','--output','--ext-diff','--textconv')) for s in tail)
        if exe in ('powershell','pwsh'):
            script=tail[-1] if tail else ''
            return (len(tail)>=2 and '-command' in [t.casefold() for t in tail[:-1]] and
                all(t.casefold() in ('-nologo','-noprofile','-noninteractive','-command') for t in tail[:-1]) and
                bool(re.fullmatch(r'(?i)(?:Get-ChildItem|Get-Content|Get-Location|Test-Path|Select-String)(?:\s+[\w./\\: *?\x27\x22=-]+)?',script)))
        if exe in ('python','python3','py'):
            return tail in (['--version'],['-V']) or len(tail)>=2 and tail[:2] in (['-m','unittest'],['-m','pytest'],['-m','compileall'])
        if exe in ('node','nodejs'):
            return tail==['--version'] or bool(tail) and tail[0] in ('--check','--test')
        if exe in ('npm','npm.cmd','pnpm','yarn'):
            return tail==['--version'] or bool(tail) and tail[0]=='test' or len(tail)>=2 and tail[:2] in (['run','build'],['run','lint'],['run','test'],['run','typecheck'])
        return False

    def task_allowed(self, run, root):
        """'Allow all commands for this task' (keyed by root run + folder) lasts until the root run is terminal."""
        key = ('task', run.get('root_id', run['id']), str(root))
        with self.lock:
            if key not in self.sessions:
                return False
        try:
            alive = self.store.get('runs', key[1])['status'] not in TERMINAL
        except DomainError:
            alive = False
        if not alive:
            with self.lock:
                self.sessions.pop(key, None)
        return alive

    def request(self, actor, args, run, command, root, cwd):
        key=hashlib.sha256(json.dumps([run['id'],command,str(cwd)],sort_keys=True).encode()).hexdigest()
        prior=next((q for q in reversed(self.store.all('decisions')) if q.get('command_key')==key),None)
        if prior and prior['status']=='answered':
            if prior.get('command_action')=='cancel':
                return {'executed':False,'cancelled':True,'reason':'Owner cancelled this command.'}
            if prior.get('command_action')=='allow' and not prior.get('command_consumed'):
                # A single approval is consumed before spawn and is never replayed.
                self.store.patch('decisions',prior['id'],{'command_consumed':True})
                return None
        approvals=getattr(self.engine,'approvals',None)
        if (not prior or prior['status']!='open') and approvals and approvals.auto(run):
            # "Always approve for me": secret-path checks, folder grants and the OS sandbox already ran/still run.
            line=subprocess.list2cmdline(command)
            approvals.notify(run,'Run '+line,kind='command',detail=line,cwd=cwd)
            return None
        if not prior or prior['status']!='open':
            approval=self.engine.enqueue('a-lead','Review a command execution request.','command_approval',
                project_id=run.get('project_id'),root_id=run.get('root_id',run['id']),channel='dm-a-lead',status='waiting_user',system_managed=True)
            actions=[{'value':'cancel','label':'Cancel','style':'quiet'},
                {'value':'allow','label':'Allow','style':'primary','hint':'Run only this command once.'},
                {'value':'session','label':'Allow all commands for 15 minutes','style':'quiet','hint':'In this folder; secrets stay blocked.'},
                {'value':'task','label':"Allow all commands for this task until it's done",'style':'quiet','hint':'In this folder, until the task finishes.'}]
            asked=self.engine.team.ask(self.store.get('agents','a-lead'),{
                'question':'Allow this agent to run the command in the approved folder?',
                'context':'@{'+actor['id']+'}\nWorking directory: '+str(cwd)+'\n```text\n'+subprocess.list2cmdline(command)+'\n```\nThe command runs in the folder sandbox without account credentials.',
                'blocking':True,'options':[a['label'] for a in actions],'actions':actions,'action_field':'command_action','action_handler':'command'},approval,_routed=True)
            prior=self.store.patch('decisions',asked['decision_id'],{'command_request':True,'command_key':key,
                'command':command,'command_root':str(root),'command_cwd':str(cwd),'requester_run_id':run['id']})
        current=self.store.get('runs',run['id'])
        self.store.patch('runs',run['id'],{'wait_for_decisions':list(dict.fromkeys(current.get('wait_for_decisions',[])+[prior['id']]))})
        return {'waiting_permission':True,'decision_id':prior['id'],'coordinator_id':'a-lead'}

    def approval(self, question, answer):
        action=answer.get('command_action')
        if action not in ('cancel','allow','session','task'):
            raise DomainError('Choose Cancel, Allow, Allow all commands for 15 minutes, or Allow all commands for this task.')
        if action=='session':
            with self.lock:
                self.sessions[(question.get('project_id'),question['command_root'])]=time.monotonic()+900
        elif action=='task':
            requester=self.store.get('runs',question['requester_run_id'])
            with self.lock:
                self.sessions[('task',requester.get('root_id',requester['id']),question['command_root'])]=float('inf')
        self.store.patch('decisions',question['id'],{'command_action':action,'command_consumed':action!='allow',
            'command_session_expires_at':time.time()+900 if action=='session' else None})
        self.store.patch('runs',question['run_id'],{'status':'done','completed_at':now()})

    def sandbox_argv(self, command, root, cwd, write):
        codex=self.engine.providers.cli.get('codex')
        if os.name!='nt' or not codex:
            raise DomainError('Windows commands require a sandbox-capable Codex CLI. Unrestricted host execution is not used as a fallback.',503)
        executable=command[0]
        if not Path(executable).is_absolute():
            executable=shutil.which(executable)
        if not executable or not Path(executable).is_file():
            raise DomainError('Program not found: '+command[0],404)
        executable=Path(executable).resolve()
        home=Path(os.getenv('USERPROFILE') or Path.home())
        permissions={':root':'deny',':minimal':'read',str(root):'write' if write else 'read',str(executable.parent):'read',
            str(self.store.directory.resolve()):'deny'}
        for name in ('.codex','.claude','.ssh','.aws','.config'):
            permissions[str(home/name)]='deny'
        # Exact deny entries work even for a broad root grant; no credential env is inherited.
        for folder,dirs,files in os.walk(root):
            dirs[:]=[n for n in dirs if n not in ('node_modules','.venv','.git','__pycache__')]
            for name in dirs+files:
                if name.startswith('.') or name.casefold().startswith(('secret','credential','auth.json')):
                    permissions[str(Path(folder)/name)]='deny'
        table='{'+','.join(json.dumps(k)+'='+json.dumps(v) for k,v in permissions.items())+'}'
        child=[str(executable),*command[1:]]
        if executable.suffix.casefold() in ('.cmd','.bat'):
            if any(re.search(r'[\r\n%!&|<>^()]',s) for s in child):
                raise DomainError('This batch command contains shell control characters. Run the executable directly or use a script in the approved folder.',403)
            child=[str(Path(os.environ.get('SYSTEMROOT','C:/Windows'))/'System32/cmd.exe'),'/d','/s','/c','"'+subprocess.list2cmdline(child)+'"']
        argv=[codex,'sandbox','-C',str(cwd),'-P','agentflow-command',
            '-c','windows.sandbox="mxc"','-c','permissions.agentflow-command.filesystem='+table,
            '-c','permissions.agentflow-command.network.enabled=false','--',*child]
        return argv

    def execute(self,args,actor,run,scope=None):
        command,root,cwd,scoped=self.normalize(args,run,scope)
        # Directory creation has a narrow broker implementation and needs no shell or Docker.
        directory=self.directory_command(command)
        if directory is not None:
            target=(cwd/directory).resolve()
            if not target.is_relative_to(root):raise DomainError('Cannot create folders outside the granted scope.',403)
            return self.engine.files.mkdir({**scoped,'path':str(target.relative_to(root))},actor,run,bool(scoped.get('project_id')))
        routine=self.routine(command)
        write=not (routine and (Path(command[0]).stem.casefold() in ('rg','ripgrep','tree','ls','pwd','where','whereis','git','powershell','pwsh') or command[1:] in (['--version'],['-V'])))
        if scoped.get('project_id') and write:
            self.engine.files.root(scoped,write=True)
        with self.lock:
            session=self.sessions.get((run.get('project_id'),str(root)),0)>time.monotonic()
        session=session or self.task_allowed(run,root)
        if not routine and not session:
            decision=self.request(actor,args,run,command,root,cwd)
            if decision is not None:return decision
        argv=self.sandbox_argv(command,root,cwd,write)
        env={k:v for k,v in os.environ.items() if k.upper() in ('PATH','SYSTEMROOT','SYSTEMDRIVE','WINDIR','COMSPEC','PATHEXT',
            'PROGRAMFILES','PROGRAMFILES(X86)','PROGRAMW6432','COMMONPROGRAMFILES','COMMONPROGRAMFILES(X86)','COMMONPROGRAMW6432',
            'PROGRAMDATA','ALLUSERSPROFILE','PUBLIC','OS','PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS',
            'USERPROFILE','HOMEDRIVE','HOMEPATH','TEMP','TMP','LOCALAPPDATA','APPDATA')}
        env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_TERMINAL_PROMPT='0',PYTHONNOUSERSITE='1')
        stdin=args.get('stdin') if isinstance(args.get('stdin'),str) else None  # script tools pass JSON arguments on stdin
        process=subprocess.Popen(argv,cwd=cwd,env=env,stdin=subprocess.PIPE if stdin is not None else None,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            text=True,encoding='utf-8',errors='replace',creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.engine.providers.processes[run['id']]=process
        try:
            stdout,stderr=process.communicate(input=stdin,timeout=min(300,max(5,int(args.get('timeout',60)))))
        except subprocess.TimeoutExpired:
            self.engine.providers.cancel(run['id'])
            process.communicate()
            raise DomainError('Command timed out. The process was stopped.',408) from None
        finally:
            self.engine.providers.processes.pop(run['id'],None)
        return {'executed':True,'sandbox':'Windows folder permissions; network disabled','cwd':str(cwd),'exit_code':process.returncode,
            'stdout':self.engine.vault.redact(stdout[-20000:]),'stderr':self.engine.vault.redact(stderr[-10000:])}
