# AgentFlow product contract, version 2 (runtime 0.3.0)

Authority: the owner's 2026-10-18 correction and Grok Bot reference screenshots.

2026-10-19 additions: rich chat uses standard Markdown tables/code/file links,
bounded chart JSON, real image galleries and self-contained interactive canvases.
Markdown/text/code previews live in the right rail; owner-clicked PDF and macro-free
Office documents use the runtime machine's default application with download fallback.
Models cannot launch desktop applications. Browser automation uses a managed separate
Playwright context, project-origin grants requested by Atlas, semantic controls and
actual screenshot evidence fed back into the next inference. Owner revocation removes
origin access and closes its session. Existing signed-in browser profiles are not imported.

English is the default internal working language: coordination, planning notes, code,
default names/roles, authored skills and tools. Owner chat follows the owner's message
language or explicit request, default English. Existing organization names/source data
are preserved; there is no automatic rename or destructive skill migration.

The primary interaction is an ongoing conversation with a persistent teammate.
One goal authorizes reversible internal execution: planning, delegation, creating
or changing roles, installing/authoring skills, research, artifact creation,
independent review, revisions and routines. There is no owner approval gate for
internal plans or agent creation, and no two-hop/four-turn ceiling.

Agents have durable inboxes, identities, instructions, skills, private memories
and deliberately shared knowledge. They see the roster, responsibilities, project
tasks, artifacts, decisions and recent team messages. Different agents run
concurrently; one actor processes one inbox item at a time. Waiting releases
the actor so it can answer other conversations.

Tasks run when dependencies finish, then a distinct reviewer approves or requests
revision. Feedback returns to the worker's private memory. Review can repeat.
Agents can change their own or another agent's role and instructions, with
revisions retained, without changing the owner's model binding.

Human judgment is a durable decision: choice, input, folder access or approval.
Only dependent work waits. Answers wake the exact checkpoint. Agents can send
multiple messages and ask further questions; incoming chat is queued while they
work. Owner cancellation can stop the whole descendant branch.

Team defaults and credentials remain owner-controlled. The Atlas chat model picker
sets a live default for inheriting agents. Private chat picks an agent's initial
model, or returns to inheritance. Models are not immutable agent assignments.
Internal agents see the observed catalog, choose initial models and call
models.switch to change the next inference at the same checkpoint. Per-phase
analysis/planning/coding rules and descendant policies are durable; agents created
directly or by plans inherit a parent's explicit child rules recursively. A mapped
phase cannot silently use a different model. Model choice never changes credentials.
Catalogs are observed, never fabricated; entitlement requires real inference.
Use authenticated CLI or an owner-managed gateway; no browser-cookie extraction.
Codex and Claude account identity is checked around each inference. Account/workspace changes
invalidate prior catalogs and verification, cancel old processes and discard stale
results. Logout waits at the existing checkpoint. Native CLIs own credentials;
account metadata contains no tokens. Native Claude quota responses preserve the
checkpoint and wait until the reported reset, or until a model/account change.

Heartbeat scans every 30 seconds without inference while idle. The inbox pump
dispatches every 0.5 seconds. Default concurrency is four actors, owner-adjustable.
Six model turns per slice is a scheduling checkpoint. Durable budgets cap each run
at 24 inference attempts and its entire goal at 64; a goal can create 24 work runs
before owner review. Repeated operations, short operation cycles and 12 tool-free
continuation turns halt the goal. Only a structured owner decision renews the budget.
Emergency stop cancels queues/tasks/process trees and disables execution persistently.

SQLite WAL persists model checkpoints, pending calls, tool ledger, decisions and
inboxes. Internal mutations and tool results share a transaction. Replay uses a
stable run/step/call key. Restart resumes checkpointed work; interrupted image
or container actions are marked unknown rather than blindly repeated.

Local single-owner backend binds 127.0.0.1. Human team accounts/RBAC are outside
this version. Company files are available to internal agents. Specific project
folders are granted once by the owner, read or read/write, shared by the project
team. Hidden files, secrets, symlinks and traversal remain excluded. Execution
uses a native Windows MXC sandbox with restricted folder reads/writes and no network.
Runtime storage, account stores and known secret paths are denied. Routine coding
commands run in an authorized folder; other commands request Cancel/Allow/15-minute
session approval through Atlas. Approval never widens the OS boundary. No unrestricted
host fallback is permitted. Directory creation has a narrow file broker without Docker.
Owner approval of a missing readwrite folder creates that folder before granting it.
Missing file access is routed by the runtime from the worker to Atlas, who asks
the owner in chat. Requests are deduplicated by project, path, scope and access.
Approval resumes all dependent checkpoints and retries the actual pending tool.
Denial continues within existing permissions. A file-only grant never includes
its siblings; a read grant never implies write permission. Plain-text workspace
refusals and existing completed refusals are recovered into this same flow.

Skill instructions from local Codex/Claude, GitHub SKILL.md and open repositories
can be installed by agents. New skills become active immediately; updates retain
the previous version. Installation does not execute third-party setup scripts.
Skill content cannot change account settings or folder grants.

New workspaces start with only Atlas, a general operator. Existing organizations
are retained. No profession/category registry limits roles. Any internal agent
can build a team, replace another role's instructions/skills/tools, or archive a
member with explicit reassignment of unfinished work. History/memory is retained.
Skills can be researched from official Anthropic/OpenAI/Google catalogs or a
user-provided link and authored/installed during work. Instruction installation
does not execute vendor scripts. Agent-authored tools are versioned broker recipes
with bounded steps, input references and repeated capability checks, not host code.

The composer model control includes reasoning levels reported by the current
catalog. Atlas defaults propagate both to inheritors; private choices remain
independent. Native adapters transmit actual reasoning configuration.

Owner-installed app adapters include Gmail, Postman, Cloudflare, Hugging Face,
SSH and fixed web/API routes. Credentials are broker-only and encrypted by DPAPI
on Windows; account metadata/operation grants are separate. Agents discover and
invoke granted operations but cannot install adapters, read vault values, alter
destinations or grant themselves access. Credential replacement clears the old
bundle. Account edits invalidate in-flight results; revocation prevents new calls,
not remote side effects already accepted. External operations explicitly granted
by the owner are standing authorizations until revoked. See APP-CONNECTIONS.md
for operation scope, OAuth/dependency setup and the local process threat boundary.

Public publishing, external replies, spending and destructive actions retain
channel-specific owner authorization. Facebook drafts are reviewed before
publication; uncertain results require reconciliation. Messenger support receives
shared public knowledge and the customer conversation, without internal admin
tools or private worker context.

The private Sites deployment is an explicitly labeled static preview. The local
application is the autonomous runtime. Preview never simulates successful AI calls;
backend disconnection never silently changes to demo storage.
