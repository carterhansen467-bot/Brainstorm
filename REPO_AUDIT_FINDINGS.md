# Brainstorm 24-Hour Integration Audit

Last updated: 2026-07-19 (America/Los_Angeles)

This is the durable handoff for the repository-wide audit of every committed
and working-tree change made during the preceding 24 hours. The review was
requested because native performance work, search telemetry, Windows pool
fixes, deletion, and automatic pool attachment had been developed in several
overlapping passes.

## Repository and release state

- Branch: `seed-pool-integrity-audit`
- Luke's stack was audited through `e9b5f5e`; the follow-up fixes and completed
  attachment work are recorded in the later commits listed in this report.
- Tracking branch: `myfork/seed-pool-integrity-audit`, synchronized through
  the latest published audit follow-up before this document's final update
- Most recent published tag: `win-v10.8-seed-pool-overhaul` at `3b2dc23`;
  Luke's later `b6c667e..e9b5f5e` commits are not in that tag
- The automatic-attachment implementation and audit fixes were committed after
  Luke's stack. They are not part of the v10.8 tag merely because this document
  describes them.
- Runtime/user state and generated native binaries remain ignored. No real
  `.attached` marker was present in the local pool library during inspection.

## Changes inside the 24-hour window

### `e95a3f8` — accelerate exhaustive Omen seed pools

Intent: reduce exhaustive voucher/Omen/Charm route cost without changing the
accepted seed set.

The commit added cached raw voucher draws, reachability-first routing, Omen
frontier pruning, Soul trace/tape reuse, and targeted Charm recovery. The audit
compared it against the last pre-optimization implementation rather than only
checking current code against itself:

- 5,000,000 exhaustive full-route Charm/Perkeo candidates: identical 2,664
  member sets after sorting; current code was about 1.5x faster in this run.
- 1,000,000 multi-voucher/exclusion candidates: identical 52,602 members;
  about 1.6x faster.
- 1,000,000 voucher + Legendary + Ante-reducer candidates: identical 3,101
  members; about 2.6x faster.

Single-thread BSP3 membership and metadata digests were identical for all
three comparisons, so the optimized route did not merely preserve membership;
it also preserved recorded occurrence/purchase metadata.

The voucher fixtures, Omen oracle, production-Lua replay, ASan/UBSan run, and
TSan run also passed. No membership bug was found in this commit.

### `b3a8aaa` — accelerate exact seed filtering

Intent: make the exact path faster while preserving LuaJIT arithmetic and
route semantics.

The commit added one-shot/batched RNG operations, odometer/shared-suffix hash
generation, deferred route clearing, generation-stamped caches, voucher memo
pruning, Soul/pack fast paths, PGO training, and reference-verification modes.

Audit evidence:

- All 38 native-vs-Lua oracle cases passed before and after PGO training.
- One million `round13` cases per family, one million PRNG cases per FMA mode,
  and 1,003,176 booster boundary/lattice samples were exact.
- Odometer and shared/pre-suffix hashing passed carry, variable-length,
  expanded-space, wraparound, and sanitizer checks.
- The representative PGO Perkeo/first-Soul/Antes-1-through-6 training scan
  measured 792,620 seeds/s on this Mac; a second representative pass measured
  761,769 seeds/s. The first result clears the earlier 750k/s stretch target.
- Ordinary and PGO binaries both passed their post-build equivalence suites.

No accuracy regression was found. The remaining SIMD/inter-candidate ideas are
optional future optimization work, not a reason to destabilize the exact path.

### `4831e5d` — search telemetry and hardened Seed Pool Tools

Intent: add analytical estimates/live progress, completed-pool deletion, and
Windows checkpoint resilience.

Confirmed defects and fixes from the combined audit:

1. `tests/search_estimate.lua` existed but was not run by CI. It is now a
   dedicated test step on both macOS and Windows.
2. Automatic attached-pool candidates are enriched, not independent random
   full-space samples. Showing the ordinary geometric “chance by now” during
   that phase was mathematically wrong. The display now marks chance
   unavailable for attached phases and rebases it at the first unrestricted
   candidate after fallback. A regression verifies the total remains monotonic
   while the probability base resets correctly.
3. Fallback telemetry always said “accelerator finished,” even for a profile
   error or changed authoritative pool. It now displays the actual transition
   reason.
4. Full-space pools were forced to attach as authoritative in the browser even
   though the core API supported an accelerator role. The UI now offers both
   roles explicitly.
5. The served Builder JavaScript is now parsed by Node when available and has
   a direct rendered-newline assertion, preventing another template escaping
   regression from disabling the whole page.
6. A non-pool native failure during an automatic-pool phase correctly fell
   back to unrestricted Lua threads, but left the enriched-pool probability
   suppression active. That transition now restores the full-space estimate
   and has a production-state-machine regression.

The deletion token, traversal gates, active-job protection, HTTP confirmation,
variable-header handling, and Windows retry logic passed their regressions.

### `2cdf160` and `79cb2bf` — concurrent writer protection and its test race

Intent: make only one process able to write a pool path and make the duplicate
writer diagnostic test deterministic.

Two cross-feature gaps were confirmed:

1. Completed-pool deletion removed `.writer.lock`. POSIX `flock` protects an
   inode, so unlinking the pathname while a scanner held the old inode allowed
   another process to create and lock a new inode for the same pool. Deletion
   now acquires the native lock, holds it through identity revalidation and
   removal, and deliberately retains the stable lock file.
2. Only native `scan` and `refilter` used the lock. Native `convert`/`merge`
   and Python Organizer split/combine publication could still race a scanner.
   All publishing paths, attachment creation, and deletion now share one
   cross-platform protocol in `tools/pool_writer_lock.py`.
3. Attachment removal was the one remaining marker mutation outside that
   protocol, so concurrent attach/detach requests could produce a surprising
   final policy. Detach now takes the same pool lock and its contention path is
   covered.
4. Organizer combine initially took its new lock before creating a requested
   nested output directory. It now creates the directory first; a regression
   covers publication to a previously absent directory.

Regressions now cover duplicate scan, convert, merge, Builder deletion,
attachment, standalone Organizer split/combine, and web Organizer publication
while the output lock is held. The Windows C binaries also cross-compiled cleanly
with Zig 0.13.0. `79cb2bf` correctly joins the diagnostic reader thread and
needed no further change.

### `3b2dc23` — Builder JavaScript newline escaping

Intent: fix literal newlines injected into JavaScript string literals by the
Python page template. The fix is correct. The audit added validation of the
actual rendered `<script>` content and a JavaScript syntax parse, rather than
testing only for source-code markers.

### `b6c667e..e9b5f5e` — lane-parallel first gates from Luke

Intent: exploit independence across the existing eight-candidate hash batch,
rejecting lanes from a cheap first RNG outcome before entering the scalar
filter, while preserving the scalar path for survivors and every ambiguous or
culled/resampled outcome.

This is a four-commit stack:

- `b6c667e` adds Soul, Legendary-anywhere, and pinned Ante-1 tag gates to the
  in-game native searcher, plus the first Joker4 gate to the Seed Pool scanner.
- `aaa00a0` adds direct pinned-tag gating and rebatches surviving Seed Pool
  Legendary lanes through a second tag gate.
- `d6d3466` adds the in-game exact-Ante voucher gate and hands a decided first
  Legendary pick/post-draw state into the Seed Pool evaluator.
- `e9b5f5e` records the measured designs and the variants rejected as net
  losses. It changes documentation only.

The stack therefore benefits both products, although the individual gates are
specialized to their different pipelines. The audit fixed two gate edge cases,
one test-coverage gap, and two audit/measurement infrastructure defects:

1. The original bucket-boundary check compared the high-byte interval's
   excluded upper endpoint. For catalog size 32 this left 32 of 256 intervals
   unnecessarily ambiguous even though every interval determines exactly one
   bucket, reducing the advertised exact-Ante voucher gain. The replacement
   classifies the inclusive first and last reachable 52-bit integers exactly.
   It exhaustively agrees with scalar `math.random(n)` interval endpoints for
   every catalog size 1 through 256 and eliminates all high-byte boundary
   fallbacks at the base-game voucher catalog size of 32.
2. The scalar tag/voucher filters compare catalog key strings, but the gate
   targeted one catalog index. A modded catalog containing the requested key
   twice could therefore reject the second valid entry. Duplicate active
   target keys now disable the index-specific shortcut; unmodified catalogs
   retain the fast path.
3. The original compile-time verifier was strong but its ad hoc benchmark
   shape could be vacuous and was not in CI. `tests/vector_gate_equivalence.sh`
   now runs a non-vacuous one-million-candidate differential for each of Soul,
   Legendary, tag, voucher, and unrestricted physical packs; exhausts all
   high-byte/catalog intervals;
   verifies duplicate-key fallback; runs a bounded ASan/UBSan differential;
   and requires byte-identical gate-on/off Seed Pool output for direct and
   survivor-rebatched tag paths. It is wired into both macOS and Windows CI.
4. The first pushed CI revision encoded the Windows LuaJIT telemetry command
   as an invalid mixed YAML/shell scalar, so GitHub rejected the workflow
   before creating jobs. It now uses a literal command block; the workflow is
   parsed locally before republishing. Replacement run `29705477272` passed
   every macOS and real-Windows job.
5. `mode_bench` made every workload unfindable by adding an impossible tag.
   For voucher, pack, and joker configurations this inserted a different
   predicate ahead of the path being measured, so its absolute rates were not
   representative. The harness now makes the active filter family impossible
   while retaining its calibrated stream/gate. On the corrected exact-Ante-7
   voucher workload, the gate measured about 563k/s versus 344k/s disabled,
   confirming a real 1.64x gain close to Luke's stated 1.68x. The prior
   multi-million/s absolute voucher figures measured the artificial tag miss
   and should not be used for capacity planning.
6. The remaining unrestricted FS_PACK candidate was implemented after the
   corrected harness established its real baseline. At config load it projects
   every exact weighted booster-picker change point into 256 conservative
   high-byte intervals. A lane is rejected only when none of its three/four
   raw Ante-1 pack draws can select any requested key. It measured 15.86M/s
   versus 6.29M/s (2.52x) for two requested packs and 10.49M/s versus 6.27M/s
   (1.67x) for one. Separate million-candidate differentials found zero dropped
   scalar matches. Forced first-shop Buffoon targets disable the useless gate,
   absent targets reject safely, and `.bspool` routes remain scalar because an
   embedded collected tag can alter shop skips and RNG consumption.
7. A decided FS_LEGEND hit was still recomputed immediately by
   `passes_prepared`. The gate now hands its exact first Joker4 index and
   post-draw stream state to both full-space and poolfile workers; ambiguous
   and resampled lanes retain the untouched scalar path. The bounded harness
   compares handed and scalar results for every eligible survivor. Corrected
   Legendary-anywhere throughput rose from about 15.4M/s to 16.5M/s (roughly
   7% additional gain), and the total gate ratio is now about 1.45x versus the
   11.3M/s disabled path.

Current bounded differential counts were 14,843, 23,706, 47,478, 66,341,
133,587, and 311,391 scalar-passing seeds across the five gate kinds and two
pack shapes, with zero gate-dropped matches.
Production PGO Seed Pool measurements were 12.34M/s versus 10.55M/s for the
tag-only gate and 19.88M/s versus 10.96M/s for the rebatch shape. The
verification build is deliberately slower because it reruns rejected lanes
and handed-off survivors through the scalar oracle; its rate is not a product
benchmark. A four-thread TSan rebatch scan and Builder handoff ASan/UBSan scan
also passed. The follow-up PGO build passed all 38 Lua/native cases; repeated
exact Perkeo/first-Soul training runs measured 753,427-789,569/s.

The FUTURE note initially still listed handoff, Builder tag rebatching, and the
exact-Ante voucher gate as future work after later commits in the same stack had
implemented them. It now separates those completed items from the actual
remaining candidates.

### Working tree — automatic seed-pool attachment

Intent: opt completed pools into automatic acceleration without ever excluding
a seed valid under the active in-game filters.

The implementation includes:

- accelerator versus authoritative physical eligibility;
- native-compatible catalog hashing and canonical cumulative predicates;
- atomic identity-bound `.attached` markers and dual-role Builder controls;
- conservative production-Lua implication matching and manual precedence;
- native pool routing plus independent main-thread replay;
- deterministic multiple-pool selection and pool-specific failure handling;
- attachment-aware telemetry, documentation, packaging preservation, and CI.

Confirmed defects and fixes:

1. Authoritative exhaustion trusted a marker validated only during discovery.
   Lua now rereads the marker/header/file identity and rechecks active semantic
   compatibility immediately before accepting a definitive no-match. Mutation
   at that boundary falls back safely.
2. Selection previously preferred authority over actual cost. It now chooses
   the fewest records, with authority and stable identities as tie-breaks.
3. Exhausting the smallest accelerator originally skipped every other attached
   pool and jumped directly to unrestricted generation. A per-search tried set
   now retires only that marker, tries the next compatible attachment, and
   reaches unrestricted generation only after the safe chain is exhausted.
4. The implication test called bounded sampling a proof and its 5M sample had
   zero Negative Fast Exact members, making that sub-check vacuous. The default
   is now 20M, the test requires a nonempty Negative subset, and the wording
   explicitly says bounded differential evidence. The current fixture found
   10,785 full-route, 2,875 Fast Exact, and 6 Negative Fast Exact members, with
   both narrower sets contained in the broader result.

Important semantic non-bug: a classic Legendary-only in-game filter models the
contents of a hypothetical Charm reward but does not require the actual A1
Small tag roll to be Charm. Therefore a pool requiring both real `tag_charm`
and Perkeo is narrower and must not attach to a Perkeo-only request. The user
must select both Charm Tag and Perkeo for that combined pool. A proposed
implicit-Charm widening was rejected by the production matcher regression.

## Full validation completed

The audit ran the normal and extra suites, including:

- native/Lua equivalence (38 cases), fast arithmetic, PGO build and post-PGO
  equivalence;
- permanent lane-gate differential and exact interval classification,
  gate-on/off Seed Pool byte equivalence, duplicate modded-key fallback, and
  post-change PGO performance;
- seed-pool compatibility, Soul depth, exact locations, voucher routes,
  Omen/Charm/frontier, pool search/exhaustion, refilter, partial refilter,
  compression/conversion, shard/merge, lineage append/resume, corruption, and
  composite provenance;
- Builder locations/vouchers/library/deletion, Organizer and web Organizer,
  variable headers, automatic attachment, telemetry, win-spawn quoting,
  Windows release layout, and a 120M-candidate pause/resume smoke test;
- ASan + UBSan on representative voucher and reducer scans;
- ASan + UBSan on all four in-game lane gates and the Seed Pool staged
  gate/handoff path;
- TSan on a 20M multi-thread pool scan, a multi-thread restricted-search
  exhaustion, and the new four-thread survivor-rebatch path;
- ordinary macOS builds and Zig x86_64-Windows cross-builds;
- Python compilation, Lua `loadfile` syntax, Git whitespace, and rendered
  Builder JavaScript syntax.

All completed tests passed after the fixes above. One direct invocation of
`tests/search_equivalence.lua` without its required `--new` argument failed as
expected; the supported wrapper had already run it successfully for all 38
cases. This machine's LuaJIT lacks the optional `-b` bytecode command, so Lua
syntax was validated by execution/`loadfile` instead.

## Remaining limitations and follow-up

- The newly changed Python Windows lock path cannot execute natively on this
  Mac. Static compilation, Windows-layout tests, and Zig C cross-builds passed;
  the next pushed CI run remains the real Windows runtime proof.
- The Lua attachment contract simulates native status transitions while the
  native suites independently prove actual pool hit/exhaustion/status files.
  A single end-to-end harness that drives automatic discovery through a real
  native child on both platforms would strengthen this further.
- The 20M implication run is deliberately bounded evidence, not an exhaustive
  proof over all `34^8` natural seeds.
- Voucher, Legendary-anywhere, tag-anywhere, broader source implications, and
  composite Boolean attachments remain disabled until each implication has a
  dedicated semantic/differential proof.
- Unrestricted fallback can revisit ranks previously contained in an
  accelerator. This is correct and normally negligible; add native exclusion
  state only if measured workloads justify the complexity.
