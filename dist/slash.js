// Composer slash commands: "/" at the start opens a compact menu; some commands open pickers.
// Pure helpers are exported for tests/slash.mjs; the DOM controller (createSlash) is the only part touching the page.
const svg=path=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+path+'</svg>';
export const ICONS={
  goal:'<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r=".8"/>',
  'create-skill':'<path d="m12 3 1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="m18 16 .7 1.8L20.5 18.5l-1.8.7L18 21l-.7-1.8-1.8-.7 1.8-.7z"/>',
  'install-skill':'<path d="M12 4v10m-4-4 4 4 4-4"/><path d="M5 18h14"/>',
  skill:'<path d="M5 5h14v14H5z"/><path d="M9 9h6m-6 3h6m-6 3h3"/>',
  mcp:'<path d="M9 7V4m6 3V4M7 7h10v4a5 5 0 0 1-10 0z"/><path d="M12 16v4"/>',
  'create-tool':'<path d="M14.5 6.5a4 4 0 0 0-5 5L4 17l3 3 5.5-5.5a4 4 0 0 0 5-5L15 12l-2-2z"/>',
  search:'<circle cx="11" cy="11" r="6"/><path d="m16 16 4 4"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  check:'<path d="m5 12.5 4.5 4.5L19 7.5"/>'
};
export const icon=name=>svg(ICONS[name]||ICONS.skill);

/** args: true = the command takes free text; chat: true = goes through the normal /chat send path. */
export const COMMANDS=[
  {id:'goal',title:'Set goal',hint:'Add a goal to this chat\'s project',usage:'/goal <goal>',args:true},
  {id:'create-skill',title:'Create skill',hint:'Research and write a new skill for the team',usage:'/create-skill <what>',args:true,chat:true},
  {id:'install-skill',title:'Install skill',hint:'Browse and install skills from libraries',usage:'/install-skill'},
  {id:'skill',title:'Use skill',hint:'Add an installed skill to this message',usage:'/skill'},
  {id:'mcp',title:'Connect MCP',hint:'Add or manage an MCP server',usage:'/mcp'},
  {id:'create-tool',title:'Create tool',hint:'Build, test and register a custom tool',usage:'/create-tool <what>',args:true,chat:true}
];
export const commandById=id=>COMMANDS.find(c=>c.id===String(id||'').toLowerCase())||null;

/** Text typed so far if the popup should be open (the "/command" token only, no spaces yet), else null. */
export function slashQuery(value) {
  const m=/^\/([a-z-]*)$/i.exec(String(value??''));
  return m?m[1].toLowerCase():null;
}
/** Commands matching what was typed after "/": prefix matches first, then word/substring matches in id, title, hint. */
export function filterCommands(query='',commands=COMMANDS) {
  const q=String(query).trim().toLowerCase();
  if(!q) return commands.slice();
  const score=c=>{
    if(c.id.startsWith(q)) return 0;
    if(c.title.toLowerCase().split(/\s+/).some(w=>w.startsWith(q))) return 1;
    if(c.id.includes(q)||c.title.toLowerCase().includes(q)||c.hint.toLowerCase().includes(q)) return 2;
    return -1;
  };
  return commands.map((c,i)=>({c,i,s:score(c)})).filter(x=>x.s>=0).sort((a,b)=>a.s-b.s||a.i-b.i).map(x=>x.c);
}
/** "/goal foo bar" -> {command:'goal', text:'foo bar'}; unknown commands and plain text -> null. */
export function parseSlash(text) {
  const m=/^\/([a-z-]+)(?:\s+([\s\S]*))?$/i.exec(String(text??'').trim());
  if(!m) return null;
  const command=commandById(m[1]);
  return command?{command:command.id,text:(m[2]||'').trim()}:null;
}
export const wrap=(index,count)=>count?((index%count)+count)%count:0;
/**
 * Keyboard model for the popup and the pickers. Returns null when the key is not ours.
 * {type:'move',index} | {type:'accept',index} | {type:'close'}
 */
export function keyAction({key,index=0,count=0,shift=false,composing=false}) {
  if(composing) return null;
  if(key==='Escape') return {type:'close'};
  if(!count) return null;
  if(key==='ArrowDown') return {type:'move',index:wrap(index+1,count)};
  if(key==='ArrowUp') return {type:'move',index:wrap(index-1,count)};
  if(key==='Home') return {type:'move',index:0};
  if(key==='End') return {type:'move',index:count-1};
  if(key==='Tab') return {type:'accept',index:wrap(index,count)};
  if(key==='Enter'&&!shift) return {type:'accept',index:wrap(index,count)};
  return null;
}
/** Text left in the composer after choosing a command from the menu. */
export const completion=command=>'/'+command.id+(command.args?' ':'');

/** Installed, non-archived skills filtered by name/description/category. */
export function installedSkills(skills,query='') {
  const q=String(query).trim().toLowerCase();
  return (skills||[]).filter(s=>s&&s.id&&s.installed!==false&&!s.archived&&!s.sample)
    .filter(s=>!q||[s.name,s.description,s.category,s.source].some(x=>String(x||'').toLowerCase().includes(q)));
}
export function catalogPath({q='',source='all'}={}) {
  const params=new URLSearchParams();
  if(String(q).trim()) params.set('q',String(q).trim());
  if(source&&source!=='all') params.set('source',source);
  const qs=params.toString();
  return '/skills/catalog'+(qs?'?'+qs:'');
}
export const SOURCE_NAMES={installed:'Installed',library:'Library',claude:'Claude',codex:'Codex',third_party:'Third party'};
export const sourceLabel=(id,sources=[])=>sources.find(s=>s.id===id)?.label||SOURCE_NAMES[id]||String(id||'');
export const sourceChips=sources=>[{id:'all',label:'All'},...(Array.isArray(sources)?sources:[]).filter(s=>s&&s.id).map(s=>({id:s.id,label:s.label||SOURCE_NAMES[s.id]||s.id,count:s.count}))];
/** Backend not deployed / route missing -> calm "Not available yet" instead of a raw error. */
export const isUnavailable=error=>/endpoint not found|not supported|unknown (file )?endpoint|page not found|404|unexpected token|<!doctype/i.test(String(error?.message||error||''));
export function debounce(fn,ms=250) {
  let timer=0;
  const wrapped=(...args)=>{clearTimeout(timer);timer=setTimeout(()=>fn(...args),ms);};
  wrapped.cancel=()=>clearTimeout(timer);
  return wrapped;
}
export function skillChipsMarkup(skills,esc) {
  if(!skills?.length) return '';
  return skills.map(s=>'<span class="skill-chip" title="'+esc(s.name)+' · used for this message only">'+icon('skill')+'<span>'+esc(s.name)+'</span><button type="button" data-action="co-skill-remove" data-id="'+esc(s.id)+'" aria-label="Remove skill '+esc(s.name)+'">'+icon('close')+'</button></span>').join('');
}
export function commandRowMarkup(command,active,esc) {
  return '<div role="option" class="slash-item'+(active?' is-active':'')+'" id="slash-opt-'+esc(command.id)+'" data-cmd="'+esc(command.id)+'" aria-selected="'+(active?'true':'false')+'"><span class="slash-ico">'+icon(command.id)+'</span><span class="slash-copy"><strong>/'+esc(command.id)+'</strong><span>'+esc(command.title)+' · '+esc(command.hint)+'</span></span></div>';
}

/* ---------------- DOM controller ---------------- */
const PAGE=60;
/**
 * h = {esc, api, toast, refresh, getAgentId(), getAgents(), getSkills(), onPickSkill(skill)}
 * Returns {sync, handleKey, close, isOpen, run}.
 */
export function createSlash(h) {
  const {esc}=h;
  let popup=null,matches=[],index=0,input=null;
  const ensure=()=>{
    if(popup) return popup;
    popup=document.createElement('div');popup.id='slash-popup';popup.className='slash-popup';popup.setAttribute('role','listbox');popup.setAttribute('aria-label','Slash commands');popup.hidden=true;
    popup.addEventListener('mousedown',e=>e.preventDefault());
    popup.addEventListener('click',e=>{const row=e.target.closest('[data-cmd]');if(row)accept(commandById(row.dataset.cmd));});
    popup.addEventListener('mousemove',e=>{const row=e.target.closest('[data-cmd]');if(!row)return;const i=matches.findIndex(c=>c.id===row.dataset.cmd);if(i>=0&&i!==index){index=i;paint();}});
    document.body.append(popup);
    document.addEventListener('mousedown',e=>{if(popup&&!popup.hidden&&!popup.contains(e.target)&&e.target!==input)close();});
    window.addEventListener('resize',()=>{if(isOpen())place();});
    return popup;
  };
  const isOpen=()=>!!popup&&!popup.hidden;
  function place() {
    const anchor=input?.closest('.co-composer')||input;if(!anchor) return;
    const r=anchor.getBoundingClientRect(),width=Math.min(r.width,480);
    popup.style.left=Math.max(8,r.left)+'px';popup.style.width=width+'px';popup.style.bottom=(innerHeight-r.top+6)+'px';
    popup.style.maxHeight=Math.max(120,r.top-16)+'px';
  }
  function paint() {
    popup.innerHTML=matches.map((c,i)=>commandRowMarkup(c,i===index,esc)).join('');
    input?.setAttribute('aria-activedescendant','slash-opt-'+matches[index].id);
    popup.querySelector('.is-active')?.scrollIntoView({block:'nearest'});
  }
  function close() {
    if(popup) popup.hidden=true;
    matches=[];index=0;
    if(input){input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant');}
  }
  function sync(el) {
    input=el;
    const query=slashQuery(el.value);
    if(query===null){close();return;}
    const found=filterCommands(query);
    if(!found.length){close();return;}
    ensure();
    const keep=matches[index]?.id;matches=found;index=Math.max(0,found.findIndex(c=>c.id===keep));
    popup.hidden=false;place();paint();
    el.setAttribute('role','combobox');el.setAttribute('aria-expanded','true');el.setAttribute('aria-controls','slash-popup');el.setAttribute('aria-autocomplete','list');
  }
  function setValue(value) {
    input.value=value;input.setSelectionRange(value.length,value.length);
    input.dispatchEvent(new Event('input',{bubbles:true}));
  }
  function accept(command) {
    if(!command||!input) return;
    const el=input;close();
    if(command.args){setValue(completion(command));el.focus();return;}
    setValue('');el.focus();run(command.id,'');
  }
  /** Returns true when the key was consumed. Must run in the capture phase before the Enter-to-send handler. */
  function handleKey(event) {
    if(!isOpen()||event.target!==input) return false;
    const action=keyAction({key:event.key,index,count:matches.length,shift:event.shiftKey,composing:event.isComposing});
    if(!action) return false;
    event.preventDefault();event.stopPropagation();
    if(action.type==='move'){index=action.index;paint();}
    else if(action.type==='accept') accept(matches[action.index]);
    else close();
    return true;
  }

  /* ---- commands that do not use the chat send path ---- */
  function run(id,text) {
    if(id==='install-skill') return installDialog();
    if(id==='skill') return skillDialog();
    if(id==='mcp') {window.dispatchEvent(new CustomEvent('agentflow:open-mcp'));return;}
  }

  /* ---- shared picker dialog ---- */
  function dialogShell(title,bodyHtml,footerHtml='') {
    const opener=document.activeElement,dialog=document.createElement('dialog');
    dialog.className='slash-dialog';dialog.setAttribute('aria-labelledby','slash-dialog-title');
    dialog.innerHTML='<form method="dialog" class="sd-form" novalidate><header class="sd-head"><h2 id="slash-dialog-title">'+esc(title)+'</h2><button type="button" class="icon-button" data-sd="close" aria-label="Close">'+icon('close')+'</button></header>'+bodyHtml+(footerHtml?'<footer class="sd-foot">'+footerHtml+'</footer>':'')+'</form>';
    dialog.addEventListener('click',e=>{if(e.target===dialog||e.target.closest('[data-sd=close]'))dialog.close();});
    dialog.addEventListener('submit',e=>e.preventDefault());
    // A type=search field would swallow the first Escape to clear itself; one Escape should close the picker.
    dialog.addEventListener('keydown',e=>{if(e.key==='Escape'&&!e.isComposing&&e.target.matches?.('input[type=search]')){e.preventDefault();dialog.close();}});
    dialog.addEventListener('close',()=>{dialog.remove();if(opener?.isConnected)opener.focus({preventScroll:true});else document.querySelector('#chat-input')?.focus({preventScroll:true});});
    document.body.append(dialog);dialog.showModal();
    return dialog;
  }
  const listKeys=(search,getCount,move,accept)=>search.addEventListener('keydown',e=>{
    const a=keyAction({key:e.key,index:0,count:getCount(),shift:e.shiftKey,composing:e.isComposing});
    if(!a||a.type==='close') return;
    if(e.key==='Tab') return;
    e.preventDefault();
    if(a.type==='move') move(e.key==='ArrowDown'?1:e.key==='ArrowUp'?-1:e.key==='Home'?'home':'end');else accept();
  });

  function skillDialog() {
    const dialog=dialogShell('Use a skill','<div class="sd-search">'+icon('search')+'<input type="search" class="sd-input" placeholder="Search installed skills…" aria-label="Search installed skills" autocomplete="off"></div><div class="sd-list" role="listbox" aria-label="Installed skills" tabindex="-1"></div><p class="sd-note">Used for your next message only. It is not assigned to the agent permanently.</p>');
    const search=dialog.querySelector('.sd-input'),list=dialog.querySelector('.sd-list');let items=[],active=0;
    const paintList=()=>{
      items=installedSkills(h.getSkills(),search.value);active=Math.min(active,Math.max(0,items.length-1));
      list.innerHTML=items.length?items.map((s,i)=>'<div role="option" class="sd-item'+(i===active?' is-active':'')+'" id="sd-s-'+i+'" data-i="'+i+'" aria-selected="'+(i===active)+'"><span class="sd-name">'+esc(s.name)+(s.category?'<em class="sd-badge">'+esc(s.category)+'</em>':'')+'</span><span class="sd-desc">'+esc(s.description||'No description')+'</span></div>').join(''):'<p class="sd-empty">'+(search.value?'No installed skill matches “'+esc(search.value)+'”.':'No skills installed yet. Use /install-skill to add one.')+'</p>';
      search.setAttribute('aria-activedescendant',items.length?'sd-s-'+active:'');
      list.querySelector('.is-active')?.scrollIntoView({block:'nearest'});
    };
    const choose=()=>{const s=items[active];if(!s)return;dialog.close();h.onPickSkill({id:s.id,name:s.name});};
    search.addEventListener('input',()=>{active=0;paintList();});
    listKeys(search,()=>items.length,delta=>{active=delta==='home'?0:delta==='end'?items.length-1:wrap(active+delta,items.length);paintList();},choose);
    list.addEventListener('click',e=>{const row=e.target.closest('[data-i]');if(row){active=Number(row.dataset.i);choose();}});
    paintList();search.focus();
  }

  function installDialog() {
    const agents=(h.getAgents?.()||[]),current=h.getAgentId?.();
    const dialog=dialogShell('Install a skill',
      '<div class="sd-search">'+icon('search')+'<input type="search" class="sd-input" placeholder="Search skills by name or description…" aria-label="Search skills" autocomplete="off"></div>'+
      '<div class="sd-chips" role="group" aria-label="Source"></div><div class="sd-list" role="listbox" aria-label="Skills" tabindex="-1" aria-busy="true"></div><p class="sd-status" role="status" aria-live="polite"></p>',
      '<label class="sd-agent"><span>Install for</span><select aria-label="Agent">'+agents.map(a=>'<option value="'+esc(a.id)+'"'+(a.id===current?' selected':'')+'>'+esc(a.name)+'</option>').join('')+'</select></label><button type="button" class="button small primary" data-sd="install" disabled>Install</button>');
    const status=dialog.querySelector('.sd-status'),search=dialog.querySelector('.sd-input'),chips=dialog.querySelector('.sd-chips'),list=dialog.querySelector('.sd-list'),select=dialog.querySelector('select'),install=dialog.querySelector('[data-sd=install]');
    let items=[],sources=[],source='all',active=-1,shown=PAGE,request=0,state='loading',message='';
    const row=(s,i)=>'<div role="option" class="sd-item'+(i===active?' is-active':'')+(s.installed?' is-installed':'')+'" id="sd-c-'+i+'" data-i="'+i+'" aria-selected="'+(i===active)+'"><span class="sd-name">'+esc(s.name)+'<em class="sd-badge src-'+esc(s.source)+'">'+esc(sourceLabel(s.source,sources))+'</em>'+(s.installed?'<em class="sd-badge ok">'+icon('check')+'Installed</em>':'')+'</span><span class="sd-desc">'+esc(s.description||'No description')+'</span></div>';
    const paintChips=()=>{chips.innerHTML=sourceChips(sources).map(c=>'<button type="button" class="sd-chip'+(c.id===source?' is-on':'')+'" data-source="'+esc(c.id)+'" aria-pressed="'+(c.id===source)+'">'+esc(c.label)+(c.count!==undefined?'<small>'+esc(c.count)+'</small>':'')+'</button>').join('');};
    const paintList=()=>{
      list.setAttribute('aria-busy',String(state==='loading'));
      if(state==='loading'&&!items.length) list.innerHTML='<p class="sd-empty" role="status">Loading skills…</p>';
      else if(state==='error') list.innerHTML='<p class="sd-empty" role="status">'+esc(message)+'</p>';
      else if(!items.length) list.innerHTML='<p class="sd-empty" role="status">'+(search.value?'No skills match “'+esc(search.value)+'”.':'No skills found in this source.')+'</p>';
      else list.innerHTML=items.slice(0,shown).map(row).join('')+(items.length>shown?'<p class="sd-more">Scroll for '+(items.length-shown)+' more…</p>':'');
      search.setAttribute('aria-activedescendant',active>=0?'sd-c-'+active:'');
      const s=items[active];install.disabled=!s||s.installed||!select.value;
      list.querySelector('.is-active')?.scrollIntoView({block:'nearest'});
    };
    const load=async()=>{
      const mine=++request;state='loading';status.textContent='';paintList();
      try {
        const result=await h.api(catalogPath({q:search.value,source}),undefined,'GET');
        if(mine!==request) return;
        items=Array.isArray(result?.items)?result.items:[];sources=Array.isArray(result?.sources)?result.sources:sources;shown=PAGE;active=items.length?0:-1;state='ready';paintChips();paintList();
      } catch(e) {
        if(mine!==request) return;
        items=[];state='error';message=isUnavailable(e)?'Not available yet. The skill catalog is not enabled on this runtime.':e.message;
        if(isUnavailable(e)) h.toast('Not available yet',true);
        paintChips();paintList();
      }
    };
    const debounced=debounce(load,250);
    const move=delta=>{if(!items.length)return;const limit=Math.min(items.length,shown);active=delta==='home'?0:delta==='end'?limit-1:wrap(active+delta,limit);paintList();};
    const doInstall=async()=>{
      const s=items[active];if(!s||s.installed)return;
      install.disabled=true;install.textContent='Installing…';
      try {
        const result=await h.api('/skills/install-ref',{ref:s.ref,agent_id:select.value});
        s.installed=true;if(result?.skill_id)s.skill_id=result.skill_id;
        const done='Installed '+(result?.name||s.name)+' for '+(agents.find(a=>a.id===(result?.agent_id||select.value))?.name||'the agent')+'.';
        status.textContent=done;status.className='sd-status ok';h.toast(done);
        await h.refresh(false);
      } catch(e) {const text=isUnavailable(e)?'Not available yet':e.message;status.textContent=text;status.className='sd-status err';h.toast(text,true);}
      finally {install.textContent='Install';paintList();}
    };
    search.addEventListener('input',()=>debounced());
    chips.addEventListener('click',e=>{const b=e.target.closest('[data-source]');if(!b)return;source=b.dataset.source;paintChips();debounced.cancel();load();});
    list.addEventListener('click',e=>{const r=e.target.closest('[data-i]');if(!r)return;active=Number(r.dataset.i);paintList();});
    list.addEventListener('dblclick',e=>{if(e.target.closest('[data-i]'))doInstall();});
    list.addEventListener('scroll',()=>{if(shown<items.length&&list.scrollTop+list.clientHeight>=list.scrollHeight-60){shown+=PAGE;paintList();}});
    listKeys(search,()=>Math.min(items.length,shown),move,doInstall);
    install.addEventListener('click',doInstall);select.addEventListener('change',paintList);
    paintChips();load();search.focus();
  }

  return {sync,handleKey,close,isOpen,run,accept};
}
