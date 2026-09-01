"""Dataset initialisation time with 1..N threads (all logical CPUs).

Each init fills the full 2080 MiB dataset from the 256 MiB cache (JIT
SuperscalarHash) with the item range split evenly across threads. The range
is zeroed first, so timings exclude first-touch page faults, and one untimed
warm-up init (1/64 of the dataset) runs first. Thread counts run interleaved
within each trial. Correctness: every init must give the same
dataset digest, and a fast-mode hash on it must match light mode. The
amortised cost uses the RandomX README's key schedule (new key every 2048
blocks) and Monero's 120 s block target. --smoke inits 1/256 of the dataset
with 1..4 threads.

    python3 experiments/bench_dataset_init.py [--smoke] [--estimate]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rxlab import bench, plotstyle, stats, sysinfo, tune  # noqa: E402

NAME = "bench_dataset_init"
FULL = {"trials": 2, "fraction": 1.0}
SMOKE = {"trials": 1, "fraction": 1 / 256, "threads": [1, 2, 3, 4]}
KEY_EPOCH_S = 2048 * 120
EST_ONE_THREAD_S = 30.0  # idle M4 guess, for --estimate only


def analyse(inits):
    by = {}
    for r in inits:
        by.setdefault(r["threads"], []).append(r["seconds"])
    rows = [{"threads": t, "seconds": stats.summarize(s)} for t, s in sorted(by.items())]
    one = next((r["seconds"]["median"] for r in rows if r["threads"] == 1), None)
    for r in rows:
        r["items_per_s"] = inits[0]["items"] / r["seconds"]["median"]
        if one:
            r["speedup"] = one / r["seconds"]["median"]
            r["parallel_efficiency"] = r["speedup"] / r["threads"]
    best = min(rows, key=lambda r: r["seconds"]["median"])
    out = {"rows": rows, "best_threads": best["threads"], "best_seconds_median": best["seconds"]["median"]}
    if inits[0]["items"] == inits[0]["total_items"]:
        out["amortised"] = {"key_epoch_s": KEY_EPOCH_S,
                            "best_init_share_of_epoch": best["seconds"]["median"] / KEY_EPOCH_S}
    return out


def plot(analysis, full):
    plt = plotstyle.apply()
    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(10, 4.2))
    rows = analysis["rows"]
    t = [r["threads"] for r in rows]
    med = [r["seconds"]["median"] for r in rows]
    err = [[m - r["seconds"]["ci95"][0] for m, r in zip(med, rows)],
           [r["seconds"]["ci95"][1] - m for m, r in zip(med, rows)]]
    ax.errorbar(t, med, yerr=err, marker="o", ms=5, lw=2, capsize=3, color=plotstyle.PALETTE[0], label="measured")
    if rows[0]["threads"] == 1:
        ax.plot(t, [med[0] / x for x in t], color="#888888", lw=1, label="ideal (1-thread time / threads)")
        ax2.plot(t, [r["speedup"] for r in rows], marker="o", ms=5, lw=2, color=plotstyle.PALETTE[0],
                 label="measured")
        ax2.plot(t, t, color="#888888", lw=1, label="ideal")
        ax2.legend(frameon=False)
    what = "full 2080 MiB dataset" if full else "dataset slice (smoke)"
    ax.set_title(f"Init time, {what}")
    ax.set_ylabel("seconds (median, 95% CI)")
    ax2.set_title("Speedup over 1 thread")
    ax2.set_ylabel("speedup")
    for a in (ax, ax2):
        a.set_xlabel("init threads (QoS unset)")
        a.set_xticks(t)
        a.set_ylim(bottom=0)
    ax.legend(frameon=False)
    return fig


def main():
    args = bench.experiment_args(__doc__).parse_args()
    cfg = SMOKE if args.smoke else FULL
    n = tune.shape()[0]
    threads = cfg.get("threads") or list(range(1, n + 1))
    if args.estimate:
        one = EST_ONE_THREAD_S * cfg["fraction"]
        per_trial = sum(one / min(t, 0.5 * n) + 1.5 * cfg["fraction"] for t in threads)
        print(round(bench.SESSION_OVERHEAD_S + cfg["trials"] * per_trial))
        return
    with bench.Session(args.tag, init_threads=bench.SMOKE_INIT_THREADS if args.smoke else None) as s:
        verifier = bench.Verifier(args.tag, info=s.info())
        warm = bench.dataset_init(s, [max(threads)], 1, 1 / 64, label=f"{NAME} warm-up")
        inits = bench.dataset_init(s, threads, cfg["trials"], cfg["fraction"], label=NAME)
        setup = s.setup
    tune.check_digests(inits)
    verification = dict(verifier.verify(inits), digests_identical=True)
    verifier.close()
    analysis = analyse(inits)
    for r in analysis["rows"]:
        print(f"{r['threads']:>2} threads: {r['seconds']['median']:7.3f} s")
    payload = {"tag": args.tag, "smoke": args.smoke, "fraction": cfg["fraction"], "trials": cfg["trials"],
               "setup": setup, "verification": verification, "analysis": analysis, "inits": inits,
               "warmup_inits": warm,
               "power_source": sysinfo.describe().get("power_source")}
    bench.finish(NAME, payload, plot(analysis, cfg["fraction"] == 1.0), args.smoke)


if __name__ == "__main__":
    main()
