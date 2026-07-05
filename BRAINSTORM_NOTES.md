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
  slot. (Ante 2+ exclusion is from source, not yet empirically verified past ante 1.)
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
     (`pseudohash`/`pseudorandom`/`pseudorandom_element` copied verbatim from
     `misc_functions.lua`), builds a **mock `G`** from the snapshot (needs `G.FUNCS = {}`
     because reroll.lua assigns `G.FUNCS.change_*` at load), then `load()`s the passed
     reroll.lua source to get the identical filter code, and loops generating seeds.
   - Channels: `brainstorm_search_session` (worker runs while its peek == its session id;
     clearing it stops the worker — love threads can't be force-killed), `..._result`
     (serialized `{seed,jokerFoundAt,session}`), `..._progress` (seeds-tried count).
     `startSearchThread`/`pollSearchThread`/`stopSearchThread` + `updateAutoReroll` drive it.
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

## Not yet built (next steps)

1. **Multi-ante search tab** ("Brainstorm: Ante Search") — independent depth settings per
   ante (e.g. Ante 1 Depth, Ante 2 Depth, Ante 3 Depth, Ante 4 Depth, each 0-8, 0 = skip
   that ante). Loop `checkShopJokerSearch`/`checkPackJokerSearch` over each ante with
   its own depth instead of hardcoding ante=1.
2. **Shop-vs-Pack match indicator** — use the mod's existing `Brainstorm.attention_text()`
   helper to flash "Found in Shop!" or "Found in Pack!" when `auto_reroll` succeeds,
   instead of silently starting the run.
3. **Multi-joker OR search** (discussed, deprioritized) — currently only one joker can be
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
