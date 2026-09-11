#!/usr/bin/env python3
"""Tag recording preserves source membership and matches independent tag rolls."""

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import brainstorm_pool_builder as builder
import brainstorm_pool_organizer as organizer
import pool_organizer_web as web
import pool_rule_workflow as workflow
import pool_tag_recording as recording
import pool_tag_rules as tags
from pool_organizer import descriptor, write_custom_bsp3


def recipe(first="A1S", last="A4B", condition=None):
    rule = {"version": 1, "range": {"start": first, "end": last}}
    if condition is not None:
        rule["condition"] = condition
    return {"version": 1, "mode": "second_tag", "rule": rule}


class ActualRecordingHelper:
    def __init__(self, binary):
        self.binary = str(binary)
        self.runner = web.NativeSplitHelper(self.binary)
        self.calls = 0

    def record_tags(self, *args, **kwargs):
        self.calls += 1
        return self.runner.record_tags(*args, **kwargs)

    def preview_sources(self, *args, **kwargs):
        return self.runner.preview_sources(*args, **kwargs)

    def summarize(self, path, cancel_check=None):
        organizer._check_cancel(cancel_check)
        result = subprocess.run([self.binary, "summarize", str(path)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise organizer.PoolError(result.stderr.strip())
        organizer._check_cancel(cancel_check)
        return web._parse_native_summary(result.stdout)


class TagRecordingRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get("BRAINSTORM_TEST_POOL_BINARY") or
                          ROOT / "native" / ("brainstorm_seed_pool.exe" if os.name == "nt"
                                             else "brainstorm_seed_pool"))
        cls.lua = shlex.split(os.environ.get("LUAJIT", "")) or ["luajit"]
        snapshot = os.environ.get("BRAINSTORM_TEST_SNAPSHOT") or str(ROOT / "tests/fixtures/tag_recording_snapshot.cfg")
        if os.name == "nt" and shutil.which("cygpath"):
            if cls.lua[0].startswith("/"):
                cls.lua[0] = subprocess.check_output(["cygpath", "-w", cls.lua[0]], text=True).strip()
            if snapshot.startswith("/"):
                snapshot = subprocess.check_output(["cygpath", "-w", snapshot], text=True).strip()
        if (not cls.binary.is_file() or not Path(snapshot).is_file()
                or not (shutil.which(cls.lua[0]) or Path(cls.lua[0]).is_file())):
            if os.environ.get("CI") or os.environ.get("BRAINSTORM_TEST_SNAPSHOT"):
                raise AssertionError("Tag recording requires the native helper, Snapshot, and LuaJIT")
            raise unittest.SkipTest("Build the native helper and install LuaJIT for tag recording tests")
        cls.shared = tempfile.TemporaryDirectory(prefix="tag recording oracle ")
        cls.addClassCleanup(cls.shared.cleanup)
        root = Path(cls.shared.name)
        snapshot_text = Path(snapshot).read_text(encoding="utf-8")
        for target, synthetic in (("tag_negative", "tag_3"), ("tag_rare", "tag_4")):
            if not re.search(r"(?m)^tagdef\s+" + target + r"\s", snapshot_text):
                snapshot_text, changed = re.subn(r"(?m)^(tagdef\s+)" + synthetic + r"(?=\s)",
                                                lambda match: match[1] + target, snapshot_text)
                if changed != 1:
                    raise AssertionError("Snapshot lacks " + target)
        # This private profile exercises both an Ante restriction and an always
        # available target while retaining the source catalog's pool ordering.
        snapshot_text = re.sub(r"(?m)^tagdef tag_negative .*", "tagdef tag_negative 1 2", snapshot_text)
        snapshot_text = re.sub(r"(?m)^tagdef tag_rare .*", "tagdef tag_rare 1 0", snapshot_text)
        unaligned = root / "unaligned.cfg"
        unaligned.write_text(snapshot_text, encoding="utf-8")
        cls.snapshot = root / "snapshot.cfg"
        cls.snapshot.write_bytes(subprocess.check_output(
            cls.lua + [str(ROOT / "tests/align_snapshot_prng.lua"), str(unaligned)],
            cwd=ROOT, timeout=30))
        cls.catalog_hash = builder.catalog_hash_file(cls.snapshot)
        cls.ranks = list(range(256))
        seeds = {organizer.rank_to_seed(rank, organizer.NATURAL_CHARSET): rank for rank in cls.ranks}
        seed_file = root / "seeds.txt"
        seed_file.write_text("\n".join(seeds) + "\n", encoding="ascii")
        # Reuse the production-worker bootstrap from the independent Lua oracle,
        # then ask its public tag generator for both physical blinds per Ante.
        oracle = (ROOT / "tests/pool_lua_oracle.lua").read_text(encoding="utf-8")
        bootstrap = oracle.split("local header = assert(Brainstorm.readPoolHeader", 1)[0]
        if bootstrap == oracle:
            raise AssertionError("The Lua oracle bootstrap changed; update this test seam")
        oracle_path = root / "tag-oracle.lua"
        oracle_path.write_text(bootstrap + """
for seed in read(seedsPath):gmatch("[^\\r\\n]+") do
    Brainstorm.random_state = { hashed_seed = pseudohash(seed) }
    for ante = 1, 4 do
        local small = Brainstorm.rollTag(seed, ante)
        local big = Brainstorm.rollTag(seed, ante)
        print(seed .. " " .. ante .. " " .. small .. " " .. big)
    end
end
""", encoding="utf-8")
        cls.oracle_path = oracle_path
        output = subprocess.check_output(cls.lua + [str(oracle_path), str(ROOT / "Brainstorm_reroll.lua"),
                                         str(cls.snapshot), "unused.bspool", str(seed_file)],
                                         cwd=ROOT, text=True, timeout=30)
        cls.expected = {rank: {} for rank in cls.ranks}
        for line in output.splitlines():
            seed, ante, small, big = line.split()
            for blind, key in enumerate((small, big)):
                if key in tags.TAG_KEYS.values():
                    cls.expected[seeds[seed]][(int(ante) - 1) * 2 + blind] = key

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tag recording test ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.helper = ActualRecordingHelper(self.binary)
        self.serial = 0

    def oracle_for(self, ranks, charset):
        seeds = {organizer.rank_to_seed(rank, charset): rank for rank in ranks}
        path = self.folder / "boundary-seeds.txt"
        path.write_text("\n".join(seeds) + "\n", encoding="ascii")
        output = subprocess.check_output(self.lua + [str(self.oracle_path), str(ROOT / "Brainstorm_reroll.lua"),
                                         str(self.snapshot), "unused.bspool", str(path)],
                                         cwd=ROOT, text=True, timeout=30)
        expected = {rank: {} for rank in ranks}
        for line in output.splitlines():
            seed, ante, small, big = line.split()
            for blind, key in enumerate((small, big)):
                if key in tags.TAG_KEYS.values():
                    expected[seeds[seed]][(int(ante) - 1) * 2 + blind] = key
        return expected

    def fixture(self, schema=3, ranks=None, complete=True, charset=organizer.NATURAL_CHARSET):
        self.serial += 1
        ranks = self.ranks if ranks is None else list(ranks)
        path = self.folder / ("source-%d.bspool" % self.serial)
        # Only unrelated existing metadata is supplied. Even seeds with zero
        # Negative or zero Rare must survive the recording operation.
        rows = [[descriptor(3, "v_crystal_ball", 1, 0, 1, 1, 0),
                 b"\x90" + rank.to_bytes(4, "little"), b"\x82OTHER\x01\x00\x4d"]
                for rank in ranks]
        write_custom_bsp3(str(path), ranks, rows, "%016x" % self.serial,
                         ["tag_route collect", "voucher v_crystal_ball 1 2"],
                         complete=complete, catalog_hash=self.catalog_hash,
                         range_end=max(300, max(ranks) + 1), charset=charset,
                         space={organizer.NATURAL_CHARSET: "natural", organizer.SETTABLE_CHARSET: "settable",
                                organizer.TOTAL_CHARSET: "total"}[charset],
                         seedspace={organizer.NATURAL_CHARSET: organizer.NATURAL_SEEDSPACE,
                                    organizer.SETTABLE_CHARSET: organizer.SETTABLE_SEEDSPACE,
                                    organizer.TOTAL_CHARSET: organizer.TOTAL_SEEDSPACE}[charset])
        source = organizer.BSPoolReader(path)
        if schema == 4:
            target = self.folder / ("source-%d-v4.bspool" % self.serial)
            writer = organizer.BSP4OutputWriter(source, "fixture", "Voucher-only fixture", str(target))
            try:
                for value in source.iter_records():
                    writer.add(value)
                writer.finalize()
                os.replace(writer.temp_path, target)
            except BaseException:
                writer.abort()
                raise
            source = organizer.BSPoolReader(target)
        return source

    def run_recording(self, source, spec=None, prefix="", helper=None, **kwargs):
        return recording.record_tags(source, spec or recipe(), self.folder / "outputs",
                                     self.snapshot, helper or self.helper, prefix=prefix, **kwargs)

    def verify_copy(self, source, report, first=0, last=7, oracle=None):
        self.assertTrue(report["completed"])
        self.assertEqual(report["engine"], "native")
        output = organizer.BSPoolReader(report["path"])
        expected = {value.rank: {item.raw for item in value.occurrences} for value in source.iter_records()}
        actual = {value.rank: value for value in output.iter_records()}
        self.assertEqual(set(actual), set(expected))
        self.assertEqual(output.records, source.records)
        self.assertEqual(output.composite_branches, source.composite_branches)
        self.assertEqual(output.composite_operands, source.composite_operands)
        self.assertEqual(output.composite_expression, source.composite_expression)
        marker = b"\x82BSTAG\x01" + bytes((first, last))
        oracle = self.expected if oracle is None else oracle
        for rank, value in actual.items():
            raw = {item.raw for item in value.occurrences}
            self.assertTrue(expected[rank].issubset(raw))
            self.assertIn(marker, raw)
            generated = {item.raw for item in value.occurrences if item.kind == 1}
            expected_tags = {descriptor(1, key, slot // 2 + 1, slot % 2 + 1, 0, 0, 0)
                             for slot, key in oracle[rank].items() if first <= slot <= last}
            old_tags = {item.raw for item in organizer.Record(rank, tuple(
                organizer.Occurrence.decode(raw) for raw in expected[rank])).occurrences if item.kind == 1}
            self.assertEqual(generated, old_tags | expected_tags)
        self.assertEqual(list((self.folder / "outputs").glob(".pool-tag-data-*")), [])
        return output

    def assert_no_output(self):
        folder = self.folder / "outputs"
        if folder.exists():
            self.assertEqual([path for path in folder.iterdir()
                              if not path.name.endswith(".writer.lock")], [])

    def test_bsp3_and_bsp4_retain_every_seed_and_match_lua_tag_placements(self):
        self.assertTrue(any(not value for value in self.expected.values()))
        self.assertTrue(any("tag_negative" not in value.values() for value in self.expected.values()))
        self.assertTrue(any("tag_rare" not in value.values() for value in self.expected.values()))
        self.assertTrue(any({"tag_negative", "tag_rare"}.issubset(value.values()) for value in self.expected.values()))
        for schema in (3, 4):
            with self.subTest(schema=schema):
                source = self.fixture(schema)
                before = Path(source.path).read_bytes()
                phases, progress = [], []
                report = self.run_recording(source, prefix="schema-%d" % schema,
                    phase=phases.append, progress=lambda done, total: progress.append((done, total)))
                output = self.verify_copy(source, report)
                self.assertEqual(Path(source.path).read_bytes(), before)
                self.assertEqual(phases, ["verifying_source", "recording_tags", "verifying_output"])
                self.assertTrue(progress)
                self.assertEqual(progress[-1], (source.records, source.records))
                classifier = tags.TagClassifier(output, recipe()["rule"])
                for value in output.iter_records():
                    classifier.classify(value)

    def test_big_start_uses_the_second_draw_and_negative_respects_minimum_ante(self):
        source = self.fixture()
        big = self.verify_copy(source, self.run_recording(source, recipe("A3B", "A4B")), 5, 7)
        for value in big.iter_records():
            self.assertFalse(any(item.kind == 1 and item.ante == 3 and item.phase == 1
                                 for item in value.occurrences))
        early = self.verify_copy(source, self.run_recording(source, recipe("A1S", "A1B")), 0, 1)
        self.assertFalse(any(item.key == "tag_negative" for value in early.iter_records()
                             for item in value.occurrences))

    def test_settable_and_total_variable_length_seeds_match_lua_including_tester_rank(self):
        for charset in (organizer.SETTABLE_CHARSET, organizer.TOTAL_CHARSET):
            base = len(charset)
            ranks = [0, base - 1, base, base + 1, base + base * base - 1,
                     base + base * base, 188495]
            with self.subTest(charset=charset):
                self.assertEqual([len(organizer.rank_to_seed(rank, charset)) for rank in ranks[:6]],
                                 [1, 1, 2, 2, 2, 3])
                source = self.fixture(schema=4, ranks=ranks, charset=charset)
                expected = self.oracle_for(ranks, charset)
                output = self.verify_copy(source, self.run_recording(source), oracle=expected)
                self.assertEqual(output.charset, charset)
                self.assertEqual(output.seed(188495), organizer.rank_to_seed(188495, charset))

    def test_repeated_disjoint_recordings_keep_gaps_unknown_until_they_are_recorded(self):
        source = self.fixture(ranks=range(8))
        first = self.verify_copy(source, self.run_recording(source, recipe("A1S", "A2B")), 0, 3)
        second = self.verify_copy(first, self.run_recording(first, recipe("A4S", "A4B")), 6, 7)
        classifier = tags.TagClassifier(second, recipe()["rule"])
        with self.assertRaises(tags.InsufficientMetadataError):
            classifier.classify(next(second.iter_records()))
        third = self.verify_copy(second, self.run_recording(second, recipe("A3S", "A3B")), 4, 5)
        classifier = tags.TagClassifier(third, recipe()["rule"])
        for value in third.iter_records():
            classifier.classify(value)

    def test_conditions_expand_recording_range_without_filtering_membership(self):
        source = self.fixture(ranks=range(16))
        condition = {"not": {"count": {"tag": "rare", "range": {"start": "A1S", "end": "A1B"}, "min": 1}}}
        spec = recipe("A3S", "A4B", condition)
        report = self.run_recording(source, spec)
        self.assertEqual(report["range"], {"start": "A1S", "end": "A4B"})
        self.verify_copy(source, report)

    def test_composite_and_recovered_pools_keep_original_membership_and_markers(self):
        first = self.fixture(ranks=range(0, 32))
        second = self.fixture(ranks=range(16, 48))
        combined = self.folder / "Complete.bspool"
        organizer.combine_pools([first, second], str(combined))
        source = organizer.BSPoolReader(combined)
        annotated = self.verify_copy(source, self.run_recording(source))
        original = {value.rank: value.occurrences for value in annotated.iter_records()}
        plan = workflow.preview(annotated, {"version": 1, "mode": "separate_sources", "source_kind": "inputs"})
        report, done = workflow.publish(annotated, plan, self.folder / "separated")
        self.assertTrue(done)
        self.assertEqual(sorted(item["records"] for item in report["outputs"]), [32, 32])
        recovered = []
        for item in report["outputs"]:
            output = organizer.BSPoolReader(item["path"])
            recovered.append(output)
            classifier = tags.TagClassifier(output, recipe()["rule"])
            for value in output.iter_records():
                self.assertEqual(value.occurrences, original[value.rank])
                classifier.classify(value)
        recombined = self.folder / "Recombined.bspool"
        organizer.combine_pools(recovered, str(recombined))
        restored = organizer.BSPoolReader(recombined)
        self.assertEqual(restored.records, annotated.records)
        classifier = tags.TagClassifier(restored, recipe()["rule"])
        for value in restored.iter_records():
            classifier.classify(value)
            # Combine assigns fresh latest-input operand IDs. Original source
            # branches, recording markers, tags and unrelated bytes survive.
            self.assertTrue({item.raw for item in original[value.rank] if not item.is_operand}.issubset(
                {item.raw for item in value.occurrences}))

    def test_recorded_tags_match_native_scan_event_output(self):
        source = self.fixture()
        output = self.verify_copy(source, self.run_recording(source))
        for key in ("tag_negative", "tag_rare"):
            criteria = self.folder / (key + ".cfg")
            criteria.write_text("\n".join([
                "poolver 1", "threads 1", "start 0", "count 256", "checkpoint 256",
                "chunk 2048", "resume 0", "format binary", "output_schema 4",
                "tag_route observe", "tag %s 1 small 4 big 1" % key, "end", ""]), encoding="ascii")
            scanned = self.folder / (key + ".bspool")
            result = subprocess.run([str(self.binary), "scan", str(self.snapshot), str(criteria), str(scanned)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            actual = {value.rank: {item.raw for item in value.occurrences if item.key == key}
                      for value in output.iter_records()}
            expected = {value.rank: {item.raw for item in value.occurrences}
                        for value in organizer.BSPoolReader(scanned).iter_records()}
            self.assertEqual({rank: rows for rank, rows in actual.items() if rows}, expected)

    def test_wrong_snapshot_and_partial_source_are_refused_before_native_recording(self):
        source = self.fixture(ranks=range(8))
        wrong = self.folder / "wrong.cfg"
        text = self.snapshot.read_text(encoding="utf-8")
        wrong.write_text(text.replace("tagdef tag_negative 1 2", "tagdef tag_negative 1 3"), encoding="utf-8")
        with self.assertRaisesRegex(organizer.PoolError, "snapshot differs"):
            recording.record_tags(source, recipe(), self.folder / "outputs", wrong, self.helper)
        self.assertEqual(self.helper.calls, 0)
        self.assert_no_output()
        partial = self.fixture(ranks=range(8), complete=False)
        with self.assertRaisesRegex(organizer.PoolError, "finished"):
            self.run_recording(partial)
        self.assertEqual(self.helper.calls, 0)
        self.assert_no_output()


    def test_existing_destination_or_companion_is_never_overwritten(self):
        source = self.fixture(ranks=range(8))
        folder = self.folder / "outputs"
        folder.mkdir()
        for suffix in ("", ".state"):
            target = folder / ("retained-tag-data-a1s-a4b.bspool" + suffix)
            target.write_bytes(b"existing data")
            with self.assertRaisesRegex(organizer.PoolError, "already exists"):
                self.run_recording(source, prefix="retained")
            self.assertEqual(target.read_bytes(), b"existing data")
            self.assertEqual([path for path in folder.iterdir()
                              if not path.name.endswith(".writer.lock")], [target])
            target.unlink()
        self.assertEqual(self.helper.calls, 0)

    def test_cancellation_before_and_after_native_completion_leaves_no_copy(self):
        source = self.fixture(ranks=range(8))
        with self.assertRaises(organizer.PoolError):
            self.run_recording(source, cancel_check=lambda: True)
        self.assertEqual(self.helper.calls, 0)
        self.assert_no_output()
        cancelled = [False]
        actual = self.helper.record_tags
        def complete(*args, **kwargs):
            result = actual(*args, **kwargs)
            cancelled[0] = True
            return result
        with mock.patch.object(self.helper, "record_tags", side_effect=complete):
            with self.assertRaises(organizer.PoolError):
                self.run_recording(source, cancel_check=lambda: cancelled[0])
        self.assertTrue(cancelled[0], "Cancellation must follow successful native recording")
        self.assert_no_output()

    def test_source_changed_after_native_completion_is_not_published(self):
        source = self.fixture(ranks=range(8))
        original_bytes = Path(source.path).read_bytes()
        actual = self.helper.record_tags
        attempted = []
        def complete(*args, **kwargs):
            result = actual(*args, **kwargs)
            attempted.append(True)
            with open(source.path, "ab") as handle:
                handle.write(b"changed after recording")
            return result
        with mock.patch.object(self.helper, "record_tags", side_effect=complete):
            with self.assertRaises((organizer.PoolError, OSError)) as failure:
                self.run_recording(source)
        self.assertTrue(attempted, "The source-mutation seam must run after native recording")
        if os.name == "nt":
            # The pinned Windows handle prevents the write itself; POSIX
            # permits it and the source guard must detect the changed file.
            self.assertIsInstance(failure.exception, PermissionError)
            self.assertEqual(Path(source.path).read_bytes(), original_bytes)
        else:
            self.assertIsInstance(failure.exception, organizer.PoolError)
            self.assertRegex(str(failure.exception), "changed")
        self.assertTrue(Path(source.path).is_file())
        self.assert_no_output()

    def test_forged_native_result_cannot_publish_a_copy(self):
        source = self.fixture(ranks=range(8))
        actual = self.helper.record_tags
        transforms = (
            lambda text: text.replace("output_records 8", "output_records 7"),
            lambda text: re.sub(r"(?m)^source_metadata_digest .*$", "source_metadata_digest 0000000000000000", text),
            lambda text: re.sub(r"(?m)^output_membership_digest .*$", "output_membership_digest 0000000000000000", text),
            lambda text: text.replace("\nend", "\nsource_records 8\nend"),
        )
        for index, transform in enumerate(transforms):
            changed = []
            def complete(*args, **kwargs):
                original = actual(*args, **kwargs)
                forged = transform(original)
                self.assertNotEqual(original, forged)
                changed.append(True)
                return forged
            with self.subTest(index=index), mock.patch.object(self.helper, "record_tags",
                    side_effect=complete):
                with self.assertRaises(organizer.PoolError):
                    self.run_recording(source)
            self.assertTrue(changed, "The result-forgery seam must run after native recording")
            self.assert_no_output()

    def test_valid_bsp4_copy_with_same_count_but_different_ranks_is_rejected(self):
        source = self.fixture(schema=4, ranks=range(8))
        actual = self.helper.record_tags
        changed = []

        def forge(snapshot, input_path, stage, first, last, **kwargs):
            result = actual(snapshot, input_path, stage, first, last, **kwargs)
            recorded = organizer.BSPoolReader(stage)

            def header(count, size, membership, metadata):
                replacements = {"records": str(count), "data_bytes": str(size),
                                "membership_digest": "%016x" % membership,
                                "metadata_digest": "%016x" % metadata}
                lines = ["%s %s" % (key, replacements[key])
                         if key in replacements else raw
                         for key, _, raw in recorded.header.lines]
                encoded = ("\n".join(lines) + "\n").encode("ascii")
                self.assertLessEqual(len(encoded), recorded.header_bytes)
                return encoded.ljust(recorded.header_bytes, b"\0"), {}

            writer = organizer.BSP4OutputWriter(
                recorded, "forged-rank", "Forged rank", stage, header_builder=header)
            try:
                for value in recorded.iter_records():
                    writer.add(organizer.Record(9 if value.rank == 7 else value.rank,
                                                value.occurrences))
                writer.finalize()
                os.replace(writer.temp_path, stage)
            except BaseException:
                writer.abort()
                raise
            forged = organizer.BSPoolReader(stage)
            self.assertEqual(forged.records, source.records)
            self.assertNotEqual(forged.membership_digest, source.membership_digest)
            # Keep result counts, source proof, output checksums, and history
            # internally valid: only comparison with source ranks can stop it.
            for kind in ("membership", "metadata"):
                result = re.sub(r"(?m)^output_%s_digest .*$" % kind,
                                "output_%s_digest %016x" % (
                                    kind, getattr(forged, kind + "_digest")), result)
            changed.append(True)
            return result

        with mock.patch.object(self.helper, "record_tags", side_effect=forge):
            with self.assertRaisesRegex(organizer.PoolError, "changed the source seed ranks"):
                self.run_recording(source)
        self.assertTrue(changed)
        self.assertEqual([value.rank for value in source.iter_records()], list(range(8)))
        self.assert_no_output()

    def test_cancellation_after_link_rolls_back_only_the_new_copy(self):
        source = self.fixture(ranks=range(8))
        cancelled = [False]
        actual = organizer.seed_pool_mutations.link_many_no_overwrite
        def link(rows):
            actual(rows)
            cancelled[0] = True
        with mock.patch.object(organizer.seed_pool_mutations, "link_many_no_overwrite", side_effect=link):
            with self.assertRaises(organizer.PoolError):
                self.run_recording(source, cancel_check=lambda: cancelled[0])
        self.assertTrue(cancelled[0], "Cancellation must run after output publication")
        self.assertTrue(Path(source.path).is_file())
        self.assert_no_output()


if __name__ == "__main__":
    unittest.main()
