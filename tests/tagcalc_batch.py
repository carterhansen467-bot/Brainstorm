#!/usr/bin/env python3
"""Batch integrity, starting-copy semantics, and end-to-end CLI regressions."""

from contextlib import redirect_stderr, redirect_stdout, contextmanager
import csv
import io
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import tagcalc_batch as batch


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tag batch ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_batch(self, rows, *extra, name="results"):
        source = self.root / (name + ".ndjson")
        source.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        output = self.root / name
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = batch.main(["--input", str(source), "--output-dir", str(output), *extra])
        return result, output

    def read(self, output, name):
        return [json.loads(line) for line in (output / name).read_text(encoding="utf-8").splitlines()]

    def seed(self, seed="EXAMPLE", tags="n7b,r12s"):
        return {"seed": seed, "second_tag": "A4S", "tags": tags}

    def test_user_small_and_big_starting_copy_rules(self):
        self.assertEqual(batch.baseline_for_second_tag("a4small"), (4, 1))
        self.assertEqual(batch.baseline_for_second_tag("a4big"), (4, 2))
        self.assertEqual(batch.baseline_for_second_tag({"position": "A4B"}), (4, 2))
        for wrong in ("a4boss", "a0s", "a39s", None):
            with self.assertRaises(ValueError):
                batch.baseline_for_second_tag(wrong)

    def test_row_settings_take_priority_and_conflicts_fail(self):
        self.assertEqual(batch.starting_position({"second_tag": "A5S"}, "A4B")[0], (5, 1))
        self.assertEqual(batch.starting_position({"baseline_copy": "A6Boss"}, "A4B")[0], (6, 2))
        for row in ({}, {"second_tag": "A4S", "baseline_copy": "A4Boss"}):
            with self.assertRaises(ValueError):
                batch.starting_position(row)

    def test_preserves_arbitrary_metadata_and_all_tags_but_ignores_ante39(self):
        original = dict(self.seed(tags="n1b,n7b,r12s,r39b,n7b"), labels=["L1", "日本語"],
                        score=900, custom={"source": "deleted pool", "opaque": [1, 2]})
        code, output = self.run_batch([original])
        self.assertEqual(code, 0)
        result = self.read(output, "leaderboard.ndjson")[0]
        self.assertEqual(result["input"], original)
        self.assertEqual(result["negative_locations"], ["A1B", "A7B"])
        self.assertEqual(result["rare_locations"], ["A12S"])
        self.assertEqual(result["ignored_ante39_tags"], 1)
        self.assertEqual(result["baseline_copy"], "A4B")
        self.assertNotIn("NEG A1", json.dumps(result["route"]))

    def test_conflicting_tags_and_partial_or_legacy_exports_are_reported(self):
        rows = [self.seed("CONFLICT", "n7b,r7b"), self.seed("BADANTE", "n40s,r12s"),
                {"seed": "OLD", "occurrences": []},
                dict(self.seed("PARTIAL"), tag_coverage={"start": "A4S", "end": "A38B", "complete": True}),
                {"seed": "NOSECOND", "tags": "n7b,r12s"}]
        code, output = self.run_batch(rows)
        self.assertEqual(code, 2)
        errors = self.read(output, "errors.ndjson")
        self.assertEqual(len(errors), 5)
        self.assertEqual([row["input"] for row in errors], rows)
        self.assertEqual(self.read(output, "leaderboard.ndjson"), [])
        self.assertEqual(json.loads((output / "summary.json").read_text(encoding="utf-8"))["status"], "completed_with_errors")

    def test_no_future_pair_is_explicitly_unscored(self):
        code, output = self.run_batch([self.seed(tags="n3s,r4s,n7b")])
        self.assertEqual(code, 0)
        row = self.read(output, "scores.ndjson")[0]
        self.assertEqual(row["status"], "no_valid_route")
        self.assertIsNone(row["score"])
        self.assertEqual(self.read(output, "leaderboard.ndjson"), [])

    def test_top_limit_stable_ties_and_reference(self):
        rows = [self.seed("FIRST"), self.seed("SECOND"), self.seed("LOW", "n36s,r38b")]
        code, output = self.run_batch(rows, "--top", "2", "--reference-score", "1", "--reference-seed", "5MSXV6")
        self.assertEqual(code, 0)
        leaders = self.read(output, "leaderboard.ndjson")
        self.assertEqual([r["seed"] for r in leaders], ["FIRST", "SECOND"])
        self.assertTrue(all(row["beats_reference"] for row in leaders))
        self.assertEqual(len(self.read(output, "scores.ndjson")), 3)

    def test_duplicate_seeds_keep_best_record_without_consuming_leaderboard_slots(self):
        rows = [self.seed("SAME", "n36s,r38b"), self.seed("OTHER"),
                dict(self.seed("SAME"), labels=["better scenario"]), self.seed("SAME")]
        code, output = self.run_batch(rows, "--top", "2")
        self.assertEqual(code, 0)
        leaders = self.read(output, "leaderboard.ndjson")
        self.assertEqual([r["seed"] for r in leaders], ["OTHER", "SAME"])
        self.assertEqual(leaders[1]["input"], rows[2])
        self.assertEqual(len(self.read(output, "scores.ndjson")), 4)

    def test_top_distinct_seeds_match_unbounded_oracle(self):
        import random
        rng = random.Random(734)
        leaders, by_seed, all_best = [], {}, {}
        for index in range(1000):
            value = {"seed": str(rng.randrange(80)), "score": rng.randrange(100)}
            batch.retain_leader(leaders, by_seed, value, index, 17)
            previous = all_best.get(value["seed"])
            candidate = value["score"], -index, value
            if previous is None or candidate[:2] > previous[:2]:
                all_best[value["seed"]] = candidate
            self.assertLessEqual(len(by_seed), 17)
            self.assertEqual(sorted(leaders, reverse=True), sorted(all_best.values(), reverse=True)[:17])

    def test_input_failure_terminates_actual_spawned_workers(self):
        args = batch.parser().parse_args(["--workers", "2"])
        def broken_rows():
            yield self.seed()
            raise ValueError("late input error")
        before = {p.pid for p in multiprocessing.active_children()}
        with self.assertRaisesRegex(ValueError, "late input error"):
            list(batch.evaluated_rows(broken_rows(), args))
        self.assertEqual(before, {p.pid for p in multiprocessing.active_children()})

    def test_export_only_needs_no_baseline_and_round_trips_labels(self):
        row = {"seed": "ONLY", "tags": "n7b,r12s", "labels": ["L3"], "data": {"n": 9}}
        code, output = self.run_batch([row], "--export-only")
        self.assertEqual(code, 0)
        second = self.root / "second"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = batch.main(["--input", str(output / "tags.ndjson"), "--output-dir", str(second),
                               "--second-tag", "A4B"])
        self.assertEqual(code, 0)
        result = self.read(second, "leaderboard.ndjson")[0]
        self.assertEqual(result["input"]["input"], row)
        self.assertEqual(result["labels"], ["L3"])
        self.assertEqual(result["baseline_copy"], "A4Boss")

    def test_valid_typed_export_requires_trailer_and_exact_count(self):
        header = {"type": "tag_export_header", "version": 1, "metadata": {"label": "Kept"}}
        seed = dict(self.seed(), tag_coverage={"start": "A1S", "end": "A38B", "complete": True})
        for index, trailer in enumerate(([], [{"type": "tag_export_complete", "records": 2}])):
            code, output = self.run_batch([header, seed, *trailer], name="truncated%d" % index)
            self.assertEqual(code, 1)
            self.assertFalse((output / "leaderboard.ndjson").exists())
            self.assertEqual(json.loads((output / "summary.json").read_text(encoding="utf-8"))["status"], "failed")
        code, output = self.run_batch([header, seed, {"type": "tag_export_complete", "records": 1}])
        self.assertEqual(code, 0)
        self.assertIn(header, json.loads((output / "summary.json").read_text(encoding="utf-8"))["source_metadata"])

    def test_unknown_typed_version_or_missing_seed_coverage_fails(self):
        for index, (header, seed) in enumerate((
                ({"type": "tag_export_header", "version": 2}, self.seed()),
                ({"type": "tag_export_header", "version": 1}, self.seed()),
                ({"type": "tag_export_header", "version": 1, "range": {"start": "A4S", "end": "A7B"}}, self.seed()))):
            code, output = self.run_batch([header, seed], name="format%d" % index)
            self.assertEqual(code, 1)
            self.assertFalse((output / "leaderboard.ndjson").exists())

    def test_late_source_change_leaves_no_complete_tag_export_or_leaderboard(self):
        @contextmanager
        def changed(*args, **kwargs):
            yield iter([self.seed()]), [{"source": "changed on close"}]
            raise ValueError("Source changed at final check")
        with mock.patch.object(batch, "input_rows", changed):
            code, output = self.run_batch([self.seed()])
        self.assertEqual(code, 1)
        tags = self.read(output, "tags.ndjson")
        self.assertNotEqual(tags[-1]["type"], "tag_export_complete")
        self.assertFalse((output / "leaderboard.ndjson").exists())
        self.assertEqual(json.loads((output / "summary.json").read_text(encoding="utf-8"))["status"], "failed")

    def test_csv_preserves_seed_strings_and_blocks_formula_execution(self):
        source = self.root / "input.csv"
        with source.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["seed", "second_tag", "tags", "pool"])
            writer.writeheader()
            writer.writerow(dict(self.seed("001234"), pool="=HYPERLINK(\"bad\")"))
        output = self.root / "csv"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = batch.main(["--input", str(source), "--output-dir", str(output)])
        self.assertEqual(code, 0)
        self.assertEqual(self.read(output, "leaderboard.ndjson")[0]["seed"], "001234")
        with (output / "leaderboard.csv").open(encoding="utf-8-sig") as handle:
            record = next(csv.DictReader(handle))
        self.assertTrue(record["pool"].startswith("'="))

    def test_existing_output_is_untouched(self):
        code, output = self.run_batch([self.seed()])
        before = {p.name: p.read_bytes() for p in output.iterdir()}
        code, same = self.run_batch([self.seed("OTHER")])
        self.assertEqual(code, 1)
        self.assertEqual(before, {p.name: p.read_bytes() for p in same.iterdir()})

    def test_duplicate_fields_malformed_json_and_trailing_data_fail(self):
        for index, text in enumerate((
                '{"seed":"A","seed":"B"}\n', '{"seed":\n',
                '{"type":"tag_export_header"}\n{"type":"tag_export_complete","records":0}\n{}\n')):
            source = self.root / ("bad%d.jsonl" % index)
            source.write_text(text, encoding="utf-8")
            output = self.root / ("bad%d" % index)
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = batch.main(["--input", str(source), "--output-dir", str(output)])
            self.assertEqual(code, 1)
            self.assertFalse((output / "leaderboard.ndjson").exists())

    def test_multiprocessing_matches_serial_results_with_paths_containing_spaces(self):
        rows = [self.seed("S%d" % i, "n7b,r12s,n16b,r22s") for i in range(5)]
        code, serial = self.run_batch(rows, name="serial")
        source = self.root / "serial.ndjson"
        parallel = self.root / "parallel results"
        result = subprocess.run([sys.executable, str(ROOT / "tools/tagcalc.py"), "--input", str(source),
                                 "--output-dir", str(parallel), "--workers", "2"],
                                capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(code, 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((serial / "leaderboard.ndjson").read_bytes(),
                         (parallel / "leaderboard.ndjson").read_bytes())


if __name__ == "__main__":
    unittest.main()
