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

        result = web.execute_split(self.source_name, {
            "snapshot": self.identity["snapshot"],
            "selectedCategories": None,
            "choicePlan": choices,
            "unmatchedPolicy": "remainder",
            "remainderName": "Needs review",
            "prefix": "paused-test",
        }, self.temp.name)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
