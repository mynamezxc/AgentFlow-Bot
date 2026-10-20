// Image generation UI: pure helpers (tested in tests/image-studio.mjs), the "Image generation" settings panel
// (Connections > Image) and the Image Studio dialog. Contracts: GET /images/engines, POST /images/generate, POST /settings {image}.
import {setupLightbox} from './lightbox.js';
import {rawUrl} from './message-files.js';

export const ASPECT_RATIOS=['1:1','3:2','2:3','4:3','3:4','4:5','5:4','16:9','9:16','21:9'];
export const SIZES=['512px','1K','2K','4K'];
const ASPECT_ORDER=['auto',...ASPECT_RATIOS],SIZE_ORDER=['auto',...SIZES];
export const QUALITIES=['auto','low','medium','high'];
export const FORMATS=['png','jpeg','webp'];
export const SIZE_LABELS={auto:'Auto','512px':'512 px','1K':'1K','2K':'2K','4K':'4K'};
export const FORMAT_LABELS={png:'PNG',jpeg:'JPEG',jpg:'JPEG',webp:'WebP'};
export const PROVIDER_ENGINE={codex:'codex',gemini:'gemini'};
export const MAX_PROMPT=4000,MAX_NEGATIVE=1000,MAX_FALLBACK=6;
export const STYLE_PRESETS=[
  ['none','None','<circle cx="12" cy="12" r="8"/><path d="m6.5 17.5 11-11"/>'],
  ['photo','Photo','<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>'],
  ['product','Product','<path d="M5 8h14l-1 12H6z"/><path d="M9 8a3 3 0 0 1 6 0"/>'],
  ['illustration','Illustration','<path d="m4 20 1-5L16 4l4 4L9 19z"/><path d="m14 6 4 4"/>'],
  ['3d','3D','<path d="M12 3 4 7.5v9L12 21l8-4.5v-9z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/>'],
  ['flat-icon','Flat icon','<rect x="4" y="4" width="7" height="7" rx="1.5"/><circle cx="16.5" cy="7.5" r="3.5"/><path d="M4 20h7l-3.5-6zM14 14h6v6h-6z"/>'],
  ['anime','Anime','<path d="m12 3 2.2 5.8L20 11l-5.8 2.2L12 19l-2.2-5.8L4 11l5.8-2.2z"/>'],
  ['watercolor','Watercolor','<path d="M12 3s6 6.5 6 11a6 6 0 0 1-12 0c0-4.5 6-11 6-11z"/>'],
  ['cinematic','Cinematic','<rect x="3" y="6" width="18" height="12" rx="2"/><path d="M7 6v12M17 6v12M3 10h4m10 0h4M3 14h4m10 0h4"/>'],
  ['logo','Logo','<path d="m12 3 7.8 4.5v9L12 21l-7.8-4.5v-9z"/><path d="m8.5 15 3.5-6 3.5 6M9.7 13h4.6"/>']
].map(([id,label,path])=>({id,label,path}));
export const DEFAULT_DEFAULTS={aspect_ratio:'1:1',size:'1K',quality:'auto',format:'png',count:1,style:'none'};
export const DEFAULT_IMAGE_SETTINGS={mode:'agent',engine:'',model:'',fallback:[],defaults:{...DEFAULT_DEFAULTS}};
export const CODEX_COST_NOTE='Codex image generation uses your Codex plan and costs about 3–5× a normal turn.';

const esc=(value='')=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const svg=path=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+path+'</svg>';

/* ---------- pure helpers ---------- */

/** Known values first in canonical order, then anything else the engine reports (order kept, duplicates dropped). */
export function orderValues(canonical,list) {
  const seen=new Set(),values=(Array.isArray(list)?list:[]).map(String).filter(v=>v&&!seen.has(v)&&seen.add(v));
  return [...canonical.filter(v=>seen.has(v)),...values.filter(v=>!canonical.includes(v))];
}
/** Width/height (px) of a rectangle that fits `box` and has the aspect ratio "W:H". */
export function aspectGeometry(ratio,box=22) {
  const [a,b]=String(ratio).split(':').map(Number);
  if(!(a>0&&b>0)) return {w:box,h:box};
  const k=box/Math.max(a,b);
  return {w:Math.max(2,Math.round(a*k)),h:Math.max(2,Math.round(b*k))};
}
export function engineParams(engine) {
  const p=engine?.params||{};
  const count=Number(p.max_count),refs=Number(p.max_references);
  return {
    aspect_ratios:orderValues(ASPECT_ORDER,p.aspect_ratios),
    sizes:orderValues(SIZE_ORDER,p.sizes),
    qualities:orderValues(QUALITIES,p.qualities),
    formats:orderValues(FORMATS,p.formats),
    background:Array.isArray(p.background)?p.background.map(String):[],
    max_count:Number.isInteger(count)&&count>0?count:1,
    max_references:Number.isInteger(refs)&&refs>0?refs:0
  };
}
/** Options offered by several engines at once: the union of their values, the largest limits. */
export function unionParams(engines) {
  const all=(engines||[]).map(engineParams);
  const merge=(canonical,key)=>orderValues(canonical,all.flatMap(p=>p[key]));
  return {
    aspect_ratios:merge(ASPECT_ORDER,'aspect_ratios'),sizes:merge(SIZE_ORDER,'sizes'),qualities:merge(QUALITIES,'qualities'),formats:merge(FORMATS,'formats'),
    background:[...new Set(all.flatMap(p=>p.background))],
    max_count:Math.max(1,...all.map(p=>p.max_count)),max_references:Math.max(0,...all.map(p=>p.max_references))
  };
}
export const engineById=(engines,id)=>(engines||[]).find(e=>e.id===id);
export const modelsOf=engine=>Array.isArray(engine?.models)?engine.models.filter(m=>m&&m.id):[];
export const defaultModelId=engine=>(modelsOf(engine).find(m=>m.default)||modelsOf(engine)[0])?.id||'';
export const engineLabel=(engines,id)=>engineById(engines,id)?.label||({codex:'Codex',gemini:'Gemini',gateway:'Gateway'}[id]||id||'');
/** Plan/cost note for an engine: the server's own `notice`, or the documented Codex note. */
export const engineNotice=engine=>engine?.notice||(engine?.id==='codex'?CODEX_COST_NOTE:'');
export function engineStatus(engine) {
  if(!engine) return {ready:false,text:'Not available'};
  return engine.available?{ready:true,text:'Ready'}:{ready:false,text:engine.reason||'Not available'};
}
/** Which engine an image request ends up on (mirrors the server rule so pickers can show the right options). */
export function resolveEngineId({engines=[],settings={},agentProvider='',override=''}={}) {
  const ready=id=>!!engineById(engines,id)?.available;
  if(settings.mode==='fixed'&&settings.engine) return settings.engine;
  if(override&&engineById(engines,override)) return override;
  const own=PROVIDER_ENGINE[agentProvider];
  if(own&&ready(own)) return own;
  for(const item of settings.fallback||[]) if(ready(item.engine)) return item.engine;
  return engines.find(e=>e.available)?.id||own||engines[0]?.id||'';
}
/** Engines whose options the pickers should show. Agent mode without a known engine: every engine that is ready or has models, since defaults are clamped to the engine actually used. */
export function activeEngines({engines=[],settings={},agentProvider='',override=''}={}) {
  const id=settings.mode==='fixed'&&settings.engine?settings.engine:override||(agentProvider?resolveEngineId({engines,settings,agentProvider,override}):'');
  const hit=engineById(engines,id);
  if(hit) return [hit];
  const usable=engines.filter(e=>e.available||modelsOf(e).length);
  return usable.length?usable:engines;
}
const pick=(value,list,fallbacks=[])=>list.includes(value)?value:(fallbacks.find(f=>list.includes(f))??list[0]??'');
/** Keep values the engine supports; otherwise fall back to a sensible supported one ('' = parameter not offered). */
export function clampParams(values={},opts) {
  const v={...DEFAULT_DEFAULTS,...values};
  const count=Math.round(Number(v.count));
  return {
    aspect_ratio:pick(v.aspect_ratio,opts.aspect_ratios,['1:1']),
    size:pick(v.size,opts.sizes,['1K']),
    quality:pick(v.quality,opts.qualities,['auto']),
    format:pick(v.format,opts.formats,['png']),
    count:Math.min(opts.max_count,Math.max(1,Number.isFinite(count)?count:1)),
    style:STYLE_PRESETS.some(s=>s.id===v.style)?v.style:'none'
  };
}
export const promptError=(prompt,negative='')=>{
  const text=String(prompt||'').trim();
  if(!text) return 'Describe the image you want to create.';
  if(text.length>MAX_PROMPT) return 'The prompt is limited to '+MAX_PROMPT+' characters.';
  if(String(negative||'').length>MAX_NEGATIVE) return 'The “Avoid” text is limited to '+MAX_NEGATIVE+' characters.';
  return '';
};
export const variationPrompt=prompt=>String(prompt||'').trim()+'\n\nCreate a new variation of the reference image. Keep its subject and overall composition, change the details.';
/** POST /images/generate body. Engine/model are only sent when the owner has not fixed an engine in Settings. */
export function buildPayload({prompt,negative='',values={},opts,engine='',model='',fixed=false,references=[],agentId='',projectId=''}) {
  const problem=promptError(prompt,negative);
  if(problem) throw new Error(problem);
  const v=clampParams(values,opts),body={prompt:String(prompt).trim()};
  if(v.aspect_ratio) body.aspect_ratio=v.aspect_ratio;
  if(v.size) body.size=v.size;
  if(v.quality) body.quality=v.quality;
  if(v.format) body.format=v.format;
  body.count=v.count;
  if(v.style!=='none') body.style=v.style;
  if(String(negative).trim()) body.negative=String(negative).trim();
  if(!fixed&&engine){body.engine=engine;if(model) body.model=model;}
  const limited=references.slice(0,opts.max_references);
  // The server resolves each id as an output artifact first, then as a chat attachment.
  if(limited.length) body.reference_artifact_ids=[...new Set(limited.map(r=>r.id))];
  if(agentId) body.agent_id=agentId;
  if(projectId) body.project_id=projectId;
  return body;
}
/** Settings (draft) -> {image:{...}} for POST /settings; throws a user-readable Error. */
export function settingsPayload(draft,engines) {
  const mode=draft.mode==='fixed'?'fixed':'agent',image={mode};
  const settings={...draft,mode};
  if(mode==='fixed') {
    const engine=engineById(engines,draft.engine);
    if(!engine) throw new Error('Choose the engine to always use.');
    const model=modelsOf(engine).some(m=>m.id===draft.model)?draft.model:defaultModelId(engine);
    image.engine=engine.id;if(model) image.model=model;
  }
  image.fallback=(draft.fallback||[]).filter(f=>f&&f.engine&&engineById(engines,f.engine)).map(f=>({engine:f.engine,model:f.model||defaultModelId(engineById(engines,f.engine))}));
  image.defaults=Object.fromEntries(Object.entries(clampParams(draft.defaults,unionParams(activeEngines({engines,settings})))).filter(([,v])=>v!==''));
  return {image};
}
export function normalizeImageSettings(raw,engines) {
  const src=raw&&typeof raw==='object'?raw:{};
  const fallback=(Array.isArray(src.fallback)?src.fallback:[]).filter(f=>f&&typeof f.engine==='string'&&f.engine).map(f=>({engine:f.engine,model:String(f.model||'')})).slice(0,MAX_FALLBACK);
  const draft={mode:src.mode==='fixed'?'fixed':'agent',engine:String(src.engine||''),model:String(src.model||''),fallback,defaults:{...DEFAULT_DEFAULTS,...(src.defaults||{})}};
  if(draft.mode==='fixed'&&engines?.length&&!engineById(engines,draft.engine)) draft.engine='';
  return draft;
}
export function moveItem(list,from,to) {
  if(from===to||from<0||to<0||from>=list.length||to>=list.length) return list.slice();
  const next=list.slice(),[item]=next.splice(from,1);next.splice(to,0,item);return next;
}
export function imageSettingsSummary(settings,engines) {
  if(settings?.mode==='fixed'&&settings.engine) return 'Always '+engineLabel(engines,settings.engine);
  return 'Follows each agent’s model';
}
const IMAGE_RX=/\.(png|jpe?g|webp|gif|bmp|svg)$/i;
/** Image artifacts/attachments the user can use as a reference, newest first. */
export function referenceItems({artifacts=[],attachments=[],projectId='',extra=[],limit=40}={}) {
  const mine=x=>!x.archived&&(!projectId||x.project_id===projectId);
  const title=x=>String(x.title||x.name||x.path||'Image').split(/[\\/]/).at(-1);
  const items=[
    ...artifacts.filter(a=>mine(a)&&(a.is_image||IMAGE_RX.test(String(a.path||'').split(/[?#]/)[0]))).map(a=>({key:'artifact:'+a.id,kind:'artifact',id:a.id,title:title(a),url:rawUrl(a)})),
    ...attachments.filter(a=>mine(a)&&a.is_image&&a.id).map(a=>({key:'attachment:'+a.id,kind:'attachment',id:a.id,title:title(a),url:'/api/attachments/'+encodeURIComponent(a.id)}))
  ].reverse().slice(0,limit);
  const known=new Set(items.map(i=>i.key));
  return [...extra.filter(i=>!known.has(i.key)),...items];
}
export function toggleReference(selected,key,max) {
  const list=selected.filter(k=>k!==key);
  if(list.length<selected.length) return {selected:list,limited:false};
  if(selected.length>=max) return {selected,limited:true};
  return {selected:[...selected,key],limited:false};
}
export function normalizeResult(result,prompt='') {
  const images=(Array.isArray(result?.images)?result.images:[]).filter(i=>i&&(i.artifact_id||i.url||i.path)).map(i=>{
    const url=i.url||(i.artifact_id?rawUrl({id:i.artifact_id}):''),title=String(i.path||i.artifact_id||'image').split(/[\\/]/).at(-1);
    return {id:i.artifact_id||'',url,title,width:i.width||0,height:i.height||0,key:i.artifact_id?'artifact:'+i.artifact_id:''};
  });
  return {engine:result?.engine||'',model:result?.model||'',params:result?.params||{},images,prompt};
}
export function paramSummary(params={}) {
  return [params.aspect_ratio,params.size,params.quality&&params.quality!=='auto'?params.quality:'',params.format?String(params.format).toUpperCase():''].filter(Boolean).join(' · ');
}

/* ---------- markup builders ---------- */

const chip=(name,value,checked,inner,extra='')=>'<label class="ig-chip'+extra+'"><input type="radio" name="'+name+'" value="'+esc(value)+'"'+(checked?' checked':'')+'><span class="ig-chip-face">'+inner+'</span></label>';
const group=(legend,body,cls='')=>'<fieldset class="ig-group '+cls+'"><legend>'+esc(legend)+'</legend>'+body+'</fieldset>';
export function aspectChipMarkup(ratio,checked) {
  const auto=ratio==='auto',{w,h}=aspectGeometry(ratio);
  return chip('ig_aspect_ratio',ratio,checked,'<i class="ig-ar'+(auto?' auto':'')+'" style="width:'+w+'px;height:'+h+'px" aria-hidden="true"></i><b>'+esc(auto?'Auto':ratio)+'</b>',' ig-aspect');
}
export const stepperMarkup=(value,max,disabled=false)=>'<div class="ig-stepper" role="group" aria-label="Number of images"><button type="button" class="button small" data-ig="step" data-dir="-1" aria-label="Fewer images"'+(disabled||value<=1?' disabled':'')+'>−</button><input id="ig-count" name="ig_count" type="number" inputmode="numeric" min="1" max="'+max+'" value="'+value+'" aria-label="Number of images"'+(disabled?' disabled':'')+'><button type="button" class="button small" data-ig="step" data-dir="1" aria-label="More images"'+(disabled||value>=max?' disabled':'')+'>+</button><small class="muted">of '+max+' max</small></div>';
export function styleCardsMarkup(value) {
  return '<div class="ig-styles">'+STYLE_PRESETS.map(s=>chip('ig_style',s.id,value===s.id,svg(s.path)+'<b>'+esc(s.label)+'</b>',' ig-style')).join('')+'</div>';
}
/** Aspect ratio, resolution, quality, format and count pickers for the given (already filtered) options. */
export function paramsMarkup(opts,values,{disabled=false}={}) {
  const v=clampParams(values,opts),out=[];
  if(opts.aspect_ratios.length) out.push(group('Aspect ratio','<div class="ig-chips">'+opts.aspect_ratios.map(r=>aspectChipMarkup(r,v.aspect_ratio===r)).join('')+'</div>'));
  if(opts.sizes.length) out.push(group('Resolution','<div class="ig-chips">'+opts.sizes.map(s=>chip('ig_size',s,v.size===s,'<b>'+esc(SIZE_LABELS[s]||s)+'</b>')).join('')+'</div>'));
  if(opts.qualities.length) out.push(group('Quality','<div class="ig-chips">'+opts.qualities.map(q=>chip('ig_quality',q,v.quality===q,'<b>'+esc(q[0].toUpperCase()+q.slice(1))+'</b>')).join('')+'</div>'));
  if(opts.formats.length) out.push(group('Format','<div class="ig-chips">'+opts.formats.map(f=>chip('ig_format',f,v.format===f,'<b>'+esc(FORMAT_LABELS[f]||f.toUpperCase())+'</b>')).join('')+'</div>'));
  if(opts.max_count>1) out.push(group('Images per request',stepperMarkup(v.count,opts.max_count,disabled)));
  return '<div class="ig-params">'+out.join('')+'</div>';
}
const engineOption=(engine,selected)=>{const s=engineStatus(engine);return '<option value="'+esc(engine.id)+'"'+(selected?' selected':'')+(s.ready?'':' disabled')+'>'+esc(engine.label+(s.ready?'':' — '+s.text))+'</option>';};
const modelOptions=(engine,selected)=>modelsOf(engine).map(m=>'<option value="'+esc(m.id)+'"'+(m.id===selected?' selected':'')+'>'+esc(m.label||m.id)+(m.default?' (default)':'')+'</option>').join('');
const selectField=(id,label,options,{disabled=false,help='',name=id}={})=>'<div class="field"><label for="'+id+'">'+esc(label)+'</label><select id="'+id+'" name="'+name+'"'+(disabled?' disabled':'')+'>'+options+'</select>'+(help?'<span class="help">'+esc(help)+'</span>':'')+'</div>';

export function enginesListMarkup(engines) {
  return '<ul class="ig-engines" aria-label="Image engines">'+engines.map(e=>{
    const s=engineStatus(e),n=modelsOf(e).length;
    return '<li><span class="ig-engine-name"><strong>'+esc(e.label)+'</strong><small class="muted">'+esc(n?n+' model'+(n===1?'':'s'):'No models loaded')+'</small></span><span class="ig-badge '+(s.ready?'ready':'blocked')+'">'+esc(s.text)+'</span></li>';
  }).join('')+'</ul>'+engines.filter(e=>engineNotice(e)).map(e=>'<p class="ig-note">'+esc(engineNotice(e))+'</p>').join('');
}
export function fallbackMarkup(items,engines) {
  const label=f=>engineLabel(engines,f.engine)+' · '+(modelsOf(engineById(engines,f.engine)).find(m=>m.id===f.model)?.label||f.model||'default model');
  const first=engines.find(e=>!items.some(f=>f.engine===e.id))||engines[0];
  return '<ol class="fallback-list ig-fallback" aria-label="Fallback engines in order">'+(items.length?items.map((f,i)=>'<li draggable="true" data-index="'+i+'"><span class="fb-index" aria-hidden="true">'+(i+1)+'</span><span class="fb-name">'+esc(label(f))+'</span><span class="fb-actions"><button type="button" class="button quiet small" data-ig="fb-up" data-id="'+i+'" aria-label="Move '+esc(label(f))+' up"'+(i?'':' disabled')+'>↑</button><button type="button" class="button quiet small" data-ig="fb-down" data-id="'+i+'" aria-label="Move '+esc(label(f))+' down"'+(i<items.length-1?'':' disabled')+'>↓</button><button type="button" class="button quiet small" data-ig="fb-remove" data-id="'+i+'" aria-label="Remove '+esc(label(f))+'">Remove</button></span></li>').join(''):'<li class="fb-empty">No fallback yet — the first ready engine is used.</li>')+'</ol>'+
    '<div class="ig-add">'+selectField('ig-fb-engine','Engine',engines.map(e=>'<option value="'+esc(e.id)+'"'+(e.id===first?.id?' selected':'')+'>'+esc(e.label)+'</option>').join(''))+selectField('ig-fb-model','Model',modelOptions(first,defaultModelId(first)))+'<button type="button" class="button small" data-ig="fb-add"'+(items.length>=MAX_FALLBACK?' disabled':'')+'>Add to list</button></div>';
}
/** The whole settings panel body for a loaded draft. */
export function settingsMarkup(draft,engines) {
  const fixed=draft.mode==='fixed',engine=engineById(engines,draft.engine);
  const opts=unionParams(activeEngines({engines,settings:draft}));
  const modeOption=(value,title,text)=>'<label class="ig-mode"><input type="radio" name="ig_mode" value="'+value+'"'+(draft.mode===value?' checked':'')+'><span class="ig-mode-face"><strong>'+esc(title)+'</strong><small>'+esc(text)+'</small></span></label>';
  return '<section class="ig-section" aria-labelledby="ig-h-engine"><h3 id="ig-h-engine">Engine</h3>'+
    '<fieldset class="ig-modes"><legend class="sr-only">Engine mode</legend>'+
      modeOption('agent','Use each agent’s current model (recommended)','Codex agents create images with Codex, Gemini agents with Gemini. Agents that cannot (Claude Code) use the fallback order.')+
      modeOption('fixed','Always use…','Every image uses one engine and model you choose, whatever the agent runs on.')+'</fieldset>'+
    (fixed?'<div class="field-grid ig-fixed">'+selectField('ig-engine','Engine',(engine?'':'<option value="" selected disabled>Choose an engine…</option>')+engines.map(e=>engineOption(e,e.id===draft.engine)).join(''),{name:'ig_engine'})+selectField('ig-model','Model',engine?modelOptions(engine,draft.model||defaultModelId(engine)):'<option value="">Choose an engine first</option>',{disabled:!engine,name:'ig_model',help:engine&&!modelsOf(engine).length?'No models were reported for this engine.':'Models come from the engine’s own catalog.'})+'</div>':'')+
    enginesListMarkup(engines)+'</section>'+
    '<section class="ig-section" aria-labelledby="ig-h-fb"><h3 id="ig-h-fb">Fallback order</h3><p class="ig-lead">Used when an agent’s provider cannot create images (Claude Code agents, for example)'+(fixed?', and not used while one engine is fixed':'')+'. Drag a row or use the arrows.</p><div id="ig-fb" class="'+(fixed?'is-off':'')+'"'+(fixed?' inert':'')+'>'+fallbackMarkup(draft.fallback,engines)+'</div></section>'+
    '<section class="ig-section" aria-labelledby="ig-h-def"><h3 id="ig-h-def">Defaults</h3><p class="ig-lead">Start values for Image Studio and for agents that do not choose their own'+(fixed?'':'. Options an engine does not support are adjusted automatically')+'.</p>'+
    '<div id="ig-params">'+paramsMarkup(opts,draft.defaults)+'</div>'+group('Style preset',styleCardsMarkup(draft.defaults.style))+'</section>';
}

/* ---------- Settings panel (Connections > Image generation) ---------- */

const $=(selector,root=document)=>root.querySelector(selector);
/** A readable reason when /images/engines fails (an older runtime answers 404 with a non-JSON body). */
export const loadError=e=>/json|token|unexpected/i.test(e?.message||'')?'This runtime does not provide image engines yet.':(e?.message||'The request failed.');
const loadEngines=async api=>{const result=await api('/images/engines',undefined,'GET');return {engines:Array.isArray(result?.engines)?result.engines:[],settings:result?.settings||{}};};

export function createImageSettings(h) {
  const {api,refresh,toast,openModal,closeModal,button,getData,getMode}=h;
  let state=null,root=null;
  const draft=()=>state.draft;
  function paint(focus='') {
    if(!root?.isConnected||!state) return;
    if(state.status==='loading') {root.innerHTML='<p class="ig-loading" role="status"><span class="spinner"></span> Loading image engines…</p>';return;}
    if(state.status==='error') {root.innerHTML='<div class="info-box ig-unavailable" role="status"><strong>Image engines are not available yet.</strong><p>'+esc(state.error)+'</p></div><div class="row-actions"><button type="button" class="button small" data-ig="retry">Try again</button></div>';return;}
    root.innerHTML=settingsMarkup(draft(),state.engines);
    const save=$('#ig-save');if(save) save.disabled=false;
    if(focus) $(focus,root)?.focus();
  }
  async function load() {
    state={status:'loading',engines:[],draft:null,error:''};paint();
    if(getMode()==='demo') {state.status='error';state.error='Open the local app to configure image generation. The preview does not call image engines.';paint();return;}
    try {
      const {engines,settings}=await loadEngines(api);
      state={status:'ready',engines,draft:normalizeImageSettings(settings&&Object.keys(settings).length?settings:getData().settings?.image,engines),error:''};
    } catch(e) {state={status:'error',engines:[],draft:null,error:'Update and restart AgentFlow to use engine-based image generation. ('+loadError(e)+')'};}
    paint();
  }
  const reclamp=()=>{const d=draft();d.defaults=clampParams(d.defaults,unionParams(activeEngines({engines:state.engines,settings:d})));};
  function onChange(event) {
    const el=event.target,d=draft();if(!d) return;
    if(el.name==='ig_mode') {d.mode=el.value;if(d.mode==='fixed'&&!engineById(state.engines,d.engine)) {const first=state.engines.find(e=>e.available);if(first){d.engine=first.id;d.model=defaultModelId(first);}}reclamp();paint('input[name=ig_mode]:checked');}
    else if(el.id==='ig-engine') {d.engine=el.value;d.model=defaultModelId(engineById(state.engines,d.engine));reclamp();paint('#ig-engine');}
    else if(el.id==='ig-model') d.model=el.value;
    else if(el.id==='ig-fb-engine') {const model=$('#ig-fb-model',root);if(model) model.innerHTML=modelOptions(engineById(state.engines,el.value),defaultModelId(engineById(state.engines,el.value)));}
    else if(el.name?.startsWith('ig_')&&el.name!=='ig_count') {d.defaults[el.name.slice(3)]=el.value;}
    else if(el.id==='ig-count') {d.defaults.count=Number(el.value)||1;reclamp();$('#ig-params',root).innerHTML=paramsMarkup(unionParams(activeEngines({engines:state.engines,settings:d})),d.defaults);}
  }
  function onClick(event) {
    const el=event.target.closest('[data-ig]');if(!el||el.disabled) return;
    const d=draft(),act=el.dataset.ig,i=Number(el.dataset.id);
    if(act==='retry') load();
    else if(act==='fb-add') {
      const engine=$('#ig-fb-engine',root).value,model=$('#ig-fb-model',root).value;
      if(d.fallback.some(f=>f.engine===engine&&f.model===model)){toast('This engine and model are already in the list.',true);return;}
      d.fallback.push({engine,model});paint('[data-ig=fb-add]');
    } else if(act==='fb-remove') {d.fallback.splice(i,1);paint('#ig-fb-engine');}
    else if(act==='fb-up'||act==='fb-down') {const j=i+(act==='fb-up'?-1:1);d.fallback=moveItem(d.fallback,i,j);paint('[data-ig='+act+'][data-id="'+j+'"]');}
    else if(act==='step') {d.defaults.count=(Number(d.defaults.count)||1)+Number(el.dataset.dir);reclamp();$('#ig-params',root).innerHTML=paramsMarkup(unionParams(activeEngines({engines:state.engines,settings:d})),d.defaults);($('#ig-params [data-ig=step][data-dir="'+el.dataset.dir+'"]:not(:disabled)',root)||$('#ig-params [data-ig=step]:not(:disabled)',root))?.focus();}
  }
  let dragFrom=-1;
  function wireDrag() {
    root.addEventListener('dragstart',e=>{const li=e.target.closest?.('.ig-fallback li[data-index]');if(!li)return;dragFrom=Number(li.dataset.index);e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',String(dragFrom));li.classList.add('dragging');});
    root.addEventListener('dragend',()=>{dragFrom=-1;root.querySelectorAll('.dragging,.drop-target').forEach(n=>n.classList.remove('dragging','drop-target'));});
    root.addEventListener('dragover',e=>{const li=e.target.closest?.('.ig-fallback li[data-index]');if(!li||dragFrom<0)return;e.preventDefault();root.querySelectorAll('.drop-target').forEach(n=>n.classList.remove('drop-target'));li.classList.add('drop-target');});
    root.addEventListener('drop',e=>{const li=e.target.closest?.('.ig-fallback li[data-index]');if(!li||dragFrom<0)return;e.preventDefault();state.draft.fallback=moveItem(draft().fallback,dragFrom,Number(li.dataset.index));dragFrom=-1;paint();});
  }
  function open() {
    const footer=button('Cancel','modal-close')+button('Open Image Studio','image-studio-open','','','image')+'<button type="submit" class="button primary" id="ig-save" disabled>Save image settings</button>';
    openModal('Image generation','Choose which engine creates images, and the defaults for new ones.','<div id="ig-settings" class="ig-settings" aria-live="polite"></div>',footer,'image-settings');
    const modal=document.getElementById('modal');
    modal.classList.add('ig-wide');
    modal.addEventListener('close',()=>modal.classList.remove('ig-wide'),{once:true});
    root=$('#ig-settings');
    root.addEventListener('change',onChange);root.addEventListener('click',onClick);wireDrag();
    load();
  }
  async function submit(form) {
    if(form.dataset.type!=='image-settings') return false;
    const box=$('#modal-error'),save=$('#ig-save');
    if(!state||state.status!=='ready'||save?.disabled) return true;
    if(box) box.textContent='';
    try {
      const payload=settingsPayload(draft(),state.engines);
      save.disabled=true;save.setAttribute('aria-busy','true');
      await api('/settings',payload);await refresh(false);
      closeModal(true);h.render?.();toast('Image settings saved.');
    } catch(e) {if(box){box.textContent=e.message;box.focus();}else toast(e.message,true);}
    finally {if(save?.isConnected){save.disabled=false;save.removeAttribute('aria-busy');}}
    return true;
  }
  return {open,submit};
}

/* ---------- Image Studio dialog ---------- */

let cfg=null;
export const configureImageStudio=h=>{cfg=h;};
const FOCUSABLE='a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),summary,[tabindex]:not([tabindex="-1"])';

export async function openImageStudio({agentId='',projectId=''}={}) {
  if(!cfg||typeof document==='undefined') return null;
  setupLightbox();
  document.querySelector('dialog.image-studio')?.close();
  const data=()=>cfg.getData()||{},opener=document.activeElement;
  const agents=(data().agents||[]).filter(a=>!a.archived&&!a.sample);
  const s={engines:[],settings:normalizeImageSettings(data().settings?.image,[]),status:'loading',error:'',agentId:agentId||'',projectId:projectId||'',
    override:'',model:'',values:{...DEFAULT_DEFAULTS},refs:[],refsAll:false,extraRefs:[],batches:[],busy:false,genError:''};
  const dialog=document.createElement('dialog');
  dialog.className='image-studio';dialog.setAttribute('aria-labelledby','ig-title');
  const project=(data().projects||[]).find(p=>p.id===s.projectId);
  dialog.innerHTML='<div class="modal-head"><div><h2 id="ig-title">Image Studio</h2><p>Create images from a prompt. They are saved as outputs'+(project?' of “'+esc(project.title||project.name||'this project')+'”':'')+'.</p></div><button type="button" class="icon-button" data-ig="close" aria-label="Close Image Studio">'+svg('<path d="m6 6 12 12M6 18 18 6"/>')+'</button></div>'+
    '<div class="ig-body"><div class="ig-controls">'+
      '<div class="field"><label for="ig-prompt">Prompt</label><textarea id="ig-prompt" rows="5" maxlength="'+MAX_PROMPT+'" placeholder="Subject, setting, mood, colors, composition…" aria-describedby="ig-count-note"></textarea><span class="ig-counter" id="ig-count-note"><span id="ig-chars">0</span> / '+MAX_PROMPT+' · Ctrl+Enter to generate</span></div>'+
      '<details class="ig-avoid"><summary>Avoid <small class="muted">(optional)</small></summary><div class="field"><label for="ig-negative">Things to leave out</label><textarea id="ig-negative" rows="2" maxlength="'+MAX_NEGATIVE+'" placeholder="Text, watermarks, extra fingers, clutter…"></textarea></div></details>'+
      '<div id="ig-engine-area"></div><div id="ig-params-area"></div><div id="ig-style-area"></div><div id="ig-refs-area"></div></div>'+
    '<div class="ig-results" aria-live="polite" aria-label="Results"><div id="ig-results-area"></div></div></div>'+
    '<div class="modal-footer ig-footer"><span class="ig-cost" id="ig-cost"></span><button type="button" class="button" data-ig="close">Close</button><button type="button" class="button primary" id="ig-generate" data-ig="generate" disabled>'+svg('<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>')+'<span>Generate</span></button></div><p class="sr-only" id="ig-live" role="status"></p>';
  document.body.append(dialog);
  const q=sel=>dialog.querySelector(sel),agent=()=>agents.find(a=>a.id===s.agentId);
  const settings=()=>s.settings;
  const fixed=()=>settings().mode==='fixed'&&!!settings().engine;
  const resolvedId=()=>resolveEngineId({engines:s.engines,settings:settings(),agentProvider:agent()?.provider||'',override:s.override});
  const active=()=>engineById(s.engines,resolvedId());
  const opts=()=>unionParams(s.status==='ready'&&active()?[active()]:s.engines.length?s.engines:[]);
  const announce=text=>{q('#ig-live').textContent=text;};
  const selectedRefs=()=>refItems().filter(r=>s.refs.includes(r.key));
  const refItems=()=>referenceItems({artifacts:data().artifacts,attachments:data().attachments,projectId:s.projectId,extra:s.extraRefs});

  function paintEngine() {
    const area=q('#ig-engine-area');
    if(s.status==='loading'){area.innerHTML='<p class="ig-loading" role="status"><span class="spinner"></span> Loading image engines…</p>';return;}
    if(s.status==='error'){area.innerHTML='<div class="info-box ig-unavailable" role="alert"><strong>Image generation is not available.</strong><p>'+esc(s.error)+'</p></div><button type="button" class="button small" data-ig="retry">Try again</button>';return;}
    const agentSelect=agents.length?selectField('ig-agent','Generate as',['<option value=""'+(s.agentId?'':' selected')+'>No agent — first ready engine</option>',...agents.map(a=>'<option value="'+esc(a.id)+'"'+(a.id===s.agentId?' selected':'')+'>'+esc(a.name+' · '+(a.model||a.provider||''))+'</option>')].join(''),{disabled:fixed(),help:fixed()?'':'The engine follows this agent’s current model.'}):'';
    let engineFields;
    if(fixed()) {
      const e=engineById(s.engines,settings().engine);
      engineFields='<div class="field-grid">'+selectField('ig-engine-sel','Engine',e?'<option>'+esc(e.label)+'</option>':'<option>'+esc(settings().engine)+'</option>',{disabled:true})+selectField('ig-model-sel','Model',e?'<option>'+esc(modelsOf(e).find(m=>m.id===settings().model)?.label||settings().model||'Engine default')+'</option>':'<option>—</option>',{disabled:true})+'</div><p class="ig-note">Engine fixed in Settings: always '+esc(engineLabel(s.engines,settings().engine))+'. Change it in Connections › Image generation.</p>';
    } else {
      const e=engineById(s.engines,s.override);
      engineFields='<div class="field-grid">'+selectField('ig-engine-sel','Engine',['<option value=""'+(s.override?'':' selected')+'>Automatic — '+esc(engineLabel(s.engines,resolvedId())||'agent’s model')+'</option>',...s.engines.map(x=>engineOption(x,x.id===s.override))].join(''))+selectField('ig-model-sel','Model',e?modelOptions(e,s.model||defaultModelId(e)):'<option value="">Agent’s current model</option>',{disabled:!e,help:e?'':'Pick an engine to choose a model.'})+'</div>';
    }
    area.innerHTML='<section class="ig-block"><h3>Engine</h3>'+agentSelect+engineFields+'</section>';
  }
  function paintParams() {
    const o=opts();s.values=clampParams(s.values,o);
    q('#ig-params-area').innerHTML=s.status==='ready'?'<section class="ig-block"><h3>Options</h3>'+paramsMarkup(o,s.values)+'</section>':'';
    q('#ig-style-area').innerHTML=s.status==='ready'?'<section class="ig-block">'+group('Style preset',styleCardsMarkup(s.values.style))+'</section>':'';
    const cost=q('#ig-cost');cost.textContent=s.status==='ready'?blockReason()||engineNotice(active()):'';
    paintGenerate();
  }
  function paintRefs() {
    const o=opts(),items=refItems(),max=o.max_references;
    if(s.status!=='ready'){q('#ig-refs-area').innerHTML='';return;}
    s.refs=s.refs.filter(k=>items.some(i=>i.key===k)).slice(0,max);
    const shown=s.refsAll?items:items.filter((r,i)=>i<12||s.refs.includes(r.key));
    q('#ig-refs-area').innerHTML='<section class="ig-block"><h3>Reference images</h3>'+(max?'<p class="ig-lead">Pick up to '+max+' image'+(max===1?'':'s')+' for the engine to follow. <span id="ig-ref-count">'+s.refs.length+' of '+max+' selected</span></p>':'<p class="ig-lead">'+esc(engineLabel(s.engines,resolvedId())||'This engine')+' does not use reference images.</p>')+
      (items.length?'<div class="ig-refs" role="group" aria-label="Reference images">'+shown.map(r=>{const on=s.refs.includes(r.key);return '<label class="ig-ref'+(on?' on':'')+'" title="'+esc(r.title)+'"><input type="checkbox" value="'+esc(r.key)+'"'+(on?' checked':'')+(!max||(!on&&s.refs.length>=max)?' disabled':'')+'><img src="'+esc(r.url)+'" alt="'+esc(r.title)+'" loading="lazy" decoding="async"><span class="ig-ref-check" aria-hidden="true">'+svg('<path d="m5 12 4 4L19 6"/>')+'</span></label>';}).join('')+'</div>'+(items.length>shown.length?'<button type="button" class="button quiet small" data-ig="refs-all">Show all '+items.length+' images</button>':''):'<p class="ig-empty">'+(max?'No images in '+(project?'this project':'your outputs')+' yet. Generate one, then use it as a reference.':'')+'</p>')+'</section>';
  }
  /** Why Generate cannot work right now ('' = fine). Agent mode may still fall back, so only block when nothing can run. */
  function blockReason() {
    if(s.status!=='ready') return '';
    if(!s.engines.some(e=>e.available)) return 'No image engine is ready. Add a Gemini API key, sign in to Codex or connect a gateway in Connections.';
    if(fixed()||s.override) {const st=engineStatus(active());if(!st.ready) return engineLabel(s.engines,resolvedId())+': '+st.text;}
    return '';
  }
  function paintGenerate() {
    const b=q('#ig-generate');b.disabled=s.busy||s.status!=='ready'||!!blockReason();
    b.setAttribute('aria-busy',String(s.busy));
    b.querySelector('span').textContent=s.busy?'Generating…':'Generate';
  }
  function resultCard(img,batch) {
    const canRef=!!img.key&&engineParams(engineById(s.engines,batch.engine)).max_references>0;
    return '<figure class="ig-result"><button type="button" class="ig-thumb" data-lightbox-src="'+esc(img.url)+'" data-lightbox-title="'+esc(img.title)+'" aria-label="View image: '+esc(img.title)+'"><img src="'+esc(img.url)+'" alt="'+esc(batch.prompt.slice(0,120)||img.title)+'" decoding="async"></button><figcaption><a class="button small" href="'+esc(img.url)+'" download="'+esc(img.title)+'">Download</a>'+
      '<button type="button" class="button small" data-ig="use-ref" data-key="'+esc(img.key)+'"'+(canRef?'':' disabled title="This engine does not accept reference images"')+'>Use as reference</button><button type="button" class="button small" data-ig="variations" data-key="'+esc(img.key)+'"'+(canRef&&!s.busy?'':' disabled')+(canRef?'':' title="This engine does not accept reference images"')+'>Generate variations</button></figcaption></figure>';
  }
  function paintResults() {
    const area=q('#ig-results-area'),parts=[];
    if(s.genError) parts.push('<div class="form-error ig-error" role="alert" tabindex="-1" id="ig-gen-error">'+esc(s.genError)+'</div>');
    if(s.busy) parts.push('<div class="ig-progress" role="status"><span class="spinner"></span><div><strong>Generating with '+esc(engineLabel(s.engines,resolvedId())||'the selected engine')+'…</strong><p class="muted">This can take up to a minute. You can close this window; the image is saved to Outputs when it is ready.</p></div></div>');
    for(const batch of s.batches) parts.push('<section class="ig-batch" aria-label="Generated images"><h3>'+esc(engineLabel(s.engines,batch.engine)+(batch.model?' · '+batch.model:''))+'</h3><p class="muted small">'+esc(paramSummary(batch.params))+'</p><div class="ig-grid" data-lightbox-group>'+batch.images.map(i=>resultCard(i,batch)).join('')+'</div></section>');
    if(!parts.length) parts.push('<div class="ig-placeholder"><span aria-hidden="true">'+svg('<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>')+'</span><strong>Your images appear here</strong><p>Describe what you want, adjust the options and press Generate.</p></div>');
    area.innerHTML=parts.join('');
  }
  const paintAll=()=>{paintEngine();paintParams();paintRefs();paintResults();};

  async function load() {
    s.status='loading';paintAll();
    if(cfg.getMode()==='demo'){s.status='error';s.error='Open the local app to generate images. The preview does not call image engines.';paintAll();return;}
    try {
      const {engines,settings:raw}=await loadEngines(cfg.api);
      s.engines=engines;s.settings=normalizeImageSettings(raw&&Object.keys(raw).length?raw:data().settings?.image,engines);
      s.values={...DEFAULT_DEFAULTS,...s.settings.defaults};s.status='ready';
    } catch(e) {s.status='error';s.error='Update and restart AgentFlow to use image generation. ('+loadError(e)+')';}
    paintAll();
  }
  async function generate(variation) {
    if(s.busy||s.status!=='ready') return;
    const promptEl=q('#ig-prompt'),base=promptEl.value;
    const refs=variation?[variation]:selectedRefs();
    s.genError='';
    let payload;
    try {
      payload=buildPayload({prompt:variation?variationPrompt(base.trim()||s.batches[0]?.prompt||''):base,negative:q('#ig-negative').value,values:s.values,opts:opts(),engine:s.override,model:s.model,fixed:fixed(),references:refs,agentId:s.agentId,projectId:s.projectId});
    } catch(e) {s.genError=e.message;paintResults();q('#ig-gen-error')?.focus();if(!base.trim()) promptEl.setAttribute('aria-invalid','true');return;}
    promptEl.removeAttribute('aria-invalid');
    s.busy=true;paintGenerate();paintResults();announce('Generating image…');
    if(matchMedia('(max-width:700px)').matches) q('#ig-results-area').scrollIntoView({block:'start',behavior:'smooth'});
    try {
      const result=normalizeResult(await cfg.api('/images/generate',payload),base.trim());
      if(!result.images.length) throw new Error('The engine did not return an image. Try again or choose another engine.');
      s.batches=[result,...s.batches].slice(0,6);
      announce(result.images.length+' image'+(result.images.length===1?'':'s')+' generated.');
      cfg.refresh(false).then(()=>{if(dialog.isConnected)paintRefs();}).catch(()=>{});
      if(!dialog.isConnected||!dialog.open) cfg.toast('Image ready. Find it in the project outputs.');
    } catch(e) {s.genError=e.message||'Image generation failed.';announce('Image generation failed.');}
    finally {
      s.busy=false;
      if(dialog.isConnected) {
        paintGenerate();paintResults();
        if(s.genError) q('#ig-gen-error')?.focus();
        else if(matchMedia('(max-width:700px)').matches) q('.ig-batch')?.scrollIntoView({block:'start',behavior:'smooth'});
      }
    }
  }
  function close() {if(dialog.open) dialog.close();else dialog.remove();}

  dialog.addEventListener('click',event=>{
    const el=event.target.closest('[data-ig]');if(!el||el.disabled) return;
    const act=el.dataset.ig;
    if(act==='close') close();
    else if(act==='retry') load();
    else if(act==='refs-all') {s.refsAll=true;paintRefs();}
    else if(act==='generate') generate();
    else if(act==='step') {s.values.count=(Number(s.values.count)||1)+Number(el.dataset.dir);paintParams();(q('[data-ig=step][data-dir="'+el.dataset.dir+'"]:not(:disabled)')||q('[data-ig=step]:not(:disabled)'))?.focus();}
    else if(act==='use-ref'||act==='variations') {
      const batchImg=s.batches.flatMap(b=>b.images).find(i=>i.key===el.dataset.key);if(!batchImg) return;
      const item={key:batchImg.key,kind:'artifact',id:batchImg.id,title:batchImg.title,url:batchImg.url};
      if(!s.extraRefs.some(r=>r.key===item.key)) s.extraRefs=[item,...s.extraRefs];
      if(act==='variations') generate(item);
      else {
        const next=toggleReference(s.refs.filter(k=>k!==item.key),item.key,opts().max_references);
        if(next.limited) {announce('Reference limit reached. Remove one first.');cfg.toast('Remove a reference first — this engine accepts '+opts().max_references+'.',true);return;}
        s.refs=next.selected;paintRefs();announce('Added as a reference image.');q('#ig-refs-area')?.scrollIntoView({block:'nearest',behavior:'smooth'});
      }
    }
  });
  dialog.addEventListener('change',event=>{
    const el=event.target;
    if(el.id==='ig-agent') {s.agentId=el.value;s.override='';s.model='';paintEngine();paintParams();paintRefs();}
    else if(el.id==='ig-engine-sel') {s.override=el.value;s.model=defaultModelId(engineById(s.engines,s.override));paintEngine();paintParams();paintRefs();q('#ig-engine-sel')?.focus();}
    else if(el.id==='ig-model-sel') s.model=el.value;
    else if(el.id==='ig-count') {s.values.count=Number(el.value)||1;paintParams();q('#ig-count')?.focus();}
    else if(el.name?.startsWith('ig_')) s.values[el.name.slice(3)]=el.value;
    else if(el.closest('.ig-refs')) {
      const next=toggleReference(s.refs,el.value,opts().max_references);s.refs=next.selected;
      const focusKey=el.value;paintRefs();[...dialog.querySelectorAll('.ig-ref input')].find(i=>i.value===focusKey)?.focus();
    }
  });
  dialog.addEventListener('input',event=>{
    if(event.target.id==='ig-prompt') {q('#ig-chars').textContent=event.target.value.length;event.target.removeAttribute('aria-invalid');}
  });
  dialog.addEventListener('keydown',event=>{
    if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)&&event.target.id==='ig-prompt') {event.preventDefault();generate();return;}
    if(event.key!=='Tab') return;
    const nodes=[...dialog.querySelectorAll(FOCUSABLE)].filter(n=>n.offsetParent!==null||n===document.activeElement);
    if(!nodes.length) return;
    const first=nodes[0],last=nodes.at(-1);
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  });
  dialog.addEventListener('close',()=>{dialog.remove();if(opener?.isConnected)opener.focus({preventScroll:true});});
  dialog.addEventListener('click',event=>{if(event.target===dialog) close();});
  dialog.showModal();
  q('#ig-prompt').focus();
  load();
  return dialog;
}

/** Add the stylesheet once even when index.html does not link it. */
if(typeof document!=='undefined'&&!document.querySelector('link[href$="image-studio.css"]')) {
  const link=document.createElement('link');link.rel='stylesheet';link.href='/image-studio.css';document.head.append(link);
}
