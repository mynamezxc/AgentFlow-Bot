// Project budget usage + token/cache statistics for the chat Context panel (SPEC contracts #1 and #3).
export const DEFAULT_BUDGET={run_turns:24,project_turns:64,project_runs:24,project_tokens:0};
const num=v=>typeof v==='number'&&Number.isFinite(v)?v:0;
export const budgetSettings=settings=>({...DEFAULT_BUDGET,...(settings?.budget||{})});

export function formatTokens(value) {
  const n=num(value);
  if(n>=1e6) return (n/1e6).toFixed(n>=1e7?0:1).replace(/\.0$/,'')+'M';
  if(n>=1e3) return (n/1e3).toFixed(n>=1e5?0:1).replace(/\.0$/,'')+'k';
  return String(Math.round(n));
}

/** Sum token_usage over runs. input = uncached input; cached = cache reads. */
export function tokenTotals(runs=[]) {
  const total={input:0,cached:0,created:0,output:0,turns:0};
  for(const r of runs) {
    const u=r?.token_usage;if(!u) continue;
    total.input+=num(u.input_tokens);total.cached+=num(u.cached_input_tokens);total.created+=num(u.cache_creation_tokens);total.output+=num(u.output_tokens);total.turns+=num(u.turns);
  }
  return total;
}
/** cached / (input + cached), as a whole percent; null when there is nothing to measure. */
export function cacheHitPercent(total) {
  const denominator=num(total.input)+num(total.cached);
  return denominator>0?Math.round(num(total.cached)/denominator*100):null;
}

/** The root run of a project: prefer a live root, otherwise the latest one. */
export function projectRoot(runs=[],projectId) {
  const roots=runs.filter(r=>r&&r.project_id===projectId&&r.kind!=='peer'&&(!r.root_id||r.root_id===r.id));
  const live=roots.filter(r=>!['done','failed','cancelled'].includes(r.status));
  return (live.length?live:roots).at(-1)||null;
}

function meter(label,used,limit,esc,unit='') {
  const ratio=limit>0?Math.min(1,used/limit):0,tone=ratio>=1?'full':ratio>=.8?'warn':'';
  return '<div class="budget-meter '+tone+'"><div><span>'+esc(label)+'</span><strong>'+esc(formatTokens(used)+' / '+formatTokens(limit)+unit)+'</strong></div><progress max="'+limit+'" value="'+Math.min(used,limit)+'" aria-label="'+esc(label)+'"></progress></div>';
}

export function budgetMarkup({root,settings,runs=[]},esc=String) {
  const budget=budgetSettings(settings),total=tokenTotals(runs),hit=cacheHitPercent(total);
  const unlimited=!!root?.budget_unlimited;
  let body='';
  if(root) {
    if(unlimited) body+='<p class="budget-unlimited"><span class="decision-chip">Unlimited for this task</span><small>Budget limits are skipped until this task finishes. Loop safety brakes stay on.</small></p>';
    body+='<div class="budget-rows">'+
      (unlimited?'<div class="budget-line"><span>Agent runs this task</span><strong>'+esc(num(root.family_attempts))+'</strong></div>':
        (budget.project_turns>0?meter('Turns this project',num(root.family_attempts),budget.project_turns,esc):'<div class="budget-line"><span>Turns this project</span><strong>'+esc(num(root.family_attempts))+'</strong></div>')+
        (budget.project_tokens>0?meter('Tokens this project',num(root.family_tokens),budget.project_tokens,esc):'<div class="budget-line"><span>Tokens this project</span><strong>'+esc(formatTokens(root.family_tokens))+' · no limit</strong></div>'))+
    '</div>';
  }
  if(total.input||total.cached||total.output||total.created) {
    body+='<p class="context-label">TOKENS</p><dl class="token-stats"><div><dt>Input</dt><dd>'+esc(formatTokens(total.input))+'</dd></div><div><dt>Cached</dt><dd>'+esc(formatTokens(total.cached))+'</dd></div><div><dt>Output</dt><dd>'+esc(formatTokens(total.output))+'</dd></div><div><dt>Cache hit</dt><dd>'+esc(hit==null?'—':hit+'%')+'</dd></div></dl>';
  }
  return body||'<p class="context-note">Budget and token usage appear once the team starts working on this goal.</p>';
}
export const budgetSummary=(root,settings,runs)=>{
  if(!root) return '';
  if(root.budget_unlimited) return 'Unlimited';
  const limit=budgetSettings(settings).project_turns;
  return limit>0?num(root.family_attempts)+'/'+limit:'';
};
