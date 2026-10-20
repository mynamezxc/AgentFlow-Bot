# Agent output and browser protocol

These capabilities use the same broker contract for Codex, Claude and compatible
gateways. The model continues to return one JSON envelope; rich content belongs in
its `reply`, `messages` or `chat.send.text` strings. No special provider API is required.

## Reports

Use standard Markdown headings, code fences, GFM pipe tables and file links. Raw
HTML is escaped in chat. Tables show at most 20 columns and 200 rows in their own
scroll area. Plain URLs may open HTTPS sources; remote images are not embedded.
Files resolve only against actual registered artifacts or owner attachments.

```markdown
| Check | Result |
| --- | --- |
| Input control | Passed |

[Verification notes](reports/verification.md)
![After the fix](media/screenshot.png)
```

For a gallery, use a fenced `images` JSON array of actual paths, or objects with
`path` and `title`. A fenced `files` JSON array renders file cards. References may
also use `artifact:ID` or `attachment:ID`; ordinary relative paths are preferred.

For charts, use a fenced `chart` JSON block:

```json
{"type":"bar","title":"Observed results","unit":"checks","source":"Actual test run","data":{"labels":["Input","Click"],"datasets":[{"label":"Passed","data":[1,1]}]}}
```

Supported types: bar, line and pie. Use 1–40 labels and 1–6 datasets with matching
finite numeric values. Pie requires one non-negative dataset and a positive total.
The renderer uses SVG plus an accessible expandable data table. Label assumptions
and estimates; do not invent metrics merely to produce a chart.

## Files and canvases

`workspace.write` stores text/code and a real diff. `outputs.register` registers
existing safe files in the company workspace (text/code, images, PDF and macro-free
DOCX/XLSX/PPTX). It does not generate binary document formats or grant folder access.

Markdown/text/code opens in the right rail. Markdown includes rendered/source tabs;
HTML/code stays escaped. Images include generated images and browser screenshots,
shown in galleries and Outputs. PDF/Office cards request the OS default application
on the runtime machine when clicked. No association yields an inline error and
download fallback. Only owner HTTP actions can launch apps; models cannot invoke
the desktop opener. A verified copy is opened, never an arbitrary command or URI.

Recent images generated or captured by a run also reach its next model inference.
`outputs.view_image` selects a current-project image for another teammate's visual
review; the broker forwards pixels, not a filename-only substitute.

`outputs.canvas` writes a self-contained `.html` artifact. Its link opens the canvas
in the expandable right rail. The iframe has `sandbox="allow-scripts"`, no same-origin
permission, and a restrictive response CSP. Inline scripts/styles and data images
work; parent DOM/cookie access, fetch connections, external resources, forms, popups
and top navigation are restricted. Do not place secrets or external-service code
in a canvas. It is a local interactive artifact, not a deployment target.

## Browser tools

The broker uses Playwright Chromium in a separate ephemeral context per project.
It never imports the owner's Chrome/Claude/Codex profile, exports cookies or exposes
arbitrary JavaScript, shell, file chooser, password or OTP controls. It is a managed
headless browser, not remote control of the owner's existing tabs.

1. `browser.install` prepares pinned `playwright==1.64.0` and Chromium if needed.
   It uses only the fixed official package/browser installer, without model-supplied
   shell commands. Network/pip access is needed for first setup. An already-installed
   compatible Playwright/Chromium can be used without reinstalling.
2. `browser.open` opens a URL or a current-project canvas artifact. Missing URL-origin
   access goes through Atlas to the owner. Grants distinguish read and interaction.
   Read grants block non-GET/HEAD requests. The AgentFlow control plane and link-local,
   multicast and unspecified targets are blocked (see "Grants, domains and host map").
3. `browser.snapshot` returns URL, title, semantic controls and a snapshot ID.
4. `browser.act` accepts that latest ID and an exact role/name, label or test ID.
   Actions: click, fill, select, check, press, scroll. Each returns a fresh snapshot.
   Stale/ambiguous controls fail rather than acting on a guessed element.
5. `browser.screenshot` saves actual PNG evidence to Outputs. The latest three images
   from that run reach the next model inference through the existing vision adapter.
6. `browser.close` releases the project context. Context also lets the owner revoke
   an origin grant and close the session; revocation is not a model tool.

Use the bundled English browser-workflow guide for before/after UI review and
desktop/mobile checks. Origin interaction permission does not authorize publishing,
payments, security changes, deletion or external messages; the actual consequential
action still requires the owner's specific decision. Browser state is ephemeral:
after restart reopen and inspect, never replay an interrupted action blindly.

## Grants, domains and host map

A project grant is either an exact origin (`{"origin": "https://app.test", "access": ...}`) or a
domain wildcard (`{"domain": "*.shop.example", "access": ..., "schemes": ["https","http"]}`). A wildcard
covers the apex and every subdomain on default ports; IP literals and `localhost` only match an
exact origin. One helper, `browser.covers(grants, url, access)`, decides this for requests, the route
guard, `browser.act`, revocation and `web.fetch`. The registrable domain is the last two labels, or
three under common public suffixes (`com.vn`, `co.uk`, `com.au` ...).

Owner decisions are generic action buttons: `allow_site` ("Allow <origin>"), `allow_domain`
("Allow all of *.<domain>", shown first for subdomains or when the project already has a grant on the
domain) and `deny`. Open requests are deduplicated per registrable domain. Atlas never auto-answers
for the model.

Route guard: navigations (main frame and iframes) and every non-GET/HEAD request need a covering
grant (interact for writes). Plain GET/HEAD subresources (CDN scripts, fonts, API hosts) may reach
public addresses (`settings.browser_public_assets`, default true); private/loopback addresses need
a grant. A redirect to an uncovered origin fails with an error naming that origin.

Name resolution uses a cached resolver (60 s) and `settings.browser_host_map`
(`{"*.shop.example": "127.0.0.1"}`; patterns are an exact host or `*.domain`, values are IPv4/IPv6
literals). The map is applied to the resolver and to Chromium (`--host-resolver-rules`); changing it
restarts the browser on its next use. Failures are explicit ("Cannot resolve demo.shop.example. If this is
a local site, map it in Browser host map ..."). When a granted subdomain fails DNS but its apex
resolves to a loopback/private address, the owner is offered "Map *.shop.example -> 127.0.0.1 for the team
browser". `POST /api/browser/host-map {pattern, address|null}` edits the map.

## Approval mode

`POST /api/approval-mode {channel, mode}` stores `settings.approval_modes[channel]` (`ask` default,
`auto`). A run resolves it from `project.channel`, then `owner_channel`, then `channel`. In `auto`,
commands (secret-path checks, folder grants and the OS sandbox still apply), browser access and host
mapping are approved without a question; the owner sees one concise "Auto-approved: ..." info message,
coalesced per 60 s. Folder access, credentials and the AgentFlow control plane are never auto-approved.
Command decisions offer Cancel / Allow / Allow all commands for 15 minutes / Allow all commands for this
task (valid until the root run finishes).

## Language policy

English is the working language for internal coordination, planning notes, default
names/roles, code/comments, authored skills and tool documentation. Owner-facing chat
uses the latest explicit language request or the owner's message language; default
English. New authored skills and recipe metadata reject Vietnamese instructions with
guidance to retry in English. Source data, existing organization names, installed
source documents and requested localized deliverables are preserved. Private model
reasoning is not exposed or audited; this is a prompt/work-product language policy.

Browser API implementation follows the official [Playwright library guide](https://playwright.dev/python/docs/library),
[locator reference](https://playwright.dev/python/docs/api/class-locator) and
[context routing reference](https://playwright.dev/python/docs/api/class-browsercontext).
