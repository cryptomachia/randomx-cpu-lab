"""Successive-halving autotuner for RandomX on this machine.

Hashing: candidates are threads x QoS placement x batch API. Each round runs
every survivor (interleaved, in one rxbench session so the dataset is built
once), keeps the best 1/eta by median H/s and grows the per-trial budget. The
winner then meets the baseline (all logical CPUs, QoS unset, batch API) in
fresh interleaved trials, so the reported ratio carries no selection bias.

Dataset-init threads are tuned the same way on init time over growing slices of
the dataset, then compared with all logical CPUs on full inits.
"""
import argparse
import json
import math
import os

from rxlab import bench, stats, sysinfo

ETA = 3
HASH_SCHEDULE = [
    {"trials": 1, "warmup": 1.0, "measure": 3.0},
    {"trials": 2, "warmup": 1.0, "measure": 4.0},
    {"trials": 3, "warmup": 1.5, "measure": 6.0},
]
HASH_FINAL = {"pairs": 6, "warmup": 1.5, "measure": 8.0}
INIT_SCHEDULE = [{"trials": 1, "fraction": 1 / 32}, {"trials": 2, "fraction": 1 / 8}]
INIT_FINAL = {"pairs": 3, "fraction": 1.0}

SMOKE_HASH_SCHEDULE = [{"trials": 1, "warmup": 0.2, "measure": 0.6}, {"trials": 1, "warmup": 0.2, "measure": 0.8}]
SMOKE_HASH_FINAL = {"pairs": 2, "warmup": 0.2, "measure": 0.8}
SMOKE_INIT_SCHEDULE = [{"trials": 1, "fraction": 1 / 512}]
SMOKE_INIT_FINAL = {"pairs": 2, "fraction": 1 / 128}


def shape():
    """(logical CPUs, P cores, E cores) of this machine."""
    info = sysinfo.describe()
    n = info.get("logical_cpus") or os.cpu_count()
    p = e = 0
    for c in info.get("clusters", []):
        name = (c.get("name") or "").lower()
        if name.startswith("perf"):
            p += c.get("cores") or 0
        elif name.startswith("eff"):
            e += c.get("cores") or 0
    return (n, p, e) if p else (n, n, 0)


def default_threads(n, p, e):
    """P-cores alone, P plus a few E, P plus half the E, and around all CPUs."""
    return sorted({p, p + max(1, e // 3), p + e // 2, n - 1, n, n + 2} - {0})


def placements(threads, p):
    """QoS mixes worth trying at a thread count."""
    out = {"unset": "unset", "ui": "ui"}
    if threads > p:
        out["p_first_bg"] = bench.qos_mix(("ui", p), "bg")
        out["p_first_ut"] = bench.qos_mix(("ui", p), "ut")
    return out


def candidates(v, threads_options, p, batches=(1, 0), mode="fast"):
    return [bench.spec(v=v, mode=mode, threads=t, qos=q, batch=b)
            for t in threads_options for q in placements(t, p).values() for b in batches]


def halving(session, cands, schedule, eta=ETA, label="tune"):
    """Returns (winner config, per-round history, all trial records)."""
    alive, history, records = list(cands), [], []
    for r, step in enumerate(schedule):
        trials = bench.sweep(session, alive, step["warmup"], step["measure"], step["trials"],
                             label=f"{label} r{r + 1}/{len(schedule)}")
        records += trials
        ranked = sorted(bench.summarize_trials(trials), key=lambda row: -row["hs"]["median"])
        keep = 1 if r == len(schedule) - 1 else max(1, math.ceil(len(alive) / eta))
        alive = [row["config"] for row in ranked[:keep]]
        history.append(dict(step, round=r + 1, kept=alive,
                            candidates=[{"config": row["config"], "hs": row["hs"]} for row in ranked]))
    return alive[0], history, records


def head_to_head(session, winner, baseline, pairs, warmup, measure, label="tune final"):
    """Fresh interleaved trials of winner vs baseline."""
    configs = [baseline] if winner == baseline else [baseline, winner]
    trials = bench.sweep(session, configs, warmup, measure, pairs, label=label)
    base = [t["hs"] for t in trials if t["config"] == baseline]
    win = [t["hs"] for t in trials if t["config"] == winner]
    return trials, {
        "winner": winner, "baseline": baseline, "winner_is_baseline": winner == baseline,
        "winner_hs": stats.summarize(win), "baseline_hs": stats.summarize(base),
        "paired": stats.compare_paired(base, win), "independent": stats.compare(base, win),
        "pairs": pairs, "warmup": warmup, "measure": measure,
    }


def _init_medians(inits):
    by = {}
    for r in inits:
        by.setdefault(r["threads"], []).append(r["seconds"])
    return {t: stats.summarize(s) for t, s in by.items()}


def check_digests(inits):
    """Every init of the same slice must produce identical dataset bytes."""
    by = {}
    for r in inits:
        by.setdefault(r["items"], set()).add(r["digest"])
    bad = {k: sorted(v) for k, v in by.items() if len(v) > 1}
    if bad:
        raise bench.VerificationError(f"dataset digests differ between thread counts: {bad}")


def tune_init(session, n, schedule, final, eta=2, label="tune init"):
    """Successive halving over dataset-init thread counts (lower time wins)."""
    alive, history, records = list(range(1, n + 1)), [], []
    for r, step in enumerate(schedule):
        inits = bench.dataset_init(session, alive, step["trials"], step["fraction"], label=f"{label} r{r + 1}")
        records += inits
        med = _init_medians(inits)
        ranked = sorted(alive, key=lambda t: med[t]["median"])
        alive = ranked[:max(1, math.ceil(len(alive) / eta))]
        history.append(dict(step, round=r + 1, kept=alive,
                            candidates=[{"threads": t, "seconds": med[t]} for t in ranked]))
    winner = alive[0]
    counts = [n] if winner == n else [n, winner]
    inits = bench.dataset_init(session, counts, final["pairs"], final["fraction"], label=f"{label} final")
    records += inits
    check_digests(records)
    base = [r["seconds"] for r in inits if r["threads"] == n]
    win = [r["seconds"] for r in inits if r["threads"] == winner]
    return records, {
        "history": history, "winner_threads": winner, "baseline_threads": n,
        "winner_s": stats.summarize(win), "baseline_s": stats.summarize(base),
        "time_ratio_paired": stats.compare_paired(base, win),
        "fraction": final["fraction"], "pairs": final["pairs"],
    }


def tune(tag=bench.DEFAULT_TAG, v=1, smoke=False, threads_options=None):
    """Run both tuners; returns a JSON-ready dict (hashes verified)."""
    n, p, e = shape()
    if smoke:
        (n, p, e), threads_options = (2, 1, 1), [1, 2]
        mode, batches = "light", (1,)
        hs, hf, ins, inf = SMOKE_HASH_SCHEDULE, SMOKE_HASH_FINAL, SMOKE_INIT_SCHEDULE, SMOKE_INIT_FINAL
    else:
        threads_options = threads_options or default_threads(n, p, e)
        mode, batches = "fast", (1, 0)
        hs, hf, ins, inf = HASH_SCHEDULE, HASH_FINAL, INIT_SCHEDULE, INIT_FINAL
    cands = candidates(v, threads_options, p, batches, mode)
    baseline = bench.spec(v=v, mode=mode, threads=n)
    with bench.Session(tag, init_threads=min(n, 4) if smoke else n) as s:
        verifier = bench.Verifier(tag, info=s.info())
        winner, history, records = halving(s, cands, hs)
        final_trials, final = head_to_head(s, winner, baseline, hf["pairs"], hf["warmup"], hf["measure"])
        init_records, init = tune_init(s, n, ins, inf)
        setup = s.setup
    verification = verifier.verify(records + final_trials + init_records)
    verifier.close()
    return {
        "tag": tag, "v": v, "mode": mode, "smoke": smoke, "eta": ETA,
        "shape": {"logical_cpus": n, "p_cores": p, "e_cores": e},
        "n_candidates": len(cands), "schedule": hs, "hashing": dict(final, history=history),
        "dataset_init": init, "setup": setup, "verification": verification,
        "trials": records + final_trials, "inits": init_records,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", default=bench.DEFAULT_TAG)
    ap.add_argument("--v", type=int, default=1, choices=[1, 2])
    ap.add_argument("--threads", help="comma-separated thread counts to search")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args(argv)
    opts = [int(x) for x in a.threads.split(",")] if a.threads else None
    res = tune(a.tag, a.v, a.smoke, opts)
    h, i = res["hashing"], res["dataset_init"]
    print(json.dumps({
        "winner": h["winner"], "winner_hs_median": h["winner_hs"]["median"],
        "baseline": h["baseline"], "baseline_hs_median": h["baseline_hs"]["median"],
        "ratio": h["paired"]["ratio"], "ratio_ci95": h["paired"]["ci95"],
        "init_winner_threads": i["winner_threads"], "init_time_ratio": i["time_ratio_paired"]["ratio"],
    }, indent=1))


if __name__ == "__main__":
    main()
