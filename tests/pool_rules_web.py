#!/usr/bin/env python3
"""Exercise the same rule endpoints used by both Program entry points."""

import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import brainstorm_pool_organizer as organizer
import pool_organizer_web as web
import pool_builder_web as builder_web

spec = importlib.util.spec_from_file_location("rules_web_fixture", ROOT / "tests/pool_organizer.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class RulesWebRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pool-rules-web-")
        self.root = self.temp.name
        web.allow_active_operations()
        with web.RULE_REVIEW_LOCK:
            web.RULE_REVIEWS.clear()
        tag = lambda key, ante, phase: fixture.descriptor(1, "tag_" + key, ante, phase, 0, 0, 0)
        self.events = [
            [tag("negative", 3, 1), tag("rare", 5, 1)],
            [tag("rare", 3, 1), tag("negative", 3, 2), tag("negative", 6, 2)],
            [tag("rare", 4, 1), tag("negative", 4, 2)],
        ]
        criteria = ["tag_route observe", "tag tag_negative 3 small 7 big 1",
                    "tag tag_rare 3 small 7 big 1"]
        fixture.write_custom_bsp3(os.path.join(self.root, "L1.bspool"),
                                 [1, 2, 3], self.events,
                                 "1111111111111111", criteria)
        fixture.write_custom_bsp3(os.path.join(self.root, "L2.bspool"),
                                 [2], [self.events[1]],
                                 "2222222222222222", criteria)
        organizer.combine_pools([
            organizer.BSPoolReader(os.path.join(self.root, name))
            for name in ("L1.bspool", "L2.bspool")],
            os.path.join(self.root, "Complete.bspool"))

    def tearDown(self):
        with web.RULE_REVIEW_LOCK:
            web.RULE_REVIEWS.clear()
        self.temp.cleanup()

    def request(self, url, data):
        req = Request(url, data=json.dumps(data).encode(),
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)

    def test_http_recover_deleted_inputs_in_both_entry_points(self):
        os.unlink(os.path.join(self.root, "L1.bspool"))
        os.unlink(os.path.join(self.root, "L2.bspool"))
        for unified in (False, True):
            with self.subTest(unified=unified):
                if unified:
                    class Handler(builder_web.Handler):
                        pool_dir = self.root
                else:
                    Handler = web.make_handler(self.root)
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%s%s/api/rules/" % (
                    server.server_port, "/organizer" if unified else "")
                try:
                    status, description = self.request(base + "describe", {"source": "Complete.bspool"})
                    self.assertEqual(status, 200, description)
                    self.assertEqual(sorted(r["records"] for r in description["direct_inputs"]), [1, 3])
                    status, validated = self.request(base + "validate", {"document": json.dumps({
                        "version": 1, "mode": "second_tag", "rule": {
                            "version": 1, "range": {"start": "a3s", "end": "a7b"}}})})
                    self.assertEqual(status, 200, validated)
                    self.assertEqual(validated["recipe"]["rule"]["range"]["start"], "A3S")
                    status, plan = self.request(base + "preview", {
                        "source": "Complete.bspool", "prefix": "restored-" + str(unified),
                        "recipe": {"version": 1, "mode": "separate_sources", "source_kind": "inputs"}})
                    self.assertEqual(status, 200, plan)
                    self.assertEqual((plan["copied_records"], plan["output_memberships"]), (3, 4))
                    status, result = self.request(base + "publish", {
                        "source": "Complete.bspool", "planToken": plan["plan_token"]})
                    self.assertEqual(status, 200, result)
                    self.assertEqual(sorted(r["records"] for r in result["outputs"]), [1, 3])
                    status, again = self.request(base + "publish", {
                        "source": "Complete.bspool", "planToken": plan["plan_token"]})
                    self.assertEqual(status, 400)
                    self.assertIn("preview", again["error"].lower())
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)

    def test_tag_preview_stays_bound_to_source_and_private_plan(self):
        request = {"source": "L1.bspool", "prefix": "second", "recipe": {
            "version": 1, "mode": "second_tag", "rule": {
                "version": 1, "range": {"start": "A3S", "end": "A7B"}}}}
        plan = web.run_rule_preview(request, self.root)
        self.assertEqual(plan["copied_records"], 2)
        self.assertEqual(plan["exclusions"], {"same_ante_only": 1})
        self.assertEqual({r["label"] for r in plan["outputs"]}, {"A5 Small Rare", "A6 Big Negative"})
        plan["outputs"][0]["records"] = 999
        result = web.run_rule_publish({"source": "L1.bspool", "planToken": plan["plan_token"]}, self.root)
        self.assertEqual([r["records"] for r in result["outputs"]], [1, 1])
        collision = web.run_rule_preview(request, self.root)
        self.assertFalse(collision["can_create"])
        self.assertEqual(len(collision["collisions"]), 3)
        self.assertIn(os.path.basename(result["report_path"]), collision["collisions"])
        for output in plan["outputs"]:
            os.unlink(os.path.join(self.root, output["name"]))
        report_collision = web.run_rule_preview(request, self.root)
        self.assertFalse(report_collision["can_create"])
        self.assertEqual(report_collision["collisions"], [report_collision["report_name"]])

    def test_changed_snapshot_and_foreign_preview_refused(self):
        with self.assertRaisesRegex(organizer.PoolError, "changed"):
            web.run_rule_describe({"source": "Complete.bspool", "snapshot": "bad"}, self.root)
        plan = web.run_rule_preview({"source": "Complete.bspool", "recipe": {
            "version": 1, "mode": "separate_sources"}}, self.root)
        with self.assertRaisesRegex(organizer.PoolError, "another pool"):
            web.run_rule_publish({"source": "L1.bspool", "planToken": plan["plan_token"]}, self.root)

    def test_unrecorded_range_is_not_treated_as_no_tags(self):
        with self.assertRaisesRegex(ValueError, "record|cover"):
            web.run_rule_preview({"source": "L1.bspool", "recipe": {
                "version": 1, "mode": "second_tag", "rule": {
                    "version": 1, "range": {"start": "A2S", "end": "A7B"}}}}, self.root)
        self.assertEqual(web.operation_progress("rules")["state"], "failed")
        self.assertEqual(web.cancel_operation("rules")["state"], "idle")

    def test_ui_fragment_is_packaged_once_with_accessible_controls(self):
        self.assertEqual(web.PAGE.count('id="rulesWorkspace"'), 1)
        for identifier in ("ruleStart", "ruleEnd", "ruleSource", "ruleName", "rulePrefix"):
            self.assertIn('for="%s"' % identifier, web.PAGE)
        self.assertNotIn("/*__RULE_", web.PAGE)
        self.assertIn("Optional tag conditions", web.PAGE)
        self.assertIn("Earlier recorded source groups", web.PAGE)

    def test_saved_rule_validation_does_not_discard_unknown_settings(self):
        recipe = {"version": 1, "mode": "second_tag", "rule": {
            "version": 1, "range": {"start": "A3S", "end": "A7B"}}}
        for invalid in (
                dict(recipe, version=2), dict(recipe, unrecognized=True),
                {"version": 1, "mode": "second_tag", "rule": {
                    "version": 1, "range": {"start": "A3S", "end": "A7B", "inclusive": False}}}):
            with self.subTest(recipe=invalid), self.assertRaises(ValueError):
                web.run_rule_validate({"document": json.dumps(invalid)})
        with self.assertRaisesRegex(ValueError, "repeats the field"):
            web.run_rule_validate({"document": '{"version":2,"version":1,"mode":"second_tag","rule":{}}'})
        recipe["rule"]["condition"] = {"not": {"not": {"count": {
            "tag": "rare", "range": {"start": "A3S", "end": "A7B"}, "min": 1}}}}
        self.assertEqual(web.run_rule_validate({"document": json.dumps(recipe)})["recipe"], recipe)


if __name__ == "__main__":
    unittest.main()
