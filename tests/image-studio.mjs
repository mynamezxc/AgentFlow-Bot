import assert from 'node:assert/strict';
import {
  ASPECT_RATIOS,STYLE_PRESETS,aspectGeometry,orderValues,engineParams,unionParams,clampParams,buildPayload,promptError,variationPrompt,
  resolveEngineId,activeEngines,settingsPayload,normalizeImageSettings,moveItem,referenceItems,toggleReference,normalizeResult,paramSummary,engineNotice,loadError,
  paramsMarkup,aspectChipMarkup,styleCardsMarkup,settingsMarkup,fallbackMarkup,enginesListMarkup,imageSettingsSummary,defaultModelId,engineStatus,DEFAULT_DEFAULTS
} from '../dist/image-studio.js';
import {modelSourceLabel,geminiStatusMarkup,geminiModelsMarkup,geminiPanelMarkup} from '../dist/gemini-ui.js';

const GEMINI={id:'gemini',label:'Gemini',available:true,models:[{id:'gemini-3.1-flash-image',label:'Nano Banana 2',default:true},{id:'gemini-3-pro-image',label:'Pro'}],
  params:{aspect_ratios:['16:9','1:1','9:16','21:9','3:2'],sizes:['4K','1K','2K'],formats:['jpeg','png'],max_count:4,max_references:14}};
const CODEX={id:'codex',label:'Codex',available:true,models:[{id:'gpt-image-2',label:'gpt-image-2'}],
  params:{aspect_ratios:['1:1','3:2','2:3'],sizes:['1K'],qualities:['low','auto','high'],formats:['png','webp'],max_count:2,max_references:4}};
const GATEWAY={id:'gateway',label:'Gateway',available:false,reason:'Connect a gateway',models:[],params:{}};
const engines=[CODEX,GEMINI,GATEWAY];
const SETTINGS={mode:'agent',fallback:[{engine:'gemini',model:'gemini-3.1-flash-image'}],defaults:{...DEFAULT_DEFAULTS}};
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ordering + supported-option filtering per engine
assert.deepEqual(orderValues(ASPECT_RATIOS,['16:9','1:1','weird','1:1']),['1:1','16:9','weird']);
assert.deepEqual(engineParams(GEMINI).aspect_ratios,['1:1','3:2','16:9','9:16','21:9']);
assert.deepEqual(engineParams(GEMINI).sizes,['1K','2K','4K']);
assert.deepEqual(engineParams(GEMINI).qualities,[]);
assert.deepEqual(engineParams(CODEX).qualities,['auto','low','high']);
assert.deepEqual(engineParams(CODEX).sizes,['1K']);
assert.equal(engineParams(CODEX).max_references,4);
assert.equal(engineParams(undefined).max_count,1);
assert.equal(engineParams(GATEWAY).max_references,0);
const union=unionParams([CODEX,GEMINI]);
assert.deepEqual(union.formats,['png','jpeg','webp']);
assert.equal(union.max_count,4);assert.equal(union.max_references,14);

// aspect chip geometry is proportional and bounded
for(const ratio of ASPECT_RATIOS) {
  const {w,h}=aspectGeometry(ratio,22),[a,b]=ratio.split(':').map(Number);
  assert.ok(Math.max(w,h)===22,ratio);
  assert.ok(Math.abs(w/h-a/b)<0.12,ratio+' '+w+'x'+h);
}
assert.deepEqual(aspectGeometry('1:1'),{w:22,h:22});assert.deepEqual(aspectGeometry('21:9'),{w:22,h:9});assert.deepEqual(aspectGeometry('9:16',32),{w:18,h:32});
assert.deepEqual(aspectGeometry('nonsense'),{w:22,h:22});
assert.match(aspectChipMarkup('16:9',true),/width:22px;height:12px/);

// pickers show only the supported options
const codexHtml=paramsMarkup(engineParams(CODEX),{...DEFAULT_DEFAULTS});
assert.ok(codexHtml.includes('value="3:2"')&&!codexHtml.includes('value="16:9"'));
assert.ok(codexHtml.includes('name="ig_quality"')&&!codexHtml.includes('value="4K"'));
const geminiHtml=paramsMarkup(engineParams(GEMINI),{...DEFAULT_DEFAULTS});
assert.ok(!geminiHtml.includes('name="ig_quality"'),'no quality group when unsupported');
assert.ok(geminiHtml.includes('value="21:9"')&&geminiHtml.includes('value="4K"')&&geminiHtml.includes('max="4"'));
assert.ok(!paramsMarkup(engineParams(GATEWAY),{}).includes('ig-stepper'),'stepper only when more than one image is possible');
assert.equal((geminiHtml.match(/name="ig_aspect_ratio"/g)||[]).length,5);
assert.equal((geminiHtml.match(/ checked>/g)||[]).length,3,'one checked value per picker');
assert.equal(STYLE_PRESETS.map(s=>s.id).join(),'none,photo,product,illustration,3d,flat-icon,anime,watercolor,cinematic,logo');
assert.equal((styleCardsMarkup('anime').match(/name="ig_style"/g)||[]).length,10);
assert.match(styleCardsMarkup('anime'),/value="anime" checked/);

// clamping when the engine changes
assert.deepEqual(clampParams({aspect_ratio:'16:9',size:'4K',quality:'high',format:'jpeg',count:4,style:'logo'},engineParams(CODEX)),{aspect_ratio:'1:1',size:'1K',quality:'high',format:'png',count:2,style:'logo'});
assert.deepEqual(clampParams({aspect_ratio:'16:9',quality:'high',count:9,style:'nope'},engineParams(GEMINI)),{aspect_ratio:'16:9',size:'1K',quality:'',format:'png',count:4,style:'none'});
assert.equal(clampParams({count:'abc'},engineParams(GEMINI)).count,1);
assert.equal(clampParams({count:0},engineParams(GEMINI)).count,1);

// engine resolution mirrors the server rules
assert.equal(resolveEngineId({engines,settings:SETTINGS,agentProvider:'codex'}),'codex');
assert.equal(resolveEngineId({engines,settings:SETTINGS,agentProvider:'gemini'}),'gemini');
assert.equal(resolveEngineId({engines,settings:SETTINGS,agentProvider:'claude'}),'gemini','claude falls back to the list');
assert.equal(resolveEngineId({engines,settings:{mode:'agent',fallback:[]},agentProvider:'claude'}),'codex','then the first ready engine');
assert.equal(resolveEngineId({engines,settings:{mode:'fixed',engine:'gemini'},agentProvider:'codex',override:'codex'}),'gemini','fixed wins over everything');
assert.equal(resolveEngineId({engines,settings:SETTINGS,agentProvider:'codex',override:'gemini'}),'gemini');
assert.equal(resolveEngineId({engines:[{...CODEX,available:false},GEMINI],settings:{mode:'agent',fallback:[]},agentProvider:'codex'}),'gemini');
assert.deepEqual(activeEngines({engines,settings:{mode:'fixed',engine:'codex'}}).map(e=>e.id),['codex']);
assert.deepEqual(activeEngines({engines,settings:SETTINGS}).map(e=>e.id),['codex','gemini'],'engines that are ready or have models, until one is known');
assert.deepEqual(activeEngines({engines,settings:SETTINGS,agentProvider:'claude'}).map(e=>e.id),['gemini']);
assert.equal(defaultModelId(GEMINI),'gemini-3.1-flash-image');assert.equal(defaultModelId(GATEWAY),'');
assert.deepEqual(engineStatus(GATEWAY),{ready:false,text:'Connect a gateway'});

// payload building
const opts=engineParams(GEMINI);
const refs=[{kind:'artifact',id:'a1'},{kind:'attachment',id:'t1'},{kind:'artifact',id:'a2'}];
assert.deepEqual(buildPayload({prompt:'  A red fox  ',negative:' text ',values:{aspect_ratio:'16:9',size:'2K',quality:'high',format:'jpeg',count:2,style:'photo'},opts,engine:'gemini',model:'gemini-3-pro-image',references:refs,agentId:'a-lead',projectId:'p1'}),
  {prompt:'A red fox',aspect_ratio:'16:9',size:'2K',format:'jpeg',count:2,style:'photo',negative:'text',engine:'gemini',model:'gemini-3-pro-image',reference_artifact_ids:['a1','t1','a2'],agent_id:'a-lead',project_id:'p1'});
const lean=buildPayload({prompt:'Logo',values:{},opts,fixed:false});
assert.deepEqual(lean,{prompt:'Logo',aspect_ratio:'1:1',size:'1K',format:'png',count:1});
assert.equal(buildPayload({prompt:'x',values:{},opts,engine:'codex',model:'m',fixed:true}).engine,undefined,'engine/model are not sent when the owner fixed an engine');
assert.equal(buildPayload({prompt:'x',values:{},opts,engine:'',model:'m'}).model,undefined,'no model without an engine');
assert.equal(buildPayload({prompt:'x',values:{},opts:engineParams(GATEWAY),references:refs}).reference_artifact_ids,undefined,'no references for engines without support');
assert.equal(buildPayload({prompt:'x',values:{},opts:{...opts,max_references:1},references:refs}).reference_artifact_ids.length,1);
assert.throws(()=>buildPayload({prompt:'   ',opts}),/Describe the image/);
assert.throws(()=>buildPayload({prompt:'x'.repeat(4001),opts}),/limited to 4000/);
assert.equal(promptError('ok','y'.repeat(1001)).includes('Avoid'),true);
// real server shapes: 'auto' values sort first and get friendly labels; engine notices come from the server
const AUTO={id:'x',label:'X',available:true,models:[],notice:'Custom plan note.',params:{aspect_ratios:['1:1','auto'],sizes:['1K','auto'],max_count:1}};
assert.deepEqual(engineParams(AUTO).aspect_ratios,['auto','1:1']);assert.deepEqual(engineParams(AUTO).sizes,['auto','1K']);
assert.match(aspectChipMarkup('auto',false),/ig-ar auto[\s\S]*<b>Auto<\/b>/);
assert.equal(engineNotice(AUTO),'Custom plan note.');assert.match(engineNotice({id:'codex'}),/3\u20135/);assert.equal(engineNotice({id:'gemini'}),'');
assert.match(enginesListMarkup([AUTO]),/Custom plan note\./);
assert.match(variationPrompt(' a cat '),/^a cat\n\nCreate a new variation/);

// settings <-> contract
const norm=normalizeImageSettings({mode:'fixed',engine:'nope',fallback:[{engine:'gemini',model:'m'},null,{}],defaults:{count:3}},engines);
assert.equal(norm.engine,'');assert.deepEqual(norm.fallback,[{engine:'gemini',model:'m'}]);assert.equal(norm.defaults.count,3);assert.equal(norm.defaults.format,'png');
assert.deepEqual(normalizeImageSettings(undefined,engines),{mode:'agent',engine:'',model:'',fallback:[],defaults:DEFAULT_DEFAULTS});
assert.deepEqual(settingsPayload({mode:'agent',engine:'gemini',model:'x',fallback:[{engine:'codex',model:''},{engine:'ghost',model:'z'}],defaults:{aspect_ratio:'16:9',size:'4K',quality:'high',format:'webp',count:3,style:'cinematic'}},engines),
  {image:{mode:'agent',fallback:[{engine:'codex',model:'gpt-image-2'}],defaults:{aspect_ratio:'16:9',size:'4K',quality:'high',format:'webp',count:3,style:'cinematic'}}});
assert.deepEqual(settingsPayload({mode:'fixed',engine:'codex',model:'bogus',fallback:[],defaults:{aspect_ratio:'16:9',size:'4K',quality:'high',format:'jpeg',count:4,style:'logo'}},engines),
  {image:{mode:'fixed',engine:'codex',model:'gpt-image-2',fallback:[],defaults:{aspect_ratio:'1:1',size:'1K',quality:'high',format:'png',count:2,style:'logo'}}});
assert.deepEqual(settingsPayload({mode:'fixed',engine:'gemini',model:'gemini-3-pro-image',fallback:[],defaults:{aspect_ratio:'16:9',quality:'high',count:2}},engines).image.defaults,{aspect_ratio:'16:9',size:'1K',format:'png',count:2,style:'none'},'unsupported pickers are omitted, not sent empty');
assert.throws(()=>settingsPayload({mode:'fixed',engine:'',fallback:[],defaults:{}},engines),/Choose the engine/);
assert.deepEqual(moveItem([1,2,3],0,2),[2,3,1]);assert.deepEqual(moveItem([1,2,3],1,5),[1,2,3]);assert.deepEqual(moveItem([1,2,3],2,1),[1,3,2]);
assert.equal(imageSettingsSummary({mode:'fixed',engine:'gemini'},engines),'Always Gemini');assert.equal(imageSettingsSummary(undefined,engines),'Follows each agent’s model');

// settings panel markup
const panel=settingsMarkup(normalizeImageSettings(SETTINGS,engines),engines);
assert.match(panel,/Use each agent.s current model \(recommended\)/);assert.match(panel,/Always use/);
assert.ok(panel.includes('Connect a gateway')&&panel.includes('3–5×'));
assert.ok(!panel.includes('id="ig-engine"'),'engine/model selects only in fixed mode');
assert.match(settingsMarkup({...norm,mode:'fixed',engine:'gemini',model:'gemini-3-pro-image'},engines),/id="ig-engine"[\s\S]*selected>Gemini/);
assert.match(settingsMarkup({...norm,mode:'fixed',engine:'gemini',model:'gemini-3-pro-image'},engines),/<option value="gemini-3-pro-image" selected>Pro/);
assert.match(settingsMarkup({...norm,mode:'fixed',engine:'gemini'},engines),/<option value="gateway" disabled>Gateway — Connect a gateway/);
assert.ok(!settingsMarkup({...norm,mode:'fixed',engine:'codex'},engines).includes('value="16:9"'),'defaults are filtered by the fixed engine');
const fb=fallbackMarkup([{engine:'gemini',model:'gemini-3-pro-image'},{engine:'codex',model:'gpt-image-2'}],engines);
assert.match(fb,/data-ig="fb-up" data-id="0"[^>]*disabled/);assert.match(fb,/data-ig="fb-down" data-id="1"[^>]*disabled/);assert.ok(fb.includes('Gemini · Pro'));assert.equal((fb.match(/draggable="true"/g)||[]).length,2);
assert.match(fallbackMarkup([],engines),/No fallback yet/);
assert.match(enginesListMarkup([CODEX,GATEWAY]),/ig-badge blocked">Connect a gateway/);

// references
const artifacts=[{id:'a1',project_id:'p1',is_image:true,path:'x/a.png'},{id:'a2',project_id:'p2',is_image:true,path:'b.png'},{id:'a3',project_id:'p1',path:'doc.md'},{id:'a4',project_id:'p1',path:'c.webp'},{id:'a5',project_id:'p1',is_image:true,archived:true,path:'d.png'}];
const list=referenceItems({artifacts,attachments:[{id:'t1',project_id:'p1',is_image:true,name:'shot.png'},{id:'t2',project_id:'p1',is_image:false,name:'n.txt'}],projectId:'p1'});
assert.deepEqual(list.map(i=>i.key),['attachment:t1','artifact:a4','artifact:a1']);
assert.equal(list[0].url,'/api/attachments/t1');assert.equal(list[2].url,'/api/outputs/a1/raw');assert.equal(list[2].title,'a.png');
assert.equal(referenceItems({artifacts,projectId:''}).length,3);
assert.equal(referenceItems({artifacts,projectId:'p1',extra:[{key:'artifact:new',id:'new'}]})[0].key,'artifact:new');
assert.deepEqual(toggleReference([],'a',2),{selected:['a'],limited:false});
assert.deepEqual(toggleReference(['a','b'],'c',2),{selected:['a','b'],limited:true});
assert.deepEqual(toggleReference(['a','b'],'a',2),{selected:['b'],limited:false});
assert.deepEqual(toggleReference(['a'],'b',0),{selected:['a'],limited:true});

// result normalisation
const result=normalizeResult({images:[{artifact_id:'z9',path:'media/2026/fox.png',url:'/api/outputs/z9/raw',width:1024,height:1024},{path:'orphan.png',url:'/x.png'},{}],engine:'gemini',model:'m',params:{aspect_ratio:'1:1',size:'1K',format:'png',quality:'auto'}},'fox');
assert.equal(result.images.length,2);assert.equal(result.images[0].title,'fox.png');assert.equal(result.images[0].key,'artifact:z9');assert.equal(result.images[1].key,'');
assert.equal(normalizeResult({images:[{artifact_id:'q'}]}).images[0].url,'/api/outputs/q/raw');assert.equal(normalizeResult(null).images.length,0);
assert.equal(paramSummary({aspect_ratio:'1:1',size:'1K',format:'png',quality:'auto'}),'1:1 · 1K · PNG');

assert.match(loadError(new SyntaxError('Unexpected token N')),/does not provide/);assert.equal(loadError(new Error('Boom')),'Boom');

// Gemini connection markup
assert.equal(modelSourceLabel('acp'),'Gemini CLI');assert.equal(modelSourceLabel('api'),'Gemini API');assert.equal(modelSourceLabel('aliases'),'CLI aliases');
assert.equal(modelSourceLabel('gemini-api'),'Gemini API');assert.equal(modelSourceLabel('cli aliases'),'CLI aliases');assert.equal(modelSourceLabel(''),'');
const connected={id:'gemini',installed:true,connected:true,account_label:'dev@example.com',auth:'oauth',api_key_set:false,models_source:'aliases'};
const status=geminiStatusMarkup(connected,esc);
assert.ok(status.includes('Connected')&&status.includes('Google sign-in')&&status.includes('No API key')&&status.includes('Models: CLI aliases')&&status.includes('dev@example.com'));
assert.ok(geminiStatusMarkup({...connected,api_key_set:true,auth:'api_key'},esc).includes('Key saved'));
assert.ok(geminiStatusMarkup({installed:false},esc).includes('CLI not found'));
assert.ok(geminiStatusMarkup(connected,esc,true).includes('Waiting for sign-in'));
assert.ok(geminiStatusMarkup(undefined,esc).includes('not available yet'));
const field=(name,label,value,type,o={})=>'<div data-field="'+name+'" data-type="'+type+'" data-help="'+esc(o.help||'')+'"></div>';
const button=(text,action)=>'<button data-action="'+action+'">'+esc(text)+'</button>';
const gp=geminiPanelMarkup({...connected,api_key_set:true},[],{esc,field,button});
assert.ok(gp.includes('gemini-login')&&gp.includes('gemini-key-save')&&gp.includes('gemini-key-remove')&&gp.includes('data-type="password"'));
assert.ok(!gp.includes('value="AIza'),'the key is never echoed');
assert.ok(!geminiPanelMarkup(connected,[],{esc,field,button}).includes('gemini-key-remove'),'remove only when a key is saved');
assert.ok(geminiPanelMarkup({installed:false},[],{esc,field,button}).includes('Gemini CLI was not found'));
const gm=geminiModelsMarkup([{id:'gemini-3.1-flash-image',name:'Nano Banana 2',source:'api',capabilities:['image']},{id:'auto',name:'Auto',source:'aliases'}],esc);
assert.ok(gm.includes('Image')&&gm.includes('Chat')&&gm.includes('Gemini API')&&gm.includes('CLI aliases'));
assert.ok(geminiModelsMarkup([],esc).includes('No Gemini models'));
console.log('image-studio tests passed');
