"""progdump against the official vectors and the spec; the skipping arithmetic by hand."""
import numpy as np
import pytest

from rxlab.analysis import programs as P
from rxlab.binding import VM, Cache, Flags, recommended_flags
from rxlab.spec import params
from test_binding_vectors import VECTORS


@pytest.fixture(scope="module")
def dumps():
    """Light-mode progdump rows (with raw programs) for every official vector, v1 and v2."""
    try:
        P.ensure_progdump()
    except Exception as exc:  # no third_party build
        pytest.skip(f"progdump unavailable: {exc}")
    out = {}
    for v2 in (False, True):
        rows = []
        for key in sorted({v[0] for v in VECTORS}):
            vecs = [v for v in VECTORS if v[0] == key]
            tab = P.dump(v2, inputs=[v[1] for v in vecs], key=key, light=True, threads=2,
                         raw_limit=len(vecs), cache=False)
            rows.append((vecs, tab))
        out[v2] = rows
    return out


@pytest.mark.parametrize("v2", [False, True])
def test_progdump_reproduces_official_hashes(dumps, v2):
    for vecs, tab in dumps[v2]:
        final = [h for h, p in zip(tab["hash"], tab["prog"]) if p == 7]
        assert final == [v[3] if v2 else v[2] for v in vecs]
        assert set(tab["size"]) == {params.PROGRAM_SIZE_V2 if v2 else params.PROGRAM_SIZE_V1}


@pytest.mark.parametrize("v2", [False, True])
def test_library_features_match_spec_reference(dumps, v2):
    """Static features from the library's bytecode equal an independent spec decoding."""
    for _, tab in dumps[v2]:
        for i, raw in enumerate(tab["raw"]):
            ref = P.reference_features(bytes.fromhex(raw), v2)
            assert {c: int(tab[c][i]) for c in P.STATIC_COLUMNS} == ref


@pytest.mark.parametrize("v2", [False, True])
def test_dynamic_counters_are_consistent(dumps, v2):
    iters = params.PROGRAM_ITERATIONS
    for _, tab in dumps[v2]:
        slots = iters * tab["size"]
        assert np.all(tab["ds_reads"] == iters)
        assert np.all((tab["executed"] > slots) == (tab["taken"] > 0))
        assert np.all(tab["executed"] >= slots)
        # a jump lands after the previous CBRANCH, so it re-executes only itself
        assert np.all(tab["cbr_exec"] == iters * tab["n_CBRANCH"] + tab["taken"])
        assert np.all(tab["cfround_exec"] >= iters * tab["n_CFROUND"])
        assert np.all(tab["fprc_changes"] <= tab["fprc_writes"])
        if v2:
            assert np.all(tab["fprc_writes"] <= tab["cfround_exec"])
        else:
            assert np.all(tab["fprc_writes"] == tab["cfround_exec"])
        assert len(set(tab["fingerprint"])) == len(tab["fingerprint"])


def test_frequency_groups_match_spec_table_5_1_1():
    freq = dict(params.FREQUENCIES)
    groups = {"integer": 120, "floating point": 94, "control": 26, "store": 16}
    integer = [t for t in P.TYPES if t.startswith("I") and t != "ISTORE"]
    assert sum(freq[t] for t in integer) == groups["integer"]
    assert sum(freq[t] for t in P.FP_TYPES) == groups["floating point"]
    assert freq["CBRANCH"] + freq["CFROUND"] == groups["control"]
    assert freq["ISTORE"] == groups["store"]
    assert np.bincount(P.OPCODE_TYPE, minlength=len(P.TYPES)).tolist() == list(freq.values())


def test_expected_counts_by_hand():
    e = P.expected_counts(256)
    # 25 integer memory-read opcodes, 1/8 with src == dst -> L3 (spec 5.1.4)
    assert e["rd_l3"] == pytest.approx(25 / 8)
    # 14 FP memory-read opcodes never use L3; mod.mem == 0 (1/4) -> L2
    assert e["rd_l2"] == pytest.approx(25 * 7 / 8 / 4 + 14 / 4)
    # ISTORE: mod.cond >= 14 (2 of 16) -> L3
    assert e["wr_l3"] == pytest.approx(2.0)
    assert e["fp"] == pytest.approx(94)
    assert P.expected_counts(384)["cbranch"] == pytest.approx(37.5)


def test_skip_gain_by_hand():
    times, score = [1, 1, 1, 3], [1, 1, 1, 3]
    assert P.skip_gain(times, score, 0.0, 0.0) == pytest.approx(0.0)
    # keep 3 unit hashes in 3 time units vs 4 hashes in 6
    assert P.skip_gain(times, score, 0.25, 0.0) == pytest.approx(0.5)
    # the skipped nonce still cost 1
    assert P.skip_gain(times, score, 0.25, 1.0) == pytest.approx(3 / 4 / (4 / 6) - 1)
    # and each kept nonce 0.5 extra
    assert P.skip_gain(times, score, 0.25, 1.0, 0.5) == pytest.approx(3 / 5.5 / (4 / 6) - 1)
    # skipping by a useless score loses hashrate once skipping costs anything
    assert P.skip_gain(times, [3, 1, 1, 1], 0.25, 0.1) < 0


def test_cv_predict_is_out_of_fold():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(4000, 5))
    y_signal = (x * [2.0, -1.0, 0.0, 0.5, 0.0]).sum(1) + 0.01 * rng.normal(size=4000)
    assert P.r2(y_signal, P.cv_predict(x, y_signal)) > 0.999
    noise = rng.normal(size=4000)
    assert P.r2(noise, P.cv_predict(x, noise)) < 0.01


def test_cost_model_memory_floor_and_bounds(dumps):
    for v2 in (False, True):
        _, tab = dumps[v2][0]
        c = P.cost_model(tab, v2)
        d = 2 if v2 else 1
        # the line requested when program i-d ends must arrive before iteration i+1 starts
        assert np.all((d + 1) * c["cycles"] - c["compute"] >= P.DATASET_LATENCY_CYCLES - 1e-9)
        assert np.all(c["cycles"] >= c["compute"])
        assert np.all((c["cycles"] == c["compute"])
                      == (c["compute"] >= P.DATASET_LATENCY_CYCLES / d))
        assert np.all(c["compute"] >= np.maximum(c["throughput"], c["int_chain"]))
        assert np.all(tab["cp_model"] >= tab["cp_int"])


def test_bench_prefix_reports_costs(dumps):
    res = P.bench_prefix(False, 50)
    assert res["count"] == 50
    assert res["ns_prefix"] > 0 and res["ns_prefix_and_features"] > 0


@pytest.mark.slow
def test_fast_mode_dump_matches_library_vm(dumps):
    """Fast (dataset) mode progdump against the library's own light-mode VM."""
    tab = P.dump(False, start=0, count=2, threads=4, cache=False)
    base = recommended_flags() & (Flags.HARD_AES | Flags.JIT | Flags.SECURE)
    with Cache(P.MONERO_KEY, flags=base) as cache, VM(base, cache=cache) as vm:
        expect = [vm.hash(P.blob_with_nonce(n)).hex() for n in range(2)]
    assert [h for h, p in zip(tab["hash"], tab["prog"]) if p == 7] == expect
