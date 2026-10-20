import assert from 'node:assert/strict';
import {decisionCard,decisionActions,actionPayload,decisionBar,decisionBarLabel,stepIndex,isLongText,decisionInfo,resolvedAnswer,groupDecisions} from '../dist/decisions.js';
import {messageFilesMarkup,resolveFiles,fileKind,formatSize} from '../dist/message-files.js';
import {wrapIndex,counterText,swipeDirection} from '../dist/lightbox.js';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=n=>'<svg data-i="'+n+'"></svg>';
const h={esc,icon,avatar:(a,size)=>'<span class="avatar '+size+'">'+esc(a.name[0])+'</span>',field:(name)=>'<input name="'+name+'">',actor:{id:'a1',name:'Atlas'},title:t=>esc(t),context:t=>'<p>'+esc(t)+'</p>'};
const base={id:'d1',status:'open',agent_id:'a1',question:'Which <plan>?',options:['Ship now','Wait']};

// Compact open card: one header line, 1-2 line question, Details toggle, pills, reply only on demand.
let html=decisionCard(base,h);
assert.ok(html.includes('class="decision-card dc"'));
assert.ok(html.includes('class="dc-head"'));assert.ok(html.includes('<strong>Atlas</strong>'));assert.ok(html.includes('avatar mini'));
assert.ok(html.includes('dc-badge ask">Needs you</span>'));
assert.ok(html.includes('class="dc-q"'));assert.ok(html.includes('Which &lt;plan&gt;?'));assert.ok(!html.includes('<plan>'));
assert.equal((html.match(/data-action="co-answer"/g)||[]).length,2);assert.ok(!html.includes('opt-key'));
assert.ok(html.includes('data-action="co-decision-reply"')&&html.includes('Reply…'));
assert.ok(/<div class="dc-reply" id="dc-reply-d1" hidden>/.test(html));
assert.ok(!html.includes('Blocking')&&!html.includes('dc-details')&&!html.includes('dc-mono'));
// Reply opened (state kept across re-render) or no options: input visible.
assert.ok(!/id="dc-reply-d1" hidden/.test(decisionCard(base,{...h,replyOpen:id=>id==='d1'})));
html=decisionCard({...base,options:[]},h);assert.ok(!html.includes('co-decision-reply')&&!/dc-reply[^>]*hidden/.test(html)&&html.includes('name="answer"'));
// Blocking dot + highlight class.
html=decisionCard({...base,blocking:true},h);
assert.ok(html.includes('class="dc-dot"')&&html.includes('aria-label="Blocking"'));assert.ok(html.includes('is-blocking'));
// Context always hides behind the Details toggle; long question text moves there too.
assert.ok(decisionCard({...base,context:'short note'},h).includes('<summary>Details'));
assert.ok(decisionCard({...base,context:'x'.repeat(300)},h).includes('dc-details'));
assert.ok(!decisionCard(base,h).includes('dc-details'));
assert.ok(decisionCard({...base,question:'Q'.repeat(200)},h).includes('dc-full'));
assert.ok(isLongText('1\n2\n3\n4')&&!isLongText('1\n2\n3'));
// Permission cards: badge + single monospace subject line.
const cmd={...base,command_request:true,command:['git','commit','-m','a b'],command_cwd:'C:/x',actions:undefined};
html=decisionCard(cmd,h);
assert.ok(html.includes('dc-badge perm">Permission</span>')&&html.includes('is-permission'));
assert.ok(html.includes('<code class="dc-mono"')&&html.includes('git commit -m &quot;a b&quot;'));
assert.deepEqual(decisionInfo({...base,permission_request:true,requested_path:'C:/Docs'}),{permission:true,label:'Permission',subject:'C:/Docs',hint:''});
assert.equal(decisionInfo({...base,browser_origin:'https://shop.example',browser_access:'interact'}).subject,'https://shop.example');
assert.equal(decisionInfo(base).label,'Needs you');
// Generic actions render as buttons posting {answer: label, [action_field]: value}.
const generic={...base,options:['Wait for reset'],actions:[{value:'wait',label:'Wait for reset',style:'primary',hint:'Resume at 14:05'},{value:'cancel',label:'Cancel work',style:'danger'},{value:'x',label:'Other',style:'weird'}],action_field:'quota_action',action_handler:'quota'};
html=decisionCard(generic,h);
assert.equal((html.match(/data-action="co-decision-action"/g)||[]).length,3);
assert.ok(html.includes('dc-btn primary')&&html.includes('dc-btn danger')&&html.includes('dc-btn quiet')&&html.includes('data-value="wait"'));
assert.ok(html.includes('title="Resume at 14:05"'));assert.ok(!html.includes('dc-reply'));assert.ok(!html.includes('co-answer'));
assert.deepEqual(actionPayload(generic,'wait'),{answer:'Wait for reset',quota_action:'wait'});
assert.deepEqual(actionPayload(generic,'cancel'),{answer:'Cancel work',quota_action:'cancel'});
assert.equal(actionPayload(generic,'bogus'),null);
assert.equal(decisionActions({...generic,actions:[{value:'a'}]}),null);
// Legacy special cases keep working without `actions`.
assert.deepEqual(actionPayload({...base,command_request:{cmd:'ls'}},'session'),{answer:'Allow all commands in this session (15 minutes)',command_action:'session'});
assert.deepEqual(actionPayload({...base,safety_request:{}},'continue'),{answer:'Continue with a new budget',safety_action:'continue'});
assert.equal(decisionCard({...base,command_request:{}},h).split('co-decision-action').length-1,3);
assert.equal(decisionCard({...base,safety_request:{}},h).split('co-decision-action').length-1,2);
assert.ok(decisionCard({...base,kind:'model_config'},h).includes('href="#connections"'));
const folder=decisionCard({...base,kind:'folder_access',requested_path:'C:/x'},h);assert.ok(folder.includes('name="folder_path"')&&folder.includes('Grant access')&&folder.includes('dc-badge perm'));
// Resolved decisions: ONE compact line with the question, a "You: <answer>" chip and the time.
const when=iso=>'09:41';
html=decisionCard({...base,status:'answered',answer:'Ship now',answered_at:'2026-10-19T02:41:00Z'},{...h,time:when});
assert.ok(html.startsWith('<details class="resolved-decision"')&&html.includes('rd-answer'));// every answered Q&A expandsassert.ok(html.includes('class="rd-q">Which &lt;plan&gt;?</span>'));
assert.ok(html.includes('class="rd-chip"')&&html.includes('You: Ship now'));assert.ok(html.includes('<time class="rd-time">09:41</time>'));
assert.ok(!html.includes('<form')&&!html.includes('Decision ·'));
// Context / long question make the row an expandable <details>.
html=decisionCard({...base,status:'answered',answer:'Ship now',context:'Because'},h);
assert.ok(html.startsWith('<details class="resolved-decision"')&&html.includes('rd-context')&&html.includes('data-key="resolved:d1"'));
assert.ok(decisionCard({...base,status:'answered',answer:'x',question:'Q'.repeat(120)},h).includes('rd-full'));
// Action decisions show the chosen action label, never the raw typed text / value.
const act={...base,status:'answered',answer:'allow',command_request:true,command_action:'session'};
assert.equal(resolvedAnswer(act),'Allow all commands in this session (15 minutes)');
assert.equal(resolvedAnswer({...generic,status:'answered',answer:'2'}),'Cancel work');
assert.equal(resolvedAnswer({...generic,status:'answered',answer:'x',quota_action:'wait'}),'Wait for reset');
assert.equal(resolvedAnswer({...base,status:'answered',answer:'typed words'}),'typed words');
assert.equal(resolvedAnswer({...base,status:'answered'}),'Resolved');
assert.ok(decisionCard({...generic,status:'answered',answer:'Cancel work',quota_action:'cancel'},h).includes('You: Cancel work'));
assert.equal(decisionCard(null,h),'');
// Grouping: 2+ consecutive open cards share one container; first 3 visible, the rest behind "Show N more".
const item=(id,open=true)=>({id,open,html:'<form class="decision-card" data-decision="'+id+'">'+id+'</form>'});
assert.ok(!groupDecisions([item('a')],{esc}).includes('decision-group'));
let grouped=groupDecisions([item('a'),item('b')],{esc});
assert.ok(grouped.includes('class="decision-group"')&&!grouped.includes('hidden')&&!grouped.includes('Show '));
grouped=groupDecisions([item('a'),item('b'),item('c'),item('d'),item('e')],{esc});
assert.equal((grouped.match(/<form hidden /g)||[]).length,2);assert.ok(grouped.includes('Show 2 more')&&grouped.includes('data-action="co-decision-more"')&&grouped.includes('data-id="a"'));
grouped=groupDecisions([item('a'),item('b'),item('c'),item('d'),item('e')],{esc,expanded:k=>k==='a'});
assert.ok(!grouped.includes('<form hidden')&&grouped.includes('Show fewer'));
// Resolved rows, messages and empty html break/skip runs; two separate runs make two groups.
const msg={html:'<article>m</article>',open:false};
grouped=groupDecisions([item('a'),item('b'),msg,item('c'),item('d'),{html:'',open:false}],{esc});
assert.equal((grouped.match(/class="decision-group"/g)||[]).length,2);assert.ok(grouped.indexOf('<article>')>grouped.indexOf('data-decision="b"')&&grouped.indexOf('<article>')<grouped.indexOf('data-decision="c"'));
assert.ok(groupDecisions([item('a'),{html:'<div class="resolved-decision"></div>',open:false},item('b')],{esc}).split('decision-group"').length===1);
// Pending bar.
assert.equal(decisionBar(0,0,icon,esc),'');
assert.equal(decisionBarLabel(1),'1 decision needs you');assert.equal(decisionBarLabel(3),'3 decisions need you');
const bar=decisionBar(3,1,icon,esc);
assert.ok(bar.includes('3 decisions need you'));assert.ok(bar.includes('co-decision-prev')&&bar.includes('co-decision-next'));assert.ok(bar.includes('2 / 3'));
assert.ok(!decisionBar(3,-1,icon,esc).includes(' / 3'));
const one=decisionBar(1,-1,icon,esc);assert.ok(one.includes('1 decision needs you'));assert.ok(!one.includes('co-decision-prev'));assert.ok(one.includes('Review'));
assert.equal(stepIndex(0,3,-1),2);assert.equal(stepIndex(2,3,1),0);assert.equal(stepIndex(0,0,1),0);
console.log('Decision cards: assertions passed');
// Lightbox helpers
assert.equal(wrapIndex(-1,12),11);assert.equal(wrapIndex(12,12),0);assert.equal(wrapIndex(5,0),0);
assert.equal(counterText(2,12),'3 / 12');
assert.equal(swipeDirection(-80,5),1);assert.equal(swipeDirection(80,5),-1);assert.equal(swipeDirection(20,0),0);assert.equal(swipeDirection(60,90),0);
console.log('Lightbox: helper assertions passed');

// Agent-attached files (artifact_ids): thumbs open the lightbox as one group, documents are chips.
const arts=[{id:'i1',title:'shot <1>.png',path:'o/shot.png',is_image:true,bytes:2048},{id:'i2',title:'b.png',path:'o/b.png',is_image:true},{id:'r1',title:'Final report',path:'reports/final.pdf',type:'pdf',bytes:3*1024*1024},{id:'x',title:'old',archived:true}];
assert.deepEqual(resolveFiles(['r1','i1','r1','gone','x'],arts).map(a=>a.id),['r1','i1']);
const files=messageFilesMarkup(resolveFiles(['i1','i2','r1'],arts),esc);
assert.equal((files.match(/data-lightbox-src=/g)||[]).length,2);assert.ok(files.includes('data-lightbox-group'));assert.ok(files.includes('/api/outputs/i1/raw'));
assert.ok(files.includes('shot &lt;1&gt;.png')&&!files.includes('<1>'));
assert.ok(files.includes('data-action="rich-file" data-source="artifact" data-id="r1"')&&files.includes('k-pdf')&&files.includes('<small>3.0 MB</small>'));
assert.equal(messageFilesMarkup([],esc),'');
assert.equal(fileKind({type:'xlsx'}),'sheet');assert.equal(fileKind({is_image:true}),'image');assert.equal(fileKind({path:'a/b.py'}),'code');assert.equal(fileKind({kind:'canvas'}),'canvas');
assert.equal(formatSize(0),'');assert.equal(formatSize(900),'900 B');assert.equal(formatSize(1536),'2 KB');
console.log('Decision rows, grouping and message files: assertions passed');
