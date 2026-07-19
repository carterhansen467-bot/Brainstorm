#!/bin/sh
# Build the Brainstorm native seed searcher.
# -ffp-contract=off is REQUIRED: the searcher replicates Lua-level IEEE
# arithmetic that LuaJIT never fuses; letting clang emit fmadd would change
# results by 1 ulp and desync every RNG stream (the LuaJIT-internal seeding
# path that MAY be fused is handled separately at runtime -- see
# brainstorm_native_search.c calibrate()).
set -e
cd "$(dirname "$0")"

# PGO profiles are coupled to both the source and the clang version, so they
# are deliberately regenerated instead of checked in.  The ordinary build
# remains the portable default; use `BRAINSTORM_PGO=1 sh native/build.sh` to
# train both helpers locally on their representative exact workloads.
if [ "${BRAINSTORM_PGO:-0}" = "1" ]; then
	exec ./build_pgo.sh train
fi

clang -O3 -Wall -ffp-contract=off -pthread -o brainstorm_native_search brainstorm_native_search.c -lm
echo "built: $(pwd)/brainstorm_native_search"
clang -O3 -Wall -Wno-unused-function -ffp-contract=off -pthread \
	-o brainstorm_seed_pool brainstorm_seed_pool.c -lm
echo "built: $(pwd)/brainstorm_seed_pool"
