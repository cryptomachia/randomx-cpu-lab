"""RandomX program generation and decoding (specs.md 4.5 and 5.1-5.5).

A program is 128 bytes of configuration entropy followed by 8-byte instruction
words. `decode` turns the words into `Op` records whose fields match the C
interpreter's InstructionByteCode, so every operand rule of chapter 5 (register
vs. immediate source, scratchpad level, CBRANCH target, IMUL_RCP reciprocal)
is settled once, before execution.
"""
import struct
from typing import NamedTuple

from .aes import AesGenerator4R
from .fp import from_bits
from .params import (DATASET_BASE_SIZE, DATASET_EXTRA_SIZE, DATASET_ITEM_SIZE, FREQUENCIES,
                     JUMP_BITS, JUMP_OFFSET, MASK64, PROGRAM_SIZE_V1, PROGRAM_SIZE_V2,
                     SCRATCHPAD_L1, SCRATCHPAD_L2, SCRATCHPAD_L3, is_zero_or_power_of_2,
                     reciprocal, signext32)

OPCODES = [name for name, freq in FREQUENCIES for _ in range(freq)]
assert len(OPCODES) == 256

L1_MASK = (SCRATCHPAD_L1 - 1) & ~7
L2_MASK = (SCRATCHPAD_L2 - 1) & ~7
L3_MASK = (SCRATCHPAD_L3 - 1) & ~7
L3_MASK64 = (SCRATCHPAD_L3 - 1) & ~63
CACHE_LINE_ALIGN_MASK = (DATASET_BASE_SIZE - 1) & ~(DATASET_ITEM_SIZE - 1)
DATASET_EXTRA_ITEMS = DATASET_EXTRA_SIZE // DATASET_ITEM_SIZE
REGISTER_NEEDS_DISPLACEMENT = 5  # IADD_RS adds imm32 only when dst is r5
STORE_L3_CONDITION = 14
CONDITION_MASK = (1 << JUMP_BITS) - 1

# Op.src when the source is not a register (specs.md Table 5.2.1, "src == dst ?").
SRC_IMM = "imm"    # use Op.imm as the operand
SRC_ZERO = "zero"  # memory operand addressed by Op.imm alone (L3)

_INT_MEMORY = {"IADD_M", "ISUB_M", "IMUL_M", "IMULH_M", "ISMULH_M", "IXOR_M"}
_INT_REG_OR_IMM = {"ISUB_R", "IMUL_R", "IXOR_R"}


def program_size(v2):
    return PROGRAM_SIZE_V2 if v2 else PROGRAM_SIZE_V1


class Instruction(NamedTuple):
    """One raw 8-byte instruction word (specs.md 5.1)."""
    opcode: int
    dst: int
    src: int
    mod: int
    imm32: int

    @property
    def name(self):
        return OPCODES[self.opcode]

    @property
    def mod_mem(self):
        return self.mod & 3

    @property
    def mod_shift(self):
        return (self.mod >> 2) & 3

    @property
    def mod_cond(self):
        return self.mod >> 4

    @classmethod
    def unpack(cls, word):
        opcode, dst, src, mod, imm32 = struct.unpack("<BBBBI", word)
        return cls(opcode, dst, src, mod, imm32)


class Config(NamedTuple):
    """VM configuration derived from the 16 entropy quadwords (specs.md 4.5)."""
    a: tuple              # a0.lo, a0.hi, ..., a3.hi as floats
    ma: int
    mx: int
    read_regs: tuple      # readReg0..readReg3
    dataset_offset: int   # bytes
    emask: tuple          # group E masks for the low and high halves


class Op(NamedTuple):
    """A decoded instruction, field for field like the C InstructionByteCode.

    kind is the executed operation (a no-op encoding decodes to "NOP"); dst and
    src are register indices (FSWAP_R dst 0-7 means f0-f3, e0-e3); src may be
    SRC_IMM or SRC_ZERO; imm is an unsigned 64-bit immediate; mask is the
    scratchpad address mask or, for CBRANCH, the condition mask; target is the
    CBRANCH target (execution resumes at target + 1).
    """
    kind: str
    dst: int = 0
    src: object = 0
    imm: int = 0
    shift: int = 0
    mask: int = 0
    target: int = 0


class Program(NamedTuple):
    """A programmed VM: entropy quadwords, raw instructions, configuration, decoded ops."""
    entropy: tuple
    instructions: list
    config: Config
    ops: list

    def disassemble(self):
        return "".join(format_instruction(i) for i in self.instructions)


def small_positive_float_bits(entropy):
    """specs.md 4.5.2: +1.fraction x 2**exponent with a 52-bit fraction and exponent 0-31."""
    exponent = ((entropy >> 59) + 1023) & 0x7FF
    return exponent << 52 | (entropy & ((1 << 52) - 1))


def float_mask(entropy):
    """specs.md 4.5.6: fraction mask (bits 0-21) and exponent mask (bits 60-63) as double bits."""
    exponent = 0x300 | (entropy >> 60) << 4
    return (entropy & ((1 << 22) - 1)) | exponent << 52


def decode_config(entropy):
    a = tuple(from_bits(small_positive_float_bits(q)) for q in entropy[:8])
    select = entropy[12]
    return Config(
        a=a,
        ma=entropy[8] & CACHE_LINE_ALIGN_MASK,
        mx=entropy[10] & 0xFFFFFFFF,
        read_regs=tuple(2 * i + (select >> i & 1) for i in range(4)),
        dataset_offset=(entropy[13] % (DATASET_EXTRA_ITEMS + 1)) * DATASET_ITEM_SIZE,
        emask=(float_mask(entropy[14]), float_mask(entropy[15])),
    )


def _memory_mask(instr):
    return L1_MASK if instr.mod_mem else L2_MASK


def decode_instruction(instr, pc, last_write):
    """Decode one instruction at index pc; `last_write[r]` is the index of the last
    instruction that modified integer register r (-1 if none) and is updated."""
    kind = instr.name
    dst, src = instr.dst % 8, instr.src % 8
    simm = signext32(instr.imm32)

    if kind == "IADD_RS":
        op = Op(kind, dst, src, simm if dst == REGISTER_NEEDS_DISPLACEMENT else 0,
                shift=instr.mod_shift)
    elif kind in _INT_MEMORY:
        if src != dst:
            op = Op(kind, dst, src, simm, mask=_memory_mask(instr))
        else:
            op = Op(kind, dst, SRC_ZERO, simm, mask=L3_MASK)
    elif kind in _INT_REG_OR_IMM:
        op = Op(kind, dst, src) if src != dst else Op(kind, dst, SRC_IMM, simm)
    elif kind in ("IROR_R", "IROL_R"):
        op = Op(kind, dst, src) if src != dst else Op(kind, dst, SRC_IMM, instr.imm32)
    elif kind in ("IMULH_R", "ISMULH_R"):
        op = Op(kind, dst, src)
    elif kind == "IMUL_RCP":
        if is_zero_or_power_of_2(instr.imm32):
            return Op("NOP")
        op = Op(kind, dst, SRC_IMM, reciprocal(instr.imm32))
    elif kind == "INEG_R":
        op = Op(kind, dst)
    elif kind == "ISWAP_R":
        if src == dst:
            return Op("NOP")
        last_write[src] = pc
        op = Op(kind, dst, src)
    elif kind == "FSWAP_R":
        return Op(kind, dst)
    elif kind in ("FADD_R", "FSUB_R", "FMUL_R"):
        return Op(kind, dst % 4, src % 4)
    elif kind in ("FADD_M", "FSUB_M", "FDIV_M"):
        return Op(kind, dst % 4, src, simm, mask=_memory_mask(instr))
    elif kind in ("FSCAL_R", "FSQRT_R"):
        return Op(kind, dst % 4)
    elif kind == "CBRANCH":
        # specs.md 5.4.2: cimm is imm32 with bit b set and bit b-1 cleared, b = cond + offset.
        shift = instr.mod_cond + JUMP_OFFSET
        cimm = simm | 1 << shift
        if JUMP_OFFSET > 0 or shift > 0:
            cimm &= ~(1 << (shift - 1)) & MASK64
        op = Op(kind, dst, imm=cimm, mask=CONDITION_MASK << shift, target=last_write[dst])
        for r in range(8):
            last_write[r] = pc
        return op
    elif kind == "CFROUND":
        return Op(kind, src=src, imm=instr.imm32 & 63)
    elif kind == "ISTORE":
        mask = L3_MASK if instr.mod_cond >= STORE_L3_CONDITION else _memory_mask(instr)
        return Op(kind, dst, src, simm, mask=mask)
    else:
        return Op("NOP")
    # Integer instructions reach this point: they modify dst (specs.md 5.4.2).
    last_write[dst] = pc
    return op


def decode(instructions):
    last_write = [-1] * 8
    return [decode_instruction(instr, pc, last_write) for pc, instr in enumerate(instructions)]


def parse(data, size):
    """Split programming bytes into 16 entropy quadwords and `size` instructions."""
    if len(data) < 128 + 8 * size:
        raise ValueError("not enough program bytes")
    entropy = struct.unpack_from("<16Q", data)
    instructions = [Instruction.unpack(data[128 + 8 * i:136 + 8 * i]) for i in range(size)]
    return entropy, instructions


def from_bytes(data, v2=False):
    entropy, instructions = parse(data, program_size(v2))
    return Program(entropy, instructions, decode_config(entropy), decode(instructions))


def generate(seed, v2=False):
    """Program the VM from a 64-byte seed with AesGenerator4R (specs.md 2, step 7)."""
    size = program_size(v2)
    return from_bytes(AesGenerator4R(seed).generate(128 + 8 * size), v2)


def _i32(x):
    return x - (1 << 32) if x & 0x80000000 else x


def _signed(x):
    return "%+d" % _i32(x)


def format_instruction(instr):
    """One line of disassembly, byte-identical to the C library's Instruction::print."""
    name, d, s, imm = instr.name, instr.dst % 8, instr.src % 8, instr.imm32
    level = "L1" if instr.mod_mem else "L2"
    reg_addr = "%s[r%d%s]" % (level, s, _signed(imm))
    imm_addr = "L3[%d]" % (imm & L3_MASK)
    if name == "IADD_RS":
        disp = ", %d" % _i32(imm) if d == REGISTER_NEEDS_DISPLACEMENT else ""
        body = "r%d, r%d%s, SHFT %d" % (d, s, disp, instr.mod_shift)
    elif name in _INT_MEMORY:
        body = "r%d, %s" % (d, reg_addr if d != s else imm_addr)
    elif name in _INT_REG_OR_IMM:
        body = "r%d, r%d" % (d, s) if d != s else "r%d, %d" % (d, _i32(imm))
    elif name in ("IROR_R", "IROL_R"):
        body = "r%d, r%d" % (d, s) if d != s else "r%d, %d" % (d, imm & 63)
    elif name in ("IMULH_R", "ISMULH_R", "ISWAP_R"):
        body = "r%d, r%d" % (d, s)
    elif name == "IMUL_RCP":
        body = "r%d, %d" % (d, imm)
    elif name == "INEG_R":
        body = "r%d" % d
    elif name == "FSWAP_R":
        body = "%s%d" % ("e" if d >= 4 else "f", d % 4)
    elif name in ("FADD_R", "FSUB_R"):
        body = "f%d, a%d" % (d % 4, s % 4)
    elif name in ("FADD_M", "FSUB_M"):
        body = "f%d, %s" % (d % 4, reg_addr)
    elif name == "FSCAL_R":
        body = "f%d" % (d % 4)
    elif name == "FMUL_R":
        body = "e%d, a%d" % (d % 4, s % 4)
    elif name == "FDIV_M":
        body = "e%d, %s" % (d % 4, reg_addr)
    elif name == "FSQRT_R":
        body = "e%d" % (d % 4)
    elif name == "CFROUND":
        body = "r%d, %d" % (s, imm & 63)
    elif name == "CBRANCH":
        body = "r%d, %d, COND %d" % (d, _i32(imm), instr.mod_cond)
    elif name == "ISTORE":
        level = "L3" if instr.mod_cond >= STORE_L3_CONDITION else level
        body = "%s[r%d%s], r%d" % (level, d, _signed(imm), s)
    else:
        body = ""
    return "%s %s\n" % (name, body)
