# NinjaScope — Follow-up: Interactive mode & per-task compiler profiling

Future improvement discussed on 2026-07-14. Not part of the initial implementation, but the
current tool is being built so this slots in later without rework.

## The idea

Two launch modes sharing the same HTML report:

- **Static mode** (current plan): `ninjascope.py <builddir> -o report.html` — pure artifact,
  opens from disk, no server. What the talk demo uses.
- **Interactive mode** (future): `ninjascope.py --interactive <builddir>` — same page, but backed
  by a live Python process. Extra buttons appear (e.g. **"Profile this task"** on a compile step,
  **"Profile whole build"** in the header) that re-run actions natively and feed results back
  into the page.

## Architecture: one page, optional bridge

- Generate the exact same HTML in both modes. At startup the JS feature-detects a native bridge
  (`window.pywebview?.api` or an open WebSocket) and only then renders the interactive buttons.
- Bridge options (evaluated 2026-07-14):
  - **pywebview** — best "tool window" fit. Native window via Edge WebView2, `js_api` exposes
    Python methods to JS (promise-based), `window.evaluate_js` for Python→JS. Actively maintained.
  - **FastAPI/Flask + WebSocket + `webbrowser.open()`** — keeps the report a normal browser tab,
    full control over the message protocol, degrades naturally to the static story.
  - Also considered: Eel (low ceremony, maintenance slowed), NiceGUI (Python owns UI state,
    `native=True` uses pywebview), PySide6 QWebEngineView + QWebChannel (heavyweight).
  - Recommendation: **pywebview** for a desktop-app feel, or FastAPI+WS for a browser tab.

## Re-running a task with trace flags

- Don't reconstruct commands from `build.ninja` variable expansion — use
  `ninja -C <builddir> -t commands <output>` to get the exact command line, inject the trace
  flag, re-run in the build dir.
- Per-compiler hooks:
  - **clang / clang-cl**: `-ftime-trace` (Chrome Trace Event JSON per TU: frontend/backend split,
    per-header include cost, per-template instantiation cost). Tune with
    `-ftime-trace-granularity`, redirect with `-ftime-trace=<path>`. This is the star feature;
    the sample project builds with clang++, so it works end-to-end here.
  - **lld**: `--time-trace` for slow link steps — links are usually the critical-path tail,
    exactly where the viz points people.
  - **MSVC**: phase 2. Quick wins: `/Bt+`, `/d2cgsummary`, `/d1reportTime` (text to parse);
    the real tool is vcperf / C++ Build Insights (whole-build tracing, WPA).
  - **GCC**: only `-ftime-report` (text tables, no flame graph) — parse-and-display.

## Rendering the trace

Three tiers of effort:
1. Save the JSON and point the user at Perfetto / speedscope (cheapest).
2. Embed speedscope (self-hostable, can be handed a profile).
3. Render a flame chart in-page: **the Gantt canvas renderer is reusable** — a flame chart is
   the same time-ordered-bars renderer with rows = stack depth instead of rows = lanes, and
   zoom/pan/tooltips already exist.

## Whole-build profiling (very talk-friendly)

**ClangBuildAnalyzer** aggregates `-ftime-trace` output across all TUs → "most expensive
headers / templates / functions" for the whole build. A "profile entire build" mode (rebuild
with `-ftime-trace` globally, aggregate, show top offenders next to the critical path) connects
the DAG-level story ("this task is on the critical path") to the code-level story ("…and it's
slow because of this header").

## Prep already being done in the current implementation

1. Every task in the report JSON keeps a stable ID mapping back to its ninja **output path**
   (needed for `ninja -t commands <output>`).
2. The task detail panel is kept pluggable so a button + result pane can be added without
   reworking the layout.


# NinjaScope — Follow-up: Hosted in-browser generator (dual-mode template)

Future improvement discussed on 2026-07-22, while evaluating non-Python rewrites for speed
and portability. Context: users are C++ developers (no Node/Deno assumed; python3 and a
browser are the only safe runtimes), and reports must remain **stored, self-contained HTML
artifacts** — which ruled out a browser page as a *replacement* for the CLI, but not as a
second generator.

## The idea

A hosted static page (e.g. GitHub Pages, zero backend) that parses a build directory
entirely client-side and produces the same report:

- Drag the build dir onto the page → lazy directory walk → parse `build.ninja` +
  included/subninja'd `*.ninja` + `.ninja_log` + `.ninja_deps` in JS → run the graph
  analysis → render the report in place.
- **Download report.html** = inject the computed payload into the page's own HTML source and
  save. The stored-artifact workflow is preserved; the hosted page is just another way to
  produce the same file.

## Dual-mode template

Make one artifact of template.html: payload embedded → render the report (today's behavior);
no payload → show the drop zone and generate in-browser. Then the hosted page, the stored
report, and the CLI output are the **same HTML file** — no separate "app" to maintain.

## Browser support (verified reasoning, 2026-07)

- `showDirectoryPicker()` / File System Access API (persistent handles, auto-refresh,
  write-back): **Chromium-only**. Firefox has declined it; Safari only has OPFS.
- Drag-and-drop via the **File and Directory Entries API** (`webkitGetAsEntry()`): works in
  Firefox and Safari too, and the walk is *lazy* — only the handful of ninja files are ever
  opened, never the gigabytes of object files.
- Avoid `<input webkitdirectory>`: eagerly enumerates the whole build dir (~100k files,
  scary confirmation prompt).
- Output download (Blob + `a[download]`) is universal.
- So: core flow is cross-browser; only conveniences (folder picker, remembered dir,
  live refresh) are Chromium-gated — ship them as progressive enhancement.

## Hard requirements / limits

- **`.ninja_deps` binary parser in JS is mandatory** — no `ninja -t deps` subprocess exists
  in a browser. Format is versioned (v3/v4); detect the version and fail clearly on unknown.
- `include`/`subninja` paths that escape the dropped directory (absolute paths) are
  unreachable — detect and warn rather than silently building a wrong graph.
- Interactive mode (clang `-ftime-trace` re-runs) inherently needs a local process:
  **CLI-only forever**.
- Privacy note on the page: command lines can reveal internal paths; everything stays
  client-side, nothing is uploaded. Works offline once cached.

## The strategic fork (decide before building)

1. **Add-on**: JS analyzer next to the full Python analyzer → analysis logic maintained in
   two languages, golden-diff both. Fine for a prototype, poor long-term.
2. **End state**: the JS analyzer becomes the single implementation (shared by hosted page
   and template); the Python CLI shrinks to a thin headless shell (read files, run the same
   logic, embed payload) for CI/stored generation. Cleaner, but is effectively the
   TypeScript rewrite of the analysis core.

Recommendation from the discussion: only pursue this if willing to land on (2).

## First milestone (pays off even if the page never ships)

1. ~~**`.ninja_deps` binary parser in Python**~~ — DONE 2026-07-22 (`parse_ninja_deps` in
   ninjascope.py, verified byte-identical against `ninja -t deps` on the webrtc build;
   0.09s vs 1.5s subprocess, falls back to the tool on unknown versions). A JS port can
   mirror it 1:1.
2. **Dual-mode template plumbing** (payload-or-dropzone switch in template.html).

## Related perf notes (2026-07-22 state, webrtc build dir)

- CLI generation was optimized 27s → 7.2s (commit 61b5bd1), then → 3.3s (producer-closure
  sharing/caching in `build_tasks`, direct `.ninja_deps` parsing). Remaining profile:
  ~1s manifest parse, ~1.1s build_tasks, ~0.8s export+startup — diminishing returns from
  here in pure Python.
- For stored/CI reports, **payload size** may matter more than speed: ~35MB of the 41MB JSON
  is near-duplicate command lines — interning commands against per-rule templates is the big
  size lever (needs matching template.html decoding).