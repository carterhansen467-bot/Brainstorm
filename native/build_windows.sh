#!/bin/sh
# Build the Brainstorm native helpers for Windows x64, either cross-compiled
# (zig cc from any macOS/Linux box; zig is a single tarball from
# ziglang.org/download, no package manager needed) or natively with a
# MinGW-w64 gcc (what the GitHub Actions windows runner uses).
# -ffp-contract=off is REQUIRED for the same reason as in build.sh.
set -e
cd "$(dirname "$0")"
ZIG="${ZIG:-zig}"
if command -v "$ZIG" >/dev/null 2>&1; then
	CC="$ZIG cc -target x86_64-windows-gnu"
elif command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
	CC=x86_64-w64-mingw32-gcc
elif command -v gcc >/dev/null 2>&1 && gcc -dumpmachine 2>/dev/null | grep -qi mingw; then
	CC=gcc
else
	echo "error: need zig (ziglang.org/download; or ZIG=/path/to/zig) or a MinGW-w64 gcc" >&2
	exit 1
fi
$CC -O2 -Wall -ffp-contract=off -o brainstorm_native_search.exe brainstorm_native_search.c -lm
echo "built: $(pwd)/brainstorm_native_search.exe"
$CC -O3 -Wall -Wno-unused-function -ffp-contract=off -o brainstorm_seed_pool.exe brainstorm_seed_pool.c -lm
echo "built: $(pwd)/brainstorm_seed_pool.exe"
