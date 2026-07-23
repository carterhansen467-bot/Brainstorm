#!/usr/bin/env python3
"""Focused tests for the production adaptive BSP4 pool codec."""

import os
import random
import shutil
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import brainstorm_pool_organizer as organizer
import brainstorm_pool_builder as builder
import pool_organizer_web as organizer_web


def descriptor(kind, key, ante=1, phase=1, source=0, ordinal=0, flags=0):
    key_bytes = key.encode("ascii")
    return bytes((kind, len(key_bytes))) + key_bytes + bytes(
        (ante, phase, source, ordinal, flags))


TAG = organizer.Occurrence.decode(descriptor(1, "tag_charm"))
LEGENDARY = organizer.Occurrence.decode(
    descriptor(2, "j_perkeo", ante=2, source=1, ordinal=1))
VOUCHER = organizer.Occurrence.decode(
    descriptor(3, "v_overstock_norm", ante=3, source=1, ordinal=1))
UNKNOWN = organizer.Occurrence.decode(bytes((0x90, 0x01, 0xA5)))


def independent_fnv64(payload, value=organizer.FNV64_OFFSET):
    for byte in payload:
        value = ((value ^ byte) * organizer.FNV64_PRIME) & organizer.MASK64
    return value


def independent_crc64(payload, value=0):
    for byte in payload:
        value ^= byte << 56
        for _ in range(8):
            value = (((value << 1) ^ 0x42F0E1EBA9EA3693)
                     if value & (1 << 63) else value << 1) & organizer.MASK64
    return value


def independent_rice_payload(ranks, k):
    bits = []
    for left, right in zip(ranks, ranks[1:]):
        value = right - left - 1
        bits.extend([0] * (value >> k))
        bits.append(1)
        bits.extend((value >> shift) & 1 for shift in range(k))
    output = bytearray(1 + (len(bits) + 7) // 8)
    output[0] = k
    for index, bit in enumerate(bits):
        output[1 + (index >> 3)] |= bit << (index & 7)
    return bytes(output)


def independent_best_rice_k(ranks):
    gaps = [right - left - 1 for left, right in zip(ranks, ranks[1:])]
    return min(
        (1 + (sum(value >> k for value in gaps)
              + len(gaps) * (k + 1) + 7) // 8, k)
        for k in range(organizer.BSP4_RICE_MAX_K + 1))[1]


class SourceStub:
    """Only the immutable lineage/header view required by output writers."""

    header_bytes = organizer.HEADER_EVENTS_BYTES
    modelver = 6
    charset = organizer.NATURAL_CHARSET
    seedspace = organizer.NATURAL_SEEDSPACE
    space_name = "natural"
    space_index = 0
    range_start = 0
    range_end = 20_000_000
    catalog_hash = 0xAAAAAAAAAAAAAAAA
    criteria_hash = 0xBBBBBBBBBBBBBBBB
    family_id = 0x1111222233334444
    segment_id = 0x2222333344445555
    lineage_id = 0x3333444455556666
    snapshot_id = 0x4444555566667777
    records = 4096
    data_bytes = 123456
    complete = 1
    coverage_complete = 1
    pool_id = "bsp4-test-source"
    is_composite = False

    header = organizer.PoolHeader(
        "stage_hash 5555666677778888\n"
        "scan_cursor 4096\n"
        "tag_route collect\n"
        "tag tag_charm 1 small 4 big 1\n"
        "end\n")

    @property
    def snapshot_token(self):
        return "%016x" % self.snapshot_id


def publish(writer, records):
    try:
        for record in records:
            writer.add(record)
        result = writer.finalize()
        os.replace(writer.temp_path, writer.final_path)
        return result
    except BaseException:
        writer.abort()
        raise


def copy_and_mutate(source, target, offset):
    shutil.copyfile(source, target)
    with open(target, "r+b") as handle:
        handle.seek(offset)
        byte = handle.read(1)
        if not byte:
            raise AssertionError("mutation offset is outside fixture")
        handle.seek(offset)
        handle.write(bytes((byte[0] ^ 0x01,)))


class BSP4CodecRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="brainstorm-bsp4-test-")
        self.source = SourceStub()

    def tearDown(self):
        self.temp.cleanup()

    def writer(self, name, schema=4):
        path = os.path.join(self.temp.name, name)
        cls = (organizer.BSP4OutputWriter
               if schema == 4 else organizer.BSP3OutputWriter)
        return path, cls(
            self.source, "tag:tag_charm:A1:small:none:o0:none",
            "BSP%d fixture" % schema, path)

    def test_production_surfaces_advertise_bsp4_native_use(self):
        path, writer = self.writer("surface-compatible.bspool")
        publish(writer, [
            organizer.Record(7, (TAG,)),
            organizer.Record(11, (TAG, LEGENDARY)),
        ])

        # The Organizer and Builder intentionally share one bounded
        # extended-header reader.
        self.assertIs(organizer.read_pool_header_text,
                      builder.read_pool_header_text)
        info = builder.PoolInfo(path).as_dict()
        self.assertEqual(info["schema"], 4)
        self.assertEqual(info["encoding"], "adaptive-events-v1")
        self.assertTrue(info["metadata_capable"])
        self.assertTrue(info["native_compatible"])
        self.assertFalse(info["native_incompatibility"])
        self.assertTrue(
            info["attachment_accelerator_eligible"],
            info["attachment_accelerator_blockers"])

        listed = organizer_web.list_sources(self.temp.name)
        row = next(item for item in listed
                   if item.get("name") == os.path.basename(path))
        self.assertEqual(row["schema"], 4)
        self.assertEqual(row["encoding"], "adaptive-events-v1")
        self.assertTrue(row["metadata_capable"])
        self.assertTrue(row["native_compatible"])

        verified = organizer.BSPoolReader(path)
        native_calls = []

        def native_summary(summary_path, cancel_check=None):
            native_calls.append(os.path.abspath(summary_path))
            self.assertIsNone(cancel_check)
            return {
                "records": 2,
                "membership_digest": "%016x" % verified.membership_digest,
                "metadata_digest": "%016x" % verified.metadata_digest,
                "record_metadata_digest": "0123456789abcdef",
                "categories": [(TAG.raw, 2), (LEGENDARY.raw, 1)],
                "filters": [
                    (1, b"tag_charm", 2, 0, 2),
                    (2, b"j_perkeo", 1, 0, 1),
                ],
                "locations": [
                    (1, b"tag_charm", 1, 1, 2),
                    (2, b"j_perkeo", 2, 1, 1),
                ],
                "ambiguous_count": 1,
                "unmatched_count": 0,
                "opaque_associations": 0,
                "records_without_provenance": 0,
                "records_without_operands": 0,
                "provenance_counts": {},
                "operand_counts": {},
            }

        old_minimum = organizer_web.NATIVE_SUMMARY_MIN_BYTES
        old_binary = organizer_web._native_pool_binary
        old_summary = organizer_web._run_native_summary
        organizer_web.NATIVE_SUMMARY_MIN_BYTES = 0
        organizer_web._native_pool_binary = lambda: "native-helper-present"
        organizer_web._run_native_summary = native_summary
        try:
            report = organizer_web.inspect_source(
                os.path.basename(path), self.temp.name)
        finally:
            organizer_web.NATIVE_SUMMARY_MIN_BYTES = old_minimum
            organizer_web._native_pool_binary = old_binary
            organizer_web._run_native_summary = old_summary
        self.assertEqual(native_calls, [os.path.abspath(path)])
        self.assertEqual(report["source"]["schema"], 4)
        self.assertTrue(report["source"]["metadata_capable"])
        self.assertEqual(report["source"]["records"], 2)

    def test_canonical_4096_blocks_and_legacy_1024_blocks_are_readable(self):
        self.assertEqual(organizer.BSP3_WRITE_RECORDS, 1024)
        self.assertEqual(organizer.BSP4_WRITE_RECORDS, 4096)
        self.assertEqual(organizer.BLOCK_MAX_RECORDS, 8192)
        records = [
            organizer.Record(index * 2, (TAG,) if index % 3 else (LEGENDARY,))
            for index in range(5000)
        ]

        canonical_path, canonical_writer = self.writer("canonical-4k.bspool")
        publish(canonical_writer, records)
        canonical = organizer.BSPoolReader(canonical_path)
        self.assertEqual([block.count for block in canonical.blocks],
                         [4096, 904])

        legacy_path, legacy_writer = self.writer("legacy-1k.bspool")
        legacy_writer.write_records = 1024
        publish(legacy_writer, records)
        legacy = organizer.BSPoolReader(legacy_path)
        self.assertEqual([block.count for block in legacy.blocks],
                         [1024, 1024, 1024, 1024, 904])

        bsp3_path, bsp3_writer = self.writer("unchanged-v3.bspool", schema=3)
        publish(bsp3_writer, records)
        bsp3 = organizer.BSPoolReader(bsp3_path)
        self.assertEqual([block.count for block in bsp3.blocks],
                         [1024, 1024, 1024, 1024, 904])

        expected = [record.rank for record in records]
        self.assertEqual([record.rank for record in canonical.iter_records()],
                         expected)
        self.assertEqual([record.rank for record in legacy.iter_records()],
                         expected)
        self.assertEqual([record.rank for record in bsp3.iter_records()],
                         expected)

    def test_adaptive_rank_and_descriptor_codec_selection_roundtrips(self):
        block_records = organizer.BSP4_WRITE_RECORDS
        sparse = [index * 1000 for index in range(block_records)]
        dense_start = sparse[-1] + 1000
        dense = [
            rank for rank in range(dense_start, dense_start + block_records + 2)
            if rank not in (dense_start + 20, dense_start + 700)
        ]
        bitmap_start = dense[-1] + 1000
        bitmap = [
            bitmap_start + index * 2 for index in range(block_records)
        ]
        ranks = sparse + dense + bitmap

        records = []
        for absolute, rank in enumerate(ranks):
            block = absolute // block_records
            index = absolute % block_records
            occurrences = []
            if block == 0 and index in (0, 100):
                occurrences.append(TAG)
            if block == 1 and index not in (20, 700):
                occurrences.append(LEGENDARY)
            if block == 2 and index % 2 == 0:
                occurrences.append(VOUCHER)
            if block == 2 and (100 <= index < 300 or 600 <= index < 900):
                occurrences.append(UNKNOWN)
            records.append(organizer.Record(rank, tuple(occurrences)))

        path, writer = self.writer("adaptive.bspool")
        publish(writer, records)
        reader = organizer.BSPoolReader(path)
        self.assertEqual(reader.schema, 4)
        self.assertEqual(
            [block.rank_codec for block in reader.blocks],
            [organizer.BSP4_RANK_RICE,
             organizer.BSP4_RANK_COMPLEMENT,
             organizer.BSP4_RANK_BITMAP])
        decoded = list(reader.iter_records())
        self.assertEqual([record.rank for record in decoded], ranks)
        self.assertEqual(
            [[item.raw for item in record.occurrences] for record in decoded],
            [[item.raw for item in record.occurrences] for record in records])

        cases = [
            ([0, 100], organizer.BSP4_META_POSITIVE),
            (list(range(128)), organizer.BSP4_META_COMPLEMENT),
            (list(range(0, 128, 2)), organizer.BSP4_META_BITMAP),
            (list(range(20, 80)), organizer.BSP4_META_RUNS),
        ]
        for indexes, expected_codec in cases:
            codec, payload = organizer._encode_adaptive_indexes(indexes, 128)
            self.assertEqual(codec, expected_codec)
            decoded_indexes, at = organizer._decode_bsp4_indexes(
                payload, 0, codec, len(indexes), 128)
            self.assertEqual(decoded_indexes, indexes)
            self.assertEqual(at, len(payload))

    def test_rice_oracle_randomized_roundtrip_and_global_selection(self):
        generator = random.Random(0xB5F40003)
        for case in range(200):
            count = generator.randint(2, 96)
            ranks = [generator.randint(0, 1000)]
            scale = 1 << generator.randint(0, 14)
            for _ in range(count - 1):
                ranks.append(
                    ranks[-1] + generator.randint(1, max(2, scale)))

            expected_k = independent_best_rice_k(ranks)
            k, payload = organizer._encode_rice_ranks(ranks)
            self.assertEqual(k, expected_k, "case %d" % case)
            self.assertEqual(
                payload, independent_rice_payload(ranks, expected_k),
                "case %d" % case)
            self.assertEqual(organizer._decode_rank_codec(
                payload, len(ranks), ranks[0], ranks[-1],
                organizer.BSP4_RANK_RICE), ranks)

            positive = organizer._positive_rank_payload(ranks)
            candidates = [
                (len(positive), organizer.BSP4_RANK_POSITIVE)]
            complement = organizer._complement_rank_payload(
                ranks, len(positive))
            if complement is not None:
                candidates.append((
                    len(complement), organizer.BSP4_RANK_COMPLEMENT))
            bitmap_bytes = (ranks[-1] - ranks[0] + 8) // 8
            if bitmap_bytes < len(positive):
                candidates.append((
                    bitmap_bytes, organizer.BSP4_RANK_BITMAP))
            candidates.append((len(payload), organizer.BSP4_RANK_RICE))
            expected_codec = min(candidates)[1]
            actual_codec, actual_payload = \
                organizer._encode_adaptive_ranks(ranks)
            self.assertEqual(actual_codec, expected_codec, "case %d" % case)
            self.assertEqual(
                len(actual_payload), min(candidates)[0], "case %d" % case)

    def test_rice_rounded_byte_ties_and_strict_global_win_rule(self):
        # x=1 occupies two Rice bits for both k=0 and k=1. Rounded payload
        # sizes tie across several k values, so the writer must choose k=0.
        ranks = [10, 12]
        k, payload = organizer._encode_rice_ranks(ranks)
        self.assertEqual(k, 0)
        self.assertEqual(payload, independent_rice_payload(ranks, 0))
        short_constant = [index * 1000 for index in range(4)]
        long_constant = [index * 1000 for index in range(1024)]
        self.assertEqual(
            organizer._encode_rice_ranks(short_constant)[0], 8)
        self.assertEqual(
            organizer._encode_rice_ranks(long_constant)[0], 9)

        # Rice and positive-varint both occupy two bytes for delta 128.
        # Codec 3 is legal but may be selected only when strictly smaller.
        tied = [0, 128]
        rice = organizer._encode_rank_codec(
            tied, organizer.BSP4_RANK_RICE)
        positive = organizer._encode_rank_codec(
            tied, organizer.BSP4_RANK_POSITIVE)
        self.assertEqual(len(rice), len(positive))
        codec, selected = organizer._encode_adaptive_ranks(tied)
        self.assertEqual(codec, organizer.BSP4_RANK_POSITIVE)
        self.assertEqual(selected, positive)

        winning = [index * 1000 for index in range(128)]
        codec, selected = organizer._encode_adaptive_ranks(winning)
        self.assertEqual(codec, organizer.BSP4_RANK_RICE)
        self.assertLess(len(selected), len(
            organizer._encode_rank_codec(
                winning, organizer.BSP4_RANK_POSITIVE)))

    def test_rice_decoder_rejects_parameter_truncation_trailing_and_padding(self):
        codec = organizer.BSP4_RANK_RICE
        with self.assertRaisesRegex(organizer.PoolError, "missing"):
            organizer._decode_rank_codec(b"", 2, 0, 1, codec)
        with self.assertRaisesRegex(organizer.PoolError, "0..41"):
            organizer._decode_rank_codec(bytes((42, 1)), 2, 0, 1, codec)
        with self.assertRaisesRegex(organizer.PoolError, "quotient.*truncated"):
            organizer._decode_rank_codec(bytes((0, 0)), 2, 0, 1, codec)
        with self.assertRaisesRegex(organizer.PoolError, "remainder.*truncated"):
            organizer._decode_rank_codec(bytes((9, 1)), 2, 0, 1, codec)
        with self.assertRaisesRegex(organizer.PoolError, "trailing"):
            organizer._decode_rank_codec(bytes((0, 1, 0)), 2, 0, 1, codec)
        with self.assertRaisesRegex(organizer.PoolError, "padding"):
            organizer._decode_rank_codec(bytes((0, 3)), 2, 0, 1, codec)
        with self.assertRaisesRegex(organizer.PoolError, "block header"):
            organizer._decode_rank_codec(bytes((0, 1)), 2, 0, 2, codec)

    def test_dense_block_is_much_smaller_than_bsp3(self):
        records = [
            organizer.Record(rank, (TAG, LEGENDARY))
            for rank in range(1024)
        ]
        bsp3_path, bsp3_writer = self.writer("dense-v3.bspool", schema=3)
        publish(bsp3_writer, records)
        bsp4_path, bsp4_writer = self.writer("dense-v4.bspool", schema=4)
        publish(bsp4_writer, records)

        bsp3 = organizer.BSPoolReader(bsp3_path)
        bsp4 = organizer.BSPoolReader(bsp4_path)
        self.assertEqual(
            [record.rank for record in bsp3.iter_records()],
            [record.rank for record in bsp4.iter_records()])
        self.assertEqual(bsp4.blocks[0].rank_codec,
                         organizer.BSP4_RANK_COMPLEMENT)
        self.assertLess(bsp4.data_bytes, bsp3.data_bytes // 5)

    def test_logical_digests_do_not_depend_on_physical_codec(self):
        ranks = [10, 11, 13, 14]
        rank_digests = set()
        for codec in (organizer.BSP4_RANK_POSITIVE,
                      organizer.BSP4_RANK_COMPLEMENT,
                      organizer.BSP4_RANK_BITMAP,
                      organizer.BSP4_RANK_RICE):
            payload = organizer._encode_rank_codec(ranks, codec)
            decoded = organizer._decode_rank_codec(
                payload, len(ranks), ranks[0], ranks[-1], codec)
            rank_digests.add(organizer._bsp4_update_membership_digest(
                organizer._bsp4_membership_start(), decoded))
        for k in (0, 1, 3, 7):
            payload = organizer._rice_rank_payload(ranks, k)
            decoded = organizer._decode_rank_codec(
                payload, len(ranks), ranks[0], ranks[-1],
                organizer.BSP4_RANK_RICE)
            rank_digests.add(organizer._bsp4_update_membership_digest(
                organizer._bsp4_membership_start(), decoded))
        self.assertEqual(len(rank_digests), 1)
        canonical_ranks = b"".join(
            organizer.encode_varint(ranks[index] - ranks[index - 1])
            for index in range(1, len(ranks)))
        expected_membership = independent_fnv64(
            canonical_ranks,
            independent_fnv64(
                struct.pack("<IQQI", len(ranks), ranks[0], ranks[-1],
                            len(canonical_ranks)),
                independent_fnv64(b"BSP4MEM1")))
        self.assertEqual(rank_digests, {expected_membership})

        indexes = [1, 2, 4]
        index_payloads = {
            organizer.BSP4_META_POSITIVE:
                organizer._positive_index_payload(indexes),
            organizer.BSP4_META_COMPLEMENT:
                organizer._complement_index_payload(indexes, 6),
            organizer.BSP4_META_BITMAP:
                organizer._bitmap_index_payload(indexes, 6),
            organizer.BSP4_META_RUNS:
                organizer._run_index_payload(indexes),
        }
        metadata_digests = set()
        for codec, payload in index_payloads.items():
            decoded, at = organizer._decode_bsp4_indexes(
                payload, 0, codec, len(indexes), 6)
            self.assertEqual(at, len(payload))
            per_record = [
                (TAG,) if index in decoded else tuple()
                for index in range(6)
            ]
            metadata_digests.add(organizer._bsp4_update_metadata_digest(
                organizer._bsp4_metadata_start(), per_record))
        self.assertEqual(len(metadata_digests), 1)
        canonical_metadata, associations = organizer._encode_bsp3_metadata([
            (TAG,) if index in indexes else tuple()
            for index in range(6)
        ])
        expected_metadata = independent_fnv64(
            canonical_metadata,
            independent_fnv64(
                struct.pack("<III", 6, associations,
                            len(canonical_metadata)),
                independent_fnv64(b"BSP4META1")))
        self.assertEqual(metadata_digests, {expected_metadata})

        # Membership is rank-only by definition.
        plain = [(TAG,) for _ in ranks]
        richer = [(TAG, LEGENDARY) for _ in ranks]
        member_plain = organizer._bsp4_update_membership_digest(
            organizer._bsp4_membership_start(), ranks)
        member_richer = organizer._bsp4_update_membership_digest(
            organizer._bsp4_membership_start(), ranks)
        self.assertEqual(member_plain, member_richer)
        self.assertNotEqual(
            organizer._bsp4_update_metadata_digest(
                organizer._bsp4_metadata_start(), plain),
            organizer._bsp4_update_metadata_digest(
                organizer._bsp4_metadata_start(), richer))

    def test_separate_checksums_and_complete_index_detect_corruption(self):
        records = [
            organizer.Record(index * 1000,
                             (TAG,) if index % 2 else (TAG, LEGENDARY))
            for index in range(128)
        ]
        path, writer = self.writer("checksums.bspool")
        publish(writer, records)
        reader = organizer.BSPoolReader(path)
        block = reader.blocks[0]
        self.assertEqual(
            struct.calcsize("<QQQQIIIIBBBBI"), organizer.INDEX4_ENTRY_BYTES)
        with open(path, "rb") as handle:
            handle.seek(block.offset)
            header = handle.read(organizer.BLOCK4_HEADER_BYTES)
            rank_payload = handle.read(block.rank_bytes)
            metadata_payload = handle.read(block.metadata_bytes)
            handle.seek(reader.header_bytes + reader.data_bytes)
            index_entry = handle.read(organizer.INDEX4_ENTRY_BYTES)
            footer = handle.read(organizer.FOOTER4_BYTES)
        self.assertEqual(header[:4], b"BSP4")
        self.assertEqual(tuple(header[4:8]), (
            organizer.BLOCK4_HEADER_BYTES, block.rank_codec,
            organizer.BSP4_METADATA_ADAPTIVE, 0))
        rank_checksum = independent_crc64(
            header[4:6] + header[8:16] + header[24:40])
        self.assertEqual(
            independent_crc64(rank_payload, rank_checksum),
            struct.unpack_from("<Q", header, 40)[0])
        metadata_checksum = independent_crc64(
            header[4:5] + header[6:7] + header[8:12] + header[16:24])
        self.assertEqual(
            independent_crc64(metadata_payload, metadata_checksum),
            struct.unpack_from("<Q", header, 48)[0])
        self.assertEqual(index_entry[48:51], bytes((
            block.rank_codec, organizer.BSP4_METADATA_ADAPTIVE, 0)))
        self.assertFalse(any(index_entry[51:]))
        self.assertEqual(footer[:8], b"BSPIDX4\n")
        self.assertFalse(any(footer[56:88]))
        self.assertEqual(
            independent_crc64(footer[:88]),
            struct.unpack_from("<Q", footer, 88)[0])

        rank_bad = os.path.join(self.temp.name, "rank-bad.bspool")
        copy_and_mutate(
            path, rank_bad, block.offset + block.header_bytes)
        with self.assertRaisesRegex(organizer.PoolError, "rank checksum"):
            organizer.BSPoolReader(rank_bad)

        metadata_bad = os.path.join(self.temp.name, "metadata-bad.bspool")
        copy_and_mutate(
            path, metadata_bad,
            block.offset + block.header_bytes + block.rank_bytes)
        with self.assertRaisesRegex(organizer.PoolError, "metadata checksum"):
            organizer.BSPoolReader(metadata_bad)

        index_bad = os.path.join(self.temp.name, "index-bad.bspool")
        copy_and_mutate(
            path, index_bad,
            reader.header_bytes + reader.data_bytes + 48)
        with self.assertRaisesRegex(organizer.PoolError, "index entry"):
            organizer.BSPoolReader(index_bad)

        footer_bad = os.path.join(self.temp.name, "footer-bad.bspool")
        copy_and_mutate(
            path, footer_bad,
            os.path.getsize(path) - organizer.FOOTER4_BYTES + 56)
        with self.assertRaisesRegex(organizer.PoolError, "footer checksum"):
            organizer.BSPoolReader(footer_bad)

    def test_lazy_open_validates_payload_once_but_index_at_open(self):
        records = [
            organizer.Record(index * 1000,
                             (TAG,) if index % 2 else (TAG, LEGENDARY))
            for index in range(5000)
        ]
        path, writer = self.writer("lazy-valid.bspool")
        publish(writer, records)

        reader = organizer.BSPoolReader(path, verify_payloads=False)
        self.assertFalse(reader._payload_verified)
        validated = []
        original = reader._read_validated_block_records

        def counting_read(handle, block, membership, metadata):
            validated.append(block.offset)
            return original(handle, block, membership, metadata)

        reader._read_validated_block_records = counting_read
        self.assertEqual(
            [record.rank for record in reader.iter_records()],
            [record.rank for record in records])
        self.assertTrue(reader._payload_verified)
        self.assertEqual(len(validated), len(reader.blocks))
        # A later traversal decodes records again as requested, but never
        # repeats the CRC/canonical-digest validation pass.
        self.assertEqual(len(list(reader.iter_records())), len(records))
        self.assertEqual(len(validated), len(reader.blocks))

        # A cached web reader deliberately does not retain the Event from the
        # request that constructed it. A later operation must still be able to
        # cancel the reader's deferred first payload pass explicitly.
        cancellable = organizer.BSPoolReader(path, verify_payloads=False)
        cancellation_checks = []

        def cancel_during_lazy_verification():
            cancellation_checks.append(True)
            return len(cancellation_checks) >= 2

        with self.assertRaisesRegex(organizer.PoolError, "cancelled"):
            list(cancellable.iter_records(
                cancel_check=cancel_during_lazy_verification))
        self.assertEqual(len(cancellation_checks), 2)
        self.assertFalse(cancellable._payload_verified)

        corrupt = os.path.join(self.temp.name, "lazy-rank-bad.bspool")
        block = reader.blocks[0]
        copy_and_mutate(
            path, corrupt, block.offset + block.header_bytes)
        deferred = organizer.BSPoolReader(corrupt, verify_payloads=False)
        self.assertFalse(deferred._payload_verified)
        with self.assertRaisesRegex(organizer.PoolError, "rank checksum"):
            list(deferred.iter_records())
        self.assertFalse(deferred._payload_verified)

        index_bad = os.path.join(self.temp.name, "lazy-index-bad.bspool")
        copy_and_mutate(
            path, index_bad,
            reader.header_bytes + reader.data_bytes + 51)
        with self.assertRaisesRegex(organizer.PoolError, "index entry"):
            organizer.BSPoolReader(index_bad, verify_payloads=False)

        footer_bad = os.path.join(self.temp.name, "lazy-footer-bad.bspool")
        copy_and_mutate(
            path, footer_bad,
            os.path.getsize(path) - organizer.FOOTER4_BYTES + 56)
        with self.assertRaisesRegex(organizer.PoolError, "footer checksum"):
            organizer.BSPoolReader(footer_bad, verify_payloads=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
