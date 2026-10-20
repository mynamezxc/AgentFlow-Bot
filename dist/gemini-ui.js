// Gemini connection card/dialog markup (pure; covered by tests/image-studio.mjs).
/* ---- Gemini connection (pure markup builders, covered by tests/image-studio.mjs) ---- */
export const GEMINI_AUTH_LABELS={oauth:'Google sign-in',api_key:'API key',vertex:'Vertex AI'};
export const GEMINI_SOURCE_LABELS={acp:'Gemini CLI',api:'Gemini API',aliases:'CLI aliases'};
/** Model/provider `source` values come from the backend ('acp', 'api', 'aliases' or free text); show a plain label. */
export function modelSourceLabel(source) {
  const text=String(source||'').toLowerCase();
  if(!text) return '';
  if(GEMINI_SOURCE_LABELS[text]) return GEMINI_SOURCE_LABELS[text];
  if(text.includes('acp')||text.includes('cli')&&!text.includes('alias')) return 'Gemini CLI';
  if(text.includes('alias')) return 'CLI aliases';
  if(text.includes('api')) return 'Gemini API';
  return String(source);
}
export function geminiStatusMarkup(p,esc,waiting=false) {
  const chip=(cls,text)=>'<span class="gm-chip '+cls+'">'+esc(text)+'</span>';
  if(!p) return '<p class="muted">Provider status is not available yet. Reload the page after restarting AgentFlow.</p>';
  const auth=GEMINI_AUTH_LABELS[p.auth];
  return '<div class="gm-status">'+
    chip(p.installed?'ok':'warn',p.installed?'CLI found':'CLI not found')+
    chip(p.connected?'ok':'idle',p.connected?'Connected':'Not signed in')+
    (auth?chip('',auth):'')+
    chip(p.api_key_set?'ok':'idle',p.api_key_set?'Key saved':'No API key')+
    (p.models_source?chip('idle','Models: '+modelSourceLabel(p.models_source)):'')+'</div>'+
    (p.account_label?'<p class="small muted" style="margin-top:8px">Current account: <strong>'+esc(p.account_label)+'</strong></p>':'')+
    (waiting?'<p class="small" role="status" style="margin-top:8px"><span class="spinner"></span> Waiting for sign-in to finish in the console window\u2026</p>':'')+
    (p.error?'<p class="small connection-error" style="margin-top:8px">'+esc(p.error)+'</p>':'');
}
export function geminiModelsMarkup(models,esc) {
  if(!models.length) return '<p class="muted small">No Gemini models loaded yet. Sign in or save an API key, then load the catalog.</p>';
  return '<div class="gm-models" role="list">'+models.map(m=>{
    const caps=(Array.isArray(m.capabilities)&&m.capabilities.length?m.capabilities:['chat']);
    return '<div class="gm-model" role="listitem"><span><strong>'+esc(m.name||m.id)+'</strong><span class="mono muted">'+esc(m.id)+'</span></span><span class="gm-model-tags">'+caps.map(c=>'<span class="gm-chip idle">'+esc(c==='image'?'Image':c==='chat'?'Chat':c)+'</span>').join('')+(m.source?'<span class="gm-src">'+esc(modelSourceLabel(m.source))+'</span>':'')+'</span></div>';
  }).join('')+'</div>';
}
export function geminiPanelMarkup(p,models,{esc,field,button,waiting=false}) {
  const installed=!!p?.installed;
  return '<div class="gm-grid" id="gemini-panel">'+
    '<div id="gemini-status">'+geminiStatusMarkup(p,esc,waiting)+'</div>'+
    '<section><h3 class="form-subheading">Sign in with Google</h3><p class="small muted">Opens a console window on the PC that runs AgentFlow. Choose your Google account there; this page updates by itself when sign-in finishes. Sign-in covers chat models only.</p>'+
    '<div class="row-actions" style="margin-top:10px">'+button(p?.connected?'Sign in again':'Sign in with Google','gemini-login','',p?.connected?'':'primary','globe')+'</div>'+
    (installed?'':'<p class="small muted" style="margin-top:8px">Gemini CLI was not found. Install it on the AgentFlow machine, or set its path in .env, then reload.</p>')+'</section>'+
    '<section><h3 class="form-subheading">Gemini API key</h3>'+
    '<div class="gm-key-row">'+field('GEMINI_API_KEY','API key','','password',{placeholder:p?.api_key_set?'Key saved \u2014 enter a new key to replace it':'Paste your Gemini API key',help:'Needed for image generation. The key is stored in the Windows credential vault and never shown again.'})+'</div>'+
    '<div class="gm-key-actions">'+button('Save key','gemini-key-save','','','check')+(p?.api_key_set?button('Remove key','gemini-key-remove','','quiet'):'')+(p?.api_key_set?'<span class="gm-chip ok" role="status">Key saved</span>':'')+'</div></section>'+
    '<section><h3 class="form-subheading">Models</h3><div id="gemini-models">'+geminiModelsMarkup(models,esc)+'</div></section></div>';
}
