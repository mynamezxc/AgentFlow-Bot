// Image lightbox: one native <dialog>, event-delegated from any [data-lightbox-src] thumbnail.
// Thumbnails inside the same [data-lightbox-group] form one gallery (hidden tiles included).
export const wrapIndex=(index,count)=>count?((index%count)+count)%count:0;
export const counterText=(index,count)=>(index+1)+' / '+count;
export const SWIPE_MIN=48;
/** True when the image is larger than the space it is fitted into, i.e. "Actual size" would show more pixels. */
export const exceedsBox=(natural,box)=>!!natural&&!!box&&(natural.width>box.width+1||natural.height>box.height+1);
/** Horizontal swipe -> -1 (next) / +1 (previous) / 0 (ignore). */
export function swipeDirection(dx,dy) {
  if(Math.abs(dx)<SWIPE_MIN||Math.abs(dx)<Math.abs(dy)*1.4) return 0;
  return dx<0?1:-1;
}
const svg=path=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+path+'</svg>';
const ICON={close:'<path d="m6 6 12 12M6 18 18 6"/>',prev:'<path d="m15 5-7 7 7 7"/>',next:'<path d="m9 5 7 7-7 7"/>'};

let dialog,items=[],index=0,opener=null,touch=null;
const q=selector=>dialog.querySelector(selector);

export function collect(trigger) {
  const group=trigger.closest('[data-lightbox-group]');
  const nodes=group?[...group.querySelectorAll('[data-lightbox-src]')]:[trigger];
  return {items:nodes.map(n=>({src:n.dataset.lightboxSrc,title:n.dataset.lightboxTitle||''})),index:Math.max(0,nodes.indexOf(trigger))};
}

function show(next) {
  index=wrapIndex(next,items.length);
  const item=items[index],img=q('.lightbox-img');
  setActual(false);
  q('.lightbox-stage').classList.remove('is-error');
  q('.lightbox-stage').classList.add('is-loading');
  img.alt=item.title||'Image '+(index+1);img.src=item.src;
  q('.lightbox-count').textContent=counterText(index,items.length);
  q('.lightbox-title').textContent=item.title;
  const open=q('[data-lb=open]'),download=q('[data-lb=download]');
  open.href=item.src;download.href=item.src;download.setAttribute('download',item.title||'image');
  dialog.classList.toggle('single',items.length<2);
  for(const offset of [1,-1]) if(items.length>1) {const pre=new Image();pre.src=items[wrapIndex(index+offset,items.length)].src;}
}

function setActual(on) {
  const stage=q('.lightbox-stage'),button=q('[data-lb=zoom]'),canvas=q('.lightbox-canvas');
  stage.classList.toggle('is-actual',on);
  button.setAttribute('aria-pressed',String(on));button.textContent=on?'Fit to screen':'Actual size';
  if(on) {canvas.setAttribute('tabindex','0');canvas.setAttribute('role','group');canvas.setAttribute('aria-label','Image at actual size, scroll to pan');canvas.scrollTo(0,0);}
  else {canvas.removeAttribute('tabindex');canvas.removeAttribute('role');canvas.removeAttribute('aria-label');}
}
/** Offer "Actual size" only when it would reveal more than the fitted view does. */
function syncZoom() {
  const img=q('.lightbox-img'),canvas=q('.lightbox-canvas'),stage=q('.lightbox-stage');
  if(stage.classList.contains('is-actual')) return;
  q('[data-lb=zoom]').hidden=!exceedsBox({width:img.naturalWidth,height:img.naturalHeight},{width:canvas.clientWidth,height:canvas.clientHeight});
}
function build() {
  dialog=document.createElement('dialog');dialog.className='lightbox';dialog.setAttribute('aria-label','Image viewer');
  dialog.innerHTML='<div class="lightbox-bar"><span class="lightbox-count" aria-live="polite"></span><strong class="lightbox-title"></strong><button type="button" class="button small" data-lb="zoom" aria-pressed="false" hidden>Actual size</button><a class="button small" data-lb="open" target="_blank" rel="noopener noreferrer">Open original</a><a class="button small" data-lb="download" download>Download</a><button type="button" class="icon-button" data-lb="close" aria-label="Close image viewer">'+svg(ICON.close)+'</button></div>'+
    '<div class="lightbox-stage is-loading"><button type="button" class="lightbox-nav prev" data-lb="prev" aria-label="Previous image">'+svg(ICON.prev)+'</button><div class="lightbox-canvas"><img class="lightbox-img" alt="" draggable="false"><span class="lightbox-fail" role="status">This image could not be loaded.</span></div><button type="button" class="lightbox-nav next" data-lb="next" aria-label="Next image">'+svg(ICON.next)+'</button></div>';
  document.body.append(dialog);
  const img=q('.lightbox-img');
  img.addEventListener('load',()=>{q('.lightbox-stage').classList.remove('is-loading');syncZoom();});
  img.addEventListener('click',()=>{if(!q('[data-lb=zoom]').hidden||q('.lightbox-stage').classList.contains('is-actual'))setActual(!q('.lightbox-stage').classList.contains('is-actual'));});
  window.addEventListener('resize',()=>{if(dialog.open)syncZoom();});
  img.addEventListener('error',()=>{const stage=q('.lightbox-stage');stage.classList.remove('is-loading');stage.classList.add('is-error');});
  dialog.addEventListener('click',event=>{
    const action=event.target.closest('[data-lb]')?.dataset.lb;
    if(action==='close'||event.target===dialog||event.target.classList.contains('lightbox-stage')||(event.target.classList.contains('lightbox-canvas')&&!q('.lightbox-stage').classList.contains('is-actual'))) dialog.close();
    else if(action==='zoom') setActual(!q('.lightbox-stage').classList.contains('is-actual'));
    else if(action==='prev') show(index-1);
    else if(action==='next') show(index+1);
  });
  dialog.addEventListener('keydown',event=>{
    const panning=q('.lightbox-stage').classList.contains('is-actual')&&event.target===q('.lightbox-canvas');
    if(event.key==='ArrowLeft'&&!panning){event.preventDefault();show(index-1);}
    else if(event.key==='ArrowRight'&&!panning){event.preventDefault();show(index+1);}
    else if((event.key==='z'||event.key==='Z')&&!q('[data-lb=zoom]').hidden){event.preventDefault();setActual(!q('.lightbox-stage').classList.contains('is-actual'));}
    else if(event.key==='Home'){event.preventDefault();show(0);}
    else if(event.key==='End'){event.preventDefault();show(items.length-1);}
  });
  dialog.addEventListener('close',()=>{q('.lightbox-img').removeAttribute('src');if(opener?.isConnected)opener.focus({preventScroll:true});opener=null;});
  const stage=q('.lightbox-stage');
  stage.addEventListener('pointerdown',event=>{if(event.pointerType!=='mouse')touch={x:event.clientX,y:event.clientY};});
  stage.addEventListener('pointerup',event=>{
    if(!touch||stage.classList.contains('is-actual')){touch=null;return;}
    const direction=swipeDirection(event.clientX-touch.x,event.clientY-touch.y);touch=null;
    if(direction)show(index+direction);
  });
  stage.addEventListener('pointercancel',()=>{touch=null;});
}

export function openLightbox(trigger) {
  const found=collect(trigger);
  if(!found.items.length) return;
  if(!dialog) build();
  items=found.items;opener=trigger;
  if(!dialog.open) dialog.showModal();
  show(found.index);
  q('[data-lb=close]').focus({preventScroll:true});
}

let installed=false;
export function setupLightbox() {
  if(installed||typeof document==='undefined') return;
  installed=true;
  // Capture phase: thumbnails must not also trigger app-level data-action handlers.
  document.addEventListener('click',event=>{
    const trigger=event.target.closest?.('[data-lightbox-src]');
    if(!trigger||trigger.closest('dialog.lightbox')) return;
    event.preventDefault();event.stopPropagation();
    openLightbox(trigger);
  },true);
}
