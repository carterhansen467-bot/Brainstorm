#!/usr/bin/env python3
"""Focused backend/API tests for the standalone organizer web UI."""

import importlib.util
import json
import os
import re
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
        with web.READER_CACHE_LOCK:
            web.READER_CACHE.clear()
        with web.REVIEWED_SPLIT_CACHE_LOCK:
            web.REVIEWED_SPLIT_CACHE.clear()
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
        with web.READER_CACHE_LOCK:
            web.READER_CACHE.clear()
        with web.REVIEWED_SPLIT_CACHE_LOCK:
            web.REVIEWED_SPLIT_CACHE.clear()
        self.temp.cleanup()

    def test_empty_choices_skip_seed_conversion_and_ambiguity_keys_are_cached(self):
        class SeedMustNotBeRead:
            @staticmethod
            def seed(_rank):
                raise AssertionError("an empty choice map converted a rank")

        self.assertEqual(
            organizer.choice_for_record(
                {}, SeedMustNotBeRead(), organizer.Record(7, tuple())),
            (None, None))

        organizer._ambiguity_rule_key_cached.cache_clear()
        first = organizer.ambiguity_rule_key(("tag:two", "tag:one"))
        second = organizer.ambiguity_rule_key(("tag:one", "tag:two"))
        cache = organizer._ambiguity_rule_key_cached.cache_info()
        self.assertEqual(first, second)
        self.assertEqual(cache.misses, 1)
        self.assertEqual(cache.hits, 1)
        self.assertLessEqual(cache.maxsize, 4096)
        self.assertEqual(web.READER_CACHE_LIMIT, web.MAX_COMBINE_INPUTS)

    def test_native_summary_accepts_optional_record_metadata_digest(self):
        document = "\n".join([
            "BRAINSTORM_POOL_SUMMARY 1",
            "records 2",
            "membership_digest 1111111111111111",
            "metadata_digest 2222222222222222",
            "record_metadata_digest 3333333333333333",
            "ambiguous_count 0",
            "unmatched_count 0",
            "opaque_associations 0",
            "records_without_provenance 0",
            "records_without_operands 0",
            "category %s 2" % fixture.TAG.hex(),
            "end",
        ])
        parsed = web._parse_native_summary(document)
        self.assertEqual(
            parsed["record_metadata_digest"], "3333333333333333")
        without = web._parse_native_summary(
            document.replace(
                "record_metadata_digest 3333333333333333\n", ""))
        self.assertNotIn("record_metadata_digest", without)

    def resolved_split_choices(self):
        reader = organizer.BSPoolReader(self.source)
        initial = web.build_split_plan(reader)
        ambiguity = initial["ambiguous"][0]
        destination = next(category for category in ambiguity["candidates"]
                           if category.startswith("legendary:"))
        choices = {
            "source_snapshot_id": self.identity["snapshot"],
            "choices": {ambiguity["seed"]: destination},
        }
        return reader, choices, destination

    @staticmethod
    def preview_records(publication):
        return {row["name"]: row["records"]
                for row in publication["outputs"]}

    @staticmethod
    def read_bytes(path):
        with open(path, "rb") as handle:
            return handle.read()

    @staticmethod
    def post_json(base, path, value):
        request = Request(
            base + path, data=json.dumps(value).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def exercise_http_split_cancel(self, handler, split_path, cancel_path):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        server_thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        entered = threading.Event()
        cancel_seen = threading.Event()
        tick = threading.Event()
        split_response = []
        unexpected = []
        polls = []
        original_execute = web.execute_split

        def blocked_execute(name, request, pool_dir=None, cancel_check=None):
            if not callable(cancel_check):
                raise AssertionError(
                    "HTTP split runner did not supply cancel_check")
            entered.set()
            for _ in range(500):
                polls.append(True)
                if cancel_check():
                    cancel_seen.set()
                    raise organizer.PoolError("operation cancelled")
                tick.wait(0.01)
            raise AssertionError("HTTP cancellation was not delivered")

        def request_split():
            try:
                split_response.append(("ok", self.post_json(base, split_path, {
                    "source": self.source_name,
                    "snapshot": self.identity["snapshot"],
                    "unmatchedPolicy": "keep",
                    "prefix": "http-cancelled-split",
                })))
            except HTTPError as exc:
                split_response.append((
                    "error", json.loads(exc.read().decode("utf-8"))))
            except BaseException as exc:
                unexpected.append(exc)

        web.execute_split = blocked_execute
        split_thread = threading.Thread(target=request_split)
        try:
            split_thread.start()
            self.assertTrue(entered.wait(5),
                            "HTTP split never entered its worker")
            cancelled = self.post_json(base, cancel_path, {
                "operation": "split",
            })
            self.assertEqual(cancelled["state"], "cancelling")
            self.assertTrue(cancel_seen.wait(5),
                            "worker never observed HTTP cancellation")
            split_thread.join(timeout=5)
            self.assertFalse(split_thread.is_alive())
            self.assertEqual(unexpected, [])
            self.assertEqual(len(split_response), 1)
            state, response = split_response[0]
            self.assertEqual(state, "error")
            self.assertIn("cancel", response["error"])
            self.assertNotIn("completed", response)
            self.assertGreater(len(polls), 0)
            self.assertEqual(web.cancel_operation("split")["state"], "idle")
            self.assertFalse(any(
                name.startswith("http-cancelled-split")
                and not name.endswith(".writer.lock")
                for name in os.listdir(self.temp.name)))
        finally:
            web.cancel_operation("split")
            web.execute_split = original_execute
            split_thread.join(timeout=5)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def test_inspect_plan_and_publish_preserve_snapshot_lineage(self):
        report = web.inspect_source(self.source_name, self.temp.name,
                                    ambiguity_limit=10)
        self.assertEqual(report["source"]["snapshot_id"], self.identity["snapshot"])
        self.assertFalse(report["source"]["complete"])
        self.assertFalse(report["source"]["coverage_complete"])
        self.assertIn("Paused or incomplete source",
                      [row["title"] for row in report["notices"]])
        self.assertIn("New pools remain traceable to this source",
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

    def test_verified_reader_cache_reuses_and_invalidates_source_snapshot(self):
        with web.READER_CACHE_LOCK:
            web.READER_CACHE.clear()
        original_reader = organizer.BSPoolReader
        constructions = []

        def counting_reader(*args, **kwargs):
            reader = original_reader(*args, **kwargs)
            constructions.append(reader)
            return reader

        organizer.BSPoolReader = counting_reader
        try:
            inspected = web.run_inspect(self.source_name, self.temp.name)
            request = {
                "source": self.source_name,
                "snapshot": inspected["source"]["snapshot_id"],
                "unmatchedPolicy": "stop",
                "prefix": "cached-reader-plan",
            }
            plan = web.run_split_plan(request, self.temp.name)
            self.assertEqual(plan["source_snapshot_id"],
                             self.identity["snapshot"])
            self.assertEqual(len(constructions), 1)

            replacement_path = os.path.join(
                self.temp.name, "replacement-source.bspool")
            replacement = fixture.write_custom_bsp3(
                replacement_path, [8], [[fixture.TAG]],
                "eeeeeeeeeeeeeeee", [
                    "tag_route collect",
                    "tag tag_negative 1 small 4 big 1",
                ])
            os.replace(replacement_path, self.source)

            with self.assertRaisesRegex(
                    organizer.PoolError, "source changed from snapshot"):
                web.run_split_plan(request, self.temp.name)
            self.assertEqual(len(constructions), 2)
            self.assertEqual(constructions[-1].snapshot_token,
                             replacement["snapshot"])
            self.assertNotEqual(constructions[0].snapshot_token,
                                constructions[-1].snapshot_token)
        finally:
            organizer.BSPoolReader = original_reader
            with web.READER_CACHE_LOCK:
                web.READER_CACHE.clear()

    def test_reader_cache_rejects_an_entry_over_its_memory_budget(self):
        original_budget = web.READER_CACHE_MAX_BYTES
        web.READER_CACHE_MAX_BYTES = 1
        try:
            reader = web.verified_source_reader(
                self.source_name, self.temp.name)
            self.assertEqual(reader.snapshot_token, self.identity["snapshot"])
            with web.READER_CACHE_LOCK:
                self.assertEqual(web.READER_CACHE, {})
        finally:
            web.READER_CACHE_MAX_BYTES = original_budget
            with web.READER_CACHE_LOCK:
                web.READER_CACHE.clear()

    def test_split_plan_scans_the_verified_reader_once(self):
        reader = organizer.BSPoolReader(self.source)
        original_iter_records = reader.iter_records
        scans = []

        def counting_iter_records(*_args, **_kwargs):
            scans.append(True)
            return original_iter_records()

        reader.iter_records = counting_iter_records
        plan = web.build_split_plan(reader, publication={
            "unmatchedPolicy": "stop",
            "prefix": "single-pass-plan",
        })
        self.assertEqual(plan["source_snapshot_id"], self.identity["snapshot"])
        self.assertEqual(len(scans), 1)

    def test_grouped_ambiguities_remain_exact_when_samples_are_bounded(self):
        grouped_name = "grouped-ambiguities.bspool"
        grouped_path = os.path.join(self.temp.name, grouped_name)
        ranks = [10, 11, 12, 13, 14, 15]
        identity = fixture.write_custom_bsp3(
            grouped_path, ranks,
            [[fixture.TAG, fixture.LEGENDARY] for _rank in ranks],
            "eeeeeeeeeeeeeeee", [
                "tag_route collect",
                "tag tag_negative 3 small 3 small 1",
                "legendary j_perkeo 4 big 4 big 1 shop",
            ])
        tag_category = organizer.Occurrence.decode(
            fixture.TAG).category_id
        legendary_category = organizer.Occurrence.decode(
            fixture.LEGENDARY).category_id
        original_limit = web.AMBIGUITY_SAMPLE_LIMIT
        original_write_prepared = organizer.write_prepared_split
        prepared_calls = []
        web.AMBIGUITY_SAMPLE_LIMIT = 2
        try:
            reader = organizer.BSPoolReader(grouped_path)
            publication = {
                "unmatchedPolicy": "stop",
                "prefix": "grouped-publish",
            }
            unresolved = web.build_split_plan(
                reader, publication=publication)
            self.assertEqual(unresolved["ambiguous_count"], len(ranks))
            self.assertEqual(len(unresolved["ambiguous"]), 2)
            self.assertEqual(unresolved["ambiguities_truncated"], 4)
            self.assertEqual(unresolved["unresolved_ambiguities"], len(ranks))
            self.assertEqual(len(unresolved["ambiguity_groups"]), 1)
            group = unresolved["ambiguity_groups"][0]
            self.assertEqual(group["candidates"], sorted(
                (tag_category, legendary_category)))
            self.assertEqual(group["records"], len(ranks))
            self.assertEqual(group["unresolved_records"], len(ranks))
            self.assertEqual(group["samples"], [
                reader.seed(rank) for rank in ranks[:3]])
            self.assertEqual(unresolved["unrepresented_ambiguities"], 0)
            self.assertEqual(unresolved["choices"], {})

            override_seed = reader.seed(ranks[0])
            choice_plan = {
                "source_snapshot_id": identity["snapshot"],
                "choices": {override_seed: legendary_category},
                "ambiguity_rules": {group["rule_key"]: tag_category},
            }
            request = {
                "snapshot": identity["snapshot"],
                "selectedCategories": None,
                "choicePlan": choice_plan,
                **publication,
            }
            resolved = web.build_split_plan(
                reader, choice_plan=choice_plan, publication=request)
            self.assertEqual(resolved["ambiguous_count"], len(ranks))
            self.assertEqual(len(resolved["ambiguous"]), 2)
            self.assertEqual(resolved["unresolved_ambiguities"], 0)
            self.assertEqual(resolved["ambiguity_rules"],
                             {group["rule_key"]: tag_category})
            self.assertEqual(resolved["ambiguity_groups"], [])
            self.assertFalse(resolved["ambiguity_groups_truncated"])
            self.assertEqual(resolved["unrepresented_ambiguities"], 0)
            self.assertEqual(resolved["choices"],
                             {override_seed: legendary_category})
            expected_counts = {
                tag_category: len(ranks) - 1,
                legendary_category: 1,
            }
            self.assertTrue(resolved["publication"]["ready"])
            self.assertEqual({
                row["category_id"]: row["records"]
                for row in resolved["publication"]["outputs"]
            }, expected_counts)
            self.assertTrue(all(
                row["records_exact"]
                for row in resolved["publication"]["outputs"]))

            def count_prepared_write(*args, **kwargs):
                prepared_calls.append(kwargs.get("ambiguity_rules"))
                return original_write_prepared(*args, **kwargs)

            organizer.write_prepared_split = count_prepared_write
            request["reviewedPlanToken"] = \
                resolved["publication"]["plan_token"]
            result = web.run_split(grouped_name, request, self.temp.name)
            self.assertTrue(result["completed"])
            self.assertEqual(prepared_calls,
                             [{group["rule_key"]: tag_category}])
            self.assertEqual({
                row["category_id"]: row["records"]
                for row in result["outputs"]
            }, expected_counts)
            self.assertEqual({
                row["category_id"]: organizer.BSPoolReader(
                    row["path"]).records for row in result["outputs"]
            }, expected_counts)
        finally:
            organizer.write_prepared_split = original_write_prepared
            web.AMBIGUITY_SAMPLE_LIMIT = original_limit
            with web.READER_CACHE_LOCK:
                web.READER_CACHE.clear()

    def test_native_large_inspection_summary_is_exact_cached_and_invalidated(self):
        if not web._native_pool_binary():
            self.skipTest("native pool helper is not built")
        expected = organizer.analyze(
            organizer.BSPoolReader(self.source), ambiguity_limit=0)
        original_minimum = web.NATIVE_SUMMARY_MIN_BYTES
        original_runner = web._run_native_summary
        calls = []

        def counted_summary(*args, **kwargs):
            calls.append(args[0])
            return original_runner(*args, **kwargs)

        web.NATIVE_SUMMARY_MIN_BYTES = 0
        web._run_native_summary = counted_summary
        try:
            first = web.inspect_source(
                self.source_name, self.temp.name, ambiguity_limit=0)
            second = web.inspect_source(
                self.source_name, self.temp.name, ambiguity_limit=0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(first, second)
            for key in (
                    "category_count", "ambiguous_count", "unmatched_count",
                    "opaque_associations", "provenance_counts",
                    "operand_counts"):
                self.assertEqual(first[key], expected[key])
            self.assertEqual([
                (row["category_id"], row["records"])
                for row in first["categories"]
            ], [
                (row["category_id"], row["records"])
                for row in expected["categories"]
            ])
            self.assertTrue(os.path.isfile(
                self.source + ".organizer-summary.json"))

            # An incomplete pool may have an uncommitted writer tail. Changing
            # it must invalidate the file-identity-bound summary cache.
            with open(self.source, "ab") as handle:
                handle.write(b"uncommitted-tail")
            third = web.inspect_source(
                self.source_name, self.temp.name, ambiguity_limit=0)
            self.assertEqual(len(calls), 2)
            self.assertEqual(third["source"]["snapshot_id"],
                             first["source"]["snapshot_id"])
        finally:
            web._run_native_summary = original_runner
            web.NATIVE_SUMMARY_MIN_BYTES = original_minimum

    def test_large_universal_category_review_reuses_cached_exact_groups(self):
        if not web._native_pool_binary():
            self.skipTest("native pool helper is not built")
        name = "universal-category-shape.bspool"
        path = os.path.join(self.temp.name, name)
        ranks = [10, 11, 12, 13, 14, 15]
        identity = fixture.write_custom_bsp3(
            path, ranks, [
                [fixture.TAG, fixture.LEGENDARY],
                [fixture.TAG, fixture.LEGENDARY],
                [fixture.TAG, fixture.LEGENDARY],
                [fixture.TAG, fixture.LEGENDARY],
                [fixture.TAG, fixture.VOUCHER],
                [fixture.TAG, fixture.VOUCHER],
            ], "abababababababab", [
                "tag_route collect",
                "tag tag_negative 3 small 3 small 1",
                "legendary j_perkeo 4 big 4 big 1 shop",
                "voucher v_overstock_norm 2 2",
            ])
        original_minimum = web.NATIVE_SUMMARY_MIN_BYTES
        original_verified = web.verified_source_reader
        web.NATIVE_SUMMARY_MIN_BYTES = 0
        try:
            inspected = web.inspect_source(name, self.temp.name)
            selected = sorted(
                row["category_id"] for row in inspected["categories"])
            request = {
                "source": name,
                "snapshot": identity["snapshot"],
                "selectedCategories": selected,
                "choicePlan": {
                    "source_snapshot_id": identity["snapshot"],
                    "choices": {},
                    "ambiguity_rules": {},
                },
                "unmatchedPolicy": "stop",
                "prefix": "cached-universal-review",
            }
            full = web.build_split_plan(
                organizer.BSPoolReader(path), selected,
                request["choicePlan"], request)

            def unexpected_verified_reader(*_args, **_kwargs):
                raise AssertionError(
                    "cached assignment review performed a Python record scan")

            web.verified_source_reader = unexpected_verified_reader
            cached = web.run_split_plan(request, self.temp.name)
            self.assertEqual(cached["planning_mode"], "summary_projection")
            self.assertEqual(cached["ambiguous_count"], 6)
            self.assertEqual(cached["unmatched_count"], 0)
            self.assertEqual(cached["publication"]["plan_token"],
                             full["publication"]["plan_token"])
            self.assertEqual([
                (row["candidates"], row["unresolved_records"])
                for row in cached["ambiguity_groups"]
            ], [
                (row["candidates"], row["unresolved_records"])
                for row in full["ambiguity_groups"]
            ])

            rules = {
                row["rule_key"]: row["candidates"][0]
                for row in cached["ambiguity_groups"]
            }
            request["choicePlan"] = {
                "source_snapshot_id": identity["snapshot"],
                "choices": {},
                "ambiguity_rules": rules,
            }
            resolved = web.run_split_plan(request, self.temp.name)
            self.assertEqual(resolved["planning_mode"],
                             "summary_projection")
            self.assertEqual(resolved["unresolved_ambiguities"], 0)
            self.assertTrue(resolved["publication"]["ready"])
            resolved_full = web.build_split_plan(
                organizer.BSPoolReader(path), selected,
                request["choicePlan"], request)
            self.assertEqual(resolved["publication"]["plan_token"],
                             resolved_full["publication"]["plan_token"])
        finally:
            web.verified_source_reader = original_verified
            web.NATIVE_SUMMARY_MIN_BYTES = original_minimum

    def test_ambiguity_group_window_reveals_remaining_candidate_sets(self):
        grouped_path = os.path.join(
            self.temp.name, "many-ambiguity-groups.bspool")
        ranks = [20, 21, 22, 23]
        identity = fixture.write_custom_bsp3(
            grouped_path, ranks, [
                [fixture.TAG, fixture.LEGENDARY],
                [fixture.TAG, fixture.VOUCHER],
                [fixture.LEGENDARY, fixture.VOUCHER],
                [fixture.TAG, fixture.LEGENDARY, fixture.VOUCHER],
            ], "edededededededed", [
                "tag_route collect",
                "tag tag_negative 3 small 3 small 1",
                "legendary j_perkeo 4 big 4 big 1 shop",
                "voucher v_overstock_norm 2 2",
            ])
        tag_category = organizer.Occurrence.decode(
            fixture.TAG).category_id
        legendary_category = organizer.Occurrence.decode(
            fixture.LEGENDARY).category_id
        voucher_category = organizer.Occurrence.decode(
            fixture.VOUCHER).category_id
        original_group_limit = web.AMBIGUITY_GROUP_LIMIT
        web.AMBIGUITY_GROUP_LIMIT = 1
        try:
            reader = organizer.BSPoolReader(grouped_path)
            publication = {
                "unmatchedPolicy": "stop",
                "prefix": "group-window",
            }
            first = web.build_split_plan(reader, publication=publication)
            self.assertEqual(first["ambiguous_count"], len(ranks))
            self.assertEqual(first["unresolved_ambiguities"], len(ranks))
            self.assertEqual(len(first["ambiguity_groups"]), 1)
            self.assertTrue(first["ambiguity_groups_truncated"])
            self.assertEqual(first["unrepresented_ambiguities"], 3)
            self.assertEqual(first["choices"], {})
            first_group = first["ambiguity_groups"][0]
            self.assertEqual(first_group["candidates"], sorted(
                (tag_category, legendary_category)))
            self.assertEqual(first_group["unresolved_records"], 1)
            self.assertEqual(first_group["samples"], [reader.seed(ranks[0])])

            first_rules = {
                first_group["rule_key"]: first_group["candidates"][0],
            }
            choice_plan = {
                "source_snapshot_id": identity["snapshot"],
                "choices": {},
                "ambiguity_rules": first_rules,
            }
            second = web.build_split_plan(
                reader, choice_plan=choice_plan, publication=publication)
            self.assertEqual(second["ambiguous_count"], len(ranks))
            self.assertEqual(second["unresolved_ambiguities"], 3)
            self.assertEqual(len(second["ambiguity_groups"]), 1)
            self.assertTrue(second["ambiguity_groups_truncated"])
            self.assertEqual(second["unrepresented_ambiguities"], 2)
            self.assertEqual(second["choices"], {})
            self.assertEqual(second["ambiguity_rules"], first_rules)
            next_group = second["ambiguity_groups"][0]
            self.assertNotEqual(next_group["rule_key"],
                                first_group["rule_key"])
            self.assertEqual(next_group["candidates"], sorted(
                (tag_category, voucher_category)))
            self.assertEqual(next_group["unresolved_records"], 1)
            self.assertEqual(next_group["samples"], [reader.seed(ranks[1])])
        finally:
            web.AMBIGUITY_GROUP_LIMIT = original_group_limit

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

    def test_split_preflight_has_exact_outputs_for_every_unmatched_policy(self):
        reader, choices, _destination = self.resolved_split_choices()
        source_before = self.read_bytes(self.source)
        files_before = set(os.listdir(self.temp.name))
        category_records = {
            organizer.Occurrence.decode(fixture.TAG).category_id: 1,
            organizer.Occurrence.decode(fixture.LEGENDARY).category_id: 1,
            organizer.Occurrence.decode(fixture.VOUCHER).category_id: 1,
        }
        cases = (
            ("stop", None, False, 0),
            ("keep", "Unmatched seeds", True, 0),
            ("remainder", "Needs human review", True, 0),
            ("omit", None, True, 1),
        )
        for policy, unmatched_label, ready, omitted in cases:
            with self.subTest(policy=policy):
                prefix = "preview-" + policy
                request = {
                    "unmatchedPolicy": policy,
                    "prefix": prefix,
                }
                if policy == "remainder":
                    request["remainderName"] = unmatched_label
                plan = web.build_split_plan(
                    reader, choice_plan=choices, publication=request)
                publication = plan["publication"]
                self.assertEqual(publication["ready"], ready)
                self.assertEqual(publication["unmatched_policy"], policy)
                self.assertEqual(publication["omitted_records"], omitted)
                self.assertTrue(publication["source_retained"])
                self.assertTrue(publication["atomic_per_file"])
                self.assertFalse(publication["transaction_atomic"])
                self.assertNotIn("atomic", publication)
                self.assertTrue(publication["writer_locked"])
                self.assertEqual(bool(publication["blockers"]), not ready)

                expected = {
                    prefix + "--" + organizer.safe_filename(category): records
                    for category, records in category_records.items()
                }
                if unmatched_label:
                    remainder_id = "remainder:%s" % quote(
                        unmatched_label, safe="_.-")
                    expected[prefix + "--" +
                             organizer.safe_filename(remainder_id)] = 1
                    labels = [row.get("label")
                              for row in publication["outputs"]]
                    self.assertIn(unmatched_label, labels)
                self.assertEqual(self.preview_records(publication), expected)

        self.assertEqual(self.read_bytes(self.source), source_before)
        self.assertEqual(set(os.listdir(self.temp.name)), files_before)

    def test_split_keep_and_omit_execution_match_the_preflight(self):
        reader, choices, _destination = self.resolved_split_choices()
        source_before = self.read_bytes(self.source)
        for policy, expected_records in (("keep", 4), ("omit", 3)):
            with self.subTest(policy=policy):
                prefix = "execute-" + policy
                publication_request = {
                    "unmatchedPolicy": policy,
                    "prefix": prefix,
                }
                plan = web.build_split_plan(
                    reader, choice_plan=choices,
                    publication=publication_request)
                request = {
                    "snapshot": self.identity["snapshot"],
                    "selectedCategories": None,
                    "choicePlan": choices,
                    "unmatchedPolicy": policy,
                    "prefix": prefix,
                }
                result = web.run_split(
                    self.source_name, request, self.temp.name)
                self.assertTrue(result["completed"])
                self.assertEqual(
                    {row["name"]: row["records"] for row in result["outputs"]},
                    self.preview_records(plan["publication"]))
                self.assertEqual(sum(row["records"] for row in result["outputs"]),
                                 expected_records)
                if policy == "keep":
                    unmatched_name = next(
                        row["name"] for row in plan["publication"]["outputs"]
                        if row.get("label") == "Unmatched seeds")
                    unmatched = organizer.BSPoolReader(os.path.join(
                        self.temp.name, unmatched_name))
                    self.assertEqual(unmatched.header.one("label"),
                                     "Unmatched seeds")
                self.assertTrue(os.path.isfile(self.source))
                self.assertEqual(self.read_bytes(self.source), source_before)

    def test_split_review_token_rejects_changed_publication_before_writing(self):
        reader, choices, _destination = self.resolved_split_choices()
        reviewed_request = {
            "snapshot": self.identity["snapshot"],
            "selectedCategories": None,
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": "reviewed-split",
        }
        plan = web.build_split_plan(
            reader, reviewed_request["selectedCategories"], choices,
            reviewed_request)
        reviewed_request["reviewedPlanToken"] = \
            plan["publication"]["plan_token"]

        ambiguity = plan["ambiguous"][0]
        changed_destination = next(
            candidate for candidate in ambiguity["candidates"]
            if candidate != ambiguity["choice"])
        mutations = (
            ("prefix", {"prefix": "changed-split-prefix"}),
            ("policy", {"unmatchedPolicy": "omit"}),
            ("choices", {"choicePlan": {
                "source_snapshot_id": self.identity["snapshot"],
                "choices": {ambiguity["seed"]: changed_destination},
            }}),
        )
        for field, changes in mutations:
            with self.subTest(field=field):
                changed = dict(reviewed_request)
                changed.update(changes)
                with self.assertRaisesRegex(
                        organizer.PoolError, "changed after review"):
                    web.run_split(
                        self.source_name, changed, self.temp.name)

        self.assertFalse(any(
            name.startswith(("reviewed-split", "changed-split-prefix"))
            for name in os.listdir(self.temp.name)))

        result = web.run_split(
            self.source_name, reviewed_request, self.temp.name)
        self.assertTrue(result["completed"])
        self.assertEqual(
            {row["name"]: row["records"] for row in result["outputs"]},
            self.preview_records(plan["publication"]))

    def test_reviewed_split_reuses_only_exact_file_identity_preflight(self):
        reader, choices, _destination = self.resolved_split_choices()
        request = {
            "snapshot": self.identity["snapshot"],
            "selectedCategories": None,
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": "review-cache-hit",
        }
        plan = web.build_split_plan(
            reader, request["selectedCategories"], choices, request)
        request["reviewedPlanToken"] = \
            plan["publication"]["plan_token"]
        original_builder = web.build_split_plan

        def unexpected_rescan(*_args, **_kwargs):
            raise AssertionError("unchanged reviewed split was rescanned")

        web.build_split_plan = unexpected_rescan
        try:
            result = web.execute_split(
                self.source_name, request, self.temp.name)
        finally:
            web.build_split_plan = original_builder
        self.assertTrue(result["completed"])
        self.assertEqual(
            {row["name"]: row["records"] for row in result["outputs"]},
            self.preview_records(plan["publication"]))

    def test_reviewed_split_file_identity_change_forces_exact_rescan(self):
        reader, choices, _destination = self.resolved_split_choices()
        request = {
            "snapshot": self.identity["snapshot"],
            "selectedCategories": None,
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": "review-cache-restat",
        }
        plan = web.build_split_plan(
            reader, request["selectedCategories"], choices, request)
        request["reviewedPlanToken"] = \
            plan["publication"]["plan_token"]
        before = web._reader_cache_key(self.source)
        status = os.stat(self.source)
        os.utime(self.source, ns=(
            status.st_atime_ns, status.st_mtime_ns + 1000000))
        self.assertNotEqual(web._reader_cache_key(self.source), before)
        original_builder = web.build_split_plan
        rescans = []

        def counted_rescan(*args, **kwargs):
            rescans.append(True)
            return original_builder(*args, **kwargs)

        web.build_split_plan = counted_rescan
        try:
            result = web.execute_split(
                self.source_name, request, self.temp.name)
        finally:
            web.build_split_plan = original_builder
        self.assertTrue(result["completed"])
        self.assertEqual(rescans, [True])

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
        self.assertIn("Different original filters are supported",
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
        with web.READER_CACHE_LOCK:
            self.assertFalse(any(
                key[0] in {os.path.abspath(self.source),
                           os.path.abspath(self.second)}
                for key in web.READER_CACHE))

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

    def test_combine_preflight_has_input_matrix_and_exact_publication(self):
        request = {
            "sources": [self.source_name, self.second_name],
            "operation": "union",
            "name": "preview-union",
            "label": "Preview union",
        }
        plan = web.build_combine_plan(request, self.temp.name)
        self.assertTrue(plan["compatible"])
        compatibility = plan["compatibility"]
        self.assertTrue(compatibility["checks"])
        self.assertEqual(
            {row["name"] for row in compatibility["inputs"]},
            {self.source_name, self.second_name})
        self.assertTrue(all("snapshot_id" in row
                            for row in compatibility["inputs"]))
        publication = plan["publication"]
        self.assertTrue(publication["ready"])
        self.assertEqual(publication["name"],
                         "preview-union.bspool")
        self.assertEqual(publication["report_name"],
                         "preview-union-combine-report.json")
        self.assertTrue(publication["atomic_per_file"])
        self.assertFalse(publication["transaction_atomic"])
        self.assertNotIn("atomic", publication)
        self.assertTrue(publication["writer_locked"])
        self.assertEqual(publication["blockers"], [])
        self.assertFalse(os.path.exists(os.path.join(
            self.temp.name, publication["name"])))

    def test_combine_review_token_rejects_changed_label_before_writing(self):
        reviewed_request = {
            "sources": [self.source_name, self.second_name],
            "operation": "union",
            "name": "reviewed-combine",
            "label": "Reviewed combine",
        }
        plan = web.build_combine_plan(reviewed_request, self.temp.name)
        reviewed_request.update({
            "snapshots": plan["snapshots"],
            "reviewedPublication": plan["publication"],
        })

        changed = dict(reviewed_request, label="Changed after review")
        with self.assertRaisesRegex(
                organizer.PoolError, "changed after review"):
            web.run_combine(changed, self.temp.name)
        self.assertFalse(os.path.exists(os.path.join(
            self.temp.name, "reviewed-combine.bspool")))
        self.assertFalse(os.path.exists(os.path.join(
            self.temp.name, plan["publication"]["report_name"])))

        result = web.run_combine(reviewed_request, self.temp.name)
        self.assertTrue(result["completed"])
        self.assertEqual(result["name"], plan["publication"]["name"])
        self.assertTrue(os.path.isfile(result["path"]))
        self.assertTrue(os.path.isfile(result["report_path"]))

    def test_incompatible_combine_is_a_structured_non_writing_plan(self):
        foreign_name = "foreign-catalog.bspool"
        foreign_path = os.path.join(self.temp.name, foreign_name)
        fixture.write_custom_bsp3(
            foreign_path, [8], [[fixture.TAG]], "dddddddddddddddd", [
                "tag_route collect",
                "tag tag_negative 1 small 4 big 1",
            ], catalog_hash="bbbbbbbbbbbbbbbb")
        request = {
            "sources": [self.source_name, foreign_name],
            "operation": "intersection",
            "name": "must-not-publish",
        }
        plan = web.build_combine_plan(request, self.temp.name)
        self.assertFalse(plan["compatible"])
        self.assertFalse(plan["publication"]["ready"])
        self.assertTrue(plan["publication"]["blockers"])
        self.assertEqual(plan["publication"]["name"],
                         "must-not-publish.bspool")
        checks = plan["compatibility"]["checks"]
        self.assertTrue(any(row["status"] == "fail" and row["blocking"]
                            for row in checks))
        self.assertEqual(
            {row["name"] for row in plan["compatibility"]["inputs"]},
            {self.source_name, foreign_name})
        self.assertFalse(os.path.exists(os.path.join(
            self.temp.name, "must-not-publish.bspool")))

    def test_cancellation_registry_reaches_split_and_combine_runners(self):
        reader, choices, _destination = self.resolved_split_choices()
        split_request = {
            "snapshot": self.identity["snapshot"],
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": "cancelled-split",
        }
        combine_plan = web.build_combine_plan({
            "sources": [self.source_name, self.second_name],
            "operation": "union",
            "name": "cancelled-combine",
        }, self.temp.name)
        combine_request = {
            "sources": [self.source_name, self.second_name],
            "operation": "union",
            "name": "cancelled-combine",
            "snapshots": combine_plan["snapshots"],
        }
        del reader

        cases = (
            ("split", "run_split", "execute_split",
             (self.source_name, split_request, self.temp.name)),
            ("combine", "run_combine", "execute_combine",
             (combine_request, self.temp.name)),
        )
        for kind, runner_name, execute_name, args in cases:
            with self.subTest(kind=kind):
                entered = threading.Event()
                release = threading.Event()
                errors = []
                original_execute = getattr(web, execute_name)

                def blocked_execute(*call_args, **call_kwargs):
                    cancel_check = call_kwargs.get("cancel_check")
                    if cancel_check is None and len(call_args) > len(args):
                        cancel_check = call_args[-1]
                    if not callable(cancel_check):
                        raise AssertionError(
                            "%s did not receive a cancellation callback" %
                            execute_name)
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("cancel test did not release worker")
                    if cancel_check():
                        raise organizer.PoolError("operation cancelled")
                    raise AssertionError("cancel callback was not set")

                def invoke():
                    try:
                        getattr(web, runner_name)(*args)
                    except BaseException as exc:  # surfaced on the test thread
                        errors.append(exc)

                setattr(web, execute_name, blocked_execute)
                try:
                    worker = threading.Thread(target=invoke)
                    worker.start()
                    self.assertTrue(entered.wait(5),
                                    "%s never entered its mutation" % kind)
                    status = web.cancel_operation(kind)
                    self.assertEqual(status["state"], "cancelling")
                    release.set()
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(len(errors), 1)
                    self.assertRegex(str(errors[0]), "cancel")
                    self.assertEqual(web.cancel_operation(kind)["state"], "idle")
                finally:
                    release.set()
                    setattr(web, execute_name, original_execute)
                    worker.join(timeout=5) if 'worker' in locals() else None

        self.assertFalse(any(name.startswith("cancelled-")
                             for name in os.listdir(self.temp.name)))

        # Omitted records have no output writer, but they still represent work
        # that must poll cancellation. Keep this check small and deterministic
        # by temporarily lowering the production polling interval.
        class OmittedOnlyReader:
            def iter_records(self, *_args, **_kwargs):
                for rank in range(3):
                    yield organizer.Record(rank, tuple())

        cancellation_calls = []
        original_interval = organizer.CANCEL_CHECK_RECORDS
        organizer.CANCEL_CHECK_RECORDS = 2
        try:
            def cancel_omitted_iteration():
                cancellation_calls.append(True)
                return len(cancellation_calls) >= 2

            omitted_report = os.path.join(
                self.temp.name, "cancelled-omitted-report.json")
            with self.assertRaisesRegex(organizer.PoolError, "cancel"):
                organizer._write_split_outputs(
                    OmittedOnlyReader(), set(), {}, None, {}, {}, {},
                    omitted_report, cancel_omitted_iteration)
            self.assertEqual(len(cancellation_calls), 2)
            self.assertFalse(os.path.exists(omitted_report))
        finally:
            organizer.CANCEL_CHECK_RECORDS = original_interval

    def test_pool_reader_can_cancel_after_committed_block_verification(self):
        cancellation_calls = []

        def cancel_after_block():
            cancellation_calls.append(True)
            # Entry and the pre-block checkpoint both proceed. The checkpoint
            # after scanning the committed block must abort construction.
            return len(cancellation_calls) >= 3

        with self.assertRaisesRegex(organizer.PoolError, "cancelled"):
            organizer.BSPoolReader(
                self.source, cancel_check=cancel_after_block)
        self.assertEqual(len(cancellation_calls), 3)

    def test_cached_lazy_reader_uses_current_operation_cancellation(self):
        reader = web.verified_source_reader(
            self.source_name, self.temp.name,
            cancel_check=lambda: False)
        self.assertIsNone(reader.cancel_check)
        self.assertFalse(reader._payload_verified)
        cancellation_calls = []

        def cancel_during_payload_verification():
            cancellation_calls.append(True)
            # analyze() checks once at entry; the explicit callback passed to
            # iter_records must then stop the deferred payload pass.
            return len(cancellation_calls) >= 2

        with self.assertRaisesRegex(organizer.PoolError, "cancelled"):
            organizer.analyze(
                reader, cancel_check=cancel_during_payload_verification)
        self.assertEqual(len(cancellation_calls), 2)
        self.assertFalse(reader._payload_verified)

    def test_staged_cleanup_cannot_rollback_a_completed_split(self):
        reader, choices, _destination = self.resolved_split_choices()
        request = {
            "snapshot": self.identity["snapshot"],
            "selectedCategories": None,
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": "cleanup-safe-split",
        }
        plan = web.build_split_plan(
            reader, choice_plan=choices, publication=request)
        request["reviewedPlanToken"] = \
            plan["publication"]["plan_token"]
        original_unlink = web.os.unlink
        forbidden_attempts = []

        def reject_individual_staged_pool_cleanup(path, *args, **kwargs):
            caller = sys._getframe(1).f_globals.get("__name__")
            path_text = os.fspath(path)
            if (caller == web.__name__
                    and ".organizer-stage-" in path_text
                    and path_text.endswith(".bspool")):
                forbidden_attempts.append(path_text)
                raise OSError("individual staged pool cleanup is forbidden")
            return original_unlink(path, *args, **kwargs)

        web.os.unlink = reject_individual_staged_pool_cleanup
        try:
            result = web.run_split(
                self.source_name, request, self.temp.name)
        finally:
            web.os.unlink = original_unlink
            with web.READER_CACHE_LOCK:
                web.READER_CACHE.clear()

        self.assertTrue(result["completed"])
        self.assertEqual(forbidden_attempts, [])
        self.assertTrue(os.path.isfile(result["report_path"]))
        self.assertTrue(all(os.path.isfile(row["path"])
                            for row in result["outputs"]))
        self.assertEqual(
            {row["name"]: row["records"] for row in result["outputs"]},
            self.preview_records(plan["publication"]))
        self.assertFalse(any(name.startswith(".organizer-stage-")
                             for name in os.listdir(self.temp.name)))

    def test_reviewed_report_lock_blocks_split_before_pool_publication(self):
        reader, choices, _destination = self.resolved_split_choices()
        request = {
            "snapshot": self.identity["snapshot"],
            "selectedCategories": None,
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": "report-locked-split",
        }
        plan = web.build_split_plan(
            reader, choice_plan=choices, publication=request)
        request["reviewedPlanToken"] = \
            plan["publication"]["plan_token"]
        report_path = os.path.join(
            self.temp.name, plan["publication"]["report_name"])

        with organizer.pool_writer_guard(report_path):
            with self.assertRaisesRegex(ValueError, "currently being written"):
                web.execute_split(
                    self.source_name, request, self.temp.name)

        self.assertFalse(os.path.exists(report_path))
        self.assertTrue(all(not os.path.exists(os.path.join(
            self.temp.name, row["name"]))
            for row in plan["publication"]["outputs"]))
        self.assertFalse(any(name.startswith(".organizer-stage-")
                             for name in os.listdir(self.temp.name)))

    def test_post_commit_lock_release_failure_preserves_split_success(self):
        reader, choices, _destination = self.resolved_split_choices()
        request = {
            "snapshot": self.identity["snapshot"],
            "selectedCategories": None,
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": "post-commit-cleanup",
        }
        plan = web.build_split_plan(
            reader, choice_plan=choices, publication=request)
        request["reviewedPlanToken"] = \
            plan["publication"]["plan_token"]
        original_exit_stack = web.ExitStack
        close_calls = []

        class CloseFailingExitStack(original_exit_stack):
            def close(self):
                close_calls.append(True)
                super().close()
                raise OSError("simulated lock-release failure")

        web.ExitStack = CloseFailingExitStack
        try:
            result = web.execute_split(
                self.source_name, request, self.temp.name)
        finally:
            web.ExitStack = original_exit_stack
            with web.READER_CACHE_LOCK:
                web.READER_CACHE.clear()

        self.assertTrue(result["completed"])
        self.assertEqual(close_calls, [True])
        self.assertTrue(os.path.isfile(result["report_path"]))
        self.assertTrue(all(os.path.isfile(row["path"])
                            for row in result["outputs"]))
        self.assertEqual(
            {row["name"]: row["records"] for row in result["outputs"]},
            self.preview_records(plan["publication"]))
        with open(result["report_path"], "r", encoding="utf-8") as handle:
            saved_report = json.load(handle)
        self.assertTrue(saved_report["completed"])
        self.assertFalse(any(name.startswith(".organizer-stage-")
                             for name in os.listdir(self.temp.name)))

    def test_split_final_report_failure_rolls_back_every_publication(self):
        _reader, choices, _destination = self.resolved_split_choices()
        prefix = "split-report-failure"
        request = {
            "snapshot": self.identity["snapshot"],
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": prefix,
        }
        source_before = self.read_bytes(self.source)
        original_atomic_json = web.organizer.atomic_json

        def fail_final_report(path, value):
            if (os.path.dirname(os.path.abspath(path)) ==
                    os.path.abspath(self.temp.name)
                    and os.path.basename(path).startswith(prefix)
                    and "split-report" in os.path.basename(path)):
                raise OSError("simulated final split report failure")
            return original_atomic_json(path, value)

        web.organizer.atomic_json = fail_final_report
        try:
            with self.assertRaisesRegex(
                    OSError, "simulated final split report failure"):
                web.run_split(self.source_name, request, self.temp.name)
        finally:
            web.organizer.atomic_json = original_atomic_json
        # Writer-lock files deliberately retain their inode after release; only
        # user-visible pools/reports and ephemeral staging files roll back.
        self.assertFalse(any(name.startswith(prefix)
                             and not name.endswith(".writer.lock")
                             for name in os.listdir(self.temp.name)))
        self.assertFalse(any(name.startswith((".organizer-stage-",
                                              ".organizer-"))
                             for name in os.listdir(self.temp.name)))
        self.assertEqual(self.read_bytes(self.source), source_before)

    def test_post_replace_report_error_rolls_back_report_and_pools(self):
        _reader, choices, _destination = self.resolved_split_choices()
        prefix = "post-replace-report-failure"
        request = {
            "snapshot": self.identity["snapshot"],
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": prefix,
        }
        plan = web.build_split_plan(
            organizer.BSPoolReader(self.source), choice_plan=choices,
            publication=request)
        request["reviewedPlanToken"] = plan["publication"]["plan_token"]
        report_path = os.path.join(
            self.temp.name, plan["publication"]["report_name"])
        original_atomic_json = web.organizer.atomic_json

        def publish_report_then_fail(path, value):
            original_atomic_json(path, value)
            if os.path.abspath(path) == os.path.abspath(report_path):
                raise OSError("simulated post-replace report failure")

        web.organizer.atomic_json = publish_report_then_fail
        try:
            with self.assertRaisesRegex(
                    OSError, "simulated post-replace report failure"):
                web.run_split(self.source_name, request, self.temp.name)
        finally:
            web.organizer.atomic_json = original_atomic_json
            with web.READER_CACHE_LOCK:
                web.READER_CACHE.clear()

        self.assertFalse(os.path.exists(report_path))
        self.assertTrue(all(not os.path.exists(os.path.join(
            self.temp.name, row["name"]))
            for row in plan["publication"]["outputs"]))
        self.assertFalse(any(name.startswith(".organizer-stage-")
                             for name in os.listdir(self.temp.name)))

    def test_post_link_interrupt_rolls_back_unbookkept_pool(self):
        _reader, choices, _destination = self.resolved_split_choices()
        prefix = "post-link-interrupt"
        request = {
            "snapshot": self.identity["snapshot"],
            "choicePlan": choices,
            "unmatchedPolicy": "keep",
            "prefix": prefix,
        }
        plan = web.build_split_plan(
            organizer.BSPoolReader(self.source), choice_plan=choices,
            publication=request)
        request["reviewedPlanToken"] = plan["publication"]["plan_token"]
        report_path = os.path.join(
            self.temp.name, plan["publication"]["report_name"])
        original_link = web.os.link
        interrupted = []

        def link_then_interrupt(source, destination, *args, **kwargs):
            caller = sys._getframe(1).f_globals.get("__name__")
            result = original_link(source, destination, *args, **kwargs)
            if (caller == web.__name__ and not interrupted
                    and ".organizer-stage-" in os.fspath(source)
                    and os.fspath(destination).endswith(".bspool")):
                interrupted.append(os.fspath(destination))
                raise KeyboardInterrupt("simulated post-link interrupt")
            return result

        web.os.link = link_then_interrupt
        try:
            with self.assertRaisesRegex(
                    KeyboardInterrupt, "simulated post-link interrupt"):
                web.run_split(self.source_name, request, self.temp.name)
        finally:
            web.os.link = original_link
            with web.READER_CACHE_LOCK:
                web.READER_CACHE.clear()

        self.assertEqual(len(interrupted), 1)
        self.assertFalse(os.path.exists(report_path))
        self.assertTrue(all(not os.path.exists(os.path.join(
            self.temp.name, row["name"]))
            for row in plan["publication"]["outputs"]))
        self.assertFalse(any(name.startswith(".organizer-stage-")
                             for name in os.listdir(self.temp.name)))

    def test_page_invalidates_stale_plans_and_exposes_preflight_cancellation(self):
        page = web.PAGE
        self.assertIn("Choose the new category pools you want", page)
        self.assertIn("one JSON line for every seed", page)
        self.assertIn("it does not create a seed pool or change the source", page)
        self.assertIn("How assignment works", page)
        self.assertIn("Review the split (no files created)", page)
        self.assertIn("Your selected pools are read-only", page)
        self.assertIn("The three combine rules", page)
        self.assertIn("Review the combined pool (no file created)", page)
        self.assertIn('id="reviewStatus"', page)
        self.assertIn("no full rescan was needed", page)
        self.assertIn('id="inspectionStatus"', page)
        self.assertIn('"Inspection complete"', page)
        self.assertIn("Still working —", page)
        self.assertIn('scrollIntoView({behavior:"smooth"', page)
        self.assertIn('"Seed pool list failed to load"', page)
        self.assertRegex(page, r'\$\("source"\)\.onchange\s*=')
        self.assertIn("publication.ready", page)
        self.assertIn("publication.outputs", page)
        self.assertIn("publication.blockers", page)
        self.assertIn("/api/cancel", page)
        self.assertIn(
            'plan=null;choices={};rules={};splitReviewedFingerprint="";'
            '$("plan").hidden=true;'
            '$("splitPublication").hidden=true;$("saveBtn").disabled=true;'
            '$("splitBtn").disabled=true;', page)
        self.assertRegex(page, r'\$\("splitBtn"\)\.disabled=true;.*await loadPools\(true\)')
        self.assertIn('function renderStatus(){\n if(!plan)return;', page)
        self.assertGreaterEqual(len(re.findall(
            r'id="[^"]*[Cc]ancel[^"]*"', page)), 2)

    def test_standalone_http_cancel_interrupts_active_split(self):
        self.exercise_http_split_cancel(
            web.make_handler(self.temp.name), "/api/split", "/api/cancel")

    def test_embedded_builder_http_cancel_interrupts_active_split(self):
        class UnifiedHandler(builder_web.Handler):
            pass
        UnifiedHandler.pool_dir = self.temp.name
        self.exercise_http_split_cancel(
            UnifiedHandler, "/organizer/api/split", "/organizer/api/cancel")

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
            self.assertIn("Combine pools", page)
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
