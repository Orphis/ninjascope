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