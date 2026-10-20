// MCP connections UI. Listens for `agentflow:open-mcp` (the /mcp slash command) and opens a self-contained
// "Connect MCP server" dialog from any page; also fills every [data-mcp-mount] element (Connections page)
// with the list of servers. Secrets are write-only: the API only returns key names and a `set` flag.
const esc=(v='')=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const state={servers:[],loaded:0,error:'',busy:new Set(),fetching:false};
let dialog=null,form=null,opener=null,toastTimer=0;

function stylesheet() {
  if(document.querySelector('link[data-mcp-css]')) return;
  const link=document.createElement('link');link.rel='stylesheet';link.href='/mcp-ui.css';link.dataset.mcpCss='1';document.head.append(link);
}
async function api(path,body,method='POST') {
  let response;
  try {response=await fetch('/api'+path,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body),signal:AbortSignal.timeout(120000)});}
  catch {throw new Error('Cannot reach the runtime. Check the local service and retry.');}
  let result={};try {result=await response.json();} catch {/* non-JSON error page */}
  if(!response.ok) throw new Error(result.error||'Unable to complete this action.');
  return result;
}
function toast(message,error=false) {
  const node=document.querySelector('#toast');if(!node) return;
  clearTimeout(toastTimer);node.textContent=message;node.className='visible'+(error?' error':'');
  toastTimer=setTimeout(()=>{node.className='';},4500);
}
function ago(iso) {
  const t=Date.parse(iso||'');if(!t) return '';
  const s=Math.max(0,Math.round((Date.now()-t)/1000));
  return s<60?'just now':s<3600?Math.round(s/60)+' min ago':s<86400?Math.round(s/3600)+' h ago':Math.round(s/86400)+' d ago';
}
const upsert=server=>{const i=state.servers.findIndex(s=>s.id===server.id);if(i<0) state.servers.push(server);else state.servers[i]=server;};

// ---- Connections page list ---------------------------------------------------------------------------
function where(s) {
  return s.transport==='http'?'HTTP · '+s.url:'Local command · '+[s.command,...(s.args||[])].join(' ');
}
function row(s) {
  const busy=state.busy.has(s.id),enabled=s.enabled!==false;
  const dot=busy?'busy':!enabled?'':s.status==='ok'?'ok':s.status==='error'?'error':'';
  const line=busy?'<span class="mcp-state">Testing connection…</span>':!enabled?'<span class="mcp-state">Disabled · agents cannot use it</span>'
    :s.status==='error'?'<span class="mcp-state error">'+esc(s.error||'Connection failed.')+'</span>'
    :s.status==='ok'?'<span class="mcp-state">'+s.tool_count+' tool'+(s.tool_count===1?'':'s')+' · checked '+esc(ago(s.checked_at))+'</span>'
    :'<span class="mcp-state">Not tested yet · agents see no tools until you test it</span>';
  return '<article class="mcp-row" data-id="'+esc(s.id)+'"><span class="mcp-dot '+dot+'" role="img" aria-label="'+esc(busy?'Testing':s.status)+'"></span>'+
    '<div class="mcp-main"><div class="mcp-title"><strong>'+esc(s.name)+'</strong><span class="status '+(s.trusted?'done':'idle')+'">'+(s.trusted?'Trusted':'Asks before calls')+'</span></div>'+
    '<div class="mcp-where" title="'+esc(where(s))+'">'+esc(where(s))+'</div>'+line+'</div>'+
    '<div class="mcp-actions"><button type="button" class="button small" data-mcp-action="test" data-id="'+esc(s.id)+'"'+(busy?' disabled':'')+'>Test</button>'+
    '<button type="button" class="button small" data-mcp-action="edit" data-id="'+esc(s.id)+'">Edit</button>'+
    '<button type="button" class="button quiet small" data-mcp-action="remove" data-id="'+esc(s.id)+'">Remove</button></div></article>';
}
function section() {
  return '<div class="section-heading"><h2>MCP servers</h2><span class="count">'+state.servers.length+'</span><span class="grow"></span>'+
    '<button type="button" class="button primary small" data-mcp-action="new">Connect MCP server</button></div>'+
    '<p class="mcp-lead">Give agents extra tools from Model Context Protocol servers. You manage the servers; agents can only list and call their tools, and untrusted servers ask you before every call. Type /mcp in chat to add one.</p>'+
    (state.error?'<p class="form-error" role="alert">'+esc(state.error)+'</p>':'')+
    (state.servers.length?state.servers.map(row).join(''):(state.loaded&&!state.error?'<div class="info-box">No MCP servers connected yet.</div>':!state.loaded&&!state.error?'<p class="mcp-lead">Loading…</p>':''));
}
function render() {
  document.querySelectorAll('[data-mcp-mount]').forEach(node=>{node.innerHTML=section();});
}
async function load() {
  if(state.fetching) return;
  state.fetching=true;
  try {
    const result=await api('/mcp/servers',undefined,'GET');
    state.servers=result.servers||[];state.error='';state.loaded=Date.now();
  } catch(error) {state.error=error.message;state.loaded=Date.now();}
  finally {state.fetching=false;render();}
}
function mountEmpty() {
  const empty=[...document.querySelectorAll('[data-mcp-mount]')].filter(n=>!n.firstChild);
  if(!empty.length) return;
  empty.forEach(n=>{n.innerHTML=section();});
  if(!state.loaded||Date.now()-state.loaded>10000) load();
}
async function testServer(id,quiet=false) {
  state.busy.add(id);render();
  try {
    const server=await api('/mcp/servers/'+encodeURIComponent(id)+'/test',{});
    upsert(server);
    if(!quiet) toast(server.status==='ok'?server.name+': '+server.tool_count+' tools available.':server.name+': '+(server.error||'connection failed'),server.status!=='ok');
    return server;
  } catch(error) {toast(error.message,true);}
  finally {state.busy.delete(id);render();}
}

// ---- dialog ------------------------------------------------------------------------------------------
function ensureDialog() {
  if(dialog) return dialog;
  dialog=document.createElement('dialog');dialog.className='mcp-dialog';dialog.setAttribute('aria-labelledby','mcp-title');
  document.body.append(dialog);
  dialog.addEventListener('close',()=>{form=null;if(opener?.isConnected) opener.focus();});
  dialog.addEventListener('submit',event=>{event.preventDefault();});
  dialog.addEventListener('click',event=>{
    const target=event.target.closest('[data-mcp-dialog]');
    if(!target) return;
    const action=target.dataset.mcpDialog;
    if(action==='close') dialog.close();
    else if(action==='transport') {syncForm();form.transport=target.dataset.value;paint();}
    else if(action==='row-add') {syncForm();secrets().push({key:'',value:'',set:false});paint();const keys=dialog.querySelectorAll('.mcp-key');keys[keys.length-1]?.focus();}
    else if(action==='row-del') {syncForm();secrets().splice(Number(target.dataset.i),1);paint();}
    else if(action==='test') runTest();
    else if(action==='save') runSave();
    else if(action==='confirm-remove') runRemove();
  });
  return dialog;
}
const X='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6"/></svg>';
const secrets=()=>form.transport==='http'?form.headers:form.env;
function blank() {
  return {id:'',name:'',transport:'stdio',command:'',args:'',url:'',trusted:false,enabled:true,env:[],headers:[],result:null,busy:'',error:'',removing:false};
}
function fromServer(s) {
  return {...blank(),id:s.id,name:s.name,transport:s.transport,command:s.command||'',args:(s.args||[]).join('\n'),url:s.url||'',trusted:!!s.trusted,enabled:s.enabled!==false,
    env:(s.env_keys||[]).map(k=>({key:k.key,value:'',set:k.set})),headers:(s.header_keys||[]).map(k=>({key:k.key,value:'',set:k.set})),
    result:s.status==='ok'||s.status==='error'?s:null};
}
function syncForm() {
  if(!form||!dialog) return;
  const q=sel=>dialog.querySelector(sel);
  if(q('#mcp-name')) form.name=q('#mcp-name').value;
  if(form.transport==='stdio'&&q('#mcp-command')) {form.command=q('#mcp-command').value;form.args=q('#mcp-args').value;}
  if(form.transport==='http'&&q('#mcp-url')) form.url=q('#mcp-url').value;
  if(q('#mcp-trusted')) form.trusted=q('#mcp-trusted').checked;
  if(q('#mcp-enabled')) form.enabled=q('#mcp-enabled').checked;
  dialog.querySelectorAll('.mcp-secret-row').forEach(node=>{
    const item=secrets()[Number(node.dataset.i)];if(!item) return;
    if(!item.set) item.key=node.querySelector('.mcp-key').value;
    item.value=node.querySelector('.mcp-val').value;
  });
}
function resultHtml(r) {
  if(form.busy==='test') return '<div class="info-box" role="status"><span class="spinner"></span> Connecting and listing tools…</div>';
  if(!r) return '';
  if(r.status==='error') return '<div class="form-error" role="alert"><strong>Connection failed.</strong>\n'+esc(r.error||'')+'</div>';
  if(r.status!=='ok') return '';
  return '<div class="mcp-tools" role="status"><strong>Connected · '+r.tool_count+' tool'+(r.tool_count===1?'':'s')+'</strong>'+
    (r.tools||[]).slice(0,60).map(t=>'<div class="mcp-tool"><span class="mono">'+esc(t.name)+'</span><span class="muted">'+esc((t.description||'').split('\n')[0].slice(0,120))+'</span></div>').join('')+
    (r.tool_count>60?'<div class="muted small">and '+(r.tool_count-60)+' more</div>':'')+'</div>';
}
function secretRows() {
  const http=form.transport==='http';
  return secrets().map((item,i)=>'<div class="mcp-secret-row" data-row="'+form.transport+'" data-i="'+i+'">'+
    '<input class="mcp-key" aria-label="'+(http?'Header name':'Variable name')+'" placeholder="'+(http?'Authorization':'API_KEY')+'" value="'+esc(item.key)+'" autocomplete="off" spellcheck="false"'+(item.set?' readonly':'')+'>'+
    '<input class="mcp-val" type="password" aria-label="Value" placeholder="'+(item.set?'Saved · leave blank to keep':'Value (write-only)')+'" value="'+esc(item.value)+'" autocomplete="new-password">'+
    '<button type="button" class="icon-button" data-mcp-dialog="row-del" data-i="'+i+'" aria-label="Remove '+esc(item.key||'row')+'">'+
    X+'</button></div>').join('');
}
function paint() {
  const http=form.transport==='http',editing=!!form.id;
  if(form.removing) {
    dialog.innerHTML='<div class="modal-head"><div><h2 id="mcp-title">Remove '+esc(form.name)+'</h2><p>Agents lose access immediately.</p></div>'+
      '<button type="button" class="icon-button" data-mcp-dialog="close" aria-label="Close dialog">'+X+'</button></div>'+
      '<div class="modal-body"><p>Stored secrets for this server are deleted and its process is stopped. You can connect it again later.</p><div id="mcp-error" class="form-error" role="alert">'+esc(form.error)+'</div></div>'+
      '<div class="modal-footer"><button type="button" class="button" data-mcp-dialog="close">Keep</button><button type="button" class="button danger" data-mcp-dialog="confirm-remove">Remove server</button></div>';
    return;
  }
  const busy=!!form.busy;
  dialog.innerHTML='<div class="modal-head"><div><h2 id="mcp-title">'+(editing?'Edit MCP server':'Connect MCP server')+'</h2><p>Add tools from a Model Context Protocol server for your agents.</p></div>'+
    '<button type="button" class="icon-button" data-mcp-dialog="close" aria-label="Close dialog">'+X+'</button></div>'+
    '<form class="modal-body" novalidate autocomplete="off">'+
    '<div class="field"><label for="mcp-name">Name</label><input id="mcp-name" maxlength="60" placeholder="Example: Filesystem" value="'+esc(form.name)+'"><span class="help">Agents see this name. 1 to 60 characters.</span></div>'+
    '<div class="field"><label id="mcp-transport-label">Connection type</label><div class="mcp-seg" role="group" aria-labelledby="mcp-transport-label">'+
      '<button type="button" data-mcp-dialog="transport" data-value="stdio" aria-pressed="'+!http+'">Local command</button>'+
      '<button type="button" data-mcp-dialog="transport" data-value="http" aria-pressed="'+http+'">HTTP URL</button></div></div>'+
    (http?'<div class="field"><label for="mcp-url">Server URL</label><input id="mcp-url" inputmode="url" placeholder="https://example.com/mcp" value="'+esc(form.url)+'" spellcheck="false"><span class="help">https:// only (http:// is allowed for 127.0.0.1 and localhost). Put keys in headers, not in the URL.</span></div>'
      :'<div class="field"><label for="mcp-command">Command</label><input id="mcp-command" class="mono" placeholder="npx" value="'+esc(form.command)+'" spellcheck="false"><span class="help">A program name such as npx or uvx, or an absolute path. Not a shell command line.</span></div>'+
        '<div class="field"><label for="mcp-args">Arguments (one per line)</label><textarea id="mcp-args" class="mono" rows="4" spellcheck="false" placeholder="-y\n@modelcontextprotocol/server-filesystem\nC:\\Projects">'+esc(form.args)+'</textarea></div>')+
    '<h3 class="form-subheading">'+(http?'Headers':'Environment variables')+' <span class="muted">(secrets)</span></h3>'+
    '<p class="muted small mcp-note">'+(http?'For example Authorization: Bearer … or X-API-Key. ':'For example API keys the server needs. ')+'Values are stored in the credential vault and never shown again.</p>'+
    secretRows()+'<button type="button" class="button small" data-mcp-dialog="row-add">Add '+(http?'header':'variable')+'</button>'+
    '<div class="mcp-checks"><label class="checkbox"><input type="checkbox" id="mcp-trusted"'+(form.trusted?' checked':'')+'><span>Allow agents to call without asking <span class="muted">· otherwise you approve each call</span></span></label>'+
    (editing?'<label class="checkbox"><input type="checkbox" id="mcp-enabled"'+(form.enabled?' checked':'')+'><span>Enabled</span></label>':'')+'</div>'+
    '<div id="mcp-result" class="mcp-result" aria-live="polite">'+resultHtml(form.result)+'</div>'+
    '<div id="mcp-error" class="form-error" role="alert" tabindex="-1">'+esc(form.error)+'</div></form>'+
    '<div class="modal-footer"><button type="button" class="button" data-mcp-dialog="close">Cancel</button>'+
    '<button type="button" class="button" data-mcp-dialog="test"'+(busy?' disabled':'')+'>Save &amp; test connection</button>'+
    '<button type="button" class="button primary" data-mcp-dialog="save"'+(busy?' disabled':'')+'>'+(form.busy==='save'?'Saving…':'Save')+'</button></div>';
}
function payload() {
  const http=form.transport==='http',map={};
  for(const item of secrets()) {
    const key=item.key.trim();
    if(!key&&!item.value) continue;
    if(!key) throw new Error('Enter a name for every '+(http?'header':'variable')+' row, or remove the row.');
    if(key in map) throw new Error('Duplicate name: '+key);
    map[key]=item.value;
  }
  const body={name:form.name.trim(),transport:form.transport,trusted:form.trusted,enabled:form.enabled};
  if(form.id) body.id=form.id;
  if(http) {body.url=form.url.trim();body.headers=map;}
  else {body.command=form.command.trim();body.args=form.args.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);body.env=map;}
  return body;
}
async function persist() {
  syncForm();
  const server=await api('/mcp/servers',payload());
  upsert(server);form.id=server.id;
  const keep=server.transport==='http'?'headers':'env',source=server.transport==='http'?server.header_keys:server.env_keys;
  form[keep]=(source||[]).map(k=>({key:k.key,value:'',set:k.set}));
  render();
  return server;
}
async function runSave() {
  if(form.busy) return;
  form.busy='save';form.error='';const snapshot=form;
  try {
    const server=await persist();
    dialog.close();toast('MCP server saved.');
    if(server.status==='untested') testServer(server.id);  // agents only see tools after a successful test
  } catch(error) {
    if(form===snapshot) {form.error=error.message;form.busy='';paint();dialog.querySelector('#mcp-error')?.focus();}
  }
}
async function runTest() {
  if(form.busy) return;
  form.error='';form.busy='test';const snapshot=form;
  try {
    syncForm();payload();  // surface row mistakes before touching the server
    form.busy='test';paint();
    const saved=await persist();
    const tested=await api('/mcp/servers/'+encodeURIComponent(saved.id)+'/test',{});
    upsert(tested);render();
    if(form===snapshot) form.result=tested;
  } catch(error) {if(form===snapshot) form.error=error.message;}
  finally {if(form===snapshot) {form.busy='';paint();dialog.querySelector('#mcp-result')?.scrollIntoView({block:'nearest'});}}
}
async function runRemove() {
  const id=form.id;
  try {await api('/mcp/servers/'+encodeURIComponent(id)+'/remove',{});state.servers=state.servers.filter(s=>s.id!==id);render();dialog.close();toast('MCP server removed.');}
  catch(error) {form.error=error.message;paint();}
}
function open(server=null,removing=false) {
  ensureDialog();stylesheet();
  if(!dialog.open) opener=document.activeElement;
  form=server?fromServer(server):blank();form.removing=removing;
  paint();
  if(dialog.open) return;
  dialog.showModal();
  dialog.querySelector(removing?'.button':'#mcp-name')?.focus();
}

document.addEventListener('click',event=>{
  const target=event.target.closest?.('[data-mcp-action]');
  if(!target||target.disabled) return;
  const id=target.dataset.id,server=state.servers.find(s=>s.id===id);
  switch(target.dataset.mcpAction) {
    case 'new': open(); break;
    case 'edit': if(server) open(server); break;
    case 'remove': if(server) open(server,true); break;
    case 'test': if(server) testServer(id); break;
  }
});
window.addEventListener('agentflow:open-mcp',()=>{open();load();});
new MutationObserver(mountEmpty).observe(document.documentElement,{childList:true,subtree:true});
stylesheet();mountEmpty();
