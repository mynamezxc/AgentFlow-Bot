# Runtime brakes, command permissions and subscription usage

Emergency stop disables execution in SQLite, cancels nonterminal runs and pending
tasks, terminates managed process trees, clears command session grants and cancels
unsent outbox messages. Existing files, conversations, agents, model choices and
native account credentials remain available. Restarting the UI does not re-enable
execution or replay cancelled work.

Inference attempts count before each call, including provider failures and invalid
envelopes. Budgets survive slices and restarts: 24 attempts per run, 64 per goal and
24 work runs per goal. Three consecutive identical operations, three repeated
two-operation cycles, repeated identical failures, four identical responses or
12 tool-free continuation turns trigger owner review. A real owner can cancel or
renew a bounded budget; model calls cannot answer this decision themselves. Waiting
for the current run or its own executing/reviewing task is rejected.

## Folder commands

Directory creation uses `workspace.mkdir` / `project.mkdir` without a shell.
`files.request_access` accepts a missing folder for readwrite access. Atlas displays
the resolved path; only owner approval creates it. A read grant cannot create a
folder or run a writing command. File-scoped access never grants a shell session.

`workspace.execute` accepts argv, a project/grant and relative cwd. Routine coding
commands include read-only git/rg commands, Python unittest/pytest/compileall, Node
syntax/tests and npm build/lint/test/typecheck. PowerShell directory/content reads
use a no-profile, noninteractive shell. Unknown or dangerous commands require
Cancel, Allow once, or Allow all commands in this session for 15 minutes. The last
choice is scoped to this runtime session, project and folder; it expires against
a monotonic clock and never survives restart. An expired session approval cannot
fall back to an unused one-shot approval.

Execution uses `codex sandbox` with an explicit MXC permission profile: root denied,
minimal runtime paths readable, only the approved folder writable, private runtime
and native account stores denied, network disabled. No credentials are inherited
from the broker environment. Shell approval does not disable this boundary. If MXC
is unavailable or blocked by Windows policy, execution fails closed; directory and
text tools remain usable. Compatibility requires a Codex CLI supporting MXC and a
Windows host providing it. Detached children do not survive the foreground MXC
command. Output includes actual exit code, stdout and stderr; nonzero is not success.

## Quota observations

Codex uses the documented `account/rateLimits/read` RPC, including multi-bucket
primary/secondary windows. `usedPercent` is provider quota usage, and `resetsAt`
is epoch seconds. This does not start a model turn. The returned account identity
and current native identity must match before observations are saved.

Claude reads subscription metadata from the vendor-internal
`https://api.anthropic.com/api/oauth/usage` endpoint, at most once per 120 seconds.
Only the broker reads the current native OAuth access token; it sends it solely to
this fixed HTTPS endpoint, refuses redirects and excludes credentials and response
bodies from errors. This internal endpoint is not a stable public API and may be
unavailable for other credential stores, plans or versions. In that case the UI
retains confirmed data and explains the limitation. No model is called as a fallback.
Subscription endpoint `utilization` is already a percentage, while native
`rate_limit_event.rate_limit_info.utilization` is a fraction. Native stream events
remain supported during normal work. Missing windows are never invented. Quota is
separate from per-response token counts and estimated API cost. A read-only probe
on 2026-10-19 confirmed actual Codex and Claude quota windows for the current accounts.

Caches and quota brakes are bound to the native account fingerprint. Changed
accounts hide previous observations during identity checks. Expired windows show
awaiting confirmation, never an invented zero. Freshness, source, reset times in
Asia/Ho_Chi_Minh and unknown states appear in chat tooltips and connection settings.
A quota error without an actual reset timestamp does not schedule blind retries.
Native account credentials remain owned by each CLI. Same-account background refresh
keeps confirmed observations visible. Codex identity revisions use the native account
scope and user subject plus authentication-related settings. Model/effort edits,
file mtimes and same-account OAuth refreshes do not hide quota. A real account/scope
change hides previous observations until identity is confirmed. Restart can reuse
confirmed Codex quota only when the persisted revision matches the current local
identity. Missing windows in partial responses retain their own observation times
and are explicitly marked as last confirmed, rather than fresh.

Mini bars display each available window in the provider-selected default bucket,
including weekly windows, as an explicitly labeled percentage **left**. Details show
both remaining and used percentages, window duration, reset countdown, absolute
Vietnam reset time, source and freshness. Other buckets remain separate in details.
Popup content is portaled to the document body to avoid composer overflow clipping,
clamped to the visual viewport, wraps long text and scrolls vertically. Hover/focus
opens it, clicking pins it, Escape/outside click closes it. Polling updates content
without replacing the focused trigger or closing an open popup.

Metadata process cleanup kills only its owned process tree, waits with a timeout
and lets the stdout reader close its own stream. Closing BufferedIO stdout on the
request thread while a reader waits on an inherited pipe previously blocked quota
refresh indefinitely. Both Codex and Claude catalog probes use bounded cleanup.

9Router is optional and is never reported as locally installed by inference from a
URL. Its connection state requires a successful `/models` metadata response for the
current gateway configuration. Fresh workspaces have no default gateway URL. Existing
owner URLs are retained as configuration, not treated as a connected service. Gemini
CLI is temporarily disabled in the model catalog and execution path.

Application controls and feedback use English (en-US). Chat replies still follow
the owner language. Stored conversations and owner-authored names are not translated.
Agent specialty badges use editable categories rather than a fixed profession list.

Primary references checked on 2026-10-19:

- [Codex app-server account/rateLimits/read](https://developers.openai.com/codex/app-server)
- [Codex permission profiles](https://developers.openai.com/codex/permissions)
- [Windows MXC sandbox](https://developers.openai.com/codex/windows)
- [Claude Code status line quota fields](https://code.claude.com/docs/en/statusline)
- [Published Claude SDK rate-limit parser](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/message_parser.py)
- [9Router Codex quota parsing](https://github.com/decolua/9router/blob/master/open-sse/services/usage/codex.js)
