---
version: v2
name: AgentFlow
description: An English-language operations studio for a collaborative marketing agent team.
colors:
  primary: "#7055d8"
  background: "#f6f7fb"
  surface: "#ffffff"
  ink: "#20283f"
  muted: "#697389"
  sidebar: "#f4f4f6"
  border: "#e8eaf1"
  success: "#238267"
  warning: "#a86416"
  danger: "#c34252"
  lavender: "#eeeafb"
  chart-teal: "#318f86"
  chart-amber: "#db9860"
  chart-rose: "#b66592"
  chart-blue: "#488dc2"
  chart-olive: "#8a9c51"
typography:
  sans:
    fontFamily: '"Be Vietnam Pro", "Segoe UI", sans-serif'
  display:
    fontFamily: '"Manrope", "Be Vietnam Pro", sans-serif'
  mono:
    fontFamily: '"Cascadia Code", Consolas, monospace'
rounded:
  DEFAULT: "0.75rem"
  sm: "0.5rem"
  md: "0.75rem"
  lg: "1rem"
spacing:
  section-gap: "1.5rem"
  page-max: "96rem"
components:
  button: {}
  card: {}
  dialog: {}
  field: {}
  toast: {}
  avatar: {}
---

# AgentFlow Design System

Quota UI uses `dist/usage-ui.js` in connection cards/dialogs and beneath the chat
composer. Mini bars reuse primary/border/muted tokens, open details on hover/focus,
and support click-to-pin and Escape dismissal. Each available default-bucket window
shows a labeled percentage left; details also show percentage used. Popup content
lives in a fixed body-level portal, wraps long text, has viewport-bounded width and
height, and scrolls internally. Polling preserves the trigger node, focus and open
popup. Unknown or expired windows never render a zero bar
as a measurement. Show source, freshness and resets in Asia/Ho_Chi_Minh. Connection
cards keep paragraphs at natural height and align their actions at the bottom.
Command and loop decisions reuse chat decision cards and canonical buttons/toasts.
Emergency stop is a visible chat header action and takes effect immediately.

## Overview

### Creative North Star
A calm English-language team messenger. A quiet gray persistent roster surrounds live messages, handoffs, questions and real artifacts. Chat takes priority over dashboards.

The composer model button sits directly beside Send. Atlas sets the team's
inherited default; other conversations can override it or return to inheritance.
The same native dialog shows observed models, supported reasoning levels and
phase rules. Reasoning updates when the model changes; never offer invented
levels. Keep this control accessible on mobile. Roles and category labels are
editable text and follow the organization the owner builds through chat.

### Product context and register
- Audience and primary job: Vietnamese business operators coordinating marketing, SEO, content and customer support agents.
- Market evidence: the user's Vietnamese brief; no assumptions about regulated business domains.
- Locale: en-US, Asia/Ho_Chi_Minh; retain provider names and technical identifiers.
- Usage: frequent desktop operations, mobile checks. Product/admin register.
- Signature: persistent role-colored roster, inline handoff events and owner decision cards.
- Restraint: familiar task boards, text labels, readable forms; no decorative charts or fabricated performance metrics.
- Anti-references: neon terminal dashboards, generic gradient KPI walls, oversized landing-page hero sections.
- Token mapping: frontmatter values map to `dist/styles.css :root` with the same names. CSS is the runtime adapter. The documented values and CSS are updated together.

## Colors
Purple primary identifies actions and selection. A light gray roster anchors the chat workspace. Neutral surfaces hold work. Success, warning, and danger always include text. Light theme only. Forced colors retain OS focus and borders.

## Typography
Manrope for restrained headings and numeric counts, Be Vietnam Pro for body and controls, Segoe UI fallback supports Vietnamese offline. Body 13px/1.65; page heading 27px/1.3; section heading 16px/1.4. Monospace only for paths, model IDs, and integration identifiers.

## Layout
268px light gray roster; 50px utility header; 79px conversation header. Transcript max-width 790px; composer remains anchored below its own scroll area. Optional 350px work/artifact rail becomes an overlay under 1100px. Under 760px the roster becomes an inert drawer. Admin screens retain shared fields/dialogs and paginated lists.

## Elevation & Depth
Border-led white cards. Subtle shadow only on floating dialogs and menus. No blur on working surfaces.

## Shapes
Cards use 12px radius, controls 8px, role avatars 12px, pills fully round. Icons use 1.7px strokes.

## Components

### Foundational visual states
Shared button and field styles own hover, focus, active, disabled, read-only, busy and invalid states. Visible 3px purple focus outline. App-owned spinner for loading, stable button geometry. Global scrollbar tokens apply to all overflow.

### Buttons and actions
One purple primary action per area. Quiet secondary bordered buttons. Danger reserved for deletion; routine pause is reversible. Busy buttons retain label width and are disabled.

### Navigation and data display
Roster selection is muted lavender on light gray. Header breadcrumbs identify workspace. Native tables for task summaries; cards for agents and skills. Counts derive from stored records. Demo data is visibly labeled throughout.

### Forms and overlays
Shared `field()` helper renders labels and inline errors; native select and native datetime/time pickers intentionally accept OS popup geometry and locale. Shared HTML dialog owns focus, Escape, inert background and restoration. One live-region toast. Errors remain inline and drafts survive failure. Secrets are password inputs and never returned by the server.
The default toast pairs the dark ink background with surface-white text; it does
not inherit the light sidebar background. Error toasts keep white text on red.
App account forms use the same field/dialog owners. Uninstalled, installed,
configured, authorized and verified are distinct states. Account grants list
agents and operations; write operations have a visible label. Credentials remain
blank on edit. Replacing credentials replaces the complete bundle.

### Iconography
Project-owned SVG line icons, 18px normal and 20px navigation. Decorative icons are aria-hidden; icon-only controls have English accessible names.

### Motion
120–180ms hover transitions. Heartbeat uses a small status indicator without an infinite pulse. Reduced motion disables nonessential transitions.

### Content and data visualization
Direct English action verbs. Provider status distinguishes installed, configured, connected, and inference verified. Dates and times explicitly use Vietnam time. No generated analytics.

## Do's and Don'ts
- Do carry each agent's role color across cards, chat and tasks.
- Do retain the same create/edit/feedback behavior across screens.
- Don't claim a configured integration is connected before verification.
- Don't show an agent as working unless a real run exists.

## Conversation states (v2)

True progress arrives as complete message events, never a simulated typewriter. Polling preserves composer and decision drafts, focus, open handoff details and reading position. Owner model selection is secondary, outside the autonomous work path. Role identity persists across reassignment.

## Chat identity and task detail rail

Twelve code-native SVG faces and twelve colors follow each agent across profiles,
the roster, task owners and chat mentions. `@{agent_id}` renders a colored face/name
button opening the agent's conversation. Literal names match outside code; all
message/file text is escaped. Internal peer messages and handoffs stay hidden in
owner transcripts and previews. Explicit `chat.send audience=owner` reports are visible.

The composer plus selects attachments with thumbnails, extraction notices, removal,
pending and failure states. Paste images/drop files share that uploader. Drafts stay
scoped to each conversation and survive failed sends.

The chat textarea grows upward with wrapped text and explicit newlines, shrinks
when content is removed, and restores its size with each conversation draft.
`resizeComposer()` in dist/collaboration.js owns sizing: at most 320px and bounded
by the conversation's remaining height. Longer drafts scroll inside the textarea;
attachments have their own bounded list. The composer does not shrink beneath its
content or push Send below the conversation viewport. Very short viewports scroll
the bounded composer area. Resizing the window/detail rail recalculates wrapping.

The 350px rail has independently collapsible Progress, Outputs and Context.
Progress is a vertical checklist: task, assignees, actual state and note. Add/edit,
reorder and soft removal use the standard dialog. Task-linked state follows runtime.
Outputs preserves immutable text revisions: line numbers, add/delete colors, full
content tab, revision download and local image previews. Context distinguishes used
folders from grants and routes permission requests to Atlas. On mobile the rail
is an overlay with Close/Escape; the composer and model picker remain accessible.

## Rich reports and interactive artifacts

`dist/rich-content.js` owns escaped Markdown, bounded semantic tables, SVG charts,
registered file cards and image evidence galleries. Cards use the existing surface,
border, radius and body type; content takes priority over ornamental framing.
Charts use primary plus chart-teal/amber/rose/blue/olive, defined in the canonical
`:root` of dist/styles.css and mirrored above. Labels and an expandable data table
carry meaning independently of color. Tables own bounded overflow inside a message.

Markdown files have rendered/source tabs. HTML is source-only unless explicitly
created as a canvas. Canvas uses an opaque sandbox and a dedicated restrictive CSP;
its local controls run without access to app cookies or parent DOM. The same rail
can expand for substantial interactive work. Its controls stay reachable while
scrolling and polling does not remount an unchanged canvas. PDF/Office cards call
the desktop opener only from an owner click; an app-owned error and download remain
available when no application is associated. Images open at readable size in the rail.

The application UI is English (en-US), including accessible labels and feedback.
Stored user content and conversation history retain their original language.
This is separate from the agent language
policy: English internal work, English authored skills/tools and English default
names; owner chat follows the owner's language or explicit request, default English.

## Layout stability and compact rails (v2.1)

Every admin page keeps one geometry while its tabs, filters or pages change: `.content` is full width and
top-aligned (never `margin:auto` inside the flex column, which centred short pages and shrink-wrapped their width),
`.co-shell` reserves its scrollbar (`scrollbar-gutter: stable`), tabs keep one font weight, cards share heights
(clamped descriptions, bottom-aligned actions) and lists sit in a `.list-region` with a minimum height so empty
states do not collapse the page. Same-route renders patch the DOM (`morph()` in `dist/layout.js`); a tab, filter
or page change brings the tab bar back into view only when it scrolled away.
The usage strip under the composer is one line: `Codex 5h ▰▰▱ 62% · Claude 5h ▰▱ 78%`; a weekly window joins it
with a warning icon only when under 10% is left (`selectWindows()` in `dist/usage-ui.js`); every window stays in
the popover. Last confirmed values stay visible between polls (tiny refresh dot, reserved slot).
Roster rows are ~40px (28px face, name and category chip on one line; a muted second line only while working or
waiting). Above eight agents, categories with two or more agents get collapsible headers; one-off categories stay
in a flat list. The right bar lists Folders used as one line each (last two segments, R / RW badge, four then
"Show all") and changed files as compact rows (repeated edits grouped with a count, images left to the gallery,
six then "Show N more"). The lightbox fits any image into the stage; "Actual size" scrolls inside it.

## Image generation (v2.1)

`dist/image-studio.js` owns the Connections > Image generation panel and the Image Studio dialog
(`openImageStudio({agentId, projectId})`, also reachable from the composer image button and the
Image card). Pickers are native radio inputs styled as chips: aspect-ratio chips draw a small
rectangle in the real proportion, resolution/quality/format are text chips, style presets are
icon cards, count is a stepper. Only values the active engine supports are offered; engine
availability shows the server's reason ("Add a Gemini API key"). Engine mode is a two-option
segmented control (agent's current model, recommended, or one fixed engine). When an engine is
fixed in Settings the Studio's engine/model controls are disabled with an explanation. Under 700px
the Studio and the settings dialog become bottom sheets. The Gemini card (`dist/gemini-ui.js`)
shows installed/connected/auth/key/model-source status, Sign in with Google, and a write-only API key.
