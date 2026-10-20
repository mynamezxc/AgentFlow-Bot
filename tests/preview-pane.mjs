import assert from 'node:assert/strict';
import {MIN_WIDTH,MIN_CHAT,MAX_TABS,STEP,suffixOf,isEditableType,canOpenNative,baseName,widthRange,clampWidth,defaultWidth,stepWidth,tabKey,isDirty,needsConfirm,dirtyTabs,openTab,closeTab,viewTabs,activeView,viewLabel,diffMarkup} from '../dist/preview-pane.js';

// width limits: min 360, chat keeps 360
assert.deepEqual(widthRange(1400),{min:360,max:1040});
assert.deepEqual(widthRange(600),{min:360,max:360});
assert.equal(clampWidth(100,1400),360);
assert.equal(clampWidth(5000,1400),1040);
assert.equal(clampWidth(700.4,1400),700);
assert.equal(clampWidth(NaN,1400),360);
assert.equal(clampWidth(900,700),360);
assert.equal(defaultWidth(1200),600);
assert.equal(defaultWidth(500),360);
assert.equal(defaultWidth(3000),1500);

// keyboard steps: left widens, right narrows, Home/End jump, clamped, other keys ignored
assert.equal(STEP,24);
assert.equal(stepWidth(500,'ArrowLeft',1400),524);
assert.equal(stepWidth(500,'ArrowRight',1400),476);
assert.equal(stepWidth(370,'ArrowRight',1400),360);
assert.equal(stepWidth(1030,'ArrowLeft',1400),1040);
assert.equal(stepWidth(500,'Home',1400),360);
assert.equal(stepWidth(500,'End',1400),1040);
assert.equal(stepWidth(500,'a',1400),null);

// tabs
const t=(id,draft)=>({key:tabKey('artifact',id),id,content:'x',...(draft===undefined?{}:{draft})});
let tabs=[],active=null;
for(const id of ['a','b','c']) {const r=openTab(tabs,t(id));tabs=r.tabs;active=r.active;assert.equal(r.opened,true);}
assert.deepEqual(tabs.map(x=>x.id),['a','b','c']);
assert.equal(active,'artifact:c');
let r=openTab(tabs,t('a'));
assert.equal(r.opened,false);assert.equal(r.active,'artifact:a');assert.equal(r.tabs,tabs);
for(const id of ['d','e','f']) {r=openTab(tabs,t(id));tabs=r.tabs;}
assert.equal(tabs.length,MAX_TABS);
r=openTab(tabs,t('g'));
assert.equal(r.tabs.length,MAX_TABS);
assert.deepEqual(r.tabs.map(x=>x.id),['b','c','d','e','f','g'],'oldest clean tab is dropped');
assert.equal(r.active,'artifact:g');
// dirty tabs survive
tabs=['a','b','c','d','e','f'].map((id,i)=>i===0?t(id,'changed'):t(id));
r=openTab(tabs,t('g'));
assert.deepEqual(r.tabs.map(x=>x.id),['a','c','d','e','f','g']);
tabs=['a','b','c','d','e','f'].map(id=>t(id,'changed'));
r=openTab(tabs,t('g'));
assert.equal(r.rejected,true);assert.equal(r.tabs,tabs);

// closing activates the right neighbour, else the left, else nothing
tabs=['a','b','c'].map(id=>t(id));
assert.equal(closeTab(tabs,'artifact:b','artifact:b').active,'artifact:c');
assert.equal(closeTab(tabs,'artifact:c','artifact:c').active,'artifact:b');
assert.equal(closeTab(tabs,'artifact:a','artifact:a').active,'artifact:b');
assert.equal(closeTab(tabs,'artifact:a','artifact:c').active,'artifact:c');
assert.deepEqual(closeTab([t('a')],'artifact:a','artifact:a'),{tabs:[],active:null});
assert.equal(closeTab(tabs,'artifact:zzz','artifact:a').tabs,tabs);

// dirty guard
assert.equal(isDirty(t('a')),false);
assert.equal(isDirty(t('a','x')),false);
assert.equal(isDirty(t('a','y')),true);
assert.equal(isDirty(null),false);
assert.equal(needsConfirm(t('a','y')),true);
assert.equal(needsConfirm(t('a')),false);
assert.equal(dirtyTabs([t('a'),t('b','y')]).length,1);

// types
assert.equal(suffixOf('docs/Plan.MD'),'.md');
assert.equal(suffixOf('Makefile'),'');
assert.equal(suffixOf('C:\\x.y\\file'),'');
assert.equal(baseName('C:\\work\\a\\notes.md'),'notes.md');
assert.equal(baseName('a/b/c.txt'),'c.txt');
for(const p of ['a.md','a.txt','a.json','a.csv','a.yaml','a.toml','a.html','a.css','a.js','a.ts','a.py','a.sql','a.log','a.xml']) assert.equal(isEditableType(p,10),true,p);
for(const p of ['a.png','a.pdf','a.docx','a.xlsx','a.zip','noext']) assert.equal(isEditableType(p,10),false,p);
assert.equal(isEditableType('a.md',2*1024*1024),true);
assert.equal(isEditableType('a.md',2*1024*1024+1),false);
for(const p of ['a.js','a.py','a.ps1','a.sh','a.html','a.svg']) assert.equal(canOpenNative(p),false,p);
for(const p of ['a.md','a.pdf','a.docx','a.png','a.csv']) assert.equal(canOpenNative(p),true,p);

// views
assert.deepEqual(viewTabs({mode:'text',suffix:'.md',path:'a.md'}),['preview','source']);
assert.deepEqual(viewTabs({mode:'text',suffix:'.py',path:'a.py'}),['source']);
assert.deepEqual(viewTabs({mode:'canvas',path:'a.html'}),['preview','source']);
assert.deepEqual(viewTabs({mode:'image',path:'a.png'}),['preview']);
assert.deepEqual(viewTabs({mode:'document',suffix:'.pdf'}),['preview']);
assert.deepEqual(viewTabs({mode:'text',path:'a.py',hasChanges:true}),['source','changes']);
assert.deepEqual(viewTabs({mode:'text',path:'a.md',error:'boom',change:{}}),['changes']);
assert.equal(activeView({mode:'text',path:'a.md',suffix:'.md'}),'preview');
assert.equal(activeView({mode:'text',path:'a.md',suffix:'.md',view:'source'}),'source');
assert.equal(activeView({mode:'text',path:'a.py',view:'preview'}),'source');
assert.equal(activeView({mode:'text',path:'a.py',change:{id:1},view:undefined}),'changes');
assert.equal(viewLabel('source',{editable:true}),'Edit');
assert.equal(viewLabel('source',{editable:false}),'Source');
assert.equal(viewLabel('preview',{mode:'canvas'}),'Canvas');
assert.equal(viewLabel('changes',{}),'Changes');

// diff markup escapes and reports truncation
const esc=v=>String(v??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const html=diffMarkup({diff:[{kind:'add',old:'',new:1,text:'<b>x</b>'},{kind:'delete',old:2,new:'',text:'gone'}],diff_truncated:true},esc);
assert.match(html,/&lt;b&gt;x&lt;\/b&gt;/);
assert.match(html,/diff-line add/);assert.match(html,/diff-line delete/);assert.match(html,/first 1,500 lines/);
assert.match(diffMarkup({diff:[]},esc),/No line changes/);

console.log('preview-pane tests passed');
