#!/usr/bin/env python3
"""End-to-end recovery and publication tests using independently encoded pools."""

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_organizer as organizer
import brainstorm_pool_builder as builder
import pool_rule_workflow as workflow
from pool_organizer import descriptor, write_custom_bsp3, write_empty_bsp3


def tag(key, ante, phase):
    return descriptor(1, "tag_" + key, ante, phase, 0, 0, 0)


class RuleWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = self.temp.name

    def fixture(self, name, ranks, events=None, criteria=None):
        path = os.path.join(self.folder, name + ".bspool")
        write_custom_bsp3(
            path, ranks, events or [[tag("negative", 3, 1)] for _ in ranks],
            workflow._fingerprint(name)[:16],
            criteria or ["tag_route observe", "tag tag_negative 3 small 7 big 1",
                         "tag tag_rare 3 small 7 big 1"])
        with open(path, "r+b") as handle:
            old = handle.read(8192)
            text = old.split(b"\0", 1)[0].decode("ascii")
            text = "\n".join("label " + name if line.startswith("label ") else line
                             for line in text.splitlines()) + "\n"
            handle.seek(0)
            handle.write(text.encode("ascii").ljust(8192, b"\0"))
        return path

    def combined(self):
        x = self.fixture("AS1-L1", [1, 2, 3])
        y = self.fixture("AS1-L2", [2, 3, 4],
                         [[tag("rare", 5, 2)] for _ in range(3)])
        z = os.path.join(self.folder, "AS1-Complete.bspool")
        organizer.combine_pools([organizer.BSPoolReader(x), organizer.BSPoolReader(y)],
                                z, "union", "AS1 Complete")
        return x, y, z

    def recipe(self, kind="inputs", ids=None):
        return {"version": 1, "mode": "separate_sources", "source_kind": kind,
                "source_ids": ids or []}

    def second_recipe(self):
        return {"version": 1, "mode": "second_tag", "rule": {
            "version": 1, "name": "AS1-L1", "range": {"start": "A3S", "end": "A7B"}}}

    def test_recover_deleted_sources_including_overlap_and_preserved_evidence(self):
        x, y, z = self.combined()
        os.unlink(x)
        os.unlink(y)
        reader = organizer.BSPoolReader(z)
        described = workflow.describe_source(reader)
        self.assertEqual(sorted(row["records"] for row in described["direct_inputs"]), [3, 3])
        self.assertEqual(described["overlap_records"]["inputs"], 2)
        plan = workflow.preview(reader, self.recipe())
        self.assertEqual((plan["copied_records"], plan["output_memberships"]), (4, 6))
        report, ok = workflow.publish(reader, plan, os.path.join(self.folder, "restored"))
        self.assertTrue(ok)
        actual = {}
        original = {record.rank: tuple(item.raw for item in record.occurrences)
                    for record in reader.iter_records()}
        for output in report["outputs"]:
            restored = organizer.BSPoolReader(output["path"])
            self.assertEqual(restored.schema, 4)
            self.assertTrue(restored.is_composite)
            self.assertEqual(restored.composite_expression, reader.composite_expression)
            self.assertEqual(restored.composite_branches, reader.composite_branches)
            self.assertEqual(restored.composite_operands, reader.composite_operands)
            records = list(restored.iter_records())
            name = os.path.basename(output["path"])
            actual["L1" if "L1" in name else "L2"] = [record.rank for record in records]
            for record in records:
                self.assertEqual(tuple(item.raw for item in record.occurrences), original[record.rank])
            saved = json.loads(organizer._decode_header_token(restored.header.one("workflow_recipe")))
            self.assertEqual(saved, plan["recipe"])
            self.assertNotEqual(restored.snapshot_token, reader.snapshot_token)
        self.assertEqual(actual, {"L1": [1, 2, 3], "L2": [2, 3, 4]})
        with open(report["report_path"], encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["plan_id"], plan["plan_id"])

    def test_nested_inputs_and_leaf_branches_are_distinct(self):
        x, y, xy = self.combined()
        third = self.fixture("AS1-Other", [5])
        z = os.path.join(self.folder, "Nested-Complete.bspool")
        organizer.combine_pools([organizer.BSPoolReader(xy), organizer.BSPoolReader(third)],
                                z, "union", "Nested")
        for path in (x, y, xy, third):
            os.unlink(path)
        reader = organizer.BSPoolReader(z)
        described = workflow.describe_source(reader)
        self.assertEqual(len(described["direct_inputs"]), 2)
        self.assertEqual(len(described["original_sources"]), 3)
        self.assertTrue(any("Complete" in row["label"] for row in described["direct_inputs"]))
        self.assertFalse(any("Complete" in row["label"] for row in described["original_sources"]))
        l1 = next(row for row in described["original_sources"] if row["label"] == "AS1-L1")
        plan = workflow.preview(reader, self.recipe("branches", [l1["id"]]))
        report, _ = workflow.publish(reader, plan, os.path.join(self.folder, "leaf"))
        self.assertEqual([r.rank for r in organizer.BSPoolReader(report["outputs"][0]["path"]).iter_records()],
                         [1, 2, 3])

    def test_later_filter_cannot_restore_removed_seeds(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        described = workflow.describe_source(reader)
        x = next(row for row in described["direct_inputs"] if "L1" in row["label"])
        plan = workflow.preview(reader, self.recipe(ids=[x["id"]]))
        report, _ = workflow.publish(reader, plan, os.path.join(self.folder, "partial"))
        partial = organizer.BSPoolReader(report["outputs"][0]["path"])
        y = next(row for row in workflow.describe_source(partial)["direct_inputs"] if "L2" in row["label"])
        self.assertEqual((y["records"], y["original_records"], y["missing_records"]), (2, 3, 1))
        second_plan = workflow.preview(partial, self.recipe(ids=[y["id"]]))
        self.assertEqual(second_plan["copied_records"], 2)

    def test_second_tag_outputs_exclusions_and_repeat_filter(self):
        path = self.fixture("Tags", [1, 2, 3, 4, 5], [
            [tag("negative", 3, 1), tag("rare", 5, 2)],
            [tag("rare", 3, 1), tag("negative", 3, 2), tag("negative", 6, 1)],
            [tag("rare", 4, 1), tag("negative", 4, 2)],
            [tag("rare", 3, 2), tag("negative", 5, 1)],
            [tag("negative", 3, 1)],
        ])
        reader = organizer.BSPoolReader(path)
        plan = workflow.preview(reader, self.second_recipe(), prefix="AS1-L1")
        self.assertEqual(plan["copied_records"], 3)
        self.assertEqual(plan["exclusions"], {"missing_opposite_tag": 1, "same_ante_only": 1})
        self.assertEqual({row["key"] for row in plan["outputs"]},
                         {"a5b-rare", "a6s-negative", "a5s-negative"})
        report, _ = workflow.publish(reader, plan, os.path.join(self.folder, "tags"))
        for output in report["outputs"]:
            second = organizer.BSPoolReader(output["path"])
            self.assertEqual(workflow.preview(second, self.second_recipe())["copied_records"], 1)
            self.assertEqual(organizer._reader_criteria(second), organizer._reader_criteria(reader))

    def test_unknown_coverage_is_an_error_not_an_excluded_seed(self):
        path = self.fixture("Unknown", [1], criteria=["tag_route observe", "tag tag_negative 3 small 7 big 1"])
        with self.assertRaises(organizer.PoolError):
            workflow.preview(organizer.BSPoolReader(path), self.second_recipe())

    def test_changed_source_is_rejected_even_when_checkpoint_matches(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        plan = workflow.preview(reader, self.recipe())
        status = os.stat(z)
        os.utime(z, ns=(status.st_atime_ns, status.st_mtime_ns + 1000000000))
        with self.assertRaisesRegex(organizer.PoolError, "changed|replaced"):
            workflow.publish(reader, plan, os.path.join(self.folder, "stale"))
        with self.assertRaisesRegex(organizer.PoolError, "changed"):
            workflow.publish(organizer.BSPoolReader(z), plan, os.path.join(self.folder, "stale"))

    def test_changed_recipe_or_counts_requires_new_preview(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        plan = workflow.preview(reader, self.recipe())
        for change in ("recipe", "counts"):
            altered = copy.deepcopy(plan)
            if change == "recipe":
                altered["recipe"]["source_kind"] = "branches"
            else:
                altered["outputs"][0]["records"] += 1
            with self.assertRaisesRegex(organizer.PoolError, "changed"):
                workflow.publish(reader, altered, os.path.join(self.folder, change))

    def test_publication_recounts_even_with_a_recomputed_plan_fingerprint(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        plan = workflow.preview(reader, self.recipe())
        plan["outputs"][0]["records"] -= 1
        del plan["plan_id"]
        plan["plan_id"] = workflow._fingerprint(plan)
        output_dir = os.path.join(self.folder, "wrong-count")
        with self.assertRaisesRegex(organizer.PoolError, "counts"):
            workflow.publish(reader, plan, output_dir)
        self.assertFalse(any(name.endswith((".bspool", ".tmp", ".json")) for name in os.listdir(output_dir)))

    def test_collision_preserves_existing_output_and_writes_nothing_else(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        plan = workflow.preview(reader, self.recipe())
        output_dir = os.path.join(self.folder, "collision")
        os.makedirs(output_dir)
        path = os.path.join(output_dir, plan["outputs"][0]["name"])
        with open(path, "wb") as handle:
            handle.write(b"keep me")
        with self.assertRaisesRegex(organizer.PoolError, "already exists"):
            workflow.publish(reader, plan, output_dir)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"keep me")
        self.assertEqual(len([name for name in os.listdir(output_dir) if name.endswith(".bspool")]), 1)

    def test_existing_companion_file_blocks_publication(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        plan = workflow.preview(reader, self.recipe())
        output_dir = os.path.join(self.folder, "sidecar")
        os.makedirs(output_dir)
        path = os.path.join(output_dir, plan["outputs"][0]["name"] + ".attached")
        with open(path, "wb") as handle:
            handle.write(b"keep marker")
        with self.assertRaisesRegex(organizer.PoolError, "companion"):
            workflow.publish(reader, plan, output_dir)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"keep marker")
        self.assertFalse(any(name.endswith(".bspool") for name in os.listdir(output_dir)))

    def test_corrupted_staged_pool_never_becomes_visible(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        plan = workflow.preview(reader, self.recipe())
        output_dir = os.path.join(self.folder, "corrupt-stage")
        original = organizer.BSP4OutputWriter.finalize

        def corrupt_stage(writer):
            output = original(writer)
            with open(writer.temp_path, "r+b") as handle:
                handle.seek(writer.header_bytes + organizer.BLOCK4_HEADER_BYTES)
                value = handle.read(1)
                handle.seek(-1, os.SEEK_CUR)
                handle.write(bytes([value[0] ^ 1]))
            return output

        with mock.patch.object(organizer.BSP4OutputWriter, "finalize", corrupt_stage):
            with self.assertRaises(organizer.PoolError):
                workflow.publish(reader, plan, output_dir)
        self.assertFalse(any(name.endswith((".bspool", ".tmp", ".json")) for name in os.listdir(output_dir)))

    def test_cancellation_during_copy_and_after_publication_rolls_back(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        plan = workflow.preview(reader, self.recipe())
        for after_link in (False, True):
            cancelled = [False]
            output_dir = os.path.join(self.folder, "cancel-" + str(after_link))
            original = organizer.seed_pool_mutations.link_many_no_overwrite

            def publish_then_cancel(publications):
                result = original(publications)
                cancelled[0] = True
                return result

            with mock.patch.object(organizer.seed_pool_mutations, "link_many_no_overwrite",
                                   side_effect=publish_then_cancel if after_link else original):
                with self.assertRaisesRegex(organizer.PoolError, "cancelled"):
                    workflow.publish(reader, plan, output_dir,
                                     cancel_check=lambda: cancelled[0],
                                     progress=None if after_link else lambda *_: cancelled.__setitem__(0, True))
            self.assertFalse(any(name.endswith((".bspool", ".tmp", ".json")) for name in os.listdir(output_dir)))
            self.assertEqual(organizer.BSPoolReader(z).records, 4)

    def test_report_link_failure_rolls_back_all_outputs(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        plan = workflow.preview(reader, self.recipe())
        output_dir = os.path.join(self.folder, "report-failure")
        original = os.link

        def fail_report(source, target, *args, **kwargs):
            if target.endswith(".json"):
                raise OSError("report link failed")
            return original(source, target, *args, **kwargs)

        with mock.patch("os.link", side_effect=fail_report):
            with self.assertRaisesRegex(OSError, "report link failed"):
                workflow.publish(reader, plan, output_dir)
        self.assertFalse(any(name.endswith((".bspool", ".tmp", ".json")) for name in os.listdir(output_dir)))

    def test_empty_pool_and_missing_source_selection(self):
        path = os.path.join(self.folder, "empty.bspool")
        write_empty_bsp3(path)
        reader = organizer.BSPoolReader(path)
        self.assertFalse(workflow.describe_source(reader)["can_separate"])
        with self.assertRaisesRegex(organizer.PoolError, "no saved source"):
            workflow.preview(reader, self.recipe())
        _x, _y, z = self.combined()
        with self.assertRaisesRegex(organizer.PoolError, "no longer recorded"):
            workflow.preview(organizer.BSPoolReader(z), self.recipe(ids=["0000000000000000"]))

    def test_progress_is_bounded_and_preview_contains_no_seed_list(self):
        _x, _y, z = self.combined()
        reader = organizer.BSPoolReader(z)
        updates = []
        plan = workflow.preview(reader, self.recipe(), progress=lambda *args: updates.append(args))
        self.assertEqual(updates[-1], (4, 4))
        self.assertFalse({"seeds", "assignments", "ranks"} & set(plan))
        self.assertEqual(len(plan["outputs"]), 2)

    def test_cli_previews_and_publishes_but_never_overwrites_existing_files(self):
        _x, _y, z = self.combined()
        recipe_path = os.path.join(self.folder, "recipe.json")
        plan_path = os.path.join(self.folder, "preview.json")
        output_dir = os.path.join(self.folder, "cli-output")
        with open(recipe_path, "w", encoding="utf-8") as handle:
            json.dump(self.recipe(), handle)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(workflow.main(["preview", z, recipe_path, plan_path]), 0)
            self.assertEqual(workflow.main(["publish", z, plan_path, output_dir]), 0)
            self.assertEqual(workflow.main(["preview", z, recipe_path, z]), 2)
            self.assertEqual(workflow.main(["preview", z, recipe_path, recipe_path]), 2)
            self.assertEqual(workflow.main(["preview", z, recipe_path, plan_path]), 2)
        self.assertEqual(organizer.BSPoolReader(z).records, 4)
        with open(recipe_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), self.recipe())

    def test_saved_json_rejects_duplicate_fields_and_requires_wrapper_version(self):
        path = os.path.join(self.folder, "duplicate.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"version":1,"version":2,"mode":"separate_sources"}')
        with self.assertRaisesRegex(organizer.PoolError, "repeats"):
            workflow._load_json(path, workflow.MAX_RECIPE_BYTES)
        with self.assertRaisesRegex(organizer.PoolError, "version"):
            workflow.normalize_recipe({"mode": "separate_sources"})

    def test_tag_destinations_sort_chronologically(self):
        keys = ["a10s-rare", "a3b-negative", "a3s-rare"]
        self.assertEqual(sorted(keys, key=lambda key: workflow._destination_sort_key(key, key)),
                         ["a3s-rare", "a3b-negative", "a10s-rare"])

    def test_loads_recipe_preserves_unicode_and_supported_nested_logic(self):
        recipe = self.second_recipe()
        recipe["rule"]["name"] = "Raré Δ"
        recipe["rule"]["range"]["start"] = "a3s"
        recipe["rule"]["condition"] = {"not": {"not": {"count": {
            "tag": "rare", "range": {"start": "A3S", "end": "A7B"}, "min": 1}}}}
        result = workflow.loads_recipe(json.dumps(recipe, ensure_ascii=False))
        self.assertEqual(result["rule"]["name"], "Raré Δ")
        self.assertEqual(result["rule"]["range"]["start"], "A3S")
        self.assertEqual(result["rule"]["condition"], recipe["rule"]["condition"])

    def test_loads_recipe_rejects_future_or_unknown_fields_without_dropping_them(self):
        cases = []
        future = self.second_recipe()
        future["version"] = 2
        cases.append(future)
        unknown_wrapper = self.second_recipe()
        unknown_wrapper["ignore_missing"] = True
        cases.append(unknown_wrapper)
        unknown_range = self.second_recipe()
        unknown_range["rule"]["range"]["inclusive"] = False
        cases.append(unknown_range)
        cases.append(self.second_recipe()["rule"])
        for recipe in cases:
            with self.subTest(recipe=recipe):
                with self.assertRaises(organizer.PoolError):
                    workflow.loads_recipe(json.dumps(recipe))

    def test_loads_recipe_rejects_duplicate_nested_fields(self):
        text = json.dumps(self.second_recipe()).replace('"start": "A3S"',
                                                       '"start":"A4S","start":"A3S"')
        with self.assertRaisesRegex(organizer.PoolError, "repeats"):
            workflow.loads_recipe(text)

    def test_loads_recipe_bounds_unicode_recursion_and_nonfinite_values(self):
        invalid = [None, b"{}", "\ud800", '{"name":"\\ud800"}',
                   '{"value":NaN}', '{"value":1e99999}',
                   "[" * 2000 + "]" * 2000,
                   " " * (workflow.MAX_RECIPE_BYTES + 1)]
        deep = self.second_recipe()
        condition = {"count": {"tag": "rare", "range": {"start": "A3S", "end": "A7B"}, "min": 1}}
        for _ in range(20):
            condition = {"not": condition}
        deep["rule"]["condition"] = condition
        invalid.append(json.dumps(deep))
        for text in invalid:
            with self.subTest(text=repr(text)[:100]):
                with self.assertRaises(organizer.PoolError):
                    workflow.loads_recipe(text)

    def test_rule_subset_is_not_authoritative_for_its_broader_native_criteria(self):
        path = os.path.join(self.folder, "full-space.bspool")
        write_custom_bsp3(
            path, [1, 2], [[tag("negative", 3, 1), tag("rare", 5, 2)],
                          [tag("negative", 3, 1), tag("rare", 6, 2)]],
            "1111111111111111", ["tag_route observe",
                                 "tag tag_negative 3 7 1", "tag tag_rare 3 7 1"],
            range_end=organizer.NATURAL_SEEDSPACE)
        source = organizer.BSPoolReader(path)
        self.assertTrue(source.coverage_complete)
        plan = workflow.preview(source, self.second_recipe())
        output_dir = os.path.join(self.folder, "not-authoritative")
        report, _ = workflow.publish(source, plan, output_dir)
        for output in report["outputs"]:
            derived = organizer.BSPoolReader(output["path"])
            self.assertFalse(derived.coverage_complete)
            self.assertTrue(derived.complete)
            self.assertTrue(derived.occurrence_metadata_complete)
            self.assertEqual(derived.header.integer("source_coverage_complete"), 1)
            self.assertEqual(derived.header.integer("parent_coverage_complete"), 1)
            self.assertFalse(output["coverage_complete"])
            # pool_id includes the conservative output coverage flag.
            material = ("%016x%016x%d-%d%s%d%d" % (
                derived.catalog_hash, derived.criteria_hash, derived.range_start,
                derived.range_end, derived.space_name, derived.records, 0)).encode("ascii")
            self.assertEqual(derived.pool_id, "%016x" % organizer.fnv64(material))
        library, _groups = builder.read_pool_library(output_dir)
        self.assertEqual(len(library), 2)
        for pool in library:
            self.assertFalse(pool["attachment_authoritative_eligible"])


if __name__ == "__main__":
    unittest.main()
