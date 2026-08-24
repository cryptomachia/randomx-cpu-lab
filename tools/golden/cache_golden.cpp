// Golden values for the pure-Python cache/dataset (rxlab/spec), computed by the
// RandomX v2.0.1 code itself (linked statically against its CMake object files).
// Build and run with tools/golden/build_cache_golden.sh.
//
//   cache_golden argon2      [key]  sampled Argon2d cache blocks, pass-0 prefix
//   cache_golden superscalar [key]  BlakeGenerator stream, the 8 programs, their outputs
//   cache_golden dataset     [key]  reciprocal table, dataset items, item-0 trace
//   cache_golden keys        [n]    digest and statistics of the 8 programs of n keys
//
// Output is JSON on stdout; 64-bit words are 16-digit hex strings.
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "argon2_core.h"
#include "blake2/blake2.h"
#include "blake2_generator.hpp"
#include "common.hpp"
#include "dataset.hpp"
#include "randomx.h"
#include "reciprocal.h"
#include "superscalar.hpp"

using namespace randomx;

static const uint32_t kPrefixBlocks = 4096;  // pass-0 blocks the fast Python test rebuilds
static const int kRandomBlocks = 16;

static void fail(const char* msg) {
	fprintf(stderr, "cache_golden: %s\n", msg);
	exit(1);
}

static void hex(const void* data, size_t size) {
	const uint8_t* p = (const uint8_t*)data;
	putchar('"');
	for (size_t i = 0; i < size; ++i)
		printf("%02x", p[i]);
	putchar('"');
}

static void digest(const void* data, size_t size) {  // Hash256 = Blake2b-256
	uint8_t h[32];
	blake2b(h, sizeof(h), data, size, nullptr, 0);
	hex(h, sizeof(h));
}

static void words(const uint64_t* w, int n) {
	putchar('[');
	for (int i = 0; i < n; ++i)
		printf("%s\"%016" PRIx64 "\"", i ? ", " : "", w[i]);
	putchar(']');
}

static randomx_cache* makeCache(const std::string& key) {
	randomx_cache* cache = randomx_alloc_cache(RANDOMX_FLAG_DEFAULT);
	if (cache == nullptr)
		fail("randomx_alloc_cache failed");
	randomx_init_cache(cache, key.data(), key.size());
	return cache;
}

// The cache again, with the same Argon2d setup as initCache (dataset.cpp) but
// stopping after pass 0 so its first blocks can be dumped, then finishing the
// remaining passes and checking the result equals the library's cache.
static void dumpArgon2(const std::string& key) {
	randomx_cache* cache = makeCache(key);
	const uint32_t blocks = RANDOMX_ARGON_MEMORY;

	argon2_context context;
	memset(&context, 0, sizeof(context));
	context.pwd = (uint8_t*)key.data();
	context.pwdlen = (uint32_t)key.size();
	context.salt = (uint8_t*)RANDOMX_ARGON_SALT;
	context.saltlen = (uint32_t)ArgonSaltSize;
	context.t_cost = RANDOMX_ARGON_ITERATIONS;
	context.m_cost = RANDOMX_ARGON_MEMORY;
	context.lanes = RANDOMX_ARGON_LANES;
	context.threads = 1;
	context.flags = ARGON2_DEFAULT_FLAGS;
	context.version = ARGON2_VERSION_NUMBER;
	if (randomx_argon2_validate_inputs(&context) != ARGON2_OK)
		fail("invalid Argon2 context");

	std::vector<block> memory(blocks);
	argon2_instance_t instance;
	memset(&instance, 0, sizeof(instance));
	instance.version = context.version;
	instance.memory = memory.data();
	instance.passes = 1;
	instance.memory_blocks = blocks;
	instance.segment_length = blocks / (context.lanes * ARGON2_SYNC_POINTS);
	instance.lane_length = instance.segment_length * ARGON2_SYNC_POINTS;
	instance.lanes = context.lanes;
	instance.threads = 1;
	instance.type = Argon2_d;
	instance.impl = &randomx_argon2_fill_segment_ref;
	randomx_argon2_initialize(&instance, &context);
	randomx_argon2_fill_memory_blocks(&instance);

	printf("{\n \"key\": ");
	hex(key.data(), key.size());
	printf(",\n \"source\": \"RandomX v2.0.1 randomx_init_cache (RANDOMX_FLAG_DEFAULT)\"");
	printf(",\n \"pass0_prefix_blocks\": %u,\n \"pass0_prefix_hash256\": ", kPrefixBlocks);
	digest(memory.data(), kPrefixBlocks * sizeof(block));
	printf(",\n \"pass0_blocks\": {");
	const uint32_t shown[] = {0, 1, 2, 3, kPrefixBlocks - 1};
	for (size_t i = 0; i < sizeof(shown) / sizeof(shown[0]); ++i) {
		printf("%s\n  \"%u\": ", i ? "," : "", shown[i]);
		hex(&memory[shown[i]], sizeof(block));
	}
	printf("\n }");

	for (uint32_t pass = 1; pass < RANDOMX_ARGON_ITERATIONS; ++pass)
		for (uint32_t slice = 0; slice < ARGON2_SYNC_POINTS; ++slice)
			for (uint32_t lane = 0; lane < instance.lanes; ++lane) {
				argon2_position_t position = {pass, lane, (uint8_t)slice, 0};
				randomx_argon2_fill_segment_ref(&instance, position);
			}
	if (memcmp(memory.data(), cache->memory, CacheSize) != 0)
		fail("replayed Argon2d differs from randomx_init_cache");

	printf(",\n \"cache_hash256\": ");
	digest(cache->memory, CacheSize);
	std::vector<uint32_t> picks = {0, blocks - 1};
	uint64_t state = 0x5258636163686521ULL;  // fixed seed; any value works
	while (picks.size() < 2 + kRandomBlocks) {
		state = state * 6364136223846793005ULL + 1442695040888963407ULL;
		uint32_t index = 1 + (uint32_t)((state >> 33) % (blocks - 2));
		bool seen = false;
		for (uint32_t p : picks)
			seen = seen || p == index;
		if (!seen)
			picks.push_back(index);
	}
	printf(",\n \"blocks\": {");
	for (size_t i = 0; i < picks.size(); ++i) {
		printf("%s\n  \"%u\": ", i ? "," : "", picks[i]);
		hex(cache->memory + (size_t)picks[i] * ArgonBlockSize, ArgonBlockSize);
	}
	printf("\n }\n}\n");
	randomx_release_cache(cache);
}

static void dumpProgram(SuperscalarProgram& prog) {
	printf("{\n   \"size\": %u, \"address_register\": %d, \"cpu_latency\": %d, \"asic_latency\": %d,",
		prog.getSize(), prog.getAddressRegister(), prog.cpuLatency, prog.asicLatency);
	printf("\n   \"code_size\": %d, \"macro_ops\": %d, \"decode_cycles\": %d,",
		prog.codeSize, prog.macroOps, prog.decodeCycles);
	printf(" \"mul_count\": %d, \"ipc\": %.17g,", prog.mulCount, prog.ipc);
	printf("\n   \"cpu_latencies\": [");
	for (int i = 0; i < 8; ++i)
		printf("%s%d", i ? ", " : "", prog.cpuLatencies[i]);
	printf("], \"asic_latencies\": [");
	for (int i = 0; i < 8; ++i)
		printf("%s%d", i ? ", " : "", prog.asicLatencies[i]);
	printf("],\n   \"instructions\": [");
	for (unsigned i = 0; i < prog.getSize(); ++i) {
		Instruction& instr = prog(i);
		printf("%s\n    [%u, %u, %u, %u, %u]", i ? "," : "", instr.opcode, instr.dst, instr.src,
			instr.mod, instr.getImm32());
	}
	printf("\n   ]");
}

// Programs straight from generateSuperscalar (IMUL_RCP still holds its divisor),
// each also run once on fixed input registers.
static void dumpSuperscalar(const std::string& key) {
	printf("{\n \"key\": ");
	hex(key.data(), key.size());
	printf(",\n \"source\": \"RandomX v2.0.1 Blake2Generator, generateSuperscalar,"
		" executeSuperscalar\"");

	Blake2Generator stream(key.data(), key.size());
	printf(",\n \"generator_bytes\": [");
	for (int i = 0; i < 65; ++i)  // one full state plus one byte of the next
		printf("%s%u", i ? ", " : "", stream.getByte());
	printf("],\n \"generator_uint32\": [");
	for (int i = 0; i < 20; ++i)  // the 16th request straddles a state and forces a rehash
		printf("%s%u", i ? ", " : "", stream.getUInt32());
	printf("]");

	Blake2Generator gen(key.data(), key.size());
	std::vector<SuperscalarProgram> programs(RANDOMX_CACHE_ACCESSES);
	printf(",\n \"programs\": [");
	for (int i = 0; i < RANDOMX_CACHE_ACCESSES; ++i) {
		SuperscalarProgram& prog = programs[i];
		generateSuperscalar(prog, gen);
		printf("%s\n  ", i ? "," : "");
		dumpProgram(prog);
		std::vector<uint64_t> reciprocals;
		for (unsigned j = 0; j < prog.getSize(); ++j)
			if ((SuperscalarInstructionType)prog(j).opcode == SuperscalarInstructionType::IMUL_RCP)
				reciprocals.push_back(randomx_reciprocal(prog(j).getImm32()));
		printf(",\n   \"reciprocals\": ");
		words(reciprocals.data(), (int)reciprocals.size());
		uint64_t r[8];
		for (int q = 0; q < 8; ++q)
			r[q] = 0x0123456789abcdefULL * (q + 1) ^ (0xfedcba9876543210ULL >> q);
		printf(",\n   \"input\": ");
		words(r, 8);
		executeSuperscalar(r, prog);
		printf(",\n   \"output\": ");
		words(r, 8);
		printf("\n  }");
	}
	printf("\n ]\n}\n");
}

// The 8 programs of each of `count` keys ("superscalar 0", ...), one row per
// program: statistics and the Hash256 of its instruction bytes (as tests.cpp).
static void dumpKeys(int count) {
	printf("{\n \"source\": \"RandomX v2.0.1 Blake2Generator, generateSuperscalar\",");
	printf("\n \"key_format\": \"superscalar %%d\",");
	printf("\n \"columns\": [\"key\", \"program\", \"size\", \"address_register\","
		" \"cpu_latency\", \"asic_latency\", \"mul_count\", \"decode_cycles\","
		" \"macro_ops\", \"code_size\", \"hash256\"],");
	printf("\n \"rows\": [");
	SuperscalarProgram prog;
	for (int k = 0; k < count; ++k) {
		const std::string key = "superscalar " + std::to_string(k);
		Blake2Generator gen(key.data(), key.size());
		for (int i = 0; i < RANDOMX_CACHE_ACCESSES; ++i) {
			generateSuperscalar(prog, gen);
			printf("%s\n  [%d, %d, %u, %d, %d, %d, %d, %d, %d, %d, ", k || i ? "," : "", k, i,
				prog.getSize(), prog.getAddressRegister(), prog.cpuLatency, prog.asicLatency,
				prog.mulCount, prog.decodeCycles, prog.macroOps, prog.codeSize);
			digest(prog.programBuffer, sizeof(Instruction) * prog.getSize());
			putchar(']');
		}
	}
	printf("\n ]\n}\n");
}

// Dataset items via initDatasetItem, plus item 0 replayed step by step.
static void dumpDataset(const std::string& key) {
	randomx_cache* cache = makeCache(key);
	printf("{\n \"key\": ");
	hex(key.data(), key.size());
	printf(",\n \"source\": \"RandomX v2.0.1 randomx_init_cache, initDatasetItem\"");
	printf(",\n \"reciprocal_cache\": ");
	words(cache->reciprocalCache.data(), (int)cache->reciprocalCache.size());
	printf(",\n \"address_registers\": [");
	for (int i = 0; i < RANDOMX_CACHE_ACCESSES; ++i)
		printf("%s%d", i ? ", " : "", cache->programs[i].getAddressRegister());
	printf("]");

	const uint64_t last = randomx_dataset_item_count() - 1;
	const uint64_t items[] = {0, 1, 2, 1000, 123456, 10000000, 20000000, 30000000, last};
	printf(",\n \"items\": {");
	for (size_t i = 0; i < sizeof(items) / sizeof(items[0]); ++i) {
		uint64_t out[8];
		initDatasetItem(cache, (uint8_t*)out, items[i]);
		printf("%s\n  \"%" PRIu64 "\": ", i ? "," : "", items[i]);
		words(out, 8);
	}
	printf("\n }");

	// specs.md §7.3 for item 0, recording every step.
	const uint64_t init[8] = {0, 9298411001130361340ULL, 12065312585734608966ULL,
		9306329213124626780ULL, 5281919268842080866ULL, 10536153434571861004ULL,
		3398623926847679864ULL, 9549104520008361294ULL};
	uint64_t r[8];
	r[0] = 6364136223846793005ULL;
	for (int q = 1; q < 8; ++q)
		r[q] = r[0] ^ init[q];
	printf(",\n \"item0_trace\": [");
	uint64_t registerValue = 0;
	for (int i = 0; i < RANDOMX_CACHE_ACCESSES; ++i) {
		const uint64_t line = registerValue & (CacheSize / CacheLineSize - 1);
		const uint64_t* mix = (const uint64_t*)(cache->memory + line * CacheLineSize);
		executeSuperscalar(r, cache->programs[i], &cache->reciprocalCache);
		printf("%s\n  {\"cache_line\": %" PRIu64 ", \"after_program\": ", i ? "," : "", line);
		words(r, 8);
		for (int q = 0; q < 8; ++q)
			r[q] ^= mix[q];
		printf(", \"after_mix\": ");
		words(r, 8);
		printf("}");
		registerValue = r[cache->programs[i].getAddressRegister()];
	}
	printf("\n ]\n}\n");
	uint64_t item0[8];
	initDatasetItem(cache, (uint8_t*)item0, 0);
	if (memcmp(item0, r, sizeof(r)) != 0)
		fail("item 0 trace disagrees with initDatasetItem");
	randomx_release_cache(cache);
}

int main(int argc, char** argv) {
	if (argc < 2 || argc > 3)
		fail("usage: cache_golden {argon2|superscalar|dataset} [key] | keys [n]");
	const std::string mode = argv[1];
	const std::string key = argc == 3 ? argv[2] : "test key 000";
	if (mode == "keys")
		dumpKeys(argc == 3 ? atoi(argv[2]) : 256);
	else if (mode == "argon2")
		dumpArgon2(key);
	else if (mode == "superscalar")
		dumpSuperscalar(key);
	else if (mode == "dataset")
		dumpDataset(key);
	else
		fail("unknown mode");
	return 0;
}
