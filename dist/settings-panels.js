// Settings panels: execution budget, provider-quota policy (+ ordered fallback models) and storage housekeeping.
// Markup builders are pure (see tests/settings-panels.mjs); createSettingsPanels wires them to the app helpers.
import {DEFAULT_BUDGET,budgetSettings} from './budget-ui.js';

export const QUOTA_POLICIES=[['ask','Ask me'],['auto_fallback','Switch to fallback automatically'],['wait','Wait for reset']];
export const BUDGET_FIELDS=[
  ['run_turns','Turns per agent run','Model turns one agent run may use before it pauses.',1,500],
  ['project_turns','Turns per project','All turns across a project, including delegated work.',1,2000],
  ['project_runs','Agent runs per project','How many agent runs one project may start.',1,500]
];

export function formatBytes(value) {
  const n=Number(value);
  if(!Number.isFinite(n)||n<=0) return '0 B';
  const units=['B','KB','MB','GB','TB'];let i=0,v=n;
  while(v>=1024&&i<units.length-1){v/=1024;i++;}
  return (v>=100||i===0?Math.round(v):v.toFixed(1))+' '+units[i];
}
const parseTime=at=>{if(at==null||at==='')return null;const n=typeof at==='number'?(at<1e12?at*1000:at):Date.parse(at);return Number.isFinite(n)?new Date(n):null;};
const fmtDate=at=>{const d=parseTime(at);return d?new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Ho_Chi_Minh',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false}).format(d)+' GMT+7':'';};

/** Normalise the budget form values to the settings contract (project_tokens is stored in tokens, edited in thousands). */
export function budgetFromValues(values) {
  const budget={};
  for(const [key,label,,min,max] of BUDGET_FIELDS) {
    const value=Number(values[key]);
    if(!Number.isInteger(value)||value<min||value>max) throw new Error(label+' must be a whole number between '+min+' and '+max+'.');
    budget[key]=value;
  }
  const thousands=Number(values.project_tokens_k===''||values.project_tokens_k==null?0:values.project_tokens_k);
  if(!Number.isFinite(thousands)||thousands<0||!Number.isInteger(thousands)) throw new Error('Token budget must be 0 (unlimited) or a whole number of thousands.');
  budget.project_tokens=thousands*1000;
  return budget;
}
export function housekeepingFromValues(values) {
  const min=Number(values.min_free_gb),days=Number(values.retention_days);
  if(!Number.isFinite(min)||min<0||min>10000) throw new Error('Minimum free space must be between 0 and 10,000 GB.');
  if(!Number.isInteger(days)||days<1||days>3650) throw new Error('Retention must be a whole number of days between 1 and 3,650.');
  return {auto_cleanup:!!values.auto_cleanup,min_free_gb:min,retention_days:days};
}

export const MAX_SKILL_SOURCES=10;
const GITHUB_SOURCE=/^https:\/\/(?:github\.com|raw\.githubusercontent\.com)\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+(?:\/|$)/;
/** Third-party skill sources: current list + the add form -> the settings.skill_sources payload (the backend assigns ids). */
export function skillSourcesFromValues(current,values) {
  const label=String(values.label||'').trim().replace(/\s+/g,' '),url=String(values.url||'').trim();
  if(!label||label.length>60) throw new Error('Enter a name of 1 to 60 characters.');
  if(!GITHUB_SOURCE.test(url)||url.length>300) throw new Error('Enter an https GitHub repository, folder or SKILL.md link.');
  const items=(current||[]).map(s=>({id:s.id,label:s.label,url:s.url}));
  if(items.length>=MAX_SKILL_SOURCES) throw new Error('You can add up to '+MAX_SKILL_SOURCES+' sources.');
  if(items.some(s=>s.url===url)) throw new Error('This source is already added.');
  return [...items,{label,url}];
}
export function skillSourcesPanel(settings,field,esc) {
  const items=Array.isArray(settings?.skill_sources)?settings.skill_sources:[];
  return '<section class="panel settings-panel" id="skill-sources-panel" aria-labelledby="skill-sources-title"><h2 id="skill-sources-title">Third-party skill sources</h2>'+
    '<p>Add GitHub repositories or folders with SKILL.md files. They appear in the /install-skill picker; nothing is installed or run until you choose a skill.</p>'+
    '<ul class="fallback-list" aria-label="Skill sources">'+(items.length?items.map(s=>'<li><span class="fb-name"><strong>'+esc(s.label)+'</strong><br><small class="muted">'+esc(s.url)+'</small></span><span class="fb-actions"><button type="button" class="button quiet small" data-action="skill-source-remove" data-id="'+esc(s.id)+'" aria-label="Remove '+esc(s.label)+'">Remove</button></span></li>').join(''):'<li class="fb-empty">No third-party sources yet.</li>')+'</ul>'+
    '<form id="skill-sources-form" novalidate><div class="field-grid">'+field('label','Name','','text',{placeholder:'Team skills',help:'Shown next to each skill from this source.'})+field('url','GitHub URL','','url',{placeholder:'https://github.com/owner/repo/tree/main/skills',help:'Repository, folder or SKILL.md link.'})+'</div>'+
    '<div class="row-actions"><button type="submit" class="button primary small"'+(items.length>=MAX_SKILL_SOURCES?' disabled':'')+'>Add source</button></div><div class="form-error" id="skill-sources-error" role="alert"></div></form></section>';
}

export const MODEL_AUTONOMY=[
  ['locked','Locked','Agents cannot change any model. Quota fallbacks still work.'],
  ['phases','Phases only (recommended)','Agents switch models per run and tune agents they created.'],
  ['full','Full','Agents may configure any agent except Atlas and owner-pinned ones.']
];
export const MODEL_SOURCES={owner_settings:'Settings',owner_chat:'Chat picker',owner_command:'Chat command',agent_tool:'Agent request',quota_switch:'Quota fallback',quota_restore:'Quota restore',migration:'Migration'};
export const modelText=b=>b&&b.model?(b.provider?b.provider+'/':'')+b.model+(b.reasoning_effort?' · '+b.reasoning_effort:''):'not set';
/** One change-log entry -> display strings. `names` maps agent ids to names. */
export function modelEventView(e,names={}) {
  const owner=String(e.source||'').startsWith('owner_'),agent=e.agent_id?(names[e.agent_id]||e.agent_id):'';
  const who=owner?'Owner':e.actor_id?(names[e.actor_id]||e.actor_id):'System';
  return {when:fmtDate(e.created_at),change:(e.kind==='agent_model'&&agent?agent+': ':'')+modelText(e.previous)+' → '+modelText(e.binding),
    source:MODEL_SOURCES[e.source]||e.source||'',who,reason:e.reason||''};
}
export function modelHistoryMarkup(history,names,esc) {
  if(!history) return '<p class="muted small">Checking recent model changes…</p>';
  const diverged=history.diverged||[],events=history.events||[];
  const rows=diverged.map(d=>'<li><span class="fb-name"><strong>'+esc(d.name)+'</strong> · '+esc(modelText(d.binding))+'<br><small class="muted">'+(d.binding_origin==='owner'?'Pinned by you':'Set by '+esc(names[d.set_by]||d.set_by))+'</small></span><span class="fb-actions"><button type="button" class="button quiet small" data-action="model-reset" data-id="'+esc(d.agent_id)+'" aria-label="Reset '+esc(d.name)+' to team default">Reset to team default</button></span></li>').join('');
  const log=events.map(e=>{const v=modelEventView(e,names);return '<li><span class="fb-name"><strong>'+esc(v.change)+'</strong><br><small class="muted">'+esc(v.when)+' · '+esc(v.source)+' · '+esc(v.who)+(v.reason?' · '+esc(v.reason):'')+'</small></span></li>';}).join('');
  return '<div class="field"><span class="label-like">Diverged agents</span>'+
    (diverged.length?'<ul class="fallback-list" aria-label="Agents not following the team default">'+rows+'</ul>':'<p class="muted small">Every agent follows the team default.</p>')+'</div>'+
    '<details class="model-history"><summary>Recent changes ('+events.length+')</summary>'+(events.length?'<ul class="fallback-list" aria-label="Recent model changes">'+log+'</ul>':'<p class="muted small">No changes recorded yet.</p>')+'</details>';
}
export function modelAutonomyPanel(settings,history,names,esc,button,withHistory=true) {
  const mode=MODEL_AUTONOMY.some(m=>m[0]===settings?.model_autonomy)?settings.model_autonomy:'phases';
  const current=settings?.default_model?modelText({provider:settings.default_provider,model:settings.default_model,reasoning_effort:settings.default_reasoning_effort}):'Not configured';
  return '<section class="panel settings-panel" id="model-panel" aria-labelledby="model-title"><h2 id="model-title">Team default model</h2>'+
    '<p><strong>'+esc(current)+'</strong> · Atlas and every agent set to inherit follow it. Only you, or a quota fallback you approved, change it.</p>'+
    '<div class="row-actions" style="margin-bottom:14px">'+button('Choose default model','co-default','','primary small','spark')+'</div>'+
    '<form id="model-autonomy-form" novalidate><fieldset class="ig-modes ma-modes"><legend class="label-like">Model autonomy</legend>'+
    MODEL_AUTONOMY.map(([id,name,hint])=>'<label class="ig-mode"><input type="radio" name="model_autonomy" value="'+id+'"'+(id===mode?' checked':'')+'><span class="ig-mode-face"><strong>'+esc(name)+'</strong><small>'+esc(hint)+'</small></span></label>').join('')+'</fieldset>'+
    '<div class="row-actions" style="margin-top:10px"><button type="submit" class="button primary small">Save autonomy</button></div><div class="form-error" id="model-autonomy-error" role="alert"></div></form>'+
    (withHistory?'<div id="model-history-slot" style="margin-top:14px">'+modelHistoryMarkup(history,names,esc)+'</div>':'')+'</section>';
}

export function budgetPanel(settings,field,esc) {
  const b=budgetSettings(settings);
  return '<section class="panel settings-panel" id="budget-panel" aria-labelledby="budget-title"><h2 id="budget-title">Budget</h2><p>Limits keep a goal from running away. Use “Allow for this task” on a halted goal to lift them once.</p>'+
    '<form id="budget-form" novalidate><div class="field-grid">'+
    BUDGET_FIELDS.map(([key,label,help,min,max])=>field(key,label,b[key],'number',{min,max,help})).join('')+
    field('project_tokens_k','Token budget per project (thousands)',Math.round((b.project_tokens||0)/1000),'number',{min:0,help:'0 means unlimited. Counts input, output and cache-creation tokens.'})+
    '</div><div class="row-actions" style="margin-top:12px"><button type="submit" class="button primary">Save budget</button><button type="button" class="button quiet" data-action="budget-reset">Restore defaults</button></div><div class="form-error" id="budget-error" role="alert"></div></form></section>';
}

export function fallbackListMarkup(items,catalog,esc) {
  const label=m=>{const hit=(catalog||[]).find(c=>c.provider===m.provider&&(c.id||c.model)===m.model);return (m.provider==='claude'?'Claude':m.provider==='codex'?'Codex':m.provider)+' · '+(hit?.name||m.model);};
  const taken=new Set(items.map(m=>m.provider+'\u0000'+m.model));
  const options=(catalog||[]).filter(c=>!taken.has(c.provider+'\u0000'+(c.id||c.model)));
  return '<ol class="fallback-list" aria-label="Fallback models in order">'+(items.length?items.map((m,i)=>'<li><span class="fb-index" aria-hidden="true">'+(i+1)+'</span><span class="fb-name">'+esc(label(m))+'</span><span class="fb-actions"><button type="button" class="button quiet small" data-action="fb-up" data-id="'+i+'" aria-label="Move '+esc(label(m))+' up"'+(i?'':' disabled')+'>↑</button><button type="button" class="button quiet small" data-action="fb-down" data-id="'+i+'" aria-label="Move '+esc(label(m))+' down"'+(i<items.length-1?'':' disabled')+'>↓</button><button type="button" class="button quiet small" data-action="fb-remove" data-id="'+i+'" aria-label="Remove '+esc(label(m))+'">Remove</button></span></li>').join(''):'<li class="fb-empty">No fallback models yet. Switching automatically needs at least one.</li>')+'</ol>'+
    '<div class="fallback-add"><label class="sr-only" for="fallback-pick">Add a fallback model</label><select id="fallback-pick"'+(options.length?'':' disabled')+'><option value="">'+(options.length?'Add a fallback model…':catalog?.length?'All catalog models added':'Load a model catalog in Connections first')+'</option>'+options.map(c=>'<option value="'+esc(JSON.stringify([c.provider,c.id||c.model]))+'">'+esc((c.provider==='claude'?'Claude':c.provider==='codex'?'Codex':c.provider)+' · '+(c.name||c.id))+'</option>').join('')+'</select><button type="button" class="button small" data-action="fb-add"'+(options.length?'':' disabled')+'>Add</button></div>';
}

export function quotaPanel(settings,items,catalog,field,esc) {
  return '<section class="panel settings-panel" id="quota-panel" aria-labelledby="quota-title"><h2 id="quota-title">When a provider runs out of usage</h2><p>Applies when Codex or Claude Code reaches its session or weekly limit.</p>'+
    '<form id="quota-form" novalidate>'+field('quota_policy','Policy',settings?.quota_policy||'ask','select',{options:QUOTA_POLICIES,help:'“Ask me” creates a decision; “Wait for reset” resumes by itself when the limit resets.'})+
    '<div class="field"><span class="label-like" id="fallback-label">Fallback models, in order</span><div id="fallback-slot" role="group" aria-labelledby="fallback-label">'+fallbackListMarkup(items,catalog,esc)+'</div><span class="help">Used by “Switch to fallback automatically” and by the decision card. Models come from the connected catalog.</span></div>'+
    '<div class="row-actions"><button type="submit" class="button primary">Save quota policy</button></div><div class="form-error" id="quota-error" role="alert"></div></form></section>';
}

export function storageMarkup(state,field,esc,button) {
  if(!state) return '<section class="panel settings-panel storage-panel" aria-busy="true"><h2>Storage</h2><p class="muted">Checking disk usage…</p></section>';
  const s=state.settings||{},cats=state.categories||[],low=Number.isFinite(state.free_bytes)&&s.min_free_gb>0&&state.free_bytes<s.min_free_gb*1073741824;
  const last=state.last_cleanup;
  return '<section class="panel settings-panel storage-panel" id="storage-panel" aria-labelledby="storage-title"><h2 id="storage-title">Storage</h2><p>What AgentFlow keeps on this machine, and what it may clean up.</p>'+
    '<div class="storage-summary"><div><small>AgentFlow uses</small><strong>'+esc(formatBytes(state.total_bytes))+'</strong></div><div class="'+(low?'is-low':'')+'"><small>Free disk</small><strong>'+esc(formatBytes(state.free_bytes))+'</strong>'+(state.disk_total_bytes?'<span>of '+esc(formatBytes(state.disk_total_bytes))+'</span>':'')+'</div><div><small>Last cleanup</small><strong>'+esc(last?formatBytes(last.freed_bytes)+' freed':'Never')+'</strong>'+(last?'<span>'+esc(fmtDate(last.at))+(last.files!=null?' · '+esc(last.files)+' files':'')+'</span>':'')+'</div></div>'+
    '<div class="table-scroll"><table class="storage-table"><thead><tr><th scope="col"><span class="sr-only">Select</span></th><th scope="col">Category</th><th scope="col">Size</th><th scope="col">Cleanup</th></tr></thead><tbody>'+
    (cats.length?cats.map(c=>'<tr><td><input type="checkbox" name="storage_category" value="'+esc(c.id)+'" aria-label="Select '+esc(c.label)+'"'+(c.bytes>0?'':' disabled')+'></td><td><strong>'+esc(c.label)+'</strong>'+(c.description?'<small>'+esc(c.description)+'</small>':'')+'</td><td class="num">'+esc(formatBytes(c.bytes))+(c.files!=null?'<small>'+esc(c.files)+' files</small>':'')+'</td><td><span class="storage-badge '+(c.auto?'auto':'ask')+'">'+(c.auto?'Auto':'Ask first')+'</span></td></tr>').join(''):'<tr><td colspan="4" class="muted">Nothing to clean yet.</td></tr>')+
    '</tbody></table></div><div class="row-actions" style="margin:12px 0 16px">'+button('Clean selected','storage-clean','','primary small')+button('Refresh','storage-refresh','','quiet small')+'</div>'+
    '<form id="housekeeping-form" novalidate><h3 class="form-subheading">Automatic cleanup</h3><label class="checkbox"><input type="checkbox" name="auto_cleanup"'+(s.auto_cleanup?' checked':'')+'>Clean “Auto” categories on its own</label>'+
    '<div class="field-grid">'+field('min_free_gb','Keep at least this much free (GB)',s.min_free_gb??5,'number',{min:0,help:'Below this, AgentFlow cleans automatic categories and asks about the rest.'})+field('retention_days','Keep generated files for (days)',s.retention_days??14,'number',{min:1,help:'Older cache, logs and finished-run files become eligible.'})+'</div>'+
    '<div class="row-actions"><button type="submit" class="button primary small">Save cleanup settings</button></div><div class="form-error" id="housekeeping-error" role="alert"></div></form></section>';
}

export function createSettingsPanels(h) {
  const {esc,button,field,api,refresh,toast,openModal,closeModal}=h;
  const data=()=>h.getData();
  let draft=null,storage=null,storageState='idle',storageAt=0,loading=false,modelHistory=null,modelOpen=false;
  const agentNames=()=>Object.fromEntries((data().agents||[]).map(a=>[a.id,a.name]));
  const modelSlot=()=>$('#model-history-slot');
  const catalog=()=>data().runtime?.models||[];
  const fallback=()=>draft||(draft=(data().settings?.fallback_models||[]).filter(m=>m&&m.provider&&m.model).map(m=>({provider:m.provider,model:m.model})));
  const $=s=>document.querySelector(s);
  const slot=()=>$('#storage-slot');
  const storageHtml=()=>storageState==='hidden'?'':storageMarkup(storage,field,esc,button);
  const paintFallback=()=>{const el=$('#fallback-slot');if(el)el.innerHTML=fallbackListMarkup(fallback(),catalog(),esc);};

  async function loadModels() {
    if(h.getMode()==='demo') return;
    try {modelHistory=await api('/models/history?limit=50',undefined,'GET');}
    catch {modelHistory={events:[],diverged:[]};}
    const el=modelSlot();
    if(el&&!el.contains(document.activeElement)) {
      modelOpen=el.querySelector('details')?.open??modelOpen;
      el.innerHTML=modelHistoryMarkup(modelHistory,agentNames(),esc);
      if(modelOpen) el.querySelector('details')?.setAttribute('open','');
    }
  }
  function view() {
    return '<div class="settings-stack">'+modelAutonomyPanel(data().settings,modelHistory,agentNames(),esc,button,h.getMode()!=='demo')+budgetPanel(data().settings,field,esc)+quotaPanel(data().settings,fallback(),catalog(),field,esc)+skillSourcesPanel(data().settings,field,esc)+'<div id="storage-slot">'+storageHtml()+'</div></div>';
  }
  async function loadStorage(force=false) {
    if(h.getMode()==='demo'){storageState='hidden';if(slot())slot().innerHTML='';return;}
    if(loading||storageState==='hidden'&&!force) return;
    if(!force&&storage&&Date.now()-storageAt<20000) return;
    loading=true;
    try {
      storage=await api('/storage',undefined,'GET');storageState='ready';storageAt=Date.now();
    } catch {
      // Backend without /api/storage (older runtime): hide the section instead of showing a broken panel.
      storage=null;storageState='hidden';
    } finally {loading=false;}
    const el=slot();
    if(el&&!el.contains(document.activeElement)) {
      const checked=[...el.querySelectorAll('input[name=storage_category]:checked')].map(i=>i.value);
      el.innerHTML=storageHtml();
      el.querySelectorAll('input[name=storage_category]').forEach(i=>{i.checked=checked.includes(i.value);});
    }
  }
  /** Called by app.js after the settings route renders. */
  function mounted() {loadModels();if(storageState==='idle'||storageState==='ready') loadStorage();}

  async function saveSettings(patch,errorId,form,message) {
    const error=$('#'+errorId);if(error)error.textContent='';
    const submit=form.querySelector('[type=submit]');if(submit?.disabled)return true;
    if(submit){submit.disabled=true;submit.setAttribute('aria-busy','true');}
    try {
      await api('/settings',patch);await refresh(false);draft=null;
      if(h.getRoute()==='settings') h.render();
      toast(message);
    } catch(e) {const box=$('#'+errorId)||error;if(box){box.textContent=e.message;}else toast(e.message,true);}
    finally {if(submit?.isConnected){submit.disabled=false;submit.removeAttribute('aria-busy');}}
    return true;
  }
  async function submit(form) {
    if(form.id==='budget-form') {
      const values=Object.fromEntries(new FormData(form));
      try {const budget=budgetFromValues(values);return await saveSettings({budget},'budget-error',form,'Budget saved.');}
      catch(e){$('#budget-error').textContent=e.message;return true;}
    }
    if(form.id==='quota-form') {
      const values=Object.fromEntries(new FormData(form)),items=fallback();
      if(values.quota_policy==='auto_fallback'&&!items.length){$('#quota-error').textContent='Add at least one fallback model, or choose another policy.';return true;}
      return saveSettings({quota_policy:values.quota_policy,fallback_models:items.map(m=>({provider:m.provider,model:m.model}))},'quota-error',form,'Quota policy saved.');
    }
    if(form.id==='model-autonomy-form') {
      const value=new FormData(form).get('model_autonomy');
      return saveSettings({model_autonomy:value},'model-autonomy-error',form,'Model autonomy saved.');
    }
    if(form.id==='skill-sources-form') {
      const values=Object.fromEntries(new FormData(form));
      try {const skill_sources=skillSourcesFromValues(data().settings?.skill_sources,values);return await saveSettings({skill_sources},'skill-sources-error',form,'Skill source added.');}
      catch(e){$('#skill-sources-error').textContent=e.message;return true;}
    }
    if(form.id==='housekeeping-form') {
      const values=Object.fromEntries(new FormData(form));values.auto_cleanup=new FormData(form).has('auto_cleanup');
      try {
        const housekeeping=housekeepingFromValues(values);
        const error=$('#housekeeping-error');if(error)error.textContent='';
        await api('/settings',{housekeeping});toast('Cleanup settings saved.');await loadStorage(true);
      } catch(e){$('#housekeeping-error').textContent=e.message;}
      return true;
    }
    return false;
  }
  async function perform(action,id,target) {
    if(action==='fb-add') {
      const pick=$('#fallback-pick');if(!pick?.value) return true;
      const [provider,model]=JSON.parse(pick.value);fallback().push({provider,model});paintFallback();return true;
    }
    if(action==='fb-remove'){fallback().splice(Number(id),1);paintFallback();return true;}
    if(action==='fb-up'||action==='fb-down') {
      const items=fallback(),i=Number(id),j=i+(action==='fb-up'?-1:1);
      if(items[j]){[items[i],items[j]]=[items[j],items[i]];paintFallback();$('#fallback-slot [data-action="'+action+'"][data-id="'+j+'"]')?.focus();}
      return true;
    }
    if(action==='model-reset') {
      target.disabled=true;
      try {await api('/agents/'+id+'/model',{inherit:true});await refresh(false);await loadModels();toast('Reset to the team default.');}
      catch(e) {toast(e.message,true);target.disabled=false;}
      return true;
    }
    if(action==='skill-source-remove') {
      const skill_sources=(data().settings?.skill_sources||[]).filter(s=>s.id!==id).map(s=>({id:s.id,label:s.label,url:s.url}));
      try {await api('/settings',{skill_sources});await refresh(false);if(h.getRoute()==='settings')h.render();toast('Skill source removed.');}
      catch(e) {toast(e.message,true);}
      return true;
    }
    if(action==='budget-reset') {
      for(const [key] of BUDGET_FIELDS) {const input=$('#f-'+key);if(input)input.value=DEFAULT_BUDGET[key];}
      const tokens=$('#f-project_tokens_k');if(tokens)tokens.value=DEFAULT_BUDGET.project_tokens/1000;
      return true;
    }
    if(action==='storage-refresh'){target.disabled=true;await loadStorage(true);return true;}
    if(action==='storage-clean') {
      const ids=[...document.querySelectorAll('#storage-panel input[name=storage_category]:checked')].map(i=>i.value);
      if(!ids.length){toast('Select at least one category to clean.',true);return true;}
      const rows=ids.map(cid=>(storage?.categories||[]).find(c=>c.id===cid)).filter(Boolean),total=rows.reduce((sum,c)=>sum+(c.bytes||0),0);
      openModal('Clean selected storage?','Deleted files cannot be restored. Active work and your own project folders are never touched.',
        '<ul class="storage-confirm">'+rows.map(c=>'<li><span>'+esc(c.label)+'</span><strong>'+esc(formatBytes(c.bytes))+'</strong></li>').join('')+'</ul><p class="storage-confirm-total">Up to <strong>'+esc(formatBytes(total))+'</strong> will be freed.</p>',
        button('Cancel','modal-close')+button('Clean now','storage-clean-confirm',ids.join(','),'primary'));
      document.querySelector('#modal .modal-footer [data-action="storage-clean-confirm"]')?.focus();
      return true;
    }
    if(action==='storage-clean-confirm') {
      target.disabled=true;
      try {
        const result=await api('/storage/cleanup',{categories:id.split(',').filter(Boolean)});
        closeModal(true);toast('Freed '+formatBytes(result?.freed_bytes)+(result?.files!=null?' · '+result.files+' files':'')+'.');await loadStorage(true);
      } catch(e) {const box=document.querySelector('#modal-error');if(box)box.textContent=e.message;else toast(e.message,true);target.disabled=false;}
      return true;
    }
    return false;
  }
  return {view,submit,perform,mounted,reload:()=>loadStorage(true)};
}
