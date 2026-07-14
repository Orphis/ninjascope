# /// script
# requires-python = ">=3.10"
# ///
"""Generate a dummy CMake C++ project with an interesting build DAG.

The project is shaped to make build-parallelism visualization interesting:
  - a slow global codegen step at the head (serialized start),
  - a layered DAG of static libraries where each library has its own codegen
    step depending on its dependencies' codegen (like IDL/protobuf chains),
  - a "core" library with a few slow translation units on the critical path,
  - a final executable linking everything (serialized tail).

Usage:
  python generate_sample.py [--libs 25] [--files-per-lib 8] [--depth 6]
                            [--seed 42] [--out sample]
"""

from __future__ import annotations

import argparse
import random
import shutil
from pathlib import Path

WORK_HEADER = """\
#pragma once
#include <algorithm>
#include <map>
#include <numeric>
#include <string>
#include <vector>

// Recursive template whose every instantiation compiles real code, so the
// instantiation depth N controls the compile time of a translation unit.
template <int N>
struct Work {
  static long long run(long long seed) {
    std::vector<long long> v(N % 7 + 2);
    std::iota(v.begin(), v.end(), seed + N);
    long long acc = std::accumulate(v.begin(), v.end(), 0LL);
    return acc + Work<N - 1>::run(seed ^ (N * 2654435761LL));
  }
};

template <>
struct Work<0> {
  static long long run(long long seed) { return seed; }
};
"""


def lib_name(i: int) -> str:
    return "core" if i == 0 else f"mod{i:02d}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--libs", type=int, default=25, help="number of libraries (including core)")
    ap.add_argument("--files-per-lib", type=int, default=8)
    ap.add_argument("--depth", type=int, default=6, help="number of dependency layers")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path("sample"))
    args = ap.parse_args()

    rng = random.Random(args.seed)
    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)
    (out / "include").mkdir(parents=True)
    (out / "templates").mkdir()

    (out / "include" / "work.h").write_text(WORK_HEADER)
    (out / "templates" / "gen_config.h.in").write_text(
        "#pragma once\n#define GEN_CONFIG_MAGIC 0x5eed\n"
    )

    # ---- assign libraries to layers; lib 0 ("core") sits alone in layer 0 ----
    layers: list[list[int]] = [[] for _ in range(args.depth)]
    layers[0].append(0)
    for i in range(1, args.libs):
        layers[1 + (i - 1) % (args.depth - 1)].append(i)

    layer_of = {i: l for l, libs in enumerate(layers) for i in libs}
    deps: dict[int, list[int]] = {}
    for i in range(args.libs):
        l = layer_of[i]
        if l == 0:
            deps[i] = []
            continue
        # everything depends on core, plus 1-3 libs from earlier layers (prefer l-1)
        pool = [j for j in range(args.libs) if 0 < layer_of[j] < l]
        prev = [j for j in pool if layer_of[j] == l - 1]
        picks = set()
        if prev:
            picks.update(rng.sample(prev, min(len(prev), rng.randint(1, 2))))
        if pool and rng.random() < 0.5:
            picks.add(rng.choice(pool))
        picks.discard(i)
        deps[i] = [0] + sorted(picks)

    # ---- single top-level CMakeLists ----
    # All custom commands live in one directory: CMake then wires precise
    # file-level dependencies between codegen steps (like protobuf imports).
    # Declared in subdirectories, CMake would coarsen cross-directory file
    # deps into whole-target deps (codegen waiting on dependency archives),
    # which serializes far more than intended.
    top = [
        "cmake_minimum_required(VERSION 3.20)",
        "project(NinjaVizSample CXX)",
        "set(CMAKE_CXX_STANDARD 17)",
        "set(CMAKE_CXX_STANDARD_REQUIRED ON)",
        "",
        "# Slow global codegen step: serialized head of the build.",
        "add_custom_command(",
        "  OUTPUT ${CMAKE_BINARY_DIR}/generated/gen_config.h",
        "  COMMAND ${CMAKE_COMMAND} -E sleep 2.5",
        "  COMMAND ${CMAKE_COMMAND} -E copy ${CMAKE_SOURCE_DIR}/templates/gen_config.h.in ${CMAKE_BINARY_DIR}/generated/gen_config.h",
        "  DEPENDS ${CMAKE_SOURCE_DIR}/templates/gen_config.h.in",
        '  COMMENT "codegen: global config header (slow)")',
        "include_directories(${CMAKE_SOURCE_DIR}/include ${CMAKE_BINARY_DIR}/generated)",
        "",
    ]

    # ---- per-library sources + build rules ----
    for i in range(args.libs):
        name = lib_name(i)
        d = out / "libs" / name
        d.mkdir(parents=True)
        n_files = args.files_per_lib
        fn_names = [f"{name}_fn{k}" for k in range(n_files)]

        # public header
        decls = "\n".join(f"long long {fn}(long long seed);" for fn in fn_names)
        (d / f"{name}.h").write_text(f"#pragma once\n{decls}\n")

        # per-lib generated header template (copied into the build tree by codegen)
        (d / f"{name}_gen.h.in").write_text(
            f"#pragma once\n#define GEN_{name.upper()}_SALT {rng.randint(1, 1 << 20)}\n"
        )

        # sources; core gets slow TUs (deep instantiation), others modest
        for k, fn in enumerate(fn_names):
            depth = rng.randint(300, 450) if i == 0 else rng.randint(30, 120)
            src = (
                f'#include "{name}.h"\n'
                f'#include "{name}_gen.h"\n'
                '#include "gen_config.h"\n'
                '#include "work.h"\n\n'
                f"long long {fn}(long long seed) {{\n"
                f"  return Work<{depth}>::run(seed * {2 * k + 3} + GEN_CONFIG_MAGIC + GEN_{name.upper()}_SALT);\n"
                "}\n"
            )
            (d / f"{fn}.cpp").write_text(src)

        # codegen for this lib depends on its dependencies' codegen outputs,
        # forming the layered DAG of generation steps.
        gen_out = f"${{CMAKE_BINARY_DIR}}/generated/{name}/{name}_gen.h"
        dep_gens = [
            f"${{CMAKE_BINARY_DIR}}/generated/{lib_name(j)}/{lib_name(j)}_gen.h" for j in deps[i]
        ]
        if i == 0:
            dep_gens = ["${CMAKE_BINARY_DIR}/generated/gen_config.h"]
        sleep = round(rng.uniform(0.1, 0.35), 2)
        top += [
            "add_custom_command(",
            f"  OUTPUT {gen_out}",
            f"  COMMAND ${{CMAKE_COMMAND}} -E sleep {sleep}",
            f"  COMMAND ${{CMAKE_COMMAND}} -E copy ${{CMAKE_SOURCE_DIR}}/libs/{name}/{name}_gen.h.in {gen_out}",
            f"  DEPENDS ${{CMAKE_SOURCE_DIR}}/libs/{name}/{name}_gen.h.in " + " ".join(dep_gens),
            f'  COMMENT "codegen: {name} interface")',
            # Anchor the generated header with its own tiny target. Listing it
            # as a library source would make CMake turn other codegen steps'
            # DEPENDS on it into whole-target (archive) dependencies.
            f"add_custom_target(gen_{name} DEPENDS {gen_out})",
            f"add_library({name} STATIC",
            *(f"  libs/{name}/{fn}.cpp" for fn in fn_names),
            ")",
            f"add_dependencies({name} gen_{name})",
            f"target_include_directories({name} PUBLIC ${{CMAKE_SOURCE_DIR}}/libs/{name} ${{CMAKE_BINARY_DIR}}/generated/{name})",
        ]
        if deps[i]:
            top.append(
                f"target_link_libraries({name} PUBLIC " + " ".join(lib_name(j) for j in deps[i]) + ")"
            )
        top.append("")

    top += [
        "add_executable(app main.cpp)",
        "target_link_libraries(app " + " ".join(lib_name(i) for i in range(args.libs)) + ")",
        "",
    ]
    (out / "CMakeLists.txt").write_text("\n".join(top))

    # ---- main.cpp calls one function from every library ----
    includes = "\n".join(f'#include "{lib_name(i)}.h"' for i in range(args.libs))
    calls = "\n".join(f"  total += {lib_name(i)}_fn0(total);" for i in range(args.libs))
    (out / "main.cpp").write_text(
        f"{includes}\n#include <cstdio>\n\nint main() {{\n  long long total = 1;\n{calls}\n"
        '  std::printf("total=%lld\\n", total);\n  return 0;\n}\n'
    )

    n_tasks = args.libs * (args.files_per_lib + 2) + 2
    print(f"Generated {out}/ with {args.libs} libs x {args.files_per_lib} files "
          f"in {args.depth} layers (~{n_tasks} build tasks)")


if __name__ == "__main__":
    main()
