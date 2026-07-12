# Brainstorm — Windows port summary (branch: `joker-search-experiment`)

> This file is a self-contained briefing on the Windows port for anyone (human or AI assistant)
> picking up this branch. Written 2026-07-11, when the port was merged and CI-green.

**Repo:** https://github.com/carterhansen467-bot/Brainstorm (fork of the Brainstorm Balatro mod, branch `joker-search-experiment`).
**Release:** https://github.com/carterhansen467-bot/Brainstorm/releases/tag/win-v1 (`brainstorm-windows-x64.zip`).

**What this branch is:** a fork of Brainstorm with a native C seed-search accelerator (CPU, not
Immolate/GPU) plus a "seed pool" pre-computation system. It was previously macOS-only; this branch
ports the entire native toolchain to Windows with full feature parity and bit-exact results, one
shared codebase.

## Changes made

### 1. `native/platform.h` (new)
The only OS-aware file. All platform differences are behind small `bs_*` static-inline shims:
Win32 threads (`_beginthreadex`), `SRWLOCK` mutexes, `bs_pread` (ReadFile + OVERLAPPED),
`MoveFileEx(MOVEFILE_REPLACE_EXISTING)` for atomic renames (Windows `rename()` doesn't overwrite),
`_fseeki64`/`_ftelli64`, `FlushFileBuffers`, `SetConsoleCtrlHandler` for Ctrl-C (preserves the
checkpointed-pause contract: exit code 130, resumable `.state` file), a `strsep` implementation
(missing from the Windows CRT), and `bs_platform_init()` which sets stdout/stderr to binary mode so
all output is LF-only (the Lua status parser and test harnesses compare exact bytes). The POSIX
side mirrors it with pthreads/unistd.

### 2. `native/brainstorm_native_search.c` + `native/brainstorm_seed_pool.c`
All direct POSIX calls replaced with the `bs_*` shims; status/export files opened `"wb"`. No logic
changes; bit-exactness preserved via `-ffp-contract=off` and the existing runtime fma-vs-plain
calibration (Mac LuaJIT rounds fma-mode, MSVC LuaJIT rounds plain-mode — the calibration detects
this automatically and CI proved both modes pass).

### 3. `native/build_windows.sh` (new)
Cross-compiles both exes with `zig cc -target x86_64-windows-gnu` (MinGW gcc fallback), same
optimization flags as the mac `build.sh`.

### 4. `win_spawn.lua` (new) + `Brainstorm_reroll.lua`
On Windows the mod launches the search exe via LuaJIT FFI `CreateProcessA` with `CREATE_NO_WINDOW`
(no console flash, no cmd.exe), with canonical argv quoting. The macOS path (`os.execute` with `&`)
is unchanged. Added `Brainstorm.isWindows()` and `.exe` suffix handling in `nativePaths()`.

### 5. `tools/brainstorm_pool_builder.py` + `Seed Pool Builder.bat` (new)
Pool builder works on Windows: curses import guarded (the curses TUI is macOS/Linux-only; Windows
uses the web UI), `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT` for pause/resume,
PyInstaller-frozen-exe support (mod dir derived from `sys.executable`). The release zip ships a
`Seed Pool Builder.exe` built with PyInstaller.

### 6. Test suites made cross-platform
The three bit-exactness harnesses (`tests/native_equivalence.sh`, `tests/seed_pool_equivalence.sh`,
`tests/pool_search_equivalence.sh`) detect MSYS/Git Bash and use `.exe` binaries. One Windows
gotcha fixed: MSYS converts command-line argument paths but not paths written *inside* cfg files,
so the poolfile line uses `cygpath -m`. New tests: `tests/win_spawn_test.lua` (spawn + status
lifecycle) and `tests/windows_builder_smoke.py` (scan → Ctrl-Break pause → rc 130 → resume from
checkpoint).

### 7. `.github/workflows/ci.yml` (new)
CI on `windows-latest` + `macos-latest`. The Windows job builds LuaJIT from source with MSVC and
regenerates the oracle fixtures with it, so passing equivalence suites is a genuine cross-platform
bit-exactness proof (Windows log shows `OK mode=plain checks=64` vs `mode=fma` on Mac). Tagging
`win-v*` builds the exes + PyInstaller app and publishes a release zip automatically. CI is green
on both platforms.

### 8. Docs
README rewritten with Windows install steps (lovely `version.dll`, clone this branch into
`%AppData%\Balatro\Mods`, drop in the release-zip exes); design notes in `BRAINSTORM_NOTES.md`
item 17; the original plan in `WINDOWS_PORT_PLAN.md` (now marked EXECUTED).

## What still needs verification

The only unverified item is a **human in-game smoke test on real Windows**:

1. Ctrl+A in-game starts a native search with no console flash and writes `native_search.cfg`.
2. `Seed Pool Builder.exe` builds a pool.
3. In-game pool search finds seeds, or gives the clean pool-abort message.

Everything else is machine-verified by CI.
