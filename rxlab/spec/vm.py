"""The RandomX virtual machine (specs.md 4.6 and chapter 5).

Each program is compiled once into a Python function whose locals are the
registers, one short statement per instruction, so the 2048 iterations run
without per-instruction dispatch. The generated source is kept on the result
(`CompiledProgram.source`) and reads like the spec's operation column. The
iteration loop around it (4.6.2) is plain Python below.

Round-to-nearest uses Python floats; the directed rounding modes call the
exact MPFR contexts in rxlab.spec.fp, so gmpy2 stays off the fprc = 0 path.
Passing a Counter as `stats` compiles in dynamic counters: executions per
instruction kind, rounded FP instructions per rounding mode, CFROUND changes
and taken CBRANCHes.
"""
import math
import struct

from . import fp
from .aes import dec_round, enc_round
from .fp import DYNAMIC_MANTISSA_MASK
from .params import MASK64, PROGRAM_ITERATIONS
from .program import CACHE_LINE_ALIGN_MASK, L3_MASK64, SRC_ZERO, format_instruction

REGS = ["r%d" % i for i in range(8)]
FLOATS = ["%s%d%s" % (g, i, h) for g in "fe" for i in range(4) for h in "lh"]
STATE = REGS + FLOATS + ["fprc"]

_Q8 = struct.Struct("<8Q")
_D8 = struct.Struct("<8d")
_I16 = struct.Struct("<16i")
_D2 = struct.Struct("<2d")
_I4 = struct.Struct("<4I")
_W16 = struct.Struct("<16I")
_REGFILE = struct.Struct("<8Q8Q8d8d")

_FLOAT_OPS = {"ADD": "+", "SUB": "-", "MUL": "*", "DIV": "/"}
_ROUNDED = {"FADD_R", "FADD_M", "FSUB_R", "FSUB_M", "FMUL_R", "FDIV_M", "FSQRT_R"}
_INT_MEMORY = {
    "IADD_M": "{d} = ({d} + {m}) & M64",
    "ISUB_M": "{d} = ({d} - {m}) & M64",
    "IMUL_M": "{d} = ({d} * {m}) & M64",
    "IMULH_M": "{d} = ({d} * {m}) >> 64",
    "ISMULH_M": "{d} = ((({d} ^ SIGN) - SIGN) * (({m} ^ SIGN) - SIGN) >> 64) & M64",
    "IXOR_M": "{d} = {d} ^ {m}",
}
_INT_BINARY = {
    "ISUB_R": "{d} = ({d} - {s}) & M64",
    "IMUL_R": "{d} = ({d} * {s}) & M64",
    "IXOR_R": "{d} = {d} ^ {s}",
    "IMULH_R": "{d} = ({d} * {s}) >> 64",
    "ISMULH_R": "{d} = ((({d} ^ SIGN) - SIGN) * (({s} ^ SIGN) - SIGN) >> 64) & M64",
    "IMUL_RCP": "{d} = ({d} * {s}) & M64",
}


def _pair(group, i):
    return "%s%dl" % (group, i), "%s%dh" % (group, i)


def _addr(reg, imm, mask, shift):
    """Scratchpad index expression ((reg + imm) & mask) >> shift (specs.md 5.1.4)."""
    if reg == SRC_ZERO:
        return str((imm & mask) >> shift)
    simm = imm - (1 << 64) if imm >> 63 else imm
    return "((r%d %s %d) & 0x%x) >> %d" % (reg, "-" if simm < 0 else "+", abs(simm), mask, shift)


def _float_op(op, dst_pair, operands):
    """Both halves of a packed double op under fprc, e.g. FADD: dst = dst + src."""
    lines = ["if fprc:"]
    lines += ["    %s = float(%s[fprc](%s, %s))" % (d, op, d, o) for d, o in zip(dst_pair, operands)]
    lines.append("else:")
    lines += ["    %s %s= %s" % (d, _FLOAT_OPS[op], o) for d, o in zip(dst_pair, operands)]
    return lines


def _statements(op, config, v2, jump):
    """Python statements for one decoded instruction; jump(target) renders a taken CBRANCH."""
    k, d = op.kind, "r%d" % op.dst
    s = "r%d" % op.src if isinstance(op.src, int) else str(op.imm)
    if k == "IADD_RS":
        shifted = s if op.shift == 0 else "(%s << %d)" % (s, op.shift)
        imm = " + %d" % op.imm if op.imm else ""
        return ["%s = (%s + %s%s) & M64" % (d, d, shifted, imm)]
    if k in _INT_MEMORY:
        return [_INT_MEMORY[k].format(d=d, m="spq[%s]" % _addr(op.src, op.imm, op.mask, 3))]
    if k in _INT_BINARY:
        return [_INT_BINARY[k].format(d=d, s=s)]
    if k == "INEG_R":
        return ["%s = -%s & M64" % (d, d)]
    if k in ("IROR_R", "IROL_R"):
        right, left = (">>", "<<") if k == "IROR_R" else ("<<", ">>")
        if isinstance(op.src, int):
            return ["_c = %s & 63" % s,
                    "%s = (%s %s _c | %s %s (64 - _c)) & M64" % (d, d, right, d, left)]
        c = op.imm & 63
        return ["%s = (%s %s %d | %s %s %d) & M64" % (d, d, right, c, d, left, 64 - c)]
    if k == "ISWAP_R":
        return ["%s, %s = %s, %s" % (d, s, s, d)]
    if k == "FSWAP_R":
        lo, hi = _pair("e" if op.dst >= 4 else "f", op.dst % 4)
        return ["%s, %s = %s, %s" % (lo, hi, hi, lo)]
    if k in ("FADD_R", "FSUB_R", "FMUL_R"):
        a = [repr(x) for x in config.a[2 * op.src:2 * op.src + 2]]
        return _float_op(k[1:4], _pair("e" if k == "FMUL_R" else "f", op.dst), a)
    if k in ("FADD_M", "FSUB_M"):
        return ["_i = %s" % _addr(op.src, op.imm, op.mask, 2)] + _float_op(
            k[1:4], _pair("f", op.dst), ["float(spi[_i])", "float(spi[_i + 1])"])
    if k == "FSCAL_R":
        return ["%s = fscal(%s)" % (x, x) for x in _pair("f", op.dst)]
    if k == "FDIV_M":
        lo, hi = config.emask
        return ["_i = %s" % _addr(op.src, op.imm, op.mask, 2)] + _float_op(
            "DIV", _pair("e", op.dst),
            ["convert_e(spi[_i], %d)" % lo, "convert_e(spi[_i + 1], %d)" % hi])
    if k == "FSQRT_R":
        pair = _pair("e", op.dst)
        return (["if fprc:"] + ["    %s = float(SQRT[fprc](%s))" % (x, x) for x in pair]
                + ["else:"] + ["    %s = sqrt(%s)" % (x, x) for x in pair])
    if k == "CBRANCH":
        return (["%s = (%s + %d) & M64" % (d, d, op.imm), "if not %s & 0x%x:" % (d, op.mask)]
                + ["    " + line for line in jump(op.target)])
    if k == "CFROUND":
        rot = "%s >> %d | %s << %d" % (s, op.imm, s, 64 - op.imm)
        if not v2:
            return ["fprc = (%s) & 3" % rot]
        return ["_t = %s" % rot, "if not _t & 60:", "    fprc = _t & 3"]
    if k == "ISTORE":
        return ["spq[%s] = %s" % (_addr(op.dst, op.imm, op.mask, 3), s)]
    return ["pass"]


def _counted(op, lines):
    """`lines` with dynamic counters around them (see the module docstring)."""
    if op.kind in _ROUNDED:
        return ["STATS[%r, fprc] += 1" % op.kind] + lines
    if op.kind == "CFROUND":
        return ["STATS['CFROUND'] += 1", "_old = fprc"] + lines + [
            "STATS['CFROUND changed'] += _old != fprc"]
    return ["STATS[%r] += 1" % op.kind] + lines


def _define(source, name, spq, spi, stats=None):
    env = {
        "M64": MASK64, "SIGN": 1 << 63, "spq": spq, "spi": spi, "sqrt": math.sqrt,
        "ADD": fp.ADD, "SUB": fp.SUB, "MUL": fp.MUL, "DIV": fp.DIV, "SQRT": fp.SQRT,
        "fscal": fp.fscal, "convert_e": fp.convert_e, "STATS": stats,
    }
    exec(compile(source, "<randomx %s>" % name, "exec"), env)
    return env.pop(name)  # no self-reference: the scratchpad views go with the function


class CompiledProgram:
    """A program body as a Python function: body(*STATE) -> new STATE tuple."""

    def __init__(self, program, spq, spi, v2=False, stats=None):
        ops = program.ops
        # Jump targets split the body into blocks. Every jump goes backwards, so
        # `if blk <= n:` guards let a jump to block n fall through all later blocks.
        starts = sorted({0} | {op.target + 1 for op in ops if op.kind == "CBRANCH"})
        label_of = {pc: n for n, pc in enumerate(starts)}

        def jump(target):
            taken = ["STATS['CBRANCH taken'] += 1"] if stats is not None else []
            return taken + ["blk = %d" % label_of[target + 1], "continue"]

        lines = ["def body(%s):" % ", ".join(STATE), "    blk = 0", "    while True:"]
        for pc, (instr, op) in enumerate(zip(program.instructions, ops)):
            if pc in label_of:
                lines.append("        if blk <= %d:" % label_of[pc])
            lines.append("            # %d: %s" % (pc, format_instruction(instr).strip()))
            code = _statements(op, program.config, v2, jump)
            lines += ["            " + st for st in (code if stats is None else _counted(op, code))]
        lines += ["        break", "    return (%s)" % ", ".join(STATE)]
        self.source = "\n".join(lines) + "\n"
        self.body = _define(self.source, "body", spq, spi, stats)


def compile_instruction(op, config, spq, spi, v2=False):
    """One decoded instruction as step(*STATE) -> STATE + (branch_taken,)."""
    lines = ["def step(%s):" % ", ".join(STATE), "    taken = False"]
    lines += ["    " + st for st in _statements(op, config, v2, lambda target: ["taken = True"])]
    lines.append("    return (%s, taken)" % ", ".join(STATE))
    return _define("\n".join(lines) + "\n", "step", spq, spi)


class VirtualMachine:
    """Runs programs against a scratchpad (bytearray) and a dataset item source.

    `item_source.item(i)` returns the eight 64-bit words of Dataset item i. The
    rounding mode `fprc` persists across the programs of one hash."""

    def __init__(self, scratchpad, item_source, v2=False, stats=None):
        self.scratchpad = scratchpad
        self.stats = stats
        self.spq = memoryview(scratchpad).cast("Q")
        self.spi = memoryview(scratchpad).cast("i")
        self.item_source = item_source
        self.v2 = v2
        self.fprc = 0

    def execute(self, program):
        """specs.md 4.6: run `program` for 2048 iterations; returns the 256-byte register file."""
        body = CompiledProgram(program, self.spq, self.spi, self.v2, self.stats).body
        cfg = program.config
        sp, item = self.scratchpad, self.item_source.item
        rr0, rr1, rr2, rr3 = cfg.read_regs
        emask = cfg.emask * 4  # alternates low/high halves like the registers
        ma, mx = cfg.ma, cfg.mx
        sp_addr0, sp_addr1 = mx, ma
        r = [0] * 8
        fprc = self.fprc
        for _ in range(PROGRAM_ITERATIONS):
            sp_mix = r[rr0] ^ r[rr1]
            sp_addr0 = (sp_addr0 ^ sp_mix) & L3_MASK64
            sp_addr1 = (sp_addr1 ^ sp_mix >> 32) & L3_MASK64
            r = [x ^ w for x, w in zip(r, _Q8.unpack_from(sp, sp_addr0))]
            ints = _I16.unpack_from(sp, sp_addr1)
            f = _D8.unpack(_D8.pack(*ints[:8]))
            e_bits = _Q8.unpack(_D8.pack(*ints[8:]))  # int32 -> double, then 4.3.2 masking
            e = _D8.unpack(_Q8.pack(*[(q & DYNAMIC_MANTISSA_MASK) | m for q, m in zip(e_bits, emask)]))

            state = body(*r, *f, *e, fprc)
            r, f, e, fprc = state[:8], state[8:16], state[16:24], state[24]

            # mp is mx in v1 and ma in v2 (specs.md 4.3); ma is read before mp changes.
            read = cfg.dataset_offset + (ma & CACHE_LINE_ALIGN_MASK)
            if self.v2:
                ma ^= (r[rr2] ^ r[rr3]) & 0xFFFFFFFF
            else:
                mx ^= (r[rr2] ^ r[rr3]) & 0xFFFFFFFF
            r = [x ^ w for x, w in zip(r, item(read // 64))]
            ma, mx = mx, ma
            _Q8.pack_into(sp, sp_addr1, *r)
            f_bits = self._mix(f, e)
            _Q8.pack_into(sp, sp_addr0, *f_bits)
            sp_addr0 = sp_addr1 = 0
        self.fprc = fprc
        return _REGFILE.pack(*r, *f_bits, *e, *cfg.a)

    def _mix(self, f, e):
        """specs.md 4.6.2 step 10 on raw bits: v1 XORs F with E, v2 runs AES rounds keyed by E."""
        if not self.v2:
            return [x ^ y for x, y in zip(_Q8.unpack(_D8.pack(*f)), _Q8.unpack(_D8.pack(*e)))]
        c0, c1, c2, c3 = [_I4.unpack(_D2.pack(f[2 * i], f[2 * i + 1])) for i in range(4)]
        for i in range(4):
            k = _I4.unpack(_D2.pack(e[2 * i], e[2 * i + 1]))
            c0 = enc_round(*c0, *k)
            c1 = dec_round(*c1, *k)
            c2 = enc_round(*c2, *k)
            c3 = dec_round(*c3, *k)
        return _Q8.unpack(_W16.pack(*c0, *c1, *c2, *c3))
