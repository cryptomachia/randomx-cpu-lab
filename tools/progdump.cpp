/*
 * progdump: the structure of RandomX programs, as the reference library makes them.
 *
 * For every input it replays randomx_calculate_hash() step by step with the
 * library's own code: Blake2b seed, AES scratchpad fill, program generation
 * (VmBase::generateProgram), VM configuration (randomx_vm::initialize), the
 * bytecode compiler and executeInstruction(). Only the interpreter's loop is
 * copied here, to count what actually executes. The final hash must equal the
 * library's hash for the same input; tests/test_programs.py checks that against
 * the official vectors.
 *
 * Output is CSV, one row per program (8 per input):
 *   static features  instruction counts by opcode type, effective NOPs, memory
 *                    operands by scratchpad level, CBRANCH jump-back body sizes,
 *                    register-dependency critical path of one iteration (all
 *                    registers, and integer registers only)
 *   dynamic counters instruction slots executed, CBRANCH executions and jumps
 *                    taken, CFROUND executions / fprc writes / mode changes,
 *                    dataset reads (all over 2048 iterations)
 *
 *   progdump [--v2] [--light] [--threads N] [--key STR] [--latency L,...]
 *            [--raw-limit M] (--input HEX ... | --blob HEX --start S --count K)
 *   progdump --bench-prefix [--v2] [--latency L,...] --blob HEX --count K
 *
 * --bench-prefix times what a miner must do per nonce before hashing to know
 * program 0 (Blake2b + scratchpad fill + generation) and then the feature pass.
 */
#include <algorithm>
#include <atomic>
#include <cfenv>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "randomx.h"
#include "common.hpp"
#include "dataset.hpp"
#include "virtual_machine.hpp"
#include "bytecode_machine.hpp"
#include "aes_hash.hpp"
#include "allocator.hpp"
#include "soft_aes.h"
#include "blake2/blake2.h"
#include "intrin_portable.h"

using namespace randomx;

namespace {

constexpr int kTypes = 30;
const char* const kTypeNames[kTypes] = {
	"IADD_RS", "IADD_M", "ISUB_R", "ISUB_M", "IMUL_R", "IMUL_M", "IMULH_R", "IMULH_M",
	"ISMULH_R", "ISMULH_M", "IMUL_RCP", "INEG_R", "IXOR_R", "IXOR_M", "IROR_R", "IROL_R",
	"ISWAP_R", "FSWAP_R", "FADD_R", "FADD_M", "FSUB_R", "FSUB_M", "FSCAL_R", "FMUL_R",
	"FDIV_M", "FSQRT_R", "CBRANCH", "CFROUND", "ISTORE", "NOP",
};
const int kCeil[kTypes] = {
	ceil_IADD_RS, ceil_IADD_M, ceil_ISUB_R, ceil_ISUB_M, ceil_IMUL_R, ceil_IMUL_M,
	ceil_IMULH_R, ceil_IMULH_M, ceil_ISMULH_R, ceil_ISMULH_M, ceil_IMUL_RCP, ceil_INEG_R,
	ceil_IXOR_R, ceil_IXOR_M, ceil_IROR_R, ceil_IROL_R, ceil_ISWAP_R, ceil_FSWAP_R,
	ceil_FADD_R, ceil_FADD_M, ceil_FSUB_R, ceil_FSUB_M, ceil_FSCAL_R, ceil_FMUL_R,
	ceil_FDIV_M, ceil_FSQRT_R, ceil_CBRANCH, ceil_CFROUND, ceil_ISTORE, ceil_NOP,
};

struct OpcodeTable {
	InstructionType type[256];
	OpcodeTable() {
		for (int op = 0, t = 0; op < 256; ++op) {
			while (op >= kCeil[t])
				++t;
			type[op] = static_cast<InstructionType>(t);
		}
	}
};
const OpcodeTable kOpcodes;

InstructionType opcodeType(uint8_t opcode) {
	return kOpcodes.type[opcode];
}

// Latencies in cycles; the order matches rxlab.analysis.programs.LATENCY_KEYS.
struct Latency {
	uint32_t alu, mul, mulh, agu, load[3], fadd, fmul, fdiv, fsqrt, fswap, fxor, fcvt, emask;
};
constexpr int kLatencyFields = 15;
// Dependency depth: every instruction counts 1, address and conversion steps 0.
const Latency kUnitLatency = {1, 1, 1, 0, {0, 0, 0}, 1, 1, 1, 1, 1, 1, 0, 0};

int memLevel(uint32_t mask) {
	if (mask == (uint32_t)ScratchpadL1Mask) return 0;
	if (mask == (uint32_t)ScratchpadL2Mask) return 1;
	return 2;
}

struct Features {
	uint32_t type[kTypes];
	uint32_t nopEff, rd[3], rdFp, wr[3], immSrc, cbrBody, cbrBodyMax, cpUnit, cpModel, cpInt;
};

struct Counters {
	uint64_t executed, taken, cbranchExec, cfroundExec, fprcWrites, fprcChanges, datasetReads;
};

// Longest register-dependency chain through one iteration, all registers ready at 0.
// ready[] ends as r0-r7, f0-f3, e0-e3; memory dependencies are not modelled.
void criticalPath(Program& prog, const InstructionByteCode* bc, uint32_t n, const Latency& L, uint32_t (&ready)[16]) {
	std::fill(ready, ready + 16, 0u);
	for (uint32_t i = 0; i < n; ++i) {
		const Instruction& in = prog(i);
		const InstructionByteCode& b = bc[i];
		const int dst = in.dst % RegistersCount, src = in.src % RegistersCount;
		const int fdst = 8 + in.dst % RegisterCountFlt, edst = 12 + in.dst % RegisterCountFlt;
		auto binop = [&](uint32_t lat) { ready[dst] = std::max(ready[dst], ready[src]) + lat; };
		auto regOrImm = [&](uint32_t lat) {
			if (src != dst) binop(lat); else ready[dst] += lat;
		};
		auto loadReady = [&]() {
			uint32_t addr = src != dst ? ready[src] + L.agu : 0;
			return addr + L.load[memLevel(b.memMask)];
		};
		auto memop = [&](uint32_t lat) { ready[dst] = std::max(ready[dst], loadReady()) + lat; };
		auto fpLoadReady = [&]() { return ready[src] + L.agu + L.load[memLevel(b.memMask)] + L.fcvt; };
		switch (opcodeType(in.opcode)) {
		case InstructionType::IADD_RS: binop(L.alu); break;
		case InstructionType::IADD_M:
		case InstructionType::ISUB_M:
		case InstructionType::IXOR_M: memop(L.alu); break;
		case InstructionType::IMUL_M: memop(L.mul); break;
		case InstructionType::IMULH_M:
		case InstructionType::ISMULH_M: memop(L.mulh); break;
		case InstructionType::ISUB_R:
		case InstructionType::IXOR_R:
		case InstructionType::IROR_R:
		case InstructionType::IROL_R: regOrImm(L.alu); break;
		case InstructionType::IMUL_R: regOrImm(L.mul); break;
		case InstructionType::IMULH_R:
		case InstructionType::ISMULH_R: binop(L.mulh); break;
		case InstructionType::IMUL_RCP:
			if (b.type != InstructionType::NOP) ready[dst] += L.mul;
			break;
		case InstructionType::INEG_R:
		case InstructionType::CBRANCH: ready[dst] += L.alu; break;
		case InstructionType::ISWAP_R:
			if (src != dst) std::swap(ready[dst], ready[src]);
			break;
		case InstructionType::FSWAP_R: ready[8 + dst] += L.fswap; break;
		case InstructionType::FADD_R:
		case InstructionType::FSUB_R: ready[fdst] += L.fadd; break;
		case InstructionType::FADD_M:
		case InstructionType::FSUB_M: ready[fdst] = std::max(ready[fdst], fpLoadReady()) + L.fadd; break;
		case InstructionType::FSCAL_R: ready[fdst] += L.fxor; break;
		case InstructionType::FMUL_R: ready[edst] += L.fmul; break;
		case InstructionType::FDIV_M:
			ready[edst] = std::max(ready[edst], fpLoadReady() + L.emask) + L.fdiv;
			break;
		case InstructionType::FSQRT_R: ready[edst] += L.fsqrt; break;
		default: break;  // CFROUND, ISTORE and NOP write no register
		}
	}
}

uint64_t fnv1a(uint64_t h, const void* data, size_t size) {
	const uint8_t* p = static_cast<const uint8_t*>(data);
	for (size_t i = 0; i < size; ++i)
		h = (h ^ p[i]) * 0x100000001b3ULL;
	return h;
}

void programBytes(Program& prog, uint32_t n, std::vector<uint8_t>& out) {
	out.resize(128 + 8 * n);
	for (int i = 0; i < 16; ++i)
		store64(&out[8 * i], prog.getEntropy(i));
	for (uint32_t i = 0; i < n; ++i)
		memcpy(&out[128 + 8 * i], &prog(i), 8);
}

class ProbeVm : public VmBase<AlignedAllocator<CacheLineSize>, false>, public BytecodeMachine {
public:
	explicit ProbeVm(randomx_flags flags) : VmBase(flags) {}
	void setCache(randomx_cache* cache) override {
		cachePtr = cache;
		mem.memory = cache->memory;
		light = true;
	}
	void setDataset(randomx_dataset* dataset) override {
		datasetPtr = dataset;
		mem.memory = dataset->memory;
		light = false;
	}
	void run(void* seed) override {
		prepare(seed);
		execute();
	}
	void prepare(void* seed) {
		generateProgram(seed);
		initialize();
	}
	uint32_t size() const { return Program::getSize(vmFlags); }
	Features features(const Latency& lat) {
		NativeRegisterFile scratch;
		compileProgram(program, bytecode, scratch, vmFlags);
		Features f = {};
		const uint32_t n = size();
		for (uint32_t i = 0; i < n; ++i) {
			const Instruction& in = program(i);
			const InstructionByteCode& b = bytecode[i];
			const InstructionType t = opcodeType(in.opcode);
			f.type[static_cast<int>(t)]++;
			if (b.type == InstructionType::NOP) f.nopEff++;
			switch (t) {
			case InstructionType::IADD_M: case InstructionType::ISUB_M: case InstructionType::IMUL_M:
			case InstructionType::IMULH_M: case InstructionType::ISMULH_M: case InstructionType::IXOR_M:
				f.rd[memLevel(b.memMask)]++;
				break;
			case InstructionType::FADD_M: case InstructionType::FSUB_M: case InstructionType::FDIV_M:
				f.rd[memLevel(b.memMask)]++;
				f.rdFp++;
				break;
			case InstructionType::ISTORE:
				f.wr[memLevel(b.memMask)]++;
				break;
			case InstructionType::ISUB_R: case InstructionType::IMUL_R: case InstructionType::IXOR_R:
			case InstructionType::IROR_R: case InstructionType::IROL_R:
				if (in.src % RegistersCount == in.dst % RegistersCount) f.immSrc++;
				break;
			case InstructionType::CBRANCH: {
				const uint32_t body = i - b.target;  // re-executes target+1 .. i
				f.cbrBody += body;
				f.cbrBodyMax = std::max(f.cbrBodyMax, body);
				break;
			}
			default: break;
			}
		}
		uint32_t ready[16];
		criticalPath(program, bytecode, n, kUnitLatency, ready);
		f.cpUnit = *std::max_element(ready, ready + 16);
		criticalPath(program, bytecode, n, lat, ready);
		f.cpModel = *std::max_element(ready, ready + 16);
		f.cpInt = *std::max_element(ready, ready + RegistersCount);
		return f;
	}
	void rawProgram(std::vector<uint8_t>& out) { programBytes(program, size(), out); }

	Counters counters;
	uint32_t fprc = 0;

private:
	void execute();
	void datasetRead(uint64_t address, int_reg_t (&r)[RegistersCount]) {
		counters.datasetReads++;
		if (light) {
			int_reg_t item[RegistersCount];
			initDatasetItem(cachePtr, (uint8_t*)item, address / CacheLineSize);
			for (int i = 0; i < RegistersCount; ++i)
				r[i] ^= item[i];
		}
		else {
			const uint64_t* line = (const uint64_t*)(mem.memory + address);
			for (int i = 0; i < RegistersCount; ++i)
				r[i] ^= line[i];
		}
	}

	bool light = false;
	InstructionByteCode bytecode[RANDOMX_PROGRAM_MAX_SIZE];
};

// InterpretedVm::execute() (vm_interpreted.cpp) with counters in the instruction loop.
void ProbeVm::execute() {
	NativeRegisterFile nreg;
	for (unsigned i = 0; i < RegisterCountFlt; ++i)
		nreg.a[i] = rx_load_vec_f128(&reg.a[i].lo);
	compileProgram(program, bytecode, nreg, vmFlags);
	const bool v2 = vmFlags & RANDOMX_FLAG_V2;
	const int n = size();
	uint32_t spAddr0 = mem.mx;
	uint32_t spAddr1 = mem.ma;

	for (unsigned ic = 0; ic < RANDOMX_PROGRAM_ITERATIONS; ++ic) {
		uint64_t spMix = nreg.r[config.readReg0] ^ nreg.r[config.readReg1];
		spAddr0 ^= spMix;
		spAddr0 &= ScratchpadL3Mask64;
		spAddr1 ^= spMix >> 32;
		spAddr1 &= ScratchpadL3Mask64;
		for (unsigned i = 0; i < RegistersCount; ++i)
			nreg.r[i] ^= load64(scratchpad + spAddr0 + 8 * i);
		for (unsigned i = 0; i < RegisterCountFlt; ++i)
			nreg.f[i] = rx_cvt_packed_int_vec_f128(scratchpad + spAddr1 + 8 * i);
		for (unsigned i = 0; i < RegisterCountFlt; ++i)
			nreg.e[i] = maskRegisterExponentMantissa(config, rx_cvt_packed_int_vec_f128(scratchpad + spAddr1 + 8 * (RegisterCountFlt + i)));

		for (int pc = 0; pc < n; ++pc) {
			InstructionByteCode& ibc = bytecode[pc];
			counters.executed++;
			if (ibc.type == InstructionType::CBRANCH) {
				counters.cbranchExec++;
				const int from = pc;
				executeInstruction(ibc, pc, scratchpad, config, vmFlags);
				if (pc != from) counters.taken++;
				continue;
			}
			if (ibc.type == InstructionType::CFROUND) {
				counters.cfroundExec++;
				const uint64_t v = (*ibc.isrc >> ibc.imm) | (*ibc.isrc << ((64 - ibc.imm) & 63));
				if (!v2 || (v & 60) == 0) {
					counters.fprcWrites++;
					if ((v & 3) != fprc) {
						counters.fprcChanges++;
						fprc = v & 3;
					}
				}
			}
			executeInstruction(ibc, pc, scratchpad, config, vmFlags);
		}

		const uint64_t readPtr = datasetOffset + (mem.ma & CacheLineAlignMask);
		auto& mp = v2 ? mem.ma : mem.mx;
		mp ^= nreg.r[config.readReg2] ^ nreg.r[config.readReg3];
		if (!light)
			rx_prefetch_nta(mem.memory + datasetOffset + (mp & CacheLineAlignMask));
		datasetRead(readPtr, nreg.r);
		std::swap(mem.mx, mem.ma);

		for (unsigned i = 0; i < RegistersCount; ++i)
			store64(scratchpad + spAddr1 + 8 * i, nreg.r[i]);

		if (v2) {
			rx_vec_i128 ekey[RegisterCountFlt], freg[RegisterCountFlt];
			for (unsigned i = 0; i < RegisterCountFlt; ++i) {
				ekey[i] = rx_cast_vec_f2i(nreg.e[i]);
				freg[i] = rx_cast_vec_f2i(nreg.f[i]);
			}
			for (unsigned i = 0; i < RegisterCountFlt; ++i) {
				freg[0] = aesenc<false>(freg[0], ekey[i]);
				freg[1] = aesdec<false>(freg[1], ekey[i]);
				freg[2] = aesenc<false>(freg[2], ekey[i]);
				freg[3] = aesdec<false>(freg[3], ekey[i]);
			}
			for (unsigned i = 0; i < RegisterCountFlt; ++i)
				nreg.f[i] = rx_cast_vec_i2f(freg[i]);
		}
		else {
			for (unsigned i = 0; i < RegisterCountFlt; ++i)
				nreg.f[i] = rx_xor_vec_f128(nreg.f[i], nreg.e[i]);
		}
		for (unsigned i = 0; i < RegisterCountFlt; ++i)
			rx_store_vec_f128((double*)(scratchpad + spAddr0 + 16 * i), nreg.f[i]);
		spAddr0 = 0;
		spAddr1 = 0;
	}

	for (unsigned i = 0; i < RegistersCount; ++i)
		store64(&reg.r[i], nreg.r[i]);
	for (unsigned i = 0; i < RegisterCountFlt; ++i)
		rx_store_vec_f128(&reg.f[i].lo, nreg.f[i]);
	for (unsigned i = 0; i < RegisterCountFlt; ++i)
		rx_store_vec_f128(&reg.e[i].lo, nreg.e[i]);
}

std::string hex(const uint8_t* p, size_t n) {
	static const char* digits = "0123456789abcdef";
	std::string s(2 * n, '0');
	for (size_t i = 0; i < n; ++i) {
		s[2 * i] = digits[p[i] >> 4];
		s[2 * i + 1] = digits[p[i] & 15];
	}
	return s;
}

bool unhex(const char* s, std::vector<uint8_t>& out) {
	const size_t n = strlen(s);
	if (n % 2) return false;
	out.resize(n / 2);
	for (size_t i = 0; i < n / 2; ++i) {
		unsigned v;
		if (sscanf(s + 2 * i, "%2x", &v) != 1) return false;
		out[i] = (uint8_t)v;
	}
	return true;
}

std::string header(bool raw) {
	std::string h = "input,prog,size,fingerprint";
	for (int t = 0; t < kTypes; ++t)
		h += std::string(",n_") + kTypeNames[t];
	h += ",nop_eff,rd_l1,rd_l2,rd_l3,rd_fp,wr_l1,wr_l2,wr_l3,imm_src,cbr_body,cbr_body_max,cp_unit,cp_model,cp_int"
	     ",executed,taken,cbr_exec,cfround_exec,fprc_writes,fprc_changes,ds_reads,hash";
	if (raw) h += ",raw";
	return h + "\n";
}

std::string row(uint32_t input, int prog, uint32_t size, uint64_t fingerprint, const Features& f, const Counters& c) {
	char buf[512];
	std::string s;
	snprintf(buf, sizeof(buf), "%u,%d,%u,%016llx", input, prog, size, (unsigned long long)fingerprint);
	s += buf;
	for (int t = 0; t < kTypes; ++t) {
		snprintf(buf, sizeof(buf), ",%u", f.type[t]);
		s += buf;
	}
	snprintf(buf, sizeof(buf), ",%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%llu,%llu,%llu,%llu,%llu,%llu,%llu",
		f.nopEff, f.rd[0], f.rd[1], f.rd[2], f.rdFp, f.wr[0], f.wr[1], f.wr[2], f.immSrc,
		f.cbrBody, f.cbrBodyMax, f.cpUnit, f.cpModel, f.cpInt,
		(unsigned long long)c.executed, (unsigned long long)c.taken, (unsigned long long)c.cbranchExec,
		(unsigned long long)c.cfroundExec, (unsigned long long)c.fprcWrites,
		(unsigned long long)c.fprcChanges, (unsigned long long)c.datasetReads);
	return s + buf;
}

// randomx_calculate_hash() (randomx.cpp), recording every program on the way.
std::string hashAndDump(ProbeVm& vm, const std::vector<uint8_t>& input, uint32_t index,
                        const Latency& lat, bool rawColumn, bool rawData) {
	fenv_t saved;
	fegetenv(&saved);
	alignas(16) uint64_t tempHash[8];
	blake2b(tempHash, sizeof(tempHash), input.data(), input.size(), nullptr, 0);
	vm.initScratchpad(tempHash);
	vm.resetRoundingMode();
	vm.fprc = 0;
	std::vector<std::string> rows, raws;
	std::vector<uint8_t> bytes;
	for (int chain = 0; chain < RANDOMX_PROGRAM_COUNT; ++chain) {
		vm.counters = Counters();
		vm.run(tempHash);
		vm.rawProgram(bytes);
		const uint64_t fingerprint = fnv1a(0xcbf29ce484222325ULL, bytes.data(), bytes.size());
		rows.push_back(row(index, chain, vm.size(), fingerprint, vm.features(lat), vm.counters));
		raws.push_back(rawData ? hex(bytes.data(), bytes.size()) : std::string());
		if (chain < RANDOMX_PROGRAM_COUNT - 1)
			blake2b(tempHash, sizeof(tempHash), vm.getRegisterFile(), sizeof(RegisterFile), nullptr, 0);
	}
	uint8_t out[RANDOMX_HASH_SIZE];
	vm.getFinalResult(out, sizeof(out));
	fesetenv(&saved);
	const std::string h = hex(out, sizeof(out));
	std::string all;
	for (size_t i = 0; i < rows.size(); ++i) {
		all += rows[i] + "," + h;
		if (rawColumn) all += "," + raws[i];
		all += "\n";
	}
	return all;
}

[[noreturn]] void usage(const char* msg) {
	fprintf(stderr, "progdump: %s\n(see the comment at the top of tools/progdump.cpp)\n", msg);
	exit(2);
}

struct Options {
	bool v2 = false, light = false, bench = false;
	int threads = 1;
	std::string key = "test key 000";
	Latency lat = kUnitLatency;
	std::vector<std::vector<uint8_t>> inputs;
	std::vector<uint8_t> blob;
	uint32_t start = 0, count = 0, nonceOffset = 39, rawLimit = 0;
};

Options parse(int argc, char** argv) {
	Options o;
	for (int i = 1; i < argc; ++i) {
		std::string a = argv[i];
		auto next = [&]() -> const char* {
			if (i + 1 >= argc) usage(("missing value for " + a).c_str());
			return argv[++i];
		};
		if (a == "--v2") o.v2 = true;
		else if (a == "--light") o.light = true;
		else if (a == "--bench-prefix") o.bench = true;
		else if (a == "--threads") o.threads = std::max(1, atoi(next()));
		else if (a == "--key") o.key = next();
		else if (a == "--start") o.start = strtoul(next(), nullptr, 10);
		else if (a == "--count") o.count = strtoul(next(), nullptr, 10);
		else if (a == "--nonce-offset") o.nonceOffset = strtoul(next(), nullptr, 10);
		else if (a == "--raw-limit") o.rawLimit = strtoul(next(), nullptr, 10);
		else if (a == "--blob") {
			if (!unhex(next(), o.blob)) usage("bad --blob hex");
		}
		else if (a == "--input") {
			std::vector<uint8_t> in;
			if (!unhex(next(), in)) usage("bad --input hex");
			o.inputs.push_back(in);
		}
		else if (a == "--latency") {
			uint32_t v[kLatencyFields];
			const char* p = next();
			for (int k = 0; k < kLatencyFields; ++k) {
				char* end;
				v[k] = strtoul(p, &end, 10);
				if (end == p || (k < kLatencyFields - 1 && *end != ',')) usage("--latency needs 15 integers");
				p = end + 1;
			}
			o.lat = {v[0], v[1], v[2], v[3], {v[4], v[5], v[6]}, v[7], v[8], v[9], v[10], v[11], v[12], v[13], v[14]};
		}
		else usage(("unknown argument " + a).c_str());
	}
	if (!o.blob.empty()) {
		if (o.blob.size() < o.nonceOffset + 4) usage("blob too short for the nonce offset");
		for (uint32_t k = 0; k < o.count; ++k) {
			std::vector<uint8_t> in = o.blob;
			store32(&in[o.nonceOffset], o.start + k);
			o.inputs.push_back(in);
		}
	}
	if (o.inputs.empty()) usage("no inputs");
	return o;
}

int bench(const Options& o, randomx_flags vmFlags) {
	ProbeVm vm(vmFlags);
	randomx_cache dummy;  // allocate() only checks that a memory source is set; nothing executes
	vm.setCache(&dummy);
	vm.allocate();
	const size_t n = o.inputs.size();
	alignas(16) uint64_t tempHash[8];
	uint64_t sink = 0;
	auto prefix = [&](size_t i) {
		blake2b(tempHash, sizeof(tempHash), o.inputs[i].data(), o.inputs[i].size(), nullptr, 0);
		vm.initScratchpad(tempHash);
		vm.prepare(tempHash);
	};
	using clock = std::chrono::steady_clock;
	for (size_t i = 0; i < std::min<size_t>(n, 64); ++i)
		prefix(i), sink += vm.features(o.lat).cpModel;
	auto t0 = clock::now();
	for (size_t i = 0; i < n; ++i)
		prefix(i), sink += tempHash[0];
	auto t1 = clock::now();
	for (size_t i = 0; i < n; ++i)
		prefix(i), sink += vm.features(o.lat).cpModel;
	auto t2 = clock::now();
	const double a = std::chrono::duration<double, std::nano>(t1 - t0).count() / n;
	const double b = std::chrono::duration<double, std::nano>(t2 - t1).count() / n;
	printf("{\"count\": %zu, \"ns_prefix\": %.1f, \"ns_prefix_and_features\": %.1f, \"sink\": %llu}\n",
		n, a, b, (unsigned long long)(sink & 1));
	return 0;
}

}  // namespace

int main(int argc, char** argv) {
	const Options o = parse(argc, argv);
	const randomx_flags available = randomx_get_flags();
	if (!(available & RANDOMX_FLAG_HARD_AES)) {
		fprintf(stderr, "progdump: needs hardware AES\n");
		return 1;
	}
	const randomx_flags vmFlags = (randomx_flags)(RANDOMX_FLAG_HARD_AES | (o.v2 ? RANDOMX_FLAG_V2 : 0));
	if (o.bench)
		return bench(o, vmFlags);

	const randomx_flags memFlags = (randomx_flags)(available & (RANDOMX_FLAG_JIT | RANDOMX_FLAG_SECURE | RANDOMX_FLAG_HARD_AES));
	randomx_cache* cache = randomx_alloc_cache(memFlags);
	if (!cache) {
		fprintf(stderr, "progdump: randomx_alloc_cache failed\n");
		return 1;
	}
	randomx_init_cache(cache, o.key.data(), o.key.size());
	randomx_dataset* dataset = nullptr;
	if (!o.light) {
		dataset = randomx_alloc_dataset(memFlags);
		if (!dataset) {
			fprintf(stderr, "progdump: randomx_alloc_dataset failed\n");
			return 1;
		}
		const unsigned long items = randomx_dataset_item_count();
		std::vector<std::thread> init;
		for (int t = 0; t < o.threads; ++t) {
			const unsigned long lo = items * t / o.threads, hi = items * (t + 1) / o.threads;
			init.emplace_back(randomx_init_dataset, dataset, cache, lo, hi - lo);
		}
		for (auto& t : init)
			t.join();
	}

	const size_t n = o.inputs.size();
	std::vector<std::string> out(n);
	std::atomic<size_t> next(0);
	auto worker = [&]() {
		std::unique_ptr<ProbeVm> vm(new ProbeVm(vmFlags));
		if (dataset) vm->setDataset(dataset); else vm->setCache(cache);
		vm->allocate();
		for (size_t i; (i = next++) < n;)
			out[i] = hashAndDump(*vm, o.inputs[i], o.start + (uint32_t)i, o.lat, o.rawLimit > 0, i < o.rawLimit);
	};
	std::vector<std::thread> pool;
	for (int t = 0; t < o.threads; ++t)
		pool.emplace_back(worker);
	for (auto& t : pool)
		t.join();

	fputs(header(o.rawLimit > 0).c_str(), stdout);
	for (const auto& s : out)
		fputs(s.c_str(), stdout);
	if (dataset) randomx_release_dataset(dataset);
	randomx_release_cache(cache);
	return 0;
}
