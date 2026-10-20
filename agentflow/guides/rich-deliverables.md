---
name: rich-deliverables
description: Present verified work as Markdown, charts, image evidence and sandboxed interactive canvases.
---

# Rich deliverables

Use when a comparison, trend, UI review or reusable interactive result is clearer
than a prose-only reply. Keep internal planning and reusable instructions in English.
Owner reports follow the owner's language. Preserve original source data.

1. Read the task and actual results. Separate observations, estimates and scenarios.
2. Use GFM pipe tables for comparisons; maximum 20 columns and 200 visible rows.
3. Use a fenced `chart` JSON block for bar/line/pie data. Use the Chart.js-style
   `data.labels` and `data.datasets: [{label, data}]` structure. Add title, unit and
   source. Verify values, dimensions and labels; do not invent analytics.
4. Write files with workspace.write. Link registered files using ordinary Markdown:
   `[Launch brief](reports/brief.md)`. Markdown/text/code open inside the detail rail.
   PDF/DOCX/XLSX/PPTX open through an explicit owner action in the desktop app.
5. Use real generated/screenshot paths in `![caption](media/image.png)` or fenced
   `images` JSON arrays. Show before/after evidence with captions and concrete findings.
   Your recent generated images/screenshots reach your next turn as actual pixels.
   To review another teammate's current-project image, call outputs.view_image first.
6. For interactive work call outputs.canvas with self-contained .html, inline CSS/JS
   and data images, then link that real file. No network, parent access, storage,
   forms, popups or navigation. Use buttons/range inputs for local calculations.
7. Verify the artifact exists, preview it, check desktop/mobile and summarize limits.

Expected outputs: an evidence-backed report and actual linked artifacts. Never
present a code block containing a filename as though the file had been created.
