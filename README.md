![Brainstorm-mod logo](Assets/BrainstormLogo.jpg)
--
## Requirements
- [Lovely](https://github.com/ethangreen-dev/lovely-injector) injector -- Get it here: https://github.com/ethangreen-dev/lovely-injector/releases

## Installation

1. Install [Lovely](https://github.com/ethangreen-dev/lovely-injector) and follow the manual installation instructions.

### Windows

2. Install [Lovely](https://github.com/ethangreen-dev/lovely-injector/releases) for
   Windows: put its `version.dll` in the Balatro game folder (right-click Balatro
   in Steam → Manage → Browse local files).
3. Easiest: download **`brainstorm-windows-full.zip`** from this fork's
   [releases](https://github.com/carterhansen467-bot/Brainstorm/releases),
   extract the whole ZIP, and double-click **`Install or Update
   Brainstorm.bat`**. It installs the mod, matching native helpers, and the
   standalone Seed Pool Builder and Organizer into
   `%AppData%\Balatro\Mods\Brainstorm`.

   To use Git instead, paste this into a Command Prompt:

   ```bat
   cd %AppData%\Balatro\Mods
   git clone -b joker-search-experiment https://github.com/carterhansen467-bot/Brainstorm.git Brainstorm
   ```

   A branch source ZIP also works for development, but it does not contain the
   compiled Windows helpers. For a normal install, use the full release.
4. If Windows blocks a downloaded `.exe`: right-click → Properties → check
   **Unblock**, or choose "More info → Run anyway" on the SmartScreen prompt.
5. Reload the game to activate the mod.

To update a ZIP installation—including the previous win-v9 layout—quit Balatro
and the Seed Pool Builder or Organizer, extract the new full package, and run
**`Install or Update Brainstorm.bat`**. The updater checks the package, replaces only shipped
files, preserves `seed_pools`, `settings.lua`, `native_search.cfg`, and scan
checkpoints, and removes the two obsolete duplicate executable locations. On
the next launch, briefly toggle Ctrl+A on and off once to refresh the snapshot
for the new version. `README-WINDOWS.txt` in the ZIP includes a manual fallback.

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

The main Brainstorm tab shows the estimated seeds per match and, after
Brainstorm has measured this search/backend once, an estimated search time.
During a search the corner panel reports the backend-confirmed seeds checked,
wall-clock time elapsed, measured seeds/second, average time remaining, and the
cumulative chance of a hit by that point. Candidate odds use the active
profile's eligible pools, booster weights, physical pack route, and configured
joker windows; time uses observed throughput on this machine. These are
analytical planning estimates. Automatically attached pools contain enriched
candidates rather than random full-space samples, so the chance is marked
unavailable during those phases and rebased if unrestricted generation begins.
For a higher-confidence match-rate measurement, the Seed Pool Builder's
**Quick estimate** actually evaluates a 2M-seed sample.

### Deep search: tags and legendaries anywhere (antes 1-8)
Two more toggles on the main Brainstorm tab: **Tag: any blind, antes 1-8**
matches the searched tag on any Small/Big blind up to ante 8 (labels like
`TagA3Big` -- skip that blind to take the tag). **Legendary: any pack, antes
1-8** finds the run's first Soul across the Arcana/Spectral packs you can
actually open up to ante 8. That route includes reachable shop packs and any
immediate Charm/Ethereal reward taken by the active tag filter. Labels such as
`LegA7P5` identify a shop-pack slot; `LegA4CharmBig` or
`LegA3EtherealSm` identify the reward opened by skipping that blind. Combine
with the Negative toggle to hunt natural negative legendaries.

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

### Native search accelerator (optional)
Filtered searches normally run on background Lua threads. You can additionally
install a small native helper that searches ~8x faster across all CPU cores
(~75M seeds/sec on an M1 Max).

**macOS** — build it once (needs the Xcode command line tools):

```bash
sh ~/Library/Application\ Support/Balatro/Mods/Brainstorm/native/build.sh
```

**Windows** — no compiler needed: drop `brainstorm_native_search.exe` and
`brainstorm_seed_pool.exe` from the release zip into
`%AppData%\Balatro\Mods\Brainstorm\native\` (building from source instead:
`native/build_windows.sh` with [zig](https://ziglang.org/download/) or a
MinGW-w64 gcc).

The mod detects the binary automatically on the next launch and uses it for
`Ctrl + a` searches; every hit is still re-verified in-game before a run
starts, and the mod falls back to the Lua search if the helper is missing or
disagrees. On every launch the helper must first reproduce a set of RNG
parity values computed by *your* game's own Lua runtime, bit for bit, or it
refuses to run — the same guarantee on both platforms. Remove the binary (or
set `useNativeSearch = false` in settings.lua) to go back to pure-Lua
searching.

### Exhaustive external seed-pool builder (phase 1)

`native/build.sh` also builds `native/brainstorm_seed_pool`, a standalone
program for exhaustively scanning an exact numeric range of a seed space.
Unlike the in-game first-hit search, it never samples, wraps, or stops after
one match. It can combine multiple ranged tag constraints with an exact
first- or second-Soul legendary constraint and write every match.

Three seed spaces are supported:

- **natural** (default): `34^8 = 1,785,793,904,896` eight-character seeds
  over the game's generation alphabet (no `0`, no `O`) — every seed the game
  can actually deal you.
- **settable** (`space settable`): `35^1 + ... + 35^8 =
  2,318,107,019,760` seeds — every seed vanilla's seed box can preserve.
  This adds `O` and 1-7 character seeds while excluding `0`, because vanilla
  changes every typed or pasted `0` to `O`.
- **total** (`space total` in the criteria): `36^1 + ... + 36^8 =
  2,901,713,047,668` seeds — every possible set seed, including `0`, `O`,
  and 1-7 character seeds. A seed containing `0` requires Brainstorm's
  **Illegal Seed Input** option because vanilla input would change it.

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
full-space record count, projected compressed and legacy sizes, and the final
compressed bytes per record. On the current development
machine, that sample found 403,012 matches (0.403012%) at about 12.3 million
seeds/second, projecting roughly 7.20 billion records and 57.6 GB before the
1 KiB header in the legacy format. The current indexed delta format projects
about 11.6 GB for the same match rate. Treat both as estimates; the active
pool/profile snapshot can change them.

For the exhaustive run, copy the example criteria and change:

```text
count all
format binary
```

Then run the same command with an output name ending in `.bspool`. New scans
use schema 3: sorted numeric seed-rank deltas and a block-local occurrence
metadata section share one CRC-protected block, followed by an index for fast
randomized access. Its 8 KiB header includes schema/model versions, the scanned
range, record count, completion flag, alphabet, catalog/criteria fingerprints,
and the criteria themselves, leaving room for lineage and route identity while
keeping a shared `.bspool` self-describing. Compression changes only the
representation: seed evaluation, checkpoint boundaries, and exhaustive search
semantics are unchanged. Schema-1 and schema-2 pools remain readable; schema-2
conversion is kept as the compatibility target for finished legacy files.

Current helpers still read existing schema-1 `u64le` pools. To shrink a
finished legacy pool without rescanning the seed space, give the output a new
filename:

```sh
./native/brainstorm_seed_pool convert seed_pools/legacy.bspool \
  seed_pools/legacy-compressed.bspool
```

Windows Command Prompt uses the same operation from the Brainstorm folder:

```bat
native\brainstorm_seed_pool.exe convert seed_pools\legacy.bspool seed_pools\legacy-compressed.bspool
```

Conversion streams the old file, preserves its pool ID and embedded criteria,
and reports the measured reduction. Keep the original until an export or
refilter of the converted pool has been verified. If conversion is interrupted,
delete the incomplete output and run it again; the input is never modified.
Older Brainstorm helpers do not understand the current schema-3 format, so
anyone receiving a newly generated pool must also update both native helpers.

#### Filtering an existing pool again

The builder can use any finished `.bspool` as its input instead of scanning
an entire seed space. Under **Input seeds**, choose a pool, set the next
requirements, and build with a new name. Every result satisfies both the
source pool's guarantees and the new criteria; the process can be repeated
to make progressively smaller pools before applying aggressive in-game
filters such as exact joker positions.

```sh
./native/brainstorm_seed_pool refilter native_search.cfg \
  next-filter.cfg seed_pools/input.bspool seed_pools/refined.bspool
```

Pools with checksummed committed seeds from the same model and unlock/pool
snapshot are accepted, including paused or split snapshots. Natural,
vanilla-settable, and total pools are supported, and the source pool decides
the seed space. The output must have a different filename.
Derived headers/manifests retain the source pool ID, criteria fingerprint,
record count, and refilter depth.
The adjacent `.state` is an atomic checkpoint containing the committed cursor
and byte boundary; rerun the identical command to resume safely after Ctrl+C
or a crash. The adjacent `.manifest` records the criteria and final statistics.

#### Split one exhaustive search across computers

The Seed Pool Builder can divide a scope into 2–256 exact, non-overlapping
parts. Choose the same criteria, seed space, scope, and **Split across
computers** count on every machine, then assign a different **part** to each.
For example, for every natural seed with Perkeo from the first Soul in antes
1–6, select Perkeo, antes 1–6, Entire seed space, 4 parts, and run parts 1, 2,
3, and 4 on four computers.

Part boundaries use integer half-open ranges:

```text
start = floor(seed_count * (part - 1) / parts)
end   = floor(seed_count * part / parts)
```

Consequently every rank is searched exactly once even when the space does not
divide evenly. Output names automatically gain `part-N-of-M`. All machines
must use the same `native_search.cfg` pool/unlock snapshot; copying the
coordinator's snapshot to the other Brainstorm folders is the simplest way to
guarantee this. A mismatch is rejected during merge rather than silently
combining different results.

When every part is complete, check them in the builder's **Merge distributed
parts** section and choose an output name. The command-line equivalent accepts
two or more inputs in any order:

```sh
./native/brainstorm_seed_pool merge seed_pools/perkeo-a1-6-full.bspool \
  seed_pools/perkeo-a1-6-part-1-of-4.bspool \
  seed_pools/perkeo-a1-6-part-2-of-4.bspool \
  seed_pools/perkeo-a1-6-part-3-of-4.bspool \
  seed_pools/perkeo-a1-6-part-4-of-4.bspool
```

The merger verifies completion, model, profile/catalog fingerprint, seed
space, criteria fingerprint, checksums, and contiguous non-overlapping ranges.
It refuses gaps, overlaps, corrupted parts, or pools built for different
filters. The merged file receives the same pool ID and membership that one
computer scanning the combined range would have produced; source files are
never changed. Combining unrelated pools as an OR-union is intentionally not
allowed because no single embedded criterion would truthfully describe every
record.

On Windows the standalone `Seed Pool Builder.exe` exposes both controls. The
equivalent Command Prompt command begins with
`native\brainstorm_seed_pool.exe merge ...`.

Small pools can instead use `format text` for one seed per line. A completed
binary pool can be exported later with:

```bash
./native/brainstorm_seed_pool export seed_pools/example.bspool \
  seed_pools/example.txt
```

Criteria directives currently supported are:

```text
tag <key> <inclusive-min-ante> <inclusive-max-ante> <minimum-count>
tag <key> <min-ante> <small|big> <max-ante> <small|big> <minimum-count>
legendary <key> <inclusive-min-ante> <inclusive-max-ante> [require-negative]
legendary <key> <min-ante> <boss|small|big> <max-ante> <boss|small|big> <negative-0|1> <any|shop|charm|ethereal>
legendary_routes full|canonical_charm
soul_depth 1|2|any
voucher <key> <inclusive-min-ante> <inclusive-max-ante>
voucher_exclude <key>
tag_route collect|observe
space natural|settable|total
label <any name, spaces allowed>
```

`label` names the pool; it is embedded in the `.bspool` header together with
a `pool_id` (a short fingerprint of catalog + criteria + range + space +
records), and both are shown by the Seed Pool Builder and the in-game
selector — two people holding the same pool see the same id.

`legendary_routes full` is exhaustive. `canonical_charm` is the Fast Exact
subset: it retains canonical Shop and targeted Charm routes but skips automatic
Omen Globe purchase recovery. The nondefault policy is part of the pool's
criteria identity, so incompatible scans cannot resume or merge together.

`tag_route collect` selects the first required matching tag occurrences as
actual blind skips. Those missing shops are fed into the source-verified
physical pack simulation before Souls are checked. Taking a Charm or Ethereal
Tag also inserts its forced Mega Arcana or Spectral reward at that exact point
in the route, so a legendary may come from either a reachable shop pack or one
of those collected rewards. `observe` requires the tags but assumes their
blinds are played, so it neither removes a shop nor opens a tag reward.
Occurrences outside a tag rule's Ante window are never collected: for example,
a Negative Tag requested only in antes 3-9 cannot remove a Perkeo shop in ante
1 or 2. Legendary rules default to exact depth 1.
`soul_depth 2` is exclusive: the first Soul must yield a different legendary,
be used, and remain owned; the target must come from the second Soul in a
later pack. A seed with the target on Soul #1 does not match depth 2. Showman,
selling or destroying the first legendary, or skipping the first Soul changes
the pool and is outside this route convention. The ante window applies to the
target Soul itself, so Soul #1 may occur before the window at depth 2.

Voucher rules search the exact per-Ante offer streams and retain the route
with the fewest purchases. An excluded voucher may appear, but the route may
not require buying it; this makes exclusions suitable for merchant or upgrade
paths the player refuses to take. Buying prerequisites dynamically changes
later voucher pools, and voucher-only searches model repeated Antes from
Hieroglyph and Petroglyph. In mixed Legendary searches, Omen Globe is the
voucher whose purchase timing is composed into every later Arcana card. If the
canonical pack route misses, the scanner also tests each actually rolled Charm
Tag as one targeted five-card alternate and records when that skip is required.

### Seed Pool Builder app (no terminal knowledge needed)

Double-click **`Seed Pool Builder.command`** in the mod folder (Windows:
**`Seed Pool Builder.bat`**, which launches the packaged app without requiring
Python). It opens a point-and-click page in your
browser (running only on your computer -- nothing is installed and nothing
leaves the machine) where you can:

- pick a Legendary with an exact Ante/blind/source window and optional
  Negative, choose first, second, or either Soul, and add exact tag windows;
- choose **Exhaustive** Legendary routes (Shop, Charm, and automatic
  Omen-purchase recovery) or **Fast Exact** (Shop and Charm). Fast Exact keeps
  every retained seed valid and restores most of the former search speed, but
  deliberately omits the small fraction of seeds that work only after the
  scanner finds and purchases Omen Globe;
- see every active filter at the top of the form and whether the scan will use
  the tags-only fast path or the substantially heavier exact Legendary route;
- start with no Legendary selected, so a tags-only build cannot silently keep
  Perkeo active from the Builder's defaults;
- add multiple voucher targets with Ante windows and purchase exclusions; the
  result records the minimum qualifying buy route;
- use a paused, split, or otherwise incomplete `.bspool` as an input—the
  refilter processes only its checksummed committed seeds and keeps its
  provisional lineage explicit;
- choose the **seed space**: natural seeds only (the 1.79 trillion the game
  can deal you), every vanilla-settable seed (2.32 trillion — adds `O` and
  short seeds but excludes `0`), or every possible seed (2.90 trillion —
  includes `0` and requires Brainstorm's Illegal Seed Input support for
  affected results);
- press **Quick estimate** to sample 2M seeds with frequent progress updates
  and see separate projections for the selected build scope and the complete
  chosen seed space. “Complete chosen seed space” means Natural,
  Vanilla-settable, or All possible—whichever is selected—not implicitly the
  Natural space. If a rare filter produces fewer than 25 sample matches, the
  app labels the size projection as rough;
- press **Build pool** and watch live progress. Pressing **Pause scan**, closing
  the launcher's Terminal/console window, or using **Close Builder** stops at a
  checkpoint; pressing Build again with the same name resumes exactly where it
  left off -- safe across reboots, so a days-long full-space scan can be done in
  sittings. Closing only the browser tab leaves the local Builder running;
- split an exhaustive scope into non-overlapping numbered parts for multiple
  computers, then validate and merge the finished shard files in the same app;
- switch to **Organize / Combine** in that same Seed Pool Tools window to split
  by recorded locations or perform a streaming **Union**, **Intersection**, or
  **Difference** across unrelated pools—even when their base filters differ;
- see every finished pool with its embedded criteria, its name, and its
  **pool id** -- a short fingerprint also shown by the in-game selector, so
  two people can confirm they're holding the same pool;
- opt a completed natural-space pool into **Attach to Brainstorm**. A partial
  range becomes an accelerator that falls back to unrestricted generation on
  exhaustion; only proven full `34^8` coverage becomes authoritative. Detach
  removes only the small local policy marker, and deleting a completed pool
  removes that marker safely;
- delete a completed pool and its checkpoint/manifest/attachment sidecars with
  an identity-bound two-step confirmation. Finished pools land
  in `seed_pools/` and appear in the in-game Seed Pool selector
  automatically; sharing a pool = sending someone that one `.bspool` file
  to drop into their own `seed_pools/` folder. A tiny `.writer.lock` may remain
  after deletion; it is an intentional reusable coordination inode, contains
  no seed data, and prevents cross-process writer races.

First-run notes: macOS may ask to install the Command Line Developer Tools
(the app compiles the scanner once, and `python3` ships with those tools) --
accept and re-run. If macOS blocks the double-click because the file came
from the internet, right-click it and choose **Open** the first time. On
Windows the app doesn't compile anything -- it uses the prebuilt
`brainstorm_seed_pool.exe` beside it in `Seed Pool Builder/` and will say so if
it is missing. The app also needs a current `native_search.cfg`, which Brainstorm
writes when you toggle an auto-reroll in-game (Ctrl+A on, then off); the page
will tell you if it is missing or stale. Prefer a terminal UI? The same tool exists as
`python3 tools/brainstorm_pool_builder.py` (arrow-key menus, curses;
macOS/Linux only -- Windows Python has no curses, use the browser UI).

The Windows release groups the Seed Pool Tools UI and exhaustive scanner under one
`Seed Pool Builder/` folder. A complete physical split from the mod would be
counterproductive: Lovely needs `lovely.toml` and the loaded Lua files at the
mod root, the running game expects its native search helper under `native/`,
and both the game and builder intentionally share root-level
`native_search.cfg` and `seed_pools/`. Those are the only filesystem
constraints; the builder's own executables no longer mix with the game helper.

Launch the regular Seed Pool Builder and select **Organize / Combine** to work
with existing files. The older **`Seed Pool Organizer.command`** (macOS) and
**`Seed Pool Organizer.bat`** (Windows) entry points remain as compatible direct
shortcuts. Exact-location splits show ambiguous seeds for an explicit category
choice; incomplete sources use only their committed checkpoint; and every
derivative records the source snapshot and lineage.

General combining is separate from **Merge distributed pool parts**. The shard
merge remains the strict fast path for completed, contiguous parts of one
identical search. The organizer accepts 2–64 compatible recorded pools and:

- **Union** keeps a seed found in any selected pool;
- **Intersection** keeps a seed found in every selected pool;
- **Difference** keeps seeds from the chosen base pool unless they occur in any
  other selected pool;
- deduplicates ranks while merging every available BSP3 occurrence descriptor;
- records both the exact input-snapshot expression and, per seed, every
  original source-filter branch it matched, so combining a Perkeo pool and a
  Negative Tag pool remains `Perkeo OR Negative Tag` rather than being
  mislabeled as a single `Perkeo AND Negative Tag` filter;
- accepts paused/provisional inputs by reading only their checksummed committed
  records and marks the result's coverage provisional; and
- refuses differing RNG model, catalog/profile snapshot, or seed-space
  encodings instead of producing a pool whose ranks could be misinterpreted.

Composite pools are valid membership sets in-game and as later Builder inputs.
Their exact input-snapshot expression and multiple original routes are retained
as organizer provenance. When an existing `A AND B` pool is later unioned with
`C`, that first pool remains one exact snapshot operand; it is never flattened
into the incorrect `A OR B OR C`. A new search applies its current filters to
those recorded members rather than silently stacking every source route into
one condition. Previously combined pools can be combined or location-split
again without losing their snapshot expression or source-filter map.

### Using a seed pool in-game (phase 2)

Drop the finished `.bspool` into `Mods/Brainstorm/seed_pools/` (the folder is
created the first time the Brainstorm settings tab opens). A **Seed Pool**
selector appears in the Brainstorm tab listing every pool in that folder;
pick one and start an auto-reroll as usual. While a pool is selected, the
native helper only considers seeds recorded in the pool -- your other active
Brainstorm filters still apply on top of it, and each search starts at a
random position in the pool so repeat searches surface different seeds.

For a pool with complete source coverage, a search that covers every record
without a hit is a definitive verdict: the mod stops and reports that no seed
in the pool matches the current filters. Paused and otherwise incomplete pools
can also be selected or refiltered; only their checksummed, committed records
are processed. Exhausting one of those snapshots is explicitly provisional,
and every derived pool retains `coverage_complete 0` even after its own
refilter finishes. Pool searches never fall back to the full-space Lua thread
search -- that could return a seed outside the selected pool. They require the
native helper (macOS: built by `native/build.sh`; Windows: release-zip exes).

An attached pool is different from a manual selection. With **Seed Pool: None**,
Brainstorm may automatically choose a compatible `.attached` pool whose
embedded predicate is proven broader than the active request. Manual selection
always wins. Automatic records still pass the native filters and the independent
main-thread Lua verification. Brainstorm tries compatible attachments from
fewest to most records; an exhausted or invalid accelerator advances to the
next safe attachment and then to unrestricted generation. An authoritative
full-natural-space pool revalidates its marker and file identity before it may
report definitive exhaustion. The initial conservative matcher supports classic tag
and classic Legendary/Charm relationships, including wider ending windows,
optional Negative, first-or-second Soul breadth, and full versus Fast Exact
coverage. Voucher, Legendary-anywhere, composite, tag-anywhere, and ambiguous
route/source relationships are ignored for automatic selection until their
implication rules have separate differential proofs; they remain available via
the manual selector.

**Sharing pools:** send someone the single `.bspool` file; they drop it into
their own `Mods/Brainstorm/seed_pools/` folder. The header carries the model
version, the criteria that built it, and a fingerprint of the unlock/pool
snapshot it was scanned against. The recipient must have the same model and
ordered profile/unlock snapshot; otherwise the helper refuses the pool and asks
for a rebuild. A warning is not sufficient here because booster bans, tag pool
order, and legendary availability can change the pool's guarantees.

Pool criteria and current in-game filters are evaluated on one cumulative
route. Every collected tag from every refilter stage removes its actual shop;
inherited Soul #1/#2 constraints are then rechecked on that final route before
a hit can be returned. The main-thread Lua filter independently repeats those
embedded tag, pack, Soul, Ante, edition, and legendary checks before applying
the seed.

A refilter always remains a strict subset of its input `.bspool`; it never
invents or searches seeds outside that file. Collected tags can change the
first/second Soul route, so filter order matters to the available candidate
set: for an exhaustive “tag plus legendary” result, build the tag pool first
and then refilter for the legendary, or scan both requirements together. If a
legendary-only pool is the input, adding a collected tag correctly revalidates
its members but cannot recover outside seeds that the new route would have made
valid.

Refiltering a paused pool is a snapshot operation: it processes exactly the
records committed when the operation starts. The standalone Builder labels the
result as a filtered snapshot whose source is incomplete, so it cannot be
mistaken for an exhaustive result over the source's full declared range.

**Model 6 migration:** pools made by model 5 or older cannot be searched or
refiltered by model 6. Model 6 adds collected Charm/Ethereal rewards to the
chronological Soul route; accepting an older pool would silently omit possible
Soul #1/#2 events. Old pools can still be exported, but must be rebuilt for
trustworthy in-game use. After updating, launch Balatro, toggle Ctrl+A on and
off once to refresh `native_search.cfg`, then rebuild the pool.

To compare the generalized engine with the established reference path over
their overlapping A1-A8 semantics, and to exercise the in-game pool search
end-to-end (member hit + definitive exhaustion):

```bash
tests/seed_pool_equivalence.sh native_search.cfg 1000000
tests/pool_search_equivalence.sh native_search.cfg 3000000
```

## Vectorized first gates (2026-07-18)

Lane-parallel "first gates" now reject most candidates from the first RNG
draw(s) of an eight-seed hashed group before the scalar filter chain runs,
in both the in-game searcher and the Seed Pool Builder. A gate may only
reject on evidence the scalar chain must also reject on (decided
`math.random(n)` buckets or certain sub-threshold rolls from output bits
44..51); anything undecided — bucket boundaries, culled resamples — reruns
the unchanged scalar path, which remains the single membership authority.
`BRAINSTORM_VECTOR_GATE=0` disables every gate at runtime, and building
with `-DBRAINSTORM_VERIFY_VECTOR_GATE` re-checks each rejection against
the scalar evaluator.

### Completed

- **Builder** (`brainstorm_seed_pool`): Joker4 first-draw gate for
  all-depth-1 legendary plans; survivor rebatching that batch-hashes Tag1
  once eight legendary-gate survivors accumulate and gates the ante-1
  Small tag pick (rules pinned to that window, natural space); decided
  first picks hand their post-draw stream state to the evaluator instead
  of recomputing the draw. Measured on M1 Max: Perkeo+Charm build **1.70x**
  single-thread / **1.83x** all-core, legendary-only 1.10x, pinned
  tag-only 1.27x.
- **Searcher** (`brainstorm_native_search`, both full-space and poolfile
  workers): FS_SOUL ante-1 reward-pack roll chain (**1.94x** on the
  classic Soul/legendary search), FS_LEGEND legendary-anywhere (**1.45x**
  total after handing its decided pick/state to the scalar evaluator),
  ante-1 FS_TAG (1.28x), and exact-Ante FS_VOUCH (**1.64x** with the
  corrected family-specific benchmark).
  Unrestricted FS_PACK also projects the exact weighted booster picker onto
  conservative high-byte intervals: **2.52x** on a two-target pack filter and
  **1.67x** on a single-target filter. Attached-pool pack searches remain
  scalar because collected route tags can change shop skips and draw counts.
- **Verification**: 14M-seed gate-vs-scalar differential harness across
  every gate kind (zero drops over 300k+ passing seeds), 200M-rank
  handoff divergence checks, byte-identical single-thread scan and
  refilter pools with gates on/off, and the Lua-oracle, soul-depth,
  fastpath, search/refilter/partial/lineage/shard, voucher-route, and
  Omen/Charm suites. `tests/vector_gate_equivalence.sh` keeps a bounded,
  non-vacuous version of the five-kind differential and byte-identical
  Builder checks in CI on macOS and Windows. It also exhausts every high-byte
  bucket interval for catalog sizes 1 through 256; duplicate modded target
  keys conservatively disable index-specific tag/voucher gates.

### Evaluated and rejected

Any-window voucher gates and the anywhere-tag gate measured as net losses
(0.88-0.93x): with roughly half the voucher catalog culled early-run,
"every roll decided-missed" is too rare to pay for the per-Ante hashes.
Removed per the keep-only-measured-gains rule in `FUTURE_CHANGES.md`.

### Remaining

- Searcher-side survivor rebatching (e.g. Soul-gate survivors through a
  batched legendary pick). The direct FS_LEGEND pick/state handoff is complete;
  Soul survivors are only 1.93% in the bounded fixture, so a second staging
  layer remains contingent on a measured gain that justifies its complexity.
- Cost/selectivity planner for multi-filter ordering (deferred in
  FUTURE_CHANGES.md).
- Known pre-existing issue to revisit for pool attachment: multi-thread
  Builder runs emit the same seed set in nondeterministic record order,
  so membership_digest differs between identical runs; single-thread
  output is deterministic.
