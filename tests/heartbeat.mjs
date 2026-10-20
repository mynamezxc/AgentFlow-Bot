import assert from 'node:assert/strict';
import {DEFAULTS,DEFAULT_PULSE_PROMPT,heartbeatConfig,formatAgo,formatUntil,formatInterval,daysSummary,windowSummary,formatWindowOpen,beatIsStale,
  heartbeatState,pulseNext,pulseLast,pulseMessage,pulsesPerDay,tokenNote,heartbeatPayload,customizeBody,heartbeatCard,heartbeatSignature,tickTimes} from '../dist/heartbeat-ui.js';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=n=>'<i data-icon="'+n+'"></i>';
const button=(text,action,id='',cls='')=>'<button class="button '+cls+'" data-action="'+action+'">'+esc(text)+'</button>';
const field=(name,label,value,type,opts={})=>'<div data-field="'+name+'" data-value="'+esc(value)+'">'+(type==='select'?(opts.options||[]).map(o=>'<option value="'+esc(o[0])+'">').join(''):'')+'</div>';
const NOW=Date.parse('2026-10-19T03:00:00Z');
const iso=ms=>new Date(ms).toISOString();

// ---- config defaults ----
assert.deepEqual(heartbeatConfig(undefined),DEFAULTS);
assert.equal(heartbeatConfig({interval_seconds:60}).interval_seconds,60);
assert.deepEqual(heartbeatConfig({active_days:[]}).active_days,[1,2,3,4,5,6,7]);
assert.deepEqual(heartbeatConfig({active_days:[5,1,1,9]}).active_days,[1,5]);
assert.equal(heartbeatConfig({pulse:{every_minutes:30}}).pulse.notify,'needed');
assert.equal(heartbeatConfig({pulse:{every_minutes:30}}).pulse.prompt,DEFAULT_PULSE_PROMPT);
assert.equal(heartbeatConfig({pause_schedules_outside_hours:false}).pause_schedules_outside_hours,false);

// ---- relative times ----
assert.equal(formatAgo(iso(NOW-2000),NOW),'just now');
assert.equal(formatAgo(iso(NOW-12000),NOW),'12 s ago');
assert.equal(formatAgo(iso(NOW-185000),NOW),'3 min ago');
assert.equal(formatAgo(iso(NOW-7300000),NOW),'2 h ago');
assert.equal(formatAgo(iso(NOW-2*86400000),NOW),'2 d ago');
assert.equal(formatAgo(null,NOW),'—');
assert.equal(formatAgo('garbage',NOW),'—');
assert.equal(formatUntil(iso(NOW+40000),NOW),'in 40 s');
assert.equal(formatUntil(iso(NOW+5*60000),NOW),'in 5 min');
assert.equal(formatUntil(iso(NOW+80*60000),NOW),'in 1 h 20 min');
assert.equal(formatUntil(iso(NOW+120*60000),NOW),'in 2 h');
assert.equal(formatUntil(iso(NOW-1000),NOW),'due now');
assert.equal(formatInterval(30),'30 s');assert.equal(formatInterval(120),'2 min');assert.equal(formatInterval(90),'90 s');

// ---- window formatting ----
assert.equal(daysSummary([1,2,3,4,5,6,7]),'Every day');
assert.equal(daysSummary([1,2,3,4,5]),'Mon–Fri');
assert.equal(daysSummary([1,3,6]),'Mon, Wed, Sat');
assert.equal(daysSummary([6,7]),'Sat, Sun');
assert.equal(daysSummary([1,2,3,5]),'Mon–Wed, Fri');
const hours=(enabled,start,end,days)=>heartbeatConfig({active_hours:{enabled,start,end},active_days:days});
assert.equal(windowSummary(hours(false,'08:00','22:00')),'Always active');
assert.equal(windowSummary(hours(true,'08:00','22:00',[1,2,3,4,5])),'08:00–22:00 GMT+7 · Mon–Fri');
assert.equal(windowSummary(hours(true,'22:00','06:00')),'22:00–06:00 GMT+7 (overnight) · Every day');
assert.equal(windowSummary(hours(true,'09:00','09:00')),'All day · Every day');
assert.equal(formatWindowOpen('2026-10-19T01:00:00Z'),'Mon 08:00 GMT+7');
assert.equal(formatWindowOpen(null),'');

// ---- status ----
const status=(extra={})=>({last_heartbeat_at:iso(NOW-12000),server_time:iso(NOW),interval_seconds:30,execution_enabled:true,active_now:true,quiet_hours:false,next_window_at:null,
  pulse:{enabled:false,last_pulse_at:null,last_result:null,last_reason:null,next_pulse_at:null},queued:1,running:2,waiting_owner:0,waiting_quota:0,...extra});
assert.equal(heartbeatState(null,NOW).key,'preview');
assert.deepEqual(heartbeatState(status(),NOW),{key:'running',label:'Running'});
assert.equal(heartbeatState(status({execution_enabled:false}),NOW).key,'paused');
assert.equal(heartbeatState(status({quiet_hours:true,active_now:false}),NOW).key,'quiet');
assert.equal(heartbeatState(status({last_heartbeat_at:null}),NOW).key,'offline');
assert.equal(heartbeatState(status({last_heartbeat_at:iso(NOW-200000)}),NOW).key,'offline');
assert.equal(beatIsStale(status({interval_seconds:300,last_heartbeat_at:iso(NOW-600000)}),NOW),false,'a 5 min interval tolerates a 10 min gap');
assert.equal(heartbeatState(status({execution_enabled:false,quiet_hours:true}),NOW).key,'paused','paused wins over quiet hours');
assert.equal(pulseNext(status(),NOW),'Off');
assert.equal(pulseNext(status({pulse:{enabled:true,next_pulse_at:iso(NOW+12*60000)}}),NOW),'Next in 12 min');
assert.equal(pulseNext(status({pulse:{enabled:true,next_pulse_at:null}}),NOW),'Starting on the next beat');
assert.equal(pulseNext(status({quiet_hours:true,next_window_at:'2026-10-19T01:00:00Z',pulse:{enabled:true}}),NOW),'Resumes Mon 08:00 GMT+7');
assert.equal(pulseLast(status(),NOW),'');
assert.equal(pulseLast(status({pulse:{enabled:true}}),NOW),'No pulse yet');
assert.equal(pulseLast(status({pulse:{enabled:true,last_pulse_at:iso(NOW-60000),last_result:'skipped',last_reason:'unchanged'}}),NOW),'Last pulse skipped (nothing changed) · 1 min ago');
assert.equal(pulseLast(status({pulse:{enabled:true,last_pulse_at:iso(NOW-9000),last_result:'done'}}),NOW),'Last pulse finished · 9 s ago');
assert.match(pulseMessage({started:true}),/started/);
assert.match(pulseMessage({started:false,reason:'nothing_open'}),/no model/);
assert.match(pulseMessage({started:false,reason:'running'}),/already running/);
assert.match(pulseMessage({started:false,reason:'agent_unavailable'}),/paused or archived/);
assert.equal(pulseMessage(undefined),'No pulse was started.');

// ---- token note ----
assert.equal(pulsesPerDay({pulse_every:60,hours_enabled:false}),24);
assert.equal(pulsesPerDay({pulse_every:15,hours_enabled:false}),96);
assert.equal(pulsesPerDay({pulse_every:1440,hours_enabled:false}),1);
assert.equal(pulsesPerDay({pulse_every:60,hours_enabled:true,hours_start:'08:00',hours_end:'22:00'}),14);
assert.equal(pulsesPerDay({pulse_every:60,hours_enabled:true,hours_start:'22:00',hours_end:'06:00'}),8,'overnight window');
assert.equal(pulsesPerDay({pulse_every:240,hours_enabled:true,hours_start:'09:00',hours_end:'10:00'}),1,'never below one');
assert.equal(pulsesPerDay({pulse_every:60,hours_enabled:true,hours_start:'09:00',hours_end:'09:00'}),24,'start == end is the whole day');
assert.match(tokenNote({pulse_enabled:false}),/team pulse is off/);
assert.match(tokenNote({pulse_enabled:true,pulse_every:60,pulse_changed:true}),/changed since the last pulse\. At most 24 pulse runs per day/);
assert.match(tokenNote({pulse_enabled:true,pulse_every:1440,pulse_changed:false}),/At most 1 pulse run per day/);

// ---- payload building ----
const values=(extra={})=>({execution_enabled:true,max_parallel:'4',turns_per_slice:'6',interval_seconds:'30',hours_enabled:true,hours_start:'22:00',hours_end:'06:00',
  days:['1','2','3','4','5'],pause_outside:true,pulse_enabled:true,pulse_agent:'a-lead',pulse_every:'60',pulse_changed:true,pulse_notify:'needed',pulse_prompt:'  Check the team.  ',...extra});
assert.deepEqual(heartbeatPayload(values()),{
  heartbeat:{interval_seconds:30,active_hours:{enabled:true,start:'22:00',end:'06:00'},active_days:[1,2,3,4,5],pause_schedules_outside_hours:true,
    pulse:{enabled:true,agent_id:'a-lead',every_minutes:60,only_if_changed:true,notify:'needed',prompt:'Check the team.'}},
  max_parallel:4,turns_per_slice:6,execution_enabled:true});
assert.equal('execution_enabled' in heartbeatPayload(values(),{omitExecution:true}),false);
assert.equal(heartbeatPayload(values({days:['7','1','1']})).heartbeat.active_days.join(),'1,7');
assert.deepEqual(heartbeatPayload(values({hours_enabled:false,days:[]})).heartbeat.active_days,[1,2,3,4,5,6,7],'days only matter while active hours are on');
for(const [extra,pattern] of [[{max_parallel:'0'},/Max concurrent agents/],[{max_parallel:'9'},/1 to 8/],[{max_parallel:'2.5'},/whole number/],[{turns_per_slice:'25'},/Steps per slice/],
  [{turns_per_slice:''},/Steps per slice/],[{interval_seconds:'5'},/heartbeat interval/],[{interval_seconds:undefined},/heartbeat interval/],[{pulse_every:'10'},/pulse interval/],
  [{hours_start:''},/HH:MM/],[{hours_end:'24:00'},/HH:MM/],[{days:[]},/weekday/],[{pulse_prompt:'   '},/cannot be empty/],[{pulse_prompt:'x'.repeat(4001)},/4,000/],[{pulse_notify:'never'},/message you/]])
  assert.throws(()=>heartbeatPayload(values(extra)),pattern,JSON.stringify(extra));

// ---- dialog markup ----
const agents=[{id:'a-lead',name:'Atlas',role:'Lead',heartbeat:true},{id:'a-seo',name:'Nova <b>',role:'SEO',heartbeat:false}];
const body=customizeBody({config:{interval_seconds:60,active_hours:{enabled:true,start:'22:00',end:'06:00'},active_days:[1,2,3],pulse:{enabled:true,every_minutes:240,notify:'always',prompt:'Custom <prompt>'}},
  settings:{execution_enabled:true,max_parallel:3,turns_per_slice:10},agents,esc,field});
for(const name of ['execution_enabled','max_parallel','turns_per_slice','interval_seconds','hours_enabled','hours_start','hours_end','days','pause_outside','pulse_enabled','pulse_every','pulse_changed','pulse_notify','pulse_prompt','agent_hb'])
  assert.ok(body.includes('name="'+name+'"'),name);
assert.ok(body.includes('name="max_parallel" value="3"')&&body.includes('name="turns_per_slice" value="10"'));
assert.ok(body.includes('name="interval_seconds" value="60" checked'));
assert.ok(body.includes('name="pulse_every" value="240" checked'));
assert.equal((body.match(/name="days" value="\d" checked/g)||[]).length,3);
assert.ok(body.includes('name="pulse_notify" value="always" checked'));
assert.ok(body.includes('Custom &lt;prompt&gt;')&&!body.includes('<prompt>'),'prompt is escaped');
assert.ok(body.includes('Nova &lt;b&gt;')&&body.includes('name="agent_hb" value="a-lead" checked')&&!body.includes('value="a-seo" checked'));
assert.ok(body.includes('more steps = fewer pauses, more tokens per slice')||body.includes('More steps = fewer pauses, more tokens per slice'));
assert.ok(body.includes('Pause scheduled work outside these hours')&&body.includes('Only when something changed (saves tokens)')&&body.includes('data-action="hb-prompt-reset"'));
assert.deepEqual([...body.matchAll(/name="interval_seconds" value="(\d+)"/g)].map(m=>m[1]),['10','15','30','60','120','300']);
assert.deepEqual([...body.matchAll(/name="pulse_every" value="(\d+)"/g)].map(m=>m[1]),['15','30','60','120','240','1440']);
assert.ok(customizeBody({config:undefined,settings:undefined,agents:[],esc,field}).includes('name="max_parallel" value="4"'),'preview defaults');

// ---- card markup ----
const card=(s,extra={})=>heartbeatCard({status:s,config:extra.config,settings:{execution_enabled:true,max_parallel:4,turns_per_slice:6,...(extra.settings||{})},mode:extra.mode||'local',esc,icon,button,nowMs:NOW});
let html=card(status());
assert.ok(html.includes('hb-state running')&&html.includes('Running')&&html.includes('12 s ago'));
assert.ok(html.includes('<dd>4</dd>')&&html.includes('<dd>6</dd>')&&html.includes('<dd>30 s</dd>')&&html.includes('<dd>Off</dd>'));
assert.ok(!html.includes('Up to 3 concurrent agents'));
assert.ok(html.includes('data-action="hb-customize"')&&html.includes('data-action="hb-pulse"')&&!/data-action="hb-pulse"[^>]*disabled/.test(html));
assert.ok(/<strong>2<\/strong><span>Running/.test(html)&&/<strong>1<\/strong><span>Queued/.test(html));
html=card(status({waiting_owner:2,waiting_quota:1}));
assert.ok(html.includes('class="warn"><strong>2</strong><span>Waiting for you')&&html.includes('class="warn"><strong>1</strong><span>Waiting for quota'));
html=card(status({pulse:{enabled:true,next_pulse_at:iso(NOW+300000),last_pulse_at:iso(NOW-3600000),last_result:'skipped',last_reason:'nothing_open'}}));
assert.ok(html.includes('Next in 5 min')&&html.includes('data-hb-kind="next"')&&html.includes('Last pulse skipped (nothing open) · 1 h ago'));
html=card(status({execution_enabled:false}),{settings:{execution_enabled:false}});
assert.ok(html.includes('hb-state paused')&&/data-action="hb-pulse"[^>]*disabled/.test(html)&&html.includes('Execution is paused'));
html=card(status({quiet_hours:true,active_now:false,next_window_at:'2026-10-19T01:00:00Z'}),{config:{active_hours:{enabled:true,start:'08:00',end:'22:00'}}});
assert.ok(html.includes('hb-state quiet')&&html.includes('Quiet hours · Scheduled work and the pulse resume Mon 08:00 GMT+7. Chats and running work continue.'));
html=card(status({last_heartbeat_at:null}));
assert.ok(html.includes('hb-state offline')&&html.includes('No recent beat'));
html=card(null,{mode:'demo'});
assert.ok(html.includes('hb-state preview')&&html.includes('Preview: keep the local app running')&&html.includes('<strong>—</strong>')&&/data-action="hb-pulse"[^>]*disabled/.test(html));
html=card(status({}),{config:{active_hours:{enabled:true,start:'08:00',end:'22:00'},active_days:[1,2,3,4,5]}});
assert.ok(html.includes('Active 08:00–22:00 GMT+7 · Mon–Fri.'));

// ---- signature ignores clocks, tracks the shown numbers ----
const sig=s=>heartbeatSignature({heartbeat:s,heartbeat_config:{interval_seconds:30}});
assert.equal(heartbeatSignature({}),'');
assert.equal(sig(status({server_time:'x',last_heartbeat_at:iso(NOW-5000)})),sig(status({server_time:'y',last_heartbeat_at:iso(NOW-1000)})));
assert.notEqual(sig(status()),sig(status({running:3})));
assert.notEqual(sig(status()),sig(status({quiet_hours:true,active_now:false})));

// ---- in-place clock updates ----
const els=[{dataset:{hbTime:iso(NOW-30000),hbKind:'ago'},textContent:''},{dataset:{hbTime:iso(NOW+600000),hbKind:'next'},textContent:''},{dataset:{hbTime:'',hbKind:'ago'},textContent:'keep'}];
tickTimes({querySelectorAll:()=>els},NOW);
assert.deepEqual(els.map(e=>e.textContent),['30 s ago','Next in 10 min','keep']);
console.log('heartbeat ui ok');
