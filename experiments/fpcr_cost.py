"""How much of v1's hash time is spent changing the FP rounding mode?

Builds and runs tools/fpcr_bench.c (marginal cost of an FPCR/MXCSR write that
changes the rounding mode, and of one that rewrites the same mode), then
combines it with the rounding-mode change rates from results/program_structure.json
and the single-thread fast-mode speeds from results/bench_v1_vs_v2.json.

    python3 experiments/fpcr_cost.py            # writes results/fpcr_cost.json
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rxlab import sysinfo  # noqa: E402
from rxlab.spec import params  # noqa: E402

ITERATIONS_PER_HASH = params.PROGRAM_COUNT * params.PROGRAM_ITERATIONS


def build():
    exe = ROOT / "build" / "fpcr_bench"
    src = ROOT / "tools" / "fpcr_bench.c"
    if not exe.exists() or src.stat().st_mtime > exe.stat().st_mtime:
        exe.parent.mkdir(exist_ok=True)
        subprocess.run(["cc", "-O2", "-o", str(exe), str(src)], check=True)
    return exe


def single_thread_hs():
    runs = json.loads((ROOT / "results" / "bench_v1_vs_v2.json").read_text())["results"]
    for r in runs:
        if r["threads"] == 1 and r["a"]["tag"] == r["b"]["tag"]:
            return {"v1": r["a_hs"]["median"], "v2": r["b_hs"]["median"],
                    "v2_over_v1_paired": r["b_over_a_paired"]}
    raise LookupError("no single-thread v1/v2 pair in bench_v1_vs_v2.json")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--iterations", type=int, default=20_000_000)
    ap.add_argument("--repeats", type=int, default=9)
    args = ap.parse_args()

    out = subprocess.run([str(build()), str(args.iterations), str(args.repeats)],
                         check=True, capture_output=True, text=True).stdout
    bench = json.loads(out)
    structure = json.loads((ROOT / "results" / "program_structure.json").read_text())
    hs = single_thread_hs()

    per_version = {}
    for v in ("v1", "v2"):
        cf = structure[v]["cfround"]
        changes = cf["fprc_changes_per_iteration"] * ITERATIONS_PER_HASH
        same = (cf["fprc_writes_per_iteration"] - cf["fprc_changes_per_iteration"]) * ITERATIONS_PER_HASH
        cost_ms = (changes * bench["toggle_write_ns"] + same * bench["same_write_ns"]) * 1e-6
        hash_ms = 1e3 / hs[v]
        per_version[v] = {
            "mode_changes_per_hash": changes,
            "same_mode_writes_per_hash": same,
            "write_cost_ms_per_hash": cost_ms,
            "single_thread_hash_ms": hash_ms,
            "share_of_hash_time": cost_ms / hash_ms,
        }
    saved = per_version["v1"]["write_cost_ms_per_hash"] - per_version["v2"]["write_cost_ms_per_hash"]
    result = {
        "machine": sysinfo.describe(),
        "microbenchmark": bench,
        "single_thread_fast_mode": hs,
        "per_version": per_version,
        "v1_time_saved_by_v2_cfround_ms": saved,
        "v1_speedup_from_fewer_writes_alone": 1 / (1 - saved / per_version["v1"]["single_thread_hash_ms"]),
        "note": ("marginal write costs measured inside a dependent FP loop on one thread; in the JIT a "
                 "write may overlap more or less with surrounding work, so the share is an estimate"),
    }
    path = ROOT / "results" / "fpcr_cost.json"
    path.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "machine"}, indent=1))


if __name__ == "__main__":
    main()
