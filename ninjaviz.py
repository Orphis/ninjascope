# /// script
# requires-python = ">=3.10"
# ///
"""NinjaViz — visualize build parallelism of a Ninja build directory.

Reads build.ninja (dependency graph), .ninja_log (task timings) and the
discovered dependencies (ninja -t deps, for generated headers), then writes a
self-contained HTML report with:
  - the actual build timeline (Gantt) + CPU utilization,
  - a what-if scheduler simulating 1..8192+ cores on the dependency DAG,
  - the critical path, highlighted and with stats.

Usage:
  python ninjaviz.py <build-dir> [-o report.html] [--title "My build"] [--no-deps]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from heapq import heappop, heappush
from pathlib import Path

CORE_OPTIONS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192]


# --------------------------------------------------------------------------
# build.ninja parsing
# --------------------------------------------------------------------------

@dataclass
class Edge:
    rule: str
    outputs: list[str] = field(default_factory=list)   # normalized keys
    inputs: list[str] = field(default_factory=list)    # explicit + implicit + order-only
    display: str = ""                                   # first output as written


class NinjaManifest:
    def __init__(self, builddir: Path):
        self.builddir = builddir
        self.scope: dict[str, str] = {}
        self.edges: list[Edge] = []

    def norm(self, path: str) -> str:
        """Canonical key for a path: absolute, forward slashes, casefolded."""
        p = path.replace("\\", "/")
        if not re.match(r"^([A-Za-z]:/|/)", p):
            p = self.builddir.as_posix() + "/" + p
        return os.path.normpath(p).replace("\\", "/").casefold()

    def parse(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in self._logical_lines(text):
            if not line or line.lstrip().startswith("#"):
                continue
            if line[0] in " \t":
                continue  # indented binding inside rule/build/pool: not needed
            keyword = line.split(None, 1)[0]
            if keyword == "build":
                self._parse_build(line)
            elif keyword in ("include", "subninja"):
                sub = self._expand_path_list(line.split(None, 1)[1])[0]
                subpath = Path(sub)
                if not subpath.is_absolute():
                    subpath = self.builddir / subpath
                self.parse(subpath)
            elif keyword in ("rule", "pool", "default"):
                continue
            elif "=" in line:
                key, _, value = line.partition("=")
                self.scope[key.strip()] = self._expand(value.strip())

    @staticmethod
    def _logical_lines(text: str):
        """Join lines ending with a '$' continuation (odd number of trailing $)."""
        pending = ""
        for raw in text.split("\n"):
            line = pending + (raw.lstrip() if pending else raw)
            stripped = line.rstrip("\r")
            trailing = len(stripped) - len(stripped.rstrip("$"))
            if trailing % 2 == 1:
                pending = stripped[:-1]
                continue
            pending = ""
            yield stripped
        if pending:
            yield pending

    def _expand(self, s: str) -> str:
        """Expand $var / ${var} and unescape $$, '$ ', $: in a plain value."""
        out = []
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c != "$":
                out.append(c)
                i += 1
                continue
            i += 1
            if i >= n:
                break
            c = s[i]
            if c in "$ :":
                out.append(c)
                i += 1
            elif c == "{":
                j = s.index("}", i)
                out.append(self.scope.get(s[i + 1:j], ""))
                i = j + 1
            else:
                m = re.match(r"[A-Za-z0-9_.-]+", s[i:])
                out.append(self.scope.get(m.group(0), "") if m else "$")
                i += len(m.group(0)) if m else 0
        return "".join(out)

    def _expand_path_list(self, s: str) -> list[str]:
        """Split a path list on unescaped spaces, expanding vars and escapes."""
        tokens, cur = [], []
        i, n = 0, len(s)
        while i < n:
            c = s[i]
            if c == "$" and i + 1 < n:
                nxt = s[i + 1]
                if nxt in "$ :":
                    cur.append(nxt)
                    i += 2
                    continue
                if nxt == "{":
                    j = s.index("}", i + 2)
                    cur.append(self.scope.get(s[i + 2:j], ""))
                    i = j + 1
                    continue
                m = re.match(r"[A-Za-z0-9_.-]+", s[i + 1:])
                if m:
                    cur.append(self.scope.get(m.group(0), ""))
                    i += 1 + len(m.group(0))
                    continue
            if c in " \t":
                if cur:
                    tokens.append("".join(cur))
                    cur = []
                i += 1
            else:
                cur.append(c)
                i += 1
        if cur:
            tokens.append("".join(cur))
        return tokens

    def _parse_build(self, line: str) -> None:
        body = line[len("build"):]
        # first unescaped ':' separates outputs from rule+inputs
        colon = None
        i = 0
        while i < len(body):
            if body[i] == "$":
                i += 2
                continue
            if body[i] == ":":
                colon = i
                break
            i += 1
        if colon is None:
            return
        outs_part, ins_part = body[:colon], body[colon + 1:]

        def split_sections(part: str) -> list[str]:
            """Split on unescaped | and || into up to three sections."""
            sections, cur = [], []
            i = 0
            while i < len(part):
                if part[i] == "$":
                    cur.append(part[i:i + 2])
                    i += 2
                elif part[i] == "|":
                    sections.append("".join(cur))
                    cur = []
                    i += 2 if part[i:i + 2] == "||" else 1
                else:
                    cur.append(part[i])
                    i += 1
            sections.append("".join(cur))
            return sections

        out_paths: list[str] = []
        for sec in split_sections(outs_part):
            out_paths += self._expand_path_list(sec)
        in_sections = split_sections(ins_part)
        rule_and_ins = self._expand_path_list(in_sections[0])
        if not rule_and_ins or not out_paths:
            return
        rule = rule_and_ins[0]
        in_paths = rule_and_ins[1:]
        for sec in in_sections[1:]:
            in_paths += self._expand_path_list(sec)

        self.edges.append(Edge(
            rule=rule,
            outputs=[self.norm(p) for p in out_paths],
            inputs=[self.norm(p) for p in in_paths],
            display=out_paths[0],
        ))


# --------------------------------------------------------------------------
# .ninja_log parsing
# --------------------------------------------------------------------------

def parse_ninja_log(path: Path, manifest: NinjaManifest):
    """Return ({output_key: (start_ms, end_ms)}, mixed_runs: bool)."""
    entries: dict[str, tuple[int, int]] = {}
    mixed = False
    with path.open(encoding="utf-8", errors="replace") as f:
        header = f.readline()
        if not header.startswith("# ninja log v"):
            sys.exit(f"error: unrecognized ninja log header: {header.strip()}")
        version = int(header.rsplit("v", 1)[1])
        if version not in (5, 6):
            print(f"warning: ninja log v{version} untested, attempting v5/v6 parse",
                  file=sys.stderr)
        for line in f:
            fields = line.rstrip("\n").split("\t")
            if len(fields) < 5:
                continue
            key = manifest.norm(fields[3])
            times = (int(fields[0]), int(fields[1]))
            if key in entries and entries[key] != times:
                mixed = True
            entries[key] = times
    return entries, mixed


# --------------------------------------------------------------------------
# discovered deps (generated headers)
# --------------------------------------------------------------------------

def parse_deps_tool(builddir: Path, manifest: NinjaManifest) -> dict[str, list[str]]:
    """Run `ninja -t deps` and return {target_output_key: [dep_keys...]}."""
    try:
        proc = subprocess.run(
            ["ninja", "-C", str(builddir), "-t", "deps"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("warning: ninja not found on PATH; skipping discovered deps "
              "(generated-header dependencies may be missing)", file=sys.stderr)
        return {}
    deps: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in proc.stdout.splitlines():
        if line.startswith((" ", "\t")):
            if current is not None:
                current.append(manifest.norm(line.strip()))
        elif ": #deps" in line:
            target = line.rsplit(": #deps", 1)[0]
            current = deps.setdefault(manifest.norm(target), [])
        else:
            current = None
    return deps


# --------------------------------------------------------------------------
# graph construction & analysis
# --------------------------------------------------------------------------

@dataclass
class Task:
    id: int
    edge: Edge
    start: int | None = None
    end: int | None = None
    dur: int = 0
    deps: set[int] = field(default_factory=set)


def build_tasks(manifest: NinjaManifest, log: dict[str, tuple[int, int]],
                discovered: dict[str, list[str]]):
    edge_by_output: dict[str, int] = {}
    for idx, e in enumerate(manifest.edges):
        for o in e.outputs:
            edge_by_output.setdefault(o, idx)

    # producers(path) -> set of real (non-phony) edge indices, seen through phony
    prod_cache: dict[int, frozenset[int]] = {}

    def edge_producers(idx: int, stack: frozenset[int] = frozenset()) -> frozenset[int]:
        if idx in prod_cache:
            return prod_cache[idx]
        if idx in stack:
            return frozenset()
        e = manifest.edges[idx]
        if e.rule != "phony":
            result = frozenset([idx])
        else:
            acc: set[int] = set()
            for p in e.inputs:
                j = edge_by_output.get(p)
                if j is not None:
                    acc |= edge_producers(j, stack | {idx})
            result = frozenset(acc)
        prod_cache[idx] = result
        return result

    def path_producers(path: str) -> frozenset[int]:
        j = edge_by_output.get(path)
        return edge_producers(j) if j is not None else frozenset()

    # real-edge dependency sets (edge idx -> set of edge idx)
    edge_deps: dict[int, set[int]] = {}
    for idx, e in enumerate(manifest.edges):
        if e.rule == "phony":
            continue
        dset: set[int] = set()
        for p in e.inputs:
            dset |= path_producers(p)
        for o in e.outputs:
            for dep_path in discovered.get(o, ()):
                dset |= path_producers(dep_path)
        dset.discard(idx)
        edge_deps[idx] = dset

    # keep edges that were built (in the log) plus anything they depend on
    logged = {idx for idx, e in enumerate(manifest.edges)
              if e.rule != "phony" and any(o in log for o in e.outputs)}
    keep: set[int] = set()
    frontier = list(logged)
    while frontier:
        idx = frontier.pop()
        if idx in keep:
            continue
        keep.add(idx)
        frontier += [d for d in edge_deps.get(idx, ()) if d not in keep]

    unlogged_kept = keep - logged
    order = sorted(keep)
    task_id = {edge_idx: i for i, edge_idx in enumerate(order)}
    tasks: list[Task] = []
    for edge_idx in order:
        e = manifest.edges[edge_idx]
        t = Task(id=task_id[edge_idx], edge=e,
                 deps={task_id[d] for d in edge_deps[edge_idx] if d in keep})
        for o in e.outputs:
            if o in log:
                t.start, t.end = log[o]
                t.dur = max(0, t.end - t.start)
                break
        tasks.append(t)
    return tasks, len(unlogged_kept)


def topo_order(tasks: list[Task]) -> list[int]:
    indeg = [len(t.deps) for t in tasks]
    succs: list[list[int]] = [[] for _ in tasks]
    for t in tasks:
        for d in t.deps:
            succs[d].append(t.id)
    order = [i for i, d in enumerate(indeg) if d == 0]
    head = 0
    while head < len(order):
        i = order[head]
        head += 1
        for s in succs[i]:
            indeg[s] -= 1
            if indeg[s] == 0:
                order.append(s)
    if len(order) != len(tasks):
        sys.exit("error: dependency graph has a cycle (corrupt manifest?)")
    return order


def critical_path(tasks: list[Task], order: list[int]):
    """Longest path; returns (length_ms, [task ids along the path], slack per task)."""
    ef = [0] * len(tasks)   # earliest finish
    best_pred = [-1] * len(tasks)
    for i in order:
        t = tasks[i]
        es = 0
        for d in t.deps:
            if ef[d] > es:
                es, best_pred[i] = ef[d], d
        ef[i] = es + t.dur
    cp_len = max(ef, default=0)

    # backward pass for slack (deadline = cp_len)
    succs: list[list[int]] = [[] for _ in tasks]
    for t in tasks:
        for d in t.deps:
            succs[d].append(t.id)
    lf = [cp_len] * len(tasks)  # latest finish
    for i in reversed(order):
        for s in succs[i]:
            lf[i] = min(lf[i], lf[s] - tasks[s].dur)
    slack = [lf[i] - ef[i] for i in range(len(tasks))]

    end = max(range(len(tasks)), key=lambda i: ef[i], default=-1)
    path = []
    while end != -1:
        path.append(end)
        end = best_pred[end]
    return cp_len, list(reversed(path)), slack


def simulate(tasks: list[Task], cores: float, tails: list[int]) -> int:
    """Greedy list scheduling (critical-path-first). Returns makespan in ms."""
    indeg = [len(t.deps) for t in tasks]
    succs: list[list[int]] = [[] for _ in tasks]
    for t in tasks:
        for d in t.deps:
            succs[d].append(t.id)
    ready = [(-tails[i], i) for i, d in enumerate(indeg) if d == 0]
    ready.sort()
    import heapq
    heapq.heapify(ready)
    running: list[tuple[int, int]] = []
    now = 0
    makespan = 0
    while ready or running:
        while ready and len(running) < cores:
            _, i = heappop(ready)
            finish = now + tasks[i].dur
            heappush(running, (finish, i))
            makespan = max(makespan, finish)
        if not running:
            break
        now, i = heappop(running)
        # release everything finishing at the same instant
        done = [i]
        while running and running[0][0] == now:
            done.append(heappop(running)[1])
        for j in done:
            for s in succs[j]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    heappush(ready, (-tails[s], s))
    return makespan


def compute_tails(tasks: list[Task], order: list[int]) -> list[int]:
    succs: list[list[int]] = [[] for _ in tasks]
    for t in tasks:
        for d in t.deps:
            succs[d].append(t.id)
    tails = [0] * len(tasks)
    for i in reversed(order):
        longest = max((tails[s] for s in succs[i]), default=0)
        tails[i] = tasks[i].dur + longest
    return tails


# --------------------------------------------------------------------------
# rule classification (for coloring)
# --------------------------------------------------------------------------

def classify_rule(rule: str) -> str:
    r = rule.upper()
    if "CUSTOM_COMMAND" in r:
        return "codegen"
    if "STATIC_LIBRARY" in r or r.startswith("AR") or "_AR_" in r:
        return "archive"
    if "LINKER" in r or "LINK" in r:
        return "link"
    if "COMPILER" in r or r.startswith(("CXX", "CC", "C_", "OBJC", "RC", "ASM")):
        return "compile"
    return "other"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("builddir", type=Path, help="ninja build directory")
    ap.add_argument("-o", "--output", type=Path, default=Path("report.html"))
    ap.add_argument("--title", default=None, help="report title")
    ap.add_argument("--no-deps", action="store_true",
                    help="skip `ninja -t deps` (generated-header dependencies)")
    args = ap.parse_args()

    builddir: Path = args.builddir.resolve()
    manifest_path = builddir / "build.ninja"
    log_path = builddir / ".ninja_log"
    if not manifest_path.exists():
        sys.exit(f"error: {manifest_path} not found")
    if not log_path.exists():
        sys.exit(f"error: {log_path} not found (run a build first)")

    manifest = NinjaManifest(builddir)
    manifest.parse(manifest_path)
    print(f"parsed {len(manifest.edges)} edges from build.ninja")

    log, mixed = parse_ninja_log(log_path, manifest)
    if mixed:
        print("warning: .ninja_log contains entries from multiple builds; the "
              "timeline shows the most recent entry per output. For accurate "
              "results use a clean full build.", file=sys.stderr)

    discovered = {} if args.no_deps else parse_deps_tool(builddir, manifest)

    tasks, n_unlogged = build_tasks(manifest, log, discovered)
    if n_unlogged:
        print(f"warning: {n_unlogged} task(s) have no timing in .ninja_log "
              "(duration treated as 0)", file=sys.stderr)
    print(f"{len(tasks)} tasks with timing/dependency info")

    order = topo_order(tasks)
    cp_len, cp_path, slack = critical_path(tasks, order)
    tails = compute_tails(tasks, order)

    timed = [t for t in tasks if t.start is not None]
    wall = max((t.end for t in timed), default=0) - min((t.start for t in timed), default=0)
    work = sum(t.dur for t in tasks)

    # observed peak concurrency
    events = sorted([(t.start, 1) for t in timed] + [(t.end, -1) for t in timed])
    peak = cur = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)

    speedup = [{"cores": n, "makespan": simulate(tasks, n, tails)} for n in CORE_OPTIONS]
    speedup.append({"cores": None, "makespan": cp_len})  # infinite cores

    rules = sorted({t.edge.rule for t in tasks})
    rule_idx = {r: i for i, r in enumerate(rules)}
    cp_set = set(cp_path)

    def display_name(t: Task) -> str:
        name = t.edge.display.replace("\\", "/")
        prefix = builddir.as_posix() + "/"
        if name.casefold().startswith(prefix.casefold()):
            name = name[len(prefix):]
        return name

    data = {
        "meta": {
            "title": args.title or f"Ninja build: {builddir.name}",
            "builddir": str(builddir),
            "generated": datetime.now().isoformat(timespec="seconds"),
            "wall": wall,
            "work": work,
            "cpLength": cp_len,
            "peakConcurrency": peak,
            "taskCount": len(tasks),
            "mixedLog": mixed,
            "speedup": speedup,
        },
        "rules": [{"name": r, "kind": classify_rule(r)} for r in rules],
        "tasks": [
            {
                "name": display_name(t),
                "rule": rule_idx[t.edge.rule],
                "start": t.start,
                "end": t.end,
                "dur": t.dur,
                "deps": sorted(t.deps),
                "slack": slack[t.id],
                "cp": t.id in cp_set,
            }
            for t in tasks
        ],
        "criticalPath": cp_path,
    }

    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = template.replace("/*__NINJAVIZ_DATA__*/null", payload)
    args.output.write_text(html, encoding="utf-8")

    print(f"wall time      : {wall / 1000:.1f}s")
    print(f"total work     : {work / 1000:.1f}s  (avg parallelism {work / max(wall, 1):.1f}x)")
    print(f"critical path  : {cp_len / 1000:.1f}s  ({len(cp_path)} tasks, "
          f"max speedup {work / max(cp_len, 1):.1f}x)")
    print(f"report written : {args.output}")


if __name__ == "__main__":
    main()
