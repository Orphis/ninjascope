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
import math
import os
import random
import re
from array import array
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
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
MIN_RATE = 0.05  # a progress rate of 0 would make the scheduler's dt infinite


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
    src: int = 0                                        # index into NinjaManifest.files


class NinjaManifest:
    def __init__(self, builddir: Path):
        self.builddir = builddir
        self.scope: dict[str, str] = {}
        self.edges: list[Edge] = []
        self.rule_bindings: dict[str, dict[str, str]] = {}  # rule name -> raw vars
        self.pools: dict[str, dict[str, str]] = {}  # pool name -> raw vars (depth)
        self.files: list[str] = []  # parsed ninja files; Edge.src indexes here
        self._cur_file = 0
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
        prev_file = self._cur_file
        self._cur_file = len(self.files)
        self.files.append(path.as_posix())
        try:
            self._parse_text(text)
        finally:
            self._cur_file = prev_file

    def _parse_text(self, text: str) -> None:
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
            elif keyword == "pool":
                # the indented `depth = N` lands in this context, like rule vars
                context = self.pools.setdefault(line.split()[1], {})
            elif keyword == "default":
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

    def edge_pool(self, edge: Edge) -> str:
        """The pool this edge runs in ('' when unpooled).

        Same precedence as any other binding: edge, then rule, then file scope.
        """
        raw = edge.bindings.get("pool")
        if raw is None:
            raw = self.rule_bindings.get(edge.rule, {}).get("pool")
        if raw is None:
            raw = self.scope.get("pool")
        return self._expand(raw).strip() if raw else ""

    def pool_depth(self, name: str) -> int | None:
        """Concurrency limit of a pool, or None when it doesn't constrain.

        `console` is built into ninja (depth 1, plus console access) and is
        never declared in the manifest. A declared depth of 0 means unlimited.
        """
        if name == "console":
            return 1
        raw = self.pools.get(name, {}).get("depth")
        if raw is None:
            return None
        try:
            depth = int(self._expand(raw).strip())
        except ValueError:
            return None
        return depth if depth > 0 else None

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
            src=self._cur_file,
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


def simulate(graph: TaskGraph, cores: float, work: list[int],
             tails: list[int], rate=None,
             pools: tuple[list[int], list[float]] | None = None
             ) -> tuple[float, float]:
    """Greedy list scheduling (critical-path-first) with rate-based progress.

    Returns (makespan_ms, busy_core_ms).

    A task's duration depends on how crowded the machine is, which depends on
    the other tasks' durations — circular if scheduled directly. The way out is
    that the rate depends only on *global* occupancy, so everything running
    progresses at the same rate: run a virtual clock `v` with dv = r·dt, and a
    task admitted at v0 with work w always finishes at v0 + w regardless of what
    starts or stops in between. The running heap is keyed on that virtual finish
    and never needs re-keying; dt = Δv / r converts back to wall time. Only
    completions are events, so r is constant between them and dt lands exactly
    on the next one — this is exact, not a discretization.

    `rate` maps the number of running jobs to a progress rate in (0, 1]; with
    the default (None) and `work` set to measured durations this reproduces
    plain fixed-duration scheduling. Cores are one shared pool, so what slows a
    task down is simply how many others are running beside it.

    `pools` is (pool index per task, depth per pool); index 0 is the implicit
    unlimited pool. A full pool must not block admission of tasks in other
    pools, so ready tasks are kept in one heap per pool and admission takes the
    highest-priority task among the pools that still have room.
    """
    tasks = graph.tasks
    pool_of, depths = pools or ([0] * len(tasks), [float("inf")])
    npools = len(depths)
    pending = list(graph.list_size)
    heaps: list[list[tuple[int, int]]] = [[] for _ in range(npools)]
    for t in tasks:
        if pending[t.dl] == 0:
            heaps[pool_of[t.id]].append((-tails[t.id], t.id))
    for h in heaps:
        heapify(h)
    inuse = [0] * npools
    running: list[tuple[float, int]] = []
    now = v = makespan = busy = 0.0
    while any(heaps) or running:
        while len(running) < cores:
            best = -1
            best_key = 0
            for p in range(npools):
                h = heaps[p]
                if h and inuse[p] < depths[p] and (best < 0 or h[0][0] < best_key):
                    best, best_key = p, h[0][0]
            if best < 0:
                break
            _, i = heappop(heaps[best])
            inuse[best] += 1
            heappush(running, (v + work[i], i))
        if not running:
            break
        c = len(running)
        r = 1.0 if rate is None else min(1.0, max(rate(c), MIN_RATE))
        v_next = running[0][0]
        dt = (v_next - v) / r
        now += dt
        v = v_next
        busy += c * dt
        if now > makespan:
            makespan = now
        # release everything finishing at the same instant
        done = []
        while running and running[0][0] == v_next:
            done.append(heappop(running)[1])
        for j in done:
            inuse[pool_of[j]] -= 1
            for li in graph.containing[j]:
                pending[li] -= 1
                if pending[li] == 0:
                    for s in graph.tasks_with_dl[li]:
                        heappush(heaps[pool_of[s]], (-tails[s], s))
    return makespan, busy


def collect_pools(manifest: NinjaManifest, tasks: list[Task]):
    """Group tasks by the ninja pool they run in.

    Returns (pool index per task, depth per pool, info per pool) with index 0
    the implicit unlimited pool, or None when no declared pool actually binds.
    A pool with no more tasks than its depth can never delay anything, so it is
    dropped rather than shown as a constraint the user could act on.
    """
    idx: dict[str, int] = {}
    names: list[str] = []
    depths: list[float] = [float("inf")]
    members: list[list[int]] = [[]]
    pool_of = [0] * len(tasks)
    for t in tasks:
        name = manifest.edge_pool(t.edge)
        if not name:
            continue
        depth = manifest.pool_depth(name)
        if depth is None:
            continue
        p = idx.get(name)
        if p is None:
            p = idx[name] = len(names) + 1
            names.append(name)
            depths.append(depth)
            members.append([])
        pool_of[t.id] = p
        members[p].append(t.id)

    keep = [p for p in range(1, len(names) + 1) if len(members[p]) > depths[p]]
    if not keep:
        return None
    remap = {p: i + 1 for i, p in enumerate(keep)}
    pool_of = [remap.get(p, 0) for p in pool_of]
    info = [{"name": names[p - 1], "depth": depths[p], "tasks": members[p]}
            for p in keep]
    return pool_of, [float("inf")] + [depths[p] for p in keep], info


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
    if "STATIC_LIBRARY" in r or r.startswith("AR") or "_AR_" in r or "ALINK" in r:
        return "archive"
    if "LINKER" in r or "LINK" in r:
        return "link"
    if "COMPILER" in r or r.startswith(("CXX", "CC", "C_", "OBJC", "RC", "ASM")):
        return "compile"
    return "other"


# --------------------------------------------------------------------------
# target inference (grouping tasks into libraries / executables)
# --------------------------------------------------------------------------

_CMAKE_DIR_RE = re.compile(r"cmakefiles/([^/]+)\.dir/")
_CMAKE_DIR_RAW_RE = re.compile(r"CMakeFiles/([^/]+)\.dir/", re.IGNORECASE)
_LIB_SUFFIXES = (".a", ".so", ".dylib", ".lib", ".dll", ".exe")


def _anchor_name(edge: Edge) -> str:
    """Best-effort target name for an archive/link edge."""
    if "__" in edge.rule:
        # CMake >= 3.18 embeds the target: CXX_STATIC_LIBRARY_LINKER__foo_Debug
        cand = edge.rule.split("__", 1)[1]
        if "_" in cand:
            cand = cand.rsplit("_", 1)[0]
        if cand:
            return cand
    base = edge.display.replace("\\", "/").rsplit("/", 1)[-1]
    for suf in _LIB_SUFFIXES:
        if base.casefold().endswith(suf):
            base = base[: -len(suf)]
            break
    if base.startswith("lib") and len(base) > 3:
        base = base[3:]
    return base or edge.display


def infer_targets(manifest: NinjaManifest, tasks: list[Task],
                  dep_lists: list[list[int]], order: list[int],
                  cp_set: set[int]):
    """Group tasks into build targets; return (targets, task_target).

    targets is a list of dicts (name/kind/dir/selfMs/start/end/count/cp/deps),
    task_target maps task id -> target index. Returns (None, None) when no
    meaningful grouping exists. Signals, strongest first: the per-target
    .ninja file GN emits via subninja, CMake's CMakeFiles/<target>.dir/
    object paths, archive/link anchor edges, CMake utility-target
    (add_custom_target) aliases claiming their codegen, generated sources
    claimed via their target's object-order phony, single-consumer
    codegen folded into the consuming target, remaining codegen grouped by
    output directory into its own targets, then nearest consuming target for
    stamps/leftovers and directory grouping for the rest. Matching runs on
    normalized (casefolded) paths; names come from as-written paths.
    """
    n = len(tasks)
    if n == 0:
        return None, None
    prefix = manifest.builddir.as_posix() + "/"
    prefix_cf = prefix.casefold()

    def rel(path: str) -> str:
        p = path.replace("\\", "/")
        if p.casefold().startswith(prefix_cf):
            p = p[len(prefix):]
        return p

    groups: list[dict] = []
    group_key: dict[tuple, int] = {}
    group_of = [-1] * n

    def get_group(key: tuple, name: str, dirname: str) -> int:
        gi = group_key.get(key)
        if gi is None:
            gi = group_key[key] = len(groups)
            groups.append({"name": name, "kind": "group", "dir": dirname})
        return gi

    # GN emits one .ninja file per target via subninja; when build edges come
    # from more than two files (root + toolchain at minimum), the file is an
    # exact target assignment. Root-file edges (stamps/regen) fall through.
    gn_mode = len({t.edge.src for t in tasks}) > 2
    kinds = [classify_rule(t.edge.rule) for t in tasks]

    for t in tasks:
        e = t.edge
        if gn_mode and e.src != 0:
            name = rel(manifest.files[e.src])
            if name.endswith(".ninja"):
                name = name[: -len(".ninja")]
            if name.startswith("obj/"):
                name = name[len("obj/"):]
            dirname = name.rsplit("/", 1)[0] if "/" in name else ""
            group_of[t.id] = get_group(("src", e.src), name, dirname)
        elif e.outputs:
            m = _CMAKE_DIR_RE.search(e.outputs[0])
            if m:
                out_rel = rel(e.display)
                raw = _CMAKE_DIR_RAW_RE.search(out_rel)
                name = raw.group(1) if raw else m.group(1)
                dirname = out_rel[: raw.start()].rstrip("/") if raw else ""
                group_of[t.id] = get_group(("cmake", m.group(1)), name, dirname)

    # archive/link anchors name and type their group. A CMake anchor's
    # objects live under CMakeFiles/<t>.dir/ among its inputs — merge it into
    # that group (covers old CMake with generic linker rule names too).
    for t in tasks:
        ki = kinds[t.id]
        if ki not in ("archive", "link"):
            continue
        gi = group_of[t.id]
        if gi < 0:
            votes: dict[tuple, int] = {}
            for p in t.edge.inputs:
                m = _CMAKE_DIR_RE.search(p)
                if m:
                    key = ("cmake", m.group(1))
                    if key in group_key:
                        votes[key] = votes.get(key, 0) + 1
            if votes:
                best = max(votes, key=lambda k: (votes[k], -group_key[k]))
                if votes[best] * 2 >= sum(votes.values()):
                    gi = group_key[best]
        if gi < 0:
            out_rel = rel(t.edge.display)
            dirname = out_rel.rsplit("/", 1)[0] if "/" in out_rel else ""
            gi = get_group(("anchor", t.id), _anchor_name(t.edge), dirname)
        group_of[t.id] = gi
        g = groups[gi]
        if ki == "link":
            out_cf = t.edge.display.casefold()
            libish = (out_cf.endswith((".so", ".dylib", ".dll", ".a", ".lib"))
                      or ".so." in out_cf)
            g["kind"] = "lib" if libish else "exe"
        elif g["kind"] != "exe":
            g["kind"] = "lib"

    # CMake utility targets (add_custom_target) own their codegen outputs.
    # ninja spells one as an alias `build <name>: phony CMakeFiles/<name> …`
    # over a stamp `CMakeFiles/<name>` — a phony over the target's DEPENDS
    # outputs, or a real edge when the target runs its own COMMAND. Claiming
    # these first keeps deliberately separate codegen targets separate; a
    # generated file that is merely a library source stays unclaimed here
    # and folds into the library below.
    if not gn_mode:
        out_task: dict[str, int] = {}
        for t in tasks:
            for o in t.edge.outputs:
                out_task.setdefault(o, t.id)
        phony_ins: dict[str, list[str]] = {}
        for e in manifest.edges:
            if e.rule == "phony" and e.outputs:
                phony_ins.setdefault(e.outputs[0], e.inputs)
        claim: dict[int, str] = {}      # task id -> owning stamp path
        contested: set[int] = set()
        stamp_name: dict[str, str] = {}
        for e in manifest.edges:
            if e.rule != "phony" or not e.outputs or not e.inputs:
                continue
            want = "/cmakefiles/" + e.outputs[0].rsplit("/", 1)[-1]
            stamp = next((p for p in e.inputs if p.endswith(want)), None)
            if stamp is None:
                continue
            stamp_name.setdefault(stamp, rel(e.display).rsplit("/", 1)[-1])
            ti = out_task.get(stamp)
            owned = [ti] if ti is not None else [
                out_task[p] for p in phony_ins.get(stamp, ()) if p in out_task]
            for ti in owned:
                if group_of[ti] >= 0 or kinds[ti] != "codegen":
                    continue
                if claim.setdefault(ti, stamp) != stamp:
                    contested.add(ti)
        for ti, stamp in claim.items():
            if ti in contested:
                continue
            out_rel = rel(tasks[ti].edge.display)
            dirname = out_rel.rsplit("/", 1)[0] if "/" in out_rel else ""
            gi = get_group(("util", stamp), stamp_name[stamp], dirname)
            groups[gi]["kind"] = "gen"
            group_of[ti] = gi

        # a target's own generated sources appear as direct file inputs of
        # its cmake_object_order_depends_target_<t> phony (dependency targets
        # appear there as aliases, not files): claim them for the target,
        # like CMake attaches the add_custom_command() that produces them.
        oo = "cmake_object_order_depends_target_"
        own: dict[int, int] = {}        # task id -> owning group
        shared: set[int] = set()
        for e in manifest.edges:
            if e.rule != "phony" or not e.outputs:
                continue
            base = e.outputs[0].rsplit("/", 1)[-1]
            if not base.startswith(oo):
                continue
            gi = group_key.get(("cmake", base[len(oo):]))
            if gi is None:
                continue
            for p in e.inputs:
                ti = out_task.get(p)
                if ti is None or group_of[ti] >= 0 or kinds[ti] != "codegen":
                    continue
                if own.setdefault(ti, gi) != gi:
                    shared.add(ti)
        for ti, gi in own.items():
            if ti not in shared:
                group_of[ti] = gi

    # the consumer graph, used by the codegen fold below and the
    # nearest-consumer walk after it
    succs: list[list[int]] = [[] for _ in range(n)]
    for t in tasks:
        for d in dep_lists[t.dl]:
            succs[d].append(t.id)

    # remaining codegen: a custom command consumed by a single target is a
    # generated source of that target — fold it in, matching how CMake
    # attaches an unanchored add_custom_command() to the consuming target.
    # Codegen with no assigned consumers or several consuming targets becomes
    # its own target grouped by output directory, keeping codegen chains
    # visible instead of dissolving them into their consumers. Reverse
    # topological order so chains resolve consumer-first.
    for i in reversed(order):
        if group_of[i] >= 0 or kinds[i] != "codegen":
            continue
        consumers = {group_of[s] for s in succs[i] if group_of[s] >= 0}
        if len(consumers) == 1:
            group_of[i] = consumers.pop()
            continue
        out_rel = rel(tasks[i].edge.display)
        dirname = out_rel.rsplit("/", 1)[0] if "/" in out_rel else ""
        parent = dirname.rsplit("/", 1)[0] if "/" in dirname else ""
        gi = get_group(("gen", dirname), dirname or "(generated)", parent)
        groups[gi]["kind"] = "gen"
        group_of[i] = gi

    # remaining custom commands / stamps: assign to the nearest consuming
    # assigned target. Among candidates at the minimal distance prefer a
    # group whose name appears in the task's own path (generated/<target>/…),
    # then the group with the most consumers there, then the smallest group
    # index — deterministic either way.
    INF = 1 << 30
    dist = [INF] * n
    nearest = [-1] * n
    for i in reversed(order):
        if group_of[i] >= 0:
            dist[i] = 0
            nearest[i] = group_of[i]
            continue
        bd = INF
        votes: dict[int, int] = {}
        for s in succs[i]:
            ds = dist[s]
            if ds >= INF:
                continue
            if ds + 1 < bd:
                bd = ds + 1
                votes = {nearest[s]: 1}
            elif ds + 1 == bd:
                votes[nearest[s]] = votes.get(nearest[s], 0) + 1
        if votes:
            dist[i] = bd
            cands = list(votes)
            if len(cands) > 1:
                comps = set(rel(tasks[i].edge.display).split("/"))
                named = [g for g in cands if groups[g]["name"] in comps]
                if named:
                    cands = named
            nearest[i] = max(cands, key=lambda g: (votes[g], -g))
    for i in range(n):
        if group_of[i] < 0 and nearest[i] >= 0:
            group_of[i] = nearest[i]

    # leftovers with no downstream anchor: group by directory
    for t in tasks:
        if group_of[t.id] >= 0:
            continue
        parts = rel(t.edge.display).split("/")
        dirname = "/".join(parts[:-1][:2])
        group_of[t.id] = get_group(("dir", dirname), dirname or "(top level)",
                                   dirname)

    if len(groups) < 2 or len(groups) > 0.8 * n:
        return None, None

    for g in groups:
        g.update(selfMs=0, count=0, start=None, end=None, cp=0)
    dep_sets: list[set[int]] = [set() for _ in groups]
    for t in tasks:
        gi = group_of[t.id]
        g = groups[gi]
        g["selfMs"] += t.dur
        g["count"] += 1
        if t.start is not None:
            g["start"] = t.start if g["start"] is None else min(g["start"], t.start)
            g["end"] = t.end if g["end"] is None else max(g["end"], t.end)
        if t.id in cp_set:
            g["cp"] = 1
        ds = dep_sets[gi]
        for d in dep_lists[t.dl]:
            gd = group_of[d]
            if gd != gi:
                ds.add(gd)
    for gi, g in enumerate(groups):
        g["deps"] = sorted(dep_sets[gi])
    return groups, group_of


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

    # observed peak concurrency, plus the plateau the build actually sat at.
    # .ninja_log records neither -j nor the machine, and the core count of
    # whatever generates the report is often not the one that built: a CI build
    # inspected locally is a normal workflow. The time-weighted p95 of
    # concurrency is the job-slot estimate — the plateau, without the odd
    # instant where ninja momentarily overshoots setting it.
    events = sorted([(t.start, 1) for t in timed] + [(t.end, -1) for t in timed])
    peak = cur = 0
    at_conc: dict[int, int] = {}
    prev_t = events[0][0] if events else 0
    for t_ev, delta in events:
        if t_ev > prev_t and cur > 0:
            at_conc[cur] = at_conc.get(cur, 0) + (t_ev - prev_t)
        prev_t = t_ev
        cur += delta
        peak = max(peak, cur)
    job_slots = None
    if at_conc:
        busy_ms = sum(at_conc.values())
        acc = 0
        for c in sorted(at_conc):
            acc += at_conc[c]
            if acc >= 0.95 * busy_ms:
                job_slots = c
                break

    # pools are a hard constraint ninja enforces, so the baked curve honors
    # them; without them the simulator promises parallelism the build can't take
    pool_data = collect_pools(manifest, tasks)
    sched_pools = pool_data[:2] if pool_data else None
    if pool_data:
        print("pools          : " + ", ".join(
            f"{p['name']} (depth {p['depth']}, {len(p['tasks'])} tasks)"
            for p in pool_data[2]))

    # once a core count reaches the floor, higher counts can't improve — fill
    # them in without simulating. That floor is the critical path, unless a pool
    # serializes work below it
    work_per_task = [t.dur for t in tasks]
    floor = cp_len
    if sched_pools:
        floor = round(simulate(graph, float("inf"), work_per_task, tails,
                               pools=sched_pools)[0])
    speedup = []
    makespan = None
    for n in CORE_OPTIONS:
        if makespan != floor:
            makespan = round(simulate(graph, n, work_per_task, tails,
                                      pools=sched_pools)[0])
        speedup.append({"cores": n, "makespan": makespan})
    speedup.append({"cores": None, "makespan": floor})  # infinite cores

    rules = sorted({t.edge.rule for t in tasks})
    rule_idx = {r: i for i, r in enumerate(rules)}
    cp_set = set(cp_path)

    targets = task_target = None
    try:
        targets, task_target = infer_targets(manifest, tasks, dep_lists,
                                             order, cp_set)
    except Exception as exc:  # inference must never break report generation
        print(f"warning: target inference failed ({exc}); target views "
              "disabled", file=sys.stderr)

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
            "jobSlots": job_slots,
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
    if pool_data:
        # member lists rather than a per-task column: pools normally hold a
        # handful of link steps, so this is bytes instead of one per task
        data["pools"] = pool_data[2]

    if targets is not None:
        cols["target"] = task_target
        data["targetsCols"] = {k: [g[k] for g in targets] for k in
                               ("name", "kind", "dir", "selfMs", "start",
                                "end", "count", "cp", "deps")}
        data["meta"]["targetCount"] = len(targets)
        n_lib = sum(1 for g in targets if g["kind"] == "lib")
        n_exe = sum(1 for g in targets if g["kind"] == "exe")
        print(f"targets        : {len(targets)} inferred "
              f"({n_lib} libs, {n_exe} exes)")

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
_OUT_TOKEN = re.compile(r'([-/])OUT:("[^"]+"|\S+)', re.IGNORECASE)


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
# contention calibration
#
# How much slower does a task run because other tasks are running? The obvious
# answer — compare tasks that ran when the machine was busy against tasks that
# ran when it was quiet, straight out of .ninja_log — does not survive contact
# with real data. A build is either saturated or nearly idle, with almost
# nothing in between, and the idle stretches are the serialized head and the
# final link: not a fair sample of anything. So the contrast is *created*: take
# a handful of the build's own commands, re-run them alone and again with the
# machine held at known load, and time them.
# --------------------------------------------------------------------------

CONTENTION_FILE = ".ninjascope-contention.json"
# Machine load to probe at, as a fraction of the job slots. The isolated run is
# the baseline; f(1/cores) is within a percent of 1 for any realistic core count.
# 1.5 is deliberately past the job count the build used: without a measurement
# up there, "would fewer jobs have been faster?" has no evidence behind it, and
# the model would have to either extrapolate or stay silent.
CALIB_LEVELS = (0.25, 0.5, 1.0, 1.5)


def harvest_commands(builddir: Path, manifest: NinjaManifest) -> dict[str, str]:
    """Output key -> the exact command ninja runs, from `ninja -t commands`."""
    proc = subprocess.run(["ninja", "-C", str(builddir), "-t", "commands"],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    out: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _O_TOKEN.search(line)
        if m:
            out[manifest.norm(m.group(1).strip('"'))] = line
    return out


def redirect_output(cmd: str, tmp: Path) -> str | None:
    """Rewrite the output path so a re-run leaves the build tree alone.

    Handles `-o <path>` (clang, gcc) and `/OUT:<path>` (MSVC link.exe, lib.exe).
    Returns None when neither is present, and that must stay a hard skip rather
    than a best effort: plenty of archivers take the output positionally
    (`llvm-ar qc libfoo.a …`) and some rules delete the real artifact first, so
    a command we can't positively redirect is a command we must not run.
    """
    m = _O_TOKEN.search(cmd)
    if m:
        ext = Path(m.group(1).strip('"')).suffix or ".out"
        return cmd[:m.start()] + f'-o {_quoted(tmp / ("out" + ext))}' + cmd[m.end():]
    m = _OUT_TOKEN.search(cmd)
    if m:
        ext = Path(m.group(2).strip('"')).suffix or ".out"
        return (cmd[:m.start()] + f'{m.group(1)}OUT:{_quoted(tmp / ("out" + ext))}'
                + cmd[m.end():])
    return None


def total_ram() -> int | None:
    """Physical RAM in bytes, or None where we can't tell."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", wintypes.DWORD),
                            ("dwMemoryLoad", wintypes.DWORD),
                            ("ullTotalPhys", ctypes.c_uint64),
                            ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64),
                            ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64),
                            ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.WinDLL("kernel32").GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullTotalPhys)
            return None
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True)
            return int(out.stdout.strip())
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except Exception:
        pass
    return None


def pick_probes(tasks: list[Task], cmd_by_output: dict[str, str],
                per_kind: int = 3) -> list[dict]:
    """Representative tasks to time: per rule kind, the median-duration step
    plus the heaviest ones.

    Steps under 300 ms are dominated by process startup, which no amount of
    contention changes; steps over 10 s make the sweep too slow to sit through.
    """
    by_kind: dict[str, list[Task]] = {}
    for t in tasks:
        if not 300 <= t.dur <= 10_000:
            continue
        if not any(o in cmd_by_output for o in t.edge.outputs):
            continue
        by_kind.setdefault(classify_rule(t.edge.rule), []).append(t)

    probes: list[dict] = []
    for kind, group in sorted(by_kind.items()):
        group.sort(key=lambda t: t.dur)
        picks = {len(group) // 2}
        for k in range(1, per_kind):
            picks.add(max(0, len(group) - k))
        for i in sorted(picks)[:per_kind]:
            t = group[i]
            cmd = next(cmd_by_output[o] for o in t.edge.outputs
                       if o in cmd_by_output)
            probes.append({"task": t.id, "kind": kind, "dur": t.dur,
                           "name": t.edge.display, "cmd": cmd})
    return probes


def pick_memory_probes(tasks: list[Task], cmd_by_output: dict[str, str],
                       per_kind: int = 3) -> list[dict]:
    """Tasks to sample peak RSS from: per kind, the heaviest plus the median.

    No duration floor, unlike the timing probes — a 165 ms link still shows its
    footprint, and link steps are usually the memory hogs. A step whose output
    can't be redirected is skipped outright rather than run against the real
    build tree, which in practice excludes archivers that take their output
    positionally.
    """
    by_kind: dict[str, list[Task]] = {}
    for t in tasks:
        if t.dur <= 0:
            continue
        cmd = next((cmd_by_output[o] for o in t.edge.outputs
                    if o in cmd_by_output), None)
        if cmd is None or redirect_output(cmd, Path(".")) is None:
            continue
        by_kind.setdefault(classify_rule(t.edge.rule), []).append(t)

    probes: list[dict] = []
    for kind, group in sorted(by_kind.items()):
        group.sort(key=lambda t: t.dur)
        # the largest step dominates the footprint; the median says whether the
        # kind is uniform or the big one is an outlier
        picks = sorted({len(group) - 1, len(group) // 2,
                        max(0, len(group) - 2)})[-per_kind:]
        for i in picks:
            t = group[i]
            cmd = next(cmd_by_output[o] for o in t.edge.outputs
                       if o in cmd_by_output)
            probes.append({"task": t.id, "kind": kind, "dur": t.dur,
                           "name": t.edge.display, "cmd": cmd})
    return probes


def _peak_memory_reader(proc: subprocess.Popen):
    """Attach a peak-memory meter to a just-started process tree.

    Returns a zero-argument callable giving peak bytes (or None). Windows gets
    an exact whole-tree figure from a job object; POSIX uses the rusage of
    waited-for children, which is only meaningful when nothing else is running —
    which is exactly when this is used (the isolated probe runs).
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                            ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD),
                            ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t),
                            ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t),
                            ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in
                            ("ReadOperationCount", "WriteOperationCount",
                             "OtherOperationCount", "ReadTransferCount",
                             "WriteTransferCount", "OtherTransferCount")]

            class EXT(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC),
                            ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            job = k32.CreateJobObjectW(None, None)
            if not job:
                return lambda: None
            # the shell is the child and the compiler its grandchild; both land
            # in the job, and neither has allocated anything yet at this point
            if not k32.AssignProcessToJobObject(job, int(proc._handle)):
                k32.CloseHandle(job)
                return lambda: None

            def read():
                info = EXT()
                ok = k32.QueryInformationJobObject(
                    job, 9, ctypes.byref(info), ctypes.sizeof(info), None)
                peak = int(info.PeakJobMemoryUsed) if ok else None
                k32.CloseHandle(job)
                return peak or None
            return read
        except Exception:
            return lambda: None
    try:
        import resource
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss

        def read():
            after = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
            if after <= before:
                return None
            scale = 1 if sys.platform == "darwin" else 1024  # bytes vs KB
            return after * scale
        return read
    except Exception:
        return lambda: None


def time_command(builddir: Path, cmd: str, measure_mem: bool = False
                 ) -> tuple[float | None, int | None]:
    """Run one build command with its output redirected away from the tree.

    Returns (wall_ms, peak_bytes); wall_ms is None if the command failed, so a
    broken probe drops out of the fit instead of poisoning it.
    """
    tmp = Path(tempfile.mkdtemp(prefix="ninjascope_calib_"))
    try:
        modified = redirect_output(cmd, tmp)
        if modified is None:
            return None, None
        t0 = time.perf_counter()
        proc = subprocess.Popen(modified, shell=True, cwd=builddir,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        read_peak = _peak_memory_reader(proc) if measure_mem else None
        try:
            rc = proc.wait(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            return None, None
        ms = (time.perf_counter() - t0) * 1000
        peak = read_peak() if read_peak else None
        return (ms if rc == 0 else None), peak
    except Exception:
        return None, None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class LoadGenerator:
    """Background load built from the build's own commands.

    A synthetic CPU spinner would contend on ALUs but not on the memory
    controller, the last-level cache or the page cache — which is most of what
    actually slows parallel compiles — so it would understate contention badly.
    Real commands it is.
    """

    def __init__(self, builddir: Path, cmds: list[str]):
        self.builddir = builddir
        self.cmds = cmds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._procs: dict[int, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def _worker(self, seed: int) -> None:
        rng = random.Random(seed)
        tmp = Path(tempfile.mkdtemp(prefix="ninjascope_load_"))
        try:
            while not self._stop.is_set():
                modified = redirect_output(rng.choice(self.cmds), tmp)
                if modified is None:
                    continue
                proc = subprocess.Popen(modified, shell=True, cwd=self.builddir,
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                with self._lock:
                    self._procs[seed] = proc
                try:
                    proc.wait()
                except Exception:
                    pass
                with self._lock:
                    self._procs.pop(seed, None)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def start(self, n: int) -> None:
        self._stop.clear()
        for i in range(n):
            th = threading.Thread(target=self._worker, args=(i,), daemon=True)
            th.start()
            self._threads.append(th)
        if n:
            time.sleep(2.0)  # let the load reach steady state before timing

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for proc in list(self._procs.values()):
                try:
                    proc.kill()
                except Exception:
                    pass
        for th in self._threads:
            th.join(timeout=30)
        self._threads.clear()


def _huber(x: float, delta: float = 0.2) -> float:
    a = abs(x)
    return 0.5 * x * x if a <= delta else delta * (a - 0.5 * delta)


def fit_inflation(samples: list[dict], cores: int) -> dict | None:
    """Fit f(u) = 1 + beta * u**gamma to timed probe runs.

    `samples` are {"probe": i, "kind": k, "c": concurrent jobs, "ms": t}; each
    probe's isolated runs give its baseline, so the fit sees only ratios and the
    probes' wildly different sizes cancel out.

    Load is measured as *excess* concurrency, u = (c - 1) / (cores - 1): 0 for a
    job running alone and 1 at the job count the build used. That makes the
    isolated baseline exact by construction — f(1 job) = 1 — which the "at 1
    core, nothing is competing" property of the simulator depends on. Using
    c/cores instead leaves a percent or so of phantom contention down there.

    Bounded by construction: the caller clamps u at the highest level actually
    timed, so the curve is never evaluated on a load nothing was measured at —
    which is what makes it safe behind a slider that goes to 8192 cores.
    """
    span = max(cores - 1, 1)
    base: dict[int, list[float]] = {}
    for s in samples:
        if s["c"] <= 1:
            base.setdefault(s["probe"], []).append(s["ms"])
    obs = []
    for s in samples:
        if s["c"] <= 1 or s["probe"] not in base:
            continue
        b = sum(base[s["probe"]]) / len(base[s["probe"]])
        if b > 0:
            obs.append(((s["c"] - 1) / span, math.log(s["ms"] / b), s["kind"]))
    if len(obs) < 8:
        return None

    def loss(beta: float, gamma: float, rows=None) -> float:
        rows = obs if rows is None else rows
        total = 0.0
        for u, ly, _ in rows:
            total += _huber(ly - math.log1p(beta * u ** gamma))
        return total

    def golden(f, lo, hi, iters=40):
        phi = (math.sqrt(5) - 1) / 2
        c, d = hi - phi * (hi - lo), lo + phi * (hi - lo)
        fc, fd = f(c), f(d)
        for _ in range(iters):
            if fc < fd:
                hi, d, fd = d, c, fc
                c = hi - phi * (hi - lo)
                fc = f(c)
            else:
                lo, c, fc = c, d, fd
                d = lo + phi * (hi - lo)
                fd = f(d)
        return (lo + hi) / 2

    def best_beta(gamma: float, rows=None) -> float:
        return golden(lambda b: loss(b, gamma, rows), 0.0, 3.0, 32)

    log_gamma = golden(lambda lg: loss(best_beta(math.exp(lg)), math.exp(lg)),
                       math.log(0.2), math.log(4.0), 24)
    gamma = math.exp(log_gamma)
    beta = best_beta(gamma)

    # goodness of fit on the log ratios, and the model-free per-level table that
    # lets anyone check the curve without trusting its shape
    mean = sum(ly for _, ly, _ in obs) / len(obs)
    ss_tot = sum((ly - mean) ** 2 for _, ly, _ in obs)
    ss_res = sum((ly - math.log1p(beta * u ** gamma)) ** 2 for u, ly, _ in obs)
    levels = []
    for u in sorted({round(u, 4) for u, _, _ in obs}):
        rows = [ly for uu, ly, _ in obs if round(uu, 4) == u]
        levels.append({"u": u, "jobs": round(1 + u * span), "n": len(rows),
                       "inflation": round(math.exp(sum(rows) / len(rows)), 4),
                       "model": round(1 + beta * u ** gamma, 4)})
    per_kind = {}
    for kind in sorted({k for _, _, k in obs}):
        rows = [r for r in obs if r[2] == kind]
        if len(rows) >= 4:
            per_kind[kind] = round(best_beta(gamma, rows), 4)

    return {"beta": round(beta, 4), "gamma": round(gamma, 4),
            # the highest load actually measured: the curve is clamped there so
            # it is never evaluated on loads nothing was timed at
            "uMax": max(u for u, _, _ in obs),
            "r2": round(1 - ss_res / ss_tot, 4) if ss_tot > 0 else None,
            "samples": len(obs), "levels": levels, "perKind": per_kind}


def calibrate(builddir: Path, manifest: NinjaManifest, tasks: list[Task],
              cores: int, repeats: int = 2, per_kind: int = 3,
              progress=None) -> dict:
    """Time representative build steps alone and under load; fit the curve."""
    cmd_by_output = harvest_commands(builddir, manifest)
    if not cmd_by_output:
        return {"error": "`ninja -t commands` produced nothing — is ninja on "
                         "PATH and the build configured?"}
    probes = pick_probes(tasks, cmd_by_output, per_kind)
    if len(probes) < 2:
        return {"error": "no suitable probe tasks (need steps between 0.3 s "
                         "and 10 s with a rewritable -o)"}

    probe_cmds = {p["cmd"] for p in probes}
    background = [c for c in cmd_by_output.values() if c not in probe_cmds]
    if not background:
        background = [p["cmd"] for p in probes]
    load = LoadGenerator(builddir, background)

    # order matters more than sample size here: sweeping the load levels in
    # ascending order would confound thermal drift with load perfectly, since
    # the machine is hottest at the end. Randomize, and interleave an isolated
    # pass between levels so the drift itself is measured.
    rng = random.Random(20260725)
    plan: list[float] = [0.0]  # discarded warm-up pass
    for _ in range(repeats):
        levels = list(CALIB_LEVELS)
        rng.shuffle(levels)
        for u in levels:
            plan += [0.0, u]
    plan.append(0.0)

    # A separate isolated pass for peak memory, before the timed sweep: the
    # timing probes are restricted to steps long enough for their duration to
    # mean something, but footprint doesn't care how long a step ran, and the
    # steps left out (short links especially) are often the biggest ones.
    mem_probes = pick_memory_probes(tasks, cmd_by_output, per_kind)
    samples: list[dict] = []
    mem_raw: dict[str, list[int]] = {}
    total = len(plan) * len(probes) + len(mem_probes)
    done = 0
    for p in mem_probes:
        ms, peak = time_command(builddir, p["cmd"], measure_mem=True)
        done += 1
        if progress:
            progress(done, total, 1)
        # a failed re-run still reports whatever it allocated before dying, which
        # would understate the real footprint — only count commands that finished
        if ms is not None and peak:
            mem_raw.setdefault(p["kind"], []).append(peak)

    for pass_idx, u in enumerate(plan):
        conc = 1 if u <= 0 else max(1, round(u * cores))
        load.start(conc - 1)
        try:
            for i, p in enumerate(probes):
                isolated = conc <= 1
                ms, peak = time_command(builddir, p["cmd"], measure_mem=isolated)
                done += 1
                if progress:
                    progress(done, total, conc)
                if ms is None:
                    continue
                if peak:
                    mem_raw.setdefault(p["kind"], []).append(peak)
                if pass_idx == 0:
                    continue  # warm-up: page cache, ASLR, first-touch
                samples.append({"probe": i, "kind": p["kind"], "c": conc,
                                "ms": ms, "pass": pass_idx})
        finally:
            load.stop()

    fit = fit_inflation(samples, cores)
    if fit is None:
        return {"error": "not enough usable timings to fit a curve "
                         f"({len(samples)} samples)"}

    # drift control: isolated passes run throughout the session, so the trend
    # across them separates sustained-load frequency drop from contention
    iso = {}
    for s in samples:
        if s["c"] <= 1:
            iso.setdefault(s["pass"], []).append((s["probe"], s["ms"]))
    drift = None
    if len(iso) >= 2:
        keys = sorted(iso)
        first, last = dict(iso[keys[0]]), dict(iso[keys[-1]])
        both = [p for p in first if p in last and first[p] > 0]
        if both:
            drift = round(sum(last[p] / first[p] for p in both) / len(both), 4)

    fit.update({
        "source": "calibrate",
        "cores": cores,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "probes": [{"name": p["name"], "kind": p["kind"], "dur": p["dur"]}
                   for p in probes],
        # the raw timings, so the fit can be re-examined or re-fitted without
        # sitting through another sweep. A few dozen numbers.
        "timings": [{"probe": s["probe"], "c": s["c"], "ms": round(s["ms"], 1),
                     "pass": s["pass"]} for s in samples],
        # peak RSS per rule kind. `unsampled` is not noise — it says which kinds
        # the report has no footprint for, so it can label its own coverage
        # instead of quietly treating them as free.
        "mem": {k: {"med": int(statistics.median(v)), "max": max(v), "n": len(v)}
                for k, v in sorted(mem_raw.items())} or None,
        "memUnsampled": sorted({classify_rule(t.edge.rule) for t in tasks
                                if t.dur > 0} - set(mem_raw)) or None,
        "drift": drift,
        "machine": {"cpuCount": os.cpu_count(), "platform": sys.platform,
                    "ramBytes": total_ram()},
    })
    return fit


def calibrate_job(bridge: "Bridge", job: dict) -> None:
    """Run a calibration sweep from the interactive bridge and save the result."""
    try:
        def progress(done, total, conc):
            job["done"], job["total"] = done, total
            job["note"] = "alone" if conc <= 1 else f"{conc} jobs at once"

        result = calibrate(bridge.builddir, bridge.manifest, bridge.tasks,
                           bridge.cores, progress=progress)
        if "error" in result:
            job.update(state="error", error=result["error"])
            return
        path = bridge.builddir / CONTENTION_FILE
        path.write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(f"calibration saved to {path}")
        job["result"] = {"beta": result["beta"], "gamma": result["gamma"],
                         "r2": result["r2"], "cores": result["cores"],
                         "drift": result.get("drift"),
                         "levels": result["levels"], "path": str(path)}
        job["state"] = "done"
    except Exception as exc:
        job.update(state="error", error=f"{type(exc).__name__}: {exc}")


def load_contention(builddir: Path, override: str | None) -> dict | None:
    """The machine's contention profile: an explicit --contention, else the
    file `--calibrate` left in the build dir."""
    if override:
        parts = override.split(",")
        try:
            beta, gamma, cores = (float(parts[0]), float(parts[1]),
                                  int(parts[2]))
        except (IndexError, ValueError):
            sys.exit("error: --contention wants beta,gamma,cores "
                     "(e.g. 0.31,0.72,16)")
        return {"beta": beta, "gamma": gamma, "cores": cores,
                "source": "flag", "r2": None, "levels": [], "perKind": {}}
    path = builddir / CONTENTION_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"warning: ignoring {path} ({exc})", file=sys.stderr)
        return None
    if not isinstance(data, dict) or "beta" not in data:
        print(f"warning: ignoring {path} (not a contention profile)",
              file=sys.stderr)
        return None
    return data


# --------------------------------------------------------------------------
# interactive mode: HTTP bridge
# --------------------------------------------------------------------------

class Bridge:
    def __init__(self, builddir: Path, html: str, tasks: list[Task],
                 manifest: NinjaManifest, cores: int = 0):
        self.builddir = builddir
        self.html = html
        self.tasks = tasks
        self.manifest = manifest
        self.cores = cores or os.cpu_count() or 8
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
            elif path == "/api/calibrate":
                jid, job = bridge.new_job()
                print(f"calibrating contention against {bridge.cores} job "
                      "slots (a few minutes)...")
                threading.Thread(target=calibrate_job, args=(bridge, job),
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
    ap.add_argument("--calibrate", action="store_true",
                    help="measure this machine's contention curve: re-run a few "
                         "of the build's own steps alone and again under "
                         "controlled load (~3 min), save the result to "
                         f"<builddir>/{CONTENTION_FILE}, and use it in the "
                         "report. Later runs pick the file up automatically")
    ap.add_argument("--cores", type=int, default=None, metavar="N",
                    help="job slots the build ran with (default: the observed "
                         "concurrency plateau, else this machine's core count). "
                         ".ninja_log records neither -j nor the build machine")
    ap.add_argument("--contention", default=None, metavar="B,G,C",
                    help="use this contention curve instead of a calibrated "
                         "one: beta,gamma,cores for f(u) = 1 + beta*u**gamma")
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

    cores = args.cores or data["meta"]["jobSlots"] or os.cpu_count() or 8
    if args.calibrate:
        if args.contention:
            sys.exit("error: --calibrate measures the curve, --contention "
                     "supplies one; pick one")
        jobs_at = ", ".join(str(max(1, round(u * cores))) for u in CALIB_LEVELS)
        print(f"calibrating    : timing probe steps at 1, {jobs_at} concurrent "
              "jobs (a few minutes)...")

        def show(done, total, conc):
            print(f"\r  {done}/{total} runs  "
                  f"({'alone' if conc <= 1 else f'{conc} jobs at once'})     ",
                  end="", flush=True)

        result = calibrate(builddir, manifest, tasks, cores, progress=show)
        print()
        if "error" in result:
            sys.exit(f"error: calibration failed: {result['error']}")
        (builddir / CONTENTION_FILE).write_text(
            json.dumps(result, indent=1), encoding="utf-8")
        print(f"  saved to {builddir / CONTENTION_FILE}")

    contention = load_contention(builddir, args.contention)
    if contention:
        data["meta"]["contention"] = contention
        at_full = 1 + contention["beta"]
        detail = [f"×{at_full:.2f} at {contention['cores']} jobs",
                  f"beta {contention['beta']:.3f}",
                  f"gamma {contention['gamma']:.2f}"]
        if contention.get("r2") is not None:
            detail.append(f"R² {contention['r2']:.2f}")
        if contention.get("drift"):
            detail.append(f"drift ×{contention['drift']:.2f}")
        print(f"contention     : {' · '.join(detail)} "
              f"({contention.get('source', 'file')})")

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
        serve(Bridge(builddir, html, tasks, manifest, cores),
              args.port, not args.no_open)
    else:
        args.output.write_text(html, encoding="utf-8")
        print(f"report written : {args.output}")


if __name__ == "__main__":
    main()
