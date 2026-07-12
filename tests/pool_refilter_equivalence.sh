#!/bin/sh
# End-to-end proof that refilter(input.bspool) is exactly the intersection of
# the input records and the newly supplied criteria.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
COUNT="${2:-3000000}"
TAG="${TAG:-tag_rare}"
SPACE="${SPACE:-natural}"
OUT="${TMPDIR:-/tmp}/brainstorm_pool_refilter_equivalence"
rm -rf "$OUT"
mkdir -p "$OUT"

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh ;;
	*) EXE=; BUILD=native/build.sh ;;
esac

sh "$BUILD"
sed 's/^modelver [0-9][0-9]*$/modelver 4/' "$SNAPSHOT" > "$OUT/snapshot.cfg"

write_criteria() {
	file="$1"
	shift
	{
		echo "poolver 1"
		echo "threads 4"
		echo "start 0"
		echo "count $COUNT"
		echo "checkpoint $COUNT"
		echo "chunk 16384"
		echo "resume 0"
		echo "format binary"
		echo "tag_route collect"
		[ "$SPACE" = natural ] || echo "space $SPACE"
		for line in "$@"; do echo "$line"; done
		echo "end"
	} > "$file"
}

write_criteria "$OUT/broad.cfg" "tag $TAG 1 8 1"
write_criteria "$OUT/narrow.cfg" "legendary j_perkeo 1 8 0"

"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
	"$OUT/broad.cfg" "$OUT/broad.bspool"
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/narrow.cfg" "$OUT/broad.bspool" "$OUT/refined.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/broad.bspool" "$OUT/broad.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/refined.bspool" "$OUT/refined.txt"
"./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" \
	"$OUT/narrow.cfg" "$OUT/broad.txt" | awk '$2 == 1 { print $1 }' > "$OUT/expected.txt"

sort "$OUT/refined.txt" > "$OUT/refined.sorted.txt"
sort "$OUT/expected.txt" > "$OUT/expected.sorted.txt"
cmp "$OUT/refined.sorted.txt" "$OUT/expected.sorted.txt"
grep -q '^complete 1$' "$OUT/refined.bspool"
grep -q '^source_criteria_hash ' "$OUT/refined.bspool"
grep -q "^space $SPACE$" "$OUT/refined.bspool"

RECORDS=$(wc -l < "$OUT/refined.txt" | tr -d ' ')
[ "$RECORDS" -gt 0 ] || { echo "FAIL: refined pool is empty; grow seed-count"; exit 1; }
echo "PASS: refilter output is the exact $RECORDS-seed intersection"
