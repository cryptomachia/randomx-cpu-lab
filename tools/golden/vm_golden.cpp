// Golden values for rxlab/spec (the pure-Python VM), dumped from the reference
// RandomX library. Built against the v2.0.1 sources, which compute both v1 and
// v2 hashes, by tools/golden/build_vm_golden.sh.
//
// usage: vm_golden OUTDIR
//   OUTDIR/vm_aes.json           AES T-tables, single rounds, AesGenerator1R/4R, AesHash1R
//   OUTDIR/vm_programs.json      generated programs: bytes, disassembly, bytecode, configuration
//   OUTDIR/vm_instructions.json  single instructions run by the C interpreter (all rounding modes)
//   OUTDIR/vm_hash_trace.json    per-program register files for the official test vectors
//
// Everything is deterministic: inputs come from a fixed splitmix64 stream or
// from Blake2b of fixed strings.

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>
#include <vector>

#include "aes_hash.hpp"
#include "blake2/blake2.h"
#include "bytecode_machine.hpp"
#include "common.hpp"
#include "intrin_portable.h"
#include "program.hpp"
#include "randomx.h"
#include "soft_aes.h"
#include "virtual_machine.hpp"

namespace {

// Reach protected members of randomx_vm through pointers to members, which the
// language allows when named through a derived class.
struct VmAccess : randomx_vm {
	static randomx::Program& programOf(randomx_vm* vm) { return vm->*(&VmAccess::program); }
	static randomx::ProgramConfiguration& configOf(randomx_vm* vm) { return vm->*(&VmAccess::config); }
	static randomx::MemoryRegisters& memOf(randomx_vm* vm) { return vm->*(&VmAccess::mem); }
	static uint64_t datasetOffsetOf(randomx_vm* vm) { return vm->*(&VmAccess::datasetOffset); }
	static void initializeVm(randomx_vm* vm) { (vm->*(&VmAccess::initialize))(); }
};

const char* const TYPE_NAMES[] = {
	"IADD_RS", "IADD_M", "ISUB_R", "ISUB_M", "IMUL_R", "IMUL_M", "IMULH_R", "IMULH_M",
	"ISMULH_R", "ISMULH_M", "IMUL_RCP", "INEG_R", "IXOR_R", "IXOR_M", "IROR_R", "IROL_R",
	"ISWAP_R", "FSWAP_R", "FADD_R", "FADD_M", "FSUB_R", "FSUB_M", "FSCAL_R", "FMUL_R",
	"FDIV_M", "FSQRT_R", "CBRANCH", "CFROUND", "ISTORE", "NOP",
};

// Opcode ranges per instruction, in the order of TYPE_NAMES.
const int CEILS[] = {
	randomx::ceil_IADD_RS, randomx::ceil_IADD_M, randomx::ceil_ISUB_R, randomx::ceil_ISUB_M,
	randomx::ceil_IMUL_R, randomx::ceil_IMUL_M, randomx::ceil_IMULH_R, randomx::ceil_IMULH_M,
	randomx::ceil_ISMULH_R, randomx::ceil_ISMULH_M, randomx::ceil_IMUL_RCP, randomx::ceil_INEG_R,
	randomx::ceil_IXOR_R, randomx::ceil_IXOR_M, randomx::ceil_IROR_R, randomx::ceil_IROL_R,
	randomx::ceil_ISWAP_R, randomx::ceil_FSWAP_R, randomx::ceil_FADD_R, randomx::ceil_FADD_M,
	randomx::ceil_FSUB_R, randomx::ceil_FSUB_M, randomx::ceil_FSCAL_R, randomx::ceil_FMUL_R,
	randomx::ceil_FDIV_M, randomx::ceil_FSQRT_R, randomx::ceil_CBRANCH, randomx::ceil_CFROUND,
	randomx::ceil_ISTORE, randomx::ceil_NOP,
};

struct SplitMix64 {
	uint64_t s;
	uint64_t next() {
		uint64_t z = (s += 0x9e3779b97f4a7c15ULL);
		z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
		z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
		return z ^ (z >> 31);
	}
};

std::string hex(const void* data, size_t size) {
	static const char digits[] = "0123456789abcdef";
	const uint8_t* p = (const uint8_t*)data;
	std::string out;
	for (size_t i = 0; i < size; ++i) {
		out += digits[p[i] >> 4];
		out += digits[p[i] & 15];
	}
	return out;
}

std::string quoted(const std::string& s) { return "\"" + s + "\""; }

std::string qhex(const void* data, size_t size) { return quoted(hex(data, size)); }

std::string blake256(const void* data, size_t size) {
	uint8_t out[32];
	randomx_blake2b(out, sizeof(out), data, size, nullptr, 0);
	return hex(out, sizeof(out));
}

void blake512(void* out, const std::string& text) {
	randomx_blake2b(out, 64, text.data(), text.size(), nullptr, 0);
}

template<typename T>
std::string joined(const std::vector<T>& items) {
	std::ostringstream os;
	for (size_t i = 0; i < items.size(); ++i)
		os << (i ? ", " : "") << items[i];
	return os.str();
}

void writeFile(const std::string& dir, const char* name, const std::string& body) {
	std::string path = dir + "/" + name;
	FILE* f = fopen(path.c_str(), "w");
	if (!f) {
		perror(path.c_str());
		exit(1);
	}
	fputs(body.c_str(), f);
	fputs("\n", f);
	fclose(f);
	printf("wrote %s (%zu bytes)\n", path.c_str(), body.size() + 1);
}

void require(bool ok, const char* what) {
	if (!ok) {
		fprintf(stderr, "check failed: %s\n", what);
		exit(1);
	}
}

// ---------------------------------------------------------------- AES

std::string dumpAes() {
	SplitMix64 rng{1};
	std::ostringstream os;
	os << "{\n";
	for (int t = 0; t < 2; ++t) {
		const uint32_t (*lut)[256] = t ? randomx_aes_lut_dec : randomx_aes_lut_enc;
		os << "  \"" << (t ? "lut_dec" : "lut_enc") << "\": [\n";
		for (int i = 0; i < 4; ++i) {
			std::vector<uint32_t> row(lut[i], lut[i] + 256);
			os << "    [" << joined(row) << "]" << (i < 3 ? ",\n" : "\n");
		}
		os << "  ],\n";
	}

	os << "  \"rounds\": [\n";
	for (int i = 0; i < 32; ++i) {
		alignas(16) uint64_t state[2] = {rng.next(), rng.next()}, key[2] = {rng.next(), rng.next()};
		alignas(16) uint8_t enc[16], dec[16];
		rx_vec_i128 s = rx_load_vec_i128((rx_vec_i128*)state), k = rx_load_vec_i128((rx_vec_i128*)key);
		rx_store_vec_i128((rx_vec_i128*)enc, soft_aesenc(s, k));
		rx_store_vec_i128((rx_vec_i128*)dec, soft_aesdec(s, k));
		os << "    {\"state\": " << qhex(state, 16) << ", \"key\": " << qhex(key, 16)
		   << ", \"enc\": " << qhex(enc, 16) << ", \"dec\": " << qhex(dec, 16) << "}"
		   << (i < 31 ? ",\n" : "\n");
	}
	os << "  ],\n";

	os << "  \"fill1r\": [\n";
	for (int i = 0; i < 3; ++i) {
		alignas(16) uint8_t seed[64], state[64], soft[4096], hard[4096], hardState[64];
		blake512(seed, "rxlab vm_golden fill1r " + std::to_string(i));
		memcpy(state, seed, 64);
		memcpy(hardState, seed, 64);
		fillAes1Rx4<true>(state, sizeof(soft), soft);
		fillAes1Rx4<false>(hardState, sizeof(hard), hard);
		require(memcmp(soft, hard, sizeof(soft)) == 0 && memcmp(state, hardState, 64) == 0,
			"soft and hard AesGenerator1R agree");
		os << "    {\"seed\": " << qhex(seed, 64) << ", \"output\": " << qhex(soft, sizeof(soft))
		   << ", \"state\": " << qhex(state, 64) << "}" << (i < 2 ? ",\n" : "\n");
	}
	os << "  ],\n";

	os << "  \"fill4r\": [\n";
	for (int i = 0; i < 3; ++i) {
		alignas(16) uint8_t seed[64], out[sizeof(randomx::Program)];
		blake512(seed, "rxlab vm_golden fill4r " + std::to_string(i));
		fillAes4Rx4<true>(seed, sizeof(out), out);
		os << "    {\"seed\": " << qhex(seed, 64) << ", \"output\": " << qhex(out, sizeof(out))
		   << "}" << (i < 2 ? ",\n" : "\n");
	}
	os << "  ],\n";

	os << "  \"hash1r\": [\n";
	const size_t sizes[] = {64, 128, 4096};
	for (int i = 0; i < 3; ++i) {
		std::vector<uint64_t> input(sizes[i] / 8);
		for (auto& w : input)
			w = rng.next();
		alignas(16) uint8_t soft[64], hard[64];
		hashAes1Rx4<true>(input.data(), sizes[i], soft);
		hashAes1Rx4<false>(input.data(), sizes[i], hard);
		require(memcmp(soft, hard, 64) == 0, "soft and hard AesHash1R agree");
		os << "    {\"input\": " << qhex(input.data(), sizes[i]) << ", \"hash\": " << qhex(soft, 64)
		   << "}" << (i < 2 ? ",\n" : "\n");
	}
	os << "  ],\n";

	// The full-size case: a 2 MiB scratchpad filled from a seed, then fingerprinted.
	alignas(16) uint8_t seed[64], state[64], fingerprint[64];
	blake512(seed, "rxlab vm_golden scratchpad");
	memcpy(state, seed, 64);
	std::vector<uint64_t> scratchpad(RANDOMX_SCRATCHPAD_L3 / 8);
	fillAes1Rx4<true>(state, RANDOMX_SCRATCHPAD_L3, scratchpad.data());
	hashAes1Rx4<true>(scratchpad.data(), RANDOMX_SCRATCHPAD_L3, fingerprint);
	os << "  \"scratchpad\": {\"seed\": " << qhex(seed, 64) << ", \"state\": " << qhex(state, 64)
	   << ", \"blake2b_256\": " << quoted(blake256(scratchpad.data(), RANDOMX_SCRATCHPAD_L3))
	   << ", \"hash1r\": " << qhex(fingerprint, 64) << "}\n}";
	return os.str();
}

// ---------------------------------------------------------------- programs

std::string operandName(const void* p, const randomx::NativeRegisterFile& n,
	const randomx::InstructionByteCode& ibc) {
	if (p == nullptr)
		return "-";
	for (int i = 0; i < 8; ++i)
		if (p == &n.r[i]) return "r" + std::to_string(i);
	for (int i = 0; i < 4; ++i) {
		if (p == &n.f[i]) return "f" + std::to_string(i);
		if (p == &n.e[i]) return "e" + std::to_string(i);
		if (p == &n.a[i]) return "a" + std::to_string(i);
	}
	if (p == &ibc.imm)
		return "imm";
	return "zero";  // BytecodeMachine::zero, a private static
}

std::string dumpPrograms(randomx_vm* vm) {
	std::ostringstream os;
	os << "{\n  \"programs\": [\n";
	const int count = 6;
	for (int i = 0; i < count; ++i) {
		alignas(16) uint8_t seed[64];
		blake512(seed, "rxlab vm_golden program " + std::to_string(i));
		randomx::Program& prog = VmAccess::programOf(vm);
		fillAes4Rx4<true>(seed, sizeof(prog), &prog);  // VmBase::generateProgram
		VmAccess::initializeVm(vm);

		std::ostringstream text;
		text << prog;
		std::vector<std::string> lines;
		std::string line;
		std::istringstream in(text.str());
		while (std::getline(in, line))
			lines.push_back(quoted(line));

		randomx::NativeRegisterFile nreg;
		randomx::BytecodeMachine decoder;
		std::vector<randomx::InstructionByteCode> bytecode(RANDOMX_PROGRAM_MAX_SIZE);
		memset(bytecode.data(), 0, bytecode.size() * sizeof(bytecode[0]));
		decoder.compileProgram(prog, bytecode.data(), nreg, RANDOMX_FLAG_V2);

		const randomx::ProgramConfiguration& cfg = VmAccess::configOf(vm);
		const randomx::MemoryRegisters& mem = VmAccess::memOf(vm);
		const randomx::RegisterFile* reg = vm->getRegisterFile();
		std::vector<uint64_t> abits;
		for (int j = 0; j < 4; ++j) {
			uint64_t lo, hi;
			memcpy(&lo, &reg->a[j].lo, 8);
			memcpy(&hi, &reg->a[j].hi, 8);
			abits.push_back(lo);
			abits.push_back(hi);
		}

		os << "    {\"seed\": " << qhex(seed, 64) << ",\n     \"bytes\": " << qhex(&prog, sizeof(prog))
		   << ",\n     \"config\": {\"a_bits\": [" << joined(abits) << "], \"ma\": " << mem.ma
		   << ", \"mx\": " << mem.mx << ", \"read_regs\": [" << cfg.readReg0 << ", " << cfg.readReg1
		   << ", " << cfg.readReg2 << ", " << cfg.readReg3 << "], \"dataset_offset\": "
		   << VmAccess::datasetOffsetOf(vm) << ", \"emask\": [" << cfg.eMask[0] << ", " << cfg.eMask[1]
		   << "]},\n     \"text\": [" << joined(lines) << "],\n     \"bytecode\": [\n";
		for (int j = 0; j < RANDOMX_PROGRAM_MAX_SIZE; ++j) {
			const randomx::InstructionByteCode& ibc = bytecode[j];
			int type = (int)ibc.type;
			int st = ibc.type == randomx::InstructionType::CBRANCH ? ibc.target : ibc.shift;
			os << "      [" << quoted(TYPE_NAMES[type]) << ", " << quoted(operandName(ibc.idst, nreg, ibc))
			   << ", " << quoted(operandName(ibc.isrc, nreg, ibc)) << ", " << ibc.imm << ", " << st
			   << ", " << ibc.memMask << "]" << (j + 1 < RANDOMX_PROGRAM_MAX_SIZE ? ",\n" : "\n");
		}
		os << "     ]}" << (i + 1 < count ? ",\n" : "\n");
	}
	os << "  ]\n}";
	return os.str();
}

// ---------------------------------------------------------------- instructions

uint64_t smallPositiveFloatBits(uint64_t entropy) {  // VM input generation (virtual_machine.cpp)
	return ((((entropy >> 59) + 1023) & 2047) << 52) | (entropy & ((1ULL << 52) - 1));
}

uint64_t floatMask(uint64_t entropy) {
	return (entropy & ((1ULL << 22) - 1)) | ((0x300 | ((entropy >> 60) << 4)) << 52);
}

double bitsToDouble(uint64_t q) {
	double d;
	memcpy(&d, &q, 8);
	return d;
}

uint64_t doubleBits(double d) {
	uint64_t q;
	memcpy(&q, &d, 8);
	return q;
}

// Group F values: int32 conversions, sums with A values, moderate doubles and edge cases.
uint64_t randomF(SplitMix64& rng) {
	const uint64_t x = rng.next(), signFraction = rng.next() & 0x800FFFFFFFFFFFFFULL;
	switch (x % 8) {
	case 0: return doubleBits((double)(int32_t)rng.next());
	case 1: return doubleBits((double)(int32_t)rng.next() + bitsToDouble(smallPositiveFloatBits(rng.next())));
	case 2: return signFraction;                                     // zero or denormal
	case 3: return signFraction | (0x7FEULL << 52);                  // near overflow
	case 4: return signFraction | (1ULL << 52);                      // smallest normals
	case 5: return signFraction | ((0xEULL + (x >> 8) % 3) << 52);   // FSCAL maps exponent 15 to 0
	default: return signFraction | ((1003 + (x >> 8) % 60) << 52);   // 2^-20 .. 2^40
	}
}

// Group E values: positive, masked like 4.3.2, plus edge cases.
uint64_t randomE(SplitMix64& rng) {
	uint64_t x = rng.next();
	switch (x % 6) {
	case 0: return rng.next() & 0x000FFFFFFFFFFFFFULL;                            // denormal
	case 1: return (rng.next() & 0x000FFFFFFFFFFFFFULL) | (0x7FEULL << 52);       // huge
	case 2: return (rng.next() & 0x000FFFFFFFFFFFFFULL) | ((uint64_t)(1 + (x >> 8) % 60) << 52);
	default: return (doubleBits((double)(int32_t)rng.next()) & randomx::dynamicMantissaMask) | floatMask(rng.next());
	}
}

uint64_t randomR(SplitMix64& rng) {
	uint64_t x = rng.next();
	switch (x % 10) {
	case 0: return 0;
	case 1: return ~0ULL;
	case 2: return 1ULL << 63;
	case 3: return x >> 40;
	default: return rng.next();
	}
}

struct State {
	uint64_t r[8], f[8], e[8], a[8];
};

void load(randomx::NativeRegisterFile& n, const State& s) {
	for (int i = 0; i < 8; ++i)
		n.r[i] = s.r[i];
	for (int i = 0; i < 4; ++i) {
		n.f[i] = rx_set_vec_f128(s.f[2 * i + 1], s.f[2 * i]);
		n.e[i] = rx_set_vec_f128(s.e[2 * i + 1], s.e[2 * i]);
		n.a[i] = rx_set_vec_f128(s.a[2 * i + 1], s.a[2 * i]);
	}
}

State save(const randomx::NativeRegisterFile& n) {
	State s;
	alignas(16) uint64_t v[2];
	for (int i = 0; i < 8; ++i)
		s.r[i] = n.r[i];
	for (int i = 0; i < 4; ++i) {
		rx_store_vec_f128((double*)v, n.f[i]);
		s.f[2 * i] = v[0], s.f[2 * i + 1] = v[1];
		rx_store_vec_f128((double*)v, n.e[i]);
		s.e[2 * i] = v[0], s.e[2 * i + 1] = v[1];
		rx_store_vec_f128((double*)v, n.a[i]);
		s.a[2 * i] = v[0], s.a[2 * i + 1] = v[1];
	}
	return s;
}

std::string dumpInstructions() {
	const uint64_t scratchpadSeed = 0x5eed;
	SplitMix64 fill{scratchpadSeed};
	std::vector<uint64_t> scratchpad(RANDOMX_SCRATCHPAD_L3 / 8);
	for (auto& w : scratchpad)
		w = fill.next();
	std::vector<uint64_t> before = scratchpad;

	SplitMix64 rng{2};
	std::ostringstream os;
	os << "{\n  \"scratchpad_splitmix64_seed\": " << scratchpadSeed << ",\n  \"cases\": [\n";
	bool first = true;
	for (int type = 0; type < 29; ++type) {
		const std::string name = TYPE_NAMES[type];
		const bool rounding = name == "FADD_R" || name == "FADD_M" || name == "FSUB_R" || name == "FSUB_M"
			|| name == "FMUL_R" || name == "FDIV_M" || name == "FSQRT_R";
		const int lo = type ? CEILS[type - 1] : 0, hi = CEILS[type];
		for (int n = 0; n < 16; ++n) {
			randomx::Instruction instr;
			instr.opcode = (uint8_t)(lo + rng.next() % (hi - lo));
			instr.dst = (uint8_t)rng.next();
			instr.src = (uint8_t)rng.next();
			instr.mod = (uint8_t)rng.next();
			uint32_t imm = (uint32_t)rng.next();
			if (name == "IMUL_RCP" && n % 4 == 0)
				imm = n % 8 ? 1u << (rng.next() % 32) : 0;
			instr.setImm32(imm);

			State s;
			for (int i = 0; i < 8; ++i) {
				s.r[i] = randomR(rng);
				s.f[i] = randomF(rng);
				s.e[i] = randomE(rng);
				s.a[i] = smallPositiveFloatBits(rng.next());
			}
			if ((name == "FADD_R" || name == "FSUB_R") && n % 4 == 3) {  // exact zero: its sign depends on fprc
				const uint64_t sign = name == "FADD_R" ? 1ULL << 63 : 0;
				s.f[2 * (instr.dst % 4)] = s.a[2 * (instr.src % 4)] ^ sign;
			}
			alignas(16) randomx::ProgramConfiguration config;
			config.eMask[0] = floatMask(rng.next());
			config.eMask[1] = floatMask(rng.next());
			config.readReg0 = config.readReg1 = config.readReg2 = config.readReg3 = 0;
			const int pc = (int)(rng.next() % 256);

			randomx::NativeRegisterFile nreg;
			randomx::BytecodeMachine decoder;
			decoder.beginCompilation(nreg);
			randomx::InstructionByteCode ibc;
			memset(&ibc, 0, sizeof(ibc));
			decoder.compileInstruction(instr, pc, ibc);
			if (name == "CBRANCH" && n % 2) {  // make the jump taken
				int d = instr.dst % 8;
				s.r[d] = ((rng.next() & ~(uint64_t)ibc.memMask) - ibc.imm);
			}

			const int modes = rounding ? 4 : 1;
			const int versions = name == "CFROUND" ? 2 : 1;
			for (int mode = 0; mode < modes; ++mode) {
				for (int v = 0; v < versions; ++v) {
					const int fprcBefore = name == "CFROUND" ? (int)(rng.next() % 4) : mode;
					const randomx_flags flags = v ? RANDOMX_FLAG_V2 : RANDOMX_FLAG_DEFAULT;
					load(nreg, s);
					int pcAfter = pc;
					rx_set_rounding_mode(fprcBefore);
					randomx::BytecodeMachine::executeInstruction(ibc, pcAfter, (uint8_t*)scratchpad.data(), config, flags);
					const int fprcAfter = rx_get_rounding_mode();
					rx_set_rounding_mode(RoundToNearest);
					State out = save(nreg);

					std::vector<std::string> stores;
					for (size_t w = 0; w < scratchpad.size(); ++w) {
						if (scratchpad[w] != before[w]) {
							stores.push_back("[" + std::to_string(w * 8) + ", " + std::to_string(scratchpad[w]) + "]");
							scratchpad[w] = before[w];
						}
					}
					os << (first ? "" : ",\n") << "    {\"name\": " << quoted(name) << ", \"word\": "
					   << qhex(&instr, 8) << ", \"pc\": " << pc << ", \"v2\": " << (v ? "true" : "false")
					   << ", \"fprc\": " << fprcBefore << ", \"emask\": [" << config.eMask[0] << ", "
					   << config.eMask[1] << "],\n     \"before\": " << qhex(&s, sizeof(s))
					   << ",\n     \"after\": " << qhex(&out, sizeof(out)) << ",\n     \"pc_after\": "
					   << pcAfter << ", \"fprc_after\": " << fprcAfter << ", \"stores\": [" << joined(stores) << "]}";
					first = false;
				}
			}
		}
	}
	os << "\n  ]\n}";
	return os.str();
}

// ---------------------------------------------------------------- hash traces

struct Vector {
	const char* id;
	std::string key, input;
	const char* v1;  // official hashes from src/tests/tests.cpp (nullptr: none published)
	const char* v2;
};

std::string unhex(const char* h) {
	std::string out;
	for (size_t i = 0; h[i] && h[i + 1]; i += 2)
		out += (char)strtol(std::string(h + i, 2).c_str(), nullptr, 16);
	return out;
}

std::string dumpHashTraces() {
	const std::string key0 = "test key 000", key1 = "test key 001";
	const std::string keyF = unhex("7797373ea4633194640bf8d8c3b66724d6aa7bd2dc20e009df2f8f1710abe8");
	const std::string lorem = "sed do eiusmod tempor incididunt ut labore et dolore magna aliqua";
	const std::vector<Vector> vectors = {
		{"1a", key0, "This is a test",
			"639183aae1bf4c9a35884cb46b09cad9175f04efd7684e7262a0ac1c2f0b4e3f",
			"22ec6b861b3eb23686b2efbad69513c967ecfce80983df66c9c5b4fbfb4cdb6f"},
		{"1b", key0, "Lorem ipsum dolor sit amet",
			"300a0adb47603dedb42228ccb2b211104f4da45af709cd7547cd049e9489c969",
			"9e2c772c12fd48f93c14c97fdc89d556264d9100597023f44d9163e279012ecf"},
		{"1c", key0, lorem,
			"c36d4ed4191e617309867ed66a443be4075014e2b061bcdaf9ce7b721d2b77a8",
			"4d6b063a1a603751d525f18a171336a4002f2f06df6c17e4b25fe17e17796e42"},
		{"1d", key1, lorem,
			"e9ff4503201c0c2cca26d285c93ae883f9b1d30c9eb240b820756f2d5a7905fc",
			"97024134686ce27d362ea8d86d8ef16483ac272abdabd46ef13359400777fe5e"},
		{"1e", key1, unhex("0b0b98bea7e805e0010a2126d287a2a0cc833d312cb786385a7c2f9de69d25537f584a9bc9977b"
			"00000000666fd8753bf61a8631f12984e3fd44f4014eca629276817b56f32e9b68bd82f416"),
			"c56414121acda1713c2f2a819d8ae38aed7c80c35c2a769298d34f03833cd5f1",
			"c8e92c5f7c1946fecf06bc382b92e3111da38ee3e6a5ad90704e1a9d8aaf6e76"},
		{"1f", keyF, unhex("1010e1eaf8cf067b37b5f0ee031ab23ed1755e090a3af4415830145853e2be3e1f6821fed84dae58"
			"d00e00da5214d6c1f2d0622e0abd51f9373d04e0b0f8e6d6514d90689721c4aac5a9bb0d"),
			"78af2a1864c42abce36d2e8983e13df99b2af0ce1362999af09fab004d4435a8", nullptr},
	};

	std::ostringstream os;
	os << "{\n  \"vectors\": [\n";
	randomx_cache* cache = randomx_alloc_cache(RANDOMX_FLAG_DEFAULT);
	require(cache != nullptr, "randomx_alloc_cache");
	std::string cachedKey;
	bool first = true;
	for (const Vector& vec : vectors) {
		if (vec.key != cachedKey) {
			randomx_init_cache(cache, vec.key.data(), vec.key.size());
			cachedKey = vec.key;
		}
		for (int v = 0; v < 2; ++v) {
			randomx_vm* vm = randomx_create_vm(v ? RANDOMX_FLAG_V2 : RANDOMX_FLAG_DEFAULT, cache, nullptr);
			require(vm != nullptr, "randomx_create_vm");
			alignas(16) uint64_t tempHash[8];
			randomx_blake2b(tempHash, sizeof(tempHash), vec.input.data(), vec.input.size(), nullptr, 0);
			os << (first ? "" : ",\n") << "    {\"id\": " << quoted(vec.id) << ", \"v2\": "
			   << (v ? "true" : "false") << ", \"key\": " << qhex(vec.key.data(), vec.key.size())
			   << ", \"input\": " << qhex(vec.input.data(), vec.input.size())
			   << ",\n     \"seed\": " << qhex(tempHash, 64);
			first = false;
			vm->initScratchpad(tempHash);
			os << ",\n     \"gen1_state\": " << qhex(tempHash, 64) << ",\n     \"scratchpad_init\": "
			   << quoted(blake256(vm->getScratchpad(), RANDOMX_SCRATCHPAD_L3)) << ",\n     \"programs\": [\n";
			vm->resetRoundingMode();
			for (int chain = 0; chain < RANDOMX_PROGRAM_COUNT; ++chain) {
				vm->run(tempHash);
				const int fprc = rx_get_rounding_mode();
				os << "       {\"register_file\": " << qhex(vm->getRegisterFile(), sizeof(randomx::RegisterFile))
				   << ", \"scratchpad\": " << quoted(blake256(vm->getScratchpad(), RANDOMX_SCRATCHPAD_L3))
				   << ", \"fprc\": " << fprc << "}" << (chain + 1 < RANDOMX_PROGRAM_COUNT ? ",\n" : "\n");
				if (chain + 1 < RANDOMX_PROGRAM_COUNT)
					randomx_blake2b(tempHash, sizeof(tempHash), vm->getRegisterFile(), sizeof(randomx::RegisterFile), nullptr, 0);
			}
			uint8_t hash[RANDOMX_HASH_SIZE];
			vm->getFinalResult(hash, sizeof(hash));
			vm->resetRoundingMode();
			const char* official = v ? vec.v2 : vec.v1;
			if (official)
				require(hex(hash, sizeof(hash)) == official, "trace reproduces the official hash");
			os << "     ],\n     \"hash\": " << qhex(hash, sizeof(hash)) << ", \"official\": "
			   << (official ? "true" : "false") << "}";
			randomx_destroy_vm(vm);
		}
	}
	randomx_release_cache(cache);
	os << "\n  ]\n}";
	return os.str();
}

}  // namespace

int main(int argc, char** argv) {
	if (argc != 2) {
		fprintf(stderr, "usage: %s OUTDIR\n", argv[0]);
		return 2;
	}
	const std::string dir = argv[1];
	writeFile(dir, "vm_aes.json", dumpAes());
	writeFile(dir, "vm_instructions.json", dumpInstructions());

	randomx_cache* cache = randomx_alloc_cache(RANDOMX_FLAG_DEFAULT);
	require(cache != nullptr, "randomx_alloc_cache");
	randomx_init_cache(cache, "rxlab", 5);
	randomx_vm* vm = randomx_create_vm(RANDOMX_FLAG_DEFAULT, cache, nullptr);
	require(vm != nullptr, "randomx_create_vm");
	writeFile(dir, "vm_programs.json", dumpPrograms(vm));
	randomx_destroy_vm(vm);
	randomx_release_cache(cache);

	writeFile(dir, "vm_hash_trace.json", dumpHashTraces());
	return 0;
}
