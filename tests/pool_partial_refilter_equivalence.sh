#!/bin/sh
# Paused pools are valid snapshots: refilter only their committed blocks,
# preserve provisional ancestry after the refilter itself completes, and never
# report record exhaustion as proof about the unfinished source range.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
COUNT="${2:-2000000}"
OUT="${TMPDIR:-/tmp}/brainstorm_pool_partial_refilter"
LUAJIT_BIN="${LUAJIT:-luajit}"
rm -rf "$OUT"
mkdir -p "$OUT"

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh
		native_path() { cygpath -m "$1"; } ;;
	*) EXE=; BUILD=native/build.sh
		native_path() { printf '%s\n' "$1"; } ;;
esac

sh "$BUILD"
"$LUAJIT_BIN" tests/align_snapshot_prng.lua "$SNAPSHOT" > "$OUT/snapshot.cfg"

write_criteria() {
	file="$1"
	shift
	{
		echo "poolver 1"; echo "threads 4"; echo "start 0"; echo "count $COUNT"
		echo "checkpoint $COUNT"; echo "chunk 16384"; echo "resume 0"
		echo "format binary"; echo "tag_route collect"
		for line in "$@"; do echo "$line"; done
		echo "end"
	} > "$file"
}

write_criteria "$OUT/broad.cfg" "tag tag_charm 1 8 1"
write_criteria "$OUT/narrow.cfg" "legendary j_perkeo 1 8 0"
write_criteria "$OUT/combined.cfg" "tag tag_charm 1 8 1" "legendary j_perkeo 1 8 0"
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
	"$OUT/broad.cfg" "$OUT/complete-source.bspool"

# Keep exactly the first two committed BSP2 blocks and discard the completed
# index/footer. This has the same on-disk shape as a cleanly checkpointed pause,
# but is deterministic and quick enough for CI.
python3 - "$OUT/complete-source.bspool" "$OUT/partial-source.bspool" <<'PY'
import re, struct, sys
src, dst = sys.argv[1:]
raw = open(src, "rb").read()
header = raw[:1024]
text = header.split(b"\0", 1)[0].decode("ascii")
pos = 1024
records = 0
for _ in range(2):
    magic, count, payload, checksum, first, last = struct.unpack(
        "<4sIIIQQ", raw[pos:pos + 32])
    assert magic == b"BSP2" and count > 0
    pos += 32 + payload
    records += count
data_bytes = pos - 1024
replacements = {
    r"^records \d+$": "records %d" % records,
    r"^data_bytes \d+$": "data_bytes %d" % data_bytes,
    r"^complete [01]$": "complete 0",
    r"^coverage_complete [01]$": "coverage_complete 0",
}
for pattern, value in replacements.items():
    text, n = re.subn(pattern, value, text, count=1, flags=re.M)
    assert n == 1, pattern
encoded = text.encode("ascii")
assert len(encoded) <= 1024
with open(dst, "wb") as f:
    f.write(encoded.ljust(1024, b"\0"))
    f.write(raw[1024:pos])
PY

"./native/brainstorm_seed_pool$EXE" export "$OUT/partial-source.bspool" "$OUT/partial.txt"
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/narrow.cfg" "$OUT/partial-source.bspool" "$OUT/refined.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/refined.bspool" "$OUT/refined.txt"

"./native/brainstorm_seed_pool$EXE" fixture "$OUT/snapshot.cfg" \
	"$OUT/combined.cfg" "$OUT/partial.txt" \
	| awk '$2 == 1 { print $1 }' | sort > "$OUT/expected.txt"
sort "$OUT/refined.txt" > "$OUT/actual.txt"
cmp "$OUT/expected.txt" "$OUT/actual.txt"
[ -s "$OUT/actual.txt" ] || { echo "FAIL: deterministic partial sample had no refined hits"; exit 1; }

head -c 1024 "$OUT/refined.bspool" | tr -d '\0' > "$OUT/refined.header"
grep -q '^complete 1$' "$OUT/refined.header"
grep -q '^coverage_complete 0$' "$OUT/refined.header"
grep -q '^source_complete 0$' "$OUT/refined.header"
grep -q '^source_coverage_complete 0$' "$OUT/refined.header"

# The standalone UI must offer both the paused source and the completed
# provisional derivative as inputs, while labeling their coverage honestly.
python3 - "$OUT" <<'PY'
import os, sys
sys.path.insert(0, "tools")
import brainstorm_pool_builder as core
import pool_builder_web as web
core.POOL_DIR = sys.argv[1]
pools = {p["name"]: p for p in web.list_pools()}
assert pools["partial-source.bspool"]["records"] > 0
assert not pools["partial-source.bspool"]["complete"]
assert not pools["partial-source.bspool"]["coverage_complete"]
assert pools["refined.bspool"]["complete"]
assert not pools["refined.bspool"]["coverage_complete"]
PY

# Exhausting a provisional pool is a snapshot result (exit 4), not the
# definitive complete-pool verdict (exit 3).
{
	echo "session 1"; echo "threads 4"; echo "modelver 6"; echo "entropy 1"
	echo "soul 0"; echo "legendary j_triboulet"; echo "neglegendary 0"; echo "tag -"
	echo "voucher -"; echo "voucherante 1"; echo "taganywhere 0"; echo "leganywhere 1"
	echo "matchany 0"; echo "jslot 1 - 0"; echo "jslot 2 - 0"; echo "jslot 3 - 0"
	echo "maslots 0 0 0 0 0 0 0 0"; echo "mapacks 0 0 0 0 0 0 0 0"; echo "packslots 2"
	echo "poolfile $(native_path "$OUT/refined.bspool")"
	grep -E '^(tagdef|vouchdef|jokerdef|boostdef|specialdef|check_[a-z0-9]+) ' "$OUT/snapshot.cfg"
	echo "end"
} > "$OUT/search.cfg"
: > "$OUT/hb"
set +e
"./native/brainstorm_native_search$EXE" search "$OUT/search.cfg" "$OUT/status" \
	"$OUT/stop" "$OUT/hb"
rc=$?
set -e
[ "$rc" -eq 4 ] || { echo "FAIL: provisional exhaustion returned $rc, want 4"; cat "$OUT/status"; exit 1; }
grep -q '^E pool: no matching seed among currently recorded seeds' "$OUT/status"

RECORDS=$(wc -l < "$OUT/partial.txt" | tr -d ' ')
MATCHES=$(wc -l < "$OUT/refined.txt" | tr -d ' ')
echo "PASS: refiltered $RECORDS committed partial records into $MATCHES provisional matches"
