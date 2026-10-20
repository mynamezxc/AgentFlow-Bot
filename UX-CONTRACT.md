# AgentFlow UX contract

Chat references, attachments, checklist and previews are owned by `dist/identity.js`
and `dist/collaboration.js`. Only owner-visible messages appear in the transcript
and roster preview. Do not link names inside source code or execute uploaded HTML.
Immutable diff/content tabs live in the detail rail, not a second dialog. Task-linked
checklist status follows the task. Removal archives after named confirmation.
Attachments do not grant folders; permission requests use Atlas's existing card.

Business sources: `PRODUCT.md` defines local owner access, task lifecycle, publishing, private memory and archive policy. The user's original brief defines the workflow scope. No billing or legal copy is part of this product.

## Canonical UI Map
| Capability | Canonical owner | Source of truth | Allowed variants | Verification |
|---|---|---|---|---|
| Select/Listbox | `field()` in dist/app.js, native select | DESIGN.md | native | browser keyboard and popup |
| Date | `field()` native date/time inputs | DESIGN.md | native | en-US date and schedule test |
| Form | shared dialog form handler and `field()` | this contract | create/edit | CRUD and failure tests |
| Scrollbar | dist/styles.css global baseline | DESIGN.md | geometry only | browser computed style |
| Toast | `toast()` and #toast | this contract | success/error/info | live-region verification |
| CRUD | `api()` and shared submit handler | PRODUCT.md | create/edit/archive | tests/test_runtime.py |
| Rich messages | dist/rich-content.js | DESIGN.md, BROWSER-AND-CHAT.md | Markdown, chart, gallery, file card | tests/rich-content.mjs + browser |
| Artifact preview | dist/collaboration.js right rail | this contract | rendered/source/diff/canvas/image | live browser |
| Desktop open | Outputs.open_native, owner API | PRODUCT.md | PDF, macro-free Office | tests/test_rich_browser.py |
| Quota observations | dist/usage-ui.js + Usage broker | OPERATIONS.md | mini tooltip / settings detail | tests/usage-ui.mjs + native probe |
| Command approvals | Atlas decision card + Commands broker | OPERATIONS.md | cancel / allow once / 15-minute session | tests/test_safety_commands_usage.py |
| Stop and loop review | RunSafety + chat header/decision card | OPERATIONS.md | emergency stop / bounded renewal | unit + browser stop test |

## Navigation and flow ledger
URL hash owns route; URL query owns committed search/filter and page. Creates/edits return to owning screen with active filter retained. Dialog cancel and Escape restore opener focus. Archive is reversible and requires an app-owned named confirmation. Native modal dialog owns focus trap and inert background. No browser alerts/prompts. Global shortcut Ctrl/Cmd+K focuses search.

Lists render 12 items per page; filters reset page. No-results provides clear-search action. Native read-only task table has visible column headings. Kanban retains dropdown status actions as keyboard alternatives. Failure keeps dialog and draft open, inline role=alert with focus. API writes are pessimistic; duplicate submission disabled. Stale version yields 409 and asks reload before retry. Connection loss never changes data backend. Initial loading, pending, empty, error and retry have explicit copy.

## Forms, feedback and asynchronous work
Every form is novalidate, every field has a visible label and aria-describedby error target. First invalid field receives focus and aria-invalid. Toast is one polite live region and never the only copy of errors. Textareas resize:none with comfortable min-height. Long dialog body scrolls while actions remain reachable. Secrets are masked; blank retains existing secret. Chat uses asynchronous durable runs and status polling; cancellation kills the owned child process. No fake streamed text. Chat draft remains on failure. Final AI text is escaped; arbitrary HTML is never rendered.

## Responsive and locale

Specialty badges reuse `agentGroup()` in `dist/identity.js` on agent cards and roster
entries. Labels derive from editable `category` values, including arbitrary custom
departments. No fixed profession list controls creation or labeling.

Provider connections require a successful metadata check; a URL is configuration,
not evidence of installation or connection. Gateway refresh appears only for a
connected gateway. Gemini CLI is temporarily disabled in selection and execution.
Quota observations remain visible during a same-account refresh, are hidden on
account identity changes until identity is confirmed, and retain freshness and
source labels. Model configuration and token refresh do not count as account
changes. Mini bars show all available windows in the default subscription bucket
as percentage left, never a maximum across unrelated buckets. Details also show
percentage used and actual reset times. Expired percentages are not current usage.
The body-level fixed quota popup has one owner in `setupUsagePopovers()`, supports
hover/focus, click-to-pin, Escape and outside dismissal, wraps text and stays within
the visual viewport. `updateUsage()` preserves focus and popup across polling.
English application copy; user and agent conversations retain their chosen language, dates via Intl en-US and timezone Asia/Ho_Chi_Minh. Native popup locale is platform-owned. Narrow layout collapses the sidebar, maintains reachable actions, and permits board horizontal scroll. Keyboard focus remains visible. Reduced motion and forced-colors are supported. Activity timestamps include Vietnam time.

## Verification
`python -m unittest discover -s tests -v` checks CRUD, conflicts, memory isolation, scheduling, signatures, paths, deduplication, automatic plan execution and provider failures. `python scripts/check_static.py` checks localized semantics, syntax owners and token drift. Browser verification covers creation/edit, failure, search empty, chat configuration failure, modal Escape/restoration, native select and narrow layout. Premium audit evidence is `premium-audit.json`.

## Autonomous conversation contract (v2)

Roster and DM transcript are primary. Internal plans, role changes, skill activation and delegation have no approval dialogs. decisions.ask creates a durable inline card; answering resumes its checkpoint. Independent branches continue. Polling preserves composer and decision drafts, open handoff details, focus and reading position. File preview is escaped plain text; HTML/code is never executed. Download links require a real artifact record. Team defaults and credentials stay owner-controlled; agents can select available models and switch by phase.

The same field(), native select, HTML dialog and toast() own folder/model forms. Decision cards have one owner in dist/collaboration.js. Choice/free text share an answer endpoint; duplicate answers return 409. Folder access is one project-level grant, not per-file approval. Missing model configuration creates a persistent card and resumes automatically when configured.

Access requests appear in Atlas's owner conversation, with the actual requested
file/folder and read/readwrite scope. Specialist conversations show the handoff,
not duplicate approval cards. The same decision form owns explicit allow/deny and
preserves its draft during polling. Connections shows current CLI account metadata;
provider/catalog changes refresh the view without overwriting open form drafts.

The composer model picker beside Send reuses field(), native select, HTML dialog, validation
and toast. Atlas owns team defaults; specialist chat owns its initial choice and
inherit action. Phase rules are visible in the same dialog. Choices apply from the
next inference; no pending checkpoint or composer text is discarded. The picker
remains available on narrow screens. Quota wait and missing-model wait are explicit
states; an unverified catalog never implies successful inference.

Supported reasoning levels come from the current provider/account catalog. Model
changes reset the reasoning control to automatic; unsupported models show no
select. Inheritance carries model and reasoning together. The selected level is
passed to the actual provider adapter from the next turn.

Apps/account forms in dist/integrations.js reuse the same controls and feedback.
Secrets are never prefilled. Scope checkboxes distinguish all future agents from
specific IDs, and external write operations from reads. Saving does not claim
successful authentication. Revocation/removal uses a named confirmation. Gỡ app
deactivates grants; reinstall does not restore them. Failed saves keep the draft.
