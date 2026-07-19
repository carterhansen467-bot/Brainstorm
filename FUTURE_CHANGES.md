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

   *Prototyped 2026-07-18 in the Builder (`pool_first_gate_batch`,
   `BRAINSTORM_VECTOR_GATE=0` to disable, `-DBRAINSTORM_VERIFY_VECTOR_GATE`
   for the differential build). The gate replays only the first Joker4 draw
   per lane, decides `math.random(n)` buckets from output bits 44..51 alone,
   and leaves boundary/culled-resample lanes undecided, so a rejection is
   always one the scalar precheck must also make; survivors rerun the
   unmodified `pool_evaluate_pre`. Eligible only when every cumulative
   legendary rule is soul_depth 1. Measured on M1 Max: 1.29x on the
   Perkeo+Charm-tag pool build (16.4M/s to 21.0M/s single-thread, same ratio
   on all cores), 1.10x on a legendary-only build (Soul-walk bound). Verified:
   200M-seed differential run, byte-identical single-thread pools gate
   on/off, and the Lua-oracle, soul-depth, search-, refilter-, voucher-route,
   and Omen/Charm suites. Same-day port to the in-game searcher
   (`first_gate_batch`, same env/verify switches) covering its FS_SOUL,
   FS_LEGEND, and non-anywhere FS_TAG first streams — FS_SOUL rejects on the
   whole ante-1 reward-pack roll chain, where bits 44..51 decide each 0.997
   threshold 255/256 of the time. Measured (`bench`, M1 Max single thread):
   classic Soul/legendary search 12.7M to 24.5M seeds/s (1.94x), legendary
   anywhere 1.33x, ante-1 tag 1.28x; the gated loop also serves poolfile
   searches. Verified: differential bench on six filter shapes, the 38-case
   native_equivalence Lua oracle, 1M-case fastpath, and pool-search suites.
   Same-day follow-ups completed the Builder pick/state handoff, the item-2
   tag survivor rebatch, and the searcher's profitable exact-Ante FS_VOUCH
   gate. Any-window voucher and FS_TAG-anywhere prototypes were removed after
   measuring net losses. A later audit completed unrestricted FS_PACK using an
   exact weighted-catalog high-byte projection (2.52x on a two-target pack
   filter, 1.67x on a single target); route-dependent attached pools remain
   scalar. The direct FS_LEGEND gate now also hands its decided first pick and
   post-draw state to the scalar evaluator (about 7% additional throughput).
   Remaining candidates are searcher-side survivor rebatching and the
   cost/selectivity planner below.*
2. **Compact and rebatch survivors.** Store a small staged candidate record
   containing rank, seed, hashed seed, first-stream result/state, and the
   post-seed hashes for required key lengths. Accumulate survivors across input
   ILV groups and repack them into full groups of eight before any further
   independent vector gate. Flush the final partial batch with an explicit
   validity mask. This can improve utilization substantially when a cheap first
   gate rejects most candidates.

   *Prototyped 2026-07-18 in the Builder (`pool_eval_staged_batch` +
   `pool_tag_gate_batch`): legendary-gate survivors accumulate as
   rank/seed/hash records, and each full group of eight batch-hashes Tag1
   and runs a second conservative gate on the ante-1 Small tag pick. Gate
   eligibility requires a tag rule pinned exactly to that window (the roll
   is the Tag1 stream's first draw and a decided miss is a certain scalar
   rejection) and the natural seed space; culled tags stay undecided. FIFO
   staging preserves ascending-rank emission, remainders flush ungated at
   chunk end. Tag-only plans skip staging: Tag1 is already the prehashed
   first stream. Measured with both gates: Perkeo+Charm build 3.08s to
   1.82s single-thread (1.70x total), 1.83x on all cores; pinned tag-only
   1.27x. Byte-identical scan and refilter pools, 200M-seed differential
   verify, and the oracle/soul-depth/search/refilter/partial/lineage/
   shard/voucher/Omen suites all pass.*

   *Searcher follow-up assessment (2026-07-19): the current Soul gate leaves
   only 19,280 of the first 1M lanes for scalar evaluation (1.93%). A second
   staged Legendary gate can therefore address only that thin tail while
   adding candidate copies, delayed evaluation, partial-batch flushing, and
   separate full-space/poolfile logic. It remains a benchmark candidate, but
   was not retained without a measured gain above that complexity cost.*
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

   *Current gate ordering already tracks the measured common-case
   selectivity: Soul rejects about 98%, unrestricted packs about 68-84%, tag
   and Legendary about 79%, and exact-Ante voucher about 44% in the bounded
   fixtures. A planner would usually select the existing first gate and adds
   little until another independent multi-filter stage is proven profitable.*
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
3. Choose the smallest compatible non-empty pool, using authority and then
   pool ID/filename as stable tie-breaks. Retire only an exhausted/invalid
   marker and try the next compatible attachment before unrestricted search.
   Revisit selection using measured density and coverage if real workloads
   show that record count alone is insufficient.
4. Feed the selected path through the existing native `poolfile` path and run
   all active filters again over decoded records. Do not create a second pool
   membership implementation in Lua.
5. Display the chosen pool and its role in the UI/telemetry, including whether
   unrestricted fallback will follow exhaustion.
6. Accelerator exhaustion or any automatic validation/opening failure must
   warn once and transparently continue with unrestricted search without
   repeating candidates where practical. Authoritative exhaustion may finish
   the search. Never silently broaden a manually requested pool search.

### Implemented foundation

- Builder eligibility is split into accelerator and authoritative roles, with
  current-catalog and semantic-translation blockers.
- The Python canonical signature covers cumulative tag, Legendary, route,
  Soul-depth, voucher, and exclusion criteria. Atomic identity-bound sidecars,
  Attach/Detach controls, stale-marker reporting, and deletion cleanup are in
  place.
- Lua reconstructs and validates the canonical pool signature, discovers
  markers only when the manual selector is None, chooses deterministically,
  and currently accepts only directly proven tag/classic-Legendary relations.
- The selected pool remains session-local. Native results and embedded routes
  are rechecked on the main thread. Accelerators fall back to unrestricted
  search on exhaustion/opening failure after trying other compatible markers;
  authoritative exhaustion is revalidated immediately before becoming
  definitive. The live display reports the role/transition and suppresses the
  full-space geometric chance while candidates come from an enriched pool.
- Native scan/refilter/convert/merge, Organizer publication, attachment
  creation/removal, and deletion now share one cross-platform writer lock. Its
  stable lock file is intentionally retained so POSIX cannot split `flock`
  ownership across two inodes.
- Builder, HTTP, Lua matcher/fallback, header, deletion, packaging, and existing
  native pool-equivalence regressions cover this initial contract.

### Remaining implementation work

1. Complete controlled automatic-versus-unrestricted differential runs for
   every accepted window, Negative, depth, and full-versus-fast implication.
2. Add real end-to-end native-status stale-catalog/missing/corruption fallback
   tests on macOS and Windows. The production-Lua harness already covers
   deterministic multi-pool ordering/chaining and mid-search marker mutation.
3. Add voucher, Legendary-anywhere, wider-source, and composite matching only
   after each implication direction has an independent proof. Ambiguous cases
   continue to ignore the attachment and search unrestricted.
4. Measure marker discovery and accelerator-to-live transition overhead, and
   decide whether avoiding possible repeated accelerator members is worth the
   additional native state.

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
