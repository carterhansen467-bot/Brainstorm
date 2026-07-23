#!/usr/bin/env python3
"""Native BSP4 rank-column, identity, corruption, and exhaustion regression."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import brainstorm_pool_organizer as organizer


def descriptor(kind: int, key: str) -> organizer.Occurrence:
    encoded = key.encode("ascii")
    return organizer.Occurrence.decode(
        bytes((kind, len(encoded))) + encoded + bytes((1, 1, 0, 0, 0)))


TAG = descriptor(1, "tag_charm")
LEGENDARY = descriptor(2, "j_perkeo")
VOUCHER = descriptor(3, "v_overstock_norm")
UNKNOWN = organizer.Occurrence.decode(bytes((0x90, 0x01, 0xA5)))


class SourceStub:
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
    pool_id = "bsp4-native-source"
    is_composite = False
    header = organizer.PoolHeader(
        "stage_hash 5555666677778888\n"
        "scan_cursor 4096\n"
        "tag_route collect\n"
        "end\n")

    @property
    def snapshot_token(self) -> str:
        return "%016x" % self.snapshot_id


def publish(path: Path, records: list[organizer.Record]) -> dict[str, object]:
    writer = organizer.BSP4OutputWriter(
        SourceStub(), "native-bsp4-oracle", "Native BSP4 oracle", str(path))
    try:
        # Keep four independently chosen codec blocks in this reader fixture.
        # It intentionally models legacy BSP4's 1K physical grouping; the
        # canonical 4K writer contract is covered separately.
        for index, record in enumerate(records, 1):
            writer.add(record)
            if index % organizer.BSP3_WRITE_RECORDS == 0:
                writer._flush()
        result = writer.finalize()
        os.replace(writer.temp_path, writer.final_path)
        return result
    except BaseException:
        writer.abort()
        raise


def fnv_rank_order(ranks: list[int]) -> int:
    value = organizer.FNV64_OFFSET
    for rank in ranks:
        value = organizer.fnv64(struct.pack("<Q", rank), value)
    return value


def rice_rank_payload(ranks: list[int],
                      forced_k: int | None = None) -> tuple[bytes, int, int]:
    """Independent codec-3 oracle: return payload, used bits, and k."""
    candidates = []
    for k in range(42):
        bits = sum((((right - left - 1) >> k) + 1 + k)
                   for left, right in zip(ranks, ranks[1:]))
        candidates.append((1 + (bits + 7) // 8, k, bits))
    _size, best_k, _bits = min(candidates)
    k = best_k if forced_k is None else forced_k
    if not 0 <= k <= 41:
        raise AssertionError("Rice k is outside its canonical field")
    bits = sum((((right - left - 1) >> k) + 1 + k)
               for left, right in zip(ranks, ranks[1:]))
    output = bytearray(1 + (bits + 7) // 8)
    output[0] = k
    at = 0
    for left, right in zip(ranks, ranks[1:]):
        value = right - left - 1
        quotient = value >> k
        at += quotient
        output[1 + (at >> 3)] |= 1 << (at & 7)
        at += 1
        remainder = value & ((1 << k) - 1)
        for bit in range(k):
            if remainder & (1 << bit):
                output[1 + (at >> 3)] |= 1 << (at & 7)
            at += 1
    if at != bits:
        raise AssertionError("Rice oracle bit accounting differs")
    return bytes(output), bits, k


class NativeBSP4ReaderRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = tempfile.TemporaryDirectory(prefix="brainstorm-bsp4-native-build-")
        cls.binary = Path(cls.build.name) / (
            "bsp4-native-reader.exe" if os.name == "nt" else "bsp4-native-reader")
        compiler = shlex.split(os.environ.get("CC", ""))
        if not compiler:
            found = shutil.which("clang") or shutil.which("cc")
            compiler = [found] if found else []
        if not compiler:
            raise unittest.SkipTest("a C11 compiler is required")
        extra_flags = shlex.split(os.environ.get("BSP4_NATIVE_TEST_CFLAGS", ""))
        command = compiler + [
            "-std=c11", "-O2", "-ffp-contract=off", *extra_flags,
            "-o", str(cls.binary), str(ROOT / "tests" / "bsp4_native_reader.c"),
            "-lm",
        ]
        if os.name == "nt":
            command.extend(["-lws2_32"])
        subprocess.run(command, cwd=ROOT, check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build.cleanup()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="brainstorm-bsp4-native-")
        self.directory = Path(self.temp.name)
        mixed = [10]
        for index in range(1023):
            mixed.append(mixed[-1] + (1 if index % 2 == 0 else 10_000))
        sparse_start = mixed[-1] + 1000
        sparse = [sparse_start + index * 1000 for index in range(1024)]
        dense_start = sparse[-1] + 1000
        dense = [
            rank for rank in range(dense_start, dense_start + 1026)
            if rank not in (dense_start + 20, dense_start + 700)
        ]
        bitmap_start = dense[-1] + 1000
        bitmap = [bitmap_start + index * 2 for index in range(1024)]
        self.ranks = mixed + sparse + dense + bitmap
        self.per_record: list[tuple[organizer.Occurrence, ...]] = []
        records = []
        for absolute, rank in enumerate(self.ranks):
            block, index = divmod(absolute, 1024)
            occurrences = []
            if block in (0, 1) and index in (0, 100):
                occurrences.append(TAG)
            if block == 2 and index not in (20, 700):
                occurrences.append(LEGENDARY)
            if block == 3 and index % 2 == 0:
                occurrences.append(VOUCHER)
            if block == 3 and (100 <= index < 300 or 600 <= index < 900):
                occurrences.append(UNKNOWN)
            items = tuple(occurrences)
            self.per_record.append(items)
            records.append(organizer.Record(rank, items))
        self.path = self.directory / "oracle.bspool"
        self.result = publish(self.path, records)
        self.oracle = organizer.BSPoolReader(str(self.path))
        codecs = [block.rank_codec for block in self.oracle.blocks]
        self.assertEqual(codecs[0], organizer.BSP4_RANK_POSITIVE)
        self.assertIn(
            codecs[1],
            (organizer.BSP4_RANK_POSITIVE,
             getattr(organizer, "BSP4_RANK_RICE", -1)))
        self.assertEqual(
            codecs[2:],
            [organizer.BSP4_RANK_COMPLEMENT,
             organizer.BSP4_RANK_BITMAP])

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_reader(self, *arguments: object,
                   success: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(self.binary), *(str(value) for value in arguments)],
            cwd=ROOT, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        if success and result.returncode:
            self.fail("native reader failed: %s" % result.stderr)
        if not success and not result.returncode:
            self.fail("native reader unexpectedly accepted corrupt input")
        return result

    def copy(self, name: str) -> Path:
        target = self.directory / name
        shutil.copyfile(self.path, target)
        return target

    @staticmethod
    def flip(path: Path, offset: int) -> None:
        with path.open("r+b") as handle:
            handle.seek(offset)
            raw = handle.read(1)
            if not raw:
                raise AssertionError("mutation is outside fixture")
            handle.seek(offset)
            handle.write(bytes((raw[0] ^ 1,)))

    @staticmethod
    def rewrite_header(path: Path, substitutions: tuple[tuple[bytes, bytes], ...],
                       truncate_data: int | None = None) -> None:
        data = path.read_bytes()
        match = re.search(br"(?m)^header_bytes ([0-9]+)$", data[:1024])
        if not match:
            raise AssertionError("fixture has no extended header size")
        header_bytes = int(match.group(1))
        text = data[:header_bytes].rstrip(b"\0")
        for before, after in substitutions:
            replaced = text.replace(before, after, 1)
            if replaced == text:
                raise AssertionError("header substitution did not match")
            text = replaced
        if len(text) > header_bytes:
            raise AssertionError("rewritten header overflowed")
        suffix = data[header_bytes:]
        if truncate_data is not None:
            suffix = suffix[:truncate_data]
        path.write_bytes(text.ljust(header_bytes, b"\0") + suffix)

    def rebuild_with_rice(
            self, name: str, block_number: int, forced_k: int | None = None,
            payload_mutator=None) -> tuple[Path, dict[str, int]]:
        source = self.path.read_bytes()
        data = bytearray()
        index = bytearray()
        target_info = {}
        for number, block in enumerate(self.oracle.blocks):
            header = bytearray(
                source[block.offset:block.offset + block.header_bytes])
            rank_start = block.offset + block.header_bytes
            metadata_start = rank_start + block.rank_bytes
            rank_payload = bytes(source[rank_start:metadata_start])
            metadata = bytes(
                source[metadata_start:metadata_start + block.metadata_bytes])
            codec = block.rank_codec
            used_bits = 0
            if number == block_number:
                ranks = self.ranks[
                    block.first_record:block.first_record + block.count]
                rank_payload, used_bits, _k = rice_rank_payload(
                    ranks, forced_k)
                if payload_mutator is not None:
                    rank_payload = payload_mutator(rank_payload, used_bits)
                codec = 3
                header[5] = codec
                struct.pack_into("<I", header, 12, len(rank_payload))
                rank_crc = organizer.crc64(
                    bytes(header[4:6] + header[8:16] + header[24:40]))
                rank_crc = organizer.crc64(rank_payload, rank_crc)
                struct.pack_into("<Q", header, 40, rank_crc)
            offset = self.oracle.header_bytes + len(data)
            data.extend(header)
            data.extend(rank_payload)
            data.extend(metadata)
            index.extend(struct.pack(
                "<QQQQIIIIBBBBI",
                offset, block.first_record, block.first_rank, block.last_rank,
                block.count, len(rank_payload), block.metadata_bytes,
                block.associations, codec, block.metadata_encoding,
                block.flags, 0, 0))
            if number == block_number:
                target_info = {
                    "offset": offset,
                    "rank_bytes": len(rank_payload),
                    "used_bits": used_bits,
                }
        footer = bytearray(organizer.FOOTER4_BYTES)
        footer[:8] = b"BSPIDX4\n"
        index_offset = self.oracle.header_bytes + len(data)
        struct.pack_into(
            "<QQQQQQ", footer, 8, index_offset, len(self.oracle.blocks),
            self.oracle.records, len(data), self.oracle.membership_digest,
            self.oracle.metadata_digest)
        struct.pack_into("<Q", footer, 88, organizer.crc64(bytes(footer[:88])))
        header = source[:self.oracle.header_bytes].rstrip(b"\0")
        header, changed = re.subn(
            br"(?m)^data_bytes [0-9]+$",
            ("data_bytes %d" % len(data)).encode("ascii"), header)
        if changed != 1 or len(header) > self.oracle.header_bytes:
            raise AssertionError("cannot rewrite BSP4 oracle data_bytes")
        path = self.directory / name
        path.write_bytes(
            header.ljust(self.oracle.header_bytes, b"\0")
            + data + index + footer)
        target_info["data_bytes"] = len(data)
        return path, target_info

    def test_oracle_all_rank_codecs_digests_and_rotated_exhaustion(self) -> None:
        decoded = [
            int(line) for line in
            self.run_reader("read", self.path).stdout.splitlines()
        ]
        self.assertEqual(decoded, self.ranks)
        digests = self.run_reader("digests", self.path).stdout.strip()
        self.assertEqual(
            digests,
            "%016x %016x" % (
                self.oracle.membership_digest, self.oracle.metadata_digest))

        prefix_membership = organizer._bsp4_membership_start()
        prefix_membership = organizer._bsp4_update_membership_digest(
            prefix_membership, self.ranks[:1024])
        prefix_metadata = organizer._bsp4_metadata_start()
        prefix_metadata = organizer._bsp4_update_metadata_digest(
            prefix_metadata, self.per_record[:1024])
        self.assertEqual(
            self.run_reader("digests", self.path, 1024, 1).stdout.strip(),
            "%016x %016x" % (prefix_membership, prefix_metadata))
        self.assertEqual(
            self.run_reader("digests", self.path, 0, 1).stdout.strip(),
            "%016x %016x" % (
                organizer._bsp4_membership_start(),
                organizer._bsp4_metadata_start()))
        self.run_reader("digests", self.path, 1, 0, success=False)

        rotation = len(self.ranks) // 3
        rotated = self.ranks[rotation:] + self.ranks[:rotation]
        expected_exhaustion = "%d %016x" % (
            len(rotated), fnv_rank_order(rotated))
        for claim in (1, 4096, 16384):
            self.assertEqual(
                self.run_reader(
                    "exhaust", self.path, claim).stdout.strip(),
                expected_exhaustion)

    def test_incomplete_committed_blocks_and_schema_encoding_contract(self) -> None:
        incomplete = self.copy("incomplete.bspool")
        self.rewrite_header(
            incomplete,
            ((b"\ncomplete 1\n", b"\ncomplete 0\n"),
             (b"\ncoverage_complete 1\n", b"\ncoverage_complete 0\n")),
            truncate_data=self.oracle.data_bytes)
        decoded = [
            int(line) for line in
            self.run_reader("read", incomplete).stdout.splitlines()
        ]
        self.assertEqual(decoded, self.ranks)
        self.assertEqual(
            self.run_reader("digests", incomplete).stdout.strip(),
            "%016x %016x" % (
                self.oracle.membership_digest, self.oracle.metadata_digest))

        wrong_encoding = self.copy("wrong-encoding.bspool")
        self.rewrite_header(
            wrong_encoding,
            ((b"\nencoding adaptive-events-v1\n",
              b"\nencoding delta-varint-events-v1\n"),))
        self.run_reader("read", wrong_encoding, success=False)

    def test_rank_corruption_is_rejected_for_every_codec(self) -> None:
        for number, block in enumerate(self.oracle.blocks):
            self.assertGreater(block.rank_bytes, 0)
            corrupt = self.copy("rank-%d-bad.bspool" % number)
            self.flip(corrupt, block.offset + block.header_bytes)
            # Complete-pool open is intentionally index-only. The accessed
            # block still fails closed on its independent rank CRC.
            self.run_reader("open", corrupt)
            self.run_reader("read", corrupt, success=False)

    def test_rice_codec_structural_acceptance_and_corruption(self) -> None:
        expected = "%016x" % self.oracle.membership_digest
        rice, info = self.rebuild_with_rice("rice.bspool", 1)
        self.assertLessEqual(
            info["rank_bytes"], self.oracle.blocks[1].rank_bytes)
        self.assertEqual(
            self.run_reader(
                "digests", rice, len(self.ranks), 0).stdout.strip(),
            expected)
        incomplete = self.directory / "rice-incomplete.bspool"
        shutil.copyfile(rice, incomplete)
        self.rewrite_header(
            incomplete,
            ((b"\ncomplete 1\n", b"\ncomplete 0\n"),
             (b"\ncoverage_complete 1\n", b"\ncoverage_complete 0\n")),
            truncate_data=info["data_bytes"])
        self.assertEqual(
            self.run_reader(
                "digests", incomplete, len(self.ranks), 0).stdout.strip(),
            expected)

        # Readers intentionally accept forced physical codecs and a valid
        # non-optimal k: logical identity must not depend on writer selection.
        forced, _info = self.rebuild_with_rice(
            "rice-forced-k41.bspool", 1, forced_k=41)
        self.assertEqual(
            self.run_reader(
                "digests", forced, len(self.ranks), 0).stdout.strip(),
            expected)
        nonwinner, _info = self.rebuild_with_rice(
            "rice-nonwinner.bspool", 2)
        self.assertEqual(
            self.run_reader(
                "digests", nonwinner, len(self.ranks), 0).stdout.strip(),
            expected)

        def bad_k(payload: bytes, _bits: int) -> bytes:
            changed = bytearray(payload)
            changed[0] = 42
            return bytes(changed)

        invalid_k, _info = self.rebuild_with_rice(
            "rice-k42.bspool", 1, payload_mutator=bad_k)
        self.run_reader("read", invalid_k, success=False)

        def bad_padding(payload: bytes, bits: int) -> bytes:
            self.assertNotEqual(bits & 7, 0)
            changed = bytearray(payload)
            changed[-1] |= 1 << (bits & 7)
            return bytes(changed)

        padding, _info = self.rebuild_with_rice(
            "rice-padding.bspool", 1, payload_mutator=bad_padding)
        self.run_reader("read", padding, success=False)
        trailing, _info = self.rebuild_with_rice(
            "rice-trailing.bspool", 1,
            payload_mutator=lambda payload, _bits: payload + b"\0")
        self.run_reader("read", trailing, success=False)
        truncated, _info = self.rebuild_with_rice(
            "rice-truncated.bspool", 1,
            payload_mutator=lambda payload, _bits: payload[:-1])
        self.run_reader("read", truncated, success=False)

    def test_rank_reads_never_touch_metadata_but_opt_in_validation_does(self) -> None:
        block = self.oracle.blocks[2]
        corrupt = self.copy("metadata-bad.bspool")
        self.flip(
            corrupt,
            block.offset + block.header_bytes + block.rank_bytes)
        decoded = [
            int(line) for line in
            self.run_reader("read", corrupt).stdout.splitlines()
        ]
        self.assertEqual(decoded, self.ranks)
        membership = self.run_reader(
            "digests", corrupt, len(self.ranks), 0).stdout.strip()
        self.assertEqual(membership, "%016x" % self.oracle.membership_digest)
        self.run_reader(
            "digests", corrupt, len(self.ranks), 1, success=False)

        # Give malformed metadata a correct physical CRC. Rank-only reads must
        # still ignore it, while the explicit logical validator rejects the
        # unknown descriptor codec rather than trusting the checksum alone.
        block = self.oracle.blocks[0]
        malformed = self.copy("metadata-codec-bad.bspool")
        data = bytearray(malformed.read_bytes())
        metadata_start = (
            block.offset + block.header_bytes + block.rank_bytes)
        metadata_end = metadata_start + block.metadata_bytes
        metadata = bytearray(data[metadata_start:metadata_end])
        descriptor_at = metadata.find(TAG.raw)
        self.assertGreaterEqual(descriptor_at, 0)
        at = descriptor_at + len(TAG.raw)
        while metadata[at] & 0x80:
            at += 1
        at += 1
        metadata[at] = 4
        data[metadata_start:metadata_end] = metadata
        semantic = (
            bytes(data[block.offset + 4:block.offset + 5])
            + bytes(data[block.offset + 6:block.offset + 7])
            + bytes(data[block.offset + 8:block.offset + 12])
            + bytes(data[block.offset + 16:block.offset + 24]))
        metadata_crc = organizer.crc64(semantic)
        metadata_crc = organizer.crc64(bytes(metadata), metadata_crc)
        struct.pack_into("<Q", data, block.offset + 48, metadata_crc)
        malformed.write_bytes(data)
        decoded = [
            int(line) for line in
            self.run_reader("read", malformed).stdout.splitlines()
        ]
        self.assertEqual(decoded, self.ranks)
        self.run_reader(
            "digests", malformed, len(self.ranks), 1, success=False)

    def test_index_footer_and_reserved_fields_are_validated(self) -> None:
        index_offset = self.oracle.header_bytes + self.oracle.data_bytes
        index_bad = self.copy("index-bad.bspool")
        self.flip(index_bad, index_offset + 51)
        self.run_reader("open", index_bad, success=False)
        self.run_reader("read", index_bad, success=False)

        index_header_mismatch = self.copy("index-header-mismatch.bspool")
        self.flip(index_header_mismatch, index_offset + 48)
        self.run_reader("open", index_header_mismatch)
        self.run_reader("read", index_header_mismatch, success=False)

        footer_bad = self.copy("footer-reserved-bad.bspool")
        data = bytearray(footer_bad.read_bytes())
        footer_offset = len(data) - organizer.FOOTER4_BYTES
        data[footer_offset + 56] = 1
        struct.pack_into(
            "<Q", data, footer_offset + 88,
            organizer.crc64(bytes(data[footer_offset:footer_offset + 88])))
        footer_bad.write_bytes(data)
        self.run_reader("open", footer_bad, success=False)
        self.run_reader("read", footer_bad, success=False)

        block_bad = self.copy("block-reserved-bad.bspool")
        self.flip(block_bad, self.oracle.blocks[0].offset + 56)
        self.run_reader("open", block_bad)
        self.run_reader("read", block_bad, success=False)

        # Make adjacent blocks share one boundary rank while preserving the
        # second header's rank checksum and its matching index entry. The
        # global ordering check, not an incidental checksum mismatch, must
        # reject the duplicate.
        ordering_bad = self.copy("cross-block-duplicate.bspool")
        data = bytearray(ordering_bad.read_bytes())
        previous = self.oracle.blocks[0]
        block = self.oracle.blocks[1]
        struct.pack_into("<Q", data, block.offset + 24, previous.last_rank)
        rank_payload = bytes(
            data[block.offset + block.header_bytes:
                 block.offset + block.header_bytes + block.rank_bytes])
        rank_crc = organizer.crc64(
            bytes(data[block.offset + 4:block.offset + 6])
            + bytes(data[block.offset + 8:block.offset + 16])
            + bytes(data[block.offset + 24:block.offset + 40]))
        rank_crc = organizer.crc64(rank_payload, rank_crc)
        struct.pack_into("<Q", data, block.offset + 40, rank_crc)
        struct.pack_into(
            "<Q", data,
            index_offset + organizer.INDEX4_ENTRY_BYTES + 16,
            previous.last_rank)
        ordering_bad.write_bytes(data)
        self.run_reader("read", ordering_bad, success=False)


if __name__ == "__main__":
    unittest.main()
