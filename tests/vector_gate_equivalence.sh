#!/bin/sh
# Reproducible correctness gate for the lane-parallel first filters:
#   - exhaustively validate every high-byte/n bucket interval;
#   - compare every gate rejection with the unchanged scalar searcher over
#     bounded natural ranks for Soul, Legendary, tag, exact-Ante voucher, and
#     unrestricted Ante-1 physical packs;
#   - prove duplicate modded tag/voucher keys disable index-specific shortcuts;
#   - compile the Builder's internal rejection/handoff verifier and require
#     gate-on/off BSP3 bytes to match for direct and survivor-rebatched tags.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
FIXTURES=${1:-"${TMPDIR:-/tmp}/brainstorm_native_fixtures"}
SEARCH_COUNT=${VECTOR_GATE_SEARCH_CASES:-1000000}
SAN_COUNT=${VECTOR_GATE_SAN_CASES:-250000}
POOL_COUNT=${VECTOR_GATE_POOL_CASES:-2000000}
OUT=${TMPDIR:-/tmp}/brainstorm_vector_gate_equivalence
rm -rf "$OUT"
mkdir -p "$OUT"

for case_name in case1 case2 case6 case7 case20 case26 case31 case36; do
	test -f "$FIXTURES/$case_name.cfg" || {
		echo "FAIL: missing fixture $FIXTURES/$case_name.cfg; run tests/native_equivalence.sh first" >&2
		exit 1
	}
done

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; CC_BIN=${CC:-gcc}; THREAD_FLAG="" ;;
	*) EXE=; CC_BIN=${CC:-clang}; THREAD_FLAG=-pthread ;;
esac

awk '
	/^tagdef tag_charm / && !copied { print; print; copied=1; next }
	{ print }
' "$FIXTURES/case2.cfg" > "$OUT/duplicate-tag.cfg"
awk '
	{ print }
	/^vouchroute v_base_2 / && !copied {
		print "vouchdef v_base_2 1"
		copied=1
	}
' "$FIXTURES/case20.cfg" > "$OUT/duplicate-voucher.cfg"
awk '
	$1 == "pack" && !replaced { print "pack p_bench_missing"; replaced=1; next }
	$1 == "pack" { next }
	{ print }
' "$FIXTURES/case6.cfg" > "$OUT/missing-pack.cfg"
awk '
	$1 == "end" { print "poolfile /definitely/missing/route-dependent.bspool" }
	{ print }
' "$FIXTURES/case6.cfg" > "$OUT/poolfile-pack.cfg"

$CC_BIN -O3 -Wall -Wno-unused-function -ffp-contract=off $THREAD_FLAG \
	-o "$OUT/vector_gate_equivalence$EXE" \
	"$ROOT/tests/vector_gate_equivalence.c" -lm
"$OUT/vector_gate_equivalence$EXE" "$SEARCH_COUNT" \
	"$FIXTURES/case7.cfg" "$FIXTURES/case26.cfg" \
	"$FIXTURES/case2.cfg" "$FIXTURES/case20.cfg" \
	"$FIXTURES/case6.cfg" "$FIXTURES/case36.cfg" \
	"$OUT/duplicate-tag.cfg" "$OUT/duplicate-voucher.cfg" \
	"$FIXTURES/case31.cfg" "$OUT/missing-pack.cfg" "$OUT/poolfile-pack.cfg"

if test -z "$EXE"; then
	$CC_BIN -O1 -g -Wall -Wno-unused-function -ffp-contract=off \
		-fsanitize=address,undefined -fno-omit-frame-pointer $THREAD_FLAG \
		-o "$OUT/vector_gate_equivalence_san" \
		"$ROOT/tests/vector_gate_equivalence.c" -lm
	"$OUT/vector_gate_equivalence_san" "$SAN_COUNT" \
		"$FIXTURES/case7.cfg" "$FIXTURES/case26.cfg" \
		"$FIXTURES/case2.cfg" "$FIXTURES/case20.cfg" \
		"$FIXTURES/case6.cfg" "$FIXTURES/case36.cfg" \
		"$OUT/duplicate-tag.cfg" "$OUT/duplicate-voucher.cfg" \
		"$FIXTURES/case31.cfg" "$OUT/missing-pack.cfg" "$OUT/poolfile-pack.cfg"
	echo "PASS vector gate searcher differential under ASan/UBSan"
fi

$CC_BIN -O3 -Wall -Wno-unused-function -ffp-contract=off $THREAD_FLAG \
	-DBRAINSTORM_VERIFY_VECTOR_GATE \
	-o "$OUT/brainstorm_seed_pool_verify$EXE" \
	"$ROOT/native/brainstorm_seed_pool.c" -lm

write_criteria() {
	name=$1
	legendary=$2
	{
		echo "poolver 1"
		echo "threads 1"
		echo "start 0"
		echo "count $POOL_COUNT"
		echo "checkpoint $POOL_COUNT"
		echo "chunk 16384"
		echo "resume 0"
		echo "format binary"
		echo "tag_route collect"
		echo "tag tag_charm 1 small 1 small 1"
		if test "$legendary" = 1; then
			echo "legendary_routes canonical_charm"
			echo "legendary j_perkeo 1 small 1 small 0 charm"
			echo "soul_depth 1"
		fi
		echo "end"
	} > "$OUT/$name.cfg"
}

write_criteria tag-only 0
write_criteria rebatch 1
for name in tag-only rebatch; do
	"$OUT/brainstorm_seed_pool_verify$EXE" scan \
		"$FIXTURES/case1.cfg" "$OUT/$name.cfg" "$OUT/$name-on.bspool"
	BRAINSTORM_VECTOR_GATE=0 "$OUT/brainstorm_seed_pool_verify$EXE" scan \
		"$FIXTURES/case1.cfg" "$OUT/$name.cfg" "$OUT/$name-off.bspool"
	cmp "$OUT/$name-on.bspool" "$OUT/$name-off.bspool"
	echo "PASS Builder $name gate-on/off output is byte-identical"
done

echo "PASS vector gate searcher/Builder equivalence"
