#!/bin/sh
# Verify that legacy soul_depth 2 is exclusive, survives pool metadata, and
# produces a route label with Soul #1 and Soul #2 in distinct packs; that
# soul_depth any ("2 Souls deep") is exactly the disjoint union of depths 1
# and 2; that a criteria may hold only ONE legendary rule; and that refilter
# stages compose inherited either-depth rules on one route.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
COUNT="${2:-5000000}"
OUT="${TMPDIR:-/tmp}/brainstorm_seed_pool_soul_depth"
LUAJIT_BIN="${LUAJIT:-luajit}"
mkdir -p "$OUT"

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh
		native_path() { cygpath -m "$1"; } ;;
	*) EXE=; BUILD=native/build.sh
		native_path() { printf '%s\n' "$1"; } ;;
esac

sh "$BUILD"
# The independent oracle may be a different LuaJIT build than Balatro's
# embedded runtime. Rewrite only PRNG expectations in the temporary snapshot
# so the C scanner and the Lua replay are calibrated to the same executable.
"$LUAJIT_BIN" tests/align_snapshot_prng.lua "$SNAPSHOT" > "$OUT/snapshot.cfg"

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
		[ "$depth" = 1 ] || echo "soul_depth $depth"
		echo "end"
	} > "$out"
}

write_criteria 1 "$OUT/depth1.cfg"
write_criteria 2 "$OUT/depth2.cfg"
write_criteria any "$OUT/depthany.cfg"

# Pinned reducer/Omen regression. The cheap Omen route probe must keep the
# Soul timeline's Ante-reducer ban even while repeated Soul validation is
# disabled. PFD21111 was formerly accepted only by buying Hieroglyph before
# Omen; production Lua correctly rejects that unresolved route.
printf '%s\n' PFD21111 > "$OUT/reducer-pfd.seed"
PFD_RESULT=$("./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" \
	"$OUT/depthany.cfg" "$OUT/reducer-pfd.seed")
case "$PFD_RESULT" in
	"PFD21111 0 -") ;;
	*) echo "FAIL: Omen probe admitted the pinned Hieroglyph route: $PFD_RESULT"; exit 1 ;;
esac

# The criteria compiler targets ONE legendary; a second rule must be refused.
sed '/^legendary j_perkeo 1 3 0$/a\
legendary j_triboulet 1 8 0' "$OUT/depth1.cfg" > "$OUT/tworules.cfg"
if "./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" "$OUT/tworules.cfg" \
		"$OUT/tworules.bspool" 2> "$OUT/tworules.err"; then
	echo "FAIL: a second legendary rule was accepted"; exit 1
fi
grep -q "only one legendary rule is supported" "$OUT/tworules.err" \
	|| { echo "FAIL: wrong error for a second legendary rule:"; cat "$OUT/tworules.err"; exit 1; }
sed 's/legendary j_perkeo 1 3 0/legendary j_perkeo 9 9 0/' \
	"$OUT/depth1.cfg" > "$OUT/ante9.cfg"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" "$OUT/depth1.cfg" "$OUT/depth1.bspool"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" "$OUT/depth2.cfg" "$OUT/depth2.bspool"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" "$OUT/ante9.cfg" "$OUT/ante9.bspool"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" "$OUT/depthany.cfg" "$OUT/depthany.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/depth1.bspool" "$OUT/depth1.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/depth2.bspool" "$OUT/depth2.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/ante9.bspool" "$OUT/ante9.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/depthany.bspool" "$OUT/depthany.txt"

D1=$(sed -n '1p' "$OUT/depth1.txt")
D2=$(sed -n '1p' "$OUT/depth2.txt")
[ -n "$D1" ] || { echo "FAIL: depth-1 sample is empty"; exit 1; }
[ -n "$D2" ] || { echo "FAIL: depth-2 sample is empty; increase seed-count"; exit 1; }
[ -s "$OUT/ante9.txt" ] || { echo "FAIL: exact-Ante-9 sample is empty; increase seed-count"; exit 1; }

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

# Either-depth accepts EXACTLY the disjoint union of the two exact depths:
# same window, same target, so no seed can be in both and none may be lost.
sort "$OUT/depth1.txt" "$OUT/depth2.txt" > "$OUT/union.sorted"
sort "$OUT/depthany.txt" > "$OUT/depthany.sorted"
NDUP=$(uniq -d < "$OUT/union.sorted" | wc -l | tr -d ' ')
[ "$NDUP" -eq 0 ] || { echo "FAIL: depth-1 and depth-2 pools overlap"; exit 1; }
cmp -s "$OUT/union.sorted" "$OUT/depthany.sorted" \
	|| { echo "FAIL: soul_depth any is not the union of depths 1 and 2"; exit 1; }

# Refilter composition: restricting the either-depth Perkeo pool to a legacy
# exclusive Triboulet-on-Soul-#2 stage forces Perkeo onto Soul #1, so the
# result must EQUAL the same refilter applied to the depth-1 Perkeo pool,
# and every member stays a depth-1 Perkeo member.
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
	echo "legendary j_triboulet 1 8 0"
	echo "soul_depth 2"
	echo "end"
} > "$OUT/refilter.cfg"

# 94LE4111 exposed the same reducer leak in the composed Soul #2 stage.
printf '%s\n' 94LE4111 > "$OUT/reducer-combo.seed"
COMBO_REDUCER_RESULT=$("./native/brainstorm_seed_pool$EXE" fixture \
	"$OUT/snapshot.cfg" "$OUT/refilter.cfg" "$OUT/reducer-combo.seed")
case "$COMBO_REDUCER_RESULT" in
	"94LE4111 0 -") ;;
	*) echo "FAIL: combined Omen probe admitted the pinned Hieroglyph route: $COMBO_REDUCER_RESULT"; exit 1 ;;
esac
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" "$OUT/refilter.cfg" \
	"$OUT/depthany.bspool" "$OUT/combo.bspool"
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" "$OUT/refilter.cfg" \
	"$OUT/depth1.bspool" "$OUT/combo1.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/combo.bspool" "$OUT/combo.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/combo1.bspool" "$OUT/combo1.txt"
[ -s "$OUT/combo.txt" ] || { echo "FAIL: combined refilter is empty; increase seed-count"; exit 1; }
sort "$OUT/combo.txt" > "$OUT/combo.sorted"
sort "$OUT/combo1.txt" > "$OUT/combo1.sorted"
cmp -s "$OUT/combo.sorted" "$OUT/combo1.sorted" \
	|| { echo "FAIL: refilter(either-depth) differs from refilter(depth-1) under a Soul#2 stage"; exit 1; }
sort "$OUT/depth1.txt" > "$OUT/depth1.sorted"
comm -23 "$OUT/combo.sorted" "$OUT/depth1.sorted" > "$OUT/combo.extra"
[ ! -s "$OUT/combo.extra" ] \
	|| { echo "FAIL: a combined-refilter member is missing from the depth-1 Perkeo pool"; exit 1; }
grep -a -q '^route_legendary j_perkeo 1 3 0 0$' "$OUT/combo.bspool" \
	|| { echo "FAIL: refiltered header lost the inherited either-depth route"; exit 1; }

grep -a -q '^soul_depth 2$' "$OUT/depth2.bspool"
grep -a -q '^soul_depth any$' "$OUT/depthany.bspool"
if grep -a -q '^soul_depth ' "$OUT/depth1.bspool"; then
	echo "FAIL: default depth 1 should not add new header metadata"; exit 1
fi
grep -a -q '^legendary j_triboulet 1 8 0$' "$OUT/combo.bspool"
grep -q '^second_soul_legendary j_perkeo 1 3 0$' "$OUT/depth2.bspool.manifest"
grep -q '^first_soul_legendary j_perkeo 1 3 0$' "$OUT/depth1.bspool.manifest"
grep -q '^either_soul_legendary j_perkeo 1 3 0$' "$OUT/depthany.bspool.manifest"
grep -q '^source_route_legendary j_perkeo 1 3 0 0$' "$OUT/combo.bspool.manifest"
grep -q '^second_soul_legendary j_triboulet 1 8 0$' "$OUT/combo.bspool.manifest"

# Pin production replay too; the old first-200 sampling could miss these
# false members when threaded block completion changed export order.
"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua "$OUT/snapshot.cfg" \
	"$OUT/depthany.bspool" "$OUT/reducer-pfd.seed" > "$OUT/reducer-pfd.lua"
grep -qx 'PFD21111 0' "$OUT/reducer-pfd.lua" \
	|| { echo "FAIL: production Lua unexpectedly accepted PFD21111"; exit 1; }
"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua "$OUT/snapshot.cfg" \
	"$OUT/combo.bspool" "$OUT/reducer-combo.seed" > "$OUT/reducer-combo.lua"
grep -qx '94LE4111 0' "$OUT/reducer-combo.lua" \
	|| { echo "FAIL: production Lua unexpectedly accepted 94LE4111"; exit 1; }

H1=$(sed -n 's/^criteria_hash //p' "$OUT/depth1.bspool.manifest")
H2=$(sed -n 's/^criteria_hash //p' "$OUT/depth2.bspool.manifest")
HA=$(sed -n 's/^criteria_hash //p' "$OUT/depthany.bspool.manifest")
[ "$H1" != "$H2" ] || { echo "FAIL: Soul depth is missing from the criteria fingerprint"; exit 1; }
[ "$HA" != "$H1" ] && [ "$HA" != "$H2" ] \
	|| { echo "FAIL: either-depth criteria must have its own fingerprint"; exit 1; }

# Independent oracle: production Brainstorm Lua replays the embedded header
# against the same snapshot. This catches a shared C-model error that the
# scanner-vs-scanner fixture checks above cannot see.
sed -n '1,200p' "$OUT/depth2.txt" > "$OUT/lua-depth2.seeds"
sed -n '1,200p' "$OUT/depth1.txt" > "$OUT/lua-depth1.seeds"
"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua "$OUT/snapshot.cfg" \
	"$OUT/depth2.bspool" "$OUT/lua-depth2.seeds" > "$OUT/lua-depth2.out"
"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua "$OUT/snapshot.cfg" \
	"$OUT/depth2.bspool" "$OUT/lua-depth1.seeds" > "$OUT/lua-depth1.out"
if grep -v ' 1$' "$OUT/lua-depth2.out" >/dev/null; then
	echo "FAIL: production Lua rejects a C depth-2 member"; exit 1
fi
if grep -v ' 0$' "$OUT/lua-depth1.out" >/dev/null; then
	echo "FAIL: production Lua accepts a depth-1 member as depth 2"; exit 1
fi

# Either-depth pool: production Lua must accept every member (both the
# depth-1-shaped and depth-2-shaped ones ride the same embedded header).
sed -n '1,200p' "$OUT/depthany.txt" > "$OUT/lua-depthany.seeds"
"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua "$OUT/snapshot.cfg" \
	"$OUT/depthany.bspool" "$OUT/lua-depthany.seeds" > "$OUT/lua-depthany.out"
if grep -v ' 1$' "$OUT/lua-depthany.out" >/dev/null; then
	echo "FAIL: production Lua rejects a C either-depth member"; exit 1
fi

# Combined-refilter pool: production Lua must accept every member (inherited
# either-depth Perkeo route + current exclusive Triboulet Soul #2 stage), and
# must reject plain depth-1 Perkeo members that lack the Triboulet Soul #2.
sed -n '1,200p' "$OUT/combo.txt" > "$OUT/lua-combo.seeds"
"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua "$OUT/snapshot.cfg" \
	"$OUT/combo.bspool" "$OUT/lua-combo.seeds" > "$OUT/lua-combo.out"
if grep -v ' 1$' "$OUT/lua-combo.out" >/dev/null; then
	echo "FAIL: production Lua rejects a C combined-refilter member"; exit 1
fi
comm -23 "$OUT/depth1.sorted" "$OUT/combo.sorted" | sed -n '1,200p' > "$OUT/lua-onlydepth1.seeds"
if [ -s "$OUT/lua-onlydepth1.seeds" ]; then
	"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua "$OUT/snapshot.cfg" \
		"$OUT/combo.bspool" "$OUT/lua-onlydepth1.seeds" > "$OUT/lua-onlydepth1.out"
	if grep -v ' 0$' "$OUT/lua-onlydepth1.out" >/dev/null; then
		echo "FAIL: production Lua accepts a depth-1-only member against the combined pool"; exit 1
	fi
fi

# The criteria engine supports Antes 1-39 even though ordinary in-game overlay
# controls stop at 8. Exercise the first dynamic Tag/shop_pack/Soul key at A9
# and verify the exact Ante window with production Lua.
sed -n '1,200p' "$OUT/ante9.txt" > "$OUT/lua-ante9.seeds"
"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua "$OUT/snapshot.cfg" \
	"$OUT/ante9.bspool" "$OUT/lua-ante9.seeds" > "$OUT/lua-ante9.out"
if grep -v ' 1$' "$OUT/lua-ante9.out" >/dev/null; then
	echo "FAIL: production Lua rejects an exact-Ante-9 C member"; exit 1
fi

# --- in-game searcher: pool-restricted search re-verifies the embedded
# either-depth / composed Soul criteria (check_pool_legend_rules) -----------
write_search_cfg() {
	legendary="$1"
	leganywhere="$2"
	poolf="$3"
	out="$4"
	{
		echo "session 1"
		echo "threads 2"
		echo "modelver 6"
		echo "entropy 98765.25"
		echo "soul 0"
		echo "legendary $legendary"
		echo "neglegendary 0"
		echo "tag -"
		echo "voucher -"
		echo "voucherante 1"
		echo "taganywhere 0"
		echo "leganywhere $leganywhere"
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

run_pool_search() {
	cfg="$1"
	date +%s > "$OUT/hb"
	rm -f "$OUT/status" "$OUT/stop"
	rc=0
	"./native/brainstorm_native_search$EXE" search "$cfg" \
		"$OUT/status" "$OUT/stop" "$OUT/hb" || rc=$?
}

write_search_cfg - 0 "$OUT/depthany.bspool" "$OUT/search_any.cfg"
run_pool_search "$OUT/search_any.cfg"
[ "$rc" -eq 0 ] || { echo "FAIL: either-depth pool search returned $rc"; cat "$OUT/status"; exit 1; }
AS=$(sed -n 's/^R \([A-Z0-9]*\) .*/\1/p' "$OUT/status")
grep -qx "$AS" "$OUT/depthany.txt" \
	|| { echo "FAIL: either-depth hit $AS is not a pool member"; exit 1; }
echo "PASS: searcher re-verified an either-depth pool member ($AS)"

write_search_cfg j_perkeo 1 "$OUT/combo.bspool" "$OUT/search_combo.cfg"
run_pool_search "$OUT/search_combo.cfg"
[ "$rc" -eq 0 ] || { echo "FAIL: combined pool search returned $rc"; cat "$OUT/status"; exit 1; }
TS=$(sed -n 's/^R \([A-Z0-9]*\) .*/\1/p' "$OUT/status")
grep -qx "$TS" "$OUT/combo.txt" \
	|| { echo "FAIL: combined-pool hit $TS is not a pool member"; exit 1; }
echo "PASS: searcher re-verified a combined either-depth+Soul#2 pool member ($TS)"

# Overlay first-Soul Triboulet contradicts the composed route (Perkeo takes
# Soul #1 in every member): the searcher must exhaust, never return a seed.
write_search_cfg j_triboulet 1 "$OUT/combo.bspool" "$OUT/search_conflict.cfg"
run_pool_search "$OUT/search_conflict.cfg"
[ "$rc" -eq 3 ] || { echo "FAIL: conflicting overlay returned $rc, want exhaustion 3"; cat "$OUT/status"; exit 1; }
grep -q "^E pool: no seed in the pool matches" "$OUT/status" \
	|| { echo "FAIL: missing exhaustion verdict"; cat "$OUT/status"; exit 1; }
echo "PASS: overlay conflicting with the composed Soul route exhausts definitively"

N1=$(wc -l < "$OUT/depth1.txt" | tr -d ' ')
N2=$(wc -l < "$OUT/depth2.txt" | tr -d ' ')
N9=$(wc -l < "$OUT/ante9.txt" | tr -d ' ')
NA=$(wc -l < "$OUT/depthany.txt" | tr -d ' ')
NC=$(wc -l < "$OUT/combo.txt" | tr -d ' ')
echo "PASS: C + production Lua agree on 1-deep, 2-deep, legacy-exclusive, and composed routes; depth1=$N1 depth2=$N2 any=$NA combo=$NC ante9=$N9 sample=$COUNT seed2=$D2"
