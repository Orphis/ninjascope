# NinjaViz

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
python ninjaviz.py sample/build -o report.html --title "My build"
```

Works on any Ninja build directory (CMake, GN, Meson, …), not just the sample.
`uv run ninjaviz.py …` also works (PEP 723 metadata, stdlib-only).

Options: `-o out.html`, `--title "…"`, `--no-deps` (skip `ninja -t deps`).

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

Gotcha worth knowing (it bit this project): custom commands whose file-level
`DEPENDS` cross CMake directories get coarsened into whole-target
dependencies, which serializes each layer behind the previous layer's
*archive*. The generator therefore emits all codegen rules in one
CMakeLists.txt and anchors each generated header with its own
`add_custom_target` instead of listing it as a library source.

## Accuracy notes

- `.ninja_log` is append-only across builds; after incremental rebuilds the
  timeline mixes runs and the tool (and the report) warn. Use a clean build.
- Tasks present in the manifest but never built get duration 0.
- The simulator assumes task durations don't change with the core count — no
  memory-bandwidth/IO contention modeling — so low-core predictions are
  slightly optimistic and high-core predictions slightly pessimistic.
