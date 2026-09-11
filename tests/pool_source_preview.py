#!/usr/bin/env python3
"""Native source-preview parity and complete semantic-proof regressions."""

import json
import os
from pathlib import Path
import re
import shutil
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


class ActualPreviewHelper:
    def __init__(self, binary):
        self.binary = str(binary)
        self.calls = 0
        self.runner = web.NativeSplitHelper(self.binary)

    def preview_sources(self, source, plan_path, cancel_check=None, progress=None):
        self.calls += 1
        return self.runner.preview_sources(
            str(source), str(plan_path), cancel_check=cancel_check, progress=progress)


class SourcePreviewRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get("BRAINSTORM_TEST_POOL_BINARY") or
                          ROOT / "native" / ("brainstorm_seed_pool.exe"
                                             if os.name == "nt" else "brainstorm_seed_pool"))
        if not cls.binary.is_file():
            if os.environ.get("CI") or os.environ.get("BRAINSTORM_TEST_POOL_BINARY"):
                raise AssertionError("The native source-preview test helper is missing")
            raise unittest.SkipTest("Build the native pool helper before this integration test")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="source preview ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.helper = ActualPreviewHelper(self.binary)
        self.serial = 0

    def fixture(self, operation="union", partial=False, schema=4, count=4):
        self.serial += 1
        paths = [self.folder / ("%d-%s.bspool" % (self.serial, name))
                 for name in ("X", "Y", "Complete")]
        tags = [descriptor(1, "tag_negative", 3, 1, 0, 0, 0),
                descriptor(1, "tag_rare", 5, 2, 0, 0, 0)]
        for index, ranks in enumerate((range(1, count), range(2, count + 1))):
            rows = [[tags[index], b"\x90\x01\xaa", b"\x80\x01"] for _ in ranks]
            write_custom_bsp3(str(paths[index]), list(ranks), rows,
                             "%016x" % (self.serial * 10 + index + 1),
                             ["tag tag_negative 3 7 1", "tag tag_rare 3 7 1"],
                             complete=not partial, range_end=max(count + 2, 100))
        organizer.combine_pools(
            [organizer.BSPoolReader(path) for path in paths[:2]],
            str(paths[2]), operation, "Complete")
        for path in paths[:2]:
            path.unlink()
        subject = organizer.BSPoolReader(paths[2], verify_payloads=False)
        if schema == 3:
            subject = self.derived(subject, lambda record: record, schema=3)
        return subject

    def derived(self, reader, transform, schema=4):
        self.serial += 1
        path = self.folder / ("derived-%d.bspool" % self.serial)
        writer_type = organizer.BSP3OutputWriter if schema == 3 else organizer.BSP4OutputWriter
        writer = writer_type(reader, "preview-fixture", "Preview fixture", str(path), allow_empty=True)
        try:
            for record in reader.iter_records():
                changed = transform(record)
                if changed is not None:
                    writer.add(changed)
            writer.finalize()
            os.replace(writer.temp_path, path)
        except BaseException:
            writer.abort()
            raise
        return organizer.BSPoolReader(path, verify_payloads=False)

    def recipe(self, reader, kind="inputs", selected=None):
        return {"version": 1, "mode": "separate_sources", "source_kind": kind,
                "source_ids": selected or []}

    def selected_for_rank(self, reader, rank, kind="inputs"):
        record = next(record for record in reader.iter_records() if record.rank == rank)
        return sorted(workflow._memberships(record, kind))

    def exact_python(self, reader, recipe):
        return workflow.preview(organizer.BSPoolReader(reader.path, verify_payloads=False), recipe)

    def assert_native_matches(self, reader, recipe):
        expected = self.exact_python(reader, recipe)
        before = set(self.folder.iterdir())
        with mock.patch.object(reader, "iter_records", side_effect=AssertionError("native preview decoded Python records")), \
                mock.patch.object(reader, "_verify_all_payloads", side_effect=AssertionError("native preview rescanned in Python")):
            actual = workflow.preview(reader, recipe, native_helper=self.helper)
        self.assertEqual(actual["engine"], "native")
        for key in ("outputs", "source_records", "copied_records", "excluded_records",
                    "overlap_records", "output_memberships", "exclusions", "source_pin", "recipe"):
            self.assertEqual(actual[key], expected[key], key)
        self.assertTrue(reader._payload_verified)
        self.assertTrue(reader._composite_metadata_verified)
        self.assertEqual(set(self.folder.iterdir()), before)
        return actual

    def test_union_intersection_difference_and_partial_sources_match_python(self):
        for schema in (3, 4):
            for operation, totals in (("union", (4, 0, 2, 6)),
                                      ("intersection", (2, 0, 2, 4)),
                                      ("difference", (1, 0, 0, 1))):
                for partial in (False, True):
                    with self.subTest(schema=schema, operation=operation, partial=partial):
                        subject = self.fixture(operation, partial, schema)
                        for kind in ("inputs", "branches"):
                            plan = self.assert_native_matches(subject, self.recipe(subject, kind))
                            self.assertEqual(tuple(plan[key] for key in (
                                "copied_records", "excluded_records", "overlap_records", "output_memberships")), totals)

    def test_selected_group_and_zero_member_groups_have_exact_counts(self):
        source = self.fixture()
        x = self.selected_for_rank(source, 1)
        fresh = organizer.BSPoolReader(source.path, verify_payloads=False)
        plan = self.assert_native_matches(fresh, self.recipe(fresh, selected=x))
        self.assertEqual((plan["copied_records"], plan["excluded_records"],
                          plan["overlap_records"], plan["output_memberships"]), (3, 1, 0, 3))
        difference = self.fixture("difference")
        members = self.selected_for_rank(difference, 1)
        zero = sorted({"%016x" % value for value in difference.composite_operands} - set(members))
        fresh = organizer.BSPoolReader(difference.path, verify_payloads=False)
        result = native.preview_sources(fresh, self.recipe(fresh, selected=zero), self.helper)
        self.assertEqual(result["counts"], {zero[0]: 0})
        plan = self.assert_native_matches(fresh, self.recipe(fresh, selected=zero))
        self.assertEqual(plan["outputs"], [])
        self.assertEqual((plan["copied_records"], plan["excluded_records"]), (0, 1))

    def test_filtered_and_empty_composites_do_not_restore_historical_counts(self):
        original = self.fixture()
        filtered = self.derived(original, lambda record: record if record.rank == 1 else None)
        plan = self.assert_native_matches(filtered, self.recipe(filtered))
        self.assertEqual([row["records"] for row in plan["outputs"]], [1])
        empty = self.derived(original, lambda _record: None)
        plan = self.assert_native_matches(empty, self.recipe(empty))
        self.assertEqual(plan["outputs"], [])
        self.assertEqual(plan["source_records"], 0)

    def test_multiblock_source_finishes_every_block_before_proof(self):
        source = self.fixture(count=5002)
        self.assertGreater(len(source.blocks), 1)
        self.assert_native_matches(source, self.recipe(source))

    def test_create_after_reader_cache_eviction_does_not_scan_in_python(self):
        source = self.fixture()
        plan = workflow.preview(source, self.recipe(source), native_helper=self.helper)
        fresh = organizer.BSPoolReader(source.path, verify_payloads=False)
        self.assertFalse(fresh._composite_metadata_verified)
        with mock.patch.object(fresh, "iter_records", side_effect=AssertionError("slow creation scan")), \
                mock.patch.object(fresh, "_read_validated_block_records",
                                  side_effect=AssertionError("slow source verification")):
            report, completed = workflow.publish(
                fresh, plan, self.folder / "recovered", native_helper=self.helper.runner)
        self.assertTrue(completed)
        self.assertEqual(report["engine"], "native")
        self.assertTrue(fresh._composite_metadata_verified)
        self.assertEqual(sorted(row["records"] for row in report["outputs"]), [3, 3])
        for output in report["outputs"]:
            verified = organizer.BSPoolReader(output["path"])
            self.assertEqual(verified.records, 3)

    def test_nested_latest_inputs_and_original_branches_remain_distinct(self):
        first = self.fixture("intersection")
        third = self.folder / "Third.bspool"
        tag = descriptor(1, "tag_negative", 3, 1, 0, 0, 0)
        write_custom_bsp3(str(third), [3, 5], [[tag], [tag]], "1111111111111111",
                         ["tag tag_negative 3 7 1"])
        combined = self.folder / "Nested.bspool"
        organizer.combine_pools([first, organizer.BSPoolReader(third)], str(combined))
        Path(first.path).unlink()
        third.unlink()
        for kind, groups, memberships, overlaps in (("inputs", 2, 4, 1), ("branches", 3, 6, 2)):
            with self.subTest(kind=kind):
                reader = organizer.BSPoolReader(combined, verify_payloads=False)
                plan = self.assert_native_matches(reader, self.recipe(reader, kind))
                self.assertEqual(len(plan["outputs"]), groups)
                self.assertEqual((plan["copied_records"], plan["output_memberships"],
                                  plan["overlap_records"]), (3, memberships, overlaps))

    def test_sixty_fourth_operand_and_more_than_128_original_branches(self):
        tag = descriptor(1, "tag_negative", 3, 1, 0, 0, 0)
        groups = []
        for group, size in enumerate((64, 64, 1)):
            paths = []
            for index in range(size):
                path = self.folder / ("many-%d-%d.bspool" % (group, index))
                write_custom_bsp3(str(path), [1], [[tag]],
                                 "%016x" % (0x10000 + group * 64 + index),
                                 ["tag tag_negative 3 7 1"])
                paths.append(path)
            if size == 1:
                groups.append(paths[0])
                continue
            target = self.folder / ("group-%d.bspool" % group)
            organizer.combine_pools([organizer.BSPoolReader(path) for path in paths], str(target))
            for path in paths:
                path.unlink()
            groups.append(target)
        reader = organizer.BSPoolReader(groups[0], verify_payloads=False)
        plan = self.assert_native_matches(reader, self.recipe(reader))
        self.assertEqual(len(plan["outputs"]), 64)
        self.assertEqual(plan["output_memberships"], 64)
        combined = self.folder / "129-branches.bspool"
        organizer.combine_pools([organizer.BSPoolReader(path) for path in groups], str(combined))
        for path in groups:
            path.unlink()
        reader = organizer.BSPoolReader(combined, verify_payloads=False)
        plan = self.assert_native_matches(reader, self.recipe(reader, "branches"))
        self.assertEqual(len(plan["outputs"]), 129)
        self.assertEqual((plan["copied_records"], plan["overlap_records"],
                          plan["output_memberships"]), (1, 1, 129))

    def test_invalid_markers_on_excluded_record_still_reject_the_whole_preview(self):
        source = self.fixture()
        selected = self.selected_for_rank(source, 1)
        unknown_id = organizer.MASK64
        for mode in ("missing_branch", "missing_operand", "unknown_branch", "unknown_operand"):
            def change(record):
                if record.rank != 4:
                    return record
                items = list(record.occurrences)
                if mode.startswith("missing"):
                    items = [item for item in items if not (
                        item.is_provenance if mode == "missing_branch" else item.is_operand)]
                else:
                    raw = (organizer.provenance_descriptor(unknown_id) if mode == "unknown_branch"
                           else organizer.operand_descriptor(unknown_id))
                    items.append(organizer.Occurrence.decode(raw))
                return organizer.Record(record.rank, tuple(items))
            with self.subTest(mode=mode):
                invalid = self.derived(source, change)
                recipe = self.recipe(invalid, selected=selected)
                with self.assertRaises(organizer.PoolError):
                    self.exact_python(invalid, recipe)
                with self.assertRaises(organizer.PoolError):
                    native.preview_sources(invalid, recipe, self.helper)
                self.assertFalse(invalid._payload_verified)
                self.assertFalse(invalid._composite_metadata_verified)

    def test_valid_header_expression_must_hold_for_each_seed(self):
        for operation in ("intersection", "difference"):
            with self.subTest(operation=operation):
                source = self.fixture()
                selected = self.selected_for_rank(source, 1)
                payload = Path(source.path).read_bytes()
                expression = dict(source.composite_expression, op=operation)
                header = re.sub(r"(?m)^composite_operation union$",
                                "composite_operation " + operation, source.header.text)
                header, changed = re.subn(
                    r"(?m)^composite_expression .+$", "composite_expression " + organizer._header_token(
                        json.dumps(expression, separators=(",", ":"), sort_keys=True)), header)
                self.assertEqual(changed, 1)
                Path(source.path).write_bytes(header.encode().ljust(source.header_bytes, b"\0")
                                              + payload[source.header_bytes:])
                invalid = organizer.BSPoolReader(source.path, verify_payloads=False)
                recipe = self.recipe(invalid, selected=selected)
                with self.assertRaises(organizer.PoolError):
                    self.exact_python(invalid, recipe)
                with self.assertRaises(organizer.PoolError):
                    native.preview_sources(invalid, recipe, self.helper)
                self.assertFalse(invalid._composite_metadata_verified)

    def test_final_block_corruption_never_promotes_partial_proof(self):
        for column in ("ranks", "metadata"):
            with self.subTest(column=column):
                source = self.fixture(count=5002)
                block = source.blocks[-1]
                offset = block.offset + block.header_bytes
                if column == "metadata":
                    offset += block.rank_bytes + block.metadata_bytes - 1
                with open(source.path, "r+b") as handle:
                    handle.seek(offset)
                    value = handle.read(1)
                    handle.seek(-1, os.SEEK_CUR)
                    handle.write(bytes((value[0] ^ 1,)))
                invalid = organizer.BSPoolReader(source.path, verify_payloads=False)
                with self.assertRaises(organizer.PoolError):
                    native.preview_sources(invalid, self.recipe(invalid), self.helper)
                self.assertFalse(invalid._payload_verified)
                self.assertFalse(invalid._composite_metadata_verified)

    def test_stale_helper_falls_back_to_python_without_claiming_native_proof(self):
        source = self.fixture()
        old = mock.Mock()
        old.preview_sources.side_effect = organizer.NativeSplitUnsupported("unknown preview-sources mode")
        for helper in (old, object()):
            with self.subTest(helper=helper):
                fresh = organizer.BSPoolReader(source.path, verify_payloads=False)
                with self.assertRaises(organizer.NativeSplitUnsupported):
                    native.preview_sources(fresh, self.recipe(fresh), helper)
                self.assertFalse(fresh._payload_verified)
                self.assertFalse(fresh._composite_metadata_verified)
                plan = workflow.preview(fresh, self.recipe(fresh), native_helper=helper)
                self.assertEqual(plan["engine"], "python")
                self.assertEqual(plan["copied_records"], 4)
                self.assertTrue(fresh._composite_metadata_verified)

    def test_bad_result_identity_or_incomplete_proof_is_never_accepted(self):
        source = self.fixture()
        recipe = self.recipe(source)
        actual = self.helper.preview_sources
        transforms = (
            lambda text: text.replace("BRAINSTORM_SOURCE_PREVIEW_RESULT 1", "BRAINSTORM_SOURCE_PREVIEW_RESULT 2"),
            lambda text: re.sub(r"(?m)^source_records \d+$", "source_records 1", text),
            lambda text: re.sub(r"(?m)^source_membership_digest [0-9a-f]+$", "source_membership_digest 0000000000000000", text),
            lambda text: re.sub(r"(?m)^source_metadata_digest [0-9a-f]+$", "source_metadata_digest 0000000000000000", text),
            lambda text: re.sub(r"(?m)^memberships \d+$", "memberships 1", text),
            lambda text: re.sub(r"(?m)^source 0 [0-9a-f]+ (\d+)$", r"source 0 ffffffffffffffff \1", text),
            lambda text: re.sub(r"(?m)^source 0 .+\n", "", text),
            lambda text: re.sub(r"(?m)^(source 0 .+)$", r"\1\n\1", text),
            lambda text: re.sub(r"(?m)^(copied \d+)$", r"\1\n\1", text),
            lambda text: re.sub(r"(?m)^overlap \d+$", "overlap 5", text),
            lambda text: re.sub(r"(?m)^excluded \d+$", "excluded 1", text),
            lambda text: re.sub(r"(?m)^source_metadata_digest [0-9a-f]+$", "source_metadata_digest xyz", text),
            lambda text: re.sub(r"(?m)^source_records \d+$", "source_records 18446744073709551616", text),
            lambda text: text.replace("end\n", "unexpected 1\nend\n"),
            lambda text: text.replace("end\n", ""),
        )
        for transform in transforms:
            fresh = organizer.BSPoolReader(source.path, verify_payloads=False)
            helper = mock.Mock()
            helper.preview_sources.side_effect = lambda *args, **kwargs: transform(actual(*args, **kwargs))
            with self.subTest(transform=transform), self.assertRaises(organizer.PoolError):
                native.preview_sources(fresh, recipe, helper)
            self.assertFalse(fresh._payload_verified)
            self.assertFalse(fresh._composite_metadata_verified)

    def test_cancel_after_helper_finishes_does_not_accept_its_proof(self):
        source = self.fixture()
        finished = False
        actual = self.helper.preview_sources
        helper = mock.Mock()

        def complete(*args, **kwargs):
            nonlocal finished
            result = actual(*args, **kwargs)
            finished = True
            return result

        helper.preview_sources.side_effect = complete
        with self.assertRaises(organizer.PoolError):
            native.preview_sources(source, self.recipe(source), helper,
                                   cancel_check=lambda: finished)
        self.assertFalse(source._payload_verified)
        self.assertFalse(source._composite_metadata_verified)

    def test_progress_then_cancellation_does_not_promote_partial_proof(self):
        source = self.fixture()
        events = []
        helper = mock.Mock()
        failure = organizer.PoolError("cancelled during native scan")

        def partial(_source, _plan, cancel_check=None, progress=None):
            progress(2, 4)
            raise failure

        helper.preview_sources.side_effect = partial
        with self.assertRaises(organizer.PoolError) as caught:
            native.preview_sources(source, self.recipe(source), helper,
                                   progress=lambda done, total: events.append((done, total)))
        self.assertIs(caught.exception, failure)
        self.assertEqual(events, [(2, 4)])
        self.assertFalse(source._payload_verified)
        self.assertFalse(source._composite_metadata_verified)

    def test_source_replaced_during_native_scan_is_refused(self):
        source = self.fixture()
        replacement = self.folder / "replacement.bspool"
        shutil.copyfile(source.path, replacement)
        actual = self.helper.preview_sources
        helper = mock.Mock()

        def replace_after_read(*args, **kwargs):
            result = actual(*args, **kwargs)
            os.replace(replacement, source.path)
            return result

        helper.preview_sources.side_effect = replace_after_read
        with self.assertRaises((organizer.PoolError, OSError)) as failure:
            native.preview_sources(source, self.recipe(source), helper)
        if os.name == "nt":
            self.assertIsInstance(failure.exception, PermissionError)
        else:
            self.assertRegex(str(failure.exception), "changed|replaced")
        self.assertFalse(source._payload_verified)
        self.assertFalse(source._composite_metadata_verified)

    def test_direct_cli_rejects_malformed_plans_without_results_or_output_files(self):
        source = self.fixture()
        document, _selected = native._preview_plan(source, self.recipe(source))
        valid = document.decode("ascii")
        branch = next(line for line in valid.splitlines() if line.startswith("branch "))
        declared = next(line for line in valid.splitlines() if line.startswith("declared "))
        first_source = next(line.split()[2] for line in valid.splitlines() if line.startswith("source 0 "))
        leaf = "o" + declared.split()[1]
        invalid = {
            "version": valid.replace("BRAINSTORM_SOURCE_PREVIEW_PLAN 1", "BRAINSTORM_SOURCE_PREVIEW_PLAN 2"),
            "missing end": valid.replace("end\n", ""),
            "unknown kind": valid.replace("kind inputs", "kind unknown"),
            "unknown selected ID": re.sub(r"(?m)^source 0 .+$", "source 0 ffffffffffffffff", valid),
            "unknown expression ID": re.sub(r"o[0-9a-f]{16}", "offffffffffffffff", valid, count=1),
            "invalid expression stack": re.sub(r"(?m)^expr .+$", "expr u2", valid),
            "duplicate branch": valid.replace(branch + "\n", branch + "\n" + branch + "\n"),
            "duplicate operand": valid.replace(declared + "\n", declared + "\n" + declared + "\n"),
            "duplicate selected ID": re.sub(r"(?m)^source 1 .+$", "source 1 " + first_source, valid),
            "source index gap": valid.replace("source 1 ", "source 2 "),
            "excess expression tokens": re.sub(r"(?m)^expr .+$", "expr " + " ".join([leaf] * 513), valid),
            "trailing text": valid + "source 2 ffffffffffffffff\n",
        }
        plan_path = self.folder / "direct-preview-plan.txt"
        for label, text in invalid.items():
            with self.subTest(plan=label):
                plan_path.write_text(text, encoding="ascii")
                before = set(self.folder.iterdir())
                result = subprocess.run([str(self.binary), "preview-sources", source.path, str(plan_path)],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertIn("source preview plan", result.stderr)
                self.assertNotIn("AddressSanitizer", result.stderr)
                self.assertNotIn("runtime error:", result.stderr)
                self.assertEqual(set(self.folder.iterdir()), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
