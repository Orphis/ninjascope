# /// script
# requires-python = ">=3.10"
# ///
"""NinjaScope — visualize build parallelism of a Ninja build directory.

Reads build.ninja (dependency graph), .ninja_log (task timings) and the
discovered dependencies (ninja -t deps, for generated headers), then writes a
self-contained HTML report with:
  - the actual build timeline (Gantt) + CPU utilization,
  - a what-if scheduler simulating 1..8192+ cores on the dependency DAG,
  - the critical path, highlighted and with stats.

Usage:
  python ninjascope.py <build-dir> [-o report.html] [--title "My build"] [--no-deps]
  python ninjascope.py <build-dir> --interactive [--port N] [--no-open]

Interactive mode serves the same report from a local Python process and adds
compiler profiling: re-run any clang compile (or lld link) with -ftime-trace
and see the flame chart in-page, or profile every clang TU of the build and
aggregate the most expensive headers / templates / functions.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from array import array
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from heapq import heapify, heappop, heappush
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
    raw_out: list[str] = field(default_factory=list)   # explicit outputs as written ($out)
    raw_in: list[str] = field(default_factory=list)    # explicit inputs as written ($in)
    bindings: dict[str, str] = field(default_factory=dict)  # edge vars, unexpanded


class NinjaManifest:
    def __init__(self, builddir: Path):
        self.builddir = builddir
        self.scope: dict[str, str] = {}
        self.edges: list[Edge] = []
        self.rule_bindings: dict[str, dict[str, str]] = {}  # rule name -> raw vars
        self._norm_cache: dict[str, str] = {}

    def norm(self, path: str) -> str:
        """Canonical key for a path: absolute, forward slashes, casefolded."""
        cached = self._norm_cache.get(path)
        if cached is not None:
            return cached
        p = path.replace("\\", "/")
        absolute = p.startswith("/") or (
            len(p) >= 3 and p[1] == ":" and p[2] == "/"
            and ("A" <= p[0] <= "Z" or "a" <= p[0] <= "z"))
        if not absolute:
            p = self.builddir.as_posix() + "/" + p
        result = os.path.normpath(p).replace("\\", "/").casefold()
        self._norm_cache[path] = result
        return result

    def parse(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8", errors="replace")
        context: dict[str, str] | None = None  # bindings of the open rule/build block
        for line in self._logical_lines(text):
            if not line or line.lstrip().startswith("#"):
                continue
            if line[0] in " \t":
                # indented binding inside the current rule/build block, kept
                # unexpanded: rule vars like $FLAGS resolve per-edge later
                if context is not None and "=" in line:
                    key, _, value = line.partition("=")
                    context[key.strip()] = value.strip()
                continue
            keyword = line.split(None, 1)[0]
            context = None
            if keyword == "build":
                context = self._parse_build(line)
            elif keyword == "rule":
                name = line.split()[1]
                context = self.rule_bindings.setdefault(name, {})
            elif keyword in ("include", "subninja"):
                sub = self._expand_path_list(line.split(None, 1)[1])[0]
                subpath = Path(sub)
                if not subpath.is_absolute():
                    subpath = self.builddir / subpath
                self.parse(subpath)
            elif keyword in ("pool", "default"):
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
            if not stripped.endswith("$"):
                pending = ""
                yield stripped
                continue
            trailing = len(stripped) - len(stripped.rstrip("$"))
            if trailing % 2 == 1:
                pending = stripped[:-1]
                continue
            pending = ""
            yield stripped
        if pending:
            yield pending

    def _expand(self, s: str, resolve=None) -> str:
        """Expand $var / ${var} and unescape $$, '$ ', $: in a plain value.

        `resolve(name) -> str` overrides the default file-scope lookup.
        """
        if resolve is None:
            resolve = lambda name: self.scope.get(name, "")
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
                out.append(resolve(s[i + 1:j]))
                i = j + 1
            else:
                m = re.match(r"[A-Za-z0-9_.-]+", s[i:])
                out.append(resolve(m.group(0)) if m else "$")
                i += len(m.group(0)) if m else 0
        return "".join(out)

    @staticmethod
    def _quote(path: str) -> str:
        return f'"{path}"' if " " in path else path

    def command(self, edge: Edge) -> str | None:
        """The expanded command line for `edge` (None for phony edges).

        Resolution order matches ninja: $in/$out specials, then edge bindings,
        then rule bindings, then file scope. Values referenced from rule vars
        are themselves expanded recursively (with a cycle guard).
        """
        template = self.rule_bindings.get(edge.rule, {}).get("command")
        if not template:
            return None
        resolving: set[str] = set()

        def resolve(name: str) -> str:
            if name == "in":
                return " ".join(self._quote(p) for p in edge.raw_in)
            if name == "out":
                return " ".join(self._quote(p) for p in edge.raw_out)
            if name in resolving:
                return ""
            raw = edge.bindings.get(name)
            if raw is None:
                raw = self.rule_bindings.get(edge.rule, {}).get(name)
            if raw is None:
                return self.scope.get(name, "")
            resolving.add(name)
            try:
                return self._expand(raw, resolve)
            finally:
                resolving.discard(name)

        return self._expand(template, resolve)

    def _expand_path_list(self, s: str) -> list[str]:
        """Split a path list on unescaped spaces, expanding vars and escapes."""
        if "$" not in s:
            return s.split()
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

    def _parse_build(self, line: str) -> dict[str, str] | None:
        body = line[len("build"):]
        # first unescaped ':' separates outputs from rule+inputs
        colon = None
        if "$" not in body:
            found = body.find(":")
            colon = found if found >= 0 else None
        else:
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
            return None
        outs_part, ins_part = body[:colon], body[colon + 1:]

        def split_sections(part: str) -> list[str]:
            """Split on unescaped | and || into up to three sections."""
            if "$" not in part:
                return re.split(r"\|\|?", part)
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

        out_sections = split_sections(outs_part)
        explicit_outs = self._expand_path_list(out_sections[0])
        out_paths = list(explicit_outs)
        for sec in out_sections[1:]:
            out_paths += self._expand_path_list(sec)
        in_sections = split_sections(ins_part)
        rule_and_ins = self._expand_path_list(in_sections[0])
        if not rule_and_ins or not out_paths:
            return None
        rule = rule_and_ins[0]
        explicit_ins = rule_and_ins[1:]
        in_paths = list(explicit_ins)
        for sec in in_sections[1:]:
            in_paths += self._expand_path_list(sec)

        edge = Edge(
            rule=rule,
            outputs=[self.norm(p) for p in out_paths],
            inputs=[self.norm(p) for p in in_paths],
            display=out_paths[0],
            raw_out=explicit_outs,
            raw_in=explicit_ins,
        )
        self.edges.append(edge)
        return edge.bindings


# --------------------------------------------------------------------------
# .ninja_log parsing
# --------------------------------------------------------------------------

@dataclass
class LogRun:
    """One ninja invocation's worth of .ninja_log entries."""
    tasks: dict[str, tuple[int, int]]  # output key -> (start_ms, end_ms)
    mtime: int = 0                     # first entry's mtime (raw log units)

    def wall(self) -> int:
        if not self.tasks:
            return 0
        return (max(e for _, e in self.tasks.values())
                - min(s for s, _ in self.tasks.values()))

    def date(self) -> str | None:
        """Best-effort absolute date from the mtime field (ns in current
        ninja, seconds in old versions)."""
        v = self.mtime
        if v > 10**11:
            v //= 10**9
        if v <= 0:
            return None
        try:
            return datetime.fromtimestamp(v).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None


def parse_ninja_log(path: Path, manifest: NinjaManifest):
    """Parse .ninja_log into per-invocation runs plus latest durations.

    start/end times are milliseconds relative to each ninja invocation's
    start, and entries are appended in completion order — so end times are
    non-decreasing within one run, and a drop marks the next invocation.

    Returns (runs, latest, recompacted):
      runs         chronological list of LogRun (empty if structure unusable)
      latest       {output_key: dur_ms} from each output's last entry
      recompacted  True when the log looks recompacted (at most one entry per
                   output, rewritten in arbitrary order): durations are still
                   usable, run structure and timelines are not
    """
    norm = manifest.norm
    runs: list[LogRun] = []
    cur: dict[str, tuple[int, int]] = {}
    cur_mtime = 0
    latest: dict[str, int] = {}
    seen_repeat = False
    prev_end = -1
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
            try:
                start, end = int(fields[0]), int(fields[1])
            except ValueError:
                continue
            if end < prev_end and cur:
                runs.append(LogRun(cur, cur_mtime))
                cur = {}
            if not cur:
                try:
                    cur_mtime = int(fields[2])
                except ValueError:
                    cur_mtime = 0
            prev_end = end
            key = norm(fields[3])
            if key in latest:
                seen_repeat = True
            cur[key] = (start, end)
            latest[key] = max(0, end - start)
    if cur:
        runs.append(LogRun(cur, cur_mtime))
    # Recompaction rewrites the log with one entry per output in arbitrary
    # order, which the segmentation above shreds into many tiny "runs". A
    # genuine multi-run history rebuilds at least one output twice.
    recompacted = len(runs) > 4 and not seen_repeat
    if recompacted:
        runs = []
    return runs, latest, recompacted


# --------------------------------------------------------------------------
# discovered deps (generated headers)
# --------------------------------------------------------------------------

def ninja_deps_version(builddir: Path) -> int | None:
    """The .ninja_deps format version, or None if absent/unrecognized."""
    try:
        with (builddir / ".ninja_deps").open("rb") as f:
            header = f.read(16)
    except OSError:
        return None
    if len(header) < 16 or not header.startswith(b"# ninjadeps\n"):
        return None
    return int.from_bytes(header[12:16], "little")


def parse_ninja_deps(builddir: Path, manifest: NinjaManifest) -> dict[str, list[str]] | None:
    """Parse .ninja_deps (format v3/v4) directly, without running ninja.

    The file is a sequence of 4-byte-size-prefixed records: path records
    (string padded to 4-byte alignment + ~id checksum) assign sequential ids,
    deps records (high bit set in the size) hold [output_id, mtime, dep_ids...]
    with the last record per output winning. Returns None on anything
    unexpected so the caller can fall back to `ninja -t deps`.
    """
    version = ninja_deps_version(builddir)
    if version not in (3, 4) or array("I").itemsize != 4:
        return None
    mtime_words = 1 if version == 3 else 2  # v4 widened mtime to 64 bits
    try:
        data = (builddir / ".ninja_deps").read_bytes()
    except OSError:
        return None
    norm = manifest.norm
    paths: list[str] = []                    # path id -> normalized key
    dep_ids_by_out: dict[int, "array"] = {}
    pos, n = 16, len(data)
    try:
        while pos + 4 <= n:
            header = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
            size = header & 0x7FFFFFFF
            if pos + size > n:
                break  # truncated tail record (interrupted write); ignore
            if header & 0x80000000:
                ids = array("I", data[pos:pos + size])
                if sys.byteorder == "big":
                    ids.byteswap()
                dep_ids_by_out[ids[0]] = ids[1 + mtime_words:]
            else:
                checksum = int.from_bytes(data[pos + size - 4:pos + size], "little")
                if checksum != ~len(paths) & 0xFFFFFFFF:
                    return None  # corrupt / not the layout we expect
                raw = data[pos:pos + size - 4].rstrip(b"\x00")
                paths.append(norm(raw.decode("utf-8", errors="replace")))
            pos += size
        return {paths[out]: [paths[d] for d in dep_ids]
                for out, dep_ids in dep_ids_by_out.items()}
    except (IndexError, ValueError):
        return None


def run_deps_tool(builddir: Path) -> str | None:
    """Run `ninja -t deps` and return its stdout (None if ninja is missing)."""
    try:
        proc = subprocess.run(
            ["ninja", "-C", str(builddir), "-t", "deps"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        print("warning: ninja not found on PATH; skipping discovered deps "
              "(generated-header dependencies may be missing)", file=sys.stderr)
        return None
    return proc.stdout


def parse_deps_output(stdout: str | None, manifest: NinjaManifest) -> dict[str, list[str]]:
    """Parse `ninja -t deps` output into {target_output_key: [dep_keys...]}."""
    if stdout is None:
        return {}
    deps: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in stdout.splitlines():
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
    dl: int = 0  # index into the shared dep-lists table


def build_tasks(manifest: NinjaManifest, timing: dict[str, tuple[int, int]],
                durations: dict[str, int], discovered: dict[str, list[str]]):
    """Build the task list: `timing` holds start/end for the run shown on the
    timeline; `durations` holds every output's most recent duration and
    decides which edges count as built (plus their transitive deps)."""
    edge_by_output: dict[str, int] = {}
    for idx, e in enumerate(manifest.edges):
        for o in e.outputs:
            edge_by_output.setdefault(o, idx)

    # producers(path) -> set of real (non-phony) edge indices, seen through
    # phony. Cache entries are (sid, set) pairs: the sid is a value-canonical
    # id assigned once at creation, so the per-input hot path below is plain
    # dict gets — no frozenset hashing and no Python call on cache hits.
    set_ids: dict[frozenset[int], int] = {}
    prod_cache: dict[int, tuple[int, frozenset[int]]] = {}

    def intern(fs: frozenset[int]) -> tuple[int, frozenset[int]]:
        sid = set_ids.get(fs)
        if sid is None:
            sid = set_ids[fs] = len(set_ids)
        return (sid, fs)

    visiting: set[int] = set()   # DFS path, for cycle detection
    combo_cache: dict[frozenset[int], tuple[int, frozenset[int]]] = {}

    def edge_producers(idx: int) -> tuple[int, frozenset[int]]:
        ent = prod_cache.get(idx)
        if ent is not None:
            return ent
        if idx in visiting:
            return intern(frozenset())  # cycle guard; not cached for idx
        e = manifest.edges[idx]
        if e.rule != "phony":
            ent = prod_cache[idx] = intern(frozenset([idx]))
            return ent
        visiting.add(idx)
        pget = prod_cache.get
        jget = edge_by_output.get
        children: dict[int, frozenset[int]] = {}
        for p in e.inputs:
            j = jget(p)
            if j is not None:
                c = pget(j) or edge_producers(j)
                if c[1]:
                    children[c[0]] = c[1]
        visiting.discard(idx)
        if not children:
            ent = intern(frozenset())
        elif len(children) == 1:
            # alias phony: share the child's set instead of copying it
            ent = intern(next(iter(children.values())))
        else:
            key = frozenset(children)
            ent = combo_cache.get(key)
            if ent is None:
                ent = combo_cache[key] = intern(
                    frozenset().union(*children.values()))
        prod_cache[idx] = ent
        return ent

    # real-edge dependency sets (edge idx -> frozenset of edge idx). Whole
    # groups of edges combine the same producer sets (GN stamp fan-outs), so
    # the merged union is cached by the combination of producer-set ids and
    # shared instead of re-unioned per edge.
    merge_cache: dict[frozenset[int], frozenset[int]] = {}
    edge_deps: dict[int, frozenset[int]] = {}
    ebo_get = edge_by_output.get
    cache_get = prod_cache.get
    disc_get = discovered.get
    for idx, e in enumerate(manifest.edges):
        if e.rule == "phony":
            continue
        parts: dict[int, frozenset[int]] = {}
        for p in e.inputs:
            j = ebo_get(p)
            if j is not None:
                ent = cache_get(j) or edge_producers(j)
                if ent[1]:
                    parts[ent[0]] = ent[1]
        for o in e.outputs:
            for dep_path in disc_get(o, ()):
                j = ebo_get(dep_path)
                if j is not None:
                    ent = cache_get(j) or edge_producers(j)
                    if ent[1]:
                        parts[ent[0]] = ent[1]
        key = frozenset(parts)
        merged = merge_cache.get(key)
        if merged is None:
            merged = frozenset().union(*parts.values()) if parts else frozenset()
            merge_cache[key] = merged
        if idx in merged:  # self-dependency (output listed among own inputs)
            merged = merged - {idx}
        edge_deps[idx] = merged

    # keep edges that were built (in the log) plus anything they depend on;
    # a shared dep set only needs expanding once, not per referencing edge
    logged = {idx for idx, e in enumerate(manifest.edges)
              if e.rule != "phony" and any(o in durations for o in e.outputs)}
    keep: set[int] = set()
    expanded: set[int] = set()
    frontier = list(logged)
    while frontier:
        idx = frontier.pop()
        if idx in keep:
            continue
        keep.add(idx)
        ds = edge_deps.get(idx)
        if not ds:
            continue
        k = id(ds)  # sets are shared and kept alive by edge_deps
        if k in expanded:
            continue
        expanded.add(k)
        frontier += [d for d in ds if d not in keep]

    unlogged_kept = keep - logged
    order = sorted(keep)
    task_id = {edge_idx: i for i, edge_idx in enumerate(order)}
    # dep lists are interned: tasks sharing a dep set share one list (its id)
    dep_lists: list[list[int]] = []
    dep_index: dict[frozenset[int], int] = {}
    tasks: list[Task] = []
    for edge_idx in order:
        e = manifest.edges[edge_idx]
        eds = edge_deps[edge_idx]
        di = dep_index.get(eds)
        if di is None:
            di = dep_index[eds] = len(dep_lists)
            dep_lists.append(sorted(task_id[d] for d in eds))
        t = Task(id=task_id[edge_idx], edge=e, dl=di)
        for o in e.outputs:
            if o in timing:
                # built in the displayed run: timeline bar and duration agree
                t.start, t.end = timing[o]
                t.dur = max(0, t.end - t.start)
                break
        else:
            for o in e.outputs:
                if o in durations:
                    # not in the displayed run: latest known duration, no bar
                    t.dur = durations[o]
                    break
        tasks.append(t)
    return tasks, dep_lists, len(unlogged_kept)


class TaskGraph:
    """Two-level dependency structure over interned dep lists.

    Tasks point at a shared dep list (`Task.dl`); propagation runs through the
    few hundred unique lists instead of the millions of flattened task->task
    edges they expand to, and is built once and reused by every pass.
    """

    def __init__(self, tasks: list[Task], dep_lists: list[list[int]]):
        self.tasks = tasks
        self.dep_lists = dep_lists
        self.list_size = [len(l) for l in dep_lists]
        # dep-list ids that task j appears in (its outgoing edges, grouped)
        self.containing: list[list[int]] = [[] for _ in tasks]
        for li, l in enumerate(dep_lists):
            for d in l:
                self.containing[d].append(li)
        # tasks whose dep list is L (released together when L is satisfied)
        self.tasks_with_dl: list[list[int]] = [[] for _ in dep_lists]
        for t in tasks:
            self.tasks_with_dl[t.dl].append(t.id)


def topo_order(graph: TaskGraph) -> list[int]:
    tasks = graph.tasks
    pending = list(graph.list_size)
    order = [t.id for t in tasks if pending[t.dl] == 0]
    head = 0
    while head < len(order):
        i = order[head]
        head += 1
        for li in graph.containing[i]:
            pending[li] -= 1
            if pending[li] == 0:
                order += graph.tasks_with_dl[li]
    if len(order) != len(tasks):
        sys.exit("error: dependency graph has a cycle (corrupt manifest?)")
    return order


def critical_path(graph: TaskGraph, order: list[int]):
    """Longest path; returns (length_ms, [task ids along the path], slack per task)."""
    tasks = graph.tasks
    n = len(tasks)
    ef = [0] * n            # earliest finish
    best_pred = [-1] * n
    max_ef = [0] * len(graph.dep_lists)    # max ef over a dep list's members
    arg_ef = [-1] * len(graph.dep_lists)
    for i in order:
        t = tasks[i]
        ef[i] = max_ef[t.dl] + t.dur
        best_pred[i] = arg_ef[t.dl]
        for li in graph.containing[i]:
            if ef[i] > max_ef[li]:
                max_ef[li], arg_ef[li] = ef[i], i
    cp_len = max(ef, default=0)

    # backward pass for slack (deadline = cp_len)
    lf = [cp_len] * n       # latest finish
    min_ls = [cp_len] * len(graph.dep_lists)  # min latest-start over dl members
    for i in reversed(order):
        t = tasks[i]
        low = cp_len
        for li in graph.containing[i]:
            if min_ls[li] < low:
                low = min_ls[li]
        lf[i] = low
        if low - t.dur < min_ls[t.dl]:
            min_ls[t.dl] = low - t.dur
    slack = [lf[i] - ef[i] for i in range(n)]

    end = max(range(n), key=lambda i: ef[i], default=-1)
    path = []
    while end != -1:
        path.append(end)
        end = best_pred[end]
    return cp_len, list(reversed(path)), slack


def simulate(graph: TaskGraph, cores: float, tails: list[int]) -> int:
    """Greedy list scheduling (critical-path-first). Returns makespan in ms."""
    tasks = graph.tasks
    pending = list(graph.list_size)
    ready = [(-tails[t.id], t.id) for t in tasks if pending[t.dl] == 0]
    heapify(ready)
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
            for li in graph.containing[j]:
                pending[li] -= 1
                if pending[li] == 0:
                    for s in graph.tasks_with_dl[li]:
                        heappush(ready, (-tails[s], s))
    return makespan


def compute_tails(graph: TaskGraph, order: list[int]) -> list[int]:
    tasks = graph.tasks
    tails = [0] * len(tasks)
    tail_max = [0] * len(graph.dep_lists)  # max tail over tasks with dl == L
    for i in reversed(order):
        t = tasks[i]
        longest = 0
        for li in graph.containing[i]:
            if tail_max[li] > longest:
                longest = tail_max[li]
        tails[i] = t.dur + longest
        if tails[i] > tail_max[t.dl]:
            tail_max[t.dl] = tails[i]
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

def build_report(builddir: Path, title: str | None, no_deps: bool,
                 no_commands: bool = False, run: int | None = None):
    """Parse the build dir; return (data dict for the template, tasks, manifest).

    `run` is a 1-based index into the builds detected in .ninja_log (see
    --list-runs); None shows the last build on the timeline. Task durations
    always come from each output's most recent log entry, so the critical
    path / what-if analysis covers the whole DAG even for incremental runs.
    """
    manifest_path = builddir / "build.ninja"
    log_path = builddir / ".ninja_log"
    if not manifest_path.exists():
        sys.exit(f"error: {manifest_path} not found")
    if not log_path.exists():
        sys.exit(f"error: {log_path} not found (run a build first)")

    # discovered deps come from .ninja_deps, parsed directly when the format
    # is a known version. Otherwise fall back to the `ninja -t deps` tool —
    # an external process that only needs builddir, so it runs concurrently
    # with the (pure-Python) manifest parse below.
    deps_future = None
    deps_executor = None
    use_binary_deps = False
    if not no_deps:
        use_binary_deps = ninja_deps_version(builddir) in (3, 4)
        if not use_binary_deps:
            deps_executor = ThreadPoolExecutor(max_workers=1)
            deps_future = deps_executor.submit(run_deps_tool, builddir)

    manifest = NinjaManifest(builddir)
    manifest.parse(manifest_path)
    print(f"parsed {len(manifest.edges)} edges from build.ninja")

    runs, latest, recompacted = parse_ninja_log(log_path, manifest)
    sel_idx = None
    timing: dict[str, tuple[int, int]] = {}
    if runs:
        sel_idx = (run - 1) if run is not None else len(runs) - 1
        if not 0 <= sel_idx < len(runs):
            sys.exit(f"error: --run {run} out of range; the log contains "
                     f"{len(runs)} run(s) (see --list-runs)")
        timing = runs[sel_idx].tasks
    elif run is not None:
        sys.exit("error: --run given but the log has no usable run structure "
                 "(recompacted?)")
    if recompacted:
        print("warning: .ninja_log has been recompacted — per-build run "
              "structure is lost, so no observed timeline is available; "
              "analysis uses each output's most recent duration.",
              file=sys.stderr)
    elif len(runs) > 1:
        r = runs[sel_idx]
        when = r.date() or "unknown date"
        print(f".ninja_log contains {len(runs)} builds; timeline shows run "
              f"{sel_idx + 1}/{len(runs)} ({when}, {len(r.tasks)} tasks, wall "
              f"{r.wall() / 1000:.1f}s). Analysis uses each output's most "
              "recent duration across all builds.")

    discovered = {}
    if use_binary_deps:
        discovered = parse_ninja_deps(builddir, manifest)
        if discovered is None:  # corrupt mid-file: fall back to the tool
            discovered = parse_deps_output(run_deps_tool(builddir), manifest)
    elif deps_future is not None:
        discovered = parse_deps_output(deps_future.result(), manifest)
        deps_executor.shutdown()

    tasks, dep_lists, n_unlogged = build_tasks(manifest, timing, latest, discovered)
    if n_unlogged:
        print(f"warning: {n_unlogged} task(s) have no timing in .ninja_log "
              "(duration treated as 0)", file=sys.stderr)
    print(f"{len(tasks)} tasks with timing/dependency info")

    graph = TaskGraph(tasks, dep_lists)
    order = topo_order(graph)
    cp_len, cp_path, slack = critical_path(graph, order)
    tails = compute_tails(graph, order)

    timed = [t for t in tasks if t.start is not None]
    wall = (max(t.end for t in timed) - min(t.start for t in timed)
            if timed else None)
    work = sum(t.dur for t in tasks)

    # observed peak concurrency
    events = sorted([(t.start, 1) for t in timed] + [(t.end, -1) for t in timed])
    peak = cur = 0
    for _, delta in events:
        cur += delta
        peak = max(peak, cur)

    # once a core count reaches the critical-path bound, higher counts can't
    # improve — fill them in without simulating
    speedup = []
    makespan = None
    for n in CORE_OPTIONS:
        if makespan != cp_len:
            makespan = simulate(graph, n, tails)
        speedup.append({"cores": n, "makespan": makespan})
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
            "mixedLog": len(runs) > 1 or recompacted,
            "recompacted": recompacted,
            "selectedRun": None if sel_idx is None else sel_idx + 1,
            "runs": [{"tasks": len(r.tasks), "wall": r.wall(), "date": r.date()}
                     for r in runs],
            "speedup": speedup,
        },
        "rules": [{"name": r, "kind": classify_rule(r)} for r in rules],
        "criticalPath": cp_path,
    }
    # tasks are emitted columnar, with dependency lists interned: whole groups
    # of tasks share identical dep sets (GN stamp fan-outs especially), and
    # materializing each copy is what makes naive exports quadratic-ish
    cols: dict[str, list] = {k: [] for k in
                             ("name", "rule", "start", "end", "dur", "dl", "slack", "cp")}
    cmds: list[str | None] | None = None if no_commands else []
    for t in tasks:
        cols["name"].append(display_name(t))
        cols["rule"].append(rule_idx[t.edge.rule])
        cols["start"].append(t.start)
        cols["end"].append(t.end)
        cols["dur"].append(t.dur)
        cols["dl"].append(t.dl)
        cols["slack"].append(slack[t.id])
        cols["cp"].append(1 if t.id in cp_set else 0)
        if cmds is not None:
            cmds.append(manifest.command(t.edge))
    if cmds is not None:
        cols["cmd"] = cmds
    data["tasksCols"] = cols
    data["depLists"] = dep_lists

    if wall is not None:
        print(f"wall time      : {wall / 1000:.1f}s")
        print(f"total work     : {work / 1000:.1f}s  "
              f"(avg parallelism {work / max(wall, 1):.1f}x)")
    else:
        print(f"total work     : {work / 1000:.1f}s  (no timeline available)")
    print(f"critical path  : {cp_len / 1000:.1f}s  ({len(cp_path)} tasks, "
          f"max speedup {work / max(cp_len, 1):.1f}x)")
    return data, tasks, manifest


def render_html(data: dict, compress: bool | None = None, level: int = 6) -> str:
    """Embed the data payload; compress it when it is large.

    compress=None (auto) deflates payloads over 1 MB — the page inflates them
    with the browser-native DecompressionStream, so nothing else is needed.
    """
    template = (Path(__file__).parent / "template.html").read_text(encoding="utf-8")
    raw = json.dumps(data, separators=(",", ":"))
    if compress is None:
        compress = len(raw) > 1_000_000
    if compress:
        z = base64.b64encode(zlib.compress(raw.encode("utf-8"), level)).decode("ascii")
        payload = json.dumps({"z": z})
        print(f"payload        : {len(raw) / 1e6:.1f} MB JSON -> "
              f"{len(z) / 1e6:.1f} MB compressed (zlib level {level})")
    else:
        payload = raw.replace("</", "<\\/")
    return template.replace("/*__NINJASCOPE_DATA__*/null", payload)


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
    tmp = Path(tempfile.mkdtemp(prefix="ninjascope_prof_"))
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

    class Server(ThreadingHTTPServer):
        # No SO_REUSEADDR: binding an in-use port must fail loudly. With it
        # set (the http.server default), a second instance silently shares
        # the port on Windows and requests go to whichever instance wins.
        allow_reuse_address = False

    try:
        httpd = Server(("127.0.0.1", port or 0), Handler)
    except OSError:
        sys.exit(f"error: port {port} is already in use (another instance?) — "
                 "pass a different --port, or omit it for a random free port")
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    print(f"interactive mode: serving report at {url}  (Ctrl+C to stop)", flush=True)
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
    ap.add_argument("--run", type=int, default=None, metavar="N",
                    help="which build from .ninja_log to show on the timeline, "
                         "1-based (default: the last one; see --list-runs). "
                         "Analysis always uses each task's most recent duration")
    ap.add_argument("--list-runs", action="store_true",
                    help="list the builds detected in .ninja_log and exit")
    ap.add_argument("--no-commands", action="store_true",
                    help="omit per-task command lines from the report "
                         "(smaller output for very large builds)")
    ap.add_argument("--no-compress", action="store_true",
                    help="embed the data as plain JSON even when large "
                         "(readable/greppable report source)")
    ap.add_argument("--compress-level", type=int, choices=range(0, 10),
                    metavar="{0-9}", default=6,
                    help="zlib level for the embedded payload (default 6; "
                         "9 is smallest but slower; ignored with --no-compress)")
    ap.add_argument("--ignore-file", type=Path, default=None,
                    help="file of finding ids to suppress, one fnmatch-style "
                         "pattern per line, '#' comments (default: "
                         "<builddir>/.ninjascope-ignore if present)")
    ap.add_argument("--interactive", action="store_true",
                    help="serve the report from a live process with compiler "
                         "profiling (clang -ftime-trace) instead of writing a file")
    ap.add_argument("--port", type=int, default=None,
                    help="interactive mode port (default: a random free port)")
    ap.add_argument("--no-open", action="store_true",
                    help="interactive mode: don't open the browser automatically")
    args = ap.parse_args()

    builddir: Path = args.builddir.resolve()

    if args.list_runs:
        log_path = builddir / ".ninja_log"
        if not log_path.exists():
            sys.exit(f"error: {log_path} not found (run a build first)")
        # norm() only needs builddir, so no manifest parse is required here
        runs, latest, recompacted = parse_ninja_log(log_path, NinjaManifest(builddir))
        if recompacted:
            print(f"log has been recompacted: {len(latest)} outputs with "
                  "durations, no per-build run structure")
        elif not runs:
            print("no entries in .ninja_log")
        for i, r in enumerate(runs):
            marker = "  (default)" if i == len(runs) - 1 else ""
            print(f"run {i + 1:3d}: {r.date() or 'unknown date':>19}  "
                  f"{len(r.tasks):6d} tasks  wall {r.wall() / 1000:8.1f}s{marker}")
        return

    data, tasks, manifest = build_report(builddir, args.title, args.no_deps,
                                         args.no_commands, args.run)

    ignore_path = args.ignore_file or builddir / ".ninjascope-ignore"
    if args.ignore_file and not ignore_path.is_file():
        sys.exit(f"error: ignore file not found: {ignore_path}")
    if ignore_path.is_file():
        patterns = [ln.strip() for ln in
                    ignore_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
        data["meta"]["ignore"] = patterns
        print(f"ignore list    : {len(patterns)} pattern(s) from {ignore_path}")
    html = render_html(data, compress=False if args.no_compress else None,
                       level=args.compress_level)

    if args.interactive:
        serve(Bridge(builddir, html, tasks, manifest), args.port, not args.no_open)
    else:
        args.output.write_text(html, encoding="utf-8")
        print(f"report written : {args.output}")


if __name__ == "__main__":
    main()
