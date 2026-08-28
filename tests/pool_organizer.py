#!/usr/bin/env python3
"""Focused regression tests for the no-rescan BSP3 organizer."""

import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import brainstorm_pool_organizer as organizer


FNV64_OFFSET = 1469598103934665603
FNV64_PRIME = 1099511628211
MASK64 = (1 << 64) - 1


def independent_fnv64(payload, value=FNV64_OFFSET):
    for byte in payload:
        value = ((value ^ byte) * FNV64_PRIME) & MASK64
    return value


def independent_fnv32(payload):
    value = 2166136261
    for byte in payload:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def independent_crc64(payload, value=0):
    for byte in payload:
        value ^= byte << 56
        for _ in range(8):
            if value & (1 << 63):
                value = ((value << 1) ^ 0x42F0E1EBA9EA3693) & MASK64
            else:
                value = (value << 1) & MASK64
    return value


def varint(value):
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(output)


def descriptor(kind, key, ante, phase, source, ordinal, flags):
    key_bytes = key.encode("ascii")
    return bytes((kind, len(key_bytes))) + key_bytes + bytes(
        (ante, phase, source, ordinal, flags))


TAG = descriptor(1, "tag_negative", 3, 1, 0, 0, 0)
LEGENDARY = descriptor(2, "j_perkeo", 4, 2, 1, 1, 1)
VOUCHER = descriptor(3, "v_overstock_norm", 2, 0, 1, 1, 0)
UNKNOWN = bytes((9, 1, 0xAA))


def metadata_payload(per_record):
    inverted = {}
    for record, descriptors in enumerate(per_record):
        for raw in descriptors:
            inverted.setdefault(raw, []).append(record)
    output = bytearray(varint(len(inverted)))
    associations = 0
    for raw in sorted(inverted):
        records = inverted[raw]
        output.extend(varint(len(raw)))
        output.extend(raw)
        output.extend(varint(len(records)))
        output.extend(varint(records[0]))
        for index in range(1, len(records)):
            output.extend(varint(records[index] - records[index - 1]))
        associations += len(records)
    return bytes(output), associations


def event_block(ranks, per_record):
    rank_payload = b"".join(varint(ranks[index] - ranks[index - 1])
                            for index in range(1, len(ranks)))
    metadata, associations = metadata_payload(per_record)
    header = bytearray(48)
    header[:4] = b"BSP3"
    header[4] = 48
    struct.pack_into("<IIIIQQ", header, 8, len(ranks), len(rank_payload),
                     len(metadata), associations, ranks[0], ranks[-1])
    checksum = independent_crc64(bytes(header[4:40]))
    checksum = independent_crc64(rank_payload, checksum)
    checksum = independent_crc64(metadata, checksum)
    struct.pack_into("<Q", header, 40, checksum)
    return bytes(header) + rank_payload + metadata, metadata


def hash_fields(kind, a, b, c, d):
    payload = ("%s:%016x:%016x:%016x:%016x" % (kind, a, b, c, d)).encode("ascii")
    return independent_fnv64(payload)


def write_bsp3(path, complete=False, charset=organizer.NATURAL_CHARSET,
               seedspace=organizer.NATURAL_SEEDSPACE, space="natural"):
    ranks = [0, 1, 2, 3]
    # Rank 1 is deliberately ambiguous. Rank 3 has only opaque future
    # metadata, so unmatched handling is also exercised.
    per_record = [
        [TAG],
        [TAG, LEGENDARY, VOUCHER],
        [VOUCHER],
        [UNKNOWN],
    ]
    block, metadata = event_block(ranks, per_record)
    membership = independent_fnv64(block)
    metadata_digest = independent_fnv64(metadata)
    family = 0x1111222233334444
    lineage = 0x2222333344445555
    segment = 0x3333444455556666
    snapshot = hash_fields("snapshot", segment, len(ranks), len(block), membership)
    lines = [
        "BRAINSTORM_SEED_POOL 3",
        "modelver 6",
        "encoding delta-varint-events-v1",
        "header_bytes 8192",
        "charset %s" % charset,
        "seedspace %d" % seedspace,
        "space %s" % space,
        "range_start 0",
        "range_end 100",
        "catalog_hash aaaaaaaaaaaaaaaa",
        "criteria_hash bbbbbbbbbbbbbbbb",
        "pool_id source-pool",
        "family_id %016x" % family,
        "segment_id %016x" % segment,
        "stage_hash 4444555566667777",
        "lineage_id %016x" % lineage,
        "derivation_id 5555666677778888",
        "snapshot_id %016x" % snapshot,
        "membership_digest %016x" % membership,
        "metadata_digest %016x" % metadata_digest,
        "scan_cursor 4",
        "tag_route collect",
        "tag tag_negative 3 small 3 small 1",
        "legendary j_perkeo 4 big 4 big 1 shop",
        "voucher v_overstock_norm 2 2",
        "records %d" % len(ranks),
        "data_bytes %d" % len(block),
        "complete %d" % int(complete),
        "coverage_complete %d" % int(complete),
        "end",
    ]
    header = ("\n".join(lines) + "\n").encode("ascii")
    header += b"\0" * (8192 - len(header))
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(block)
        if complete:
            index_offset = 8192 + len(block)
            # One block, first record zero.
            rank_bytes = len(varint(1) + varint(1) + varint(1))
            handle.write(struct.pack("<QQIIII", 8192, 0, len(ranks),
                                     rank_bytes, len(metadata), 6))
            footer = bytearray(80)
            footer[:8] = b"BSPIDX3\n"
            struct.pack_into("<QQQQQQ", footer, 8, index_offset, 1, len(ranks),
                             len(block), membership, metadata_digest)
            struct.pack_into("<Q", footer, 72, independent_crc64(bytes(footer[:72])))
            handle.write(footer)
        else:
            # A paused writer may have bytes beyond the last checkpoint. The
            # organizer must never treat these as committed records.
            handle.write(b"UNCOMMITTED-TAIL-MUST-BE-IGNORED")
    return {
        "snapshot": "%016x" % snapshot,
        "family": "%016x" % family,
        "segment": "%016x" % segment,
    }


def write_empty_bsp3(path):
    """Write a completed BSP3 search that found no matching seeds."""
    family = 0x1011222233334444
    lineage = 0x2022333344445555
    segment = 0x3033444455556666
    stage = 0x4044555566667777
    membership = FNV64_OFFSET
    metadata_digest = FNV64_OFFSET
    snapshot = hash_fields("snapshot", segment, 0, 0, membership)
    lines = [
        "BRAINSTORM_SEED_POOL 3",
        "modelver 6",
        "encoding delta-varint-events-v1",
        "header_bytes 8192",
        "charset %s" % organizer.NATURAL_CHARSET,
        "seedspace %d" % organizer.NATURAL_SEEDSPACE,
        "space natural",
        "range_start 0",
        "range_end 100",
        "catalog_hash aaaaaaaaaaaaaaaa",
        "criteria_hash bbbbbbbbbbbbbbbb",
        "pool_id empty-source-pool",
        "family_id %016x" % family,
        "segment_id %016x" % segment,
        "stage_hash %016x" % stage,
        "lineage_id %016x" % lineage,
        "derivation_id 5055666677778888",
        "snapshot_id %016x" % snapshot,
        "membership_digest %016x" % membership,
        "metadata_digest %016x" % metadata_digest,
        "scan_cursor 100",
        "tag_route collect",
        "tag tag_negative 3 small 3 small 1",
        "records 0",
        "data_bytes 0",
        "complete 1",
        "coverage_complete 1",
        "end",
    ]
    header = ("\n".join(lines) + "\n").encode("ascii").ljust(8192, b"\0")
    footer = bytearray(80)
    footer[:8] = b"BSPIDX3\n"
    struct.pack_into(
        "<QQQQQQ", footer, 8, 8192, 0, 0, 0,
        membership, metadata_digest)
    struct.pack_into(
        "<Q", footer, 72, independent_crc64(bytes(footer[:72])))
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(footer)
    return {
        "snapshot": "%016x" % snapshot,
        "family": "%016x" % family,
        "lineage": "%016x" % lineage,
        "stage": "%016x" % stage,
    }


def expand_bsp3_header(source, target, header_bytes=16 * 1024):
    """Copy a complete BSP3 pool into a valid larger event-header layout."""
    reader = organizer.BSPoolReader(source)
    if (reader.schema != 3 or not reader.complete
            or header_bytes <= reader.header_bytes
            or header_bytes > organizer.HEADER_MAX_BYTES):
        raise ValueError("extended BSP3 fixture parameters are invalid")
    with open(source, "rb") as handle:
        raw = handle.read()
    old_header_bytes = reader.header_bytes
    header = raw[:old_header_bytes].split(b"\0", 1)[0].decode("ascii")
    header, changed = re.subn(
        r"(?m)^header_bytes \d+$",
        "header_bytes %d" % header_bytes, header, count=1)
    if changed != 1:
        raise AssertionError("BSP3 fixture header_bytes did not rewrite")
    encoded_header = header.encode("ascii")
    if len(encoded_header) > header_bytes:
        raise AssertionError("extended BSP3 fixture header overflowed")

    data_end = old_header_bytes + reader.data_bytes
    index_bytes = len(reader.blocks) * organizer.INDEX3_ENTRY_BYTES
    raw_index = bytearray(raw[data_end:data_end + index_bytes])
    shift = header_bytes - old_header_bytes
    for number in range(len(reader.blocks)):
        at = number * organizer.INDEX3_ENTRY_BYTES
        offset = struct.unpack_from("<Q", raw_index, at)[0]
        struct.pack_into("<Q", raw_index, at, offset + shift)
    footer = bytearray(raw[data_end + index_bytes:])
    if len(footer) != organizer.FOOTER3_BYTES \
            or footer[:8] != b"BSPIDX3\n":
        raise AssertionError("extended BSP3 fixture footer is invalid")
    struct.pack_into("<Q", footer, 8, header_bytes + reader.data_bytes)
    struct.pack_into(
        "<Q", footer, 72, independent_crc64(bytes(footer[:72])))
    with open(target, "wb") as handle:
        handle.write(encoded_header.ljust(header_bytes, b"\0"))
        handle.write(raw[old_header_bytes:data_end])
        handle.write(raw_index)
        handle.write(footer)
    organizer.BSPoolReader(target)
    return target


def write_bsp2(path, complete=False):
    ranks = [5, 7]
    rank_payload = varint(2)
    block = struct.pack("<4sIIIQQ", b"BSP2", len(ranks), len(rank_payload),
                        independent_fnv32(rank_payload), ranks[0], ranks[-1]) + rank_payload
    membership = independent_fnv64(block)
    lines = [
        "BRAINSTORM_SEED_POOL 2", "modelver 6",
        "encoding delta-varint-blocks-v1", "header_bytes 1024",
        "charset %s" % organizer.NATURAL_CHARSET,
        "seedspace %d" % organizer.NATURAL_SEEDSPACE,
        "space natural", "range_start 0", "range_end 100",
        "catalog_hash aaaaaaaaaaaaaaaa", "criteria_hash bbbbbbbbbbbbbbbb",
        "pool_id old-pool", "membership_digest %016x" % membership,
        "tag_route observe", "records 2", "data_bytes %d" % len(block),
        "complete %d" % int(complete), "coverage_complete %d" % int(complete), "end",
    ]
    header = ("\n".join(lines) + "\n").encode("ascii")
    header += b"\0" * (1024 - len(header))
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(block)
        if complete:
            index_offset = 1024 + len(block)
            handle.write(struct.pack("<QQII", 1024, 0, len(ranks), len(rank_payload)))
            handle.write(struct.pack("<8sQQQQ", b"BSPIDX2\n", index_offset, 1,
                                     len(ranks), len(block)))


def write_rank_bsp2_shard(path, block_ranks, range_start, range_end,
                          criteria_hash="abababababababab"):
    """Write a complete BSP2 shard, preserving the supplied physical order."""
    encoded = []
    for ranks in block_ranks:
        if not ranks:
            raise ValueError("rank block cannot be empty")
        rank_payload = b"".join(
            varint(ranks[index] - ranks[index - 1])
            for index in range(1, len(ranks)))
        block = struct.pack(
            "<4sIIIQQ", b"BSP2", len(ranks), len(rank_payload),
            independent_fnv32(rank_payload), ranks[0], ranks[-1])
        encoded.append(block + rank_payload)
    records = sum(len(ranks) for ranks in block_ranks)
    data_bytes = sum(len(block) for block in encoded)
    membership = FNV64_OFFSET
    for block in encoded:
        membership = independent_fnv64(block, membership)
    lines = [
        "BRAINSTORM_SEED_POOL 2", "modelver 6",
        "encoding delta-varint-blocks-v1", "header_bytes 1024",
        "charset %s" % organizer.NATURAL_CHARSET,
        "seedspace %d" % organizer.NATURAL_SEEDSPACE,
        "space natural", "range_start %d" % range_start,
        "range_end %d" % range_end,
        "catalog_hash aaaaaaaaaaaaaaaa",
        "criteria_hash %s" % criteria_hash,
        "pool_id rank-shard-%d" % range_start,
        "tag_route observe", "records %d" % records,
        "data_bytes %d" % data_bytes,
        "complete 1", "coverage_complete 1", "end",
    ]
    header = ("\n".join(lines) + "\n").encode("ascii").ljust(
        1024, b"\0")
    index_rows = []
    offset = 1024
    first_record = 0
    with open(path, "wb") as handle:
        handle.write(header)
        for ranks, block in zip(block_ranks, encoded):
            handle.write(block)
            index_rows.append((offset, first_record, len(ranks),
                               len(block) - 32))
            offset += len(block)
            first_record += len(ranks)
        index_offset = 1024 + data_bytes
        for row in index_rows:
            handle.write(struct.pack("<QQII", *row))
        handle.write(struct.pack(
            "<8sQQQQ", b"BSPIDX2\n", index_offset, len(index_rows),
            records, data_bytes))


def write_custom_bsp3(path, ranks, per_record, criteria_hash,
                      criteria_lines, complete=True, coverage_complete=None,
                      catalog_hash="aaaaaaaaaaaaaaaa", modelver=6,
                      range_start=0, range_end=100,
                      charset=organizer.NATURAL_CHARSET,
                      seedspace=organizer.NATURAL_SEEDSPACE,
                      space="natural", segment=None):
    """Write a small independently encoded pool for set-operation tests."""
    if not ranks or len(ranks) != len(per_record):
        raise ValueError("custom fixture needs matching non-empty records")
    if coverage_complete is None:
        coverage_complete = complete
    block, metadata = event_block(ranks, per_record)
    membership = independent_fnv64(block)
    metadata_digest = independent_fnv64(metadata)
    criteria_value = int(criteria_hash, 16)
    if segment is None:
        segment = hash_fields("test-segment", criteria_value, ranks[0],
                              ranks[-1], len(ranks))
    family = hash_fields("test-family", int(catalog_hash, 16), criteria_value, 0, 0)
    lineage = hash_fields("test-lineage", family, criteria_value, 0, 0)
    snapshot = hash_fields("snapshot", segment, len(ranks), len(block), membership)
    lines = [
        "BRAINSTORM_SEED_POOL 3",
        "modelver %d" % modelver,
        "encoding delta-varint-events-v1",
        "header_bytes 8192",
        "charset %s" % charset,
        "seedspace %d" % seedspace,
        "space %s" % space,
        "range_start %d" % range_start,
        "range_end %d" % range_end,
        "catalog_hash %s" % catalog_hash,
        "criteria_hash %s" % criteria_hash,
        "pool_id fixture-%s" % criteria_hash[:8],
        "family_id %016x" % family,
        "segment_id %016x" % segment,
        "stage_hash %016x" % criteria_value,
        "lineage_id %016x" % lineage,
        "derivation_id %016x" % hash_fields(
            "test-derive", lineage, segment, snapshot, 0),
        "snapshot_id %016x" % snapshot,
        "membership_digest %016x" % membership,
        "metadata_digest %016x" % metadata_digest,
        "scan_cursor %d" % (range_end if complete else ranks[-1] + 1),
        "label Fixture %s" % criteria_hash[:8],
    ]
    lines.extend(criteria_lines)
    lines.extend([
        "records %d" % len(ranks),
        "data_bytes %d" % len(block),
        "complete %d" % int(complete),
        "coverage_complete %d" % int(coverage_complete),
        "end",
    ])
    header = ("\n".join(lines) + "\n").encode("ascii")
    header += b"\0" * (8192 - len(header))
    with open(path, "wb") as handle:
        handle.write(header)
        handle.write(block)
        if complete:
            index_offset = 8192 + len(block)
            rank_bytes = sum(len(varint(ranks[index] - ranks[index - 1]))
                             for index in range(1, len(ranks)))
            associations = sum(len(set(items)) for items in per_record)
            handle.write(struct.pack("<QQIIII", 8192, 0, len(ranks),
                                     rank_bytes, len(metadata), associations))
            footer = bytearray(80)
            footer[:8] = b"BSPIDX3\n"
            struct.pack_into("<QQQQQQ", footer, 8, index_offset, 1, len(ranks),
                             len(block), membership, metadata_digest)
            struct.pack_into("<Q", footer, 72,
                             independent_crc64(bytes(footer[:72])))
            handle.write(footer)
        else:
            handle.write(b"UNCOMMITTED-CUSTOM-TAIL")
    return {
        "snapshot": "%016x" % snapshot,
        "family": "%016x" % family,
        "segment": "%016x" % segment,
    }


def write_out_of_order_bsp3(path, overlapping=True, duplicate=False,
                            range_start=0, range_end=100,
                            long_opaque=False, boundary=False):
    """Write two sorted blocks in deliberately wrong physical order."""
    if boundary:
        tag_variant = descriptor(
            1, "tag_negative", 3, 1, 2, 7, 4)
        long_descriptor = b"\x70" + b"x" * 299
        logical = []
        for pair in range(3):
            start = pair * 1800
            for parity in (0, 1):
                ranks = list(range(start + parity, start + 1800, 2))
                occurrences = []
                for rank in ranks:
                    row = [TAG]
                    if rank % 3 == 0:
                        row.append(LEGENDARY)
                    if rank % 7 == 0:
                        row.append(tag_variant)
                    if rank % 101 == 0:
                        row.append(long_descriptor)
                    occurrences.append(row)
                logical.append((ranks, occurrences))
        # Shuffled physical order plus pairwise-overlapping intervals force
        # the k-way path. The 5,400 records cross a BSP4 flush boundary and
        # split the final source-block pair across output batches.
        base_specs = [logical[index] for index in (2, 1, 4, 3, 0, 5)]
        range_end = max(range_end, range_start + 6000)
    else:
        base_specs = (
            [([1, 5], [[TAG], [TAG]]), ([1, 4], [[TAG], [TAG]])]
            if duplicate else
            [([1, 5], [[TAG], [TAG]]), ([2, 4], [[TAG], [TAG]])]
            if overlapping else
            [([4, 5], [[TAG], [TAG]]), ([1, 2], [[TAG], [TAG]])])
    specs = [
        ([rank + range_start for rank in ranks], occurrences)
        for ranks, occurrences in base_specs
    ]
    if long_opaque:
        # BSP3's forward-compatible contract permits arbitrary
        # length-delimited opaque descriptors, not only native-sized filter
        # descriptors. Keep this above MAX_KEY + 8 to exercise repacking.
        specs[0][1][0].append(b"\x70" + b"x" * 299)
    encoded = [event_block(ranks, occurrences)
               for ranks, occurrences in specs]
    total_records = sum(len(ranks) for ranks, _occurrences in specs)
    membership = FNV64_OFFSET
    metadata_digest = FNV64_OFFSET
    for block, metadata in encoded:
        membership = independent_fnv64(block, membership)
        metadata_digest = independent_fnv64(metadata, metadata_digest)
    segment = 0x7654321012345678 ^ range_start
    data_bytes = sum(len(block) for block, _metadata in encoded)
    snapshot = hash_fields(
        "snapshot", segment, total_records, data_bytes, membership)
    lines = [
        "BRAINSTORM_SEED_POOL 3",
        "modelver 6",
        "encoding delta-varint-events-v1",
        "header_bytes 8192",
        "charset %s" % organizer.NATURAL_CHARSET,
        "seedspace %d" % organizer.NATURAL_SEEDSPACE,
        "space natural",
        "range_start %d" % range_start,
        "range_end %d" % range_end,
        "catalog_hash aaaaaaaaaaaaaaaa",
        "criteria_hash 9999999999999999",
        "pool_id out-of-order-fixture",
        "family_id 1111111111111111",
        "segment_id %016x" % segment,
        "stage_hash 2222222222222222",
        "lineage_id 3333333333333333",
        "derivation_id 4444444444444444",
        "snapshot_id %016x" % snapshot,
        "membership_digest %016x" % membership,
        "metadata_digest %016x" % metadata_digest,
        "scan_cursor %d" % range_end,
        "label Out of order blocks",
        "tag_route collect",
        "tag tag_negative 1 small 4 big 1",
        "records %d" % total_records,
        "data_bytes %d" % data_bytes,
        "complete 1",
        "coverage_complete 1",
        "end",
    ]
    header = ("\n".join(lines) + "\n").encode("ascii").ljust(8192, b"\0")
    index_rows = []
    offset = 8192
    first_record = 0
    with open(path, "wb") as handle:
        handle.write(header)
        for block, _metadata in encoded:
            count, rank_bytes, metadata_bytes, associations = struct.unpack_from(
                "<IIII", block, 8)
            handle.write(block)
            index_rows.append((offset, first_record, count, rank_bytes,
                               metadata_bytes, associations))
            offset += len(block)
            first_record += count
        for row in index_rows:
            handle.write(struct.pack("<QQIIII", *row))
        footer = bytearray(80)
        footer[:8] = b"BSPIDX3\n"
        struct.pack_into("<QQQQQQ", footer, 8, 8192 + data_bytes,
                         len(index_rows), total_records, data_bytes, membership,
                         metadata_digest)
        struct.pack_into("<Q", footer, 72,
                         independent_crc64(bytes(footer[:72])))
        handle.write(footer)


class OrganizerRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="brainstorm-organizer-test-")
        self.source = os.path.join(self.temp.name, "paused.bspool")
        self.identity = write_bsp3(self.source, complete=False)
        self.tool = os.path.join(TOOLS, "brainstorm_pool_organizer.py")

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, *args, expected=0):
        result = subprocess.run([sys.executable, self.tool] + list(args),
                                text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        self.assertEqual(result.returncode, expected,
                         "stdout:\n%s\nstderr:\n%s" % (result.stdout, result.stderr))
        return result

    def test_inspect_records_and_committed_boundary(self):
        result = self.run_tool("inspect", self.source, "--json",
                               "--ambiguity-limit", "-1")
        report = json.loads(result.stdout)
        self.assertEqual(report["source"]["records"], 4)
        self.assertFalse(report["source"]["complete"])
        self.assertEqual(report["source"]["snapshot_id"], self.identity["snapshot"])
        self.assertEqual(report["category_count"], 3)
        self.assertEqual(report["ambiguous_count"], 1)
        self.assertEqual(report["unmatched_count"], 1)
        self.assertEqual(report["opaque_associations"], 1)
        records = self.run_tool("records", self.source).stdout.strip().splitlines()
        decoded = [json.loads(line) for line in records]
        self.assertEqual([item["seed"] for item in decoded],
                         ["11111111", "21111111", "31111111", "41111111"])
        self.assertEqual(len(decoded[1]["occurrences"]), 3)
        self.assertFalse(decoded[3]["occurrences"][0]["known"])

    def test_cli_traversals_validate_and_consume_without_constructor_pass(self):
        source_path = os.path.abspath(self.source)
        original_validated = (
            organizer.BSPoolReader._read_validated_block_records)
        original_plain = organizer.BSPoolReader._read_block_records

        def command_reads(command):
            reads = []

            def validated(reader, handle, block, membership, metadata):
                reads.append(reader.path)
                return original_validated(
                    reader, handle, block, membership, metadata)

            def plain(reader, handle, block):
                reads.append(reader.path)
                return original_plain(reader, handle, block)

            with mock.patch.object(
                    organizer.BSPoolReader, "_read_validated_block_records",
                    validated), mock.patch.object(
                        organizer.BSPoolReader, "_read_block_records", plain):
                command()
            return reads

        def inspect():
            with redirect_stdout(io.StringIO()):
                self.assertEqual(organizer.command_inspect(SimpleNamespace(
                    input=self.source, ambiguity_limit=0, json=True)), 0)

        self.assertEqual(command_reads(inspect).count(source_path), 1)

        def records():
            with redirect_stdout(io.StringIO()):
                self.assertEqual(organizer.command_records(SimpleNamespace(
                    input=self.source, output="-")), 0)

        self.assertEqual(command_reads(records).count(source_path), 1)

        second = os.path.join(self.temp.name, "single-pass-second.bspool")
        write_custom_bsp3(
            second, [4], [[TAG]], "cccccccccccccccc", [
                "tag_route collect",
                "tag tag_negative 1 small 4 big 1",
            ])
        combined = os.path.join(self.temp.name, "single-pass-union.bspool")

        def combine():
            with redirect_stdout(io.StringIO()):
                self.assertEqual(organizer.command_combine(SimpleNamespace(
                    inputs=[self.source, second], output=combined,
                    operation="union", label="Single pass union")), 0)

        combine_reads = command_reads(combine)
        self.assertEqual(combine_reads.count(source_path), 1)
        self.assertEqual(combine_reads.count(os.path.abspath(second)), 1)

        category = organizer.Occurrence.decode(TAG).category_id
        split_dir = os.path.join(self.temp.name, "single-pass-split")
        report_path = os.path.join(self.temp.name, "single-pass-plan.json")

        def split():
            with redirect_stdout(io.StringIO()):
                self.assertEqual(organizer.command_split(SimpleNamespace(
                    input=self.source, output_dir=split_dir,
                    category=[category], choices=None, report=report_path,
                    remainder=None, omit_unmatched=True)), 0)

        # Split necessarily plans and stages separately. Its inspect traversal
        # now performs validation, so no fourth constructor payload pass occurs.
        self.assertEqual(command_reads(split).count(source_path), 3)

    def test_source_replacement_and_inflight_mutation_are_rejected(self):
        stale = organizer.BSPoolReader(
            self.source, verify_payloads=False)
        replacement = os.path.join(self.temp.name, "replacement.bspool")
        write_bsp3(replacement, complete=False)
        os.replace(replacement, self.source)
        with self.assertRaisesRegex(
                organizer.PoolError, "changed or was replaced"):
            list(stale.iter_records())

        current = organizer.BSPoolReader(self.source)
        records = current.iter_records()
        self.assertEqual(next(records).rank, 0)
        status = os.stat(self.source)
        os.utime(
            self.source,
            ns=(status.st_atime_ns, status.st_mtime_ns + 2000000000))
        with self.assertRaisesRegex(
                organizer.PoolError, "changed or was replaced"):
            list(records)

    def test_windows_snapshot_wrapper_transfers_ownership_exactly_once(self):
        import ctypes

        def dependencies(create_result=1234):
            kernel32 = SimpleNamespace(
                CreateFileW=mock.Mock(return_value=create_result),
                CloseHandle=mock.Mock(return_value=1),
            )
            ctypes_api = SimpleNamespace(
                WinDLL=mock.Mock(return_value=kernel32),
                c_wchar_p=ctypes.c_wchar_p,
                c_uint32=ctypes.c_uint32,
                c_void_p=ctypes.c_void_p,
                c_int=ctypes.c_int,
                get_last_error=mock.Mock(return_value=32),
                WinError=lambda code: OSError(code, "sharing violation"),
            )
            crt = SimpleNamespace(
                open_osfhandle=mock.Mock(return_value=17))
            return ctypes_api, crt, kernel32

        ctypes_api, crt, kernel32 = dependencies()
        stream = object()
        fdopen = mock.Mock(return_value=stream)
        close_fd = mock.Mock()
        opened = organizer._open_windows_read_snapshot(
            "C:\\fixture\\pool.bspool", _ctypes=ctypes_api, _msvcrt=crt,
            _fdopen=fdopen, _close_fd=close_fd)
        self.assertIs(opened, stream)
        ctypes_api.WinDLL.assert_called_once_with(
            "kernel32", use_last_error=True)
        kernel32.CreateFileW.assert_called_once_with(
            "C:\\fixture\\pool.bspool",
            0x80000000, 0x00000001, None, 3, 0x00000080, None)
        crt.open_osfhandle.assert_called_once_with(
            1234, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        fdopen.assert_called_once_with(17, "rb", closefd=True)
        kernel32.CloseHandle.assert_not_called()
        close_fd.assert_not_called()

        ctypes_api, crt, kernel32 = dependencies()
        crt.open_osfhandle.side_effect = OSError("CRT adoption failed")
        close_fd = mock.Mock()
        with self.assertRaisesRegex(OSError, "CRT adoption failed"):
            organizer._open_windows_read_snapshot(
                "C:\\fixture\\pool.bspool", _ctypes=ctypes_api,
                _msvcrt=crt, _fdopen=mock.Mock(), _close_fd=close_fd)
        kernel32.CloseHandle.assert_called_once_with(1234)
        close_fd.assert_not_called()

        ctypes_api, crt, kernel32 = dependencies()
        fdopen = mock.Mock(side_effect=OSError("stream adoption failed"))
        close_fd = mock.Mock()
        with self.assertRaisesRegex(OSError, "stream adoption failed"):
            organizer._open_windows_read_snapshot(
                "C:\\fixture\\pool.bspool", _ctypes=ctypes_api,
                _msvcrt=crt, _fdopen=fdopen, _close_fd=close_fd)
        kernel32.CloseHandle.assert_not_called()
        close_fd.assert_called_once_with(17)

        ctypes_api, crt, kernel32 = dependencies(
            ctypes.c_void_p(-1).value)
        with self.assertRaisesRegex(OSError, "sharing violation"):
            organizer._open_windows_read_snapshot(
                "C:\\fixture\\pool.bspool", _ctypes=ctypes_api,
                _msvcrt=crt)
        crt.open_osfhandle.assert_not_called()
        kernel32.CloseHandle.assert_not_called()

    def test_windows_source_identity_normalizes_stat_ctime_contract(self):
        common = {
            "st_dev": 7,
            "st_ino": 123456789,
            "st_mode": 0o100666,
            "st_size": 4096,
            "st_mtime_ns": 1700000000000000000,
            "st_birthtime_ns": 1600000000000000000,
        }
        handle_status = SimpleNamespace(
            **common, st_ctime_ns=1750000000000000000)
        path_status = SimpleNamespace(
            **common, st_ctime_ns=1600000000000000000)

        with mock.patch.object(organizer.os, "name", "nt"):
            handle_identity = organizer._source_identity(handle_status)
            path_identity = organizer._source_identity(path_status)

        self.assertEqual(handle_identity, path_identity)
        self.assertEqual(
            handle_identity.ctime_ns, common["st_birthtime_ns"])

    @unittest.skipUnless(os.name == "nt", "Windows sharing semantics")
    def test_windows_snapshot_denies_write_and_delete_until_closed(self):
        import ctypes

        reader = organizer.BSPoolReader(
            self.source, verify_payloads=False)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        invalid = ctypes.c_void_p(-1).value
        share_all = 0x00000001 | 0x00000002 | 0x00000004

        replacement = os.path.join(
            self.temp.name, "sharing-replacement.bspool")
        write_bsp3(replacement, complete=False)
        with reader._open_source_snapshot() as snapshot:
            self.assertFalse(snapshot.closed)
            writer = kernel32.CreateFileW(
                self.source, 0x40000000, share_all, None, 3, 0x80, None)
            self.assertEqual(writer, invalid)
            self.assertEqual(ctypes.get_last_error(), 32)
            with self.assertRaises(OSError):
                os.replace(replacement, self.source)

        writer = kernel32.CreateFileW(
            self.source, 0x40000000, share_all, None, 3, 0x80, None)
        self.assertNotIn(writer, (None, invalid))
        self.assertTrue(kernel32.CloseHandle(writer))

    def test_replaced_sources_cannot_feed_records_split_or_combine_outputs(self):
        records_source = os.path.join(self.temp.name, "stale-records.bspool")
        records_replacement = os.path.join(
            self.temp.name, "stale-records-replacement.bspool")
        write_bsp3(records_source, complete=False)
        write_bsp3(records_replacement, complete=False)
        real_reader = organizer.BSPoolReader

        def replace_after_inspect(path, *args, **kwargs):
            reader = real_reader(path, *args, **kwargs)
            os.replace(records_replacement, records_source)
            return reader

        record_output = os.path.join(self.temp.name, "stale-records.ndjson")
        with mock.patch.object(
                organizer, "BSPoolReader", replace_after_inspect):
            with self.assertRaisesRegex(
                    organizer.PoolError, "changed or was replaced"):
                organizer.command_records(SimpleNamespace(
                    input=records_source, output=record_output))
        self.assertFalse(os.path.exists(record_output))

        split_source = os.path.join(self.temp.name, "stale-split.bspool")
        write_bsp3(split_source, complete=False)
        split_reader = organizer.BSPoolReader(
            split_source, verify_payloads=False)
        replacement = os.path.join(
            self.temp.name, "stale-split-replacement.bspool")
        write_bsp3(replacement, complete=False)
        os.replace(replacement, split_source)
        split_dir = os.path.join(self.temp.name, "stale-split-output")
        split_report = os.path.join(self.temp.name, "stale-split-plan.json")
        with self.assertRaisesRegex(
                organizer.PoolError, "changed or was replaced"):
            organizer.split_pool(
                split_reader, split_dir, None, None, split_report, None, True)
        self.assertFalse(os.path.exists(split_report))
        self.assertFalse([
            name for name in os.listdir(split_dir)
            if name.endswith(".bspool")
        ])

        first = os.path.join(self.temp.name, "stale-combine.bspool")
        second = os.path.join(self.temp.name, "stable-combine.bspool")
        write_custom_bsp3(
            first, [1], [[TAG]], "1111111111111111", [
                "tag_route collect", "tag tag_negative 1 small 1 small 1",
            ])
        write_custom_bsp3(
            second, [2], [[TAG]], "2222222222222222", [
                "tag_route collect", "tag tag_negative 1 small 1 small 1",
            ])
        stale_reader = organizer.BSPoolReader(
            first, verify_payloads=False)
        stable_reader = organizer.BSPoolReader(
            second, verify_payloads=False)
        replacement = os.path.join(
            self.temp.name, "stale-combine-replacement.bspool")
        write_custom_bsp3(
            replacement, [1], [[TAG]], "1111111111111111", [
                "tag_route collect", "tag tag_negative 1 small 1 small 1",
            ])
        os.replace(replacement, first)
        combined = os.path.join(self.temp.name, "stale-union.bspool")
        with self.assertRaisesRegex(
                organizer.PoolError, "changed or was replaced"):
            organizer.combine_pools(
                [stale_reader, stable_reader], combined, "union",
                "Must not publish")
        self.assertFalse(os.path.exists(combined))

    def test_prepared_split_caps_simultaneous_output_streams(self):
        class Reader:
            metadata_capable = True
            records = organizer.MAX_SPLIT_OUTPUTS + 1

        categories = [
            "tag:fixture-%03d" % index
            for index in range(organizer.MAX_SPLIT_OUTPUTS + 1)
        ]
        rows = [{
            "category_id": category,
            "label": "Fixture %d" % index,
            "records": 1,
        } for index, category in enumerate(categories)]
        output_dir = os.path.join(self.temp.name, "too-many-split-outputs")
        with self.assertRaisesRegex(
                organizer.PoolError,
                r"%d non-empty pools.*maximum %d outputs" % (
                    organizer.MAX_SPLIT_OUTPUTS + 1,
                    organizer.MAX_SPLIT_OUTPUTS)):
            organizer.write_prepared_split(
                Reader(), output_dir, categories, {}, rows, {},
                os.path.join(self.temp.name, "too-many-plan.json"))
        self.assertEqual(os.listdir(output_dir), [])

    def test_split_output_limit_reserves_two_posix_descriptors_per_pool(self):
        fake_resource = SimpleNamespace(
            RLIMIT_NOFILE=7,
            RLIM_INFINITY=-1,
            getrlimit=lambda _kind: (256, 256),
        )
        with mock.patch.object(organizer.os, "name", "posix"), \
                mock.patch.dict(sys.modules, {"resource": fake_resource}):
            self.assertEqual(organizer._safe_split_output_limit(), 112)
            fake_resource.getrlimit = lambda _kind: (-1, -1)
            self.assertEqual(organizer._safe_split_output_limit(), 256)

        fake_resource.getrlimit = mock.Mock(side_effect=OSError("unavailable"))
        with mock.patch.object(organizer.os, "name", "posix"), \
                mock.patch.dict(sys.modules, {"resource": fake_resource}):
            self.assertEqual(organizer._safe_split_output_limit(), 96)
        with mock.patch.object(organizer.os, "name", "nt"):
            self.assertEqual(organizer._safe_split_output_limit(), 256)

    def test_seed_pool_mutation_owner_publication_and_deletion_contract(self):
        owner = organizer.seed_pool_mutations
        root = os.path.join(self.temp.name, "mutation-owner")
        os.makedirs(root)
        staged = os.path.join(root, ".staged-pool")
        destination = os.path.join(root, "published.bspool")
        with open(staged, "wb") as handle:
            handle.write(b"verified pool bytes")

        owner.link_no_overwrite(staged, destination)
        self.assertTrue(os.path.samefile(staged, destination))
        with self.assertRaises(FileExistsError):
            owner.link_no_overwrite(staged, destination)
        self.assertTrue(owner.rollback_link(staged, destination))
        self.assertFalse(os.path.exists(destination))
        self.assertFalse(any(
            name.startswith(".pool-rollback-")
            for name in os.listdir(root)))

        staged_second = os.path.join(root, ".staged-second")
        first_destination = os.path.join(root, "first.bspool")
        with open(staged_second, "wb") as handle:
            handle.write(b"second verified pool")
        with open(destination, "wb") as handle:
            handle.write(b"occupied")
        with self.assertRaises(FileExistsError):
            owner.link_many_no_overwrite((
                (staged, first_destination),
                (staged_second, destination),
            ))
        self.assertFalse(os.path.exists(first_destination))
        self.assertTrue(os.path.exists(staged))
        self.assertTrue(os.path.exists(staged_second))

        raced_destination = os.path.join(root, "raced.bspool")
        later_destination = os.path.join(root, "later.bspool")
        real_link = organizer.pool_mutation.os.link
        link_calls = 0

        def replace_then_fail(source, target, *args, **kwargs):
            nonlocal link_calls
            link_calls += 1
            if link_calls == 1:
                return real_link(source, target, *args, **kwargs)
            if link_calls == 2:
                os.unlink(raced_destination)
                with open(raced_destination, "wb") as handle:
                    handle.write(b"foreign replacement during publication")
                raise FileExistsError("simulated later publication collision")
            return real_link(source, target, *args, **kwargs)

        with mock.patch.object(
                organizer.pool_mutation.os, "link",
                side_effect=replace_then_fail):
            with self.assertRaises(FileExistsError):
                owner.link_many_no_overwrite((
                    (staged, raced_destination),
                    (staged_second, later_destination),
                ))
        with open(raced_destination, "rb") as handle:
            self.assertEqual(
                handle.read(), b"foreign replacement during publication")
        self.assertFalse(os.path.exists(later_destination))
        self.assertTrue(os.path.exists(staged))
        self.assertTrue(os.path.exists(staged_second))

        verified_destination = os.path.join(root, "verified-race.bspool")
        owner.link_no_overwrite(staged, verified_destination)
        real_identity_check = owner._same_artifact_entry
        replacement_written = False

        def replace_after_identity_check(source, target):
            nonlocal replacement_written
            identical = real_identity_check(source, target)
            if identical and not replacement_written:
                replacement_written = True
                if os.path.exists(verified_destination):
                    os.unlink(verified_destination)
                with open(verified_destination, "wb") as handle:
                    handle.write(b"foreign replacement after identity check")
            return identical

        with mock.patch.object(
                owner, "_same_artifact_entry",
                side_effect=replace_after_identity_check):
            self.assertTrue(
                owner.rollback_link(staged, verified_destination))
        with open(verified_destination, "rb") as handle:
            self.assertEqual(
                handle.read(), b"foreign replacement after identity check")

        with open(destination, "wb") as handle:
            handle.write(b"foreign replacement")
        self.assertFalse(owner.rollback_link(staged, destination))
        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), b"foreign replacement")

        marker = os.path.join(root, "published.bspool.attached")
        report = os.path.join(root, "publication.json")
        owner.atomic_text(marker, "enabled 1\n")
        owner.atomic_json(report, {"completed": True, "records": 1})
        with open(marker, "r", encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "enabled 1\n")
        with open(report, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {
                "completed": True,
                "records": 1,
            })

        main = os.path.join(root, "delete-me.bspool")
        blocking_sidecar = main + ".manifest"
        with open(main, "wb") as handle:
            handle.write(b"pool")
        os.mkdir(blocking_sidecar)
        with self.assertRaises(OSError):
            owner.delete_artifacts([main, blocking_sidecar], main)
        self.assertTrue(os.path.isfile(main))

    def test_seed_pool_mutation_rollback_preserves_restore_collision(self):
        owner = organizer.seed_pool_mutations
        root = os.path.join(self.temp.name, "rollback-restore-race")
        os.makedirs(root)
        staged = os.path.join(root, ".staged-pool")
        destination = os.path.join(root, "published.bspool")
        with open(staged, "wb") as handle:
            handle.write(b"our staged pool")
        with open(destination, "wb") as handle:
            handle.write(b"first foreign artifact")

        real_identity_check = owner._same_artifact_entry
        replacement_written = False

        def replace_while_quarantined(source, target):
            nonlocal replacement_written
            identical = real_identity_check(source, target)
            if not identical and not replacement_written:
                replacement_written = True
                if os.path.exists(destination):
                    os.unlink(destination)
                with open(destination, "wb") as handle:
                    handle.write(b"second foreign artifact")
            return identical

        with mock.patch.object(
                owner, "_same_artifact_entry",
                side_effect=replace_while_quarantined):
            self.assertFalse(owner.rollback_link(staged, destination))

        with open(destination, "rb") as handle:
            self.assertEqual(handle.read(), b"second foreign artifact")
        recovery_paths = [
            os.path.join(root, name, "artifact")
            for name in os.listdir(root)
            if name.startswith(".pool-rollback-")
        ]
        self.assertEqual(len(recovery_paths), 1)
        with open(recovery_paths[0], "rb") as handle:
            self.assertEqual(handle.read(), b"first foreign artifact")

    def test_seed_pool_mutation_rollback_preserves_foreign_symlink(self):
        owner = organizer.seed_pool_mutations
        root = os.path.join(self.temp.name, "rollback-symlink-race")
        os.makedirs(root)
        staged = os.path.join(root, ".staged-pool")
        destination = os.path.join(root, "published.bspool")
        with open(staged, "wb") as handle:
            handle.write(b"our staged pool")
        try:
            os.symlink(staged, destination)
        except (NotImplementedError, OSError) as exc:
            self.skipTest("symbolic links unavailable: %s" % exc)

        self.assertFalse(owner.rollback_link(staged, destination))
        self.assertTrue(os.path.islink(destination))
        self.assertEqual(os.readlink(destination), staged)

    def test_inspect_groups_exact_variants_into_friendly_ordered_locations(self):
        perkeo_a2_small_shop = descriptor(
            2, "j_perkeo", 2, 1, 1, 0, 0)
        perkeo_a2_small_charm = descriptor(
            2, "j_perkeo", 2, 1, 2, 1, 1)
        perkeo_a2_big = descriptor(2, "j_perkeo", 2, 2, 1, 0, 0)
        perkeo_a2_boss = descriptor(2, "j_perkeo", 2, 0, 1, 0, 0)
        perkeo_a10_small = descriptor(2, "j_perkeo", 10, 1, 1, 0, 0)
        negative_tags = [
            descriptor(1, "tag_negative", ante, 1, 0, 0, 0)
            for ante in range(1, 6)
        ]
        path = os.path.join(self.temp.name, "friendly-locations.bspool")
        write_custom_bsp3(
            path, list(range(5)), [
                [perkeo_a2_small_shop, perkeo_a2_small_charm,
                 negative_tags[0]],
                [perkeo_a2_big, negative_tags[1]],
                [perkeo_a2_boss, negative_tags[2]],
                [perkeo_a10_small, negative_tags[3]],
                [perkeo_a10_small, negative_tags[4]],
            ],
            "1234567890abcdef", [
                "tag_route collect",
                "tag tag_negative 1 small 5 small 1",
                "legendary j_perkeo 2 small 10 small 1 shop",
            ])

        report = organizer.analyze(organizer.BSPoolReader(path))
        filters = {
            row["filter_id"]: row for row in report["filters"]
        }
        self.assertEqual(
            report["recommended_filter_id"], "legendary:j_perkeo")
        self.assertEqual(filters["legendary:j_perkeo"]["label"], "Perkeo")
        self.assertEqual(
            [row["label"]
             for row in filters["legendary:j_perkeo"]["locations"]],
            [
                "Perkeo Ante 2 Small",
                "Perkeo Ante 2 Big",
                "Perkeo Ante 2 Boss",
                "Perkeo Ante 10 Small",
            ])

        # Source, ordinal, and flag variants are retained as exact metadata,
        # but collapse into one new-user-facing Ante/blind location.
        small = filters["legendary:j_perkeo"]["locations"][0]
        self.assertEqual(small["records"], 1)
        self.assertEqual(len(small["exact_category_ids"]), 2)
        self.assertTrue(any(":shop:o0:none" in category
                            for category in small["exact_category_ids"]))
        self.assertTrue(any(":charm:o1:negative" in category
                            for category in small["exact_category_ids"]))
        self.assertEqual(
            filters["legendary:j_perkeo"]["multiple_location_records"], 0)
        self.assertEqual(
            filters["legendary:j_perkeo"]["location_associations"], 5)
        self.assertEqual(filters["tag:tag_negative"]["location_count"], 5)

    def test_settable_space_maps_every_rank_without_zero(self):
        self.assertEqual(organizer.rank_to_seed(0, organizer.SETTABLE_CHARSET), "1")
        self.assertEqual(organizer.rank_to_seed(
            organizer.SETTABLE_CHARSET.index("O"), organizer.SETTABLE_CHARSET), "O")
        self.assertEqual(organizer.rank_to_seed(35, organizer.SETTABLE_CHARSET), "11")
        self.assertEqual(organizer.rank_to_seed(
            organizer.SETTABLE_SEEDSPACE - 1, organizer.SETTABLE_CHARSET),
            "ZZZZZZZZ")

        path = os.path.join(self.temp.name, "settable.bspool")
        write_bsp3(path, charset=organizer.SETTABLE_CHARSET,
                   seedspace=organizer.SETTABLE_SEEDSPACE, space="settable")
        reader = organizer.BSPoolReader(path)
        self.assertEqual(reader.space_name, "settable")
        self.assertEqual(reader.space_index, 2)
        seeds = [reader.seed(record.rank)
                 for record in reader.iter_records()]
        self.assertEqual(seeds, ["1", "2", "3", "4"])
        self.assertFalse(any("0" in seed for seed in seeds))

    def test_split_requires_choice_and_unmatched_policy_then_roundtrips(self):
        output_dir = os.path.join(self.temp.name, "split")
        report_path = os.path.join(self.temp.name, "plan.json")
        self.run_tool("split", self.source, output_dir, "--report", report_path,
                      expected=2)
        with open(report_path, "r", encoding="utf-8") as handle:
            plan = json.load(handle)
        self.assertEqual(plan["unresolved_ambiguities"], 1)
        self.assertEqual(plan["unmatched_count"], 1)
        self.assertFalse([name for name in os.listdir(output_dir)
                          if name.endswith(".bspool")])
        ambiguity = plan["ambiguous"][0]
        self.assertEqual(plan["source_snapshot_id"], plan["source"]["snapshot_id"])
        self.assertEqual(plan["choices"], {ambiguity["seed"]: ""})
        legendary_category = [value for value in ambiguity["candidates"]
                              if value.startswith("legendary:")][0]
        choices_path = os.path.join(self.temp.name, "choices.json")
        with open(choices_path, "w", encoding="utf-8") as handle:
            json.dump({
                "source_snapshot_id": plan["source"]["snapshot_id"],
                "choices": {ambiguity["seed"]: legendary_category},
            }, handle)
        locked_output = os.path.join(
            output_dir, organizer.safe_filename(legendary_category))
        with organizer.pool_writer_guard(locked_output):
            with self.assertRaisesRegex(ValueError, "currently being written"):
                organizer.split_pool(
                    organizer.BSPoolReader(self.source), output_dir, None,
                    choices_path, report_path, "Needs review", False)
        self.assertFalse([name for name in os.listdir(output_dir)
                          if name.endswith(".bspool")])
        result = self.run_tool("split", self.source, output_dir,
                               "--report", report_path, "--choices", choices_path,
                               "--remainder", "Needs review")
        self.assertIn("into 4 pool(s)", result.stdout)
        with open(report_path, "r", encoding="utf-8") as handle:
            finished = json.load(handle)
        self.assertEqual(len(finished["outputs"]), 4)
        by_category = {item["category_id"]: item for item in finished["outputs"]}
        self.assertEqual(sum(item["records"] for item in finished["outputs"]), 4)
        self.assertEqual(by_category[legendary_category]["records"], 1)
        self.assertEqual(organizer.BSPoolReader(self.source).schema, 3)
        for output in finished["outputs"]:
            split_reader = organizer.BSPoolReader(output["path"])
            self.assertEqual(split_reader.schema, 4)
            self.assertEqual(split_reader.encoding, "adaptive-events-v1")

        legendary_reader = organizer.BSPoolReader(by_category[legendary_category]["path"])
        self.assertEqual(legendary_reader.schema, 4)
        self.assertEqual(legendary_reader.encoding, "adaptive-events-v1")
        legendary_records = list(legendary_reader.iter_records())
        self.assertEqual([item.rank for item in legendary_records], [1])
        # Selecting the Legendary category must not erase its tag/voucher
        # annotations. Future organizer passes still see the complete truth.
        self.assertEqual({item.raw for item in legendary_records[0].occurrences},
                         {TAG, LEGENDARY, VOUCHER})
        self.assertTrue(legendary_reader.complete)
        self.assertFalse(legendary_reader.coverage_complete)
        self.assertEqual(legendary_reader.family_id,
                         int(self.identity["family"], 16))
        self.assertEqual(legendary_reader.header.one("parent_snapshot_id"),
                         self.identity["snapshot"])
        self.assertEqual(legendary_reader.header.one("parent_segment_id"),
                         self.identity["segment"])
        self.assertEqual(legendary_reader.header.integer("parent_records"), 4)

        # The native reader is the final compatibility oracle for files made
        # by this independent Python writer.
        binary = os.path.join(ROOT, "native", "brainstorm_seed_pool" +
                              (".exe" if os.name == "nt" else ""))
        if os.path.isfile(binary):
            for output in finished["outputs"]:
                exported = output["path"] + ".txt"
                native = subprocess.run([binary, "export", output["path"], exported],
                                        text=True, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE)
                self.assertEqual(native.returncode, 0, native.stderr)
                with open(exported, "r", encoding="ascii") as handle:
                    seeds = [line.strip() for line in handle if line.strip()]
                self.assertEqual(len(seeds), output["records"])

    def test_cli_handles_split_policy_errors_without_a_traceback(self):
        choices_path = os.path.join(self.temp.name, "unused-choices.json")
        with open(choices_path, "w", encoding="utf-8") as handle:
            json.dump({
                "source_snapshot_id": self.identity["snapshot"],
                "choices": {
                    "UNUSED": organizer.Occurrence.decode(TAG).category_id,
                },
            }, handle)

        result = self.run_tool(
            "split", self.source, os.path.join(self.temp.name, "unused"),
            "--choices", choices_path, "--omit-unmatched", expected=1)
        self.assertIn("organizer error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_matching_copy_policy_and_cli_publish_every_overlap(self):
        reader = organizer.BSPoolReader(self.source)
        inspection = organizer.analyze(reader, ambiguity_limit=None)
        selected = [row["category_id"] for row in inspection["categories"]]
        spec = organizer.split_policy.SplitSpec.create(
            "matching_copies", selected)
        policy = organizer.split_policy.PoolSplitPolicy(spec)
        reviewed = policy.review(reader.iter_records(), reader.seed)

        self.assertEqual(reviewed.overlap_records, 1)
        self.assertEqual(reviewed.unmatched_records, 1)
        self.assertEqual(reviewed.unique_copied_records, 3)
        self.assertEqual(reviewed.output_memberships, 5)
        self.assertEqual(reviewed.unresolved_records, 0)
        self.assertEqual(sorted(reviewed.destinations().values()), [1, 2, 2])

        # Repeated occurrences of one destination never duplicate a seed
        # within that destination pool.
        repeated = organizer.Record(
            0, (organizer.Occurrence.decode(TAG),
                organizer.Occurrence.decode(TAG)))
        distribution = policy.distribute(repeated, reader.seed(0))
        self.assertEqual(len(distribution.destinations), 1)

        output_dir = os.path.join(self.temp.name, "matching-copies")
        report_path = os.path.join(self.temp.name, "matching-copies.json")
        with open(self.source, "rb") as handle:
            source_before = handle.read()
        self.run_tool(
            "split", self.source, output_dir, "--copy-overlaps",
            "--report", report_path)
        with open(report_path, "r", encoding="utf-8") as handle:
            report = json.load(handle)
        self.assertEqual(report["assignment_mode"], "matching_copies")
        self.assertEqual(report["overlap_records"], 1)
        self.assertEqual(report["unique_copied_records"], 3)
        self.assertEqual(report["output_memberships"], 5)
        self.assertEqual(report["unmatched_policy"], "omit")
        self.assertEqual(len(report["outputs"]), 3)
        self.assertEqual(sum(row["records"] for row in report["outputs"]), 5)
        with open(self.source, "rb") as handle:
            self.assertEqual(handle.read(), source_before)

        source_overlap = next(
            record for record in organizer.BSPoolReader(self.source).iter_records()
            if record.rank == 1)
        containing_overlap = []
        for output in report["outputs"]:
            records = list(organizer.BSPoolReader(output["path"]).iter_records())
            matching = [record for record in records if record.rank == 1]
            if matching:
                containing_overlap.append(matching[0])
        self.assertEqual(len(containing_overlap), 3)
        for copied in containing_overlap:
            self.assertEqual(
                [item.raw for item in copied.occurrences],
                [item.raw for item in source_overlap.occurrences])

        incompatible = self.run_tool(
            "split", self.source, os.path.join(self.temp.name, "invalid-mode"),
            "--copy-overlaps", "--choices", "choices.json", expected=2)
        self.assertIn("not allowed with argument", incompatible.stderr)

    def test_matching_copy_split_never_decodes_seed_text(self):
        reader = organizer.BSPoolReader(self.source)
        output_dir = os.path.join(self.temp.name, "rank-only-matching")
        report_path = os.path.join(self.temp.name, "rank-only-matching.json")

        with mock.patch.object(
                reader, "seed",
                side_effect=AssertionError("matching copies decoded a seed")):
            report, completed = organizer.split_pool(
                reader, output_dir, None, None, report_path, None, False,
                assignment_mode=organizer.split_policy.MODE_MATCHING_COPIES)

        self.assertTrue(completed)
        self.assertEqual(report["unique_copied_records"], 3)
        self.assertEqual(report["output_memberships"], 5)

    def test_split_policy_modes_decisions_and_other_seed_output(self):
        tag = organizer.Occurrence.decode(TAG)
        legendary = organizer.Occurrence.decode(LEGENDARY)
        tag_id = tag.category_id
        legendary_id = legendary.category_id
        records = [
            organizer.Record(0, tuple()),
            organizer.Record(1, (tag,)),
            organizer.Record(2, (tag, legendary)),
            organizer.Record(3, (tag, tag)),
        ]
        seed = lambda rank: "seed%d" % rank

        matching_spec = organizer.split_policy.SplitSpec.create(
            "matching_copies", [tag_id, legendary_id],
            remainder_id="remainder:Other")
        matching = organizer.split_policy.PoolSplitPolicy(
            matching_spec).review(records, seed)
        self.assertEqual(matching.unmatched_records, 1)
        self.assertEqual(matching.overlap_records, 1)
        self.assertEqual(matching.unique_copied_records, 4)
        self.assertEqual(matching.output_memberships, 5)
        self.assertEqual(matching.destinations(), {
            tag_id: 3,
            legendary_id: 1,
            "remainder:Other": 1,
        })

        rule_key = organizer.split_policy.ambiguity_rule_key(
            [tag_id, legendary_id])
        exclusive_spec = organizer.split_policy.SplitSpec.create(
            "exclusive", [tag_id, legendary_id],
            choices={"seed2": tag_id},
            ambiguity_rules={rule_key: legendary_id},
            remainder_id="remainder:Other")
        exclusive_policy = organizer.split_policy.PoolSplitPolicy(
            exclusive_spec)
        direct = exclusive_policy.distribute(records[2], seed(2))
        self.assertEqual(direct.destinations, (tag_id,))
        self.assertFalse(direct.resolved_by_rule)
        exclusive = exclusive_policy.review(records, seed)
        self.assertEqual(exclusive.destinations(), {
            tag_id: 3,
            "remainder:Other": 1,
        })

        shared_spec = organizer.split_policy.SplitSpec.create(
            "exclusive", [tag_id, legendary_id],
            ambiguity_rules={rule_key: legendary_id})
        shared = organizer.split_policy.PoolSplitPolicy(
            shared_spec).review(records, seed)
        self.assertEqual(shared.destinations(), {
            tag_id: 2,
            legendary_id: 1,
        })
        with self.assertRaisesRegex(
                organizer.split_policy.SplitPolicyError, "not used"):
            organizer.split_policy.PoolSplitPolicy(
                organizer.split_policy.SplitSpec.create(
                    "exclusive", [tag_id, legendary_id],
                    choices={"missing-seed": tag_id})).review(records, seed)
        with self.assertRaisesRegex(
                organizer.split_policy.SplitPolicyError,
                "does not accept exclusive"):
            organizer.split_policy.SplitSpec.create(
                "matching_copies", [tag_id], choices={"seed1": tag_id})

    def _mixed_filter_sources(self, paused_second=False):
        first = os.path.join(self.temp.name, "tag-filter.bspool")
        second = os.path.join(self.temp.name, "legendary-filter.bspool")
        write_custom_bsp3(
            first, [1, 3, 5], [[TAG], [TAG], [TAG]],
            "1111111111111111", [
                "tag_route collect",
                "tag tag_negative 1 small 4 big 1",
            ])
        write_custom_bsp3(
            second, [2, 3, 6], [[LEGENDARY], [LEGENDARY], [LEGENDARY]],
            "2222222222222222", [
                "tag_route observe",
                "legendary j_perkeo 1 small 4 big 0 shop",
                "soul_depth any",
            ], complete=not paused_second,
            coverage_complete=not paused_second)
        return first, second

    def test_combine_respects_native_output_writer_lock(self):
        first, second = self._mixed_filter_sources()
        output = os.path.join(self.temp.name, "locked-union.bspool")
        with organizer.pool_writer_guard(output):
            with self.assertRaisesRegex(ValueError, "currently being written"):
                organizer.combine_pools(
                    [organizer.BSPoolReader(first), organizer.BSPoolReader(second)],
                    output, "union", "Locked output")
        self.assertFalse(os.path.exists(output))
        self.assertTrue(os.path.isfile(output + ".writer.lock"))

    def test_combine_creates_output_directory_before_locking(self):
        first, second = self._mixed_filter_sources()
        output = os.path.join(
            self.temp.name, "new", "nested", "union.bspool")
        result = organizer.combine_pools(
            [organizer.BSPoolReader(first), organizer.BSPoolReader(second)],
            output, "union", "Nested output")
        self.assertEqual(result["records"], 5)
        self.assertTrue(os.path.isfile(output))
        self.assertTrue(os.path.isfile(output + ".writer.lock"))

    def test_combine_reuses_decoded_descriptors_across_every_record(self):
        first, second = self._mixed_filter_sources()
        readers = [
            organizer.BSPoolReader(first),
            organizer.BSPoolReader(second),
        ]
        output = os.path.join(self.temp.name, "descriptor-reuse.bspool")
        original_descriptor = organizer.Occurrence.__dict__["decode"]
        original_decode = organizer.Occurrence.decode
        decoded = []

        def counted_decode(raw):
            decoded.append(raw)
            return original_decode(raw)

        organizer.Occurrence.decode = staticmethod(counted_decode)
        try:
            result = organizer.combine_pools(
                readers, output, "union", "Descriptor reuse")
        finally:
            organizer.Occurrence.decode = original_descriptor

        self.assertEqual(result["records"], 5)
        # Each source block decodes its one descriptor once. One branch and
        # one operand descriptor per input are then synthesized once; none is
        # decoded again for every normalized/output record.
        self.assertEqual(len(decoded), 3 * len(readers))
        self.assertEqual(sum(
            organizer.provenance_branch_id(raw) is not None
            or organizer.operand_id_from_descriptor(raw) is not None
            for raw in decoded), 2 * len(readers))
        combined = organizer.BSPoolReader(output)
        self.assertEqual(
            [record.rank for record in combined.iter_records()],
            [1, 2, 3, 5, 6])

    def test_union_different_filters_deduplicates_and_preserves_provenance(self):
        first, second = self._mixed_filter_sources()
        output = os.path.join(self.temp.name, "union.bspool")
        result = organizer.combine_pools(
            [organizer.BSPoolReader(first), organizer.BSPoolReader(second)],
            output, "union", "Tag OR Perkeo")
        self.assertEqual(result["records"], 5)
        self.assertTrue(result["coverage_complete"])
        self.assertEqual(result["branch_count"], 2)

        reader = organizer.BSPoolReader(output)
        self.assertEqual(reader.schema, 4)
        self.assertEqual(reader.encoding, "adaptive-events-v1")
        self.assertTrue(reader.is_composite)
        self.assertEqual(reader.composite_operation, "union")
        self.assertEqual(len(reader.composite_branches), 2)
        self.assertEqual(len(reader.composite_operands), 2)
        self.assertEqual(reader.composite_expression["op"], "union")
        self.assertFalse(reader.header.values.get("tag"))
        self.assertFalse(reader.header.values.get("legendary"))
        records = {record.rank: record for record in reader.iter_records()}
        self.assertEqual(sorted(records), [1, 2, 3, 5, 6])
        duplicate = records[3]
        self.assertEqual({item.raw for item in duplicate.occurrences
                          if item.known}, {TAG, LEGENDARY})
        self.assertEqual(len({item.provenance_id for item in duplicate.occurrences
                              if item.is_provenance}), 2)
        self.assertEqual(len({item.operand_id for item in duplicate.occurrences
                              if item.is_operand}), 2)
        self.assertTrue(all(
            any(item.is_provenance for item in record.occurrences)
            for record in records.values()))
        analysis = organizer.analyze(reader)
        self.assertEqual(analysis["records_without_provenance"], 0)
        self.assertEqual(sum(analysis["provenance_counts"].values()), 6)

        # A forged expression may not repeat one input while silently omitting
        # another declared operand. Header metadata is deliberately mutable,
        # so the reader must enforce this relationship itself.
        with open(output, "rb") as handle:
            corrupted = bytearray(handle.read())
        expression_line = next(
            raw for key, _value, raw in reader.header.lines
            if key == "composite_expression")
        encoded = expression_line.split(None, 1)[1]
        expression = json.loads(organizer._decode_header_token(encoded))
        expression["inputs"][1] = dict(expression["inputs"][0])
        replacement = "composite_expression " + organizer._header_token(
            json.dumps(expression, sort_keys=True, separators=(",", ":")))
        self.assertEqual(len(replacement), len(expression_line))
        corrupted[:reader.header_bytes] = bytes(
            corrupted[:reader.header_bytes]).replace(
                expression_line.encode("ascii"), replacement.encode("ascii"), 1)
        malformed = os.path.join(self.temp.name, "malformed-expression.bspool")
        with open(malformed, "wb") as handle:
            handle.write(corrupted)
        with self.assertRaisesRegex(organizer.PoolError, "expression disagrees"):
            organizer.BSPoolReader(malformed)

        count_mismatch = os.path.join(
            self.temp.name, "malformed-input-count.bspool")
        with open(output, "rb") as handle:
            corrupted = handle.read().replace(
                b"composite_inputs 2\n", b"composite_inputs 3\n", 1)
        with open(count_mismatch, "wb") as handle:
            handle.write(corrupted)
        with self.assertRaisesRegex(organizer.PoolError,
                                    "composite_inputs disagrees"):
            organizer.BSPoolReader(count_mismatch)

        # A later category split must keep the branch dictionary alongside the
        # selected records' provenance descriptors.
        split_dir = os.path.join(self.temp.name, "union-split")
        report, completed = organizer.split_pool(
            reader, split_dir, [organizer.Occurrence.decode(TAG).category_id],
            None, None, None, True)
        self.assertTrue(completed)
        derived = organizer.BSPoolReader(report["outputs"][0]["path"])
        self.assertTrue(derived.is_composite)
        self.assertEqual(set(derived.composite_branches),
                         set(reader.composite_branches))

        binary = os.path.join(ROOT, "native", "brainstorm_seed_pool" +
                              (".exe" if os.name == "nt" else ""))
        if os.path.isfile(binary):
            exported = output + ".txt"
            native = subprocess.run([binary, "export", output, exported],
                                    text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            self.assertEqual(native.returncode, 0, native.stderr)
            with open(exported, "r", encoding="ascii") as handle:
                self.assertEqual(len([line for line in handle if line.strip()]), 5)

    def test_intersection_difference_empty_and_nested_combine(self):
        first, second = self._mixed_filter_sources()
        readers = [organizer.BSPoolReader(first), organizer.BSPoolReader(second)]
        intersection = os.path.join(self.temp.name, "intersection.bspool")
        organizer.combine_pools(readers, intersection, "intersection", "Both")
        both = organizer.BSPoolReader(intersection)
        self.assertEqual([record.rank for record in both.iter_records()], [3])
        self.assertEqual(len([item for item in next(both.iter_records()).occurrences
                              if item.is_provenance]), 2)

        difference = os.path.join(self.temp.name, "difference.bspool")
        organizer.combine_pools(readers, difference, "difference", "Tag only")
        difference_reader = organizer.BSPoolReader(difference)
        self.assertEqual([record.rank for record in
                          difference_reader.iter_records()], [1, 5])
        self.assertTrue(all(len([item for item in record.occurrences
                                 if item.is_operand]) == 1
                            for record in difference_reader.iter_records()))

        third = os.path.join(self.temp.name, "voucher-filter.bspool")
        write_custom_bsp3(
            third, [3, 7], [[VOUCHER], [VOUCHER]], "3333333333333333",
            ["tag_route observe", "voucher v_overstock_norm 1 4"])
        nested_path = os.path.join(self.temp.name, "nested.bspool")
        organizer.combine_pools(
            [organizer.BSPoolReader(intersection), organizer.BSPoolReader(third)],
            nested_path, "union", "Nested union")
        nested = organizer.BSPoolReader(nested_path)
        self.assertEqual(len(nested.composite_branches), 3)
        # The exact intersection result is one atomic operand in this later
        # union. It is not flattened into the incorrect A OR B OR C equation.
        self.assertEqual(len(nested.composite_operands), 2)
        self.assertEqual(nested.composite_expression["op"], "union")
        self.assertEqual(len(nested.composite_expression["inputs"]), 2)
        nested_records = {record.rank: record for record in nested.iter_records()}
        self.assertEqual(sorted(nested_records), [3, 7])
        self.assertEqual(len([item for item in nested_records[3].occurrences
                              if item.is_provenance]), 3)

        disjoint = os.path.join(self.temp.name, "disjoint.bspool")
        write_custom_bsp3(
            disjoint, [8], [[VOUCHER]], "4444444444444444",
            ["tag_route observe", "voucher v_overstock_norm 1 4"])
        empty_path = os.path.join(self.temp.name, "empty.bspool")
        organizer.combine_pools(
            [organizer.BSPoolReader(first), organizer.BSPoolReader(disjoint)],
            empty_path, "intersection", "Empty intersection")
        empty = organizer.BSPoolReader(empty_path)
        self.assertEqual(empty.records, 0)
        self.assertEqual(list(empty.iter_records()), [])
        self.assertTrue(empty.complete)

    def test_partial_literal_combine_is_provisional_and_incompatibilities_fail(self):
        first, second = self._mixed_filter_sources(paused_second=True)
        output = os.path.join(self.temp.name, "partial-union.bspool")
        result = organizer.combine_pools(
            [organizer.BSPoolReader(first), organizer.BSPoolReader(second)],
            output, "union", "Current snapshots")
        self.assertFalse(result["coverage_complete"])
        combined = organizer.BSPoolReader(output)
        self.assertTrue(combined.complete)
        self.assertFalse(combined.coverage_complete)
        self.assertEqual([record.rank for record in combined.iter_records()],
                         [1, 2, 3, 5, 6])

        incompatible = os.path.join(self.temp.name, "foreign-catalog.bspool")
        write_custom_bsp3(
            incompatible, [9], [[TAG]], "5555555555555555",
            ["tag_route collect", "tag tag_negative 1 small 4 big 1"],
            catalog_hash="bbbbbbbbbbbbbbbb")
        with self.assertRaisesRegex(organizer.PoolError, "catalog/profile"):
            organizer.prepare_combine([
                organizer.BSPoolReader(first),
                organizer.BSPoolReader(incompatible),
            ], "union")

        old = os.path.join(self.temp.name, "old-for-union.bspool")
        write_bsp2(old, complete=True)
        mixed_path = os.path.join(self.temp.name, "mixed-schema.bspool")
        mixed = organizer.combine_pools([
            organizer.BSPoolReader(first), organizer.BSPoolReader(old),
        ], mixed_path, "union", "BSP2 plus BSP3")
        self.assertFalse(mixed["metadata_complete"])
        mixed_reader = organizer.BSPoolReader(mixed_path)
        self.assertEqual(len(mixed_reader.composite_branches), 2)
        self.assertTrue(all(any(item.is_provenance for item in record.occurrences)
                            for record in mixed_reader.iter_records()))

        nested_mixed_path = os.path.join(self.temp.name, "nested-mixed-schema.bspool")
        nested_mixed = organizer.combine_pools([
            mixed_reader, organizer.BSPoolReader(second),
        ], nested_mixed_path, "union", "Nested missing metadata")
        self.assertFalse(nested_mixed["metadata_complete"])

    def test_sixty_four_input_limit_streams_and_scales_the_header(self):
        paths = []
        for index in range(organizer.COMPOSITE_MAX_INPUTS):
            path = os.path.join(self.temp.name, "source-%02d.bspool" % index)
            write_custom_bsp3(
                path, [index + 20, 1000], [[TAG], [TAG]],
                "%016x" % (0x1000 + index), [
                    "tag_route collect",
                    "tag tag_negative 1 small 4 big 1",
                ], range_end=2000)
            paths.append(path)
        readers = [organizer.BSPoolReader(path) for path in paths]
        output = os.path.join(self.temp.name, "sixty-four-way-union.bspool")
        result = organizer.combine_pools(
            readers, output, "union", "Maximum input union")
        self.assertEqual(result["input_count"], organizer.COMPOSITE_MAX_INPUTS)
        self.assertEqual(result["records"], organizer.COMPOSITE_MAX_INPUTS + 1)
        combined = organizer.BSPoolReader(output)
        self.assertEqual(len(combined.composite_operands),
                         organizer.COMPOSITE_MAX_INPUTS)
        self.assertEqual(len(combined.composite_branches),
                         organizer.COMPOSITE_MAX_INPUTS)
        self.assertGreater(combined.header_bytes, organizer.HEADER_EVENTS_BYTES)
        text_bytes = len(organizer.read_pool_header_text(output).encode("latin-1"))
        self.assertGreaterEqual(combined.header_bytes - text_bytes,
                                organizer.COMPOSITE_HEADER_SPARE_BYTES)
        common = next(record for record in combined.iter_records()
                      if record.rank == 1000)
        self.assertEqual(len([item for item in common.occurrences
                              if item.is_operand]),
                         organizer.COMPOSITE_MAX_INPUTS)
        self.assertEqual(len([item for item in common.occurrences
                              if item.is_provenance]),
                         organizer.COMPOSITE_MAX_INPUTS)

        binary = os.path.join(ROOT, "native", "brainstorm_seed_pool" +
                              (".exe" if os.name == "nt" else ""))
        if os.path.isfile(binary):
            exported = output + ".txt"
            native = subprocess.run([binary, "export", output, exported],
                                    text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
            self.assertEqual(native.returncode, 0, native.stderr)
            with open(exported, "r", encoding="ascii") as handle:
                self.assertEqual(len([line for line in handle if line.strip()]),
                                 organizer.COMPOSITE_MAX_INPUTS + 1)

        with self.assertRaisesRegex(organizer.PoolError, "at most 64"):
            organizer.prepare_combine(readers + [readers[0]], "union")

    def test_native_style_out_of_order_blocks_are_globally_streamed(self):
        unordered_path = os.path.join(self.temp.name, "unordered.bspool")
        write_out_of_order_bsp3(unordered_path)
        unordered = organizer.BSPoolReader(unordered_path)
        self.assertEqual([record.rank for record in unordered.iter_records()],
                         [1, 2, 4, 5])

        other_path = os.path.join(self.temp.name, "ordered-other.bspool")
        write_custom_bsp3(
            other_path, [3, 6], [[TAG], [TAG]], "8888888888888888", [
                "tag_route collect",
                "tag tag_negative 1 small 4 big 1",
            ])
        combined_path = os.path.join(self.temp.name, "ordered-union.bspool")
        organizer.combine_pools(
            [unordered, organizer.BSPoolReader(other_path)], combined_path,
            "union", "Ordered union")
        combined = organizer.BSPoolReader(combined_path)
        self.assertEqual([record.rank for record in combined.iter_records()],
                         [1, 2, 3, 4, 5, 6])

        category = organizer.Occurrence.decode(TAG).category_id
        split_dir = os.path.join(self.temp.name, "unordered-split")
        report, completed = organizer.split_pool(
            unordered, split_dir, [category], None, None, None, False)
        self.assertTrue(completed)
        split = organizer.BSPoolReader(report["outputs"][0]["path"])
        self.assertEqual([record.rank for record in split.iter_records()],
                         [1, 2, 4, 5])

        disjoint_path = os.path.join(
            self.temp.name, "unordered-disjoint.bspool")
        write_out_of_order_bsp3(disjoint_path, overlapping=False)
        disjoint = organizer.BSPoolReader(disjoint_path)
        read_order = []
        original_read = disjoint._read_block_records

        def tracked_read(handle, block):
            read_order.append(block.first_rank)
            return original_read(handle, block)

        disjoint._read_block_records = tracked_read
        self.assertEqual(
            [record.rank for record in disjoint.iter_records()],
            [1, 2, 4, 5])
        self.assertEqual(read_order, [1, 4])

    def test_corruption_is_rejected_inside_committed_boundary(self):
        corrupt = os.path.join(self.temp.name, "corrupt.bspool")
        with open(self.source, "rb") as handle:
            raw = bytearray(handle.read())
        raw[8192 + 48] ^= 0x01
        with open(corrupt, "wb") as handle:
            handle.write(raw)
        result = self.run_tool("inspect", corrupt, expected=1)
        self.assertRegex(result.stderr, r"checksum|rank payload")

    def test_damaged_bsp3_header_reports_block_offset_and_prefix(self):
        corrupt = os.path.join(
            self.temp.name, "corrupt-bsp3-header.bspool")
        with open(self.source, "rb") as handle:
            raw = bytearray(handle.read())
        raw[8192] ^= 0x01
        with open(corrupt, "wb") as handle:
            handle.write(raw)
        result = self.run_tool("inspect", corrupt, expected=1)
        self.assertIn(
            "source pool is damaged: BSP3 block 0 at byte 8192",
            result.stderr)
        self.assertRegex(
            result.stderr, r"first 8 bytes: [0-9a-f]{16}")

    def test_complete_bsp3_recovers_only_first_eight_header_bytes(self):
        corrupt = os.path.join(
            self.temp.name, "recoverable-complete-bsp3.bspool")
        write_bsp3(corrupt, complete=True)
        with open(corrupt, "rb") as handle:
            raw = bytearray(handle.read())
        raw[8192:8200] = b"DAMAGED!"
        damaged_bytes = bytes(raw)
        with open(corrupt, "wb") as handle:
            handle.write(damaged_bytes)

        # Recovery is allowed even for a lazy caller only because the reader
        # forces the full CRC/rank/metadata/pool-digest pass first.
        reader = organizer.BSPoolReader(corrupt, verify_payloads=False)
        self.assertTrue(reader._payload_verified)
        self.assertEqual(reader._repaired_bsp3_headers, {8192})
        self.assertEqual(
            [record.rank for record in reader.iter_records()],
            [0, 1, 2, 3])
        result = self.run_tool("inspect", corrupt, "--json")
        self.assertEqual(json.loads(result.stdout)["source"]["records"], 4)
        with open(corrupt, "rb") as handle:
            self.assertEqual(handle.read(), damaged_bytes)

    def test_complete_bsp3_rejects_damage_beyond_first_eight_bytes(self):
        corrupt = os.path.join(
            self.temp.name, "unrecoverable-complete-bsp3.bspool")
        write_bsp3(corrupt, complete=True)
        with open(corrupt, "rb") as handle:
            raw = bytearray(handle.read())
        raw[8192:8200] = b"DAMAGED!"
        raw[8192 + organizer.BLOCK3_HEADER_BYTES] ^= 0x01
        damaged_bytes = bytes(raw)
        with open(corrupt, "wb") as handle:
            handle.write(damaged_bytes)

        result = self.run_tool("inspect", corrupt, expected=1)
        self.assertIn("BSP3 block checksum differs at byte 8192",
                      result.stderr)
        with open(corrupt, "rb") as handle:
            self.assertEqual(handle.read(), damaged_bytes)

    def test_complete_bsp3_recovery_requires_whole_pool_digests(self):
        corrupt = os.path.join(
            self.temp.name, "digest-damaged-complete-bsp3.bspool")
        write_bsp3(corrupt, complete=True)
        with open(corrupt, "rb") as handle:
            raw = bytearray(handle.read())
        block_offset = 8192
        raw[block_offset:block_offset + 8] = b"DAMAGED!"
        rank_bytes, metadata_bytes = struct.unpack_from(
            "<II", raw, block_offset + 12)
        payload_offset = block_offset + organizer.BLOCK3_HEADER_BYTES
        metadata_offset = payload_offset + rank_bytes
        unknown_offset = raw.find(
            UNKNOWN, metadata_offset, metadata_offset + metadata_bytes)
        self.assertGreaterEqual(unknown_offset, metadata_offset)
        raw[unknown_offset + len(UNKNOWN) - 1] ^= 0x01

        # Make the local block CRC agree with the broader corruption. The
        # reconstructed block must still be rejected by the independently
        # committed whole-pool membership/metadata digests.
        recovered_header = (
            organizer.BSP3_HEADER_PREFIX
            + bytes(raw[block_offset + 8:
                        block_offset + organizer.BLOCK3_HEADER_BYTES]))
        payload_bytes = bytes(
            raw[payload_offset:
                payload_offset + rank_bytes + metadata_bytes])
        checksum = independent_crc64(recovered_header[4:40])
        checksum = independent_crc64(payload_bytes, checksum)
        struct.pack_into("<Q", raw, block_offset + 40, checksum)
        damaged_bytes = bytes(raw)
        with open(corrupt, "wb") as handle:
            handle.write(damaged_bytes)

        result = self.run_tool("inspect", corrupt, expected=1)
        self.assertRegex(
            result.stderr,
            r"membership_digest differs|metadata_digest differs")
        with open(corrupt, "rb") as handle:
            self.assertEqual(handle.read(), damaged_bytes)

    def test_complete_bsp3_recovers_at_most_one_header_prefix(self):
        corrupt = os.path.join(
            self.temp.name, "two-damaged-bsp3-headers.bspool")
        write_out_of_order_bsp3(corrupt, overlapping=True)
        clean = organizer.BSPoolReader(corrupt)
        self.assertGreaterEqual(len(clean.blocks), 2)
        with open(corrupt, "r+b") as handle:
            for block in clean.blocks[:2]:
                handle.seek(block.offset)
                handle.write(b"DAMAGED!")
        with open(corrupt, "rb") as handle:
            damaged_bytes = handle.read()
        result = self.run_tool("inspect", corrupt, expected=1)
        self.assertIn(
            "second invalid header prefix", result.stderr)
        with open(corrupt, "rb") as handle:
            self.assertEqual(handle.read(), damaged_bytes)

    def test_complete_bsp3_index_and_footer_are_verified(self):
        complete = os.path.join(self.temp.name, "complete.bspool")
        write_bsp3(complete, complete=True)
        reader = organizer.BSPoolReader(complete)
        self.assertTrue(reader.complete)
        self.assertEqual([record.rank for record in reader.iter_records()],
                         [0, 1, 2, 3])
        with open(complete, "rb") as handle:
            raw = bytearray(handle.read())
        raw[-1] ^= 1
        broken = os.path.join(self.temp.name, "broken-footer.bspool")
        with open(broken, "wb") as handle:
            handle.write(raw)
        result = self.run_tool("inspect", broken, expected=1)
        self.assertIn("footer checksum", result.stderr)

    def test_bsp2_is_safe_to_inspect_but_not_position_split(self):
        old = os.path.join(self.temp.name, "old.bspool")
        write_bsp2(old, complete=True)
        report = json.loads(self.run_tool("inspect", old, "--json").stdout)
        self.assertEqual(report["source"]["records"], 2)
        self.assertTrue(report["source"]["complete"])
        self.assertFalse(report["source"]["metadata_capable"])
        self.assertEqual(report["category_count"], 0)
        result = self.run_tool("split", old, os.path.join(self.temp.name, "old-split"),
                               expected=1)
        self.assertIn("no per-seed occurrence metadata", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
