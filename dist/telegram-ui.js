import {relTime} from './reltime.js';

export const FORWARD=[
  ['questions','Questions agents ask you'],
  ['permissions','Permission and access requests'],
  ['agent_messages','Messages and replies from the team'],
  ['status_digest','Status digests'],
  ['files','Files and deliverables']
];
const MODES=[['polling','Long polling (recommended, no public address needed)'],['webhook','Webhook (needs a public HTTPS address)']];

export function telegramLabel(tg={},hasToken=false) {
  if(!hasToken) return {text:'Not configured',tone:'idle'};
  if(tg.halted==='401') return {text:'Token rejected',tone:'idle'};
  if(tg.halted==='409') return {text:'Stopped · another program uses this bot',tone:'idle'};
  if(tg.mode==='webhook') return {text:'Configured · webhook',tone:'idle'};
  if(tg.connected) return {text:'Connected'+(tg.username?' as @'+tg.username:'')+(tg.linked?' · '+(tg.chat_title||'chat linked'):' · waiting for a chat'),tone:'done'};
  if(!tg.polling) return {text:'Token saved · not connected',tone:'idle'};
  return {text:'Connecting…',tone:'idle'};
}

export function telegramStatus(tg={},hasToken=false,esc=String,now=Date.now()) {
  const label=telegramLabel(tg,hasToken);
  const lines=['<strong>'+esc(label.text)+'</strong>'];
  if(tg.linked) lines.push('Linked chat: '+esc(tg.chat_title||'Telegram chat')+(tg.chat_type?' ('+esc(tg.chat_type)+')':''));
  for(const item of tg.pending||[]) lines.push('Waiting for your approval in the Atlas chat: link '+esc(item.title||'a Telegram chat')+(item.user?' (from '+esc(item.user)+')':'')+'.');
  if(tg.last_update_at) lines.push('Last update from Telegram: '+esc(relTime(tg.last_update_at,now)));
  if(tg.last_error) lines.push('<span class="connection-error">Last error: '+esc(tg.last_error)+'</span>');
  return '<div class="info-box tg-status">'+lines.map(l=>'<p style="margin:2px 0">'+l+'</p>').join('')+'</div>';
}

export function telegramBody({tg={},settings={},hasToken=false,agentOptions=[],esc=String,now=Date.now(),secretField,field,checkbox,button}) {
  const mode=tg.mode==='webhook'?'webhook':'polling';
  let html=telegramStatus(tg,hasToken,esc,now)+
    field('telegram_mode','Connection',mode,'select',{options:MODES,help:'Changes apply as soon as you save. No restart is needed.'})+
    secretField('TELEGRAM_BOT_TOKEN','Bot token')+(hasToken?'<div class="row-actions wrap">'+button('Remove bot token','telegram-token-clear','','quiet small')+'</div>':'');
  if(tg.linked) {
    html+='<h3 class="form-subheading">Linked chat</h3>'+
      field('tg_default_agent','Plain messages go to',tg.default_agent_id||'a-lead','select',{options:agentOptions,help:'Start a message with @Name or Name: to talk to someone else. Reply to a message to answer that member.'})+
      '<div class="field"><label>Send to Telegram</label><div class="checkbox-group">'+
        FORWARD.map(([key,label])=>checkbox('tg_fwd_'+key,label,!!tg.forward?.[key])).join('')+'</div></div>'+
      '<div class="field"><label>Owner users</label><div class="checkbox-group">'+
        (tg.owner_user_ids||[]).map(id=>checkbox('tg_owner','User ID '+id,true,String(id))).join('')+'</div>'+
        '<span class="help">Only these Telegram users can control AgentFlow in the chat. Untick one to remove them when you save. New owners can only be added here, not from Telegram.</span></div>'+
      '<div class="row-actions wrap">'+button('Send test message','telegram-test','','','telegram')+button('Unlink chat','telegram-unlink','','danger')+'</div>';
  } else {
    html+='<div class="info-box">No chat is linked yet. Add the bot to your group (or open a private chat with it) and send any message. AgentFlow then asks you in the Atlas chat whether to link it, and only that person can control AgentFlow from Telegram.</div>';
  }
  html+='<div class="info-box"><strong>Group privacy</strong><p>In @BotFather open <em>Bot Settings → Group Privacy</em> and turn privacy mode off (or make the bot a group admin). Otherwise the bot only sees /commands and replies, not plain group messages.</p></div>';
  if(mode==='webhook') {
    html+='<h3 class="form-subheading">Webhook</h3>'+secretField('TELEGRAM_WEBHOOK_SECRET','Webhook secret token')+
      field('telegram_chats','Allowed chat IDs',settings.telegram_chats||'','text',{help:'Comma-separated IDs. Leave empty to deny all chats.'})+
      field('telegram_agent_id','Message recipient',settings.telegram_agent_id||'a-lead','select',{options:agentOptions})+
      '<div class="info-box">Register https://YOUR-DOMAIN/webhooks/telegram with setWebhook and the same secret_token. Forward only /webhooks/* to the local runtime. Set PUBLIC_WEBHOOK_HOST in .env.</div>';
  }
  return html+'<div class="row-actions wrap">'+button('Restart runtime','restart-runtime','','quiet small','refresh')+'</div>';
}

// Settings written by the connection form: the connection mode, webhook extras only in webhook mode, and (when a chat is linked) the web-editable parts of settings.telegram.
export function telegramSettings(body={},values={has:()=>false,getAll:()=>[]},tg={}) {
  const mode=body.telegram_mode==='webhook'?'webhook':'polling';
  const settings={telegram_mode:mode};
  if(mode==='webhook') {
    if('telegram_chats' in body) settings.telegram_chats=body.telegram_chats;
    if('telegram_agent_id' in body) settings.telegram_agent_id=body.telegram_agent_id;
  }
  if(tg&&tg.linked) {
    settings.telegram={
      default_agent_id:body.tg_default_agent||tg.default_agent_id||'a-lead',
      forward:Object.fromEntries(FORWARD.map(([key])=>[key,values.has('tg_fwd_'+key)])),
      owner_user_ids:values.getAll('tg_owner').map(Number).filter(Number.isInteger)
    };
  }
  return settings;
}
