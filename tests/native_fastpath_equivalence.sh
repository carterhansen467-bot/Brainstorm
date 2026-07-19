#!/bin/sh
# Differential proofs for the exact arithmetic shortcuts used by both native
# executables.  Keep these separate from the Lua oracle: they compare each
# optimized primitive with its independently compiled scalar/reference path
# across both LuaJIT seed-rounding modes.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT="${TMPDIR:-/tmp}/brainstorm_native_fastpath_equivalence"
CASES=${FASTPATH_CASES:-5000000}
SNAPSHOT=${1:-"$ROOT/native_search.cfg"}
mkdir -p "$OUT"

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe ;;
	*) EXE= ;;
esac

for test in round13_native_equivalence prng_oneshot_equivalence booster_picker_equivalence; do
	${CC:-clang} -O3 -Wall -Wno-unused-function -ffp-contract=off \
		-o "$OUT/$test$EXE" "$ROOT/tests/$test.c" -lm
done

"$OUT/round13_native_equivalence$EXE" "$CASES"
"$OUT/prng_oneshot_equivalence$EXE" "$CASES"
"$OUT/booster_picker_equivalence$EXE" "$SNAPSHOT" "$CASES"

echo "PASS: native arithmetic fast paths match their reference implementations"
