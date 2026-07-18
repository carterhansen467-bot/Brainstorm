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
LUAJIT_BIN="${LUAJIT:-luajit}"
rm -rf "$OUT"
mkdir -p "$OUT"

# Windows CI runs this same harness under Git Bash against the .exe builds.
# MSYS converts POSIX paths in command-line ARGUMENTS automatically, but the
# poolfile line goes into a cfg FILE the .exe reads, so that one path must be
# written in native form (cygpath -m: C:/... with fopen-safe forward slashes).
case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh
		native_path() { cygpath -m "$1"; } ;;
	*) EXE=; BUILD=native/build.sh
		native_path() { printf '%s\n' "$1"; } ;;
esac

sh "$BUILD"

"$LUAJIT_BIN" tests/align_snapshot_prng.lua "$SNAPSHOT" > "$OUT/snapshot.cfg"

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
# $3: which .bspool restricts the search (default: the natural-space pool).
write_search_cfg() {
	legendary="$1"
	out="$2"
	poolf="${3:-$OUT/pool.bspool}"
	{
		echo "session 1"
		echo "threads 4"
		echo "modelver 6"
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
		echo "poolfile $(native_path "$poolf")"
		grep -E '^(tagdef|vouchdef|vouchroute|vouchowned|jokerdef|boostdef|specialdef|check_[a-z0-9]+) ' "$OUT/snapshot.cfg"
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

# General organizer combines use a larger BSP3 header plus forward-compatible
# provenance descriptors. Prove both native consumers treat that result as the
# same literal membership set: the search helper can search it, and the pool
# helper can export and refilter it. Reapplying only the tag rule creates a
# distinct compatible snapshot with the same members, so their union should be
# byte-for-byte equivalent after export without relying on duplicate inputs.
sed '/^legendary /d' "$OUT/criteria.cfg" > "$OUT/criteria_tag_refilter.cfg"
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/criteria_tag_refilter.cfg" "$OUT/pool.bspool" "$OUT/tag-refilter.bspool"
python3 tools/brainstorm_pool_organizer.py combine "$OUT/composite-union.bspool" \
	"$OUT/pool.bspool" "$OUT/tag-refilter.bspool" --operation union \
	--label "CI composite native compatibility"
"./native/brainstorm_seed_pool$EXE" export "$OUT/composite-union.bspool" \
	"$OUT/composite-union.txt"
sort "$OUT/pool.txt" > "$OUT/pool.sorted"
sort "$OUT/composite-union.txt" > "$OUT/composite-union.sorted"
cmp "$OUT/pool.sorted" "$OUT/composite-union.sorted"
write_search_cfg j_perkeo "$OUT/search_composite.cfg" "$OUT/composite-union.bspool"
date +%s > "$OUT/hb"
rm -f "$OUT/status" "$OUT/stop"
"./native/brainstorm_native_search$EXE" search "$OUT/search_composite.cfg" \
	"$OUT/status" "$OUT/stop" "$OUT/hb"
COMPOSITE_SEED=$(sed -n 's/^R \([A-Z0-9]*\) .*/\1/p' "$OUT/status")
[ -n "$COMPOSITE_SEED" ] \
	|| { echo "FAIL: native search could not read the composite pool"; cat "$OUT/status"; exit 1; }
grep -qx "$COMPOSITE_SEED" "$OUT/composite-union.txt" \
	|| { echo "FAIL: composite hit is not a recorded member"; exit 1; }
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/criteria_tag_refilter.cfg" "$OUT/composite-union.bspool" \
	"$OUT/composite-refilter.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/composite-refilter.bspool" \
	"$OUT/composite-refilter.txt"
sort "$OUT/composite-refilter.txt" > "$OUT/composite-refilter.sorted"
cmp "$OUT/pool.sorted" "$OUT/composite-refilter.sorted"
echo "PASS: native search/export/refilter accept composite provenance pools"

# A pool's membership/route is only valid for the exact ordered profile
# snapshot that built it. A mismatch must be fatal (the former warning could
# return a seed with the right tag or Soul one ante/depth off).
cp "$OUT/search_none.cfg" "$OUT/search_mismatch.cfg"
python3 - "$OUT/search_mismatch.cfg" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s2, n = re.subn(r"^(tagdef\s+\S+\s+)[01](\s+\d+)$", r"\g<1>0\2",
                 s, count=1, flags=re.M)
assert n == 1 and s2 != s
open(p, "w", encoding="utf-8").write(s2)
PY
date +%s > "$OUT/hb"
rm -f "$OUT/status" "$OUT/stop"
rc=0
"./native/brainstorm_native_search$EXE" search "$OUT/search_mismatch.cfg" \
	"$OUT/status" "$OUT/stop" "$OUT/hb" || rc=$?
[ "$rc" -eq 1 ] || { echo "FAIL: profile-mismatched pool returned $rc"; cat "$OUT/status"; exit 1; }
grep -q '^E pool: profile/unlock snapshot differs' "$OUT/status" \
	|| { echo "FAIL: profile mismatch was not a pool-fatal error"; cat "$OUT/status"; exit 1; }
echo "PASS: profile/catalog mismatch is rejected before searching"

# Model 5 did not include collected tag rewards in its Soul timeline. A helper
# must reject that pool rather than reinterpret its existing membership under
# Model 6.
cp "$OUT/pool.bspool" "$OUT/model5.bspool"
python3 - "$OUT/model5.bspool" <<'PY'
import sys
p = sys.argv[1]
with open(p, "r+b") as f:
    data = f.read(1024)
    old, new = b"modelver 6\n", b"modelver 5\n"
    assert data.count(old) == 1
    f.seek(0); f.write(data.replace(old, new, 1))
PY
write_search_cfg j_perkeo "$OUT/search_model5.cfg" "$OUT/model5.bspool"
date +%s > "$OUT/hb"
rm -f "$OUT/status" "$OUT/stop"
rc=0
"./native/brainstorm_native_search$EXE" search "$OUT/search_model5.cfg" \
	"$OUT/status" "$OUT/stop" "$OUT/hb" || rc=$?
[ "$rc" -eq 1 ] || { echo "FAIL: Model-5 pool returned $rc"; cat "$OUT/status"; exit 1; }
grep -q '^E pool: built with model 5, this helper is model 6' "$OUT/status" \
	|| { echo "FAIL: stale pool did not report a model mismatch"; cat "$OUT/status"; exit 1; }
echo "PASS: stale Model-5 pool is rejected before searching"

# A decode/checksum failure must never be mislabeled as definitive pool
# exhaustion. Corrupt one payload byte and require a fatal read verdict.
cp "$OUT/pool.bspool" "$OUT/corrupt.bspool"
python3 - "$OUT/corrupt.bspool" <<'PY'
import re, sys
with open(sys.argv[1], "r+b") as f:
    prefix = f.read(1024)
    match = re.search(br"^header_bytes (\d+)$", prefix, re.M)
    header_bytes = int(match.group(1)) if match else 1024
    f.seek(header_bytes)
    magic = f.read(4)
    assert magic in (b"BSP2", b"BSP3")
    f.seek(header_bytes + (48 if magic == b"BSP3" else 32))
    b = f.read(1); assert b
    f.seek(-1, 1); f.write(bytes([b[0] ^ 1]))
PY
write_search_cfg j_triboulet "$OUT/search_corrupt.cfg" "$OUT/corrupt.bspool"
date +%s > "$OUT/hb"
rm -f "$OUT/status" "$OUT/stop"
rc=0
"./native/brainstorm_native_search$EXE" search "$OUT/search_corrupt.cfg" \
	"$OUT/status" "$OUT/stop" "$OUT/hb" || rc=$?
[ "$rc" -eq 1 ] || { echo "FAIL: corrupt pool returned $rc"; cat "$OUT/status"; exit 1; }
grep -q '^E pool: record decode/read failed' "$OUT/status" \
	|| { echo "FAIL: corrupt pool was reported as exhaustion"; cat "$OUT/status"; exit 1; }
echo "PASS: pool corruption is fatal, never false exhaustion"

# --- vanilla-settable space: variable-length seeds, never a zero ------------
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
	echo "space settable"
	echo "label CI vanilla-settable pool"
	echo "tag $TAG 1 8 1"
	echo "legendary j_perkeo 1 8"
	echo "end"
} > "$OUT/criteria_settable.cfg"

"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
	"$OUT/criteria_settable.cfg" "$OUT/pool_settable.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/pool_settable.bspool" \
	"$OUT/pool_settable.txt"

head -c 1024 "$OUT/pool_settable.bspool" | tr -d '\0' > "$OUT/header_settable.txt"
grep -q "^space settable$" "$OUT/header_settable.txt" \
	|| { echo "FAIL: header lacks 'space settable'"; exit 1; }
grep -q "^charset 123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ$" "$OUT/header_settable.txt" \
	|| { echo "FAIL: settable pool has the wrong alphabet"; exit 1; }
grep -q "^seedspace 2318107019760$" "$OUT/header_settable.txt" \
	|| { echo "FAIL: settable pool has the wrong rank count"; exit 1; }
SRECORDS=$(wc -l < "$OUT/pool_settable.txt" | tr -d ' ')
[ "$SRECORDS" -gt 0 ] || { echo "FAIL: settable-space pool is empty; grow seed-count"; exit 1; }
if grep -q '0' "$OUT/pool_settable.txt"; then
	echo "FAIL: vanilla-settable pool exported a seed containing 0"
	exit 1
fi
if [ -n "$(awk 'length($0) == 8 && $0 !~ /O/' "$OUT/pool_settable.txt")" ]; then
	echo "FAIL: early settable-space ranks must still be short or contain O"
	exit 1
fi

write_search_cfg j_perkeo "$OUT/search_settable.cfg" "$OUT/pool_settable.bspool"
date +%s > "$OUT/hb"
rm -f "$OUT/status" "$OUT/stop"
"./native/brainstorm_native_search$EXE" search "$OUT/search_settable.cfg" \
	"$OUT/status" "$OUT/stop" "$OUT/hb"
SSEED=$(sed -n 's/^R \([A-Z0-9]*\) .*/\1/p' "$OUT/status")
[ -n "$SSEED" ] || { echo "FAIL: settable-space pool search found nothing"; cat "$OUT/status"; exit 1; }
case "$SSEED" in *0*) echo "FAIL: settable-space hit contains 0: $SSEED"; exit 1;; esac
grep -qx "$SSEED" "$OUT/pool_settable.txt" \
	|| { echo "FAIL: hit $SSEED is not a settable-pool member"; exit 1; }
echo "$SSEED" > "$OUT/hit_settable.seed"
SFIX=$("./native/brainstorm_native_search$EXE" fixture \
	"$OUT/search_settable.cfg" "$OUT/hit_settable.seed")
case "$SFIX" in
	"$SSEED 1"*) ;;
	*) echo "FAIL: fixture rejects the settable-space hit: $SFIX"; exit 1 ;;
esac
echo "PASS: vanilla-settable pool hit $SSEED contains no 0 and is one of $SRECORDS members"

# Refiltering must inherit the source's rank alphabet rather than falling back
# to the criteria file's default natural space.
REFILTER_TAG=$(awk -v primary="$TAG" \
	'$1 == "tagdef" && $3 == 1 && $2 != primary { print $2; exit }' \
	"$OUT/snapshot.cfg")
[ -n "$REFILTER_TAG" ] || REFILTER_TAG=$TAG
{
	echo "poolver 1"
	echo "threads 4"
	echo "start 0"
	echo "count all"
	echo "checkpoint 16384"
	echo "chunk 16384"
	echo "resume 0"
	echo "format binary"
	echo "tag_route observe"
	echo "tag $REFILTER_TAG 1 8 1"
	echo "end"
} > "$OUT/criteria_settable_refilter.cfg"
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/criteria_settable_refilter.cfg" "$OUT/pool_settable.bspool" \
	"$OUT/pool_settable_refiltered.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/pool_settable_refiltered.bspool" \
	"$OUT/pool_settable_refiltered.txt"
head -c 1024 "$OUT/pool_settable_refiltered.bspool" | tr -d '\0' \
	| grep -q '^space settable$' \
	|| { echo "FAIL: settable refilter lost its source seed space"; exit 1; }
if grep -q '0' "$OUT/pool_settable_refiltered.txt"; then
	echo "FAIL: settable refilter exported a seed containing 0"
	exit 1
fi
echo "PASS: refiltered settable pool preserves the no-zero rank space"

# --- total-space pool: all possible seeds are first-class members -----------
# Ranks 0..$COUNT of the total space are all seeds SHORTER than 8 characters
# (lengths 1-4 cover the first ~1.7M ranks alone), so every member -- and the
# eventual hit -- is a seed the game only reaches typed in. Also proves the
# space/label/pool_id header lines end to end.
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
	echo "space total"
	echo "label CI total-space pool"
	echo "tag $TAG 1 8 1"
	echo "legendary j_perkeo 1 8"
	echo "end"
} > "$OUT/criteria_total.cfg"

"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" "$OUT/criteria_total.cfg" "$OUT/pool_total.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/pool_total.bspool" "$OUT/pool_total.txt"

head -c 1024 "$OUT/pool_total.bspool" | tr -d '\0' > "$OUT/header_total.txt"
grep -q "^space total$" "$OUT/header_total.txt" || { echo "FAIL: header lacks 'space total'"; exit 1; }
grep -q "^label CI total-space pool$" "$OUT/header_total.txt" || { echo "FAIL: header lacks the label"; exit 1; }
grep -Eq "^pool_id [0-9a-f]{16}$" "$OUT/header_total.txt" || { echo "FAIL: header lacks a pool_id"; exit 1; }
TRECORDS=$(wc -l < "$OUT/pool_total.txt" | tr -d ' ')
[ "$TRECORDS" -gt 0 ] || { echo "FAIL: total-space pool is empty; grow seed-count"; exit 1; }
if [ -n "$(awk 'length($0) == 8 && $0 !~ /[0O]/' "$OUT/pool_total.txt")" ]; then
	echo "FAIL: pool over early total-space ranks must contain only non-natural seeds"
	exit 1
fi

# Production Lua independently verifies short/0/O members against the
# embedded tag Ante window and legendary Ante window. This is the regression
# for typed short seeds appearing one Ante off in-game.
sed -n '1,200p' "$OUT/pool_total.txt" > "$OUT/total-lua.seeds"
"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua \
	"$OUT/snapshot.cfg" "$OUT/pool_total.bspool" "$OUT/total-lua.seeds" \
	> "$OUT/total-lua.out"
if grep -v ' 1$' "$OUT/total-lua.out" >/dev/null; then
	echo "FAIL: production Lua rejects a possible short-seed pool member"; exit 1
fi

write_search_cfg j_perkeo "$OUT/search_total.cfg" "$OUT/pool_total.bspool"
date +%s > "$OUT/hb"
rm -f "$OUT/status" "$OUT/stop"
"./native/brainstorm_native_search$EXE" search "$OUT/search_total.cfg" \
	"$OUT/status" "$OUT/stop" "$OUT/hb"
TSEED=$(sed -n 's/^R \([A-Z0-9]*\) .*/\1/p' "$OUT/status")
[ -n "$TSEED" ] || { echo "FAIL: total-space pool search found nothing"; cat "$OUT/status"; exit 1; }
grep -qx "$TSEED" "$OUT/pool_total.txt" || { echo "FAIL: hit $TSEED is not a total-pool member"; exit 1; }
echo "$TSEED" > "$OUT/hit_total.seed"
TFIX=$("./native/brainstorm_native_search$EXE" fixture "$OUT/search_total.cfg" "$OUT/hit_total.seed")
case "$TFIX" in
	"$TSEED 1"*) ;;
	*) echo "FAIL: fixture rejects the total-space pool hit: $TFIX"; exit 1 ;;
esac
echo "PASS: total-space pool hit $TSEED (non-natural) is a member of the $TRECORDS-record pool and passes filters"

write_search_cfg j_triboulet "$OUT/search_total_none.cfg" "$OUT/pool_total.bspool"
date +%s > "$OUT/hb"
rm -f "$OUT/status" "$OUT/stop"
rc=0
"./native/brainstorm_native_search$EXE" search "$OUT/search_total_none.cfg" \
	"$OUT/status" "$OUT/stop" "$OUT/hb" || rc=$?
[ "$rc" -eq 3 ] || { echo "FAIL: expected total-space exhaustion exit 3, got $rc"; cat "$OUT/status"; exit 1; }
echo "PASS: total-space pool exhausts with a definitive verdict"

echo "POOL SEARCH EQUIVALENCE: ALL PASS"
