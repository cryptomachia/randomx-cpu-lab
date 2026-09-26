# XMRig benchmark rigs (`rigs.csv`)

One row per **physical rig** built from the public XMRig benchmark database
(api.xmrig.com), for RandomX `rx/0`. It is the input of
`experiments/hardware_model.py`; the raw API responses are cached (gzipped,
git-ignored) under `data/cache/xmrig/` and are not committed.

Rebuild (about an hour of polite crawling the first time, under a minute from the cache):

```
python3 experiments/hardware_model.py --rebuild   # crawl what is not cached, rewrite rigs.csv, model
python3 experiments/hardware_model.py             # replay the cache if present, else use rigs.csv as is
```

Without a cache the model runs from `rigs.csv` alone, except that the
headline comparison and the Apple table read the index and the Apple brand
lists (fetched and cached on first use); `crawl_counts` is then `null`.

## Provenance

| step | endpoint | notes |
|---|---|---|
| index | `GET /1/benchmarks?algo=rx/0` | one row per CPU brand string: `count`, `size` (total hashes over all runs), `hashrate` = the **maximum** submission. The leaderboard page https://xmrig.com/benchmark is rendered from this. |
| brand list | `GET /1/benchmarks?algo=rx/0&cpu=<brand>` | every submission of that brand (no paging), compact: id, size, status, threads, hashrate, os, version, `cpu.packages`. Only brands whose index `size` exceeds `count` x 1M are listed (the rest have only 1M runs). |
| submission | `GET /1/benchmark/<id>` | full hwloc CPU info (cores, threads, L2, L3, family/model, packages), SMBIOS `dmi` (board, DIMMs with type/speed/product/bank), backend (huge pages, MSR mod), per-thread hashrates. |

Crawler: `rxlab/analysis/leaderboard.py` (`crawl`, `build`), at most 4 concurrent
requests, retries with exponential backoff, every response cached. The
experiment additionally fetches the brand lists of every `Apple M*` brand for
its Apple table (all run sizes).

Snapshot committed here: crawled 2026-09-26 (`crawl_counts` in
`results/hardware_model.json`):

| stage | count |
|---|---|
| submissions in the index / brand strings | 89,776 / 2,334 |
| brands with any run longer than 1M (lists fetched) | 1,184 |
| submissions in those lists | 85,190 |
| rx/0, finished, size >= 10M | 12,145 |
| ... and `packages == 1` (submissions fetched) | 9,604 (0 missing) |
| rigs in `rigs.csv` | 5,726 (5,511 dmi, 11 user, 204 anon) |

## Filters

1. `algo == "rx/0"`, `status == 3` (finished), `size >= 10,000,000` hashes.
   1M runs last ~12 s and inflate numbers; 10M runs are still only minutes of
   burst, not sustained operation.
2. `cpu.packages == 1`. Multi-socket rows are whole systems (~2x); VMs that
   present every vCPU as a socket also drop out here.
3. Grouping into rigs (below); the rig hashrate is the **median** of its runs.
4. Downstream only (not applied to this file): `cpu_table()` keeps, per brand,
   the rigs whose `(cores, L3)` equals the brand's most common topology, which
   removes VM slices and spoofed/misreported brand strings, then takes the
   median of rig medians per CPU.

## Rig identity

* `basis = dmi`: SMBIOS present -> key = (cpu brand, cores, os, board product,
  sorted set of DIMM part numbers, DIMM count). Identical off-the-shelf
  machines owned by different people merge into one rig; that is deliberate
  (it keeps a popular prebuilt from outvoting everything else).
* `basis = user`: no SMBIOS but a submitter name -> one rig per name. The name
  itself is not stored; `rig_id` is a truncated SHA-1 of the key.
* `basis = anon`: no SMBIOS, no name -> all such runs of one (cpu, cores, os)
  pool into a single pseudo-rig. macOS runs never carry SMBIOS, so Apple rows
  are `user` or `anon` rigs.

## Columns

| column | meaning |
|---|---|
| `rig_id` | first 12 hex of SHA-1 of the rig key |
| `basis` | `dmi`, `user` or `anon` (see above) |
| `cpu` | brand string, whitespace-normalised |
| `vendor`, `arch` | hwloc vendor (`amd`, `intel`, `other`...) and `x86_64` / `aarch64` |
| `family`, `model` | CPUID family / model (decimal); used for the microarchitecture label |
| `cores`, `threads` | physical cores and logical CPUs reported by hwloc |
| `l2_mib`, `l3_mib` | total L2 and L3 in MiB (Apple: L3 = 0, L2 = sum over clusters) |
| `nodes` | NUMA nodes |
| `os` | `windows`, `linux`, `macos`, ... |
| `board` | SMBIOS board product (empty without SMBIOS) |
| `dimms` | populated DIMMs |
| `channels` | distinct 64-bit memory channels parsed from DIMM bank/slot labels; empty when any DIMM's label has no channel information, and for LPDDR (its 16/32-bit channels are not comparable) |
| `mem_type` | most common DIMM type (DDR4, DDR5, ...) |
| `mem_mts` | slowest populated DIMM's reported speed, MT/s; empty when it is below the lowest JEDEC grade of its type (DDR2 400, DDR3 800, DDR4 1600, DDR5 3200), because such a value is the I/O clock, not the transfer rate. A clock that happens to be >= that floor (e.g. DDR4-3200 reported as 1600) cannot be detected. |
| `mem_gib` | total populated memory, GiB |
| `n` | runs merged into this rig |
| `threads_used` | most common benchmark thread count |
| `hashrate`, `hashrate_min`, `hashrate_max` | median / min / max H/s over the rig's runs |
| `hugepages_frac` | median fraction of huge-page allocations that succeeded |
| `msr` | XMRig MSR mod applied (most common value) |
| `asm` | XMRig assembly variant (`ryzen`, `intel`, `none`, ...) |
| `version` | most common XMRig version |
| `year_first`, `year_last` | UTC years of the first and last run |

Units: hashrates in H/s for the whole rig at `threads_used` threads.
