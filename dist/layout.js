// Layout helpers shared by the shell: DOM morphing (no full-page flashes), roster grouping,
// folder compaction and file-change grouping. Everything except `morph` and `keepInView` is pure.
const IMAGE_RX=/\.(png|jpe?g|gif|webp|bmp|avif|svg|ico|heic)$/i;
export const isImagePath=path=>IMAGE_RX.test(String(path||'').split(/[?#]/)[0]);
const norm=path=>String(path||'').replace(/\\/g,'/').replace(/\/+$/,'');
const segments=path=>norm(path).split('/').filter(Boolean);
const separatorOf=path=>String(path||'').includes('\\')?'\\':'/';

/** Last two path segments ("tenant\routers"); a one-segment path is shown whole. */
export function lastSegments(path,count=2) {
  const parts=segments(path);
  if(parts.length<=1) return String(path||'');
  return parts.slice(-count).join(separatorOf(path));
}

/** Longest granted folder that contains `path`; its access is the effective one. */
export function accessFor(path,grants=[]) {
  const target=norm(path).toLowerCase();
  let best=null;
  for(const grant of grants||[]) {
    const root=norm(grant?.path).toLowerCase();
    if(!root) continue;
    if(target===root||target.startsWith(root+'/')) if(!best||root.length>norm(best.path).length) best=grant;
  }
  return best?(best.access==='read'?'R':'RW'):'';
}

/**
 * Folders-used list: unique paths, one compact row each (last two segments, full path in `title`),
 * a tiny R / RW access badge, and at most `max` rows until `expanded`.
 */
export function compactFolders(paths,grants=[],{max=4,expanded=false}={}) {
  const seen=new Set(),rows=[];
  for(const raw of paths||[]) {
    const path=String(raw||'').trim(),key=norm(path).toLowerCase();
    if(!path||seen.has(key)) continue;
    seen.add(key);
    rows.push({path,label:lastSegments(path),access:accessFor(path,grants)});
  }
  const shown=expanded?rows:rows.slice(0,max);
  return {rows:shown,total:rows.length,hidden:rows.length-shown.length};
}

/**
 * Changed files: repeated edits of the same file collapse into one row (count, summed +/-, newest
 * revision opens the diff). Image files are left out (they live in the Images & screenshots grid).
 * Rows keep the order of their newest revision.
 */
export function groupChanges(changes,{isImage=()=>false}={}) {
  const groups=new Map();
  for(const change of changes||[]) {
    if(!change||isImagePath(change.path)||isImage(change)) continue;
    const key=norm(change.path).toLowerCase();
    let group=groups.get(key);
    if(!group) {
      const parts=segments(change.path);
      group={key,path:change.path,name:parts.at(-1)||String(change.path||''),dir:parts.length>1?parts.at(-2):'',count:0,added:0,deleted:0,latest:change,agentIds:[],created:false};
      groups.set(key,group);
    }
    group.count++;
    group.added+=Number(change.added)||0;
    group.deleted+=Number(change.deleted)||0;
    if(change.kind==='created') group.created=true;
    if(String(change.created_at||'')>String(group.latest.created_at||'')) group.latest=change;
    if(change.agent_id&&!group.agentIds.includes(change.agent_id)) group.agentIds.push(change.agent_id);
  }
  return [...groups.values()].sort((a,b)=>String(b.latest.created_at||'').localeCompare(String(a.latest.created_at||'')));
}

/** First `limit` rows, or all when expanded. */
export const limitRows=(rows,limit,expanded)=>({rows:expanded?rows:rows.slice(0,limit),hidden:expanded?0:Math.max(0,rows.length-limit)});

/**
 * Roster grouping: collapsible category headers only when there are more than `threshold` agents, and only
 * for categories that hold at least two agents. One-off categories stay in a flat, header-less list (their
 * rows carry the category chip instead), so a team of unique specialists never turns into header-per-row clutter.
 * The flat list (`key:''`) comes first when it holds the lead, otherwise last.
 */
export const GROUP_THRESHOLD=8;
export const categoryLabel=category=>String(category||'general').trim().slice(0,80).replace(/[_-]+/g,' ').replace(/\b\w+/g,word=>word.length<=3?word.toUpperCase():word[0].toUpperCase()+word.slice(1))||'General';
export function rosterGroups(agents,{threshold=GROUP_THRESHOLD,leadId='a-lead'}={}) {
  const list=agents||[];
  if(list.length<=threshold) return [{key:'',label:'',agents:list}];
  const map=new Map();
  for(const agent of list) {
    const key=String(agent.category||'general').trim().toLowerCase()||'general';
    if(!map.has(key)) map.set(key,{key,label:categoryLabel(agent.category),agents:[]});
    map.get(key).agents.push(agent);
  }
  const all=[...map.values()],groups=all.filter(group=>group.agents.length>1),flat=all.filter(group=>group.agents.length<2).flatMap(group=>group.agents);
  if(!groups.length) return [{key:'',label:'',agents:list}];
  if(!flat.length) return groups;
  const flatGroup={key:'',label:'',agents:flat};
  return flat.some(agent=>agent.id===leadId)?[flatGroup,...groups]:[...groups,flatGroup];
}
/** Does a roster entry match the search text? Name, role and category all count. */
export const rosterMatches=(agent,query)=>{
  const text=String(query||'').trim().toLocaleLowerCase('en');
  return !text||[agent.name,agent.role,agent.category].join(' ').toLocaleLowerCase('en').includes(text);
};

/* ---------- DOM ---------- */
const SKIP_ATTR={open:true};
function syncAttributes(old,next) {
  for(const {name,value} of [...next.attributes]) if(!SKIP_ATTR[name]&&old.getAttribute(name)!==value) old.setAttribute(name,value);
  for(const {name} of [...old.attributes]) if(!SKIP_ATTR[name]&&!next.hasAttribute(name)) old.removeAttribute(name);
}
function morphNode(old,next) {
  if(old.nodeType!==next.nodeType||old.nodeName!==next.nodeName) {old.replaceWith(next);return;}
  if(old.nodeType!==1) {if(old.nodeValue!==next.nodeValue) old.nodeValue=next.nodeValue;return;}
  if(old.isEqualNode(next)) return;
  syncAttributes(old,next);
  const tag=old.nodeName;
  if(tag==='SELECT'||tag==='TEXTAREA'||tag==='INPUT') {
    morphChildren(old,next);
    if(document.activeElement!==old&&old.value!==next.value) old.value=next.value;
    if(tag==='INPUT'&&(old.type==='checkbox'||old.type==='radio')) old.checked=next.checked;
    return;
  }
  morphChildren(old,next);
}
function morphChildren(old,next) {
  const from=[...old.childNodes],to=[...next.childNodes];
  to.forEach((node,index)=>{
    if(index<from.length) morphNode(from[index],node);
    else old.append(node);
  });
  for(let index=from.length-1;index>=to.length;index--) from[index].remove();
}
/**
 * Replace the children of `container` with `html` by patching only what changed: unchanged nodes
 * (heading, tab bar, cards) stay, so there is no flash, and focus/scroll inside them survive.
 */
export function morph(container,html) {
  const template=document.createElement('template');
  template.innerHTML=html;
  morphChildren(container,template.content);
}

/**
 * After the list under a tab/filter bar changed: when the bar scrolled out of view, bring it back to
 * the top of the scroller so every tab switch lands in the same place; otherwise leave the scroll alone.
 */
export function keepInView(scroller,anchor,gap=12) {
  if(!scroller||!anchor) return;
  const offset=anchor.getBoundingClientRect().top-scroller.getBoundingClientRect().top;
  if(offset<0) scroller.scrollTop+=offset-gap;
}
