// Identity is shared by the roster, profile picker and chat references.
export const esc=(v='')=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export const faces=['smile','wink','happy','curious','calm','star','cat','bear','robot','dream','sunny','cheer'];
export const colors=[['purple','Purple'],['blue','Blue'],['pink','Pink'],['orange','Orange'],['green','Green'],['teal','Teal'],['indigo','Indigo'],['slate','Slate'],['rose','Rose'],['amber','Amber'],['cyan','Cyan'],['mint','Mint']];
const eyes='<circle cx="8" cy="10" r="1.2" fill="currentColor"/><circle cx="16" cy="10" r="1.2" fill="currentColor"/>';
const smiles='<path d="M8 15q4 4 8 0"/>';
const expressions={
  smile:eyes+smiles,
  wink:'<path d="m6 10 3-1-3-1"/><circle cx="16" cy="10" r="1.2" fill="currentColor"/>'+smiles,
  happy:'<path d="M6 10q2-4 4 0m4 0q2-4 4 0M8 14q4 7 8 0Z"/>',
  curious:eyes+'<ellipse cx="12" cy="16" rx="1.6" ry="2"/><path d="m14 6 4 1"/>',
  calm:'<path d="M6 10q2 2 4 0m4 0q2 2 4 0M10 16h4"/>',
  star:'<path d="m8 7 .8 2 2 .2-1.5 1.4.4 2-1.7-1-1.7 1 .4-2L5.2 9.2l2-.2ZM16 7l.8 2 2 .2-1.5 1.4.4 2-1.7-1-1.7 1 .4-2-1.5-1.4 2-.2Z"/>'+smiles,
  cat:'<path d="m5 6 1-3 4 3m4 0 4-3 1 3M5 14l-3-1m3 3-3 1m17-3 3-1m-3 3 3 1M10 15l2 1 2-1m-2 1v2"/>'+eyes,
  bear:'<circle cx="5" cy="5" r="2.5"/><circle cx="19" cy="5" r="2.5"/>'+eyes+'<ellipse cx="12" cy="16" rx="4" ry="3"/><path d="m11 15 1 1 1-1m-1 1v1"/>',
  robot:'<rect x="4" y="5" width="16" height="15" rx="4"/><path d="M12 5V2m-9 8v5m18-5v5M8 16h8"/><path d="M8 10v2m8-2v2"/>',
  dream:'<path d="M6 11q2-3 4 0m4 0q2-3 4 0"/>'+smiles+'<path d="m18 3 1-2 1 2 2 1-2 1-1 2-1-2-2-1Z"/>',
  sunny:eyes+smiles+'<path d="M12 1v2m0 18v2M1 12h2m18 0h2M3 3l2 2m14 14 2 2M3 21l2-2M19 5l2-2"/>',
  cheer:'<path d="m6 8 3 2-3 2m12-4-3 2 3 2M8 15q4 5 8 0"/>'
};
export const faceFor=id=>id==='a-lead'?'smile':faces[[...String(id||'')].reduce((n,c)=>n+c.codePointAt(0),0)%faces.length];
export function agentGroup(a={}) {
  const category=String(a.category||'general').trim().slice(0,80)||'general';
  const label=category.replace(/[_-]+/g,' ').replace(/\b\w+/g,word=>word.length<=3?word.toUpperCase():word[0].toUpperCase()+word.slice(1));
  return '<span class="agent-group" title="Specialty group: '+esc(category)+'">'+esc(label)+'</span>';
}
export function agentAvatar(a={},size='') {
  const face=faces.includes(a.face)?a.face:faceFor(a.id),color=colors.some(c=>c[0]===a.color)?a.color:'purple';
  return '<span class="avatar agent-face '+color+' '+esc(size)+'" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'+expressions[face]+'</svg></span>';
}
export function agentMention(a) {
  if(!a) return '';
  const color=colors.some(c=>c[0]===a.color)?a.color:'purple';
  return '<button type="button" class="agent-mention '+color+'" data-action="co-room" data-id="'+esc(a.id)+'" aria-label="Chat with '+esc(a.name)+'">'+agentAvatar(a,'mini')+'<span>'+esc(a.name)+'</span></button>';
}
export function facePicker(a) {
  return '<fieldset class="face-field"><legend>Agent face</legend><div class="face-options">'+faces.map((face,i)=>'<label class="face-choice" title="Face '+(i+1)+'"><input type="radio" name="face" value="'+face+'" '+((a.face||faceFor(a.id))===face?'checked':'')+' aria-label="Face '+(i+1)+'">'+agentAvatar({...a,face})+'</label>').join('')+'</div></fieldset>';
}
const quoteRx=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
export function formatMessage(value='',agents=[],artifacts=[]) {
  const active=agents.filter(a=>!a.archived&&a.name),alias=a=>a.name.split(/\s[-–—]\s/)[0];
  const aliases=active.map(alias).filter(name=>active.filter(a=>alias(a)===name).length===1);
  const content=String(value),names=[...new Set([...active.map(a=>a.name),...aliases])].sort((a,b)=>b.length-a.length);
  const paths=artifacts.flatMap(a=>[a.path,a.path?.replaceAll('\\','/')]).filter(Boolean).sort((a,b)=>b.length-a.length);
  const tokens=['```[\\s\\S]*?(?:```|$)','`[^`\\n]+`','\\*\\*[^*\\n]+\\*\\*','@\\{[^}\\n]+\\}',...paths.map(quoteRx),...names.map(quoteRx)];
  const regex=new RegExp(tokens.join('|'),'gu');let html='',offset=0;
  for(const match of content.matchAll(regex)) {
    const token=match[0],i=match.index;html+=esc(content.slice(offset,i));offset=i+token.length;
    if(token.startsWith('```')) {html+='<pre class="message-code"><code>'+esc(token.slice(3).replace(/```$/,'').replace(/^\w*\n/,''))+'</code></pre>';continue;}
    if(token.startsWith('**')) {html+='<strong>'+formatMessage(token.slice(2,-2),agents,artifacts)+'</strong>';continue;}
    const inline=token.startsWith('`'),text=inline?token.slice(1,-1):token;
    const file=artifacts.find(a=>[a.path,a.path?.replaceAll('\\','/')].includes(text));
    if(file) {html+='<button type="button" class="artifact-inline" data-action="co-artifact" data-id="'+esc(file.id)+'">'+esc(text)+'</button>';continue;}
    if(inline) {html+='<code>'+esc(text)+'</code>';continue;}
    const structured=/^@\{(.+)\}$/.exec(token),a=structured?agents.find(a=>a.id===structured[1]):active.find(a=>a.name===token||(aliases.includes(token)&&alias(a)===token));
    const boundary=structured||!/[\p{L}\p{N}_]/u.test(content[i-1]||'')&&!/[\p{L}\p{N}_]/u.test(content[offset]||'');
    html+=a&&boundary?agentMention(a):esc(token);
  }
  return html+esc(content.slice(offset));
}
export function ownerVisible(message,runs=[]) {
  if(message.audience) return message.audience==='owner';
  return !['peer','handoff','peer_reply'].includes(message.kind)&&!(runs.find(r=>r.id===message.run_id)?.kind==='peer'&&message.kind!=='decision');
}
