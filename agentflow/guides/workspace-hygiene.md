---
name: workspace-hygiene
description: Keep folders organized, write clear reports, attach deliverables and delete only your own temporary files.
---

# Workspace hygiene

1. Organize: `projects/<slug>/{reports,data,scratch}` in the company workspace
   (in a granted project folder, follow its existing layout first). Reuse folders;
   do not scatter files in the root.
2. Name files `<topic>-<what>-<YYYY-MM-DD>.md`, e.g. `reports/pricing-review-2026-10-19.md`.
   Never overwrite an owner file: write a new one beside it.
3. Write the final report as Markdown: outcome first, then evidence, tables and
   charts per rich-deliverables, risks, next steps. Keep one current report.
4. Put drafts, scraps and intermediate data in `scratch/`. When the work is done,
   remove them with workspace.delete / project.delete (a recoverable trash).
   Company workspace: only files you created or anything under `scratch/`. A folder
   the owner granted readwrite: any file the task requires (e.g. obsolete code);
   read-only grants refuse. Delete only what the task needs; never to hide mistakes.
5. When finished, attach the report and key deliverables to your final reply with
   the envelope field `"files":["projects/x/reports/final.md"]` (max 10 existing
   paths; each is registered and sent to the owner, also on Telegram).
6. If a procedure will recur (report format, folder cleanup, data export),
   capture it once with skills.create (or improve it with skills.update) in
   English, with inputs, steps and verification.
