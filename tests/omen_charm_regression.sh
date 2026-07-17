#!/usr/bin/env bash
# Deterministic oracle regression for Omen Globe + targeted Charm routes.
# This test is production-independent by design: it pins the base-game call
# order and provides golden traces for the native/Lua implementation to match.
set -euo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ORACLE="$ROOT/tests/omen_charm_oracle.lua"
LUAJIT_BIN=${LUAJIT_BIN:-luajit}

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

if ! command -v "$LUAJIT_BIN" >/dev/null 2>&1; then
	fail "luajit is required (set LUAJIT_BIN to its path)"
fi

expected_summaries=$'SUMMARY\t11111111\tconverted=1\tsoul=0\tblack_hole=0\tnormal_spectral=1\tomen_advances=5\ttarot_advances=4\tspectral_advances=2\nSUMMARY\tM8111111\tconverted=3\tsoul=4\tblack_hole=0\tnormal_spectral=2\tomen_advances=5\ttarot_advances=2\tspectral_advances=5\nSUMMARY\tER111111\tconverted=1\tsoul=0\tblack_hole=4\tnormal_spectral=0\tomen_advances=5\ttarot_advances=4\tspectral_advances=2\nSUMMARY\tMV111111\tconverted=3\tsoul=0\tblack_hole=3\tnormal_spectral=2\tomen_advances=5\ttarot_advances=2\tspectral_advances=5'
actual_summaries=$("$LUAJIT_BIN" "$ORACLE" fixtures)
if [[ "$actual_summaries" != "$expected_summaries" ]]; then
	echo "Expected:" >&2
	printf '%s\n' "$expected_summaries" >&2
	echo "Actual:" >&2
	printf '%s\n' "$actual_summaries" >&2
	fail "Omen/Charm fixture summaries changed"
fi
echo "PASS five Omen advances per five-card Charm reward"
echo "PASS converted Arcana cards use Spectral Soul advances"

no_omen=$("$LUAJIT_BIN" "$ORACLE" trace M8111111 1 1 0 | sed -n '1p')
expected_no_omen=$'SUMMARY\tM8111111\tconverted=0\tsoul=0\tblack_hole=0\tnormal_spectral=0\tomen_advances=0\ttarot_advances=5\tspectral_advances=0'
[[ "$no_omen" == "$expected_no_omen" ]] \
	|| fail "Arcana cards advanced Omen before the voucher was owned"
echo "PASS Omen stream advances only while voucher is owned"

ownership=$("$LUAJIT_BIN" "$ORACLE" ownership M8111111 1)
expected_ownership=$'OWNERSHIP\tseed=M8111111\tprepurchase_omen_advances=0\tpostpurchase_first_index=1\tmatches_fresh=1\tpostpurchase_first_roll=0.91469008459800394\tfresh_first_roll=0.91469008459800394'
[[ "$ownership" == "$expected_ownership" ]] \
	|| fail "buying Omen before a same-shop pack did not start its stream correctly"
echo "PASS same-shop packs see Omen immediately after purchase"

er_trace=$("$LUAJIT_BIN" "$ORACLE" trace ER111111 1)
er_overwrite=$'CHARM\tcard=4\tomen=0.98220106577863953\toi=4\tconverted=1\ttype=Spectral\tsoul=0.99810098171210226\tsi=1\tsoul_hit=1\tblack_hole=0.99854395350759328\tbi=2\tblack_hole_hit=1\toverwrite=1\toutcome=BlackHole'
grep -Fqx "$er_overwrite" <<< "$er_trace" \
	|| fail "ER111111 no longer proves same-card Black Hole overwrites Soul"
echo "PASS Soul then Black Hole call order and overwrite"

er_banned=$("$LUAJIT_BIN" "$ORACLE" trace ER111111 1 0 1)
er_banned_row=$'CHARM\tcard=4\tomen=0.98220106577863953\toi=4\tconverted=1\ttype=Spectral\tsoul=0.99810098171210226\tsi=1\tsoul_hit=1\tblack_hole=0.99854395350759328\tbi=2\tblack_hole_hit=1\toverwrite=1\toutcome=Spectral'
grep -Fqx "$er_banned_row" <<< "$er_banned" \
	|| fail "banned Black Hole did not consume/overwrite without entering the pack"
echo "PASS banned Black Hole still consumes and overwrites its Soul roll"

mv_trace=$("$LUAJIT_BIN" "$ORACLE" trace MV111111 1)
mv_hit=$'CHARM\tcard=3\tomen=0.92214688932239164\toi=3\tconverted=1\ttype=Spectral\tsoul=0.26061661917003121\tsi=3\tsoul_hit=0\tblack_hole=0.99762013486912493\tbi=4\tblack_hole_hit=1\toverwrite=0\toutcome=BlackHole'
mv_gate=$'CHARM\tcard=4\tomen=0.93573608203174441\toi=4\tconverted=1\ttype=Spectral\tsoul=0.4701017612242433\tsi=5\tsoul_hit=0\tblack_hole=-\tbi=0\tblack_hole_hit=0\toverwrite=0\toutcome=Spectral'
grep -Fqx "$mv_hit" <<< "$mv_trace" \
	|| fail "MV111111 Black Hole gate fixture lost its hit"
grep -Fqx "$mv_gate" <<< "$mv_trace" \
	|| fail "later Spectral card rerolled Black Hole after one entered the pack"
echo "PASS pack-wide Black Hole gate is shared across converted cards"

branch=$("$LUAJIT_BIN" "$ORACLE" branch M8111111 1 | sed -n '1p')
expected_branch=$'BRANCH\tseed=M8111111\tcanonical_unchanged=1\tcharm_cards=5\tcanonical_cards=3\tcharm_soul=4\tcanonical_soul=0\trequired_charm=1'
[[ "$branch" == "$expected_branch" ]] \
	|| fail "targeted Charm branch did not isolate canonical RNG or mark a branch-only Soul"
echo "PASS Charm opens five cards in a cloned branch"
echo "PASS M8111111 distinguishes required-Charm from canonical route"

# Production integration: give the native pool scanner an exact fresh-run
# voucher catalog, then compare the same seed with and without starting Omen.
# M8111111 has a Big-blind Charm whose fourth converted card is the first Soul;
# the canonical shop route has none, so the accepted label must name the
# required branch rather than silently treating Charm as already collected.
SNAPSHOT=${SNAPSHOT:-$ROOT/native_search.cfg}
if [[ -f "$SNAPSHOT" ]]; then
	tmpdir=$(mktemp -d)
	trap 'rm -rf "$tmpdir"' EXIT
	expected_vouchers=$'v_overstock_norm\nv_overstock_plus\nv_clearance_sale\nv_liquidation\nv_hone\nv_glow_up\nv_reroll_surplus\nv_reroll_glut\nv_crystal_ball\nv_omen_globe\nv_telescope\nv_observatory\nv_grabber\nv_nacho_tong\nv_wasteful\nv_recyclomancy\nv_tarot_merchant\nv_tarot_tycoon\nv_planet_merchant\nv_planet_tycoon\nv_seed_money\nv_money_tree\nv_blank\nv_antimatter\nv_magic_trick\nv_illusion\nv_hieroglyph\nv_petroglyph\nv_directors_cut\nv_retcon\nv_paint_brush\nv_palette'
	fixture_snapshot=$SNAPSHOT
	actual_vouchers=$(awk '$1 == "vouchdef" { print $2 }' "$SNAPSHOT")
	if [[ "$actual_vouchers" != "$expected_vouchers" ]]; then
		fixture_snapshot="$tmpdir/vanilla-vouchers.cfg"
		{
			awk '$1 != "vouchdef" && $1 != "vouchroute" && $1 != "vouchowned" && $1 != "end" { print }' "$SNAPSHOT"
			printf '%s\n' "$expected_vouchers" | awk '
				NR % 2 == 1 { print "vouchdef " $0 " 1" }
				NR % 2 == 0 { print "vouchdef " $0 " 0" }'
			printf '%s\n' end
		} > "$fixture_snapshot"
	fi
	awk '
	$1 == "vouchroute" || $1 == "vouchowned" { next }
	$1 == "boostdef" { $7 = 1 }
	{ print }
	$1 == "vouchdef" {
		n++
		if (n % 2) { prerequisite = $2; print "vouchroute " $2 " 1 -" }
		else print "vouchroute " $2 " 1 " prerequisite
	}' "$fixture_snapshot" > "$tmpdir/base.cfg"
	awk '$1 == "end" { print "vouchowned v_crystal_ball"; print "vouchowned v_omen_globe" }
		{ print }' "$tmpdir/base.cfg" > "$tmpdir/omen.cfg"
	printf '%s\n' M8111111 > "$tmpdir/seeds"
	printf '%s\n' \
		'poolver 1' 'threads 1' 'start 0' 'count 8' 'checkpoint 8' \
		'chunk 8' 'resume 0' 'format binary' 'tag_route collect' \
		'legendary j_chicot 1 small 4 big 0 charm' 'end' > "$tmpdir/criteria.cfg"
	"$ROOT/native/brainstorm_seed_pool" fixture "$tmpdir/base.cfg" \
		"$tmpdir/criteria.cfg" "$tmpdir/seeds" > "$tmpdir/base.out"
	"$ROOT/native/brainstorm_seed_pool" fixture "$tmpdir/omen.cfg" \
		"$tmpdir/criteria.cfg" "$tmpdir/seeds" > "$tmpdir/omen.out"
	[[ $(<"$tmpdir/base.out") == 'M8111111 0 -' ]] \
		|| fail "native canonical route unexpectedly found the Charm-only Soul"
	[[ $(<"$tmpdir/omen.out") == 'M8111111 1 j_chicot=A1CharmBig CharmRequired=A1Big' ]] \
		|| fail "native targeted Charm/Omen route did not match its pinned label"
	echo "PASS native targeted Charm branch and starting-Omen timing"
	printf '%s\n' 1SNK1111 > "$tmpdir/purchased.seed"
	printf '%s\n' \
		'poolver 1' 'threads 1' 'start 0' 'count 8' 'checkpoint 8' \
		'chunk 8' 'resume 0' 'format binary' 'tag_route collect' \
		'legendary j_chicot 1 small 4 big 0 shop' 'end' > "$tmpdir/purchased.cfg"
	"$ROOT/native/brainstorm_seed_pool" fixture "$tmpdir/base.cfg" \
		"$tmpdir/purchased.cfg" "$tmpdir/purchased.seed" > "$tmpdir/purchased.out"
	[[ $(<"$tmpdir/purchased.out") == \
		'1SNK1111 1 BuyRoute=v_crystal_ball@A1V1,v_omen_globe@A2V1 j_chicot=A3ShopBoss' ]] \
		|| fail "native purchased-Omen route did not activate before the later shop Soul"
	echo "PASS native minimum purchase route activates Omen before later packs"

	seed_rank() {
		python3 - "$1" <<'PY'
import sys
chars = "123456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
rank = 0
for position, char in enumerate(sys.argv[1]):
    rank += chars.index(char) * (len(chars) ** position)
print(rank)
PY
	}
	write_scan_criteria() {
		local start=$1 predicate=$2 output=$3
		printf '%s\n' \
			'poolver 1' 'threads 1' "start $start" 'count 1' \
			'checkpoint 8' 'chunk 8' 'resume 0' 'format binary' \
			'tag_route collect' "$predicate" 'end' > "$output"
	}
	write_overlay() {
		local snapshot=$1 pool=$2 output=$3
		{
			printf '%s\n' \
				'session 1' 'threads 1' 'modelver 6' 'entropy 1' \
				'soul 0' 'legendary -' 'neglegendary 0' 'tag -' \
				'voucher -' 'voucherante 1' 'taganywhere 0' 'leganywhere 0' \
				'matchany 0' 'jslot 1 - 0' 'jslot 2 - 0' 'jslot 3 - 0' \
				'maslots 0 0 0 0 0 0 0 0' 'mapacks 0 0 0 0 0 0 0 0' \
				'packslots 2'
			printf 'poolfile %s\n' "$pool"
			grep -E '^(tagdef|vouchdef|vouchroute|vouchowned|jokerdef|boostdef|specialdef|check_[a-z0-9]+) ' "$snapshot"
			printf '%s\n' end
		} > "$output"
	}

	write_scan_criteria "$(seed_rank M8111111)" \
		'legendary j_chicot 1 small 4 big 0 charm' "$tmpdir/charm-scan.cfg"
	"$ROOT/native/brainstorm_seed_pool" scan "$tmpdir/omen.cfg" \
		"$tmpdir/charm-scan.cfg" "$tmpdir/charm.bspool" >/dev/null 2>/dev/null
	write_overlay "$tmpdir/omen.cfg" "$tmpdir/charm.bspool" "$tmpdir/charm-overlay.cfg"
	"$ROOT/native/brainstorm_native_search" fixture "$tmpdir/charm-overlay.cfg" \
		"$tmpdir/seeds" > "$tmpdir/charm-overlay.out"
	grep -q '^M8111111 1 ' "$tmpdir/charm-overlay.out" \
		|| fail "in-game pool loader did not replay a targeted Charm branch"

	write_scan_criteria "$(seed_rank 1SNK1111)" \
		'legendary j_chicot 1 small 4 big 0 shop' "$tmpdir/purchased-scan.cfg"
	"$ROOT/native/brainstorm_seed_pool" scan "$tmpdir/base.cfg" \
		"$tmpdir/purchased-scan.cfg" "$tmpdir/purchased.bspool" >/dev/null 2>/dev/null
	write_overlay "$tmpdir/base.cfg" "$tmpdir/purchased.bspool" "$tmpdir/purchased-overlay.cfg"
	"$ROOT/native/brainstorm_native_search" fixture "$tmpdir/purchased-overlay.cfg" \
		"$tmpdir/purchased.seed" > "$tmpdir/purchased-overlay.out"
	grep -q '^1SNK1111 1 ' "$tmpdir/purchased-overlay.out" \
		|| fail "in-game pool loader did not replay a purchased-Omen branch"
	"$LUAJIT_BIN" "$ROOT/tests/pool_lua_oracle.lua" "$ROOT/Brainstorm_reroll.lua" \
		"$tmpdir/omen.cfg" "$tmpdir/charm.bspool" "$tmpdir/seeds" \
		> "$tmpdir/charm-lua.out"
	grep -q '^M8111111 1$' "$tmpdir/charm-lua.out" \
		|| fail "production Lua rejected the targeted-Charm pool member"
	"$LUAJIT_BIN" "$ROOT/tests/pool_lua_oracle.lua" "$ROOT/Brainstorm_reroll.lua" \
		"$tmpdir/base.cfg" "$tmpdir/purchased.bspool" "$tmpdir/purchased.seed" \
		> "$tmpdir/purchased-lua.out"
	grep -q '^1SNK1111 1$' "$tmpdir/purchased-lua.out" \
		|| fail "production Lua rejected the purchased-Omen pool member"
	echo "PASS native and production-Lua loaders replay targeted Charm and purchased Omen routes"

	# Voucher availability is unrelated when there is no voucher predicate and
	# the minimum route buys nothing. A challenge may ban every offer; starting
	# Omen must still compose with a targeted Charm instead of making the route
	# walker fail merely because no voucher can be rolled.
	awk '
		$1 == "vouchdef" || $1 == "vouchroute" { $3 = 0 }
		{ print }
	' "$tmpdir/omen.cfg" > "$tmpdir/no-offers-omen.cfg"
	write_scan_criteria "$(seed_rank M8111111)" \
		'legendary j_chicot 1 small 4 big 0 charm' "$tmpdir/no-offers-charm.cfg"
	"$ROOT/native/brainstorm_seed_pool" scan "$tmpdir/no-offers-omen.cfg" \
		"$tmpdir/no-offers-charm.cfg" "$tmpdir/no-offers-charm.bspool" \
		>/dev/null 2>/dev/null
	"$LUAJIT_BIN" "$ROOT/tests/pool_lua_oracle.lua" "$ROOT/Brainstorm_reroll.lua" \
		"$tmpdir/no-offers-omen.cfg" "$tmpdir/no-offers-charm.bspool" \
		"$tmpdir/seeds" > "$tmpdir/no-offers-charm-lua.out"
	grep -q '^M8111111 1$' "$tmpdir/no-offers-charm-lua.out" \
		|| fail "a no-offer challenge rejected its voucher-independent Charm route"
	echo "PASS voucher-independent Charm route works when every offer is unavailable"
else
	echo "SKIP native targeted-branch fixture (snapshot not found: $SNAPSHOT)"
fi

if [[ -n "${BALATRO_SRC:-}" ]]; then
	"$LUAJIT_BIN" "$ORACLE" source-check "$BALATRO_SRC"
else
	echo "SKIP base-source text check (set BALATRO_SRC to an extracted Balatro source tree)"
fi

echo "PASS omen/charm oracle regression"
