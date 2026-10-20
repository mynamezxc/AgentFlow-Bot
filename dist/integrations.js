import './mcp-ui.js'; // MCP servers section (fills [data-mcp-mount]) + /mcp dialog

import {geminiStatusMarkup,geminiModelsMarkup,geminiPanelMarkup} from './gemini-ui.js';

export function createIntegrations(h) {
  const {esc,icon,button,field,openModal,closeModal,api,refresh,toast}=h;
  const provider=()=>h.getData().runtime?.providers?.find(p=>p.id==='gemini');
  const geminiModels=()=>(h.getData().runtime?.models||[]).filter(m=>m.provider==='gemini');
  let waiting=false,pollId=0;
  const paintGemini=()=>{
    const panel=document.getElementById('gemini-panel');if(!panel) return false;
    panel.querySelector('#gemini-status').innerHTML=geminiStatusMarkup(provider(),esc,waiting);
    panel.querySelector('#gemini-models').innerHTML=geminiModelsMarkup(geminiModels(),esc);
    return true;
  };
  const geminiBody=()=>geminiPanelMarkup(provider(),geminiModels(),{esc,field,button,waiting});
  /** After the console opens, poll provider status until the sign-in lands (or 2 minutes pass). */
  async function pollSignIn() {
    const token=++pollId,before=provider(),signature=p=>[p?.connected,p?.account_label,p?.auth].join('|'),start=signature(before);
    waiting=true;paintGemini();
    for(let i=0;i<48&&token===pollId;i++) {
      await new Promise(r=>setTimeout(r,2500));
      if(token!==pollId||!document.getElementById('gemini-panel')) break;
      await refresh(false);
      const now=provider();
      if(now?.connected&&signature(now)!==start||now?.connected&&!before?.connected) {waiting=false;paintGemini();toast('Gemini signed in'+(now.account_label?' as '+now.account_label:'')+'.');return;}
    }
    if(token===pollId){waiting=false;paintGemini();}
  }
  async function gemini(action) {
    if(h.getMode?.()==='demo') throw new Error('Open the local app to connect Gemini. The preview does not store credentials.');
    if(action==='gemini-login') {
      await api('/providers/gemini/login',{});
      toast('A sign-in window opened on the PC. Finish signing in with Google there.');
      pollSignIn();
    } else if(action==='gemini-key-save') {
      const input=document.getElementById('f-GEMINI_API_KEY'),value=input?.value.trim();
      if(!value) throw new Error('Paste your Gemini API key first.');
      await api('/providers/gemini/key',{api_key:value});
      input.value='';await refresh(false);paintGemini();toast('Gemini API key saved. Image generation can now use Gemini.');
    } else if(action==='gemini-key-remove') {
      await api('/providers/gemini/key',{api_key:null});
      await refresh(false);paintGemini();toast('Gemini API key removed.');
    }
    document.querySelector('#modal-error')&&(document.querySelector('#modal-error').textContent='');
    return true;
  }
  const state=()=>h.getData().runtime?.plugins||{catalog:[],accounts:[]};
  const check=(name,value,label,checked=false)=>'<label class="checkbox"><input type="checkbox" name="'+name+'" value="'+esc(value)+'" '+(checked?'checked':'')+'><span>'+esc(label)+'</span></label>';
  function view() {
    const {catalog,accounts}=state();
    return '<section class="panel model-list"><div class="section-heading"><h2>Apps & accounts</h2><span class="count">'+accounts.length+'</span></div><p>Install apps and authorize operations for each agent. Credentials stay in the broker; never paste secrets into chat.</p>'+
      (!catalog.length?'<p class="info-box">Open the local app to install Gmail, Postman, Cloudflare, Hugging Face, SSH and web/API integrations.</p>':'<div class="connections-grid">'+catalog.map(p=>'<article class="connection"><span class="connection-logo">'+icon('plug')+'</span><h3>'+esc(p.name)+'</h3><p>'+esc(p.description)+'</p><span class="status '+(p.installed?'ready':'idle')+'">'+(p.installed?'Adapter installed':'Not installed')+'</span>'+
        (p.installed?button('Connect account','app-account-new',p.id,'','plus')+button('Uninstall app','app-remove',p.id,'quiet small'):button('Install app','app-install',p.id,'','plus'))+'</article>').join('')+'</div>')+
      '<div class="account-list">'+accounts.map(a=>'<article class="model-row"><div><strong>'+esc(a.label)+'</strong><p class="small muted">'+esc(catalog.find(p=>p.id===a.plugin_id)?.name||a.plugin_id)+' · '+(a.enabled?'Access granted':'Revoked')+' · '+a.operations.length+' operations · '+(a.verified_at?'Operation verified':'Connection not verified')+'</p></div><div class="row-actions">'+button('Manage','app-account-edit',a.id,'small')+(a.enabled?button('Revoke','app-account-revoke',a.id,'quiet small'):'')+'</div></article>').join('')+'</div></section><section class="panel model-list mcp-section" data-mcp-mount></section>';
  }
  function accountForm(pluginId,id='') {
    const {catalog,accounts}=state(),p=catalog.find(p=>p.id===pluginId),a=accounts.find(a=>a.id===id);
    const defaults={cloudflare:{zone_ids:[]},ssh:{host:'',port:22,username:'',host_key:'',actions:{status:'uptime'}},web:{origin:'',routes:{status:{method:'GET',path:'/api/status'}}}};
    const config=a?.config||defaults[pluginId]||{};
    const secretLabels={token:'API token / API key',access_token:'OAuth access token',refresh_token:'OAuth refresh token',client_id:'OAuth client ID',client_secret:'OAuth client secret',private_key:'SSH private key (PEM / OpenSSH)',passphrase:'Private key passphrase',cookie:'Session cookie'};
    const agents=h.getData().agents.filter(a=>!a.archived&&!a.sample);
    openModal((a?'Manage ':'Connections ')+p.name,'Only you manage credentials. Agents receive connection names and authorized operations.',
      field('label','Connection name',a?.label||'','text',{required:true,placeholder:'Example: Company account'})+
      '<h3 class="form-subheading">Credentials</h3><p class="muted">Leave all fields empty to keep saved credentials. New credentials replace the entire set; include the full OAuth set when using refresh tokens.</p>'+
      p.secret_fields.map(key=>field(key,secretLabels[key]||key,'', 'password',{help:key==='private_key'?'Paste the full key. This field is never returned by the API.':''})).join('')+
      (Object.keys(config).length?field('config','Connection settings (JSON)',JSON.stringify(config,null,2),'textarea',{rows:7,required:true,help:pluginId==='ssh'?'host_key must match the target. Actions are fixed commands; agents choose only action names.':pluginId==='cloudflare'?'Only listed zone_ids are accessible.':'Only this fixed origin and route can be called. Enter credentials in the secret fields, outside the JSON.'}):'')+
      '<h3 class="form-subheading">Allowed agents</h3><div class="checkbox-group">'+check('agent_ids','*','All agents, including future teammates',a?.agent_ids.includes('*'))+agents.map(agent=>check('agent_ids',agent.id,agent.name,a?.agent_ids.includes(agent.id))).join('')+'</div>'+
      '<h3 class="form-subheading">Allowed operations</h3><p class="muted">Permissions apply until revoked. Write, send and execute operations may directly affect the account.</p><div class="checkbox-group">'+Object.keys(p.operations).map(op=>check('operations',op,op+(p.write_operations.includes(op)?' · Write / send / execute':''),a?a.operations.includes(op):!p.write_operations.includes(op))).join('')+'</div>'+
      check('enabled','true','Enable this connection',a?a.enabled:true)+
      (p.docs?'<p><a class="text-link" target="_blank" rel="noopener noreferrer" href="'+esc(p.docs)+'">Official connection documentation '+icon('arrow')+'</a></p>':''),
      button('Cancel','modal-close')+'<button class="button primary" type="submit">Save connection & permissions</button>','app-account',id||pluginId);
    if(pluginId==='ssh') {
      // Password inputs normally strip PEM line breaks; encode the paste before
      // the browser normalizes it, keeping the UI masked and the complete key.
      document.querySelector('#f-private_key').addEventListener('paste',event=>{
        const text=event.clipboardData?.getData('text');
        if(text?.includes('PRIVATE KEY')) {
          event.preventDefault();if(text.length>32000){toast('SSH private key exceeds 32 KB.',true);return;}
          event.target.value='base64:'+btoa(String.fromCharCode(...new TextEncoder().encode(text)));
          event.target.dispatchEvent(new Event('input',{bubbles:true}));
        }
      });
    }
  }
  async function perform(action,id,target) {
    if(action.startsWith('gemini-')) {
      if(target?.disabled) return true;
      if(target){target.disabled=true;target.setAttribute('aria-busy','true');}
      try {return await gemini(action);}
      finally {if(target?.isConnected){target.disabled=false;target.removeAttribute('aria-busy');}}
    }
    if(!action.startsWith('app-')) return false;
    if(target?.disabled) return true;
    if(target){target.disabled=true;target.setAttribute('aria-busy','true');}
    try {
    if(action==='app-account-new') accountForm(id);
    if(action==='app-account-edit') accountForm(state().accounts.find(a=>a.id===id).plugin_id,id);
    if(['app-remove','app-account-revoke'].includes(action)) {
      const remove=action==='app-remove',a=state().accounts.find(a=>a.id===id),p=state().catalog.find(p=>p.id===id);
      openModal(remove?'Remove '+p.name:'Revoke '+a.label,'Agents will no longer be able to start operations through this connection.',
        '<p>'+esc(remove?'All connections for this app will lose access. Reinstalling does not restore permissions automatically.':'Work history is retained. Reconnect through Manage when needed.')+'</p>',
        button('Keep','modal-close')+button('Confirm revocation',remove?'app-remove-confirm':'app-revoke-confirm',id,'danger'));
    }
    if(action==='app-install'||action==='app-remove-confirm') {
      await api('/plugins/install',{plugin_id:id,enabled:action==='app-install'});closeModal(true);await refresh();toast(action==='app-install'?'Adapter installed. Connect an account to grant access.':'App uninstalled and access revoked.');
    }
    if(action==='app-revoke-confirm') {
      const a=state().accounts.find(a=>a.id===id);
      await api('/plugins/accounts/revoke',{id,version:a.version});closeModal(true);await refresh();toast('Connection revoked.');
    }
    return true;
    } finally {if(target?.isConnected){target.disabled=false;target.removeAttribute('aria-busy');}}
  }
  async function submit(form) {
    if(form.dataset.type!=='app-account') return false;
    if(form.dataset.submitting||!h.validateForm(form)) return true;
    form.dataset.submitting='true';const submit=form.querySelector('[type=submit]');submit.disabled=true;
    try {
      const values=new FormData(form),previous=state().accounts.find(a=>a.id===form.dataset.id),pluginId=previous?.plugin_id||form.dataset.id,p=state().catalog.find(p=>p.id===pluginId);
      let config={};try{config=JSON.parse(values.get('config')||'{}');}catch{throw new Error('Connection settings must be valid JSON.');}
      const secrets=Object.fromEntries(p.secret_fields.filter(k=>values.get(k)).map(k=>[k,values.get(k)]));
      const result=await api('/plugins/accounts/save',{...(previous?{id:previous.id,version:previous.version}:{}),plugin_id:pluginId,label:values.get('label'),config,secrets,
        agent_ids:values.getAll('agent_ids'),operations:values.getAll('operations'),enabled:values.has('enabled')});
      closeModal(true);await refresh();toast(result.notice||'Connection saved. Only authorized agents and operations can use it.');
    } catch(e) {const box=document.querySelector('#modal-error');box.textContent=e.message;box.focus();}
    finally {delete form.dataset.submitting;if(submit.isConnected)submit.disabled=false;}
    return true;
  }
  return {view,perform,submit,geminiBody};
}
