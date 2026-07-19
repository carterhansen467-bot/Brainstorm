#!/bin/sh
# Compression compatibility proof: create a current schema-3 pool, synthesize
# the equivalent legacy schema-1 u64le representation, convert that to schema
# 2, then verify every generation represents the same seeds and corruption is
# rejected instead of silently producing a different seed set.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
COUNT="${2:-2000000}"
TAG="${TAG:-tag_charm}"
EXE=""
case "$(uname -s 2>/dev/null || echo Windows)" in
  MINGW*|MSYS*|CYGWIN*) EXE=".exe" ;;
esac
OUT="${TMPDIR:-/tmp}/brainstorm_pool_compression_equivalence"
rm -rf "$OUT"
mkdir -p "$OUT"
cp "$SNAPSHOT" "$OUT/snapshot.cfg"

cat > "$OUT/criteria.cfg" <<EOF
poolver 1
threads 4
start 0
count $COUNT
checkpoint $COUNT
chunk 16384
resume 0
format binary
tag_route collect
tag $TAG 1 8 1
legendary j_perkeo 1 8
end
EOF

"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
  "$OUT/criteria.cfg" "$OUT/native-v3.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/native-v3.bspool" "$OUT/native-v3.txt"

python3 - "$OUT/native-v3.bspool" <<'PY'
import re, sys
with open(sys.argv[1], "rb") as f:
    text = f.read(1024).split(b"\0", 1)[0].decode("ascii")
assert re.search(r"^BRAINSTORM_SEED_POOL 3$", text, re.M)
assert re.search(r"^encoding delta-varint-events-v1$", text, re.M)
assert re.search(r"^header_bytes 8192$", text, re.M)
PY

# A checkpoint is a trust boundary: inconsistent counters must be rejected,
# never used to truncate/append the pool at a fabricated committed position.
sed 's/^resume 0$/resume 1/' "$OUT/criteria.cfg" > "$OUT/resume.cfg"
cp "$OUT/native-v3.bspool" "$OUT/bad-state.bspool"
cp "$OUT/native-v3.bspool.state" "$OUT/bad-state.bspool.state"
python3 - "$OUT/bad-state.bspool.state" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding="ascii").read()
m = re.search(r"^scanned (\d+)$", s, re.M)
assert m and int(m.group(1)) > 0
s = s[:m.start(1)] + str(int(m.group(1)) - 1) + s[m.end(1):]
open(p, "w", encoding="ascii").write(s)
PY
if "./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
    "$OUT/resume.cfg" "$OUT/bad-state.bspool" 2>/dev/null; then
  echo "FAIL: inconsistent resume state was accepted"; exit 1
fi

# A Windows sharing violation can happen after the new header/data checkpoint
# is durable but before its atomic .state replacement.  Recreate that exact
# ordering: keep a valid incomplete header at the final scan cursor while the
# state sidecar is one cursor behind.  Resume must validate the header payload,
# repair the sidecar, and finalize without rescanning or duplicating records.
cp "$OUT/native-v3.bspool" "$OUT/ahead.bspool"
cp "$OUT/native-v3.bspool.state" "$OUT/ahead.bspool.state"
python3 - "$OUT/ahead.bspool" "$OUT/ahead.bspool.state" <<'PY'
import re, sys
pool, state = sys.argv[1:]
with open(pool, "r+b") as f:
    prefix = f.read(8192)
    text = prefix.split(b"\0", 1)[0].decode("ascii")
    header_bytes = int(re.search(r"^header_bytes (\d+)$", text, re.M).group(1))
    data_bytes = int(re.search(r"^data_bytes (\d+)$", text, re.M).group(1))
    cursor = int(re.search(r"^scan_cursor (\d+)$", text, re.M).group(1))
    assert cursor > 1 and re.search(r"^complete 1$", text, re.M)
    raw = prefix[:header_bytes]
    raw, changed = re.subn(br"(?m)^complete 1$", b"complete 0", raw)
    assert changed == 1
    raw, changed = re.subn(br"(?m)^coverage_complete 1$",
                           b"coverage_complete 0", raw)
    assert changed == 1
    f.seek(0)
    f.write(raw)
    f.truncate(header_bytes + data_bytes)

with open(state, encoding="ascii") as f:
    saved = f.read()
old_cursor = cursor // 2
saved, n1 = re.subn(r"(?m)^cursor \d+$", "cursor %d" % old_cursor, saved)
saved, n2 = re.subn(r"(?m)^scanned \d+$", "scanned %d" % old_cursor, saved)
saved, n3 = re.subn(r"(?m)^output_bytes \d+$",
                    "output_bytes %d" % (header_bytes + data_bytes), saved)
saved, n4 = re.subn(r"(?m)^done 1$", "done 0", saved)
assert (n1, n2, n3, n4) == (1, 1, 1, 1)
with open(state, "w", encoding="ascii", newline="\n") as f:
    f.write(saved)
PY
"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
  "$OUT/resume.cfg" "$OUT/ahead.bspool" 2>"$OUT/ahead.log"
grep -q '^recovered committed checkpoint at rank ' "$OUT/ahead.log"
"./native/brainstorm_seed_pool$EXE" export "$OUT/ahead.bspool" "$OUT/ahead.txt"
cmp "$OUT/native-v3.txt" "$OUT/ahead.txt"

# Re-encode the exact exported set as the former schema-1 u64le format.
python3 - "$OUT/native-v3.bspool" "$OUT/native-v3.txt" "$OUT/legacy-v1.bspool" <<'PY'
import re, struct, sys
source, seeds, output = sys.argv[1:]
with open(source, "rb") as f:
    prefix = f.read(1024)
    prefix_text = prefix.split(b"\0", 1)[0].decode("ascii")
    header_bytes = int(re.search(r"^header_bytes (\d+)$", prefix_text, re.M).group(1))
    f.seek(0)
    raw = f.read(header_bytes)
lines = raw.split(b"\0", 1)[0].decode("ascii").splitlines()
outlines = []
legacy_keys = {
    "modelver", "charset", "seedspace", "space", "range_start", "range_end",
    "catalog_hash", "criteria_hash", "pool_id", "tag_route", "label",
    "route_tag", "route_legendary", "tag", "legendary", "soul_depth",
    "refilter_depth", "merged_parts", "records", "complete",
    "coverage_complete", "end",
}
for line in lines:
    key = line.split(" ", 1)[0]
    if key == "BRAINSTORM_SEED_POOL":
        outlines.append("BRAINSTORM_SEED_POOL 1")
    elif key == "encoding":
        outlines.append("encoding u64le")
    elif key == "data_bytes":
        continue
    elif key == "header_bytes":
        outlines.append("header_bytes 1024")
    elif key in legacy_keys:
        outlines.append(line)
header = ("\n".join(outlines) + "\n").encode("ascii")
assert len(header) <= 1024
charset = "123456789ABCDEFGHIJKLMNPQRSTUVWXYZ"
with open(output, "wb") as dst:
    dst.write(header.ljust(1024, b"\0"))
    with open(seeds, "r", encoding="ascii") as src:
        for seed in src:
            seed = seed.strip()
            rank = sum(charset.index(ch) * (len(charset) ** i)
                       for i, ch in enumerate(seed))
            dst.write(struct.pack("<Q", rank))
PY

"./native/brainstorm_seed_pool$EXE" export "$OUT/legacy-v1.bspool" "$OUT/legacy-v1.txt"
"./native/brainstorm_seed_pool$EXE" convert "$OUT/legacy-v1.bspool" "$OUT/converted-v2.bspool"
"./native/brainstorm_seed_pool$EXE" export "$OUT/converted-v2.bspool" "$OUT/converted-v2.txt"
sort "$OUT/native-v3.txt" > "$OUT/native.sorted"
sort "$OUT/legacy-v1.txt" > "$OUT/legacy.sorted"
sort "$OUT/converted-v2.txt" > "$OUT/converted.sorted"
cmp "$OUT/native.sorted" "$OUT/legacy.sorted"
cmp "$OUT/native.sorted" "$OUT/converted.sorted"

# Schema 3 protects the full rank+metadata payload with CRC64. Flip the first
# rank byte without repairing the checksum and require a hard decode failure.
cp "$OUT/native-v3.bspool" "$OUT/corrupt-v3.bspool"
python3 - "$OUT/corrupt-v3.bspool" <<'PY'
import re, struct, sys
p = sys.argv[1]
with open(p, "r+b") as f:
    prefix = f.read(1024)
    text = prefix.split(b"\0", 1)[0].decode("ascii")
    header_bytes = int(re.search(r"^header_bytes (\d+)$", text, re.M).group(1))
    f.seek(header_bytes)
    raw = f.read(48)
    fields = struct.unpack("<4sHHIIIIQQQ", raw)
    assert fields[0] == b"BSP3" and fields[1] == 48 and fields[4] > 0
    f.seek(header_bytes + 48)
    b = f.read(1)
    assert b
    f.seek(-1, 1)
    f.write(bytes([b[0] ^ 1]))
PY
if "./native/brainstorm_seed_pool$EXE" export "$OUT/corrupt-v3.bspool" \
    "$OUT/corrupt-v3.txt" 2>/dev/null; then
  echo "FAIL: schema-3 payload corruption was accepted"; exit 1
fi

grep -q '^BRAINSTORM_SEED_POOL 2$' "$OUT/converted-v2.bspool"
grep -q '^encoding delta-varint-blocks-v1$' "$OUT/converted-v2.bspool"
LEGACY_ID=$(head -c 1024 "$OUT/legacy-v1.bspool" | tr -d '\0' | sed -n 's/^pool_id //p')
CONVERTED_ID=$(head -c 1024 "$OUT/converted-v2.bspool" | tr -d '\0' | sed -n 's/^pool_id //p')
[ -n "$LEGACY_ID" ] && [ "$LEGACY_ID" = "$CONVERTED_ID" ] || {
  echo "FAIL: conversion changed pool identity"; exit 1;
}
LEGACY_BYTES=$(wc -c < "$OUT/legacy-v1.bspool" | tr -d ' ')
COMPRESSED_BYTES=$(wc -c < "$OUT/converted-v2.bspool" | tr -d ' ')
[ "$COMPRESSED_BYTES" -lt "$LEGACY_BYTES" ] || {
  echo "FAIL: compressed pool is not smaller ($COMPRESSED_BYTES >= $LEGACY_BYTES)"; exit 1;
}

cp "$OUT/converted-v2.bspool" "$OUT/corrupt.bspool"
python3 - "$OUT/corrupt.bspool" <<'PY'
import sys
p = sys.argv[1]
with open(p, "r+b") as f:
    f.seek(1024 + 32)
    b = f.read(1)
    assert b
    f.seek(-1, 1)
    f.write(bytes([b[0] ^ 1]))
PY
if "./native/brainstorm_seed_pool$EXE" export "$OUT/corrupt.bspool" "$OUT/corrupt.txt" 2>/dev/null; then
  echo "FAIL: payload corruption was accepted"; exit 1
fi

# Even a checksummed block is invalid when one of its ranks lies outside the
# shard's declared half-open range. Move range_start just past block 1's first
# rank without touching the payload/checksum and require a decode failure.
cp "$OUT/converted-v2.bspool" "$OUT/out-of-range.bspool"
python3 - "$OUT/out-of-range.bspool" <<'PY'
import re, struct, sys
p = sys.argv[1]
with open(p, "r+b") as f:
    header = f.read(1024)
    text = header.split(b"\0", 1)[0].decode("ascii")
    f.seek(1024 + 16)
    first = struct.unpack("<Q", f.read(8))[0]
    text2, n = re.subn(r"^range_start \d+$", "range_start %d" % (first + 1),
                       text, count=1, flags=re.M)
    assert n == 1 and len(text2.encode("ascii")) <= 1024
    f.seek(0)
    f.write(text2.encode("ascii").ljust(1024, b"\0"))
PY
if "./native/brainstorm_seed_pool$EXE" export "$OUT/out-of-range.bspool" \
    "$OUT/out-of-range.txt" 2>/dev/null; then
  echo "FAIL: rank outside the declared shard range was accepted"; exit 1
fi

echo "PASS: schema-1 compatibility + conversion + checksummed compression ($LEGACY_BYTES -> $COMPRESSED_BYTES bytes)"
