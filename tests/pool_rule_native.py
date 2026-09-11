#!/usr/bin/env python3
"""Actual native source recovery parity, fallback, and staged failure checks."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import brainstorm_pool_organizer as organizer
import pool_organizer_web as web
import pool_rule_native as native
import pool_rule_workflow as workflow
from pool_organizer import descriptor, write_custom_bsp3


class ActualHelper:
    def __init__(self, binary):
        self.binary = str(binary)
        self.runner = web.NativeSplitHelper(self.binary)

    def split(self, *args, **kwargs):
        return self.runner.split(*args, **kwargs)

    def summarize(self, path, cancel_check=None):
        organizer._check_cancel(cancel_check)
        result = subprocess.run([self.binary, "summarize", path],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise organizer.PoolError(result.stderr.strip())
        organizer._check_cancel(cancel_check)
        return web._parse_native_summary(result.stdout)


class NativeSourceRecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = ROOT / "native" / ("brainstorm_seed_pool.exe"
                                       if os.name == "nt" else "brainstorm_seed_pool")
        if not cls.binary.is_file():
            raise unittest.SkipTest("Build the native pool helper before this integration test.")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native source recovery ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.helper = ActualHelper(self.binary)

    def fixture(self, count=4):
        x, y, combined = (self.folder / name for name in ("L1.bspool", "L2.bspool", "Complete.bspool"))
        first = list(range(1, count))
        second = list(range(2, count + 1))
        negative = descriptor(1, "tag_negative", 3, 1, 0, 0, 0)
        rare = descriptor(1, "tag_rare", 5, 2, 0, 0, 0)
        # Unknown descriptors must survive alongside known events and markers.
        for path, ranks, tag, token in ((x, first, negative, "1111111111111111"),
                                        (y, second, rare, "2222222222222222")):
            write_custom_bsp3(str(path), ranks, [[tag, b"\x90\x01\xab"] for _ in ranks],
                             token, ["tag_route observe", "tag tag_negative 3 7 1",
                                     "tag tag_rare 3 7 1"], range_end=max(100, count + 1))
        organizer.combine_pools([organizer.BSPoolReader(x), organizer.BSPoolReader(y)],
                                str(combined), "union", "AS1 Complete")
        x.unlink()
        y.unlink()
        return organizer.BSPoolReader(combined)

    def plan(self, reader, kind="inputs", ids=None):
        return workflow.preview(reader, {"version": 1, "mode": "separate_sources",
                                         "source_kind": kind, "source_ids": ids or []})

    def factory(self, reader, plan):
        return lambda key, label: workflow._header_builder(
            reader, key, label, plan, minimum_size=reader.header_bytes)

    def stage(self, reader, plan, helper=None, factory=None):
        out = self.folder / "native"
        out.mkdir(exist_ok=True)
        destinations = {row["key"]: str(out / row["name"]) for row in plan["outputs"]}
        return native.stage_sources(reader, plan, destinations, helper or self.helper,
                                    header_factory=factory or self.factory(reader, plan))

    def assert_no_stages(self):
        self.assertEqual(list(self.folder.glob("native/.pool-rules-native-*")), [])

    def test_deleted_sources_preserve_full_records_and_match_python(self):
        for kind in ("inputs", "branches"):
            with self.subTest(kind=kind):
                reader = self.fixture()
                plan = self.plan(reader, kind)
                expected = {record.rank: tuple(item.raw for item in record.occurrences)
                            for record in reader.iter_records()}
                stages = self.stage(reader, plan)
                try:
                    self.assertEqual(len(stages.outputs), 2)
                    python, ok = workflow.publish(reader, plan, str(self.folder / ("python-" + kind)))
                    self.assertTrue(ok)
                    by_key = {row["category_id"]: row for row in python["outputs"]}
                    for output, (staged_path, final_path) in zip(stages.outputs, stages.publications):
                        self.assertFalse(os.path.exists(final_path))
                        restored = organizer.BSPoolReader(staged_path)
                        self.assertFalse(restored.coverage_complete)
                        self.assertEqual(restored.composite_expression, reader.composite_expression)
                        self.assertEqual(restored.composite_branches, reader.composite_branches)
                        self.assertEqual(restored.composite_operands, reader.composite_operands)
                        for record in restored.iter_records():
                            self.assertEqual(tuple(item.raw for item in record.occurrences), expected[record.rank])
                        self.assertEqual(Path(staged_path).read_bytes(),
                                         Path(by_key[output["category_id"]]["path"]).read_bytes())
                finally:
                    stages.cleanup()
                self.assert_no_stages()
                Path(reader.path).unlink()

    def test_selected_source_has_exact_unmatched_and_overlap_accounting(self):
        reader = self.fixture()
        token = "%016x" % next(iter(reader.composite_operands))
        plan = self.plan(reader, ids=[token])
        self.assertEqual((plan["copied_records"], plan["excluded_records"], plan["overlap_records"]), (3, 1, 0))
        stages = self.stage(reader, plan)
        try:
            self.assertEqual([row["records"] for row in stages.outputs], [3])
        finally:
            stages.cleanup()

    def test_multiblock_copy_retains_rank_order_and_descriptors(self):
        reader = self.fixture(4105)
        plan = self.plan(reader)
        stages = self.stage(reader, plan)
        try:
            source = {row.rank: row.occurrences for row in reader.iter_records()}
            for stage, _final in stages.publications:
                output = organizer.BSPoolReader(stage)
                self.assertGreater(len(output.blocks), 1)
                records = list(output.iter_records())
                self.assertEqual([row.rank for row in records], sorted(row.rank for row in records))
                self.assertTrue(all(row.occurrences == source[row.rank] for row in records))
        finally:
            stages.cleanup()

    def test_unvalidated_unordered_and_header_growth_fall_back_without_running(self):
        reader = self.fixture()
        plan = self.plan(reader)
        helper = mock.Mock()
        with mock.patch.object(reader, "_composite_metadata_verified", False):
            with self.assertRaises(organizer.NativeSplitUnsupported):
                self.stage(reader, plan, helper)
        with mock.patch.object(type(reader.blocks), "physical_rank_order", return_value=(False, False)):
            with self.assertRaises(organizer.NativeSplitUnsupported):
                self.stage(reader, plan, helper)
        with self.assertRaises(organizer.NativeSplitUnsupported):
            self.stage(reader, plan, helper, factory=lambda key, label: (reader.header_bytes * 2, None))
        helper.split.assert_not_called()
        self.assert_no_stages()

    def test_late_unsupported_discards_every_native_stage(self):
        reader = self.fixture()
        plan = self.plan(reader)
        helper = mock.Mock(wraps=self.helper)
        def declined(*args, **kwargs):
            self.helper.split(*args, **kwargs)
            raise organizer.NativeSplitUnsupported("old helper")
        helper.split.side_effect = declined
        with self.assertRaises(organizer.NativeSplitUnsupported):
            self.stage(reader, plan, helper)
        self.assert_no_stages()

    def test_count_mismatch_is_failure_and_discards_stages(self):
        reader = self.fixture()
        plan = self.plan(reader)
        helper = mock.Mock(wraps=self.helper)
        helper.split.side_effect = lambda *a, **k: self.helper.split(*a, **k).replace("overlap 2\n", "overlap 0\n")
        with self.assertRaisesRegex(organizer.PoolError, "reviewed counts"):
            self.stage(reader, plan, helper)
        self.assert_no_stages()

    def test_wrong_membership_and_corrupt_bytes_are_rejected(self):
        reader = self.fixture()
        plan = self.plan(reader)
        for error in ("marker", "bytes"):
            with self.subTest(error=error):
                helper = mock.Mock(wraps=self.helper)
                def invalid(path, cancel_check=None):
                    if error == "bytes":
                        with open(path, "r+b") as handle:
                            handle.seek(reader.header_bytes + organizer.BLOCK4_HEADER_BYTES)
                            byte = handle.read(1)
                            handle.seek(-1, os.SEEK_CUR)
                            handle.write(bytes([byte[0] ^ 1]))
                    result = self.helper.summarize(path, cancel_check)
                    if error == "marker":
                        result["operand_counts"] = {}
                    return result
                helper.summarize.side_effect = invalid
                with self.assertRaises(organizer.PoolError):
                    self.stage(reader, plan, helper)
                self.assert_no_stages()

    def test_cancel_during_verification_discards_all_stages(self):
        reader = self.fixture()
        plan = self.plan(reader)
        helper = mock.Mock(wraps=self.helper)
        helper.summarize.side_effect = organizer.PoolError("operation cancelled")
        with self.assertRaisesRegex(organizer.PoolError, "cancelled"):
            self.stage(reader, plan, helper)
        self.assert_no_stages()

    def test_stale_source_pin_rejected_before_helper(self):
        reader = self.fixture()
        plan = copy.deepcopy(self.plan(reader))
        plan["source_pin"]["snapshot_id"] = "0" * 16
        helper = mock.Mock()
        with self.assertRaisesRegex(organizer.PoolError, "source changed"):
            self.stage(reader, plan, helper)
        helper.split.assert_not_called()
        self.assert_no_stages()

    def test_publication_reports_native_and_matches_python_outputs(self):
        reader = self.fixture()
        plan = self.plan(reader)
        result = {}
        for engine, helper in (("native", self.helper), ("python", None)):
            report, completed = workflow.publish(
                reader, plan, str(self.folder / engine), native_helper=helper)
            self.assertTrue(completed)
            self.assertEqual(report["engine"], engine)
            self.assertEqual(report["reviewed_outputs"], plan["outputs"])
            with open(report["report_path"], encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), report)
            result[engine] = {row["category_id"]: Path(row["path"]).read_bytes()
                              for row in report["outputs"]}
            self.assertEqual(list((self.folder / engine).glob(".pool-rules-*")), [])
        self.assertEqual(result["native"], result["python"])

    def test_publication_unsupported_helper_falls_back_cleanly(self):
        reader = self.fixture()
        plan = self.plan(reader)
        helper = mock.Mock(wraps=self.helper)
        helper.split.side_effect = organizer.NativeSplitUnsupported("old helper")
        report, completed = workflow.publish(
            reader, plan, str(self.folder / "fallback"), native_helper=helper)
        self.assertTrue(completed)
        self.assertEqual(report["engine"], "python")
        self.assertEqual(sorted(row["records"] for row in report["outputs"]), [3, 3])
        self.assertEqual(list((self.folder / "fallback").glob(".pool-rules-*")), [])
        helper.split.assert_called_once()

    def test_publication_late_cancel_and_source_change_roll_back_all_links(self):
        for reason in ("cancelled", "source changed"):
            with self.subTest(reason=reason):
                reader = self.fixture()
                plan = self.plan(reader)
                out = self.folder / reason
                linked = [False]
                real_link = organizer.seed_pool_mutations.link_many_no_overwrite
                real_assert = reader._assert_source_unchanged
                def link_then_flag(publications):
                    result = real_link(publications)
                    self.assertTrue(all(os.path.isfile(final) for _stage, final in publications))
                    linked[0] = True
                    return result
                def check_source(handle):
                    real_assert(handle)
                    # A late identity-change observation is simulated here so
                    # Windows' write-denying source handle can use this test.
                    if linked[0] and reason == "source changed":
                        raise organizer.PoolError("source changed after linking")
                with mock.patch.object(organizer.seed_pool_mutations, "link_many_no_overwrite",
                                       side_effect=link_then_flag), \
                        mock.patch.object(reader, "_assert_source_unchanged", side_effect=check_source):
                    with self.assertRaisesRegex(organizer.PoolError, reason):
                        workflow.publish(reader, plan, str(out), native_helper=self.helper,
                                         cancel_check=lambda: linked[0] and reason == "cancelled")
                self.assertTrue(linked[0])
                self.assertEqual(list(out.glob("*.bspool")), [])
                self.assertEqual(list(out.glob("*.json")), [])
                self.assertEqual(list(out.glob(".pool-rules-*")), [])
                self.assertTrue(os.path.isfile(reader.path))
                Path(reader.path).unlink()

    def test_publication_collision_preserves_existing_file_and_companion(self):
        reader = self.fixture()
        plan = self.plan(reader)
        helper = mock.Mock(wraps=self.helper)
        for suffix in ("", ".attached"):
            with self.subTest(suffix=suffix):
                out = self.folder / ("collision" + (suffix or "-pool"))
                out.mkdir()
                collision = out / (plan["outputs"][0]["name"] + suffix)
                collision.write_bytes(b"existing user file")
                with self.assertRaisesRegex(organizer.PoolError, "already exists"):
                    workflow.publish(reader, plan, str(out), native_helper=helper)
                self.assertEqual(collision.read_bytes(), b"existing user file")
                self.assertEqual(list(out.glob(".pool-rules-*")), [])
        helper.split.assert_not_called()

    def test_private_cleanup_failure_does_not_mask_completion_or_cancellation(self):
        reader = self.fixture()
        plan = self.plan(reader)
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                out = self.folder / ("cleanup-" + str(cancel))
                linked = [False]
                real_link = organizer.seed_pool_mutations.link_many_no_overwrite
                real_rmtree = tempfile.TemporaryDirectory._rmtree
                def link_then_flag(publications):
                    result = real_link(publications)
                    linked[0] = True
                    return result
                def failed_cleanup(path, *args, **kwargs):
                    if ".pool-rules-native-" in path:
                        raise PermissionError("antivirus holds private stage")
                    return real_rmtree(path, *args, **kwargs)
                with mock.patch.object(organizer.seed_pool_mutations, "link_many_no_overwrite",
                                       side_effect=link_then_flag), \
                        mock.patch.object(tempfile.TemporaryDirectory, "_rmtree", side_effect=failed_cleanup):
                    if cancel:
                        with self.assertRaisesRegex(organizer.PoolError, "cancelled"):
                            workflow.publish(reader, plan, str(out), native_helper=self.helper,
                                             cancel_check=lambda: linked[0])
                    else:
                        report, completed = workflow.publish(reader, plan, str(out), native_helper=self.helper)
                        self.assertTrue(completed)
                        self.assertEqual(report["engine"], "native")
                self.assertTrue(linked[0])
                self.assertEqual(len(list(out.glob("*.bspool"))), 0 if cancel else 2)

    def test_prepublication_cleanup_failure_preserves_original_cancellation(self):
        reader = self.fixture()
        plan = self.plan(reader)
        helper = mock.Mock(wraps=self.helper)
        helper.summarize.side_effect = organizer.PoolError("operation cancelled")
        with mock.patch.object(tempfile.TemporaryDirectory, "_rmtree",
                               side_effect=PermissionError("antivirus holds private stage")):
            with self.assertRaisesRegex(organizer.PoolError, "operation cancelled"):
                self.stage(reader, plan, helper)
        self.assertEqual(list((self.folder / "native").glob("*.bspool")), [])


if __name__ == "__main__":
    unittest.main()
