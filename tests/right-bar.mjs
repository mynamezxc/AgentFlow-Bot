import assert from 'node:assert/strict';
import {roomProjects,chooseProject,goalsOf,orderGoals,goalsMarkup,forYouFiles,changedAfterForYou,agentEventMarkup} from '../dist/right-bar.js';
import {isActivity} from '../dist/worklog.js';

const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const P=(id,extra={})=>({id,title:'Project '+id,channel:'dm-a-lead',created_at:'2026-10-19T01:00:00Z',...extra});
const projects=[P('p1'),P('p2',{created_at:'2026-10-19T02:00:00Z'}),P('p3',{archived:true}),P('px',{channel:'dm-other'})];

// Project choice: follow the most recent activity in this room, not the oldest/last-created channel project
const messages=[{channel:'dm-a-lead',project_id:'p1',created_at:'2026-10-19T03:00:00Z'},{channel:'dm-a-lead',project_id:'p2',created_at:'2026-10-19T02:30:00Z'},{channel:'dm-other',project_id:'px',created_at:'2026-10-19T09:00:00Z'}];
const channel='dm-a-lead';
assert.equal(chooseProject({projects,messages,runs:[],channel}).id,'p1');
const runs=[{channel,project_id:'p2',created_at:'2026-10-19T04:00:00Z',updated_at:'2026-10-19T04:10:00Z'}];
assert.equal(chooseProject({projects,messages,runs,channel}).id,'p2','a newer run moves the bar to its project');
assert.deepEqual(roomProjects({projects,messages,runs,channel}).map(p=>p.id),['p2','p1'],'archived and foreign projects are excluded, newest first');
assert.equal(chooseProject({projects,messages:[],runs:[],channel}).id,'p2','no activity: the channel project');
assert.equal(chooseProject({projects:[],messages:[],runs:[],channel,fallback:()=>({id:'f'})}).id,'f');
// Owner pick wins until the room's work moves to a different project after that pick
assert.equal(chooseProject({projects,messages,runs,channel,chosen:{id:'p1',at:Date.parse('2026-10-19T05:00:00Z')}}).id,'p1');
assert.equal(chooseProject({projects,messages,runs,channel,chosen:{id:'p1',at:Date.parse('2026-10-19T03:30:00Z')}}).id,'p2');
assert.equal(chooseProject({projects,messages,runs,channel,chosen:'p1'}).id,'p1','a plain id is sticky');
assert.equal(chooseProject({projects,messages,runs,channel,chosen:{id:'p3',at:0}}).id,'p2','an archived pick is ignored');

// Goals: legacy fallback, ordering, markup
const legacy=goalsOf({title:'Old title',brief:'Old brief',status:'active'});
assert.equal(legacy.length,1);assert.equal(legacy[0].title,'Old title');assert.ok(legacy[0].legacy);
assert.deepEqual(goalsOf(null),[]);
const goals=[{id:'g1',title:'First',text:'First',status:'done'},{id:'g2',title:'Second',text:'Second goal text',status:'active'},{id:'g3',title:'Third',text:'Third',status:'active'}];
const order=orderGoals(goals);
assert.deepEqual(order.active.map(g=>g.id),['g2','g3']);assert.deepEqual(order.done.map(g=>g.id),['g1']);
let html=goalsMarkup({id:'p1',title:'My <project>',goals},esc);
assert.ok(html.includes('<small>GOALS</small>')&&!html.includes('CURRENT GOAL'));
assert.ok(html.includes('<h3>My &lt;project&gt;</h3>'));
assert.ok(html.indexOf('Second')<html.indexOf('Third')&&html.indexOf('Third')<html.indexOf('1 done'));
assert.ok(html.includes('data-action="co-goal-remove" data-id="g2"'));
assert.ok(/<details class="goal-done"[^>]*><summary>1 done<\/summary>/.test(html)&&!/goal-done"[^>]*open/.test(html));
assert.ok(goalsMarkup({id:'p1',title:'T',goals},esc,{doneOpen:true}).includes('data-section="goaldone" open'));
assert.ok(!html.includes('project-switch'),'no switcher for a single project');
html=goalsMarkup({id:'p1',title:'T',goals:[]},esc);assert.ok(html.includes('No active goals.'));
html=goalsMarkup({id:'p1',title:'Legacy',brief:'b'},esc);assert.ok(html.includes('Legacy')&&!html.includes('co-goal-remove'),'legacy goal cannot be removed');
html=goalsMarkup({id:'p2',title:'Two',goals},esc,{projects:[{id:'p1',title:'One'},{id:'p2',title:'Two'}]});
assert.ok(html.includes('id="co-project-switch"')&&html.includes('data-action="co-project-pick"'));
assert.ok(html.includes('<option value="p2" selected>Two</option>')&&html.includes('<option value="p1">One</option>'));
assert.equal(goalsMarkup(null,esc),'');

// Outputs: "For you" split
const art=[{id:'a1',deliverable:true,path:'r/report.md'},{id:'a2',path:'scratch.txt'},{id:'a3',deliverable:true,path:'shot.png',is_image:true},{id:'a4',deliverable:true,path:'old.md',trashed_at:'x'},{id:'a5',deliverable:true,path:'demo.html'}];
const isPicture=a=>!!a.is_image;
assert.deepEqual(forYouFiles(art,isPicture).map(a=>a.id),['a5','a1'],'newest first; images, trashed and non-deliverables excluded');
const groups=[{name:'report.md',latest:{artifact_id:'a1'}},{name:'scratch.txt',latest:{artifact_id:'a2'}}];
assert.deepEqual(changedAfterForYou(groups,forYouFiles(art,isPicture)).map(g=>g.name),['scratch.txt']);
assert.deepEqual(changedAfterForYou(groups,[]).length,2);

// Agent event cards are never folded into the work log
const ev={id:'m1',kind:'agent_event',audience:'owner',event:'created',agent_id:'a-lead',created_at:'2026-10-19T03:00:00Z',content:'x',items:[{id:'a-mira',name:'Mira',role:'Researcher',model:'codex/gpt-6.1-sol'}]};
assert.equal(isActivity(ev),false);assert.equal(isActivity({...ev,audience:'activity'}),false);
const h={esc,icon:n=>'<i>'+n+'</i>',avatar:a=>'<b class="av">'+esc(a.name)+'</b>',actor:id=>({id,name:id==='a-lead'?'Atlas':'Mira'}),stamp:v=>'T'};
html=agentEventMarkup(ev,h);
assert.ok(html.includes('<strong>Atlas</strong> created')&&html.includes('<strong>Mira</strong>')&&html.includes('Researcher · codex/gpt-6.1-sol'));
assert.ok(html.includes('data-action="agent-edit" data-id="a-mira">Open profile</button>'));
html=agentEventMarkup({...ev,event:'updated',changed:['role','model']},h);
assert.ok(html.includes('updated · role, model')&&html.includes('data-event="updated"'));
html=agentEventMarkup({...ev,items:[...ev.items,{id:'a-leo',name:'Leo',role:'Coder',model:''}]},h);
assert.ok(html.includes('created 2 agents')&&(html.match(/Open profile/g)||[]).length===2);
assert.ok(agentEventMarkup({...ev,items:undefined},h).includes('>x<'),'falls back to the stored text');
console.log('right bar assertions passed');
