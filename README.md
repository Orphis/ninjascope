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

Options: `-o out.html`, `--title "…"`, `--no-deps` (skip discovered dependencies),
`--list-runs` / `--run N` (pick which build from a multi-build log to show on the
timeline; default is the last one),
`--no-commands` (omit per-task command lines),
`--no-compress` (payloads over 1 MB are deflate-compressed automatically; this keeps
the embedded JSON readable instead), `--compress-level {0-9}`,
`--ignore-file` (suppress Insights findings by id, one fnmatch pattern per line;
`<builddir>/.ninjascope-ignore` is picked up automatically),
`--calibrate` / `--cores N` / `--contention B,G,C` (see below).

## Measuring contention

Every duration in `.ninja_log` was measured *in a crowd*. When 16 compiles run
at once they compete for memory bandwidth, cache, disk and turbo headroom, so a
file that takes 1.0 s alone may take 1.7 s during the build — and the what-if
simulator, replaying those durations at 1 core, would be pricing in a crowd that
isn't there.

You can't recover the crowd-free duration from the log: a build is either
saturated or nearly idle, with almost nothing in between, and its idle stretches
are the serialized head and the final link — not a fair sample. So NinjaScope
measures it directly instead:

```sh
python ninjascope.py sample/build --calibrate
```

This re-runs about a dozen of the build's *own* commands (outputs redirected to a
temp dir, so the build tree is untouched) alone, and again with the machine held
at 25 %, 50 %, 100 % and 150 % of the job count the build used — the levels in
randomized order, with isolated runs interleaved so thermal drift is measured
rather than absorbed. Background load is made of real build commands, because a
synthetic spinner contends on ALUs but not on the memory controller, which is
most of the effect. It takes a few minutes and writes
`<builddir>/.ninjascope-contention.json`, which later runs pick up
automatically — including for builds already recorded.

The report then shows the **contention tax** (work that exists only because the
machine was crowded, on the whole build and on the critical path), a per-task
inflation factor with `w:` / `infl:` search filters, and a **contention target**
control next to the cores slider: as measured, half, or none at all. Because the
150 % level is measured rather than extrapolated, the simulator can also answer
whether a *lower* `-j` would have been faster.

### Peak memory

The same sweep records peak RSS for each sampled step, run on its own, and the
report sums those footprints over everything running at each instant: a **peak
RAM** figure for the build as it ran, a live one that follows the cores slider,
and a curve on the speedup chart with the machine's physical RAM marked — where
they cross is where the box would start paging. A finding fires only when the
machine is actually near its limit, and names the largest `-j` that still fits.

Two honest caveats, both stated in the report:

- it is an **upper bound in time** — a compile's peak is a brief spike near the
  end, and summing peaks assumes every running step spikes together;
- it is a **lower bound in coverage** — a step is only sampled if its output path
  can be positively redirected to a temp dir (`-o`, `/OUT:`). Archivers that take
  the output positionally, like `llvm-ar qc libfoo.a …`, can't be re-run without
  touching the real build tree, so they are skipped and counted as zero. The
  report lists which kinds it measured and which it didn't rather than
  substituting a guess.

`--cores N` sets the job count the build ran with (`.ninja_log` records neither
`-j` nor the machine, so it is inferred from the concurrency plateau; the core
count of whatever generates the report is often not the one that built).
`--contention B,G,C` supplies a curve directly instead of calibrating.

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
- **"Measure contention"** (header button) runs the calibration sweep described
  above from the page and reports progress in place; reload afterwards to apply
  the curve. Same thing `--calibrate` does, without leaving the report.
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
  wall time, speedup, and core utilization live. Ninja **pools** are honored:
  a `pool link { depth = 2 }` caps those steps however many cores there are, so
  the prediction can't promise parallelism the build wouldn't take. A toggle
  turns them off to price what the pool costs.
- **Contention target** (with a calibrated profile) — *as measured*, *half*, or
  *none*. Cores are one shared pool, so what slows a task down is simply how
  many others are running; the two ends of that row are the two readings of the
  slider's high end. **As measured** is that many cores all sharing one
  machine's memory system. **None** is that many independent workers — the
  remote-execution reading — and it is also the ideal-DAG bound. Past the
  highest load calibration measured, the curve holds steady rather than
  extrapolating. A **model check** line below replays the build at the
  configuration it actually ran with and compares to the wall time it took.
- **Speedup curve** — predicted wall time vs. core count on log-log axes,
  against perfect scaling (work ÷ cores) and the critical-path floor. The
  knee is where adding cores stops paying. With a contention profile it draws
  two curves — contention modeled and an ideal machine — and the gap between
  them is what the crowding costs. The dashed floor is then the critical path
  measured in *uncontended work*, the only bound that holds for both curves:
  the ideal machine converges on it, while the modeled one settles above,
  because at high core counts the whole DAG crowds one machine harder than the
  real build at its `-j` ever did. A dotted line marks the same path measured
  with the build's own durations — the figure in the critical-path tile, which
  sits between the two because those durations already carry the crowding the
  build ran under.
- **Critical path** — length, task list with per-task share; click a row to
  zoom to that task in the timeline.
- **Hot headers** — the discovered dependencies inverted: what touching a
  header would cost. Each row is a header with the tasks that read it, the
  transitive rebuild set (recompile 200 TUs and every archive behind them
  relinks), that set's CPU work, and its wall time scheduled at the job count
  the build ran with. Click a row to light its consumers up in the timeline, or
  type `header:foo.h` in the search bar. Only headers under the project root
  are indexed — toolchain headers are included by everything and editable by
  nobody — and the costliest are kept, so the table stays small on large builds.
- **Targets** — a layered dependency graph of the *targets* (libraries,
  executables) inferred from the Ninja graph: CMake object directories and
  rule names, GN's per-target `.ninja` files, archive/link steps as anchors
  elsewhere; generated code forms its own targets grouped by output
  directory, so codegen chains stay visible. Node
  height tracks total build time; `‹N ›M` badges show dependency/dependent
  counts. Edges are drawn only for the hovered or selected target, so hub
  libraries with hundreds of dependents stay readable; selecting a target
  opens a panel with its dependencies, dependents, and tasks, and lights its
  tasks up in the timeline. Toggles: hide transitive edges, draw all edges
  (small graphs only).
- **Treemap** — where the minutes go: box area is total build time,
  directories contain targets, targets contain their tasks. Click to zoom in,
  breadcrumb (or right-click) to zoom out; clicking a task opens it in the
  timeline. Both tabs appear only when targets could be inferred.

Sanity properties you can check live: at 1 core the prediction equals total
work; at ∞ cores it equals the critical path; at the actual core count it
should be within a few percent of the measured wall time.

With a contention profile the simulator schedules *uncontended* work, so:

- **at 1 core** the prediction is total work minus the contention tax — strictly
  below the tile, which stays descriptive and keeps measured durations. Both
  contention settings agree here, because a job running alone competes with
  nothing under either;
- **at ∞ cores** it is the uncontended critical path only with contention
  **none**. With contention *as measured* it lands above that: the wide phase of
  the DAG still crowds one shared machine, and the tail — where the critical path
  actually binds — is where the two converge. On the sample: 5.3 s against 7.9 s;
- **at the actual core count** the property is unchanged and is now a test rather
  than a coincidence — exactly what the model-check line reports. Note it mostly
  checks the *scheduler*: replaying at the concurrency the durations were measured
  at recovers those durations by construction. The curve earns its keep away from
  that point.

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

- `.ninja_log` is append-only across builds. NinjaScope splits it into per-build
  runs: the timeline shows one run (the last by default; `--run N` to pick,
  `--list-runs` to inspect), with tasks not rebuilt in it shown as "not built".
  The critical path / what-if / Insights always use each task's most recent
  recorded duration across all runs, so they cover the whole build even from an
  incremental log — durations sampled under different conditions (load, warm
  caches) make them approximate, and the report says so. A clean full build is
  still the gold standard.
- If ninja has recompacted the log (`ninja -t recompact`, or automatically on
  large logs), run structure is unrecoverable: no timeline, analysis only.
- Tasks present in the manifest but never built get duration 0.
- Without a contention profile the simulator assumes task durations don't change
  with the core count. Durations were measured at whatever concurrency the build
  ran at — near saturation for a clean full build — so **low-core predictions
  come out pessimistic** (the real machine would contend less) and high-core
  predictions optimistic. (Earlier versions of this note had those two the wrong
  way round.) `--calibrate` removes the assumption; the model-check line in the
  report says how far off the prediction lands either way.
- With a profile, the contention model has bounds worth knowing:
  - it is a **single curve for the whole build**, driven by total concurrency.
    Per-kind inflation is fitted and reported as a diagnostic (links and compiles
    do differ) but not fed to the simulator: a uniform rate is what makes the
    scheduler exact and O(n log n) rather than a fixed-point iteration;
  - it is clamped at the highest load actually timed, so it never extrapolates —
    but that also means it has nothing to say about job counts beyond there;
  - it is measured on **one machine at one moment**. Calibration interleaves
    isolated runs to measure thermal drift separately (reported as `drift`), but
    a busy machine during calibration, a different CPU, or a different disk all
    move the number. Re-run it when the hardware changes;
  - uncontended work is only computed for tasks the *selected run* built. If that
    run covers less than 80 % of total work the contention UI stays hidden rather
    than mixing durations measured under unknown crowding with de-contended ones.
