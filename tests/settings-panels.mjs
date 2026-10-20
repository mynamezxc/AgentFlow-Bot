import assert from 'node:assert/strict';
import {budgetFromValues,housekeepingFromValues,formatBytes,budgetPanel,quotaPanel,fallbackListMarkup,storageMarkup,QUOTA_POLICIES} from '../dist/settings-panels.js';
import {tokenTotals,cacheHitPercent,budgetMarkup,budgetSummary,formatTokens,projectRoot,budgetSettings} from '../dist/budget-ui.js';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const field=(name,label,value)=>'<div data-field="'+name+'" data-value="'+esc(value)+'">'+esc(label)+'</div>';
const button=(text,action,id='',cls='')=>'<button class="button '+cls+'" data-action="'+action+'" data-id="'+esc(id)+'">'+esc(text)+'</button>';

// Budget form <-> settings contract
assert.deepEqual(budgetFromValues({run_turns:'24',project_turns:'64',project_runs:'24',project_tokens_k:'500'}),{run_turns:24,project_turns:64,project_runs:24,project_tokens:500000});
assert.equal(budgetFromValues({run_turns:'1',project_turns:'2',project_runs:'3',project_tokens_k:''}).project_tokens,0);
assert.throws(()=>budgetFromValues({run_turns:'0',project_turns:'64',project_runs:'24',project_tokens_k:'0'}),/Turns per agent run/);
assert.throws(()=>budgetFromValues({run_turns:'2.5',project_turns:'64',project_runs:'24',project_tokens_k:'0'}));
assert.throws(()=>budgetFromValues({run_turns:'2',project_turns:'64',project_runs:'24',project_tokens_k:'-1'}),/Token budget/);
assert.deepEqual(housekeepingFromValues({auto_cleanup:true,min_free_gb:'5',retention_days:'14'}),{auto_cleanup:true,min_free_gb:5,retention_days:14});
assert.throws(()=>housekeepingFromValues({min_free_gb:'-1',retention_days:'14'}));
assert.throws(()=>housekeepingFromValues({min_free_gb:'5',retention_days:'0'}));
let html=budgetPanel({budget:{run_turns:10,project_turns:30,project_runs:8,project_tokens:2500000}},field,esc);
for(const name of ['run_turns','project_turns','project_runs','project_tokens_k']) assert.ok(html.includes('data-field="'+name+'"'));
assert.ok(html.includes('data-value="2500"'));assert.ok(html.includes('id="budget-form"'));
assert.equal(budgetSettings(undefined).project_turns,64);
// Quota policy + ordered fallbacks
assert.deepEqual(QUOTA_POLICIES.map(p=>p[0]),['ask','auto_fallback','wait']);
const catalog=[{provider:'codex',id:'gpt-a',name:'GPT A'},{provider:'claude',id:'sonnet',name:'Sonnet'},{provider:'claude',id:'haiku',name:'Haiku'}];
const list=fallbackListMarkup([{provider:'claude',model:'sonnet'},{provider:'codex',model:'gpt-a'}],catalog,esc);
assert.ok(list.indexOf('Claude · Sonnet')<list.indexOf('Codex · GPT A'));
assert.ok(list.includes('data-action="fb-up" data-id="0" aria-label="Move Claude · Sonnet up" disabled'));
assert.ok(list.includes('Claude · Haiku</option>')&&!list.includes('Sonnet</option>'));
assert.ok(fallbackListMarkup([],[],esc).includes('Load a model catalog in Connections first'));
assert.ok(fallbackListMarkup([],catalog,esc).includes('No fallback models yet'));
assert.ok(quotaPanel({quota_policy:'wait'},[],catalog,field,esc).includes('data-value="wait"'));
// Storage section
const state={total_bytes:3.5*1073741824,free_bytes:2*1073741824,disk_total_bytes:500*1073741824,categories:[{id:'cache',label:'Provider cache',description:'Rebuilt on demand',bytes:2048,files:3,auto:true},{id:'runs',label:'Old run files',bytes:0,files:0,auto:false}],last_cleanup:{at:1792405923,freed_bytes:1048576,files:12},settings:{auto_cleanup:true,min_free_gb:5,retention_days:14}};
html=storageMarkup(state,field,esc,button);
assert.ok(html.includes('3.5 GB')&&html.includes('2.0 GB')&&html.includes('of 500 GB'));
assert.ok(html.includes('storage-badge auto">Auto')&&html.includes('storage-badge ask">Ask first'));
assert.ok(html.includes('value="cache" aria-label="Select Provider cache"')&&/value="runs"[^>]* disabled/.test(html));
assert.ok(html.includes('1.0 MB freed')&&html.includes('GMT+7'));assert.ok(html.includes('data-action="storage-clean"'));
assert.ok(html.includes('is-low'));assert.ok(html.includes('type="checkbox" name="auto_cleanup" checked'));
assert.ok(storageMarkup({...state,last_cleanup:null},field,esc,button).includes('Never'));
assert.ok(storageMarkup(null,field,esc,button).includes('Checking disk usage'));
assert.equal(formatBytes(0),'0 B');assert.equal(formatBytes(1536),'1.5 KB');assert.equal(formatBytes('x'),'0 B');
console.log('Settings panels: assertions passed');
// Context budget panel
const runs=[{id:'r1',project_id:'p',root_id:'r1',status:'running',family_attempts:16,family_tokens:420000,token_usage:{input_tokens:1000,cached_input_tokens:9000,cache_creation_tokens:0,output_tokens:500,turns:4}},{id:'r2',project_id:'p',root_id:'r1',status:'done',token_usage:{input_tokens:1000,cached_input_tokens:1000,output_tokens:100,turns:1}}];
const total=tokenTotals(runs);
assert.deepEqual(total,{input:2000,cached:10000,created:0,output:600,turns:5});
assert.equal(cacheHitPercent(total),83);assert.equal(cacheHitPercent({input:0,cached:0}),null);
assert.equal(projectRoot(runs,'p').id,'r1');assert.equal(projectRoot(runs,'other'),null);
const root=runs[0];
html=budgetMarkup({root,settings:{budget:{project_turns:20,project_tokens:500000}},runs},esc);
assert.ok(html.includes('16 / 20')&&html.includes('420k / 500k'));assert.ok(html.includes('budget-meter warn'));
assert.ok(html.includes('Cache hit')&&html.includes('83%'));assert.ok(html.includes('>2k<')&&html.includes('>10k<'));
assert.ok(budgetMarkup({root,settings:{},runs},esc).includes('no limit'));
html=budgetMarkup({root:{...root,budget_unlimited:true},settings:{},runs},esc);
assert.ok(html.includes('Unlimited for this task'));assert.ok(!html.includes('<progress'));
assert.ok(budgetMarkup({root:null,settings:{},runs:[]},esc).includes('appear once'));
assert.equal(budgetSummary(root,{budget:{project_turns:20}}),'16/20');assert.equal(budgetSummary({...root,budget_unlimited:true},{}),'Unlimited');
assert.equal(formatTokens(950),'950');assert.equal(formatTokens(12500),'12.5k');assert.equal(formatTokens(2400000),'2.4M');
console.log('Budget context: assertions passed');
