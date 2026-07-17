#!/bin/sh
# Prove that exact distributed ranges cover a scope once, merge back to the
# monolithic result, preserve pool identity, and reject gaps/overlaps/damage.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
COUNT="${2:-2000003}"
TAG="${TAG:-tag_charm}"
EXE=""
case "$(uname -s 2>/dev/null || echo Windows)" in
  MINGW*|MSYS*|CYGWIN*) EXE=".exe" ;;
esac
OUT="${TMPDIR:-/tmp}/brainstorm_pool_shard_merge"
rm -rf "$OUT"
mkdir -p "$OUT"

write_criteria() {
  file="$1" start="$2" count="$3" label="$4" tag="$5"
  {
    echo "poolver 1"; echo "threads 4"
    echo "start $start"; echo "count $count"; echo "checkpoint $count"
    echo "chunk 16384"; echo "resume 0"; echo "format binary"
    echo "tag_route collect"; echo "label $label"
    echo "tag $tag 1 8 1"; echo "legendary j_perkeo 1 8"; echo "end"
  } > "$file"
}

write_criteria "$OUT/mono.cfg" 0 "$COUNT" mono "$TAG"
"./native/brainstorm_seed_pool$EXE" scan "$SNAPSHOT" "$OUT/mono.cfg" "$OUT/mono.bspool"

prev=0
for i in 1 2 3 4; do
  end=$((COUNT * i / 4))
  n=$((end - prev))
  write_criteria "$OUT/part-$i.cfg" "$prev" "$n" "part-$i-of-4" "$TAG"
  "./native/brainstorm_seed_pool$EXE" scan "$SNAPSHOT" "$OUT/part-$i.cfg" "$OUT/part-$i.bspool"
  prev=$end
done
[ "$prev" -eq "$COUNT" ]

"./native/brainstorm_seed_pool$EXE" merge "$OUT/merged.bspool" \
  "$OUT/part-4.bspool" "$OUT/part-2.bspool" "$OUT/part-1.bspool" "$OUT/part-3.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/mono.bspool" "$OUT/mono.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/merged.bspool" "$OUT/merged.txt"
sort "$OUT/mono.txt" > "$OUT/mono.sorted"
sort "$OUT/merged.txt" > "$OUT/merged.sorted"
cmp "$OUT/mono.sorted" "$OUT/merged.sorted"

header() { head -c 1024 "$1" | tr -d '\0'; }
MONO_ID=$(header "$OUT/mono.bspool" | sed -n 's/^pool_id //p')
MERGED_ID=$(header "$OUT/merged.bspool" | sed -n 's/^pool_id //p')
[ -n "$MONO_ID" ] && [ "$MONO_ID" = "$MERGED_ID" ]
header "$OUT/merged.bspool" | grep -q '^range_start 0$'
header "$OUT/merged.bspool" | grep -q "^range_end $COUNT$"
header "$OUT/merged.bspool" | grep -q '^merged_parts 4$'

# Nested merges remain composable and retain the leaf-part count.
"./native/brainstorm_seed_pool$EXE" merge "$OUT/half-a.bspool" "$OUT/part-1.bspool" "$OUT/part-2.bspool"
"./native/brainstorm_seed_pool$EXE" merge "$OUT/half-b.bspool" "$OUT/part-3.bspool" "$OUT/part-4.bspool"
"./native/brainstorm_seed_pool$EXE" merge "$OUT/nested.bspool" "$OUT/half-b.bspool" "$OUT/half-a.bspool"
header "$OUT/nested.bspool" | grep -q '^merged_parts 4$'
NESTED_ID=$(header "$OUT/nested.bspool" | sed -n 's/^pool_id //p')
[ "$NESTED_ID" = "$MONO_ID" ]

# Gaps and overlaps must fail without leaving an output that looks usable.
if "./native/brainstorm_seed_pool$EXE" merge "$OUT/gap.bspool" \
    "$OUT/part-1.bspool" "$OUT/part-3.bspool" 2>/dev/null; then
  echo "FAIL: non-contiguous shards were accepted"; exit 1
fi
[ ! -e "$OUT/gap.bspool" ]
if "./native/brainstorm_seed_pool$EXE" merge "$OUT/overlap.bspool" \
    "$OUT/part-1.bspool" "$OUT/part-1.bspool" 2>/dev/null; then
  echo "FAIL: overlapping shards were accepted"; exit 1
fi
[ ! -e "$OUT/overlap.bspool" ]

cp "$OUT/part-2.bspool" "$OUT/wrong-criteria.bspool"
python3 - "$OUT/wrong-criteria.bspool" <<'PY'
import re, sys
with open(sys.argv[1], "r+b") as f:
    head = f.read(1024)
    changed = re.sub(br"criteria_hash [0-9a-f]{16}",
                     b"criteria_hash 0000000000000001", head, count=1)
    assert changed != head
    f.seek(0); f.write(changed)
PY
if "./native/brainstorm_seed_pool$EXE" merge "$OUT/mismatch.bspool" \
    "$OUT/part-1.bspool" "$OUT/wrong-criteria.bspool" 2>/dev/null; then
  echo "FAIL: different criteria were accepted"; exit 1
fi
[ ! -e "$OUT/mismatch.bspool" ]

# A checksummed block damaged in transit is rejected during merge.
cp "$OUT/part-1.bspool" "$OUT/corrupt.bspool"
python3 - "$OUT/corrupt.bspool" <<'PY'
import re, sys
with open(sys.argv[1], "r+b") as f:
    prefix = f.read(1024)
    match = re.search(br"^header_bytes (\d+)$", prefix, re.M)
    header_bytes = int(match.group(1)) if match else 1024
    f.seek(header_bytes)
    magic = f.read(4)
    block_header_bytes = 48 if magic == b"BSP3" else 32
    assert magic in (b"BSP2", b"BSP3")
    f.seek(header_bytes + block_header_bytes)
    b = f.read(1)
    assert b
    f.seek(-1, 1)
    f.write(bytes([b[0] ^ 1]))
PY
if "./native/brainstorm_seed_pool$EXE" merge "$OUT/corrupt-output.bspool" \
    "$OUT/corrupt.bspool" "$OUT/part-2.bspool" 2>/dev/null; then
  echo "FAIL: corrupted shard was accepted"; exit 1
fi
[ ! -e "$OUT/corrupt-output.bspool" ]

# Builder-side huge-space boundaries are also exact for uneven divisions.
python3 - <<'PY'
import sys
sys.path.insert(0, "tools")
from brainstorm_pool_builder import Criteria, SEEDSPACE, SEEDSPACE_TOTAL
for space, limit in (("natural", SEEDSPACE), ("total", SEEDSPACE_TOTAL)):
    for parts in (2, 4, 8, 16, 256):
        prior = 0
        for index in range(1, parts + 1):
            c = Criteria(); c.space = space; c.shard_total = parts; c.shard_index = index
            start, end = c.shard_bounds(0)
            assert start == prior and end > start
            prior = end
        assert prior == limit
c = Criteria(); c.legendary = "j_perkeo"; c.shard_total = 4; c.shard_index = 2
text = c.text("binary", 2_000_003)
assert "start 500000\n" in text and "count 500001\n" in text
assert "label perkeo-a1-8-part-2-of-4\n" in text
PY

echo "PASS: four disjoint shards exactly merge to the monolithic $COUNT-rank result (pool_id $MONO_ID)"
