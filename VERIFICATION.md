# Verification — AgentFlow v2 (runtime 0.3.0), 2026-10-19

## Automated evidence

Latest completed verification (2026-10-19, after the emergency loop stop):

- 145 Python unit/integration tests pass; `compileall` succeeds. Added regression
  coverage for varying-prose loops, repeated tools, shared/restart budgets, peer
  budget transaction rollback, explicit owner renewal, self-wait rejection,
  emergency cancellation, missing-folder grants, PowerShell directory creation,
  dangerous-command cancellation, one-shot approval consumption and 15-minute expiry.
- `tests/rich-content.mjs` (21), `tests/chat-rendering.mjs` (17) and
  `tests/usage-ui.mjs` (10) pass: 48 frontend assertions. Node syntax checks pass.
- Static contract checks: 18 pass. Premium strict audit: zero findings.
- Real Windows MXC broker commands: PowerShell `Get-ChildItem` exits 0; Python
  unittest exits 0 with four checks proving local writes, outside-canary denial,
  hidden `.env` canary denial and loopback-network denial. No real credentials
  were probed. Initial missing platform environment fields were corrected and
  both commands were rerun successfully. No inference was used in these checks.
- Codex quota RPC read successfully observed both 5-hour and 7-day windows, with
  percentages and reset timestamps. Native account metadata was checked around
  the read; no model turn was started. Claude event parsing is verified with
  official-shape fixtures; no new Claude inference was run just for quota data.
- Browser UI-only QA: command card shows Cancel / Allow / session approval;
  Cancel resolves without spawning, Stop all removes pending work. Usage appears
  in connection cards, the Codex dialog and chat. At 390 px the page/tooltip
  remain within viewport, and Escape dismisses the tooltip. Temporary viewport
  override was reset. Existing tables/charts/canvas preview remain functional.
- A runtime reset backs up the SQLite state, sets execution to false, cancels
  unfinished runs/tasks and preserves agents, model choices and native accounts.

Local ignored evidence: `.agentflow/command-usage.jpg`, `.agentflow/usage-mobile.jpg`,
`.agentflow/rich-chat.jpg`, `.agentflow/qa-usage/provider-usage.json`, and
`.agentflow/qa-command/company/probe-tests/test_boundary.py`. The browser fixture
is a separate database with its scheduler/inference disabled.

Earlier verification history follows.

- 87 distinct Python unit/integration tests pass. Coverage includes self-created
  agents/skills and versions, owner-controlled credentials, model routing, nine model turns
  across slices, four peer hops, independent review/revision, natural review
  handoff cycle recovery, durable human decisions, exact checkpoint continuation,
  natural chat answers backed by a real newer owner message, concurrent writes,
  tool replay idempotency, cancel cascade, restart, folder grants/read-only,
  customer isolation, signed/deduplicated webhooks and publishing uncertainty.
- Telegram decision outbox and scoped /answer resume tested through mocked
  transport; conversation IDs cannot answer another conversation's question.
- 18 static contract checks pass, both JavaScript modules parse, Python compiles.
- Premium UI strict audit has zero findings. Static audit is not a WCAG certification.

## Atlas access routing and current account

- Thirteen additional access tests cover worker → Atlas → owner routing, one shared
  project request, approval/denial, exact tool retry, file-only grants, read-only
  upgrades, refusal recovery after restart, tentative/negative consent and verified
  owner chat answers.
- Thirteen account tests cover login/account/workspace changes, logout and checkpoint
  resume, cancelling stale processes, discarding results from an old account,
  native credential-store settings, gateway key replacement, stale verification
  and a slow CLI probe that must not block workspace loading.
- A real Codex catalog probe returned seven models under the current native login.
  A fresh gpt-6-luna inference succeeded using that same login. No live account was
  switched or logged out during QA; account changes are covered by regression tests.
- Restart recovery turns an earlier worker refusal into an open Atlas read-access
  request for a project folder (for example C:\Projects\demo-shop). The request stays
  pending until the owner decides; tests never grant or read a real project folder.
- A separate live Pixel run requested access to the disposable access-check README
  through Atlas. Approving only this QA file in Chrome resumed the same pending
  workspace.read call, returned its actual marker and completed the run. The grant
  was file-only. Persisted evidence is in ignored .agentflow/access-proof.json.

Account tracking stores identity metadata and a fingerprint, never copied tokens.
The native CLI owns login. Codex identity is checked before and after inference;
an account change invalidates the previous catalog and inference verification.
Claude identity comes from official auth status, including organization, and is
checked around inference. Gemini uses its current CLI profile and credential revision.

## Inherited defaults, model switching and Claude discovery

- Seventeen routing tests cover Atlas defaults, private selection/inherit, changing
  the next turn, retained context, recursive descendant policy, plan workers,
  required phase models, unavailable models, quota wait/resume, restart, owner
  commands, customer isolation and native CLI metadata/discovery.
- Windows inspection finds Claude Code installed outside PATH (for example inside an
  editor extension); auth status confirms an existing login.
  SDK initialize returned 13 real model choices, including opus → claude-opus-5-5
  and sonnet → claude-sonnet-5-5. No Desktop cookies or tokens were extracted.
- A real Claude inference returned HTTP/API status 429 with a session-limit reset
  time. Runtime classified a second live probe as
  waiting_quota with its checkpoint intact. Successful Opus/Sonnet inference is
  unverified while that account is capped; their phase routing has regression tests.
- A separate real Codex run completed planning with gpt-6-luna, called models.switch
  and completed coding with gpt-5.6-luna, returning AGENTFLOW_TWO_MODELS_OK in two
  model turns. The run retained its prior context and has both entries in model_history.
  Evidence is stored in ignored .agentflow/model-routing-proof.json.
- Chrome verified saving the existing Atlas default, private Sonnet selection and
  return to inheritance. The model dialog showed all observed Codex/Claude options.
  Narrow layout kept the header picker usable without horizontal document overflow.

## Real subscription-backed collaboration

Codex CLI was exercised using the existing signed-in account and its observed
model gpt-6-luna. Three durable runs completed: Atlas (8 model turns), newly
created Launch Writer (5), and Sage (7). This is real inference, not mock output.

The goal caused skill creation/activation, agent creation with inherited owner
binding, automatic task delegation, a choice question answered in the browser,
file writing, independent review with 4/5, and coordinator summary plus file readback.

Artifacts:
- workspaces/company/qa-launch/brief.md
- workspaces/company/qa-launch/summary.md

The initial live attempt exposed a native CLI/broker instruction conflict. It
was corrected using model_instructions_file and final-message parsing. Another
real interaction exposed a circular reviewer wait; the runtime now recognizes
submission from an actual artifact/handoff and resumes review. Regression tests
cover the cycle. The work continued from persisted checkpoints after restart.

Run scripts/verify_collaboration.py to check the persisted evidence and actual
files. It writes hashes, completed run IDs, model IDs and task scores to ignored
.agentflow/collaboration-proof.json. Runtime data and artifacts are not published.
Codex's observed catalog contains seven entries; only the exercised model is
inference-verified.

## Browser evidence

Checked in Chrome against the local backend:
- Persistent roster updates when an agent is created.
- Inline audience choice submits once, shows the answer and resumes the run.
- Composer draft survives polling, role updates and transcript refresh.
- Work rail shows the real completed task, artifacts and tool outcomes.
- Artifact opens the actual markdown in escaped plain text with a download action.
- Direct specialist chat shows the handoff and reply; it remains a persistent DM.
- Owner default model dialog shows the real seven-entry catalog and concurrency.
- 390 × 844 responsive check: composer remains reachable, roster opens/closes,
  switching DM works, no document horizontal overflow. Viewport reset afterward.
- Existing shared forms, native select, modal focus and close behavior remain.
- Atlas shows the real folder request; Pixel shows a handoff instead of another
  permission form. Multiple cards have distinct field/label IDs. The Codex card
  displays the identity obtained from the current CLI account/read response.

## Dynamic organization, apps and reasoning (19 October)

103 automated tests pass, plus 18 static checks and JavaScript syntax checks.
New cases cover nested arbitrary roles, role/skill replacement, task reassignment
before archive, model/reasoning inheritance and actual native CLI arguments,
broker grants, current credential bundles after account rotation, rejection of
stale results, fixed routes/zone scope, no credential-bearing redirects, private
IP rejection, secret redaction, skill research/install and recipe privilege checks.

Live Codex inference created a three-level organization: CEO → Marketing →
Designer, each child created by its immediate parent. It created two skills,
created/invoked a recipe that wrote an actual marker file, read it back, then
changed Designer's role and attached a suitable new skill. Four runs completed
20 model turns. The coordinator used gpt-6-luna with reasoning=medium through the
actual Codex CLI. `scripts/verify_capabilities.py` checks the persisted chain,
broker results, model history, marker and SHA-256 and writes ignored evidence.
The exercise exposed missing parent_id metadata; agent inspection and team context
now include the actual organization hierarchy. A fresh live follow-up completed
two more model turns and read parent_id directly for all three agents, without
error. QA agents/tools are archived and QA skills disabled after capture.

Chrome verified the model button beside Send, real Codex and Claude reasoning
options after catalog refresh, model-specific option updates, discard behavior,
app catalog, Postman adapter install and account-form validation without entering
credentials or making external app calls. The composer draft survives saving a
model selection. A 390 × 844 override kept the picker directly beside Send with
no document overflow; the override was reset. The test adapter was uninstalled.
Plugin adapters are tested with mock
service responses; authenticated Gmail/Postman/Cloudflare/Hub/SSH/web operations
remain unverified with live user accounts. No external messages were sent.

The existing private Sites project could not be found with the current connected
account. This revision is delivered in the local runtime; no replacement Site was
created and the previous hosted preview was not updated.

## Remaining live integration limits

No Facebook posts or customer messages were sent. Facebook, Messenger, Telegram,
image gateway and Gemini need configured accounts to verify live end-to-end.
Claude metadata, quota handling and native image inference are verified. Docker execution returns an actionable error when Docker is absent;
there was no live Docker test. Memory is bounded text retrieval, not semantic/vector
memory. The hosted private site remains a static, explicitly labeled preview;
the local Python process is the real multi-agent runtime.

## Chat identity, attachments and detail rail (19 October)

116 Python tests, 17 JavaScript assertions and 18 static checks pass. New cases
cover upload content/scope/integrity, native image payloads, Office shared strings,
immutable diffs, used folders, editable/task-linked checklist state, agent identity,
image outputs and suppression of private peer notifications to the owner. Message
escaping/linking is checked against HTML, code blocks, unknown IDs and filenames.

Actual Codex `gpt-6.1-sol` and Claude `sonnet` both correctly identified a synthetic
image's green left/purple right halves, using existing CLI logins with current-account
fingerprint validation. No private project data or external messages were sent.
Ignored proof is in `.agentflow/qa-vision`. Pinned pypdf extracted exact text from a
generated PDF and rejected an empty PDF with scan guidance.

Chrome used an isolated database/workspace on port 8766 to verify mention navigation,
private-message hiding, checklist creation, real add/delete diff and content tabs,
file thumbnails, mobile layout without horizontal overflow, and Close/Escape focus.
Pasting a real PNG through the browser clipboard uploaded the image; sending an
image-only chat cleared the draft and persisted that exact attachment reference
on both the owner message and queued run. The QA run was then cancelled.
The 390x844 viewport override was reset. PNG/text HTTP uploads round-tripped exact
bytes and bound to the submitted chat run. Chrome automation's file chooser transfer
was blocked by the extension's missing Allow access to file URLs permission; chooser
opening was verified, transfer via that UI was not claimed. Owner data/team were
not replaced with fixtures. Screenshots are ignored in `.agentflow/chat-*.jpg`.

## Rich reports, English work policy and managed browser

127 Python tests pass, plus 21 rich-content and 17 chat-rendering JS assertions,
18 static checks and a clean strict premium UI audit. Coverage includes safe rich
rendering, actual file references, finite chart dimensions/data, canvas CSP/auth,
macro/renamed-file rejection, owner-only desktop opening, English authoring, owner
reply-language inheritance, Atlas-owned browser grants, stale/read-only actions,
control-plane blocking, revocation and actual screenshot pixel forwarding.

A live managed Chromium fixture opened an HTTP page, filled Name, selected Channel,
checked Reviewed and clicked Save report. Semantic snapshots confirmed the new
state. Desktop and mobile PNGs were saved and returned as model image inputs.
Evidence is ignored under `.agentflow/qa-rich-browser`.

A real Codex run in an isolated workspace completed seven model turns. It authored
the English Report Verification skill, created an interactive canvas, opened it,
clicked Verify, captured desktop-before/desktop-after/mobile-after PNGs, and closed
the browser. The owner-facing final response was Vietnamese, with a Markdown table
and actual canvas/image paths. No test agent was added to the default organization,
no external site was opened, and no external messages were sent. Full broker results
and output references are in ignored `.agentflow/qa-autonomous-rich/proof.json`.

Chrome confirmed rich chat tables/charts/data expansion, registered file cards,
Markdown rendered/source preview, image galleries, an interactive sandboxed canvas
and expanded canvas view without losing its state. Clicking a PDF card successfully
called Windows's default-app opener. Office native opening is covered at the broker
level; this does not claim Word is installed. A mobile check shows no page horizontal
overflow; the viewport override is reset after testing. Screenshots use the isolated
QA workspace, not fabricated business analytics. Browser state is ephemeral and
authenticated website workflows remain untested with live user accounts.

## Chat composer auto-sizing

Verified the composer in Chrome using an isolated static preview without starting
the agent runtime or using model quota. Eight lines expanded the input from 45 px
to 168 px; a 14,340-character draft stopped at 320 px and scrolled internally.
Replacing the draft with short text returned it to 45 px. A long paragraph restored
after reload retained its text and expanded automatically. Narrow and short viewport
checks kept Send visible without document overflow; at the short viewport the input
adapted to 139 px. The browser viewport override was reset after checking.
Screenshot: `.agentflow/chat-autogrow.jpg`. Virtual-keyboard behavior on a physical
phone was not tested. Validation passed: 145 Python tests, 18 static checks, JavaScript
syntax checks, and the strict UI audit with zero findings.

## English UI, provider status and quota reliability

Read-only native probes on 2026-10-19 returned actual Codex primary/secondary
subscription windows and Claude five-hour usage. Probes used a temporary metadata
store without starting Engine or inference. The current account identities were
checked before saving observations. Claude uses a vendor-internal subscription
endpoint; availability for other plans and credential stores is not guaranteed.

Regression coverage verifies same-account refresh retention, hiding on authentication
revision changes, empty Codex responses, Claude percentage/fraction differences,
partial and repeated Claude events, secret exclusion, disconnected gateways, and
Gemini execution/catalog rejection. Mini bars select the primary subscription
window instead of taking the maximum across unrelated buckets.

Chrome verified English navigation and document language, English required-field
errors, provider options excluding Gemini, disabled Gemini connection controls,
no gateway-refresh button for an unconnected gateway, and category badges on cards
and roster entries. A sample-only agent with the arbitrary Customer Experience
category verified creation and label updates. The narrow viewport showed no page
horizontal overflow. This UI session used a clearly labeled static preview; the
real organization, model defaults and credentials were not edited. Temporary
viewport settings were reset, the tab closed and the preview server stopped.
Screenshots: `.agentflow/english-agent-groups.jpg` and
`.agentflow/english-agent-mobile.jpg`.

Validation: 151 Python tests, 56 frontend assertions, 18 static checks, JavaScript
syntax checks and a strict UI audit with zero findings. Conversation history and
owner-authored content keep their original language.

## Repeated quota refresh and popup clipping regression

Reproduced a real Codex metadata cleanup hang: the request thread waited inside
`proc.stdout.close()` while the daemon reader waited on the pipe. Bounded owned-tree
cleanup and reader-owned stdout closure fixed it. A regression test holds the pipe
open after returning valid quota and confirms the metadata request still returns.
Codex identity tests also cover repeated model/effort edits and same-account OAuth
refresh, actual account switches and safe account-bound restoration after restart.

An isolated read-only server completed three real native quota refresh cycles,
each in approximately 4–5 seconds. All six provider observations retained usage
while refreshing: two Codex windows and one Claude window. No inference or worker
engine was started. Evidence is in `.agentflow/qa-usage-popup/proof.json`.

Chrome showed actual quota in the composer and body-level popup, both Codex windows,
explicit used/left labels and reset countdowns. The popup stayed visible and retained
keyboard focus through polling; Escape closed it. Desktop and narrow-viewport bounds
were inside the viewport with no popup horizontal overflow. Claude usage was also
visible on the narrow screen. Browser overrides were reset after checking.
Screenshots: `.agentflow/quota-popup-desktop.jpg` and `.agentflow/quota-popup-mobile.jpg`.

Validation: 155 Python tests, 63 frontend assertions and 18 static checks passed.
JavaScript syntax checks passed. The previously installed premium audit script is
no longer present in the plugin cache, so that external audit was not rerun for this
change; project-owned static and browser checks were run instead.
