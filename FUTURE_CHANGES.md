# Future Changes

This is the handoff list for work intentionally deferred from the current
release. Point future coding sessions to this file before beginning the next
Seed Pool Tools overhaul.

## Search performance follow-up

- Do not add the separate metadata-free Builder reachability precheck unless a
  later profile changes the tradeoff. Its exact prototype improved tested
  workloads by only about 0.7-3.5% while duplicating voucher-route semantics.

## Balatro Seed Oracle research

Research performed 2026-07-18 against
[BalatroSeedOracle](https://github.com/OptimusPi/BalatroSeedOracle) commit
`237f8bd266f874acc6a8f4f300f7d08a7c0f747a` and its
[MotelyJAML](https://github.com/OptimusPi/MotelyJAML) submodule commit
`8a3001f76996873096dcf01634b69091c6a2b5f8`.

### What the program is

Balatro Seed Oracle is a standalone .NET 10/Avalonia desktop seed-search
application, not an in-game Balatro mod. Its MotelyJAML engine searches and
scores seeds from typed JAML documents containing `must`, `should`, `mustNot`,
AND/OR, Ante, source, edition, count, deck, and stake criteria. It is designed
to retain, rank, analyze, and export many results. Brainstorm instead combines
an in-game Lua interface, native C first-result search, trusted Lua
re-verification, and persistent/refilterable `.bspool` indexes.

Important architectural differences:

- Motely keeps many RNG streams and predicates eight candidates wide with
  `Vector512` operations. Complex filters can prefilter in SIMD and finish only
  surviving lanes through scalar route logic.
- Survivors from one filter are copied into dense eight-lane batches before
  the next filter. Their seed characters and cached partial pseudohashes move
  with them, preventing sparse survivors from wasting most SIMD lanes.
- Motely creates long-lived per-search worker threads. Workers claim large
  prefix batches through one shared atomic counter, but keep counters, buffers,
  hash caches, and filter state thread-local in the hot path.
- Motely caches the post-seed pseudohash state by key length, then finishes
  each requested key from that state. Brainstorm now independently has this
  optimization in `pool_pseudohash_ks_n`, plus the more specialized natural
  seed odometer and shared-suffix chain.
- Motely's sequential mode primarily enumerates fixed eight-character seeds
  over `123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ`. Brainstorm deliberately separates
  Balatro's natural eight-character, O-excluding `34^8` space from settable and
  total variable-length spaces.
- Motely offers arbitrary typed filters and scored output, while Brainstorm's
  narrower filters model the exact playable routes needed by the mod and pool
  tools. Brainstorm additionally proves voucher-purchase paths with DFS,
  handles targeted Charm and Omen recovery, embeds route membership in pool
  headers, and re-verifies native hits in production Lua before applying them.
- Seed Oracle has a generic list/provider search path but its application-level
  sequential seed library is explicitly not wired in the inspected build. It
  does not provide Brainstorm-style pool identity, compatibility invalidation,
  refilter lineage, automatic in-game pool selection, or accelerator fallback.
- Its README's claimed 10-50 million seeds/second is not directly comparable
  to Brainstorm throughput: filter work, route coverage, seed space, hardware,
  output behavior, and accuracy obligations differ. No .NET 10 runtime was
  available on the M1 Max test host for an independent benchmark.

### High-value Brainstorm applications

1. **Vectorize the first candidate gate across seeds.** This is the strongest
   performance idea from Motely. Brainstorm currently hashes an ILV group of
   eight seeds and immediately evaluates its lanes serially. Prototype an exact
   eight-lane first stage that advances all first streams together, calls the
   existing batched LuaRandom reseed, resolves Joker4 or another selected first
   roll with an active-lane mask, and performs masked resamples. Pass only the
   surviving candidates to `pool_evaluate_pre`. Keep identical floating-point
   operations and verify every lane against the scalar path.
2. **Compact and rebatch survivors.** Store a small staged candidate record
   containing rank, seed, hashed seed, first-stream result/state, and the
   post-seed hashes for required key lengths. Accumulate survivors across input
   ILV groups and repack them into full groups of eight before any further
   independent vector gate. Flush the final partial batch with an explicit
   validity mask. This can improve utilization substantially when a cheap first
   gate rejects most candidates.
3. **Use masked vector stages only where predicates are independent.** Specific
   Legendary selection and independent raw tag rolls are good candidates.
   Route-dependent tag collection, voucher purchasing, Charm/Omen recovery,
   and Soul walks share simulated state and must remain in their proven order
   unless a new staged representation preserves all of that state exactly.
4. **Carry hash-prefix state through survivor batches.** Brainstorm already
   caches post-seed state by key length. A rebatching implementation should
   copy those values rather than rebuild them, mirroring Motely's useful cache
   handoff while retaining Brainstorm's faster contiguous candidate generation.
5. **Add a real cost/selectivity planner for safe first gates.** Motely includes
   an Ante-span cost estimate and a `CheapestFirst` helper, but its inspected
   JAML builder does not call that helper; authored clause order still controls
   the stages. Brainstorm should not copy the simple estimator. Use static RNG
   probabilities plus measured first-epoch rejection and time counters, and
   choose only among predicates proven independent. Preserve a deterministic
   choice in pool identity/telemetry when selection could affect traversal or
   reproducibility.
6. **Use a typed canonical predicate representation for pool attachment.** The
   valuable JAML lesson is one structured semantic model rather than matching
   filenames or UI text. Define versioned Brainstorm predicate atoms for item,
   count, location, source, edition, route coverage, voucher ownership/purchase,
   and Boolean composition. Generate Builder and Lua signatures from the same
   field definitions, then prove `active predicate => pool predicate` as
   specified below. Do not adopt JAML wholesale or create another independent
   route evaluator.

### Lower-priority product ideas

- Optional `should` criteria and scoring could rank acceptable pool members
  instead of returning the first. This is a separate product feature and does
  not improve generation throughput.
- A survivor-only native analyzer could return richer route metadata without a
  second native pass. The trusted main-thread Lua verification must remain the
  final authority before a seed is applied.
- Remote search abstraction is interesting for managed distributed work, but
  Brainstorm's explicit shards, checkpoints, manifests, and merge tools are a
  better fit for current exhaustive pool construction.
- General JAML-style filter authoring would greatly expand scope and duplicate
  route semantics. It should not precede the focused attachment and performance
  work above.

### Techniques not to transplant directly

- Do not replace Brainstorm's exact integer-assisted `round13` or runtime
  plain/FMA calibration with Motely's vector FMA rounding without exhaustive
  LuaJIT parity across macOS and Windows.
- Do not translate fixed C# `Vector512` code literally into the C engine.
  Benchmark a portable ILV implementation, compiler vectorization, NEON on
  Apple Silicon, and AVX2/AVX-512 on Windows separately while retaining the
  scalar exact reference.
- Do not use every logical processor by default for the in-game search merely
  because a standalone desktop application can. Leave capacity for Balatro's
  main/render threads; exhaustive Builder scans may continue using all tested
  profitable cores.
- Do not use Seed Oracle as the correctness oracle for Brainstorm pools. The
  inspected generic source model does not prove every Charm/Omen/voucher
  purchase route Brainstorm supports, and its source configuration still marks
  Omen Globe's Arcana-pack interaction as unfinished.
- Do not copy source without a license review. BalatroSeedOracle's top-level
  project is GPLv3 while Brainstorm is MPL 2.0. Independently reimplement and
  differential-test the architectural ideas unless compatible permission is
  established.

### Required prototype proof

- Benchmark scalar evaluation, eight-lane first gate, and survivor rebatching
  separately on representative Legendary, tag, voucher, and mixed workloads.
- Require byte-identical accepted ranks, metadata, pool digests, and refilter
  results against the scalar engine over controlled ranges and randomized
  configurations.
- Run the existing Lua oracle, native equivalence, round13/PRNG, Charm/Omen,
  voucher-route, Soul-depth, shard/merge, pool compression, and Windows CI
  suites before retaining any speedup.
- Keep each vector stage removable behind a compile-time/reference switch until
  its measured gain exceeds its state-copying, compaction, and maintenance cost.

## Automatic attached pool selection

Let completed seed pools be attached to Brainstorm so the in-game search can
recognize compatible active filters and automatically search the relevant pool
instead of starting with unrestricted seed generation. The detailed design and
research live in `ATTACHED_SEED_POOLS.md`.

This is worthwhile even though automatic selection is not intrinsically faster
than manually choosing the same pool. Its value is that Brainstorm consistently
captures the pool's potentially enormous search-speed improvement without the
player remembering which pool to select, understanding Builder route semantics,
or risking an incompatible manual choice. It also turns finished Builder work
into a reusable in-game index rather than a one-off export.

### Pool roles

Support two deliberately different attachment roles:

1. **Accelerator pool:** a completed, internally consistent pool covering only
   a declared rank range may be searched first. If it produces a result, that
   result still passes every ordinary active Brainstorm filter. If it is
   exhausted, missing, stale, or incompatible, Brainstorm warns and continues
   with unrestricted generation. A partial pool therefore improves expected
   time-to-first-result but can never turn pool exhaustion into a false
   “no matching seed exists” answer.
2. **Authoritative pool:** a completed pool with proven coverage of the entire
   natural `34^8` seed space may fully replace unrestricted generation. Only
   this tier may treat exhaustion as definitive and omit fallback.

For example, the current completed Perkeo/Ante-1-Small/Charm test pool covers
100 million ranks and contains 19,777 records. It is not authoritative, but it
is still a valuable accelerator: matching active filters can search those
records first and usually return immediately. Requiring a roughly 1.8-trillion
rank, multi-gigabyte pool before providing any benefit would discard most of
the practical value of automatic attachment.

### Correct matching rule

- Build one versioned canonical semantic signature from the Builder criteria
  and an equivalent signature from the active Lua filters. Never match by pool
  filename, display label, or `criteria_hash` alone.
- Prove the relation **active predicate implies pool predicate**. The pool may
  be broader because every decoded member is rechecked by the active filters;
  it must never be narrower and silently omit a valid active result.
- Include tag identity/count/location, Legendary identity/Negative requirement,
  Soul depth and route source, Charm/Omen/full-route behavior, voucher windows
  and purchases, exclusions, seed-space model, and every other constraint that
  affects membership.
- Begin with exact semantic equivalence and a small explicitly tested
  implication lattice. Safe examples include a pool with a wider location
  window, `any` source versus one requested source, or full Legendary route
  coverage versus a requested subset. Ambiguous relationships must refuse
  automatic use and fall back.
- The motivating classic Perkeo case must include the active Ante-1 Small Charm
  tag filter; the classic Legendary option alone assumes a Charm reward and
  does not prove the Charm tag actually rolled.

### Attachment metadata and lifecycle

- Add **Attach to Brainstorm** and **Detach** controls to completed pool cards.
  Attachment is opt-in and reversible; it must not rewrite a potentially large
  `.bspool`.
- Store policy in an atomic, line-based `<pool>.attached` sidecar readable by
  both Python and Lua. Bind it to the attachment schema, role, enabled state,
  exact filename, pool ID, catalog hash, criteria identity, snapshot identity,
  canonical signature, and pool file identity.
- Validate the sidecar against the live pool header on every discovery. Moving
  a pool may be supported by rediscovery, but replacing, refiltering, mutating,
  or changing its profile must invalidate stale attachment metadata.
- Completed-pool deletion must remove the `.attached` sidecar with the pool.
  The deletion implementation already reserves and cleans up that suffix.
- Packaging and updates must preserve user-created pools and their valid
  attachment sidecars on both macOS and Windows.

### In-game selection behavior

1. An explicitly selected manual pool always wins and preserves the existing
   hard-error behavior when invalid; automatic selection never changes the
   saved manual selector.
2. When the manual selector is None, discover enabled `.attached` sidecars,
   validate their bound headers, and evaluate semantic compatibility.
3. Prefer an authoritative compatible pool. Otherwise choose the accelerator
   expected to be most useful—initially the smallest compatible non-empty pool,
   with a stable pool-ID/filename tie-break. Revisit selection using measured
   density and coverage if real workloads show that size alone is insufficient.
4. Feed the selected path through the existing native `poolfile` path and run
   all active filters again over decoded records. Do not create a second pool
   membership implementation in Lua.
5. Display the chosen pool and its role in the UI/telemetry, including whether
   unrestricted fallback will follow exhaustion.
6. Accelerator exhaustion or any automatic validation/opening failure must
   warn once and transparently continue with unrestricted search without
   repeating candidates where practical. Authoritative exhaustion may finish
   the search. Never silently broaden a manually requested pool search.

### Remaining implementation work

1. Split the existing physical eligibility result into accelerator eligibility
   and authoritative/full-space eligibility, with human-readable blockers for
   both roles.
2. Implement and version the Python criteria-to-signature translator, the Lua
   active-filter translator, and the small proven implication lattice.
3. Implement atomic Attach/Detach sidecar controls and show attachment state,
   role, coverage, compatibility, and invalidation reasons in the Seed Pool UI.
4. Add Lua marker discovery and automatic choice to `buildNativeConfigText`,
   then implement the accelerator-to-unrestricted continuation path without
   resetting user-visible search intent.
5. Add deterministic multiple-pool selection, stale marker handling, catalog
   invalidation, update preservation, and observability.

### Required correctness and performance proof

- Differentially compare automatic pool results with unrestricted search over
  controlled ranges for the exact Perkeo/Charm case and every supported
  implication rule.
- Exercise each signature field independently; a mismatch without a proven
  implication must refuse the pool.
- Test partial, empty, provisional, expanded-space, old-model, composite,
  stale-ID, changed-file, and catalog-mismatched pools.
- Prove accelerator exhaustion/failure falls back and still finds a result
  outside the pool; prove authoritative exhaustion does not fall back.
- Prove manual selection overrides automatic selection and retains hard-error
  semantics, while automatic failures remain non-fatal.
- Test deterministic choice among multiple compatible attachments, active
  filter rechecking of broader pool records, atomic attach/detach, completed
  pool deletion, and macOS/Windows package/update preservation.
- Benchmark end-to-end time-to-first-result, fallback overhead, marker discovery,
  and memory/I/O behavior. The feature should add negligible cost when no pool
  matches and should approach ordinary pool-reader speed when one does.

## Organize / Combine (merge and splice)

- Overhaul the entire merge/splice program. The current interface and workflow
  are not reliably usable yet; treat this as a redesign and validation task,
  not a small polish pass.
- Re-test inspection, category splitting, ambiguous-occurrence handling,
  union, intersection, difference, distributed-part merging, provenance, and
  output publication end to end before calling the replacement ready.
