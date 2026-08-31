"""rxbench driver, rxlab.bench and rxlab.tune.

Golden values: the upstream randomx-benchmark reference results (copied into
bench.UPSTREAM_REFERENCE and checked against the upstream source here) and
hashes recomputed through rxlab.binding. Timing values are never asserted.
"""
import functools
import struct
import subprocess
import sys

import pytest

from rxlab import bench, binding, tune

TAG = binding.DEFAULT_TAG
needs_rxbench = pytest.mark.skipif(not (bench.ROOT / "build" / f"rxbench-{TAG}").exists(),
                                   reason="run tools/build_rxbench.sh")


@pytest.fixture(scope="module")
def session():
    with bench.Session(TAG, init_threads=bench.SMOKE_INIT_THREADS) as s:
        yield s


@pytest.fixture(scope="module")
def verifier(session):
    v = bench.Verifier(TAG, info=session.info())
    yield v
    v.close()


def test_qos_mix_and_spec():
    assert bench.qos_mix(("user_interactive", 4), "background") == "ui*4+bg"
    assert bench.qos_mix("unset") == "unset"
    with pytest.raises(ValueError):
        bench.qos_mix("turbo")
    s = bench.spec(v=2, mode="light", jit=False, aes="soft", batch=False, threads=3, qos="ut")
    assert s == "v=2,mode=light,jit=0,aes=soft,batch=0,threads=3,qos=ut"
    assert bench.parse_spec(s) == {"v": 2, "mode": "light", "jit": 0, "aes": "soft", "batch": 0,
                                   "threads": 3, "qos": "ut"}


def test_cluster_share():
    clusters = {0: "E", 1: "E", 2: "P"}
    assert bench.cluster_share({"0": 1, "1": 1, "2": 2}, clusters) == {"E": 0.5, "P": 0.5}
    assert bench.cluster_share({}, clusters) == {}


def test_upstream_constants_match_upstream_source():
    src = bench.ROOT / "third_party" / "randomx-v2.0.1" / "src" / "tests" / "benchmark.cpp"
    if not src.exists():
        pytest.skip("third_party sources missing; run scripts/build_randomx.sh")
    text = src.read_text()
    for value in bench.UPSTREAM_REFERENCE.values():
        assert f'"{value}"' in text


@needs_rxbench
def test_rxbench_echoes_canonical_specs(session):
    specs = [bench.spec(mode="light", threads=2, qos=bench.qos_mix(("ui", 1), "bg")),
             bench.spec(v=2, mode="light", jit=False, aes="soft", batch=False, qos="def"),
             bench.spec(mode="light", threads=3, qos=bench.qos_mix(("in", 1), ("ut", 1), "unset"))]
    for s in specs:
        rec = next(r for r in session.run("verify", "--nonces", 0, s) if r["type"] == "verify")
        assert rec["config"] == s


@needs_rxbench
@pytest.mark.parametrize("cfg", [{"v": 1, "batch": True}, {"v": 2, "batch": False}, {"v": 1, "jit": False}])
def test_verify_xor_matches_binding(session, verifier, cfg):
    """rxbench's XOR over nonces 0..15 equals the XOR of binding light hashes."""
    rec = next(r for r in session.run("verify", "--nonces", 16, bench.spec(mode="light", threads=3, **cfg))
               if r["type"] == "verify")
    hashes = verifier.expected([(cfg["v"], n) for n in range(16)])
    acc = functools.reduce(lambda a, h: a ^ int.from_bytes(bytes.fromhex(h), "little"), hashes.values(), 0)
    assert rec["result"] == acc.to_bytes(32, "little").hex()


@needs_rxbench
def test_light_sweep_is_verified(session, verifier):
    configs = [bench.spec(v=1, mode="light", threads=2, qos=bench.qos_mix(("ui", 1), "bg")),
               bench.spec(v=2, mode="light", threads=1, batch=False)]
    trials = bench.sweep(session, configs, warmup=0.2, measure=0.6, trials=1)
    assert [t["config"] for t in trials] == configs
    report = verifier.verify(trials)
    assert report["mismatches"] == 0 and report["hashes_checked"] >= 3
    mixed = trials[0]
    assert [pt["qos"] for pt in mixed["per_thread"]] == ["user_interactive", "background"]
    info = session.info()
    for i, pt in enumerate(mixed["per_thread"]):
        lo, hi = i * info["nonce_stride"], (i + 1) * info["nonce_stride"]
        assert lo <= pt["first"][0] <= pt["last"][0] < hi
        assert pt["hashes"] == pt["last"][0] - pt["first"][0]  # batch pipeline loses no nonce
    assert all(t["hs"] > 0 and t["window_s"] >= 0.6 for t in trials)


@needs_rxbench
def test_verifier_rejects_a_wrong_hash(session, verifier):
    trials = bench.sweep(session, [bench.spec(mode="light")], warmup=0.1, measure=0.3, trials=1)
    first = trials[0]["per_thread"][0]["first"]
    first[1] = ("0" if first[1][0] != "0" else "1") + first[1][1:]
    with pytest.raises(bench.VerificationError):
        verifier.verify(trials)


@needs_rxbench
def test_dataset_init_digest_is_thread_invariant(session):
    inits = bench.dataset_init(session, [1, 3], trials=1, fraction=1 / 1024)
    assert len({r["digest"] for r in inits}) == 1
    assert inits[0]["items"] == inits[0]["total_items"] // 1024
    tune.check_digests(inits)
    broken = [dict(inits[0]), dict(inits[1], digest="0" * 16)]
    with pytest.raises(bench.VerificationError):
        tune.check_digests(broken)


class FakeSession:
    """Answers `sweep` commands with synthetic trials whose H/s is rates[config]."""

    def __init__(self, rates):
        self.rates, self.calls = rates, []

    def run(self, *args, on_record=None):
        args = [str(a) for a in args]
        trials = int(args[args.index("--trials") + 1])
        configs = [a for a in args[1:] if "=" in a]
        self.calls.append(configs)
        out = []
        for i in range(trials):
            for c in configs:
                n = bench.parse_spec(c)["threads"]
                hs = self.rates[c] * (1 + 0.001 * i)
                out.append(dict(bench.parse_spec(c), type="trial", config=c, trial=i, hs=hs, complete=True,
                                per_thread=[{"hs": hs / n, "qos": "unset", "cpus": {}}] * n))
        return out


def test_halving_keeps_the_fastest():
    cands = tune.candidates(1, [2, 4, 6], p=4, batches=(1, 0))
    assert len(cands) == len(set(cands)) == 2 * (2 + 2 + 4)
    rates = {c: 100.0 + i for i, c in enumerate(cands)}
    fake = FakeSession(rates)
    winner, history, records = tune.halving(fake, cands, tune.HASH_SCHEDULE)
    assert winner == max(rates, key=rates.get)
    assert [len(h["candidates"]) for h in history] == [16, 6, 2]
    assert fake.calls[0] == cands


def test_head_to_head_ratio():
    base, win = bench.spec(threads=10), bench.spec(threads=6, qos="ui")
    _, res = tune.head_to_head(FakeSession({base: 100.0, win: 110.0}), win, base, pairs=4, warmup=0, measure=1)
    assert res["paired"]["ratio"] == pytest.approx(1.1)
    assert not res["winner_is_baseline"]


def test_default_threads_and_placements():
    assert tune.default_threads(10, 4, 6) == [4, 6, 7, 9, 10, 12]
    assert set(tune.placements(4, 4)) == {"unset", "ui"}
    assert tune.placements(6, 4)["p_first_bg"] == "ui*4+bg"


@pytest.mark.slow
@needs_rxbench
@pytest.mark.parametrize("v", [1, 2])
@pytest.mark.parametrize("mode", ["light", "fast"])
def test_upstream_reference_result(session, v, mode):
    """XOR of hashes of nonces 0..999, seed 0, equals randomx-benchmark's reference."""
    assert bench.reference_check(session, v=v, mode=mode, threads=4)["ok"]


@pytest.mark.slow
@needs_rxbench
def test_fast_mode_sweep_is_verified(session, verifier):
    trials = bench.sweep(session, [bench.spec(v=1, threads=2), bench.spec(v=2, threads=2, batch=False)],
                         warmup=0.5, measure=1.0, trials=1)
    assert verifier.verify(trials)["mismatches"] == 0
    init = bench.dataset_init(session, [4], trials=1, fraction=1.0)
    assert verifier.verify(init)["hashes_checked"] == 1


def test_blob_places_nonce_little_endian():
    info = {"template": "00" * 76, "nonce_offset": 39}
    v = bench.Verifier(TAG, info=info)
    assert v.blob(0x01020304)[39:43] == struct.pack("<I", 0x01020304)
