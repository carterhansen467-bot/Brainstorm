#!/bin/sh
# Bounded differential check for the initial automatic-attachment route
# widening used by the classic Perkeo/Charm request: every observed Fast Exact
# member must also exist in the exhaustive/full pool built from otherwise
# identical criteria. This is regression evidence, not an all-space proof.
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
sed "s/__COUNT__/$COUNT/; s/__ROUTES__/full/; s/__TAG_MAX_ANTE__/2/; s/__TAG_MAX_PHASE__/big/; s/__LEG_MAX_ANTE__/2/; s/__LEG_MAX_PHASE__/big/; s/__NEG__/0/; s/__SOUL_DEPTH__/any/" \
  "$ROOT/tests/fixtures/pool_attachment_template.cfg" > "$OUT/full.cfg"
sed "s/__COUNT__/$COUNT/; s/__ROUTES__/canonical_charm/; s/__TAG_MAX_ANTE__/1/; s/__TAG_MAX_PHASE__/small/; s/__LEG_MAX_ANTE__/1/; s/__LEG_MAX_PHASE__/small/; s/__NEG__/0/; s/__SOUL_DEPTH__/1/" \
  "$ROOT/tests/fixtures/pool_attachment_template.cfg" > "$OUT/fast.cfg"
sed "s/__COUNT__/$COUNT/; s/__ROUTES__/canonical_charm/; s/__TAG_MAX_ANTE__/1/; s/__TAG_MAX_PHASE__/small/; s/__LEG_MAX_ANTE__/1/; s/__LEG_MAX_PHASE__/small/; s/__NEG__/1/; s/__SOUL_DEPTH__/1/" \
  "$ROOT/tests/fixtures/pool_attachment_template.cfg" > "$OUT/fast-negative.cfg"

"$SCANNER" scan "$SNAPSHOT" "$OUT/full.cfg" "$OUT/full.bspool"
"$SCANNER" scan "$SNAPSHOT" "$OUT/fast.cfg" "$OUT/fast.bspool"
"$SCANNER" scan "$SNAPSHOT" "$OUT/fast-negative.cfg" "$OUT/fast-negative.bspool"
"$SCANNER" export "$OUT/full.bspool" "$OUT/full.txt"
"$SCANNER" export "$OUT/fast.bspool" "$OUT/fast.txt"
"$SCANNER" export "$OUT/fast-negative.bspool" "$OUT/fast-negative.txt"
LC_ALL=C sort "$OUT/full.txt" > "$OUT/full.sorted.txt"
LC_ALL=C sort "$OUT/fast.txt" > "$OUT/fast.sorted.txt"
LC_ALL=C sort "$OUT/fast-negative.txt" > "$OUT/fast-negative.sorted.txt"

if [ ! -s "$OUT/fast-negative.sorted.txt" ]; then
  echo "FAIL: bounded sample contained no Negative Fast Exact member; increase COUNT" >&2
  exit 1
fi

if comm -23 "$OUT/fast.sorted.txt" "$OUT/full.sorted.txt" | grep -q .; then
  echo "FAIL: exhaustive attachment pool omitted a Fast Exact Perkeo/Charm member" >&2
  exit 1
fi
if comm -23 "$OUT/fast-negative.sorted.txt" "$OUT/full.sorted.txt" | grep -q .; then
  echo "FAIL: broader non-Negative attachment pool omitted a Negative active member" >&2
  exit 1
fi

echo "PASS: bounded differential found full Perkeo/Charm contains Fast Exact and Negative subsets across $COUNT ranks"
