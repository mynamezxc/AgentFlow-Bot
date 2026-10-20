import assert from 'node:assert/strict';
import {isActivity,actionsOf,logSummary,renderWorkLog,buildLogExtras,mergeLog,treeRuns,MAX_ROWS} from '../dist/worklog.js';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const h={esc,icon:n=>'<i data-i="'+n+'"></i>',stamp:()=>'08:03',format:t=>esc(t)};
// New structured auto-approval record.
const cmd={id:'m1',role:'assistant',kind:'action',audience:'activity',content:'Auto-approved: Run python -I -c ...',
  actions:[{type:'command',label:'Run python -I -c "from pathlib import Path',detail:'python -I -c "from pathlib import Path\nimport re,json\np=Path(\'x.md\')"',cwd:'C:\\Projects\\demo-shop',auto:true}]};
assert.ok(isActivity(cmd));
const html=renderWorkLog([cmd],h);
assert.ok(html.startsWith('<details class="work-log"'));        // whole log collapsed by default
assert.ok(!html.includes('<details class="work-log" open'));
assert.ok(html.includes('<details data-key="act:m1:0">'));         // each action collapsed, expandable
assert.ok(html.includes('<pre class="wl-detail">python -I -c &quot;from pathlib import Path\nimport re,json'));  // full command, never Markdown
assert.ok(html.includes('in C:\\Projects\\demo-shop'));
assert.ok(!html.includes('<a '));                                 // no accidental links from Markdown
// Legacy "info" Markdown notices from before this change are folded and parsed too.
const legacy={id:'m2',role:'assistant',kind:'info',audience:'owner',content:"Auto-approved: run `python -I -c \"print(1)\"` in C:\\Projects\\demo-shop"};
assert.ok(isActivity(legacy));
assert.deepEqual(actionsOf(legacy).map(a=>[a.type,a.cwd]),[['command','C:\\Projects\\demo-shop']]);
const many={id:'m3',role:'assistant',kind:'info',content:'Auto-approved 3 requests:\n- browser access to *.shop.example\n- call MCP tool `echo` on Files\n- map *.shop.example → 127.0.0.1 for the team browser'};
assert.deepEqual(actionsOf(many).map(a=>a.type),['browser','mcp','browser']);
const note={id:'m4',role:'assistant',kind:'progress',audience:'activity',content:'Reading the brief'};
assert.equal(logSummary([note,cmd,many]),'Work log · 1 update · 4 auto-approved actions');
// Owner messages, final replies and attachments stay visible.
assert.ok(!isActivity({role:'user',kind:'chat',content:'hi'}));
assert.ok(!isActivity({role:'assistant',kind:'chat',audience:'owner',content:'Done'}));
assert.ok(!isActivity({role:'assistant',kind:'progress',audience:'owner',content:'x',artifact_ids:['a1']}));
console.log('worklog ok');

// ---- Rich work log: run notes, team messages and tool calls, merged chronologically ----------------------------------------------------
{
  const room='dm-a-lead',names={'a-lead':'Atlas','a-mai':'Mai','a-minh':'Minh','a-lan':'Lan'};
  const at=min=>new Date(Date.UTC(2026,9,20,8,min)).toISOString();
  const runs=[
    {id:'r-root',agent_id:'a-lead',kind:'chat',channel:room,root_id:'r-root',started_at:at(0),work_log:{notes:[{s:1,t:'Splitting the plan',at:at(2)},{s:2,t:'Waiting for Mai',at:at(9)}],calls:[{s:1,n:'tasks.create',a:'title=Plan',r:'ok',e:false,at:at(3)},{s:2,n:'workspace.read',a:'path=brief.md',r:'ok',e:false,at:at(4)}]}},
    {id:'r-task',agent_id:'a-mai',kind:'task',channel:'dm-a-mai',root_id:'r-root',started_at:at(5),work_log:{notes:[{s:1,t:'Drafting the outline'}],calls:[{s:1,n:'workspace.write',a:'path=outline.md',r:'ok',e:true}]}},
    {id:'r-peer',agent_id:'a-minh',kind:'peer',channel:'dm-a-minh',root_id:'r-root',started_at:at(6),work_log:{}},
    {id:'r-other',agent_id:'a-lan',kind:'chat',channel:'dm-a-lan',root_id:'r-other',started_at:at(1),work_log:{notes:[{s:1,t:'Not this room',at:at(1)}],calls:[]}},
    {id:'r-sys',agent_id:'a-lead',kind:'telegram',channel:room,root_id:'r-sys',system_managed:true,work_log:{notes:[{s:1,t:'system',at:at(1)}]}}];
  const msg=(id,min,over={})=>({id,role:'assistant',channel:room,created_at:at(min),agent_id:'a-lead',...over});
  const messages=[
    msg('u1',1,{role:'user',kind:'chat',audience:'owner',content:'Plan the launch'}),
    msg('p1',2,{kind:'progress',audience:'activity',content:'Splitting the plan',run_id:'r-root'}),   // same text as the run note: shown once
    msg('t1',7,{kind:'peer',audience:'internal',channel:'dm-a-mai',agent_id:'a-lead',target_id:'a-mai',run_id:'r-root',content:'Please outline the launch plan\nwith three channels.'}),
    msg('h1',7,{kind:'handoff',audience:'internal',agent_id:'a-lead',target_id:'a-mai',run_id:'r-root',content:'Messaged Mai'}),   // redundant with t1
    msg('t2',8,{kind:'peer_reply',audience:'internal',channel:'dm-a-lead',agent_id:'a-mai',target_id:'a-lead',run_id:'r-task',content:'Outline ready'}),
    msg('x1',8,{kind:'peer',audience:'internal',run_id:'r-other',agent_id:'a-lan',target_id:'a-mai',content:'unrelated'}),
    msg('o1',12,{kind:'chat',audience:'owner',content:'Here is the plan'})];
  assert.deepEqual(treeRuns(runs,room).map(r=>r.id),['r-root','r-task','r-peer']);   // tree = runs whose root started in this room; system runs excluded
  const extras=buildLogExtras({room,messages,runs});
  assert.ok(!extras.some(e=>e.content==='Splitting the plan'),'a note that duplicates an activity message is shown once');
  assert.ok(!extras.some(e=>/not this room|unrelated|system/.test(e.content||'')));
  assert.ok(!extras.some(e=>e.id==='team:h1'),'a handoff next to its peer message is skipped');
  assert.deepEqual(extras.map(e=>e.created_at),[...extras.map(e=>e.created_at)].sort());   // chronological
  assert.equal(extras.find(e=>e.id==='calls:r-root').calls.length,2);
  assert.equal(extras.find(e=>e.id==='calls:r-task').calls[0].at.slice(0,10),'2026-10-20');   // entries saved before `at` existed use the run start
  assert.equal(extras.find(e=>e.id==='note:r-task:0').created_at,at(5));
  assert.ok(extras.every(e=>e.audience==='activity'&&isActivity(e)));
  const owner=messages.filter(m=>m.audience==='owner'||m.audience==='activity');
  const flow=mergeLog(owner,extras);
  assert.deepEqual(flow.map(m=>m.id),['u1','p1','calls:r-root','note:r-task:0','calls:r-task','team:t1','team:t2','note:r-root:1','o1']);
  // One streak between the two owner bubbles: collapsed, one summary line, team names without the room's own agent.
  const streak=flow.slice(1,-1);
  const hh={...h,agentName:id=>names[id],nameOf:id=>names[id],mainId:'a-lead',avatar:id=>'<i class="av" data-id="'+id+'"></i>',expanded:()=>false,stamp:t=>t.slice(11,16)};
  assert.equal(logSummary(streak,hh.nameOf,'a-lead'),'Work log · 5 updates · 3 tool calls · team: Mai');
  const html=renderWorkLog(streak,hh);
  assert.ok(html.startsWith('<details class="work-log" data-key="log:p1"><summary>'));
  assert.ok(!html.includes('<details class="work-log" open'));
  assert.ok(html.includes('<b>Atlas → Mai</b>')||html.includes('<b>Atlas</b> → <b>Mai</b>'));          // team row "A → B"
  assert.ok(html.includes('Please outline the launch plan with three channels.'));                     // first line, one-lined
  assert.ok(html.includes('<details data-key="team:team:t1">')&&html.includes('wl-full'));             // long text expands
  assert.ok(html.includes('class="wl-line"'));            // short team row stays one line
  assert.ok(html.includes('wl-count">2 tool calls<')&&html.includes('wl-count">1 tool call · 1 failed<'));
  assert.ok(html.includes('<ul class="wl-call-list">')&&html.includes('<em>failed</em>'));
  assert.ok(html.includes('<b><i class="av" data-id="a-mai"></i>Mai</b>')||html.includes('Mai</b>'));    // "Name · note" rows carry the name
  // Cap: only the newest MAX_ROWS rows render, "Show N earlier" appears until expanded.
  const many=Array.from({length:MAX_ROWS+25},(_,i)=>({id:'n'+i,role:'assistant',kind:'progress',audience:'activity',agent_id:'a-mai',content:'step '+i,created_at:at(0)}));
  const capped=renderWorkLog(many,hh);
  assert.equal((capped.match(/class="wl-note"/g)||[]).length,MAX_ROWS);
  assert.ok(capped.includes('data-action="wl-earlier"')&&capped.includes('Show 25 earlier')&&capped.includes('step '+(MAX_ROWS+24))&&!capped.includes('step 24<'));
  const all=renderWorkLog(many,{...hh,expanded:key=>key==='log:n0'});
  assert.equal((all.match(/class="wl-note"/g)||[]).length,MAX_ROWS+25);
  assert.ok(!all.includes('wl-earlier'));
  // A long note folds into a nested details with a one-line summary.
  const long=renderWorkLog([{id:'L1',role:'assistant',kind:'progress',audience:'activity',agent_id:'a-mai',content:'x'.repeat(260),created_at:at(0)}],hh);
  assert.ok(long.includes('class="wl-note wl-long"')&&long.includes('<details data-key="note:L1">'));
  // Rooms without any run data keep the old behaviour.
  assert.deepEqual(buildLogExtras({room:'dm-none',messages,runs}),[]);
  assert.deepEqual(mergeLog([],[]),[]);
  console.log('rich work log ok');
}
{ // lazy mode: collapsed logs carry no rows; the rows come from renderWorkLogRows when opened
  const {renderWorkLogRows}=await import('../dist/worklog.js');
  const entries=[{id:'n1',kind:'progress',audience:'activity',agent_id:'a1',content:'First note',created_at:'2026-01-01T00:00:00Z'},{id:'n2',kind:'progress',audience:'activity',agent_id:'a1',content:'Second note',created_at:'2026-01-01T00:01:00Z'}];
  const helpers={esc:s=>String(s),icon:()=>'',stamp:()=>'t',format:s=>s,agentName:()=>'A',avatar:()=>'',expanded:null,nameOf:()=>'A',mainId:'a1'};
  const lazy=renderWorkLog(entries,{...helpers,lazy:true}),eager=renderWorkLog(entries,helpers);
  assert.ok(lazy.includes('<ol data-lazy="1"></ol>')&&!lazy.includes('First note</div>'));
  assert.ok(lazy.includes('data-key="log:n1"')&&lazy.includes('Work log'));
  assert.equal(eager,lazy.replace('<ol data-lazy="1"></ol>','<ol>'+renderWorkLogRows(entries,helpers)+'</ol>'));
  assert.ok(renderWorkLogRows(entries,helpers).includes('Second note'));
  console.log('lazy work log ok');
}
