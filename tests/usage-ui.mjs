import assert from 'node:assert/strict';
import {usageMini,usageDetails,withRetained,selectWindows,usagePlanBadge,planLabel} from '../dist/usage-ui.js';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const p={id:'codex',account_label:'Current account',usage:{windows:[{id:'codex:primary',label:'codex',used_percent:37,window_minutes:300,resets_at:4102444800}],observed_at:'2026-10-19T05:43:00Z',notice:'Observed native metadata'}};
const mini=usageMini([p],esc),details=usageDetails(p,esc);
assert.ok(mini.includes('<b>63%</b>'));
assert.ok(mini.includes('Codex usage limits. '));
assert.ok(mini.includes('Claude Code usage limits'));
assert.ok(mini.includes('aria-describedby="usage-popover"'));
assert.ok(mini.includes('<template data-usage-content>'));
assert.ok(details.includes('<progress max="100" value="63"'));
assert.ok(details.includes('GMT+7'));
assert.ok(details.includes('07:00'));
const expiredDetails=usageDetails({usage:{windows:[{id:'session',used_percent:100,expired:true}]}},esc);
assert.ok(expiredDetails.includes('Reset · refreshing'));
assert.ok(expiredDetails.includes('<progress max="100" value="100"'));
assert.ok(!expiredDetails.includes('100% used'));
assert.ok(usageMini([],esc).includes('—'));
assert.ok(!usageDetails({usage:{notice:'<script>bad()</script>'}},esc).includes('<script>'));
console.log('10 quota UI assertions passed');
const multi={id:'codex',usage:{windows:[{id:'codex:primary',used_percent:12,window_minutes:300},{id:'codex:secondary',used_percent:90,window_minutes:10080},{id:'other:primary',used_percent:99}]}};
assert.ok(usageMini([multi],esc).includes('<b>88%</b>'));
// Weekly window at exactly 10% left is NOT low (rule is strictly under 10%): the strip stays at the 5-hour window only.
assert.ok(!usageMini([multi],esc).includes('<b>10%</b>'));
assert.ok(!usageMini([multi],esc).includes('is-low'));
assert.ok(usageMini([multi],esc).includes('<span>5h</span>'));
assert.ok(usageMini([{id:'codex',usage:{windows:[{id:'codex:primary',used_percent:80,resets_at:1}]}}],esc).includes('<b>Reset</b>'));
assert.ok(usageDetails({usage:{source:'Native quota',updating:true}},esc).includes('Refreshing in the background'));
assert.ok(usageMini([{id:'claude',usage:{windows:[{id:'five_hour',used_percent:49,window_minutes:300}]}}],esc).includes('<b>51%</b>'));
console.log('6 account quota regression assertions passed');
assert.ok(details.includes('37% used'));
assert.ok(!usageMini([multi],esc).includes('<b>1%</b>'));
const custom={id:'codex',usage:{default_bucket:'business',windows:[{id:'business:primary',bucket:'business',used_percent:4,window_minutes:10080}]}};
assert.ok(usageMini([custom],esc).includes('<b>96%</b>'));
assert.ok(usageMini([custom],esc).includes('<span>7d</span>'));
assert.ok(usageDetails(custom,esc).includes('7-day limit'));
assert.ok(usageMini([{id:'codex',usage:{windows:[{id:'codex:primary',used_percent:null}]}}],esc).includes('<b>—</b>'));
assert.ok(!usageMini([{id:'codex',usage:{windows:[{id:'codex:primary',used_percent:NaN}]}}],esc).includes('NaN'));
console.log('7 remaining-quota and window-label assertions passed');

// SPEC contract 7: windows stay visible while updating; Checking… only without windows; no blank between polls.
const upd={id:'codex',account_label:'A',usage:{windows:[{id:'codex:primary',used_percent:20,window_minutes:300}],updating:true,state:'observed'}};
const updMini=usageMini([upd],esc);
assert.ok(updMini.includes('<b>80%</b>'));
assert.ok(updMini.includes('usage-sync'));
assert.ok(!updMini.includes('Checking'));
assert.ok(!usageMini([{id:'codex',usage:{windows:[{id:'codex:primary',used_percent:20,window_minutes:300}],updating:false}}],esc).includes('usage-sync'));
assert.ok(usageMini([{id:'codex',usage:{windows:[],state:'pending',updating:true}}],esc).includes('Checking…'));
assert.ok(!usageMini([{id:'codex',usage:{windows:[],state:'unavailable'}}],esc).includes('Checking…'));
assert.ok(usageDetails(upd,esc).includes('usage-refreshing'));
const expiredMini=usageMini([{id:'codex',usage:{windows:[{id:'codex:primary',used_percent:99,window_minutes:300,resets_at:1}]}}],esc);
assert.ok(expiredMini.includes('Reset · refreshing'));
assert.ok(expiredMini.includes('width:100%'));
console.log('9 usage refresh-state assertions passed');
// Client-side retention: pending refresh without windows keeps the last confirmed ones (same account only).
withRetained([{id:'claude',account_label:'me',usage:{windows:[{id:'five_hour',used_percent:10,window_minutes:300}]}}]);
const kept=withRetained([{id:'claude',account_label:'me',usage:{windows:[],state:'pending',updating:true}}])[0];
assert.equal(kept.usage.windows.length,1);
assert.ok(usageMini([kept],esc).includes('<b>~90%</b>'));
assert.equal(withRetained([{id:'claude',account_label:'someone-else',usage:{windows:[],state:'pending'}}])[0].usage.windows.length,0);
// Same account: even an 'unavailable' poll keeps the last confirmed values (marked stale) instead of blanking the strip.
assert.equal(withRetained([{id:'claude',account_label:'me',usage:{windows:[],state:'unavailable'}}])[0].usage.windows.length,1);
assert.equal(withRetained([{id:'claude',account_label:'someone-else',usage:{windows:[],state:'unavailable'}}])[0].usage.windows.length,0);
console.log('5 usage retention assertions passed');

// Owner rule: only the 5-hour window, weekly joins with a warning icon when < 10% is left (Codex and Claude alike).
const win=(id,used,minutes,extra={})=>({id,used_percent:used,window_minutes:minutes,resets_at:Math.floor(Date.now()/1000)+7200,...extra});
const codexOk={id:'codex',usage:{windows:[win('codex:primary',38,300),win('codex:secondary',40,10080)]}};
const okMini=usageMini([codexOk],esc);
assert.deepEqual(selectWindows(codexOk).primary.id,'codex:primary');
assert.equal(selectWindows(codexOk).extra.length,0);
assert.ok(okMini.includes('<b>62%</b>'));
assert.ok(!okMini.includes('data-window="7d"'));
assert.ok(!okMini.includes('usage-warn'));
const codexLow={id:'codex',usage:{windows:[win('codex:primary',38,300),win('codex:secondary',92,10080)]}};
assert.deepEqual(selectWindows(codexLow).extra.map(w=>w.id),['codex:secondary']);
const lowMini=usageMini([codexLow],esc);
assert.ok(lowMini.includes('data-window="7d"'));
assert.ok(lowMini.includes('usage-warn'));
assert.ok(lowMini.includes('<b>8%</b>'));
assert.ok(lowMini.includes('(low)'));
const claudeOk={id:'claude',usage:{windows:[win('five_hour',22,300),win('seven_day',85,10080)]}};
assert.equal(selectWindows(claudeOk).extra.length,0);
assert.ok(!usageMini([claudeOk],esc).includes('usage-warn'));
const claudeLow={id:'claude',usage:{windows:[win('five_hour',22,300),win('seven_day',95,10080),win('seven_day_opus',99,10080),win('seven_day_sonnet',10,10080)]}};
assert.deepEqual(selectWindows(claudeLow).extra.map(w=>w.id),['seven_day','seven_day_opus']);
assert.equal((usageMini([claudeLow],esc).match(/usage-warn/g)||[]).length,2);
// Expired (already reset) and unknown weekly windows are never "low".
assert.equal(selectWindows({id:'claude',usage:{windows:[win('five_hour',10,300),win('seven_day',99,10080,{resets_at:1})]}}).extra.length,0);
assert.equal(selectWindows({id:'claude',usage:{windows:[win('five_hour',10,300),win('seven_day',null,10080)]}}).extra.length,0);
// No 5-hour window reported: the shortest available stands in; never blank.
assert.equal(selectWindows({id:'claude',usage:{windows:[win('seven_day',50,10080)]}}).primary.id,'seven_day');
assert.equal(selectWindows({id:'codex',usage:{windows:[]}}).primary,null);
// Details/popover still carry every window, in duration order.
const full=usageDetails(claudeLow,esc);
assert.ok(full.indexOf('5-hour')<full.indexOf('7-day'));
assert.equal((full.match(/class="usage-window"/g)||[]).length,4);
assert.ok(usageMini([codexLow],esc).includes('data-usage-content'));
assert.ok(usageMini([codexLow],esc).includes('weekly limit joins it'));
// One compact line: both providers in a single strip; the sync dot slot is always reserved (no width jump).
const strip=usageMini([codexOk,claudeOk],esc);
assert.equal((strip.match(/class="usage-minis"/g)||[]).length,1);
assert.equal((strip.match(/class="usage-slot"/g)||[]).length,2);
assert.ok(strip.indexOf('>Codex<')<strip.indexOf('>Claude<'));
// Escaping.
assert.ok(!usageMini([{id:'codex',account_label:'<img onerror=x>',usage:{windows:[win('codex:primary',1,300)]}}],esc).includes('<img onerror'));
console.log('26 compact usage-line assertions passed');
// Owner request: plan is a floating badge, and noisy source/notice lines are gone (source lives in a tooltip).
const quiet={id:'codex',account_label:'Current account',usage:{plan_type:'plus',source:'Codex account/rateLimits/read',notice:'',observed_at:'2026-10-19T05:43:00Z',
  windows:[{id:'codex:primary',used_percent:20,window_minutes:300,resets_at:4102444800}]}};
const quietDetails=usageDetails(quiet,esc),quietMini=usageMini([quiet],esc);
const visible=html=>html.replace(/ title="[^"]*"/g,'');
for(const noise of ['Plan:','Source:','no inference','Quota reported by','No quota observation']) assert.ok(!visible(quietDetails).includes(noise)&&!visible(quietMini).includes(noise),noise);
assert.ok(quietDetails.includes('title="Source: Codex account/rateLimits/read"'));
assert.ok(quietDetails.includes('Updated: ')&&quietDetails.includes('GMT+7'));
assert.equal(planLabel('plus'),'Plus');assert.equal(planLabel('pro_lite'),'Pro Lite');assert.equal(planLabel(''),'');assert.equal(planLabel(null),'');
assert.equal(usagePlanBadge(quiet,esc),'<span class="usage-plan" title="Subscription plan">Plus</span>');
assert.equal(usagePlanBadge({usage:{}},esc),'');assert.ok(!usagePlanBadge({usage:{plan_type:'<b>'}},esc).includes('<b>'));
assert.ok(quietMini.includes('<template data-usage-content><span class="usage-plan"'));
assert.ok(!usageMini([{id:'codex',usage:{windows:[]}}],esc).includes('usage-plan'));
const errored=usageDetails({usage:{state:'unavailable',windows:[],notice:'Account could not be rechecked.'}},esc);
assert.ok(errored.includes('Quota data unavailable.')&&errored.includes('Account could not be rechecked.'));
assert.ok(usageDetails({usage:{state:'pending',windows:[],notice:'Checking the current account.'}},esc).includes('Checking account limits…')&&!usageDetails({usage:{state:'pending',windows:[],notice:'Checking the current account.'}},esc).includes('Checking the current account.'));
console.log('plan badge and quiet usage assertions passed');

// Claude usage must not vanish (rate limiting, events without utilization).
{
  const now=Math.floor(Date.now()/1000),next=now+900;
  const claude=(usage,extra={})=>({id:'claude',account_label:'me',usage:{state:'observed',observed_at:new Date().toISOString(),windows:[],...usage},...extra});
  const five={id:'five_hour',used_percent:37,window_minutes:300,resets_at:now+7200};
  // 429: the last windows stay, no warning text, a tooltip and "next check HH:MM" in the popover only.
  const limited=claude({windows:[five],rate_limited:true,next_check_epoch:next,rate_notice:'Claude usage endpoint rate-limited; showing last confirmed usage.',stale:false});
  const mini429=usageMini([limited],esc),details429=usageDetails(limited,esc);
  assert.ok(mini429.includes('<b>63%</b>'));
  assert.ok(!/<b>—<\/b>/.test(mini429.split('data-provider="claude"')[1]));
  assert.ok(!details429.includes('usage-warn'));
  assert.ok(details429.includes('rate-limited')&&details429.includes('title="'));
  assert.ok(details429.includes(' · next check '));
  assert.ok(/Updated: \d\d:\d\d GMT\+7 · next check \d\d:\d\d/.test(details429));
  assert.ok(!details429.includes('Last confirmed'));
  // Not rate limited: no next-check text.
  assert.ok(!usageDetails(claude({windows:[five]}),esc).includes('next check'));
  // Old values stay visible with the subtle stale style and a "~" mark.
  const aged=claude({windows:[{...five,aged:true,stale:true}],stale:true});
  const agedMini=usageMini([aged],esc);
  assert.ok(agedMini.includes('usage-mini-window is-stale'));
  assert.ok(agedMini.includes('<b>~63%</b>'));
  assert.ok(!usageMini([claude({windows:[five]})],esc).includes('is-stale'));
  assert.ok(usageMini([claude({windows:[{...five,estimated:true}]})],esc).includes('<b>~63%</b>'));
  assert.ok(usageDetails(aged,esc).includes('37% used · Last confirmed'));
  // An expired window reads "Reset" until the refresh arrives.
  const expired=usageMini([claude({windows:[{...five,resets_at:now-5,expired:true,used_percent:0}]})],esc);
  assert.ok(expired.includes('<b>Reset</b>'));
  assert.ok(expired.includes('width:100%'));
  // Never "—" while any window is known: a weekly window stands in for a missing five-hour percentage, a status-only window reads OK.
  const weeklyOnly=usageMini([claude({windows:[{id:'five_hour',used_percent:null,status:'allowed',resets_at:now+60},{id:'seven_day',used_percent:12,window_minutes:10080,resets_at:now+86400}]})],esc);
  assert.ok(weeklyOnly.includes('<b>88%</b>')&&weeklyOnly.includes('data-window="7d"'));
  const statusOnly=usageMini([claude({windows:[{id:'five_hour',used_percent:null,status:'allowed',resets_at:now+3600}]})],esc);
  assert.ok(statusOnly.includes('<b>OK</b>'));
  assert.ok(!/<b>—<\/b>/.test(statusOnly.split('data-provider="claude"')[1]));
  assert.ok(usageDetails(claude({windows:[{id:'five_hour',used_percent:null,status:'allowed',resets_at:now+3600}]}),esc).includes('Status: allowed'));
  // A window id the strip does not know (overage) is still shown instead of a blank.
  const odd=usageMini([claude({windows:[{id:'overage',used_percent:30,resets_at:now+3600}]})],esc);
  assert.ok(odd.includes('<b>70%</b>'));
  assert.equal(selectWindows(claude({windows:[{id:'overage',used_percent:30}]})).primary.id,'overage');
  // Right after a restart the persisted windows arrive with the checking notice: still shown, never blank.
  const startup=usageMini([claude({windows:[five],updating:true,notice:'Checking the current Claude account; showing last confirmed usage.'})],esc);
  assert.ok(startup.includes('<b>63%</b>')&&!startup.includes('Checking…'));
  console.log('Claude usage resilience assertions passed');
}
