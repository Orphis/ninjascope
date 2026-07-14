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
  python ninjaviz.py <build-dir> --interactive [--port 8017] [--no-open]

Interactive mode serves the same report from a local Python process and adds
compiler profiling: re-run any clang compile (or lld link) with -ftime-trace
and see the flame chart in-page, or profile every clang TU of the build and
aggregate the most expensive headers / templates / functions.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from heapq import heappop, heappush
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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

def build_report(builddir: Path, title: str | None, no_deps: bool):
    """Parse the build dir; return (data dict for the template, tasks, manifest)."""
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

    discovered = {} if no_deps else parse_deps_tool(builddir, manifest)

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
            "title": title or f"Ninja build: {builddir.name}",
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

    print(f"wall time      : {wall / 1000:.1f}s")
    print(f"total work     : {work / 1000:.1f}s  (avg parallelism {work / max(wall, 1):.1f}x)")
    print(f"critical path  : {cp_len / 1000:.1f}s  ({len(cp_path)} tasks, "
          f"max speedup {work / max(cp_len, 1):.1f}x)")
    return data, tasks, manifest


def render_html(data: dict) -> str:
    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return template.replace("/*__NINJAVIZ_DATA__*/null", payload)


# --------------------------------------------------------------------------
# compiler profiling (interactive mode)
# --------------------------------------------------------------------------

_O_TOKEN = re.compile(r'-o\s+("[^"]+"|\S+)')


def _quoted(p) -> str:
    s = str(p)
    return f'"{s}"' if " " in s else s


def edge_command(builddir: Path, output: str) -> str | None:
    """The exact command ninja runs for the edge producing `output`.

    `ninja -t commands <target>` prints the whole transitive chain; the last
    line is the requested edge's own command.
    """
    proc = subprocess.run(["ninja", "-C", str(builddir), "-t", "commands", output],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    return lines[-1] if lines else None


def profile_command(builddir: Path, cmd: str, kind: str) -> dict:
    """Re-run a clang compile / lld link with time tracing, outputs to temp.

    Rewrites the `-o <path>` token so the build tree is left untouched.
    Returns {"events": [...]} (Chrome trace events) or {"error": "..."}.
    """
    lower = cmd.lower()
    m = _O_TOKEN.search(cmd)
    if not m:
        return {"error": "could not find -o <output> in the command line"}
    tmp = Path(tempfile.mkdtemp(prefix="ninjaviz_prof_"))
    try:
        trace = tmp / "trace.json"
        out_ext = Path(m.group(1).strip('"')).suffix or ".out"
        if kind == "compile" and "clang" in lower:
            newtok = f'-o {_quoted(tmp / ("out" + out_ext))} -ftime-trace={_quoted(trace)}'
        elif kind == "link" and "clang" in lower and "lld" in lower:
            newtok = (f'-o {_quoted(tmp / ("out" + out_ext))} '
                      f'-Xlinker --time-trace={_quoted(trace)}')
        else:
            return {"error": "profiling is supported for clang compile steps "
                             "and clang+lld link steps only"}
        modified = cmd[:m.start()] + newtok + cmd[m.end():]
        proc = subprocess.run(modified, shell=True, cwd=builddir,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
        if not trace.exists():
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            return {"error": f"no trace produced (exit {proc.returncode}): {tail}"}
        events = json.loads(trace.read_text(encoding="utf-8")).get("traceEvents", [])
        out = [e for e in events if e.get("ph") == "X" and e.get("dur", 0) > 0]
        # Newer clang emits Source (include) spans as async begin/end pairs
        # (ph "b"/"e") instead of complete "X" events — stitch them together.
        stacks: dict[tuple, list] = {}
        for e in events:
            ph = e.get("ph")
            key = (e.get("tid"), e.get("cat"), e.get("id"))
            if ph == "b":
                stacks.setdefault(key, []).append(e)
            elif ph == "e" and stacks.get(key):
                b = stacks[key].pop()
                dur = e.get("ts", 0) - b.get("ts", 0)
                if dur > 0:
                    out.append({"ph": "X", "name": b.get("name", "?"),
                                "ts": b.get("ts"), "dur": dur,
                                "tid": b.get("tid"), "pid": b.get("pid"),
                                "args": b.get("args") or {}})
        return {"events": out}
    except subprocess.TimeoutExpired:
        return {"error": "command timed out after 600s"}
    except Exception as exc:  # surface anything to the UI rather than a 500
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def demangle_names(names: list[str]) -> list[str]:
    """Best-effort symbol demangling via whatever tool is on PATH.

    llvm-undname/undname handle MSVC (?...) names, llvm-cxxfilt/c++filt handle
    Itanium (_Z...) names. Tools pass through names they can't demangle, so
    trying them in sequence is safe; with no tool available this is a no-op.
    """
    out = list(names)
    for tool in ("llvm-undname", "undname", "llvm-cxxfilt", "c++filt"):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            proc = subprocess.run([exe], input="\n".join(out), capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=30)
            lines = proc.stdout.splitlines()
            if proc.returncode == 0 and len(lines) == len(out):
                out = [l.strip() or o for l, o in zip(lines, out)]
        except Exception:
            pass
    return out


def profile_build_job(bridge: "Bridge", job: dict) -> None:
    """Re-compile every clang TU with -ftime-trace and aggregate the results
    (a small in-process ClangBuildAnalyzer)."""
    try:
        proc = subprocess.run(["ninja", "-C", str(bridge.builddir), "-t", "commands"],
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        cmd_by_output: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            l = line.strip()
            if not l or "clang" not in l.lower() or " -c " not in l:
                continue
            m = _O_TOKEN.search(l)
            if m:
                cmd_by_output[bridge.manifest.norm(m.group(1).strip('"'))] = l

        targets: list[tuple[int, str]] = []
        for t in bridge.tasks:
            if classify_rule(t.edge.rule) != "compile":
                continue
            for o in t.edge.outputs:
                if o in cmd_by_output:
                    targets.append((t.id, cmd_by_output[o]))
                    break
        job["total"] = len(targets)
        if not targets:
            job.update(state="error", error="no clang compile commands found")
            return

        lock = threading.Lock()
        headers: dict[str, list] = {}     # path -> [total_us, count]
        templates: dict[str, list] = {}
        functions: dict[str, list] = {}
        tus: list[dict] = []
        totals = {"frontend": 0, "backend": 0}
        failed = 0

        def add(table, key, dur):
            e = table.setdefault(key, [0, 0])
            e[0] += dur
            e[1] += 1

        def one(item):
            nonlocal failed
            tid, cmd = item
            res = profile_command(bridge.builddir, cmd, "compile")
            with lock:
                job["done"] += 1
                if "error" in res:
                    failed += 1
                    return
                tu = {"task": tid, "total": 0, "frontend": 0, "backend": 0}
                for e in res["events"]:
                    name, dur = e.get("name", ""), e.get("dur", 0)
                    detail = (e.get("args") or {}).get("detail", "")
                    if name == "ExecuteCompiler":
                        tu["total"] = max(tu["total"], dur)
                    elif name == "Total Frontend":
                        tu["frontend"] = dur
                        totals["frontend"] += dur
                    elif name == "Total Backend":
                        tu["backend"] = dur
                        totals["backend"] += dur
                    elif name == "Source" and detail:
                        add(headers, detail, dur)
                    elif name in ("InstantiateClass", "InstantiateFunction") and detail:
                        add(templates, detail, dur)
                    elif name in ("OptFunction", "CodeGen Function") and detail:
                        add(functions, detail, dur)
                tus.append(tu)

        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
            list(ex.map(one, targets))

        def top(table, n=25, demangle=False):
            rows = sorted(table.items(), key=lambda kv: -kv[1][0])[:n]
            names = [k for k, _ in rows]
            if demangle:
                names = demangle_names(names)
            return [{"name": name, "ms": round(v[0] / 1000), "count": v[1]}
                    for name, (_, v) in zip(names, rows)]

        tus.sort(key=lambda t: -t["total"])
        job["result"] = {
            "tusProfiled": len(tus),
            "failed": failed,
            "frontendMs": round(totals["frontend"] / 1000),
            "backendMs": round(totals["backend"] / 1000),
            "slowestTus": [{"task": t["task"], "ms": round(t["total"] / 1000),
                            "frontendMs": round(t["frontend"] / 1000),
                            "backendMs": round(t["backend"] / 1000)}
                           for t in tus[:25]],
            "topHeaders": top(headers),
            "topTemplates": top(templates),
            "topFunctions": top(functions, demangle=True),
            "note": "header/template/function times are inclusive sums across "
                    "all TUs (like ClangBuildAnalyzer), so nested entries overlap",
        }
        job["state"] = "done"
    except Exception as exc:
        job.update(state="error", error=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# interactive mode: HTTP bridge
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, builddir: Path, html: str, tasks: list[Task],
                 manifest: NinjaManifest):
        self.builddir = builddir
        self.html = html
        self.tasks = tasks
        self.manifest = manifest
        self.jobs: dict[str, dict] = {}
        self._job_seq = 0
        self.lock = threading.Lock()

    def new_job(self) -> tuple[str, dict]:
        with self.lock:
            self._job_seq += 1
            jid = f"job{self._job_seq}"
            job = {"state": "running", "done": 0, "total": 0}
            self.jobs[jid] = job
        return jid, job


def serve(bridge: Bridge, port: int, open_browser: bool) -> None:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/report.html"):
                body = bridge.html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/ping":
                self._json({"ok": True, "builddir": str(bridge.builddir)})
            elif path.startswith("/api/job/"):
                job = bridge.jobs.get(path.rsplit("/", 1)[1])
                if not job:
                    self._json({"error": "unknown job"}, 404)
                else:
                    self._json(job)
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                self._json({"error": "bad request body"}, 400)
                return
            if path == "/api/profile-task":
                tid = body.get("task")
                if not isinstance(tid, int) or not 0 <= tid < len(bridge.tasks):
                    self._json({"error": "unknown task id"}, 400)
                    return
                task = bridge.tasks[tid]
                kind = classify_rule(task.edge.rule)
                cmd = edge_command(bridge.builddir, task.edge.display)
                if not cmd:
                    self._json({"error": "ninja -t commands returned nothing "
                                         "for " + task.edge.display})
                    return
                print(f"profiling task {tid}: {task.edge.display}")
                self._json(profile_command(bridge.builddir, cmd, kind))
            elif path == "/api/profile-build":
                jid, job = bridge.new_job()
                print("profiling whole build (all clang TUs, -ftime-trace)...")
                threading.Thread(target=profile_build_job, args=(bridge, job),
                                 daemon=True).start()
                self._json({"job": jid})
            else:
                self._json({"error": "not found"}, 404)

        def log_message(self, *a):  # quiet
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"interactive mode: serving report at {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("builddir", type=Path, help="ninja build directory")
    ap.add_argument("-o", "--output", type=Path, default=Path("report.html"))
    ap.add_argument("--title", default=None, help="report title")
    ap.add_argument("--no-deps", action="store_true",
                    help="skip `ninja -t deps` (generated-header dependencies)")
    ap.add_argument("--interactive", action="store_true",
                    help="serve the report from a live process with compiler "
                         "profiling (clang -ftime-trace) instead of writing a file")
    ap.add_argument("--port", type=int, default=8017, help="interactive mode port")
    ap.add_argument("--no-open", action="store_true",
                    help="interactive mode: don't open the browser automatically")
    args = ap.parse_args()

    builddir: Path = args.builddir.resolve()
    data, tasks, manifest = build_report(builddir, args.title, args.no_deps)
    html = render_html(data)

    if args.interactive:
        serve(Bridge(builddir, html, tasks, manifest), args.port, not args.no_open)
    else:
        args.output.write_text(html, encoding="utf-8")
        print(f"report written : {args.output}")


if __name__ == "__main__":
    main()
