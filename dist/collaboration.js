import {agentMention,formatMessage,ownerVisible,agentGroup} from './identity.js';
import {renderMarkdown} from './rich-content.js';
import {createPreviewPane} from './preview-pane.js';
import {reltimeMarkup,stableMarkup,lastActivity,agentActivity,activityText,ensureTicker,familyRuns,stuckInfo,THINK_LIMIT_MS} from './reltime.js';
import {usageMini,setupUsagePopovers,updateUsage,withRetained} from './usage-ui.js';
import {decisionCard,decisionActions,actionPayload,decisionBar,stepIndex,groupDecisions} from './decisions.js';
import {createSlash,parseSlash,skillChipsMarkup,isUnavailable,commandById} from './slash.js';
import {messageFilesMarkup,resolveFiles} from './message-files.js';
import {setupLightbox} from './lightbox.js';
import {openImageStudio} from './image-studio.js';
import {galleryMarkup,galleryTiles,imageCard} from './rich-content.js';
import {isActivity,renderWorkLog} from './worklog.js';
import {budgetMarkup,budgetSummary,projectRoot} from './budget-ui.js';
import {chooseProject,roomProjects,goalsMarkup,forYouFiles,changedAfterForYou,agentEventMarkup} from './right-bar.js';
import {morph,compactFolders,groupChanges,limitRows,rosterGroups,rosterMatches,isImagePath,GROUP_THRESHOLD} from './layout.js';
export function createCollaboration(h) {
  setupUsagePopovers();setupLightbox();ensureTicker();
  const {esc,icon,avatar,button,field,api,refresh,toast,openModal,closeModal}=h;
  const $=s=>document.querySelector(s);
  const d=()=>h.getData();
  const list=k=>(d()[k]||[]).filter(x=>!x.archived&&!x.sample);
  const actor=id=>list('agents').find(a=>a.id===id)||(id==='owner'?{id,name:'You',role:'Owner',color:'purple',avatar:'Y'}:{id,name:'Agent',role:'',color:'purple',avatar:'A'});
  const terminal=new Set(['done','failed','cancelled']);
  const statusNames={queued:'Queued',running:'Working',waiting_user:'Waiting for your decision',waiting_team:'Waiting for teammates',blocked_model:'Model required',blocked_account:'Account sign-in required',waiting_quota:'Waiting for account quota',retrying:'Reconnecting',done:'Done',review:'Reviewing',ready:'Ready',failed:'Needs attention',cancelled:'Stopped'};
  let contextOpen=matchMedia('(min-width:1101px)').matches, transcriptKey='', rosterKey='', contextKey='';
  const sectionOpen={progress:true,outputs:true,budget:true,context:true},uploads=new Map();
  let composerFrame=0,composerInput=null,composerSizeKey='',decisionIndex=-1,decisionCount=0,decisionBarKey='';
  const pulsed=new Set(),replyOpen=new Set(),groupOpen=new Set(),skillDrafts=new Map();
  const draftSkills=(key=room())=>{if(!skillDrafts.has(key)) skillDrafts.set(key,[]);return skillDrafts.get(key);};
  const composerObserver=new ResizeObserver(()=>resizeComposer());
  function resizeComposer() {
    cancelAnimationFrame(composerFrame);
    composerFrame=requestAnimationFrame(()=>{
      const input=$('#chat-input');
      if(input!==composerInput){
        composerObserver.disconnect();composerInput=input;composerSizeKey='';
        if(input)composerObserver.observe(input.closest('.conversation'));
      }
      if(!input)return;
      const conversation=input.closest('.conversation'),area=input.closest('.composer-area');
      const available=Math.max(0,conversation.clientHeight-conversation.querySelector('.conversation-header').offsetHeight);
      const chrome=area.offsetHeight-input.offsetHeight;
      const key=JSON.stringify([input.value,input.clientWidth,available,chrome]);
      if(key===composerSizeKey)return;
      const transcript=$('#co-transcript'),pinned=transcript.scrollHeight-transcript.clientHeight-transcript.scrollTop<24;
      const style=getComputedStyle(input),minimum=parseFloat(style.minHeight)||45;
      const maximum=Math.max(minimum,Math.min(320,available/2,available-chrome-Math.min(80,available/5)));
      const scroll=input.scrollTop;
      area.style.maxHeight=available+'px';
      input.style.maxHeight=maximum+'px';input.style.height='auto';
      const natural=input.scrollHeight+(parseFloat(style.borderTopWidth)||0)+(parseFloat(style.borderBottomWidth)||0);
      input.style.height=Math.max(minimum,Math.min(natural,maximum))+'px';
      input.style.overflowY=natural>maximum?'auto':'hidden';
      if(natural>maximum)input.scrollTop=document.activeElement===input&&input.selectionEnd===input.value.length?input.scrollHeight:scroll;
      if(pinned)transcript.scrollTop=transcript.scrollHeight;
      composerSizeKey=JSON.stringify([input.value,input.clientWidth,available,area.offsetHeight-input.offsetHeight]);
    });
  }
  const draftFiles=(key=room())=>{if(!uploads.has(key)) uploads.set(key,[]);return uploads.get(key);};
  const room=()=>h.getRoom();
  const selected=()=>actor(room().replace('dm-',''));
  const stamp=v=>v?new Intl.DateTimeFormat('en-US',{hour:'2-digit',minute:'2-digit',day:'2-digit',month:'2-digit'}).format(new Date(v)):'';
  // The preview opens in its own pane beside the chat; the work panel only reflects the open file.
  const previewPane=createPreviewPane({esc,icon,api,toast,format:text=>format(text),actor,stamp,agentMention,
    changesFor:artifactId=>list('file_changes').filter(c=>c.artifact_id===artifactId),
    onChange:()=>{contextKey='';update();},
    afterSave:()=>refresh(false)});
  const plain=value=>String(value||'').replace(/@\{([^}]+)\}/g,(token,id)=>(d().agents||[]).find(a=>a.id===id)?.name||token);
  const roomMessages=()=>list('messages').filter(m=>m.channel===room()&&ownerVisible(m,d().runs));
  // The bar follows the CURRENT work: the owner's pick for this room, else the project of the room's latest activity.
  const PROJECT_KEY='agentflow-room-project';
  const projectChoice=new Map(Object.entries((()=>{try{return JSON.parse(localStorage.getItem(PROJECT_KEY)||'{}')||{};}catch{return {};}})()));
  const saveChoice=()=>{try{localStorage.setItem(PROJECT_KEY,JSON.stringify(Object.fromEntries(projectChoice)));}catch{}};
  let projectMemo={key:'',value:undefined};
  const roomProject=()=>{
    const data=d(),key=[room(),projectChoice.get(room()),data.projects?.length,data.messages?.length,data.runs?.length,data.projects?.at(-1)?.version].join('|');
    if(projectMemo.data===data&&projectMemo.key===key) return projectMemo.value;
    const value=chooseProject({projects:list('projects'),messages:list('messages'),runs:list('runs'),channel:room(),chosen:projectChoice.get(room()),fallback:()=>{
      const assigned=list('tasks').filter(t=>t.agent_id===selected().id||t.reviewer_id===selected().id).at(-1);
      const step=list('checklist').filter(c=>(c.agent_ids||[]).includes(selected().id)).at(-1);
      return list('projects').find(p=>p.id===(assigned?.project_id||step?.project_id));
    }});
    projectMemo={data,key,value};return value;
  };
  const roomProjectList=()=>roomProjects({projects:list('projects'),messages:list('messages'),runs:list('runs'),channel:room()});
  const roomRuns=()=>list('runs').filter(r=>r.channel===room()&&r.kind!=='peer'&&!terminal.has(r.status));
  const questions=()=>list('decisions').filter(q=>q.status==='open');
  // Waiting for provider quota is a calm, expected state: clock icon + amber, never an error.
  const quotaWait=text=>/^waiting for\b/i.test(String(text||''))&&/quota/i.test(String(text||''));
  const baseModel=a=>(a.binding_origin==='default'||!a.model)?{provider:d().settings.default_provider,model:d().settings.default_model,reasoning_effort:d().settings.default_reasoning_effort}:{provider:a.provider,model:a.model,reasoning_effort:a.reasoning_effort};
  function modelLabel(a) {
    const run=list('runs').filter(r=>r.agent_id===a.id&&!terminal.has(r.status)&&!r.system_managed).at(-1);
    const phase=run?.model_phase||a.model_policy?.initial_phase;
    const binding=run?.model_override||(!run&&a.chat_model_override)||a.model_policy?.phases?.[phase]||a.model_policy?.default||baseModel(a);
    const sameChat=run?.model_override&&a.chat_model_override&&run.model_override.model===a.chat_model_override.model;
    const prefix=run?.quota_override&&run?.model_override?'Quota fallback · ':sameChat||(!run&&a.chat_model_override)?'This chat · ':run?.model_override?'This run only · ':a.model_policy?.phases?.[phase]?'':a.id==='a-lead'?'Team default · ':a.binding_origin==='default'?'Inherited · ':a.binding_origin==='owner'?'Pinned · ':'';
    return prefix+(binding.model||'Choose model')+(binding.reasoning_effort?' · '+binding.reasoning_effort:'');
  }
  const SCOPES=[['chat','This chat only','Team default stays'],['team','Team default','Atlas and inheriting agents']];
  const scopeToast=scope=>scope==='team'?'Team default updated. Waiting work will resume automatically.':scope==='chat'?'Model changed for this chat only. The team default is unchanged.':'Model updated. Work resumes from the next turn.';
  function modelDialog(a,teamOnly=false) {
    const lead=a.id==='a-lead',chatPick=lead&&!teamOnly&&a.chat_model_override;
    const models=d().runtime?.models||[],base=chatPick?a.chat_model_override:baseModel(a),key=m=>JSON.stringify([m.provider,m.id||m.model]);
    const inherited=chatPick?false:!lead&&(a.binding_origin==='default'||!a.model);
    const scopeField=!lead?'':teamOnly?'<input type="hidden" name="scope" value="team">':
      '<fieldset class="ig-modes ma-scope"><legend class="label-like">Apply to</legend>'+SCOPES.map(([v,t,hint],i)=>'<label class="ig-mode"><input type="radio" name="scope" value="'+v+'"'+(i===0?' checked':'')+'><span class="ig-mode-face"><strong>'+t+'</strong><small>'+hint+'</small></span></label>').join('')+'</fieldset>';
    const options=[...(lead?[['','Choose an available model'],...(chatPick?[['inherit','Use the team default · '+(d().settings.default_model||'not selected')]]:[])]:[['inherit','Inherit team default · '+(d().settings.default_model||'not selected')]]),
      ...models.map(m=>[key(m),(m.provider==='claude'?'Claude':m.provider==='codex'?'Codex':m.provider==='gemini'?'Gemini':m.provider)+' · '+(m.name||m.id)+(m.resolved_model?' · '+m.resolved_model:'')])];
    const selected=inherited?'inherit':models.some(m=>m.provider===base.provider&&m.id===base.model)?key(base):'';
    const phases=Object.entries(a.model_policy?.phases||{}),phaseNames={analysis:'Analysis',planning:'Planning',coding:'Coding',testing:'Testing',review:'Review'};
    openModal(lead?(teamOnly?'Default model for the team':'Model in Atlas chat'):'Model trong chat '+a.name,
      lead?(teamOnly?'Atlas and every agent that inherits follow this default. Individual selections are retained.':'Choose where it applies. Atlas keeps following the team default unless you pick “Team default”.'):'Choose an initial model or inherit the team default. Applies from the next turn; agents can still switch models for their work.',
      scopeField+field('model_key',lead?'Model':'Initial model',selected,'select',{options,required:true})+
      '<div id="co-reasoning-field"></div>'+
      (phases.length?'<div class="info-box">Phase rules: '+phases.map(([phase,b])=>esc(phaseNames[phase]||phase)+': '+esc(b.model)).join(' · ')+'. The agent switches models as the phase changes.</div>':'')+
      '<div class="observed-models">'+(d().runtime?.providers||[]).filter(p=>p.installed&&['codex','claude','gemini'].includes(p.id)).map(p=>'<button type="button" class="button small" data-action="co-model-refresh" data-id="'+esc(p.id)+'" data-agent="'+esc(a.id)+'">Refresh '+esc(p.id==='claude'?'Claude':p.id==='gemini'?'Gemini':'Codex')+'</button>').join('')+'</div><p class="muted">The catalog comes from the current account. Access remains subject to account permissions and quota.</p>',
      button('Cancel','modal-close')+'<button type="submit" class="button primary">Save selection</button>','co-chat-model',a.id);
    const renderEffort=initial=>{
      const choice=$('#f-model_key').value;
      const entry=models.find(m=>key(m)===choice),levels=entry?.reasoning_efforts||[];
      $('#co-reasoning-field').innerHTML=levels.length?field('reasoning_effort','Reasoning effort',initial&&levels.includes(base.reasoning_effort)?base.reasoning_effort:'','select',{options:[['','Model default'],...levels.map(level=>[level,level])],help:'Applies from the next model turn. Only levels reported by the current model are offered.'}):'<p class="muted">'+(choice==='inherit'?'Inherit model and reasoning from Atlas.':'This model does not report configurable reasoning levels.')+'</p>';
    };
    $('#f-model_key').addEventListener('change',()=>renderEffort(false));renderEffort(true);
  }
  // Compact roster (owner request): ~40px rows, one line per agent; a second muted line only while working/waiting.
  const ROSTER_KEY='agentflow-roster-collapsed';
  const rosterCollapsed=new Set((()=>{try{return JSON.parse(localStorage.getItem(ROSTER_KEY)||'[]');}catch{return [];}})());
  let rosterQuery='',foldersAll=false,changesAll=false;
  const saveCollapsed=()=>{try{localStorage.setItem(ROSTER_KEY,JSON.stringify([...rosterCollapsed]));}catch{}};
  const pluralDecisions=n=>n+(n===1?' decision needs':' decisions need')+' you';
  function rosterRow(a,grouped) {
    const latest=list('messages').filter(m=>m.channel==='dm-'+a.id&&ownerVisible(m,d().runs)).at(-1);
    const pending=questions().filter(q=>q.agent_id===a.id).length;
    const active=list('runs').find(r=>r.agent_id===a.id&&!terminal.has(r.status));
    const waiting=quotaWait(a.current_action)&&active?.kind!=='peer';
    const line=waiting?plain(a.current_action):active?plain(active.kind==='peer'?'Collaborating':a.current_action||statusNames[active.status]||'Working'):'';
    const selectedRow=room()==='dm-'+a.id;
    const full=[a.name+(a.role&&!a.name.includes(a.role)?' · '+a.role:''),agentGroupLabel(a),line||(latest?'Last: '+plain(latest.content).replace(/\s+/g,' ').slice(0,90):'')].filter(Boolean).join('\n');
    return '<button class="roster-person'+(selectedRow?' selected':'')+(line?' has-line':'')+'" data-action="co-room" data-id="'+esc(a.id)+'" title="'+esc(full)+'"'+(selectedRow?' aria-current="true"':'')+'>'+avatar(a)+
      '<span class="roster-copy"><span class="roster-line"><strong>'+esc(a.name)+'</strong>'+(grouped?'':agentGroup(a))+'</span>'+(line?'<small'+(waiting?' class="is-quota-wait"':'')+'>'+(waiting?icon('clock'):'')+esc(line)+(active&&!waiting?reltimeMarkup({ts:agentActivity(active),mode:'compact',esc,className:'roster-age'}):'')+'</small>':'')+'</span>'+
      (pending?'<span class="question-count" title="'+pluralDecisions(pending)+'" aria-label="'+pluralDecisions(pending)+'">'+pending+'</span>':active?'<span class="working-orb" title="'+esc(statusNames[active.status]||active.status)+'"></span>':'')+'</button>';
  }
  const agentGroupLabel=a=>String(a.category||'general').trim().replace(/[_-]+/g,' ').replace(/\b\w+/g,w=>w.length<=3?w.toUpperCase():w[0].toUpperCase()+w.slice(1));
  function roster() {
    const everyone=list('agents'),query=rosterQuery.trim(),shown=everyone.filter(a=>rosterMatches(a,query));
    if(!shown.length) return '<p class="roster-empty" role="status">No teammates match “'+esc(query)+'”.</p>';
    const groups=rosterGroups(shown,{threshold:everyone.length>GROUP_THRESHOLD?-1:Infinity});
    return groups.map(g=>{
      if(!g.key) return g.agents.map(a=>rosterRow(a,false)).join('');
      const collapsed=rosterCollapsed.has(g.key)&&!query;
      const waitingCount=g.agents.filter(a=>list('runs').some(r=>r.agent_id===a.id&&!terminal.has(r.status))).length;
      const decisions=g.agents.reduce((n,a)=>n+questions().filter(q=>q.agent_id===a.id).length,0);
      const hasSelected=g.agents.some(a=>room()==='dm-'+a.id);
      return '<div class="roster-group'+(collapsed?' is-collapsed':'')+'"><button type="button" class="roster-group-head'+(hasSelected?' has-selected':'')+'" data-action="co-roster-group" data-id="'+esc(g.key)+'" aria-expanded="'+!collapsed+'">'+icon('down')+'<span>'+esc(g.label)+'</span><small>'+g.agents.length+'</small>'+
        (collapsed&&decisions?'<span class="question-count" title="'+pluralDecisions(decisions)+'" aria-label="'+pluralDecisions(decisions)+'">'+decisions+'</span>':collapsed&&waitingCount?'<span class="working-orb" title="'+waitingCount+' working"></span>':'')+'</button>'+
        '<div class="roster-group-rows" role="group" aria-label="'+esc(g.label)+'"'+(collapsed?' hidden':'')+'>'+g.agents.map(a=>rosterRow(a,true)).join('')+'</div></div>';
    }).join('');
  }
  function syncRoster() {
    const nav=$('#agent-roster');if(!nav) return;
    const markup=roster(),count=$('#roster-count');
    if(stableMarkup(markup)!==rosterKey) {
      const focused=nav.contains(document.activeElement)?{action:document.activeElement.dataset.action,id:document.activeElement.dataset.id}:null,top=nav.scrollTop;
      morph(nav,markup);nav.scrollTop=top;rosterKey=stableMarkup(markup);
      if(focused&&!nav.contains(document.activeElement)) [...nav.querySelectorAll('[data-action]')].find(el=>el.dataset.action===focused.action&&el.dataset.id===focused.id)?.focus({preventScroll:true});
    }
    if(count) count.textContent=rosterQuery.trim()?nav.querySelectorAll('.roster-person').length+' / '+list('agents').length:list('agents').length;
  }
  function shell(route) {
    transcriptKey=rosterKey=contextKey='';
    const menu=[['tasks','tasks','Work'],['content','calendar','Content'],['skills','skills','Skills'],['knowledge','book','Memory'],['automations','pulse','Work cadence'],['connections','plug','Connections & models'],['agents','agents','Team profiles'],['settings','settings','Settings']];
    return '<div class="layout co-layout"><aside class="sidebar co-sidebar" aria-label="Team and navigation">'+
      '<div class="co-brand"><a class="brand" href="#chat"><span class="brand-mark">'+icon('logo')+'</span>AgentFlow</a><button class="icon-button" data-action="co-new" aria-label="Create a teammate from a request">'+icon('plus')+'</button></div>'+
      '<label class="roster-search">'+icon('search')+'<input id="roster-search" type="search" aria-label="Find teammates" placeholder="Find teammates…" value="'+esc(rosterQuery)+'"></label>'+
      '<div class="roster-heading"><span>YOUR TEAM</span><span id="roster-count">'+list('agents').length+'</span></div><nav id="agent-roster" aria-label="Chat with agent">'+roster()+'</nav>'+
      '<div class="co-nav"><details '+(route!=='chat'?'open':'')+'><summary>'+icon('grid')+'Workspace'+icon('down')+'</summary>'+menu.map(([key,ico,label])=>'<a class="nav-item '+(route===key?'active':'')+'" href="#'+key+'">'+icon(ico)+label+'</a>').join('')+'</details></div>'+
      '<div class="co-owner"><span class="runtime-light '+(!d().settings.execution_enabled?'off':'')+'"></span><span>'+esc(d().settings.workspace_name||'Your workspace')+'<small>'+esc(h.getMode()==='demo'?'Preview · AI disconnected':d().settings.execution_enabled?'Team accepting work':'New work paused')+'</small></span><a href="#connections" aria-label="Connection settings">'+icon('settings')+'</a></div></aside>'+
      '<div class="shell co-shell"><header class="topbar co-topbar"><button class="icon-button mobile-menu" data-action="mobile-menu" aria-label="Open team" aria-expanded="false">'+icon('menu')+'</button><span id="breadcrumb"></span><label class="header-search">'+icon('search')+'<input id="global-search" type="search" placeholder="Search this page…" aria-label="Search this page"></label><button class="icon-button" data-action="activity" aria-label="Activity">'+icon('bell')+'</button></header>'+
      '<main id="main" class="content '+(route==='chat'?'co-content':'')+'" tabindex="-1"></main></div></div>';
  }
  function format(content='',projectId=roomProject()?.id) {
    const start=content.indexOf('{"reply"');
    if(start>=0) {try {content=JSON.parse(content.slice(start)).reply||content;}catch{}}
    return renderMarkdown(content,{agents:d().agents,artifacts:list('artifacts').filter(a=>!projectId||a.project_id===projectId),attachments:list('attachments').filter(a=>!projectId||a.project_id===projectId)});
  }
  function decision(q) {
    if(!q) return '';
    const who=actor(q.agent_id);
    return decisionCard(q,{esc,icon,avatar,field,actor:who,title:text=>formatMessage(text||'',d().agents),context:text=>format(text),time:stamp,plain,replyOpen:id=>replyOpen.has(id)});
  }
  // Open decisions this chat shows: decision messages in the transcript plus project/agent questions not yet rendered inline.
  function chatDecisions(msgs=roomMessages()) {
    const inline=msgs.filter(m=>m.kind==='decision').map(m=>m.decision_id),rendered=new Set(inline),project=roomProject();
    const extra=questions().filter(q=>(!q.permission_request||selected().id==='a-lead')&&(q.project_id===project?.id||q.agent_id===selected().id)&&!rendered.has(q.id));
    const open=[...new Set(inline)].map(id=>list('decisions').find(q=>q.id===id)).filter(q=>q?.status==='open');
    return {rendered,extra,open:[...open,...extra]};
  }
  function workLog(entries) {
    return renderWorkLog(entries,{esc,icon,stamp,format,agentName:id=>id!==selected().id?actor(id).name:''});
  }
  function transcript() {
    const msgs=roomMessages(),shown=new Set(msgs.filter(m=>m.kind==='decision').map(m=>m.decision_id)),items=[],doneShown=new Set();
    const find=id=>list('decisions').find(q=>q.id===id);
    const push=(html,open=false,id='')=>{if(html) items.push({html,open,id});};
    for(const m of msgs) {
      if(m.kind==='decision') {
        const q=find(m.decision_id);
        if(q&&!doneShown.has(q.id)){doneShown.add(q.id);push(decision(q),q.status==='open',q.id);}
        continue;
      }
      // The owner's decision answer is rendered with its question; never as a bare "Allow" bubble.
      if(m.kind==='decision_answer') {
        if(shown.has(m.decision_id)||doneShown.has(m.decision_id)) continue;
        const q=find(m.decision_id);doneShown.add(m.decision_id);
        if(q?.status==='open') continue;
        push(decision(q?{...q,status:'answered',answer:q.answer||m.content}:{id:m.decision_id||m.id,status:'answered',question:'Answered a question',answer:m.content,answered_at:m.created_at,agent_id:m.agent_id}));
        continue;
      }
      if(m.kind==='handoff') {
        const replies=list('messages').filter(x=>x.handoff_event_id===m.id&&x.kind==='peer_reply');
        const source=list('messages').find(x=>x.id===m.related_message_id);
        push('<details class="handoff-event"><summary>'+icon('team')+'<span>'+esc(actor(m.agent_id).name)+' → '+esc(actor(m.target_id).name)+'</span><small>'+esc(replies.length?'Reply received':'Message forwarded')+'</small>'+icon('down')+'</summary><div><p>'+format(source?.content||m.content)+'</p>'+replies.map(r=>'<p><strong>'+esc(actor(r.agent_id).name)+':</strong> '+format(r.content)+'</p>').join('')+'</div></details>');
        continue;
      }
      if(m.kind==='peer_reply'&&m.handoff_event_id) continue;
      if(m.kind==='agent_event') {push(agentEventMarkup(m,{esc,icon,avatar,actor,stamp}));continue;}
      // Only what the owner needs: step narration folds into one collapsed work log per streak.
      if(isActivity(m)) {
        const last=items[items.length-1];
        if(last?.log) {last.log.push(m);last.html=workLog(last.log);} else items.push({html:workLog([m]),open:false,id:'',log:[m]});
        continue;
      }
      const peer=m.agent_id!==selected().id&&m.role!=='user';
      push('<article class="co-message '+(m.role==='user'?'from-user':'from-agent')+(peer?' peer-message':'')+(m.kind==='error'?' message-error':'')+'">'+
        (m.role!=='user'?'<div class="message-byline">'+agentMention(actor(m.agent_id))+'<time>'+stamp(m.created_at)+'</time></div>':'')+
        '<div class="message-body">'+format(m.content,m.project_id)+attachmentMarkup((m.attachment_ids||[]).map(id=>(d().attachments||[]).find(a=>a.id===id)).filter(Boolean),false)+messageFilesMarkup(resolveFiles(m.artifact_ids,list('artifacts')),esc)+'</div></article>');
    }
    for(const q of chatDecisions(msgs).extra) push(decision(q),true,q.id);
    let result=groupDecisions(items,{limit:3,expanded:id=>groupOpen.has(id),esc});
    if(!result) result='<div class="co-welcome"><span class="welcome-mark">'+icon('spark')+'</span><span class="eyebrow">ONE GOAL. ONE TEAM.</span><h1>Assign work to '+esc(selected().name)+'.</h1><p>'+esc(selected().role)+'. The team plans, delegates, collaborates and brings results here.</p><div class="welcome-prompts">'+
      ['Create a product launch team: planner, writer, designer and tester. Delegate work, build the required skills and ask me about missing decisions.','Review project documents, identify improvements and coordinate SEO and content for a launch plan.'].map((text,i)=>'<button data-action="suggestion" data-id="'+esc(text)+'">'+icon(i?'folder':'team')+'<span>'+(i?'Prepare a project launch':'Build a team for a goal')+'</span>'+icon('arrow')+'</button>').join('')+'</div></div>';
    const runs=roomRuns();
    const stuck=runs.length?stuckInfo(familyRuns(list('runs'),runs),r=>actor(r.agent_id).name):null;
    const liveText=stuck?stuck.text:runs.length?selected().current_action||statusNames[runs[0].status]||'Processing':'',waiting=!stuck&&quotaWait(liveText);
    const nowMs=Date.now(),act=runs.length?lastActivity({runs:list('runs'),messages:list('messages'),channel:room(),roomRuns:runs}):null;
    const limit=(Number(d().settings?.model_call_timeout_seconds)||0)*1000||THINK_LIMIT_MS;
    const quiet=!!act&&act.ts!==null&&activityText(act.iso,nowMs,{warn:act.progressing,since:act.since,limit}).stalled;
    const newest=msgs.at(-1);
    if(newest?.created_at) result+='<p class="latest-time'+(newest.role==='user'?' is-user':'')+'">'+reltimeMarkup({ts:newest.created_at,mode:'ago',esc,now:nowMs,title:stamp(newest.created_at)})+'</p>';
    result+=runs.length?'<div class="live-work'+(waiting?' is-quota-wait':'')+(quiet?' is-stalled':'')+(stuck?' is-stuck':'')+'" role="status">'+(waiting?icon('clock'):'<span class="working-orb"></span>')+'<span>'+esc(liveText)+(runs.length>1?' · '+runs.length+' work runs':'')+(act&&act.ts!==null?reltimeMarkup({ts:act.iso,mode:'activity',esc,now:nowMs,warn:act.progressing,since:act.since,limit}):'')+'</span>'+(stuck?button('Resume','co-unstick',stuck.rootId,'small primary'):'')+button('Stop this branch','co-cancel',runs[0].id,'quiet small')+'</div>':'';
    return result;
  }
  const lastSegmentsOf=path=>compactFolders([path]).rows[0]?.label||String(path||'');
  const ACCESS_TEXT={R:'Read only',RW:'Read & write'};
  // One line per folder: icon, last two path segments, tiny R / RW badge. Full path in the tooltip and for screen readers.
  const folderList=rows=>'<ul class="folder-list">'+rows.map(r=>'<li class="folder-row" title="'+esc(r.path+(r.access?' · '+ACCESS_TEXT[r.access]:''))+'">'+icon('folder')+'<span class="folder-name" aria-hidden="true">'+esc(r.label)+'</span><span class="sr-only">'+esc(r.path)+'</span>'+(r.access?'<span class="access-badge '+(r.access==='R'?'is-read':'is-write')+'" role="img" aria-label="'+ACCESS_TEXT[r.access]+'">'+r.access+'</span>':'')+'</li>').join('')+'</ul>';
  function context() {
    const project=roomProject();
    const previewed=previewPane.activeArtifactId(),isPrev=id=>!!id&&id===previewed;
    const items=progressItems(),done=items.filter(i=>i.status==='done').length,changes=list('file_changes').filter(c=>c.project_id===project?.id).reverse();
    const projectArtifacts=list('artifacts').filter(a=>a.project_id===project?.id),isPicture=a=>!!a&&(a.is_image||isImagePath(a.path));
    // Images live only in the Images & screenshots grid; the changed-files list is for documents and code.
    const images=projectArtifacts.filter(isPicture).slice(-60);
    const grouped=groupChanges(changes,{isImage:c=>isPicture(projectArtifacts.find(a=>a.id===c.artifact_id))});
    const otherFiles=projectArtifacts.filter(a=>!isPicture(a)&&!changes.some(c=>c.artifact_id===a.id));
    const forYou=forYouFiles(projectArtifacts,isPicture);
    const fileRows=[...changedAfterForYou(grouped,forYou).map(g=>({change:g})),...otherFiles.filter(a=>!a.deliverable).map(a=>({artifact:a}))];
    const shownRows=limitRows(fileRows,6,changesAll);
    const fileRow=row=>{
      if(row.change) {
        const g=row.change,who=actor(g.latest.agent_id),tip=g.path+'\n'+(g.count>1?g.count+' edits':g.created?'Created':'Modified')+' · '+who.name+' · '+stamp(g.latest.created_at);
        return '<button class="output-row'+(isPrev(g.latest.artifact_id)?' is-previewed':'')+'" data-action="co-change" data-id="'+esc(g.latest.id)+'"'+(isPrev(g.latest.artifact_id)?' aria-current="true"':'')+' title="'+esc(tip)+'" aria-label="'+esc(g.name+', '+(g.count>1?g.count+' edits, ':'')+'+'+g.added+' −'+g.deleted+', '+who.name)+'">'+icon('file')+'<span class="output-name"><strong>'+esc(g.name)+'</strong>'+(g.dir?'<small>'+esc(g.dir)+'</small>':'')+'</span>'+(g.count>1?'<span class="output-count" title="'+g.count+' edits">×'+g.count+'</span>':'')+'<span class="change-count"><b>+'+g.added+'</b><i>−'+g.deleted+'</i></span>'+avatar(who,'mini')+'</button>';
      }
      const a=row.artifact,who=actor(a.agent_id),parts=String(a.path||'').split(/[\\/]/).filter(Boolean);
      return '<button class="output-row'+(isPrev(a.id)?' is-previewed':'')+'" data-action="co-artifact" data-id="'+esc(a.id)+'"'+(isPrev(a.id)?' aria-current="true"':'')+' title="'+esc(a.path+(a.title?'\n'+a.title:''))+'">'+icon('file')+'<span class="output-name"><strong>'+esc(parts.at(-1)||a.path)+'</strong>'+(parts.length>1?'<small>'+esc(parts.at(-2))+'</small>':'')+'</span>'+(a.agent_id?avatar(who,'mini'):'')+'</button>';
    };
    const projectRuns=project?list('runs').filter(r=>r.project_id===project.id):[],root=projectRoot(projectRuns,project?.id);
    const folders=[...new Set(project?.context_paths||[])],grants=project?.folder_grants||[],folderView=compactFolders(folders,grants,{expanded:foldersAll}),pending=questions().filter(q=>q.kind==='folder_access'&&q.project_id===project?.id);
    const section=(key,title,count,body)=>'<details class="detail-section" data-section="'+key+'" '+(sectionOpen[key]?'open':'')+'><summary><span>'+title+'</span><small>'+esc(count)+'</small>'+icon('down')+'</summary><div class="detail-section-body">'+body+'</div></details>';
    const forYouRow=a=>{
      const who=actor(a.agent_id),name=String(a.path||'').split(/[\\/]/).filter(Boolean).at(-1)||a.path;
      return '<button class="output-row for-you-row'+(isPrev(a.id)?' is-previewed':'')+'" data-action="co-artifact" data-id="'+esc(a.id)+'"'+(isPrev(a.id)?' aria-current="true"':'')+' title="'+esc(a.path+' · '+who.name+' · '+stamp(a.updated_at||a.created_at))+'">'+icon('file')+'<span class="output-name"><strong>'+esc(a.title||name)+'</strong><small>'+esc(name)+'</small></span>'+(a.agent_id?avatar(who,'mini'):'')+icon('chevron')+'</button>';
    };
    const changedOpen=sectionOpen.changedfiles??!forYou.length;
    return goalsMarkup(project,esc,{projects:roomProjectList(),doneOpen:!!sectionOpen.goaldone})+
      section('progress','Progress',items.length?done+'/'+items.length:'',
        (items.length?'<div class="progress-caption"><span>'+done+' completed · '+items.filter(i=>i.status==='doing').length+' working</span><span>'+items.filter(i=>i.status==='blocked').length+' needs attention</span></div><ol class="progress-list">'+items.map((item,index)=>'<li class="progress-step '+esc(item.status)+'"><span class="step-marker" aria-label="'+esc(stepNames[item.status])+'">'+(item.status==='done'?icon('check'):item.status==='blocked'?'!':index+1)+'</span><div class="step-copy"><button class="step-title" data-action="co-step" data-id="'+esc(item.id)+'">'+esc(item.title)+'</button><div class="step-people">'+item.agent_ids.map(id=>agentMention(actor(id))).join('')+'</div><small class="step-status">'+esc(stepNames[item.status])+(item.task_id?' · Per task':'')+'</small>'+(item.note?'<p class="step-note">'+esc(item.note)+'</p>':'')+'<div class="step-actions">'+button('Edit','co-step',item.id,'quiet small')+(index?button('↑','co-step-up',item.id,'quiet small'):'')+(index<items.length-1?button('↓','co-step-down',item.id,'quiet small'):'')+button('Remove','co-step-remove',item.id,'quiet small')+'</div></div></li>').join('')+'</ol>':'<p class="context-note">Steps will appear when the team creates a plan.</p>')+
        (project?button('Add step','co-step','','quiet small','plus'):''))+
      section('outputs','Outputs',fileRows.length+images.length+forYou.length||'',
        (forYou.length?'<p class="context-label">FOR YOU</p><div class="output-list for-you-list">'+forYou.map(forYouRow).join('')+'</div>':'')+
        (images.length?'<p class="context-label">IMAGES & SCREENSHOTS</p>'+format('```images\n'+JSON.stringify(images.map(a=>({path:a.path,title:a.title})))+'\n```'):'')+
        (fileRows.length?'<details class="output-sub" data-section="changedfiles"'+(changedOpen?' open':'')+'><summary><span>Changed files</span><small>'+fileRows.length+'</small>'+icon('down')+'</summary><div class="output-list">'+shownRows.rows.map(fileRow).join('')+'</div>'+(fileRows.length>6?'<button type="button" class="button quiet small output-more" data-action="co-outputs-all" aria-expanded="'+changesAll+'">'+(changesAll?'Show fewer':'Show '+shownRows.hidden+' more')+'</button>':'')+'</details>':images.length||forYou.length?'':'<p class="context-note">Created or modified files will appear here. Select a file to preview changes.</p>'))+
      (project?section('budget','Budget & tokens',budgetSummary(root,d().settings,projectRuns),budgetMarkup({root,settings:d().settings,runs:projectRuns},esc)):'')+
      section('context','Context',folders.length||'',
        (folders.length?'<p class="context-label">FOLDERS USED</p>'+folderList(folderView.rows)+(folderView.total>4?'<button type="button" class="button quiet small folder-more" data-action="co-folders-all" aria-expanded="'+foldersAll+'">'+(foldersAll?'Show fewer':'Show all ('+folderView.total+')')+'</button>':''):'<p class="context-note">No folders have been used in this task yet.</p>')+
        (grants.length?'<p class="context-label">GRANTED ACCESS</p>'+folderList(grants.map(g=>({path:g.path,label:lastSegmentsOf(g.path),access:g.access==='read'?'R':'RW'}))):'')+
        ((project?.browser_grants||[]).length?'<p class="context-label">BROWSER</p>'+project.browser_grants.map(g=>'<div class="context-browser-grant"><span>'+esc(g.origin)+'<small>'+esc(g.access==='interact'?'Read & control':'Read only')+'</small></span><button class="button quiet small" data-action="rich-revoke-browser" data-id="'+esc(g.origin)+'">Revoke</button></div>').join(''):'')+
        pending.map(q=>'<button class="context-permission" data-action="co-room" data-id="a-lead">'+icon('help')+'<span>'+esc(actor(q.requesters?.[0]?.agent_id||q.agent_id).name)+' requesting access through Atlas<small>'+esc(q.requested_path||q.question)+'</small></span>'+icon('chevron')+'</button>').join('')+
        attachmentMarkup(list('attachments').filter(a=>a.project_id===project?.id),false)+
        (project?button('Grant folder access','co-folder',project.id,'quiet small','plus'):''));
  }
  const stepNames={todo:'Not started',doing:'Running',done:'Done',blocked:'Needs attention',skipped:'Skipped'};
  function progressItems() {
    const project=roomProject();if(!project) return [];
    const saved=(d().checklist||[]).filter(c=>c.project_id===project.id),linked=new Set(saved.map(c=>c.task_id));
    const taskStates={backlog:'todo',ready:'todo',running:'doing',review:'doing',done:'done',failed:'blocked',cancelled:'skipped'};
    const tasks=list('tasks').filter(t=>t.project_id===project.id);
    return [...saved.filter(c=>!c.archived).map(c=>{const task=tasks.find(t=>t.id===c.task_id);return {...c,...(task?{status:taskStates[task.status]||'todo'}:{})};}),
      ...tasks.filter(t=>!linked.has(t.id)).map((t,i)=>({id:'task:'+t.id,title:t.title,note:t.review_note||t.progress_note||'',task_id:t.id,project_id:project.id,agent_ids:[t.agent_id],status:taskStates[t.status]||'todo',position:saved.length+i,derived:true}))].sort((a,b)=>a.position-b.position);
  }
  function stepDialog(id) {
    const item=progressItems().find(c=>c.id===id)||{title:'',note:'',agent_ids:[selected().id],status:'todo',position:progressItems().length};
    openModal(id?'Edit step':'Add work step','Owners, tasks, progress and notes in one place.',
      field('title','Tasks',item.title,'text',{required:true,maxlength:250})+
      field('status','Progress',item.status,'select',{options:Object.entries(stepNames),help:item.task_id?'Linked steps update automatically with the actual task status.':''})+
      '<fieldset class="step-assignees"><legend>Who is responsible?</legend>'+list('agents').map(a=>'<label><input type="checkbox" name="agent_ids" value="'+esc(a.id)+'" '+(item.agent_ids.includes(a.id)?'checked':'')+'>'+avatar(a,'mini')+esc(a.name)+'</label>').join('')+'</fieldset>'+
      field('note','Notes',item.note,'textarea',{rows:3,maxlength:4000}),button('Cancel','modal-close')+'<button type="submit" class="button primary">Save step</button>','co-step',id);
    if(item.task_id) $('#f-status').disabled=true;
  }
  async function saveStep(item,patch) {
    if(item.derived) {const {id,derived,...doc}=item;return api('/checklist',{...doc,...patch});}
    return api('/checklist/'+item.id,{...patch,version:item.version},'PATCH');
  }
  function attachmentMarkup(files,editable) {
    if(!files.length) return '';
    // Sent images become a tight thumbnail gallery (3 + "+N"); the composer keeps removable chips with a lightbox thumb.
    const pictures=editable?[]:files.filter(f=>f.is_image&&f.id),rest=editable?files:files.filter(f=>!pictures.includes(f));
    const gallery=pictures.length?galleryMarkup(pictures.map((f,i)=>imageCard({source:'attachment',record:{...f,is_image:true,title:f.name}},f.name,galleryTiles(pictures.length,i))),'rich-gallery attachment-gallery'):'';
    return gallery+(rest.length?'<div class="attachment-list">'+rest.map((f,i)=>'<div class="attachment-chip '+(f.error?'has-error':'')+'">'+(f.is_image&&f.id?'<button type="button" class="attachment-thumb" data-lightbox-src="/api/attachments/'+esc(f.id)+'" data-lightbox-title="'+esc(f.name)+'" aria-label="View image: '+esc(f.name)+'"><img src="/api/attachments/'+esc(f.id)+'" alt="'+esc(f.name)+'"></button>':icon('file'))+'<span>'+(f.id?'<button type="button" class="artifact-inline" data-action="rich-file" data-source="attachment" data-id="'+esc(f.id)+'">'+esc(f.name)+'</button>':'<strong>'+esc(f.name)+'</strong>')+'<small>'+esc(f.error|| (f.pending?'Loading…':Math.ceil(f.bytes/1024)+' KB · '+(f.is_image?'Original image':'Content loaded')))+'</small>'+(f.notice&&!f.pending?'<small class="attachment-notice">'+esc(f.notice)+'</small>':'')+'</span>'+(editable?'<button type="button" class="icon-button" data-action="co-remove-file" data-id="'+i+'" aria-label="Remove '+esc(f.name)+'">'+icon('close')+'</button>':'')+'</div>').join('')+'</div>':'');
  }
  const attachmentIds=()=>{if(draftFiles().some(f=>f.pending||f.error))throw new Error('Wait for uploads to finish or remove failed files before sending.');return draftFiles().map(f=>f.id);};
  const slash=createSlash({esc,api,toast,refresh,getAgentId:()=>selected().id,getAgents:()=>list('agents'),getSkills:()=>list('skills'),
    onPickSkill:skill=>{const chosen=draftSkills();if(!chosen.some(x=>x.id===skill.id))chosen.push(skill);updateSkills();$('#chat-input')?.focus();}});
  function updateSkills() {const el=$('#composer-skills');if(el)el.innerHTML=skillChipsMarkup(draftSkills(),esc);resizeComposer();}
  const notice=e=>isUnavailable(e)?'Not available yet.':e.message;
  async function sendChat(form) {
    const input=$('#chat-input'),submit=form.querySelector('button[type=submit]'),error=$('#chat-error');
    if(!input||!submit||submit.disabled) return;
    const text=input.value.trim(),parsed=parseSlash(text),sentRoom=room();
    if(!text&&!draftFiles(sentRoom).length) return;
    error.textContent='';input.removeAttribute('aria-invalid');submit.disabled=true;
    const clear=()=>{sessionStorage.removeItem('agentflow-chat-draft-'+sentRoom);input.value='';input.dispatchEvent(new Event('input',{bubbles:true}));};
    try {
      const command=parsed&&commandById(parsed.command);
      if(command&&!command.chat) {
        if(command.id==='goal') {
          if(!parsed.text) throw new Error('Add the goal after /goal, for example: /goal Launch the spring campaign.');
          try {const added=await api('/projects/goal',{channel:sentRoom,goal:parsed.text});if(added?.project?.id){projectChoice.set(sentRoom,{id:added.project.id,at:Date.now()});saveChoice();}}
          catch(e) {if(isUnavailable(e)) {toast('Not available yet',true);return;}throw e;}
          clear();await refresh(false);contextKey='';update();toast('Goal added to this project.');
        } else {clear();slash.run(command.id,parsed.text);}
        return;
      }
      if(command&&!parsed.text) throw new Error('Describe what to create after /'+command.id+'.');
      const skills=draftSkills(sentRoom);
      await api('/chat',{message:command?parsed.text:text,agent_id:new FormData(form).get('agent_id'),channel:sentRoom,project_id:roomProject()?.id,attachment_ids:draftFiles(sentRoom).length?attachmentIds():[],
        ...(command?{command:command.id}:{}),...(skills.length?{skill_ids:skills.map(x=>x.id)}:{})});
      uploads.delete(sentRoom);skillDrafts.delete(sentRoom);updateUploads();updateSkills();clear();
      await refresh(false);update();$('#chat-input')?.focus();
    } catch(e) {error.textContent=notice(e);input.setAttribute('aria-invalid','true');}
    finally {if(submit.isConnected) submit.disabled=false;resizeComposer();}
  }
  function updateUploads() {const el=$('#composer-attachments');if(el)el.innerHTML=attachmentMarkup(draftFiles(),true);resizeComposer();}
  async function addFiles(files) {
    const key=room(),draft=draftFiles(key);
    for(const file of Array.from(files)) {
      if(draft.length>=6) {toast('Up to 6 attachments per message.',true);break;}
      const item={name:file.name,bytes:file.size,pending:true};draft.push(item);updateUploads();
      try {
        const image=/\.(png|jpe?g|webp)$/i.test(file.name);
        if(file.size>(image?5:4)*1024*1024) throw new Error('Images up to 5 MB; documents up to 4 MB.');
        const base64=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=()=>reject(new Error('Unable to read the file.'));reader.readAsDataURL(file);});
        Object.assign(item,await api('/attachments',{name:file.name,data:base64}));
      } catch(e) {item.error=e.message;}
      finally {item.pending=false;if(key===room())updateUploads();}
    }
  }
  const APPROVAL_HINT='Applies to running commands and opening websites in this chat. Folder access and credentials always ask.';
  const approvalMode=()=>d().settings?.approval_modes?.[room()]==='auto'?'auto':'ask';
  function approvalChip() {
    const mode=approvalMode();
    return '<label class="approval-chip'+(mode==='auto'?' is-auto':'')+'" title="'+esc(APPROVAL_HINT)+'">'+icon('shield')+'<span class="sr-only">Approval mode</span><select id="co-approval-mode" aria-describedby="co-approval-hint"><option value="ask"'+(mode==='ask'?' selected':'')+'>Ask for approval</option><option value="auto"'+(mode==='auto'?' selected':'')+'>Always approve for me</option></select></label><span id="co-approval-hint" class="sr-only">'+esc(APPROVAL_HINT)+'</span>';
  }
  function decisionBarMarkup() {
    const open=chatDecisions().open;
    if(decisionIndex>=open.length) decisionIndex=-1;
    decisionBarKey=decisionBar(open.length,decisionIndex,icon,esc);
    return '<div id="decision-bar" class="decision-bar" role="status" aria-label="Pending decisions"'+(open.length?'':' hidden')+'>'+decisionBarKey+'</div>';
  }
  function syncDecisionBar() {
    const open=chatDecisions().open,bar=$('#decision-bar');
    if(decisionIndex>=open.length||open.length!==decisionCount) decisionIndex=-1;
    decisionCount=open.length;
    if(!bar) return;
    const markup=decisionBar(open.length,decisionIndex,icon,esc);
    if(markup===decisionBarKey&&bar.hidden===!open.length) return;
    const focusedAction=bar.contains(document.activeElement)?document.activeElement.dataset.action:null;
    bar.innerHTML=markup;bar.hidden=!open.length;decisionBarKey=markup;
    if(focusedAction) bar.querySelector('[data-action="'+focusedAction+'"]')?.focus({preventScroll:true});
    resizeComposer();
  }
  // One-time attention pulse the first time a decision card is rendered (CSS removes it for reduced motion).
  function pulseNewDecisions() {
    document.querySelectorAll('#co-transcript .decision-card').forEach(card=>{
      const id=card.dataset.decision;if(pulsed.has(id)) return;
      pulsed.add(id);card.classList.add('attention');
      card.addEventListener('animationend',()=>card.classList.remove('attention'),{once:true});
    });
  }
  function jumpDecision(delta) {
    const open=chatDecisions().open,cards=()=>[...document.querySelectorAll('#co-transcript .decision-card')];
    if(cards().length!==open.length) {
      if(document.activeElement?.closest('.decision-card')) document.activeElement.blur();
      transcriptKey='';update();
    }
    const found=cards();if(!found.length) return;
    decisionIndex=decisionIndex<0?(delta>0?0:found.length-1):stepIndex(decisionIndex,found.length,delta);
    let card=found[decisionIndex];
    if(card.hidden) {
      const key=card.closest('.decision-group')?.dataset.group;
      if(key){groupOpen.add(key);transcriptKey='';update();card=cards()[decisionIndex]||card;}
    }
    card.scrollIntoView({block:'center',behavior:matchMedia('(prefers-reduced-motion:reduce)').matches?'auto':'smooth'});
    card.classList.remove('attention');void card.offsetWidth;card.classList.add('attention');
    card.addEventListener('animationend',()=>card.classList.remove('attention'),{once:true});
    card.querySelector('button:not(:disabled),input,a[href],summary')?.focus({preventScroll:true});
    syncDecisionBar();
  }
  async function setApprovalMode(select) {
    const channel=room(),mode=select.value==='auto'?'auto':'ask',previous=approvalMode();
    select.disabled=true;
    try {
      const result=await api('/approval-mode',{channel,mode});
      const settings=d().settings;settings.approval_modes={...(settings.approval_modes||{}),[channel]:result?.mode||mode};
      toast(mode==='auto'?'Always approve is on for this chat. '+APPROVAL_HINT:'Asking for approval in this chat.');
      await refresh(false);
    } catch(e) {select.value=previous;toast(e.message,true);}
    finally {select.disabled=false;syncApproval();}
  }
  function syncApproval() {
    const select=$('#co-approval-mode');if(!select) return;
    const mode=approvalMode();
    if(select.value!==mode&&!select.disabled) select.value=mode;
    select.closest('.approval-chip')?.classList.toggle('is-auto',mode==='auto');
  }
  function view() {
    const a=selected();
    return '<div class="co-chat '+(contextOpen?'with-context':'')+'"><section class="conversation" aria-label="Conversation with '+esc(a.name)+'"><header class="conversation-header">'+avatar(a)+'<div class="grow"><h1>'+esc(a.name)+'</h1><p>'+esc(a.role)+'</p></div>'+
      '<button type="button" class="button small" data-action="co-stop-all">Stop all</button>'+
      '<button class="icon-button" data-action="co-context" aria-label="Work and files" aria-expanded="'+contextOpen+'">'+icon('tasks')+'</button></header>'+
      (h.getMode()==='demo'?'<div class="co-preview">Interface preview. <a href="http://127.0.0.1:8765">Open local app</a> to start real team work.</div>':'')+
      '<div id="co-transcript" class="chat-messages co-transcript" aria-label="Messages"><div class="transcript-inner">'+transcript()+'</div></div>'+
      '<div class="composer-area">'+decisionBarMarkup()+'<form id="chat-form" class="co-composer" novalidate><input type="hidden" name="agent_id" value="'+esc(a.id)+'"><input id="chat-attachments" type="file" multiple hidden accept=".png,.jpg,.jpeg,.webp,.txt,.md,.csv,.json,.xml,.yaml,.yml,.toml,.html,.css,.js,.ts,.tsx,.jsx,.py,.sql,.log,.svg,.pdf,.docx,.xlsx,.pptx"><div id="composer-attachments">'+attachmentMarkup(draftFiles(),true)+'</div><div id="composer-skills" class="composer-skills" aria-label="Skills for this message">'+skillChipsMarkup(draftSkills(),esc)+'</div><label class="sr-only" for="chat-input">Message '+esc(a.name)+'</label><textarea id="chat-input" name="message" rows="2" placeholder="Describe a goal, ask a question, or type / for commands…" aria-haspopup="listbox" maxlength="20000"></textarea><div class="composer-tools"><button class="icon-button" type="button" data-action="co-attach" aria-label="Attach images or files" title="Images, documents or code · Up to 6 files">'+icon('plus')+'</button><button class="icon-button" type="button" data-action="co-image-studio" aria-label="Generate an image" title="Image Studio">'+icon('image')+'</button><span>Self-coordinating team · Asks when decisions are needed</span>'+approvalChip()+'<button type="button" id="co-model-binding" class="model-binding" data-action="co-model" data-id="'+esc(a.id)+'" aria-label="Model & reasoning">'+icon('spark')+'<span>'+esc(modelLabel(a))+'</span>'+icon('down')+'</button><button class="composer-send" type="submit" aria-label="Send message">'+icon('arrow')+'</button></div><p id="chat-error" class="inline-error" role="alert"></p></form><div id="chat-usage">'+usageMini(withRetained(d().runtime?.providers),esc)+'</div><div class="composer-note">Enter to send · Shift + Enter for a new line · / for commands · Paste images or drop files</div></div></section>'+
      '<aside id="co-context" class="work-context" aria-label="Work and files"><header><h2>Work & files</h2><button class="icon-button" data-action="co-context" aria-label="Close work panel">'+icon('close')+'</button></header><div id="co-context-body" tabindex="-1">'+context()+'</div></aside></div>'+afterView();
  }
  function afterView() {slash.close();queueMicrotask(()=>previewPane.attach($('.co-chat')));setTimeout(()=>{pulseNewDecisions();syncApproval();},0);return '';}
  function update() {
    resizeComposer();
    const usage=$('#chat-usage');if(usage)updateUsage(usage,d().runtime?.providers,esc);
    syncApproval();
    const label=$('#co-model-binding span');
    if(label&&label.textContent!==modelLabel(selected())) label.textContent=modelLabel(selected());
    syncRoster();
    const el=$('#co-transcript');
    if(!el) return;
    const markup=transcript();
    const focused=document.activeElement;
    // Keep decision drafts, focused controls, open handoffs and the composer intact.
    if(stableMarkup(markup)!==transcriptKey&&!focused?.closest('.decision-card')) {
      const bottom=el.scrollHeight-el.scrollTop-el.clientHeight<110, oldScroll=el.scrollTop;
      const detailKey=x=>x.dataset.key||x.querySelector('summary')?.textContent;
      const opened=[...el.querySelectorAll('details[open]')].map(detailKey);
      el.innerHTML='<div class="transcript-inner">'+markup+'</div>';
      el.querySelectorAll('details').forEach(x=>x.open=opened.includes(detailKey(x)));
      el.scrollTop=bottom?el.scrollHeight:oldScroll;
      transcriptKey=stableMarkup(markup);
    }
    syncDecisionBar();pulseNewDecisions();
    const next=context();
    const body=$('#co-context-body');
    if(body&&next!==contextKey) {
      const scroll=body.scrollTop,codeScroll=body.querySelector('.diff-view,.file-content')?.scrollTop||0;
      const control=body.contains(focused)?{action:focused.dataset.action,id:focused.dataset.id}:null;
      body.innerHTML=next;body.scrollTop=scroll;contextKey=next;
      const code=body.querySelector('.diff-view,.file-content');if(code)code.scrollTop=codeScroll;
      if(control) [...body.querySelectorAll('[data-action]')].find(el=>el.dataset.action===control.action&&el.dataset.id===control.id)?.focus({preventScroll:true});
    }
  }
  async function answer(id,text,form,grant) {
    if(!text?.trim()) throw new Error('Select an option or enter an answer.');
    await api('/decisions/'+id+'/answer',{answer:text, ...(grant?{grant}:{})});
    await refresh(false);update();
    if(document.activeElement?.closest('.decision-card')) document.activeElement.blur();
    transcriptKey='';update();toast('Decision sent. The agent will resume automatically.');
  }
  async function perform(action,id,target) {
    if(!action.startsWith('co-')&&!action.startsWith('rich-')) return false;
    try {
      if(action==='rich-file') await previewPane.openFile({source:target?.dataset.source||'artifact',id});
      if(action==='rich-revoke-browser') {
        const project=roomProject();
        openModal('Revoke browser access?','Close the project browser session and stop accessing this source.',field('origin','Website',id,'text',{required:true})+'<input type="hidden" name="version" value="'+project.version+'">',button('Cancel','modal-close')+'<button type="submit" class="button primary">Revoke access</button>','co-browser-revoke',project.id);
        $('#f-origin').readOnly=true;
      }
      if(action==='co-model') modelDialog(actor(id));
      if(action==='co-model-refresh') {
        await api('/models/refresh',{provider:id});await refresh(false);modelDialog(actor(target.dataset.agent));
      }
      if(action==='co-room') {h.setRoom('dm-'+id);location.hash='chat';h.render();$('.sidebar')?.classList.remove('open');if(matchMedia('(max-width:760px)').matches) {$('.sidebar').inert=true;$('.mobile-menu')?.setAttribute('aria-expanded','false');}}
      if(action==='co-context') {contextOpen=!contextOpen;$('.co-chat')?.classList.toggle('with-context',contextOpen);previewPane.attach($('.co-chat'));document.querySelector('.conversation-header [data-action=co-context]')?.setAttribute('aria-expanded',String(contextOpen));if(contextOpen) $('#co-context-body')?.focus();else $('.conversation-header [data-action=co-context]')?.focus();}
      if(action==='co-attach') $('#chat-attachments').click();
      if(action==='co-image-studio') {openImageStudio({agentId:selected().id,projectId:roomProject()?.id||''});return true;}
      if(action==='co-remove-file') {draftFiles().splice(Number(id),1);updateUploads();}
      if(action==='co-step') stepDialog(id);
      if(action==='co-step-remove') {
        const item=progressItems().find(c=>c.id===id);
        openModal('Remove step “'+esc(item.title)+'”?','This step will be hidden from the checklist. Its linked task will be retained.',
          '<p>You can add the step again when the plan changes.</p>',button('Cancel','modal-close')+'<button type="submit" class="button primary">Remove step</button>','co-step-remove',id);
      }
      if(action==='co-step-up'||action==='co-step-down') {
        const items=progressItems(),index=items.findIndex(c=>c.id===id),other=items[index+(action==='co-step-up'?-1:1)],item=items[index];
        if(other) {await saveStep(item,{position:other.position+(action==='co-step-up'?-.5:.5)});await refresh(false);contextKey='';update();}
      }
      if(action==='co-preview-back') return true;
      if(action==='co-change') await previewPane.openChange(id);
      if(action==='co-new') {h.setRoom('dm-a-lead');location.hash='chat';h.render();$('#chat-input').value='Create a new teammate with the role ';resizeComposer();$('#chat-input').focus();}
      if(action==='co-unstick') {target.disabled=true;const result=await api('/runs/'+id+'/unstick',{});await refresh(false);update();toast(result.message||'Resumed.');}
      if(action==='co-cancel') {await api('/runs/'+id+'/cancel',{cascade:true});await refresh(false);update();toast('This work branch has stopped.');}
      if(action==='co-stop-all') {target.disabled=true;await api('/runtime/stop',{});await refresh(false);h.render();toast('All agents stopped and execution disabled.');}
      if(action==='co-special-answer'||action==='co-decision-action') {
        const buttons=[...(target.closest('.decision-actions')?.querySelectorAll('button')||[target])];buttons.forEach(b=>b.disabled=true);
        try {
          const payload=actionPayload(list('decisions').find(q=>q.id===id),target.dataset.value);
          if(!payload) throw new Error('This decision is no longer available.');
          await api('/decisions/'+id+'/answer',payload);await refresh(false);
          if(document.activeElement?.closest('.decision-card')) document.activeElement.blur();
          transcriptKey='';update();toast('Decision recorded.');
        } catch(e) {buttons.forEach(b=>{if(b.isConnected)b.disabled=false;});throw e;}
      }
      if(action==='co-decision-next'||action==='co-decision-prev') jumpDecision(action==='co-decision-next'?1:-1);
      if(action==='co-decision-reply') {
        const card=target.closest('.decision-card'),box=card?.querySelector('.dc-reply');
        if(box) {
          const show=box.hidden;box.hidden=!show;target.setAttribute('aria-expanded',String(show));
          if(show){replyOpen.add(id);box.querySelector('input')?.focus();}else replyOpen.delete(id);
        }
      }
      if(action==='co-decision-more') {
        if(groupOpen.has(id)) groupOpen.delete(id);else groupOpen.add(id);
        if(document.activeElement?.closest('.decision-group')) document.activeElement.blur();
        transcriptKey='';update();
        document.querySelector('#co-transcript [data-action="co-decision-more"][data-id="'+CSS.escape(id)+'"]')?.focus({preventScroll:true});
      }
      if(action==='co-skill-remove') {const chosen=draftSkills(),at=chosen.findIndex(x=>x.id===id);if(at>=0)chosen.splice(at,1);updateSkills();$('#chat-input')?.focus();}
      if(action==='co-answer') {target.disabled=true;await answer(id,target.dataset.answer,target.closest('form'));}
      if(action==='co-artifact') await previewPane.openFile({source:'artifact',id});
      if(action==='co-download') {
        const result=await api('/artifacts/'+id,undefined,'GET');
        const blob=new Blob([result.file.content||''],{type:'text/plain;charset=utf-8'}),url=URL.createObjectURL(blob),a=document.createElement('a');
        a.href=url;a.download=result.artifact.path.split(/[\\/]/).at(-1);a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
      }
      if(action==='co-folder') {
        if(!id) throw new Error('Send a goal to create a project before connecting a folder.');
        openModal('Connect project folder','Grant access once for this project so the team can read the files it needs.',
          field('path','Absolute path on the runtime machine','','text',{required:true,placeholder:'C:\\Projects\\my-app'})+
          field('access','Access','read','select',{options:[['read','Read documents and source code'],['readwrite','Read & write']]}),
          button('Cancel','modal-close')+'<button type="submit" class="button primary">Connect folder</button>','co-folder',id);
      }
      if(action==='co-roster-group') {
        if(rosterCollapsed.has(id)) rosterCollapsed.delete(id);else rosterCollapsed.add(id);
        saveCollapsed();syncRoster();
      }
      if(action==='co-folders-all') {foldersAll=!foldersAll;update();}
      if(action==='co-outputs-all') {changesAll=!changesAll;update();}
      if(action==='co-goal-remove') {
        const project=roomProject();if(!project) return true;
        await api('/projects/'+project.id+'/goals/'+encodeURIComponent(id)+'/remove',{});
        await refresh(false);contextKey='';update();toast('Goal removed.');
      }
      if(action==='co-default') modelDialog(actor('a-lead'),true);
      if(action==='co-model-pick') {$('#f-default_model').value=id;$('#f-default_provider').value=target.dataset.provider;}
    } catch(e) {const error=target?.closest('form')?.querySelector('.inline-error')||$('#modal-error');if(error) error.textContent=e.message;else toast(e.message,true);}
    finally {if(target?.isConnected)target.disabled=false;}
    return true;
  }
  async function submit(form) {
    if(form.id==='chat-form') {await sendChat(form);return true;}
    if(form.dataset.type==='co-browser-revoke') {
      if(form.dataset.submitting)return true;form.dataset.submitting='true';const submit=form.querySelector('[type=submit]');submit.disabled=true;
      try{const values=Object.fromEntries(new FormData(form));values.version=Number(values.version);await api('/projects/'+form.dataset.id+'/browser-revoke',values);closeModal(true);await refresh(false);contextKey='';update();toast('Browser access revoked.');}
      catch(e){$('#modal-error').textContent=e.message;}
      finally{delete form.dataset.submitting;if(submit.isConnected)submit.disabled=false;}
      return true;
    }
    if(form.dataset.type==='co-step'||form.dataset.type==='co-step-remove') {
      if(form.dataset.submitting) return true;
      if(form.dataset.type==='co-step'&&!h.validateForm(form)) return true;
      const item=progressItems().find(c=>c.id===form.dataset.id);form.dataset.submitting='true';const submit=form.querySelector('[type=submit]');submit.disabled=true;
      try {
        const values=new FormData(form),patch=form.dataset.type==='co-step-remove'?{archived:true}:{title:values.get('title'),note:values.get('note'),agent_ids:values.getAll('agent_ids'),status:values.get('status')||item?.status||'todo'};
        if(item) await saveStep(item,patch);else await api('/checklist',{...patch,project_id:roomProject().id,position:progressItems().length});
        closeModal(true);await refresh(false);contextKey='';update();toast('Checklist updated.');
      } catch(e) {$('#modal-error').textContent=e.message;}
      finally {delete form.dataset.submitting;if(submit.isConnected)submit.disabled=false;}
      return true;
    }
    if(form.dataset.type==='co-chat-model') {
      if(!h.validateForm(form)) return true;
      if(form.dataset.submitting) return true;
      form.dataset.submitting='true';
      const submit=form.querySelector('button[type=submit]');submit.disabled=true;
      try {
        const values=new FormData(form),key=values.get('model_key');
        if(!key) throw new Error('Choose an available model or inherit the team default.');
        const [provider,model]=key==='inherit'?[]:JSON.parse(key);
        const scope=values.get('scope')||undefined;
        await api('/agents/'+form.dataset.id+'/model',key==='inherit'?{inherit:true,...(scope?{scope:'chat'}:{})}:{provider,model,reasoning_effort:values.get('reasoning_effort')||null,...(scope?{scope}:{})});
        closeModal(true);await refresh(false);if($('#chat-form'))update();else h.render();toast(scopeToast(key==='inherit'&&scope?'chat':scope));
      } catch(e) {$('#modal-error').textContent=e.message;$('#f-model_key')?.setAttribute('aria-invalid','true');$('#f-model_key')?.focus();}
      finally {delete form.dataset.submitting;if(submit.isConnected)submit.disabled=false;}
      return true;
    }
    if(form.dataset.decision) {
      const data=new FormData(form),grant=data.get('folder_path')?{path:data.get('folder_path'),access:data.get('folder_access')}:undefined;
      try {await answer(form.dataset.decision,String(data.get('answer')|| (grant?'Grant folder access '+grant.path:'')),form,grant);}
      catch(e) {form.querySelector('.inline-error').textContent=e.message;}
      return true;
    }
    if(form.dataset.type==='co-default') {
      try {const values=Object.fromEntries(new FormData(form));values.max_parallel=Number(values.max_parallel);await api('/settings',values);closeModal(true);await refresh(false);h.render();toast('Default model saved. Waiting work will resume automatically.');}
      catch(e) {$('#modal-error').textContent=e.message;}
      return true;
    }
    if(form.dataset.type==='co-folder') {
      try {const values=Object.fromEntries(new FormData(form));await api('/projects/'+form.dataset.id+'/folder',values);closeModal(true);await refresh(false);update();toast('Project folder connected.');}
      catch(e) {$('#modal-error').textContent=e.message;}
      return true;
    }
    return false;
  }
  document.addEventListener('toggle',event=>{if(event.target.matches?.('.detail-section,.goal-done,.output-sub'))sectionOpen[event.target.dataset.section]=event.target.open;},true);
  document.addEventListener('input',event=>{if(event.target.id==='roster-search'){rosterQuery=event.target.value;syncRoster();}});
  document.addEventListener('input',event=>{if(event.target.id==='chat-input'){resizeComposer();slash.sync(event.target);}});
  // Capture phase: the slash menu must see Enter/Tab/arrows before the Enter-to-send handler.
  document.addEventListener('keydown',event=>{if(event.target.id==='chat-input') slash.handleKey(event);},true);
  document.addEventListener('change',event=>{
    if(event.target.id==='co-approval-mode')setApprovalMode(event.target);
    if(event.target.dataset?.action==='co-project-pick'){projectChoice.set(room(),{id:event.target.value,at:Date.now()});saveChoice();contextKey='';update();}
  });
  window.addEventListener('resize',resizeComposer);
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'){
      if(contextOpen&&matchMedia('(max-width:1100px)').matches&&!document.querySelector('dialog[open]'))perform('co-context','');
    }
  });
  document.addEventListener('paste',event=>{if(event.target.id==='chat-input'&&event.clipboardData?.files.length) {event.preventDefault();addFiles(event.clipboardData.files);}});
  document.addEventListener('dragover',event=>{if(event.target.closest('.co-composer')&&event.dataTransfer?.types.includes('Files')){event.preventDefault();event.dataTransfer.dropEffect='copy';}});
  document.addEventListener('drop',event=>{if(event.target.closest('.co-composer')&&event.dataTransfer?.files.length){event.preventDefault();addFiles(event.dataTransfer.files);}});
  return {shell,view,update,perform,submit,resizeComposer,projectId:()=>roomProject()?.id,addFiles,hasAttachments:()=>draftFiles().length>0,
    attachmentIds,clearAttachments:key=>{uploads.delete(key);updateUploads();}};
}
