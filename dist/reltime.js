// Relative time ("30s ago") for the chat live line, the newest message and the roster, plus the stall warning.
// Everything except createTicker/updateNode's DOM use is pure so tests/reltime.mjs can cover it.

export const STALL_MS=3*60*1000,TICK_MS=15000,THINK_LIMIT_MS=20*60*1000;
// Runs in these states are expected to keep producing activity; waiting on the owner or on quota is calm and never a "stall".
export const PROGRESSING=new Set(['running','queued','retrying','waiting_team']);
const TERMINAL=new Set(['done','failed','cancelled']);
const MONTHS=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

export const parseTs=value=>{const t=typeof value==='number'?value:Date.parse(value);return Number.isFinite(t)?t:null;};
/** "30s", "5 min", "2 h" (no suffix). */
export function durationText(ms) {
  const s=Math.max(0,Math.floor(ms/1000));
  if(s<60) return s+'s';
  const m=Math.floor(s/60);
  if(m<60) return m+' min';
  return Math.floor(m/60)+' h';
}
const dayStart=t=>{const d=new Date(t);return new Date(d.getFullYear(),d.getMonth(),d.getDate()).getTime();};
/** "just now" (<10s), "30s ago", "5 min ago", "2 h ago", "yesterday", then a date. */
export function relTime(ts,now=Date.now()) {
  const t=parseTs(ts),n=parseTs(now);
  if(t===null||n===null) return '';
  const diff=n-t;
  if(diff<10000) return 'just now';
  if(diff<24*3600*1000) return durationText(diff)+' ago';
  const days=Math.round((dayStart(n)-dayStart(t))/86400000);
  if(days<=1) return 'yesterday';
  const d=new Date(t),sameYear=d.getFullYear()===new Date(n).getFullYear();
  return MONTHS[d.getMonth()]+' '+d.getDate()+(sameYear?'':', '+d.getFullYear());
}
/** Roster form: "now", "30s", "2m", "3h", "2d". */
export function compactAge(ts,now=Date.now()) {
  const t=parseTs(ts),n=parseTs(now);
  if(t===null||n===null) return '';
  const s=Math.floor((n-t)/1000);
  if(s<10) return 'now';
  if(s<60) return s+'s';
  if(s<3600) return Math.floor(s/60)+'m';
  if(s<86400) return Math.floor(s/3600)+'h';
  return Math.floor(s/86400)+'d';
}
/** Chat live line: "last activity 30s ago", or "no activity for 7 min" once a still-running task goes quiet. */
export function activityText(ts,now=Date.now(),{warn=false,since=null,limit=THINK_LIMIT_MS}={}) {
  const t=parseTs(ts),n=parseTs(now),started=parseTs(since);
  // A model call in flight is read without streaming: that is "thinking", not silence, until it outlives its timeout.
  if(started!==null&&n!==null) {
    const over=!!warn&&n-started>(Number(limit)>0?Number(limit):THINK_LIMIT_MS);
    return {text:over?'no response for '+durationText(n-started):'thinking for '+durationText(n-started),stalled:over};
  }
  if(t===null||n===null) return {text:'',stalled:false};
  const stalled=!!warn&&n-t>STALL_MS;
  return {text:stalled?'no activity for '+durationText(n-t):'last activity '+relTime(t,n),stalled};
}

/** The active runs of a room plus every live run they delegated to (children, grandchildren, same root). */
export function familyRuns(runs,roomRuns) {
  const roots=new Set(roomRuns.map(r=>r.root_id||r.id)),ids=new Set(roomRuns.map(r=>r.id));
  const live=(runs||[]).filter(r=>!TERMINAL.has(r.status));
  let grew=true;
  while(grew) {
    grew=false;
    for(const r of live) if(!ids.has(r.id)&&(ids.has(r.parent_run_id)||roots.has(r.root_id))) {ids.add(r.id);grew=true;}
  }
  return live.filter(r=>ids.has(r.id));
}
/** "Stuck: waiting on each other - Lan, Tuan" when every live run of the family waits for teammates and nothing runs or thinks. */
export function stuckInfo(runs=[],nameOf=r=>r.agent_id) {
  const live=(runs||[]).filter(r=>!TERMINAL.has(r.status));
  if(!live.length||live.some(r=>r.status!=='waiting_team'||parseTs(r.model_call_started_at)!==null)) return null;
  const names=[...new Set(live.map(nameOf).filter(Boolean))];
  return {text:'Stuck: waiting on each other \u2014 '+names.join(', '),rootId:live[0].root_id||live[0].id,count:live.length};
}
/** Newest of: any message in the room (all audiences), messages of the family runs, and the family runs' own activity stamps. */
export function lastActivity({runs=[],messages=[],channel='',roomRuns=[]}) {
  const family=familyRuns(runs,roomRuns),ids=new Set(family.map(r=>r.id));
  let best=null;
  const take=value=>{const t=parseTs(value);if(t!==null&&(best===null||t>best)) best=t;};
  for(const m of messages) if(m.channel===channel||ids.has(m.run_id)) take(m.created_at);
  for(const r of family) {take(r.last_activity_at);take(r.updated_at);take(r.started_at);}
  const calls=family.map(r=>parseTs(r.model_call_started_at)).filter(t=>t!==null);
  const since=calls.length?Math.max(...calls):null;
  return {ts:best,iso:best===null?'':new Date(best).toISOString(),family,progressing:family.some(r=>PROGRESSING.has(r.status)),thinkingSince:since,since:since===null?'':new Date(since).toISOString()};
}
/** Newest activity stamp of one agent's active run (roster line). */
export function agentActivity(run) {
  if(!run) return null;
  const times=[run.last_activity_at,run.updated_at,run.started_at].map(parseTs).filter(t=>t!==null);
  return times.length?Math.max(...times):null;
}

const MODES={
  ago:(t,n)=>({text:relTime(t,n)}),
  compact:(t,n)=>({text:' · '+compactAge(t,n)}),
  activity:(t,n,warn,extra={})=>{const a=activityText(t,n,{warn,...extra});return {text:' · '+a.text,stalled:a.stalled};}
};
/** A text node that the shared ticker keeps fresh. `warn` lets the activity form flag a stall on its .live-work line. */
export function reltimeMarkup({ts,mode='ago',esc,now=Date.now(),warn=false,className='',title='',since='',limit=0}) {
  const t=parseTs(ts);
  if(t===null) return '';
  const out=(MODES[mode]||MODES.ago)(t,now,warn,{since:since||null,limit});
  return '<span class="reltime'+(className?' '+className:'')+(out.stalled?' is-stalled':'')+'" data-reltime="'+esc(new Date(t).toISOString())+'" data-rt="'+mode+'"'+(warn?' data-rt-warn="1"':'')+(since?' data-rt-since="'+esc(since)+'"':'')+(limit?' data-rt-limit="'+esc(limit)+'"':'')+(title?' title="'+esc(title)+'"':'')+'>'+esc(out.text)+'</span>';
}
/** Re-rendering must not be triggered by the clock: compare markup with the ticking text removed. */
export const stableMarkup=markup=>String(markup).replace(/(data-reltime="[^"]*"[^>]*>)[^<]*/g,'$1');

/** Update one [data-reltime] element in place (text and stall class only). */
export function updateNode(el,now=Date.now()) {
  const t=parseTs(el.dataset?.reltime);
  if(t===null) return;
  const out=(MODES[el.dataset.rt]||MODES.ago)(t,now,el.dataset.rtWarn==='1',{since:el.dataset.rtSince||null,limit:Number(el.dataset.rtLimit)||undefined});
  if(el.textContent!==out.text) el.textContent=out.text;
  if(el.dataset.rtWarn==='1') {
    el.classList.toggle('is-stalled',!!out.stalled);
    el.closest?.('.live-work')?.classList.toggle('is-stalled',!!out.stalled);
  }
}
/** One shared ticker for the whole page: no re-render, paused while the tab is hidden. */
export function createTicker({root=document,interval=TICK_MS,now=()=>Date.now()}={}) {
  let timer=0;
  const tick=()=>{if(root.hidden) return;root.querySelectorAll('[data-reltime]').forEach(el=>updateNode(el,now()));};
  const stop=()=>{clearInterval(timer);timer=0;};
  const start=()=>{if(!timer) timer=setInterval(tick,interval);};
  const onVisibility=()=>{if(root.hidden) stop();else {tick();start();}};
  root.addEventListener?.('visibilitychange',onVisibility);
  if(!root.hidden) start();
  return {tick,stop,start,destroy:()=>{stop();root.removeEventListener?.('visibilitychange',onVisibility);}};
}
let shared=null;
/** The page-wide ticker, created once however many times the chat view is built. */
export const ensureTicker=()=>shared||(shared=createTicker());
