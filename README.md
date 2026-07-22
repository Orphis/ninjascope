# NinjaScope

Visualize the parallelism of a Ninja build: what actually ran, how well the
cores were used, what would happen with more cores (up to remote-execution
scale), and where the critical path puts the hard floor on build time.

Point it at a Ninja build directory; it reads

- `build.ninja` — the dependency DAG (custom parser, handles CMake output
  including `include`/`subninja`, variable expansion, and `$`-escapes),
- `.ninja_log` (v5/v6) — measured start/end timestamps per task,
- `ninja -t deps` — discovered dependencies, so compiles that include
  *generated* headers are correctly ordered after their codegen steps,

and writes a **single self-contained HTML report** — double-click to open, no
server needed. (The speedup chart uses ECharts from a CDN; without network it
degrades to a table, everything else works offline.)

## Usage

```sh
# 1. generate the demo project (25 libs × 8 files, 6 dependency layers)
python generate_sample.py

# 2. build it — a CLEAN full build gives the accurate timeline
cmake -S sample -B sample/build -G Ninja -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_BUILD_TYPE=Release
ninja -C sample/build

# 3. generate the report
python ninjascope.py sample/build -o report.html --title "My build"
```

### Before/after: the over-serialized flavor

The same sources also configure into a deliberately *unoptimized* build graph —
the kind of serialization this tool exists to find:

```sh
cmake -S sample -B sample/build-coarse -G Ninja -DCMAKE_CXX_COMPILER=clang++ -DCMAKE_BUILD_TYPE=Release -DCOARSE_DEPS=ON
ninja -C sample/build-coarse
python ninjascope.py sample/build-coarse -o report-coarse.html --title "Coarse deps"
```

Open the two reports side by side: same tasks, same durations, but in the
coarse flavor each library's codegen waits on its dependencies' *archives*
instead of just their generated headers, so every layer serializes behind the
previous one and the critical path balloons.

(Don't compare *total work* between the two reports: the precise build keeps
the machine saturated, so contention inflates its measured task durations —
compare the critical paths and the speedup curves instead.)

Works on any Ninja build directory (CMake, GN, Meson, …), not just the sample.
`uv run ninjascope.py …` also works (PEP 723 metadata, stdlib-only).

Options: `-o out.html`, `--title "…"`, `--no-deps` (skip `ninja -t deps`),
`--no-commands` (omit per-task command lines),
`--no-compress` (payloads over 1 MB are deflate-compressed automatically; this keeps
the embedded JSON readable instead),
`--ignore-file` (suppress Insights findings by id, one fnmatch pattern per line;
`<builddir>/.ninjascope-ignore` is picked up automatically).

## Interactive mode: compiler profiling

```sh
python ninjascope.py sample/build --interactive     # serves on a random free port and opens it
```

Serves the *same* report from a live Python process — the page feature-detects
the bridge (`fetch /api/ping`) and lights up profiling on top of the static
views (options: `--port`, `--no-open`):

- **Click any task** → detail panel (duration, slack, deps, rule). For clang
  compile and lld link steps a **"Profile with -ftime-trace"** button re-runs
  that exact command (from `ninja -t commands`, with `-o` redirected to a temp
  dir so the build tree is untouched) and renders the Chrome-trace output as an
  in-page **flame chart** — the Gantt renderer reused with rows = stack depth —
  plus phase totals and the most expensive includes/instantiations of that TU.
- **"Profile whole build"** (header button) re-compiles every clang TU with
  `-ftime-trace` in parallel and aggregates a mini ClangBuildAnalyzer report:
  slowest TUs (click through to the timeline), most expensive headers,
  template instantiations, and functions — connecting the DAG-level story
  ("this task is on the critical path") to the code-level story ("…because of
  this header/template").

Opened as a plain file (or served without the bridge), the same HTML stays
fully static — the profiling UI simply never appears.

## What the report shows

- **Actual build timeline** — Gantt of every task from `.ninja_log`, one row
  per parallel lane, colored by kind (compile / codegen / archive / link).
  Wheel = zoom, drag = pan, double-click = reset; hover for duration and
  slack; a toggle dims everything not on the critical path. A utilization
  strip below shows how many tasks were running at each instant.
- **What-if simulation** — a slider from 1 to 8192 cores (and ∞) re-schedules
  the real DAG with measured durations in the browser (greedy
  critical-path-first list scheduling) and redraws the predicted timeline,
  wall time, speedup, and core utilization live.
- **Speedup curve** — predicted wall time vs. core count on log-log axes,
  against perfect scaling (work ÷ cores) and the critical-path floor. The
  knee is where adding cores stops paying.
- **Critical path** — length, task list with per-task share; click a row to
  zoom to that task in the timeline.

Sanity properties you can check live: at 1 core the prediction equals total
work; at ∞ cores it equals the critical path; at the actual core count it
should be within a few percent of the measured wall time.

## The sample project

`generate_sample.py` shapes the DAG to make the demo interesting:

- a **slow global codegen step** (2.5 s) everything waits on — serialized head;
- a layered DAG of **per-library codegen steps** (each library's generated
  header depends on its dependencies' generated headers, like protobuf
  imports) — real graph depth;
- a **core library with slow translation units** — a fat critical path;
- a final **executable link** — serialized tail.

Knobs: `--libs 25 --files-per-lib 8 --depth 6 --seed 42 --out sample`.

The project configures into two flavors of the same graph, switched by
`-DCOARSE_DEPS` (see above):

- **precise** (default, the optimized "after"): all codegen rules live in the
  top-level CMakeLists.txt, so CMake wires exact file-level `DEPENDS` between
  codegen steps; each generated header is anchored with its own
  `add_custom_target` instead of being listed as a library source.
- **coarse** (`-DCOARSE_DEPS=ON`, the "before"): codegen is declared in each
  library's own subdirectory, the way projects naturally grow. File-level
  `DEPENDS` on custom-command outputs can't cross CMake directories, so the
  generated header is anchored as a library source and ordering falls back to
  whole-target dependencies — each codegen step (and every compile behind it)
  waits for its dependencies' *archives*, serializing each layer behind the
  previous one. This gotcha bit this very project, and it's exactly the kind
  of structure the reports make visible.

## Accuracy notes

- `.ninja_log` is append-only across builds; after incremental rebuilds the
  timeline mixes runs and the tool (and the report) warn. Use a clean build.
- Tasks present in the manifest but never built get duration 0.
- The simulator assumes task durations don't change with the core count — no
  memory-bandwidth/IO contention modeling — so low-core predictions are
  slightly optimistic and high-core predictions slightly pessimistic.
