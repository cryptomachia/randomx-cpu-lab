"""Drive the native rxbench benchmark and validate what it reports.

tools/rxbench.c (built into build/rxbench-<tag> by tools/build_rxbench.sh)
prints one JSON object per line. A Session keeps one rxbench process, and so
one cache and dataset, alive across commands. Every hash rxbench reports is
recomputed in light mode through rxlab.binding before results are accepted.

macOS has no hard CPU affinity: per-thread QoS classes are the only lever that
steers threads towards P- or E-cores, and rxbench samples which logical CPU
each hash finished on so placement is measured, not assumed.
"""
import argparse
import json
import plistlib
import struct
import subprocess
import sys
import threading

from rxlab import binding, stats, sysinfo

ROOT = binding.ROOT
DEFAULT_TAG = binding.DEFAULT_TAG
RESULTS = ROOT / "results"
FIGURES = ROOT / "figures"
SMOKE = ROOT / "data" / "cache" / "smoke"
SMOKE_INIT_THREADS = 4  # smoke runs share the machine; keep dataset init light
SESSION_OVERHEAD_S = 10.0  # cache + dataset init + verification, idle M4 estimate
TRIAL_OVERHEAD_S = 0.3

QOS_ALIASES = {
    "unset": "unset", "user_interactive": "ui", "user_initiated": "in",
    "default": "def", "utility": "ut", "background": "bg",
}

# "Reference result" printed by upstream randomx-benchmark for --nonces 1000
# --seed 0 (third_party/randomx-v2.0.1/src/tests/benchmark.cpp).
UPSTREAM_REFERENCE = {
    1: "10b649a3f15c7c7f88277812f2e74b337a0f20ce909af09199cccb960771cfa1",
    2: "b85d79e080b10b6ad28c2e6c993601a1361917dba979e03a0a8f7248aaf4ba52",
}


class VerificationError(RuntimeError):
    pass


def binary(tag=DEFAULT_TAG):
    path = ROOT / "build" / f"rxbench-{tag}"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run tools/build_rxbench.sh")
    return path


def qos_mix(*parts):
    """Canonical QoS spec from class names or (class, count) pairs, e.g.
    qos_mix(("ui", 4), "bg") == "ui*4+bg" (the last class fills the rest)."""
    out = []
    for part in parts:
        name, count = (part, None) if isinstance(part, str) else part
        alias = QOS_ALIASES.get(name, name)
        if alias not in QOS_ALIASES.values():
            raise ValueError(f"unknown QoS class {name!r}")
        out.append(alias if count is None else f"{alias}*{int(count)}")
    return "+".join(out)


def spec(v=1, mode="fast", jit=True, aes="hard", batch=True, threads=1, qos="unset"):
    """Canonical rxbench CONFIG string (identical to the one rxbench echoes back)."""
    return (f"v={int(v)},mode={mode},jit={int(bool(jit))},aes={aes},batch={int(bool(batch))},"
            f"threads={int(threads)},qos={qos}")


def parse_spec(s):
    d = dict(kv.split("=", 1) for kv in s.split(","))
    for k in ("v", "jit", "batch", "threads"):
        d[k] = int(d[k])
    return d


def cpu_clusters():
    """{logical cpu id: 'P' | 'E'} from the IORegistry device tree (macOS only)."""
    try:
        out = subprocess.run(["ioreg", "-a", "-l", "-p", "IODeviceTree", "-r", "-n", "cpus"],
                             capture_output=True, check=True).stdout
        nodes = plistlib.loads(out)[0].get("IORegistryEntryChildren", [])
    except (OSError, subprocess.CalledProcessError, ValueError, IndexError):
        return {}
    clusters = {}
    for node in nodes:
        cid, kind = node.get("logical-cpu-id"), node.get("cluster-type")
        if isinstance(cid, int) and isinstance(kind, bytes):
            clusters[cid] = kind.rstrip(b"\0").decode()
    return clusters


class Session:
    """One rxbench process in serve mode; commands share its cache and dataset."""

    def __init__(self, tag=DEFAULT_TAG, seed=0, init_threads=None):
        self.tag, self.seed = tag, int(seed)
        cmd = [str(binary(tag)), "--seed", str(self.seed)]
        if init_threads:
            cmd += ["--init-threads", str(int(init_threads))]
        self.proc = subprocess.Popen(cmd + ["serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self.setup = []
        first = self.proc.stdout.readline()
        if not first:
            raise RuntimeError(f"rxbench failed to start: {self.proc.stderr.read().strip()}")

    def _read(self, on_record=None):
        records = []
        for line in self.proc.stdout:
            rec = json.loads(line)
            if rec["type"] == "end":
                return records
            if rec["type"] in ("cache", "dataset"):
                self.setup.append(rec)
            records.append(rec)
            if on_record:
                on_record(rec)
        err = self.proc.stderr.read().strip()
        raise RuntimeError(f"rxbench exited ({self.proc.wait()}): {err}")

    def run(self, *args, on_record=None):
        """Run one command (e.g. 'sweep', '--trials', '3', CONFIG); returns its records."""
        self.proc.stdin.write(" ".join(str(a) for a in args) + "\n")
        self.proc.stdin.flush()
        return self._read(on_record)

    def info(self):
        return next(r for r in self.run("info") if r["type"] == "info")

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self._read()
            self.proc.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Verifier:
    """Recomputes reported hashes in light mode (the hash never depends on mode,
    JIT or AES flags, only on key, input and version)."""

    def __init__(self, tag=DEFAULT_TAG, seed=0, info=None, workers=4):
        if info is None:
            with Session(tag, seed) as s:
                info = s.info()
        self.tag, self.workers = tag, workers
        self.template, self.offset = bytes.fromhex(info["template"]), info["nonce_offset"]
        self.key = struct.pack("<i", int(seed))
        self.memo = {}
        self._cache = None

    def blob(self, nonce):
        b = bytearray(self.template)
        b[self.offset:self.offset + 4] = struct.pack("<I", nonce)
        return bytes(b)

    def expected(self, pairs):
        """{(v, nonce): hex hash} for every requested pair, computed in parallel."""
        todo = sorted(set(pairs) - self.memo.keys())
        if todo and self._cache is None:
            self._cache = binding.Cache(self.key, tag=self.tag)
        chunks = [todo[i::self.workers] for i in range(self.workers)]

        def work(chunk):
            vms = {}
            try:
                for v, nonce in chunk:
                    if v not in vms:
                        flags = self._cache.flags | (binding.Flags.V2 if v == 2 else 0)
                        vms[v] = binding.VM(flags, cache=self._cache)
                    self.memo[(v, nonce)] = vms[v].hash(self.blob(nonce)).hex()
            finally:
                for vm in vms.values():
                    vm.close()

        threads = [threading.Thread(target=work, args=(c,)) for c in chunks if c]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return {p: self.memo[p] for p in pairs}

    def verify(self, records):
        """Check every hash in trial/sample/init records; raises VerificationError."""
        claims = list(_claims(records))
        want = self.expected([(v, n) for v, n, _ in claims])
        bad = [{"v": v, "nonce": n, "got": h, "want": want[(v, n)]} for v, n, h in claims if want[(v, n)] != h]
        if bad:
            raise VerificationError(f"{len(bad)} of {len(claims)} hashes wrong, e.g. {bad[0]}")
        return {"hashes_checked": len(claims), "distinct_inputs": len(want), "mismatches": 0,
                "method": "light-mode rxlab.binding recomputation"}

    def close(self):
        if self._cache is not None:
            self._cache.close()
            self._cache = None


def _claims(records):
    for r in records:
        if r["type"] in ("trial", "sample_end"):
            if "error" in r:
                continue
            yield (r["v"], r["check"][0], r["check"][1])
            for t in r["per_thread"]:
                for k in ("first", "last"):
                    if k in t:
                        yield (r["v"], t[k][0], t[k][1])
        elif r["type"] == "init" and "check" in r:
            yield (1, r["check"][0], r["check"][1])


class Progress:
    """Prints one stderr line per trial/sample so long runs show they are alive."""

    def __init__(self, label, total=None):
        self.label, self.total, self.done = label, total, 0

    def __call__(self, rec):
        kind = rec["type"]
        if kind in ("cache", "dataset"):
            msg = f"{kind} ready in {rec['seconds']:.1f} s"
        elif kind == "trial":
            self.done += 1
            of = f"/{self.total}" if self.total else ""
            msg = f"{self.done}{of} {rec['config']} t{rec['trial']}: " + (
                rec.get("error") or f"{rec['hs']:.1f} H/s")
        elif kind == "sample":
            msg = f"t={rec['t']:.0f}s {rec['hs']:.1f} H/s thermal={rec['thermal']}"
        elif kind == "init":
            msg = f"init {rec['threads']} threads t{rec['trial']}: {rec['seconds']:.2f} s"
        else:
            return
        print(f"[{self.label}] {msg}", file=sys.stderr, flush=True)


def _check_trials(trials):
    errors = [t for t in trials if "error" in t]
    if errors:
        raise RuntimeError(f"{errors[0]['config']}: {errors[0]['error']}")
    return trials


def sweep(session, configs, warmup=2.0, measure=5.0, trials=3, order="interleave", label="sweep"):
    """Run configs; returns the raw trial records (unverified)."""
    prog = Progress(label, len(configs) * trials)
    recs = session.run("sweep", "--warmup", warmup, "--measure", measure, "--trials", trials,
                       "--order", order, *configs, on_record=prog)
    return _check_trials([r for r in recs if r["type"] == "trial"])


def sample(session, config, duration, interval, label="sample"):
    """Sustained run; returns (samples, end record)."""
    recs = session.run("sample", "--duration", duration, "--interval", interval, config,
                       on_record=Progress(label))
    return [r for r in recs if r["type"] == "sample"], next(r for r in recs if r["type"] == "sample_end")


def dataset_init(session, threads_list, trials=1, fraction=1.0, qos="unset", label="init"):
    recs = session.run("init", "--threads-list", ",".join(str(int(t)) for t in threads_list),
                       "--trials", trials, "--fraction", fraction, "--qos", QOS_ALIASES.get(qos, qos),
                       on_record=Progress(label))
    return [r for r in recs if r["type"] == "init"]


def reference_check(session, v=1, mode="light", threads=4, jit=True, batch=True):
    """XOR of hashes of nonces 0..999 (seed 0), compared with the upstream value."""
    if session.seed != 0:
        raise ValueError("the upstream reference is for seed 0")
    rec = next(r for r in session.run("verify", "--nonces", 1000,
                                      spec(v=v, mode=mode, jit=jit, batch=batch, threads=threads))
               if r["type"] == "verify")
    return {"config": rec["config"], "result": rec["result"], "expected": UPSTREAM_REFERENCE[v],
            "ok": rec["result"] == UPSTREAM_REFERENCE[v]}


def cluster_share(cpus, clusters):
    """Fraction of sampled hashes that finished on each cluster kind."""
    counts = {}
    for cpu, n in cpus.items():
        kind = clusters.get(int(cpu), "?")
        counts[kind] = counts.get(kind, 0) + n
    total = sum(counts.values())
    return {k: n / total for k, n in sorted(counts.items())} if total else {}


def paged(rec):
    """True if the process took major faults or the system swapped in during the window."""
    vm = rec.get("vm", {})
    return vm.get("majflt", 0) > 0 or vm.get("swapins", 0) > 0


def smoke_mode(smoke):
    """--smoke runs use light mode: no 2080 MiB dataset build, so they fit in 30 s
    on a busy machine. The fast path is covered by the slow tests and the
    reference preflight of experiments/run_measurements.sh."""
    return "light" if smoke else "fast"


def summarize_trials(trials, clusters=None):
    """One row per config (in first-seen order): H/s summary and per-thread medians."""
    groups = {}
    for t in trials:
        groups.setdefault(t["config"], []).append(t)
    rows = []
    for cfg, ts in groups.items():
        row = dict(parse_spec(cfg), config=cfg, hs=stats.summarize([t["hs"] for t in ts]),
                   incomplete_trials=sum(not t["complete"] for t in ts),
                   paging_trials=sum(paged(t) for t in ts))
        n = ts[0]["threads"]
        row["thread_hs_median"] = [stats.summarize([t["per_thread"][i]["hs"] for t in ts])["median"]
                                   for i in range(n)]
        row["thread_qos"] = [ts[0]["per_thread"][i]["qos"] for i in range(n)]
        if clusters:
            merged = [{} for _ in range(n)]
            for t in ts:
                for i, pt in enumerate(t["per_thread"]):
                    for cpu, c in pt["cpus"].items():
                        merged[i][cpu] = merged[i].get(cpu, 0) + c
            row["thread_cluster_share"] = [cluster_share(m, clusters) for m in merged]
        rows.append(row)
    return rows


def output_paths(name, smoke=False):
    """(json path, png path): results/ and figures/, or data/cache/smoke/ for smoke runs."""
    if smoke:
        SMOKE.mkdir(parents=True, exist_ok=True)
        return SMOKE / f"{name}.json", SMOKE / f"{name}.png"
    RESULTS.mkdir(exist_ok=True)
    FIGURES.mkdir(exist_ok=True)
    return RESULTS / f"{name}.json", FIGURES / f"{name}.png"


def write_json(path, payload):
    """Write payload with the machine description; returns the path."""
    doc = {"machine": sysinfo.describe(), "cpu_clusters": {str(k): v for k, v in sorted(cpu_clusters().items())}}
    doc.update(payload)
    path.write_text(json.dumps(doc, indent=1) + "\n")
    return path


def measured_sweep(configs, tag=DEFAULT_TAG, smoke=False, warmup=2.0, measure=5.0, trials=3,
                   order="interleave", label="sweep"):
    """Sweep in a fresh session and verify every hash; returns (trials, meta)."""
    with Session(tag, init_threads=SMOKE_INIT_THREADS if smoke else None) as s:
        verifier = Verifier(tag, info=s.info())
        records = sweep(s, configs, warmup, measure, trials, order, label)
        setup = s.setup
    meta = {"tag": tag, "smoke": smoke, "setup": setup, "verification": verifier.verify(records),
            "settings": {"warmup_s": warmup, "measure_s": measure, "trials": trials, "order": order}}
    verifier.close()
    return records, meta


def estimate_sweep(n_configs, trials, warmup, measure):
    """Rough wall-clock seconds for one measured_sweep on an idle machine."""
    return SESSION_OVERHEAD_S + n_configs * trials * (warmup + measure + TRIAL_OVERHEAD_S)


def experiment_args(doc):
    """Standard experiment flags: --smoke, --tag, --estimate."""
    ap = argparse.ArgumentParser(description=doc.splitlines()[0])
    ap.add_argument("--smoke", action="store_true", help="<= 30 s run; outputs go to data/cache/smoke/")
    ap.add_argument("--tag", default=DEFAULT_TAG, help="RandomX library tag")
    ap.add_argument("--estimate", action="store_true", help="print the estimated runtime in seconds and exit")
    return ap


def load_result(name):
    path = RESULTS / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def best_threads(v=1, default=None):
    """Best thread count for version v from results/bench_thread_scaling.json."""
    res = load_result("bench_thread_scaling")
    if res and not res.get("smoke"):
        return res["analysis"]["best"][f"v{v}"]["threads"]
    return default or sysinfo.describe()["logical_cpus"]


def finish(name, payload, fig, smoke):
    """Write the JSON and the figure; prints both paths."""
    js, png = output_paths(name, smoke)
    payload = {"name": name, **payload}
    write_json(js, payload)
    fig.savefig(png)
    print(f"wrote {js.relative_to(ROOT)} and {png.relative_to(ROOT)}")
    return js, png


def reference_main(a):
    """Check both versions against the upstream reference; exit status 1 on mismatch."""
    threads = min(4, sysinfo.describe()["logical_cpus"])
    with Session(a.tag, init_threads=a.init_threads) as s:
        versions = (1, 2) if s.info()["v2"] else (1,)
        checks = [reference_check(s, v=v, mode=a.mode, threads=threads) for v in versions]
    for c in checks:
        print(f"{'OK  ' if c['ok'] else 'FAIL'} {c['config']}: {c['result']}")
    if not all(c["ok"] for c in checks):
        sys.exit(1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["sweep", "sample", "init", "reference"])
    ap.add_argument("configs", nargs="*", help="rxbench CONFIG strings")
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--init-threads", type=int)
    ap.add_argument("--warmup", type=float, default=2.0)
    ap.add_argument("--measure", type=float, default=5.0)
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--order", default="interleave", choices=["interleave", "block"])
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--threads-list", default="1,2,4")
    ap.add_argument("--fraction", type=float, default=1.0)
    ap.add_argument("--mode", default="light", choices=["light", "fast"], help="reference: mode to check")
    ap.add_argument("--out", help="write <out>.json to results/ (or data/cache/smoke/ with --smoke)")
    ap.add_argument("--smoke", action="store_true")
    a = ap.parse_args(argv)
    if a.command == "reference":
        return reference_main(a)
    with Session(a.tag, a.seed, a.init_threads) as s:
        verifier = Verifier(a.tag, a.seed, info=s.info())
        payload = {"tag": a.tag, "command": a.command}
        if a.command == "sweep":
            trials = sweep(s, a.configs, a.warmup, a.measure, a.trials, a.order)
            payload.update(summary=summarize_trials(trials, cpu_clusters()), trials=trials,
                           verification=verifier.verify(trials))
        elif a.command == "sample":
            samples, end = sample(s, a.configs[0], a.duration, a.interval)
            payload.update(samples=samples, end=end, verification=verifier.verify([end]))
        elif a.command == "init":
            inits = dataset_init(s, [int(x) for x in a.threads_list.split(",")], a.trials, a.fraction)
            payload.update(inits=inits, verification=verifier.verify(inits))
        payload["setup"] = s.setup
    verifier.close()
    if a.out:
        print(write_json(output_paths(a.out, a.smoke)[0], payload))
    else:
        json.dump({k: v for k, v in payload.items() if k not in ("trials", "samples")}, sys.stdout, indent=1)
        print()


if __name__ == "__main__":
    main()
