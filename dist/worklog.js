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

/** One-line summary text for a group of work-log messages. */
export function logSummary(entries) {
  const actions = entries.reduce((n, e) => n + actionsOf(e).length, 0), notes = entries.filter(e => !actionsOf(e).length).length;
  const parts = [];
  if (notes) parts.push(notes + (notes === 1 ? ' update' : ' updates'));
  if (actions) parts.push(actions + (actions === 1 ? ' auto-approved action' : ' auto-approved actions'));
  return 'Work log · ' + (parts.join(' · ') || '0 updates');
}

/** h = {esc, icon, stamp, format(text, projectId), agentName(id)|null} */
export function renderWorkLog(entries, h) {
  const {esc, icon} = h;
  const rows = [];
  entries.forEach(e => {
    const who = h.agentName ? h.agentName(e.agent_id) : '';
    const acts = actionsOf(e);
    if (acts.length) {
      acts.forEach((a, i) => rows.push('<li class="wl-action"><details data-key="act:' + esc(e.id) + ':' + i + '"><summary>' +
        icon(ACTION_ICONS[a.type] || 'spark') + '<span class="wl-badge">Auto-approved</span><code class="wl-label" title="' + esc(a.label) + '">' + esc(a.label) + '</code>' +
        '<time>' + esc(h.stamp(e.created_at)) + '</time></summary>' +
        '<pre class="wl-detail">' + esc(a.detail || a.label) + '</pre>' + (a.cwd ? '<small class="wl-cwd">in ' + esc(a.cwd) + '</small>' : '') + '</details></li>'));
    } else {
      rows.push('<li class="wl-note"><time>' + esc(h.stamp(e.created_at)) + '</time>' + (who ? '<b>' + esc(who) + '</b> ' : '') +
        '<div>' + h.format(e.content, e.project_id) + '</div></li>');
    }
  });
  const latest = entries[entries.length - 1], lastActs = actionsOf(latest);
  const preview = (lastActs.length ? lastActs[lastActs.length - 1].label : String(latest.content || '')).replace(/\s+/g, ' ').slice(0, 140);
  return '<details class="work-log" data-key="log:' + esc(entries[0].id || '') + '"><summary>' + icon('down') + '<span>' + esc(logSummary(entries)) +
    '</span><small>' + esc(preview) + '</small></summary><ol>' + rows.join('') + '</ol></details>';
}
