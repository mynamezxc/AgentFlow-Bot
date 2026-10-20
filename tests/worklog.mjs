import assert from 'node:assert/strict';
import {isActivity,actionsOf,logSummary,renderWorkLog} from '../dist/worklog.js';
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
