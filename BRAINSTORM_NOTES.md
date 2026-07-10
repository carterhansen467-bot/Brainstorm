# Brainstorm Mod Extension — Session Notes

Context for continuing this project in Claude Code. Everything below was verified
directly against Balatro's real Lua source (extracted from `Balatro.love`, unzipped
to `~/Desktop/balatro_src` on this Mac) — not guessed, not from third-party tools.

## What's been built so far

Extensions to the Brainstorm seed-search mod (`~/Library/Application Support/Balatro/Mods/Brainstorm`),
on git branch `joker-search-experiment`:

1. **Shop Joker search** — `Brainstorm.checkShopJokerSearch(seed_found, ante, num_slots, target_key)`
   in `Brainstorm_reroll.lua`. Finds a specific joker in the shop within `num_slots`
   (slots 1-2 = initial fill, 3-4 = after 1st reroll, 5-6 = after 2nd reroll, etc. —
   confirmed `reroll_shop` reuses `create_card_for_shop` per slot, same RNG sequence).
2. **Buffoon Pack Joker search** — `Brainstorm.checkPackJokerSearch(seed_found, ante, num_cards, target_key)`.
   Same RNG mechanism, `'buf'` key suffix instead of `'sho'`.
3. **Legendary search** (Soul card → Joker4) — inline in `auto_reroll()`, merged with the
   original `searchForSoul` setting to avoid double-consuming the `"soul_Tarot1"` RNG key.
4. **UI**: split across 2 tabs ("Brainstorm" and "Brainstorm: Jokers") because vanilla
   tabs at `tab_h = 7.05` only fit ~7-8 rows before going off-screen with no scroll support.
   Joker picker is split into 3 dropdowns by rarity (Common/Uncommon/Rare) since the full
   ~130-joker alphabetical list was too cumbersome to cycle through.

## Verified RNG key formats (from real source, not Immolate guesses)

All confirmed via `get_current_pool()` in `functions/common_events.lua` and call sites
in `card.lua` / `functions/UI_definitions.lua`:

- Shop voucher: **per-ante key `"Voucher"..ante`** (ante 1 = `"Voucher1"`), first advance
  only — each ante is an INDEPENDENT key, like the joker keys. Resamples use
  `"Voucher"..ante.."_resample"..it`. **VERIFIED EMPIRICALLY, not from source** (the
  extracted `get_current_pool` showed a bare `"Voucher"` key, but the shipped game appends
  the ante): live seed `BFXJ42PE` had ante-1 voucher `v_seed_money`, and the game's own
  `G.GAME.pseudorandom` held keys `Voucher1`, `Voucher1_resample2..5`. A key brute-force in
  `debugPredictVoucher` (Ctrl+B) confirmed only `"Voucher1"` reproduced the live voucher.
  Lesson: **do NOT trust `~/Desktop/balatro_src` for the shipped RNG keys — verify against
  a live run.** The mid-session detour to a single advancing `"Voucher"` stream was wrong
  and reverted; `Brainstorm.rollVoucherSequence` uses `"Voucher"..ante`.
  Pool = base vouchers only (`v.unlocked ~= false and not v.requires`); ~half the 32-entry
  pool is UNAVAILABLE (upgraded vouchers gated on their base in `used_vouchers`), so
  resamples are frequent (ante 1 above resampled 4x). `get_current_pool` also excludes
  vouchers still in the shop / already redeemed; under "redeem nothing" that means ante
  N≥2 excludes ante N-1's voucher, which `rollVoucherSequence` mirrors by blanking prev's
  slot. Ante 1 and a couple of ante 2+ spot-checks confirmed live (Ctrl+B self-test).
- Shop slot type roll: `"cdt" .. ante`
- Shop joker rarity: `"rarity" .. ante .. "sho"`
- Buffoon pack joker rarity: `"rarity" .. ante .. "buf"`
- Joker pool pick (shop): `"Joker" .. rarity .. "sho" .. ante`
- Joker pool pick (pack): `"Joker" .. rarity .. "buf" .. ante`
- Legendary pick (Soul card): `"Joker4"` — global, no ante, no source suffix
- Soul-in-pack roll: `"soul_Tarot1" .. ante` for Arcana-type packs (Spectral uses `"soul_Spectral"`, untested)
- Resample (when pool slot is `'UNAVAILABLE'`): append `'_resample' .. n` *before* the seed,
  i.e. `rkey .. '_resample' .. it .. seed_found` — get this ordering wrong and matches silently fail.
- All keys get `seed_found` appended at the very end when calling `Brainstorm.pseudoseed()`
  (the mod's local RNG replica) — this is NOT needed in real game code since the real
  `pseudoseed()` pulls the seed from `G.GAME.pseudorandom` automatically.

## Critical mechanics learned the hard way

- **Pool culling preserves array index.** Ineligible jokers (locked/owned/banned) are NOT
  removed from the pool array — they're replaced in-place with the string `'UNAVAILABLE'`,
  then resampled via `_resample`. A naive compacted array gives wrong picks even with
  correct RNG math.
- **`joker_is_pool_eligible` should NOT check current-run state** (`used_jokers`,
  `enhancement_gate` against `G.playing_cards`) when predicting a *future* fresh run —
  `G:start_run()` always begins with empty `used_jokers` and an unenhanced starting deck,
  regardless of what your current run has collected. Checking live state causes
  legendary/joker searches to falsely exclude already-owned items and infinite-loop.
- **Charm Tag does NOT force shop jokers.** Only Uncommon Tag and Rare Tag have
  `config.type = 'store_joker_create'` (forces a shop slot's rarity, bypassing normal
  RNG for that slot — would desync our predictions if combined with Joker search).
  Charm Tag's actual type is `'new_blind_choice'`.
- **Legendary search requires an Arcana/Spectral pack to actually appear** — the Soul-roll
  prediction only means something if a Tarot-type pack genuinely gets generated. Currently
  handled by convention (pair Legendary search with Pack search = Arcana, or Charm Tag),
  not by code. Same caveat applies to Buffoon-pack-depth settings — checking depth=4
  assumes that many Buffoon-type card draws actually occur in that ante; if only one
  Normal (2-card) pack appears, slots 3-4 check RNG that was never really rolled.
- **All of this assumes default shop rates**: no Tarot Merchant / Planet Merchant / Magic
  Trick vouchers redeemed (these change `joker_rate`/`tarot_rate`/`spectral_rate`), default
  White Stake + Red Deck. Search happens pre-run so this is a fresh-run assumption, not
  something the code currently verifies.
- **Lua scoping gotcha (bit us twice):** `local` functions/variables are only visible to
  code positioned *after* them in the file, regardless of call order at runtime. Fix used
  throughout: attach shared helpers to `function Brainstorm.X(...)` instead of `local
  function X(...)` so they're resolved via table lookup, not lexical position.

## Known UI quirk (fixed)

`create_option_cycle`'s `current_option` must be computed from the actual saved setting
(search the rarity-specific name list for the key that matches `Brainstorm.SETTINGS.autoreroll.searchJoker`)
— hardcoding `current_option = 1` causes the dropdown to visually reset to "None" every
time the tab re-renders, even though the underlying saved value is untouched.

5. **Multi-ante voucher search** — "Search Voucher" on the main tab now has a companion
   "Voucher Ante" cycle (Ante 1 / 2 / 3 / 4 / Any 1-4). Backed by
   `Brainstorm.checkVoucherSearch(seed, key, ante_mode)` (ante_mode 0 = any). Fixed the
   incorrect `"Voucher"..ante` key at the same time (see RNG key note above).

6. **Background (threaded) seed search** — the filtered search now runs on a
   `love.thread` worker instead of blocking the main thread, so heavy filters no longer
   stutter the game. Key pieces in `Brainstorm_reroll.lua`:
   - `Brainstorm.passesAllFilters(seed)` — the entire filter suite, extracted verbatim
     from the old inline `auto_reroll` body. **Single source of truth**: both the
     synchronous fallback AND the worker call it, so any RNG fix applies to both.
   - `Brainstorm.buildSearchSnapshot()` / `serializeValue()` — snapshot the pools + game
     flags (joker rarity pools, Booster/Tag/Voucher pools, `banned_keys`, `pool_flags`)
     into a Lua-literal string. Pool eligibility is static for a fresh run, so the worker
     rebuilds the same index-preserving `'UNAVAILABLE'`-culled arrays itself.
   - `Brainstorm.SEARCH_WORKER_SRC` — the worker chunk. It has its own Lua state (no `G`),
     so it: stubs `require("lovely"/"nativefs")`, defines the game's pure RNG globals
     (`pseudohash`/`pseudorandom` verbatim from `misc_functions.lua`; `pseudorandom_element`
     is a **hot-path-optimized but RNG-identical** rewrite — for our plain string-valued
     joker/voucher pools the game's sort-by-index is a no-op, so it skips the per-call
     keys-table + `table.sort` and does `_t[math.random(#_t)]` directly, one `math.random`
     call as before; Tag pools with `sort_id` keep the original sort path), builds a
     **mock `G`** from the snapshot (needs `G.FUNCS = {}`
     because reroll.lua assigns `G.FUNCS.change_*` at load), then `load()`s the passed
     reroll.lua source to get the identical filter code, and loops generating seeds.
   - Channels: `brainstorm_search_session` (workers run while their peek == the session id;
     clearing it stops them all — love threads can't be force-killed), `..._result`
     (serialized `{seed,jokerFoundAt,session}`), `..._progress` (serialized `{i=threadIndex,n=tried}`).
     `startSearchThread`/`pollSearchThread`/`stopSearchThread` + `updateAutoReroll` drive it.
   - **Parallel workers** (the speed win): `startSearchThread` spawns N workers, not 1.
     N = `Brainstorm.getSearchThreadCount()` = the `searchThreads` setting, or auto =
     `love.system.getProcessorCount()-1` when 0. Each worker gets `(configStr, rerollSrc,
     threadIndex, N)` and partitions the SAME global seed sequence with NO overlap:
     worker `i` tests indices `k = (tried-1)*N + i` (i = 0..N-1), so together they cover
     `0,1,2,...` exactly once. Shared result channel; first finder wins; `stopSearchThread`
     clears the session so all exit. `pollSearchThread` loops all threads for errors and
     sums per-thread progress into `A.searchTried`. Near-linear speedup with core count.
     UI: "Search Threads" cycle (Auto/1/2/3/4/6/8/12/16) — it REPLACED "Rerolls per Frame",
     which only ever throttled the synchronous fallback the threaded path doesn't use.
   - **Why parity holds without in-game testing**: the real worker runs in the SAME LuaJIT
     VM as the game, the game never overrides global `math.random`, and the RNG functions
     are byte-identical. Validated the serialize→reconstruct-`G`→`passesAllFilters` pipeline
     with 5000 seeds under embedded LuaJIT: 0 mismatches vs the direct path.
   - Toggle: `Brainstorm.SETTINGS.useSearchThread` (default true). Set false to fall back to
     the old per-frame synchronous search. Worker errors auto-fall-back for the session.
   - **Search-in-progress UI** (`updateAutoReroll` phases): brief delay -> big centered
     "Rerolling..." banner for ~2.25s -> small spinning corner indicator until found/stopped.
     The corner spinner is `Brainstorm.draw_search_indicator()`, drawn in raw canvas pixels
     (`push("all")`+`origin()`) via a new lovely.toml patch (`before
     love.graphics.setCanvas(G.AA_CANVAS)`, same injection point as the debug watermark).
     Phase flags live on `Brainstorm.AUTOREROLL` (`searchElapsed`, `bigTextShown`,
     `bigTextRemoved`, `showSearchIndicator`); `Brainstorm.resetSearchUI()` tears it all down
     and is called on found, on toggle-on, and on toggle-off.

7. **Bank found seed to a save slot** — instead of overwriting the current run,
   a found seed can be saved into one of the 5 save slots and started later, so you
   can search while playing. Setting `Brainstorm.SETTINGS.autoreroll.foundSeedSlot`
   (0 = overwrite current run immediately, old behavior; 1-5 = bank to that slot),
   surfaced as the "Found Seed" cycle on the main settings tab.
   - **Key constraint**: `save_run()` (misc_functions.lua) serializes the *live* G, so
     a real resumable save can't be fabricated for an unplayed seed without transiently
     loading it (which would disrupt the current run). So we DON'T store a save blob.
   - `Brainstorm.bankFoundSeed(seed, slot, joker)` writes a lightweight marker table
     `{brainstorm_found_seed, stake, joker, ts}` to `saveState<slot>.jkr` via the same
     `compress_and_save` the manual slots use. The current run is never touched.
   - The load handler (`Brainstorm_keyhandler.lua`) checks for `.brainstorm_found_seed`:
     if present it starts a FRESH run on that seed via `Brainstorm.applyFoundSeed(seed,
     stake)` (ante 1); otherwise it loads the save blob as before. Real saves never carry
     that key, so no false positives.
   - `applyFoundSeed` now takes an optional `stake` (banked runs restore the stake stored
     at bank time; the live overwrite path still inherits `G.GAME.stake`). Both search
     paths (threaded + sync fallback) funnel through ONE decision point in `updateAutoReroll`
     — search always stops on a find, then either banks (with a `saveManagerAlert`
     "Seed X banked to slot [N]") or overwrites.

8. **Parallel search + hot-path pool cache + diagnostics** (perf pass):
   - **Parallel workers**: `startSearchThread` spawns N `love.thread`s (N = `searchThreads`
     setting, 0 = auto = `getProcessorCount()-1`). They partition ONE seed sequence with no
     overlap — worker `i` tests `k = (tried-1)*N + i`. Shared result channel, first finder
     wins, clearing the session stops all. `pollSearchThread` sums per-thread progress.
     Near-linear speedup. UI: "Search Threads" cycle (replaced "Rerolls per Frame", which
     only ever throttled the sync fallback).
   - **`pseudorandom_element` fast path** (worker copy only): our joker/voucher pools are
     plain string arrays, so the game's sort-by-index is a no-op — it now does
     `_t[math.random(#_t)]` directly, one `math.random` as before, no keys-table/sort.
     RNG-identical; Tag pools (sort_id) keep the original path.
   - **Culled-pool cache**: `buildCulledPools()` precomputes the index-preserving
     `'UNAVAILABLE'` arrays ONCE per search; `getJokerCulledPool`/`getVoucherCulledPool`
     read them. Built only in the worker (fresh Lua state per search → no staleness);
     main thread leaves `Brainstorm.CULLED` nil so its `passesAllFilters` is the untouched
     inline path. Read-only (voucher blanking copy-on-writes). Kill switch:
     `SETTINGS.useCulledCache=false` (threaded via snapshot). Eliminates the per-slot pool
     rebuild that dominated deep multi-ante joker searches.
   - **SAFETY RAIL — re-verify every hit**: `updateAutoReroll` re-runs
     `passesAllFilters(res.seed)` on the MAIN thread (trusted inline pools) before accepting
     a worker result. Agree → accept; disagree → `logSeedMismatch` (writes
     `brainstorm_mismatch.txt`) and keep searching. So a cache/worker bug can only cost
     throughput, never start a wrong seed — and it self-reports.
   - **Diagnostics (Ctrl+D)**: `dumpDiagnostics` → `brainstorm_diagnostics.txt` for the
     current run's seed: enabled settings + per-filter prediction (tag/pack/voucher/joker
     shop slots per ante/legendary/soul) + live comparison + overall `passesAllFilters`.
     `buildDiagnosticsText(seed)` is shared by the mismatch logger. This is the file to
     collect when a search result looks wrong. (Ctrl+B voucher self-test still exists too.)

8. **Stacked-filter search optimization + multi-target correctness fix** (measured
   6.9x single-thread on legendary+voucher+pack+2-joker stacked filters; multiplies
   with worker threads):
   - **Filter reorder** in `passesAllFilters`: soul/legendary block FIRST (~6 rolls,
     rejects ~98.5-99.7%), then tag → pack → voucher → multi-ante jokers LAST. Safe
     because every filter reads its own independent keyed RNG stream — proven by a
     30k-seed old-vs-new equivalence test (0 mismatches).
   - **Single-pass ante simulation** (`simulateShopJokers` / `simulatePackJokers` /
     `getSimulatedPacks`): rolls each ante's shop/pack sequence ONCE and matches all
     targets against the data. **Fixes a real bug**: the old matcher re-walked the shop
     per target while `random_state` persisted, so target 2+ checked slots 7-12 instead
     of 1-6 — old code disagreed with a correct per-target reference on 11% of seeds
     (15k-seed test); new matcher agrees 100%. Old `checkShopJokerSearch` /
     `checkAntePacksForJoker` kept as verified single-target references.
   - **Rarity skip**: pool picks are skipped for rarities outside the target set
     (each rarity has its own pick stream nothing else reads). `getJokerRarity` memoizes
     key→rarity. edisho/edibuf still advance per joker (their Nth advance = Nth joker).
   - **etperpoll dropped**: per real source (common_events.lua:2138) it only feeds
     eternal/perishable flags no filter reads; streams are independent. 30k-seed
     negative-mode equivalence test: 0 mismatches.
   - **Pack filter checks BOTH ante-1 pack slots** via the shared `'shop_pack'..ante`
     stream (one advance per slot, per vanilla create_card_for_shop). Strict superset:
     0 seeds lost, ~70% more matches in test pools. The joker-in-pack matcher now uses
     the same per-(seed,ante)-memoized sim (`Brainstorm._packSim`) — the old per-slot
     keys `'shop_pack'..slot..ante` were likely wrong. **Verify in-game with Ctrl+P**
     (`debugPredictPacks` → debug_predict.txt, use on an ante's first shop).
   - **Found Stake setting** ("Found Stake" cycle: White/Black/Gold → stake 1/4/8,
     `autoreroll.foundSeedStake`): the stake a found run starts at, for both overwrite
     and banked slots. Safe at any stake: stake modifiers only gate what etperpoll/ssjr
     roll VALUES do (game.lua:2055-2059 sets modifiers; common_events.lua:2138-2146
     rolls unconditionally for jokers), so the streams the filters read are identical
     across stakes — the same found seed is valid at White, Black, or Gold.

9. **Per-candidate hot-loop pass** (measured per worker thread, LuaJIT built from
   the same 2.1 branch LOVE embeds: **9.2x** end-to-end worker loop on a tag-first
   config, jit-on (108k/s → 996k/s); 2.4-3.2x on soul/legendary/joker-walk configs;
   still 5.3x/~2x with the JIT forced off. Multiplies with worker-thread count):
   - **`round13` replaces the `string.format("%.13f")` + `tonumber` round-trip** in
     `pseudoseed` — that sprintf+strtod pair ran on EVERY RNG advance of every
     candidate. All-arithmetic and bit-identical: for x in [0,1), n/1e13 (n =
     floor/ceil of x*1e13) is the same double strtod parses from the printed
     13-digit decimal, because n and 1e13 are exact and IEEE division rounds
     correctly. fl(x*1e13) carries at most ~0.001 error, so only the band
     |frac−0.5| ≤ 0.0015 is ambiguous — those ~0.3% of calls fall back to the
     original string path. Fuzz-verified (10M values incl. constructed near-ties +
     200k chained advances, 0 mismatches). Exported as `Brainstorm.round13` for the
     harness.
   - **Sorted-keys cache for Tag picks** (worker `pseudorandom_element`): tag pools
     take the sort_id path, which rebuilt a ~24-entry keys table + `table.sort`
     (Lua comparator) on every candidate seed when a tag filter is first in line.
     The sorted order is a pure function of the pool table, and the only sort_id
     pool a worker ever passes is the static tag-pool snapshot — so the keys array
     is cached per pool table (weak keys). Unique sort_ids ⇒ deterministic order
     regardless of pairs() order ⇒ same element for the same roll.
   - **`random_string` without string churn** (worker copy): fills a reused byte
     buffer and emits ONE `string.char(unpack(...))` instead of 8 per-char
     concatenations, and drops the `string.upper` (every produced byte is already
     an uppercase letter or digit). Same math.random consumption, byte-identical
     seeds.
   - **`boosterCume` precompute** in `buildCulledPools` (+ inline fallback in
     `getSimulatedPacks`, same ipairs order ⇒ bit-identical float sum); settings
     hoisted to a local in `passesAllFilters`; `checkMultiAnteJokerSearch` does its
     predicate-only early-outs before building any tables; worker loop hoists
     `passesAllFilters`/`serializeValue` and the RNG functions localize their
     library lookups.
   - **Considered and rejected**: passing bare RNG keys + `random_state.seed`
     (vanilla-style) instead of baking the seed into every key — ~60 call sites of
     churn for ~0 gain on the dominant workload (first-filter keys are single-use,
     so the concat count per rejected seed is identical).
   - **`tests/search_equivalence.lua`** — permanent harness proving old vs new
     accept/reject EXACTLY the same seeds: it runs each file's real
     `SEARCH_WORKER_SRC` under a fake `love.thread`, compares 15 filter configs
     direct (20k seeds each) + full worker loops incl. a multi-thread partition
     slice, fuzzes round13, and self-checks useCulledCache on vs off. Run:
     `git show HEAD:Brainstorm_reroll.lua > /tmp/baseline.lua && luajit
     tests/search_equivalence.lua --old /tmp/baseline.lua --new
     Brainstorm_reroll.lua [--bench 2]`. All green before shipping any change to
     the filter/RNG code.

10. **Native search helper** (`native/brainstorm_native_search.c`, macOS/POSIX;
    measured on the M1 Max: ~10.7M seeds/s single-thread, ~75M seeds/s on 9
    threads for tag-first rejects, ~30M/s for soul-first stacks — ~8-10x the
    whole Lua worker farm; full 34^8 seed-space sweep ≈ 6.6h):
    - **What it is**: a C port of `passesAllFilters` + every helper (tag, pack,
      voucher incl. per-ante blanking + resamples, soul/legendary + negative,
      multi-ante joker walk with ALL/ANY + per-slot negative and the matchSeq
      first-occurrence rule). Pool ELIGIBILITY IS NOT re-derived in C — the mod
      resolves it with `joker_is_pool_eligible` / the voucher rules and writes
      resolved avail flags into the config, so C stays dumb.
    - **RNG replication**: LuaJIT's Tausworthe TW223 `math.random`/`randomseed`
      ported verbatim from `lj_prng.h`/`lib_math.c`; pseudohash + the round13
      advance are Lua-level IEEE math, so the C build REQUIRES
      `-ffp-contract=off` (build.sh). The one binary-dependent detail — whether
      the game's LuaJIT fused `d*pi+e` in its PRNG seeding — is CALIBRATED AT
      RUNTIME: the mod writes 64 parity checks computed with the game's own
      functions (`buildNativeChecks`), and the helper must reproduce every one
      bit-for-bit before it searches (it picks fma vs plain mode from them;
      this arm64 LuaJIT is fma). Any check failure -> E status -> Lua fallback.
    - **Protocol** (`Brainstorm_reroll.lua` native section): mod writes
      `native_search.cfg` (line format, ends with `end`; helper refuses
      truncated configs / missing checks so garbage can never degrade into
      "accept everything"), spawns the binary detached via `os.execute(... &)`,
      polls `native_search.status` (atomic tmp+rename rewrite: `P tried`,
      `R seed label`, `E msg`, `D`), stops it via `native_search.stop`, and
      touches `native_search.hb` every ~2s — the helper exits on a stale
      heartbeat (>30s) so a crashed game never orphans a CPU burner (also a 6h
      hard cap). Candidates are a partitioned sequential counter over the
      no-0/O charset from an entropy-derived start: zero duplicate work.
    - **Safety rails**, in order: resolved-eligibility config; bit-exact parity
      checks before searching; main-thread `passesAllFilters` re-verify of
      every hit (same rail as the thread search); any error / 5s status
      silence / re-verify mismatch sets `A.nativeFailed` and the love.thread
      search takes over seamlessly. Kill switch:
      `Brainstorm.SETTINGS.useNativeSearch = false` (nil-safe, no UI).
    - **Verification**: `tests/native_equivalence.sh` (uses
      `tests/dump_native_fixtures.lua`; keep its CASES/pools in sync with
      search_equivalence.lua) boots the real worker bootstrap, writes configs
      via the PRODUCTION serializer, and diffs C verdicts+labels against the
      Lua oracle: 15 cases x 30k seeds, byte-identical. Smoke-tested: find/exit,
      stop-file, corrupt config -> E, stale heartbeat -> self-exit. Run it
      before shipping any change to the filters, the config format, or the C.
    - **Interleaved hashing** (implemented): pseudohash is a serial fdiv chain,
      so one candidate is latency-bound; the worker hashes 8 candidates in
      lockstep (`batch_hash_seed`/`batch_hash_key`) and preloads the first
      active filter's stream init (`Config.fsId/fsKey`, mirrors passes()
      order). Bit-exact by construction (each candidate's op sequence is
      unchanged, only temporally interleaved); guarded by an always-on
      `batch_selftest` against the serial reference, and fixture mode runs the
      batched pipeline so the equivalence suite validates it end-to-end.
      Gained +76% (tag path) / +50% (soul-first) over the serial helper.

11. **Deep search phase 1: Anywhere mode + wildcard targets** (antes 1-8 over
    the already-verified per-ante stream models; all 22 fixture cases Lua==C
    byte-identical, and anywhere-OFF verdicts proven unchanged vs baseline):
    - **Anywhere (Antes 1-8)** toggle on the Multi-Ante tab + a Depth cycle
      (4/6/8/12/16 slots/ante): overrides the per-ante rows with one uniform
      window -- every ante 1-8, shops to that depth plus first-shop packs.
      Resolved in ONE place (`Brainstorm.effectiveMultiAnte`) consumed by both
      the Lua filter and the native config writer, so the helper never knows
      about UI modes. Antes 5-8 use the same keyed streams
      (cdt<a>/rarity<a>sho|buf/Joker<r>sho|buf<a>/edisho|edibuf<a>/
      shop_pack<a>/Voucher<a>) the verified 1-4 models use. Depth is capped
      modest on purpose: found seeds must be affordable to reach (rerolls).
    - **Wildcard joker targets**: assign "Any Joker/Common/Uncommon/Rare
      (wildcard)" from the Jokers tab search (keys `*any/*common/*uncommon/
      *rare`, rendered as rarity chips); combined with the slot's Negative
      toggle they search e.g. "any natural negative Rare anywhere". Wildcards
      match by RARITY so the pool pick is skipped entirely (safe: pick streams
      are read by nothing else) -- wildcard-only searches never touch the
      joker pools at all. Matching semantics differ deliberately from specific
      keys: a specific key keeps the first-occurrence rule (that card is what
      you'd see); a wildcard matches if ANY entry of the right rarity passes
      the negative check. Sim sequences now carry `rarity` alongside key/neg.
    - **Voucher ante options extended**: exact antes 5-8 and "Any (1-8)"
      (= -1; 0 stays Any 1-4). Same per-ante `Voucher<a>` keys +
      redeem-nothing blanking, just deeper. Options appended so saved
      searchVoucherAnteID indices stay valid.
    - **Conventions unchanged** (they're what makes deep finds real): pool
      eligibility frozen at fresh-run state -- don't buy pool-affecting items
      before collecting the target; the foundAt label (e.g. "J1A4Shop") says
      where to go. Soul/legendary stays the ante-1 charm-tag convention in
      phase 1. PHASE 2 (needs live-run verification via the Ctrl+B/P/D
      predictors first): big-blind tag (2nd `Tag<ante>` advance), pack slots
      3-6 (shops 2-3 of an ante), per-pack-size soul roll counts, 2nd soul ->
      2nd `Joker4` advance.
    - Settings: `multiAnteSearch.anywhereMode` / `anywhereSlots` (backfilled
      in Brainstorm.lua). C: ante arrays widened to [9], RBASES 29->57,
      NULL-safe token parsing, wildcards parsed only for the four known keys
      (unknown `*` keys behave like never-matching specific keys, same as Lua).

12. **Deep search phase 2 + three source-verified bug fixes** (models extracted
    from the CURRENTLY SHIPPED Balatro.love, freshly unzipped from the app
    bundle -- see scratch notes below; 31 fixture cases Lua==C byte-identical;
    old-vs-new harness diffs confirmed to be EXACTLY the intended fix set:
    tag cases + pack-joker cases changed, everything else identical):
    - **BUG FIX -- tag pool culling** (get_current_pool 'Tag' branch): tags are
      picked from an index-preserving culled STRING array -- requires-center
      not DISCOVERED (profile state!) or min_ante > ante (tag_negative has
      min_ante=2) or banned => 'UNAVAILABLE' + 'Tag'..ante..'_resample'..it.
      The old raw-pool sorted pick diverged whenever the roll landed on a
      culled tag (~1/24 of ante-1 rolls hit tag_negative alone).
      `Brainstorm.rollTag(seed, ante)` + per-ante `CULLED.tag` arrays;
      snapshot/config carry requiresOk (discovery resolved at search time) +
      min_ante per tag.
    - **BUG FIX -- forced first Buffoon** (get_pack first_shop_buffoon): the
      run's FIRST pack is a forced normal Buffoon consuming NO 'shop_pack1'
      advance (variant via raw math.random => matched by kind, never key).
      Ante 1's physical pack slots are [forced, adv1, ...]. getSimulatedPacks
      models it; the pack filter scans physical slots 1-3 (same accept set as
      before for non-buffoon targets; normal-buffoon targets now truthfully
      always match); the pack-joker matcher includes the forced pack's 2 cards
      (they consume rarity1buf/Joker1buf1/edibuf1 FIRST when opened first).
    - **BUG FIX -- pack card counts** = config.extra (Card:open uses
      ability.extra): mega Buffoon is 4 cards, NOT 6 (old model desynced every
      buf stream after a mega buffoon). Arcana 3/5/5, Spectral 2/4/4,
      Celestial/Standard 3/5/5. packCardCount() + snapshot .cards.
    - **Tag: any blind, antes 1-8** (autoreroll.searchTagAnywhere, toggle on
      the core tab): Small rolls before Big per ante (game.lua reset_blinds,
      both via get_next_tag_key); label "TagA<n>Sm|Big"; obtain by skipping
      that blind.
    - **Legendary: any pack, antes 1-8** (autoreroll.searchLegendaryAnywhere):
      finds the run's FIRST Soul. Per ante, packs are scanned in physical slot
      order; Arcana cards roll 'soul_Tarot'..ante once each; Spectral cards
      roll 'soul_Spectral'..ante TWICE (soul then black hole -- create_card
      sets forced_key twice, so a black-hole hit OVERWRITES a soul on the same
      card; after a black hole exists later spectral cards skip the second
      roll). First Soul's legendary = first bare-'Joker4' advance (source: the
      pool key gets the ante appended EXCEPT for legendary), edition =
      'edisou'..ante. Label "LegA<n>P<slot>". CONVENTION: reach the ante with
      pools untouched, open that ante's Arcana/Spectral packs in slot order,
      use the Soul in that ante. searchForSoul is ignored in this mode.
    - Anywhere joker mode now scans all 6 physical pack slots per ante
      (packslots config, 2 per shop x 3 shops); per-ante rows keep the
      first-shop window.
    - **Verify in-game**: Ctrl+P now prints all 6 predicted slots per ante
      incl. the FORCED marker; Ctrl+D prints Small+Big tags for antes 1-2 and
      the legendary-anywhere scan result. Run both on a fresh seed before
      trusting a big hunt (the ante-2+ tag rolls and 6-slot pack list are
      source-verified but not yet live-confirmed).
    - NOTE: `tests/search_equivalence.lua` old-vs-new comparisons are now
      STALE for tag/pack-joker cases against pre-phase-2 baselines (intended
      model fixes). After these changes are committed, HEAD is the valid
      baseline again.

13. **Model 3: skip-aware physical shop layout** (found by a LIVE FAILURE, then
    source-verified; supersedes the "2 per shop x 3 shops = 6 slots/ante" and
    "pack filter scans slots 1-3" claims in item 12; 35 fixture cases Lua==C
    byte-identical; old-vs-new harness diff = the `pack` case only):
    - **The failure**: filter = ante-1 `tag_coupon` + `p_spectral_mega_1` +
      negative `j_gift` in packs, seed `3W9R7L3Y`. In game: forced Buffoon
      (gift card inside) + `p_celestial_normal_2` in the first shop -- both
      predicted -- but the mega Spectral (predicted "slot 3") never spawned.
    - **ROOT CAUSE 1 -- ease_ante timing** (state_events.lua end_round: `if
      Boss then ... ease_ante(1)`): the ante counter ticks when the Boss DIES,
      BEFORE its shop opens. The post-boss shop draws from the NEXT ante's
      streams (its 2 `get_pack('shop_pack')` picks, 'buf' contents, vouchers,
      'sho' jokers -- everything keyed `G.GAME.round_resets.ante`) and shows
      that ante on the HUD. So ante 1 physically has TWO shops (Small, Big =
      forced + 3 picks, 4 slots max -- 'shop_pack1' picks 4-5 NEVER roll), and
      antes 2+ have THREE: the post-boss "entry" shop (picks 1-2), Small
      (3-4), Big (5-6). The pick VALUES per key never changed -- only which
      shop shows them and that ante 1's tail was phantom.
    - **ROOT CAUSE 2 -- blind skips remove shops** (skip_blind just advances
      blind_on_deck: no round, no shop, no picks). Filtering a tag MEANS the
      player skips that blind -- that's how you take a tag -- so the search
      must assume it: the matched blind's shop drops off that ante's layout
      (classic tag = ante-1 Small; anywhere = this seed's TagA<n>Sm|Big;
      classic soul/legendary = ante-1 Small too, the charm-tag convention).
      On `3W9R7L3Y` the skip left ante 1 = [forced Buffoon, pick1] -- the mega
      Spectral at pick 2 was never drawn. Exactly what the user saw.
    - **Implementation**: `skipsFromFilters(tagLoc)` (pure) +
      `setPackSkipAssumption(seed, sm, big)` install a per-seed assumption
      BEFORE any pack consumer (passesAllFilters step 2.4, after the tag roll
      since anywhere-mode skips depend on where THIS seed matched);
      `getSimulatedPacks` builds per-ante physical slot lists (2 x opened
      shops, forced Buffoon leads the run's FIRST opened shop -- it cascades
      to ante 2's entry shop if both ante-1 blinds are skipped) and its memo
      is keyed to the `Brainstorm.random_state` TABLE IDENTITY so a fresh
      evaluation can never extend picks cached under a dead stream state.
      C mirror: `Ctx.skipSm/skipBig/forcedAnte` + `pack_max_slots`, consumers
      read `packs_n[a]` capped by their own window. Shop-JOKER depths are NOT
      truncated by skips (purchases/rerolls restock those streams; pack slots
      can't restock).
    - **Config handshake**: `modelver 3` line, REQUIRED by the helper (`E
      config modelver X != helper model Y`) so a stale binary refuses to
      search instead of silently using the old model. Bump it on every future
      model-semantics change.
    - **Ctrl+P** now prints shop-segmented layouts (`ENTRY[..] SMALL[..]
      BIG[..]`, "Assumed skips: ..." line, anywhere mode prints antes 1-8) and
      installs the same skip assumption the search uses; Ctrl+D installs it
      before the joker/legendary sections. LIVE CHECK for any seed: the shop
      right after beating the ante-N boss must equal `A(N+1) ENTRY[..]`.
    - Semantics note: a pack filter "at ante 1" now means the Small/Big shops
      of ante 1 ONLY (2 free slots without a tag filter, 1 with). The very
      next shop after the ante-1 boss is A2's ENTRY -- players who'd accept it
      should use anywhere/multi-ante pack search instead of the ante-1 filter.

14. **External exhaustive seed-pool builder, phase 1**
    (`native/brainstorm_seed_pool.c`):
    - Built as a separate CLI, but includes `brainstorm_native_search.c` in
      core-only mode. RNG, config parsing, runtime FMA calibration, ordered
      pools, round13, resamples, interleaved hashing, charset/rank mapping,
      and Model-3 pack behavior therefore have one implementation.
    - Deterministically covers an exact half-open numeric range inside all
      `34^8 = 1,785,793,904,896` seeds. Atomic chunk assignment plus bounded
      epochs gives all workers balanced work without duplicates. A checkpoint
      is committed only after every chunk in the epoch finishes and output is
      fsynced; state records both the next rank and output byte boundary, so
      resume first truncates any crash tail.
    - Query schema 1 compiles multiple ANDed
      `tag key minAnte maxAnte minCount` rules in one tag walk, plus one
      `legendary key minAnte maxAnte [negative]` rule. Ante arrays/keys extend
      through 39, so A9 is real rather than aliasing/out-of-bounds. Ranges are
      inclusive. The legendary predicate is deliberately the verified FIRST
      Soul only.
    - Route semantics are explicit: `tag_route collect` selects the first
      required tag occurrences as blind skips before pack/Soul simulation;
      `observe` assumes those blinds are played. A specific first legendary
      is prechecked through bare `Joker4` before the expensive tag/pack walk,
      rejecting about 80% of vanilla candidates cheaply while preserving
      stream equivalence.
    - Output modes: `count` (prevalence/storage sample), `text` (8-char seed
      per line), and canonical `binary` (512-byte versioned/fingerprinted
      header + u64le ranks). `.state` and `.manifest` are sidecars. `export`
      converts a binary pool to text. Binary record order is intentionally
      unspecified because workers flush buffered hit blocks independently.
    - Validation: both helpers build cleanly; ASan+UBSan pass; 100,000 example
      candidates produce identical 408 records in text and binary/export;
      `tests/seed_pool_equivalence.sh` compares one million candidates against
      the old Model-3 path on their overlapping single-tag + first-Soul A1-A8
      semantics with zero verdict differences.
    - Current 100M example sample (Rare + Negative tags A3-A9, collected;
      first Soul Perkeo A1-A6): 403,012 matches = 0.403012%, ~12.3M seeds/sec,
      projected 7.20B u64 records / 57.6GB. Do NOT start broad full scans
      before running count-only mode.
    - Phase-2 integration is intentionally not faked: the game does not read
      `.bspool` yet. The native helper should stream ranks and apply active
      filters; Lua must not load billions. Composition must merge/re-evaluate
      skip routes, since new tag/Soul filters can change which shops/packs
      physically exist. Manifest catalog/query fingerprints are the safety
      contract for that work.

## Not yet built (next steps)

1. **Seed-pool in-game integration** — select a `.bspool`, validate its
   model/catalog/query fingerprints, and have the native helper stream only
   its ranks while compiling the active Brainstorm overlay filters with the
   pool's route effects.
2. **Multi-ante search tab** ("Brainstorm: Ante Search") — independent depth settings per
   ante (e.g. Ante 1 Depth, Ante 2 Depth, Ante 3 Depth, Ante 4 Depth, each 0-8, 0 = skip
   that ante). Loop `checkShopJokerSearch`/`checkPackJokerSearch` over each ante with
   its own depth instead of hardcoding ante=1.
3. **Shop-vs-Pack match indicator** — use the mod's existing `Brainstorm.attention_text()`
   helper to flash "Found in Shop!" or "Found in Pack!" when `auto_reroll` succeeds,
   instead of silently starting the run.
4. **Multi-joker OR search** (discussed, deprioritized) — currently only one joker can be
   searched at a time since all 3 rarity dropdowns write to the same single
   `Brainstorm.SETTINGS.autoreroll.searchJoker` field. Would need to become a list.

## Workflow notes

- Game source extracted to `~/Desktop/balatro_src` (unzipped `Balatro.love`, found at
  `~/Library/Application Support/Steam/steamapps/common/Balatro/Balatro.app/Contents/Resources/Balatro.love`).
  Re-grep this whenever a new mechanic needs verifying — don't assume third-party seed-search
  tool source (Immolate, etc.) is accurate for the current game version; it wasn't, twice.
- Mod lives at `~/Library/Application Support/Balatro/Mods/Brainstorm`, on git branch
  `joker-search-experiment`. `.bak`/`.bak2` files in that folder are manual backups from
  mid-session recovery — safe to delete once everything's confirmed stable, or just leave them.
- Quit Balatro fully (Cmd+Q) and relaunch after every edit — Lovely only reads mod files at launch.
