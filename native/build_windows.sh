#!/bin/sh
# Build the Windows (x86_64) helper binaries.
#
# Works two ways:
#   - cross-compile from macOS/Linux with Zig (a single tarball download,
#     https://ziglang.org/download/):   ZIG=/path/to/zig sh native/build_windows.sh
#   - natively on Windows under Git Bash / MSYS2 with the same Zig zip, or any
#     clang/gcc that accepts these flags (MSVC users: /fp:precise is the
#     -ffp-contract=off equivalent).
#
# -ffp-contract=off is REQUIRED, same as build.sh: the searcher replicates
# Lua-level IEEE arithmetic that LuaJIT never fuses; a fused multiply-add
# would desync every RNG stream. The LuaJIT-internal seeding path that MAY be
# fused is calibrated at runtime from the config's check_* lines, never at
# compile time -- do NOT "fix" a parity failure with fp flags.
set -e
cd "$(dirname "$0")"
: "${ZIG:=zig}"
: "${WINCC:=$ZIG cc -target x86_64-windows-gnu}"
$WINCC -O2 -Wall -ffp-contract=off \
	-o brainstorm_native_search.exe brainstorm_native_search.c
echo "built: $(pwd)/brainstorm_native_search.exe"
$WINCC -O3 -Wall -Wno-unused-function -ffp-contract=off \
	-o brainstorm_seed_pool.exe brainstorm_seed_pool.c
echo "built: $(pwd)/brainstorm_seed_pool.exe"
