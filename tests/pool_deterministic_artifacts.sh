#!/bin/sh
# Scheduling must never become part of a pool snapshot. Exercise both BSP3
# scan and refilter writers with one and many workers, then require the full
# artifacts (header, data blocks, index, footer, and all identities) to match.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
SNAPSHOT=${1:-"$ROOT/native_search.cfg"}
COUNT=${2:-2000000}
OUT=${TMPDIR:-/tmp}/brainstorm_pool_deterministic_artifacts

case "$(uname -s 2>/dev/null || echo Windows)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh ;;
	*) EXE=; BUILD=native/build.sh ;;
esac
SCANNER="$ROOT/native/brainstorm_seed_pool$EXE"

rm -rf "$OUT"
mkdir -p "$OUT"
if [ ! -x "$SCANNER" ]; then sh "$ROOT/$BUILD"; fi

write_scan_criteria() {
	file=$1 threads=$2 checkpoint=$3
	{
		echo "poolver 1"; echo "threads $threads"; echo "start 0"
		echo "count $COUNT"; echo "checkpoint $checkpoint"; echo "chunk 2048"
		echo "resume 0"; echo "format binary"; echo "tag_route collect"
		echo "tag tag_charm 1 small 1 small 1"
		echo "legendary_routes canonical_charm"
		echo "legendary j_perkeo 1 small 1 small 0 charm"
		echo "soul_depth 1"; echo "end"
	} > "$file"
}

write_refilter_criteria() {
	file=$1 threads=$2
	{
		echo "poolver 1"; echo "threads $threads"; echo "start 0"
		echo "count $COUNT"; echo "checkpoint 1000003"; echo "chunk 2048"
		echo "resume 0"; echo "format binary"; echo "tag_route collect"
		echo "tag tag_3 1 8 1"; echo "end"
	} > "$file"
}

# The deliberately non-multiple checkpoint proves that normalization does not
# introduce a scheduler-dependent final partial chunk.
write_scan_criteria "$OUT/one.cfg" 1 1000003
write_scan_criteria "$OUT/many.cfg" 8 1000003
write_scan_criteria "$OUT/many-repeat.cfg" 8 1000003

"$SCANNER" scan "$SNAPSHOT" "$OUT/one.cfg" "$OUT/one.bspool"
"$SCANNER" scan "$SNAPSHOT" "$OUT/many.cfg" "$OUT/many.bspool"
"$SCANNER" scan "$SNAPSHOT" "$OUT/many-repeat.cfg" "$OUT/many-repeat.bspool"
cmp "$OUT/one.bspool" "$OUT/many.bspool"
cmp "$OUT/many.bspool" "$OUT/many-repeat.bspool"

# Refilter workers claim compressed input-record chunks rather than numeric
# rank chunks, so cover that independent scheduler as well.
write_refilter_criteria "$OUT/refilter-one.cfg" 1
write_refilter_criteria "$OUT/refilter-many.cfg" 8
"$SCANNER" refilter "$SNAPSHOT" "$OUT/refilter-one.cfg" \
	"$OUT/one.bspool" "$OUT/refilter-one.bspool"
"$SCANNER" refilter "$SNAPSHOT" "$OUT/refilter-many.cfg" \
	"$OUT/one.bspool" "$OUT/refilter-many.bspool"
cmp "$OUT/refilter-one.bspool" "$OUT/refilter-many.bspool"

python3 - "$ROOT" "$OUT/one.bspool" "$OUT/refilter-one.bspool" <<'PY'
import os
import sys

root, *paths = sys.argv[1:]
sys.path.insert(0, os.path.join(root, "tools"))
from brainstorm_pool_organizer import BSPoolReader

for index, path in enumerate(paths):
    reader = BSPoolReader(path)
    ranks = [record.rank for record in reader.iter_records()]
    assert ranks, "%s produced a vacuous deterministic sample" % path
    assert all(a < b for a, b in zip(ranks, ranks[1:])), path
    manifest = open(path + ".manifest", encoding="ascii").read()
    expected = ("record_order rank-ascending\n" if index == 0 else
                "record_order source-stable-block-sorted\n")
    assert expected in manifest
PY

echo "PASS: BSP3 scan and refilter artifacts are byte-identical across one/eight workers and repeated scheduling"
