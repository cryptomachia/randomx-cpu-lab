"""SuperscalarHash (specs.md §6): program generation and execution.

generate() is a line-by-line port of generateSuperscalar (superscalar.cpp),
including the simulation of the reference CPU's decoder and its three
execution ports: every random draw, stall and thrown-away instruction has to
happen in the same order as in C for the programs to come out identical.
"""
import collections

from rxlab.spec.params import (MASK64, SUPERSCALAR_LATENCY, is_zero_or_power_of_2, reciprocal,
                               signext32)

NAMES = ("ISUB_R", "IXOR_R", "IADD_RS", "IMUL_R", "IROR_C", "IADD_C7", "IXOR_C7", "IADD_C8",
         "IXOR_C8", "IADD_C9", "IXOR_C9", "IMULH_R", "ISMULH_R", "IMUL_RCP")
(ISUB_R, IXOR_R, IADD_RS, IMUL_R, IROR_C, IADD_C7, IXOR_C7, IADD_C8, IXOR_C8, IADD_C9, IXOR_C9,
 IMULH_R, ISMULH_R, IMUL_RCP) = range(len(NAMES))
INVALID = -1
IADD_C = (IADD_C7, IADD_C8, IADD_C9)
IXOR_C = (IXOR_C7, IXOR_C8, IXOR_C9)
MULTIPLICATIONS = (IMUL_R, IMULH_R, ISMULH_R, IMUL_RCP)

MAX_SIZE = 3 * SUPERSCALAR_LATENCY + 2
CYCLE_MAP_SIZE = SUPERSCALAR_LATENCY + 4
LOOK_FORWARD_CYCLES = 4
MAX_THROWAWAY_COUNT = 256
REGISTER_NEEDS_DISPLACEMENT = 5  # r5 cannot be the destination of IADD_RS (x86 lea + r13)

Instruction = collections.namedtuple("Instruction", "opcode dst src mod imm32")
Program = collections.namedtuple(
    "Program",
    "instructions address_register cpu_latency asic_latency code_size macro_ops "
    "decode_cycles mul_count ipc cpu_latencies asic_latencies")
Program.__doc__ = """A SuperscalarHash program and the reference-CPU statistics of its generation.

IMUL_RCP keeps its divisor in imm32 (the C cache later swaps it for an index
into its reciprocal table)."""

# Execution ports; a micro-op is the set of ports it may issue to (Table 6.2.1).
P0, P1, P5 = 1, 2, 4
P01, P05, P015 = P0 | P1, P0 | P5, P0 | P1 | P5

MacroOp = collections.namedtuple("MacroOp", "name size latency uop1 uop2 dependent")
SUB_RR = MacroOp("sub r,r", 3, 1, P015, 0, False)
XOR_RR = MacroOp("xor r,r", 3, 1, P015, 0, False)
IMUL_R_1OP = MacroOp("imul r", 3, 4, P1, P5, False)
MUL_R = MacroOp("mul r", 3, 4, P1, P5, False)
MOV_RR = MacroOp("mov r,r", 3, 0, 0, 0, False)  # eliminated: no port, no latency
LEA_SIB = MacroOp("lea r,r+r*s", 4, 1, P01, 0, False)
IMUL_RR = MacroOp("imul r,r", 4, 3, P1, 0, False)
ROR_RI = MacroOp("ror r,i", 4, 1, P05, 0, False)
ADD_RI = MacroOp("add r,i", 7, 1, P015, 0, False)
XOR_RI = MacroOp("xor r,i", 7, 1, P015, 0, False)
MOV_RI64 = MacroOp("mov rax,i64", 10, 1, P015, 0, False)
IMUL_RR_DEP = IMUL_RR._replace(dependent=True)  # waits for the mov in IMUL_RCP

# Macro-ops of each instruction and which of them reads the source, picks the
# destination and writes the result (-1: no source register).
Info = collections.namedtuple("Info", "opcode ops result_op dst_op src_op")
INFO = {
    ISUB_R: Info(ISUB_R, (SUB_RR,), 0, 0, 0),
    IXOR_R: Info(IXOR_R, (XOR_RR,), 0, 0, 0),
    IADD_RS: Info(IADD_RS, (LEA_SIB,), 0, 0, 0),
    IMUL_R: Info(IMUL_R, (IMUL_RR,), 0, 0, 0),
    IROR_C: Info(IROR_C, (ROR_RI,), 0, 0, -1),
    IADD_C7: Info(IADD_C7, (ADD_RI,), 0, 0, -1),
    IXOR_C7: Info(IXOR_C7, (XOR_RI,), 0, 0, -1),
    IADD_C8: Info(IADD_C8, (ADD_RI,), 0, 0, -1),
    IXOR_C8: Info(IXOR_C8, (XOR_RI,), 0, 0, -1),
    IADD_C9: Info(IADD_C9, (ADD_RI,), 0, 0, -1),
    IXOR_C9: Info(IXOR_C9, (XOR_RI,), 0, 0, -1),
    IMULH_R: Info(IMULH_R, (MOV_RR, MUL_R, MOV_RR), 1, 0, 1),
    ISMULH_R: Info(ISMULH_R, (MOV_RR, IMUL_R_1OP, MOV_RR), 1, 0, 1),
    IMUL_RCP: Info(IMUL_RCP, (MOV_RI64, IMUL_RR_DEP), 1, 1, -1),
}
NOP = Info(INVALID, (), 0, 0, 0)

# Decoder configurations: how one 16-byte fetch splits into instruction slots.
Decoder = collections.namedtuple("Decoder", "index slots")
DECODERS = (Decoder(0, (4, 8, 4)), Decoder(1, (7, 3, 3, 3)), Decoder(2, (3, 7, 3, 3)),
            Decoder(3, (4, 9, 3)), Decoder(4, (4, 4, 4, 4)), Decoder(5, (3, 3, 10)))

SLOT_3 = (ISUB_R, IXOR_R)
SLOT_3_LAST = (ISUB_R, IXOR_R, IMULH_R, ISMULH_R)
SLOTS = {4: (IROR_C, IADD_RS), 7: (IXOR_C7, IADD_C7), 8: (IXOR_C8, IADD_C8),
         9: (IXOR_C9, IADD_C9)}


def _next_decoder(opcode, cycle, mul_count, gen):
    """DecoderBuffer::fetchNext (rules in specs.md §6.3.1)."""
    if opcode in (IMULH_R, ISMULH_R):
        return DECODERS[5]
    if mul_count < cycle + 1:
        return DECODERS[4]
    if opcode == IMUL_RCP:
        return DECODERS[0] if gen.get_byte() & 1 else DECODERS[3]
    return DECODERS[gen.get_byte() & 3]


def _opcode_for_slot(gen, size, decoder, is_last):
    """SuperscalarInstruction::createForSlot (Table 6.3.2)."""
    if size == 3:
        return SLOT_3_LAST[gen.get_byte() & 3] if is_last else SLOT_3[gen.get_byte() & 1]
    if size == 10:
        return IMUL_RCP
    if size == 4 and decoder == 4 and not is_last:
        return IMUL_R
    return SLOTS[size][gen.get_byte() & 1]


def _select_register(available, gen):
    if not available:
        return None
    return available[gen.get_uint32() % len(available) if len(available) > 1 else 0]


def _signed32(x):
    return x - (1 << 32) if x & 0x80000000 else x


class _Candidate:
    """The instruction being placed (SuperscalarInstruction in superscalar.cpp).

    `group`/`group_par` identify "the same operation" for the rule that a
    register must not get the same operation twice in a row; ISUB_R shares
    IADD_RS's group, and group_par is the source register or -1 for constants.
    """

    def __init__(self):
        # As the zero-initialised static SuperscalarInstruction::Null.
        self.info = NOP
        self.src = self.dst = -1
        self.mod = self.imm32 = self.group = self.group_par = 0
        self.can_reuse = self.group_par_is_source = False

    def create(self, opcode, gen):
        self.info = INFO[opcode]
        self.src = self.dst = -1
        self.can_reuse = self.group_par_is_source = False
        self.mod = self.imm32 = 0
        if opcode in (ISUB_R, IXOR_R, IADD_RS, IMUL_R):
            if opcode == IADD_RS:
                self.mod = gen.get_byte()
            self.group = IADD_RS if opcode == ISUB_R else opcode
            self.group_par_is_source = True
        elif opcode == IROR_C:
            while self.imm32 == 0:
                self.imm32 = gen.get_byte() & 63
            self.group, self.group_par = IROR_C, -1
        elif opcode in IADD_C or opcode in IXOR_C:
            self.imm32 = gen.get_uint32()
            self.group, self.group_par = (IADD_C7 if opcode in IADD_C else IXOR_C7), -1
        elif opcode in (IMULH_R, ISMULH_R):
            self.can_reuse = True
            self.group, self.group_par = opcode, _signed32(gen.get_uint32())
        elif opcode == IMUL_RCP:
            self.imm32 = gen.get_uint32()
            while is_zero_or_power_of_2(self.imm32):
                self.imm32 = gen.get_uint32()
            self.group, self.group_par = IMUL_RCP, -1

    def select_source(self, cycle, regs, gen):
        available = [i for i in range(8) if regs.latency[i] <= cycle]
        # With only two candidates and one of them r5, r5 must be IADD_RS's source.
        if (len(available) == 2 and self.info.opcode == IADD_RS
                and REGISTER_NEEDS_DISPLACEMENT in available):
            self.src = self.group_par = REGISTER_NEEDS_DISPLACEMENT
            return True
        reg = _select_register(available, gen)
        if reg is None:
            return False
        self.src = reg
        if self.group_par_is_source:
            self.group_par = reg
        return True

    def select_destination(self, cycle, allow_chained_mul, regs, gen):
        available = [
            i for i in range(8)
            if regs.latency[i] <= cycle
            and (self.can_reuse or i != self.src)
            and (allow_chained_mul or self.group != IMUL_R or regs.last_group[i] != IMUL_R)
            and (regs.last_group[i] != self.group or regs.last_par[i] != self.group_par)
            and (self.info.opcode != IADD_RS or i != REGISTER_NEEDS_DISPLACEMENT)
        ]
        reg = _select_register(available, gen)
        if reg is None:
            return False
        self.dst = reg
        return True

    def instruction(self):
        src = self.src if self.src >= 0 else self.dst
        return Instruction(self.info.opcode, self.dst, src, self.mod, self.imm32)


class _Registers:
    """Per-register state of the simulation: ready cycle and last operation."""

    def __init__(self):
        self.latency = [0] * 8
        self.last_group = [INVALID] * 8
        self.last_par = [-1] * 8


def _schedule_uop(uop, busy, cycle, commit):
    """First cycle >= `cycle` with a free port for `uop`, trying P5, P0, P1 in that order."""
    for cycle in range(cycle, CYCLE_MAP_SIZE):
        ports = busy[cycle]
        for port, mask in ((2, P5), (0, P0), (1, P1)):
            if uop & mask and not ports[port]:
                if commit:
                    ports[port] = uop
                return cycle
    return -1


def _schedule_mop(mop, busy, cycle, dep_cycle, commit):
    if mop.dependent:
        cycle = max(cycle, dep_cycle)
    if mop.uop1 == 0:
        return cycle
    if mop.uop2 == 0:
        return _schedule_uop(mop.uop1, busy, cycle, commit)
    # Both micro-ops of a fused pair must issue in the same cycle.
    for cycle in range(cycle, CYCLE_MAP_SIZE):
        cycle1 = _schedule_uop(mop.uop1, busy, cycle, False)
        cycle2 = _schedule_uop(mop.uop2, busy, cycle, False)
        if cycle1 >= 0 and cycle1 == cycle2:
            if commit:
                _schedule_uop(mop.uop1, busy, cycle1, True)
                _schedule_uop(mop.uop2, busy, cycle2, True)
            return cycle1
    return -1


def generate(gen):
    """Generate one SuperscalarHash program from a BlakeGenerator (generateSuperscalar)."""
    busy = [[0, 0, 0] for _ in range(CYCLE_MAP_SIZE)]
    regs = _Registers()
    cur = _Candidate()
    program = []
    mop_index = code_size = macro_ops = cycle = dep_cycle = retire_cycle = mul_count = 0
    throw_away = 0
    saturated = False
    decode_cycle = 0
    # Decode 16 bytes per cycle until an execution port is saturated.
    while decode_cycle < SUPERSCALAR_LATENCY and not saturated and len(program) < MAX_SIZE:
        decoder = _next_decoder(cur.info.opcode, decode_cycle, mul_count, gen)
        slot = 0
        while slot < len(decoder.slots):
            top_cycle = cycle
            if mop_index >= len(cur.info.ops):
                if saturated or len(program) >= MAX_SIZE:
                    break
                is_last = slot + 1 == len(decoder.slots)
                opcode = _opcode_for_slot(gen, decoder.slots[slot], decoder.index, is_last)
                cur.create(opcode, gen)
                mop_index = 0
            mop = cur.info.ops[mop_index]
            schedule_cycle = _schedule_mop(mop, busy, cycle, dep_cycle, False)
            if schedule_cycle < 0:
                saturated = True
                break
            # Operands must be ready when the macro-op executes; stall up to
            # LOOK_FORWARD_CYCLES, then throw the instruction away. The stall
            # also moves `cycle`, which is not rewound for a thrown-away one.
            if mop_index == cur.info.src_op:
                forward = 0
                while forward < LOOK_FORWARD_CYCLES and not cur.select_source(
                        schedule_cycle, regs, gen):
                    schedule_cycle += 1
                    cycle += 1
                    forward += 1
                if forward == LOOK_FORWARD_CYCLES:
                    if throw_away < MAX_THROWAWAY_COUNT:
                        throw_away += 1
                        mop_index = len(cur.info.ops)
                        continue
                    cur = _Candidate()
                    break
            if mop_index == cur.info.dst_op:
                forward = 0
                while forward < LOOK_FORWARD_CYCLES and not cur.select_destination(
                        schedule_cycle, throw_away > 0, regs, gen):
                    schedule_cycle += 1
                    cycle += 1
                    forward += 1
                if forward == LOOK_FORWARD_CYCLES:
                    if throw_away < MAX_THROWAWAY_COUNT:
                        throw_away += 1
                        mop_index = len(cur.info.ops)
                        continue
                    cur = _Candidate()
                    break
            throw_away = 0
            schedule_cycle = _schedule_mop(mop, busy, schedule_cycle, schedule_cycle, True)
            if schedule_cycle < 0:
                saturated = True
                break
            dep_cycle = schedule_cycle + mop.latency
            if mop_index == cur.info.result_op:
                retire_cycle = dep_cycle
                regs.latency[cur.dst] = retire_cycle
                regs.last_group[cur.dst] = cur.group
                regs.last_par[cur.dst] = cur.group_par
            code_size += mop.size
            slot += 1
            mop_index += 1
            macro_ops += 1
            if schedule_cycle >= SUPERSCALAR_LATENCY:
                saturated = True
            cycle = top_cycle
            if mop_index >= len(cur.info.ops):
                program.append(cur.instruction())
                mul_count += cur.info.opcode in MULTIPLICATIONS
        cycle += 1
        decode_cycle += 1

    # ASIC model: 1-cycle ops, unlimited parallelism. The register at the end of
    # the longest dependency chain addresses the next cache line.
    asic = [0] * 8
    for ins in program:
        from_src = asic[ins.src] + 1 if ins.src != ins.dst else 0
        asic[ins.dst] = max(asic[ins.dst] + 1, from_src)
    address_register = max(range(8), key=lambda i: (asic[i], -i))
    return Program(tuple(program), address_register, retire_cycle, asic[address_register],
                   code_size, macro_ops, decode_cycle, mul_count, macro_ops / retire_cycle,
                   tuple(regs.latency), tuple(asic))


def _signed64(x):
    return x - (1 << 64) if x >> 63 else x


def execute(program, r):
    """Run `program` on the registers `r` (a list of 8 ints) in place; returns `r`."""
    for op, dst, src, mod, imm in program.instructions:
        if op == ISUB_R:
            r[dst] = (r[dst] - r[src]) & MASK64
        elif op == IXOR_R:
            r[dst] ^= r[src]
        elif op == IADD_RS:
            r[dst] = (r[dst] + (r[src] << ((mod >> 2) & 3))) & MASK64
        elif op == IMUL_R:
            r[dst] = (r[dst] * r[src]) & MASK64
        elif op == IROR_C:
            r[dst] = ((r[dst] >> imm) | (r[dst] << (64 - imm))) & MASK64
        elif op in IADD_C:
            r[dst] = (r[dst] + signext32(imm)) & MASK64
        elif op in IXOR_C:
            r[dst] ^= signext32(imm)
        elif op == IMULH_R:
            r[dst] = (r[dst] * r[src]) >> 64
        elif op == ISMULH_R:
            r[dst] = ((_signed64(r[dst]) * _signed64(r[src])) >> 64) & MASK64
        elif op == IMUL_RCP:
            r[dst] = (r[dst] * reciprocal(imm)) & MASK64
        else:
            raise ValueError(f"not a SuperscalarHash opcode: {op}")
    return r


def to_python(program):
    """`program` as straight-line Python over locals r0..r7, given M = 2**64 - 1, S = 2**63.

    One statement per instruction, mirroring execute(); used to build a fast
    dataset-item function.
    """
    lines = []
    for op, dst, src, mod, imm in program.instructions:
        d, s = f"r{dst}", f"r{src}"
        if op == ISUB_R:
            lines.append(f"{d} = ({d} - {s}) & M")
        elif op == IXOR_R:
            lines.append(f"{d} ^= {s}")
        elif op == IADD_RS:
            lines.append(f"{d} = ({d} + ({s} << {(mod >> 2) & 3})) & M")
        elif op == IMUL_R:
            lines.append(f"{d} = ({d} * {s}) & M")
        elif op == IROR_C:
            lines.append(f"{d} = (({d} >> {imm}) | ({d} << {64 - imm})) & M")
        elif op in IADD_C:
            lines.append(f"{d} = ({d} + {signext32(imm)}) & M")
        elif op in IXOR_C:
            lines.append(f"{d} ^= {signext32(imm)}")
        elif op == IMULH_R:
            lines.append(f"{d} = ({d} * {s}) >> 64")
        elif op == ISMULH_R:  # (x ^ 2**63) - 2**63 reinterprets x as signed
            lines.append(f"{d} = ((({d} ^ S) - S) * (({s} ^ S) - S) >> 64) & M")
        elif op == IMUL_RCP:
            lines.append(f"{d} = ({d} * {reciprocal(imm)}) & M")
        else:
            raise ValueError(f"not a SuperscalarHash opcode: {op}")
    return lines
