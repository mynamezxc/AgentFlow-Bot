// Dedicated file preview pane: opens beside the chat (between the conversation and the right bar), is resizable,
// can open the file in its desktop app and lets the owner edit and save text files.
// Pure helpers are exported for tests/preview-pane.mjs; createPreviewPane wires them to the DOM.

export const MIN_WIDTH=360,MIN_CHAT=360,MAX_TABS=6,STEP=24,CONTEXT_WIDTH=350,EDIT_LIMIT=2*1024*1024;
export const EDITABLE_SUFFIXES=new Set(['.md','.txt','.json','.csv','.html','.css','.js','.py','.svg','.toml','.yaml','.yml','.ts','.tsx','.jsx','.xml','.sql','.ini','.cfg','.sh','.ps1','.log']);
// Scripts and web files are never handed to the desktop (they could run code); the server enforces the same list.
export const NO_NATIVE_OPEN=new Set(['.js','.py','.ps1','.sh','.html','.svg']);

export const suffixOf=path=>{const m=/\.[^./\\]+$/.exec(String(path||''));return m?m[0].toLowerCase():'';};
export const isEditableType=(path,bytes)=>EDITABLE_SUFFIXES.has(suffixOf(path))&&!(Number(bytes)>EDIT_LIMIT);
export const canOpenNative=path=>!NO_NATIVE_OPEN.has(suffixOf(path));
export const baseName=path=>String(path||'').split(/[\\/]/).filter(Boolean).at(-1)||String(path||'');

/** Pane width limits: at least 360px, and the chat keeps at least 360px of the available width. */
export const widthRange=available=>({min:MIN_WIDTH,max:Math.max(MIN_WIDTH,Math.floor(Number(available)||0)-MIN_CHAT)});
export function clampWidth(width,available) {
  const {min,max}=widthRange(available),w=Number(width);
  return Math.round(Math.min(max,Math.max(min,Number.isFinite(w)?w:min)));
}
export const defaultWidth=available=>clampWidth(Math.round((Number(available)||0)*0.5),available);
/** Keyboard resizing on the handle at the pane's left edge: Left widens, Right narrows, Home = narrowest, End = widest. */
export function stepWidth(width,key,available) {
  const {min,max}=widthRange(available);
  if(key==='ArrowLeft') return clampWidth(width+STEP,available);
  if(key==='ArrowRight') return clampWidth(width-STEP,available);
  if(key==='Home') return min;
  if(key==='End') return max;
  return null;
}

export const tabKey=(source,id)=>source+':'+id;
export const isDirty=tab=>!!tab&&typeof tab.draft==='string'&&tab.draft!==tab.content;
/** Closing a tab with unsaved edits needs the owner's confirmation. */
export const needsConfirm=isDirty;
export const dirtyTabs=tabs=>tabs.filter(isDirty);
/** Add or activate a tab; past `max` the oldest clean tab is dropped (dirty tabs are never dropped silently). */
export function openTab(tabs,tab,max=MAX_TABS) {
  const existing=tabs.find(t=>t.key===tab.key);
  if(existing) return {tabs,active:existing.key,opened:false};
  const next=[...tabs,tab];
  while(next.length>max) {
    const drop=next.findIndex(t=>t.key!==tab.key&&!isDirty(t));
    if(drop<0) return {tabs,active:null,opened:false,rejected:true};
    next.splice(drop,1);
  }
  return {tabs:next,active:tab.key,opened:true};
}
/** Remove a tab; if it was active the neighbour that takes its place (else the previous one) becomes active. */
export function closeTab(tabs,key,active) {
  const index=tabs.findIndex(t=>t.key===key);
  if(index<0) return {tabs,active};
  const next=tabs.filter(t=>t.key!==key);
  return {tabs:next,active:active!==key?active:(next[index]||next[index-1]||null)?.key??null};
}

const isMarkdown=tab=>tab.suffix==='.md'||/\.md$/i.test(tab.path||'');
/** Views a file tab offers: rendered Preview, Source/Edit for text, Changes when a diff exists. */
export function viewTabs(tab) {
  const views=[];
  if(!tab.error) {
    if(tab.mode==='image'||tab.mode==='document'||tab.mode==='canvas'||isMarkdown(tab)) views.push('preview');
    if(tab.mode==='text'||tab.mode==='canvas') views.push('source');
  }
  if(tab.change||tab.hasChanges) views.push('changes');
  return views;
}
export function activeView(tab) {
  const views=viewTabs(tab);
  if(tab.view&&views.includes(tab.view)) return tab.view;
  if(tab.change&&views.includes('changes')) return 'changes';
  return views[0]||'preview';
}
export const viewLabel=(view,tab)=>view==='preview'?(tab.mode==='canvas'?'Canvas':'Preview'):view==='source'?(tab.editable?'Edit':'Source'):'Changes';

export function diffMarkup(change,esc) {
  const lines=change?.diff||[];
  return '<div class="diff-view" tabindex="0" role="tabpanel" aria-label="Changed lines">'+(lines.map(line=>'<div class="diff-line '+esc(line.kind)+'"><span>'+esc(line.old??'')+'</span><span>'+esc(line.new??'')+'</span><span>'+esc(line.kind==='add'?'+':line.kind==='delete'?'−':'')+'</span><code>'+esc(line.text)+'</code></div>').join('')||'<p class="context-note">No line changes.</p>')+'</div>'+
    (change?.diff_truncated?'<p class="context-note">Showing the first 1,500 lines. The Edit or Source tab has the full file.</p>':'');
}

export function createPreviewPane(h) {
  const {esc,icon,api,toast}=h;
  const WIDTH_KEY='agentflow-preview-width';
  const root=document.createElement('aside');
  root.id='co-preview-pane';root.className='preview-pane';root.setAttribute('aria-label','File preview');
  let tabs=[],active=null,maximized=false,width=null,host=null,frame=0;
  const tab=()=>tabs.find(t=>t.key===active)||null;
  const readWidth=()=>{try{return Number(localStorage.getItem(WIDTH_KEY))||null;}catch{return null;}};
  const saveWidth=()=>{try{localStorage.setItem(WIDTH_KEY,String(width));}catch{}};
  const narrow=()=>innerWidth<900;
  const contextInline=()=>!!host?.classList.contains('with-context')&&innerWidth>1100;
  const available=()=>Math.max(0,(host?.clientWidth||innerWidth)-(contextInline()?CONTEXT_WIDTH:0));
  const q=sel=>root.querySelector(sel);

  function applyLayout() {
    if(!host) return;
    const open=tabs.length>0;
    host.classList.toggle('with-preview',open);
    host.classList.toggle('preview-max',open&&maximized);
    if(open) {
      width=clampWidth(width??readWidth()??defaultWidth(available()),available());
      host.style.setProperty('--pane-w',width+'px');
      const handle=q('.pp-handle');
      if(handle) {const {min,max}=widthRange(available());handle.setAttribute('aria-valuemin',min);handle.setAttribute('aria-valuemax',max);handle.setAttribute('aria-valuenow',width);}
    } else host.style.removeProperty('--pane-w');
  }
  /** Called after every full chat render: put the persistent pane node back beside the conversation. */
  function attach(chat) {
    host=chat||null;
    if(!host) return;
    if(tabs.length) {
      const conversation=host.querySelector('.conversation');
      if(conversation&&root.previousElementSibling!==conversation) conversation.after(root);
    } else root.remove();
    applyLayout();
  }
  const changed=()=>h.onChange?.();

  // ---- rendering -------------------------------------------------------------------------------
  function headMarkup() {
    const t=tab();
    return '<div class="pp-handle" role="separator" aria-orientation="vertical" tabindex="0" aria-label="Resize preview. Use left and right arrow keys; double-click to maximize." aria-valuemin="'+MIN_WIDTH+'" aria-valuemax="'+MIN_WIDTH+'" aria-valuenow="'+(width||MIN_WIDTH)+'"></div>'+
      '<header class="pp-head"><div class="pp-tabs" role="tablist" aria-label="Open files">'+tabs.map(x=>'<div class="pp-tab'+(x.key===active?' is-active':'')+(isDirty(x)?' is-dirty':'')+'" role="presentation"><button type="button" role="tab" class="pp-tab-main" aria-selected="'+(x.key===active)+'" data-pp="tab" data-key="'+esc(x.key)+'" title="'+esc(x.path||x.title||'')+'"><span class="pp-name">'+esc(x.title||baseName(x.path)||'Loading…')+'</span><i class="pp-dot" aria-label="Unsaved changes"></i></button><button type="button" class="pp-tab-close" data-pp="close-tab" data-key="'+esc(x.key)+'" aria-label="Close '+esc(x.title||baseName(x.path)||'file')+'">'+icon('close')+'</button></div>').join('')+'</div>'+
      '<div class="pp-actions">'+
        '<button type="button" class="icon-button" data-pp="max" aria-label="'+(maximized?'Restore size':'Maximize')+'" title="'+(maximized?'Restore (Esc)':'Maximize')+'">'+icon(maximized?'collapse':'expand')+'</button>'+
        '<button type="button" class="icon-button" data-pp="open-app" aria-label="Open in app" title="'+(t&&t.canOpen===false?'Scripts and web files are not opened in a desktop app':'Open in app')+'"'+(!t||t.kind==='change'||t.loading||t.error||t.canOpen===false?' disabled':'')+'>'+icon('folder')+'</button>'+
        '<button type="button" class="icon-button" data-pp="download" aria-label="Download" title="Download"'+(!t||t.loading?' disabled':'')+'>'+icon('download')+'</button>'+
        '<button type="button" class="icon-button" data-pp="close" aria-label="Close preview" title="Close">'+icon('close')+'</button>'+
      '</div></header>';
  }
  function metaMarkup(t) {
    return '<div class="pp-meta"><h3>'+esc(t.title||baseName(t.path))+'</h3><p class="preview-path">'+esc(t.path||'')+'</p>'+
      (t.agent_id?'<div class="preview-meta">'+h.agentMention(h.actor(t.agent_id))+'<small>'+esc(h.stamp(t.created_at))+'</small></div>':'')+'</div>';
  }
  function toolbarMarkup(t) {
    const dirty=isDirty(t);
    return '<div class="pp-toolbar"><button type="button" class="button primary small" data-pp="save"'+(!dirty||t.saving?' disabled':'')+'>'+(t.saving?'Saving…':'Save')+'</button><button type="button" class="button quiet small" data-pp="revert"'+(!dirty?' disabled':'')+'>Revert</button><span class="pp-status" role="status">'+esc(t.saving?'':dirty?'Unsaved changes · Ctrl/Cmd+S saves':t.savedAt?'Saved':'')+'</span></div>'+
      (t.conflict?'<div class="pp-conflict" role="alert"><p>This file changed on disk after you opened it, so nothing was overwritten.</p><div class="row-actions"><button type="button" class="button small" data-pp="reload">Reload from disk (discard my edits)</button><button type="button" class="button quiet small" data-pp="overwrite">Save mine anyway</button></div></div>':'')+
      (t.saveError?'<p class="inline-error" role="alert">'+esc(t.saveError)+'</p>':'');
  }
  function bodyMarkup(t) {
    if(t.loading) return '<p class="context-note pp-note" role="status">Loading…</p>';
    const views=viewTabs(t),view=activeView(t);
    const strip=views.length>1?'<div class="pp-views preview-tabs" role="tablist" aria-label="View">'+views.map(v=>'<button type="button" role="tab" aria-selected="'+(v===view)+'" data-pp="view" data-view="'+v+'">'+esc(viewLabel(v,t))+(v==='changes'&&t.change?' <span class="change-count"><b>+'+(t.change.added??0)+'</b><i>−'+(t.change.deleted??0)+'</i></span>':'')+(v==='source'&&isDirty(t)?' <i class="pp-dot is-on" aria-label="Unsaved changes"></i>':'')+'</button>').join('')+'</div>':'';
    let body='';
    if(t.error&&view!=='changes') body='<p class="inline-error" role="alert">'+esc(t.error)+'</p><button type="button" class="button small" data-pp="retry">Retry</button>';
    else if(view==='changes') body=changesMarkup(t);
    else if(view==='source') body=sourceMarkup(t);
    else body=previewMarkup(t);
    return strip+'<div class="pp-body" data-view="'+view+'">'+body+'</div>';
  }
  function changesMarkup(t) {
    const list=t.artifactId?h.changesFor(t.artifactId):[];
    const picker=list.length>1?'<label class="sr-only" for="pp-change-pick">Revision</label><select id="pp-change-pick" class="pp-pick" data-pp="pick-change">'+list.slice().reverse().map(c=>'<option value="'+esc(c.id)+'"'+(c.id===t.change?.id?' selected':'')+'>'+esc((c.agent_id==='owner'?'You':h.actor(c.agent_id).name)+' · '+h.stamp(c.created_at)+' · +'+(c.added??0)+' −'+(c.deleted??0))+'</option>').join('')+'</select>':'';
    if(t.changeLoading) return picker+'<p class="context-note pp-note" role="status">Loading changes…</p>';
    if(!t.change) return picker+'<p class="context-note pp-note">No recorded changes.</p>';
    return picker+diffMarkup(t.change,esc);
  }
  function sourceMarkup(t) {
    const text=t.draft??t.content??'';
    if(!t.editable) return '<p class="context-note pp-note">'+esc(t.reason||'Read only.')+'</p><pre class="file-content pp-source" tabindex="0" role="tabpanel" aria-label="File content">'+esc(text)+'</pre>';
    return '<div class="pp-edit">'+toolbarMarkup(t)+'<textarea class="pp-editor" id="pp-editor" spellcheck="false" wrap="off" aria-label="Edit '+esc(baseName(t.path))+'" autocapitalize="off" autocomplete="off">'+esc(text)+'</textarea></div>';
  }
  function previewMarkup(t) {
    if(t.mode==='canvas') return '<iframe class="canvas-preview pp-frame" title="'+esc(t.title)+'" sandbox="allow-scripts" referrerpolicy="no-referrer" src="/api/canvas/'+esc(t.id)+'?r='+(t.rev||0)+'"></iframe><p class="canvas-notice">Canvas · Isolated session, no network access</p>';
    if(t.mode==='image') return '<div class="pp-imagebar"><button type="button" class="button quiet small" data-pp="zoom" aria-pressed="'+(t.zoom==='100')+'">'+(t.zoom==='100'?'Fit to pane':'Actual size 100%')+'</button></div><div class="pp-image '+(t.zoom==='100'?'is-actual':'is-fit')+'"><img src="'+esc(t.mediaUrl)+'" alt="'+esc(t.title)+'"></div>';
    if(t.mode==='document') {
      const pdf=t.suffix==='.pdf';
      return (pdf?'<iframe class="pp-pdf pp-frame" title="'+esc(t.title)+'" src="'+esc(t.mediaUrl)+'"></iframe><p class="canvas-notice">If the PDF does not appear, open it in the app or download it.</p>':
        '<div class="native-document"><span aria-hidden="true">▤</span><h4>'+esc((t.suffix||'').slice(1).toUpperCase())+'</h4><p>Open this file in its default application on the AgentFlow machine.</p></div>');
    }
    return '<div class="markdown-preview" role="tabpanel" aria-label="Markdown preview">'+h.format(t.content||'')+'</div>';
  }
  function render() {
    if(!tabs.length) {root.remove();applyLayout();changed();return;}
    const t=tab();
    root.innerHTML=headMarkup()+'<div class="pp-file">'+(t?metaMarkup(t)+bodyMarkup(t):'')+'</div>';
    applyLayout();
    if(host&&!root.isConnected) attach(host);
  }
  /** Keystrokes only touch the dirty indicators, never the editor itself. */
  function syncDirty() {
    const t=tab();if(!t) return;
    const dirty=isDirty(t);
    root.querySelectorAll('.pp-tab').forEach(el=>el.classList.toggle('is-dirty',el.querySelector('[data-key]')?.dataset.key&&tabs.find(x=>x.key===el.querySelector('[data-key]').dataset.key&&isDirty(x))!==undefined));
    const save=q('[data-pp=save]'),revert=q('[data-pp=revert]'),status=q('.pp-status');
    if(save) save.disabled=!dirty||!!t.saving;
    if(revert) revert.disabled=!dirty;
    if(status) status.textContent=dirty?'Unsaved changes · Ctrl/Cmd+S saves':t.savedAt?'Saved':'';
    const dot=q('.pp-views [data-view=source] .pp-dot');
    if(dot) dot.classList.toggle('is-on',dirty);
  }

  // ---- loading ---------------------------------------------------------------------------------
  const rawUrl=(source,id)=>source==='artifact'?'/api/outputs/'+id+'/raw':'/api/attachments/'+id;
  async function load(t) {
    t.loading=true;t.error='';render();
    try {
      const result=await api('/files/'+t.source+'/'+t.id+'/preview',undefined,'GET'),file=result.file||{};
      Object.assign(t,{loading:false,error:'',title:file.title||file.name||baseName(file.path),path:file.path||file.name||t.path,mode:result.mode,suffix:result.suffix,
        content:result.content??'',sha256:result.sha256,editable:!!result.editable&&result.mode!=='image'&&result.mode!=='document',reason:result.readonly_reason||'',canOpen:result.can_open!==false,
        mediaUrl:rawUrl(t.source,t.id),agent_id:file.agent_id,created_at:file.created_at,artifactId:t.source==='artifact'?t.id:null,rev:(t.rev||0)+1});
      if(t.draft!==undefined&&t.draft===t.content) t.draft=undefined;
      t.hasChanges=!!t.artifactId&&h.changesFor(t.artifactId).length>0;
      if(t.view==='changes'&&t.change===undefined&&t.hasChanges) ensureChange(t);
    } catch(e) {
      t.loading=false;t.error=e.message||'Unable to open this file.';
    }
    render();changed();
  }
  async function ensureChange(t,id) {
    const list=t.artifactId?h.changesFor(t.artifactId):[];
    const wanted=id||t.change?.id||list.at(-1)?.id;
    if(!wanted) return;
    t.changeLoading=true;render();
    try {t.change=await api('/file_changes/'+wanted,undefined,'GET');}
    catch(e) {t.change=null;toast(e.message,true);}
    t.changeLoading=false;render();
  }
  function add(next) {
    const result=openTab(tabs,next);
    if(result.rejected) {toast('Save or close an open file first. Six files are open.',true);return null;}
    tabs=result.tabs;active=result.active;
    return tabs.find(t=>t.key===active);
  }
  async function openFile({source='artifact',id,view,title,path}) {
    const key=tabKey(source,id),existing=tabs.find(t=>t.key===key);
    if(existing) {
      active=key;if(view) existing.view=view;
      render();changed();
      if(view==='changes'&&!existing.change&&existing.hasChanges) ensureChange(existing);
      if(existing.error&&!existing.loading) load(existing);
      return existing;
    }
    const t=add({key,source,id,kind:'file',title:title||'',path:path||'',loading:true,view,artifactId:source==='artifact'?id:null});
    if(!t) return null;
    render();changed();
    await load(t);
    return t;
  }
  /** Open the file a change belongs to (Changes tab first); a change whose file is gone opens as a diff-only tab. */
  async function openChange(changeId) {
    const request=await api('/file_changes/'+changeId,undefined,'GET');
    const artifactId=request.artifact_id;
    if(artifactId) {
      const t=await openFile({source:'artifact',id:artifactId,view:'changes',title:request.title,path:request.path});
      if(t) {t.change=request;t.hasChanges=true;t.view='changes';if(!t.error&&!t.loading) render();else render();changed();}
      return t;
    }
    const key=tabKey('change',changeId);
    const t=add({key,source:'change',id:changeId,kind:'change',title:request.title||baseName(request.path),path:request.path,mode:'text',content:request.after||'',editable:false,
      reason:'This revision is read-only.',change:request,view:'changes',agent_id:request.agent_id,created_at:request.created_at});
    render();changed();
    return t;
  }

  // ---- actions ---------------------------------------------------------------------------------
  function confirmDiscard(list) {
    if(!list.length) return true;
    return window.confirm('Discard unsaved changes to '+list.map(t=>baseName(t.path)||t.title).join(', ')+'?');
  }
  function closeOne(key) {
    const t=tabs.find(x=>x.key===key);
    if(!t||!confirmDiscard(needsConfirm(t)?[t]:[])) return;
    const next=closeTab(tabs,key,active);
    tabs=next.tabs;active=next.active;
    if(!tabs.length) maximized=false;
    render();changed();
  }
  function closeAll() {
    if(!confirmDiscard(dirtyTabs(tabs))) return;
    tabs=[];active=null;maximized=false;render();changed();
  }
  async function save(t,{overwrite=false}={}) {
    if(!t||!isDirty(t)||t.saving) return;
    t.saving=true;t.saveError='';t.conflict=null;syncDirty();
    try {
      const result=await api('/files/artifact/'+t.id+'/save',{content:t.draft,base_sha256:overwrite&&t.currentSha?t.currentSha:t.sha256});
      Object.assign(t,{content:result.content??t.draft,draft:undefined,sha256:result.sha256,editable:!!result.editable,reason:result.readonly_reason||'',rev:(t.rev||0)+1,savedAt:Date.now(),currentSha:null,hasChanges:true,change:undefined});
      toast('Saved '+baseName(t.path)+'.');
      await h.afterSave?.();
      if(t.view==='changes') ensureChange(t);
    } catch(e) {
      if(e.status===409) {t.conflict=true;t.currentSha=e.data?.current_sha256||null;}
      else t.saveError=e.message;
    }
    t.saving=false;render();changed();
  }
  function revert(t) {
    if(!t||!isDirty(t)) return;
    t.draft=undefined;t.conflict=null;t.saveError='';render();
  }
  async function reload(t) {
    t.draft=undefined;t.conflict=null;t.saveError='';t.currentSha=null;await load(t);
  }
  async function openInApp(t) {
    if(!t||t.kind==='change') return;
    try {await api('/files/'+t.source+'/'+t.id+'/open',{});toast('Requested opening the file in its application.');}
    catch(e) {toast(e.message,true);}
  }
  function download(t) {
    if(!t) return;
    const direct=t.kind!=='change',url=direct?t.mediaUrl:URL.createObjectURL(new Blob([t.content||''],{type:'text/plain;charset=utf-8'})),a=document.createElement('a');
    a.href=url;a.download=baseName(t.path)||'file';document.body.append(a);a.click();a.remove();
    if(!direct) setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function toggleMax(force) {
    maximized=force??!maximized;applyLayout();
    const button=q('[data-pp=max]');if(button) {button.setAttribute('aria-label',maximized?'Restore size':'Maximize');button.title=maximized?'Restore (Esc)':'Maximize';button.innerHTML=icon(maximized?'collapse':'expand');}
  }

  root.addEventListener('click',event=>{
    const el=event.target.closest?.('[data-pp]');if(!el||el.disabled) return;
    const t=tab(),act=el.dataset.pp;
    if(act==='tab'&&el.dataset.key!==active) {active=el.dataset.key;render();changed();}
    else if(act==='close-tab') closeOne(el.dataset.key);
    else if(act==='close') closeAll();
    else if(act==='max') toggleMax();
    else if(act==='open-app') openInApp(t);
    else if(act==='download') download(t);
    else if(act==='view'&&t) {t.view=el.dataset.view;render();if(t.view==='changes'&&!t.change) ensureChange(t);}
    else if(act==='save') save(t);
    else if(act==='revert') revert(t);
    else if(act==='reload') reload(t);
    else if(act==='overwrite') save(t,{overwrite:true});
    else if(act==='retry') load(t);
    else if(act==='zoom'&&t) {t.zoom=t.zoom==='100'?'fit':'100';render();}
  });
  root.addEventListener('change',event=>{
    if(event.target.matches?.('[data-pp=pick-change]')) {const t=tab();if(t) ensureChange(t,event.target.value);}
  });
  root.addEventListener('input',event=>{
    if(event.target.id!=='pp-editor') return;
    const t=tab();if(!t) return;
    t.draft=event.target.value;t.savedAt=null;t.saveError='';syncDirty();
  });
  root.addEventListener('keydown',event=>{
    const t=tab();
    if(event.target.id==='pp-editor') {
      if(event.key==='Tab'&&!event.shiftKey&&!event.ctrlKey&&!event.metaKey&&!event.altKey) {
        event.preventDefault();
        const area=event.target;area.setRangeText('  ',area.selectionStart,area.selectionEnd,'end');
        area.dispatchEvent(new Event('input',{bubbles:true}));
      }
    }
    if((event.ctrlKey||event.metaKey)&&!event.altKey&&event.key.toLowerCase()==='s'&&t?.editable) {event.preventDefault();save(t);}
    if(event.target.classList?.contains('pp-handle')) {
      const next=stepWidth(width??available(),event.key,available());
      if(next!=null) {event.preventDefault();width=next;saveWidth();applyLayout();}
    }
  });
  // Drag to resize: pointer capture on the handle, one layout write per animation frame, iframes inert while dragging.
  root.addEventListener('pointerdown',event=>{
    const handle=event.target.closest?.('.pp-handle');if(!handle||event.button!==0||narrow()||maximized||!host) return;
    event.preventDefault();handle.setPointerCapture(event.pointerId);host.classList.add('is-resizing-preview');
    const right=host.getBoundingClientRect().right-(contextInline()?CONTEXT_WIDTH:0);
    const move=e=>{cancelAnimationFrame(frame);frame=requestAnimationFrame(()=>{width=clampWidth(right-e.clientX,available());applyLayout();});};
    const done=()=>{handle.removeEventListener('pointermove',move);handle.removeEventListener('pointerup',done);handle.removeEventListener('pointercancel',done);cancelAnimationFrame(frame);host?.classList.remove('is-resizing-preview');saveWidth();applyLayout();};
    handle.addEventListener('pointermove',move);handle.addEventListener('pointerup',done);handle.addEventListener('pointercancel',done);
  });
  root.addEventListener('dblclick',event=>{if(event.target.closest?.('.pp-handle')) toggleMax();});
  document.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&maximized&&tabs.length&&!document.querySelector('dialog[open]')) {event.preventDefault();event.stopImmediatePropagation();toggleMax(false);}
  },true);
  window.addEventListener('resize',()=>{if(tabs.length) {cancelAnimationFrame(frame);frame=requestAnimationFrame(applyLayout);}});
  window.addEventListener('beforeunload',event=>{if(dirtyTabs(tabs).length) {event.preventDefault();event.returnValue='';}});

  return {
    attach,openFile,openChange,render,
    isOpen:()=>tabs.length>0,
    activeArtifactId:()=>{const t=tab();return t?(t.artifactId||t.change?.artifact_id||null):null;},
    hasDirty:()=>dirtyTabs(tabs).length>0,
    closeAll
  };
}
