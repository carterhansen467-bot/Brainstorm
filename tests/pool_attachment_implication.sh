#!/bin/sh
# Native differential evidence for each containment dimension accepted by the
# classic Perkeo/Charm automatic-attachment matcher. The exhaustive finite
# policy proof lives in pool_attachment_matrix.lua; this bounded seed sample
# independently checks that the native evaluator follows the same containment
# relationships without treating sampling as the logical proof itself.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
SNAPSHOT=${1:-"$ROOT/native_search.cfg"}
COUNT=${2:-20000000}
OUT=${TMPDIR:-/tmp}/brainstorm_pool_attachment_implication
SCANNER="$ROOT/native/brainstorm_seed_pool"

if [ ! -x "$SCANNER" ]; then
  sh "$ROOT/native/build.sh"
fi

mkdir -p "$OUT"

make_config() {
  name=$1 routes=$2 tag_ante=$3 tag_phase=$4 leg_ante=$5 leg_phase=$6 neg=$7 depth=$8
  sed "s/__COUNT__/$COUNT/; s/__ROUTES__/$routes/; s/__TAG_MAX_ANTE__/$tag_ante/; s/__TAG_MAX_PHASE__/$tag_phase/; s/__LEG_MAX_ANTE__/$leg_ante/; s/__LEG_MAX_PHASE__/$leg_phase/; s/__NEG__/$neg/; s/__SOUL_DEPTH__/$depth/" \
    "$ROOT/tests/fixtures/pool_attachment_template.cfg" > "$OUT/$name.cfg"
}

scan_export() {
  name=$1
  "$SCANNER" scan "$SNAPSHOT" "$OUT/$name.cfg" "$OUT/$name.bspool"
  "$SCANNER" export "$OUT/$name.bspool" "$OUT/$name.txt"
  LC_ALL=C sort "$OUT/$name.txt" > "$OUT/$name.sorted.txt"
}

assert_subset() {
  subset=$1 superset=$2 relationship=$3
  if comm -23 "$OUT/$subset.sorted.txt" "$OUT/$superset.sorted.txt" | grep -q .; then
    echo "FAIL: $relationship" >&2
    exit 1
  fi
}

# Baseline UI request and one isolated superset per accepted implication rule.
make_config fast canonical_charm 1 small 1 small 0 1
make_config fast-negative canonical_charm 1 small 1 small 1 1
make_config wider-window canonical_charm 2 big 2 big 0 1
make_config full-routes full 1 small 1 small 0 1
make_config either-soul canonical_charm 1 small 1 small 0 any
make_config all-widened full 2 big 2 big 0 any

for name in fast fast-negative wider-window full-routes either-soul all-widened; do
  scan_export "$name"
done

if [ ! -s "$OUT/fast-negative.sorted.txt" ]; then
  echo "FAIL: bounded sample contained no Negative Fast Exact member; increase COUNT" >&2
  exit 1
fi

assert_subset fast wider-window "wider route window omitted an exact-window member"
assert_subset fast full-routes "Full Exhaustive routes omitted a Fast Exact member"
assert_subset fast either-soul "either-Soul pool omitted a first-Soul member"
assert_subset fast all-widened "combined widening omitted a Fast Exact member"
assert_subset fast-negative fast "non-Negative pool omitted a Negative active member"
assert_subset fast-negative all-widened "combined widening omitted a Negative active member"

echo "PASS: native attachment differentials preserve window, route, Soul-depth, and Negative implications across $COUNT ranks"
