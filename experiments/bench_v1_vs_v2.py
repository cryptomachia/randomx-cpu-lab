"""RandomX v1 vs v2, and v1 through two library versions, in paired trials.

v2 vs v1: both versions run from the v2.0.1 library (RANDOMX_FLAG_V2 selects
v2) in one session sharing one dataset, at the best thread count from
bench_thread_scaling and at 1 thread. Library: v1 hashes through the v1.2.1
library (what Monero runs) vs through v2.0.1, from two rxbench processes kept
alive side by side (one dataset each). Each pair runs both arms back to back,
alternating which goes first (ABBA), so thermal drift and order effects cancel
in the per-pair ratio. --smoke runs 2 light-mode pairs of each.

    python3 experiments/bench_v1_vs_v2.py [--smoke] [--estimate]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rxlab import bench, plotstyle, stats  # noqa: E402
from rxlab.spec import params  # noqa: E402

NAME = "bench_v1_vs_v2"
OLD_TAG = "v1.2.1"
FULL = {"pairs": {"best": 10, "one": 6, "lib": 6}, "warmup": 1.5, "measure": 5.0}
SMOKE = {"pairs": {"best": 2, "one": 2, "lib": 2}, "warmup": 0.3, "measure": 0.8}


def describe(arm):
    name, session, config = arm
    return {"name": name, "tag": session.tag, "config": config}


def run_pairs(arm_a, arm_b, pairs, cfg, label):
    """ABBA pairs of two (name, session, config) arms; ratio is B / A."""
    records, ratios = [], []
    for i in range(pairs):
        got = {}
        for name, session, config in ([arm_a, arm_b] if i % 2 == 0 else [arm_b, arm_a]):
            trial = bench.sweep(session, [config], cfg["warmup"], cfg["measure"], 1,
                                label=f"{label} pair {i + 1}/{pairs}")[0]
            got[name] = dict(trial, pair=i, arm=name, tag=session.tag)
            records.append(got[name])
        ratios.append(got[arm_b[0]]["hs"] / got[arm_a[0]]["hs"])
    a = [t["hs"] for t in records if t["arm"] == arm_a[0]]
    b = [t["hs"] for t in records if t["arm"] == arm_b[0]]
    return records, {"a": describe(arm_a), "b": describe(arm_b), "threads": bench.parse_spec(arm_a[2])["threads"],
                     "a_hs": stats.summarize(a), "b_hs": stats.summarize(b),
                     "b_over_a_paired": stats.compare_paired(a, b), "b_over_a_independent": stats.compare(a, b),
                     "pair_ratios": ratios}


def plot(results):
    plt = plotstyle.apply()
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), gridspec_kw={"width_ratios": [1.4, 1]})
    panels = [("v2 vs v1 (v2.0.1 library)", "v2 H/s / v1 H/s", [r for r in results if r["b"]["name"] == "v2"]),
              (f"v1: v2.0.1 vs {OLD_TAG} library", f"v2.0.1 H/s / {OLD_TAG} H/s",
               [r for r in results if r["b"]["name"] != "v2"])]
    for ax, (title, ylabel, rows) in zip(axes, panels):
        for i, r in enumerate(rows):
            color = plotstyle.PALETTE[i]
            c = r["b_over_a_paired"]
            ax.axhspan(c["ci95"][0], c["ci95"][1], color=color, alpha=0.12, lw=0)
            ax.plot(range(1, len(r["pair_ratios"]) + 1), r["pair_ratios"], "o-", ms=6, lw=2, color=color,
                    label=f"{r['threads']} thread{'s' if r['threads'] > 1 else ''}: median x{c['ratio']:.3f}")
        ax.axhline(1, color="#888888", lw=1)
        ax.set_xticks(range(1, max(len(r["pair_ratios"]) for r in rows) + 1))
        ax.set_xlabel("pair (ABBA order)")
        ax.set_ylabel(ylabel)
        ax.set_title(f"{title}\nband: 95% CI of the median ratio", fontsize=10)
        ax.legend(frameon=False, fontsize=8)
    fig.tight_layout()
    return fig


def main():
    args = bench.experiment_args(__doc__).parse_args()
    cfg = SMOKE if args.smoke else FULL
    mode = bench.smoke_mode(args.smoke)
    best = 2 if args.smoke else bench.best_threads(v=1)
    if args.estimate:
        n = sum(cfg["pairs"].values())
        print(round(bench.estimate_sweep(2, n, cfg["warmup"], cfg["measure"]) + bench.SESSION_OVERHEAD_S))
        return
    init = bench.SMOKE_INIT_THREADS if args.smoke else None
    records, results = [], []
    with bench.Session(args.tag, init_threads=init) as s:
        verifier = bench.Verifier(args.tag, info=s.info())
        for threads, pairs in ((best, cfg["pairs"]["best"]), (1, cfg["pairs"]["one"])):
            v1, v2 = (bench.spec(v=v, mode=mode, threads=threads) for v in (1, 2))
            recs, res = run_pairs(("v1", s, v1), ("v2", s, v2), pairs, cfg, NAME)
            records += recs
            results.append(res)
        with bench.Session(OLD_TAG, init_threads=init) as old:
            v1 = bench.spec(v=1, mode=mode, threads=best)
            recs, res = run_pairs((OLD_TAG, old, v1), (args.tag, s, v1), cfg["pairs"]["lib"], cfg,
                                  f"{NAME} lib")
            records += recs
            results.append(res)
            setup = {args.tag: s.setup, OLD_TAG: old.setup}
    # one verifier for both libraries: v1.2.1 output is checked against v2.0.1's v1 hashes
    verification = verifier.verify(records)
    verifier.close()
    for r in results:
        c = r["b_over_a_paired"]
        print(f"{r['b']['name']} / {r['a']['name']} at {r['threads']} threads: x{c['ratio']:.4f} "
              f"[{c['ci95'][0]:.4f}, {c['ci95'][1]:.4f}] p={c['p_value']}")
    payload = {"tag": args.tag, "smoke": args.smoke, "mode": mode, "setup": setup, "verification": verification,
               "settings": {"warmup_s": cfg["warmup"], "measure_s": cfg["measure"], "order": "ABBA pairs"},
               "program_size": {"v1": params.PROGRAM_SIZE_V1, "v2": params.PROGRAM_SIZE_V2},
               "results": results, "trials": records}
    bench.finish(NAME, payload, plot(results), args.smoke)


if __name__ == "__main__":
    main()
