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


def test_tests_cpp_integer_instructions():
    m = Machine()
    op = decode(word("IADD_RS", 0, 1, mod=255))
    assert op == program.Op("IADD_RS", 0, 1, 0, shift=3)
    m.r[0], m.r[1] = 0x8000000000000000, 0x1000000000000000
    m.run(op)
    assert m.r[0] == 0

    op = decode(word("IADD_RS", 5, 1, mod=8))
    assert op == program.Op("IADD_RS", 5, 1, IMM64, shift=2)
    m.r[5], m.r[1] = 0x8000000000000000, 0x2000000000000000
    m.run(op)
    assert m.r[5] == IMM64

    op = decode(word("IADD_M", 0, 1, mod=1))
    assert op == program.Op("IADD_M", 0, 1, IMM64, mask=program.L1_MASK)

    op = decode(word("ISUB_R", 0, 1))
    assert op == program.Op("ISUB_R", 0, 1)
    m.r[0], m.r[1] = 1, 0xFFFFFFFF
    m.run(op)
    assert m.r[0] == 0xFFFFFFFF00000002

    op = decode(word("ISUB_R", 0, 0))
    assert op == program.Op("ISUB_R", 0, program.SRC_IMM, IMM64)
    m.r[0] = 0
    m.run(op)
    assert m.r[0] == (~IMM64 + 1) & MASK64

    op = decode(word("ISUB_M", 0, 1, mod=0))
    assert op == program.Op("ISUB_M", 0, 1, IMM64, mask=program.L2_MASK)

    op = decode(word("IMUL_R", 0, 1))
    m.r[0], m.r[1] = 0xBC550E96BA88A72B, 0xF5391FA9F18D6273
    m.run(op)
    assert m.r[0] == 0x28723424A9108E51

    op = decode(word("IMUL_R", 0, 0))
    assert op == program.Op("IMUL_R", 0, program.SRC_IMM, IMM64)
    m.r[0] = 1
    m.run(op)
    assert m.r[0] == IMM64

    op = decode(word("IMUL_M", 0, 0, mod=0))
    assert op == program.Op("IMUL_M", 0, program.SRC_ZERO, IMM64, mask=program.L3_MASK)

    op = decode(word("IMULH_R", 0, 1))
    m.r[0], m.r[1] = 0xBC550E96BA88A72B, 0xF5391FA9F18D6273
    m.run(op)
    assert m.r[0] == 0xB4676D31D2B34883
    assert decode(word("IMULH_R", 0, 0)) == program.Op("IMULH_R", 0, 0)
    assert decode(word("IMULH_M", 0, 1, mod=0)) == program.Op("IMULH_M", 0, 1, IMM64, mask=program.L2_MASK)

    op = decode(word("ISMULH_R", 0, 1))
    m.r[0], m.r[1] = 0xBC550E96BA88A72B, 0xF5391FA9F18D6273
    m.run(op)
    assert m.r[0] == 0x02D93EF1269D3EE5
    assert decode(word("ISMULH_R", 0, 0)) == program.Op("ISMULH_R", 0, 0)
    assert decode(word("ISMULH_M", 0, 1, mod=3)) == program.Op("ISMULH_M", 0, 1, IMM64, mask=program.L1_MASK)

    op = decode(word("IMUL_RCP", 0))
    assert op == program.Op("IMUL_RCP", 0, program.SRC_IMM, reciprocal(IMM32))
    assert decode(word("IMUL_RCP", 0, imm32=0)).kind == "NOP"

    op = decode(word("INEG_R", 0))
    m.r[0] = 0xFFFFFFFFFFFFFFFF
    m.run(op)
    assert m.r[0] == 1

    op = decode(word("IXOR_R", 0, 1))
    m.r[0], m.r[1] = 0x8888888888888888, 0xAAAAAAAAAAAAAAAA
    m.run(op)
    assert m.r[0] == 0x2222222222222222

    op = decode(word("IXOR_R", 0, 0))
    m.r[0] = 0xFFFFFFFFFFFFFFFF
    m.run(op)
    assert m.r[0] == ~IMM64 & MASK64
    assert decode(word("IXOR_M", 0, 0)) == program.Op("IXOR_M", 0, program.SRC_ZERO, IMM64, mask=program.L3_MASK)

    op = decode(word("IROR_R", 0, 1))
    m.r[0], m.r[1] = 953360005391419562, 4569451684712230561
    m.run(op)
    assert m.r[0] == 0xD835C455069D81EF

    op = decode(word("IROL_R", 0, 1))
    m.r[0], m.r[1] = 953360005391419562, 4569451684712230561
    m.run(op)
    assert m.r[0] == 6978065200552740799

    op = decode(word("ISWAP_R", 0, 1))
    m.r[0], m.r[1] = 953360005391419562, 4569451684712230561
    m.run(op)
    assert (m.r[0], m.r[1]) == (4569451684712230561, 953360005391419562)


def test_tests_cpp_reciprocals():
    assert reciprocal(3) == 12297829382473034410
    assert reciprocal(13) == 11351842506898185609
    assert reciprocal(33) == 17887751829051686415
    assert reciprocal(65537) == 18446462603027742720
    assert reciprocal(15000001) == 10316166306300415204
    assert reciprocal(3845182035) == 10302264209224146340
    assert reciprocal(0xFFFFFFFF) == 9223372039002259456


ROUNDING = {"nearest": fp.ROUND_NEAREST, "down": fp.ROUND_DOWN, "up": fp.ROUND_UP, "zero": fp.ROUND_ZERO}


def test_tests_cpp_float_instructions():
    m = Machine()
    op = decode(word("FSWAP_R", 0))
    assert op == program.Op("FSWAP_R", 0)
    m.f[0:2] = vec(953360005391419562, 4569451684712230561)
    m.run(op)
    assert vec_hex(m.f[0:2]) == "aa886bb0df033b0da12e95e518f4693f"

    op = decode(word("FADD_R", 0, 1))
    assert op == program.Op("FADD_R", 0, 1)
    a = [1.0] * 8
    a[2:4] = vec(0x402a26a86a60c8fb, 0x40b8f684057a59e1)
    want = {"nearest": "b932e048a730cec1fea6ea633bcc2d40", "down": "b932e048a730cec1fda6ea633bcc2d40",
            "up": "b832e048a730cec1fea6ea633bcc2d40", "zero": "b832e048a730cec1fda6ea633bcc2d40"}
    for mode, expected in want.items():
        m.f[0:2] = vec(0x3ffd2c97cc4ef015, 0xc1ce30b3c4223576)
        m.fprc = ROUNDING[mode]
        m.run(op, config(a))
        assert vec_hex(m.f[0:2]) == expected, mode

    op = decode(word("FADD_M", 0, 1, mod=1))
    assert op == program.Op("FADD_M", 0, 1, IMM64, mask=program.L1_MASK)
    m.fprc = fp.ROUND_NEAREST
    m.r[1] = 0xFFFFFFFFFFFFE930
    addr = (m.r[1] + IMM64) & program.L1_MASK
    struct.pack_into("<Q", m.sp, addr, 0x1234567890abcdef)
    m.f[0:2] = [0.0, 0.0]
    m.run(op)
    assert vec_hex(m.f[0:2]) == "000040840cd5dbc1000000785634b241"

    assert decode(word("FSUB_R", 0, 1)) == program.Op("FSUB_R", 0, 1)
    assert decode(word("FSUB_M", 0, 1, mod=2)) == program.Op("FSUB_M", 0, 1, IMM64, mask=program.L1_MASK)

    op = decode(word("FSCAL_R", 0))
    assert op == program.Op("FSCAL_R", 0)
    m.f[0:2] = vec(0x41dbc35cef248783, 0x40fdfdabb6173d07)
    m.run(op)
    assert vec_hex(m.f[0:2]) == "073d17b6abfd0dc0838724ef5cc32bc1"

    op = decode(word("FMUL_R", 0, 1))
    assert op == program.Op("FMUL_R", 0, 1)
    a[2:4] = vec(0x40eba861aa31c7c0, 0x41c4561212ae2d50)
    want = {"nearest": "69697aff350fd3422f1589cdecfed742", "down": "69697aff350fd3422e1589cdecfed742",
            "up": "6a697aff350fd3422f1589cdecfed742", "zero": "69697aff350fd3422e1589cdecfed742"}
    for mode, expected in want.items():
        m.e[0:2] = vec(0x41dbc35cef248783, 0x40fdfdabb6173d07)
        m.fprc = ROUNDING[mode]
        m.run(op, config(a))
        assert vec_hex(m.e[0:2]) == expected, mode

    op = decode(word("FDIV_M", 0, 1, mod=3))
    assert op == program.Op("FDIV_M", 0, 1, IMM64, mask=program.L1_MASK)
    m.r[1] = 0xFFFFFFFFFFFFE930
    struct.pack_into("<II", m.sp, (m.r[1] + IMM64) & program.L1_MASK, 0xd350a1b6, 0x8b2460d9)
    emask = (0x3a0000000005d11a, 0x39000000001ba31e)
    want = {"nearest": "e7b269639484434632474a66635ba547", "down": "e6b269639484434632474a66635ba547",
            "up": "e7b269639484434633474a66635ba547", "zero": "e6b269639484434632474a66635ba547"}
    for mode, expected in want.items():
        m.e[0:2] = vec(0x41937f76fede16ee, 0x411b414296ce93b6)
        m.fprc = ROUNDING[mode]
        m.run(op, config(emask=emask))
        assert vec_hex(m.e[0:2]) == expected, mode

    op = decode(word("FSQRT_R", 0))
    assert op == program.Op("FSQRT_R", 0)
    want = {"nearest": "e81f300b612a21408dbaa33f570ed340", "down": "e81f300b612a21408cbaa33f570ed340",
            "up": "e91f300b612a21408dbaa33f570ed340", "zero": "e81f300b612a21408cbaa33f570ed340"}
    for mode, expected in want.items():
        m.e[0:2] = vec(0x41b6b21c11affea7, 0x40526a7e778d9824)
        m.fprc = ROUNDING[mode]
        m.run(op)
        assert vec_hex(m.e[0:2]) == expected, mode


def test_tests_cpp_control_and_store():
    last_write = [0] * 8  # tests.cpp decodes many instructions at pc 0 before these
    op = decode(word("CBRANCH", 0, mod=48), 100, last_write)
    assert op == program.Op("CBRANCH", 0, 0, 0xFFFFFFFFC0CB9AD2, mask=0x7F800, target=0)
    op = decode(word("CBRANCH", 0, mod=48), 200, last_write)
    assert (op.imm, op.mask, op.target) == (0xFFFFFFFFC0CB9AD2, 0x7F800, 100)
    m = Machine()
    m.r[0] = 0
    assert not m.run(op)
    m.r[0] = 0xFFFFFFFFFFFC6800
    assert m.run(op)

    op = decode(word("CFROUND", src=1), 100)
    assert (op.kind, op.src, op.imm) == ("CFROUND", 1, 18)

    for mod, mask in ((1, program.L1_MASK), (0, program.L2_MASK), (224, program.L3_MASK)):
        op = decode(word("ISTORE", 0, 1, mod=mod))
        assert op == program.Op("ISTORE", 0, 1, IMM64, mask=mask)


def splitmix64(seed, n):
    out, s = [], seed
    for _ in range(n):
        s = (s + 0x9E3779B97F4A7C15) & MASK64
        z = s
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & MASK64
        out.append(z ^ (z >> 31))
    return out


def test_instructions_match_c_interpreter():
    """816 single instructions run by BytecodeMachine::executeInstruction (all modes, edge values)."""
    g = golden("instructions")
    original = struct.pack("<%dQ" % (SCRATCHPAD_L3 // 8), *splitmix64(g["scratchpad_splitmix64_seed"], SCRATCHPAD_L3 // 8))
    m = Machine(bytearray(original))
    failures = []
    for case in g["cases"]:
        before = struct.unpack("<32Q", bytes.fromhex(case["before"]))
        instr = program.Instruction.unpack(bytes.fromhex(case["word"]))
        assert instr.name == case["name"]
        op = decode(instr, case["pc"])
        m.r = list(before[:8])
        m.f = [fp.from_bits(q) for q in before[8:16]]
        m.e = [fp.from_bits(q) for q in before[16:24]]
        m.fprc = case["fprc"]
        cfg = config(tuple(fp.from_bits(q) for q in before[24:32]), tuple(case["emask"]))
        taken = m.run(op, cfg, case["v2"])
        after = m.r + [fp.to_bits(x) for x in m.f + m.e] + list(before[24:32])
        got = (struct.pack("<32Q", *after).hex(), op.target if taken else case["pc"], m.fprc)
        if got != (case["after"], case["pc_after"], case["fprc_after"]):
            failures.append((case["name"], case["word"], case["fprc"]))
        for offset, value in case["stores"]:
            assert struct.unpack_from("<Q", m.sp, offset)[0] == value
            m.sp[offset:offset + 8] = original[offset:offset + 8]
        assert m.sp == original, case["name"]
    assert not failures, failures[:10]


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
