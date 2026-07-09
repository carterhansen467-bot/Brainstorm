#!/bin/sh
# Build the Brainstorm native seed searcher.
# -ffp-contract=off is REQUIRED: the searcher replicates Lua-level IEEE
# arithmetic that LuaJIT never fuses; letting clang emit fmadd would change
# results by 1 ulp and desync every RNG stream (the LuaJIT-internal seeding
# path that MAY be fused is handled separately at runtime -- see
# brainstorm_native_search.c calibrate()).
set -e
cd "$(dirname "$0")"
clang -O2 -Wall -ffp-contract=off -pthread -o brainstorm_native_search brainstorm_native_search.c -lm
echo "built: $(pwd)/brainstorm_native_search"
