import assert from 'node:assert/strict';
import {telegramLabel,telegramStatus,telegramBody,telegramSettings,FORWARD} from '../dist/telegram-ui.js';
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const field=(name,label,value,type,opts={})=>'<div data-field="'+name+'" data-type="'+type+'" data-value="'+esc(value)+'">'+esc(label)+(opts.options?opts.options.map(o=>'<i>'+esc(o[0])+'</i>').join(''):'')+esc(opts.help||'')+'</div>';
const secretField=(key,label)=>'<div data-secret="'+key+'">'+esc(label)+'</div>';
const checkbox=(name,label,checked,value='on')=>'<label data-check="'+name+'" data-value="'+esc(value)+'" data-checked="'+!!checked+'">'+esc(label)+'</label>';
const button=(text,action,id='',cls='')=>'<button class="'+cls+'" data-action="'+action+'">'+esc(text)+'</button>';
const NOW=Date.parse('2026-10-20T12:00:00Z');
const agents=[['a-lead','Atlas'],['a-seo','Nova']];
const linked={mode:'polling',polling:true,connected:true,username:'af_bot',linked:true,chat_title:'Team room',chat_type:'supergroup',owner_user_ids:[7,8],default_agent_id:'a-seo',
  forward:{questions:true,permissions:true,agent_messages:false,status_digest:false,files:true},last_update_at:'2026-10-20T11:55:00Z',last_error:'',pending:[]};

// labels
assert.deepEqual(telegramLabel({},false),{text:'Not configured',tone:'idle'});
assert.equal(telegramLabel({mode:'polling',polling:false},true).text,'Token saved · not connected');
assert.equal(telegramLabel({mode:'polling',polling:true},true).text,'Connecting…');
assert.equal(telegramLabel(linked,true).text,'Connected as @af_bot · Team room');
assert.equal(telegramLabel(linked,true).tone,'done');
assert.equal(telegramLabel({...linked,linked:false},true).text,'Connected as @af_bot · waiting for a chat');
assert.equal(telegramLabel({halted:'401'},true).text,'Token rejected');
assert.match(telegramLabel({halted:'409'},true).text,/another program/);
assert.equal(telegramLabel({mode:'webhook'},true).text,'Configured · webhook');

// status line: bot, chat, last update (relative), last error, pending link, and everything escaped
let status=telegramStatus({...linked,last_error:'Bad <b>gateway</b>',pending:[{title:'Other <group>',user:'@x'}]},true,esc,NOW);
assert.ok(status.includes('Connected as @af_bot'));assert.ok(status.includes('Linked chat: Team room (supergroup)'));
assert.ok(status.includes('Last update from Telegram: 5 min ago'));
assert.ok(status.includes('Last error: Bad &lt;b&gt;gateway&lt;/b&gt;'));assert.ok(!status.includes('<b>gateway'));
assert.ok(status.includes('Waiting for your approval in the Atlas chat: link Other &lt;group&gt; (from @x).'));
assert.ok(!telegramStatus({},true,esc,NOW).includes('Last error'));

// modal body when linked
const parts={tg:linked,settings:{},hasToken:true,agentOptions:agents,esc,now:NOW,secretField,field,checkbox,button};
let body=telegramBody(parts);
assert.ok(body.includes('data-field="telegram_mode"')&&body.includes('data-value="polling"')&&body.includes('<i>webhook</i>'));
assert.ok(body.includes('data-secret="TELEGRAM_BOT_TOKEN"'));
assert.ok(body.includes('data-field="tg_default_agent"')&&body.includes('data-value="a-seo"'));
for(const [key] of FORWARD) assert.ok(body.includes('data-check="tg_fwd_'+key+'"'),key);
assert.ok(body.includes('data-check="tg_fwd_agent_messages" data-value="on" data-checked="false"'));
assert.ok(body.includes('data-check="tg_fwd_questions" data-value="on" data-checked="true"'));
assert.ok(body.includes('data-check="tg_owner" data-value="7" data-checked="true"')&&body.includes('data-value="8"'));
for(const action of ['telegram-test','telegram-unlink','restart-runtime','telegram-token-clear']) assert.ok(body.includes('data-action="'+action+'"'),action);
assert.ok(!telegramBody({...parts,hasToken:false}).includes('telegram-token-clear'));
assert.ok(body.includes('No restart is needed'));
assert.ok(body.includes('Group Privacy')&&body.includes('@BotFather')&&body.includes('privacy mode off'));
assert.ok(!body.includes('TELEGRAM_WEBHOOK_SECRET'),'polling mode must not ask for a webhook secret');
assert.ok(!body.includes('telegram_chats'));

// not linked: explains how to link, no linked-chat controls
body=telegramBody({...parts,tg:{mode:'polling',linked:false}});
assert.ok(body.includes('No chat is linked yet'));
for(const absent of ['tg_default_agent','tg_owner','telegram-unlink','telegram-test']) assert.ok(!body.includes(absent),absent);

// webhook mode shows the legacy fields
body=telegramBody({...parts,tg:{...linked,mode:'webhook'},settings:{telegram_chats:'-1,-2',telegram_agent_id:'a-seo'}});
assert.ok(body.includes('data-secret="TELEGRAM_WEBHOOK_SECRET"')&&body.includes('data-field="telegram_chats"')&&body.includes('data-value="-1,-2"'));
assert.ok(body.includes('data-value="webhook"'));

// form -> settings
const values=(entries)=>({has:name=>entries.some(([k])=>k===name),getAll:name=>entries.filter(([k])=>k===name).map(([,v])=>v)});
let entries=[['tg_fwd_questions','on'],['tg_fwd_files','on'],['tg_owner','7']];
let settings=telegramSettings({telegram_mode:'polling',tg_default_agent:'a-code',telegram_chats:'ignored',telegram_agent_id:'ignored'},values(entries),linked);
assert.deepEqual(settings,{telegram_mode:'polling',telegram:{default_agent_id:'a-code',forward:{questions:true,permissions:false,agent_messages:false,status_digest:false,files:true},owner_user_ids:[7]}});
settings=telegramSettings({telegram_mode:'webhook',telegram_chats:'-5',telegram_agent_id:'a-seo'},values([]),{linked:false});
assert.deepEqual(settings,{telegram_mode:'webhook',telegram_chats:'-5',telegram_agent_id:'a-seo'});
assert.equal(telegramSettings({telegram_mode:'bogus'},values([]),{}).telegram_mode,'polling');
assert.deepEqual(telegramSettings({telegram_mode:'polling',tg_default_agent:'a-lead'},values([['tg_owner','x'],['tg_owner','9']]),linked).telegram.owner_user_ids,[9]);
assert.equal('telegram' in telegramSettings({telegram_mode:'polling'},values([]),{linked:false}),false);
console.log('telegram-ui ok');
