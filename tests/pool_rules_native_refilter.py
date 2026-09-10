#!/usr/bin/env python3
"""Real native refilters must retain the limits of recorded-rule subsets."""

import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import brainstorm_pool_builder as builder
import brainstorm_pool_organizer as organizer
import pool_rule_workflow as workflow


class NativeRuleRefilter(unittest.TestCase):
    def test_location_subset_cannot_replace_complete_broader_pool(self):
        from pool_organizer import descriptor, write_custom_bsp3

        with tempfile.TemporaryDirectory(prefix="brainstorm-location-coverage-") as folder:
            directory = Path(folder)
            original = directory / "original.bspool"
            events = [[descriptor(1, "tag_negative", 3, 1, 0, 0, 0),
                       descriptor(1, "tag_rare", ante, 2, 0, 0, 0)]
                      for ante in (5, 6)]
            write_custom_bsp3(str(original), [1, 2], events, "1111111111111111",
                             ["tag_route observe", "tag tag_negative 3 7 1",
                              "tag tag_rare 3 7 1"],
                             range_end=organizer.NATURAL_SEEDSPACE)
            source = organizer.BSPoolReader(original)
            report, completed = organizer.split_pool(
                source, str(directory / "split"),
                ["tag:tag_rare:A5:big:none:o0:none"], None, None, None, True)
            self.assertTrue(completed)
            self.assertEqual(len(report["outputs"]), 1)
            output = organizer.BSPoolReader(report["outputs"][0]["path"])
            self.assertEqual(output.records, 1)
            self.assertTrue(output.complete)
            self.assertFalse(output.coverage_complete)
            self.assertEqual(output.header.one("source_coverage_complete"), "1")
            self.assertEqual(output.header.one("parent_coverage_complete"), "1")
            info = builder.PoolInfo(output.path).as_dict()
            self.assertFalse(info["attachment_authoritative_eligible"])

    def test_two_native_refilters_preserve_restricted_coverage(self):
        scanner = ROOT / "native" / (
            "brainstorm_seed_pool.exe" if os.name == "nt" else "brainstorm_seed_pool")
        lua = shlex.split(os.environ.get("LUAJIT", "")) or ["luajit"]
        if os.name == "nt" and lua[0].startswith("/") and shutil.which("cygpath"):
            lua[0] = subprocess.check_output(["cygpath", "-w", lua[0]], text=True).strip()
        explicit_snapshot = os.environ.get("BRAINSTORM_TEST_SNAPSHOT")
        snapshot_path = explicit_snapshot or str(ROOT / "native_search.cfg")
        if os.name == "nt" and snapshot_path.startswith("/") and shutil.which("cygpath"):
            snapshot_path = subprocess.check_output(
                ["cygpath", "-w", snapshot_path], text=True).strip()
        snapshot = Path(snapshot_path)
        if not scanner.is_file() or not snapshot.is_file() or not (
                shutil.which(lua[0]) or Path(lua[0]).is_file()):
            message = ("Native scanner, current snapshot, and LuaJIT are required; "
                       "snapshot: %s" % snapshot)
            if explicit_snapshot or os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
                self.fail(message)
            self.skipTest(message)

        with tempfile.TemporaryDirectory(prefix="brainstorm-rule-refilter-") as folder:
            directory = Path(folder)
            private_snapshot = directory / "input-snapshot.cfg"
            snapshot_text = snapshot.read_text(encoding="utf-8")
            # The CI oracle uses synthetic tag_3/tag_4 in these two slots.
            # Only rename their keys in our private copy; their slot order,
            # eligibility, Ante limits, and every other catalog fact stay put.
            for target, synthetic in (("tag_negative", "tag_3"), ("tag_rare", "tag_4")):
                if not re.search(r"(?m)^tagdef\s+" + target + r"\s", snapshot_text):
                    snapshot_text, replaced = re.subn(
                        r"(?m)^(tagdef\s+)" + synthetic + r"(?=\s)",
                        lambda match: match[1] + target, snapshot_text)
                    self.assertEqual(replaced, 1, "Snapshot needs " + target)
            private_snapshot.write_text(snapshot_text, encoding="utf-8")
            aligned = directory / "snapshot.cfg"
            aligned.write_bytes(subprocess.check_output(
                lua + [str(ROOT / "tests" / "align_snapshot_prng.lua"), str(private_snapshot)],
                cwd=ROOT, timeout=30))
            criteria = directory / "tags.cfg"
            criteria.write_text("\n".join([
                "poolver 1", "threads 1", "start 0", "count 2000",
                "checkpoint 2000", "chunk 2048", "resume 0", "format binary",
                "output_schema 4", "tag_route observe",
                "tag tag_negative 3 small 7 big 1",
                "tag tag_rare 3 small 7 big 1", "end", "",
            ]), encoding="ascii")

            def native(command, *paths):
                result = subprocess.run([str(scanner), command, *map(str, paths)],
                                        cwd=ROOT, text=True, capture_output=True, timeout=45)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            original = directory / "original.bspool"
            native("scan", aligned, criteria, original)
            source = organizer.BSPoolReader(original)
            self.assertGreater(source.records, 0)
            self.assertTrue(source.coverage_complete)
            recipe = {"version": 1, "mode": "second_tag", "rule": {
                "version": 1, "range": {"start": "A3S", "end": "A7B"}}}
            plan = workflow.preview(source, recipe)
            report, _ = workflow.publish(source, plan, directory / "classified")
            location = next(row for row in organizer.analyze(source)["categories"]
                            if row["key"] == "tag_rare" and row["records"] < source.records)
            split, completed = organizer.split_pool(
                source, str(directory / "location"), [location["category_id"]],
                None, None, None, True)
            self.assertTrue(completed)
            starting_pools = {
                "rule": Path(report["outputs"][0]["path"]),
                "location": Path(split["outputs"][0]["path"]),
            }

            # Metadata-only classification adds a requirement the native
            # attachment matcher cannot express. It must never regain a
            # complete-coverage claim when the native writer drops recipe
            # annotations during one or more subsequent refilters.
            for kind, current in starting_pools.items():
                original_assignments = None
                for generation in range(3):
                    with self.subTest(kind=kind, generation=generation):
                        reader = organizer.BSPoolReader(current)
                        self.assertTrue(reader.complete)
                        self.assertFalse(reader.coverage_complete)
                        self.assertGreater(reader.records, 0)
                        assignments = {row["key"]: row["records"] for row in
                                       workflow.preview(reader, recipe)["outputs"]}
                        if original_assignments is None:
                            original_assignments = assignments
                        self.assertEqual(assignments, original_assignments)
                        info = builder.PoolInfo(str(current)).as_dict()
                        self.assertFalse(info["attachment_authoritative_eligible"])
                        if generation:
                            self.assertEqual(reader.header.one("source_coverage_complete"), "0")
                            self.assertEqual(reader.header.one("parent_coverage_complete"), "0")
                        else:
                            self.assertEqual(reader.header.one("source_coverage_complete"), "1")
                            self.assertEqual(reader.header.one("parent_coverage_complete"), "1")
                        if generation < 2:
                            target = directory / ("%s-refilter-%d.bspool" % (kind, generation + 1))
                            native("refilter", aligned, criteria, current, target)
                            current = target


if __name__ == "__main__":
    unittest.main()
