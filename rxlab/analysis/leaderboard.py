"""XMRig public benchmark database: crawl, filter, group into rigs, model.

api.xmrig.com serves an index (/1/benchmarks: one row per CPU brand, headline
= the MAX submission), a per-brand list (/1/benchmarks?cpu=...) and full
submissions (/1/benchmark/<id>) with hwloc CPU info and SMBIOS (dmi) data.
Every raw response is cached gzipped under data/cache/xmrig/ so reruns are
offline. Only compact derived rows (data/leaderboard/rigs.csv) are committed.
"""
import csv
import gzip
import hashlib
import json
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from rxlab.spec import params

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE = ROOT / "data" / "cache" / "xmrig"
RIGS_CSV = ROOT / "data" / "leaderboard" / "rigs.csv"
API = "https://api.xmrig.com/1"
USER_AGENT = "rxlab/0.1 (RandomX hardware research; cached, <=4 concurrent requests)"

ALGO = "rx/0"
MIN_SIZE = 10_000_000
MIB = 1 << 20


class Offline(RuntimeError):
    """A response is not cached and network access was disabled."""


def _cache_path(kind, name):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:60]
    digest = hashlib.sha1(name.encode()).hexdigest()[:10]
    return CACHE / kind / f"{safe}-{digest}.json.gz"


def _get(url, path, offline=False, tries=6):
    if path.exists():
        with gzip.open(path, "rt") as fh:
            return json.load(fh)
    if offline:
        raise Offline(url)
    delay = 2.0
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
            break
        except urllib.error.HTTPError as err:
            if err.code == 404 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, OSError):
            if attempt == tries - 1:
                raise
        time.sleep(delay)
        delay *= 2
    data = json.loads(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wb") as fh:
        fh.write(raw)
    tmp.replace(path)
    return data


def fetch_index(offline=False):
    """One row per CPU brand: count, total hashes (size), MAX hashrate."""
    return _get(f"{API}/benchmarks?algo={ALGO}", CACHE / "index.json.gz", offline)


def fetch_cpu(brand, offline=False):
    """Every rx/0 submission for one brand string (compact objects)."""
    q = urllib.parse.urlencode({"algo": ALGO, "cpu": brand})
    return _get(f"{API}/benchmarks?{q}", _cache_path("cpu", brand), offline)


def fetch_detail(bench_id, offline=False):
    return _get(f"{API}/benchmark/{bench_id}", _cache_path("bench", bench_id), offline)


def keep_listing(row):
    """Filters that the compact list rows can already decide."""
    return (row.get("algo") == ALGO and row.get("status") == 3
            and (row.get("size") or 0) >= MIN_SIZE
            and (row.get("cpu") or {}).get("packages") == 1)


def crawl(workers=4, offline=False, log=print):
    """Fetch (or read from cache) the index, brand lists and kept submissions."""
    index = fetch_index(offline)
    # index size = total hashes; brands whose runs are all 1M cannot pass the filter
    brands = [r["cpu"] for r in index if r["size"] > r["count"] * 1_000_000]
    log(f"index: {len(index)} brands, {sum(r['count'] for r in index)} submissions, "
        f"{len(brands)} brands with runs > 1M")
    with ThreadPoolExecutor(workers) as pool:
        listings = dict(zip(brands, pool.map(lambda b: fetch_cpu(b, offline), brands)))
        ids = [r["id"] for rows in listings.values() for r in rows if keep_listing(r)]
        log(f"lists: {sum(len(v) for v in listings.values())} rows, {len(ids)} kept")
        details, missing = [], 0
        for i, d in enumerate(pool.map(lambda x: _try_detail(x, offline), ids)):
            if d is None:
                missing += 1
            else:
                details.append(d)
            if (i + 1) % 1000 == 0:
                log(f"details: {i + 1}/{len(ids)}")
    log(f"details: {len(details)} fetched, {missing} unavailable")
    return index, listings, details


def _try_detail(bench_id, offline):
    try:
        return fetch_detail(bench_id, offline)
    except Offline:
        return None
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise


# --- submissions -> rigs -----------------------------------------------------

_CHANNEL = re.compile(r"CHANNEL[\s_-]*([A-Z0-9]+)", re.I)
_SCOPE = re.compile(r"(NODE|CONTROLLER)[\s_-]*(\d+)", re.I)
_DIMM_LETTER = re.compile(r"(?:DIMM|DDR\d)[\s_-]*(?:P\d+[\s_-]*)?([A-Z])\d", re.I)


def _clean(s):
    if not isinstance(s, str):
        return ""
    s = "".join(ch for ch in s if ch.isprintable() and ch != "\ufffd")
    return re.sub(r"\s+", " ", s).strip()


def _channel(dimm):
    """Best-effort memory-channel label from SMBIOS bank/slot/id strings."""
    fields = [(dimm.get(k) or "").strip() for k in ("bank", "slot", "id")]
    scopes = [m for f in fields for m in [_SCOPE.search(f)] if m]
    scope = ",".join(m.group(2) for m in scopes)
    # bare slot names such as "A1".."A12" (EPYC 9004 boards) name the socket, not a channel: unknown
    for pattern, order in ((_CHANNEL, fields), (_DIMM_LETTER, fields[::-1])):
        for f in order:
            m = pattern.search(f)
            if m:
                return f"{scope}:{m.group(1).upper()}"
    if any(m.group(1).upper() == "CONTROLLER" for m in scopes):
        return f"{scope}:"  # Intel "Controller<n>-DIMM<k>": one 64-bit channel per controller
    return None


# lowest JEDEC data rate of each generation: a smaller SMBIOS "speed" is the I/O clock, not MT/s
_MIN_MTS = {"DDR2": 400, "DDR3": 800, "DDR4": 1600, "DDR5": 3200}


def _memory(dmi):
    dimms = [m for m in (dmi or {}).get("memory") or [] if (m.get("size") or 0) > 0]
    if not dimms:
        return {"dimms": None, "mem_type": "", "mem_mts": None, "mem_gib": None,
                "channels": None, "dimm_products": ""}
    mem_type = _mode(m.get("type") for m in dimms if m.get("type") != "Unknown") or ""
    speeds = [m["speed"] / 1e6 for m in dimms if m.get("speed")]
    mts = min(speeds) if speeds else None  # the slowest DIMM sets the bus
    if mts is not None and mts < _MIN_MTS.get(mem_type, 0):
        mts = None
    chans = [_channel(m) for m in dimms]
    return {
        "dimms": len(dimms),
        "mem_type": mem_type,
        "mem_mts": mts,
        "mem_gib": sum(m["size"] for m in dimms) / 2 ** 30,
        # LPDDR channels are 16/32 bits wide, so their count is not comparable to 64-bit DDR channels
        "channels": len(set(chans)) if all(chans) and not mem_type.startswith("LP") else None,
        "dimm_products": "|".join(sorted({_clean(m.get("product")) for m in dimms})),
    }


def submission(d):
    """Flatten one /1/benchmark/<id> response; None if it lacks the essentials."""
    cpu, backend = d.get("cpu") or {}, d.get("backend") or {}
    if not (cpu.get("cores") and d.get("threads") and d.get("hashrate")):
        return None
    hp = backend.get("hugepages") or [None, None]
    rec = {
        "id": d["id"],
        "cpu": _clean(cpu.get("brand")),
        "vendor": cpu.get("vendor") or "",
        "arch": cpu.get("arch") or "",
        "family": cpu.get("family"),
        "model": cpu.get("model"),
        "cores": cpu["cores"],
        "threads": cpu.get("threads"),
        "l2_mib": (cpu.get("l2") or 0) / MIB,
        "l3_mib": (cpu.get("l3") or 0) / MIB,
        "nodes": cpu.get("nodes"),
        "packages": cpu.get("packages"),
        "os": d.get("os") or "",
        "version": d.get("version") or "",
        "size": d.get("size"),
        "algo": d.get("algo"),
        "threads_used": d["threads"],
        "hashrate": d["hashrate"],
        "year": time.gmtime(d["done_ts"] / 1000).tm_year if d.get("done_ts") else None,
        "hugepages_frac": hp[0] / hp[1] if hp[1] else None,
        "msr": backend.get("msr"),
        "asm": {None: "", False: "none"}.get(backend.get("asm"), str(backend.get("asm"))),
        "board": _clean(((d.get("dmi") or {}).get("board") or {}).get("product")),
        "user": d.get("user") or "",
    }
    rec.update(_memory(d.get("dmi")))
    return rec


def rig_key(rec):
    """Physical-rig identity: board + DIMM products + os + cpu when SMBIOS exists.

    Without SMBIOS, a named user is one rig and all anonymous runs of a
    cpu/os pool into one pseudo-rig, so repeat submitters cannot dominate.
    """
    base = (rec["cpu"], rec["cores"], rec["os"])
    if rec["board"] or rec["dimm_products"]:
        return "dmi", base + (rec["board"], rec["dimm_products"], rec["dimms"])
    if rec["user"]:
        return "user", base + (rec["user"],)
    return "anon", base


def _mode(values):
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return None
    return max(sorted(set(vals), key=str), key=vals.count)


def _median(values):
    vals = [v for v in values if v is not None]
    return statistics.median(vals) if vals else None


RIG_FIELDS = [
    "rig_id", "basis", "cpu", "vendor", "arch", "family", "model", "cores", "threads",
    "l2_mib", "l3_mib", "nodes", "os", "board", "dimms", "channels", "mem_type",
    "mem_mts", "mem_gib", "n", "threads_used", "hashrate", "hashrate_min",
    "hashrate_max", "hugepages_frac", "msr", "asm", "version", "year_first", "year_last",
]


def build_rigs(records):
    """Group kept submissions into rigs; hashrate is the per-rig median."""
    groups = {}
    for rec in records:
        if rec["algo"] != ALGO or (rec["size"] or 0) < MIN_SIZE or rec["packages"] != 1:
            continue
        basis, key = rig_key(rec)
        groups.setdefault((basis, key), []).append(rec)
    rigs = []
    for (basis, key), subs in groups.items():
        hs = [s["hashrate"] for s in subs]
        rigs.append({
            "rig_id": hashlib.sha1(repr(key).encode()).hexdigest()[:12],
            "basis": basis,
            **{k: _mode(s[k] for s in subs) for k in (
                "cpu", "vendor", "arch", "family", "model", "cores", "threads", "l2_mib",
                "l3_mib", "nodes", "os", "board", "dimms", "channels", "mem_type",
                "mem_mts", "mem_gib", "msr", "asm", "version")},
            "n": len(subs),
            "threads_used": _mode(s["threads_used"] for s in subs),
            "hashrate": statistics.median(hs),
            "hashrate_min": min(hs),
            "hashrate_max": max(hs),
            "hugepages_frac": _median(s["hugepages_frac"] for s in subs),
            "year_first": min((s["year"] for s in subs if s["year"]), default=None),
            "year_last": max((s["year"] for s in subs if s["year"]), default=None),
        })
    rigs.sort(key=lambda r: (r["cpu"], r["cores"], r["os"], r["rig_id"]))
    return rigs


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def write_rigs(rigs, path=RIGS_CSV):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, RIG_FIELDS, lineterminator="\n")
        w.writeheader()
        for r in rigs:
            w.writerow({k: _fmt(r[k]) for k in RIG_FIELDS})


_INT = {"family", "model", "cores", "threads", "nodes", "dimms", "channels", "n",
        "threads_used", "year_first", "year_last"}
_FLOAT = {"l2_mib", "l3_mib", "mem_mts", "mem_gib", "hashrate", "hashrate_min",
          "hashrate_max", "hugepages_frac"}


def load_rigs(path=RIGS_CSV):
    """Typed rows of rigs.csv (empty cells -> None)."""
    rows = []
    with open(path, newline="") as fh:
        for raw in csv.DictReader(fh):
            row = {}
            for k, v in raw.items():
                if v == "":
                    row[k] = None if (k in _INT or k in _FLOAT or k == "msr") else ""
                elif k in _INT:
                    row[k] = int(float(v))
                elif k in _FLOAT:
                    row[k] = float(v)
                elif k == "msr":
                    row[k] = v == "True"
                else:
                    row[k] = v
            rows.append(row)
    return rows


# --- per-CPU table and models ------------------------------------------------

_INTEL = {
    "nehalem": (26, 30, 31, 46), "westmere": (37, 44, 47), "sandybridge": (42, 45),
    "ivybridge": (58, 62), "haswell": (60, 63, 69, 70), "broadwell": (61, 71, 79, 86),
    "skylake": (78, 94, 142, 158, 165, 166), "skylake-sp": (85,), "icelake": (125, 126),
    "icelake-sp": (106, 108), "tigerlake": (140, 141), "rocketlake": (167,),
    "alderlake": (151, 154), "raptorlake": (183, 186, 191), "sapphirerapids": (143,),
    "emeraldrapids": (207,), "meteorlake": (170, 172), "lunarlake": (189,),
    "arrowlake": (197, 198), "graniterapids": (173, 174), "atom": (
        55, 76, 77, 90, 92, 93, 95, 122, 134, 138, 150, 156, 190), "xeonphi": (87, 133),
}
_INTEL_BY_MODEL = {m: name for name, models in _INTEL.items() for m in models}


def uarch(vendor, family, model, cpu, arch):
    """Coarse microarchitecture label from CPUID family/model (brand for Apple)."""
    if cpu.startswith("Apple M"):
        return "apple-" + cpu.split()[1].lower().split("-")[0]
    if arch == "aarch64" or vendor not in ("amd", "intel", "hygon"):
        return f"{arch or vendor}-other"
    if family is None or model is None:
        return f"{vendor}-unknown"
    if vendor == "hygon":
        return "hygon-dhyana"  # Zen 1 derivative; a shared "zen1" label would give that level two parent vendors
    if vendor == "intel":
        return _INTEL_BY_MODEL.get(model, f"intel-f{family}-m{model}") if family == 6 else f"intel-f{family}"
    if family == 23:
        return "zen1" if model < 0x30 else "zen2"
    if family == 25:
        return "zen4" if (0x10 <= model < 0x20 or 0x60 <= model < 0x80 or 0xA0 <= model < 0xB0) else "zen3"
    if family == 26:
        return "zen5"
    if family in (21, 22):
        return "amd-bulldozer" if family == 21 else "amd-jaguar"
    return f"amd-f{family}"


_INTEL_SKU = re.compile(r"(?:i[3579]-\d{4,5}|Ultra [3579] \d{3})([A-Z]{0,2}\d?)")
_AMD_SKU = re.compile(r"Ryzen (?:AI )?(?:\d|Z\d)(?: PRO)? \d{3,4}([A-Z]{0,3})")
_MOBILE = {"U", "Y", "P", "H", "HK", "HQ", "HX", "HS", "M", "QM", "MQ", "V"}


def power_class(cpu):
    """Clock/power proxy from the brand string: server, hedt, mobile, desktop or unknown."""
    if re.search(r"Eng Sample|Genuine Intel\(R\) CPU 0000|KVM|QEMU|Virtual|DO-Premium", cpu):
        return "unknown"
    if re.search(r"EPYC|Xeon|Opteron", cpu):
        return "server"
    intel, amd = _INTEL_SKU.search(cpu), _AMD_SKU.search(cpu)
    suffix = (intel or amd).group(1) if (intel or amd) else ""
    if "Threadripper" in cpu or (intel and suffix in ("X", "XE")):
        return "hedt"
    head = re.split(r" w/ | with ", cpu)[0]  # drop iGPU names such as "Radeon 780M"
    if (suffix in _MOBILE or (intel and re.fullmatch(r"G\d", suffix))
            or re.search(r"\d{3,4}[MUY]\b", head) or "Ryzen AI" in head):
        return "mobile"
    return "desktop"


def is_apple(row):
    return row["cpu"].startswith("Apple ")


def cpu_table(rigs):
    """Per-CPU median of rig medians over rigs whose (cores, L3) match the brand's mode.

    The mode filter drops VM slices and spoofed/misreported topology rows.
    """
    by_cpu = {}
    for r in rigs:
        by_cpu.setdefault(r["cpu"], []).append(r)
    table = []
    for cpu, rows in sorted(by_cpu.items()):
        topo = _mode((r["cores"], r["l3_mib"]) for r in rows)
        rows = [r for r in rows if (r["cores"], r["l3_mib"]) == topo]
        hs = [r["hashrate"] for r in rows]
        chans = [r["channels"] for r in rows if r["channels"]]
        first = rows[0]
        table.append({
            "cpu": cpu,
            "vendor": first["vendor"],
            "arch": first["arch"],
            "uarch": uarch(first["vendor"], _mode(r["family"] for r in rows),
                           _mode(r["model"] for r in rows), cpu, first["arch"]),
            "power_class": power_class(cpu),
            "cores": topo[0],
            "threads": _mode(r["threads"] for r in rows),
            "l2_mib": _mode(r["l2_mib"] for r in rows),
            "l3_mib": topo[1],
            "rigs": len(rows),
            "submissions": sum(r["n"] for r in rows),
            "threads_used": _mode(r["threads_used"] for r in rows),
            "hashrate": statistics.median(hs),
            "hashrate_rig_max": max(hs),
            "mem_type": _mode(r["mem_type"] for r in rows) or "",
            "mem_mts": _median([r["mem_mts"] for r in rows]),
            "channels": statistics.median(chans) if chans else None,
            "hugepages_frac": _median([r["hugepages_frac"] for r in rows]),
        })
    return table


CACHE_MODES = ("l3", "l3+l2", None)


def cache_slots(row, cache="l3"):
    """How many 2 MiB scratchpads fit in L3 ("l3"), L3 + L2 ("l3+l2") or unlimited (None)."""
    if cache is None:
        return float("inf")
    mib = row["l3_mib"] + (row["l2_mib"] if cache == "l3+l2" else 0)
    return mib * MIB / params.SCRATCHPAD_L3


def effective_threads(row, cache="l3", smt=1.0):
    """min(threads_used, cache slots), with threads beyond physical cores weighted by `smt`."""
    t = min(row["threads_used"], cache_slots(row, cache))
    return min(t, row["cores"]) + smt * max(0.0, t - row["cores"])


def kfold(n, k, seed=0):
    """Fold id per row, balanced and shuffled with a fixed seed."""
    order = np.random.default_rng(seed).permutation(n)
    folds = np.empty(n, dtype=int)
    folds[order] = np.arange(n) % k
    return folds


def r2(y, pred):
    y, pred = np.asarray(y, float), np.asarray(pred, float)
    if not (np.isfinite(y).all() and np.isfinite(pred).all()):
        raise FloatingPointError("non-finite values in r2")
    return float(1 - np.sum((y - pred) ** 2) / np.sum((y - y.mean()) ** 2))


class StructuralModel:
    """log H = log r + log T_eff with r per uarch (optionally per uarch x power class).

    Rates are partially pooled: each level is shrunk toward its parent
    (uarch x power class -> uarch -> vendor -> global) with `prior` pseudo-rows,
    and unseen levels fall back to the parent. `smt` is chosen on the training
    rows from a grid unless fixed; `cache` picks what counts as scratchpad cache.
    """

    def __init__(self, cache="l3", smt=None, prior=2.0, by_power=False):
        self.cache, self.smt_fixed, self.prior, self.by_power = cache, smt, prior, by_power

    def _levels(self, r):
        keys = [("vendor", r["vendor"]), ("uarch", r["uarch"])]
        if self.by_power:
            keys.append(("uarch+power", r["uarch"], r["power_class"]))
        return keys

    def _fit_rates(self, rows, smt):
        logr = [np.log(r["hashrate"] / effective_threads(r, self.cache, smt)) for r in rows]
        rates = {(): float(np.mean(logr))}
        for depth in range(len(self._levels(rows[0]))):
            groups, parent = {}, {}
            for r, lr in zip(rows, logr):
                levels = [()] + self._levels(r)
                groups.setdefault(levels[depth + 1], []).append(lr)
                parent[levels[depth + 1]] = levels[depth]
            for key, vals in groups.items():
                rates[key] = (sum(vals) + self.prior * rates[parent[key]]) / (len(vals) + self.prior)
        return rates

    def _rate(self, r, rates):
        best = rates[()]
        for key in self._levels(r):
            if key not in rates:
                break
            best = rates[key]
        return best

    def fit(self, rows):
        grid = [self.smt_fixed] if self.smt_fixed is not None else np.linspace(0, 1, 21)
        y = np.log([r["hashrate"] for r in rows])
        best = None
        for smt in grid:
            rates = self._fit_rates(rows, smt)
            score = r2(y, self._predict(rows, smt, rates))
            if best is None or score > best[0]:
                best = (score, float(smt), rates)
        _, self.smt, self.rates = best
        return self

    def _predict(self, rows, smt, rates):
        return np.array([self._rate(r, rates) + np.log(effective_threads(r, self.cache, smt)) for r in rows])

    def predict(self, rows):
        """Predicted log hashrate."""
        return self._predict(rows, self.smt, self.rates)

    def table(self, level="uarch"):
        """Fitted per-thread rates (H/s per effective thread) at one level."""
        return {k[1:] if len(k) > 2 else k[1]: float(np.exp(v)) for k, v in self.rates.items() if k and k[0] == level}


class LogLinear:
    """Least squares of log hashrate on numeric features plus one-hot categories.

    Category effects carry a light ridge penalty so thin levels shrink toward
    the pooled intercept and levels unseen in training predict at it.
    """

    def __init__(self, features, categories=(), ridge=1.0):
        self.features, self.categories, self.ridge = dict(features), list(categories), ridge

    def _design(self, rows):
        cols = [np.ones(len(rows))]
        cols += [np.array([f(r) for r in rows], float) for f in self.features.values()]
        cols += [np.array([r[c] == lv for r in rows], float) for c, lv in self.dummies]
        return np.column_stack(cols)

    def fit(self, rows):
        self.dummies = [(c, lv) for c in self.categories for lv in sorted({r[c] for r in rows}, key=str)]
        X, y = self._design(rows), np.log([r["hashrate"] for r in rows])
        k = 1 + len(self.features)
        # ridge as extra rows; lstsq also copes with collinear numeric features
        pen = np.hstack([np.zeros((len(self.dummies), k)), np.sqrt(self.ridge) * np.eye(len(self.dummies))])
        self.coef = np.linalg.lstsq(np.vstack([X, pen]), np.r_[y, np.zeros(len(self.dummies))], rcond=None)[0]
        return self

    def predict(self, rows):
        return self._design(rows) @ self.coef


def cross_validate(make_model, rows, k=10, seed=0, repeats=5, groups=None):
    """Out-of-fold predictions of log hashrate over repeated k-fold splits.

    Returns R^2 on log hashrate and on log per-thread rate (log H - log threads_used),
    mean and sd over repeats, plus the first repeat's out-of-fold predictions.
    median_abs_pct_error is the median of exp(|log error|) - 1 over that first repeat,
    a symmetric ratio error (a prediction off by 1.25x either way counts as 25%).
    With `groups`, whole groups are held out together.
    """
    y = np.log([r["hashrate"] for r in rows])
    lt = np.log([r["threads_used"] for r in rows])
    keys = sorted(set(groups), key=str) if groups is not None else None
    s_h, s_t, first = [], [], None
    for rep in range(repeats):
        if keys is None:
            folds = kfold(len(rows), k, seed + rep)
        else:
            gf = dict(zip(keys, kfold(len(keys), k, seed + rep)))
            folds = np.array([gf[g] for g in groups])
        oof = np.empty(len(rows))
        for f in range(k):
            test = np.flatnonzero(folds == f)
            if not len(test):
                continue
            model = make_model().fit([r for r, g in zip(rows, folds) if g != f])
            oof[test] = model.predict([rows[i] for i in test])
        s_h.append(r2(y, oof))
        s_t.append(r2(y - lt, oof - lt))
        if first is None:
            first = oof
    return {"r2_log_hashrate": float(np.mean(s_h)), "r2_log_hashrate_sd": float(np.std(s_h)),
            "r2_log_per_thread": float(np.mean(s_t)), "r2_log_per_thread_sd": float(np.std(s_t)),
            "median_abs_pct_error": float(np.median(np.expm1(np.abs(y - first))) * 100),
            "oof_log_pred": first}


def dram_demand_fraction(row):
    """Dataset traffic (1 MiB per hash) as a fraction of theoretical 64-bit-channel DRAM peak bandwidth."""
    per_hash = params.PROGRAM_COUNT * params.PROGRAM_ITERATIONS * params.DATASET_ITEM_SIZE
    if not (row.get("channels") and row.get("mem_mts")):
        return None
    peak = row["channels"] * 8 * row["mem_mts"] * 1e6  # 64-bit channel = 8 bytes per transfer
    return row["hashrate"] * per_hash / peak


def build(offline=False, log=print):
    """Crawl (or replay the cache), write rigs.csv, return (rigs, filter counts)."""
    index, listings, details = crawl(offline=offline, log=log)
    rows = [r for rs in listings.values() for r in rs]
    records = [r for r in map(submission, details) if r]
    rigs = build_rigs(records)
    write_rigs(rigs)
    counts = {
        "index_brands": len(index),
        "index_submissions": sum(r["count"] for r in index),
        "brands_with_runs_over_1M": len(listings),
        "listed_submissions": len(rows),
        "rx0_done_size_ge_10M": sum(r.get("algo") == ALGO and r.get("status") == 3
                                    and (r.get("size") or 0) >= MIN_SIZE for r in rows),
        "kept_packages_eq_1": sum(map(keep_listing, rows)),
        "details_fetched": len(details),
        "details_usable": len(records),
        "rigs": len(rigs),
        "rigs_by_basis": {b: sum(r["basis"] == b for r in rigs) for b in ("dmi", "user", "anon")},
    }
    return rigs, counts
