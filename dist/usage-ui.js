const providerName=id=>id==='codex'?'Codex':'Claude Code';
const RESET_TEXT='Reset · refreshing';
const finite=value=>typeof value==='number'&&Number.isFinite(value);
export function quotaWindows(provider){
  return (provider?.usage?.windows||[]).map(w=>({...w,used_percent:finite(w.used_percent)?Math.max(0,Math.min(100,w.used_percent)):null,expired:!!w.expired||!!(finite(w.resets_at)&&w.resets_at*1000<=Date.now())}));
}
function duration(w,short=false){
  const minutes=w.window_minutes||(w.id==='five_hour'?300:w.id?.startsWith('seven_day')?10080:null);
  if(!minutes)return short?'Limit':w.label||w.id||'Quota';
  const value=minutes>=1440?minutes/1440:minutes>=60?minutes/60:minutes,unit=minutes>=1440?'day':minutes>=60?'hour':'minute';
  return short?value+(unit==='day'?'d':unit==='hour'?'h':'m'):value+'-'+unit+' limit';
}
function label(w,provider){
  const bucket=w.bucket||w.id?.split(':')[0],ordinary=provider?.id==='codex'?(provider.usage?.default_bucket||'codex'):null;
  const name=provider?.id==='codex'&&bucket!==ordinary?w.label||bucket:w.id?.includes('opus')?'Opus':w.id?.includes('sonnet')?'Sonnet':'';
  return [name,duration(w)].filter(Boolean).join(' · ');
}
function resetText(w){
  if(!finite(w.resets_at))return 'Reset time unavailable';
  const absolute=new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Ho_Chi_Minh',hour:'2-digit',minute:'2-digit',hour12:false,day:'2-digit',month:'short'}).format(new Date(w.resets_at*1000))+' GMT+7';
  const minutes=Math.ceil((w.resets_at*1000-Date.now())/60000);
  if(minutes<=0)return 'Awaiting refresh · '+absolute;
  const days=Math.floor(minutes/1440),hours=Math.floor(minutes%1440/60),rest=minutes%60;
  return 'Resets in '+(days?days+'d ':'')+(hours?hours+'h ':'')+(!days&&rest?rest+'m':'')+' · '+absolute;
}
/** Plan (Plus, Pro, Max…) as a small floating label in the top corner of a usage card or popover. */
export function planLabel(plan){
  const text=String(plan||'').replace(/[_-]+/g,' ').trim();
  return text?text.replace(/\b\w/g,c=>c.toUpperCase()):'';
}
export function usagePlanBadge(provider,esc){
  const text=planLabel(provider?.usage?.plan_type);
  return text?'<span class="usage-plan" title="Subscription plan">'+esc(text)+'</span>':'';
}
export function usageDetails(provider,esc){
  const u=provider?.usage||{},windows=quotaWindows(provider).sort((a,b)=>(windowMinutes(a)||0)-(windowMinutes(b)||0));
  const rows=windows.map(w=>{
    // An expired window has reset: show it as full ("Reset · refreshing") until a fresh observation arrives, never as a stale measurement or a blank.
    const known=w.used_percent!=null&&!w.expired,left=w.expired?100:known?100-w.used_percent:null;
    return '<div class="usage-window" data-window-id="'+esc(w.id)+'"><div><strong>'+esc(label(w,provider))+'</strong><span>'+esc(w.expired?RESET_TEXT:left==null?'Unavailable':Math.round(left)+'% left')+'</span></div>'+(left!=null?'<progress max="100" value="'+left+'" aria-label="'+esc(label(w,provider))+' remaining"></progress>':'')+(known?'<small>'+esc(Math.round(w.used_percent)+'% used'+(u.stale||isOld(w)?' · Last confirmed':''))+'</small>':w.expired||w.used_percent!=null?'':w.status?'<small>'+esc('Status: '+String(w.status).replace(/_/g,' ')+' (no percentage reported yet)')+'</small>':'')+'<small>'+esc(resetText(w))+'</small></div>';
  }).join('');
  // Only meaningful lines: the windows, a real error/checking state and "Updated HH:MM". The source is a tooltip, never text.
  const note=u.state==='pending'?'':u.notice||'';
  // Rate limiting is not an error: no warning text, only the next check time and a tooltip.
  const tip=[u.source?'Source: '+u.source:'',u.rate_limited&&u.rate_notice?u.rate_notice:''].filter(Boolean).join(' · ');
  const hhmm=value=>new Intl.DateTimeFormat('en-US',{timeZone:'Asia/Ho_Chi_Minh',hour:'2-digit',minute:'2-digit',hour12:false}).format(value);
  const next=u.rate_limited&&finite(u.next_check_epoch)?' · next check '+hhmm(new Date(u.next_check_epoch*1000)):'';
  return '<div class="usage-details"'+(tip?' title="'+esc(tip)+'"':'')+'>'+(rows||'<p class="muted">'+esc(u.state==='pending'?'Checking account limits…':'Quota data unavailable.')+'</p>')+(note?'<small>'+esc(note)+'</small>':'')+(u.updating?'<small class="usage-refreshing"><span class="usage-sync" aria-hidden="true"></span>Refreshing in the background…</small>':'')+(u.observed_at?'<small>'+esc((u.stale&&!u.rate_limited?'Last confirmed: ':'Updated: ')+hhmm(new Date(u.observed_at))+' GMT+7'+next)+'</small>':'')+'</div>';
}
/** Weekly windows join the compact strip only when this little is left (percent). */
export const LOW_REMAINING=10;
const isOld=w=>!!(w.aged||w.retained||w.estimated);
const windowMinutes=w=>w.window_minutes||(w.id==='five_hour'||w.id==='session'?300:w.id?.startsWith('seven_day')?10080:null);
const isLong=w=>{const m=windowMinutes(w);return m?m>=1440:/:secondary$/.test(w.id||'');};
export const remaining=w=>w.expired?100:w.used_percent!=null?100-w.used_percent:null;
const bucketOf=w=>w.bucket||w.id?.split(':')[0];
/**
 * Which windows the one-line strip shows (owner rule, same for Codex and Claude Code):
 * the 5-hour window always; weekly windows only when under LOW_REMAINING % is left.
 * With no 5-hour window reported, the shortest available one stands in so the strip is never blank.
 * Full details (every window) stay in the popover / connection card.
 */
export function selectWindows(provider){
  const u=provider?.usage||{},bucket=u.default_bucket||'codex',id=provider?.id;
  const pool=quotaWindows(provider).filter(w=>id==='codex'?bucketOf(w)===bucket:id==='claude'?['five_hour','session'].includes(w.id)||w.id?.startsWith('seven_day'):true)
    .sort((a,b)=>(windowMinutes(a)||0)-(windowMinutes(b)||0));
  const shown=pool.length?pool:quotaWindows(provider).sort((a,b)=>(windowMinutes(a)||0)-(windowMinutes(b)||0));   // never blank while any window is known
  const known=w=>w.expired||w.used_percent!=null;
  const primary=shown.find(w=>!isLong(w)&&known(w))||shown.find(known)||shown.find(w=>!isLong(w))||shown[0]||null;
  const extra=pool.filter(w=>w!==primary&&isLong(w)&&remaining(w)!=null&&!w.expired&&remaining(w)<LOW_REMAINING);
  return {primary,extra,all:shown};
}
const WARN_ICON='<svg class="usage-warn" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3 2.5 20h19L12 3Z"/><path d="M12 10v4m0 3h.01"/></svg>';
const pct=(w,u)=>{const left=remaining(w);return w.expired?'Reset':left==null?(w.status&&w.status!=='rejected'?'OK':'—'):(u.stale||isOld(w)?'~':'')+Math.round(left)+'%';};
const spoken=(w,provider)=>{const left=remaining(w);return label(w,provider)+': '+(w.expired?RESET_TEXT:left==null?'unavailable':Math.round(left)+'% left');};
export function usageMini(providers,esc){
  return '<div class="usage-minis" aria-label="Account quota">'+['codex','claude'].map(id=>{
    const p=(providers||[]).find(p=>p.id===id)||{id},u=p.usage||{},{primary,extra}=selectWindows(p);
    const row=(w,low)=>{
      const left=remaining(w);
      return low
        ?'<span class="usage-mini-window is-low" data-window="'+esc(duration(w,true))+'"><span>'+esc(duration(w,true))+'</span>'+WARN_ICON+'<b>'+esc(pct(w,u))+'</b></span>'
        :'<span class="usage-mini-window'+(u.stale||isOld(w)?' is-stale':'')+'" data-window="'+esc(duration(w,true))+'"><span>'+esc(duration(w,true))+'</span><span class="usage-track" aria-hidden="true"><i style="width:'+(left??0)+'%"></i></span><b>'+esc(pct(w,u))+'</b></span>';
    };
    const rows=primary?row(primary,false)+extra.map(w=>row(w,true)).join(''):'<span class="usage-mini-empty">'+esc(u.state==='pending'?'Checking…':'—')+'</span>';
    const summary=[primary,...extra].filter(Boolean).map(w=>spoken(w,p)+(extra.includes(w)?' (low)':'')).join('; ');
    return '<div class="usage-mini" data-provider="'+id+'"><button type="button" class="usage-trigger" aria-describedby="usage-popover" aria-label="'+esc(providerName(id)+' usage limits'+(summary?'. '+summary:''))+'"><span class="usage-provider">'+(id==='codex'?'Codex':'Claude')+'</span><span class="usage-mini-windows">'+rows+'</span><span class="usage-slot">'+(u.updating?'<span class="usage-sync" role="img" aria-label="Refreshing"></span>':'')+'</span></button><template data-usage-content>'+usagePlanBadge(p,esc)+'<strong>'+providerName(id)+'</strong><small>'+esc(p.account_label||'Account not confirmed')+'</small>'+usageDetails(p,esc)+'<small class="usage-rule">The line under the composer shows the 5-hour limit; the weekly limit joins it when less than '+LOW_REMAINING+'% is left.</small></template></div>';
  }).join('')+'</div>';
}
// Keep showing the last confirmed windows while a same-account refresh is pending: never flash blank between polls.
const lastKnown=new Map(),RETAIN_MS=30*60*1000;
export function withRetained(providers){
  return (providers||[]).map(p=>{
    const u=p?.usage||{};
    if(u.windows?.length){lastKnown.set(p.id,{account:p.account_label,windows:u.windows,bucket:u.default_bucket,at:Date.now()});return p;}
    const previous=lastKnown.get(p?.id);
    const fresh=previous&&previous.account===p.account_label&&Date.now()-previous.at<RETAIN_MS;
    if(fresh&&(u.updating||['pending','unavailable'].includes(u.state)))return {...p,usage:{...u,windows:previous.windows.map(w=>({...w,retained:true})),default_bucket:u.default_bucket||previous.bucket,stale:true}};
    return p;
  });
}
let controller;
export function setupUsagePopovers(){
  if(controller)return controller;
  const panel=document.createElement('div');panel.id='usage-popover';panel.className='usage-popover';panel.role='tooltip';panel.hidden=true;document.body.append(panel);
  let trigger=null,pinned=false,timer=0,dismissed=null;
  function close(){clearTimeout(timer);panel.hidden=true;trigger=null;pinned=false;}
  function place(){
    if(!trigger?.isConnected){close();return;}
    const viewport=window.visualViewport,width=viewport?.width||innerWidth,height=viewport?.height||innerHeight,offsetX=viewport?.offsetLeft||0,offsetY=viewport?.offsetTop||0;
    const box=trigger.getBoundingClientRect(),spaceAbove=box.top-offsetY-20,spaceBelow=offsetY+height-box.bottom-20,above=spaceAbove>=Math.min(240,spaceBelow);
    panel.style.width=Math.max(0,Math.min(360,width-24))+'px';panel.style.maxHeight=Math.max(40,Math.min(480,above?spaceAbove:spaceBelow,height-24))+'px';
    const rect=panel.getBoundingClientRect();panel.style.left=Math.max(offsetX+12,Math.min(box.left,offsetX+width-rect.width-12))+'px';panel.style.top=Math.max(offsetY+12,Math.min(above?box.top-rect.height-8:box.bottom+8,offsetY+height-rect.height-12))+'px';
  }
  function refresh(){
    if(!trigger)return;
    const template=trigger.closest('.usage-mini')?.querySelector('[data-usage-content]');
    if(!template||!trigger.isConnected){close();return;}
    if(panel.innerHTML!==template.innerHTML){const top=panel.scrollTop;panel.innerHTML=template.innerHTML;panel.scrollTop=top;}
    place();
  }
  function open(next,pin=false){
    if(next===dismissed&&!pin)return;
    clearTimeout(timer);pinned=pin||pinned&&next===trigger;trigger=next;panel.hidden=false;refresh();
  }
  function later(){if(!pinned){clearTimeout(timer);timer=setTimeout(close,140);}}
  document.addEventListener('pointerover',event=>{const next=event.target.closest?.('.usage-trigger');if(next)open(next);else if(panel.contains(event.target))clearTimeout(timer);});
  document.addEventListener('pointerout',event=>{
    if(event.target.closest?.('.usage-trigger')||panel.contains(event.target)){
      if(event.relatedTarget?.closest?.('.usage-trigger')===trigger||panel.contains(event.relatedTarget))return;
      dismissed=null;later();
    }
  });
  document.addEventListener('focusin',event=>{const next=event.target.closest?.('.usage-trigger');if(next){dismissed=null;open(next);}});
  document.addEventListener('focusout',event=>{if(event.target===trigger&&!panel.contains(event.relatedTarget))later();});
  document.addEventListener('click',event=>{const next=event.target.closest?.('.usage-trigger');if(next){if(trigger===next&&pinned){dismissed=next;close();}else{dismissed=null;open(next,true);}}else if(!panel.contains(event.target))close();});
  document.addEventListener('keydown',event=>{if(event.key==='Escape'&&trigger){dismissed=trigger;close();event.preventDefault();event.stopImmediatePropagation();}},true);
  document.addEventListener('scroll',()=>{if(trigger)place();},true);window.addEventListener('resize',()=>{if(trigger)place();});window.visualViewport?.addEventListener('resize',()=>{if(trigger)place();});
  controller={refresh,close};return controller;
}
export function updateUsage(container,providers,esc){
  const next=document.createElement('div');next.innerHTML=usageMini(withRetained(providers),esc);
  if(!container.querySelector('.usage-minis'))container.innerHTML=next.innerHTML;
  else for(const incoming of next.querySelectorAll('.usage-mini')){
    const current=container.querySelector('.usage-mini[data-provider="'+incoming.dataset.provider+'"]');if(!current)continue;
    for(const selector of ['.usage-trigger','[data-usage-content]']){const old=current.querySelector(selector),value=incoming.querySelector(selector);if(old.innerHTML!==value.innerHTML)old.innerHTML=value.innerHTML;}
    const trigger=current.querySelector('.usage-trigger'),label=incoming.querySelector('.usage-trigger').getAttribute('aria-label');if(trigger.getAttribute('aria-label')!==label)trigger.setAttribute('aria-label',label);
  }
  controller?.refresh();
}