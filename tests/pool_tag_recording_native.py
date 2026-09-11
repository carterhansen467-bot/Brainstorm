#!/usr/bin/env python3
"""Direct native tag-recording refusal and byte-preservation regressions."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import brainstorm_pool_organizer as organizer
from pool_organizer import descriptor, write_custom_bsp3

spec = importlib.util.spec_from_file_location(
    "tag_recording_test_fixtures", ROOT / "tests" / "pool_tag_recording.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class NativeTagRecordingRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.TagRecordingRegression.setUpClass()
        cls.addClassCleanup(fixtures.TagRecordingRegression.doClassCleanups)

    def setUp(self):
        self.fixture = fixtures.TagRecordingRegression()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.binary = str(self.fixture.binary)
        self.snapshot = str(self.fixture.snapshot)
        self.folder = self.fixture.folder

    def command(self, source, destination, first="0", last="7", extra=()):
        return subprocess.run([self.binary, "record-tags", self.snapshot,
                               str(source), str(destination), first, last] + list(extra),
                              capture_output=True, text=True, timeout=30)

    def assert_refused(self, result, destination, expected=None):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("AddressSanitizer", result.stderr)
        self.assertNotIn("runtime error:", result.stderr)
        if expected:
            self.assertIn(expected, result.stderr)
        self.assertFalse(destination.exists())
        # The POSIX native abort deliberately retains private staging paths:
        # deleting by pathname could remove another process's replacement.
        # The application removes its owned temporary directory after failure.
        partials = list(destination.parent.glob(destination.name + ".partial.*"))
        if partials:
            self.assertIn("incomplete temporary output retained", result.stderr)

    def transform(self, source, suffix, append):
        target = self.folder / (suffix + ".bspool")
        writer = organizer.BSP4OutputWriter(source, "fixture", suffix, str(target))
        try:
            for record in source.iter_records():
                writer.add(organizer.Record(record.rank, record.occurrences + tuple(append(record))))
            writer.finalize()
            os.replace(writer.temp_path, target)
        except BaseException:
            writer.abort()
            raise
        return target

    def test_invalid_ranges_and_argument_counts_create_no_output(self):
        source = self.fixture.fixture(ranks=range(4))
        destination = self.folder / "invalid.bspool"
        for first, last in (("-1", "7"), ("0", "78"), ("8", "7"), ("x", "7"),
                            ("0", "18446744073709551616")):
            with self.subTest(first=first, last=last):
                self.assert_refused(self.command(source.path, destination, first, last),
                                    destination, "slots")
        result = self.command(source.path, destination, extra=("extra",))
        self.assert_refused(result, destination, "usage")
        result = subprocess.run([self.binary, "record-tags", self.snapshot,
                                 source.path, str(destination), "0"],
                                capture_output=True, text=True, timeout=30)
        self.assert_refused(result, destination, "usage")

    def test_corrupt_last_block_cannot_publish_the_earlier_valid_records(self):
        source_path = self.folder / "multiblock-source.bspool"
        ranks = list(range(5005))
        write_custom_bsp3(str(source_path), ranks, [[b"\x90\x01"] for _ in ranks],
                         "1234567890abcdef", ["tag_route observe"],
                         catalog_hash=self.fixture.catalog_hash, range_end=6000)
        source = organizer.BSPoolReader(source_path)
        adaptive = self.transform(source, "multiblock-adaptive", lambda _record: ())
        parsed = organizer.BSPoolReader(adaptive)
        self.assertGreater(len(parsed.blocks), 1)
        block = parsed.blocks[-1]
        offset = block.offset + block.header_bytes + block.rank_bytes + block.metadata_bytes - 1
        with open(adaptive, "r+b") as handle:
            handle.seek(offset)
            byte = handle.read(1)
            handle.seek(-1, os.SEEK_CUR)
            handle.write(bytes((byte[0] ^ 1,)))
        before = adaptive.read_bytes()
        destination = self.folder / "corrupt-output.bspool"
        self.assert_refused(self.command(adaptive, destination, "0", "1"), destination)
        self.assertEqual(adaptive.read_bytes(), before)

    def test_any_known_tag_conflict_fails_even_with_the_matching_profile(self):
        rank = next(rank for rank, positions in self.fixture.expected.items()
                    if positions.get(0) == "tag_rare")
        source = self.fixture.fixture(ranks=[rank])
        other = next(line.split()[1] for line in self.fixture.snapshot.read_text().splitlines()
                     if line.startswith("tagdef ") and line.split()[1] not in ("tag_rare", "tag_negative"))
        wrong = organizer.Occurrence.decode(descriptor(1, other, 1, 1, 0, 0, 0))
        conflicting = self.transform(source, "conflicting-tag", lambda _record: (wrong,))
        before = conflicting.read_bytes()
        destination = self.folder / "conflict-output.bspool"
        self.assert_refused(self.command(conflicting, destination), destination, "existing tag conflicts")
        self.assertEqual(conflicting.read_bytes(), before)

    def test_matching_existing_tag_is_retained_without_duplicate_associations(self):
        rank = next(rank for rank, positions in self.fixture.expected.items()
                    if positions.get(0) == "tag_rare")
        source = self.fixture.fixture(ranks=[rank])
        raw = descriptor(1, "tag_rare", 1, 1, 0, 0, 0)
        agreeing = self.transform(source, "matching-tag", lambda _record: (organizer.Occurrence.decode(raw),))
        destination = self.folder / "matching-output.bspool"
        result = self.command(agreeing, destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = next(organizer.BSPoolReader(destination).iter_records())
        self.assertEqual(sum(item.raw == raw for item in record.occurrences), 1)

    def test_reserved_malformed_markers_fail_but_other_opaque_bytes_survive(self):
        source = self.fixture.fixture(ranks=range(4))
        for index, marker in enumerate((b"\x82BSTAG\x02\x00\x07", b"\x82BSTAG\x01\x07\x00",
                                        b"\x82BSTAG\x01\x00\x4e", b"\x82BSTAG\x01")):
            with self.subTest(marker=marker):
                invalid = self.transform(source, "bad-marker-%d" % index,
                                         lambda _record: (organizer.Occurrence.decode(marker),))
                destination = self.folder / ("bad-marker-output-%d.bspool" % index)
                self.assert_refused(self.command(invalid, destination), destination, "invalid tag placement")
        destination = self.folder / "opaque-output.bspool"
        result = self.command(source.path, destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        old = {record.rank: {item.raw for item in record.occurrences} for record in source.iter_records()}
        for record in organizer.BSPoolReader(destination).iter_records():
            self.assertTrue(old[record.rank].issubset({item.raw for item in record.occurrences}))
            self.assertTrue(any(item.raw == b"\x82OTHER\x01\x00\x4d" for item in record.occurrences))

    def test_source_or_existing_destination_is_never_overwritten(self):
        source = self.fixture.fixture(ranks=range(4))
        source_path = Path(source.path)
        before = source_path.read_bytes()
        result = self.command(source_path, source_path)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(source_path.read_bytes(), before)
        destination = self.folder / "existing-output.bspool"
        destination.write_bytes(b"existing user data")
        result = self.command(source_path, destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists", result.stderr)
        self.assertEqual(destination.read_bytes(), b"existing user data")
        self.assertEqual(source_path.read_bytes(), before)
        self.assertEqual(list(self.folder.glob("*.partial.*")), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
