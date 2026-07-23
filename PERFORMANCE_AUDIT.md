# Brainstorm end-to-end performance audit

Date: 2026-07-22
Scope: in-game native and Lua seed search, exhaustive Seed Pool Builder,
multi-Ante and composite filters, `.bspool` storage, restricted pool search,
refilter/merge, and Organizer inspect/split/combine workflows.

## Outcome

The audit found three different bottleneck classes, so there is no single
"make searching faster" switch:

1. Unrestricted and Builder searches are compute-bound in exact Balatro RNG
   and route simulation.
2. Dense pool construction was additionally constrained by metadata memory,
   encoding, and serialized publication.
3. Existing-pool workflows were constrained by redundant metadata decoding,
   repeated file validation, and the size of schema-3 metadata.

The retained changes attack each class without changing accepted seeds,
occurrences, route provenance, checkpoint boundaries, or exhaustion semantics.
The highest-value results are:

| Rank | Retained improvement | Measured result | Primary beneficiary |
|---:|---|---|---|
| 1 | Exact Omen recovery and route pruning already established by the preceding native audit | 94.64 s to 2.79 s on the 1M-seed Omen fixture, **33.9x**, identical 31,346 accepts | Complex multi-Ante Omen/Charm searches |
| 2 | Move dense BSP3/BSP4 block preparation out of the writer lock and pipeline ordered blocks | 198,934 to 1,313,426 records/s, **6.60x**, with byte-identical deterministic BSP3 | Dense Builder scans |
| 3 | Adaptive BSP4 ranks/metadata plus 4,096-record publication blocks | Real 355,038,024-record pool: 1,471,452,792 to 631,165,420 bytes, **57.11% smaller** | Storage, transfer, cache, refilter, pool search |
| 4 | Reuse decoded occurrence descriptors during combine | 14.82 s to 5.45 s on a 65,536-record mixed BSP3/BSP4 union, **63.2% less time** | Composite/Boolean Organizer work |
| 5 | Cache validated split analysis and bounded ambiguity rules | 13.19 s to 7.43 s on a sparse BSP3 split and 32.37 s to 13.89 s on a multi-occurrence BSP4 split, **43.7–57.1% less time** | Location/category splits |
| 6 | Eight-lane joker stream priming | 172,352 to 242,044 seeds/s at one thread and 1,326,432 to 1,870,148 at nine threads, **40.4–41.0% faster** | Joker-first, deep multi-Ante unrestricted search |
| 7 | Linear-time Python Rice selection plus canonical-payload reuse | 1.941 s to 1.136 s on a 131,072-record, four-pattern BSP4 writer fixture, **41.5% less time**, byte-identical | Organizer split/combine publication |
| 8 | Compact buffered event hits | 1,296 to 48 bytes per ordinary buffered hit; 1,024-hit storage 1,327,104 to 49,176 bytes, **96.29% smaller**; dense-run RSS about 32.6 to 4.8 MiB | Low-memory Builder operation |
| 9 | Lazy physical-block validation for complete BSP4 indexes | Warm initialization improved **9x** for the production 1K conversion and **28x** for 4K; cold 4K bytes touched fell from about 631 MB to 4.85 MB | In-game/manual pool startup |
| 10 | Slotted Organizer indexes, 4K layout, and weighted reader/preflight caches | Slotted legacy reader peak **23.74% lower**; real lazy BSP4 open 0.850 to 0.192 s and RSS 166,150,144 to 53,592,064 bytes; controlled 64-input combine 107.13 to 37.76 s, **2.84x** | Pool inspection and composite operations |
| 11 | Voucher `Any A1–4/A1–8` early success | About **10% faster** on the A1–4 fixture; A1–8 gains grow when the requested voucher appears early | Voucher-heavy searches |

The earlier exact-search foundation remains important context. On the stored
2M-seed Perkeo/first-Soul A1–6 workload, the exact portable search rose from
roughly 310–319K seeds/s before that pass to about 857.7K seeds/s, while
retaining all 47,183 exact members. The old 752,643-seed/s canonical-only
implementation was not a valid speed target because it missed 4.152% of the
exact result.

## Architecture and bottleneck map

```text
Balatro UI
  ├─ native unrestricted search ──> exact gates ──> route simulation ──> hit
  ├─ Lua fallback workers ────────> same Lua oracle ───────────────────> hit
  └─ selected/attached pool ──────> BSP index/rank decode ─> active filters

Seed Pool Builder
  └─ seed enumeration ─> exact event evaluator ─> buffered hits
       └─ sort + adaptive rank/metadata encode ─> ordered atomic publication

Organizer
  ├─ inspect ─> bounded header/index/native summary
  ├─ split ───> one metadata traversal on a reusable reviewed plan
  └─ combine ─> streaming rank merge + occurrence/provenance union
```

Compute work dominates unrestricted rare searches. Rank I/O and decoding can
dominate pool searches whose active filters are cheap; complex active filters
can remain compute-bound. Metadata decoding dominates Organizer splits and
summaries. Keeping ranks and metadata independently checksummed in BSP4 is
therefore deliberate: a normal in-game restricted search reads and validates
the rank column without decompressing metadata it does not use.

## Search-engine findings

### Unrestricted native search

The common exact engine was already well optimized before this pass:
conservative selective gates reject candidates before expensive route state;
seed/key hashing shares suffix work across consecutive seeds; Soul event tapes
avoid replay; shop-pack and voucher streams are cached; Omen recovery uses
frontiers, dominance, memoization, prerequisite bounds, and one activation
timing trace.

The remaining profitable common choice was a joker-first path. Eight
consecutive candidates now have their independent shop joker streams primed in
an interleaved batch. It changes only when lazy hash state is calculated; each
candidate's draws and decisions remain scalar and in the original order. A
compile-time scalar reference is retained for differential tests.

This is especially useful for large multi-Ante searches because the same
configured shop streams recur across several Antes. It is not enabled for
unrelated filter families, where eagerly preparing those streams would waste
work.

### Voucher filters

`Any A1–4` and `Any A1–8` previously generated the complete voucher sequence
before testing it. Each Ante uses an independent stream, so the evaluator now
returns as soon as the requested voucher is observed. Exact-Ante behavior is
unchanged. This is a modest common-case win rather than a universal one:
misses still evaluate the full requested window.

### Lua fallback and in-game overhead

Lua fallback cancellation remains checked every 250 candidates. Only progress
publication changed:

- a worker publishes at most 10 progress samples/s plus a forced final sample;
- the message is `worker:count`, not a serialized table;
- the main thread parses digits instead of compiling every message with
  `load()`.

In a 100,000-message microbenchmark, encoding fell from 0.143 s to 0.053 s and
decoding from 0.479 s to 0.142 s. Real searches also send up to 20 times fewer
messages. This mostly protects frame time and weak CPUs; it does not inflate
the core seed/s number.

The native status file and visible search statistics are now sampled at 10 Hz
instead of once per rendered frame. Heartbeats use two seconds of monotonic
wall time rather than 120 frames, so 60 Hz, high-refresh, and temporarily
stalled games behave consistently. Worker completion signals the supervisor
immediately; the old fixed 200 ms terminal tail is no longer mandatory.

### Complex and uncommon filters

The audit did not add a general dynamic filter planner. A planner is useful
only when it can choose between independent predicates with materially
different measured cost/selectivity. Most current complex routes share state,
and changing their order can alter Balatro RNG consumption. The current
family-specific gates are faster and substantially easier to prove exact.

Likewise, survivor rebatching remains deferred. The representative Soul gate
leaves about 1.93% of candidates, too little work for a second batch handoff to
repay copying rank, seed, hash, stream, and prefix state. It should be
reconsidered only if a profile exposes a thicker survivor set plus another
independent selective predicate.

## Builder and publication findings

### Dense event buffering

The exact evaluator needs a large `PoolMetadata` scratch object while exploring
routes, but persisted matches usually contain only a few eight-byte
occurrences. Buffering the entire scratch object per hit made dense searches
memory-heavy.

Buffered hits now contain the rank, four inline occurrences, a count, and an
overflow offset. Only records with more than four occurrences allocate entries
in the owning run's overflow arena. The 48-byte layout is compile-time asserted
to prevent a silent regression.

### Encoding outside the publication lock

Sorting, occurrence normalization, adaptive codec selection, payload encoding,
and CRC calculation now happen before taking the writer mutex. Up to four
encoded blocks can be prepared concurrently; publication and logical-digest
updates remain strictly ordered. Encoder concurrency is deliberately
conservative on small machines: one encoder for one through four scan threads,
then one per four scanners, capped at four. Reserving encoders from the scan
thread budget was rejected because one scanner plus one encoder took 9.891 s
on the dense 2M fixture versus 4.943 s for two scanners plus an encoder.

This retained the exact BSP3 byte stream and SHA-256 digest in one-thread,
multi-thread, resume, shard, and merge tests. It is the largest newly measured
Builder throughput gain.

### Representative Quick Estimate

The old Quick Estimate used `format count`, which skipped occurrence capture,
block sorting, adaptive codecs, CRCs, and publication while presenting its
rate as projected build time. It also projected schema-4 size from a legacy
geometric delta heuristic.

Both Builder front ends now create a disposable BSP4 sample through the real
pipeline. Time projection uses final process wall time, disk projection uses
the measured final bytes per matched record when available, and the complete
temporary pool/state/manifest/criteria/lock set is removed after its result is
cached. Rare and zero-match samples retain explicit uncertainty instead of
inventing precise byte estimates.

On a one-thread 2M-seed `tag_rare` comparison, both modes found 1,037,917
matches. Count-only reported 1,087,129 seeds/s; the real disposable BSP4
pipeline reported 836,744 seeds/s and wrote 1,790,339 bytes. The old estimate
therefore overstated publication throughput by 29.92%.

## BSP4 storage design

Schema 4, `adaptive-events-v1`, keeps the self-describing 8 KiB pool header,
random-access block index, committed-prefix rules, writer locking, and atomic
publication model. Each 4,096-record block independently chooses the smallest
exact representation:

- ranks: positive deltas, complement deltas, bitmap, or Golomb–Rice gaps;
- descriptor membership: positive indexes, complement indexes, bitmap, or
  contiguous runs.

Existing 1,024-record BSP4 pools remain readable, and readers enforce an
8,192-record upper bound for forward-compatible block sizing. Schema-3
publication retains its original 1,024-record boundaries and deterministic
bytes.

Ties choose the lower codec ID and Rice is selected only when strictly smaller
than the other rank encodings, making output deterministic across thread counts
and implementations. Rank and metadata CRC64 values are separate. Membership
and metadata logical digests are calculated from canonical positive-delta
frames, so the identity is independent of the selected codec for a fixed
logical block layout. Block regrouping intentionally creates a new snapshot
identity.

The format was implemented in both the Python Organizer and native C scanner,
including fresh scan, resume, refilter, all-BSP4 merge, mixed BSP3/BSP4 merge,
restricted in-game search, export, summary, and non-destructive BSP3 upgrade.
The normal Builder and Organizer publication paths request BSP4; legacy
schemas remain readable.

### Full production-pool proof

The source was
`seed_pools/Perkeo-Charm-Tag-Complete-Pool.bspool`; it was never modified.
The final non-destructive converted artifact is
`/tmp/Brainstorm-Perkeo-Charm-Tag-Complete-Pool-BSP4-4K-Probe-20260722.bspool`.
The earlier 1,024-record BSP4 control is
`/tmp/Brainstorm-Perkeo-Charm-Tag-Complete-Pool-BSP4-20260722.bspool`.

| Property | BSP3 source | BSP4 4K upgrade |
|---|---:|---:|
| File bytes | 1,471,452,792 | 631,165,420 |
| Records | 355,038,024 | 355,038,024 |
| Charm occurrences | 355,038,024 | 355,038,024 |
| Perkeo outcome 2 | 353,974,779 | 353,974,779 |
| Perkeo outcome 3 | 1,063,245 | 1,063,245 |
| Schema-independent rank/metadata digest | `050e16bb8fdd178c` | same |
| Export SHA-256 | `cf7c739232d4698c56a44171fac8b214c72bbeedf347947162378a789b0eb284` | same |

Pool ID, family, segment, stage, lineage, derivation, criteria, provenance,
range, and catalog/model identity also match. Encoding- and block-frame-
dependent snapshot and logical digest fields changed as required. The 1K
control conversion took 297.86 s wall, 87.11 s user CPU, 14.29 s system CPU,
and about 21.6 MiB maximum RSS while the machine was under other audit load.

A real native restricted search opened the converted file, evaluated 45,583
pool members, and found a valid result in 1.57 s with about 24.3 MiB RSS. A
complete native traversal recomputed the 355,038,024-record membership digest
for each physical layout exactly as declared in its header.

The explicit audit command
`summarize POOL --record-digest` additionally binds every ascending rank to
its sorted raw occurrence descriptors without schema, codec, or block fields.
The BSP3 source, 1K BSP4 control, and 4K BSP4 output all produced
`050e16bb8fdd178c`. It is gated because calculating it on every ordinary UI
summary would regress that path: full source verification took 25.45 s and 4K
verification 24.33 s with about 7 MiB RSS, while the normal summary remains
about 11 s.

### Block-size result

The production pool contains 346,717 1K blocks. Their 64-byte headers and
56-byte index entries alone cost about 41.6 MB. Repacking the exact same
355,038,024 logical records into 86,680 4K blocks reduced the 1K BSP4 control
by another 45,789,049 bytes, or 6.764%:

- 31,204,440 bytes came from fewer block headers and index entries;
- adaptive metadata improved by another 14,659,002 bytes;
- Rice rank payloads grew by only 74,393 bytes.

All 86,680 consecutive real 4K groups were compared. Mean saving was 528.25
bytes/group, median 531, with p05 513 and p95 534; every eighth of the pool
saved 6.763–6.765%, so the result is not an isolated dense region.

Sequential native rank reads improved from 60.7M to 63.2M records/s at one
thread and 342M to 427M records/s at eight threads. Loaded index RSS fell from
about 22.1 to 7.7 MiB at one thread and 23.2 to 8.6 MiB at eight threads.
Per-worker decode scratch grows by only about 29 KiB.

The tradeoff is explicit: isolated uniformly random one-record lookups fell
from 54.3K to 15.2K lookups/s at one thread and 329K to 106K at eight threads.
Brainstorm workers claim sequential 16K-record ranges, so ordinary pool search,
export, refilter, and Organizer streaming benefit. The writer was not widened
to the reader's 8K safety cap because point latency and temporary buffers would
continue to grow.

### Alternatives measured

A 1,024-block evenly distributed production sample, covering 1,048,392
records, produced:

| Rank representation | Sample bytes | Relative to current positive deltas |
|---|---:|---:|
| Positive deltas | 2,109,089 | baseline |
| Elias–Fano estimate | 1,864,895 | −11.58% |
| Golomb–Rice | 1,807,492 | −14.30% |
| Per-block zstd level 1 | 2,119,329 | +0.49% |

Adaptive metadata on that sample fell from a projected 2,154,045 bytes to
68,316 bytes, a 96.83% reduction. Per-block zstd level 1 used 88,324 bytes,
larger than the selected specialized encodings. Compressing a whole raw 16 MiB
sample with zstd or LZ4 was competitive in aggregate, but would force
decompression of unrelated metadata, weaken bounded random reads, introduce a
runtime dependency, and complicate committed-prefix recovery. It was rejected
for the core format.

Elias–Fano remains a sound monotone-set representation, but Rice was smaller on
the real distribution and has a shorter exact bounded decoder. The adaptive
choice also handles dense spans with bitmaps and near-complete spans with
complements, cases where one global monotone codec is not ideal.

## Existing-pool and Organizer findings

### Native pool startup

A complete BSP4 index has hundreds of thousands of entries. Validating every
referenced block header with an individual `pread` caused 346,000-plus small
system calls on the 1K production pool. An interim 1 MiB-window validator cut
open time from 5.98 s to 2.33 s and instructions from about 1.67B to 0.475B,
but still read most of the payload region.

Complete pools now validate the checksummed footer/index, entry bounds, rank
ordering, and file layout at open, then compare the physical header and rank
CRC when each block is decoded. Summary, export, digest, and eventual search
exhaustion still validate every reached block. Incomplete/footer-free
checkpoints retain eager bounded physical-header discovery because they do not
have an authoritative final index.

Restricted-search claims are now the smaller of 16K records and a 4K-aligned
`records / (threads * 4)` target, never below 4K. Large pools keep the proven
16K locality; a 96,635-record/eight-worker pool exposes 24 claims instead of
six, allowing every worker to participate. Rotated one-record, 4K, and 16K
claim tests produced identical exhaustion digests.

### Split and ambiguity analysis

Split publication previously repeated a full source traversal after preflight.
Reviewed plans now carry an exact bounded preflight identity and can reuse the
validated destination mapping. Empty category choices skip seed conversion.
Ambiguity rules use a 4,096-entry LRU keyed by the exact candidate set, so
repeated multi-location cases do not rebuild the same decision.

The cache is deliberately bounded: it accelerates common repetition without
allowing a pathological pool to make memory proportional to the number of
ambiguous seeds.

### Combine

The streaming rank merge was already asymptotically appropriate. The avoidable
cost was decoding an occurrence descriptor and immediately reconstructing an
equivalent object for every source match. The combine normalizer now retains
decoded descriptor objects through the merge and only materializes the final
deduplicated record.

Union, intersection, difference, nested composites, Boolean snapshot
provenance, 64-way inputs, gap/overlap rejection, cancellation, and
no-overwrite publication remain part of the regression suite.

The complete 64-input, 524,288-record warm combine now takes 37.7608 s with
37.77 MB RSS, versus the original 107.13 s baseline. The immediately preceding
optimized 1K/BSP3 publication path took 50.7582 s, so linear Rice selection,
canonical reuse, slots, and 4K BSP4 publication removed another 25.60%. Output
fell from 3,014,224 to 1,874,016 bytes.

### Python BSP4 publication

The initial Rice parameter search rescanned every gap for each of 42 possible
parameters. It now matches the native writer's one gap pass plus 42-step
shifted-sum/popcount recurrence. The writer also reuses the positive rank and
metadata payloads already built during adaptive selection when calculating
logical digests, rather than rebuilding descriptor maps and canonical bytes.

On 131,072 records in 32 4K blocks with four descriptor patterns, median
publication fell from 1.941417 s to 1.135835 s, or 41.495%. All six compared
outputs were byte-identical at 239,232 bytes with SHA-256
`3b5d35574ad3a1613c792dcc36e1d1c64ba0da672af7bb8c9138890dd5245ae2`.

### Cache budget

Reader reuse is valuable because a verified large index is expensive to
rebuild, but a count-only cache could retain many production-scale indexes.
The retained-reader cache is therefore weighted and explicitly evicted at
64 MiB. Reviewed split plans remain separately capped at eight exact entries.
This bounds cached retention, not total process RSS: readers actively involved
in a combine must remain live, and legacy 1K pools have large Python block
indexes. Manual slots reduced a real 346,717-block BSP3 full-reader peak from
223,019,008 to 170,065,920 bytes, or 23.74%.

For structural opens that defer payload traversal, the real 1K BSP4 required
0.850 s and 166,150,144 bytes peak RSS; the 4K file required 0.192 s and
53,592,064 bytes. That is 4.42x faster and 67.75% less peak memory. The 4K
reader fits under the retained-cache budget; the legacy 1K reader is
immediately evicted. A packed/mapped legacy-index representation remains
worthwhile for simultaneous old-pool combines.

## External comparison and what was borrowed

- [Immolate](https://github.com/SpectralPack/Immolate) demonstrates the upside
  of compiling a fixed Balatro filter into an OpenCL kernel. That is attractive
  for optional high-end bulk searches, but not as Brainstorm's baseline:
  driver availability, GPU memory, dynamic route filters, metadata/provenance,
  and exact cross-platform behavior conflict with the low-end requirement.
- The [CPU Balatro Seed Finder](https://github.com/izanagi1995/balatro-seed-finder)
  confirms the value of multithreading, early pruning, and reducing strings.
  Brainstorm already uses fixed arrays, cached streams, exact family gates,
  resumable ranges, and a Lua oracle; importing its more allocation-heavy
  containers would not be a gain.
- [CRoaring](https://github.com/RoaringBitmap/CRoaring) validates the broader
  adaptive-container idea. BSP4 borrows the principle—choose an encoding by
  local density—but a full Roaring dependency is unnecessary for ordered
  4,096-record blocks and custom occurrence descriptors.
- [Apache Parquet's encodings](https://parquet.apache.org/docs/file-format/data-pages/encodings/)
  support the use of delta, run-length, and bit-packed representations by
  column. BSP4 applies that idea to exactly the two columns Brainstorm needs
  while retaining the existing pool identity and checkpoint model.
- [DuckDB's Parquet reader](https://duckdb.org/docs/current/data/parquet/overview)
  uses projection and filter pushdown. The analogous Brainstorm decision is
  rank-only reads for in-game search and metadata decoding only for Organizer
  operations.
- The [Zstandard seekable format](https://github.com/facebook/zstd/blob/dev/contrib/seekable_format/zstd_seekable_compression_format.md)
  shows how independent frames and a seek table can restore random access.
  Its frame/table overhead still lost to the measured specialized block
  codecs, so it was not adopted.
- [CCSDS lossless compression](https://ccsds.org/searchpubs/) and monotone
  sequence literature such as [Elias–Fano](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.CPM.2017.30)
  motivated evaluating low-complexity integer codes rather than general
  compression alone.
- [Clang's PGO documentation](https://clang.llvm.org/docs/UsersManual.html#profile-guided-optimization)
  reinforces that profiles are tied to instrumented workloads and matching
  binaries. macOS PGO remains optional; Windows PGO must be trained and
  measured on the intended Windows target before shipping.

No source code was copied from those projects. The retained implementations
were independently written against Brainstorm's exact scalar/Lua behavior.

## Rejected or deferred avenues

| Avenue | Decision | Reason |
|---|---|---|
| GPU/OpenCL as default | Rejected | Excellent fixed-filter high-end potential, poor low-end/driver baseline and substantial exact dynamic-filter duplication |
| Generic zstd/LZ4 pool payload | Rejected | Measured per-block loss to specialized codecs; whole-stream mode harms random access and recovery |
| Full CRoaring/Parquet/DuckDB dependency | Rejected | More format/runtime surface than the two Brainstorm columns require |
| Global Elias–Fano ranks | Rejected for BSP4 | Rice was smaller on the real sample; adaptive bitmap/complement also cover density extremes |
| General dynamic filter planner | Deferred | Current routes rarely offer an independent, safe, profitable choice |
| Survivor rebatching | Deferred | 1.93% survivor fixture does not repay state handoff |
| More portable SIMD/NEON | Rejected on profiled path | Earlier prototypes were neutral or slower; exact division/hash dependencies limit lane payoff |
| Larger refilter buffers | Rejected | Measured previously without a meaningful gain |
| Rank-only Difference RHS cursor | Deferred | Discarded RHS metadata is not needed for set membership, but skipping its decode would also skip the current canonical metadata-digest and composite-provenance validation. A fused native verifier/cursor can preserve that contract; a rushed Python shortcut cannot. |
| Buffer canonical metadata during native summary | Rejected | Runs make a second decode cheaper: full-pool wall time rose 24.72 to 26.54 s (+7.4%) and instructions rose 3.8% |
| Target-native Windows PGO | Deferred | Requires real Windows training, parity, and representative low-end measurement |
| Native rewrite of all Organizer transforms | Deferred | Python streaming paths are substantially faster, but active-reader index memory still merits a narrower packed/native verification path; a full rewrite has high semantic/provenance risk |

Earlier rejected native variants remain recorded in `BRAINSTORM_NOTES.md` and
`BLUEPRINT.md`, including wider lookup tables, alternate TW223 jumps, Soul
waves, eager voucher hashes, recursive/hash-deduplicated Omen search, static
tag-first ordering, alternate compiler/LTO flags, persistent worker barriers,
QoS/affinity changes, and metadata-free reachability prechecks.

## Correctness and validation

The retained changes are guarded by:

- 38 native/Lua equivalence configurations covering Joker, Soul, Charm,
  Ethereal, Omen, voucher, tag, malformed, and stale-config behavior;
- 14 joker-heavy lane-prime on/off/Lua differential cases;
- randomized BSP4 Python/C codec oracles, including malformed Rice parameter,
  truncation, trailing data, padding, checksum, cross-block ordering, and
  logical-digest cases;
- deterministic one-thread/eight-thread, resume, refilter, shard, all-BSP4
  merge, mixed BSP3/BSP4 merge, export, summary, and upgrade tests;
- pool attachment, variable-header, Organizer split/combine/web, native
  polling, Lua progress, bounded native filesystem, deferred-verification
  cancellation, and ordered-allocation-failure regressions;
- ASan/UBSan and targeted TSan runs;
- native macOS and Zig Windows cross-builds.

The converted production pool supplies the strongest storage proof: exact
record count, occurrence totals, schema-independent per-rank raw-metadata
digest, and textual export all match the source. A human Windows Balatro smoke
test is still required because CI cannot observe console flashes,
Defender/SmartScreen prompts, real Lovely loading, or in-game presentation.

## Remaining work ranked by expected value

1. **Human low-end Windows profile and smoke test.** Measure one through all
   useful cores, memory pressure, thermals, real Defender behavior, native pool
   startup, and deep voucher/Joker routes. This is the only way to validate the
   user's lowest-end target rather than extrapolating from an M1 Max.
2. **Pack or map legacy Python block indexes.** The 4K format solves this for
   new pools, but simultaneous Organizer operations on several large BSP3/1K
   pools still retain substantial active-reader memory. Prototype an array or
   mapped index and keep it only if traversal speed remains competitive.
3. **Fuse rank-only set cursors with native metadata verification.** Difference
   and some intersection inputs do not need materialized RHS descriptors, but
   the optimization must retain canonical metadata-digest and provenance
   validation before Python publication.
4. **Windows-native PGO experiment.** Retain only if an exact representative
   suite improves consistently enough to justify packaging and profile
   regeneration.
5. **Profile a genuinely thick complex-filter survivor workload.** Only then
   revisit survivor rebatching or a cost/selectivity planner.
6. **Consider optional GPU workers as a separate backend.** Limit the first
   experiment to an exact fixed Builder predicate and require native/Lua
   membership plus occurrence parity; never make it the compatibility
   baseline.
7. **Measure pool-selection policy with real hit latency.** Record density,
   rank codec cost, active-filter acceptance, coverage, and I/O before replacing
   the deterministic record-count heuristic.

## Benchmark environment and interpretation

Measurements in this audit were made on an Apple M1 Max, macOS 26.5.2, Apple
Clang 21.0.0, Python 3.9.6, and LuaJIT 2.1. Values from long production-pool
runs were sometimes collected while other audit jobs were active; paired
microbenchmarks were rerun clean where throughput ratios are reported.
Absolute rates will differ on low-end Windows hardware, but byte sizes,
digests, exact membership, algorithmic I/O counts, file-format bounds, and
explicit cache-retention caps are hardware-independent.
