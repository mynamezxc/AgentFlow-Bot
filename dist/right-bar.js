// Pure helpers for the chat's right bar: which project it follows, the goals list and the "For you" outputs split.
const stamp=v=>{const n=Date.parse(v||'');return Number.isFinite(n)?n:0;};
const live=p=>p&&!p.archived&&!p.sample;

/** Projects the room has worked in (or owns), most recent activity first. */
function ranked({projects=[],messages=[],runs=[],channel}) {
  const last=new Map();
  const touch=(id,at)=>{if(id&&at>=(last.get(id)??-1)) last.set(id,at);};
  messages.forEach(m=>{if(m.channel===channel) touch(m.project_id,stamp(m.created_at));});
  runs.forEach(r=>{if(r.channel===channel) touch(r.project_id,Math.max(stamp(r.updated_at),stamp(r.created_at)));});
  const owned=projects.filter(p=>live(p)&&p.channel===channel);
  owned.forEach(p=>{if(!last.has(p.id)) last.set(p.id,stamp(p.created_at)-1);});
  return projects.filter(p=>live(p)&&last.has(p.id)).map(p=>({project:p,at:last.get(p.id)})).sort((a,b)=>b.at-a.at);
}
export const roomProjects=input=>ranked(input).map(x=>x.project);

/**
 * The project the bar follows: the owner's pick for this room ({id,at} or an id) until the room's work moves to another
 * project after that pick; else the project of the most recent activity (latest message or run with a project in this
 * channel), else the channel's own project, else `fallback()`.
 */
export function chooseProject({projects=[],messages=[],runs=[],channel,chosen,fallback}) {
  const options=ranked({projects,messages,runs,channel}),top=options[0];
  const id=chosen&&typeof chosen==='object'?chosen.id:chosen,at=chosen&&typeof chosen==='object'?Number(chosen.at)||0:Infinity;
  const picked=id&&projects.find(p=>p.id===id&&live(p));
  if(picked&&!(top&&top.project.id!==picked.id&&top.at>at)) return picked;
  if(top) return top.project;
  const direct=projects.filter(p=>live(p)&&p.channel===channel).at(-1);
  return direct||(fallback?fallback():undefined);
}

const legacyGoal=p=>({id:'legacy',title:p.title||'',text:p.brief||p.title||'',status:p.status==='done'?'done':'active',legacy:true});
/** project.goals, or the title/brief as the single goal for projects saved before goals existed. */
export function goalsOf(project) {
  if(!project) return [];
  return Array.isArray(project.goals)?project.goals:[legacyGoal(project)];
}
/** Active goals keep their order; done goals follow separately. */
export function orderGoals(goals) {
  const list=Array.isArray(goals)?goals:[];
  return {active:list.filter(g=>g.status!=='done'),done:list.filter(g=>g.status==='done')};
}
const goalRow=(g,esc,done)=>'<li class="goal-row'+(done?' is-done':'')+'"><span class="goal-text" title="'+esc(g.text&&g.text!==g.title?g.text:g.title)+'">'+esc(g.title||g.text)+'</span>'+
  (g.legacy?'':'<button type="button" class="goal-remove" data-action="co-goal-remove" data-id="'+esc(g.id)+'" aria-label="Remove goal: '+esc(g.title||g.text)+'" title="Remove goal">×</button>')+'</li>';
/** GOALS block: project title as the header, optional project switcher, active goals, then "N done" collapsed. */
export function goalsMarkup(project,esc,{projects=[],doneOpen=false}={}) {
  if(!project) return '';
  const {active,done}=orderGoals(goalsOf(project));
  const switcher=projects.length>1?'<label class="sr-only" for="co-project-switch">Project</label><select id="co-project-switch" class="project-switch" data-action="co-project-pick" aria-label="Project shown in this bar">'+
    projects.map(p=>'<option value="'+esc(p.id)+'"'+(p.id===project.id?' selected':'')+'>'+esc(p.title||'Untitled project')+'</option>').join('')+'</select>':'';
  return '<div class="context-title"><small>GOALS</small>'+(switcher||'<h3>'+esc(project.title)+'</h3>')+(switcher?'<h3 class="sr-only">'+esc(project.title)+'</h3>':'')+
    '<ul class="goal-list" aria-label="Goals">'+(active.length?active.map(g=>goalRow(g,esc,false)).join(''):'<li class="goal-empty">No active goals.</li>')+'</ul>'+
    (done.length?'<details class="goal-done" data-section="goaldone"'+(doneOpen?' open':'')+'><summary>'+done.length+' done</summary><ul class="goal-list">'+done.map(g=>goalRow(g,esc,true)).join('')+'</ul></details>':'')+'</div>';
}

/** "For you": files an agent attached or flagged as deliverable (newest first), never pictures (those stay in the image grid). */
export function forYouFiles(artifacts=[],isPicture=()=>false) {
  return artifacts.filter(a=>a&&a.deliverable&&!a.trashed_at&&!isPicture(a)).slice().reverse();
}
/** Change groups left for "Changed files" once the deliverables are listed under "For you". */
export function changedAfterForYou(groups=[],forYou=[]) {
  const ids=new Set(forYou.map(a=>a.id));
  return groups.filter(g=>!ids.has(g.latest?.artifact_id));
}

/** One card for agents.create / agents.update: avatars, one line, an "Open profile" action per subject. */
export function agentEventMarkup(m,h) {
  const {esc,icon,avatar,actor,stamp:time}=h;
  const items=Array.isArray(m.items)&&m.items.length?m.items:[];
  const who=actor(m.agent_id);
  const one=items.length===1;
  const rows=items.map(i=>{
    const a=actor(i.id);
    return '<li class="agent-event-row">'+avatar(a,'mini')+'<span class="agent-event-name"><strong>'+esc(i.name||a.name)+'</strong>'+(i.role?'<small>'+esc(i.role)+(i.model?' · '+esc(i.model):'')+'</small>':'')+'</span>'+
      '<button type="button" class="button quiet small" data-action="agent-edit" data-id="'+esc(i.id)+'">Open profile</button></li>';
  }).join('');
  const head=m.event==='updated'?'updated':'created';
  const summary=one?'<strong>'+esc(who.name)+'</strong> '+head+(m.event==='updated'&&(m.changed||[]).length?' · '+esc(m.changed.join(', ')):''):'<strong>'+esc(who.name)+'</strong> '+head+' '+items.length+' agents';
  return '<article class="agent-event" data-event="'+esc(m.event||'created')+'"><div class="agent-event-head">'+icon(m.event==='updated'?'spark':'team')+'<span>'+summary+'</span><time>'+esc(time(m.created_at))+'</time></div><ul class="agent-event-list">'+(rows||'<li class="agent-event-row"><span class="agent-event-name">'+esc(m.content)+'</span></li>')+'</ul></article>';
}
