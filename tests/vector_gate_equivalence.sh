#!/bin/sh
# Reproducible correctness gate for the lane-parallel first filters:
#   - exhaustively validate every high-byte/n bucket interval;
#   - compare every gate rejection with the unchanged scalar searcher over
#     bounded natural ranks for Soul, Legendary, tag, exact-Ante voucher, and
#     unrestricted Ante-1 physical packs;
#   - prove duplicate modded tag/voucher keys disable index-specific shortcuts;
#   - compile the Builder's internal rejection/handoff verifier and require
#     gate-on/off BSP3 bytes to match for direct and survivor-rebatched tags;
#   - compare the source-implied direct Charm route byte-for-byte with the
#     generic evaluator across full/fast, Negative, duplicate, and Omen plans.
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
	routes=${3:-canonical_charm}
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
			echo "legendary_routes $routes"
			echo "legendary j_perkeo 1 small 1 small 0 charm"
			echo "soul_depth 1"
		fi
		echo "end"
	} > "$OUT/$name.cfg"
}

write_inferred_charm_criteria() {
	name=$1
	routes=$2
	negative=$3
	start=${4:-0}
	{
		echo "poolver 1"
		echo "threads 1"
		echo "start $start"
		echo "count $POOL_COUNT"
		echo "checkpoint $POOL_COUNT"
		echo "chunk 16384"
		echo "resume 0"
		echo "format binary"
		echo "tag_route collect"
		echo "legendary_routes $routes"
		echo "legendary j_perkeo 1 small 1 small $negative charm"
		echo "soul_depth 1"
		echo "end"
	} > "$OUT/$name.cfg"
}

write_criteria tag-only 0
write_criteria rebatch 1
write_criteria rebatch-full 1 full
write_inferred_charm_criteria inferred-charm full 0
write_inferred_charm_criteria inferred-charm-fast canonical_charm 0
write_inferred_charm_criteria inferred-charm-negative full 1 8000000
for name in tag-only rebatch rebatch-full inferred-charm inferred-charm-fast inferred-charm-negative; do
	"$OUT/brainstorm_seed_pool_verify$EXE" scan \
		"$FIXTURES/case1.cfg" "$OUT/$name.cfg" "$OUT/$name-on.bspool" \
		2> "$OUT/$name-on.log"
	BRAINSTORM_VECTOR_GATE=0 "$OUT/brainstorm_seed_pool_verify$EXE" scan \
		"$FIXTURES/case1.cfg" "$OUT/$name.cfg" "$OUT/$name-off.bspool" \
		2> "$OUT/$name-off.log"
	cmp "$OUT/$name-on.bspool" "$OUT/$name-off.bspool"
	python3 - "$OUT/$name-on.bspool" <<'PY'
import sys

path = sys.argv[1]
with open(path, "rb") as handle:
    header = handle.read(8192).split(b"\0", 1)[0].decode("ascii")
records = int(next(line.split()[1] for line in header.splitlines()
                   if line.startswith("records ")))
if records <= 0:
    raise SystemExit("gate differential produced no accepted records: %s" % path)
PY
	echo "PASS Builder $name gate-on/off output is byte-identical"
done

for name in inferred-charm inferred-charm-fast inferred-charm-negative; do
	BRAINSTORM_DIRECT_CHARM=0 "$OUT/brainstorm_seed_pool_verify$EXE" scan \
		"$FIXTURES/case1.cfg" "$OUT/$name.cfg" "$OUT/$name-reference.bspool" \
		2> "$OUT/$name-reference.log"
	BRAINSTORM_VECTOR_GATE=0 BRAINSTORM_DIRECT_CHARM=0 \
		"$OUT/brainstorm_seed_pool_verify$EXE" scan \
		"$FIXTURES/case1.cfg" "$OUT/$name.cfg" "$OUT/$name-scalar.bspool" \
		2> "$OUT/$name-scalar.log"
	cmp "$OUT/$name-on.bspool" "$OUT/$name-reference.bspool"
	cmp "$OUT/$name-on.bspool" "$OUT/$name-scalar.bspool"
	echo "PASS Builder $name direct-Charm/reference output is byte-identical"
done

grep -q '^vector-gate-plan first=1 tag=2 target=[0-9][0-9]* direct_charm=1$' \
	"$OUT/inferred-charm-on.log" || {
	echo "FAIL source-implied Charm plan did not enable its staged gate and direct route" >&2
	exit 1
}
grep -q '^vector-gate-plan first=0 tag=0 target=.* direct_charm=1$' \
	"$OUT/inferred-charm-off.log" || {
	echo "FAIL BRAINSTORM_VECTOR_GATE=0 did not disable the inferred gate" >&2
	exit 1
}
grep -q '^vector-gate-plan first=1 tag=2 target=.* direct_charm=0$' \
	"$OUT/inferred-charm-reference.log" || {
	echo "FAIL BRAINSTORM_DIRECT_CHARM=0 did not restore the generic route" >&2
	exit 1
}

# The inferred predicate changes execution only. Its bounded membership must
# equal the physically equivalent explicit A1-Small Charm rule, while keeping
# the source-only pool's own criteria/header identity.
"$OUT/brainstorm_seed_pool_verify$EXE" export \
	"$OUT/inferred-charm-on.bspool" "$OUT/inferred-charm.txt"
"$OUT/brainstorm_seed_pool_verify$EXE" export \
	"$OUT/rebatch-full-on.bspool" "$OUT/explicit-charm.txt"
awk '{ print $1 }' "$OUT/inferred-charm.txt" > "$OUT/inferred-charm.seeds"
awk '{ print $1 }' "$OUT/explicit-charm.txt" > "$OUT/explicit-charm.seeds"
cmp "$OUT/inferred-charm.seeds" "$OUT/explicit-charm.seeds"
echo "PASS source-implied and explicit A1-Small Charm membership agree"

# Duplicate modded Charm keys select one physical catalog index in the scalar
# targeted-Charm walk. The derived gate must follow that resolved index and
# may never reject a scalar member.
"$OUT/brainstorm_seed_pool_verify$EXE" scan \
	"$OUT/duplicate-tag.cfg" "$OUT/inferred-charm.cfg" \
	"$OUT/inferred-duplicate-on.bspool" 2> "$OUT/inferred-duplicate-on.log"
BRAINSTORM_VECTOR_GATE=0 "$OUT/brainstorm_seed_pool_verify$EXE" scan \
	"$OUT/duplicate-tag.cfg" "$OUT/inferred-charm.cfg" \
	"$OUT/inferred-duplicate-off.bspool" 2> "$OUT/inferred-duplicate-off.log"
BRAINSTORM_DIRECT_CHARM=0 "$OUT/brainstorm_seed_pool_verify$EXE" scan \
	"$OUT/duplicate-tag.cfg" "$OUT/inferred-charm.cfg" \
	"$OUT/inferred-duplicate-reference.bspool" 2> "$OUT/inferred-duplicate-reference.log"
cmp "$OUT/inferred-duplicate-on.bspool" "$OUT/inferred-duplicate-off.bspool"
cmp "$OUT/inferred-duplicate-on.bspool" "$OUT/inferred-duplicate-reference.bspool"
echo "PASS source-implied Charm gate preserves duplicate-catalog output"

# Starting Omen changes the contents and call order inside the Charm reward,
# but not the fact that the required A1-Small source has exactly one route.
awk '
	$1 == "end" { print "vouchowned v_crystal_ball"; print "vouchowned v_omen_globe" }
	{ print }
' "$ROOT/native_search.cfg" > "$OUT/starting-omen.cfg"
"$OUT/brainstorm_seed_pool_verify$EXE" scan \
	"$OUT/starting-omen.cfg" "$OUT/inferred-charm.cfg" \
	"$OUT/inferred-omen-on.bspool" 2> "$OUT/inferred-omen-on.log"
BRAINSTORM_DIRECT_CHARM=0 "$OUT/brainstorm_seed_pool_verify$EXE" scan \
	"$OUT/starting-omen.cfg" "$OUT/inferred-charm.cfg" \
	"$OUT/inferred-omen-reference.bspool" 2> "$OUT/inferred-omen-reference.log"
cmp "$OUT/inferred-omen-on.bspool" "$OUT/inferred-omen-reference.bspool"
echo "PASS direct Charm route preserves starting-Omen output"

echo "PASS vector gate searcher/Builder equivalence"
