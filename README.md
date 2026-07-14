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
3. Get this fork into your mods folder (paste into a Command Prompt):

   ```bat
   cd %AppData%\Balatro\Mods
   git clone -b joker-search-experiment https://github.com/carterhansen467-bot/Brainstorm.git Brainstorm
   ```

   No git? On the repo page pick the `joker-search-experiment` branch →
   Code → Download ZIP, and extract it into `%AppData%\Balatro\Mods`. The mod
   finds its own folder at launch, so the ZIP's default folder name
   (`Brainstorm-joker-search-experiment`) works as-is — renaming it to
   `Brainstorm` is tidier but optional.
4. **For the fast native search and seed pools** (optional but recommended):
   download `brainstorm-windows-x64.zip` from this fork's
   [releases](https://github.com/carterhansen467-bot/Brainstorm/releases) and
   follow its `INSTALL.txt` (two `.exe` files go into the mod folder's
   `native\` subfolder, the Seed Pool Builder goes next to them). If Windows blocks a downloaded
   file: right-click → Properties → check **Unblock**, or choose
   "More info → Run anyway" on the SmartScreen prompt.
5. Reload the game to activate the mod.

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

Two seed spaces are supported:

- **natural** (default): `34^8 = 1,785,793,904,896` eight-character seeds
  over the game's generation alphabet (no `0`, no `O`) — every seed the game
  can actually deal you.
- **total** (`space total` in the criteria): `36^1 + ... + 36^8 =
  2,901,713,047,668` seeds — everything the seed box *accepts typed in*,
  adding `0`/`O` and 1-7 character seeds. Those extra ~1.1 trillion seeds
  never occur naturally; they only exist if someone types them.

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

Then run the same command with an output name ending in `.bspool`. The
canonical schema-2 pool stores sorted numeric seed-rank deltas in independently
checksummed blocks, followed by an index for fast randomized access. Its 1 KiB
header includes schema/model versions, the scanned range, record count,
completion flag, alphabet, catalog/criteria fingerprints, and the criteria
themselves, so a shared `.bspool` is self-describing. Compression changes only
the representation: seed evaluation, checkpoint boundaries, and exhaustive
search semantics are unchanged.

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
Older Brainstorm helpers do
not understand schema 2, so anyone receiving a newly compressed pool must also
update both native helpers.

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

Only complete pools from the same model and unlock/pool snapshot are
accepted. Natural and all-typeable pools are both supported, and the source
pool decides the seed space. The output must have a different filename.
Derived headers/manifests retain the source pool ID, criteria fingerprint,
record count, and refilter depth.
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
soul_depth 1|2
tag_route collect|observe
space natural|total
label <any name, spaces allowed>
```

`label` names the pool; it is embedded in the `.bspool` header together with
a `pool_id` (a short fingerprint of catalog + criteria + range + space +
records), and both are shown by the Seed Pool Builder and the in-game
selector — two people holding the same pool see the same id.

`tag_route collect` selects the first required matching tag occurrences as
actual blind skips. Those missing shops are fed into the Model-3 physical pack
simulation before Souls are checked. `observe` requires the tags but assumes
their blinds are played. Legendary rules default to exact depth 1.
`soul_depth 2` is exclusive: the first Soul must yield a different legendary,
be used, and remain owned; the target must come from the second Soul in a
later pack. A seed with the target on Soul #1 does not match depth 2. Showman,
selling or destroying the first legendary, or skipping the first Soul changes
the pool and is outside this route convention. The ante window applies to the
target Soul itself, so Soul #1 may occur before the window at depth 2.

### Seed Pool Builder app (no terminal knowledge needed)

Double-click **`Seed Pool Builder.command`** in the mod folder (Windows:
**`Seed Pool Builder.exe`** from the release zip, or `Seed Pool Builder.bat`
if you have Python installed). It opens a point-and-click page in your
browser (running only on your computer -- nothing is installed and nothing
leaves the machine) where you can:

- pick a legendary with an ante window and optional Negative, optionally
  require it from the second Soul exclusively, and add any tag requirements;
- choose the **seed space**: natural seeds only (the 1.79 trillion the game
  can deal you), or all typeable seeds (2.90 trillion -- adds seeds with
  `0`/`O` and seeds shorter than 8 characters, which only exist typed into
  the seed box);
- press **Estimate** to sample 100M seeds and see the projected pool size
  and how long the full scan would take on your machine;
- press **Build pool** and watch live progress. Closing the window (or
  pressing **Pause scan**) stops at a checkpoint; pressing Build again with
  the same name resumes exactly where it left off -- safe across reboots,
  so a days-long full-space scan can be done in sittings;
- see every finished pool with its embedded criteria, its name, and its
  **pool id** -- a short fingerprint also shown by the in-game selector, so
  two people can confirm they're holding the same pool. Finished pools land
  in `seed_pools/` and appear in the in-game Seed Pool selector
  automatically; sharing a pool = sending someone that one `.bspool` file
  to drop into their own `seed_pools/` folder.

First-run notes: macOS may ask to install the Command Line Developer Tools
(the app compiles the scanner once, and `python3` ships with those tools) --
accept and re-run. If macOS blocks the double-click because the file came
from the internet, right-click it and choose **Open** the first time. On
Windows the app doesn't compile anything -- it uses the prebuilt
`brainstorm_seed_pool.exe` from the release zip and will say so if it's
missing. The app also needs `native_search.cfg`, which Brainstorm writes the
first time you toggle an auto-reroll in-game (Ctrl+A on, then off); the page
will tell you if it's missing. Prefer a terminal UI? The same tool exists as
`python3 tools/brainstorm_pool_builder.py` (arrow-key menus, curses;
macOS/Linux only -- Windows Python has no curses, use the browser UI).

### Using a seed pool in-game (phase 2)

Drop the finished `.bspool` into `Mods/Brainstorm/seed_pools/` (the folder is
created the first time the Brainstorm settings tab opens). A **Seed Pool**
selector appears in the Brainstorm tab listing every pool in that folder;
pick one and start an auto-reroll as usual. While a pool is selected, the
native helper only considers seeds recorded in the pool -- your other active
Brainstorm filters still apply on top of it, and each search starts at a
random position in the pool so repeat searches surface different seeds.

Because the pool file is the complete match set, a pool search that covers
every record without a hit is a definitive verdict: the mod stops and reports
that no seed in the pool matches the current filters (instead of silently
searching the full seed space). For the same reason, pool searches never fall
back to the Lua thread search -- they need the native helper (macOS: built by
`native/build.sh`; Windows: the release-zip exes).

**Sharing pools:** send someone the single `.bspool` file; they drop it into
their own `Mods/Brainstorm/seed_pools/` folder. The header carries the model
version, the criteria that built it, and a fingerprint of the unlock/pool
snapshot it was scanned against. If the recipient's unlock state differs, the
search still runs -- every hit is re-verified against *their* profile before
being applied -- but the pool's own guarantees (e.g. "Perkeo by ante 6")
were computed against the builder's snapshot, and the helper logs a warning
noting the difference.

Overlay-filter caveat: in-game filters are evaluated with the same blind-skip
conventions the normal search uses; they do not yet merge the pool's own
collected-tag skip route into pack/joker predictions. Pools carry their
criteria in the header so that composition can be added later.

To compare the generalized engine with the established Model-3 path over
their overlapping A1-A8 semantics, and to exercise the in-game pool search
end-to-end (member hit + definitive exhaustion):

```bash
tests/seed_pool_equivalence.sh native_search.cfg 1000000
tests/pool_search_equivalence.sh native_search.cfg 3000000
```
