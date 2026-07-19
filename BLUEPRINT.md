# Brainstorm Blueprint

This is the single source of truth for unfinished Brainstorm work. It is
ordered from highest to lowest priority. Completed and fully verified features
do not belong here; Git history and the main README describe what already
ships. When an item is completed, remove it instead of adding a completion log.

The governing rule for seed-search work is exactness first: an optimization or
pool shortcut may change execution order, but it must not change the seeds,
routes, metadata, or exhaustion semantics accepted by the established scalar
and Lua models.

## Priority 0 — correctness and release readiness

### 1. Prove automatic attachment through one real end-to-end path

The current tests separately exercise Lua attachment selection/status
transitions and native pool hit/exhaustion behavior. Add one harness that starts
with real `.attached` discovery, launches the real native child, consumes its
status protocol, and observes the final Lua continuation decision.

Cover on both macOS and Windows:

- a compatible accelerator that finds a seed;
- smallest-pool exhaustion followed by the next compatible attachment;
- final accelerator exhaustion followed by unrestricted generation;
- a compatible authoritative pool whose exhaustion is definitive;
- missing, unreadable, corrupt, changed, stale-ID, stale-catalog, and
  stale-profile pools;
- marker or pool mutation immediately before authoritative exhaustion is
  accepted;
- manual selection taking precedence and retaining hard-error behavior;
- a fallback search finding a valid result outside the accelerator.

Acceptance requires no false exhaustion, no silent broadening of a manual
search, stable user-visible telemetry, and the same result when the path is
replayed on Windows and macOS.

### 2. Complete the attachment implication proof matrix

Differentially compare automatic-pool and unrestricted membership for every
attachment relationship that the runtime currently accepts:

- every accepted Ante and blind window;
- Negative and non-Negative requests;
- each accepted Soul depth;
- Full Exhaustive versus Fast Exact route relationships;
- wider pool windows satisfying contained active windows;
- active joker/pack predicates layered over a broader pool;
- deterministic choice and chaining across several compatible pools.

Mutate every canonical signature field independently. Any relationship without
an explicit implication proof must refuse automatic use and fall back. Replace
bounded-sampling language with structural or exhaustive finite-domain proofs
where feasible; retain large randomized/bounded differentials as regression
evidence, not as the logical proof itself.

### 3. Make multithreaded pool artifacts deterministic

Identical multithreaded scans currently contain the same seed set but can emit
records in different orders, producing different `membership_digest` values.
Choose and benchmark one exact solution:

- publish completed chunks in rank order;
- perform an ordered final merge; or
- define an order-independent canonical membership identity while preserving a
  separate integrity check for the physical record stream.

Verify identical membership and metadata identities across thread counts,
pause/resume, checkpoint boundaries, refiltering, compression, distributed
shards, and merged outputs. Do not trade away enough throughput to make the
identity improvement more costly than its reproducibility value.

### 4. Redesign Organize / Combine

Treat this as a workflow and semantic redesign, not a cosmetic pass. The final
interface must make source pools, compatibility, ambiguity, unmatched seeds,
chosen operations, provenance, and output publication obvious before writing.

Re-test end to end:

- inspection and compatibility diagnostics;
- category/location splitting;
- seeds occurring in several categories;
- unmatched-seed keep, omit, and remainder policies;
- union, intersection, and difference;
- distributed-part merging and gap/overlap rejection;
- previously combined and composite inputs;
- lineage and Boolean provenance;
- writer locking, cancellation, and atomic output publication;
- the embedded Builder view and standalone Organizer on both platforms.

### 5. Perform a human in-game Windows smoke test

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

### 6. Publish a current Windows release

The newest Windows tag predates the attachment and audited vector-gate work.
After priorities 1–5 are either complete or explicitly accepted as nonblocking:

- create a new `win-v*` tag from the intended release commit;
- let CI build the full and incremental packages;
- verify both release archives and their checksums;
- install the full archive into a clean mod directory;
- upgrade an older installation and confirm pools, checkpoints, manifests,
  `.attached` markers, settings, and user state are preserved;
- confirm the published commit and release notes match the tested source.

## Priority 1 — extend automatic pool attachment safely

Add one predicate family at a time. Each item requires an independent proof of
`active predicate => pool predicate`; similar names or overlapping samples are
not sufficient.

### 7. Voucher attachment matching

Model target windows, owned vouchers, purchase prerequisites, exclusions,
Ante reducers, and the difference between observing and purchasing a voucher.
Prove exact-Ante and ranged implications independently, including Omen routes.

### 8. Anywhere and broader-source attachment matching

Add dedicated semantics and differentials for:

- Legendary-anywhere;
- tag-anywhere;
- broader Legendary sources such as `any` satisfying a requested `charm`
  source;
- contained versus wider location windows;
- Charm, Ethereal, Omen, and Charm+Omen route coverage.

Chronological route state must remain authoritative. Do not infer compatibility
from item identity alone.

### 9. Composite Boolean attachment matching

Represent union, intersection, and difference provenance as Boolean predicates
and prove implication against that expression. Do not flatten a composite into
a single conjunction. Refuse automatic use whenever the expression or lineage
cannot be translated exactly.

### 10. Generate canonical predicates from one schema

The Builder and Lua runtime currently reconstruct compatible representations
in different languages. Define one versioned predicate-field specification for
item, count, location, source, Negative/edition, Soul depth, route coverage,
voucher ownership/purchase, exclusions, and Boolean composition. Generate or
validate both implementations from it so new fields cannot silently drift.

### 11. Measure and improve pool choice

The current deterministic choice favors the fewest records. Benchmark real
time-to-result using record count, coverage, density, decode cost, active-filter
acceptance, and storage behavior. Change the heuristic only if those
measurements beat the simpler rule while preserving deterministic tie-breaks.

### 12. Measure accelerator revisit cost

Unrestricted fallback may revisit ranks that were already members of an
accelerator. This is correct. Add native exclusion state only if measured
workloads show a meaningful time cost and the state can be bounded without
changing checkpoint/resume semantics.

## Priority 2 — performance research

### 13. Reprofile the final pipeline before changing it

The recent exact-route, vector-gate, pack-gate, and handoff work changed the
hotspots. Capture representative profiles for:

- exact Perkeo/first-Soul Antes 1–6;
- full Omen/Charm recovery;
- voucher-heavy DFS routes;
- tag-only and Legendary-plus-tag Builder scans;
- unrestricted pack, tag, voucher, Soul, and Legendary searches;
- automatic and manual `.bspool` searches;
- one thread through all profitable core counts;
- macOS portable/PGO and Windows portable builds.

Record end-to-end rate, CPU time by stage, scaling, cache behavior, memory, I/O,
checkpoint tail time, and thermal variance. Use the corrected family-specific
benchmark path and keep result membership nonempty so measurements are not
vacuous.

### 14. Evaluate target-native Windows PGO

Train Windows search and Builder profiles on Windows, rebuild with matching
identity metadata, run full Lua/native parity, and compare ordinary plus PGO
rates. macOS profiles are neither portable nor evidence of a Windows gain.
Adopt Windows PGO only if the packaged complexity produces a repeatable
improvement on representative hardware.

### 15. Benchmark searcher survivor rebatching only on a suitable workload

The current Soul gate leaves about 1.93% of the bounded fixture for scalar
evaluation, so a second staged Legendary gate has little room to help. Revisit
only if profiling finds a useful plan with a thicker survivor set or a second
very selective independent predicate. Carry rank, seed, hashed seed, first
stream result/state, and cached hash-prefix state; preserve FIFO order and flush
partial batches explicitly.

### 16. Add a cost/selectivity planner only when it has a real choice

A planner should choose only among predicates proven independent, using static
RNG probability plus measured rejection cost. The selected plan must remain
deterministic and observable in telemetry/pool identity where relevant. Current
common-case ordering is already close to measured selectivity, so this waits
for another profitable independent stage.

### 17. Revisit portable SIMD only after profiling

If profiles expose a new lane-parallel hotspot, compare portable ILV code,
compiler vectorization, Apple NEON, and Windows AVX2/AVX-512 separately. Retain
the scalar exact oracle, disable/reference switches, exact floating-point
operation order, and cross-platform LuaJIT parity. Do not translate another
project's fixed `Vector512` code literally.

## Priority 3 — product and UX additions

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
- alternate FMA/cold `round13` variants;
- split fast/general evaluator paths;
- tested alternate compiler/LTO flag combinations;
- persistent worker barriers for checkpoint epochs;
- worker QoS and affinity changes;
- larger refilter buffers.

## Guardrails for external ideas

Do not translate another project's fixed C# `Vector512` implementation
literally. It is not portable across Brainstorm's targets or exact
floating-point modes. Architectural ideas may be independently reimplemented
and differentially tested, but source from GPLv3 projects must not be copied
into this MPL 2.0 codebase without an explicit compatibility review.
