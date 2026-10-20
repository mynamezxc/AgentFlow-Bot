import assert from 'node:assert/strict';
import {mergeState,prependMessages,pollDelay,createFormatCache,cacheable} from '../dist/state-sync.js';

const msg=(id,channel,at,extra={})=>({id,channel,created_at:at,version:1,content:id,...extra});
const base=()=>({rev:'e.1',settings:{name:'x'},runtime:{a:1},agents:[{id:'a1',version:1,name:'A'}],
  messages:[msg('m5','dm-a','2026-01-05T00:00:00Z'),msg('m6','dm-a','2026-01-06T00:00:00Z'),msg('b1','dm-b','2026-01-01T00:00:00Z')]});

// create / patch / delete
{
  const data=base(),agents=data.agents;
  const r=mergeState(data,{rev:'e.2',changes:{agents:[{id:'a1',version:2,name:'A2'},{id:'a2',version:1,name:'B'}]},deleted:{},runtime:{a:2}});
  assert.equal(r.changed,true);assert.deepEqual(r.kinds,['agents']);
  assert.equal(data.agents,agents,'arrays are updated in place');
  assert.deepEqual(data.agents.map(a=>a.name),['A2','B']);
  assert.equal(data.rev,'e.2');assert.deepEqual(data.runtime,{a:2});
  mergeState(data,{rev:'e.3',changes:{},deleted:{agents:['a1','nope']}});
  assert.deepEqual(data.agents.map(a=>a.id),['a2']);
}
// nothing changed: only rev/runtime move, `changed` stays false
{
  const data=base(),r=mergeState(data,{rev:'e.9',changes:{},deleted:{},runtime:{a:1}});
  assert.equal(r.changed,false);assert.equal(data.rev,'e.9');
  assert.equal(mergeState(data,{rev:'e.10',changes:{agents:[]},deleted:{agents:[]}}).changed,false);
}
// settings only when sent
{
  const data=base();
  mergeState(data,{rev:'e.2',changes:{},deleted:{}});assert.equal(data.settings.name,'x');
  const r=mergeState(data,{rev:'e.3',changes:{},deleted:{},settings:{name:'y'}});
  assert.equal(data.settings.name,'y');assert.deepEqual(r.kinds,['settings']);
}
// a missing collection is created
{
  const data=base();mergeState(data,{rev:'e.2',changes:{tasks:[{id:'t1',version:1}]},deleted:{}});
  assert.equal(data.tasks.length,1);
}
// messages: new ones append, patched loaded ones replace, patched never-loaded old ones are ignored
{
  const data=base();
  mergeState(data,{rev:'e.2',changes:{messages:[
    msg('m7','dm-a','2026-01-07T00:00:00Z'),
    {...msg('m6','dm-a','2026-01-06T00:00:00Z'),content:'edited',version:2},
    msg('m1','dm-a','2026-01-01T00:00:00Z',{version:5}),     // older than anything loaded in dm-a: not part of the window
    msg('c1','dm-c','2020-01-01T00:00:00Z')]},deleted:{}});    // a channel with nothing loaded takes any message
  assert.deepEqual(data.messages.map(m=>m.id),['m5','m6','b1','m7','c1']);
  assert.equal(data.messages[1].content,'edited');
}
// older pages go in front and are not duplicated
{
  const data=base();
  assert.equal(prependMessages(data,[msg('m3','dm-a','2026-01-03T00:00:00Z'),msg('m4','dm-a','2026-01-04T00:00:00Z'),msg('m5','dm-a','2026-01-05T00:00:00Z')]),2);
  assert.deepEqual(data.messages.map(m=>m.id),['m3','m4','m5','m6','b1']);
  assert.equal(prependMessages(data,[]),0);
}
// poll pacing: fast while work runs or just changed, slower when idle, paused when hidden, backing off on failures
{
  assert.equal(pollDelay({hidden:true,active:true}),null);
  assert.equal(pollDelay({active:true,sinceChange:Infinity}),1500);
  assert.equal(pollDelay({sinceChange:1000}),2000);
  assert.equal(pollDelay({sinceChange:60000}),5000);
  assert.equal(pollDelay({sinceChange:10*60000}),10000);
  assert.equal(pollDelay({}),10000);
  const waits=[1,2,3,4,5,6,9].map(failures=>pollDelay({active:true,failures}));
  assert.deepEqual(waits,[4000,8000,16000,30000,30000,30000,30000]);
  assert.ok(waits.every((w,i)=>!i||w>=waits[i-1]),'never speeds up while failing');
  assert.equal(pollDelay({hidden:true,failures:3}),null);
}
// format cache: hit, least-recently-used eviction, reset when dependencies change
{
  const cache=createFormatCache(2);let calls=0;
  const make=v=>()=>{calls++;return v;};
  assert.equal(cache.get('a:1',make('A')),'A');assert.equal(cache.get('a:1',make('X')),'A');assert.equal(calls,1);
  cache.get('b:1',make('B'));cache.get('a:1',make('X'));      // a is now the most recent
  cache.get('c:1',make('C'));                                   // evicts b
  assert.equal(cache.size,2);calls=0;
  cache.get('a:1',make('X'));assert.equal(calls,0);
  cache.get('b:1',make('B2'));assert.equal(calls,1);
  cache.setDeps('agents=1');const before=cache.size;cache.setDeps('agents=1');assert.equal(cache.size,before,'same deps keep the cache');
  cache.setDeps('agents=2');assert.equal(cache.size,0);
  assert.equal(cache.get('a:1',make('fresh')),'fresh');
}
// what is cached for the next load
{
  assert.equal(cacheable(null),null);assert.equal(cacheable({agents:[]}),null);
  const copy=cacheable({rev:'e.1',agents:[],runtime:{secrets:{X:true}}});
  assert.equal(copy.rev,'e.1');assert.ok(copy.cached_at>0);
}
console.log('state-sync ok');
