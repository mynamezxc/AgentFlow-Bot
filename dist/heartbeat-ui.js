// Team heartbeat: live status card on Automations + the Customize dialog.
// Everything except createHeartbeat() is pure (see tests/heartbeat.mjs); createHeartbeat wires it to the app helpers.
export const DEFAULT_PULSE_PROMPT=`Team pulse. Review the digest below and keep the team moving. Unblock or reassign stalled or blocked work with the tasks and team tools (tasks.inspect, tasks.delegate, team.message); do not redo teammates' work and do not start new projects. Message the owner only when a decision or an important result needs them (chat.send with audience owner, or decisions.ask). Otherwise finish silently with a short English summary in \`summary\`.`;
export const DEFAULTS={
  interval_seconds:30,
  active_hours:{enabled:false,start:'08:00',end:'22:00'},
  active_days:[1,2,3,4,5,6,7],
  pause_schedules_outside_hours:true,
  pulse:{enabled:false,agent_id:'a-lead',every_minutes:60,only_if_changed:true,notify:'needed',prompt:DEFAULT_PULSE_PROMPT}
};
export const INTERVALS=[[10,'10 s'],[15,'15 s'],[30,'30 s'],[60,'1 min'],[120,'2 min'],[300,'5 min']];
export const PULSE_EVERY=[[15,'15 min'],[30,'30 min'],[60,'1 h'],[120,'2 h'],[240,'4 h'],[1440,'Daily']];
export const WEEKDAYS=[[1,'Mon','Monday'],[2,'Tue','Tuesday'],[3,'Wed','Wednesday'],[4,'Thu','Thursday'],[5,'Fri','Friday'],[6,'Sat','Saturday'],[7,'Sun','Sunday']];
export const LIMITS={max_parallel:[1,8],turns_per_slice:[1,24]};
const TIME=/^([01]\d|2[0-3]):[0-5]\d$/;

/** Effective heartbeat configuration: stored values over defaults (the runtime sends the merged object; the preview does not). */
export function heartbeatConfig(raw) {
  const r=raw&&typeof raw==='object'?raw:{};
  const days=Array.isArray(r.active_days)&&r.active_days.length?[...new Set(r.active_days.map(Number))].filter(d=>d>=1&&d<=7).sort():DEFAULTS.active_days;
  return {
    interval_seconds:Number.isInteger(r.interval_seconds)?r.interval_seconds:DEFAULTS.interval_seconds,
    active_hours:{...DEFAULTS.active_hours,...(r.active_hours||{})},
    active_days:days.length?days:DEFAULTS.active_days,
    pause_schedules_outside_hours:r.pause_schedules_outside_hours!==false,
    pulse:{...DEFAULTS.pulse,...(r.pulse||{})}
  };
}

const parse=at=>{if(at==null||at==='')return null;const n=typeof at==='number'?(at<1e12?at*1000:at):Date.parse(at);return Number.isFinite(n)?n:null;};
const plural=(n,one,many)=>n+' '+(n===1?one:many);

/** "12 s ago" / "3 min ago" / "2 h ago"; '—' when the time is unknown. */
export function formatAgo(at,nowMs=Date.now()) {
  const t=parse(at);if(t==null) return '—';
  const s=Math.max(0,Math.round((nowMs-t)/1000));
  if(s<5) return 'just now';
  if(s<60) return s+' s ago';
  if(s<3600) return Math.floor(s/60)+' min ago';
  if(s<86400) return Math.floor(s/3600)+' h ago';
  return Math.floor(s/86400)+' d ago';
}
/** "in 40 s" / "in 5 min" / "in 1 h 20 min"; "due now" when the time has passed. */
export function formatUntil(at,nowMs=Date.now()) {
  const t=parse(at);if(t==null) return '—';
  const s=Math.round((t-nowMs)/1000);
  if(s<=0) return 'due now';
  if(s<60) return 'in '+s+' s';
  const m=Math.round(s/60);
  if(m<60) return 'in '+m+' min';
  const h=Math.floor(m/60),rest=m%60;
  if(h<24) return 'in '+h+' h'+(rest?' '+rest+' min':'');
  return 'in '+Math.floor(h/24)+' d '+(h%24)+' h';
}
export const formatInterval=seconds=>seconds<60?seconds+' s':seconds%60===0?(seconds/60)+' min':seconds+' s';
export const formatEvery=minutes=>minutes===1440?'day':minutes<60?minutes+' min':minutes%60===0?(minutes/60)+' h':minutes+' min';

/** Compact weekday summary: "Every day", "Mon–Fri", "Mon, Wed, Sat". */
export function daysSummary(days) {
  const d=[...new Set(days||[])].sort((a,b)=>a-b),name=n=>WEEKDAYS[n-1][1];
  if(d.length===7) return 'Every day';
  if(!d.length) return 'No days';
  const runs=[];
  for(const n of d) {const last=runs.at(-1);if(last&&last[1]===n-1) last[1]=n;else runs.push([n,n]);}
  return runs.map(([a,b])=>a===b?name(a):b===a+1?name(a)+', '+name(b):name(a)+'–'+name(b)).join(', ');
}
/** "08:00–22:00 GMT+7 · Mon–Fri", "22:00–06:00 GMT+7 (overnight)" or "Always active". */
export function windowSummary(config) {
  const h=config.active_hours;
  if(!h.enabled) return 'Always active';
  const whole=h.start===h.end,overnight=!whole&&h.end<h.start;
  return (whole?'All day':h.start+'–'+h.end+' GMT+7'+(overnight?' (overnight)':''))+' · '+daysSummary(config.active_days);
}
const WINDOW_FORMAT=typeof Intl!=='undefined'?new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Ho_Chi_Minh',weekday:'short',hour:'2-digit',minute:'2-digit',hour12:false}):null;
export const formatWindowOpen=at=>{const t=parse(at);return t==null||!WINDOW_FORMAT?'':WINDOW_FORMAT.format(new Date(t)).replace(',','')+' GMT+7';};

/** A beat older than three intervals (min 90 s) means the loop is not running (for example --no-scheduler). */
export function beatIsStale(status,nowMs=Date.now()) {
  const t=parse(status?.last_heartbeat_at);
  if(t==null) return true;
  return nowMs-t>Math.max(90000,3*1000*(status.interval_seconds||30));
}
/** Status dot: running / paused / quiet hours / offline. */
export function heartbeatState(status,nowMs=Date.now()) {
  if(!status) return {key:'preview',label:'Preview'};
  if(!status.execution_enabled) return {key:'paused',label:'Paused'};
  if(beatIsStale(status,nowMs)) return {key:'offline',label:'Not beating'};
  if(status.quiet_hours) return {key:'quiet',label:'Quiet hours'};
  return {key:'running',label:'Running'};
}
const REASONS={unchanged:'nothing changed',nothing_open:'nothing open',agent_unavailable:'agent unavailable',running:'previous pulse still running'};
/** One line for the pulse tile: "Off", "Next pulse in 12 min", "Waiting for active hours"... */
export function pulseNext(status,nowMs=Date.now()) {
  const p=status?.pulse;
  if(!p||!p.enabled) return 'Off';
  if(status.quiet_hours) return 'Resumes '+(formatWindowOpen(status.next_window_at)||'in the next window');
  return p.next_pulse_at?'Next '+formatUntil(p.next_pulse_at,nowMs):'Starting on the next beat';
}
export function pulseLast(status,nowMs=Date.now()) {
  const p=status?.pulse;
  if(!p||!p.last_pulse_at) return p?.enabled?'No pulse yet':'';
  const reason=p.last_result==='skipped'?' ('+(REASONS[p.last_reason]||p.last_reason||'skipped')+')':'';
  const label={started:'started',running:'running',done:'finished',failed:'failed',cancelled:'cancelled',skipped:'skipped'}[p.last_result]||p.last_result||'';
  return 'Last pulse '+label+reason+' · '+formatAgo(p.last_pulse_at,nowMs);
}
/** Toast for POST /heartbeat/pulse-now. */
export function pulseMessage(result) {
  if(result?.started) return 'Pulse started. Your lead agent is reviewing the digest.';
  return {
    nothing_open:'Nothing needs attention, so no model was called.',
    running:'A pulse is already running. Wait for it to finish.',
    agent_unavailable:'The pulse agent is paused or archived. Choose another agent in Customize.',
    unchanged:'Nothing changed since the last pulse, so no model was called.'
  }[result?.reason]||'No pulse was started.';
}

/** Rough ceiling on pulse runs per day for the token note. */
export function pulsesPerDay(values) {
  const every=Number(values.pulse_every);if(!(every>0)) return 0;
  let minutes=1440;
  if(values.hours_enabled) {
    const [sh,sm]=String(values.hours_start).split(':').map(Number),[eh,em]=String(values.hours_end).split(':').map(Number);
    const s=sh*60+sm,e=eh*60+em;
    minutes=s===e?1440:e>s?e-s:1440-s+e;
  }
  return Math.max(1,Math.floor(minutes/every));
}
export function tokenNote(values) {
  const base='The heartbeat itself never calls a model. A run starts only when a schedule is due or the team pulse finds open work';
  if(!values.pulse_enabled) return base+'. The team pulse is off.';
  const n=pulsesPerDay(values);
  return base+(values.pulse_changed?' that changed since the last pulse':'')+'. At most '+plural(n,'pulse run','pulse runs')+' per day with these settings.';
}

const num=(value,label,[min,max])=>{
  const n=Number(value);
  if(value===''||value==null||!Number.isInteger(n)||n<min||n>max) throw new Error(label+' must be a whole number from '+min+' to '+max+'.');
  return n;
};
/**
 * Form values -> POST /settings body. `values` uses the dialog field names (see customizeBody).
 * Throws a readable Error for anything the runtime would reject; opts.omitExecution leaves execution_enabled out.
 */
export function heartbeatPayload(values,opts={}) {
  const interval=num(values.interval_seconds,'The heartbeat interval',[10,300]);
  const every=num(values.pulse_every,'The pulse interval',[15,1440]);
  if(!TIME.test(String(values.hours_start))||!TIME.test(String(values.hours_end))) throw new Error('Enter the active hours as HH:MM.');
  const days=[...new Set((values.days||[]).map(Number))].filter(d=>d>=1&&d<=7).sort();
  if(values.hours_enabled&&!days.length) throw new Error('Choose at least one active weekday.');
  const prompt=String(values.pulse_prompt||'').trim();
  if(!prompt) throw new Error('The pulse prompt cannot be empty. Use “Reset to default” to restore it.');
  if(prompt.length>4000) throw new Error('The pulse prompt is limited to 4,000 characters.');
  if(!['needed','always'].includes(values.pulse_notify)) throw new Error('Choose when the pulse should message you.');
  const body={
    heartbeat:{
      interval_seconds:interval,
      active_hours:{enabled:!!values.hours_enabled,start:values.hours_start,end:values.hours_end},
      active_days:days.length?days:DEFAULTS.active_days,
      pause_schedules_outside_hours:!!values.pause_outside,
      pulse:{enabled:!!values.pulse_enabled,agent_id:values.pulse_agent||'a-lead',every_minutes:every,only_if_changed:!!values.pulse_changed,notify:values.pulse_notify,prompt}
    },
    max_parallel:num(values.max_parallel,'Max concurrent agents',LIMITS.max_parallel),
    turns_per_slice:num(values.turns_per_slice,'Steps per slice',LIMITS.turns_per_slice)
  };
  if(!opts.omitExecution) body.execution_enabled=!!values.execution_enabled;
  return body;
}

// ---- markup ------------------------------------------------------------------------------------------------------
const group=(id,title,hint,inner,extra='')=>'<fieldset class="hb-section" id="hb-'+id+'"'+extra+'><legend>'+title+'</legend>'+(hint?'<p class="hb-hint">'+hint+'</p>':'')+inner+'</fieldset>';
const toggle=(name,label,checked,help='',id=name)=>'<label class="hb-switch"><input type="checkbox" role="switch" name="'+name+'" id="hb-'+id+'"'+(checked?' checked':'')+'><span class="hb-track" aria-hidden="true"></span><span class="hb-switch-text"><strong>'+label+'</strong>'+(help?'<small>'+help+'</small>':'')+'</span></label>';
const chips=(name,label,options,selected,esc)=>'<div class="hb-chips" role="radiogroup" aria-label="'+esc(label)+'">'+options.map(([value,text])=>'<label class="hb-chip"><input type="radio" name="'+name+'" value="'+esc(value)+'"'+(String(value)===String(selected)?' checked':'')+'><span>'+esc(text)+'</span></label>').join('')+'</div>';
const stepper=(name,label,value,[min,max],esc,help='')=>'<div class="hb-row"><div class="hb-row-text"><label for="hb-'+name+'">'+esc(label)+'</label>'+(help?'<small>'+esc(help)+'</small>':'')+'</div><div class="hb-stepper" role="group" aria-label="'+esc(label)+'"><button type="button" class="hb-step" data-action="hb-step" data-id="'+name+':-1" aria-label="Decrease '+esc(label)+'">−</button><input type="number" id="hb-'+name+'" name="'+name+'" value="'+esc(value)+'" min="'+min+'" max="'+max+'" inputmode="numeric"><button type="button" class="hb-step" data-action="hb-step" data-id="'+name+':1" aria-label="Increase '+esc(label)+'">+</button></div></div>';

/** Body of the Customize dialog. `agents` is [{id,name,role,heartbeat}], `settings` the workspace settings. */
export function customizeBody({config,settings,agents,esc,field}) {
  const c=heartbeatConfig(config),p=c.pulse,h=c.active_hours;
  const exec=settings?.execution_enabled!==false;
  const maxParallel=Number.isInteger(settings?.max_parallel)?settings.max_parallel:4;
  const turns=Number.isInteger(settings?.turns_per_slice)?settings.turns_per_slice:6;
  const live={interval_seconds:c.interval_seconds,hours_enabled:h.enabled,hours_start:h.start,hours_end:h.end,pulse_enabled:p.enabled,pulse_every:p.every_minutes,pulse_changed:p.only_if_changed};
  const agentSelect=field('pulse_agent','Agent that runs the pulse',p.agent_id,'select',{options:agents.map(a=>[a.id,a.name+' · '+a.role]),help:'Usually the lead. It reviews the digest and delegates through tasks and team tools.'});
  return '<div class="hb-form">'+
    group('execution','Execution','Controls how much work runs at the same time.',
      toggle('execution_enabled','Run team work',exec,'When off, nothing new starts: schedules, tasks and the team pulse wait.')+
      stepper('max_parallel','Max concurrent agents',maxParallel,LIMITS.max_parallel,esc,'1–8. Each running agent uses its own model session.')+
      stepper('turns_per_slice','Steps per slice',turns,LIMITS.turns_per_slice,esc,'1–24. More steps = fewer pauses, more tokens per slice.'))+
    group('beat','Heartbeat','How often the runtime looks for due work. This check is free: it makes no model call.',
      chips('interval_seconds','Heartbeat interval',INTERVALS,c.interval_seconds,esc))+
    group('hours','Active hours','Times use GMT+7. An end time before the start time means overnight. Chats you start and work already running are never paused.',
      toggle('hours_enabled','Only work during active hours',h.enabled,'',"hours_enabled")+
      '<div class="hb-times"><div class="field"><label for="f-hours_start">From</label><input type="time" id="f-hours_start" name="hours_start" value="'+esc(h.start)+'" required></div><div class="field"><label for="f-hours_end">Until</label><input type="time" id="f-hours_end" name="hours_end" value="'+esc(h.end)+'" required></div></div>'+
      '<div class="hb-days" role="group" aria-label="Active weekdays">'+WEEKDAYS.map(([n,short,long])=>'<label class="hb-chip hb-day" title="'+long+'"><input type="checkbox" name="days" value="'+n+'"'+(c.active_days.includes(n)?' checked':'')+' aria-label="'+long+'"><span>'+short+'</span></label>').join('')+'</div>'+
      toggle('pause_outside','Pause scheduled work outside these hours',c.pause_schedules_outside_hours,'Missed schedules run once when the window opens, not as a backlog.'),
      ' data-off-when="hours_enabled"')+
    group('pulse','Team pulse','A light check-in that costs tokens only when something changed.',
      toggle('pulse_enabled','Run a team pulse',p.enabled,'Builds a digest of open, blocked and stalled work without a model call.')+
      '<div data-pulse-fields>'+agentSelect+
      '<div class="field"><span class="label-like" id="hb-every-label">Pulse every</span>'+chips('pulse_every','Pulse every',PULSE_EVERY,p.every_minutes,esc)+'</div>'+
      toggle('pulse_changed','Only when something changed (saves tokens)',p.only_if_changed,'Skips the model call when the digest matches the last pulse or nothing is open.')+
      '<div class="field"><span class="label-like" id="hb-notify-label">Message me</span><div class="hb-radios" role="radiogroup" aria-labelledby="hb-notify-label">'+
        '<label class="hb-radio"><input type="radio" name="pulse_notify" value="needed"'+(p.notify==='needed'?' checked':'')+'><span><strong>Only when needed</strong><small>A decision or important result. Otherwise it finishes silently.</small></span></label>'+
        '<label class="hb-radio"><input type="radio" name="pulse_notify" value="always"'+(p.notify==='always'?' checked':'')+'><span><strong>Always</strong><small>Every pulse ends with a short status for you.</small></span></label></div></div>'+
      '<div class="field"><label for="f-pulse_prompt">Pulse prompt</label><textarea id="f-pulse_prompt" name="pulse_prompt" rows="6" aria-describedby="hb-prompt-help">'+esc(p.prompt)+'</textarea><span class="help" id="hb-prompt-help">Agent-facing instructions in English. The digest and your message setting are added automatically.</span>'+
      '<button type="button" class="button quiet small hb-reset" data-action="hb-prompt-reset">Reset to default</button></div></div>')+
    group('agents','Agents taking part','Agents that are switched off do not start scheduled work.',
      '<ul class="hb-agents">'+agents.map(a=>'<li><label class="hb-switch hb-agent"><input type="checkbox" role="switch" name="agent_hb" value="'+esc(a.id)+'"'+(a.heartbeat?' checked':'')+'><span class="hb-track" aria-hidden="true"></span><span class="hb-switch-text"><strong>'+esc(a.name)+'</strong><small>'+esc(a.role||'Agent')+'</small></span></label></li>').join('')+'</ul>')+
    '<p class="hb-token" id="hb-token" role="status"><strong>Tokens.</strong> <span id="hb-token-text">'+esc(tokenNote({...live,pulse_every:p.every_minutes}))+'</span></p></div>';
}

/** Live card for Automations. `status` is runtime.heartbeat (null in the preview). */
export function heartbeatCard({status,config,settings,mode,esc,icon,button,nowMs=Date.now()}) {
  const c=heartbeatConfig(config),state=heartbeatState(status,nowMs),exec=settings?.execution_enabled!==false;
  const maxParallel=settings?.max_parallel??4,turns=settings?.turns_per_slice??6;
  const beat=status?formatAgo(status.last_heartbeat_at,nowMs):'—';
  const stat=(value,label,attrs='')=>'<div class="hb-stat"><dt>'+label+'</dt><dd'+attrs+'>'+value+'</dd></div>';
  const counter=(n,label,warn)=>'<li class="'+(warn&&n?'warn':'')+'"><strong>'+(status?n:'—')+'</strong><span>'+label+'</span></li>';
  let note='';
  if(mode==='demo') note='<p class="hb-note">Preview: keep the local app running for schedules and the pulse to work after you close the browser.</p>';
  else if(state.key==='paused') note='<p class="hb-note">Execution is paused. Nothing new starts until you turn “Run team work” back on in Customize.</p>';
  else if(state.key==='offline') note='<p class="hb-note">No recent beat. The runtime loop is not running; restart AgentFlow to resume scheduled work.</p>';
  else if(state.key==='quiet') note='<p class="hb-note">Quiet hours · '+(c.pause_schedules_outside_hours?'Scheduled work and the pulse resume ':'The pulse resumes ')+esc(formatWindowOpen(status.next_window_at)||'when the next window opens')+'. Chats and running work continue.</p>';
  else if(c.active_hours.enabled) note='<p class="hb-note">Active '+esc(windowSummary(c))+'.</p>';
  const last=pulseLast(status,nowMs),p=status?.pulse;
  const liveNext=p?.enabled&&p.next_pulse_at&&!status.quiet_hours;
  const lock=!exec||mode==='demo'?' disabled':'';
  return '<section class="panel hb-card" aria-labelledby="hb-title"><div class="hb-head"><span class="activity-mark">'+icon('pulse')+'</span>'+
    '<div class="grow"><h3 id="hb-title">Team heartbeat <span class="hb-state '+state.key+'"><span class="hb-dot" aria-hidden="true"></span>'+esc(state.label)+'</span></h3>'+
    '<p class="hb-sub">Checks for due work every '+esc(formatInterval(c.interval_seconds))+(status?' · last beat <span data-hb-time="'+esc(status.last_heartbeat_at||'')+'" data-hb-kind="ago">'+esc(beat)+'</span>':'')+'</p></div>'+
    '<div class="row-actions hb-actions"><button type="button" class="button small" data-action="hb-pulse" title="Run the team pulse now"'+lock+'>'+icon('spark')+'Pulse now</button>'+button('Customize','hb-customize','','primary small','settings')+'</div></div>'+
    '<dl class="hb-stats">'+stat(maxParallel,'Concurrent agents')+stat(turns,'Steps per slice')+stat(esc(formatInterval(c.interval_seconds)),'Heartbeat')+
      stat(esc(pulseNext(status,nowMs)),'Team pulse',liveNext?' data-hb-time="'+esc(p.next_pulse_at)+'" data-hb-kind="next"':'')+'</dl>'+
    '<ul class="hb-counters" aria-label="Current work">'+counter(status?.running,'Running')+counter(status?.queued,'Queued')+counter(status?.waiting_owner,'Waiting for you',true)+counter(status?.waiting_quota,'Waiting for quota',true)+'</ul>'+
    (last?'<p class="hb-last">'+esc(last)+'</p>':'')+note+'</section>';
}

/** Cheap signature of what the card shows, minus clocks (those update in place). */
export function heartbeatSignature(runtime) {
  const s=runtime?.heartbeat;
  if(!s) return '';
  const p=s.pulse||{};
  return JSON.stringify([s.queued,s.running,s.waiting_owner,s.waiting_quota,s.active_now,s.execution_enabled,s.interval_seconds,beatIsStale(s),p.enabled,p.last_result,p.last_reason,p.last_pulse_at,p.next_pulse_at,runtime.heartbeat_config]);
}

/** Refresh the relative times inside `root` without re-rendering. */
export function tickTimes(root,nowMs=Date.now()) {
  for(const el of root.querySelectorAll('[data-hb-time]')) {
    const at=el.dataset.hbTime;if(!at) continue;
    el.textContent=el.dataset.hbKind==='next'?'Next '+formatUntil(at,nowMs):formatAgo(at,nowMs);
  }
}

// ---- wiring ------------------------------------------------------------------------------------------------------
export function createHeartbeat(h) {
  const {esc,icon,button,field,api,refresh,toast,openModal,closeModal}=h;
  const data=()=>h.getData();
  const agents=()=>(data().agents||[]).filter(a=>!a.archived).map(a=>({id:a.id,name:a.name,role:a.role,heartbeat:!!a.heartbeat}));
  const form=()=>document.querySelector('#modal-form[data-type="heartbeat"]');
  const defaults=()=>heartbeatConfig(data().runtime?.heartbeat_defaults||DEFAULTS);

  function card() {
    return heartbeatCard({status:data().runtime?.heartbeat||null,config:data().runtime?.heartbeat_config||data().settings?.heartbeat,settings:data().settings,mode:h.getMode(),esc,icon,button});
  }
  function intervalText() {
    const c=heartbeatConfig(data().runtime?.heartbeat_config||data().settings?.heartbeat);
    return formatInterval(c.interval_seconds);
  }
  function open() {
    const config=data().runtime?.heartbeat_config||data().settings?.heartbeat;
    openModal('Customize team heartbeat','Choose how often the team checks for work, when it may run and how it checks in.',
      customizeBody({config,settings:data().settings,agents:agents(),esc,field}),
      button('Cancel','modal-close')+'<button type="submit" class="button primary">'+icon('check')+'Save changes</button>','heartbeat');
    sync();
    document.querySelector('#modal .hb-switch input')?.focus();
  }
  function values(f) {
    const get=name=>f.elements[name];
    const checked=name=>!!get(name)?.checked;
    const radio=name=>f.querySelector('input[name="'+name+'"]:checked')?.value;
    return {
      execution_enabled:checked('execution_enabled'),max_parallel:get('max_parallel').value,turns_per_slice:get('turns_per_slice').value,
      interval_seconds:radio('interval_seconds'),hours_enabled:checked('hours_enabled'),hours_start:get('hours_start').value,hours_end:get('hours_end').value,
      days:[...f.querySelectorAll('input[name="days"]:checked')].map(i=>i.value),pause_outside:checked('pause_outside'),
      pulse_enabled:checked('pulse_enabled'),pulse_agent:get('pulse_agent').value,pulse_every:radio('pulse_every'),pulse_changed:checked('pulse_changed'),
      pulse_notify:radio('pulse_notify'),pulse_prompt:get('pulse_prompt').value
    };
  }
  /** Dim groups whose switch is off and refresh the token note. */
  function sync() {
    const f=form();if(!f) return;
    for(const group of f.querySelectorAll('[data-off-when]')) group.dataset.off=String(!f.elements[group.dataset.offWhen].checked);
    const pulse=f.querySelector('[data-pulse-fields]');if(pulse) pulse.dataset.off=String(!f.elements.pulse_enabled.checked);
    const text=f.querySelector('#hb-token-text');if(text) text.textContent=tokenNote(values(f));
  }
  async function submit(f) {
    if(f.id!=='modal-form'||f.dataset.type!=='heartbeat') return false;
    const errorBox=document.querySelector('#modal-error'),submitButton=f.querySelector('[type=submit]');
    if(submitButton?.disabled) return true;
    errorBox.textContent='';
    let body;
    try {body=heartbeatPayload(values(f),{omitExecution:h.getMode()==='demo'&&!!f.elements.execution_enabled.checked===(data().settings.execution_enabled!==false)});}
    catch(e) {errorBox.textContent=e.message;errorBox.focus();return true;}
    submitButton.disabled=true;submitButton.setAttribute('aria-busy','true');
    try {
      await api('/settings',body);
      const wanted=new Set([...f.querySelectorAll('input[name="agent_hb"]:checked')].map(i=>i.value));
      const failed=[];
      for(const a of agents()) {
        if(wanted.has(a.id)===a.heartbeat) continue;
        try {await api('/agents/'+a.id,{heartbeat:wanted.has(a.id),version:(data().agents.find(x=>x.id===a.id)||{}).version},'PATCH');await refresh(false);}
        catch(e) {failed.push(a.name+': '+e.message);}
      }
      await refresh(false);
      if(failed.length) {errorBox.textContent='Saved, but some agents were not updated. '+failed.join(' ');errorBox.focus();h.render();return true;}
      closeModal(true);h.render();toast('Heartbeat settings saved.');
    } catch(e) {errorBox.textContent=e.message;errorBox.focus();}
    finally {if(submitButton.isConnected){submitButton.disabled=false;submitButton.removeAttribute('aria-busy');}}
    return true;
  }
  function changed(input) {
    if(!input.closest('#modal-form[data-type="heartbeat"]')) return false;
    sync();return true;
  }
  async function perform(action,id,target) {
    if(action==='hb-customize'){open();return true;}
    if(action==='hb-step') {
      const [name,delta]=id.split(':'),input=form()?.elements[name];if(!input) return true;
      const [min,max]=LIMITS[name],next=Math.min(max,Math.max(min,(Number(input.value)||min)+Number(delta)));
      input.value=next;input.dispatchEvent(new Event('input',{bubbles:true}));return true;
    }
    if(action==='hb-prompt-reset') {
      const area=form()?.elements.pulse_prompt;if(area){area.value=defaults().pulse.prompt;area.dispatchEvent(new Event('input',{bubbles:true}));area.focus();}
      return true;
    }
    if(action==='hb-pulse') {
      if(h.getMode()==='demo'){toast('The team pulse runs in the local app.',true);return true;}
      target.disabled=true;target.setAttribute('aria-busy','true');
      try {const result=await api('/heartbeat/pulse-now',{});await refresh(false);h.render();toast(pulseMessage(result),!result.started&&result.reason==='agent_unavailable');}
      catch(e) {toast(e.message,true);}
      finally {if(target.isConnected){target.disabled=false;target.removeAttribute('aria-busy');}}
      return true;
    }
    return false;
  }
  return {card,intervalText,perform,submit,changed,signature:()=>heartbeatSignature(data().runtime),tick:()=>tickTimes(document)};
}
