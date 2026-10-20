import assert from 'node:assert/strict';
import {lastSegments,accessFor,compactFolders,groupChanges,limitRows,isImagePath,rosterGroups,rosterMatches,categoryLabel,GROUP_THRESHOLD} from '../dist/layout.js';
import {selectWindows,LOW_REMAINING} from '../dist/usage-ui.js';
import {exceedsBox} from '../dist/lightbox.js';

// ---- usage window selection (owner rule: 5h only; weekly joins when < 10% left) ----
const soon=Math.floor(Date.now()/1000)+7200;
const win=(id,used,minutes,extra={})=>({id,used_percent:used,window_minutes:minutes,resets_at:soon,...extra});
assert.equal(LOW_REMAINING,10);
for(const [provider,five,week] of [['codex','codex:primary','codex:secondary'],['claude','five_hour','seven_day']]) {
  const make=used=>({id:provider,usage:{windows:[win(five,30,300),win(week,used,10080)]}});
  assert.equal(selectWindows(make(50)).primary.id,five,provider+' primary is the 5h window');
  assert.equal(selectWindows(make(50)).extra.length,0,provider+' weekly hidden at 50% left');
  assert.equal(selectWindows(make(90)).extra.length,0,provider+' weekly hidden at exactly 10% left');
  assert.equal(selectWindows(make(90.5)).extra.length,1,provider+' weekly shown just under 10% left');
  assert.equal(selectWindows(make(100)).extra[0].id,week,provider+' weekly shown at 0% left');
  assert.equal(selectWindows(make(null)).extra.length,0,provider+' unknown weekly never "low"');
}
assert.equal(selectWindows({id:'claude',usage:{windows:[win('five_hour',10,300),win('seven_day',99,10080,{resets_at:1})]}}).extra.length,0,'expired weekly is a fresh window');
assert.equal(selectWindows({id:'codex',usage:{windows:[win('codex:primary',5,300),win('other:primary',99,300)]}}).all.length,1,'only the default bucket counts for Codex');
assert.equal(selectWindows({id:'codex',usage:{windows:[win('codex:secondary',50,10080)]}}).primary.id,'codex:secondary','falls back to the shortest window, never blank');
assert.equal(selectWindows({id:'codex',usage:{}}).primary,null);
assert.equal(selectWindows(undefined).primary,null);

// ---- folder compaction ----
assert.equal(lastSegments('C:\\Projects\\demo-shop\\backend\\app\\tenant\\routers'),'tenant\\routers');
assert.equal(lastSegments('/home/me/project/src/lib'),'src/lib');
assert.equal(lastSegments('/'),'/');
assert.equal(lastSegments('C:\\'),'C:\\');
assert.equal(lastSegments('docs'),'docs');
assert.equal(lastSegments('C:\\a\\b\\'),'a\\b','trailing separator ignored');
const grants=[{path:'C:\\Projects\\demo-shop',access:'readwrite'},{path:'C:\\Projects\\demo-shop\\docs',access:'read'},{path:'D:\\Other',access:'read'}];
assert.equal(accessFor('C:\\Projects\\demo-shop\\backend\\app',grants),'RW');
assert.equal(accessFor('c:/projects/demo-shop/docs/review',grants),'R','the most specific grant wins, case and slash insensitive');
assert.equal(accessFor('C:\\Projects\\demo-shopx\\a',grants),'','a sibling with the same prefix is not covered');
assert.equal(accessFor('E:\\none',grants),'');
const paths=Array.from({length:19},(_,i)=>'C:\\Projects\\demo-shop\\dir'+i);
const folded=compactFolders(paths,grants);
assert.equal(folded.rows.length,4);
assert.equal(folded.total,19);
assert.equal(folded.hidden,15);
assert.equal(folded.rows[0].label,'demo-shop\\dir0');
assert.equal(folded.rows[0].path,paths[0],'full path stays available for the tooltip');
assert.equal(folded.rows[0].access,'RW');
assert.equal(compactFolders(paths,grants,{expanded:true}).rows.length,19);
assert.equal(compactFolders(paths,grants,{expanded:true}).hidden,0);
assert.equal(compactFolders(['C:\\a\\b','c:/A/B/','C:\\a\\b',''],[]).total,1,'duplicates (slash/case/trailing) collapse; blanks dropped');
assert.deepEqual(compactFolders(undefined),{rows:[],total:0,hidden:0});
assert.equal(compactFolders(['C:\\x'],[]).rows[0].access,'','no covering grant -> no badge');

// ---- file-change grouping ----
const ch=(id,path,at,added,deleted,extra={})=>({id,path,created_at:at,added,deleted,agent_id:'a1',kind:'modified',...extra});
const changes=[
  ch('1','projects\\x\\cost.md','2026-10-19T08:00:00Z',266,0,{kind:'created'}),
  ch('2','projects\\x\\cost.md','2026-10-19T09:00:00Z',4,4,{agent_id:'a2'}),
  ch('3','src/app.py','2026-10-19T08:30:00Z',10,2),
  ch('4','media\\shot.PNG','2026-10-19T08:40:00Z',0,0,{kind:'created'}),
  ch('5','PROJECTS/x/cost.md','2026-10-19T10:00:00Z',1,1),
  ch('6','logo.svg','2026-10-19T10:05:00Z',3,0),
  ch('7','notes/readme','2026-10-19T07:00:00Z',1,0,{artifact_id:'img1'}),
];
const groups=groupChanges(changes,{isImage:c=>c.artifact_id==='img1'});
assert.equal(groups.length,2,'images (by extension and by artifact) are not listed; same file groups once');
const cost=groups.find(g=>g.name==='cost.md');
assert.equal(cost.count,3);
assert.equal(cost.added,271);
assert.equal(cost.deleted,5);
assert.equal(cost.latest.id,'5','the newest revision opens the diff');
assert.deepEqual(cost.agentIds,['a1','a2']);
assert.equal(cost.created,true);
assert.equal(cost.dir,'x');
assert.equal(groups[0].name,'cost.md','rows are ordered by their newest revision');
assert.equal(groups[1].name,'app.py');
assert.equal(groups[1].count,1);
assert.deepEqual(groupChanges([]),[]);
assert.deepEqual(groupChanges(undefined),[]);
assert.ok(isImagePath('a/b/Shot.JPEG'));
assert.ok(isImagePath('x.webp?v=2'));
assert.ok(!isImagePath('notes/image.md'));
assert.ok(!isImagePath(''));
const rows=Array.from({length:10},(_,i)=>i);
assert.deepEqual(limitRows(rows,6,false),{rows:[0,1,2,3,4,5],hidden:4});
assert.deepEqual(limitRows(rows,6,true).hidden,0);
assert.equal(limitRows(rows,6,true).rows.length,10);
assert.deepEqual(limitRows([1,2],6,false),{rows:[1,2],hidden:0});

// ---- roster grouping & search ----
const person=(id,category,name=id)=>({id,name,category,role:'role of '+id});
const small=Array.from({length:GROUP_THRESHOLD},(_,i)=>person('p'+i,'cat'+i));
assert.equal(rosterGroups(small).length,1,'up to 8 agents: no headers');
assert.equal(rosterGroups(small)[0].key,'');
const uniques=Array.from({length:11},(_,i)=>person('u'+i,'unique '+i));
assert.deepEqual(rosterGroups(uniques).map(g=>g.key),[''],'all-unique categories never become header-per-row');
const mixed=[person('a-lead','general'),person('e1','Engineering'),person('e2','engineering'),person('e3','Engineering'),person('m1','Marketing'),person('d1','Design'),person('d2','Design'),person('q1','QA'),person('x1','Ops'),person('x2','Ops'),person('x3','Ops')];
const grouped=rosterGroups(mixed);
assert.deepEqual(grouped.map(g=>g.key),['','engineering','design','ops'],'singletons share the flat list; lead keeps it first');
assert.equal(grouped[0].agents.map(a=>a.id).join(),'a-lead,m1,q1');
assert.equal(grouped[1].agents.length,3);
assert.equal(grouped[1].label,'Engineering');
assert.equal(rosterGroups(mixed.slice(1))[rosterGroups(mixed.slice(1)).length-1].key,'','flat list goes last when the lead is not in it');
assert.equal(rosterGroups(mixed).reduce((n,g)=>n+g.agents.length,0),mixed.length,'no agent lost or duplicated');
assert.equal(categoryLabel('business_analysis'),'Business Analysis');
assert.equal(categoryLabel('qa'),'QA');
assert.equal(categoryLabel(''),'General');
assert.ok(rosterMatches({name:'Lucas - QA Tester',role:'Tester',category:'Quality Assurance'},'quality'));
assert.ok(rosterMatches({name:'Lucas',role:'Tester'},'  tester '));
assert.ok(rosterMatches({name:'x'},''));
assert.ok(!rosterMatches({name:'Lucas',role:'Tester',category:'QA'},'designer'));

// ---- lightbox: offer "Actual size" only when it reveals more pixels ----
assert.equal(exceedsBox({width:1366,height:6000},{width:1246,height:700}),true);
assert.equal(exceedsBox({width:300,height:200},{width:1246,height:700}),false);
assert.equal(exceedsBox({width:1246,height:700},{width:1246,height:700}),false);
assert.equal(exceedsBox({width:0,height:0},{width:100,height:100}),false);
assert.equal(exceedsBox(null,{width:1,height:1}),false);
console.log('layout helper assertions passed');
