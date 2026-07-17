# Windows release and update layout

The source checkout remains arranged for builds and tests. The published ZIP
is an installed-user layout assembled by `release/package_windows.py` from an
explicit allow-list; it never sweeps the working tree.

```text
Brainstorm-Windows/
├── Install or Update Brainstorm.bat
├── install-or-update.ps1
├── README-WINDOWS.txt
├── RELEASE-MANIFEST.sha256
└── Brainstorm/                         active SMODS mod
    ├── Brainstorm*.lua, lovely.toml …  game-loaded files
    ├── native/
    │   └── brainstorm_native_search.exe
    ├── Seed Pool Builder.bat           stable user shortcut
    └── Seed Pool Builder/              standalone-program boundary
        ├── Seed Pool Builder.exe
        ├── Seed Pool Organizer.exe
        ├── brainstorm_seed_pool.exe
        ├── seed_pool.example.cfg
        └── README.txt
```

## Why the split stops there

- Lovely applies `lovely.toml` from the active mod and `Brainstorm.lua` loads
  its Lua siblings, so the game files must remain at the mod root unless the
  loader and patch manifest are changed together.
- The in-game accelerator is resolved as `native/brainstorm_native_search.exe`.
  It belongs to the running mod, not the standalone pool program.
- `native_search.cfg` is written by the running game and consumed by both
  programs. `seed_pools/` is likewise written by the builder and read by the
  game. Keeping this shared, potentially huge user state at the mod root avoids
  duplicate copies and migration risk.
- `Seed Pool Builder.exe`, `Seed Pool Organizer.exe`, and
  `brainstorm_seed_pool.exe` are standalone-only and can therefore be grouped
  without changing SMODS mechanics. Both apps find their parent mod by marker
  files rather than assuming the folder is named `Brainstorm`.

Separating the builder into an entirely independent download is technically
possible with `BRAINSTORM_MOD_DIR`, but it increases version-mismatch risk: the
builder, scanner, Lua replay, and snapshot model are a tested set. The release
therefore uses one full ZIP and visually separates the builder inside it.

## Previous-release upgrade path

For a user already on win-v9 or another current ZIP install:

1. Quit Balatro and close the Seed Pool Builder.
2. Download and fully extract the new `brainstorm-windows-full.zip`.
3. Double-click `Install or Update Brainstorm.bat`.
4. Launch Balatro and toggle Ctrl+A on and off once to refresh
   `native_search.cfg` for the installed model and profile.

The updater validates every payload checksum, backs up managed destination
files during the copy, and rolls them back on failure. It preserves
`seed_pools/`, `settings.lua`, `native_search.cfg`, runtime status files, and
`.state` checkpoints because none is in the release manifest. Once the grouped
builder is installed, it removes only these known old duplicates:

```text
Brainstorm\Seed Pool Builder.exe
Brainstorm\native\brainstorm_seed_pool.exe
```

It does not remove `native\brainstorm_native_search.exe`.

## Building the ZIP

After the Windows native helpers and PyInstaller executable exist:

```sh
python release/package_windows.py \
  --builder-exe "dist/Seed Pool Builder.exe" \
  --organizer-exe "dist/Seed Pool Organizer.exe" \
  --search-exe native/brainstorm_native_search.exe \
  --pool-exe native/brainstorm_seed_pool.exe \
  --version win-vNEXT --commit GIT_SHA
```

This creates `out/brainstorm-windows-full.zip`. Run
`python tests/windows_release_layout.py` before publishing. Tag-triggered CI
performs the same assembly only after the macOS and Windows test jobs pass.
