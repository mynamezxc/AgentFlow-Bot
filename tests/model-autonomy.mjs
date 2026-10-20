import assert from 'node:assert/strict';
import {MODEL_AUTONOMY,MODEL_SOURCES,modelText,modelEventView,modelHistoryMarkup,modelAutonomyPanel} from '../dist/settings-panels.js';

const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const button=(label,action,id='',cls='')=>'<button data-action="'+action+'" class="'+cls+'">'+esc(label)+'</button>';
const names={'a-lead':'Atlas','a-code':'Coder'};

assert.deepEqual(MODEL_AUTONOMY.map(m=>m[0]),['locked','phases','full']);
assert.ok(MODEL_AUTONOMY[1][1].includes('recommended'));
assert.equal(modelText({provider:'codex',model:'gpt-6.1-sol',reasoning_effort:'high'}),'codex/gpt-6.1-sol · high');
assert.equal(modelText({provider:'',model:''}),'not set');
assert.equal(modelText(null),'not set');

let v=modelEventView({kind:'team_default',source:'quota_switch',actor_id:null,created_at:'2026-10-19T03:00:00Z',previous:{provider:'codex',model:'a'},binding:{provider:'claude',model:'sonnet'},reason:'quota fallback'},names);
assert.equal(v.change,'codex/a → claude/sonnet');
assert.equal(v.source,'Quota fallback');assert.equal(v.who,'System');assert.ok(v.when.includes('GMT+7'));
v=modelEventView({kind:'agent_model',source:'agent_tool',actor_id:'a-lead',agent_id:'a-code',previous:{provider:'codex',model:'a'},binding:{provider:'codex',model:'b'}},names);
assert.equal(v.change,'Coder: codex/a → codex/b');assert.equal(v.who,'Atlas');
v=modelEventView({kind:'team_default',source:'owner_chat',previous:{},binding:{provider:'claude',model:'opus'}},names);
assert.equal(v.who,'Owner');assert.equal(v.source,MODEL_SOURCES.owner_chat);

let html=modelHistoryMarkup(null,names,esc);
assert.ok(html.includes('Checking'));
html=modelHistoryMarkup({events:[],diverged:[]},names,esc);
assert.ok(html.includes('Every agent follows the team default.'));assert.ok(html.includes('No changes recorded yet.'));
assert.ok(html.includes('<details class="model-history"><summary>Recent changes (0)'));assert.ok(!/<details[^>]*\bopen\b/.test(html));
html=modelHistoryMarkup({events:[{kind:'team_default',source:'owner_settings',previous:{provider:'codex',model:'a'},binding:{provider:'codex',model:'b'},created_at:'2026-10-19T03:00:00Z'}],
  diverged:[{agent_id:'a-code',name:'Coder',binding:{provider:'claude',model:'opus'},binding_origin:'agent',set_by:'a-lead'},{agent_id:'a-x',name:'<b>X</b>',binding:{provider:'codex',model:'c'},binding_origin:'owner',set_by:'owner'}]},names,esc);
assert.ok(html.includes('data-action="model-reset" data-id="a-code"'));
assert.ok(html.includes('Set by Atlas'));assert.ok(html.includes('Pinned by you'));assert.ok(html.includes('&lt;b&gt;X&lt;/b&gt;'));
assert.ok(html.includes('Recent changes (1)'));

let panel=modelAutonomyPanel({default_provider:'codex',default_model:'gpt-6.1-sol'},null,names,esc,button);
assert.ok(panel.includes('codex/gpt-6.1-sol'));
assert.ok(/value="phases" checked/.test(panel),'phases is the default');
assert.equal((panel.match(/name="model_autonomy"/g)||[]).length,3);
assert.ok(panel.includes('id="model-history-slot"'));
panel=modelAutonomyPanel({model_autonomy:'locked'},null,names,esc,button,false);
assert.ok(/value="locked" checked/.test(panel)&&!/value="phases" checked/.test(panel));
assert.ok(!panel.includes('model-history-slot'));assert.ok(panel.includes('Not configured'));
assert.ok(/value="phases"/.test(modelAutonomyPanel({model_autonomy:'bogus'},null,names,esc,button))&&/value="phases" checked/.test(modelAutonomyPanel({model_autonomy:'bogus'},null,names,esc,button)));
console.log('model autonomy assertions passed');
