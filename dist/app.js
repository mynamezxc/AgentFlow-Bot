import {createCollaboration} from './collaboration.js';
import {createIntegrations} from './integrations.js';
import {agentAvatar,facePicker,colors,agentGroup} from './identity.js';
import {usageDetails,usagePlanBadge,withRetained} from './usage-ui.js';
import {morph,keepInView} from './layout.js';
import {createSettingsPanels} from './settings-panels.js';
import {createHeartbeat} from './heartbeat-ui.js';
import {createImageSettings,configureImageStudio,openImageStudio,imageSettingsSummary} from './image-studio.js';
import {telegramLabel,telegramBody,telegramSettings} from './telegram-ui.js';
import {mergeState,pollDelay,loadCachedState,saveCachedState,cacheable} from './state-sync.js';
// The web fonts load without blocking the first paint (index.html leaves the stylesheet on media=print until now).
document.getElementById('web-fonts')?.setAttribute('media','all');
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value = '') => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = {
  logo:'<path d="m5 19 7-14 7 14M8 14h8"/>',
  grid:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  agents:'<rect x="5" y="7" width="14" height="13" rx="4"/><path d="M12 3v4M2 12h3m14 0h3M9 12h.01M15 12h.01M9 16h6"/><circle cx="12" cy="3" r="1"/>',
  tasks:'<rect x="5" y="4" width="15" height="17" rx="2"/><path d="m8 9 1 1 2-2m2 1h4m-9 6 1 1 2-2m2 1h4M9 4V2m7 2V2"/>',
  chat:'<path d="M21 11a8 8 0 0 1-8 8H6l-4 3 1-7a8 8 0 1 1 18-4Z"/><path d="M7 10h10m-10 4h6"/>',
  calendar:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4m10-4v4M3 11h18m-13 4h.01M12 15h.01M16 15h.01"/>',
  skills:'<path d="m12 3 8 4.5v9L12 21l-8-4.5v-9L12 3Z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9m-4-16 8 4.5"/>',
  book:'<path d="M12 5C8 2 4 3 2 4v15c3-1 7-1 10 2 3-3 7-3 10-2V4c-2-1-6-2-10 1Zm0 0v16"/>',
  pulse:'<path d="M2 12h5l3-8 4 16 3-8h5"/>',
  plug:'<path d="m8 9 7 7m-3-11 7 7M8 9l-3 3a5 5 0 0 0 7 7l3-3M16 9l4-4M12 5l4-4M5 19l-3 3"/>',
  settings:'<path d="m10 3-.5 3-2 1-3-.5-2 3 2 2v2l-2 2 2 3 3-.5 2 1 .5 3h4l.5-3 2-1 3 .5 2-3-2-2v-2l2-2-2-3-3 .5-2-1-.5-3Z"/><circle cx="12" cy="12" r="3"/>',
  search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>',
  down:'<path d="m7 10 5 5 5-5"/>',
  expand:'<path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/>',
  collapse:'<path d="M20 10h-6V4M4 14h6v6M14 10l7-7M3 21l7-7"/>',
  bell:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  help:'<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2c-1 .5-1.5 1-1.5 2M12 17h.01"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
  more:'<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  check:'<path d="m5 12 4 4L19 6"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  spark:'<path d="m12 3 2.6 6.4L21 12l-6.4 2.6L12 21l-2.6-6.4L3 12l6.4-2.6L12 3Zm7-2v4m-2-2h4"/>',
  send:'<path d="m22 2-7 20-4-9-9-4 20-7ZM11 13 22 2"/>',
  file:'<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M8 13h8m-8 4h5"/>',
  facebook:'<path d="M14 21v-8h3l.5-4H14V7c0-1 .3-2 2-2h2V2h-3a5 5 0 0 0-5 5v2H7v4h3v8"/>',
  telegram:'<path d="m22 3-4 18-6-5-4 4v-7L22 3ZM8 13 2 10 22 3"/>',
  globe:'<circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/>',
  image:'<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
  play:'<path d="m8 4 12 8-12 8V4Z"/>',
  pause:'<path d="M8 5v14M16 5v14"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
  download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  folder:'<path d="M3 7V4h6l2 3h10v13H3V7Z"/>',
  lock:'<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3m-4 5v2"/>',
  team:'<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m2-17a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 4v3"/>',
  link:'<path d="m10 13 4-4m-5-2 3-3a5 5 0 0 1 7 7l-3 3m-1 3-3 3a5 5 0 0 1-7-7l3-3"/>',
  code:'<path d="m7 6-6 6 6 6m10-12 6 6-6 6M14 3l-4 18"/>',
  refresh:'<path d="M20 7a9 9 0 0 0-15-3L2 7m0-5v5h5m-3 10a9 9 0 0 0 15 3l3-3m0 5v-5h-5"/>',
  flag:'<path d="M5 21V4m0 1h12l-2 4 2 4H5"/>',
  shield:'<path d="M12 3 4 6v5c0 5 3.4 8.4 8 10 4.6-1.6 8-5 8-10V6l-8-3Z"/><path d="m9 12 2 2 4-4"/>'
};
const icon = name => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (icons[name] || icons.agents) + '</svg>';
const labels = {overview:'Overview', agents:'Agents', tasks:'Tasks', chat:'Chat', content:'Content calendar', skills:'Skill library', knowledge:'Memory & knowledge', automations:'Automations', connections:'Connections', settings:'Settings'};
const states = {idle:'Ready',working:'Working',paused:'Paused',backlog:'Backlog',ready:'Ready',running:'Running',review:'Awaiting review',done:'Done',failed:'Needs attention',draft:'Draft',approved:'Approved',published:'Published',unknown:'Reconciliation required',publishing:'Publishing',queued:'Queued',cancelled:'Stopped',proposed:'Awaiting approval',rejected:'Reject'};
let categories = {all:'All',general:'General'};
function refreshCategories() {
  categories=Object.fromEntries([['all','All'],...new Set(['general',...['agents','tasks','skills'].flatMap(k=>(data?.[k]||[]).filter(x=>!x.archived).map(x=>x.category)).filter(x=>typeof x==='string'&&x&&x!=='all')])].map(x=>Array.isArray(x)?x:[x,x==='general'?'General':x]));
}
const providerNames = {codex:'Codex CLI',claude:'Claude Code',gemini:'Gemini',gateway:'9Router / Gateway'};
states.blocked_account='Account sign-in required';
states.waiting_quota='Waiting for account quota';
let data, mode, route = 'chat', search = new URLSearchParams(location.search).get('q') || '', filter = new URLSearchParams(location.search).get('filter') || 'all';
let page = 1, taskView = 'board', chatRoom = 'dm-a-lead', chatAgent = 'a-lead', weekOffset = 0, knowledgeScope = 'all', skillsTab = 'all';
let appError = '', searchTimer, toastTimer, opener, modalDirty = false, lastSignature = '', inflight = null;
const modal = $('#modal');
const agent = id => data.agents.find(a => a.id === id) || {id, name:'Unassigned',avatar:'?',color:'slate',role:''};
const visible = kind => (data[kind] || []).filter(x => !x.archived && !x.sample);
const matches = (...values) => values.join(' ').toLocaleLowerCase('en').includes(search.trim().toLocaleLowerCase('en'));
const avatar = (a, size='') => agentAvatar(a,size);
const status = value => '<span class="status ' + esc(value) + '"><span class="dot"></span>' + esc(states[value] || value) + '</span>';
const date = (value, options={day:'2-digit',month:'2-digit'}) => value ? new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Ho_Chi_Minh',...options}).format(new Date(value.length === 10 ? value + 'T00:00:00+07:00' : value)) : 'Not set';
const time = value => value ? date(value,{hour:'2-digit',minute:'2-digit'}) : '—';
const button = (text, action, id='', cls='', ico='') => '<button type="button" class="button ' + cls + '" data-action="' + action + '" data-id="' + esc(id) + '">' + (ico ? icon(ico) : '') + esc(text) + '</button>';
const linkButton = (text, target) => '<a class="text-link" href="#' + target + '">' + esc(text) + icon('arrow') + '</a>';
const sectionHeading = (title, count, right='') => '<div class="section-heading"><h2>' + title + (count !== null ? '<span class="count">' + count + '</span>' : '') + '</h2>' + right + '</div>';
const empty = (title, description, action='') => '<div class="empty">' + icon('search') + '<h3>' + esc(title) + '</h3><p>' + esc(description) + '</p>' + action + '</div>';
const priority = value => '<span class="priority ' + value + '"><i></i>' + ({high:'High',medium:'Medium',low:'Low'}[value] || 'Medium') + '</span>';
const pagination = count => count > 12 ? '<div class="pagination"><span>' + ((page-1)*12+1) + '–' + Math.min(page*12,count) + ' / ' + count + '</span>' + button('Previous','page-prev','','small') + button('Next','page-next',String(Math.ceil(count/12)),'small') + '</div>' : '';
function toast(message, error=false) {
  clearTimeout(toastTimer);
  $('#toast').textContent = message;
  $('#toast').className = 'visible' + (error ? ' error' : '');
  toastTimer = setTimeout(() => $('#toast').className = '', 4500);
}
function persistDemo() { localStorage.setItem('agentflow-preview-v1', JSON.stringify(data)); }
async function api(path, body, method='POST') {
  if (mode === 'demo') return demoApi(path,body,method);
  let response;
  try {
    response = await fetch('/api' + path,{method,headers:{'Content-Type':'application/json'},body:body === undefined ? undefined : JSON.stringify(body),signal:AbortSignal.timeout(350000)});
  } catch { throw new Error('Cannot reach the runtime. Check the local service and retry. Your draft is retained.'); }
  const result = await response.json();
  if (!response.ok) {const failure=new Error(result.error || 'Unable to complete this action.');failure.data=result;failure.status=response.status;throw failure;}
  return result;
}
async function demoApi(path,body={},method) {
  if (['/chat','/models/refresh','/skills/scan','/images/generate','/secrets'].includes(path))
    throw new Error('This preview uses sample data. Open the local app to connect accounts and perform this action.');
  const [kind,id,action] = path.slice(1).split('/');
  if (kind === 'settings') {
    if(body.execution_enabled || body.messenger_enabled) throw new Error('Enable execution and customer replies in the local app.');
    data.settings = {...data.settings,...body};
  } else if(action) {
    const item = data[kind].find(x => x.id === id);
    if(!item) throw new Error('Record not found.');
    if(['run','review','publish'].includes(action)) throw new Error('AI execution and publishing require the local app and connected accounts.');
    if(kind === 'plans' && action === 'approve') throw new Error('Plans that create subagents run in the local app.');
    if(kind === 'tasks') item.status = action === 'approve' ? 'done' : 'ready';
    if(kind === 'posts') {
      if(item.media_type !== 'none' && !item.media_url) throw new Error('A media URL is required before approval.');
      item.status = 'approved';
    }
    item.version = (item.version || 1) + 1;
    if(body.note) item.review_note = body.note;
    if(body.score) item.score = Number(body.score);
  } else if(kind === 'skills' && id === 'import') {
    if(body.url) throw new Error('Importing SKILL.md from a URL requires the local app. Paste content or choose a file in the preview.');
    data.skills.push({...body,id:crypto.randomUUID(),version:1,installed:true});
  } else if(id && method === 'PATCH') {
    const index = data[kind].findIndex(x => x.id === id);
    if(index < 0) throw new Error('Record not found.');
    if(body.version !== data[kind][index].version) throw new Error('Data has changed. Reload before saving.');
    data[kind][index] = {...data[kind][index],...body,version:body.version+1};
  } else {
    const item = {...body,id:crypto.randomUUID(),version:1,created_at:new Date().toISOString()};
    if(kind === 'posts') item.status = 'draft';
    if(kind === 'agents') Object.assign(item,{status:'idle',avatar:item.name[0].toUpperCase(),parent_id:null});
    data[kind].push(item);
  }
  data.events.push({id:crypto.randomUUID(),title:'Preview updated',detail:'Changes are saved in this browser.',created_at:new Date().toISOString()});
  persistDemo();
  return {ok:true};
}
const OPTIONAL_KINDS=['projects','decisions','tool_events','artifacts','skill_versions','agent_revisions','plugin_accounts','custom_tools'];
const withDefaults=state=>{for(const kind of OPTIONAL_KINDS) state[kind] ||= [];return state;};
// Poll bookkeeping: when anything last changed (drives the poll rate), failures in a row (backs off), the revision+runtime stamp (skips idle renders).
let lastChangeAt=Date.now(), pollFailures=0, stateStamp='', runtimeVersion=0, pollTimer=0, cacheDirty=false, cacheSavedAt=0;
const REFRESH_KINDS=new Set(['runs','tasks','agents']);   // non-chat pages only repaint when one of these changed
async function loadState() {
  const since=data?.rev?'?since='+encodeURIComponent(data.rev):'';
  const response = await fetch('/api/state'+since,{signal:AbortSignal.timeout(15000)});
  const result = await response.json();
  if(!response.ok) throw new Error(result.error);
  const before=data?.runtime?JSON.stringify(data.runtime):'';
  let changed=true, kinds=null;
  if(result.full||!data) data=withDefaults(result);
  else {const merged=mergeState(data,result);changed=merged.changed;kinds=merged.kinds;}
  const runtimeChanged=JSON.stringify(data.runtime||{})!==before;
  if(runtimeChanged) runtimeVersion++;
  stateStamp=data.rev+'|'+runtimeVersion;   // identical stamps mean nothing on screen can differ
  if(changed||runtimeChanged) {lastChangeAt=Date.now();cacheDirty=true;}
  delete data.cached;
  return {ok:true,full:!!result.full,changed,kinds,runtimeChanged};
}
async function refresh(render=true) {
  if(mode === 'demo') { refreshCategories();if(render) renderMain(); return {ok:true,changed:true}; }
  while(inflight) await inflight.catch(()=>{});
  try {
    inflight=loadState();
    const result=await inflight;
    pollFailures=0;appError='';
    if(result.changed||result.full) refreshCategories();
    if(render) renderMain();
    return result;
  } catch(error) {
    pollFailures++;
    appError = error.message || 'Unable to load runtime data. Try reloading.';
    if(render) renderMain();
    return {ok:false,error};
  } finally {inflight=null;}
}
function syncQuery() {
  const url = new URL(location.href);
  search ? url.searchParams.set('q',search) : url.searchParams.delete('q');
  filter !== 'all' ? url.searchParams.set('filter',filter) : url.searchParams.delete('filter');
  history.replaceState(null,'',url);
}
function navItem(key,ico,badge='') {
  return '<a href="#' + key + '" class="nav-item ' + (route === key ? 'active' : '') + '"' + (route === key ? ' aria-current="page"' : '') + '>' + icon(ico) + '<span>' + labels[key] + '</span>' + (badge ? '<span class="nav-count">' + badge + '</span>' : '') + '</a>';
}
const co = createCollaboration({getData:()=>data,getMode:()=>mode,getRoom:()=>chatRoom,setRoom:value=>{chatRoom=value;chatAgent=value.replace('dm-','');},esc,icon,avatar,button,field,api,refresh,toast,openModal,closeModal,validateForm,render:()=>{if(route==='chat')renderMain();else shell();}});
const panels=createSettingsPanels({getData:()=>data,getMode:()=>mode,getRoute:()=>route,esc,button,field,api,refresh,toast,openModal,closeModal,render:()=>renderMain()});
const hb=createHeartbeat({getData:()=>data,getMode:()=>mode,esc,icon,button,field,api,refresh,toast,openModal,closeModal,render:()=>renderMain()});
const apps=createIntegrations({getData:()=>data,getMode:()=>mode,esc,icon,button,field,api,refresh,toast,openModal,closeModal,validateForm});
const imageSettings=createImageSettings({getData:()=>data,getMode:()=>mode,api,refresh,toast,openModal,closeModal,button,render:()=>renderMain()});
configureImageStudio({getData:()=>data,getMode:()=>mode,api,refresh,toast});
const openStudio=opts=>openImageStudio({agentId:data.agents?.some(a=>a.id===chatAgent&&!a.archived)?chatAgent:'',projectId:route==='chat'?co.projectId()||'':'',...opts});
function shell() {
  renderedRoute=null;
  $('#app').innerHTML=co.shell(route);
  renderMain();
  const nav=$('.sidebar'), narrow=matchMedia('(max-width:760px)').matches;
  nav.inert=narrow&&!nav.classList.contains('open');
}
function heading(title,description,actions='') {
  return '<div class="page-heading"><div><h1>' + title + '</h1><p>' + description + '</p></div><div class="heading-actions">' + actions + '</div></div>';
}
function modeBanner() {
  if(mode !== 'demo' && !data.settings.demo_data) return '';
  return '<div class="mode-banner">' + icon('help') + '<span>' + (mode === 'demo' ? 'Interactive preview · Sample data · AI and channels are disconnected' : 'Sample workspace · Choose a model and connect an account to get started') + '</span><a class="text-link" href="#connections">Configure connection ' + icon('arrow') + '</a></div>';
}
let renderedRoute=null;
// opts.anchor: a tab/filter/page change inside one route -> patch the DOM in place and keep the filter bar in view.
function renderMain(opts={}) {
  const previousFocus=document.activeElement, focusAction=previousFocus?.dataset.action, focusId=previousFocus?.dataset.id||'';
  const focusInMain=$('#main')?.contains(previousFocus);
  $('#main').classList.toggle('co-content',route==='chat');
  $('#breadcrumb').textContent = labels[route] || 'Not found';
  document.title = (labels[route] || 'Not found') + ' · AgentFlow';
  const views = {overview:overviewView,agents:agentsView,tasks:tasksView,chat:chatView,content:contentView,skills:skillsView,knowledge:knowledgeView,automations:automationsView,connections:connectionsView,settings:settingsView};
  const view = views[route];
  const html = (appError ? '<div class="app-error" role="alert"><p>' + esc(appError) + '</p>' + button('Reload','refresh','','small','refresh') + '</div>' : '') +
    (route === 'overview' ? '' : modeBanner()) + (view ? view() : empty('Page not found','Return to the workspace to continue.','<a class="button primary" href="#overview">Back to overview</a>'));
  // Same route again (filters, tabs, paging, polling): morph so the heading, tab bar and unchanged cards never flash or jump.
  const main = $('#main'), soft = renderedRoute === route && !['chat','settings'].includes(route);
  if(soft) morph(main,html); else main.innerHTML = html;
  renderedRoute = route;
  if(opts.anchor) keepInView($('.co-shell'),$('.tabs',main)||$('.toolbar',main));
  if(route === 'chat') {
    const messages = $('.chat-messages');
    if(messages) messages.scrollTop = messages.scrollHeight;
    $('#chat-input').value = sessionStorage.getItem('agentflow-chat-draft-' + chatRoom) || '';
    co.resizeComposer();
  }
  if(route === 'settings') panels.mounted();
  $$('.filter-button,.tab',$('#main')).forEach(control=>control.setAttribute('aria-pressed',String(control.classList.contains('active'))));
  if(focusInMain&&!previousFocus.isConnected) {
    const next=$$('[data-action]',$('#main')).find(x=>x.dataset.action===focusAction&&(x.dataset.id||'')===focusId);
    (next||$('#main')).focus({preventScroll:true});
  }
}
function metric(label,value,unit,foot,ico,color='') {
  return '<div class="metric"><div class="metric-top"><span>' + label + '</span><span class="metric-icon ' + color + '">' + icon(ico) + '</span></div><div class="metric-value"><strong>' + value + '</strong><span>' + unit + '</span></div><p class="metric-foot">' + foot + '</p></div>';
}
function agentCard(a) {
  const tasks = visible('tasks').filter(t => t.agent_id === a.id && t.status !== 'done');
  return '<article class="agent-card">'+agentGroup(a)+'<div class="agent-top">' + avatar(a) +
    '<button class="icon-button" data-action="agent-menu" data-id="' + a.id + '" aria-label="Configure ' + esc(a.name) + '">' + icon('more') + '</button></div>' +
    '<div class="agent-name"><a href="#agents" data-action="agent-edit" data-id="' + a.id + '">' + esc(a.name) + '</a>' + status(a.status) + '</div><p class="agent-role">' + esc(a.role) + (a.parent_id ? ' · Subagent' : '') + '</p>' +
    '<p class="agent-description" title="' + esc(a.description) + '">' + esc(a.description) + '</p><div class="agent-meta"><span class="model-chip" title="' + esc(a.model || 'No model selected') + '">' + esc(a.model || providerNames[a.provider]) + '</span><span>' + (a.skills || []).filter(id=>data.skills.some(s=>s.id===id&&s.installed)).length + ' skills</span></div>' +
    '<div class="agent-footer"><span>' + icon('tasks') + ' ' + tasks.length + ' tasks</span><button class="text-link" data-action="chat-agent" data-id="' + a.id + '">Chat ' + icon('arrow') + '</button></div></article>';
}
function taskTable(tasks, limited=false) {
  if(!tasks.length) return empty('No matching tasks','Try another keyword or create a task.',button('Create task','task-new','','primary','plus'));
  return '<div class="table-scroll"><table><thead><tr><th scope="col">TASK</th><th scope="col">OWNER</th><th scope="col">STATUS</th><th scope="col">PRIORITY</th>' + (!limited ? '<th scope="col">DUE DATE</th>' : '') + '</tr></thead><tbody>' +
    tasks.map(t => '<tr><td><button class="table-title" data-action="task-detail" data-id="' + t.id + '">' + esc(t.title) + '</button></td><td><span class="assignee">' + avatar(agent(t.agent_id),'tiny') + esc(agent(t.agent_id).name) + '</span></td><td>' + status(t.status) + '</td><td>' + priority(t.priority) + '</td>' + (!limited ? '<td>' + date(t.due) + '</td>' : '') + '</tr>').join('') + '</tbody></table></div>';
}
function activities(limit=4) {
  return '<div class="activity-list">' + [...data.events].reverse().slice(0,limit).map(e =>
    '<div class="activity-item"><span class="activity-mark">' + icon(e.type === 'task' ? 'check' : e.type === 'connection' ? 'plug' : e.type === 'error' ? 'help' : 'spark') + '</span><div><strong>' + esc(e.title) + '</strong><p>' + esc(e.detail) + '</p><time>' + date(e.created_at,{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) + ' · GMT+7</time></div></div>').join('') + '</div>';
}
function overviewView() {
  const agents = visible('agents'), tasks = visible('tasks'), running = data.runs.filter(r=>r.status === 'running').length;
  const greetingDate = date(new Date().toISOString(),{weekday:'long',day:'numeric',month:'long'});
  return heading('A productive day, ' + esc(data.settings.owner_name.split(' ').slice(-2).join(' ')) + ' <span style="font-size:24px">☀</span>',
    'Your AI team is ready. Turn plans into results together.',
    '<span class="muted small">' + icon('calendar') + ' ' + esc(greetingDate) + '</span>' + button('Create task','task-new','','primary','plus')) +
    modeBanner() +
    '<section class="briefing" aria-label="Workflow guide"><div><p class="eyebrow">AI TEAM · ONE SHARED GOAL</p><h2>From ideas to growth.</h2><p>Marketing, SEO and content — together in one workspace.</p><button class="text-link" data-action="plan-new">Start with a plan ' + icon('arrow') + '</button></div>' +
    '<div class="relay" aria-hidden="true"><div class="relay-person">' + avatar(agent('a-lead')) + '<small>Plan work</small></div><span class="relay-line"></span><div class="relay-person">' + avatar(agent('a-content')) + '<small>Creative</small></div><span class="relay-line"></span><div class="relay-person">' + avatar(agent('a-review')) + '<small>Reconcile</small></div></div></section>' +
    '<div class="overview-grid"><div class="primary-column"><section class="metrics" aria-label="Team overview">' +
    metric('Your team',agents.length,'agents','<span class="dot"></span>' + agents.filter(a=>a.status!=='paused').length + ' ready for work','agents') +
    metric('Open tasks',tasks.filter(t=>!['done'].includes(t.status)).length,'tasks',running + ' active AI runs','tasks','blue') +
    metric('Awaiting review',tasks.filter(t=>t.status==='review').length,'tasks',tasks.filter(t=>t.status==='done').length + ' completed tasks','check','green') + '</section>' +
    '<section>' + sectionHeading('Agents',agents.length,linkButton('View all','agents')) + '<div class="agents-grid featured">' + agents.slice(0,4).map(agentCard).join('') + '</div></section>' +
    '<section class="panel">' + sectionHeading('Tasks needing attention',null,linkButton('View task board','tasks')) + taskTable(tasks.filter(t=>t.status!=='done').filter(t=>matches(t.title)).slice(0,4),true) + '</section></div>' +
    '<aside class="activity-column" aria-label="Activity and schedules"><section class="panel"><div class="rail-header"><h2>Recent activity</h2>' + icon('pulse') + '</div>' + activities(3) +
    '<div class="activity-footer"><button class="text-link" data-action="activity">View all activity ' + icon('arrow') + '</button></div></section>' +
    '<section class="panel"><div class="rail-header"><h2>Daily work cadence</h2><small>GMT+7</small></div>' +
    visible('schedules').slice(0,3).map(s=>'<div class="schedule-mini"><span class="schedule-time">' + (s.mode==='daily'?esc(s.time):esc(s.interval_minutes/60)+'h') + '</span><div class="grow"><strong>' + esc(s.name) + '</strong><p>' + esc(agent(s.agent_id).name) + ' · ' + (s.enabled?'Enabled':'Schedule disabled') + '</p></div></div>').join('') +
    '<div class="activity-footer">' + linkButton('Manage schedules','automations') + '</div></section><div class="tip"><div class="tip-title">' + icon('spark') + 'Work better together</div><p>Every task has an owner and an independent reviewer. Shared knowledge keeps the team aligned.</p></div></aside></div>' +
    '<p class="footer-note">AgentFlow · A workspace for growing teams</p>';
}
function filterBar(keys=Object.keys(categories)) {
  return '<div class="toolbar">' + keys.map(k=>'<button class="filter-button ' + (filter===k?'active':'') + '" data-action="filter" data-id="' + esc(k) + '">' + esc(categories[k]) + '</button>').join('') + (search ? '<span class="grow"></span>' + button('Clear search','clear-search','','quiet small','close') : '') + '</div>';
}
function agentsView() {
  const list = visible('agents').filter(a=>(filter==='all'||a.category===filter)&&matches(a.name,a.role,a.description));
  page=Math.min(page,Math.max(1,Math.ceil(list.length/12)));
  return heading('Agents','A teammate for every specialty. Work toward shared goals.',button('Create agent','agent-new','','primary','plus')) +
    filterBar() + '<div class="list-region"><div class="agents-grid">' + (list.length ? list.slice((page-1)*12,page*12).map(agentCard).join('') : empty('Agent not found','Try another name or clear the filters.',button('Clear search','clear-search'))) + '</div>' + pagination(list.length) + '</div>';
}
function tasksView() {
  const list=visible('tasks').filter(t=>(filter==='all'||t.category===filter)&&matches(t.title,t.description,agent(t.agent_id).name));
  const plans=data.plans.filter(p=>p.status==='proposed');
  let result=heading('Every task moves work forward','Assign owners, track progress and review quality together.',button('Planning','plan-new','','','spark') + button('Create task','task-new','','primary','plus'));
  result += '<div class="tabs"><button class="tab ' + (taskView==='board'?'active':'') + '" data-action="task-view" data-id="board">' + icon('grid') + ' Task board</button><button class="tab ' + (taskView==='list'?'active':'') + '" data-action="task-view" data-id="list">' + icon('tasks') + ' List</button><button class="tab ' + (taskView==='plans'?'active':'') + '" data-action="task-view" data-id="plans">' + icon('spark') + ' Plan ' + (plans.length?'('+plans.length+')':'') + '</button></div>';
  if(taskView==='plans') return result + (data.plans.length ? [...data.plans].reverse().map(planCard).join('') : empty('Start your next plan here','Ask Atlas to propose steps, owners and subagents.',button('Planning','plan-new','','primary','spark')));
  result += filterBar(['all','marketing','seo','content','support','engineering']);
  if(!list.length) return result + '<div class="list-region">' + empty('No matching tasks','Try another keyword or create a task.',button('Create task','task-new','','primary','plus')) + '</div>';
  if(taskView==='list') {
    page=Math.min(page,Math.max(1,Math.ceil(list.length/12)));
    return result + '<div class="list-region"><div class="panel">' + taskTable(list.slice((page-1)*12,page*12)) + '</div>' + pagination(list.length) + '</div>';
  }
  return result + '<div class="board">' + ['backlog','ready','running','review','done'].map(s=>{
    const column=list.filter(t=>t.status===s||(s==='ready'&&t.status==='failed'));
    return '<section class="board-column"><div class="board-head"><span class="dot ' + (s==='running'?'purple':s==='done'?'':'gray') + '"></span>' + states[s] + '<span class="count">' + column.length + '</span></div>' + column.slice(0,12).map(t=>
      '<article class="task-card">' + priority(t.priority) + '<h3><button class="table-title" data-action="task-detail" data-id="' + t.id + '">' + esc(t.title) + '</button></h3><div class="tags">' + (t.tags||[categories[t.category]||t.category||'']).map(tag=>'<span class="tag">' + esc(tag) + '</span>').join('') + '</div><div class="task-card-footer"><span class="assignee">' + avatar(agent(t.agent_id),'tiny') + esc(agent(t.agent_id).name) + '</span><span>' + icon('calendar') + ' ' + date(t.due) + '</span></div>' + (t.status==='failed'?'<div class="small" style="color:var(--danger);margin-top:8px">Needs attention</div>':'') + '</article>').join('') + (column.length>12?'<p class="small muted">Remaining ' + (column.length-12) + ' tasks. Open List to see all tasks.</p>':'') + '</section>';
  }).join('') + '</div>';
}
function planCard(p) {
  return '<article class="panel plan-card"><div class="section-heading"><h3>' + esc(p.title) + '</h3>' + status(p.status) + '</div><p>' + esc(p.description) + '</p><ol class="plan-steps">' + p.tasks.map((t,i)=>'<li><span class="step-index">' + (i+1) + '</span><span class="grow">' + esc(t.title) + '</span><span class="muted small">' + esc(agent(t.agent_id).name) + '</span></li>').join('') + '</ol>' + (p.subagents?.length?'<p>' + p.subagents.length + ' subagents will be created after approval.</p>':'') + (p.status==='proposed'?'<div class="row-actions" style="margin-top:15px">' + button('Approve plan','plan-approve',p.id,'primary','check') + button('Reject','plan-reject',p.id,'quiet') + '</div>':'') + '</article>';
}
function chatView() { return co.view(); }
function weekDates() {
  const current=new Date(new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Ho_Chi_Minh',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())+'T12:00:00+07:00');
  const day=(current.getUTCDay()+6)%7; current.setUTCDate(current.getUTCDate()-day+weekOffset*7);
  return Array.from({length:7},(_,i)=>{const d=new Date(current);d.setUTCDate(d.getUTCDate()+i);return d;});
}
function contentView() {
  const days=weekDates();
  return heading('Good content, right on time','From draft to publication. Take control of your content calendar.',button('Generate image','image-new','','','image')+button('Create content','post-new','','primary','plus')) +
    '<div class="toolbar"><span style="font-weight:550">' + date(days[0].toISOString()) + ' – ' + date(days[6].toISOString(),{day:'2-digit',month:'2-digit',year:'numeric'}) + '</span><span class="grow"></span>' + button('Previous week','week-prev','','small') + button('This week','week-today','','small') + button('Next week','week-next','','small') + '</div>' +
    '<div class="panel calendar">' + days.map(d=>{
      const key=date(d.toISOString(),{year:'numeric',month:'2-digit',day:'2-digit'});
      const posts=visible('posts').filter(p=>p.scheduled_at&&date(p.scheduled_at,{year:'numeric',month:'2-digit',day:'2-digit'})===key&&matches(p.title,p.body));
      return '<section class="calendar-day"><div class="calendar-head ' + (key===date(new Date().toISOString(),{year:'numeric',month:'2-digit',day:'2-digit'})?'today':'') + '">' + date(d.toISOString(),{weekday:'short'}) + '<strong>' + date(d.toISOString(),{day:'2-digit'}) + '</strong></div><div class="calendar-body">' + posts.map(p=>
        '<button class="post-card ' + p.channel + '" data-action="post-edit" data-id="' + p.id + '"><span class="post-channel">' + icon(p.channel==='facebook'?'facebook':'file') + (p.channel==='facebook'?'Facebook':'Blog SEO') + '</span><h3>' + esc(p.title) + '</h3><p><span>' + time(p.scheduled_at) + '</span><span>' + states[p.status] + '</span></p></button>').join('') + '</div></section>';
    }).join('') + '</div><div class="info-box">' + icon('help') + ' Only approved content can be scheduled for automatic publishing. SEO blog posts remain drafts for your CMS.</div>' +
    '<section class="panel">' + sectionHeading('All content',visible('posts').length) + (visible('posts').length?'<div class="table-scroll"><table><thead><tr><th scope="col">CONTENT</th><th scope="col">CHANNEL</th><th scope="col">STATUS</th><th scope="col">SCHEDULE</th></tr></thead><tbody>' + visible('posts').filter(p=>matches(p.title,p.body)).slice(0,12).map(p=>'<tr><td><button class="table-title" data-action="post-edit" data-id="' + p.id + '">' + esc(p.title) + '</button></td><td>' + (p.channel==='facebook'?'Facebook':'Blog SEO') + '</td><td>' + status(p.status) + '</td><td>' + date(p.scheduled_at,{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) + '</td></tr>').join('') + '</tbody></table></div>':empty('Your content calendar is empty','Create your first post to plan the week ahead.')) + '</section>';
}
function skillsView() {
  let list=visible('skills').filter(s=>(filter==='all'||s.category===filter)&&matches(s.name,s.description,s.source)&&(skillsTab==='all'||s.installed));
  page=Math.min(page,Math.max(1,Math.ceil(list.length/12)));
  return heading('Better skills, stronger teams','One shared library. Each agent chooses the skills it needs.',button('Scan local skills','skill-scan','','','folder')+button('Import skill','skill-new','','primary','plus')) +
    '<div class="panel sources-banner"><p>Connect ecosystems through <span class="mono">SKILL.md</span>. Choose a source, read the instructions and import the skill you need.</p><a href="https://github.com/anthropics/skills" target="_blank" rel="noopener noreferrer">' + icon('code') + 'Claude</a><a href="https://github.com/openai/skills" target="_blank" rel="noopener noreferrer">' + icon('code') + 'Codex</a><a href="https://agenticskills.io/" target="_blank" rel="noopener noreferrer">' + icon('globe') + 'AgenticSkills</a></div>' +
    '<div class="tabs"><button class="tab ' + (skillsTab==='all'?'active':'') + '" data-action="skills-tab" data-id="all">Explore library</button><button class="tab ' + (skillsTab==='installed'?'active':'') + '" data-action="skills-tab" data-id="installed">Installed (' + data.skills.filter(s=>s.installed).length + ')</button></div>' + filterBar(['all','marketing','seo','content','quality','support','engineering','custom']) +
    '<div class="list-region"><div class="skills-grid">' + (list.length?list.slice((page-1)*12,page*12).map(s=>'<article class="panel skill-card"><div class="skill-top"><span class="skill-icon">' + icon(s.category==='seo'?'search':s.category==='content'?'file':s.category==='quality'?'check':'skills') + '</span><div class="skill-title"><h3 title="' + esc(s.name) + '">' + esc(s.name) + '</h3><small>' + esc(categories[s.category]||'Custom skill') + '</small></div></div><p class="skill-desc" title="' + esc(s.description) + '">' + esc(s.description) + '</p><div class="skill-bottom"><span class="source-chip">' + esc(s.source) + '</span><button class="button small ' + (s.installed?'':'primary') + '" data-action="skill-detail" data-id="' + s.id + '">' + (s.installed?icon('check')+'Installed':icon('plus')+'View skill') + '</button></div></article>').join(''):empty('No matching skills','Clear filters or import SKILL.md from a trusted source.',button('Import skill','skill-new','','primary','plus'))) + '</div>' + pagination(list.length) + '</div>';
}
function knowledgeView() {
  let list=visible('memories').filter(m=>(knowledgeScope==='all'||m.scope===knowledgeScope||m.agent_id===knowledgeScope)&&matches(m.title,m.content));
  page=Math.min(page,Math.max(1,Math.ceil(list.length/12)));
  return heading('Knowledge retained. A stronger team.','Private memory for each agent, shared knowledge for the organization.',button('Add knowledge','memory-new','','primary','plus')) +
    '<div class="toolbar"><button class="filter-button ' + (knowledgeScope==='all'?'active':'') + '" data-action="knowledge-scope" data-id="all">All knowledge</button><button class="filter-button ' + (knowledgeScope==='shared'?'active':'') + '" data-action="knowledge-scope" data-id="shared">' + icon('team') + ' Shared knowledge</button><button class="filter-button ' + (knowledgeScope==='private'?'active':'') + '" data-action="knowledge-scope" data-id="private">' + icon('lock') + ' Private memory</button><span class="grow"></span><label class="small muted" for="memory-owner">Agent</label><select id="memory-owner" class="filter-select"><option value="all">All agents</option>' + visible('agents').map(a=>'<option value="' + a.id + '"' + (knowledgeScope===a.id?' selected':'') + '>' + esc(a.name) + '</option>').join('') + '</select></div>' +
    '<div class="list-region"><div class="knowledge-grid">' + (list.length?list.slice((page-1)*12,page*12).map(m=>'<article class="panel knowledge-card"><span class="skill-icon">' + icon(m.scope==='shared'?'book':'lock') + '</span><h3><button class="table-title" data-action="memory-edit" data-id="' + m.id + '">' + esc(m.title) + '</button></h3><p class="knowledge-preview">' + esc(m.content) + '</p><div class="knowledge-footer"><span>' + (m.scope==='shared'?'Shared with the team':esc(agent(m.agent_id).name)+' · Private') + '</span><button class="text-link" data-action="memory-edit" data-id="' + m.id + '">View details ' + icon('arrow') + '</button></div></article>').join(''):empty('No knowledge here yet','Add brand information, procedures or private notes for an agent.',button('Add knowledge','memory-new','','primary','plus'))) + '</div>' + pagination(list.length) + '</div>' +
    '<div class="info-box">' + icon('lock') + ' The workspace owner manages all records. Agents receive only their own private memory; support receives shared knowledge only.</div>';
}
function automationsView() {
  const runs=[...data.runs].reverse().filter(r=>matches(r.prompt,agent(r.agent_id).name)).slice(0,30);
  return heading('The right work, on schedule','Agents check for due work every ' + hb.intervalText() + ' and only call a model when work is available.',button('Create schedule','schedule-new','','primary','plus')) +
    hb.card() +
    '<div class="schedule-list">' + visible('schedules').filter(s=>matches(s.name,s.prompt)).map(s=>'<article class="panel schedule-row"><span class="schedule-time">' + (s.mode==='daily'?esc(s.time):esc(s.interval_minutes)+'p') + '</span><div class="grow"><h3>' + esc(s.name) + '</h3><p>' + esc(agent(s.agent_id).name) + ' · ' + (s.mode==='daily'?'Daily at '+esc(s.time)+' (GMT+7)':'Every '+s.interval_minutes+' minutes') + '</p><p>' + (s.last_error?'<span style="color:var(--danger)">'+esc(s.last_error)+'</span>':s.next_run&&s.enabled?'Next run: '+date(s.next_run,{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})+' GMT+7':'Not scheduled') + '</p></div><div class="row-actions"><button class="switch ' + (s.enabled?'on':'') + '" role="switch" aria-checked="' + !!s.enabled + '" aria-label="' + (s.enabled?'Disable':'Enable') + ' schedule ' + esc(s.name) + '" data-action="schedule-toggle" data-id="' + s.id + '"></button>' + button('Edit','schedule-edit',s.id,'small') + '</div></article>').join('') + '</div>' +
    '<section class="run-list">' + sectionHeading('Run history',data.runs.length) + '<div class="panel">' + (runs.length?runs.map(r=>'<div class="run-row">' + avatar(agent(r.agent_id),'mini') + '<div class="grow"><strong>' + esc(agent(r.agent_id).name) + '</strong><p>' + esc(r.prompt.slice(0,140)) + '</p>' + (r.error?'<p style="color:var(--danger)">' + esc(r.error) + '</p>':'') + '</div>' + status(r.status) + (['running','queued'].includes(r.status)?button('Stop','run-cancel',r.id,'small'):button('Details','run-detail',r.id,'quiet small')) + '</div>').join(''):empty('No runs yet','Real run history and results will appear when an agent starts working.')) + '</div></section>';
}
function connectionCard(id,title,description,logo,action='connection-edit',extra='') {
  const p=data.runtime?.providers?.find(p=>p.id===id);
  const secrets=data.runtime?.secrets || {};
  const configured = id==='facebook'?!!(data.settings.page_id&&secrets.FACEBOOK_PAGE_TOKEN):id==='telegram'?!!secrets.TELEGRAM_BOT_TOKEN:id==='messenger'?data.settings.messenger_enabled:id==='image'?true:id==='seo'?!!secrets.SEO_WEBHOOK_SECRET:p?.configured;
  const disabled=!!p?.disabled;
  const authNames={oauth:'Google sign-in',api_key:'API key',vertex:'Vertex AI'};
  const label=id==='telegram'?telegramLabel(data.runtime?.telegram,configured).text:id==='image'?imageSettingsSummary(data.settings.image,data.runtime?.image_engines):disabled?'Temporarily disabled':id==='gemini'&&p?.connected?'Connected · '+(authNames[p.auth]||'signed in'):id==='gateway'?(p?.connected?'Connected':configured?'Configured · Not connected':'Not configured'):p?.verified?'Run verified':p?.connected?'Connected':p?.installed?'CLI found':configured?'Configured':'Not configured';
  return '<article class="panel connection">'+usagePlanBadge(p,esc)+'<span class="connection-logo">' + (icons[logo]?icon(logo):esc(logo)) + '</span><h3>' + title + '</h3><p>' + description + '</p>'+(p?.account_label&&id!=='gateway'&&!disabled?'<p class="small muted">Current account: '+esc(p.account_label)+'</p>':'')+(p?.usage?usageDetails(withRetained([p])[0],esc):'')+(id==='gemini'&&p?.api_key_set?'<p class="small muted">API key saved · image generation ready</p>':'')+(id==='gemini'&&p?.models_source?'<p class="small muted">Models from '+esc(({acp:'Gemini CLI',api:'Gemini API',aliases:'CLI aliases'})[p.models_source]||p.models_source)+'</p>':'')+(p?.error?'<p class="small connection-error">'+esc(p.error)+'</p>':'')+'<span class="status ' + (disabled?'paused':id==='image'||p?.connected||p?.verified||(id==='telegram'&&data.runtime?.telegram?.connected)?'done':'idle') + '"><span class="dot"></span>' + label + '</span>' + (disabled?'<button class="button" disabled>Temporarily disabled</button>':'<div class="row-actions wrap">'+extra+button(['codex','claude','gemini'].includes(id)?'View connection':id==='image'?'Image settings':'Configure connection',action,id,'','plug')+'</div>') + '</article>';
}
function connectionsView() {
  const models=data.runtime?.models || [];
  return heading('Connect your team','Your accounts and tools. Choose the right model for each task.') +
    '<section class="panel runtime-panel"><span class="activity-mark">'+icon('agents')+'</span><div class="grow"><h3>Team default model</h3><p>'+esc(data.settings.default_model||'Not configured')+' · New agents inherit this default; individual selections are retained.</p></div>'+button('Choose default model','co-default','','primary','spark')+'</section>'+
    '<section class="panel runtime-panel"><span class="activity-mark">' + icon('pulse') + '</span><div class="grow"><h3>Local AI execution</h3><p>' + (mode==='demo'?'The preview does not call models or publish content. A local runtime is included with the source.':'Enable after selecting agent models. Enabled schedules and automatic tasks will be processed.') + '</p></div><div class="row-actions">' + button(data.settings.execution_enabled?'Pause execution':'Enable execution','execution-toggle','','primary',data.settings.execution_enabled?'pause':'play') + '</div></section>' +
    '<div class="connections-grid">' +
    connectionCard('codex','Codex','Use the current Codex CLI sign-in. The model catalog comes from app-server.','code') +
    connectionCard('claude','Claude Code','Use your current Claude Code sign-in. Model access follows account permissions.','C') +
    connectionCard('gemini','Gemini','Sign in with Google for chat models, or add a Gemini API key to generate images.','spark') +
    connectionCard('gateway','9Router / Gateway','Load models through /v1/models and route work through your gateway.','plug') +
    connectionCard('facebook','Facebook Page','Schedule posts, images and videos after content approval.','facebook') +
    connectionCard('telegram','Telegram','Run the team from a Telegram group: questions, permission requests, files and work status arrive in the chat. Long polling needs no public address.','telegram') +
    connectionCard('messenger','Messenger','Support replies using shared knowledge and customer messages.','chat') +
    connectionCard('image','Image generation','Images follow each agent\u2019s current model (Codex or Gemini), or one engine you fix for the team.','image','connection-edit',button('Open Image Studio','image-new','','primary','image')) +
    connectionCard('seo','Webhook SEO','Receive flexible briefs from your system with event_id deduplication.','link') + '</div>' + apps.view()+
    '<section class="panel model-list">' + sectionHeading('Observed model catalog',models.length,(data.runtime?.providers||[]).filter(p=>!p.disabled&&(p.id==='gateway'?p.connected:p.installed)).map(p=>button('Refresh '+(p.id==='gateway'?'gateway':p.id==='claude'?'Claude':p.id==='gemini'?'Gemini':'Codex')+' models','models-refresh',p.id,'small','refresh')).join('')) +
    (models.length?models.slice(0,100).map(m=>'<div class="model-row"><span><strong>' + esc(m.name) + '</strong><br><span class="mono muted">' + esc(m.id) + '</span></span><span class="small muted">' + esc(m.source) + ' · ' + time(m.observed_at) + '</span></div>').join(''):empty('No model catalog loaded','Connect Codex, Claude Code or a gateway to load the available models. Model names are never assumed.')) +
    '<div class="info-box">Catalogs reflect the current account; successful execution verifies model access. Codex uses model/list, Claude Code its SDK catalog, and Gemini the CLI or the Gemini API.</div></section>';
}
function settingsView() {
  return heading('Your workspace','Update workspace details and manage organization data.') +
    '<div class="settings-grid"><section class="panel settings-panel"><h2>Workspace details</h2><p>This information appears in the team interface.</p><form id="settings-form" novalidate>' +
    field('workspace_name','Workspace name',data.settings.workspace_name,'text',{required:true}) + field('owner_name','Owner name',data.settings.owner_name,'text',{required:true}) +
    '<div class="field"><label>Working timezone</label><p class="info-box" style="margin:0">Vietnam · Asia/Ho_Chi_Minh · GMT+7</p></div>' +
    '<button type="submit" class="button primary">Save changes</button><div class="form-error" id="settings-error" role="alert"></div></form></section>' +
    '<section class="panel settings-panel"><h2>Data & workspace</h2><p>' + (mode==='demo'?'Preview changes are saved in this browser. The local runtime uses SQLite.':'Data is stored locally in SQLite. Exports exclude secrets.') + '</p><div class="info-box"><strong>Agent workspace folder</strong><p class="mono">' + esc(data.runtime?.workspace || 'workspaces/company') + '</p></div>' +
    '<div class="row-actions">' + button('View workspace files','workspace-files','','','folder') + button('Export data','export','','','download') + (mode==='demo'?'':button('Restart runtime','restart-runtime','','','refresh')) + '</div><div class="info-box">This local workspace has one owner and AI worker profiles. Multi-user deployment requires additional authentication and access controls.</div>' +
    '<div style="margin-top:12px">' + button('Restore archived records','archives','','quiet small','refresh') + '</div>' +
    '<a class="text-link" href="#connections">Manage connections & credentials ' + icon('arrow') + '</a></section></div>' +
    '<section class="panel settings-panel" style="margin-top:22px"><h2>Workflow</h2><p>Goal → Plan → Delegate → Execute → Cross-review → Approve → Publish.</p><div class="info-box">The team creates agents and skills for each task. Atlas asks for access or decisions when needed. Work resumes from checkpoints; you manage model defaults.</div></section>' +
    '<div style="margin-top:22px">' + panels.view() + '</div>';
}

function field(name,label,value='',type='text',opts={}) {
  const id='f-'+name+(opts.scope?'-'+String(opts.scope).replace(/[^a-zA-Z0-9_-]/g,''):'');
  const common=' id="' + id + '" name="' + name + '" aria-describedby="' + id + '-help ' + id + '-error"' + (opts.required?' data-required="true"':'') + (opts.readonly?' readonly':'');
  let control;
  if(type==='textarea') control='<textarea' + common + ' rows="' + (opts.rows||4) + '" placeholder="' + esc(opts.placeholder||'') + '">' + esc(value) + '</textarea>';
  else if(type==='select') control='<select' + common + '>' + (opts.options||[]).map(o=>{
    const [v,l]=Array.isArray(o)?o:[o,o];
    return '<option value="' + esc(v) + '"' + (String(v)===String(value)?' selected':'') + '>' + esc(l) + '</option>';
  }).join('') + '</select>';
  else control='<input' + common + ' type="' + type + '" value="' + esc(value) + '" placeholder="' + esc(opts.placeholder||'') + '"' + (type==='password'?' autocomplete="new-password"':'') + (opts.min!==undefined?' min="'+opts.min+'"':'') + (opts.max!==undefined?' max="'+opts.max+'"':'') + '>';
  return '<div class="field ' + (opts.wide?'span-2':'') + '"><label for="' + id + '">' + label + (opts.required?' <span class="muted" aria-hidden="true">*</span>':'') + '</label>' + control + '<span class="help" id="' + id + '-help">' + esc(opts.help||'') + '</span><span class="field-error" id="' + id + '-error"></span></div>';
}
const agentOptions=()=>visible('agents').map(a=>[a.id,a.name+' · '+a.role]);
const checkbox=(name,label,checked=false,value='on')=>'<label class="checkbox"><input type="checkbox" name="' + name + '" value="' + esc(value) + '"' + (checked?' checked':'') + '>' + esc(label) + '</label>';
function openModal(title,description,body,footer='',formType='',recordId='') {
  if(modal.open) { modalDirty=false;modal.close(); }
  opener=document.activeElement;
  modal.innerHTML='<div class="modal-head"><div><h2 id="modal-title">' + title + '</h2><p>' + description + '</p></div><button type="button" class="icon-button" data-action="modal-close" aria-label="Close dialog">' + icon('close') + '</button></div>' +
    (formType?'<form id="modal-form" data-type="' + formType + '" data-id="' + esc(recordId) + '" novalidate>':'') +
    '<div class="modal-body">' + body + '<div id="modal-error" class="form-error" role="alert" tabindex="-1"></div></div><div class="modal-footer">' +
    (footer || button('Close','modal-close')) + '</div>' + (formType?'</form>':'');
  modalDirty=false;modal.showModal();
}
function closeModal(force=false) {
  if(modalDirty&&!force) {
    if(!$('.discard-warning',modal)) $('.modal-footer',modal).insertAdjacentHTML('beforebegin','<div class="info-box discard-warning" style="margin:0 25px 15px" role="alert">You have unsaved changes. ' + button('Discard changes','modal-discard','','small') + ' ' + button('Keep editing','modal-continue','','small') + '</div>');
    $('.discard-warning button',modal).focus();return;
  }
  modalDirty=false;modal.close();
  if(opener?.isConnected) opener.focus();
}
const saveFooter=()=>button('Cancel','modal-close')+'<button type="submit" class="button primary">'+icon('check')+'Save changes</button>';
function agentForm(id) {
  const a=id?agent(id):{name:'',role:'',description:'',category:'general',provider:data.settings.default_provider||'codex',model:'',skills:[],tools:['workspace.read','memory.write','team.message'],heartbeat:true,color:'purple'};
  const models=(data.runtime?.models||[]).filter(m=>m.provider===a.provider);
  openModal(id?'Profile '+esc(a.name):'Add an AI teammate','Clear responsibilities, appropriate tools and the right model for the task.',
    (id?'<div class="detail-summary">' + avatar(a) + '<div><h3>' + esc(a.name) + '</h3><p>' + esc(a.role) + '</p></div><span class="grow"></span>' + status(a.status) + '</div>':'') +
    '<div class="field-grid">' + field('name','Agent name',a.name,'text',{required:true}) + field('role','Role',a.role,'text',{required:true}) +
    field('category','Specialty group (custom)',a.category,'text') + field('color','Agent color',a.color,'select',{options:colors}) + '<div class="span-2">'+facePicker(a)+'</div>' +
    field('description','Responsibilities & workflow',a.description,'textarea',{wide:true,rows:3}) +
    field('provider','Provider',a.provider,'select',{options:Object.entries(providerNames)}) +
    field('model','Initial model',a.binding_origin==='default'?'':a.model,'text',{help:'Leave empty to inherit the Atlas chat default. Agents can switch models for their tasks.',placeholder:'Choose from the catalog or enter an exact ID'}) +
    '<div class="span-2">' + field('catalog_model','Choose from the loaded catalog','','select',{options:[['','Choose an observed model…'],...models.map(m=>[m.id,m.name])],help:'A provider catalog does not guarantee account access.'}) + '</div></div>' +
    '<h3 class="form-subheading">Assigned skills</h3><div class="checkbox-group">' + data.skills.filter(s=>s.installed).map(s=>checkbox('skills',s.name,a.skills.includes(s.id),s.id)).join('') + '</div>' +
    '<h3 class="form-subheading">Allowed tools</h3><div class="checkbox-group">' + [['workspace.read','Read workspace files'],['workspace.write','Write workspace files'],['memory.write','Save private memory'],['team.message','Talk to a teammate'],['plan.propose','Propose plan']].concat([['web.fetch','Web research'],['content.draft','Create draft'],['image.generate','Generate image']]).map(([v,l])=>checkbox('tools',l,a.tools.includes(v),v)).join('') + '</div>' +
    checkbox('heartbeat','Allow periodic work through heartbeat',a.heartbeat) +
    (id?'<div class="info-box">' + visible('memories').filter(m=>m.agent_id===id).length + ' private memories · ' + visible('tasks').filter(t=>t.agent_id===id).length + ' tasks. Agents create and refine teammates for the goal.</div>':''),
    (id?button(a.status==='paused'?'Activate agent':'Pause agent','agent-toggle',id,'quiet'):'')+saveFooter(),'agents',id||'');
}
function taskForm(id) {
  const t=id?data.tasks.find(t=>t.id===id):{title:'',description:'',agent_id:'a-lead',reviewer_id:'a-review',priority:'medium',category:'marketing',due:'',status:'backlog',auto_run:false};
  openModal(id?'Edit task':'Assign a new task','Add clear criteria for the owner and reviewer to evaluate.',
    field('title','Task name',t.title,'text',{required:true}) + field('description','Description & completion criteria',t.description,'textarea',{required:true,rows:5}) +
    '<div class="field-grid">' + field('agent_id','Owner',t.agent_id,'select',{options:agentOptions()}) + field('reviewer_id','Reviewer',t.reviewer_id,'select',{options:agentOptions()}) +
    field('category','Task group (custom)',t.category,'text') + field('priority','Priority',t.priority,'select',{options:[['low','Low'],['medium','Medium'],['high','High']]}) +
    field('due','Due date',t.due,'date') + field('status','Status',t.status,'select',{options:(id&&!['backlog','ready','failed'].includes(t.status)?[[t.status,states[t.status]]]:[['backlog','Backlog'],['ready','Ready']])}) + '</div>' +
    checkbox('auto_run','Run automatically when ready and AI execution is enabled',t.auto_run) + '<div class="info-box">Dependencies must be completed before this task can run.</div>',
    saveFooter(),'tasks',id||'');
}
function taskDetail(id) {
  const t=data.tasks.find(t=>t.id===id);
  const a=agent(t.agent_id),r=agent(t.reviewer_id);
  let actions=button('Close','modal-close');
  if(['backlog','ready','failed'].includes(t.status)) actions+=button('Edit','task-edit',id)+(t.status==='backlog'?button('Mark ready','task-ready',id,'primary','check'):button('Run task','task-run',id,'primary','play'));
  if(t.status==='running'&&t.run_id) actions+=button('Stop execution','run-cancel',t.run_id,'danger','pause');
  if(t.status==='review') actions=button('Request changes','task-revise',id)+button('Request agent review','task-review',id)+button('Approve result','task-approve',id,'primary','check');
  openModal(esc(t.title),'Task details and review results.',
    '<div class="detail-meta">' + status(t.status)+priority(t.priority)+'<span class="status">'+icon('calendar')+date(t.due)+'</span></div>' +
    '<div class="row-actions"><span class="assignee">'+avatar(a,'mini')+esc(a.name)+'</span><span class="muted">→</span><span class="assignee">'+avatar(r,'mini')+esc(r.name)+' · reviewer</span></div>'+
    '<div class="result-box">'+esc(t.description)+'</div>'+
    (t.error?'<div class="form-error">'+esc(t.error)+'</div>':'')+
    (t.result?'<h3 class="form-subheading">Work results</h3><div class="result-box">'+esc(t.result)+'</div>':'')+
    (t.review_feedback?'<h3 class="form-subheading">Independent review</h3><div class="result-box">'+esc(t.review_feedback)+'</div>':'')+
    (t.status==='review'?'<div class="field-grid">'+field('score','Quality score','4','select',{options:[['1','1 / 5'],['2','2 / 5'],['3','3 / 5'],['4','4 / 5'],['5','5 / 5']]})+field('note','Review notes','','textarea',{rows:2})+'</div>':'')+
    (t.score?'<div class="info-box">Score: '+t.score+'/5 · '+esc(t.review_note||'Approved by the workspace owner.')+'</div>':''),actions);
}
function memoryForm(id) {
  const m=id?data.memories.find(m=>m.id===id):{title:'',content:'',scope:'shared',agent_id:'a-lead'};
  openModal(id?'Edit knowledge':'Add team knowledge','Specific context helps agents make informed decisions.',
    field('title','Title',m.title,'text',{required:true}) + field('content','Content',m.content,'textarea',{required:true,rows:8}) +
    '<div class="field-grid">'+field('scope','Scope',m.scope,'select',{options:[['shared','Shared knowledge · Team'],['private','Private memory · One agent']]})+
    field('agent_id','Owner agent',m.agent_id||'a-lead','select',{options:agentOptions(),help:'Applies to private memory only.'})+'</div>',
    (id?button('Archive record','archive-memory',id,'danger'):'')+saveFooter(),'memories',id||'');
}
function scheduleForm(id) {
  const s=id?data.schedules.find(s=>s.id===id):{name:'',agent_id:'a-seo',prompt:'',mode:'daily',time:'08:30',interval_minutes:60,enabled:false};
  openModal(id?'Edit schedule':'Schedule recurring work','Schedules run while the local runtime is active and use Vietnam time.',
    field('name','Schedule name',s.name,'text',{required:true})+field('agent_id','Assigned agent',s.agent_id,'select',{options:agentOptions()})+
    field('prompt','Work to complete',s.prompt,'textarea',{required:true,rows:5})+
    '<div class="field-grid">'+field('mode','Schedule type',s.mode,'select',{options:[['daily','Daily at a fixed time'],['interval','Repeat at an interval']]})+
    field('time','Daily run time',s.time,'time',{help:'GMT+7 · Applies to daily schedules'})+
    field('interval_minutes','Repeat interval (minutes)',s.interval_minutes,'number',{min:5,max:10080,help:'Between 5 minutes and 7 days'})+'</div>'+checkbox('enabled','Enable this schedule',s.enabled),
    (id?button('Archive schedule','archive-schedule',id,'danger'):'')+saveFooter(),'schedules',id||'');
}
function postForm(id) {
  const p=id?data.posts.find(p=>p.id===id):{title:'',body:'',channel:'facebook',agent_id:'a-social',scheduled_at:'',media_type:'none',media_url:'',status:'draft'};
  const scheduled=p.scheduled_at?new Date(new Date(p.scheduled_at).getTime()+7*3600000).toISOString().slice(0,16):'';
  const editable=['draft','approved'].includes(p.status);
  openModal(id?'Content & publishing':'Create content draft','Save drafts, check and approve content before publishing.',
    (id?'<div class="detail-meta">'+status(p.status)+(p.external_id?'<span class="mono">ID: '+esc(p.external_id)+'</span>':'')+'</div>':'')+
    field('title','Internal title',p.title,'text',{required:true,readonly:!editable})+
    field('body','Post content',p.body,'textarea',{required:true,rows:7,readonly:!editable})+
    '<div class="field-grid">'+field('channel','Publishing channel',p.channel,'select',{options:[['facebook','Facebook Page'],['blog','SEO blog · Draft']]})+
    field('agent_id','Assigned agent',p.agent_id,'select',{options:agentOptions()})+
    field('scheduled_at','Publish time (Vietnam time)',scheduled,'datetime-local')+
    field('media_type','Attached media',p.media_type,'select',{options:[['none','No media'],['image','Image'],['video','Video']]})+'</div>'+
    field('media_url','Image / video URL',p.media_url,'url',{help:'Facebook requires a public HTTPS URL. Upload local images to media storage before publishing.'})+
    (p.error?'<div class="form-error">'+esc(p.error)+'</div>':'')+
    '<div class="info-box">Editing returns content to Draft for review. SEO blogs remain drafts until a CMS adapter is connected.</div>',
    (id&&p.status==='draft'?button('Approve draft','post-approve',id):'')+
    (id&&p.status==='approved'&&p.channel==='facebook'?button('Publish to Facebook now','post-publish-confirm',id,'primary','facebook'):'')+
    (editable?saveFooter():button('Close','modal-close')),editable?'posts':'',id||'');
}
function skillForm() {
  openModal('Import a new skill','Compatible with SKILL.md from Claude, Codex and open repositories. Skills are imported as instructions; installation scripts are not executed.',
    field('name','Skill name','','text',{required:true})+
    field('description','Short description','','textarea',{rows:2})+
    '<div class="field-grid">'+field('source','Skill sources','Custom','select',{options:['Custom','Claude','Codex','Open Skills','AgenticSkills']})+
    field('category','Specialty (custom)','custom','text')+'</div>'+
    field('url','GitHub SKILL.md URL','','url',{help:'Paste a GitHub file URL, select a file or paste its contents below.'})+
    '<div class="field"><label for="skill-file">Choose SKILL.md file</label><input id="skill-file" type="file" accept=".md,text/markdown,text/plain" aria-describedby="skill-file-help"><span id="skill-file-help" class="help">Up to 100 KB. Review the source and content before installing.</span></div>'+
    field('content','SKILL.md content','','textarea',{rows:8,placeholder:'# Skill name\n\nInstructions for the agent…'}),
    button('Cancel','modal-close')+'<button type="submit" class="button primary">'+icon('plus')+'Import skill</button>','skill-import');
}
function skillDetail(id) {
  const s=data.skills.find(s=>s.id===id);
  openModal(esc(s.name),esc(s.description),
    '<div class="detail-meta"><span class="tag">'+esc(s.source)+'</span><span class="tag">'+esc(categories[s.category]||'Custom')+'</span></div>'+
    '<div class="result-box">'+esc(s.content)+'</div>'+
    (s.sha256?'<p class="mono muted">SHA-256: '+esc(s.sha256)+'</p>':'')+
    '<div class="info-box">Assign installed skills through an agent profile. Disabling a skill removes its instructions from new runs.</div>',
    button('Close','modal-close')+button(s.installed?'Disable skill':'Install skill','skill-toggle',id,s.installed?'':'primary',s.installed?'pause':'plus'));
}
function connectionForm(id) {
  const s=data.settings;
  const has=key=>data.runtime?.secrets?.[key]?'Saved. Leave empty to keep the existing value.':'Open the local app to configure this connection.';
  const secretField=(key,label)=>field(key,label,'','password',{help:has(key)});
  let body='',footer=saveFooter(), form='connection';
  if(id==='gemini') {
    openModal('Gemini','Sign in with Google for chat models; add an API key for image generation.',apps.geminiBody(),
      button('Close','modal-close')+button('Load model catalog','models-refresh','gemini','primary','refresh'));
    return;
  }
  if(id==='image') {imageSettings.open();return;}
  if(['codex','claude'].includes(id)) {
    const p=data.runtime?.providers?.find(p=>p.id===id);
    body='<div class="info-box">'+icon('code')+(p?.installed?'CLI found. The native CLI manages sign-in; browser cookies are not read.':'Install and sign in to the CLI on the AgentFlow machine. Set its path in .env if it is unavailable in PATH.')+'</div>'+
      '<div class="info-box"><strong>Current account: '+esc(p?.account_label||'Unknown')+'</strong><p>Changing the CLI sign-in applies to new turns. Results from an old account are discarded if the account changes during a turn; work retains its checkpoint.</p></div>'+
      usageDetails(p||{id},esc)+
      '<h3 class="form-subheading">Connect your account</h3><div class="result-box">'+({codex:'codex login\ncodex --version',claude:'claude auth login\nclaude --version'}[id])+'</div>'+
      '<p class="small muted">Choose models in agent profiles. Access and limits depend on the account, subscription and installed CLI.</p>'+
      (id==='codex'?'<div class="info-box">Read available models through Codex app-server model/list. Use them in Connections and the chat model picker.</div>':'<div class="info-box">Discover Claude Code in PATH, local installations and VS Code extensions. Read the account with auth status and models with SDK initialize. Every run uses the current CLI account.</div>');
    footer=button('Close','modal-close')+button('Load model catalog','models-refresh',id,'primary','refresh');
    form='';
  }
  if(id==='gateway') body=field('gateway_url','Gateway URL',s.gateway_url,'url',{required:true,help:'OpenAI-compatible gateway URL, usually ending in /v1.'})+secretField('GATEWAY_API_KEY','Gateway key (optional)')+
    '<div class="info-box">Sign in to providers through your own gateway. AgentFlow calls /models and /chat/completions without browser sign-in or cookie extraction. Save, then refresh gateway models.</div>';
  if(id==='facebook') body=field('page_id','Facebook Page ID',s.page_id,'text',{required:true})+field('graph_version','Graph API version',s.graph_version,'text',{required:true,help:'Choose a version supported by your Meta app.'})+secretField('FACEBOOK_PAGE_TOKEN','Page access token')+
    '<div class="info-box">The Page token and Meta app need appropriate publishing permissions. Approve posts in Content calendar. Uncertain publishing results require reconciliation to prevent duplicates.</div>';
  if(id==='telegram') body=telegramBody({tg:data.runtime?.telegram||{},settings:s,hasToken:!!data.runtime?.secrets?.TELEGRAM_BOT_TOKEN,agentOptions:agentOptions(),esc,secretField,field,checkbox,button});
  if(id==='messenger') body=secretField('FACEBOOK_APP_SECRET','Meta app secret')+secretField('META_VERIFY_TOKEN','Webhook verify token')+
    field('support_agent_id','Customer support agent',s.support_agent_id,'select',{options:agentOptions()})+
    checkbox('messenger_enabled','Automatically reply to customer messages',s.messenger_enabled)+
    '<div class="info-box">Configure your Facebook Page, then register /webhooks/messenger. Webhooks use X-Hub-Signature-256. Support uses shared knowledge and the current message, escalating when context is missing.</div>';
  if(id==='seo') body=secretField('SEO_WEBHOOK_SECRET','Bearer secret cho webhook SEO')+
    '<div class="info-box">POST /webhooks/seo with Authorization: Bearer SECRET. Each request needs a unique event_id and prompt. The assigned agent stores drafts and proposes work using its allowed tools.</div><div class="result-box">'+esc(JSON.stringify({event_id:'seo-2026-10-19',prompt:'Propose an SEO brief for today using our brand knowledge.'},null,2))+'</div>';
  openModal(esc(providerNames[id]||({facebook:'Facebook Page',telegram:'Telegram',messenger:'Messenger',image:'Image Studio',seo:'Webhook SEO'}[id])),'Set up workspace connections.',body,footer,form,id);
}
function planForm() {
  openModal('Give the team a goal','Atlas will plan, delegate, create teammates and ask questions as needed.',
    field('objective','Goal & resources','','textarea',{required:true,rows:7,placeholder:'Example: Plan two weeks of content for a SaaS product. Focus on SEO and Facebook to generate qualified leads. Resources: 3 blog posts per week…'})+
    '<div class="info-box">Results, files and decisions will appear in this conversation.</div>',
    button('Cancel','modal-close')+'<button type="submit" class="button primary">'+icon('spark')+'Send brief to Atlas</button>','plan-brief');
}
function confirmAction(title,description,action,id,text) {
  openModal(title,description,'<div class="info-box">'+esc(description)+'</div>',button('Cancel','modal-close')+button(text,action,id,'primary'));
  $('.modal-footer button',modal)?.focus();
}
function validateForm(form) {
  let valid=true, first;
  $$('[data-required]',form).forEach(input=>{
    const invalid=!input.value.trim();
    input.setAttribute('aria-invalid',String(invalid));
    const msg=$('#'+input.id+'-error');
    if(msg) msg.textContent=invalid?'Please fill out this field.':'';
    if(invalid) {valid=false;first ||= input;}
  });
  if(first) first.focus();
  return valid;
}
async function saveForm(form) {
  if(!validateForm(form)) return;
  const values=new FormData(form), type=form.dataset.type, id=form.dataset.id;
  let body=Object.fromEntries(values),reopenTelegram=false;
  const submit=$('button[type=submit]',form);
  if(submit?.disabled) return;
  if(submit) {submit.disabled=true;submit.setAttribute('aria-busy','true');}
  const errorBox=$('#modal-error',form)||$('#settings-error',form);
  if(errorBox) errorBox.textContent='';
  try {
    if(form.id==='settings-form') await api('/settings',body);
    else if(type==='agents') {
      body.skills=values.getAll('skills');body.tools=values.getAll('tools');body.heartbeat=values.has('heartbeat');
      const exposed=['workspace.read','workspace.write','memory.write','team.message','plan.propose','web.fetch','content.draft','image.generate'];
      if(id) body.tools=[...new Set([...body.tools,...agent(id).tools.filter(t=>!exposed.includes(t))])];
      if(id) body.version=agent(id).version;
      await api('/agents'+(id?'/'+id:''),body,id?'PATCH':'POST');
    } else if(['tasks','memories','schedules','posts'].includes(type)) {
      if(id) body.version=data[type].find(x=>x.id===id).version;
      if(type==='tasks') {
        body.auto_run=values.has('auto_run');
        if(body.agent_id===body.reviewer_id) throw new Error('The reviewer must be different from the task owner.');
      }
      if(type==='memories') {body.type='memory';if(body.scope==='shared') body.agent_id=null;}
      if(type==='schedules') {
        body.enabled=values.has('enabled');body.interval_minutes=Number(body.interval_minutes);
        if(body.mode==='interval'&&(body.interval_minutes<5||body.interval_minutes>10080)) throw new Error('The interval must be between 5 minutes and 7 days.');
      }
      if(type==='posts') {
        body.scheduled_at=body.scheduled_at?new Date(body.scheduled_at+':00+07:00').toISOString():'';
        if(body.media_url&&!body.media_url.startsWith('https://')) throw new Error('Media requires a public HTTPS URL.');
      }
      await api('/'+type+(id?'/'+id:''),body,id?'PATCH':'POST');
    } else if(type==='skill-import') {
      if(!body.content?.trim()&&!body.url?.trim()) throw new Error('Paste content, select a file or enter a SKILL.md URL.');
      delete body[''];
      await api('/skills/import',body);
    } else if(type==='connection') {
      if(mode==='demo') throw new Error('Configure accounts in the local app. The preview does not store secrets.');
      const secretKeys=['GATEWAY_API_KEY','FACEBOOK_PAGE_TOKEN','FACEBOOK_APP_SECRET','META_VERIFY_TOKEN','TELEGRAM_BOT_TOKEN','TELEGRAM_WEBHOOK_SECRET','SEO_WEBHOOK_SECRET'];
      const secrets={},settings={};
      for(const [key,value] of Object.entries(body)) {
        if(secretKeys.includes(key)) {if(value) secrets[key]=value;}
        else settings[key]=value;
      }
      if(id==='telegram') {reopenTelegram=true;for(const key of Object.keys(settings)) delete settings[key];Object.assign(settings,telegramSettings(body,values,data.runtime?.telegram));}
      if(id==='messenger') settings.messenger_enabled=values.has('messenger_enabled');
      // Save settings first: invalid settings must not partially save credentials.
      await api('/settings',settings);
      if(Object.keys(secrets).length) {
        const saved=await api('/secrets',secrets);
        if(saved.notice) {
          await refresh(false);modalDirty=false;
          openModal('Connection saved for this runtime session','Windows credential storage is unavailable in this environment.','<div class="info-box">'+esc(saved.notice)+'</div>');
          return;
        }
      }
    } else if(type==='plan-brief') {
      await api('/chat',{agent_id:'a-lead',channel:'dm-a-lead',message:body.objective});
      chatRoom='dm-a-lead';chatAgent='a-lead';location.hash='chat';
    }
    modalDirty=false;if(modal.open) closeModal(true);
    await refresh(false);shell();
    if(reopenTelegram) {toast('Telegram: '+telegramLabel(data.runtime?.telegram,!!data.runtime?.secrets?.TELEGRAM_BOT_TOKEN).text);connectionForm('telegram');}
    else toast('Changes saved.');
  } catch(error) {
    if(errorBox) {errorBox.textContent=error.message;errorBox.setAttribute('tabindex','-1');errorBox.focus();}
    else toast(error.message,true);
  } finally {
    if(submit?.isConnected) {submit.disabled=false;submit.removeAttribute('aria-busy');}
  }
}
async function waitForRuntime() {
  const pause=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const started=Date.now();
  let wasDown=false;
  await pause(3000);
  while(Date.now()-started<120000) {
    try {
      const response=await fetch('/api/health',{signal:AbortSignal.timeout(3000)});
      if(response.ok&&(wasDown||Date.now()-started>30000)) {location.reload();return;}
    } catch {wasDown=true;}
    await pause(2000);
  }
  toast('The runtime did not come back. Start it with Start-AgentFlow.ps1.',true);
}
async function perform(action,id,target) {
  const fail=error=>{
    if(modal.open&&$('#modal-error')) {$('#modal-error').textContent=error.message;$('#modal-error').focus();}
    else {appError=error.message;renderMain();toast(error.message,true);}
  };
  const update=async(kind,id,changes)=>{
    const doc=data[kind].find(x=>x.id===id);
    await api('/'+kind+'/'+id,{...changes,version:doc.version},'PATCH');
    await refresh(false);if(modal.open) closeModal(true);shell();
  };
  try {
    if(await co.perform(action,id,target)) return;
    if(await apps.perform(action,id,target)) return;
    if(await panels.perform(action,id,target)) return;
    if(await hb.perform(action,id,target)) return;
    switch(action) {
      case 'mobile-menu': {
        const nav=$('.sidebar');nav.classList.toggle('open');nav.inert=!nav.classList.contains('open');
        target.setAttribute('aria-expanded',String(!nav.inert));
        if(!nav.inert) $('.nav-item',nav)?.focus();
        break;
      }
      case 'modal-close': closeModal();break;
      case 'modal-discard': closeModal(true);break;
      case 'modal-continue': $('.discard-warning',modal)?.remove();$('input,textarea,select',modal)?.focus();break;
      case 'modal-go-agents': closeModal(true);location.hash='agents';break;
      case 'refresh': await refresh();break;
      case 'workspace': location.hash='settings';break;
      case 'filter': filter=id;page=1;syncQuery();renderMain({anchor:true});break;
      case 'clear-search': search='';filter='all';page=1;syncQuery();$('#global-search').value='';renderMain({anchor:true});break;
      case 'page-prev': page=Math.max(1,page-1);renderMain({anchor:true});break;
      case 'page-next': page=Math.min(Number(id),page+1);renderMain({anchor:true});break;
      case 'task-view': taskView=id;page=1;renderMain({anchor:true});break;
      case 'skills-tab': skillsTab=id;page=1;renderMain({anchor:true});break;
      case 'knowledge-scope': knowledgeScope=id;page=1;renderMain({anchor:true});break;
      case 'agent-new': agentForm();break;
      case 'agent-edit': case 'agent-menu': agentForm(id);break;
      case 'agent-toggle': await update('agents',id,{status:agent(id).status==='paused'?'idle':'paused'});toast('Agent status updated.');break;
      case 'task-new': taskForm();break;
      case 'task-edit': taskForm(id);break;
      case 'task-detail': taskDetail(id);break;
      case 'task-ready': await update('tasks',id,{status:'ready'});toast('Task is ready.');break;
      case 'memory-new': memoryForm();break;
      case 'memory-edit': memoryForm(id);break;
      case 'schedule-new': scheduleForm();break;
      case 'schedule-edit': scheduleForm(id);break;
      case 'schedule-toggle': {
        const s=data.schedules.find(s=>s.id===id);
        if(mode==='demo'&&!s.enabled) throw new Error('Enable scheduling in the local app. You can edit sample schedules in the preview.');
        await update('schedules',id,{enabled:!s.enabled});toast('Schedule updated.');break;
      }
      case 'post-new': postForm();break;
      case 'post-edit': postForm(id);break;
      case 'skill-new': skillForm();break;
      case 'skill-detail': skillDetail(id);break;
      case 'skill-toggle': {
        const s=data.skills.find(s=>s.id===id);
        await update('skills',id,{installed:!s.installed});toast(s.installed?'Skill disabled.':'Skill installed.');break;
      }
      case 'skill-scan': {
        target.disabled=true;
        const result=await api('/skills/scan',{});await refresh(false);renderMain();toast('Imported '+result.imported+' new skills.');break;
      }
      case 'chat-agent': chatRoom='dm-'+id;chatAgent=id;location.hash='chat';if(route==='chat') renderMain();break;
      case 'chat-room': chatRoom=id;appError='';renderMain();break;
      case 'suggestion': $('#chat-input').value=id;sessionStorage.setItem('agentflow-chat-draft-'+chatRoom,id);co.resizeComposer();$('#chat-input').focus();break;
      case 'plan-new': planForm();break;
      case 'image-new': openStudio();break;
      case 'image-studio-open': closeModal(true);openStudio();break;
      case 'connection-edit': connectionForm(id);break;
      case 'models-refresh': {
        target.disabled=true;target.setAttribute('aria-busy','true');
        await api('/models/refresh',{provider:id});await refresh(false);
        if(modal.open) closeModal(true);shell();toast('Model catalog loaded from '+providerNames[id]+'.');break;
      }
      case 'execution-toggle':
        if(!data.settings.execution_enabled) confirmAction('Enable AI execution?','Enabled schedules and automatic tasks may start using the selected account and model. Keep the runtime running.','execution-enable','','Enable execution');
        else {await api('/settings',{execution_enabled:false});await refresh(false);shell();toast('New work is paused. Stop active runs in Automations if needed.');}
        break;
      case 'telegram-test': target.disabled=true;await api('/telegram/test',{});target.disabled=false;toast('Greeting queued. Check your Telegram chat.');break;
      case 'telegram-token-clear': {
        target.disabled=true;
        const cleared=await api('/telegram/token-clear',{});
        await refresh(false);shell();connectionForm('telegram');
        toast(cleared.from_environment?'Saved token removed. A token from .env is still in use.':'Bot token removed. Telegram is off.');break;
      }
      case 'telegram-unlink': confirmAction('Unlink this Telegram chat?','Messages from the chat will be ignored until you link a chat again. The bot token stays saved.','telegram-unlink-confirm','','Unlink chat');break;
      case 'telegram-unlink-confirm': await api('/telegram/unlink',{});closeModal(true);await refresh(false);shell();toast('Telegram chat unlinked.');break;
      case 'restart-runtime':
        if(mode==='demo') throw new Error('Restart is available in the local app.');
        confirmAction('Restart the runtime?','AgentFlow restarts in a few seconds and running work resumes from its checkpoints. This page reconnects by itself. Telegram is reconnected with the saved bot token.','restart-confirm','','Restart runtime');break;
      case 'restart-confirm':
        await api('/admin/restart',{});
        openModal('Restarting AgentFlow','Waiting for the runtime to come back.','<div class="info-box">This page reloads when AgentFlow is running again. If it does not, start it with Start-AgentFlow.ps1.</div>','');
        waitForRuntime();break;
      case 'execution-enable': await api('/settings',{execution_enabled:true});closeModal(true);await refresh(false);shell();toast('AI execution enabled.');break;
      case 'task-run': case 'task-review': case 'task-approve': case 'task-revise': {
        target.disabled=true;
        await api('/tasks/'+id+'/'+({'task-run':'run','task-review':'review','task-approve':'approve','task-revise':'revise'}[action]),
          {score:Number($('#f-score')?.value||4),note:$('#f-note')?.value||''});
        if(modal.open) closeModal(true);await refresh(false);shell();toast(action==='task-approve'?'Result approved.':action==='task-revise'?'Changes requested.':'Work queued.');break;
      }
      case 'post-approve':
        if(modalDirty) throw new Error('Save the draft before approval to use the latest content.');
        await api('/posts/'+id+'/approve',{});closeModal(true);await refresh(false);renderMain();toast('Draft approved.');break;
      case 'post-publish-confirm':
        if(modalDirty) throw new Error('Save and approve the draft again before publishing.');
        confirmAction('Publish to Facebook Page?','Approved content will be published publicly to the configured Facebook Page immediately.','post-publish',id,'Publish to Facebook');break;
      case 'post-publish': await api('/posts/'+id+'/publish',{});closeModal(true);await refresh(false);renderMain();toast('Publish request processed. Check content status to reconcile the result.');break;
      case 'plan-approve': confirmAction('Approve plan?','The planned tasks and subagents will be created. New tasks will be ready but will not start automatically.','plan-approve-confirm',id,'Approve plan');break;
      case 'plan-approve-confirm': await api('/plans/'+id+'/approve',{});closeModal(true);await refresh(false);shell();toast('Tasks and subagents created from the plan.');break;
      case 'plan-reject': await api('/plans/'+id+'/reject',{});await refresh(false);renderMain();toast('Plan rejected.');break;
      case 'run-cancel': await api('/runs/'+id+'/cancel',{});if(modal.open) closeModal(true);await refresh(false);renderMain();toast('Run cancellation requested.');break;
      case 'run-detail': {
        const r=data.runs.find(r=>r.id===id);
        openModal('Run by '+esc(agent(r.agent_id).name),date(r.created_at,{day:'numeric',month:'numeric',hour:'2-digit',minute:'2-digit'})+' GMT+7',
          '<div class="detail-meta">'+status(r.status)+'<span class="mono">'+esc(r.model||agent(r.agent_id).model||'No model selected')+'</span></div><h3 class="form-subheading">Request</h3><div class="result-box">'+esc(r.prompt)+'</div><h3 class="form-subheading">Results</h3><div class="result-box">'+esc(r.result||r.error||'No results yet.')+'</div>');break;
      }
      case 'archive-memory': case 'archive-schedule': confirmAction('Archive record?','This record will no longer be used in new runs. Its data will be retained.','archive-confirm',action==='archive-memory'?'memories:'+id:'schedules:'+id,'Archive');break;
      case 'archive-confirm': {const [kind,recordId]=id.split(':');await update(kind,recordId,{archived:true});toast('Record archived.');break;}
      case 'archives': {
        const items=['memories','schedules','tasks','agents'].flatMap(kind=>data[kind].filter(x=>x.archived).map(x=>({kind,...x})));
        openModal('Archived records','Restore a record to use it in the workspace again.',
          items.length?items.map(x=>'<div class="run-row"><span class="grow">'+esc(x.title||x.name)+'</span>'+button('Restore','archive-restore',x.kind+':'+x.id,'small')+'</div>').join(''):empty('No archived records','Your archived records will appear here.'));
        break;
      }
      case 'archive-restore': {const [kind,recordId]=id.split(':');await update(kind,recordId,{archived:false});toast('Record restored.');break;}
      case 'week-prev': weekOffset--;renderMain();break;
      case 'week-next': weekOffset++;renderMain();break;
      case 'week-today': weekOffset=0;renderMain();break;
      case 'activity': openModal('Workspace activity','Changes and results to follow up on.',activities(50));break;
      case 'help': openModal('Get started with AgentFlow','One shared workspace for your marketing team.',
        '<h3 class="form-subheading">1. Connect your account</h3><p class="small muted">Open the local app, connect a signed-in CLI or gateway, load available models and select models for your agents.</p><h3 class="form-subheading" style="margin-top:20px">2. Share knowledge</h3><p class="small muted">Add product details, audiences, brand voice and procedures to shared knowledge.</p><h3 class="form-subheading" style="margin-top:20px">3. Assign work and review results</h3><p class="small muted">Create tasks or ask Atlas to plan. Review the plan, run tasks, cross-review results and approve content before publishing.</p><div class="info-box">Ctrl/Cmd + K opens search. Enter sends, Shift + Enter adds a line. Escape closes dialogs.</div>',
        button('Close','modal-close')+'<a class="button primary" href="#connections" data-action="modal-close">Open connection</a>');break;
      case 'workspace-files': {
        if(mode==='demo') {openModal('Workspace local','Workspace folder for agent tools.','<div class="info-box"><span class="mono">workspaces/company/</span><p>Open the local app to view actual files. Add your business documents to the workspace.</p></div>');break;}
        const result=await api('/workspace',undefined,'GET');
        openModal('Workspace files','Only files in the company workspace are available to agent tools.',
          result.files.length?'<div class="result-box">'+result.files.map(f=>esc(f.path)+' · '+f.bytes+' bytes').join('\n')+'</div>':empty('No files','Add brief.md and documents to workspaces/company.'));break;
      }
      case 'export': {
        // The page only holds a slim copy of the state; the full workspace comes from /api/export.
        let snapshot;
        if(mode==='local') {
          const response=await fetch('/api/export',{signal:AbortSignal.timeout(120000)});
          if(!response.ok) throw new Error('Unable to export the workspace.');
          snapshot=await response.json();
        } else snapshot=structuredClone(data);
        delete snapshot.runtime;delete snapshot.rev;delete snapshot.meta;
        const blob=new Blob([JSON.stringify(snapshot,null,2)],{type:'application/json'});
        const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='agentflow-workspace-'+new Date().toISOString().slice(0,10)+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast('Data exported without secrets.');break;
      }
    }
  } catch(error) {fail(error);}
  finally {if(target?.isConnected) {target.disabled=false;target.removeAttribute('aria-busy');}}
}
document.addEventListener('click',event=>{
  if(event.target.closest('.skip-link')) {event.preventDefault();$('#main')?.focus();return;}
  const target=event.target.closest('[data-action]');
  if(!target) return;
  if(target.disabled) return;
  if(target.tagName==='A'&&target.dataset.action==='agent-edit') event.preventDefault();
  perform(target.dataset.action,target.dataset.id||'',target);
});
document.addEventListener('submit',async event=>{
  event.preventDefault();
  const form=event.target;
  if(await co.submit(form)) return;
  if(await apps.submit(form)) return;
  if(await panels.submit(form)) return;
  if(await hb.submit(form)) return;
  if(await imageSettings.submit(form)) return;
  if(form.id==='chat-form') {
    const input=$('#chat-input'),submit=$('button[type=submit]',form);
    if(submit.disabled||(!input.value.trim()&&!co.hasAttachments())) return;
    $('#chat-error').textContent='';submit.disabled=true;
    try {
      const sentRoom=chatRoom;
      await api('/chat',{message:input.value.trim(),agent_id:new FormData(form).get('agent_id'),channel:sentRoom,project_id:co.projectId(),attachment_ids:co.attachmentIds()});
      co.clearAttachments(sentRoom);sessionStorage.removeItem('agentflow-chat-draft-'+sentRoom);input.value='';
      await refresh(false);co.update();$('#chat-input').focus();
    } catch(error) {$('#chat-error').textContent=error.message;input.setAttribute('aria-invalid','true');}
    finally {if(submit.isConnected) submit.disabled=false;co.resizeComposer();}
  } else if(form.id==='modal-form'||form.id==='settings-form') saveForm(form);
});
document.addEventListener('input',event=>{
  const input=event.target;
  if(input.closest('#modal-form')) modalDirty=true;
  if(input.id==='global-search') {
    clearTimeout(searchTimer);
    if(event.isComposing) return;
    searchTimer=setTimeout(()=>{search=input.value;page=1;syncQuery();renderMain();},300);
  }
  if(input.id==='chat-input') sessionStorage.setItem('agentflow-chat-draft-'+chatRoom,input.value);
});
document.addEventListener('compositionend',event=>{
  if(event.target.id==='global-search') {search=event.target.value;page=1;syncQuery();renderMain();}
});
document.addEventListener('change',async event=>{
  const input=event.target;
  if(input.id==='chat-attachments') {await co.addFiles(input.files);input.value='';return;}
  if(input.closest('#modal-form')) modalDirty=true;
  hb.changed(input);
  if(input.id==='memory-owner') {knowledgeScope=input.value;page=1;renderMain({anchor:true});}
  if(input.name==='catalog_model'&&input.value) $('#f-model').value=input.value;
  if(input.name==='provider') {
    const modelSelect=$('#f-catalog_model');
    if(modelSelect) {
      const list=(data.runtime?.models||[]).filter(m=>m.provider===input.value);
      modelSelect.innerHTML='<option value="">Choose an observed model…</option>'+list.map(m=>'<option value="'+esc(m.id)+'">'+esc(m.name)+'</option>').join('');
      $('#f-model').value='';
    }
  }
  if(input.id==='skill-file'&&input.files[0]) {
    const file=input.files[0];
    if(file.size>100000) {$('#modal-error').textContent='Skill exceeds the 100 KB limit.';input.value='';return;}
    $('#f-content').value=await file.text();
    if(!$('#f-name').value) $('#f-name').value=file.name.replace(/\.md$/i,'');
  }
});
document.addEventListener('keydown',event=>{
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k') {event.preventDefault();$('#global-search')?.focus();}
  if(event.target.id==='chat-input'&&event.key==='Enter'&&!event.shiftKey&&!event.isComposing) {event.preventDefault();$('#chat-form').requestSubmit();}
});
modal.addEventListener('cancel',event=>{event.preventDefault();closeModal();});
modal.addEventListener('close',()=>{modalDirty=false;});
window.addEventListener('hashchange',()=>{
  if(!data) return; // still booting: boot() reads the hash itself
  route=location.hash.slice(1).split('/')[0]||'chat';page=1;filter='all';syncQuery();shell();
  window.scrollTo({top:0,behavior:'instant'});$('#main').focus({preventScroll:true});
});
matchMedia('(max-width:760px)').addEventListener('change',event=>{
  const nav=$('.sidebar');if(nav) nav.inert=event.matches&&!nav.classList.contains('open');
});
const CACHE_KEY='state:'+location.origin;
const routeOfHash=()=>location.hash.slice(1).split('/')[0]||'chat';
/** Repaint after a state response: chat updates in place (draft and scroll stay), other pages morph. */
function paint(result,{poll=false}={}) {
  if(!result||!result.ok) {if(result) renderMain();return;}
  if(route==='chat') co.update(poll?{poll:true,stamp:stateStamp}:undefined);
  else if(!modal.open&&!document.activeElement?.closest('form')&&(result.full||result.runtimeChanged||(result.kinds||[]).some(k=>REFRESH_KINDS.has(k)))) renderMain();
}
async function boot() {
  try {
    // A copy of the last state paints the workspace at once; the network then only has to send what changed since.
    const cached=await Promise.race([loadCachedState(CACHE_KEY),new Promise(resolve=>setTimeout(resolve,400,null))]);
    if(cached?.rev&&cached.agents&&cached.settings) {
      data=withDefaults(cached);data.cached=true;mode='local';route=routeOfHash();
      refreshCategories();shell();
      paint(await refresh(false));
      return;
    }
    const response=await fetch('/api/state',{signal:AbortSignal.timeout(10000)});
    const json=response.headers.get('content-type')?.includes('application/json');
    if(response.ok&&json) {data=withDefaults(await response.json());mode='local';stateStamp=data.rev+'|0';cacheDirty=true;}
    else if(response.status===404||response.status===405||(response.ok&&!json)) {
      const seedResponse=await fetch('/seed.json');
      if(!seedResponse.ok) throw new Error('Unable to load initial data.');
      const seed=await seedResponse.json();
      try {const saved=JSON.parse(localStorage.getItem('agentflow-preview-v1'));data=saved?.agents&&saved?.settings?saved:seed;}
      catch {data=seed;}
      for(const kind of ['agents','tasks','messages','memories','schedules','posts','skills','plans','runs','events'])
        data[kind].forEach(item=>item.version ||= 1);
      mode='demo';data.runtime={mode:'demo',providers:[],models:[],secrets:{}};
    } else {
      const error=json?await response.json():{};
      throw new Error(error.error||'Unable to open the runtime. Reload to start a new session.');
    }
    route=routeOfHash();
    withDefaults(data);
    refreshCategories();
    shell();
  } catch(error) {
    $('#app').innerHTML='<div class="boot" role="alert"><h2>Unable to open the workspace</h2><p style="margin:14px 0">'+esc(error.message)+'</p><button class="button primary" id="boot-retry">Retry</button></div>';
    $('#boot-retry').addEventListener('click',boot);
  }
}
// Polling: one timer at a time, paced by activity (fast while work runs or just changed, slow when idle), paused while the tab is hidden.
const hasActiveRuns=()=>(data?.runs||[]).some(r=>r.status==='running'||r.status==='queued');
async function pollCycle() {
  if(mode!=='local'||!data||document.hidden||inflight) return;
  const result=await refresh(false);
  paint(result,{poll:true});
  maybeSaveCache();
}
function schedulePoll() {
  clearTimeout(pollTimer);pollTimer=0;
  const delay=pollDelay({hidden:document.hidden,active:hasActiveRuns(),sinceChange:Date.now()-lastChangeAt,failures:pollFailures});
  if(delay===null) return;
  pollTimer=setTimeout(async()=>{pollTimer=0;try {await pollCycle();} finally {schedulePoll();}},delay);
}
/** The last state is kept in IndexedDB (at most every 30 s while changing, and when the tab is hidden) so a reload can paint at once. */
function maybeSaveCache(force=false) {
  if(mode!=='local'||!data||!cacheDirty||(!force&&Date.now()-cacheSavedAt<30000)) return;
  const copy=cacheable(data);if(!copy) return;
  cacheDirty=false;cacheSavedAt=Date.now();saveCachedState(CACHE_KEY,copy);
}
document.addEventListener('visibilitychange',()=>{
  if(document.hidden) {clearTimeout(pollTimer);pollTimer=0;maybeSaveCache(true);return;}
  lastChangeAt=Date.now();   // coming back: look again right away and keep the faster pace for a while
  clearTimeout(pollTimer);pollTimer=setTimeout(async()=>{pollTimer=0;try {await pollCycle();} finally {schedulePoll();}},0);
});
window.addEventListener('pagehide',()=>maybeSaveCache(true));
window.__afPollNow=async()=>{while(inflight) await inflight.catch(()=>{});return pollCycle();};   // lets a test or profiler run one poll cycle on demand
setInterval(()=>{if(route==='automations'&&!document.hidden) hb.tick();},1000);
boot().then(()=>{maybeSaveCache(true);schedulePoll();});
