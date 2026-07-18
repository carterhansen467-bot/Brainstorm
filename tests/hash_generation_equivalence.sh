#!/bin/sh
# Prove the contiguous-scan fast paths (seed odometer, shared/per-lane suffix
# hashing) bit-exact against the serial reference at radix carries, length
# transitions, and rotation wraparound in all three seed spaces. Runs the
# same binary twice: once optimized like production, once under
# Address/UndefinedBehavior sanitizers (skipped on Windows toolchains that
# lack them).
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT="${TMPDIR:-/tmp}/brainstorm_hash_generation_equivalence"
mkdir -p "$OUT"

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; SANITIZE=0 ;;
	*) EXE=; SANITIZE=1 ;;
esac

${CC:-clang} -O3 -Wall -Wno-unused-function -ffp-contract=off \
	-o "$OUT/hash_generation_equivalence$EXE" \
	"$ROOT/tests/hash_generation_equivalence.c" -lm
"$OUT/hash_generation_equivalence$EXE"

if [ "$SANITIZE" = 1 ]; then
	${CC:-clang} -O1 -g -Wall -Wno-unused-function -ffp-contract=off \
		-fsanitize=address,undefined -fno-sanitize-recover=all \
		-o "$OUT/hash_generation_equivalence_san" \
		"$ROOT/tests/hash_generation_equivalence.c" -lm
	"$OUT/hash_generation_equivalence_san"
	echo "PASS: sanitizer run clean"
fi
