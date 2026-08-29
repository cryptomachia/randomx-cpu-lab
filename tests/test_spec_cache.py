"""Pure-Python cache, SuperscalarHash and dataset (rxlab.spec) against golden values.

Golden sources: RFC 9106 §5.1 (Argon2d), RandomX's own tests (tests.cpp, quoted
below) and data/golden/cache_*.json written by the C library through
tools/golden/build_cache_golden.sh.
"""
import ctypes
import hashlib
import json
import struct
from pathlib import Path

import numpy as np
import pytest

from rxlab import binding
from rxlab.spec import argon2d, dataset, params, superscalar
from rxlab.spec.blake2gen import BlakeGenerator

GOLDEN = Path(__file__).resolve().parent.parent / "data" / "golden"
KEY = b"test key 000"

# third_party/randomx-v2.0.1/src/tests/tests.cpp, key "test key 000".
TESTS_CPP_CACHE_WORDS = {0: 0x191e0e1d23c02186, 1568413: 0xf1b62fe6210bf8b1,
                         33554431: 0x1f47f056d05cd99b}
TESTS_CPP_PROGRAM_HASHES = [
    "d3a4a6623738756f77e6104469102f082eff2a3e60be7ad696285ef7dfc72a61",
    "f5e7e0bbc7e93c609003d6359208688070afb4a77165a552ff7be63b38dfbc86",
    "85ed8b11734de5b3e9836641413a8f36e99e89694f419c8cd25c3f3f16c40c5a",
    "5dd956292cf5d5704ad99e362d70098b2777b2a1730520be52f772ca48cd3bc0",
    "6f14018ca7d519e9b48d91af094c0f2d7e12e93af0228782671a8640092af9e5",
    "134be097c92e2c45a92f23208cacd89e4ce51f1009a0b900dbe83b38de11d791",
    "268f9392c20c6e31371a5131f82bd7713d3910075f2f0468baafaa1abd2f3187",
    "c668a05fd909714ed4a91e8d96d67b17e44329e88bc71e0672b529a3fc16be47",
    "99739351315840963011e4c5d8e90ad0bfed3facdcb713fe8f7138fbf01c4c94",
    "14ab53d61880471f66e80183968d97effd5492b406876060e595fcf9682f9295",
]
TESTS_CPP_ITEM_WORD0 = {0: 0x680588a85ae222db, 10000000: 0x7943a1f6186ffb72,
                        20000000: 0x9035244d718095e1, 30000000: 0x145a5091f7853099}


def golden(name):
    with open(GOLDEN / f"cache_{name}.json") as fh:
        return json.load(fh)


def ints(hexes):
    return [int(h, 16) for h in hexes]


def hash256(data):
    """Blake2b-256 of bytes or of a contiguous array's memory."""
    return hashlib.blake2b(data, digest_size=32).hexdigest()


def check_cache(words, g):
    flat = np.ascontiguousarray(words, dtype="<u8").reshape(-1)
    assert hash256(flat) == g["cache_hash256"]
    blocks = flat.reshape(-1, argon2d.BLOCK_WORDS)
    for index, block in g["blocks"].items():
        assert blocks[int(index)].astype("<u8").tobytes().hex() == block, index
    for index, word in TESTS_CPP_CACHE_WORDS.items():
        assert int(flat[index]) == word


@pytest.fixture(scope="module")
def c_cache():
    """The C library's cache for KEY (randomx_get_cache_memory) as bytes, or None."""
    try:
        cache = binding.Cache(KEY, flags=binding.Flags.DEFAULT)
    except OSError:  # includes FileNotFoundError: library not built
        return None
    with cache:
        return ctypes.string_at(cache.memory(), argon2d.CACHE_WORDS * 8)


@pytest.fixture(scope="module")
def cache_words(c_cache):
    """KEY's cache for the dataset tests: the C bytes, else the memoised Python fill."""
    if c_cache is not None:
        return c_cache
    if argon2d.cache_path(KEY).exists():
        return argon2d.fill_cache(KEY)
    pytest.skip("needs the RandomX library or a memoised Python cache")


def test_argon2d_rfc9106_vector():
    g = golden("rfc9106")
    a = argon2d.Argon2d(bytes.fromhex(g["password"]), bytes.fromhex(g["salt"]), g["passes"],
                        g["memory_kib"], lanes=g["lanes"], tag_len=g["tag_len"],
                        secret=bytes.fromhex(g["secret"]),
                        ad=bytes.fromhex(g["associated_data"]), version=g["version"])
    assert a.h0.hex() == g["prehash"]
    for pass_no, blocks in enumerate(g["after_pass"]):
        a.fill_pass(pass_no)
        for block, words in blocks.items():
            for word, value in words.items():
                got = int(a.memory[int(block), int(word)])
                assert got == int(value, 16), (pass_no, block, word)
    assert a.tag().hex() == g["tag"]


def test_argon2d_randomx_pass0_prefix():
    """The real RandomX Argon2d (H0, first blocks, 4094 compressions) against C."""
    g = golden("argon2")
    a = argon2d.randomx_argon2d(bytes.fromhex(g["key"]))
    n = g["pass0_prefix_blocks"]
    a.fill_segment(0, 0, 0, stop=n)
    assert hash256(a.memory[:n].astype("<u8").tobytes()) == g["pass0_prefix_hash256"]
    for index, block in g["pass0_blocks"].items():
        assert a.memory[int(index)].astype("<u8").tobytes().hex() == block


def test_c_cache_matches_golden(c_cache):
    if c_cache is None:
        pytest.skip("RandomX library unavailable; run scripts/build_randomx.sh")
    check_cache(np.frombuffer(c_cache, dtype="<u8"), golden("argon2"))


def test_memoised_cache_matches_golden():
    if not argon2d.cache_path(KEY).exists():
        pytest.skip("no memoised Python cache; argon2d.fill_cache(KEY) makes one")
    check_cache(argon2d.fill_cache(KEY), golden("argon2"))


@pytest.mark.slow
def test_full_python_cache_fill(tmp_path, monkeypatch):
    """All 786,432 compressions (about a minute of CPU), via the memoising path."""
    monkeypatch.setattr(argon2d, "CACHE_DIR", tmp_path)
    words = argon2d.fill_cache(KEY)
    assert argon2d.cache_path(KEY).exists()
    check_cache(words, golden("argon2"))


def test_blake_generator_stream():
    g = golden("superscalar")
    gen = BlakeGenerator(bytes.fromhex(g["key"]))
    assert [gen.get_byte() for _ in g["generator_bytes"]] == g["generator_bytes"]
    assert [gen.get_uint32() for _ in g["generator_uint32"]] == g["generator_uint32"]


def program_hash(program):
    """Hash256 of the 8-byte C Instruction structs, as tests.cpp computes it."""
    return hash256(b"".join(struct.pack("<4BI", *ins) for ins in program.instructions))


def test_superscalar_tests_cpp_hashes():
    gen = BlakeGenerator(KEY)
    for expected in TESTS_CPP_PROGRAM_HASHES:
        assert program_hash(superscalar.generate(gen)) == expected


def check_keys(count):
    g = golden("superscalar_keys")
    for row in g["rows"][:count * params.CACHE_ACCESSES]:
        expected = dict(zip(g["columns"], row))
        if expected["program"] == 0:
            gen = BlakeGenerator((g["key_format"] % expected["key"]).encode())
        p = superscalar.generate(gen)
        assert program_hash(p) == expected["hash256"], (expected["key"], expected["program"])
        for field in ("address_register", "cpu_latency", "asic_latency", "mul_count",
                      "decode_cycles", "macro_ops", "code_size"):
            assert getattr(p, field) == expected[field], field


def test_superscalar_more_keys():
    check_keys(4)


@pytest.mark.slow
def test_superscalar_all_golden_keys():
    """All 2048 programs of cache_superscalar_keys.json (256 keys)."""
    check_keys(256)


def test_superscalar_programs_match_c():
    """Instructions and every reference-CPU statistic of the 8 cache programs."""
    g = golden("superscalar")
    gen = BlakeGenerator(bytes.fromhex(g["key"]))
    for expected in g["programs"]:
        p = superscalar.generate(gen)
        assert [list(ins) for ins in p.instructions] == expected["instructions"]
        assert len(p.instructions) == expected["size"]
        for field in ("address_register", "cpu_latency", "asic_latency", "code_size",
                      "macro_ops", "decode_cycles", "mul_count", "ipc"):
            assert getattr(p, field) == expected[field], field
        assert list(p.cpu_latencies) == expected["cpu_latencies"]
        assert list(p.asic_latencies) == expected["asic_latencies"]


def run_statements(program, registers):
    body = "".join(f"    {line}\n" for line in superscalar.to_python(program))
    namespace = {"M": params.MASK64, "S": 1 << 63}
    exec(f"def f(r0, r1, r2, r3, r4, r5, r6, r7):\n{body}"
         "    return [r0, r1, r2, r3, r4, r5, r6, r7]", namespace)
    return namespace["f"](*registers)


def test_superscalar_execution_matches_c():
    g = golden("superscalar")
    gen = BlakeGenerator(bytes.fromhex(g["key"]))
    for expected in g["programs"]:
        p = superscalar.generate(gen)
        assert superscalar.execute(p, ints(expected["input"])) == ints(expected["output"])
        assert run_statements(p, ints(expected["input"])) == ints(expected["output"])


def test_dataset_constants_are_nothing_up_my_sleeve():
    """specs.md §7.3: Hash512 of a fixed string, bytes 8-63, two documented tweaks."""
    digest = hashlib.blake2b(b"RandomX SuperScalarHash initialize").digest()
    derived = list(struct.unpack("<7Q", digest[8:]))
    derived[0] += 2 ** 33 + 700
    derived[2] += 2 ** 14
    assert tuple(derived) == dataset.XORS


def test_dataset_programs_and_reciprocals():
    g = golden("dataset")
    source = dataset.DatasetItemSource(np.zeros(dataset.CACHE_WORDS, np.uint64), KEY)
    assert source.reciprocals == ints(g["reciprocal_cache"])
    assert [p.address_register for p in source.programs] == g["address_registers"]


def test_dataset_items_match_c(cache_words):
    g = golden("dataset")
    source = dataset.DatasetItemSource(cache_words, KEY)
    for number, words in g["items"].items():
        assert source.item(int(number)) == tuple(ints(words)), number
        assert source.reference_item(int(number)) == tuple(ints(words)), number
    for number, word0 in TESTS_CPP_ITEM_WORD0.items():
        assert source.item(number)[0] == word0
    assert str(params.DATASET_ITEM_COUNT - 1) in g["items"]  # C's last item index agrees


def test_dataset_item0_trace(cache_words):
    """Each of the 8 cache accesses of item 0: line index, program output, mix."""
    source = dataset.DatasetItemSource(cache_words, KEY)
    r = dataset.initial_registers(0)
    cache_index = 0
    for program, step in zip(source.programs, golden("dataset")["item0_trace"]):
        assert cache_index % params.CACHE_LINE_COUNT == step["cache_line"]
        line = source.cache_line(cache_index)
        superscalar.execute(program, r)
        assert r == ints(step["after_program"])
        r = [x ^ y for x, y in zip(r, line)]
        assert r == ints(step["after_mix"])
        cache_index = r[program.address_register]
