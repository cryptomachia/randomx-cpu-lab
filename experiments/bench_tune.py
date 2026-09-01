"""Autotune: successive halving over threads x QoS placement x batch API.

Runs rxlab.tune: about 40 fast-mode v1 candidates are narrowed over three
rounds with growing budgets, then the winner meets the baseline (all logical
CPUs, QoS unset, batch API) in fresh interleaved pairs; dataset-init threads
are tuned the same way. --smoke runs a 6-candidate light-mode version.

    python3 experiments/bench_tune.py [--smoke] [--estimate]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rxlab import bench, plotstyle, tune  # noqa: E402

NAME = "bench_tune"
EST_INIT_S = 60.0  # init tuning, idle M4 guess, for --estimate only


def estimate(smoke):
    if smoke:
        alive, schedule, f, total = 6, tune.SMOKE_HASH_SCHEDULE, tune.SMOKE_HASH_FINAL, bench.SESSION_OVERHEAD_S
    else:
        n, p, e = tune.shape()
        alive = len(tune.candidates(1, tune.default_threads(n, p, e), p))
        schedule, f, total = tune.HASH_SCHEDULE, tune.HASH_FINAL, bench.SESSION_OVERHEAD_S + EST_INIT_S
    for r, step in enumerate(schedule):
        total += alive * step["trials"] * (step["warmup"] + step["measure"] + bench.TRIAL_OVERHEAD_S)
        alive = 1 if r == len(schedule) - 1 else -(-alive // tune.ETA)
    return total + 2 * f["pairs"] * (f["warmup"] + f["measure"] + bench.TRIAL_OVERHEAD_S)


def short(config):
    d = bench.parse_spec(config)
    return f"{d['threads']} threads\nqos {d['qos']}\n{'batch' if d['batch'] else 'single'} API"


def plot(res):
    plt = plotstyle.apply()
    fig, (ax, ax2, ax3) = plt.subplots(1, 3, figsize=(12, 4.2), gridspec_kw={"width_ratios": [1.6, 1, 1]})
    blue, grey = plotstyle.PALETTE[0], "#888888"
    h = res["hashing"]
    for rnd in h["history"]:
        kept = set(rnd["kept"])
        for c in rnd["candidates"]:
            won = c["config"] in kept
            ax.plot(rnd["round"], c["hs"]["median"], "o", ms=6, alpha=0.8, markeredgecolor="white",
                    color=blue if won else grey, zorder=3 if won else 2)
    ax.plot([], [], "o", color=blue, label="kept")
    ax.plot([], [], "o", color=grey, label="eliminated")
    ax.set_xticks([r["round"] for r in h["history"]])
    ax.set_xticklabels([f"round {r['round']}\n{len(r['candidates'])} configs, {r['trials']}x{r['measure']:g} s"
                        for r in h["history"]], fontsize=8)
    ax.set_ylabel("median H/s")
    ax.set_title("Successive halving")
    ax.legend(frameon=False, fontsize=8)

    def duel(a, rows, unit, title, ratio, note):
        for x, (name, s) in enumerate(rows):
            a.errorbar(x, s["median"], yerr=[[s["median"] - s["ci95"][0]], [s["ci95"][1] - s["median"]]],
                       fmt="o", ms=8, capsize=4, color=blue if x else grey)
        a.set_xticks(range(len(rows)))
        a.set_xticklabels([name for name, _ in rows], fontsize=8)
        a.set_xlim(-0.6, len(rows) - 0.4)
        a.set_ylabel(unit)
        a.set_title(f"{title}\n{note} x{ratio['ratio']:.3f} [{ratio['ci95'][0]:.3f}, {ratio['ci95'][1]:.3f}]",
                    fontsize=10)

    duel(ax2, [("baseline\n" + short(h["baseline"]), h["baseline_hs"]), ("winner\n" + short(h["winner"]), h["winner_hs"])],
         "H/s (median, 95% CI)", "Winner vs baseline (fresh pairs)", h["paired"], "paired ratio")
    i = res["dataset_init"]
    part = "full dataset" if i["fraction"] == 1 else f"1/{round(1 / i['fraction'])} of dataset"
    duel(ax3, [(f"{i['baseline_threads']} threads\n(baseline)", i["baseline_s"]),
               (f"{i['winner_threads']} threads\n(winner)", i["winner_s"])],
         "seconds (median, 95% CI)", f"Dataset init, {part}", i["time_ratio_paired"], "time ratio")
    fig.tight_layout()
    return fig


def main():
    args = bench.experiment_args(__doc__).parse_args()
    if args.estimate:
        print(round(estimate(args.smoke)))
        return
    res = tune.tune(args.tag, v=1, smoke=args.smoke)
    h = res["hashing"]
    print(f"winner {h['winner']}: {h['winner_hs']['median']:.1f} H/s vs baseline {h['baseline_hs']['median']:.1f} "
          f"(x{h['paired']['ratio']:.3f} [{h['paired']['ci95'][0]:.3f}, {h['paired']['ci95'][1]:.3f}])")
    bench.finish(NAME, res, plot(res), args.smoke)


if __name__ == "__main__":
    main()
