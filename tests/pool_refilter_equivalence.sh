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
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh
		native_path() { cygpath -m "$1"; } ;;
	*) EXE=; BUILD=native/build.sh
		native_path() { printf '%s\n' "$1"; } ;;
esac

sh "$BUILD"
cp "$SNAPSHOT" "$OUT/snapshot.cfg"

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
write_criteria "$OUT/combined.cfg" "tag $TAG 1 8 1" "legendary j_perkeo 1 8 0"
write_criteria "$OUT/early-collect.cfg" "tag $TAG 3 8 1" "legendary j_perkeo 1 2 0"
sed 's/^tag_route collect$/tag_route observe/' "$OUT/early-collect.cfg" \
	> "$OUT/early-observe.cfg"
HAS_ETHEREAL=0
if grep -q '^tagdef tag_ethereal ' "$OUT/snapshot.cfg"; then
	HAS_ETHEREAL=1
	write_criteria "$OUT/ethereal.cfg" "tag tag_ethereal 1 8 1" \
		"legendary j_perkeo 1 8 0"
fi

"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
	"$OUT/broad.cfg" "$OUT/broad.bspool"
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/narrow.cfg" "$OUT/broad.bspool" "$OUT/refined.bspool"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
	"$OUT/combined.cfg" "$OUT/combined.bspool"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
	"$OUT/narrow.cfg" "$OUT/legend-source.bspool"
if [ "$HAS_ETHEREAL" -eq 1 ]; then
	"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
		"$OUT/ethereal.cfg" "$OUT/ethereal.bspool"
fi
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/broad.cfg" "$OUT/legend-source.bspool" "$OUT/reverse-refined.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/broad.bspool" "$OUT/broad.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/refined.bspool" "$OUT/refined.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/combined.bspool" "$OUT/expected.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/legend-source.bspool" "$OUT/legend-source.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/reverse-refined.bspool" "$OUT/reverse-refined.txt"
if [ "$HAS_ETHEREAL" -eq 1 ]; then
	"./native/brainstorm_seed_pool$EXE" export "$OUT/ethereal.bspool" "$OUT/ethereal.txt"
fi

# A tag occurrence before its requested Ante window is not collected. Since
# these tag rules begin at A3 while the target Soul is restricted to A1-A2,
# collect and observe must accept exactly the same seeds even if the same tag
# rolled on an earlier blind.
sed -n '1,200000p' "$OUT/broad.txt" > "$OUT/early-window-candidates.txt"
"./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" \
	"$OUT/early-collect.cfg" "$OUT/early-window-candidates.txt" \
	| awk '$2 == 1 { print $1 }' | sort > "$OUT/early-collect.accepted"
"./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" \
	"$OUT/early-observe.cfg" "$OUT/early-window-candidates.txt" \
	| awk '$2 == 1 { print $1 }' | sort > "$OUT/early-observe.accepted"
cmp "$OUT/early-collect.accepted" "$OUT/early-observe.accepted"
[ -s "$OUT/early-collect.accepted" ] || {
	echo "FAIL: Ante-window regression sample is empty; grow seed-count"; exit 1;
}

# With a collected Charm/Ethereal rule, prove that at least one accepted pool
# member gets its target from the immediate reward rather than a shop pack.
case "$TAG" in
	tag_charm) reward_name=Charm ;;
	tag_ethereal) reward_name=Ethereal ;;
	*) reward_name= ;;
esac
if [ -n "$reward_name" ]; then
	"./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" \
		"$OUT/combined.cfg" "$OUT/expected.txt" > "$OUT/reward-source.fixture"
	if ! grep -Eq "j_perkeo=A[0-9]+${reward_name}(Sm|Big)" "$OUT/reward-source.fixture"; then
		echo "FAIL: no accepted $reward_name reward-pack legendary hit"; exit 1
	fi
fi
if [ "$HAS_ETHEREAL" -eq 1 ]; then
	"./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" \
		"$OUT/ethereal.cfg" "$OUT/ethereal.txt" > "$OUT/ethereal.fixture"
	if ! grep -Eq 'j_perkeo=A[0-9]+Ethereal(Sm|Big)' "$OUT/ethereal.fixture"; then
		echo "FAIL: no accepted Ethereal reward-pack legendary hit"; exit 1
	fi
	sed -n '1,200p' "$OUT/ethereal.txt" > "$OUT/ethereal-lua.seeds"
	"${LUAJIT:-luajit}" tests/pool_lua_oracle.lua Brainstorm_reroll.lua \
		"$OUT/snapshot.cfg" "$OUT/ethereal.bspool" "$OUT/ethereal-lua.seeds" \
		> "$OUT/ethereal-lua.out"
	if grep -v ' 1$' "$OUT/ethereal-lua.out" >/dev/null; then
		echo "FAIL: production Lua rejects an Ethereal reward member"; exit 1
	fi
fi

sort "$OUT/refined.txt" > "$OUT/refined.sorted.txt"
sort "$OUT/expected.txt" > "$OUT/expected.sorted.txt"
cmp "$OUT/refined.sorted.txt" "$OUT/expected.sorted.txt"
sort "$OUT/legend-source.txt" > "$OUT/legend-source.sorted.txt"
comm -12 "$OUT/legend-source.sorted.txt" "$OUT/expected.sorted.txt" \
	> "$OUT/reverse-expected.sorted.txt"
sort "$OUT/reverse-refined.txt" > "$OUT/reverse-refined.sorted.txt"
cmp "$OUT/reverse-refined.sorted.txt" "$OUT/reverse-expected.sorted.txt"
grep -q '^complete 1$' "$OUT/refined.bspool"
grep -q '^source_criteria_hash ' "$OUT/refined.bspool"
grep -q "^route_tag collect $TAG 1 8 1$" "$OUT/refined.bspool"
grep -q '^route_legendary j_perkeo 1 8 0 1$' "$OUT/reverse-refined.bspool"
grep -q "^space $SPACE$" "$OUT/refined.bspool"

# The in-game native overlay must inherit the input pool's collected-tag
# skips. With no active tag filter, first-Soul Perkeo over the broad pool must
# still equal the one-pass/refilter result exactly.
{
	echo "session 1"; echo "threads 4"; echo "modelver 6"; echo "entropy 1"
	echo "soul 0"; echo "legendary j_perkeo"; echo "neglegendary 0"; echo "tag -"
	echo "voucher -"; echo "voucherante 1"; echo "taganywhere 0"; echo "leganywhere 1"
	echo "matchany 0"; echo "jslot 1 - 0"; echo "jslot 2 - 0"; echo "jslot 3 - 0"
	echo "maslots 0 0 0 0 0 0 0 0"; echo "mapacks 0 0 0 0 0 0 0 0"; echo "packslots 2"
	echo "poolfile $(native_path "$OUT/broad.bspool")"
	grep -E '^(tagdef|vouchdef|jokerdef|boostdef|specialdef|check_[a-z0-9]+) ' "$OUT/snapshot.cfg"
	echo "end"
} > "$OUT/overlay.cfg"
"./native/brainstorm_native_search$EXE" fixture "$OUT/overlay.cfg" "$OUT/broad.txt" \
	| awk '$2 == 1 { print $1 }' | sort > "$OUT/overlay.sorted.txt"
cmp "$OUT/refined.sorted.txt" "$OUT/overlay.sorted.txt"

# Adding a collected tag on top of a legendary pool must re-check that inherited
# Soul after the tag changes its route. A refilter can only remove source
# records, however: it cannot recover seeds newly made valid by the later tag.
# Native and Lua overlays therefore equal (source pool INTERSECT one-pass), not
# the unrestricted one-pass scan.
{
	echo "session 1"; echo "threads 4"; echo "modelver 6"; echo "entropy 1"
	echo "soul 0"; echo "legendary -"; echo "neglegendary 0"; echo "tag $TAG"
	echo "voucher -"; echo "voucherante 1"; echo "taganywhere 1"; echo "leganywhere 0"
	echo "matchany 0"; echo "jslot 1 - 0"; echo "jslot 2 - 0"; echo "jslot 3 - 0"
	echo "maslots 0 0 0 0 0 0 0 0"; echo "mapacks 0 0 0 0 0 0 0 0"; echo "packslots 2"
	echo "poolfile $(native_path "$OUT/legend-source.bspool")"
	grep -E '^(tagdef|vouchdef|jokerdef|boostdef|specialdef|check_[a-z0-9]+) ' "$OUT/snapshot.cfg"
	echo "end"
} > "$OUT/reverse-overlay.cfg"
"./native/brainstorm_native_search$EXE" fixture "$OUT/reverse-overlay.cfg" "$OUT/legend-source.txt" \
	| awk '$2 == 1 { print $1 }' | sort > "$OUT/reverse-overlay.sorted.txt"
cmp "$OUT/reverse-expected.sorted.txt" "$OUT/reverse-overlay.sorted.txt"
"${LUAJIT:-luajit}" tests/pool_lua_oracle.lua Brainstorm_reroll.lua \
	"$OUT/snapshot.cfg" "$OUT/legend-source.bspool" "$OUT/legend-source.txt" "$TAG" \
	| awk '$2 == 1 { print $1 }' | sort > "$OUT/reverse-lua-overlay.sorted.txt"
cmp "$OUT/reverse-expected.sorted.txt" "$OUT/reverse-lua-overlay.sorted.txt"
sed -n '1,200p' "$OUT/reverse-refined.txt" > "$OUT/reverse-lua.seeds"
"${LUAJIT:-luajit}" tests/pool_lua_oracle.lua Brainstorm_reroll.lua \
	"$OUT/snapshot.cfg" "$OUT/reverse-refined.bspool" "$OUT/reverse-lua.seeds" \
	> "$OUT/reverse-lua.out"
if grep -v ' 1$' "$OUT/reverse-lua.out" >/dev/null; then
	echo "FAIL: production Lua rejects a cumulative-route refilter member"; exit 1
fi

RECORDS=$(wc -l < "$OUT/refined.txt" | tr -d ' ')
REVERSE_RECORDS=$(wc -l < "$OUT/reverse-refined.txt" | tr -d ' ')
[ "$RECORDS" -gt 0 ] || { echo "FAIL: refined pool is empty; grow seed-count"; exit 1; }
echo "PASS: final routes, source-set intersection, Ante windows, tag rewards, and in-game overlays agree (forward=$RECORDS reverse=$REVERSE_RECORDS)"
