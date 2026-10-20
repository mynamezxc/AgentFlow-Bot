import assert from 'node:assert/strict';
import {COMMANDS,slashQuery,filterCommands,parseSlash,keyAction,completion,commandById,installedSkills,catalogPath,sourceChips,sourceLabel,isUnavailable,debounce,skillChipsMarkup,commandRowMarkup,wrap} from '../dist/slash.js';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// Command list.
assert.deepEqual(COMMANDS.map(c=>c.id),['goal','create-skill','install-skill','skill','mcp','create-tool']);
assert.ok(COMMANDS.every(c=>c.title&&c.hint));
// Popup trigger: only the "/command" token at the very start.
assert.equal(slashQuery('/'),'');assert.equal(slashQuery('/go'),'go');assert.equal(slashQuery('/Create-S'),'create-s');
assert.equal(slashQuery('hello /goal'),null);assert.equal(slashQuery('/goal build'),null);assert.equal(slashQuery(' /goal'),null);assert.equal(slashQuery('/a/b'),null);assert.equal(slashQuery(''),null);
// Filtering: prefix first, then title word / substring in id, title, hint.
assert.equal(filterCommands('').length,6);
assert.deepEqual(filterCommands('go').map(c=>c.id),['goal']);
assert.deepEqual(filterCommands('s').map(c=>c.id).slice(0,4),['skill','goal','create-skill','install-skill']); // id-prefix first, then title-word matches in list order
assert.deepEqual(filterCommands('skill').map(c=>c.id),['skill','create-skill','install-skill']);
assert.deepEqual(filterCommands('create').map(c=>c.id),['create-skill','create-tool']);
assert.deepEqual(filterCommands('tool').map(c=>c.id),['create-tool']);
assert.deepEqual(filterCommands('server').map(c=>c.id),['mcp']);           // hint match
assert.deepEqual(filterCommands('zzz'),[]);
assert.deepEqual(filterCommands('MC').map(c=>c.id),['mcp']);
// Parsing.
assert.deepEqual(parseSlash('/goal foo'),{command:'goal',text:'foo'});
assert.deepEqual(parseSlash('  /goal   ship the launch  \n'),{command:'goal',text:'ship the launch'});
assert.deepEqual(parseSlash('/create-skill SEO audit\nwith steps'),{command:'create-skill',text:'SEO audit\nwith steps'});
assert.deepEqual(parseSlash('/create-tool resize images'),{command:'create-tool',text:'resize images'});
assert.deepEqual(parseSlash('/mcp'),{command:'mcp',text:''});assert.deepEqual(parseSlash('/GOAL x'),{command:'goal',text:'x'});
assert.equal(parseSlash('/unknown foo'),null);assert.equal(parseSlash('goal foo'),null);assert.equal(parseSlash('/usr/bin/env'),null);assert.equal(parseSlash(''),null);assert.equal(parseSlash(null),null);
assert.equal(completion(commandById('goal')),'/goal ');assert.equal(completion(commandById('mcp')),'/mcp');
// Keyboard selection (pure): arrows wrap, Enter/Tab accept, Esc closes, Shift+Enter and IME ignored.
assert.deepEqual(keyAction({key:'ArrowDown',index:0,count:3}),{type:'move',index:1});
assert.deepEqual(keyAction({key:'ArrowDown',index:2,count:3}),{type:'move',index:0});
assert.deepEqual(keyAction({key:'ArrowUp',index:0,count:3}),{type:'move',index:2});
assert.deepEqual(keyAction({key:'Enter',index:1,count:3}),{type:'accept',index:1});
assert.deepEqual(keyAction({key:'Tab',index:2,count:3}),{type:'accept',index:2});
assert.deepEqual(keyAction({key:'Escape',index:0,count:3}),{type:'close'});
assert.deepEqual(keyAction({key:'Escape',index:0,count:0}),{type:'close'});
assert.equal(keyAction({key:'Enter',index:0,count:3,shift:true}),null);
assert.equal(keyAction({key:'Enter',index:0,count:3,composing:true}),null);
assert.equal(keyAction({key:'Enter',index:0,count:0}),null);assert.equal(keyAction({key:'a',index:0,count:3}),null);
assert.deepEqual(keyAction({key:'End',index:0,count:4}),{type:'move',index:3});assert.deepEqual(keyAction({key:'Home',index:2,count:4}),{type:'move',index:0});
assert.equal(wrap(-1,4),3);assert.equal(wrap(4,4),0);assert.equal(wrap(1,0),0);
// Installed skills picker data.
const skills=[{id:'a',name:'SEO Audit',description:'Check pages',category:'seo',installed:true},{id:'b',name:'Draft',installed:false},{id:'c',name:'Old',installed:true,archived:true},{id:'d',name:'Writer',description:'Long-form SEO copy',installed:true}];
assert.deepEqual(installedSkills(skills).map(s=>s.id),['a','d']);
assert.deepEqual(installedSkills(skills,'seo').map(s=>s.id),['a','d']);assert.deepEqual(installedSkills(skills,'writer').map(s=>s.id),['d']);assert.deepEqual(installedSkills(skills,'nope'),[]);
// Catalog helpers.
assert.equal(catalogPath(),'/skills/catalog');assert.equal(catalogPath({q:' seo ',source:'all'}),'/skills/catalog?q=seo');
assert.equal(catalogPath({q:'a b',source:'third_party'}),'/skills/catalog?q=a+b&source=third_party');assert.equal(catalogPath({source:'codex'}),'/skills/catalog?source=codex');
assert.deepEqual(sourceChips([{id:'library',label:'Library',count:4},{id:'claude',count:2}]),[{id:'all',label:'All'},{id:'library',label:'Library',count:4},{id:'claude',label:'Claude',count:2}]);
assert.deepEqual(sourceChips(null),[{id:'all',label:'All'}]);
assert.equal(sourceLabel('codex'),'Codex');assert.equal(sourceLabel('x',[{id:'x',label:'Mine'}]),'Mine');
// Unavailable backend.
assert.ok(isUnavailable(new Error('API endpoint not found.')));assert.ok(isUnavailable(new Error('Operation not supported.')));assert.ok(isUnavailable(new Error('Unexpected token < in JSON')));
assert.ok(!isUnavailable(new Error('Goal is too long.')));
// Debounce: only the last call fires.
{const calls=[];const fn=debounce(v=>calls.push(v),20);fn(1);fn(2);fn(3);await new Promise(r=>setTimeout(r,60));assert.deepEqual(calls,[3]);fn(4);fn.cancel();await new Promise(r=>setTimeout(r,40));assert.deepEqual(calls,[3]);}
// Markup escaping.
const chip=skillChipsMarkup([{id:'a<b',name:'<img>'}],esc);
assert.ok(chip.includes('data-action="co-skill-remove"')&&chip.includes('data-id="a&lt;b"')&&!chip.includes('<img>')&&chip.includes('aria-label="Remove skill &lt;img&gt;"'));
assert.equal(skillChipsMarkup([],esc),'');
const row=commandRowMarkup(commandById('goal'),true,esc);
assert.ok(row.includes('role="option"')&&row.includes('aria-selected="true"')&&row.includes('data-cmd="goal"')&&row.includes('/goal')&&row.includes('Set goal'));
assert.ok(commandRowMarkup(commandById('mcp'),false,esc).includes('aria-selected="false"'));
console.log('Slash commands: assertions passed');
