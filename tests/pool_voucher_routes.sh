#!/bin/sh
# Deterministic regression for the standalone pool builder's voucher-route
# search. The fixture snapshot makes the complete vanilla voucher catalog
# route-eligible while preserving its index order and base/upgrade pairs.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
OUT="${TMPDIR:-/tmp}/brainstorm_pool_voucher_routes"
LUAJIT_BIN="${LUAJIT:-luajit}"
rm -rf "$OUT"
mkdir -p "$OUT"

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh
		native_path() { cygpath -m "$1"; } ;;
	*) EXE=; BUILD=native/build.sh
		native_path() { printf '%s\n' "$1"; } ;;
esac

fail() {
	printf 'FAIL: %s\n' "$*" >&2
	exit 1
}

[ -f "$SNAPSHOT" ] || fail "snapshot not found: $SNAPSHOT"

# These seeds are tied to the vanilla catalog's stable index order. Real game
# snapshots already contain it; the small CI oracle snapshot does not, so that
# fixture is upgraded below with the same canonical catalog before testing.
EXPECTED_VOUCHERS='v_overstock_norm
v_overstock_plus
v_clearance_sale
v_liquidation
v_hone
v_glow_up
v_reroll_surplus
v_reroll_glut
v_crystal_ball
v_omen_globe
v_telescope
v_observatory
v_grabber
v_nacho_tong
v_wasteful
v_recyclomancy
v_tarot_merchant
v_tarot_tycoon
v_planet_merchant
v_planet_tycoon
v_seed_money
v_money_tree
v_blank
v_antimatter
v_magic_trick
v_illusion
v_hieroglyph
v_petroglyph
v_directors_cut
v_retcon
v_paint_brush
v_palette'
ACTUAL_VOUCHERS=$(awk '$1 == "vouchdef" { print $2 }' "$SNAPSHOT")
FIXTURE_SNAPSHOT=$SNAPSHOT
if [ "$ACTUAL_VOUCHERS" != "$EXPECTED_VOUCHERS" ]; then
	FIXTURE_SNAPSHOT="$OUT/vanilla-catalog.cfg"
	{
		awk '$1 != "vouchdef" && $1 != "vouchroute" && $1 != "vouchowned" && $1 != "end" { print }' "$SNAPSHOT"
		printf '%s\n' "$EXPECTED_VOUCHERS" | awk '
			NR % 2 == 1 { print "vouchdef " $0 " 1" }
			NR % 2 == 0 { print "vouchdef " $0 " 0" }'
		printf '%s\n' end
	} > "$FIXTURE_SNAPSHOT"
fi

# Older snapshots have only vouchdef. Newer snapshots may already carry
# vouchroute, so discard any existing route lines before installing the fully
# unlocked deterministic fixture catalog. Every even entry is the upgrade of
# the base voucher immediately before it.
awk '
$1 == "vouchroute" { next }
{ print }
$1 == "vouchdef" {
	voucher_number++
	if (voucher_number % 2 == 1) {
		prerequisite = $2
		print "vouchroute " $2 " 1 -"
	} else {
		print "vouchroute " $2 " 1 " prerequisite
	}
}
' "$FIXTURE_SNAPSHOT" > "$OUT/snapshot.cfg"

sh "$BUILD"

write_criteria() {
	file=$1
	shift
	{
		printf '%s\n' \
			'poolver 1' \
			'threads 1' \
			'start 0' \
			'count 16384' \
			'checkpoint 16384' \
			'chunk 16384' \
			'resume 0' \
			'format binary' \
			'tag_route collect'
		for predicate in "$@"; do
			printf '%s\n' "$predicate"
		done
		printf '%s\n' end
	} > "$file"
}

assert_fixture() {
	name=$1
	seed=$2
	expected=$3
	shift 3
	write_criteria "$OUT/$name.cfg" "$@"
	printf '%s\n' "$seed" > "$OUT/$name.seed"
	actual=$("./native/brainstorm_seed_pool$EXE" fixture \
		"$OUT/snapshot.cfg" "$OUT/$name.cfg" "$OUT/$name.seed")
	if [ "$actual" != "$expected" ]; then
		printf 'FAIL: %s\n  expected: %s\n  actual:   %s\n' \
			"$name" "$expected" "$actual" >&2
		exit 1
	fi
}

# An unbought voucher is eligible again on the next normal roll. This catches
# the old, incorrect behavior that blanked the previous offer regardless of
# whether it was purchased.
assert_fixture repeat_unbought 71111111 \
	'71111111 1 v_honeA2V1' \
	'voucher v_hone 2 2'

# The A1 Clearance Sale offer has two valid branches: skip it to see Seed
# Money at A2, or buy it to unlock and see Liquidation at A2.
assert_fixture skip_base I1111111 \
	'I1111111 1 v_seed_moneyA2V1' \
	'voucher v_seed_money 2 2'
assert_fixture buy_base_for_upgrade I1111111 \
	'I1111111 1 v_liquidationA2V1 BuyRoute=v_clearance_sale@A1V1' \
	'voucher v_liquidation 2 2'
assert_fixture excluded_prerequisite I1111111 \
	'I1111111 0 -' \
	'voucher v_liquidation 2 2' \
	'voucher_exclude v_clearance_sale'

# Excluding the target forbids buying it; it must not hide an otherwise valid
# offer of that voucher.
assert_fixture excluded_target_still_offered A1111111 \
	'A1111111 1 v_overstock_normA1V1' \
	'voucher v_overstock_norm 1 1' \
	'voucher_exclude v_overstock_norm'

# Hieroglyph and Petroglyph each reduce the displayed Ante. The subsequent
# Boss returns to the same Ante and consumes the next value from Voucher1.
assert_fixture hieroglyph_repeat 22111111 \
	'22111111 1 v_telescopeA1V2 BuyRoute=v_hieroglyph@A1V1' \
	'voucher v_telescope 1 1'
assert_fixture hieroglyph_petroglyph_repeat P6111111 \
	'P6111111 1 v_reroll_surplusA1V3 BuyRoute=v_hieroglyph@A1V1,v_petroglyph@A1V2' \
	'voucher v_reroll_surplus 1 1'

# The currently scoped mixed model allows a minimum voucher-target route to
# use Hieroglyph while leaving the canonical Legendary pack timeline unchanged;
# only an Omen/Soul fallback or a tag route forbids the unresolved interaction.
assert_fixture mixed_legendary_reducer XE511111 \
	'XE511111 1 v_honeA4V2 BuyRoute=v_hieroglyph@A4V1 j_chicot=A4ShopBig' \
	'voucher v_hone 1 4' \
	'voucher_exclude v_overstock_norm' \
	'legendary j_chicot 1 small 4 big 0 any'

# This seed reaches Overstock by A4 only by purchasing Tarot Merchant at A1.
# Excluding both merchant bases represents the user's disallowed merchant set
# and must therefore make the route fail.
assert_fixture merchant_route 44111111 \
	'44111111 1 v_overstock_normA4V1 BuyRoute=v_tarot_merchant@A1V1' \
	'voucher v_overstock_norm 1 4'
assert_fixture merchants_excluded 44111111 \
	'44111111 0 -' \
	'voucher v_overstock_norm 1 4' \
	'voucher_exclude v_tarot_merchant' \
	'voucher_exclude v_planet_merchant'

# Persist a broad voucher route as a schema-3 pool, then add a second voucher
# in a refilter. This exercises event metadata and the promotion of the source
# criteria into cumulative route_* header directives.
write_criteria "$OUT/source-pool.cfg" \
	'voucher v_overstock_norm 1 4' \
	'voucher_exclude v_tarot_merchant' \
	'voucher_exclude v_planet_merchant'
write_criteria "$OUT/refilter-pool.cfg" \
	'voucher v_hone 1 4' \
	'voucher_exclude v_overstock_norm'
write_criteria "$OUT/combined-pool.cfg" \
	'voucher v_overstock_norm 1 4' \
	'voucher v_hone 1 4' \
	'voucher_exclude v_tarot_merchant' \
	'voucher_exclude v_planet_merchant' \
	'voucher_exclude v_overstock_norm'

"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
	"$OUT/source-pool.cfg" "$OUT/source.bspool"
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/refilter-pool.cfg" "$OUT/source.bspool" "$OUT/refined.bspool"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
	"$OUT/combined-pool.cfg" "$OUT/combined.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/source.bspool" "$OUT/source.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/refined.bspool" "$OUT/refined.txt"
"./native/brainstorm_seed_pool$EXE" export "$OUT/combined.bspool" "$OUT/combined.txt"

SOURCE_RECORDS=$(wc -l < "$OUT/source.txt" | tr -d ' ')
REFINED_RECORDS=$(wc -l < "$OUT/refined.txt" | tr -d ' ')
[ "$SOURCE_RECORDS" -gt 0 ] || fail "schema-3 voucher source pool is empty"
[ "$REFINED_RECORDS" -gt 0 ] || fail "schema-3 voucher refilter is empty"

sort "$OUT/refined.txt" > "$OUT/refined.sorted.txt"
sort "$OUT/combined.txt" > "$OUT/combined.sorted.txt"
cmp "$OUT/refined.sorted.txt" "$OUT/combined.sorted.txt" ||
	fail "voucher refilter differs from the equivalent one-pass scan"
sed -n '1,200p' "$OUT/refined.txt" > "$OUT/lua.seeds"
"$LUAJIT_BIN" tests/pool_lua_oracle.lua Brainstorm_reroll.lua \
	"$OUT/snapshot.cfg" "$OUT/refined.bspool" "$OUT/lua.seeds" \
	> "$OUT/lua.out"
if grep -v ' 1$' "$OUT/lua.out" >/dev/null; then
	fail "production Lua rejected a committed voucher-route member"
fi

grep -a -q '^BRAINSTORM_SEED_POOL 3$' "$OUT/source.bspool" ||
	fail "source pool is not BSP3"
grep -a -q '^encoding delta-varint-events-v1$' "$OUT/source.bspool" ||
	fail "source pool does not use event metadata encoding"
grep -a -q '^voucher v_overstock_norm 1 4$' "$OUT/source.bspool" ||
	fail "source voucher rule is missing from its header"
grep -a -q '^voucher_exclude v_tarot_merchant$' "$OUT/source.bspool" ||
	fail "source Tarot Merchant exclusion is missing"
grep -a -q '^voucher_exclude v_planet_merchant$' "$OUT/source.bspool" ||
	fail "source Planet Merchant exclusion is missing"
grep -a -q '^complete 1$' "$OUT/source.bspool" ||
	fail "source pool did not finalize"

grep -a -q '^BRAINSTORM_SEED_POOL 3$' "$OUT/refined.bspool" ||
	fail "refiltered pool is not BSP3"
grep -a -q '^route_voucher v_overstock_norm 1 4$' "$OUT/refined.bspool" ||
	fail "source voucher rule did not survive refilter"
grep -a -q '^route_voucher_exclude v_tarot_merchant$' "$OUT/refined.bspool" ||
	fail "source Tarot Merchant exclusion did not survive refilter"
grep -a -q '^route_voucher_exclude v_planet_merchant$' "$OUT/refined.bspool" ||
	fail "source Planet Merchant exclusion did not survive refilter"
grep -a -q '^voucher v_hone 1 4$' "$OUT/refined.bspool" ||
	fail "new voucher rule is missing after refilter"
grep -a -q '^voucher_exclude v_overstock_norm$' "$OUT/refined.bspool" ||
	fail "new target-purchase exclusion is missing after refilter"
grep -a -q '^refilter_depth 1$' "$OUT/refined.bspool" ||
	fail "refilter ancestry is missing"
grep -a -q '^complete 1$' "$OUT/refined.bspool" ||
	fail "refiltered pool did not finalize"

# Require each committed record to retain every target occurrence in BSP3's
# inverted metadata table. Voucher descriptors use kind=3.
check_voucher_metadata() {
	python3 - "$@" <<'PY'
import re
import struct
import sys

path, *wanted = sys.argv[1:]
assert wanted, "no voucher metadata keys requested"
raw = open(path, "rb").read()
prefix = raw[:8192].split(b"\0", 1)[0].decode("ascii")

def field(name):
    match = re.search(r"^%s (\S+)$" % re.escape(name), prefix, re.M)
    assert match, "%s: missing %s" % (path, name)
    return match.group(1)

assert re.search(r"^BRAINSTORM_SEED_POOL 3$", prefix, re.M), path
header_bytes = int(field("header_bytes"))
data_bytes = int(field("data_bytes"))
records = int(field("records"))
assert records > 0

def varint(payload, pos):
    value = 0
    shift = 0
    while True:
        assert pos < len(payload), "%s: truncated metadata varint" % path
        byte = payload[pos]
        pos += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        assert shift < 70, "%s: oversized metadata varint" % path

pos = header_bytes
end = header_bytes + data_bytes
seen_records = 0
while pos < end:
    assert pos + 48 <= end
    fields = struct.unpack("<4sHHIIIIQQQ", raw[pos:pos + 48])
    (magic, block_header, flags, count, rank_bytes, metadata_bytes,
     associations, first, last, checksum) = fields
    assert magic == b"BSP3" and block_header == 48 and 0 < count <= 1024
    meta_start = pos + block_header + rank_bytes
    payload = raw[meta_start:meta_start + metadata_bytes]
    assert len(payload) == metadata_bytes
    matched = {key: [False] * count for key in wanted}
    cursor = 0
    descriptor_count, cursor = varint(payload, cursor)
    for _ in range(descriptor_count):
        length, cursor = varint(payload, cursor)
        descriptor = payload[cursor:cursor + length]
        cursor += length
        assert len(descriptor) == length and length >= 7
        kind = descriptor[0]
        key_len = descriptor[1]
        assert length == key_len + 7
        key = descriptor[2:2 + key_len].decode("ascii")
        member_count, cursor = varint(payload, cursor)
        assert member_count > 0
        record = -1
        for index in range(member_count):
            delta, cursor = varint(payload, cursor)
            record = delta if index == 0 else record + delta
            assert 0 <= record < count
            if kind == 3 and key in matched:
                matched[key][record] = True
    assert cursor == len(payload)
    for key in wanted:
        assert all(matched[key]), \
            "%s: voucher %s metadata is missing from a record" % (path, key)
    seen_records += count
    pos = meta_start + metadata_bytes

assert pos == end and seen_records == records
PY
}

check_voucher_metadata "$OUT/source.bspool" v_overstock_norm
check_voucher_metadata "$OUT/refined.bspool" v_overstock_norm v_hone

# Build the same minimal config used by the in-game native helper. Its pool
# loader must import both generations of voucher criteria and exclusions from
# the selected pool before evaluating candidates.
{
	printf '%s\n' \
		'session 1' 'threads 2' 'modelver 6' 'entropy 1' \
		'soul 0' 'legendary -' 'neglegendary 0' 'tag -' \
		'voucher -' 'voucherante 1' 'taganywhere 0' 'leganywhere 0' \
		'matchany 0' 'jslot 1 - 0' 'jslot 2 - 0' 'jslot 3 - 0' \
		'maslots 0 0 0 0 0 0 0 0' 'mapacks 0 0 0 0 0 0 0 0' \
		'packslots 2'
	printf 'poolfile %s\n' "$(native_path "$OUT/refined.bspool")"
	grep -E '^(tagdef|vouchdef|vouchroute|vouchowned|jokerdef|boostdef|specialdef|check_[a-z0-9]+) ' \
		"$OUT/snapshot.cfg"
	printf '%s\n' end
} > "$OUT/overlay.cfg"

cp "$OUT/refined.txt" "$OUT/overlay.seeds"
# A1111111 proves an excluded target remains a valid offer; 44111111 proves
# the inherited merchant exclusions can invalidate an otherwise valid route.
printf '%s\n' A1111111 44111111 >> "$OUT/overlay.seeds"
"./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" \
	"$OUT/combined-pool.cfg" "$OUT/overlay.seeds" > "$OUT/standalone.fixture"
"./native/brainstorm_native_search$EXE" fixture "$OUT/overlay.cfg" \
	"$OUT/overlay.seeds" > "$OUT/overlay.fixture"

awk '{ print $1, $2 }' "$OUT/standalone.fixture" > "$OUT/standalone.status"
awk '{ print $1, $2 }' "$OUT/overlay.fixture" > "$OUT/overlay.status"
cmp "$OUT/standalone.status" "$OUT/overlay.status" ||
	fail "in-game voucher pool revalidation differs from the standalone route"
if awk -v records="$REFINED_RECORDS" \
	'NR <= records && $2 != 1 { bad=1 } END { exit !bad }' "$OUT/overlay.fixture"; then
	fail "in-game loader rejected a committed voucher-pool member"
fi
grep -q '^A1111111 1 ' "$OUT/overlay.fixture" ||
	fail "in-game loader treated an excluded target as hidden"
grep -q '^44111111 0 ' "$OUT/overlay.fixture" ||
	fail "in-game loader ignored inherited merchant exclusions"

# vouchroute is part of the catalog fingerprint. A valid but changed route
# catalog must not be allowed to consume membership built by another profile.
grep -q '^vouchroute v_hone 1 -$' "$OUT/overlay.cfg" ||
	fail "test snapshot has an unexpected Hone route definition"
sed 's/^vouchroute v_hone 1 -$/vouchroute v_hone 0 -/' \
	"$OUT/overlay.cfg" > "$OUT/changed-route.cfg"
if "./native/brainstorm_native_search$EXE" fixture "$OUT/changed-route.cfg" \
		"$OUT/overlay.seeds" > "$OUT/changed-route.out" \
		2> "$OUT/changed-route.err"; then
	fail "pool loader accepted a changed vouchroute catalog hash"
fi
grep -q 'profile/unlock snapshot differs' "$OUT/changed-route.err" ||
	fail "changed vouchroute catalog failed for the wrong reason"

printf 'voucher route regression passed (10 fixtures, BSP3/refilter=%s/%s records, native revalidation)\n' \
	"$SOURCE_RECORDS" "$REFINED_RECORDS"
