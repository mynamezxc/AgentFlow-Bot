// Files an agent attached to its reply (`artifact_ids`): compact chips, images as 48px thumbs that open the lightbox.
const KIND={pdf:'pdf',docx:'doc',doc:'doc',xlsx:'sheet',xls:'sheet',csv:'sheet',pptx:'slides',md:'text',txt:'text',json:'code',html:'code',js:'code',ts:'code',py:'code',css:'code',xml:'code',yaml:'code',yml:'code',sql:'code'};
const PATHS={
  doc:'<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M9.5 12h5m-5 3h5m-5 3h3"/>',
  pdf:'<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/><path d="M9 17v-4h1.4a1.3 1.3 0 0 1 0 2.6H9"/>',
  sheet:'<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M9 12h6m-6 3h6m-3-3v6"/>',
  slides:'<rect x="4" y="5" width="16" height="11" rx="1.5"/><path d="M12 16v4m-3 0h6"/>',
  text:'<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4M9.5 12h5m-5 3h5"/>',
  code:'<path d="m9 8-4 4 4 4m6-8 4 4-4 4"/>',
  image:'<rect x="4" y="5" width="16" height="14" rx="2"/><circle cx="9" cy="10" r="1.5"/><path d="m5 17 5-4 3 2.5 3-3 3 3"/>',
  canvas:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5z"/>'
};
const ico=kind=>'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(PATHS[kind]||PATHS.doc)+'</svg>';

export function fileKind(a) {
  if(a?.is_image) return 'image';
  if(a?.kind==='canvas') return 'canvas';
  const type=String(a?.type||String(a?.path||'').split('.').pop()||'').toLowerCase();
  return KIND[type]||'doc';
}
export function formatSize(bytes) {
  const n=Number(bytes);
  if(!Number.isFinite(n)||n<=0) return '';
  if(n<1024) return n+' B';
  if(n<1024*1024) return Math.max(1,Math.round(n/1024))+' KB';
  return (n/1024/1024).toFixed(n<10*1024*1024?1:0)+' MB';
}
/** Resolve ids against artifact records, de-duplicated, unknown/archived ids dropped. */
export function resolveFiles(ids,artifacts) {
  const seen=new Set(),out=[];
  for(const id of Array.isArray(ids)?ids:[]) {
    if(seen.has(id)) continue;seen.add(id);
    const record=(artifacts||[]).find(a=>a.id===id&&!a.archived);
    if(record) out.push(record);
  }
  return out;
}
export const rawUrl=a=>'/api/outputs/'+encodeURIComponent(a.id)+'/raw';
const title=a=>String(a.title||a.path||'File').split(/[\\/]/).at(-1);

/** Image thumbs first (one lightbox group), then document chips. */
export function messageFilesMarkup(records,esc) {
  if(!records.length) return '';
  const images=records.filter(a=>a.is_image),rest=records.filter(a=>!a.is_image);
  const thumbs=images.length?'<div class="msg-thumbs" data-lightbox-group>'+images.map(a=>'<button type="button" class="msg-thumb" data-lightbox-src="'+esc(rawUrl(a))+'" data-lightbox-title="'+esc(title(a))+'" aria-label="View image: '+esc(title(a))+'" title="'+esc(title(a)+(a.bytes?' · '+formatSize(a.bytes):''))+'"><img src="'+esc(rawUrl(a))+'" alt="'+esc(title(a))+'" loading="lazy" decoding="async"></button>').join('')+'</div>':'';
  const chips=rest.length?rest.map(a=>{
    const kind=fileKind(a),size=formatSize(a.bytes);
    return '<button type="button" class="msg-file" data-action="rich-file" data-source="artifact" data-id="'+esc(a.id)+'" title="'+esc(a.path||title(a))+'"><span class="msg-file-icon k-'+kind+'">'+ico(kind)+'</span><span class="msg-file-name">'+esc(title(a))+'</span>'+(size?'<small>'+esc(size)+'</small>':'')+'</button>';
  }).join(''):'';
  return '<div class="msg-files" aria-label="Attached files">'+thumbs+chips+'</div>';
}
