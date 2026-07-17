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
