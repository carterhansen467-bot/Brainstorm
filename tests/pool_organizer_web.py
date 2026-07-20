#!/usr/bin/env python3
"""Focused backend/API tests for the standalone organizer web UI."""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import brainstorm_pool_organizer as organizer
import pool_organizer_web as web
import pool_builder_web as builder_web

# Reuse the independently encoded BSP2/BSP3 fixtures from the organizer's
# format regression tests without making tests/ a Python package.
FIXTURE_PATH = os.path.join(ROOT, "tests", "pool_organizer.py")
SPEC = importlib.util.spec_from_file_location("pool_organizer_fixture", FIXTURE_PATH)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class OrganizerWebRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="brainstorm-organizer-web-")
        self.source_name = "paused-source.bspool"
        self.source = os.path.join(self.temp.name, self.source_name)
        self.identity = fixture.write_bsp3(self.source, complete=False)
        self.second_name = "other-filter.bspool"
        self.second = os.path.join(self.temp.name, self.second_name)
        self.second_identity = fixture.write_custom_bsp3(
            self.second, [1, 4], [[fixture.LEGENDARY], [fixture.LEGENDARY]],
            "cccccccccccccccc", [
                "tag_route observe",
                "legendary j_perkeo 1 small 4 big 0 shop",
                "soul_depth any",
            ])

    def tearDown(self):
        self.temp.cleanup()

    def test_inspect_plan_and_publish_preserve_snapshot_lineage(self):
        report = web.inspect_source(self.source_name, self.temp.name,
                                    ambiguity_limit=10)
        self.assertEqual(report["source"]["snapshot_id"], self.identity["snapshot"])
        self.assertFalse(report["source"]["complete"])
        self.assertFalse(report["source"]["coverage_complete"])
        self.assertIn("Paused or incomplete source",
                      [row["title"] for row in report["notices"]])
        self.assertIn("Lineage is preserved",
                      [row["title"] for row in report["notices"]])

        reader = organizer.BSPoolReader(self.source)
        plan = web.build_split_plan(reader)
        self.assertEqual(plan["ambiguous_count"], 1)
        self.assertEqual(plan["unresolved_ambiguities"], 1)
        self.assertEqual(plan["unmatched_count"], 1)
        ambiguity = plan["ambiguous"][0]
        destination = next(category for category in ambiguity["candidates"]
                           if category.startswith("legendary:"))
        choices = {
            "source_snapshot_id": self.identity["snapshot"],
            "choices": {ambiguity["seed"]: destination},
        }
        resolved = web.build_split_plan(reader, choice_plan=choices)
        self.assertEqual(resolved["unresolved_ambiguities"], 0)

        request = {
            "snapshot": self.identity["snapshot"],
            "selectedCategories": None,
            "choicePlan": choices,
            "unmatchedPolicy": "remainder",
            "remainderName": "Needs review",
            "prefix": "paused-test",
        }
        locked_final = os.path.join(
            self.temp.name,
            "paused-test--" + organizer.safe_filename(destination))
        with organizer.pool_writer_guard(locked_final):
            with self.assertRaisesRegex(ValueError, "currently being written"):
                web.execute_split(self.source_name, request, self.temp.name)
        self.assertFalse(any(name.startswith("paused-test--")
                             and name.endswith(".bspool")
                             for name in os.listdir(self.temp.name)))

        result = web.execute_split(self.source_name, request, self.temp.name)
        self.assertTrue(result["completed"])
        self.assertEqual(len(result["outputs"]), 4)
        self.assertTrue(os.path.isfile(result["report_path"]))
        self.assertTrue(all(os.path.dirname(row["path"]) == self.temp.name
                            for row in result["outputs"]))

        self.assertTrue(all(row["name"].startswith("paused-test--")
                            for row in result["outputs"]))
        self.assertFalse(any(name.startswith(".organizer-stage-")
                             for name in os.listdir(self.temp.name)))

        selected_output = next(row for row in result["outputs"]
                               if row["category_id"] == destination)
        derived = organizer.BSPoolReader(selected_output["path"])
        self.assertTrue(derived.complete)
        self.assertFalse(derived.coverage_complete)
        self.assertEqual(derived.header.one("parent_snapshot_id"),
                         self.identity["snapshot"])
        self.assertEqual(derived.family_id, int(self.identity["family"], 16))

    def test_large_inspection_has_visible_busy_feedback(self):
        page = web.PAGE
        self.assertIn("Inspecting ${fmt(row.records)} seeds…", page)
        self.assertIn("button.disabled=true", page)
        self.assertIn('button.textContent="Inspect pool"', page)

    def test_stale_plan_traversal_and_unresolved_split_are_safe(self):
        with self.assertRaisesRegex(organizer.PoolError, "choose a pool"):
            web.resolve_source("../paused-source.bspool", self.temp.name)
        with self.assertRaisesRegex(organizer.PoolError, "source changed"):
            web.execute_split(self.source_name, {
                "snapshot": "0" * 16,
                "unmatchedPolicy": "omit",
            }, self.temp.name)

        reader = organizer.BSPoolReader(self.source)
        plan = web.build_split_plan(reader)
        result = web.execute_split(self.source_name, {
            "snapshot": self.identity["snapshot"],
            "choicePlan": {
                "source_snapshot_id": self.identity["snapshot"],
                "choices": {},
            },
            "unmatchedPolicy": "stop",
            "prefix": "must-not-publish",
        }, self.temp.name)
        self.assertFalse(result["completed"])
        self.assertEqual(result["unresolved_ambiguities"],
                         plan["unresolved_ambiguities"])
        self.assertFalse(any(name.startswith("must-not-publish--")
                             for name in os.listdir(self.temp.name)))
        self.assertFalse(any(name.startswith(".organizer-stage-")
                             for name in os.listdir(self.temp.name)))

    def test_mixed_filter_combine_plan_publish_and_snapshot_pin(self):
        request = {
            "sources": [self.source_name, self.second_name],
            "operation": "union",
        }
        plan = web.build_combine_plan(request, self.temp.name)
        self.assertEqual(plan["input_count"], 2)
        self.assertEqual(plan["branch_count"], 2)
        self.assertEqual(plan["operand_count"], 2)
        self.assertIn(" OR ", plan["expression_text"])
        self.assertTrue(plan["criteria_differ"])
        self.assertFalse(plan["coverage_complete"])
        self.assertIn("Different base filters preserved",
                      [row["title"] for row in plan["notices"]])

        create = dict(request, snapshots=plan["snapshots"],
                      name="mixed-filter-union", label="Paused OR other")
        result = web.execute_combine(create, self.temp.name)
        self.assertTrue(result["completed"])
        self.assertEqual(result["records"], 5)
        self.assertEqual(result["name"], "mixed-filter-union.bspool")
        self.assertTrue(os.path.isfile(result["report_path"]))
        combined = organizer.BSPoolReader(result["path"])
        self.assertTrue(combined.is_composite)
        self.assertEqual(combined.composite_operation, "union")
        self.assertEqual(len(combined.composite_branches), 2)
        self.assertEqual(len(combined.composite_operands), 2)
        records = {record.rank: record for record in combined.iter_records()}
        self.assertEqual(sorted(records), [0, 1, 2, 3, 4])
        self.assertEqual(len([item for item in records[1].occurrences
                              if item.is_provenance]), 2)

        stale = dict(request, snapshots=dict(plan["snapshots"]),
                     name="must-not-exist")
        stale["snapshots"][self.second_name] = "0" * 16
        with self.assertRaisesRegex(organizer.PoolError, "changed from snapshot"):
            web.execute_combine(stale, self.temp.name)
        self.assertFalse(os.path.exists(os.path.join(
            self.temp.name, "must-not-exist.bspool")))

        report_failure = dict(request, snapshots=plan["snapshots"],
                              name="report-failure")
        original_atomic_json = web.organizer.atomic_json
        try:
            def fail_report(_path, _value):
                raise OSError("simulated report failure")
            web.organizer.atomic_json = fail_report
            with self.assertRaisesRegex(OSError, "simulated report failure"):
                web.execute_combine(report_failure, self.temp.name)
        finally:
            web.organizer.atomic_json = original_atomic_json
        self.assertFalse(os.path.exists(os.path.join(
            self.temp.name, "report-failure.bspool")))

    def test_local_http_inspect_and_snapshot_pinned_export(self):
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), web.make_handler(self.temp.name))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            body = json.dumps({"source": self.source_name}).encode("utf-8")
            request = Request(base + "/api/inspect", data=body,
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                inspected = json.loads(response.read().decode("utf-8"))
            self.assertEqual(inspected["source"]["records"], 4)

            export_url = "%s/api/export?source=%s&snapshot=%s" % (
                base, quote(self.source_name), self.identity["snapshot"])
            with urlopen(export_url, timeout=10) as response:
                lines = response.read().decode("utf-8").strip().splitlines()
                self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(len(lines), 4)
            self.assertEqual(json.loads(lines[1])["seed"], "21111111")

            bad_url = base + "/api/export?source=../outside.bspool&snapshot=x"
            with self.assertRaises(HTTPError) as raised:
                urlopen(bad_url, timeout=10)
            self.assertEqual(raised.exception.code, 400)
            error = json.loads(raised.exception.read().decode("utf-8"))
            self.assertIn("choose a pool", error["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_builder_serves_organizer_as_a_second_tab(self):
        class UnifiedHandler(builder_web.Handler):
            pass
        UnifiedHandler.pool_dir = self.temp.name

        server = ThreadingHTTPServer(("127.0.0.1", 0), UnifiedHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with urlopen(base + "/organize", timeout=10) as response:
                page = response.read().decode("utf-8")
            self.assertIn("Organize / Combine", page)
            self.assertIn("Combine seed pools", page)
            with urlopen(base + "/organizer/api/pools", timeout=10) as response:
                pools = json.loads(response.read().decode("utf-8"))["pools"]
            self.assertIn(self.source_name, [row["name"] for row in pools])

            body = json.dumps({
                "sources": [self.source_name, self.second_name],
                "operation": "intersection",
            }).encode("utf-8")
            request = Request(
                base + "/organizer/api/combine/plan", data=body,
                headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=10) as response:
                plan = json.loads(response.read().decode("utf-8"))
            self.assertEqual(plan["operation"], "intersection")
            self.assertEqual(plan["branch_count"], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
