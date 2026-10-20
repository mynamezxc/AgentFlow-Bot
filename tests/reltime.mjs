import assert from 'node:assert/strict';
import {THINK_LIMIT_MS,STALL_MS,durationText,relTime,compactAge,activityText,familyRuns,stuckInfo,lastActivity,agentActivity,reltimeMarkup,stableMarkup,updateNode,createTicker} from '../dist/reltime.js';

const NOW=Date.parse('2026-10-19T12:00:00Z'),ago=ms=>new Date(NOW-ms).toISOString(),S=1000,M=60*S,H=60*M;
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// relTime
assert.equal(relTime(ago(0),NOW),'just now');
assert.equal(relTime(ago(9*S),NOW),'just now');
assert.equal(relTime(ago(10*S),NOW),'10s ago');
assert.equal(relTime(ago(30*S),NOW),'30s ago');
assert.equal(relTime(ago(59*S),NOW),'59s ago');
assert.equal(relTime(ago(60*S),NOW),'1 min ago');
assert.equal(relTime(ago(5*M+20*S),NOW),'5 min ago');
assert.equal(relTime(ago(59*M),NOW),'59 min ago');
assert.equal(relTime(ago(2*H),NOW),'2 h ago');
assert.equal(relTime(ago(23*H),NOW),'23 h ago');
assert.equal(relTime(ago(-5*S),NOW),'just now','clock skew never shows the future');
assert.equal(relTime('nonsense',NOW),'');
assert.equal(relTime(null,NOW),'');
// yesterday, then a date (local calendar days)
const local=(y,m,d,h=12)=>new Date(y,m-1,d,h).getTime();
assert.equal(relTime(local(2026,10,18,9),local(2026,10,19,12)),'yesterday');
assert.equal(relTime(local(2026,10,17,9),local(2026,10,19,12)),'Oct 17');
assert.equal(relTime(local(2025,12,31,9),local(2026,10,19,12)),'Dec 31, 2025');

// compact + duration
assert.equal(durationText(7*M),'7 min');
assert.equal(compactAge(ago(3*S),NOW),'now');
assert.equal(compactAge(ago(30*S),NOW),'30s');
assert.equal(compactAge(ago(2*M+5*S),NOW),'2m');
assert.equal(compactAge(ago(3*H),NOW),'3h');
assert.equal(compactAge(ago(50*H),NOW),'2d');

// activity text and stall rule (> 3 min and only when warn)
assert.deepEqual(activityText(ago(30*S),NOW,{warn:true}),{text:'last activity 30s ago',stalled:false});
assert.deepEqual(activityText(ago(STALL_MS),NOW,{warn:true}),{text:'last activity 3 min ago',stalled:false});
assert.deepEqual(activityText(ago(7*M),NOW,{warn:true}),{text:'no activity for 7 min',stalled:true});
assert.deepEqual(activityText(ago(7*M),NOW,{warn:false}),{text:'last activity 7 min ago',stalled:false},'waiting on the owner is never a stall');

// family and last activity
const runs=[
  {id:'r1',root_id:'r1',channel:'dm-a',status:'waiting_team',updated_at:ago(8*M)},
  {id:'r2',root_id:'r1',parent_run_id:'r1',channel:'dm-b',kind:'peer',status:'running',updated_at:ago(40*S),last_activity_at:ago(20*S)},
  {id:'r3',root_id:'r1',parent_run_id:'r2',channel:'dm-c',kind:'peer',status:'running',updated_at:ago(2*M)},
  {id:'r4',root_id:'r4',channel:'dm-z',status:'running',updated_at:ago(1*S)},
  {id:'r5',root_id:'r1',parent_run_id:'r1',status:'done',updated_at:ago(1*S)}
];
const roomRuns=[runs[0]];
assert.deepEqual(familyRuns(runs,roomRuns).map(r=>r.id),['r1','r2','r3'],'children and grandchildren, not unrelated or finished runs');
let act=lastActivity({runs,messages:[],channel:'dm-a',roomRuns});
assert.equal(act.ts,NOW-20*S);assert.equal(act.progressing,true);
act=lastActivity({runs,messages:[{channel:'dm-a',created_at:ago(5*S),audience:'activity'}],channel:'dm-a',roomRuns});
assert.equal(act.ts,NOW-5*S,'room messages of any audience count');
act=lastActivity({runs,messages:[{channel:'dm-q',run_id:'r3',created_at:ago(2*S)},{channel:'dm-other',run_id:'rx',created_at:ago(1*S)}],channel:'dm-a',roomRuns});
assert.equal(act.ts,NOW-2*S,'a delegated run message counts, other rooms do not');
const quiet=[{id:'q1',root_id:'q1',status:'waiting_user',updated_at:ago(9*M)}];
act=lastActivity({runs:quiet,messages:[],channel:'dm-a',roomRuns:quiet});
assert.equal(act.progressing,false);assert.equal(act.ts,NOW-9*M);
assert.equal(lastActivity({runs:[],messages:[],channel:'x',roomRuns:[]}).ts,null);
assert.equal(agentActivity({updated_at:ago(5*M),last_activity_at:ago(10*S)}),NOW-10*S);
assert.equal(agentActivity(null),null);

// markup + stable key
const html=reltimeMarkup({ts:ago(30*S),mode:'activity',esc,now:NOW,warn:true});
assert.match(html,/data-reltime="2026-10-19T11:59:30.000Z"/);assert.match(html,/data-rt-warn="1"/);assert.match(html,/ · last activity 30s ago/);
assert.match(reltimeMarkup({ts:ago(8*M),mode:'activity',esc,now:NOW,warn:true}),/is-stalled[^>]*>[^<]*no activity for 8 min/);
assert.equal(reltimeMarkup({ts:null,esc}),'');
const later=reltimeMarkup({ts:ago(30*S),mode:'activity',esc,now:NOW+45*S,warn:true});
assert.notEqual(html,later);
assert.equal(stableMarkup('<p>'+html+'</p>'),stableMarkup('<p>'+later+'</p>'),'ticking text must not change the render key');
assert.notEqual(stableMarkup(html),stableMarkup(reltimeMarkup({ts:ago(31*S),mode:'activity',esc,now:NOW,warn:true})));

// in-place update
const fake=(ts,rt,warn)=>{const line={classes:new Set(),classList:{toggle(c,on){on?line.classes.add(c):line.classes.delete(c);}}};const el={dataset:{reltime:ts,rt,...(warn?{rtWarn:'1'}:{})},textContent:'x',classes:new Set(),classList:{toggle(c,on){on?el.classes.add(c):el.classes.delete(c);}},closest:()=>line};return {el,line};};
let n=fake(ago(30*S),'activity',true);updateNode(n.el,NOW);
assert.equal(n.el.textContent,' · last activity 30s ago');assert.equal(n.line.classes.has('is-stalled'),false);
updateNode(n.el,NOW+6*M);
assert.equal(n.el.textContent,' · no activity for 6 min');assert.equal(n.line.classes.has('is-stalled'),true);assert.equal(n.el.classes.has('is-stalled'),true);
n=fake(ago(2*M),'compact',false);updateNode(n.el,NOW);assert.equal(n.el.textContent,' · 2m');
n=fake(ago(2*H),'ago',false);updateNode(n.el,NOW);assert.equal(n.el.textContent,'2 h ago');

// model call in flight: neutral "thinking for", stall only past the call limit
assert.equal(THINK_LIMIT_MS,20*M);
assert.deepEqual(activityText(ago(9*M),NOW,{warn:true,since:ago(2*M)}),{text:'thinking for 2 min',stalled:false},'a long model turn is not silence');
assert.deepEqual(activityText(ago(30*S),NOW,{warn:true,since:ago(45*S)}),{text:'thinking for 45s',stalled:false});
assert.deepEqual(activityText(ago(25*M),NOW,{warn:true,since:ago(21*M)}),{text:'no response for 21 min',stalled:true});
assert.deepEqual(activityText(ago(25*M),NOW,{warn:true,since:ago(21*M),limit:30*M}),{text:'thinking for 21 min',stalled:false},'provider timeout from settings');
assert.deepEqual(activityText(ago(25*M),NOW,{warn:false,since:ago(21*M)}).stalled,false);
assert.deepEqual(activityText(ago(7*M),NOW,{warn:true,since:null}),{text:'no activity for 7 min',stalled:true},'no call in flight: the 3 minute rule');
const thinking=[{id:'t1',root_id:'t1',channel:'dm-a',status:'running',updated_at:ago(4*M),model_call_started_at:ago(4*M)},{id:'t2',root_id:'t1',parent_run_id:'t1',status:'running',updated_at:ago(1*M),model_call_started_at:ago(1*M)}];
act=lastActivity({runs:thinking,messages:[],channel:'dm-a',roomRuns:[thinking[0]]});
assert.equal(act.thinkingSince,NOW-1*M);assert.equal(act.since,ago(1*M));
assert.equal(lastActivity({runs,messages:[],channel:'dm-a',roomRuns}).since,'');
const tm=reltimeMarkup({ts:ago(4*M),mode:'activity',esc,now:NOW,warn:true,since:ago(4*M),limit:1200000});
assert.match(tm,/data-rt-since="/);assert.match(tm,/ · thinking for 4 min/);assert.doesNotMatch(tm,/is-stalled/);
n=fake(ago(4*M),'activity',true);n.el.dataset.rtSince=ago(4*M);updateNode(n.el,NOW);
assert.equal(n.el.textContent,' · thinking for 4 min');assert.equal(n.line.classes.has('is-stalled'),false);
updateNode(n.el,NOW+17*M);assert.equal(n.el.textContent,' · no response for 21 min');assert.equal(n.line.classes.has('is-stalled'),true);
n.el.dataset.rtLimit=String(40*M);updateNode(n.el,NOW+17*M);assert.equal(n.el.textContent,' · thinking for 21 min');assert.equal(n.line.classes.has('is-stalled'),false);

// ticker: updates only data-reltime nodes, pauses while hidden
let ticks=0,hiddenHandlers=[];
const nodes=[fake(ago(30*S),'ago',false).el];
const root={hidden:false,querySelectorAll:()=>({forEach:fn=>nodes.forEach(x=>{ticks++;fn(x);})}),addEventListener:(t,fn)=>hiddenHandlers.push(fn),removeEventListener(){}};
const ticker=createTicker({root,interval:1e9,now:()=>NOW+60*S});
ticker.tick();assert.equal(nodes[0].textContent,'1 min ago');assert.equal(ticks,1);
root.hidden=true;ticker.tick();assert.equal(ticks,1,'paused while the tab is hidden');
root.hidden=false;hiddenHandlers[0]();assert.equal(ticks,2,'refreshes immediately when visible again');
ticker.destroy();


// Stuck line: every live run of the family waits for teammates, nothing runs, queues or thinks
{
  const name=r=>({a:'Lan',b:'Tuan',c:'Huy'}[r.agent_id]);
  const waiting=[{id:'r1',agent_id:'a',status:'waiting_team',root_id:'r1'},{id:'r2',agent_id:'b',status:'waiting_team',root_id:'r1'},{id:'r3',agent_id:'a',status:'waiting_team',root_id:'r1'},{id:'r0',agent_id:'c',status:'done',root_id:'r1'}];
  const info=stuckInfo(waiting,name);
  assert.equal(info.text,'Stuck: waiting on each other \u2014 Lan, Tuan');
  assert.equal(info.rootId,'r1');assert.equal(info.count,3);
  assert.equal(stuckInfo([...waiting,{id:'r4',agent_id:'c',status:'queued',root_id:'r1'}],name),null,'a queued run is progress');
  assert.equal(stuckInfo([...waiting,{id:'r4',agent_id:'c',status:'running',root_id:'r1'}],name),null);
  assert.equal(stuckInfo([{...waiting[0],model_call_started_at:'2026-10-20T10:00:00Z'},waiting[1]],name),null,'a model call in flight is not stuck');
  assert.equal(stuckInfo([...waiting,{id:'r5',agent_id:'c',status:'waiting_user',root_id:'r1'}],name),null,'waiting for the owner is a decision, not a deadlock');
  assert.equal(stuckInfo([{id:'x',agent_id:'a',status:'done'}],name),null);
  assert.equal(stuckInfo([],name),null);
  assert.equal(stuckInfo([{id:'solo',agent_id:'b',status:'waiting_team'}],name).rootId,'solo');
}
console.log('reltime tests passed');
