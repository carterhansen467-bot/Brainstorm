# Derive the interactive search PGO workload from a native_search.cfg snapshot.
#
# The snapshot is also the catalog/parity handshake with the Lua mod. Preserve
# its ordered tag/joker/booster metadata and checks, strip every user-active
# setting, then force the standard Soul/Charm/Perkeo availability and fresh
# vanilla voucher route used by this fixed workload. Unknown directives fail
# closed so a future filter cannot silently leak into training.

BEGIN {
	vanilla_voucher_count = split("v_overstock_norm v_overstock_plus v_clearance_sale v_liquidation v_hone v_glow_up v_reroll_surplus v_reroll_glut v_crystal_ball v_omen_globe v_telescope v_observatory v_grabber v_nacho_tong v_wasteful v_recyclomancy v_tarot_merchant v_tarot_tycoon v_planet_merchant v_planet_tycoon v_seed_money v_money_tree v_blank v_antimatter v_magic_trick v_illusion v_hieroglyph v_petroglyph v_directors_cut v_retcon v_paint_brush v_palette", vanilla_voucher, " ")
}

/^[[:space:]]*#/ || NF == 0 { print; next }
saw_end { next }

$1 == "threads"      { print "threads 1"; next }
$1 == "soul"         { print "soul 0"; next }
$1 == "legendary"    { print "legendary j_perkeo"; next }
$1 == "neglegendary" { print "neglegendary 0"; next }
$1 == "tag"          { print "tag -"; next }
$1 == "voucher"      { print "voucher -"; next }
$1 == "voucherante"  { print "voucherante 1"; next }
$1 == "taganywhere"  { print "taganywhere 0"; next }
$1 == "leganywhere"  { print "leganywhere 1"; next }
$1 == "matchany"     { print "matchany 0"; next }
$1 == "jslot"        { print "jslot " $2 " - 0"; next }
$1 == "maslots"      { print "maslots 0 0 0 0 0 0 0 0"; next }
$1 == "mapacks"      { print "mapacks 0 0 0 0 0 0 0 0"; next }
$1 == "packslots"    { print "packslots 2"; next }

# `pack` and `poolfile` are active filters. Voucher definitions, unlock state,
# and ownership are replaced below with a deterministic fresh vanilla route.
$1 == "pack" || $1 == "poolfile" || $1 == "vouchdef" \
	|| $1 == "vouchroute" || $1 == "vouchowned" { next }

# Immutable snapshot/catalog directives used by config loading and parity.
$1 == "session" || $1 == "modelver" || $1 == "entropy" \
	|| $1 == "check_ph" || $1 == "check_r13" || $1 == "check_pr" \
	|| $1 == "check_prn" { print; next }

$1 == "tagdef" {
	if ($2 == "tag_charm") { $3 = 1; charm_metadata = 1 }
	print
	next
}

$1 == "boostdef" {
	$7 = 1
	if ($2 == "p_arcana_mega_1" && $6 == "A") charm_pack_metadata = 1
	if ($2 == "p_spectral_normal_1" && $6 == "S") ethereal_pack_metadata = 1
	print
	next
}

$1 == "jokerdef" {
	if ($2 == 4 && $3 == "j_perkeo") { $4 = 1; perkeo_metadata = 1 }
	print
	next
}

$1 == "specialdef" {
	print "specialdef 1 1"
	special_metadata = 1
	next
}

$1 == "end" {
	for (i = 1; i <= vanilla_voucher_count; i++) {
		available = i % 2
		print "vouchdef " vanilla_voucher[i] " " available
		requires = available ? "-" : vanilla_voucher[i - 1]
		print "vouchroute " vanilla_voucher[i] " 1 " requires
	}
	print "end"
	saw_end = 1
	next
}

{
	print "pgo_search_training.awk: refusing unknown directive `" $1 \
		"` on line " NR > "/dev/stderr"
	refused = 1
	exit 2
}

END {
	if (refused) exit 2
	failures = 0
	if (!saw_end) {
		print "PGO training snapshot is truncated (no end marker)" > "/dev/stderr"
		failures++
	}
	if (!special_metadata) {
		print "PGO training requires Soul/Black Hole special metadata" > "/dev/stderr"
		failures++
	}
	if (!perkeo_metadata) {
		print "PGO training requires rarity-4 j_perkeo catalog metadata" > "/dev/stderr"
		failures++
	}
	if (!charm_metadata) {
		print "PGO training requires tag_charm catalog metadata" > "/dev/stderr"
		failures++
	}
	if (!charm_pack_metadata || !ethereal_pack_metadata) {
		print "PGO training requires Charm/Ethereal reward-pack metadata" > "/dev/stderr"
		failures++
	}
	if (failures) exit 2
}
