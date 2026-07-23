#!/usr/bin/env python3
"""Focused regression tests for the no-rescan BSP3 organizer."""

import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest

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


def write_out_of_order_bsp3(path, overlapping=True):
    """Write two sorted blocks in deliberately wrong physical order."""
    specs = (
        [([1, 5], [[TAG], [TAG]]), ([2, 4], [[TAG], [TAG]])]
        if overlapping else
        [([4, 5], [[TAG], [TAG]]), ([1, 2], [[TAG], [TAG]])])
    encoded = [event_block(ranks, occurrences)
               for ranks, occurrences in specs]
    membership = FNV64_OFFSET
    metadata_digest = FNV64_OFFSET
    for block, metadata in encoded:
        membership = independent_fnv64(block, membership)
        metadata_digest = independent_fnv64(metadata, metadata_digest)
    segment = 0x7654321012345678
    data_bytes = sum(len(block) for block, _metadata in encoded)
    snapshot = hash_fields("snapshot", segment, 4, data_bytes, membership)
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
        "scan_cursor 100",
        "label Out of order blocks",
        "tag_route collect",
        "tag tag_negative 1 small 4 big 1",
        "records 4",
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
                         len(index_rows), 4, data_bytes, membership,
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
