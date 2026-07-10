# Windows port plan ("Tier 3" — full experience parity)

Reprompt target for a future Claude Code session. Goal: Windows users of the
`joker-search-experiment` branch get the native fast search, in-game seed-pool
filtering, and the Seed Pool Builder app — as a downloadable release zip with
prebuilt binaries, no compiler or Python install needed. Same branch, one
codebase; platform differences live behind ifdefs/runtime checks, never in a
parallel branch.

## Context (verify against current code before starting)

- The Lua mod (all filters, UI, Lua-thread search) is already cross-platform;
  only the C binaries and their launch plumbing are POSIX-bound.
- `native/brainstorm_seed_pool.c` `#include`s `native/brainstorm_native_search.c`
  (core-only mode), so ONE Win32 shim covers both binaries.
- Bit-exactness rules apply (BRAINSTORM_NOTES.md §10/§13/§15): compile with
  `-ffp-contract=off` equivalent (MSVC: `/fp:precise`, clang same flag); the
  runtime `check_*` parity lines + fma calibration already handle per-binary
  FP differences — do NOT hardcode an fp mode.
- All three harnesses must keep passing on macOS after every step:
  `tests/native_equivalence.sh` (needs `LUAJIT=~/.local/bin/luajit`),
  `tests/seed_pool_equivalence.sh`, `tests/pool_search_equivalence.sh`.
- modelver is 4. Do not bump unless helper<->config semantics change again.

## Work items, in order

### 1. Win32 compatibility shim in the C core (~40% of effort)
Small `#ifdef _WIN32` layer (either inline or `native/platform.h`) mapping:
- pthreads -> Win32 threads (CreateThread/WaitForSingleObject) or C11 threads.
- `pread` -> `ReadFile` with `OVERLAPPED.Offset` (thread-safe positional read;
  used by `pool_read_ranks` and the pool builder).
- `fsync(fileno(f))` -> `FlushFileBuffers((HANDLE)_get_osfhandle(_fileno(f)))`.
- `ftello/fseeko/ftruncate` -> `_ftelli64/_fseeki64/_chsize_s` (64-bit!).
- **`rename(tmp, path)` does NOT overwrite on Windows** (status files,
  checkpoint `.state` commit). Use `MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)`.
- `sysconf(_SC_NPROCESSORS_ONLN)` -> `GetSystemInfo`.
- SIGINT/SIGTERM handlers -> `SetConsoleCtrlHandler` (pool builder's
  checkpointed Ctrl+C stop must keep working).
- `nanosleep`, `access`, `stat` mtime (heartbeat age), `unistd.h`/`fcntl.h`
  includes, `dup`.
Compile-verify locally by **cross-compiling from this Mac with Zig**
(`zig cc -target x86_64-windows-gnu`; zig is a single tarball download, no
Homebrew on this machine). Keep `native/build.sh` working unchanged for macOS;
add the Windows build line(s) to it or a `build_windows.sh`.

### 2. CI on real Windows (~20%)
GitHub Actions workflow on the fork (carterhansen467-bot/Brainstorm):
- `windows-latest`: build both exes; install/build LuaJIT; run
  `tests/dump_native_fixtures.lua` + fixture diff (the Lua oracle generates
  parity checks WITH that machine's LuaJIT, so this is a true bit-exactness
  proof on Windows); run the pool build->restricted-search->exhaustion flow
  (port the .sh test logic to PowerShell or run under bash which ships on
  the runners).
- `macos-latest`: run all three existing harnesses so regressions are caught.
- Release job: on tag, zip `brainstorm_native_search.exe`,
  `brainstorm_seed_pool.exe`, PyInstaller-built `Seed Pool Builder.exe`
  (from `tools/pool_builder_web.py`), a `.bat` launcher, and install notes;
  attach to a GitHub release.

### 3. Lua-side Windows launch (~30%, the only real unknown)
In `Brainstorm_reroll.lua`:
- Detect OS via `love.system.getOS()`.
- `nativePaths().bin`: append `.exe` on Windows; `nativeAvailable()` checks it.
- `startNativeSearch()` spawn: current line is POSIX
  (`sh -c '...' & ` shape). Windows `os.execute` goes through cmd.exe —
  quoting differs completely. Hide the console-window flash (console exe
  spawned from a GUI app pops a black window): options ranked — (a)
  `start "" /b "exe" args >NUL 2>&1` (may still flash), (b) a tiny
  `.vbs`/`powershell -WindowStyle Hidden` shim, (c) build the helper with
  `-Wl,--subsystem,windows` and have it AttachConsole only when run from a
  terminal. Pick after testing on CI; document the choice in the notes.
- Failure modes must stay safe: spawn failure -> `nativeFailed` -> Lua thread
  search (or pool-abort alert when a pool is selected). Never wrong seeds.
- Cannot be fully verified without a human running Balatro on Windows: ship
  it, then have one Windows user do the 5-minute smoke test (install branch,
  Ctrl+A on/off, confirm `native_search.status` appears, select a shared
  .bspool, search). Watch for Defender/SmartScreen blocking unsigned exes —
  add a README note (right-click -> properties -> unblock, or "More info ->
  Run anyway").

### 4. Launchers, packaging, docs (~10%)
- `Seed Pool Builder.bat` next to the `.command` (runs the web UI; with the
  PyInstaller exe in the release zip, the bat is only for from-source users).
- `tools/pool_builder_web.py` / `brainstorm_pool_builder.py`: audit for POSIX
  assumptions (they're stdlib Python and mostly fine; check `preflight()`
  which shells `sh native/build.sh` — on Windows it should instead just
  verify the prebuilt exe exists and explain the release zip if not; curses
  TUI is macOS/Linux-only, keep it that way and say so).
- README: per-OS install sections (macOS: current story; Windows: download
  release zip, drop exes into `Mods/Brainstorm/native/`, double-click the
  builder exe). Update BRAINSTORM_NOTES.md with a §17 describing the port and
  its verification, and the .bat/console-flash decision.

## Non-goals / invariants
- `.bspool` format is already platform-independent (u64le + text header):
  no format changes. Pools built anywhere work everywhere.
- No permanent `windows` branch: work on a short-lived branch off
  `joker-search-experiment`, merge back when CI is green.
- Don't touch filter/RNG semantics anywhere in this port.

## Definition of done
1. CI green on windows-latest + macos-latest (oracle equivalence + pool e2e).
2. Release zip downloadable with the three exes + bat + notes.
3. macOS harnesses still pass locally.
4. One human Windows smoke test confirms in-game spawn + pool search
   (tracked as the only open risk until it happens).
