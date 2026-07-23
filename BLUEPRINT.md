# Brainstorm Blueprint

This is the single source of truth for unfinished Brainstorm work. It is
ordered from highest to lowest priority. Completed and fully verified features
do not belong here; Git history and the main README describe what already
ships. When an item is completed, remove it instead of adding a completion log.

The governing rule for seed-search work is exactness first: an optimization or
pool shortcut may change execution order, but it must not change the seeds,
routes, metadata, or exhaustion semantics accepted by the established scalar
and Lua models.

## Review notes applied 2026-07-20

Two behaviors changed after the post-attachment review; future work should
build on these semantics rather than the ones they replaced.

1. **Deterministic Builder publication now parks finished chunks instead of
   blocking.** The in-order chunk protocol had workers idle in a publication
   convoy, measured at 6–14% on multi-thread builds. A finished chunk out of
   cursor order now deposits its worker-local run (capped ring, buffer
   recycling, `pool_chunk_publish_or_deposit`) and the worker continues
   scanning; whichever thread advances the cursor drains parked chunks in
   order. Byte output is unchanged — deposits publish in exactly the cursor
   order — and multi-thread pools remain byte-identical to single-thread.
   Measured: dense-build overhead cut from +14% to +7%, sparse builds back to
   the pre-determinism baseline; TSan-clean. Slot collisions beyond the
   64-entry ring are possible now that spans exceed worker count, so every
   writer wake is a broadcast; keep it that way.
2. **Automatic pool selection prefers an authoritative pool over a smaller
   accelerator**, matching ATTACHED_SEED_POOLS.md runtime selection rule 3.
   The prior smallest-first order optimized time-to-first-hit but let a
   definitive miss fall back from an exhausted accelerator into a full
   unrestricted scan; authoritative exhaustion ends the search instead.
   Records, pool id, and filename remain the tie-breaks, and
   `pool_attachment_matrix.lua` now pins the large-authoritative-over-small-
   accelerator case.

## Priority 0 — post-release platform validation

### 2. Perform a human in-game Windows smoke test

CI proves Windows Lua/native parity, CreateProcess quoting, status files,
CTRL_BREAK pause/resume, Builder headless behavior, and package layout. A real
Balatro session still needs to verify what CI cannot:

1. Ctrl+A starts and stops native searching without a console flash.
2. Status/configuration files appear and the in-game progress display updates.
3. The packaged Seed Pool Builder creates and resumes a pool.
4. Manual and automatic pool searches find valid seeds and show clean failure
   or exhaustion messages.
5. Defender/SmartScreen behavior and the documented unblock path are usable.

Record the Balatro, Lovely, Steamodded, Windows, and package versions used.

## Priority 1 — extend automatic pool attachment safely

Add one predicate family at a time. Each item requires an independent proof of
`active predicate => pool predicate`; similar names or overlapping samples are
not sufficient.

### 4. Voucher attachment matching

Model target windows, owned vouchers, purchase prerequisites, exclusions,
Ante reducers, and the difference between observing and purchasing a voucher.
Prove exact-Ante and ranged implications independently, including Omen routes.

### 5. Anywhere and broader-source attachment matching

Add dedicated semantics and differentials for:

- Legendary-anywhere;
- tag-anywhere;
- broader Legendary sources such as `any` satisfying a requested `charm`
  source;
- contained versus wider location windows;
- Charm, Ethereal, Omen, and Charm+Omen route coverage.

Chronological route state must remain authoritative. Do not infer compatibility
from item identity alone.

### 6. Composite Boolean attachment matching

Represent union, intersection, and difference provenance as Boolean predicates
and prove implication against that expression. Do not flatten a composite into
a single conjunction. Refuse automatic use whenever the expression or lineage
cannot be translated exactly.

### 7. Generate canonical predicates from one schema

The Builder and Lua runtime currently reconstruct compatible representations
in different languages. Define one versioned predicate-field specification for
item, count, location, source, Negative/edition, Soul depth, route coverage,
voucher ownership/purchase, exclusions, and Boolean composition. Generate or
validate both implementations from it so new fields cannot silently drift.

### 8. Measure and improve pool choice

The current deterministic choice prefers authoritative coverage, then the
fewest records within the same role. Benchmark real time-to-result using record
count, coverage, density, decode cost, active-filter acceptance, and storage
behavior. Change the within-role heuristic only if those measurements beat the
simpler rule while preserving deterministic tie-breaks and authoritative-first
exhaustion.

### 9. Measure accelerator revisit cost

Unrestricted fallback may revisit ranks that were already members of an
accelerator. This is correct. Add native exclusion state only if measured
workloads show a meaningful time cost and the state can be bounded without
changing checkpoint/resume semantics.

## Priority 2 — performance research

### 10. Profile the final pipeline on intended low-end Windows hardware

The 2026-07-22 end-to-end audit reprofiled the macOS portable paths, exact
Perkeo/Omen/voucher/Joker workloads, dense and sparse Builder publication,
manual BSP4 search, and Organizer split/combine behavior. Its retained and
rejected results are in `PERFORMANCE_AUDIT.md`.

The remaining evidence must come from representative low-end Windows systems,
not extrapolation from an M1 Max. Measure exact Perkeo/first-Soul A1–6, full
Omen/Charm recovery, voucher-heavy DFS, joker-first multi-Ante search,
tag/Legendary Builder scans, and automatic/manual BSP4 search from one thread
through all profitable core counts. Record end-to-end rate, CPU time by stage,
scaling, cache behavior, memory pressure, I/O, checkpoint tail time, thermal
variance, and UI responsiveness. Use the corrected family-specific benchmark
path and keep result membership nonempty so measurements are not vacuous.

### 11. Pack or map legacy Organizer indexes

New 4K BSP4 pools reduce index count substantially, but simultaneous Organizer
operations on several large BSP3 or 1K BSP4 inputs still retain one Python
object per block for every active reader. Prototype a compact typed-array,
packed, or read-only mapped index. Benchmark open time, ordered iteration,
random block lookup, combine throughput, and peak RSS; retain it only if the
memory reduction does not materially regress streaming transforms.

### 12. Fuse rank-only set cursors with native metadata verification

Difference and some intersection inputs need RHS ranks for set membership but
do not need materialized RHS occurrence descriptors. A shortcut must still
validate canonical metadata digests, composite provenance, and corruption
before publishing output. Design a native verifier/rank cursor that preserves
that contract, then measure it against the current Python metadata traversal.

### 13. Evaluate target-native Windows PGO

Train Windows search and Builder profiles on Windows, rebuild with matching
identity metadata, run full Lua/native parity, and compare ordinary plus PGO
rates. macOS profiles are neither portable nor evidence of a Windows gain.
Adopt Windows PGO only if the packaged complexity produces a repeatable
improvement on representative hardware.

### 14. Benchmark searcher survivor rebatching only on a suitable workload

The current Soul gate leaves about 1.93% of the bounded fixture for scalar
evaluation, so a second staged Legendary gate has little room to help. Revisit
only if profiling finds a useful plan with a thicker survivor set or a second
very selective independent predicate. Carry rank, seed, hashed seed, first
stream result/state, and cached hash-prefix state; preserve FIFO order and flush
partial batches explicitly.

### 15. Add a cost/selectivity planner only when it has a real choice

A planner should choose only among predicates proven independent, using static
RNG probability plus measured rejection cost. The selected plan must remain
deterministic and observable in telemetry/pool identity where relevant. Current
common-case ordering is already close to measured selectivity, so this waits
for another profitable independent stage.

### 16. Revisit portable SIMD only after profiling

If profiles expose a new lane-parallel hotspot, compare portable ILV code,
compiler vectorization, Apple NEON, and Windows AVX2/AVX-512 separately. Retain
the scalar exact oracle, disable/reference switches, exact floating-point
operation order, and cross-platform LuaJIT parity. Do not translate another
project's fixed `Vector512` code literally.

## Priority 3 — product and UX additions

### 17. Delete paused and incomplete pools safely

Extend the Builder's completed-pool deletion workflow to paused, resumable,
and non-resumable incomplete pools. Refuse deletion while any Builder,
Organizer, refilter, merge, or native writer has the pool open as an input or
output. Bind confirmation to the pool identity plus its current checkpoint and
file identities so a stale browser action cannot delete a scan that resumed in
the meantime. Clearly distinguish deleting recoverable scan progress from
removing an unusable partial artifact; enumerate the `.bspool`, `.state`,
`.manifest`, `.criteria.cfg`, and applicable temporary/attachment sidecars,
remove the main pool last, and preserve the reusable writer-lock inode. Test
paused fresh scans, refilters, distributed parts, missing or damaged sidecars,
stale confirmations, concurrent writers, and Windows file-lock behavior.

### 18. Show where a found joker was located

The search already records locations. Add a concise result notification such
as “Found in Shop,” “Found in Pack,” or the exact route label, including when a
seed is banked rather than immediately applied. Avoid overlapping the existing
seed-slot and search-progress messages.

### 19. Optional scoring and ranked results

Add `should`-style criteria only as a separate result-ranking feature. It must
not weaken mandatory predicates or be presented as a generation-speed gain.
Define deterministic scoring, tie-breaking, retention limits, and export.

### 20. Richer native survivor metadata

Consider returning route/location metadata with survivors to avoid a second
native analysis pass. Main-thread Lua verification remains the final authority
before a seed is applied.

### 21. Managed remote search

Consider a remote worker abstraction only after local shards, checkpoints,
merge publication, and Organizer workflows are dependable. Preserve explicit
ranges, identities, resumability, provenance, and independent result
verification.

### 22. General typed filter authoring

A JAML-style must/should/must-not and Boolean editor could expand the product,
but it risks duplicating route semantics. Do not begin it before canonical
attachment predicates and composite pool semantics are stable.

## Priority 4 — repository maintenance

### 23. Remove obsolete workspace artifacts

After confirming they contain no unique user work, remove the old `.bak` files
and prune the stale distributed-worktree registration. Keep this separate from
feature commits so recovery data is never deleted accidentally.

## Acceptance contract for future changes

Every search-semantic or performance change must, as applicable:

- compare against the scalar/native reference and production Lua model;
- preserve accepted ranks, metadata, membership digest semantics, and refilter
  results over controlled and randomized cases;
- test plain and FMA modes, `round13`, PRNG, hash/odometer carry and wrap,
  culled/resampled picker boundaries, and missing/duplicate modded keys;
- cover Charm, Omen, Charm+Omen, voucher routes, Soul depth, tag rewards,
  Negative Legendaries, shop/pack skip topology, and expanded seed spaces;
- cover full scans, partial checkpoints, resume, refilter, compression,
  conversion, shards, merge, attachment, and restricted search when affected;
- run ASan/UBSan and TSan where the modified language/runtime supports them;
- build and pass the real Lua oracle on macOS and Windows;
- preserve a removable reference switch until the measured gain and exactness
  proof justify committing to the new path;
- reject an optimization that does not beat its state copying, synchronization,
  maintenance, and verification cost.

## Measured and rejected ideas

Do not retry these without a materially different profile, algorithm, or
hardware target. They were prototyped and measured as slower, neutral, below
the keep threshold, or too semantics-heavy for the gain:

- any-window voucher first gate and tag-anywhere first gate (about 0.88–0.93x);
- separate metadata-free Builder reachability precheck (about 0.7–3.5% gain
  while duplicating route semantics);
- explicit NEON implementation of the previously profiled exact path;
- wider picker lookup tables and alternative TW223 jump formulations;
- Soul wave/batch evaluation on the tested workloads;
- eager or interleaved voucher hashing;
- recursive or hash-deduplicated Omen search variants;
- static tag-first ordering;
- exact culled-tag resample replay in the derived Charm gate (54.4M seeds/s
  versus 56.0M/s for the simpler conservative gate on the M1 Max workload);
- alternate FMA/cold `round13` variants;
- split fast/general evaluator paths;
- tested alternate compiler/LTO flag combinations;
- persistent worker barriers for checkpoint epochs;
- worker QoS and affinity changes;
- larger refilter buffers;
- OpenCL/GPU as the required baseline: retain only as a possible optional
  fixed-filter backend because driver availability, exact dynamic routes, and
  low-end compatibility do not match the core product;
- per-block or whole-stream zstd/LZ4 pool payloads: measured specialized BSP4
  rank/metadata codecs were smaller per block and preserve cheaper bounded
  random access and committed-prefix recovery;
- full CRoaring, Parquet, or DuckDB dependencies for the two pool columns;
- global Elias–Fano ranks for BSP4: Golomb–Rice was smaller on the sampled
  production distribution, while adaptive bitmap/complement codecs handle
  density extremes;
- reserving Builder scan threads for publication encoders: total-budget
  variants regressed the dense low-core fixture by 31–100%, while one encoder
  kept pace with two through four scanners;
- retaining canonical metadata bytes during native summary to avoid its second
  run decode: full-pool wall time increased 7.4% and instructions increased
  3.8%.

## Guardrails for external ideas

Do not translate another project's fixed C# `Vector512` implementation
literally. It is not portable across Brainstorm's targets or exact
floating-point modes. Architectural ideas may be independently reimplemented
and differentially tested, but source from GPLv3 projects must not be copied
into this MPL 2.0 codebase without an explicit compatibility review.
