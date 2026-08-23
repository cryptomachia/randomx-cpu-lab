"""rxlab.spec VM, AES and program decoding against the reference C library.

Golden values come from the official RandomX tests (src/tests/tests.cpp,
constants copied below) and from data/golden/vm_*.json, which
tools/golden/build_vm_golden.sh dumps from the v2.0.1 C library.
"""
import collections
import json
import math
import random
import struct
import time
from fractions import Fraction
from pathlib import Path

import pytest

from rxlab.spec import aes, fp, program, vm
from rxlab.spec.hasher import CDatasetItemSource, hash256, hash512, randomx_hash
from rxlab.spec.params import FREQUENCIES, MASK64, SCRATCHPAD_L3, reciprocal, signext32
from test_binding_vectors import VECTORS

GOLDEN = Path(__file__).resolve().parent.parent / "data" / "golden"


def golden(name):
    with open(GOLDEN / ("vm_%s.json" % name)) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- AES

def test_sbox_fips197():
    assert aes.SBOX[0x00] == 0x63 and aes.SBOX[0x53] == 0xED  # FIPS-197 5.1.1
    assert all(aes.INV_SBOX[aes.SBOX[x]] == x for x in range(256))


def test_tables_match_soft_aes_luts():
    g = golden("aes")
    assert [list(t) for t in (aes.TE0, aes.TE1, aes.TE2, aes.TE3)] == g["lut_enc"]
    assert [list(t) for t in (aes.TD0, aes.TD1, aes.TD2, aes.TD3)] == g["lut_dec"]


def test_single_rounds():
    for case in golden("aes")["rounds"]:
        state, key = bytes.fromhex(case["state"]), bytes.fromhex(case["key"])
        assert aes.encrypt_block(state, key).hex() == case["enc"]
        assert aes.decrypt_block(state, key).hex() == case["dec"]


def test_constants_follow_their_derivation():
    """specs.md 3.2-3.4 give each constant and the Blake2b string it came from."""
    def words(keys):
        return b"".join(struct.pack("<4I", *k) for k in keys)
    assert words(aes.GEN1R_KEYS) == hash512(b"RandomX AesGenerator1R keys")
    assert words(aes.GEN4R_KEYS) == (hash512(b"RandomX AesGenerator4R keys 0-3")
                                     + hash512(b"RandomX AesGenerator4R keys 4-7"))
    assert words(aes.HASH1R_STATE) == hash512(b"RandomX AesHash1R state")
    assert words(aes.HASH1R_XKEYS) == hash256(b"RandomX AesHash1R xkeys")


def test_aes_generator_1r_official_vector():
    state = bytes.fromhex("6c19536eb2de31b6c0065f7f116e86f960d8af0c57210a6584c3237b9d064dc7")
    out = aes.AesGenerator1R(state + bytes(32)).generate(64)
    assert out[:32].hex() == "fa89397dd6ca422513aeadba3f124b5540324c4ad4b6db434394307a17c833ab"


def test_aes_generators_and_hash():
    g = golden("aes")
    for case in g["fill1r"]:
        gen = aes.AesGenerator1R(bytes.fromhex(case["seed"]))
        assert gen.generate(len(case["output"]) // 2).hex() == case["output"]
        assert gen.state.hex() == case["state"]
    for case in g["fill4r"]:
        gen = aes.AesGenerator4R(bytes.fromhex(case["seed"]))
        assert gen.generate(len(case["output"]) // 2).hex() == case["output"]
    for case in g["hash1r"]:
        assert aes.aes_hash_1r(bytes.fromhex(case["input"])).hex() == case["hash"]


def test_full_scratchpad_fill_and_fingerprint():
    case = golden("aes")["scratchpad"]
    gen = aes.AesGenerator1R(bytes.fromhex(case["seed"]))
    scratchpad = gen.generate(SCRATCHPAD_L3)
    assert gen.state.hex() == case["state"]
    assert hash256(scratchpad).hex() == case["blake2b_256"]
    assert aes.aes_hash_1r(scratchpad).hex() == case["hash1r"]


# ---------------------------------------------------------------- programs

def c_bytecode(op):
    """An Op as vm_golden.cpp prints the C InstructionByteCode."""
    k = op.kind
    if k.startswith("F") and k != "FSWAP_R":
        dst = ("e%d" if k in ("FMUL_R", "FDIV_M", "FSQRT_R") else "f%d") % op.dst
    elif k == "FSWAP_R":
        dst = ("e%d" if op.dst >= 4 else "f%d") % (op.dst % 4)
    elif k in ("CFROUND", "NOP"):
        dst = "-"
    else:
        dst = "r%d" % op.dst
    if k in ("INEG_R", "FSWAP_R", "FSCAL_R", "FSQRT_R", "CBRANCH", "NOP"):
        src = "-"
    elif k in ("FADD_R", "FSUB_R", "FMUL_R"):
        src = "a%d" % op.src
    else:
        src = op.src if isinstance(op.src, str) else "r%d" % op.src
    # The C decoder runs IMUL_RCP as IMUL_R by the precomputed reciprocal.
    k = "IMUL_R" if k == "IMUL_RCP" else k
    return [k, dst, src, op.imm, op.target if k == "CBRANCH" else op.shift, op.mask]


@pytest.fixture(scope="module")
def programs():
    return golden("programs")["programs"]


def test_program_generation_and_disassembly(programs):
    for g in programs:
        data = aes.AesGenerator4R(bytes.fromhex(g["seed"])).generate(len(g["bytes"]) // 2)
        assert data.hex() == g["bytes"]
        prog = program.from_bytes(data, v2=True)
        assert prog.disassemble().splitlines() == g["text"]


def test_decode_matches_c_bytecode(programs):
    for g in programs:
        prog = program.from_bytes(bytes.fromhex(g["bytes"]), v2=True)
        for pc, (op, want) in enumerate(zip(prog.ops, g["bytecode"])):
            assert c_bytecode(op) == want, (pc, prog.instructions[pc], op)
        v1 = program.from_bytes(bytes.fromhex(g["bytes"]), v2=False)
        assert v1.ops == prog.ops[:256]


def test_configuration_matches_c(programs):
    for g in programs:
        cfg = program.from_bytes(bytes.fromhex(g["bytes"])).config
        want = g["config"]
        assert [fp.to_bits(x) for x in cfg.a] == want["a_bits"]
        assert (cfg.ma, cfg.mx, list(cfg.read_regs)) == (want["ma"], want["mx"], want["read_regs"])
        assert cfg.dataset_offset == want["dataset_offset"]
        assert list(cfg.emask) == want["emask"]
        assert all(1 <= x < 2 ** 32 for x in cfg.a)  # specs.md 4.3


def test_opcode_table():
    counts = {name: program.OPCODES.count(name) for name, _ in FREQUENCIES}
    assert counts == dict(FREQUENCIES)


# ---------------------------------------------------------------- instructions (tests.cpp)

IMM32 = 3234567890
IMM64 = signext32(IMM32)
CEIL = {}
_total = 0
for _name, _freq in FREQUENCIES:
    _total += _freq
    CEIL[_name] = _total


def word(name, dst=0, src=0, mod=0, imm32=IMM32):
    """An instruction word as tests.cpp builds it: last opcode of `name`, high bits set in dst/src."""
    return program.Instruction(CEIL[name] - 1, 192 | dst, 192 | src, mod, imm32)


def decode(instr, pc=0, last_write=None):
    return program.decode_instruction(instr, pc, [-1] * 8 if last_write is None else last_write)


def config(a=(1.0,) * 8, emask=(0, 0)):
    return program.Config(a, 0, 0, (0, 2, 4, 6), 0, emask)


class Machine:
    """Register state plus a scratchpad for running single instructions."""

    def __init__(self, scratchpad=None):
        self.sp = bytearray(SCRATCHPAD_L3) if scratchpad is None else scratchpad
        self.r = [0] * 8
        self.f = [0.0] * 8
        self.e = [0.0] * 8
        self.fprc = 0

    def run(self, op, cfg=None, v2=False):
        step = vm.compile_instruction(op, cfg or config(), memoryview(self.sp).cast("Q"),
                                      memoryview(self.sp).cast("i"), v2)
        out = step(*self.r, *self.f, *self.e, self.fprc)
        self.r, self.f, self.e, self.fprc = list(out[:8]), list(out[8:16]), list(out[16:24]), out[24]
        return out[25]


def vec(hi, lo):
    """rx_set_vec_f128(hi, lo) as a (lo, hi) pair of doubles."""
    return [fp.from_bits(lo), fp.from_bits(hi)]


def vec_hex(pair):
    return struct.pack("<2d", *pair).hex()


ROUNDING = {"nearest": fp.ROUND_NEAREST, "down": fp.ROUND_DOWN, "up": fp.ROUND_UP, "zero": fp.ROUND_ZERO}


def splitmix64(seed, n):
    out, s = [], seed
    for _ in range(n):
        s = (s + 0x9E3779B97F4A7C15) & MASK64
        z = s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        out.append(z ^ (z >> 31))
    return out


# ---------------------------------------------------------------- floating point

# ---------------------------------------------------------------- the VM and the hash

def trace_vectors():
    return golden("hash_trace")["vectors"]


def run_program(t, index, source, stats=None):
    """Replay the hash in `t` up to program `index` from the traced seed; returns the VM."""
    gen1 = aes.AesGenerator1R(bytes.fromhex(t["seed"]))
    scratchpad = bytearray(gen1.generate(SCRATCHPAD_L3))
    assert gen1.state.hex() == t["gen1_state"]
    assert hash256(scratchpad).hex() == t["scratchpad_init"]
    machine = vm.VirtualMachine(scratchpad, source, t["v2"], stats)
    seed = gen1.state
    for i in range(index + 1):
        register_file = machine.execute(program.generate(seed, t["v2"]))
        seed = hash512(register_file)
    return machine, register_file


class _OneDataset:
    """Keeps at most one fully initialised C Dataset alive across the slow tests."""
    key = source = None

    @classmethod
    def get(cls, key):
        if key != cls.key:
            cls.close()
            cls.source, cls.key = CDatasetItemSource(key, threads=4), key
        return cls.source

    @classmethod
    def close(cls):
        if cls.source:
            cls.source.close()
        cls.key = cls.source = None


@pytest.fixture(scope="module")
def full_dataset():
    yield _OneDataset.get
    _OneDataset.close()
