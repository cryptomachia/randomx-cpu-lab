"""Offline tests: leaderboard parsing/grouping/models on synthetic rows and on data/leaderboard/rigs.csv."""
import statistics

import numpy as np
import pytest

from rxlab import econ
from rxlab.analysis import leaderboard as lb


def detail(i, board="X670", dimm="HMCG78", size=10_000_000, packages=1, hashrate=1000.0, os_="linux",
           user=None, memory=True):
    mem = [{"id": "DIMM_A1", "slot": "DIMM 1", "bank": "P0 CHANNEL A", "type": "DDR5", "size": 16 << 30,
            "speed": 6000_000_000, "product": dimm},
           {"id": "DIMM_B1", "slot": "DIMM 1", "bank": "P0 CHANNEL B", "type": "DDR5", "size": 16 << 30,
            "speed": 6000_000_000, "product": dimm},
           {"id": "DIMM_B0", "slot": "DIMM 0", "bank": "P0 CHANNEL B", "type": "Unknown", "size": 0}]
    return {
        "id": f"id{i}", "algo": "rx/0", "size": size, "threads": 32, "hashrate": hashrate, "os": os_,
        "version": "6.22.2", "done_ts": 1731858813113, "user": user,
        "cpu": {"brand": "AMD Ryzen 9 7950X 16-Core Processor ", "vendor": "amd", "arch": "x86_64",
                "family": 25, "model": 97, "cores": 16, "threads": 32, "l2": 16 << 20, "l3": 64 << 20,
                "packages": packages, "nodes": 1},
        "dmi": {"board": {"product": board}, "memory": mem} if memory else None,
        "backend": {"hugepages": [35, 35], "msr": True, "asm": "ryzen"},
    }


def test_submission_flattens_memory():
    rec = lb.submission(detail(1))
    assert rec["cpu"] == "AMD Ryzen 9 7950X 16-Core Processor"
    assert (rec["dimms"], rec["channels"], rec["mem_type"], rec["mem_mts"]) == (2, 2, "DDR5", 6000)
    assert rec["l3_mib"] == 64 and rec["hugepages_frac"] == 1.0


def _dimms(types, mts, labels):
    if isinstance(types, str):
        types = [types] * len(labels)
    return {"memory": [{"bank": b, "slot": b, "id": b, "type": t, "size": 8 << 30, "speed": mts * 1_000_000,
                        "product": "X"} for b, t in zip(labels, types)]}


def test_memory_rejects_clock_reported_as_transfer_rate():
    # DDR4's slowest JEDEC grade is DDR4-1600, so "1333" can only be the I/O clock of DDR4-2666
    assert lb._memory(_dimms("DDR4", 1333, ["P0 CHANNEL A", "P0 CHANNEL B"]))["mem_mts"] is None
    assert lb._memory(_dimms("DDR4", 2666, ["P0 CHANNEL A", "P0 CHANNEL B"]))["mem_mts"] == 2666
    assert lb._memory(_dimms("DDR5", 2400, ["P0 CHANNEL A"]))["mem_mts"] is None


def test_memory_lpddr_channels_unknown():
    labels = [f"Controller{c}-Channel{x}" for c in (0, 1) for x in "ABCD"]
    mem = lb._memory(_dimms("LPDDR5", 5200, labels))
    assert mem["channels"] is None and mem["mem_mts"] == 5200 and mem["dimms"] == 8


def test_memory_type_tie_is_deterministic():
    # tie broken by sorted order, not by set iteration order (which varies with PYTHONHASHSEED)
    assert lb._memory(_dimms(["Other", "Flash"], 33, ["DIMM0", "DIMM1"]))["mem_type"] == "Flash"


@pytest.mark.parametrize("dimm, label", [
    ({"bank": "P0 CHANNEL A", "slot": "DIMM 0", "id": "DIMM_A0"}, ":A"),
    ({"bank": "BANK 2", "slot": "ChannelB-DIMM0", "id": "DIMM_B0"}, ":B"),
    ({"bank": "P0_Node1_Channel2_Dimm0", "slot": "DIMMF1", "id": "DIMMF1"}, "1:2"),
    ({"bank": "NODE 1", "slot": "DIMM_C2", "id": "DIMM_C2"}, "1:C"),
    ({"bank": "BANK 0", "slot": "DIMM_P0_C0", "id": "DIMM_P0_C0"}, ":C"),
    ({"bank": "BANK 1", "slot": "DIMM_A2", "id": "DIMM_A2"}, ":A"),
    ({"bank": "BANK 0", "slot": "DDR5-A2", "id": "DDR5-A2"}, ":A"),
    ({"bank": "BANK 0", "slot": "Controller1-DIMMB2", "id": "Controller1-DIMMB2"}, "1,1:B"),
    ({"bank": "BANK 0", "slot": "Controller0-DIMM1", "id": "Controller0-DIMM1"}, "0,0:"),
    ({"bank": "A1", "slot": "A1", "id": "A1"}, None),  # socket A, slot 1: no channel information
    ({"bank": "PROC 1 DIMM 3", "slot": "PROC 1 DIMM 3", "id": "PROC 1 DIMM 3"}, None),
])
def test_channel_labels_from_real_smbios_strings(dimm, label):
    # label strings copied verbatim from api.xmrig.com submissions
    assert lb._channel(dimm) == label


def test_rigs_group_by_hardware_and_filter():
    recs = [lb.submission(d) for d in (
        detail(1, hashrate=1000), detail(2, hashrate=1100), detail(3, hashrate=3000),  # one rig
        detail(4, board="B650", hashrate=900),                                        # another rig
        detail(5, size=1_000_000, hashrate=9000),                                     # 1M burst: dropped
        detail(6, packages=2, hashrate=9000),                                         # dual socket: dropped
        detail(7, memory=False, hashrate=800), detail(8, memory=False, hashrate=820),  # anonymous pool
        detail(9, memory=False, user="alice", hashrate=700),                          # named user
    )]
    rigs = lb.build_rigs(recs)
    by_n = sorted((r["basis"], r["n"], r["hashrate"]) for r in rigs)
    assert by_n == [("anon", 2, 810.0), ("dmi", 1, 900.0), ("dmi", 3, 1100.0), ("user", 1, 700.0)]
    assert all(r["hashrate_min"] <= r["hashrate"] <= r["hashrate_max"] for r in rigs)


def test_cpu_table_is_median_of_rig_medians_with_topology_mode():
    base = {"vendor": "amd", "arch": "x86_64", "family": 25, "model": 33, "threads": 24, "l2_mib": 6.0,
            "mem_type": "DDR4", "mem_mts": 3200.0, "channels": 2, "hugepages_frac": 1.0, "n": 1}
    rows = [dict(base, cpu="R9", cores=12, l3_mib=64.0, threads_used=24, hashrate=h) for h in (10, 14, 15, 30)]
    rows.append(dict(base, cpu="R9", cores=4, l3_mib=16.0, threads_used=4, hashrate=2))  # VM slice
    (row,) = lb.cpu_table(rows)
    assert row["rigs"] == 4 and row["hashrate"] == statistics.median([10, 14, 15, 30])
    assert row["uarch"] == "zen3"


@pytest.mark.parametrize("vendor, family, model, cpu, want", [
    ("amd", 25, 97, "AMD Ryzen 9 7950X", "zen4"),     # family/model as reported by api.xmrig.com
    ("amd", 25, 33, "AMD Ryzen 9 5950X", "zen3"),
    ("amd", 23, 49, "AMD EPYC 7642", "zen2"),
    ("amd", 23, 1, "AMD Ryzen 7 1700", "zen1"),
    ("amd", 25, 17, "AMD EPYC 9654", "zen4"),
    ("amd", 26, 68, "AMD Ryzen 9 9950X", "zen5"),
    ("intel", 6, 85, "Intel Xeon Gold 6126", "skylake-sp"),
    ("intel", 6, 158, "Intel Core i9-9900K", "skylake"),
    ("intel", 6, 183, "Intel Core i9-13900K", "raptorlake"),
    ("hygon", 24, 0, "Hygon C86 3185 8-core Processor", "hygon-dhyana"),
    ("other", None, None, "Apple M4 Pro", "apple-m4"),
])
def test_uarch(vendor, family, model, cpu, want):
    arch = "aarch64" if cpu.startswith("Apple") else "x86_64"
    assert lb.uarch(vendor, family, model, cpu, arch) == want


@pytest.mark.parametrize("cpu, want", [
    ("Intel(R) Core(TM) i7-1065G7 CPU @ 1.30GHz", "mobile"),
    ("12th Gen Intel(R) Core(TM) i5-1245U", "mobile"),
    ("12th Gen Intel(R) Core(TM) i7-1260P", "mobile"),
    ("AMD Ryzen 9 7845HX with Radeon Graphics", "mobile"),
    ("AMD Ryzen 7 8700G w/ Radeon 780M Graphics", "desktop"),   # 780M is the iGPU
    ("Intel(R) Core(TM) i5-10600KF CPU @ 4.10GHz", "desktop"),
    ("Intel(R) Core(TM) i9-7980XE CPU @ 2.60GHz", "hedt"),
    ("AMD Ryzen Threadripper PRO 7975WX 32-Cores", "hedt"),
    ("AMD EPYC 9654 96-Core Processor", "server"),
    ("AMD Eng Sample: 100-000000894-04", "unknown"),
    ("Genuine Intel(R) CPU 0000%@", "unknown"),
])
def test_power_class(cpu, want):
    assert lb.power_class(cpu) == want


def _synthetic_cpus(noise=0.0, seed=1):
    rng = np.random.default_rng(seed)
    rates = {"zen2": 600.0, "zen3": 800.0, "skylake": 450.0}
    vendor = {"zen2": "amd", "zen3": "amd", "skylake": "intel"}
    rows = []
    for i in range(90):
        u = list(rates)[i % 3]
        cores = int(rng.choice([4, 6, 8, 12, 16]))
        l3 = float(rng.choice([8, 16, 32, 64]))
        used = int(rng.integers(1, 2 * cores + 1))
        eff = min(used, l3 / 2)
        eff = min(eff, cores) + 0.5 * max(0, eff - cores)
        rows.append({"cpu": f"c{i}", "uarch": u, "vendor": vendor[u], "power_class": "desktop",
                     "cores": cores, "l3_mib": l3,
                     "l2_mib": 0.5 * cores, "threads_used": used,
                     "hashrate": rates[u] * eff * float(np.exp(noise * rng.normal()))})
    return rows, rates


def test_structural_model_recovers_generating_rates():
    rows, rates = _synthetic_cpus()
    model = lb.StructuralModel("l3", prior=0.0).fit(rows)
    assert model.smt == pytest.approx(0.5)
    for u, r in rates.items():
        assert model.table()[u] == pytest.approx(r, rel=1e-9)
    cv = lb.cross_validate(lambda: lb.StructuralModel("l3", prior=0.0), rows, k=5)
    assert cv["r2_log_hashrate"] == pytest.approx(1.0)


def test_loglinear_recovers_coefficients():
    rng = np.random.default_rng(3)
    rows = [{"a": float(rng.uniform(1, 10)), "g": ["x", "y"][i % 2]} for i in range(200)]
    for r in rows:
        r["hashrate"] = float(np.exp(1.5 + 0.7 * np.log(r["a"]) + (0.3 if r["g"] == "y" else 0.0)))
    model = lb.LogLinear({"log_a": lambda r: np.log(r["a"])}, ["g"], ridge=0.0).fit(rows)
    pred = model.predict(rows)
    assert np.allclose(pred, np.log([r["hashrate"] for r in rows]))
    assert model.coef[1] == pytest.approx(0.7)


def test_kfold_is_balanced_and_seeded():
    f = lb.kfold(103, 10, seed=5)
    assert np.array_equal(f, lb.kfold(103, 10, seed=5))
    assert set(np.bincount(f)) == {10, 11}


def test_dram_demand_fraction():
    # 1000 H/s x 1 MiB against 2 channels x 8 B x 3200 MT/s
    row = {"hashrate": 1000.0, "channels": 2, "mem_mts": 3200.0}
    assert lb.dram_demand_fraction(row) == pytest.approx(1000 * 2 ** 20 / (2 * 8 * 3200e6))


def test_econ_formulas():
    # 86400 s x 0.6 XMR x $100 / 5,184,000 = $1 per H/s-day; 1 W for 24 h = 0.024 kWh
    assert econ.usd_per_hs_day(0.6, 100.0, 5_184_000) == pytest.approx(1.0)
    assert econ.breakeven_h_per_w(1.0, 0.10) == pytest.approx(0.0024)


@pytest.fixture(scope="module")
def rigs():
    if not lb.RIGS_CSV.exists():
        pytest.skip("data/leaderboard/rigs.csv not built")
    return lb.load_rigs()


def test_rigs_csv_is_consistent(rigs):
    assert len(rigs) > 1000
    assert len({r["rig_id"] for r in rigs}) == len(rigs)
    for r in rigs:
        assert r["basis"] in ("dmi", "user", "anon")
        assert r["n"] >= 1 and r["threads_used"] >= 1 and r["cores"] >= 1
        assert r["hashrate_min"] <= r["hashrate"] <= r["hashrate_max"]
    assert "user" not in rigs[0]


def test_known_cpus_in_rigs_csv(rigs):
    cpus = {c["cpu"]: c for c in lb.cpu_table(rigs)}
    zen4 = cpus["AMD Ryzen 9 7950X 16-Core Processor"]
    assert (zen4["cores"], zen4["l3_mib"], zen4["uarch"]) == (16, 64.0, "zen4")
    assert cpus["AMD Ryzen 9 5950X 16-Core Processor"]["rigs"] >= 50
