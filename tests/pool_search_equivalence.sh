#!/bin/sh
# End-to-end check of the pool-restricted native search:
#   1. build a small binary .bspool with the external builder;
#   2. run brainstorm_native_search with `poolfile` + the SAME semantics as
#      the pool criteria and require the hit to be a pool member that the
#      fixture path also accepts;
#   3. run again with a contradictory filter and require the definitive
#      "no seed in the pool matches" exhaustion verdict (exit 3).
#
# Usage:
#   tests/pool_search_equivalence.sh [native-snapshot.cfg] [seed-count]
#
# native_search.cfg is produced by Brainstorm whenever its native search runs.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
COUNT="${2:-3000000}"
TAG="${TAG:-tag_rare}"
OUT="${TMPDIR:-/tmp}/brainstorm_pool_search_equivalence"
rm -rf "$OUT"
mkdir -p "$OUT"

# Windows CI runs this same harness under Git Bash against the .exe builds.
case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh ;;
	*) EXE=; BUILD=native/build.sh ;;
esac

sh "$BUILD"

# The snapshot on disk may predate the current config protocol; the catalog
# data (pools/checks) is version-independent, so rewrite only the handshake.
sed 's/^modelver [0-9][0-9]*$/modelver 4/' "$SNAPSHOT" > "$OUT/snapshot.cfg"

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
	echo "tag $TAG 1 8 1"
	echo "legendary j_perkeo 1 8"
	echo "end"
} > "$OUT/criteria.cfg"

"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" "$OUT/criteria.cfg" "$OUT/pool.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/pool.bspool" "$OUT/pool.txt"
RECORDS=$(wc -l < "$OUT/pool.txt" | tr -d ' ')
[ "$RECORDS" -gt 0 ] || { echo "FAIL: pool is empty; grow seed-count"; exit 1; }

# A search config with the same route-sensitive semantics as the criteria:
# rare tag anywhere A1-8 (collected) + first-Soul Perkeo anywhere A1-8.
write_search_cfg() {
	legendary="$1"
	out="$2"
	{
		echo "session 1"
		echo "threads 4"
		echo "modelver 4"
		echo "entropy 98765.25"
		echo "soul 0"
		echo "legendary $legendary"
		echo "neglegendary 0"
		echo "tag $TAG"
		echo "voucher -"
		echo "voucherante 1"
		echo "taganywhere 1"
		echo "leganywhere 1"
		echo "matchany 0"
		echo "jslot 1 - 0"
		echo "jslot 2 - 0"
		echo "jslot 3 - 0"
		echo "maslots 0 0 0 0 0 0 0 0"
		echo "mapacks 0 0 0 0 0 0 0 0"
		echo "packslots 2"
		echo "poolfile $OUT/pool.bspool"
		grep -E '^(tagdef|vouchdef|jokerdef|boostdef|check_[a-z0-9]+) ' "$OUT/snapshot.cfg"
		echo "end"
	} > "$out"
}

# --- hit path -------------------------------------------------------------
write_search_cfg j_perkeo "$OUT/search_hit.cfg"
date +%s > "$OUT/hb"
rm -f "$OUT/status" "$OUT/stop"
"./native/brainstorm_native_search$EXE" search "$OUT/search_hit.cfg" \
	"$OUT/status" "$OUT/stop" "$OUT/hb"
SEED=$(sed -n 's/^R \([A-Z0-9]*\) .*/\1/p' "$OUT/status")
[ -n "$SEED" ] || { echo "FAIL: pool search found nothing"; cat "$OUT/status"; exit 1; }
grep -qx "$SEED" "$OUT/pool.txt" || { echo "FAIL: hit $SEED is not a pool member"; exit 1; }
echo "$SEED" > "$OUT/hit.seed"
FIX=$("./native/brainstorm_native_search$EXE" fixture "$OUT/search_hit.cfg" "$OUT/hit.seed")
case "$FIX" in
	"$SEED 1"*) ;;
	*) echo "FAIL: fixture rejects the pool hit: $FIX"; exit 1 ;;
esac
echo "PASS: pool hit $SEED is a member of the $RECORDS-record pool and passes filters"

# --- definitive exhaustion path --------------------------------------------
write_search_cfg j_triboulet "$OUT/search_none.cfg"
date +%s > "$OUT/hb"
rm -f "$OUT/status" "$OUT/stop"
rc=0
"./native/brainstorm_native_search$EXE" search "$OUT/search_none.cfg" \
	"$OUT/status" "$OUT/stop" "$OUT/hb" || rc=$?
[ "$rc" -eq 3 ] || { echo "FAIL: expected exhaustion exit 3, got $rc"; cat "$OUT/status"; exit 1; }
grep -q "^E pool: no seed in the pool matches" "$OUT/status" \
	|| { echo "FAIL: missing exhaustion verdict"; cat "$OUT/status"; exit 1; }
echo "PASS: contradictory filter exhausts the pool with a definitive verdict"

echo "POOL SEARCH EQUIVALENCE: ALL PASS"
