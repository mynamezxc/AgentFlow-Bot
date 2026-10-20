import {esc,formatMessage} from './identity.js';

export function fileReference(target,artifacts=[],attachments=[]) {
  let path;try {path=decodeURIComponent(String(target).replace(/^<(.*)>$/,'$1')).replaceAll('\\','/').replace(/^\.\//,'');}catch{return null;}
  const record=artifacts.find(a=>!a.archived&&(path==='artifact:'+a.id||[a.path,a.absolute_path].some(p=>p&&p.replaceAll('\\','/')===path)));
  if(record)return {source:'artifact',record};
  const upload=attachments.find(a=>path==='attachment:'+a.id||path===a.name||path==='/api/attachments/'+a.id);
  return upload?{source:'attachment',record:upload}:null;
}
const attrs=ref=>' data-action="rich-file" data-source="'+ref.source+'" data-id="'+esc(ref.record.id)+'"';
export function fileCard(ref,label) {
  const a=ref.record,name=label||a.title||a.name||a.path;
  return '<button type="button" class="rich-file-card"'+attrs(ref)+'><span class="rich-file-symbol" aria-hidden="true">'+(a.kind==='canvas'?'◇':a.is_image?'▧':'▤')+'</span><span><strong>'+esc(name)+'</strong><small>'+esc(a.path||a.name)+' · '+(a.bytes?Math.ceil(a.bytes/1024)+' KB':'File')+'</small></span><span aria-hidden="true">↗</span></button>';
}
export const GALLERY_SHOWN=3;
export const imageSource=ref=>ref.source==='attachment'?'/api/attachments/'+ref.record.id:'/api/outputs/'+ref.record.id+'/raw';
/** One thumbnail tile. opts.more = N renders the "+N" overlay tile; opts.hidden keeps it in the DOM only for the lightbox. */
export function imageCard(ref,label,opts={}) {
  if(!ref.record.is_image)return opts.hidden?'':fileCard(ref,label);
  const src=imageSource(ref),title=label||ref.record.title||ref.record.name||'Image';
  return '<button type="button" class="rich-image'+(opts.more?' has-more':'')+'"'+(opts.hidden?' hidden':'')+' data-lightbox-src="'+esc(src)+'" data-lightbox-title="'+esc(title)+'" aria-label="'+esc(opts.more?'View '+title+' and '+(opts.more-1)+' more images':'View image: '+title)+'" title="'+esc(title)+'"><img src="'+esc(src)+'" alt="'+esc(title)+'" loading="lazy" decoding="async"><span class="rich-image-title">'+esc(title)+'</span>'+(opts.more?'<span class="rich-more" aria-hidden="true">+'+opts.more+'</span>':'')+'</button>';
}
/** Wraps thumbnail tiles in a tight grid; more than 4 images -> 3 tiles + a "+N" tile, the rest stay hidden for the lightbox. */
export function galleryTiles(count,index) {
  return count>GALLERY_SHOWN+1?(index===GALLERY_SHOWN?{more:count-GALLERY_SHOWN}:index>GALLERY_SHOWN?{hidden:true}:{}):{};
}
export const galleryMarkup=(tiles,cls='rich-gallery')=>'<div class="'+cls+'" data-lightbox-group>'+tiles.join('')+'</div>';
function inline(text,ctx) {
  const regex=/(`[^`\n]+`)|(!?\[([^\]\n]+)\]\(([^)\n]+)\))/g;let html='',last=0;
  for(const m of text.matchAll(regex)) {
    html+=formatMessage(text.slice(last,m.index),ctx.agents,ctx.artifacts);last=m.index+m[0].length;
    if(m[1]){html+=formatMessage(m[1],ctx.agents,ctx.artifacts);continue;}
    const ref=fileReference(m[4],ctx.artifacts,ctx.attachments);
    if(ref)html+=m[0].startsWith('!')?imageCard(ref,m[3]):'<button type="button" class="artifact-inline"'+attrs(ref)+'>'+esc(m[3])+'</button>';
    else if(/^https:\/\/[^\s<>]+$/i.test(m[4]))html+='<a href="'+esc(m[4])+'" target="_blank" rel="noopener noreferrer">'+esc(m[3])+'</a>';
    else html+=esc(m[0]);
  }
  return html+formatMessage(text.slice(last),ctx.agents,ctx.artifacts);
}
const splitRow=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split(/(?<!\\)\|/).map(c=>c.trim().replaceAll('\\|','|'));
export function tableMarkup(headers,rows,caption='Data',cell=esc) {
  return '<div class="rich-table-wrap" tabindex="0" role="region" aria-label="'+esc(caption)+'"><table class="rich-table"><caption class="sr-only">'+esc(caption)+'</caption><thead><tr>'+headers.map(h=>'<th scope="col">'+cell(String(h))+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+headers.map((_,i)=>'<td>'+cell(String(row[i]??''))+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
}
export function normalizeChart(value) {
  const type=value?.type;if(!['bar','line','pie'].includes(type))throw Error('Use bar, line or pie.');
  const labels=value.data?.labels,datasets=value.data?.datasets;
  if(!Array.isArray(labels)||!labels.length||labels.length>40||labels.some(x=>typeof x!=='string'||x.length>100)||!Array.isArray(datasets)||!datasets.length||datasets.length>6)throw Error('Use 1–40 labels and 1–6 datasets.');
  for(const s of datasets)if(!Array.isArray(s.data)||s.data.length!==labels.length||s.data.some(x=>typeof x!=='number'||!Number.isFinite(x)||Math.abs(x)>1e12))throw Error('Every label needs one finite number per dataset.');
  if(type==='pie'&&(datasets.length!==1||datasets[0].data.some(x=>x<0)||datasets[0].data.every(x=>x===0)))throw Error('Pie needs one non-negative dataset with a positive total.');
  return {type,labels,datasets:datasets.map((s,i)=>({label:String(s.label||'Series '+(i+1)).slice(0,100),data:s.data})),title:String(value.title||'Chart').slice(0,180),unit:String(value.unit||'').slice(0,30),source:String(value.source||'').slice(0,300)};
}
export function chartMarkup(value) {
  const c=normalizeChart(value),W=640,H=280,L=58,R=18,T=18,B=46,pw=W-L-R,ph=H-T-B;
  const vals=c.datasets.flatMap(s=>s.data),lo=Math.min(0,...vals),maximum=Math.max(0,...vals),hi=maximum===lo?lo+1:maximum,range=hi-lo,y=v=>T+(hi-v)/range*ph;
  const n=v=>new Intl.NumberFormat('en',{maximumFractionDigits:2,notation:Math.abs(v)>=100000?'compact':'standard'}).format(v);
  let svg='';
  if(c.type==='pie') {
    const sum=vals.reduce((a,b)=>a+b,0);let angle=-Math.PI/2;
    c.datasets[0].data.forEach((v,i)=>{if(!v)return;const end=angle+v/sum*Math.PI*2,x=a=>320+100*Math.cos(a),y=a=>136+100*Math.sin(a);
      svg+=v===sum?'<circle class="chart-series s'+i%6+'" cx="320" cy="136" r="100"><title>'+esc(c.labels[i]+': '+n(v))+'</title></circle>':'<path class="chart-series s'+i%6+'" d="M320 136L'+x(angle)+' '+y(angle)+'A100 100 0 '+(end-angle>Math.PI?1:0)+' 1 '+x(end)+' '+y(end)+'Z"><title>'+esc(c.labels[i]+': '+n(v))+'</title></path>';angle=end;});
  } else {
    for(let i=0;i<=4;i++){const v=lo+range*i/4;svg+='<line class="chart-grid" x1="'+L+'" x2="'+(W-R)+'" y1="'+y(v)+'" y2="'+y(v)+'"/><text class="chart-axis" x="'+(L-8)+'" y="'+(y(v)+4)+'" text-anchor="end">'+esc(n(v))+'</text>';}
    const step=pw/c.labels.length;
    c.labels.forEach((label,i)=>{if(i%Math.ceil(c.labels.length/6)===0)svg+='<text class="chart-axis" x="'+(L+step*(i+.5))+'" y="'+(H-15)+'" text-anchor="middle">'+esc(label.length>8?label.slice(0,7)+'…':label)+'</text>';});
    c.datasets.forEach((s,j)=>{
      if(c.type==='line')svg+='<polyline class="chart-line s'+j+'" points="'+s.data.map((v,i)=>(L+step*(i+.5))+','+y(v)).join(' ')+'"/>';
      s.data.forEach((v,i)=>{const x=L+step*(i+.5),label=esc(c.labels[i]+' · '+s.label+': '+n(v)+' '+c.unit);
        svg+=c.type==='bar'?'<rect class="chart-series s'+j+'" x="'+(L+step*i+step*.12+j*step*.76/c.datasets.length)+'" y="'+Math.min(y(0),y(v))+'" width="'+Math.max(1,step*.76/c.datasets.length-2)+'" height="'+Math.max(1,Math.abs(y(v)-y(0)))+'" rx="2"><title>'+label+'</title></rect>':'<circle class="chart-series s'+j+'" cx="'+x+'" cy="'+y(v)+'" r="3"><title>'+label+'</title></circle>';});
    });
  }
  const legend=c.type==='pie'?c.labels:c.datasets.map(s=>s.label);
  return '<figure class="rich-chart"><figcaption><strong>'+esc(c.title)+'</strong><small>'+esc(c.unit)+'</small></figcaption><svg viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+esc(c.title)+'">'+svg+'</svg><div class="chart-legend">'+legend.map((label,i)=>'<span><i class="chart-series s'+i%6+'"></i>'+esc(label)+'</span>').join('')+'</div>'+(c.source?'<p class="chart-source">'+esc(c.source)+'</p>':'')+'<details class="chart-data"><summary>Data</summary>'+tableMarkup(['Label',...c.datasets.map(s=>s.label)],c.labels.map((label,i)=>[label,...c.datasets.map(s=>s.data[i])]),c.title)+'</details></figure>';
}
export function renderMarkdown(content='',context={}) {
  const ctx={agents:[],artifacts:[],attachments:[],...context},lines=String(content).replaceAll('\r','').split('\n');let out='';
  for(let i=0;i<lines.length;i++) {
    const line=lines[i],fence=/^\s*```([\w-]*)\s*$/.exec(line);
    if(fence){const body=[];while(++i<lines.length&&!/^\s*```\s*$/.test(lines[i]))body.push(lines[i]);const text=body.join('\n'),language=fence[1].toLowerCase();
      if(language==='chart'){try{out+=chartMarkup(JSON.parse(text));}catch(e){out+='<div class="rich-invalid" role="note">Chart: '+esc(e.message)+'</div><pre class="message-code"><code>'+esc(text)+'</code></pre>';}}
      else if(['files','images'].includes(language)){try{const parsed=JSON.parse(text),items=Array.isArray(parsed)?parsed:parsed.files||parsed.images;if(!Array.isArray(items)||items.length>60)throw Error();out+='<div class="'+(language==='images'?'rich-gallery':'rich-files')+'"'+(language==='images'?' data-lightbox-group':'')+'>'+items.map((item,index)=>{const ref=fileReference(typeof item==='string'?item:item.path||item.id,ctx.artifacts,ctx.attachments);return ref?(language==='images'?imageCard(ref,item.title,galleryTiles(items.length,index)):fileCard(ref,item.title)):'<p class="rich-invalid">File unavailable: '+esc(typeof item==='string'?item:item.path||item.id)+'</p>';}).join('')+'</div>';}catch{out+='<pre class="message-code"><code>'+esc(text)+'</code></pre>';}}
      else out+='<div class="rich-code"><small>'+esc(language||'text')+'</small><pre class="message-code"><code>'+esc(text)+'</code></pre></div>';continue;
    }
    if(line.includes('|')&&i+1<lines.length&&/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[i+1])){
      const headers=splitRow(line).slice(0,20),rows=[];i++;while(i+1<lines.length&&lines[i+1].includes('|')&&lines[i+1].trim()){const row=splitRow(lines[++i]);if(rows.length<200)rows.push(row);}
      out+=tableMarkup(headers,rows,'Table',cell=>inline(cell,ctx))+(rows.length===200?'<small>Showing up to 200 rows.</small>':'');continue;
    }
    if(!line.trim()){continue;}
    const image=/^!\[([^\]]*)\]\(([^)]+)\)\s*$/.exec(line),file=/^\[([^\]]+)\]\(([^)]+)\)\s*$/.exec(line);
    if(image||file){const m=image||file,ref=fileReference(m[2],ctx.artifacts,ctx.attachments);if(ref){out+=image?galleryMarkup([imageCard(ref,m[1])]):fileCard(ref,m[1]);continue;}}
    const heading=/^(#{1,4}) (.*)/.exec(line),bullet=/^\s*([-*]|\d+\.) (.*)/.exec(line);
    out+=heading?'<h'+(heading[1].length+2)+' class="rich-heading">'+inline(heading[2],ctx)+'</h'+(heading[1].length+2)+'>':bullet?'<div class="rich-bullet"><span aria-hidden="true">'+esc(bullet[1]==='-'||bullet[1]==='*'?'•':bullet[1])+'</span><span>'+inline(bullet[2],ctx)+'</span></div>':'<p class="rich-paragraph">'+inline(line,ctx)+'</p>';
  }
  return out;
}
