// Owner decision cards: compact, highlighted, generic `actions` aware.
// Pure markup helpers so they can be unit-tested without a DOM (tests/decisions.mjs).
const LEGACY={
  safety_request:{field:'safety_action',actions:[{value:'cancel',label:'Cancel work',style:'quiet'},{value:'continue',label:'Continue with a new budget',style:'primary'}]},
  command_request:{field:'command_action',actions:[{value:'cancel',label:'Cancel',style:'quiet'},{value:'allow',label:'Allow',style:'primary'},{value:'session',label:'Allow all commands in this session (15 minutes)',style:'primary'}]}
};
const LONG_TITLE=130;

/** Generic actions first (SPEC: q.actions), otherwise the legacy special cases, otherwise null. */
export function decisionActions(q) {
  if(Array.isArray(q?.actions)&&q.actions.length) {
    const actions=q.actions.filter(a=>a&&a.value!==undefined&&a.value!==null&&String(a.label||'').trim()).map(a=>({value:String(a.value),label:String(a.label),style:['primary','quiet','danger'].includes(a.style)?a.style:'quiet',hint:a.hint?String(a.hint):''}));
    if(actions.length) return {field:q.action_field||'action',actions,generic:true};
  }
  const legacy=Object.keys(LEGACY).find(key=>q?.[key]);
  return legacy?{field:LEGACY[legacy].field,actions:LEGACY[legacy].actions.map(a=>({...a,hint:''})),generic:false}:null;
}

/** Body posted to /decisions/{id}/answer for an action button. */
export function actionPayload(q,value) {
  const spec=decisionActions(q),action=spec?.actions.find(a=>a.value===String(value));
  if(!spec||!action) return null;
  return {answer:action.label,[spec.field]:action.value};
}

export const isLongText=(text,lines=3,chars=240)=>{const t=String(text||'').trim();return t.split('\n').length>lines||t.length>chars;};

export const optionLabel=option=>typeof option==='string'?option:option?.label||option?.text||'';

const buttonClass=style=>style==='primary'?'primary':style==='danger'?'danger':'quiet';
const plainText=text=>String(text||'').replace(/@\{([^}]+)\}/g,'@$1');

/** Permission-like decisions (commands, folders, websites) get a badge, a monospace subject line and no free-text reply. */
export function decisionInfo(q) {
  const permission=!!(q?.command_request||q?.permission_request||q?.browser_origin||q?.kind==='folder_access');
  let subject='',hint='';
  if(q?.command_request||Array.isArray(q?.command)) {
    const parts=Array.isArray(q.command)?q.command:[];
    subject=parts.map(part=>/[\s"']/.test(String(part))?JSON.stringify(String(part)):String(part)).join(' ');
    hint=q.command_cwd?'in '+q.command_cwd:'';
  } else if(q?.browser_origin) {subject=String(q.browser_origin);hint=q.browser_access?(q.browser_access==='interact'?'read & control':'read only'):'';}
  else if(q?.requested_path) {subject=String(q.requested_path);hint=q.requested_access==='readwrite'?'read & write':q.requested_access?'read only':'';}
  return {permission,label:permission?'Permission':'Needs you',subject:subject.replace(/\s*\n\s*/g,' ').trim(),hint};
}

/** What the owner chose/typed, shown as "You: ..." (action label for button decisions). */
export function resolvedAnswer(q) {
  const raw=String(q?.answer||'').trim(),spec=decisionActions(q);
  if(spec) {
    const value=q?.[spec.field],byValue=value!==undefined&&value!==null?spec.actions.find(a=>a.value===String(value)):null;
    if(byValue) return byValue.label;
    const typed=raw.toLowerCase(),index=spec.actions.findIndex((a,i)=>typed===a.label.toLowerCase()||typed===a.value.toLowerCase()||typed===String(i+1));
    if(index>=0) return spec.actions[index].label;
  }
  return raw||'Resolved';
}

/**
 * h = {esc, icon, avatar, field, title(text)->html, context(text)->html, actor:{id,name}, time?(iso)->text,
 *      plain?(text)->text, replyOpen?(id)->bool}
 * Returns the compact open card or the one-line resolved row.
 */
export function decisionCard(q,h) {
  if(!q) return '';
  const {esc,icon}=h,plain=h.plain||plainText;
  if(q.status!=='open') {
    const answer=resolvedAnswer(q),question=plain(q.question),long=question.length>90||!!String(q.context||'').trim();
    const when=h.time&&q.answered_at?h.time(q.answered_at):'';
    const row=icon('check')+'<span class="rd-q">'+esc(question)+'</span><span class="rd-chip" title="You: '+esc(answer)+'">You: '+esc(answer)+'</span>'+(when?'<time class="rd-time">'+esc(when)+'</time>':'');
    // Every answered question/selection stays one line and expands on click: full question, context,
    // who asked, the options offered (chosen one marked) and the owner's answer.
    const choices=(decisionActions(q)?.actions||(q.options||[]).map(o=>typeof o==='string'?{label:o}:{label:o.label||o.text||o.value}))
      .map(o=>String(o.label||'')).filter(Boolean);
    const who=h.actor?.name?'<p class="rd-who">Asked by '+esc(h.actor.name)+(when?' · answered '+esc(when):'')+'</p>':'';
    const body='<p class="rd-full">'+h.title(q.question)+'</p>'+(q.context?'<div class="rd-context">'+h.context(q.context)+'</div>':'')+
      (choices.length?'<ul class="rd-options">'+choices.map(c=>'<li'+(c===answer?' class="chosen"':'')+'>'+esc(c)+'</li>').join('')+'</ul>':'')+
      '<p class="rd-answer"><strong>You:</strong> '+esc(answer)+'</p>'+who;
    void long;
    return '<details class="resolved-decision" data-key="resolved:'+esc(q.id)+'" data-decision-done="'+esc(q.id)+'"><summary title="'+esc(question)+'">'+row+'</summary><div class="rd-body">'+body+'</div></details>';
  }
  const spec=decisionActions(q),info=decisionInfo(q),who=h.actor||{id:q.agent_id,name:'Agent'};
  const longTitle=String(q.question||'').length>LONG_TITLE;
  const head='<div class="dc-head">'+h.avatar(who,'mini')+'<strong>'+esc(who.name)+'</strong><span class="dc-badge '+(info.permission?'perm':'ask')+'">'+info.label+'</span>'+
    (q.blocking?'<span class="dc-dot" role="img" aria-label="Blocking" title="Blocking: the agent resumes when you reply. Other work can continue."></span>':'')+'</div>';
  const question='<p class="dc-q" title="'+esc(plain(q.question))+'">'+h.title(q.question)+'</p>';
  const mono=info.subject?'<code class="dc-mono" title="'+esc(info.subject+(info.hint?'  ('+info.hint+')':''))+'">'+esc(info.subject)+'</code>':'';
  const hasDetails=!!String(q.context||'').trim()||longTitle;
  const details=hasDetails?'<details class="dc-details" data-key="decision:'+esc(q.id)+'"><summary>Details'+icon('down')+'</summary><div class="dc-detail-body">'+(longTitle?'<p class="dc-full">'+h.title(q.question)+'</p>':'')+(q.context?'<div class="decision-context">'+h.context(q.context)+'</div>':'')+'</div></details>':'';
  let controls='';
  const open=h.replyOpen?.(q.id);
  if(spec) {
    controls='<div class="dc-actions">'+spec.actions.map(a=>'<button type="button" class="button small dc-btn '+buttonClass(a.style)+'" data-action="co-decision-action" data-id="'+esc(q.id)+'" data-value="'+esc(a.value)+'"'+(a.hint?' title="'+esc(a.hint)+'" aria-description="'+esc(a.hint)+'"':'')+'>'+esc(a.label)+'</button>').join('')+'</div>';
  } else if(q.kind==='model_config') {
    controls='<div class="dc-actions"><a class="button small dc-btn primary" href="#connections">Choose default model</a></div>';
  } else {
    const options=(q.options||[]).map(optionLabel).filter(Boolean),folder=q.kind==='folder_access'&&h.field;
    const alwaysReply=!options.length||folder;
    controls=(folder?'<div class="decision-folder">'+h.field('folder_path',q.target_type==='file'?'Local project files':'Local project folder',q.requested_path||'','text',{required:true,scope:q.id})+h.field('folder_access','Access',q.requested_access||'read','select',{scope:q.id,options:[['read','Read documents and source code'],['readwrite','Read & write']]})+'</div>':'')+
      '<div class="dc-actions">'+options.map((label,i)=>'<button type="button" class="button small dc-btn dc-option '+(i===0&&options.length<=2?'primary':'quiet')+'" data-action="co-answer" data-id="'+esc(q.id)+'" data-answer="'+esc(label)+'">'+esc(label)+'</button>').join('')+
      (folder?'<button type="submit" class="button small dc-btn primary">Grant access</button>':'')+
      (alwaysReply?'':'<button type="button" class="dc-link" data-action="co-decision-reply" data-id="'+esc(q.id)+'" aria-expanded="'+(open?'true':'false')+'" aria-controls="dc-reply-'+esc(q.id)+'">Reply…</button>')+'</div>'+
      '<div class="dc-reply" id="dc-reply-'+esc(q.id)+'"'+(alwaysReply||open?'':' hidden')+'><input name="answer" aria-label="Answer for '+esc(plain(q.question))+'" placeholder="'+(options.length?'Type a reply…':'Your answer…')+'" maxlength="20000" autocomplete="off">'+(folder?'':'<button type="submit" class="icon-button" aria-label="Send decision">'+icon('send')+'</button>')+'</div>';
  }
  return '<form class="decision-card dc'+(q.blocking?' is-blocking':'')+(info.permission?' is-permission':'')+'" data-decision="'+esc(q.id)+'" aria-label="'+esc(info.label)+': decision needed">'+head+question+mono+details+controls+'<div class="inline-error" role="alert"></div></form>';
}

/**
 * Collapses consecutive open-decision items into grouped containers.
 * items: [{html, open:boolean, id?}] (non-open items break the run; empty html is skipped).
 * Returns html. Runs of 2+ open items become one container, first `limit` visible, the rest hidden behind "Show N more".
 */
export function groupDecisions(items,opts={}) {
  const limit=opts.limit||3,expanded=opts.expanded||(()=>false),esc=opts.esc||String;
  const out=[];let run=[];
  const flush=()=>{
    if(!run.length) return;
    if(run.length===1) out.push(run[0].html);
    else {
      const key=run[0].id||'group',hidden=run.length-limit,open=expanded(key);
      out.push('<div class="decision-group" role="group" aria-label="'+run.length+' decisions need you" data-group="'+esc(key)+'">'+
        run.map((item,i)=>i<limit||open?item.html:item.html.replace(/^<form /,'<form hidden ')).join('')+
        (hidden>0?'<div class="decision-group-foot"><button type="button" class="dc-link" data-action="co-decision-more" data-id="'+esc(key)+'" aria-expanded="'+(open?'true':'false')+'">'+(open?'Show fewer':'Show '+hidden+' more')+'</button></div>':'')+'</div>');
    }
    run=[];
  };
  for(const item of items) {
    if(!item||!item.html) continue;
    if(item.open) run.push(item);else {flush();out.push(item.html);}
  }
  flush();
  return out.join('');
}

export const decisionBarLabel=count=>count+(count===1?' decision needs':' decisions need')+' you';

/** Inner markup for the sticky pending-decisions bar. Empty string when nothing is open. */
export function decisionBar(count,index,icon,esc=String) {
  if(!count) return '';
  const position=index>=0?Math.min(index,count-1)+1:0;
  return '<span class="decision-bar-label">'+icon('flag')+'<strong>'+esc(decisionBarLabel(count))+'</strong></span>'+
    '<span class="decision-bar-nav">'+(count>1?'<button type="button" class="button small quiet" data-action="co-decision-prev" aria-label="Previous decision">Prev</button>'+(position?'<span class="decision-bar-pos" aria-live="polite">'+position+' / '+count+'</span>':''):'')+
    '<button type="button" class="button small primary" data-action="co-decision-next" aria-label="'+(count>1?'Next decision':'Go to decision')+'">'+(count>1?'Next':'Review')+'</button></span>';
}

/** Index arithmetic for Next/Prev (wraps). */
export const stepIndex=(index,count,delta)=>count?((index+delta)%count+count)%count:0;
