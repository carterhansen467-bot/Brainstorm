![Brainstorm-mod logo](Assets/BrainstormLogo.jpg)
--
## Requirements
- [Lovely](https://github.com/ethangreen-dev/lovely-injector) injector -- Get it here: https://github.com/ethangreen-dev/lovely-injector/releases

## Installation

1. Install [Lovely](https://github.com/ethangreen-dev/lovely-injector) and follow the manual installation instructions.

### Windows

2. Download the [latest release](https://github.com/OceanRamen/Brainstorm/releases/) of Brainstorm.
3. Unzip the file, and place it in `.../%appdata%/balatro/mods` -- Make sure the Mod's directory name is 'Brainstorm' [^1]
4. Reload the game to activate the mod.

### Macos

2. Clone this repo into your Balatro Mods folder:

   ```bash
   mkdir -p ~/Library/Application\ Support/Balatro/Mods && git clone https://github.com/carterhansen467-bot/Brainstorm.git ~/Library/Application\ Support/Balatro/Mods/Brainstorm
   ```

3. Install the lovely injector into the game folder (downloads the two loader files,
   `liblovely.dylib` and `run_lovely_macos.sh`, from lovely's official releases):

   ```bash
   bash ~/Library/Application\ Support/Balatro/Mods/Brainstorm/install_lovely_macos.sh
   ```

4. **Launch the game from Terminal** — on macOS Steam's Play button does NOT load
   mods (it skips the injector), so start Balatro with:

   ```bash
   ~/Library/Application\ Support/Steam/steamapps/common/Balatro/run_lovely_macos.sh
   ```

5. To update the mod later:

   ```bash
   git -C ~/Library/Application\ Support/Balatro/Mods/Brainstorm pull
   ```

   > **Updating from an older version?** If `git pull` complains about local
   > changes to `settings.lua`, run this once (your in-game mod settings reset
   > to defaults; newer versions store settings outside of git so this never
   > happens again):
   >
   > ```bash
   > git -C ~/Library/Application\ Support/Balatro/Mods/Brainstorm checkout -- settings.lua && git -C ~/Library/Application\ Support/Balatro/Mods/Brainstorm pull
   > ```

## Features
### Save-States
Brainstorm has the capability to save up to 5 save-states through the use of in-game key binds. 
> To create a save-state: Hold `z + 1-5`
> To load a save-state:	Hold `x + 1-5`

Each number from 0 - 5 corresponds to a save slot. To overwrite an old save, simply create a new save-state in it's slot. 

### Fast Rerolling
Brainstorm allows for super-fast rerolling through the use of an in-game key bind. 
> To fast-roll:	Press `Ctrl + t`

### Auto-Rerolling
Brainstorm can automatically reroll for parameters as specified by the user.
You can edit the Auto-Reroll parameters in the Brainstorm in-game settings page.
> To Auto-Reroll:	Press `Ctrl + a`

### Deep search: tags and legendaries anywhere (antes 1-8)
Two more toggles on the main Brainstorm tab: **Tag: any blind, antes 1-8**
matches the searched tag on any Small/Big blind up to ante 8 (labels like
`TagA3Big` -- skip that blind to take the tag). **Legendary: any pack, antes
1-8** finds the run's first Soul in any Arcana/Spectral pack up to ante 8
(labels like `LegA7P5` = ante 7, pack slot 5; open that ante's Arcana/Spectral
packs in slot order and use the Soul there). Combine with the Negative toggle
to hunt natural negative legendaries.

### Deep search: Anywhere mode + wildcards
On the **Multi-Ante** tab, toggle **Anywhere (Antes 1-8)** to search every ante
1-8 at one uniform shop depth (packs included) instead of configuring antes
individually. On the **Jokers** tab, search for "wildcard" to pick **Any
Common/Uncommon/Rare/Joker** targets -- combine with a slot's **Negative**
toggle to hunt e.g. a natural negative Rare anywhere in the first 8 antes. The
Voucher Ante cycle likewise gains antes 5-8 and **Any (1-8)**. Found seeds
report where the target appears (e.g. `J1A4Shop` = slot-1 joker in ante 4's
shop); reach it by playing to that ante without buying pool-affecting items
first, and reroll the shop to reveal the configured depth.

### How pack predictions map to shops (and blind skips)
The ante counter ticks the moment a Boss dies, so the shop right after a boss
already belongs to the NEXT ante. Per ante that means: ante 1 has two shops
(after Small, after Big); antes 2+ have three (the post-boss "entry" shop,
then after Small, after Big), two pack slots each. The run's very first shop
leads with the game's forced normal Buffoon pack. **Skipping a blind removes
its shop entirely** -- and taking a tag means skipping -- so when your filter
includes a tag (or the classic Soul/legendary filter, which relies on the
charm-tag skip), the search assumes you skip that blind and only matches packs
in the shops you'll actually see. Play everything else without skipping, or
later pack predictions for that ante shift. `Ctrl+P` prints the exact
shop-by-shop layout (with the assumed skips) for your current seed.

### Native search accelerator (macOS, optional)
Filtered searches normally run on background Lua threads. On macOS you can
additionally build a small native helper that searches ~8x faster across all
CPU cores (~75M seeds/sec on an M1 Max). Build it once:

```bash
sh ~/Library/Application\ Support/Balatro/Mods/Brainstorm/native/build.sh
```

The mod detects the binary automatically on the next launch and uses it for
`Ctrl + a` searches; every hit is still re-verified in-game before a run
starts, and the mod falls back to the Lua search if the helper is missing or
disagrees. Remove the binary (or set `useNativeSearch = false` in
settings.lua) to go back to pure-Lua searching.

### Exhaustive external seed-pool builder (phase 1)

`native/build.sh` also builds `native/brainstorm_seed_pool`, a standalone
program for exhaustively scanning an exact numeric range of Balatro's full
`34^8 = 1,785,793,904,896` eight-character seed space. Unlike the in-game
first-hit search, it never samples, wraps, or stops after one match. It can
combine multiple ranged tag constraints with a first-Soul legendary
constraint and write every match.

The scanner reads two inputs:

1. `native_search.cfg`, Brainstorm's runtime snapshot of ordered pools,
   unlock/discovery state, bans, pack definitions, and game-computed RNG
   parity checks. Build the native helpers, launch the game, and briefly start
   a native auto-search to refresh this file for the profile you intend to
   scan.
2. A pool criteria file. [`native/seed_pool.example.cfg`](native/seed_pool.example.cfg)
   expresses: Rare Tag and Negative Tag, each at least once during antes 3-9,
   plus the run's first Soul yielding Perkeo before ante 7 (the inclusive
   range is written explicitly as antes 1-6).

Run its checked-in 100-million-seed count-only sample from the repository
root:

```bash
mkdir -p seed_pools
./native/brainstorm_seed_pool scan native_search.cfg \
  native/seed_pool.example.cfg seed_pools/rare-negative-perkeo-count
```

The `.manifest` reports the observed match rate, throughput, projected
full-space record count, and projected binary size. On the current development
machine, that sample found 403,012 matches (0.403012%) at about 12.3 million
seeds/second, projecting roughly 7.20 billion records and 57.6 GB before the
512-byte header. Treat that as an estimate; the active pool/profile snapshot
can change it.

For the exhaustive run, copy the example criteria and change:

```text
count all
format binary
```

Then run the same command with an output name ending in `.bspool`. The
canonical pool stores each match as an eight-byte little-endian numeric seed
rank. Its 512-byte header includes schema/model versions, the scanned range,
record count, completion flag, alphabet, and catalog/criteria fingerprints.
The adjacent `.state` is an atomic checkpoint containing the committed cursor
and byte boundary; rerun the identical command to resume safely after Ctrl+C
or a crash. The adjacent `.manifest` records the criteria and final statistics.

Small pools can instead use `format text` for one seed per line. A completed
binary pool can be exported later with:

```bash
./native/brainstorm_seed_pool export seed_pools/example.bspool \
  seed_pools/example.txt
```

Criteria directives currently supported are:

```text
tag <key> <inclusive-min-ante> <inclusive-max-ante> <minimum-count>
legendary <key> <inclusive-min-ante> <inclusive-max-ante> [require-negative]
tag_route collect|observe
```

`tag_route collect` selects the first required matching tag occurrences as
actual blind skips. Those missing shops are fed into the Model-3 physical pack
simulation before Souls are checked. `observe` requires the tags but assumes
their blinds are played. The legendary rule intentionally means the run's
**first Soul**; later-Soul pool mutation has not yet been source-verified.

This is phase 1: the external builder and versioned pool contract are present,
but the in-game UI does not consume `.bspool` files yet. That integration
should stream ranks through the native helper instead of loading billions of
seeds into Lua. It must also combine route-changing in-game filters with the
base pool's recorded tag route before re-verification.

To compare the generalized engine with the established Model-3 path over
their overlapping A1-A8 semantics:

```bash
tests/seed_pool_equivalence.sh native_search.cfg 1000000
```
