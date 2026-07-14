#!/bin/sh
# Verify that soul_depth 2 is exclusive, survives pool metadata, and produces
# a route label with Soul #1 and Soul #2 in distinct packs.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
COUNT="${2:-5000000}"
OUT="${TMPDIR:-/tmp}/brainstorm_seed_pool_soul_depth"
mkdir -p "$OUT"

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh ;;
	*) EXE=; BUILD=native/build.sh ;;
esac

sh "$BUILD"
sed 's/^modelver [0-9][0-9]*$/modelver 4/' "$SNAPSHOT" > "$OUT/snapshot.cfg"

write_criteria() {
	depth="$1"
	out="$2"
	{
		echo "poolver 1"
		echo "threads 4"
		echo "start 0"
		echo "count $COUNT"
		echo "checkpoint $COUNT"
		echo "chunk 16384"
		echo "resume 0"
		echo "format binary"
		echo "tag_route observe"
		echo "legendary j_perkeo 1 3 0"
		[ "$depth" -eq 1 ] || echo "soul_depth $depth"
		echo "end"
	} > "$out"
}

write_criteria 1 "$OUT/depth1.cfg"
write_criteria 2 "$OUT/depth2.cfg"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" "$OUT/depth1.cfg" "$OUT/depth1.bspool"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" "$OUT/depth2.cfg" "$OUT/depth2.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/depth1.bspool" "$OUT/depth1.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/depth2.bspool" "$OUT/depth2.txt"

D1=$(sed -n '1p' "$OUT/depth1.txt")
D2=$(sed -n '1p' "$OUT/depth2.txt")
[ -n "$D1" ] || { echo "FAIL: depth-1 sample is empty"; exit 1; }
[ -n "$D2" ] || { echo "FAIL: depth-2 sample is empty; increase seed-count"; exit 1; }

printf '%s\n' "$D1" > "$OUT/depth1.seed"
printf '%s\n' "$D2" > "$OUT/depth2.seed"
D1_AS_1=$("./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" "$OUT/depth1.cfg" "$OUT/depth1.seed")
D1_AS_2=$("./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" "$OUT/depth2.cfg" "$OUT/depth1.seed")
D2_AS_1=$("./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" "$OUT/depth1.cfg" "$OUT/depth2.seed")
D2_AS_2=$("./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" "$OUT/depth2.cfg" "$OUT/depth2.seed")

case "$D1_AS_1" in "$D1 1 "*) ;; *) echo "FAIL: depth 1 rejected its own seed: $D1_AS_1"; exit 1 ;; esac
case "$D1_AS_2" in "$D1 0 -") ;; *) echo "FAIL: depth 2 accepted a depth-1 target: $D1_AS_2"; exit 1 ;; esac
case "$D2_AS_1" in "$D2 0 -") ;; *) echo "FAIL: depth 1 accepted a depth-2 target: $D2_AS_1"; exit 1 ;; esac
case "$D2_AS_2" in "$D2 1 "*"Soul1("*" Soul2(j_perkeo)="*) ;;
	*) echo "FAIL: depth-2 label is unclear or incomplete: $D2_AS_2"; exit 1 ;;
esac

grep -a -q '^soul_depth 2$' "$OUT/depth2.bspool"
if grep -a -q '^soul_depth ' "$OUT/depth1.bspool"; then
	echo "FAIL: default depth 1 should not add new header metadata"; exit 1
fi
grep -q '^second_soul_legendary j_perkeo 1 3 0$' "$OUT/depth2.bspool.manifest"
grep -q '^first_soul_legendary j_perkeo 1 3 0$' "$OUT/depth1.bspool.manifest"

H1=$(sed -n 's/^criteria_hash //p' "$OUT/depth1.bspool.manifest")
H2=$(sed -n 's/^criteria_hash //p' "$OUT/depth2.bspool.manifest")
[ "$H1" != "$H2" ] || { echo "FAIL: Soul depth is missing from the criteria fingerprint"; exit 1; }

N1=$(wc -l < "$OUT/depth1.txt" | tr -d ' ')
N2=$(wc -l < "$OUT/depth2.txt" | tr -d ' ')
echo "PASS: exact depths are disjoint; depth1=$N1 depth2=$N2 sample=$COUNT seed2=$D2"
