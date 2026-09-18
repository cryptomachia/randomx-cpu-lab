# Pure-Python RandomX

`rxlab.spec` is an implementation of RandomX, Monero's proof-of-work, written
from `doc/specs.md` in Python. It uses numpy for Argon2d and gmpy2/MPFR for
floating-point arithmetic under directed rounding. It computes light-mode
hashes for RandomX v1 and v2 from nothing but the key and the input. Its
purpose is to be read, instrumented and checked stage by stage against the
reference C library (RandomX v2.0.1, which computes both v1 and v2 hashes). A
hash takes seconds, so it is not a way to hash for real.

**Headline.** Starting from a fresh in-memory Argon2d fill, and without loading
any RandomX C code, the chain reproduces all 12 distinct official hash
constants in the library's `src/tests/tests.cpp`:

- the v1 hashes of vectors 1a-1f;
- the v2 hashes of vectors 1a-1e;
- the v2 "Commitment test" of vector 1a.

It also matches the C library on vector 1f under v2, for which tests.cpp
publishes no constant.

| Result | Evidence |
|---|---|
| 11 official hashes and the v2 commitment, each from a fresh Python Argon2d fill of its key | `python3 -m rxlab.spec --vectors --fresh-cache --json` reports `"all_match": true` and `"cache": "argon2d"` for all 3 keys |
| No RandomX C code involved (proved in a test for vector 1a: v1, v2 and the commitment) | [`test_vector_1a_in_pure_python`](../tests/test_spec_full.py) |
| 1f v2 = `59a892571444ddfc309f876d620220b83e926395c56f568efeb0358bad7dbdf6`, the C library's value | `--check`, [`data/golden/vm_hash_trace.json`](../data/golden/vm_hash_trace.json) |
| For all 12 hashes, all 8 intermediate register files, scratchpad digests and `fprc` values of the Python VM (run on the C library's full Dataset) equal the C interpreter's | [`test_official_vector_end_to_end`](../tests/test_spec_vm.py) |
| 816 single instructions, including every FP operation in all four rounding modes, bit-identical to the C interpreter | [`test_instructions_match_c_interpreter`](../tests/test_spec_vm.py) |
| 8 SuperscalarHash programs identical to C in every instruction and reference-CPU statistic; 2,048 more identical in digest and 7 statistics | [`tests/test_spec_cache.py`](../tests/test_spec_cache.py) |

## Contents

- [Architecture](#architecture)
- [Validation](#validation)
- [Semantics that are easy to get wrong](#semantics-that-are-easy-to-get-wrong)
- [Measurements](#measurements)
- [Running it](#running-it)
- [Development notes](#development-notes)
- [Known limitations and open questions](#known-limitations-and-open-questions)

## Architecture

```
Cache side, once per key
  key ─► Argon2d (argon2d.py) ──────► cache: 256 MiB = 4,194,304 lines × 64 B
  key ─► BlakeGenerator ────────────► 8 SuperscalarHash programs (superscalar.py)
  item(n) = 8 × (run program i, XOR one cache line)     DatasetItemSource (dataset.py)

Hash side, per input
  input ─► Blake2b-512 ─► AesGenerator1R (aes.py) ─► 2 MiB scratchpad
                          generator state = seed of program 0
  for each of 8 programs:
      seed ─► AesGenerator4R ─► 128 B configuration + 256 (v1) / 384 (v2) instruction words
           ─► decode (program.py) ─► one compiled Python function (vm.py)
           ─► 2048 iterations, each reading one Dataset item through item(n)
           ─► 256-byte register file ─► Blake2b-512 ─► seed of the next program
  hash = Blake2b-256(register file[0:192] ‖ AesHash1R(scratchpad))     (hasher.py)
```

A hash runs 8 programs of 2048 iterations. Each iteration reads one Dataset
item, so a hash reads 16,384 items. In light mode, each item is computed from
the cache when the VM asks for it.

| Module | Role |
|---|---|
| [`rxlab/spec/params.py`](../rxlab/spec/params.py) | `configuration.h` constants (v1.2.1 / v2.0.1), the opcode frequency table, `reciprocal`, `signext32` |
| [`rxlab/spec/argon2d.py`](../rxlab/spec/argon2d.py) | Argon2d (RFC 9106) in numpy. It is general enough for the RFC vector (several Argon2 lanes, secret, associated data, tag, v1.0/v1.3). `randomx_argon2d(key)` builds the RandomX instance, `cache_memory(key)` fills it, and `fill_cache(key)` memoises the fill. |
| [`rxlab/spec/blake2gen.py`](../rxlab/spec/blake2gen.py) | `BlakeGenerator`: bytes and uint32s from a 64-byte state rehashed with Blake2b-512 (specs.md §3.5) |
| [`rxlab/spec/superscalar.py`](../rxlab/spec/superscalar.py) | SuperscalarHash (§6). `generate(gen)` is a line-by-line port of `generateSuperscalar`, including the decoder and port simulation. `execute` is an interpreter. `to_python` emits one Python statement per instruction. |
| [`rxlab/spec/dataset.py`](../rxlab/spec/dataset.py) | `DatasetItemSource(cache_words, key)` (§7.2-7.3). `.item(n)` runs generated straight-line code. `.reference_item(n)` follows §7.3 step by step with the interpreter. The class also exposes `.programs`, `.reciprocals` (in the C `reciprocalCache` order) and `.cache_line(i)`. |
| [`rxlab/spec/aes.py`](../rxlab/spec/aes.py) | S-box from the GF(2^8) inverse and the FIPS-197 affine map; T-tables; AESENC/AESDEC-semantics rounds; AesGenerator1R, AesGenerator4R, AesHash1R (chapter 3) |
| [`rxlab/spec/program.py`](../rxlab/spec/program.py) | Program generation, configuration (§4.5), decoding into `Op` records that mirror the C `InstructionByteCode`, and a disassembler whose output is byte-identical to the C `Instruction::print` |
| [`rxlab/spec/fp.py`](../rxlab/spec/fp.py) | binary64 add/sub/mul/div/sqrt under the four `fprc` modes, FSCAL_R, group-E conversion |
| [`rxlab/spec/vm.py`](../rxlab/spec/vm.py) | The VM: each program compiles to one Python function, wrapped in the §4.6.2 iteration loop; optional dynamic counters |
| [`rxlab/spec/hasher.py`](../rxlab/spec/hasher.py) | `randomx_hash(data, item_source, v2=False, observer=None, stats=None, timings=None)` (chapter 2). `CDatasetItemSource` serves items from the C library, for full-memory checks. |
| [`rxlab/spec/light.py`](../rxlab/spec/light.py) | `LightHasher(key, fresh=False, words=None)` glues the key, the cache, `DatasetItemSource` and `randomx_hash` together. Also holds the official vector table `VECTORS`, `COMMITMENT_1A_V2` and `commitment()`. |
| [`rxlab/spec/__main__.py`](../rxlab/spec/__main__.py) | CLI with a per-phase timing breakdown |

### Cache side: key to Dataset item

**Argon2d.** The RandomX instance is Argon2d v1.3 with 262,144 KiB of memory,
3 passes, parallelism 1 and salt `"RandomX\x03"`. That makes 3 × 262,144 block
positions; the first two blocks come from H0 and every other position is one
compression. A 1 KiB block is 128 little-endian uint64 words. Each half of a
BLAKE2b-with-BlaMka round inside G is 32 independent quarter-rounds, so the
state is kept as a 128-word buffer laid out as (a, b, c, d) × 32. Each
quarter-round operand is then a contiguous 32-element view, and one
`ndarray.take(..., mode="clip")` with a precomputed permutation moves the
state from one half-round's layout to the next. BlaMka takes 6 ufunc calls
and each xor-rotate takes 4. Every call writes into a preallocated buffer, and
shift counts and the low-32-bit mask are held as arrays. That comes to about
168 numpy calls per block. Blocks form a strict chain: block *i* needs block
*i*−1 and a reference block chosen by block *i*−1's first word. Nothing can be
batched across blocks, so per-call overhead is the whole cost of a fill.

The memo `data/cache/cache-<sha256(key)[:16]>.npy` (256 MiB, ignored by git)
is written under a unique temporary name and moved into place with
`os.replace`. It is returned read-only with `mmap_mode="r"`, so processes
share the page cache.

**SuperscalarHash.** `generate` has to reproduce every random draw of the C
generator, including the reference-CPU simulation: the decoder buffer
configurations, the three execution ports (P5 → P0 → P1 order), fused-pair
scheduling, operand stalls of up to 4 cycles, and thrown-away instructions.
The result is a `Program` with the instructions and the C statistics:

- `cpu_latency`, `asic_latency`, `code_size`, `macro_ops`, `decode_cycles`,
  `mul_count` and `ipc`;
- the per-register latencies;
- the address register.

**Dataset items.** `DatasetItemSource` generates the 8 programs for the key.
It then `exec`s one Python function in which the 8 programs are unrolled into
straight-line integer statements, with constants and reciprocals inlined. This
is the same idea as the library's dataset-init JIT. The item computation
follows §7.3:

1. `r0 = (n + 1) · 6364136223846793005`, and `r1..r7 = r0 XOR` seven constants.
2. For each program: read cache line `address mod 4,194,304`, run the program,
   XOR the line in, and take the next address from the program's address
   register.

Both item paths are checked against C.

### Hash side: input to 32 bytes

**AES.** The S-box and T-tables are built from first principles. A 16-byte
state is four little-endian 32-bit words, word *j* holding column *j*. That is
the byte order that x86 AESENC/AESDEC and ARM AESE/AESMC use for a 128-bit
register. The generator keys and AesHash1R constants are tested against the
Blake2b strings that specs.md says they come from.

**Program generation and decoding.** AesGenerator4R produces 128 bytes of
configuration entropy followed by 8-byte instruction words: 256 of them for v1
and 384 for v2. `decode` settles every chapter-5 operand rule before
execution. It fixes register or immediate source, the scratchpad level mask,
the CBRANCH target and condition mask, the IMUL_RCP reciprocal, and no-op
encodings. The resulting `Op` fields can therefore be compared one for one
with the C bytecode.

**Floating point.** `fprc = 0` (round to nearest even) uses plain Python
floats. Modes 1-3 call exact MPFR contexts, `gmpy2.ieee(64)`, with 53-bit
precision, emin −1073, emax 1024 and subnormalization. As a result, overflow,
gradual underflow and the sign of exact zeros follow IEEE 754. gmpy2 is never
called on the `fprc = 0` path.

**VM.** A decoded program becomes one Python function whose locals are
`r0..r7`, `f0l..e3h` and `fprc`. Each instruction becomes one or two
statements, preceded by the C disassembly as a comment
(`CompiledProgram.source`). CBRANCH is the only control flow and always jumps
backwards. The body is therefore split into blocks at jump targets and guarded
by `if blk <= n:`. A taken branch sets `blk` and `continue`s an enclosing
`while True`, falling through every later block. This avoids a Python call per
instruction. The §4.6.2 loop around the body is plain Python. The F/E register
loads batch the int32-to-double conversion and the E masking into single
`struct` calls. The result of F/E mixing stays an integer bit pattern (see
[NaN bit patterns](#vm-side)).

**Item-source contract.** The two halves meet at one interface:
`item_source.item(i)` returns Dataset item *i* as a tuple of eight 64-bit
ints. `DatasetItemSource` (pure Python) and `CDatasetItemSource` (the C
library's Dataset memory) both implement it. The Python VM can therefore be
checked in full-memory mode independently of the Python Dataset code.

## Validation

### Where the golden values come from

Every golden value comes from one of three places: the RFC 9106 text,
RandomX's own `src/tests/tests.cpp`, or the C library itself. None is computed
by the Python code.

| File | Source | Contents |
|---|---|---|
| [`cache_rfc9106.json`](../data/golden/cache_rfc9106.json) | RFC 9106 §5.1, transcribed from the RFC text | Argon2d inputs, H0, 8 words after each of the 3 passes, tag |
| [`cache_argon2.json`](../data/golden/cache_argon2.json) | [`cache_golden.cpp`](../tools/golden/cache_golden.cpp) | Key `"test key 000"`: Blake2b-256 of the whole 256 MiB cache, 18 sampled 1 KiB blocks (first, last, 16 from a fixed-seed LCG), and the pass-0 prefix (Blake2b-256 of blocks 0-4095 plus blocks 0, 1, 2, 3 and 4095 in full) |
| [`cache_superscalar.json`](../data/golden/cache_superscalar.json) | same | The first 65 bytes and 20 uint32s of the BlakeGenerator stream; 8 programs with every instruction, statistic and reciprocal, and one execution each on fixed registers |
| [`cache_superscalar_keys.json`](../data/golden/cache_superscalar_keys.json) | same | Keys `"superscalar 0"` .. `"superscalar 255"`: statistics and the Hash256 of each of their 2,048 programs |
| [`cache_dataset.json`](../data/golden/cache_dataset.json) | same | The C `reciprocalCache` (239 values), the 8 address registers, items 0, 1, 2, 1000, 123456, 10^7, 2·10^7, 3·10^7 and 34078718 (the last), and every step of item 0 |
| [`vm_aes.json`](../data/golden/vm_aes.json) | [`vm_golden.cpp`](../tools/golden/vm_golden.cpp) | The C soft-AES T-tables; 32 single encryption and decryption rounds; AesGenerator1R and AesGenerator4R outputs; AesHash1R of 64 B, 128 B and 4 KiB inputs and of a full 2 MiB scratchpad |
| [`vm_programs.json`](../data/golden/vm_programs.json) | same | 6 programs: raw 3,200 bytes, 384-line C disassembly, C bytecode for every instruction, and the configuration read out of `randomx_vm::initialize()` |
| [`vm_instructions.json`](../data/golden/vm_instructions.json) | same | 816 single instructions run by `BytecodeMachine::executeInstruction`: every rounded FP instruction under all 4 modes, CFROUND under v1 and v2 |
| [`vm_hash_trace.json`](../data/golden/vm_hash_trace.json) | same | Vectors 1a-1f under v1 and v2. For each: seed, AesGenerator1R state, scratchpad digest; then, after each of the 8 programs, the register file, scratchpad digest and `fprc`; then the hash. |

Some provenance details:

- **What the harnesses link.** `cache_golden.cpp` links the v2.0.1 CMake object
  files statically and includes internal headers (`dataset.hpp`,
  `superscalar.hpp`, `argon2_core.h`). `vm_golden.cpp` links `librandomx` and
  reaches protected VM state (configuration, `ma`, `mx`, `datasetOffset`)
  through pointers to members named via a derived class. The configuration in
  the goldens is therefore the one the library's own `initialize()` computes.
- **Build flags.** Both harnesses are built with the library's target flags
  (`-march=armv8-a+crypto` on arm64), never `-march=native`.
- **Self-checks.** The pass-0 Argon2d replay is guarded by a final `memcmp`
  against `randomx_init_cache`. The item-0 trace is guarded by a `memcmp`
  against `initDatasetItem`. Every traced hash that has an official constant
  must equal it, or the tool exits.
- **Reproducibility.** Rebuilding either harness from source and rerunning it
  produced byte-identical JSON. The goldens hold no timestamps.
- **Official constants.** Every `tests.cpp` constant quoted in the tests
  appears literally in `third_party/randomx-v2.0.1/src/tests/tests.cpp`:
  3 cache words, 10 SuperscalarHash program hashes, 4 item words, the
  instruction and reciprocal results, the AesGenerator1R vector, and the hash
  vectors with the 31-byte key of 1f.

### Stage by stage

| Stage | Checked against | Tests |
|---|---|---|
| Argon2d core (4 Argon2 lanes, secret, associated data, XOR passes, tag) | RFC 9106 §5.1: H0, all 24 interim words, tag `512b391b…4acb` | `test_argon2d_rfc9106_vector` |
| RandomX Argon2d, pass 0 | C replay of `initCache` stopped after pass 0: H0, the first blocks and 4,094 compressions | `test_argon2d_randomx_pass0_prefix` |
| Full 256 MiB cache | tests.cpp words `[0]`, `[1568413]`, `[33554431]`; C Hash256 of all 256 MiB; 18 sampled blocks | `test_full_python_cache_fill` (slow), `test_memoised_cache_matches_golden`, `test_c_cache_matches_golden` |
| BlakeGenerator | C byte and uint32 stream, including a request that straddles a rehash | `test_blake_generator_stream` |
| SuperscalarHash generation | tests.cpp Hash256 of 10 consecutive programs; 8 full C programs with all statistics; 2,048 C programs over 256 keys (digest and 7 statistics) | `test_superscalar_tests_cpp_hashes`, `test_superscalar_programs_match_c`, `test_superscalar_more_keys`, `test_superscalar_all_golden_keys` (slow) |
| SuperscalarHash execution | C `executeSuperscalar` output, for the interpreter and for `to_python` | `test_superscalar_execution_matches_c` |
| Dataset items | tests.cpp word 0 of items 0, 10^7, 2·10^7, 3·10^7; 9 full C items and the 8-step trace of item 0; `reciprocalCache` and address registers; 1,006 items per official key against `randomx_init_dataset` | `test_dataset_items_match_c`, `test_dataset_item0_trace`, `test_dataset_programs_and_reciprocals`, `test_python_items_match_c_items` |
| §7.3 constants | Hash512("RandomX SuperScalarHash initialize") bytes 8-63 with the two documented tweaks (the spec checked against itself) | `test_dataset_constants_are_nothing_up_my_sleeve` |
| AES | FIPS-197 S-box values; C T-tables; 32 C rounds; the tests.cpp AesGenerator1R vector; C generator and AesHash1R outputs up to a full 2 MiB scratchpad; constant derivations | `test_sbox_fips197` … `test_full_scratchpad_fill_and_fingerprint` |
| Program generation and decoding | 6 C programs × 384 instructions: raw bytes, disassembly text, bytecode fields, configuration; v1 decoding equals the first 256 v2 ops | `test_program_generation_and_disassembly`, `test_decode_matches_c_bytecode`, `test_configuration_matches_c` |
| Single instructions | 816 C interpreter cases; all tests.cpp instruction and reciprocal tests; directed modes bracketing the exact rational result (more than 5,000 checked operations) | `test_instructions_match_c_interpreter`, `test_tests_cpp_*`, `test_float_modes_bracket_the_exact_result`, `test_float_edge_semantics`, `test_group_e_conversion` |
| Whole hash | The C trace after each of the 8 programs, for all 12 (vector, version) pairs, on full C Datasets | `test_official_vector_end_to_end` (slow), `test_first_program_matches_c` |
| Dynamic counters | Directed-mode FP counts equal the MPFR calls made; for vector 1a, per-program counts equal the C interpreter's (via progdump) | `test_dynamic_counters_match_the_executed_work`, `test_whole_hash_executes_like_the_library` (slow) |
| Whole light-mode chain, no RandomX C | Official hashes and commitment | `test_vector_1a_in_pure_python` (slow), `python3 -m rxlab.spec --vectors --fresh-cache` |

The single-instruction goldens are built to exercise IEEE edge cases:

- 36 overflows, all positive: 18 to +inf (9 under round-to-nearest, 9 under
  round-toward-positive) and 18 to +DBL_MAX (9 under round-toward-negative,
  9 under round-toward-zero);
- 12 inexact denormal results from gradual underflow in FMUL_R, 3 in each
  rounding mode;
- 32 exact zeros, which are −0 only under round-toward-negative;
- 16 CBRANCH cases (8 taken), 32 CFROUND cases (13 changed the mode under v1,
  6 under v2) and 16 ISTORE cases.

`test_instruction_goldens_cover_ieee_edges` asserts that denormal results
exist in every mode, that results of infinite and of DBL_MAX magnitude and −0
are present, and that −0 appears only under round-toward-negative. A
regeneration therefore cannot silently drop this coverage.

### Official vectors with no RandomX C code

"No RandomX C code" means that nothing from `librandomx` is loaded or called.
The chain still uses CPython's `hashlib.blake2b`, numpy and gmpy2/MPFR, none of
which is RandomX code. `rxlab.binding` is imported only inside
`CDatasetItemSource.__init__` and by the CLI's `--check`, so the light-mode
path never imports it.

`test_vector_1a_in_pure_python` runs the CLI entry point
(`rxlab.spec.__main__.main`) with `--vectors 1a --fresh-cache --json` in a
child Python process, with `RXLAB_LIB` pointing at a missing file, so that any attempt to load the library would raise. It then
asserts four things:

- the cache origin is `argon2d`, a fresh fill;
- the v1 and v2 hashes and the commitment are the official ones;
- `rxlab.binding` is not in `sys.modules`;
- no loaded shared object (dyld image list on macOS, `/proc/self/maps` on
  Linux) has "randomx" in its path.

The full run over every vector,
`python3 -m rxlab.spec --vectors --fresh-cache --json`, gives:

- `"reproduced"` = the 11 hash ids;
- `"all_match": true`;
- `"cache": "argon2d"` for all 3 keys.

That run uses the same code path but does not scan loaded images.

| Vector | Key | v1 | v2 |
|---|---|---|---|
| 1a | `"test key 000"` | `639183aa…` | `22ec6b86…` |
| 1b | `"test key 000"` | `300a0adb…` | `9e2c772c…` |
| 1c | `"test key 000"` | `c36d4ed4…` | `4d6b063a…` |
| 1d | `"test key 001"` | `e9ff4503…` | `97024134…` |
| 1e | `"test key 001"` | `c5641412…` | `c8e92c5f…` |
| 1f | 31-byte binary key | `78af2a18…` | no official constant; matches C `59a89257…` |
| commitment | Blake2b-256(1a input ‖ 1a v2 hash) | | `133be717…` |

The full values are in [`rxlab/spec/light.py`](../rxlab/spec/light.py). A
test checks that they agree with
[`tests/test_binding_vectors.py`](../tests/test_binding_vectors.py).

### Against the library's own program dump

[`tests/test_cross_program.py`](../tests/test_cross_program.py) compares the
spec generator and decoder with `build/progdump`
([`tools/progdump.cpp`](../tools/progdump.cpp); see
[Program structure](program-structure.md)). progdump replays
`randomx_calculate_hash` with the library's own generator, bytecode compiler
and interpreter. The comparison covers 128 programs: 8 inputs (vectors 1a-1c
and the 1e blob with nonces 0, 1, 2, 3 and 0xFFFFFFFF, all under
`"test key 000"`) × 8 programs × v1 and v2.

- **Raw bytes and features.** The raw bytes round-trip, the program sizes
  match, and 41 decoder-dependent columns match for every program: opcode
  counts, effective NOPs, the scratchpad level of every read and store,
  immediate-source cases, and CBRANCH body lengths.
- **Program 0.** Program 0 of every one of the 16 (input, version) pairs,
  generated in Python, is byte-identical to progdump's, including its FNV-1a
  fingerprint.
- **Dynamic counters (slow).** For vector 1a under both versions, the spec
  VM's executed slots, taken CBRANCHes, CBRANCH executions, CFROUND executions
  and rounding-mode changes equal the C interpreter's for each of the 8
  programs.
- **Mutation test.** Three decoder bugs were injected into
  `rxlab.spec.program`: L1/L2 masks swapped, `ISWAP_R r, r` decoded as a real
  swap, and CBRANCH target + 1. `test_decoder_matches_library_bytecode` caught
  all three.

### Version coverage

The dataset path is byte-identical in RandomX v1.2.1 and v2.0.1:

- `superscalar.*`, `superscalar_program.hpp`, `dataset.cpp`,
  `blake2_generator.cpp`, `argon2_core.c`, `argon2_ref.c` and `reciprocal.c`
  are unchanged;
- `configuration.h` differs only in program size;
- `dataset.hpp` differs only in `programs[8]` becoming `std::array`.

One cache and Dataset implementation therefore serves both versions. With the
v1.2.1 library forced through `RXLAB_LIB`, the fast cache tests give 14 passed
and 1 skipped. On the VM side, v2.0.1 masks `ma`/`mx` where they are used instead
of right after the XOR, which is equivalent for v1.

## Semantics that are easy to get wrong

### Cache side

The SuperscalarHash generator has to follow the C code exactly, quirks
included, or every later random draw shifts.

| Behaviour | Rule |
|---|---|
| Thrown-away instruction | `cycle` is **not** rewound: the C `cycle = topCycle` is commented out on that path, and the stall cycles stay counted |
| `SuperscalarInstruction::Null` | Zero-initialised; the Python `_Candidate()` mirrors it, both at the start and after an abort |
| IMULH_R / ISMULH_R group parameter | A **signed** 32-bit int taken from `getUInt32()` |
| ISUB_R | Shares IADD_RS's operation group for the "same operation twice" rule |
| IADD_C8/C9, IXOR_C8/C9 | Count 7 bytes of code size, not 8 or 9 |
| Address register | The **lowest**-index register with the maximum ASIC latency |
| r5 and IADD_RS | r5 cannot be IADD_RS's destination; if exactly two source candidates remain for IADD_RS and one is r5, r5 must be the source |
| IMUL_RCP | `generateSuperscalar` leaves the divisor in `imm32`; the C cache later rewrites it into an index into `reciprocalCache`. The Python keeps the divisor and exposes `.reciprocals` in the C order. |
| BlakeGenerator | A request that does not fit in the unused tail discards the tail and rehashes first; the seed is truncated to 60 bytes and followed by a 32-bit nonce |

### VM side

| Topic | Rule | Code |
|---|---|---|
| FSCAL_R mask | XOR with `0x80F0000000000000` flips the sign and exponent bits **0-3** (bits 52-55), not bits 4-7. For normal *x* the result is −*x*·2^*k* with *k* odd in [−15, 15]. As a bit operation it is exact in every rounding mode; zero maps to −2^−1008 and denormals become normal. | `fp.fscal` |
| gmpy2 rounding numbering | `fprc` 0-3 means nearest, toward −∞, toward +∞, toward zero (specs.md Table 4.3.1). gmpy2 numbers its modes RoundToNearest = 0, RoundToZero = 1, RoundUp = 2, RoundDown = 3. Indexing gmpy2 by `fprc` would silently swap modes 1 and 3 while mode 2 happened to work, so the table is built by name. | `fp._CONTEXTS` |
| Directed-mode edge cases | Overflow goes to ±inf or ±DBL_MAX depending on mode and sign. Gradual underflow is IEEE. An exact zero sum is −0 only under round-toward-negative. | `test_float_edge_semantics` |
| CFROUND | The rotation count is `imm32 & 63`. **v1:** `fprc = rotr(src, imm) & 3` on every execution. **v2:** `t = rotr(src, imm)`, and `fprc = t & 3` only if `t & 60 == 0` (probability 1/16). Python shifts do not truncate at 64 bits, so every rotation must be masked; CFROUND can skip the mask because only the low 6 bits are read. | `vm._statements` |
| Group-E conversion (§4.3.2) | int32 → double, keep the fraction and the low 4 exponent bits (`& (2^56 − 1)`), then OR the per-program mask. The sign is cleared, the exponent becomes `0b011ssssdddd`, the low 22 fraction bits come from the mask (an int32 leaves them zero), and the value lies in [2^−255, 2). Applies to E-register loads and to the FDIV_M divisor. | `fp.convert_e`, `test_group_e_conversion` |
| Denormals, FTZ/DAZ | The C library runs x86 with MXCSR `0x9FC0` (flush-to-zero and denormals-are-zero) and arm64 with plain `fesetround`, i.e. IEEE denormals. The Python VM follows IEEE, like arm64. The goldens were produced on arm64, so the denormal cases in `vm_instructions.json` are arm64-specific. Real hashes are unaffected: in vector 1a, all 3,013,878 (v1) and 4,520,980 (v2) scalar results of FADD/FSUB/FMUL/FDIV/FSQRT/FSCAL were normal numbers (no zero, denormal, infinity or NaN). | `fp.py` docstring |
| NaN bit patterns | F/E mixing (v1 XOR, v2 AES rounds keyed by E) can produce NaN bit patterns. The mixed result stays an integer bit pattern all the way to the scratchpad and the register file and never passes through a Python float. | `VirtualMachine._mix` |
| Program size | C fills `sizeof(Program)` = 3,200 bytes (384 slots) for both versions. The spec asks for `128 + 8 · RANDOMX_PROGRAM_SIZE`. Because the AesGenerator4R stream is sequential, the v1 prefix is identical either way; the Python generates the spec size. | `program.generate` |
| Iteration order | The Dataset read address uses `ma` before `mp` changes (`mp` is `mx` in v1 and `ma` in v2), then `mx` and `ma` swap. This order is what makes v2 prefetch two iterations ahead. | `VirtualMachine.execute` |
| Operand and NOP rules at decode | IMUL_RCP with a zero or power-of-two divisor is a NOP; C relabels the rest as IMUL_R by the reciprocal. `ISWAP_R r, r` is a NOP. With `src == dst`, an integer instruction's memory operand is `L3[imm]`. IADD_RS adds `imm32` only when `dst` is r5. ISTORE goes to L3 when `mod.cond ≥ 14`. The CBRANCH target is the last instruction that modified `dst`, after which every register's last-write mark moves to the branch. `cimm` sets bit *b* and clears bit *b*−1, with *b* = cond + 8. | `program.decode_instruction` |
| Byte order | The VM assumes a little-endian host (`memoryview.cast` on the scratchpad). | `vm.VirtualMachine` |

## Measurements

### SuperscalarHash program statistics

These are computed from the 2,048 C-generated programs in
`cache_superscalar_keys.json` (256 keys × 8 programs).

| Statistic | Value |
|---|---|
| Instructions per program | mean 447.17, sd 9.53, range 400-474 (the 512-instruction cap was never hit) |
| Reference-CPU latency | 171-174 cycles (mean 172.74) |
| ASIC latency (1-cycle ops, unlimited parallelism) | mean 92.81, range 79-108 |
| ASIC / CPU latency | mean 0.5373, range 0.457-0.624 |
| Multiplications | 155.9 per program (34.86% of instructions) |
| IPC on the 3 simulated ports | mean 3.104 macro-ops per cycle, range 3.02-3.22 |
| Instructions per Dataset item (8 programs) | 3,577.3 |

The ASIC/CPU ratio means an idealised ASIC finishes each program's dependency
chain in about half the reference CPU's cycles.

**Light-mode cost model.** This is arithmetic, not a measurement. A hash reads
16,384 items and so executes about 58.6 M SuperscalarHash instructions. At the
reference CPU's latency, that is about 22.6 M cycles per hash before any cache
misses. Fast mode replaces each item with one 64-byte read.

**Address-register bias.** The address register is skewed toward r0:

| Register | r0 | r1 | r2 | r3 | r4 | r5 | r6 | r7 |
|---|---|---|---|---|---|---|---|---|
| Programs | 410 | 373 | 278 | 258 | 221 | 160 | 195 | 153 |
| Share | 20.0% | 18.2% | 13.6% | 12.6% | 10.8% | 7.8% | 9.5% | 7.5% |

The cause is the lowest-index tie-break. In 869 programs (42.4%), two or more
registers tie at the maximum ASIC latency: 548 two-way, 226 three-way, 77
four-way, 17 five-way and 1 six-way. When the winner is unique, every register
wins 111-171 times. r5 is lowest, which fits its IADD_RS-destination ban. A
uniformly random tie-break would give each register 210-280 wins. The tie
counts can be reproduced with the C-exact Python generator:

```python
import collections
from rxlab.spec import superscalar
from rxlab.spec.blake2gen import BlakeGenerator
ties = collections.Counter()
for k in range(256):
    gen = BlakeGenerator(b"superscalar %d" % k)
    for _ in range(8):
        a = superscalar.generate(gen).asic_latencies
        ties[a.count(max(a))] += 1
```

**Opcode mix** of the 8 `"test key 000"` programs (3,563 instructions, from
`cache_superscalar.json`):

| Opcode | Share |
|---|---|
| IMUL_R | 21.8% |
| IROR_C | 11.3% |
| ISUB_R | 11.2% |
| IXOR_R | 11.2% |
| IADD_RS | 11.1% |
| IXOR_C (C7, C8 and C9 combined) | 10.9% |
| IADD_C (C7, C8 and C9 combined) | 9.2% |
| IMUL_RCP | 6.7% |
| ISMULH_R | 3.5% |
| IMULH_R | 3.3% |

This is close to the approximate frequencies in specs.md Table 6.1.1. The 8
programs hold 239 IMUL_RCP reciprocals, about 30 per program.

**Generator paths exercised.** An instrumented copy of `generate`, which is not
in the repository, ran the 2,048 programs and matched all 2,048 hashes. It
counted:

- 97,558 operand stall cycles;
- 6,087 thrown-away instructions;
- 7,330 destination selections with chained multiplication allowed (6,075 of
  them succeeded);
- 1,686 uses of the r5 source rule;
- **zero** aborts after 256 consecutive throw-aways.

### Dynamic instruction mix

The `stats=` counters were collected for **one input per version** (vector
1a). They describe single hashes, not a sample. For the rows progdump also
counts (instructions executed, CBRANCH taken and executed, CFROUND executed,
wrote and changed), the C interpreter's totals agree exactly; the "wrote
`fprc`" row comes from progdump. The FP rows come from the spec VM alone.

| | v1 | v2 |
|---|---|---|
| Instructions executed | 4,204,954 | 6,307,616 |
| Static slots (8 × 2048 × program size) | 4,194,304 | 6,291,456 |
| Extra from taken CBRANCH | +0.25% | +0.26% |
| CBRANCH taken / executed | 1,616 / 398,928 (0.41%) | 2,438 / 633,222 (0.39%) |
| Rounded FP instructions (each updates two doubles) | 1,408,337 | 2,075,713 |
| … executed in a directed mode (`fprc` 1-3) | 81.1% | 65.2% |
| CFROUND executed | 26,700 | 22,613 |
| CFROUND that wrote `fprc` | 26,700 | 1,433 (6.3%; 1/16 = 6.25% expected) |
| CFROUND that changed `fprc` | 19,907 (74.6%) | 1,058 (4.7%; 1/16 × 3/4 = 4.69% expected) |

- **Rounding-mode switches.** v2's CFROUND change cuts switches per CFROUND
  by 16×, but directed rounding still covers about two thirds of v2's FP work.
  The cost that RandomX's `design_v2.md` targets is the switch itself (MXCSR
  writes on x86), not directed-mode arithmetic.
- **Branch rate.** The taken rates match the 2^−8 chance that 8 condition
  bits are all zero: under a binomial model with n = CBRANCH executions, v1 is
  +1.5 sd and v2 is −0.7 sd from 1/256. Branch outcomes are not independent
  draws, so this is only a consistency check.
- **Workload ratio.** v2 executes 1.5× the instructions of v1.

To reproduce:

```python
import collections
from rxlab.spec.hasher import CDatasetItemSource, randomx_hash
with CDatasetItemSource(b"test key 000", lazy=True) as items:
    stats = collections.Counter()
    randomx_hash(b"This is a test", items, v2=False, stats=stats)
# stats["CFROUND"], stats["CFROUND changed"], stats["CBRANCH taken"], stats[("FMUL_R", 2)], ...
```

### Timings

**Every timing in this section was measured on a loaded machine; none is a
benchmark result.** All runs were on an arm64 macOS machine running other
jobs, with Python 3.9.6. The light-mode runs were also on battery power. Load
average was 6-8 during the full-vector run, and that process's CPU/wall ratio
was 0.996. Idle-machine timings have not been measured.

**Pure-Python light-mode hash.** These figures come from an earlier run of
`python3 -m rxlab.spec --vectors --fresh-cache --json`: 6 v1 and 5 v2 hashes,
with 16,384 items per hash. They are observations on a loaded machine, not a
benchmark result. The committed run, `results/spec_light.json`, was taken
under different load; FINDINGS.md quotes its figures.

| Phase | v1 | v2 |
|---|---|---|
| Dataset items | 3.99-4.52 s (0.243-0.276 ms/item) | 4.03-4.75 s (0.246-0.290 ms/item) |
| VM execution (includes compiling each program) | 1.02-1.23 s (median 1.08) | 1.86-2.29 s (median 1.97) |
| AesGenerator1R fill + program generation + AesHash1R/Blake2b | 0.30-0.39 s | 0.32-0.37 s |
| Whole hash | 5.42-6.10 s (median 5.73) | 6.21-7.42 s (median 6.52) |
| Items' share of the hash | 73-76% | 64-66% |

- **Per-key setup.** Fresh Argon2d fills took 29.3, 32.3 and 31.0 s wall
  (28.8, 32.2 and 30.9 s CPU), about 37-41 µs of CPU per block. The 1f v2
  check, run alongside other tests, took 38.3 s wall (38.0 s CPU). An earlier
  fill under heavier load took 52.3 s CPU (134.5 s wall).
  SuperscalarHash generation plus code generation took 0.04-0.06 s. The first
  hash of a new key therefore costs about 35-40 s, and each later hash about
  5.5-7.5 s.
- **Phase accounting.** The phases tile the hash: their sum equals the hash's
  wall time to 4 digits in every run.
- **v2 vs v1 VM time.** v2's VM time is about 1.8× v1's (medians 1.97 vs
  1.08 s). That is consistent with 1.5× more executed instructions plus v2's
  per-iteration AES mixing of the F registers. The split between the two has
  not been profiled.
- **Items dominate light mode.** Python items cost 0.243-0.290 ms each. In a
  spot check, the C library took 0.96 µs per item in a batch of 1024.

**Item code paths.** These are observations on a loaded machine, not a
benchmark result. The generated straight-line `item()` is 3-4× faster than the
interpreter-based `reference_item()`: 0.36-0.45 vs 1.35-1.47 ms per item on an
in-RAM cache, and 0.34 vs 1.24-1.35 ms per item on the C cache bytes, where the
two paths agreed on 200 random items.

**Memo page faults.** These are observations on a loaded machine, not a
benchmark result. `fill_cache` maps the memo lazily, so its page faults land
in the "Dataset items" phase. The first hash after a memo load spent
0.420 ms per item and ran on the CPU for 7.17 s of 8.78 s wall. The next hash,
with the pages resident, spent 0.316 ms per item at 0.99 CPU/wall. Timing runs
should use `--fresh-cache`.

**Full-Dataset VM runs.** These were measured on a loaded machine and are not
a benchmark result. Repeated runs of the slow end-to-end suite, with full C
Datasets, gave per-hash times that varied by up to 4× for the same hash.
Vector 1d v1, for example, took 1.36 s in one run and 5.54 s in another. On a
shared machine these runs cannot resolve differences between versions or code
changes.

## Running it

**Requirements.**

- Python ≥ 3.9, numpy and gmpy2 (see `pyproject.toml`). Nothing else is needed
  for light-mode hashes.
- The C library is needed only for `--check`, for the C-backed tests and for
  regenerating goldens. [`scripts/build_randomx.sh`](../scripts/build_randomx.sh)
  builds v1.2.1 and v2.0.1 under `third_party/`.

**Command line.**

```
python3 -m rxlab.spec --key "test key 000" --text "This is a test"          # 639183aa... (1a, v1)
python3 -m rxlab.spec --key "test key 000" --text "This is a test" --v2     # 22ec6b86... (1a, v2)
python3 -m rxlab.spec --key-hex 7797373e... --input 1010e1ea...             # binary key and input (as vector 1f)
python3 -m rxlab.spec --vectors 1a 1f                                       # selected official vectors
python3 -m rxlab.spec --vectors --fresh-cache --json > vectors.json         # every vector, no memo, no C
```

| Option | Effect |
|---|---|
| `--fresh-cache` | Fills the cache in memory with Argon2d instead of using the `data/cache` memo |
| `--check` | For a single hash, also computes the hash with the C library, which is loaded only then |
| `--json` | Prints a report instead of text: per-phase seconds, items built, CPU and wall time, the cache origin (`argon2d`, `argon2d, memoised`, `memo` or `given`), and `rxlab.sysinfo.describe()` |

The exit status is non-zero if any hash, or the commitment, differs from its
expected value. A single hash has an expected value only with `--check`.

**Python API.**

```python
from rxlab.spec.light import LightHasher
h = LightHasher(b"test key 000")          # data/cache memo; fresh=True fills in memory
h.hash(b"This is a test").hex()           # 639183aa...
h.timed_hash(b"This is a test", v2=True)  # {'hash': '22ec6b86...', 'seconds': {...}, 'items': 16384, ...}

from rxlab.spec import argon2d, dataset
words = argon2d.fill_cache(key)                   # 33,554,432 uint64, read-only, memoised
source = dataset.DatasetItemSource(words, key)    # also accepts raw cache bytes
source.item(i)                                    # tuple of 8 ints

from rxlab.spec.hasher import CDatasetItemSource, randomx_hash
with CDatasetItemSource(b"test key 000") as items:   # full 2080 MiB C Dataset
    randomx_hash(b"This is a test", items, v2=True, observer=None, stats=None)
```

`CDatasetItemSource(key, lazy=True)` allocates the Dataset but builds each item
only when asked, so a hash touches about 16k items instead of initialising
34 M. The fast tests use it. `randomx_hash(..., observer=f)` calls
`f(index, register_file, vm)` after each program. `stats=Counter()` compiles
in dynamic counters, and `timings={}` accumulates wall seconds per phase.

**Tests.**

```
pytest tests/test_spec_cache.py tests/test_spec_vm.py tests/test_spec_full.py \
       tests/test_cross_program.py -m "not slow"     # 49 tests
pytest <same files> -m slow                          # 19 tests
```

The 19 slow tests include the full Python cache fill, all 2,048 multi-key
SuperscalarHash programs, the 12 end-to-end traces and the pure-Python 1a
proof.

| File | Fast | Slow | With no C library (`RXLAB_LIB` pointing at a missing file) |
|---|---|---|---|
| [`test_spec_cache.py`](../tests/test_spec_cache.py) | 15 | 2 | fast tests with no memo either: 10 passed, 5 skipped |
| [`test_spec_vm.py`](../tests/test_spec_vm.py) | 24 | 12 | all 36: 21 passed, 15 skipped |
| [`test_spec_full.py`](../tests/test_spec_full.py) | 5 | 3 | all 15 tests of this file and the next, with progdump also unbuildable: 3 passed, 12 skipped, 0 errors; one of the passes is the slow pure-Python 1a proof |
| [`test_cross_program.py`](../tests/test_cross_program.py) | 5 | 2 | skips if progdump cannot be built |

Observed durations, measured on a loaded machine and not a benchmark result:

- the 49 fast tests: 12 s;
- `test_spec_vm.py -m slow`: 55-133 s;
- `test_spec_full.py` plus `test_cross_program.py`, `-m slow`: 56-73 s.

**Regenerating goldens.**

```
tools/golden/build_cache_golden.sh    # data/golden/cache_{argon2,superscalar,dataset,superscalar_keys}.json
tools/golden/build_vm_golden.sh       # data/golden/vm_{aes,programs,instructions,hash_trace}.json
```

Both scripts need `scripts/build_randomx.sh` to have been run first.
`cache_rfc9106.json` is transcribed from the RFC and is not rebuilt.

## Development notes

These are the notable bugs found while building and checking the
implementation, and how each was fixed.

**BlakeGenerator returned a stale byte after a rehash.**

- *Symptom:* the first SuperscalarHash port mismatched all 10 tests.cpp
  program hashes. The C harness's byte stream showed the difference at the
  first byte: 116 (`'t'`, the first byte of the unhashed seed) instead of 216.
- *Cause:* `return self.state[self._reserve(1)]` evaluates `self.state` before
  `_reserve` rehashes. Every `get_byte()` that triggered a rehash therefore
  read the old state. `get_uint32` was already correct.
- *Fix:* `get_byte` now reserves first and then reads. All 10 official hashes
  and all 8 full C programs then matched, every statistic included.

**A numpy integer index gave a silently wrong Dataset item.**

- *Symptom:* `DatasetItemSource.item(np.uint64(n))` returned a wrong item.
  `np.int64`, `np.uint32` and `np.arange` elements raised `OverflowError`
  instead.
- *Cause:* numpy scalars turn the register arithmetic into wrapping 64-bit
  math, where `(a * b) >> 64` is 0.
- *Fix:* both `item` and `reference_item` take `operator.index(number)`.
  `test_dataset_item_accepts_numpy_integers` checks these inputs against C
  item 1000.

**Reference cycles kept the 256 MiB cache alive.**

- *Symptom:* during the all-vectors run, the process RSS reached 587 MB and
  then 857 MB, where one 256 MiB cache at a time was expected.
- *Cause:* `dataset._compile` `exec`'d the item function into a namespace dict
  and returned `namespace["item"]`. The function's `__globals__` is that dict,
  and the dict contains the function. The pair, together with the cache array
  it references, survived until a cyclic GC pass. A weakref check confirmed
  this: the cache was not freed by refcount, but was freed after
  `gc.collect()`.
- *Fix:* `namespace.pop("item")`. `vm._define` had the same cycle, which held
  the scratchpad memoryviews, and got the same fix. With GC disabled, the
  cache is now freed by refcount.

**The memo file had a write race.**

- *Cause:* every process wrote the same `cache-….npy.partial` path, so two
  concurrent first fills of one key could truncate or rename away each other's
  file. This was found by reading the code, not by reproducing it.
- *Fix:* `fill_cache` writes to a unique `tempfile.mkstemp` name in the same
  directory, removes it on failure, and publishes with `os.replace`.
  `test_fill_cache_memo` covers the memo mechanics (write once, reload without
  refilling, no temporary file left, wrong-shape memo rejected).

**The instruction goldens did not test gradual underflow.**

- *Cause:* the denormal values in the first version of `vm_instructions.json`
  were 3 FSCAL_R outputs and 3 FSWAP_R moves, not results of rounded
  arithmetic. Underflow under directed rounding had been checked only against
  exact rational arithmetic.
- *Fix:* `vm_golden.cpp` now turns 4 of the 16 FMUL_R instructions into
  denormal-E × A-in-[1, 2) cases. It does this by masking bits, so the random
  stream and the other 800 cases are unchanged. C now produces 12 inexact
  denormal results (3 per rounding mode), and Python matches all of them.
- *Related:* the goldens also had no signed-zero results, so
  exact-cancellation FADD_R/FSUB_R cases were added.
  `test_instruction_goldens_cover_ieee_edges` keeps all of this coverage in
  place.

**Two tests had wrong expectations; the code was right.**

- A test assumed FSCAL_R's mask flips exponent bits 4-7. It flips bits 0-3,
  which is exactly the spec's "odd *k* from −15 to +15". The test, a
  docstring, and the golden generator's FSCAL edge inputs (exponent 15, not
  `0xF0`) were corrected.
- A port of the tests.cpp FADD_R case swapped the halves of `a1`, because
  `rx_set_vec_f128` takes the high half first.

**Running one instruction is not running a one-instruction program.**

- *Cause:* a taken CBRANCH in a one-instruction program would loop back onto
  itself.
- *Fix:* `vm.compile_instruction` returns a step function that reports whether
  the branch was taken.

**Smaller fixes.**

- **Golden harness.** `vm_golden.cpp` failed to compile because its accessor
  functions had the same names as the protected members they reach, so
  `&VmAccess::program` named the function itself. They are now `programOf`,
  `configOf` and so on.
- **Skips without the C library.** Two cases errored instead of skipping: the
  `c_cache` fixture did not catch the `NotImplementedError` raised by a v1.x
  library, which lacks `randomx_get_cache_memory`, and three VM tests raised
  `FileNotFoundError` when `third_party/` was not built. Both now skip, with a
  pointer to `scripts/build_randomx.sh`.
- **Test harness in `test_spec_full.py`.** The child process did not inherit
  the environment (`rxlab.sysinfo` calls `sysctl` in `/usr/sbin`). The item
  sample was seeded by the key's length, so `"test key 000"` and
  `"test key 001"` drew the same sample; it is now seeded by the key. A
  length assertion that would break if the shared vector table grew was
  removed.
- **Truncation guard.** `check_keys` asserts that the multi-key golden really
  holds `count × 8` rows before comparing them.
- **Import hygiene.** `hasher.py` imported `rxlab.binding` at module level.
  The import now lives inside `CDatasetItemSource.__init__`, so the light-mode
  path never imports the C wrapper.
- **CLI.** Non-printable keys such as 1f's are printed as hex instead of a
  bytes repr, and `--vectors` streams its text output instead of printing
  everything at the end.

**Argon2d performance.** A Python scalar operand roughly doubled the cost of a
numpy ufunc call on 32 elements in a microbenchmark: about 0.55 vs 0.28 µs,
measured on a loaded machine and not a benchmark result. An in-place ufunc on
overlapping views cost about 1.6 µs, because numpy copies the input. The
compressor therefore uses preallocated buffers, constant arrays for shift
counts and masks, module-level ufunc names with positional `out`, and one
gather per half-round. Two further tricks were measured and not taken, because
they would save roughly 10% at a cost in readability: pairing the two
low-32-bit masks into one 64-wide call, and byte-permuting the 16/24/32-bit
rotations.

## Known limitations and open questions

**Speed and timing**

- **Speed.** A light-mode hash takes seconds and each new key's cache takes
  tens of seconds. Idle-machine timings for the Argon2d fill, `item()`,
  `generate()` and the VM (v1 and v2) have not been measured.
  `python3 -m rxlab.spec --vectors --fresh-cache --json` produces exactly that
  report, machine description included.
- **Where VM time goes.** The split between AES, MPFR and the integer core has
  not been measured on an idle machine.
- **Faster items?** Python items cost about 0.25 ms each (measured on a loaded
  machine; not a benchmark result) and dominate light mode (64-76% of a hash). One option is to evaluate the 8 SuperscalarHash programs
  across many items at once with numpy. The VM consumes items one at a time,
  though, and the item numbers depend on the registers, so batching would need
  prefetching.
- **The lazy C item source is not a timing tool.** A spot check on a loaded
  machine (not a benchmark result) measured 7.9 µs per single-item
  `randomx_init_dataset` call on fresh pages and 5.6 µs on already-touched
  pages, against 0.96 µs per item in a batch of 1024. Per-call overhead, not
  page faults, dominates.

**Coverage gaps**

- **Unvalidated abort path.** The SuperscalarHash abort after 256 consecutive
  throw-aways (`MAX_THROWAWAY_COUNT`) is ported but not validated: 2,048
  programs produced zero aborts. A C search over many keys for a program that
  aborts would give a golden for it.
- **Argon2 v1.0.** The v1.0 branch (`version=0x10`, no XOR on later passes) is
  untested. RFC 9106 has only v1.3 vectors, and RandomX never uses v1.0.
- **Denormal goldens are arm64-specific.** On x86, RandomX runs with FTZ/DAZ.
  Regenerating `vm_instructions.json` there would change the 12 FMUL_R
  underflow cases and the FSCAL_R denormal cases, and the IEEE-modelling
  Python VM would then disagree with the regenerated file. Real hashes are
  unaffected as far as the instrumented vector-1a runs show.
- **Dynamic mix from one input.** The dynamic mix comes from one input per
  version. The 81% and 65% directed-mode shares should not be quoted as
  typical until `stats=` has been run over many inputs.
  `test_cross_program.py` likewise compares dynamic counters for one input per
  version.
- **In-test no-C proof covers only 1a.** Only vector 1a gets the child-process
  proof (missing library, module check, image scan). The other vectors rest on
  the full CLI run's JSON, committed as `results/spec_light.json` (all 11
  hashes match; regenerate with the command above). Checking them all in a
  test would cost two more fresh fills.
- **Python cache → Python items in the fast tests.** When the library is
  present, the fast Dataset tests feed the C cache bytes. The full Python
  cache → Python items chain is covered by the slow tests and by the digest
  check on the memo.
- **1f v2 has no official constant.** Its value is checked against the C
  library only.

**Operational caveats**

- **Memo trust.** `fill_cache` trusts a memo by file name and shape alone, so a
  memo written by an older, buggy build would still be served. Only the
  `"test key 000"` memo is checked against the C digest.
  `argon2d.CACHE_DIR` resolves from the source tree, so the memo location
  assumes a checkout.
- **Build-script platforms.** The golden build scripts have only been run on
  macOS arm64. The Linux x86-64 branches (for example `-pthread`, and
  `-maes -mssse3` for the VM harness) are untested.
- **Little-endian host.** The VM assumes a little-endian host.

**Open question**

- **Does the r0 bias matter?** It is untested whether the address-register
  bias toward r0 changes anything measurable, such as the distribution of the
  8 cache-line indices per item. All registers carry well-mixed values, so an
  effect is not expected.
