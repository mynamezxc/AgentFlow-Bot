// Collapsed work log for the owner chat: agent step narration and auto-approved actions
// (commands, website access, MCP calls) fold into one line; every row expands on click.

const ACTION_ICONS = {command: 'code', browser: 'globe', hostmap: 'globe', mcp: 'plug', other: 'spark'};

/** True for messages that belong in the collapsed work log instead of a chat bubble. */
export function isActivity(m) {
  if (!m || m.role === 'user' || (m.artifact_ids || []).length) return false;
  if (m.kind === 'agent_event') return false;  // agent created/updated cards are always shown to the owner
  if (m.audience === 'activity' || m.kind === 'progress' || m.kind === 'action') return true;
  // Older auto-approval notices were stored as kind "info" Markdown bubbles.
  return m.kind === 'info' && /^Auto-approved/.test(String(m.content || ''));
}

/** Structured action rows for a message: new records carry `actions`; legacy notices are parsed from text. */
export function actionsOf(m) {
  if (Array.isArray(m?.actions) && m.actions.length) return m.actions;
  if (m?.kind !== 'info' && m?.kind !== 'action') return [];
  const text = String(m.content || '').replace(/^Auto-approved(?: \d+ (?:requests|actions))?:?\s*/, '');
  const lines = text.split(/\n(?=- )/).map(s => s.replace(/^- /, '').trim()).filter(Boolean);
  return (lines.length ? lines : [text]).map(line => {
    const command = /^run `([\s\S]*)` in (.+)$/.exec(line);
    if (command) return {type: 'command', label: 'Run ' + command[1].replace(/\s+/g, ' '), detail: command[1], cwd: command[2], auto: true};
    const type = /^call MCP/i.test(line) ? 'mcp' : /^(browser access|map )/i.test(line) ? 'browser' : 'other';
    return {type, label: line.replace(/`/g, '').replace(/\s+/g, ' '), detail: line.replace(/`/g, ''), auto: true};
  });
}

const time = v => { const t = Date.parse(v || ''); return Number.isFinite(t) ? t : 0; };
const oneLine = (text, n = 140) => String(text || '').replace(/\s+/g, ' ').trim().slice(0, n);
export const MAX_ROWS = 200;       // rows rendered per streak before "Show earlier"
const TEAM_KINDS = new Set(['peer', 'peer_reply', 'handoff', 'chat', 'assignment']);

/** Runs that belong to a room: every run whose ROOT run was started in that room (delegated tasks, reviews and peers included). */
export function treeRuns(runs, room) {
  const byId = new Map((runs || []).map(r => [r.id, r]));
  return (runs || []).filter(r => !r.system_managed && ((byId.get(r.root_id) || r).channel === room));
}

/** Time stamp for an entry saved before `at` existed: the run's own start (kept in order by the index). */
function fallbackAt(run, index) {
  const base = time(run.started_at) || time(run.created_at) || time(run.updated_at);
  return base ? new Date(base + index).toISOString() : '';
}

/**
 * Extra work-log entries for a room, built only from data already in /api/state: progress notes of every run in the room's tree,
 * one tool-call summary per run, and internal team messages. Entries look like messages (audience "activity") so they fold into streaks.
 */
export function buildLogExtras({room, messages = [], runs = [], include = {}}) {
  const tree = treeRuns(runs, room), ids = new Set(tree.map(r => r.id));
  const seen = new Set((messages || []).filter(m => m.channel === room && m.audience === 'activity').map(m => (m.agent_id || '') + '|' + oneLine(m.content, 200)));
  const out = [];
  for (const run of tree) {
    const log = run.work_log || {};
    if (include.notes !== false) (log.notes || []).forEach((n, i) => {
      const text = oneLine(n?.t, 200);
      if (!text || seen.has((run.agent_id || '') + '|' + text)) return;
      out.push({id: 'note:' + run.id + ':' + i, role: 'assistant', kind: 'progress', audience: 'activity', agent_id: run.agent_id, run_id: run.id,
        content: String(n.t).trim(), created_at: n.at || fallbackAt(run, i), _log: 'note'});
    });
    const calls = (log.calls || []).filter(c => c && c.n);
    if (include.calls !== false && calls.length) out.push({id: 'calls:' + run.id, role: 'assistant', kind: 'calls', audience: 'activity', agent_id: run.agent_id,
      run_id: run.id, calls: calls.map((c, i) => ({...c, at: c.at || fallbackAt(run, i)})), created_at: calls[0].at || fallbackAt(run, 0), content: ''});
  }
  if (include.team !== false) {
    const peers = (messages || []).filter(m => m.kind === 'peer' && ids.has(m.run_id));
    for (const m of messages || []) {
      if (m.audience !== 'internal' || !TEAM_KINDS.has(m.kind) || !ids.has(m.run_id)) continue;
      // The handoff record only says "Messaged X": skip it when the peer message itself is shown.
      if (m.kind === 'handoff' && peers.some(p => p.agent_id === m.agent_id && p.target_id === m.target_id && Math.abs(time(p.created_at) - time(m.created_at)) < 120000)) continue;
      const text = String(m.content || '').trim();
      if (!text) continue;
      out.push({id: 'team:' + m.id, role: 'assistant', kind: 'team', audience: 'activity', agent_id: m.agent_id, target_id: m.target_id || '',
        run_id: m.run_id, content: text, created_at: m.created_at, _src: m.kind});
    }
  }
  return out.sort((a, b) => time(a.created_at) - time(b.created_at));
}

/** Messages in their own order with the extras inserted chronologically (an extra never moves a message). */
export function mergeLog(msgs, extras) {
  const sorted = [...extras].sort((a, b) => time(a.created_at) - time(b.created_at)), out = [];
  let i = 0;
  for (const m of msgs) {
    const t = time(m.created_at);
    while (i < sorted.length && time(sorted[i].created_at) <= t) out.push(sorted[i++]);
    out.push(m);
  }
  while (i < sorted.length) out.push(sorted[i++]);
  return out;
}

/** One-line summary text for a group of work-log entries; `names(id)` and `mainId` add "team: Lan, Tuan". */
export function logSummary(entries, names, mainId) {
  const callsOf = e => e.kind === 'calls' ? (e.calls || []).length : 0;
  const actions = entries.reduce((n, e) => n + actionsOf(e).length, 0), calls = entries.reduce((n, e) => n + callsOf(e), 0);
  const notes = entries.filter(e => !actionsOf(e).length && e.kind !== 'calls').length;
  const parts = [];
  if (notes) parts.push(notes + (notes === 1 ? ' update' : ' updates'));
  if (actions) parts.push(actions + (actions === 1 ? ' auto-approved action' : ' auto-approved actions'));
  if (calls) parts.push(calls + (calls === 1 ? ' tool call' : ' tool calls'));
  if (names) {
    const team = [...new Set(entries.flatMap(e => [e.agent_id, e.target_id]).filter(id => id && id !== mainId))].map(names).filter(Boolean);
    if (team.length) parts.push('team: ' + team.slice(0, 4).join(', ') + (team.length > 4 ? ' +' + (team.length - 4) : ''));
  }
  return 'Work log · ' + (parts.join(' · ') || '0 updates');
}

const callTarget = c => oneLine(c.a, 60);

/**
 * h = {esc, icon, stamp, format(text, projectId), agentName(id)|null, avatar(id)|null, expanded(key)|null, nameOf(id)|null, mainId}
 * With h.lazy the rows are left out (an empty <ol data-lazy>) and filled by renderWorkLogRows when the owner opens the log:
 * collapsed streaks are the bulk of a long transcript's DOM.
 */
export function renderWorkLog(entries, h) {
  const {esc, icon} = h;
  const key = 'log:' + (entries[0].id || '');
  const latest = entries[entries.length - 1], lastActs = actionsOf(latest);
  const previewText = lastActs.length ? lastActs[lastActs.length - 1].label : latest.kind === 'calls' ? (latest.calls || []).length + ' tool calls' : latest.kind === 'team' ? latest.content : latest.content;
  return '<details class="work-log" data-key="' + esc(key) + '"><summary>' + icon('down') + '<span>' + esc(logSummary(entries, h.nameOf, h.mainId)) +
    '</span><small>' + esc(oneLine(String(previewText || '').replace(/\[([^\]]*)\]\([^)]*\)/g, '$1'))) + '</small></summary>' +
    (h.lazy ? '<ol data-lazy="1"></ol>' : '<ol>' + renderWorkLogRows(entries, h) + '</ol>') + '</details>';
}

/** The <li> rows of one streak (capped at MAX_ROWS unless h.expanded(key)). */
export function renderWorkLogRows(entries, h) {
  const {esc, icon} = h;
  const who = id => h.agentName ? h.agentName(id) : '';
  const face = id => h.avatar && id ? h.avatar(id) : '';
  const rows = [];
  entries.forEach(e => {
    const name = who(e.agent_id), acts = actionsOf(e);
    if (acts.length) {
      acts.forEach((a, i) => rows.push('<li class="wl-action"><details data-key="act:' + esc(e.id) + ':' + i + '"><summary>' +
        icon(ACTION_ICONS[a.type] || 'spark') + '<span class="wl-badge">Auto-approved</span><code class="wl-label" title="' + esc(a.label) + '">' + esc(a.label) + '</code>' +
        '<time>' + esc(h.stamp(e.created_at)) + '</time></summary>' +
        '<pre class="wl-detail">' + esc(a.detail || a.label) + '</pre>' + (a.cwd ? '<small class="wl-cwd">in ' + esc(a.cwd) + '</small>' : '') + '</details></li>'));
    } else if (e.kind === 'calls') {
      const list = e.calls || [], failed = list.filter(c => c.e).length;
      const tools = [...new Set(list.map(c => c.n))];
      rows.push('<li class="wl-calls"><details data-key="calls:' + esc(e.run_id || e.id) + '"><summary>' + icon('code') + face(e.agent_id) + (name ? '<b>' + esc(name) + '</b>' : '') +
        '<span class="wl-count">' + list.length + (list.length === 1 ? ' tool call' : ' tool calls') + (failed ? ' · ' + failed + ' failed' : '') + '</span>' +
        '<small class="wl-tools">' + esc(tools.slice(0, 4).join(', ') + (tools.length > 4 ? ' +' + (tools.length - 4) : '')) + '</small><time>' + esc(h.stamp(e.created_at)) + '</time></summary>' +
        '<ul class="wl-call-list">' + list.map(c => '<li class="' + (c.e ? 'is-error' : '') + '"><code>' + esc(c.n) + '</code><span title="' + esc(c.a || '') + '">' + esc(callTarget(c)) + '</span>' +
          (c.e ? '<em>failed</em>' : '') + '<time>' + esc(c.at ? h.stamp(c.at) : '') + '</time></li>').join('') + '</ul></details></li>');
    } else if (e.kind === 'team') {
      const to = h.nameOf && e.target_id ? h.nameOf(e.target_id) : '', from = h.nameOf ? h.nameOf(e.agent_id) : name;
      const route = '<span class="wl-route">' + face(e.agent_id) + '<b>' + esc(from) + '</b>' + (to ? ' → <b>' + esc(to) + '</b>' : '') + '</span>';
      const text = oneLine(e.content), long = String(e.content).length > text.length || /\n/.test(e.content) || text.length >= 140;
      rows.push('<li class="wl-team">' + (long
        ? '<details data-key="team:' + esc(e.id) + '"><summary>' + route + '<span class="wl-text">' + esc(text) + '</span><time>' + esc(h.stamp(e.created_at)) + '</time></summary><div class="wl-full">' + h.format(e.content, e.project_id) + '</div></details>'
        : '<div class="wl-line">' + route + '<span class="wl-text">' + esc(text) + '</span><time>' + esc(h.stamp(e.created_at)) + '</time></div>') + '</li>');
    } else {
      const text = oneLine(e.content, 200), long = String(e.content || '').length > 200 || /\n/.test(e.content || '');
      rows.push(long
        ? '<li class="wl-note wl-long"><details data-key="note:' + esc(e.id) + '"><summary>' + face(e.agent_id) + (name ? '<b>' + esc(name) + '</b>' : '') + '<span class="wl-text">' + esc(oneLine(e.content, 140)) + '</span><time>' + esc(h.stamp(e.created_at)) + '</time></summary><div class="wl-full">' + h.format(e.content, e.project_id) + '</div></details></li>'
        : '<li class="wl-note"><time>' + esc(h.stamp(e.created_at)) + '</time>' + (name ? '<b>' + face(e.agent_id) + esc(name) + '</b> ' : '') + '<div>' + h.format(e.content, e.project_id) + '</div></li>');
    }
  });
  const key = 'log:' + (entries[0].id || '');
  let shown = rows;
  if (rows.length > MAX_ROWS && !(h.expanded && h.expanded(key))) {
    shown = [`<li class="wl-more"><button type="button" class="link-button" data-action="wl-earlier" data-id="${esc(key)}">Show ${rows.length - MAX_ROWS} earlier</button></li>`, ...rows.slice(-MAX_ROWS)];
  }
  return shown.join('');
}
