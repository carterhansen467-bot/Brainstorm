#!/bin/sh
# A resumed refilter is bound to the exact committed source snapshot it began
# with.  Appending later committed blocks is safe; changing the pinned prefix
# is not.  The fabricated checkpoint keeps this deterministic and fast enough
# for routine regression runs.
set -eu

SNAPSHOT="${1:-native_search.cfg}"
COUNT="${2:-300000}"
OUT="${TMPDIR:-/tmp}/brainstorm_pool_lineage_incremental"
LUAJIT_BIN="${LUAJIT:-luajit}"
rm -rf "$OUT"
mkdir -p "$OUT"

case "$(uname -s)" in
	MINGW*|MSYS*|CYGWIN*) EXE=.exe; BUILD=native/build_windows.sh ;;
	*) EXE=; BUILD=native/build.sh ;;
esac

sh "$BUILD"
"$LUAJIT_BIN" tests/align_snapshot_prng.lua "$SNAPSHOT" > "$OUT/snapshot.cfg"

{
	echo "poolver 1"
	echo "threads 1"
	echo "start 0"
	echo "count $COUNT"
	echo "checkpoint $COUNT"
	echo "chunk 16384"
	echo "resume 0"
	echo "format binary"
	echo "tag_route collect"
	echo "tag tag_charm 1 8 1"
	echo "end"
} > "$OUT/broad.cfg"

{
	echo "poolver 1"
	echo "threads 1"
	echo "start 0"
	echo "count all"
	echo "checkpoint 8192"
	echo "chunk 8192"
	echo "resume 1"
	echo "format binary"
	echo "tag_route collect"
	echo "legendary j_perkeo 1 8 0"
	echo "end"
} > "$OUT/narrow.cfg"

"./native/brainstorm_seed_pool$EXE" scan "$OUT/snapshot.cfg" \
	"$OUT/broad.cfg" "$OUT/broad-complete.bspool"

# The first 32 committed BSP3 blocks are the initial incomplete source.  A
# second view containing 48 blocks is its append-stable continuation.  Both
# headers get snapshot identities and metadata digests for precisely the bytes
# they expose.
python3 - "$OUT/broad-complete.bspool" "$OUT/source-original.bspool" \
	"$OUT/source-extended.bspool" <<'PY'
import re
import struct
import sys

src, original, extended = sys.argv[1:]
raw = open(src, "rb").read()
prefix = raw[:1024].split(b"\0", 1)[0].decode("ascii")
schema = int(re.search(r"^BRAINSTORM_SEED_POOL (\d+)$", prefix, re.M).group(1))
header_bytes = int(re.search(r"^header_bytes (\d+)$", prefix, re.M).group(1))
assert schema == 3, "lineage regression requires current BSP3 output"
text = raw[:header_bytes].split(b"\0", 1)[0].decode("ascii")
segment = int(re.search(r"^segment_id ([0-9a-f]+)$", text, re.M).group(1), 16)

FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211
MASK = (1 << 64) - 1

def fnv(data, value=FNV_OFFSET):
    for byte in data:
        value = ((value ^ byte) * FNV_PRIME) & MASK
    return value

def snapshot_id(records, data_bytes, digest):
    identity = ("snapshot:%016x:%016x:%016x:%016x" %
                (segment, records, data_bytes, digest)).encode("ascii")
    return fnv(identity)

def rewrite(pattern, replacement, value):
    value, count = re.subn(pattern, replacement, value, count=1, flags=re.M)
    assert count == 1, pattern
    return value

def emit(path, wanted_blocks):
    pos = header_bytes
    records = 0
    last = None
    metadata_digest = FNV_OFFSET
    for index in range(wanted_blocks):
        assert raw[pos:pos + 4] == b"BSP3", (
            "COUNT is too small: need at least 48 full source blocks")
        fields = struct.unpack("<4sHHIIIIQQQ", raw[pos:pos + 48])
        magic, block_header, flags, count, rank_bytes, metadata_bytes, associations, first, last, checksum = fields
        assert magic == b"BSP3" and block_header == 48 and count == 1024
        assert rank_bytes > 0 and metadata_bytes > 0 and associations > 0
        metadata_start = pos + block_header + rank_bytes
        metadata_digest = fnv(raw[metadata_start:metadata_start + metadata_bytes],
                              metadata_digest)
        pos += block_header + rank_bytes + metadata_bytes
        records += count
    data = raw[header_bytes:pos]
    digest = fnv(data)
    snapshot = snapshot_id(records, len(data), digest)
    header = text
    for pattern, replacement in (
        (r"^records \d+$", "records %d" % records),
        (r"^data_bytes \d+$", "data_bytes %d" % len(data)),
        (r"^complete [01]$", "complete 0"),
        (r"^coverage_complete [01]$", "coverage_complete 0"),
        (r"^membership_digest [0-9a-f]+$", "membership_digest %016x" % digest),
        (r"^metadata_digest [0-9a-f]+$", "metadata_digest %016x" % metadata_digest),
        (r"^snapshot_id [0-9a-f]+$", "snapshot_id %016x" % snapshot),
        (r"^scan_cursor \d+$", "scan_cursor %d" % (last + 1)),
    ):
        header = rewrite(pattern, replacement, header)
    encoded = header.encode("ascii")
    assert len(encoded) <= header_bytes
    with open(path, "wb") as output:
        output.write(encoded.ljust(header_bytes, b"\0"))
        output.write(data)

emit(original, 32)
emit(extended, 48)
PY

# This is the control result against the exact 32-block source snapshot.
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/narrow.cfg" "$OUT/source-original.bspool" "$OUT/clean.bspool"

# Retain the first two single-threaded 8192-input epochs from the completed
# child.  The two output blocks therefore describe cursor 16384 exactly.  Its
# state and header are rewritten to the same durable boundary the scanner
# itself would have produced after that checkpoint.
python3 - "$OUT/clean.bspool" "$OUT/source-original.bspool" \
	"$OUT/checkpoint.bspool" "$OUT/checkpoint.bspool.state" <<'PY'
import re
import struct
import sys

clean, source, output, state_path = sys.argv[1:]
raw = open(clean, "rb").read()

def header(path):
    data = open(path, "rb").read()
    prefix = data[:1024].split(b"\0", 1)[0].decode("ascii")
    size = int(re.search(r"^header_bytes (\d+)$", prefix, re.M).group(1))
    return data, size, data[:size].split(b"\0", 1)[0].decode("ascii")

_, _, source_text = header(source)
header_bytes = int(re.search(r"^header_bytes (\d+)$",
                             raw[:1024].split(b"\0", 1)[0].decode("ascii"), re.M).group(1))
text = raw[:header_bytes].split(b"\0", 1)[0].decode("ascii")
source_records = int(re.search(r"^records (\d+)$", source_text, re.M).group(1))
assert source_records == 32768

FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211
MASK = (1 << 64) - 1

pos = header_bytes
records = 0
metadata_digest = FNV_OFFSET
for _ in range(2):
    fields = struct.unpack("<4sHHIIIIQQQ", raw[pos:pos + 48])
    magic, block_header, flags, count, rank_bytes, metadata_bytes, associations, first, last, checksum = fields
    assert magic == b"BSP3" and block_header == 48 and 0 < count < 1024
    assert metadata_bytes > 0 and associations > 0
    metadata_start = pos + block_header + rank_bytes
    for byte in raw[metadata_start:metadata_start + metadata_bytes]:
        metadata_digest = ((metadata_digest ^ byte) * FNV_PRIME) & MASK
    pos += block_header + rank_bytes + metadata_bytes
    records += count
data = raw[header_bytes:pos]

def fnv(value):
    digest = FNV_OFFSET
    for byte in value:
        digest = ((digest ^ byte) * FNV_PRIME) & MASK
    return digest

membership = fnv(data)
segment = int(re.search(r"^segment_id ([0-9a-f]+)$", text, re.M).group(1), 16)
identity = ("snapshot:%016x:%016x:%016x:%016x" %
            (segment, records, len(data), membership)).encode("ascii")
snapshot = fnv(identity)
cursor = 16384

replacements = (
    (r"^records \d+$", "records %d" % records),
    (r"^data_bytes \d+$", "data_bytes %d" % len(data)),
    (r"^complete [01]$", "complete 0"),
    (r"^coverage_complete [01]$", "coverage_complete 0"),
    (r"^membership_digest [0-9a-f]+$", "membership_digest %016x" % membership),
    (r"^metadata_digest [0-9a-f]+$", "metadata_digest %016x" % metadata_digest),
    (r"^snapshot_id [0-9a-f]+$", "snapshot_id %016x" % snapshot),
    (r"^scan_cursor \d+$", "scan_cursor %d" % cursor),
    (r"^input_cursor \d+$", "input_cursor %d" % cursor),
)
for pattern, replacement in replacements:
    text, count = re.subn(pattern, replacement, text, count=1, flags=re.M)
    assert count == 1, pattern
encoded = text.encode("ascii")
assert len(encoded) <= header_bytes
with open(output, "wb") as file:
    file.write(encoded.ljust(header_bytes, b"\0"))
    file.write(data)

catalog = re.search(r"^catalog_hash ([0-9a-f]+)$", text, re.M).group(1)
criteria = re.search(r"^criteria_hash ([0-9a-f]+)$", text, re.M).group(1)
with open(state_path, "w", newline="\n") as state:
    state.write("BRAINSTORM_SEED_POOL_STATE 3\n")
    state.write("catalog_hash %s\n" % catalog)
    state.write("criteria_hash %s\n" % criteria)
    state.write("range_start 0\nrange_end %d\n" % source_records)
    state.write("cursor %d\n" % cursor)
    state.write("output_bytes %d\n" % (header_bytes + len(data)))
    state.write("membership_digest %016x\n" % membership)
    state.write("metadata_digest %016x\n" % metadata_digest)
    state.write("matched %d\nscanned %d\n" % (records, cursor))
    state.write("elapsed_seconds 0.000000000\ndone 0\nend\n")
PY

cp "$OUT/checkpoint.bspool" "$OUT/resumed.bspool"
cp "$OUT/checkpoint.bspool.state" "$OUT/resumed.bspool.state"
cp "$OUT/checkpoint.bspool" "$OUT/rejected.bspool"
cp "$OUT/checkpoint.bspool.state" "$OUT/rejected.bspool.state"

# Replacing the source with an append-stable longer view must not broaden the
# in-progress child.  It resumes only through the 32-block pinned boundary.
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/narrow.cfg" "$OUT/source-extended.bspool" "$OUT/resumed.bspool" \
	2> "$OUT/resume.log"
grep -q '^resuming at input record 16384 ' "$OUT/resume.log"
grep -q '^complete: scanned=32768 ' "$OUT/resume.log"
cmp "$OUT/clean.bspool" "$OUT/resumed.bspool"

# Make a different but structurally valid first source block: adjust one
# interior rank, re-encode its canonical deltas, and repair its CRC64.  Repair
# the live source header too, so rejection comes from the child's pinned
# snapshot identity rather than generic file-corruption detection.
python3 - "$OUT/source-extended.bspool" "$OUT/source-replaced.bspool" <<'PY'
import re
import struct
import sys

src, dst = sys.argv[1:]
raw = bytearray(open(src, "rb").read())
prefix = bytes(raw[:1024]).split(b"\0", 1)[0].decode("ascii")
header_bytes = int(re.search(r"^header_bytes (\d+)$", prefix, re.M).group(1))
text = bytes(raw[:header_bytes]).split(b"\0", 1)[0].decode("ascii")
offset = header_bytes
fields = struct.unpack("<4sHHIIIIQQQ", raw[offset:offset + 48])
magic, block_header, flags, count, rank_bytes, metadata_bytes, associations, first, last, checksum = fields
assert magic == b"BSP3" and block_header == 48 and count == 1024
rank_payload = bytes(raw[offset + 48:offset + 48 + rank_bytes])

def decode_uleb(data, at):
    value = 0
    shift = 0
    while True:
        byte = data[at]
        at += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, at
        shift += 7

ranks = [first]
at = 0
for _ in range(1, count):
    delta, at = decode_uleb(rank_payload, at)
    ranks.append(ranks[-1] + delta)
assert at == rank_bytes and ranks[-1] == last
changed = None
for index in range(1, len(ranks) - 1):
    if ranks[index] + 1 < ranks[index + 1]:
        ranks[index] += 1
        changed = index
        break
assert changed is not None

def encode_uleb(value):
    result = bytearray()
    while True:
        byte = value & 0x7f
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return result

encoded = bytearray()
for index in range(1, len(ranks)):
    encoded.extend(encode_uleb(ranks[index] - ranks[index - 1]))
assert len(encoded) == rank_bytes
raw[offset + 48:offset + 48 + rank_bytes] = encoded

POLY = 0x42f0e1eba9ea3693
MASK = (1 << 64) - 1

def crc64(data, crc=0):
    for byte in data:
        crc ^= byte << 56
        for _ in range(8):
            crc = (((crc << 1) ^ POLY) if crc & (1 << 63) else (crc << 1)) & MASK
    return crc

payload = bytes(raw[offset + 48:offset + 48 + rank_bytes + metadata_bytes])
crc = crc64(bytes(raw[offset + 4:offset + 40]))
crc = crc64(payload, crc)
raw[offset + 40:offset + 48] = struct.pack("<Q", crc)

FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211

def fnv(data):
    value = FNV_OFFSET
    for byte in data:
        value = ((value ^ byte) * FNV_PRIME) & MASK
    return value

data_bytes = int(re.search(r"^data_bytes (\d+)$", text, re.M).group(1))
records = int(re.search(r"^records (\d+)$", text, re.M).group(1))
segment = int(re.search(r"^segment_id ([0-9a-f]+)$", text, re.M).group(1), 16)
membership = fnv(raw[header_bytes:header_bytes + data_bytes])
identity = ("snapshot:%016x:%016x:%016x:%016x" %
            (segment, records, data_bytes, membership)).encode("ascii")
snapshot = fnv(identity)
text, count1 = re.subn(r"^membership_digest [0-9a-f]+$",
                       "membership_digest %016x" % membership, text, count=1, flags=re.M)
text, count2 = re.subn(r"^snapshot_id [0-9a-f]+$",
                       "snapshot_id %016x" % snapshot, text, count=1, flags=re.M)
assert count1 == count2 == 1
encoded_header = text.encode("ascii")
assert len(encoded_header) <= header_bytes
raw[:header_bytes] = encoded_header.ljust(header_bytes, b"\0")
open(dst, "wb").write(raw)
PY

set +e
"./native/brainstorm_seed_pool$EXE" refilter "$OUT/snapshot.cfg" \
	"$OUT/narrow.cfg" "$OUT/source-replaced.bspool" "$OUT/rejected.bspool" \
	> "$OUT/rejected.log" 2>&1
rc=$?
set -e
[ "$rc" -ne 0 ] || { echo "FAIL: changed pinned source prefix was accepted"; exit 1; }
grep -q 'committed source prefix digest differs from the pinned snapshot' "$OUT/rejected.log"

# Verify both the direct parent references and every deterministic identity
# edge.  This also proves the resumed child retained the old source snapshot,
# not the newly appended live snapshot.
python3 - "$OUT/source-original.bspool" "$OUT/source-extended.bspool" \
	"$OUT/clean.bspool" "$OUT/resumed.bspool" \
	"$OUT/resumed.bspool.state" <<'PY'
import re
import struct
import sys

FNV_OFFSET = 1469598103934665603
FNV_PRIME = 1099511628211
MASK = (1 << 64) - 1

def fnv(data, value=FNV_OFFSET):
    for byte in data:
        value = ((value ^ byte) * FNV_PRIME) & MASK
    return value

def hash_fields(kind, *fields):
    assert len(fields) == 4
    value = "%s:%016x:%016x:%016x:%016x" % ((kind,) + tuple(fields))
    return fnv(value.encode("ascii"))

def read(path):
    raw = open(path, "rb").read()
    prefix = raw[:1024].split(b"\0", 1)[0].decode("ascii")
    header_bytes = int(re.search(r"^header_bytes (\d+)$", prefix, re.M).group(1))
    text = raw[:header_bytes].split(b"\0", 1)[0].decode("ascii")
    values = {}
    for key in ("family_id", "segment_id", "stage_hash", "lineage_id",
                "derivation_id", "snapshot_id", "membership_digest", "metadata_digest",
                "parent_snapshot_id", "parent_segment_id"):
        match = re.search(r"^%s ([0-9a-f]+)$" % key, text, re.M)
        if match:
            values[key] = int(match.group(1), 16)
    for key in ("records", "data_bytes", "range_start", "range_end",
                "source_records", "parent_records", "parent_data_bytes",
                "input_cursor"):
        match = re.search(r"^%s (\d+)$" % key, text, re.M)
        if match:
            values[key] = int(match.group(1))
    values["raw"] = raw
    values["header_bytes"] = header_bytes
    return values

def data_digests(pool):
    raw = pool["raw"]
    pos = pool["header_bytes"]
    end = pos + pool["data_bytes"]
    metadata = FNV_OFFSET
    records = 0
    associations = 0
    while pos < end:
        fields = struct.unpack("<4sHHIIIIQQQ", raw[pos:pos + 48])
        magic, block_header, flags, count, rank_bytes, metadata_bytes, block_associations, first, last, checksum = fields
        assert magic == b"BSP3" and block_header == 48 and 0 < count <= 1024
        assert rank_bytes > 0 and metadata_bytes > 0
        metadata_start = pos + block_header + rank_bytes
        metadata = fnv(raw[metadata_start:metadata_start + metadata_bytes], metadata)
        records += count
        associations += block_associations
        pos += block_header + rank_bytes + metadata_bytes
    assert pos == end and records == pool["records"] and associations > 0
    membership = fnv(raw[pool["header_bytes"]:end])
    assert membership == pool["membership_digest"]
    assert metadata == pool["metadata_digest"]
    return membership, metadata

source, extended, clean, resumed = map(read, sys.argv[1:5])
assert source["records"] == 32768
assert extended["records"] > source["records"]
assert extended["family_id"] == source["family_id"]
assert extended["segment_id"] == source["segment_id"]
assert extended["lineage_id"] == source["lineage_id"]
assert extended["snapshot_id"] != source["snapshot_id"]

source_membership, source_metadata = data_digests(source)
extended_membership, extended_metadata = data_digests(extended)
assert extended_membership != source_membership
assert extended_metadata != source_metadata
assert source["snapshot_id"] == hash_fields(
    "snapshot", source["segment_id"], source["records"],
    source["data_bytes"], source_membership)

for child in (clean, resumed):
    assert child["family_id"] == source["family_id"]
    assert child["parent_snapshot_id"] == source["snapshot_id"]
    assert child["parent_segment_id"] == source["segment_id"]
    assert child["source_records"] == source["records"]
    assert child["parent_records"] == source["records"]
    assert child["parent_data_bytes"] == source["data_bytes"]
    assert child["input_cursor"] == source["records"]
    expected_lineage = hash_fields(
        "refilter", source["lineage_id"], child["stage_hash"],
        source["snapshot_id"], 0)
    assert child["lineage_id"] == expected_lineage
    expected_segment = hash_fields(
        "segment", expected_lineage, child["range_start"],
        child["range_end"], 0)  # SPACE_NATURAL
    assert child["segment_id"] == expected_segment
    assert child["derivation_id"] == hash_fields(
        "derive-refilter", expected_lineage, expected_segment,
        source["snapshot_id"], child["stage_hash"])
    child_membership, child_metadata = data_digests(child)
    assert child["snapshot_id"] == hash_fields(
        "snapshot", expected_segment, child["records"],
        child["data_bytes"], child_membership)

for key in ("family_id", "segment_id", "stage_hash", "lineage_id",
            "derivation_id", "snapshot_id", "membership_digest", "metadata_digest", "records",
            "data_bytes", "parent_snapshot_id", "parent_segment_id",
            "parent_records", "parent_data_bytes", "input_cursor"):
    assert resumed[key] == clean[key], key

state_text = open(sys.argv[5], "r", encoding="ascii").read()
assert state_text.startswith("BRAINSTORM_SEED_POOL_STATE 3\n")
def state_value(key, base=10):
    return int(re.search(r"^%s ([0-9a-f]+|\d+)$" % key,
                         state_text, re.M).group(1), base)
assert state_value("membership_digest", 16) == resumed["membership_digest"]
assert state_value("metadata_digest", 16) == resumed["metadata_digest"]
assert state_value("cursor") == source["records"]
assert state_value("scanned") == source["records"]
assert state_value("matched") == resumed["records"]
assert state_value("output_bytes") == len(resumed["raw"])
assert state_value("done") == 1
PY

RECORDS=$(python3 - "$OUT/source-original.bspool" <<'PY'
import re, sys
raw = open(sys.argv[1], "rb").read()
prefix = raw[:1024].split(b"\0", 1)[0].decode("ascii")
size = int(re.search(r"^header_bytes (\d+)$", prefix, re.M).group(1))
text = raw[:size].split(b"\0", 1)[0].decode("ascii")
print(re.search(r"^records (\d+)$", text, re.M).group(1))
PY
)
MATCHES=$(python3 - "$OUT/resumed.bspool" <<'PY'
import re, sys
raw = open(sys.argv[1], "rb").read()
prefix = raw[:1024].split(b"\0", 1)[0].decode("ascii")
size = int(re.search(r"^header_bytes (\d+)$", prefix, re.M).group(1))
text = raw[:size].split(b"\0", 1)[0].decode("ascii")
print(re.search(r"^records (\d+)$", text, re.M).group(1))
PY
)
echo "PASS: resumed $MATCHES matches from pinned $RECORDS-record snapshot; append accepted and prefix replacement rejected"
