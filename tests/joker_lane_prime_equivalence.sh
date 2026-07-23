#!/bin/sh
# Differentially validate the default eight-lane joker stream-prime path
# against both the scalar native reference and the Lua fixture oracle.
set -eu

cd "$(dirname "$0")/.."

LUAJIT="${LUAJIT:-luajit}"
CC="${CC:-clang}"
NSEEDS="${NSEEDS:-30000}"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/brainstorm-joker-lane.XXXXXX")
trap 'rm -rf "$WORK"' EXIT HUP INT TERM

# Conventional CC values may include a driver subcommand and target flags
# (for example `zig cc -target x86_64-windows-gnu`). Preserve those as
# separate argv entries instead of treating the entire value as one filename.
# shellcheck disable=SC2086
set -- $CC
if [ "$#" -eq 0 ]; then
	echo "FAIL: CC is empty" >&2
	exit 1
fi

if [ -n "${BRAINSTORM_FIXTURES_DIR:-}" ]; then
	FIXTURES="$BRAINSTORM_FIXTURES_DIR"
	if [ ! -f "$FIXTURES/case11.cfg" ] || [ ! -f "$FIXTURES/case37.expected" ]; then
		echo "FAIL: BRAINSTORM_FIXTURES_DIR is not a complete fixture directory" >&2
		exit 1
	fi
else
	FIXTURES="$WORK/fixtures"
	mkdir -p "$FIXTURES"
	"$LUAJIT" tests/dump_native_fixtures.lua Brainstorm_reroll.lua \
		"$FIXTURES" "$NSEEDS" > "$WORK/fixture-generation.log"
fi

COMMON_FLAGS="-O3 -Wall -ffp-contract=off -pthread"
"$@" $COMMON_FLAGS -DBRAINSTORM_JOKER_LANE_PRIME=0 \
	-o "$WORK/native-off" native/brainstorm_native_search.c -lm
"$@" $COMMON_FLAGS -DBRAINSTORM_JOKER_LANE_PRIME=1 \
	-o "$WORK/native-on" native/brainstorm_native_search.c -lm

# The first eight cases enter the optimized joker-first branch. The remaining
# stacked cases prove the switch cannot perturb joker searches whose earlier
# tag, pack, voucher, or Legendary predicates retain the scalar handoff.
CASES="11 12 13 16 17 18 21 32 14 15 22 30 35 37"
for n in $CASES; do
	base="$FIXTURES/case$n"
	"$WORK/native-off" fixture "$base.cfg" "$base.seeds" > "$WORK/case$n.off"
	"$WORK/native-on" fixture "$base.cfg" "$base.seeds" > "$WORK/case$n.on"
	if ! cmp -s "$base.expected" "$WORK/case$n.off"; then
		echo "FAIL: scalar native reference diverged from Lua in case$n" >&2
		diff "$base.expected" "$WORK/case$n.off" | head -12 >&2
		exit 1
	fi
	if ! cmp -s "$base.expected" "$WORK/case$n.on"; then
		echo "FAIL: joker lane-prime path diverged from Lua in case$n" >&2
		diff "$base.expected" "$WORK/case$n.on" | head -12 >&2
		exit 1
	fi
	if ! cmp -s "$WORK/case$n.off" "$WORK/case$n.on"; then
		echo "FAIL: joker lane-prime on/off mismatch in case$n" >&2
		diff "$WORK/case$n.off" "$WORK/case$n.on" | head -12 >&2
		exit 1
	fi
done

echo "PASS: joker lane-prime on/off matches Lua across 14 joker-heavy fixtures ($NSEEDS generated seeds plus typed-edge seeds per case)"
