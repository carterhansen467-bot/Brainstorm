#!/usr/bin/env python3
"""Streaming tag export completeness, history, and safe publication tests."""

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import brainstorm_pool_organizer as organizer
import pool_rule_workflow as workflow
import pool_tag_export as export
import pool_tag_rules as tags
import tagcalc_batch as batch
from pool_organizer import descriptor, write_custom_bsp3


MARKER = b"\x82BSTAG\x01\x00\x4b"


def tag(name, ante, phase=1, flags=0):
    return descriptor(1, "tag_" + name, ante, phase, 0, 0, flags)


class TagExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def fixture(self, name="pool", rows=None, criteria=None):
        if rows is None:
            rows = [[MARKER, tag("negative", 3), tag("rare", 5, 2), b"\x92opaque"], [MARKER]]
        path = self.folder / (name + ".bspool")
        write_custom_bsp3(str(path), list(range(len(rows))), rows,
                         workflow._fingerprint(name)[:16], criteria or ["tag_route observe"],
                         range_end=max(100, len(rows) + 1))
        self.reheader(path, lambda text: "\n".join(
            "label " + name if line.startswith("label ") else line
            for line in text.splitlines()) + "\n")
        return path

    def reheader(self, path, edit):
        reader = organizer.BSPoolReader(path, verify_payloads=False)
        with open(path, "r+b") as handle:
            data = handle.read(reader.header_bytes)
            text = data.split(b"\0", 1)[0].decode("ascii")
            result = edit(text).encode("ascii")
            self.assertLess(len(result), reader.header_bytes)
            handle.seek(0)
            handle.write(result.ljust(reader.header_bytes, b"\0"))

    def test_complete_export_retains_empty_seed_raw_metadata_and_ante38_boundary(self):
        path = self.fixture(rows=[[MARKER, tag("negative", 3), tag("rare", 38, 2),
                                   tag("negative", 39), b"\x92opaque"], [MARKER]])
        before = path.read_bytes()
        with export.open_pool(path) as stream:
            documents = list(stream.iter_documents())
            self.assertEqual(documents[0]["source"]["header_text"], stream.reader.header.text)
        first, empty = documents[1:3]
        self.assertEqual(first["negative_locations"], ["A3S"])
        self.assertEqual(first["rare_locations"], ["A38B"])
        self.assertEqual(first["negative_slots"], [4])
        self.assertEqual(first["rare_slots"], [75])
        self.assertEqual(first["tags"], ["A3S-negative", "A38B-rare"])
        self.assertEqual(first["tag_string"], "A3S-negative A38B-rare")
        self.assertEqual(first["tag_coverage"], {"start": "A1S", "end": "A38B", "complete": True})
        self.assertEqual(empty["tags"], [])
        self.assertIsNone(first["second_tag"])
        self.assertEqual(documents[-1], {"type": "tag_export_complete", "version": 1, "records": 2})
        original = next(organizer.BSPoolReader(path).iter_records())
        self.assertEqual({bytes.fromhex(item["raw_hex"]) for item in first["occurrences"]},
                         {item.raw for item in original.occurrences})
        self.assertEqual(path.read_bytes(), before)

    def test_declared_complete_criteria_can_prove_absence_without_marker(self):
        path = self.fixture(rows=[[tag("negative", 3)], []], criteria=[
            "tag_route observe", "tag tag_negative 1 38 1", "tag tag_rare 1 38 1"])
        with export.open_pool(path) as stream:
            rows = list(stream.iter_records())
        self.assertEqual(rows[1]["negative_locations"], [])
        self.assertEqual(rows[1]["rare_locations"], [])

    def test_missing_evidence_on_later_seed_does_not_publish_partial_file(self):
        path = self.fixture(rows=[[MARKER], []])
        output = self.folder / "complete.ndjson"
        with export.open_pool(path) as stream:
            with self.assertRaises(tags.InsufficientMetadataError) as error:
                export.write_ndjson(stream, output)
        self.assertEqual(error.exception.rank, 1)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.folder.glob(".tag-export-*")), [])

    def test_marker_gaps_and_invalid_versions_are_rejected(self):
        for marker in (b"\x82BSTAG\x01\x01\x4b", b"\x82BSTAG\x02\x00\x4b"):
            with self.subTest(marker=marker):
                path = self.fixture(rows=[[marker]])
                with export.open_pool(path) as stream:
                    with self.assertRaises(tags.TagRuleError):
                        list(stream.iter_records())

    def test_conflicting_known_tags_are_not_flattened_away(self):
        path = self.fixture(rows=[[MARKER, tag("negative", 3), tag("rare", 3)]])
        with export.open_pool(path) as stream:
            with self.assertRaisesRegex(tags.TagRuleError, "conflicting tags"):
                list(stream.iter_records())

    def test_overlap_composite_preserves_all_original_source_labels(self):
        one = self.fixture("L1", rows=[[MARKER, tag("negative", 3)]])
        two = self.fixture("L2", rows=[[MARKER, tag("rare", 5)]])
        combined = self.folder / "Complete.bspool"
        organizer.combine_pools([organizer.BSPoolReader(one), organizer.BSPoolReader(two)],
                                str(combined), "union", "Complete pool")
        one.unlink()
        two.unlink()
        with export.open_pool(combined) as stream:
            row = next(stream.iter_records())
            self.assertEqual(len(stream.metadata["source"]["composite_branches"]), 2)
        self.assertEqual(row["pool_label"], "Complete pool")
        self.assertEqual(len(row["source_labels"]), 2)
        self.assertEqual(set(row["original_source_labels"]), {"L1", "L2"})
        self.assertEqual(row["negative_locations"], ["A3S"])
        self.assertEqual(row["rare_locations"], ["A5S"])

    def test_composite_cannot_borrow_coverage_from_another_seed(self):
        one = self.fixture("covered", rows=[[MARKER]])
        two = self.folder / "uncovered.bspool"
        write_custom_bsp3(str(two), [1], [[]], "1111111111111111", ["tag_route observe"])
        combined = self.folder / "mixed.bspool"
        organizer.combine_pools([organizer.BSPoolReader(one), organizer.BSPoolReader(two)], str(combined))
        with export.open_pool(combined) as stream:
            iterator = stream.iter_records()
            self.assertEqual(next(iterator)["rank"], 0)
            with self.assertRaises(tags.InsufficientMetadataError):
                next(iterator)

    def derived(self, later=()):
        path = self.fixture(rows=[[MARKER, tag("negative", 3), tag("rare", 5, 2), *later]])
        reader = organizer.BSPoolReader(path)
        recipe = {"version": 1, "mode": "second_tag", "rule": {
            "version": 1, "range": {"start": "A3S", "end": "A7B"}}}
        plan = workflow.preview(reader, recipe)
        result, _completed = workflow.publish(reader, plan, self.folder / "derived")
        return Path(result["outputs"][0]["path"])

    def test_saved_workflow_identifies_second_tag_and_checks_each_record(self):
        path = self.derived()
        with export.open_pool(path) as stream:
            self.assertEqual(stream.metadata["second_tag"]["position"], "A5B")
            self.assertEqual(stream.metadata["second_tag"]["tag"], "rare")
            row = next(stream.iter_records())
        self.assertEqual(row["second_tag"]["source"], "workflow")
        # A syntactically consistent header alone cannot bless the wrong rows.
        reader = organizer.BSPoolReader(path)
        recipe_id = reader.header.one("workflow_recipe_id")
        self.reheader(path, lambda text: text.replace(
            organizer._header_token("a5b-rare"), organizer._header_token("a6b-rare")).replace(
                "rules:%s:a5b-rare" % recipe_id, "rules:%s:a6b-rare" % recipe_id))
        with export.open_pool(path) as stream:
            with self.assertRaisesRegex(organizer.PoolError, "disagrees"):
                list(stream.iter_records())

    def test_untrusted_labels_do_not_invent_a_second_tag(self):
        path = self.fixture("a5b-rare")
        with export.open_pool(path) as stream:
            self.assertIsNone(stream.metadata["second_tag"])
            self.assertIn("explicit second tag", stream.metadata["second_tag_reason"])

    def test_corrupt_saved_recipe_identity_fails(self):
        path = self.derived()
        self.reheader(path, lambda text: text.replace("workflow_recipe_id ", "workflow_recipe_id 0"))
        with self.assertRaisesRegex(organizer.PoolError, "identity"):
            with export.open_pool(path):
                pass

    def test_named_output_collision_and_cancellation_preserve_existing_files(self):
        path = self.fixture()
        output = self.folder / "existing.ndjson"
        output.write_text("user data")
        with export.open_pool(path) as stream:
            with self.assertRaisesRegex(organizer.PoolError, "already exists"):
                export.write_ndjson(stream, output)
            with self.assertRaisesRegex(organizer.PoolError, "cancelled"):
                export.write_ndjson(stream, self.folder / "cancel.ndjson", cancel_check=lambda: True)
        self.assertEqual(output.read_text(), "user data")
        self.assertFalse((self.folder / "cancel.ndjson").exists())

    def test_cli_emits_typed_stream_and_trailer_only_after_success(self):
        path = self.fixture()
        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output):
            self.assertEqual(export.main([str(path), "-"]), 0)
        documents = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([item["type"] for item in documents],
                         ["tag_export_header", "seed", "seed", "tag_export_complete"])

    def test_late_corruption_has_no_completion_trailer_or_published_output(self):
        source = organizer.BSPoolReader(self.fixture(rows=[[MARKER]] * 5005))
        adaptive = self.folder / "multiblock.bspool"
        writer = organizer.BSP4OutputWriter(source, "fixture", "large", str(adaptive))
        try:
            for record in source.iter_records():
                writer.add(record)
            writer.finalize()
            os.replace(writer.temp_path, adaptive)
        except BaseException:
            writer.abort()
            raise
        parsed = organizer.BSPoolReader(adaptive)
        last = parsed.blocks[-1]
        self.assertGreater(len(parsed.blocks), 1)
        with open(adaptive, "r+b") as handle:
            handle.seek(last.offset + last.header_bytes + last.rank_bytes + last.metadata_bytes - 1)
            original = handle.read(1)
            handle.seek(-1, os.SEEK_CUR)
            handle.write(bytes((original[0] ^ 1,)))
        target = self.folder / "corrupt.ndjson"
        with export.open_pool(adaptive) as stream:
            with self.assertRaises(organizer.PoolError):
                export.write_ndjson(stream, target)
        self.assertFalse(target.exists())
        output = io.StringIO()
        with export.open_pool(adaptive) as stream, mock.patch.object(sys, "stdout", output):
            with self.assertRaises(organizer.PoolError):
                export.write_ndjson(stream, "-")
        self.assertNotIn('"type":"tag_export_complete"', output.getvalue())

    def test_cancellation_after_link_rolls_back_only_new_output(self):
        path = self.fixture()
        target = self.folder / "cancel-after-link.ndjson"
        linked = [False]
        original_link = organizer.seed_pool_mutations.link_no_overwrite
        def link(*args, **kwargs):
            result = original_link(*args, **kwargs)
            linked[0] = True
            return result
        with export.open_pool(path) as stream:
            with mock.patch.object(organizer.seed_pool_mutations, "link_no_overwrite", side_effect=link):
                with self.assertRaisesRegex(organizer.PoolError, "cancelled"):
                    export.write_ndjson(stream, target, cancel_check=lambda: linked[0])
        self.assertTrue(linked[0])
        self.assertFalse(target.exists())
        self.assertTrue(path.exists())

    def test_source_changed_since_reader_creation_is_rejected(self):
        path = self.fixture()
        stream = export.TagExport(organizer.BSPoolReader(path, verify_payloads=False))
        status = path.stat()
        os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1000000000))
        target = self.folder / "stale.ndjson"
        with self.assertRaisesRegex(organizer.PoolError, "changed|replaced"):
            export.write_ndjson(stream, target)
        self.assertFalse(target.exists())

    def run_batch(self, path, name, *args):
        destination = self.folder / name
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", stderr):
            code = batch.main(["--input", str(path), "--output-dir", str(destination), *args])
        return code, destination, stderr.getvalue()

    @staticmethod
    def documents(path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_batch_pool_infers_saved_second_tag_and_preserves_scored_record_metadata(self):
        path = self.derived(later=(tag("negative", 7, 2), tag("rare", 12)))
        source = organizer.BSPoolReader(path)
        before = path.read_bytes()
        code, destination, stderr = self.run_batch(path, "auto-score")
        self.assertEqual(code, 0, stderr)
        rows = self.documents(destination / "scores.ndjson")
        self.assertEqual(len(rows), 1)
        scored = rows[0]
        self.assertEqual(scored["status"], "scored")
        self.assertGreater(scored["score"], 0)
        self.assertEqual(scored["second_tag"], "A5B")
        self.assertEqual(scored["baseline_copy"], "A5Boss")
        self.assertEqual(scored["negative_locations"], ["A3S", "A7B"])
        self.assertEqual(scored["rare_locations"], ["A5B", "A12S"])
        self.assertEqual(scored["input"]["second_tag"]["source"], "workflow")
        self.assertEqual(scored["input"]["rank"], 0)
        self.assertEqual(scored["pool_label"], source.header.one("label"))
        self.assertEqual({bytes.fromhex(item["raw_hex"]) for item in scored["input"]["occurrences"]},
                         {item.raw for item in next(source.iter_records()).occurrences})
        summary = json.loads((destination / "summary.json").read_text())
        self.assertEqual(summary["status"], "complete")
        metadata = summary["source_metadata"][0]["source"]
        self.assertEqual(metadata["header_text"], source.header.text)
        self.assertEqual(metadata["snapshot_id"], source.snapshot_token)
        self.assertEqual(self.documents(destination / "tags.ndjson")[-1]["type"], "tag_export_complete")
        self.assertEqual(len(self.documents(destination / "leaderboard.ndjson")), 1)
        self.assertEqual(path.read_bytes(), before)

    def test_batch_late_missing_coverage_marks_failure_without_completion_or_leaderboard(self):
        future = [tag("negative", 7, 2), tag("rare", 12)]
        path = self.fixture(rows=[[MARKER, *future], future])
        code, destination, stderr = self.run_batch(path, "late-missing", "--second-tag", "A4S")
        self.assertEqual(code, 1)
        self.assertIn("lacks recorded coverage", stderr)
        summary = json.loads((destination / "summary.json").read_text())
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["records"], 1)
        self.assertEqual(summary["scored"], 1)
        self.assertEqual(len(self.documents(destination / "scores.ndjson")), 1)
        self.assertNotIn("tag_export_complete", [item["type"] for item in self.documents(destination / "tags.ndjson")])
        self.assertFalse((destination / "leaderboard.ndjson").exists())
        self.assertFalse((destination / "leaderboard.csv").exists())

    def test_batch_ante39_baselines_fail_before_any_output_is_created(self):
        path = self.fixture()
        for option, value in (("--baseline-copy", "A39S"), ("--baseline-copy", "A39Boss"),
                              ("--second-tag", "A39B")):
            with self.subTest(option=option, value=value):
                code, destination, stderr = self.run_batch(path, "invalid-baseline", option, value)
                self.assertEqual(code, 1)
                self.assertIn("1–38", stderr)
                self.assertFalse(destination.exists())


class NativeTagExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("tag_export_recording_fixtures", ROOT / "tests/pool_tag_recording.py")
        cls.fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixtures)
        cls.fixtures.TagRecordingRegression.setUpClass()
        cls.addClassCleanup(cls.fixtures.TagRecordingRegression.doClassCleanups)

    def test_optional_native_recording_proves_full_range_and_preserves_original_identity(self):
        fixture = self.fixtures.TagRecordingRegression()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.fixture(ranks=range(16))
        original = Path(source.path).read_bytes()
        phases = []
        with mock.patch.object(self.fixtures.web, "_native_split_helper", return_value=fixture.helper), \
                export.open_pool(source.path, fixture.snapshot,
                                 temp_dir=fixture.folder, phase=phases.append) as stream:
            recorded_path = Path(stream.reader.path)
            self.assertTrue(recorded_path.exists())
            self.assertEqual(stream.metadata["source"]["snapshot_id"], source.snapshot_token)
            self.assertEqual(stream.metadata["source"]["header_text"], source.header.text)
            self.assertIsNotNone(stream.metadata["recorded_source"])
            rows = list(stream.iter_records())
            self.assertEqual(len(rows), 16)
            self.assertTrue(all(row["tag_coverage"]["complete"] for row in rows))
            self.assertTrue(any(slot > 7 for row in rows for slot in row["negative_slots"] + row["rare_slots"]))
        self.assertFalse(recorded_path.exists())
        self.assertEqual(Path(source.path).read_bytes(), original)
        self.assertEqual(phases, ["verifying_source", "recording_tags", "verifying_output"])

    def test_batch_snapshot_uses_default_helper_and_retains_complete_original_metadata(self):
        fixture = self.fixtures.TagRecordingRegression()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.fixture(ranks=[0])
        before = Path(source.path).read_bytes()
        destination = fixture.folder / "native-batch"
        stderr = io.StringIO()
        with mock.patch.object(self.fixtures.web, "_native_split_helper", return_value=fixture.helper) as discover, \
                mock.patch.object(sys, "stdout", io.StringIO()), mock.patch.object(sys, "stderr", stderr):
            code = batch.main(["--input", source.path, "--output-dir", str(destination),
                               "--snapshot", str(fixture.snapshot), "--second-tag", "A4S"])
        self.assertEqual(code, 0, stderr.getvalue())
        discover.assert_called_once_with()
        self.assertEqual(fixture.helper.calls, 1)
        rows = TagExportTests.documents(destination / "scores.ndjson")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "scored")
        self.assertGreater(rows[0]["score"], 0)
        self.assertEqual(rows[0]["input"]["seed"], source.seed(0))
        self.assertEqual(rows[0]["input"]["tag_coverage"], {"start": "A1S", "end": "A38B", "complete": True})
        self.assertTrue({item.raw.hex() for item in next(source.iter_records()).occurrences}.issubset(
            {item["raw_hex"] for item in rows[0]["input"]["occurrences"]}))
        self.assertIn(MARKER.hex(), {item["raw_hex"] for item in rows[0]["input"]["occurrences"]})
        self.assertTrue(any(slot > 7 for slot in rows[0]["input"]["negative_slots"] + rows[0]["input"]["rare_slots"]))
        summary = json.loads((destination / "summary.json").read_text())
        self.assertEqual(summary["status"], "complete")
        metadata = summary["source_metadata"][0]
        self.assertEqual(metadata["source"]["header_text"], source.header.text)
        self.assertEqual(metadata["source"]["snapshot_id"], source.snapshot_token)
        self.assertEqual(metadata["source"]["label"], source.header.one("label"))
        self.assertFalse(Path(metadata["recorded_source"]["path"]).exists())
        self.assertEqual(TagExportTests.documents(destination / "tags.ndjson")[-1]["type"], "tag_export_complete")
        self.assertTrue((destination / "leaderboard.ndjson").exists())
        self.assertEqual(Path(source.path).read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
