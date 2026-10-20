import assert from 'node:assert/strict';
import {skillSourcesFromValues,skillSourcesPanel,MAX_SKILL_SOURCES} from '../dist/settings-panels.js';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const field=(name,label,value)=>'<div data-field="'+name+'">'+esc(label)+'</div>';

// Add form -> settings.skill_sources payload (existing ids are kept; the backend assigns the id of a new entry)
const url='https://github.com/acme/skills/tree/main/skills';
assert.deepEqual(skillSourcesFromValues([],{label:'  Team   skills ',url}),[{label:'Team skills',url}]);
const current=[{id:'a',label:'A',url:'https://github.com/a/b',extra:'dropped'}];
assert.deepEqual(skillSourcesFromValues(current,{label:'B',url}),[{id:'a',label:'A',url:'https://github.com/a/b'},{label:'B',url}]);
assert.deepEqual(skillSourcesFromValues(undefined,{label:'Raw',url:'https://raw.githubusercontent.com/o/r/main/x/SKILL.md'}).length,1);
for(const bad of [{label:'',url},{label:'x'.repeat(61),url},{label:'A',url:'http://github.com/a/b'},{label:'A',url:'https://example.com/a/b'},{label:'A',url:'https://github.com/a'},{label:'A',url:''}])
  assert.throws(()=>skillSourcesFromValues([],bad),/Enter/);
assert.throws(()=>skillSourcesFromValues(current,{label:'Dup',url:'https://github.com/a/b'}),/already/);
const full=Array.from({length:MAX_SKILL_SOURCES},(_,i)=>({id:'s'+i,label:'S'+i,url:'https://github.com/o/r'+i}));
assert.throws(()=>skillSourcesFromValues(full,{label:'Extra',url}),/up to 10/);

// Panel markup: list, remove buttons, add form; escaped; add disabled at the limit
let html=skillSourcesPanel({skill_sources:[{id:'acme',label:'<Acme>',url}]},field,esc);
assert.ok(html.includes('id="skill-sources-form"')&&html.includes('data-action="skill-source-remove" data-id="acme"'));
assert.ok(html.includes('&lt;Acme&gt;')&&!html.includes('<Acme>'));
assert.ok(html.includes('data-field="label"')&&html.includes('data-field="url"'));
assert.ok(skillSourcesPanel({},field,esc).includes('No third-party sources yet.'));
assert.ok(skillSourcesPanel({skill_sources:full},field,esc).includes(' disabled>Add source'));
console.log('Skill sources: assertions passed');
