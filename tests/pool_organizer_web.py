#!/usr/bin/env python3
"""Focused backend/API tests for the standalone organizer web UI."""

import contextlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import unittest
from unittest import mock
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
from pool_record_export_web import RecordExportWebRegression

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

    def test_builder_merge_reserves_the_complete_output_artifact_family(self):
        first_name = "builder-merge-a.bspool"
        second_name = "builder-merge-b.bspool"
        fixture.write_bsp3(
            os.path.join(self.temp.name, first_name), complete=True)
        fixture.write_bsp3(
            os.path.join(self.temp.name, second_name), complete=True)
        request = {
            "pools": [first_name, second_name],
            "name": "protected-merge",
        }
        output = os.path.join(
            self.temp.name, "protected-merge.bspool")
        sentinel = b"pre-existing sidecar remains unchanged\n"

        old_jobs = builder_web.JOBS
        self.addCleanup(setattr, builder_web, "JOBS", old_jobs)
        builder_web.JOBS = builder_web.BuilderJobLifecycle()
        for suffix in web.FORMAT_UPGRADE_PROTECTED_SUFFIXES:
            collision = output + suffix
            with self.subTest(suffix=suffix):
                with open(collision, "wb") as handle:
                    handle.write(sentinel)
                with mock.patch.object(
                        builder_web.core, "MergeRunner") as runner:
                    with self.assertRaisesRegex(
                            ValueError, "already has pool files"):
                        builder_web.start_merge_job(
                            request, self.temp.name)
                    runner.assert_not_called()
                with open(collision, "rb") as handle:
                    self.assertEqual(handle.read(), sentinel)
                os.unlink(collision)

    def test_native_summary_accepts_optional_record_metadata_digest(self):
        document = "\n".join([
            "BRAINSTORM_POOL_SUMMARY 2",
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

    def exercise_http_format_upgrade(
            self, handler, plan_path, update_path, source_name):
        source = os.path.join(self.temp.name, source_name)
        fixture.write_bsp3(source, complete=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        server_thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            plan = self.post_json(
                base, plan_path, {"source": source_name})
            self.assertTrue(plan["eligible"])
            result = self.post_json(base, update_path, {
                "source": source_name,
                "planToken": plan["plan_token"],
            })
            output = os.path.join(self.temp.name, result["output"])
            reader = organizer.BSPoolReader(output)
            self.assertEqual(reader.schema, 4)
            self.assertEqual(reader.records, 4)
            self.assertTrue(os.path.isfile(source))
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

    def exercise_http_format_cancel(
            self, handler, plan_path, update_path, cancel_path, source_name):
        source = os.path.join(self.temp.name, source_name)
        fixture.write_bsp3(source, complete=True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(
            target=server.serve_forever, daemon=True)
        server_thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        entered = threading.Event()
        cancel_seen = threading.Event()
        tick = threading.Event()
        responses = []
        unexpected = []
        original_execute = web.execute_format_upgrade

        def blocked_execute(request, pool_dir=None, cancel_check=None):
            if not callable(cancel_check):
                raise AssertionError(
                    "HTTP format runner did not supply cancel_check")
            entered.set()
            for _ in range(500):
                if cancel_check():
                    cancel_seen.set()
                    raise web.OperationCancelled("operation cancelled")
                tick.wait(0.01)
            raise AssertionError("HTTP format cancellation was not delivered")

        try:
            plan = self.post_json(
                base, plan_path, {"source": source_name})
            output = os.path.join(self.temp.name, plan["output_name"])

            def request_update():
                try:
                    responses.append(("ok", self.post_json(
                        base, update_path, {
                            "source": source_name,
                            "planToken": plan["plan_token"],
                        })))
                except HTTPError as exc:
                    responses.append((
                        "error", json.loads(exc.read().decode("utf-8"))))
                except BaseException as exc:
                    unexpected.append(exc)

            web.execute_format_upgrade = blocked_execute
            update_thread = threading.Thread(target=request_update)
            update_thread.start()
            self.assertTrue(entered.wait(5),
                            "HTTP format update never entered its worker")
            cancelled = self.post_json(base, cancel_path, {
                "operation": "upgrade",
            })
            self.assertEqual(cancelled["state"], "cancelling")
            self.assertTrue(cancel_seen.wait(5),
                            "format worker never observed HTTP cancellation")
            update_thread.join(timeout=5)
            self.assertFalse(update_thread.is_alive())
            self.assertEqual(unexpected, [])
            self.assertEqual(len(responses), 1)
            state, response = responses[0]
            self.assertEqual(state, "error")
            self.assertEqual(
                response["error_code"], "operation_cancelled")
            self.assertIn("cancel", response["error"])
            self.assertFalse(os.path.lexists(output))
            self.assertFalse(os.path.lexists(output + ".manifest"))
            self.assertEqual(
                web.cancel_operation("upgrade")["state"], "idle")
        finally:
            web.cancel_operation("upgrade")
            web.execute_format_upgrade = original_execute
            if "update_thread" in locals():
                update_thread.join(timeout=5)
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)

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
                    raise web.OperationCancelled("operation cancelled")
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
            self.assertEqual(
                response["error_code"], "operation_cancelled")
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

    def test_selected_filter_split_collapses_variants_and_preserves_metadata(self):
        perkeo_a1_shop = fixture.descriptor(
            2, "j_perkeo", 1, 2, 1, 0, 0)
        perkeo_a1_charm = fixture.descriptor(
            2, "j_perkeo", 1, 2, 2, 1, 1)
        perkeo_a2 = fixture.descriptor(
            2, "j_perkeo", 2, 1, 1, 0, 0)
        negative_a3 = fixture.descriptor(
            1, "tag_negative", 3, 1, 0, 0, 0)
        negative_a4 = fixture.descriptor(
            1, "tag_negative", 4, 2, 0, 0, 0)
        voucher = fixture.descriptor(
            3, "v_overstock_norm", 2, 0, 1, 1, 4)
        opaque = bytes((9, 2, 0xAA, 0xBB))
        name = "filter-location-roundtrip.bspool"
        path = os.path.join(self.temp.name, name)
        identity = fixture.write_custom_bsp3(
            path, [20, 21, 22, 23], [
                [perkeo_a1_shop, perkeo_a1_charm, negative_a3, voucher],
                [perkeo_a1_shop, negative_a4],
                [perkeo_a2, negative_a3, opaque],
                [perkeo_a2, negative_a4, voucher],
            ],
            "13579bdf2468ace0", [
                "tag_route collect",
                "tag tag_negative 3 small 4 big 1",
                "legendary j_perkeo 1 big 2 small 1 shop",
                "voucher v_overstock_norm 2 2",
            ])
        reader = organizer.BSPoolReader(path)
        inspection = organizer.analyze(reader)
        filters = {
            row["filter_id"]: row for row in inspection["filters"]
        }
        perkeo_filter = "legendary:j_perkeo"
        tag_filter = "tag:tag_negative"
        perkeo_locations = [
            row["location_id"] for row in filters[perkeo_filter]["locations"]
        ]
        tag_locations = [
            row["location_id"] for row in filters[tag_filter]["locations"]
        ]
        self.assertEqual(perkeo_locations, [
            "legendary:j_perkeo:A1:big",
            "legendary:j_perkeo:A2:small",
        ])
        self.assertEqual(
            filters[perkeo_filter]["multiple_location_records"], 0)

        choice_plan = {
            "source_snapshot_id": identity["snapshot"],
            "group_by_filter": perkeo_filter,
            "choices": {},
            "ambiguity_rules": {},
        }
        request = {
            "snapshot": identity["snapshot"],
            "groupByFilter": perkeo_filter,
            "selectedCategories": perkeo_locations,
            "choicePlan": choice_plan,
            "unmatchedPolicy": "stop",
            "prefix": "organized-by-perkeo",
        }
        plan = web.build_split_plan(
            reader, selected_ids=perkeo_locations,
            choice_plan=choice_plan, publication=request,
            inspection=inspection)
        self.assertEqual(plan["group_by_filter"], perkeo_filter)
        self.assertEqual(plan["selected_categories"], perkeo_locations)
        self.assertEqual(plan["planning_mode"], "summary_projection")
        self.assertEqual(plan["unresolved_ambiguities"], 0)
        self.assertTrue(plan["publication"]["ready"])
        self.assertTrue(plan["publication"]["plan_token"])

        # The chosen grouping filter is part of saved decisions and cannot be
        # silently reused for a different view of the same snapshot.
        mismatched_decisions = dict(choice_plan, group_by_filter=tag_filter)
        with self.assertRaisesRegex(
                organizer.PoolError, "different organizing filter"):
            web.build_split_plan(
                reader, selected_ids=perkeo_locations,
                choice_plan=mismatched_decisions, publication=request)

        tag_choice_plan = dict(choice_plan, group_by_filter=tag_filter)
        tag_request = dict(
            request, groupByFilter=tag_filter,
            selectedCategories=tag_locations, choicePlan=tag_choice_plan,
            prefix="organized-by-tag")
        tag_plan = web.build_split_plan(
            reader, selected_ids=tag_locations,
            choice_plan=tag_choice_plan, publication=tag_request,
            inspection=inspection)
        self.assertEqual(tag_plan["group_by_filter"], tag_filter)
        self.assertNotEqual(
            tag_plan["publication"]["plan_token"],
            plan["publication"]["plan_token"])

        original_metadata = {
            record.rank: {item.raw for item in record.occurrences}
            for record in organizer.BSPoolReader(path).iter_records()
        }
        request["reviewedPlanToken"] = plan["publication"]["plan_token"]
        result = web.execute_split(name, request, self.temp.name)
        self.assertTrue(result["completed"])
        self.assertEqual(result["group_by_filter"], perkeo_filter)
        self.assertEqual(len(result["outputs"]), 2)
        self.assertEqual(
            {row["category_id"] for row in result["outputs"]},
            set(perkeo_locations))
        roundtripped = {}
        for output in result["outputs"]:
            derived = organizer.BSPoolReader(output["path"])
            for record in derived.iter_records():
                self.assertNotIn(record.rank, roundtripped)
                roundtripped[record.rank] = {
                    item.raw for item in record.occurrences
                }
        self.assertEqual(roundtripped, original_metadata)

    def test_selected_filter_reports_only_true_multi_location_ambiguity(self):
        perkeo_a1_shop = fixture.descriptor(
            2, "j_perkeo", 1, 2, 1, 0, 0)
        perkeo_a1_charm = fixture.descriptor(
            2, "j_perkeo", 1, 2, 2, 1, 1)
        perkeo_a2 = fixture.descriptor(
            2, "j_perkeo", 2, 1, 1, 0, 0)
        path = os.path.join(self.temp.name, "multi-location.bspool")
        identity = fixture.write_custom_bsp3(
            path, [30, 31], [
                [perkeo_a1_shop, perkeo_a1_charm, fixture.TAG],
                [perkeo_a1_shop, perkeo_a2, fixture.TAG],
            ],
            "02468ace13579bdf", [
                "tag_route collect",
                "tag tag_negative 3 small 3 small 1",
                "legendary j_perkeo 1 big 2 small 1 shop",
            ])
        reader = organizer.BSPoolReader(path)
        inspection = organizer.analyze(reader)
        perkeo_filter = next(
            row for row in inspection["filters"]
            if row["filter_id"] == "legendary:j_perkeo")
        locations = [
            row["location_id"] for row in perkeo_filter["locations"]
        ]
        self.assertEqual(perkeo_filter["multiple_location_records"], 1)
        self.assertEqual(perkeo_filter["location_associations"], 3)

        choice_plan = {
            "source_snapshot_id": identity["snapshot"],
            "group_by_filter": perkeo_filter["filter_id"],
            "choices": {},
            "ambiguity_rules": {},
        }
        plan = web.build_split_plan(
            reader, selected_ids=locations, choice_plan=choice_plan,
            publication={
                "groupByFilter": perkeo_filter["filter_id"],
                "unmatchedPolicy": "omit",
                "prefix": "true-multi-location",
            })
        self.assertEqual(plan["ambiguous_count"], 1)
        self.assertEqual(plan["unresolved_ambiguities"], 1)
        self.assertEqual(len(plan["ambiguous"]), 1)
        self.assertEqual(plan["ambiguous"][0]["rank"], 31)
        self.assertEqual(plan["ambiguous"][0]["candidates"], locations)
        self.assertEqual(len(plan["ambiguity_groups"]), 1)
        self.assertIn(
            "multiple locations", plan["publication"]["blockers"][0])

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

    def test_reader_cache_charges_packed_index_storage_once(self):
        blocks = organizer.PackedBlockSequence()
        block = organizer.Block(
            8192, 0, 1, 0, 1, 1, 0, 0, 48, 0, 0, 0)
        # Match the block count of the production 355-million-record BSP3.
        for _index in range(346717):
            blocks.append(block)

        class Header:
            text = ""

        class Reader:
            header = Header()

        reader = Reader()
        reader.blocks = blocks
        weight = web._reader_cache_weight(reader)
        self.assertEqual(blocks.packed_bytes, 346717 * 56)
        self.assertLess(weight, web.READER_CACHE_MAX_BYTES)
        self.assertLess(
            weight - sys.getsizeof(blocks),
            4096,
            "packed block views must not be charged once per index entry")

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
                    "operand_counts", "filters",
                    "recommended_filter_id"):
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

    def test_large_inspection_uses_verified_python_for_repaired_bsp3(self):
        name = "large-path-repaired-header.bspool"
        path = os.path.join(self.temp.name, name)
        fixture.write_bsp3(path, complete=True)
        clean = organizer.BSPoolReader(path)
        expected = organizer.analyze(clean)
        with open(path, "r+b") as handle:
            handle.seek(clean.blocks[0].offset)
            handle.write(b"DAMAGED!")

        original_minimum = web.NATIVE_SUMMARY_MIN_BYTES
        original_binary = web._native_pool_binary
        original_runner = web._run_native_summary
        web.NATIVE_SUMMARY_MIN_BYTES = 0
        web._native_pool_binary = lambda: "available-native-helper"

        def unexpected_native_summary(*_args, **_kwargs):
            raise AssertionError(
                "repaired BSP3 was reopened by byte-strict native summary")

        web._run_native_summary = unexpected_native_summary
        try:
            actual = web.inspect_source(name, self.temp.name)
        finally:
            web._run_native_summary = original_runner
            web._native_pool_binary = original_binary
            web.NATIVE_SUMMARY_MIN_BYTES = original_minimum
        self.assertEqual(actual["source"]["records"],
                         expected["source"]["records"])
        self.assertEqual(actual["categories"], expected["categories"])
        self.assertEqual(actual["filters"], expected["filters"])
        self.assertEqual(
            actual["source"]["reconstructed_bsp3_header_prefixes"], 1)
        self.assertIn(
            "The original BSP3 has a damaged block header",
            [notice["title"] for notice in actual["notices"]])

    def test_repaired_large_inspection_rechecks_source_identity(self):
        name = "changing-repaired-header.bspool"
        path = os.path.join(self.temp.name, name)
        fixture.write_bsp3(path, complete=True)
        clean = organizer.BSPoolReader(path)
        with open(path, "r+b") as handle:
            handle.seek(clean.blocks[0].offset)
            handle.write(b"DAMAGED!")

        original_minimum = web.NATIVE_SUMMARY_MIN_BYTES
        original_binary = web._native_pool_binary
        original_identity = web._summary_identity
        calls = []

        def changing_identity(source):
            value = original_identity(source)
            calls.append(source)
            if len(calls) >= 3:
                value = dict(value)
                value["mtime_ns"] += 1
            return value

        web.NATIVE_SUMMARY_MIN_BYTES = 0
        web._native_pool_binary = lambda: "available-native-helper"
        web._summary_identity = changing_identity
        try:
            with self.assertRaisesRegex(
                    organizer.PoolError,
                    "source changed while its verified reconstructed view"):
                web.inspect_source(name, self.temp.name)
        finally:
            web._summary_identity = original_identity
            web._native_pool_binary = original_binary
            web.NATIVE_SUMMARY_MIN_BYTES = original_minimum
        self.assertGreaterEqual(len(calls), 3)

    def test_native_summary_rejects_missing_friendly_group_rows(self):
        if not web._native_pool_binary():
            self.skipTest("native pool helper is not built")
        summary = web._run_native_summary(self.source)
        self.assertTrue(summary["filters"])
        self.assertTrue(summary["locations"])
        reader = organizer.BSPoolReader(
            self.source, verify_payloads=False)
        missing_filter = dict(summary, filters=summary["filters"][:-1])
        with self.assertRaisesRegex(
                organizer.PoolError, "filter/location summary is incomplete"):
            web._report_from_native_summary(reader, missing_filter)

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
            self.assertEqual(cached["overlap_records"],
                             full["overlap_records"])
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
            self.assertEqual(resolved["overlap_records"],
                             resolved_full["overlap_records"])
            web.verified_source_reader = original_verified
            execute_request = dict(
                request,
                snapshot=identity["snapshot"],
                reviewedPlanToken=resolved["publication"]["plan_token"])
            result = web.execute_split(
                name, execute_request, self.temp.name)
            self.assertTrue(result["completed"])
            self.assertEqual(result["overlap_records"],
                             resolved["overlap_records"])
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

    def test_matching_copy_preview_publish_and_cached_projection_agree(self):
        reader = organizer.BSPoolReader(self.source)
        inspection = organizer.analyze(reader, ambiguity_limit=100)
        selected = [row["category_id"] for row in inspection["categories"]]
        decisions = {
            "source_snapshot_id": reader.snapshot_token,
            "assignment_mode": "matching_copies",
            "choices": {},
            "ambiguity_rules": {},
        }
        request = {
            "assignmentMode": "matching_copies",
            "selectedCategories": selected,
            "choicePlan": decisions,
            "unmatchedPolicy": "omit",
            "prefix": "matching-copy-web",
        }
        source_before = self.read_bytes(self.source)
        full = web.build_split_plan(
            reader, selected, decisions, request)
        direct = organizer.split_policy.PoolSplitPolicy(
            organizer.split_policy.SplitSpec.create(
                "matching_copies", selected)).review(
                    organizer.BSPoolReader(self.source).iter_records(),
                    reader.seed)
        self.assertEqual(
            direct.destinations(),
            {row["category_id"]: row["records"]
             for row in full["publication"]["outputs"]
             if row["kind"] == "category"})
        projected_selected = [selected[0]]
        projected_request = dict(
            request,
            selectedCategories=projected_selected,
            prefix="matching-copy-projection")
        projected_full = web.build_split_plan(
            organizer.BSPoolReader(self.source), projected_selected,
            decisions, projected_request)
        projected = web.build_split_plan(
            web._InspectionPlanReader(self.source, inspection["source"]),
            projected_selected, decisions, projected_request,
            inspection=inspection)

        self.assertEqual(full["assignment_mode"], "matching_copies")
        self.assertEqual(full["overlap_records"], 1)
        self.assertEqual(full["ambiguous_count"], 0)
        self.assertEqual(full["unresolved_ambiguities"], 0)
        self.assertEqual(full["unique_copied_records"], 3)
        self.assertEqual(full["output_memberships"], 5)
        self.assertEqual(
            self.preview_records(projected["publication"]),
            self.preview_records(projected_full["publication"]))
        self.assertEqual(projected["planning_mode"], "summary_projection")

        execute_request = dict(request, snapshot=reader.snapshot_token)
        execute_request["reviewedPlanToken"] = \
            full["publication"]["plan_token"]
        result = web.execute_split(
            self.source_name, execute_request, self.temp.name)
        self.assertTrue(result["completed"])
        self.assertEqual(result["assignment_mode"], "matching_copies")
        self.assertEqual(result["overlap_records"], 1)
        self.assertEqual(result["output_memberships"], 5)
        self.assertEqual(sum(row["records"] for row in result["outputs"]), 5)
        self.assertEqual(self.read_bytes(self.source), source_before)

        remainder_request = dict(
            request, prefix="matching-copy-with-other",
            unmatchedPolicy="remainder", remainderName="Other seeds")
        remainder = web.build_split_plan(
            organizer.BSPoolReader(self.source), selected, decisions,
            remainder_request)
        self.assertEqual(remainder["unique_copied_records"], 4)
        self.assertEqual(remainder["output_memberships"], 6)
        self.assertEqual(remainder["publication"]["output_count"], 4)

    def test_matching_copy_review_token_includes_assignment_mode(self):
        reader = organizer.BSPoolReader(self.source)
        selected = [
            row["category_id"]
            for row in organizer.analyze(reader)["categories"]
        ]
        decisions = {
            "source_snapshot_id": reader.snapshot_token,
            "assignment_mode": "matching_copies",
            "choices": {},
            "ambiguity_rules": {},
        }
        request = {
            "snapshot": reader.snapshot_token,
            "assignmentMode": "matching_copies",
            "selectedCategories": selected,
            "choicePlan": decisions,
            "unmatchedPolicy": "omit",
            "prefix": "mode-token",
        }
        plan = web.build_split_plan(
            reader, selected, decisions, request)
        changed = dict(request)
        changed["assignmentMode"] = "exclusive"
        changed["choicePlan"] = dict(decisions, assignment_mode="exclusive")
        changed["reviewedPlanToken"] = plan["publication"]["plan_token"]
        with self.assertRaisesRegex(
                organizer.PoolError, "changed after review"):
            web.execute_split(self.source_name, changed, self.temp.name)
        self.assertFalse(any(name.startswith("mode-token")
                             for name in os.listdir(self.temp.name)))

    def test_split_preflight_blocks_too_many_output_files(self):
        reader, choices, _destination = self.resolved_split_choices()
        original_limit = organizer.MAX_SPLIT_OUTPUTS
        organizer.MAX_SPLIT_OUTPUTS = 1
        try:
            plan = web.build_split_plan(
                reader, choice_plan=choices, publication={
                    "unmatchedPolicy": "keep",
                    "prefix": "too-many-preview-files",
                })
        finally:
            organizer.MAX_SPLIT_OUTPUTS = original_limit

        publication = plan["publication"]
        self.assertGreater(publication["output_count"], 1)
        self.assertFalse(publication["ready"])
        self.assertIn(
            "maximum 1 outputs per publication",
            " ".join(publication["blockers"]))
        self.assertFalse(any(
            name.startswith("too-many-preview-files--")
            for name in os.listdir(self.temp.name)))

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
            omitted_spec = organizer.split_policy.SplitSpec.create(
                "exclusive", ["tag:test"])
            omitted_plan = \
                organizer.split_policy.ReviewedSplitPlan.for_publication(
                    omitted_spec, {}, 3, 0, 0, 0)
            with self.assertRaisesRegex(organizer.PoolError, "cancel"):
                organizer._write_split_outputs(
                    OmittedOnlyReader(), omitted_plan, {}, {}, {},
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
        original_link = web.seed_pool_mutations.link_many_no_overwrite
        interrupted = []

        def link_then_interrupt(publications, *args, **kwargs):
            pairs = tuple(publications)
            source, destination = pairs[0]
            should_interrupt = (
                not interrupted and ".organizer-stage-" in os.fspath(source)
                and os.fspath(destination).endswith(".bspool"))
            result = original_link(
                (pairs[0],) if should_interrupt else pairs,
                *args, **kwargs)
            if should_interrupt:
                interrupted.append(os.fspath(destination))
                raise KeyboardInterrupt("simulated post-link interrupt")
            return result

        web.seed_pool_mutations.link_many_no_overwrite = link_then_interrupt
        try:
            with self.assertRaisesRegex(
                    KeyboardInterrupt, "simulated post-link interrupt"):
                web.run_split(self.source_name, request, self.temp.name)
        finally:
            web.seed_pool_mutations.link_many_no_overwrite = original_link
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
        self.assertIn("Recorded data", page)
        self.assertIn("Technical pool details", page)
        self.assertIn("Export all records (.ndjson)", page)
        self.assertLess(page.index('id="exportBtn"'),
                        page.index('id="categoryCard"'))
        self.assertIn(
            "What kind of result should organize the new pools?", page)
        self.assertIn('{id:"legendary",label:"Legendary"', page)
        self.assertIn('{id:"tag",label:"Tag"', page)
        self.assertIn('{id:"voucher",label:"Voucher"', page)
        self.assertIn("Choose locations to create pools for", page)
        self.assertIn("Advanced: split by exact recorded event metadata", page)
        self.assertNotIn('id="filterKinds" role="radiogroup"', page)
        self.assertRegex(
            page,
            r'<fieldset class="choicegroup"><legend>What kind of result '
            r'should organize the new pools\?</legend>[\s\S]*?'
            r'id="exactKind"[\s\S]*?</fieldset>',
        )
        self.assertNotIn("What these checkboxes control", page)
        self.assertNotIn("Beginning of each new filename", page)
        self.assertIn('<label for="prefix">New file name</label>', page)
        self.assertIn("Create pools from one pool", page)
        self.assertIn("Also create an Other seeds pool", page)
        self.assertIn('id="policy" value="omit"', page)
        self.assertIn("Seeds without ${name} at a checked location", page)
        self.assertNotIn("Seeds outside the checked locations", page)
        self.assertNotIn("Do not decide yet", page)
        self.assertIn("Preview new pools", page)
        self.assertIn("Review files to create", page)
        self.assertIn("Advanced: require each seed to go to only one pool", page)
        self.assertIn('assignmentMode:assignmentMode()', page)
        self.assertIn('"matching_copies"', page)
        self.assertIn('id="applyDecisionsBtn" hidden>Update preview', page)
        self.assertIn('id="updateStatus" role="status"', page)
        self.assertIn(
            '$("applyDecisionsBtn").onclick=()=>prepare(true)', page)
        self.assertIn(
            'activeButton.textContent=fromReview?'
            '"Updating preview…":"Building preview…"', page)
        self.assertIn(
            "Advanced: exclusive split decision files", page)
        self.assertIn("Combine seed lists", page)
        self.assertIn("Any selected pool", page)
        self.assertIn("Every selected pool", page)
        self.assertIn("First pool, minus the others", page)
        self.assertIn("Check compatibility and preview file", page)
        self.assertNotIn("The three combine rules", page)
        self.assertIn("Update pool format", page)
        self.assertIn("Check pool format", page)
        self.assertIn("Create BSP4 copy", page)
        self.assertIn("Original BSP3 will be kept", page)
        self.assertIn("/api/format/plan", page)
        self.assertIn("/api/format/update", page)
        self.assertIn('error.code=v.error_code||""', page)
        self.assertIn(
            'error.publicationState=v.publication_state||""', page)
        self.assertIn(
            'cancelled=e.code==="operation_cancelled"', page)
        self.assertIn(
            'failedSafely=e.publicationState==="not_published"', page)
        self.assertIn('"Source pool is damaged"', page)
        self.assertIn(
            "No BSP4 copy was published, and the original BSP3 was not changed.",
            page)
        self.assertIn(
            "safely reconstructed from the committed index, checksums, "
            "and whole-pool identities",
            page)
        self.assertNotIn("/cancel/i", page)
        self.assertIn(
            'id="formatElapsed" aria-hidden="true" hidden', page)
        self.assertIn(
            '$("formatElapsed").textContent=`Still working —', page)
        self.assertIn(
            'clearInterval(timer);$("formatElapsed").hidden=true;'
            '$("formatElapsed").textContent=""', page)
        self.assertIn(
            '$("formatStatus").hidden=true;'
            '$("formatStatus").className="workstatus"', page)
        self.assertIn(
            "could not confirm whether a BSP4 copy was published", page)
        self.assertIn(
            'check.disabled=refreshFailed||!workflowState.pools.some('
            'p=>!p.error)',
            page)
        self.assertIn(
            '$("formatSource").disabled=workflowState.format.running;'
            'formatButton.disabled=workflowState.format.running||'
            '!workflowState.pools.some(p=>!p.error)',
            page)
        self.assertIn(
            '$("formatSource").disabled=true;formatButton.disabled=true',
            page)
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
        self.assertIn("class OrganizerWorkflowState", page)
        self.assertIn("const workflowState=new OrganizerWorkflowState()", page)
        self.assertNotIn("Object.defineProperties(globalThis", page)
        self.assertNotIn("splitRunning=true", page)
        self.assertNotIn("combineRunning=true", page)
        self.assertNotIn("formatRunning=true", page)
        self.assertNotIn("poolRows=v.pools||[]", page)
        self.assertNotIn("choices={};rules={}", page)
        self.assertNotRegex(
            page,
            r"workflowState\.(?:split|combine|format|export)\.\w+\s*=",
        )
        self.assertIn("workflowState.completeSplit();", page)
        self.assertIn("workflowState.reviewCombine(v,fingerprint)", page)
        self.assertIn("workflowState.resetFormat();", page)
        self.assertNotIn(
            'let inspected=null,inspectedName="",plan=null', page)
        self.assertIn(
            '$("sumAmbiguous").textContent="Preview to calculate";'
            '$("sumUnmatched").textContent="Preview to calculate"', page)
        self.assertIn(
            '${fmt(publication.overlap_records||0)} overlapping seed(s); '
            '${fmt(publication.unique_copied_records||0)} unique copied '
            'seed(s); ${fmt(publication.output_memberships||0)} total '
            'output memberships.', page)
        self.assertNotIn(
            'const overlap=publication.overlap_records?', page)
        self.assertRegex(page, r'\$\("splitBtn"\)\.disabled=true;.*await loadPools\(true\)')
        self.assertIn(
            'function renderStatus(){\n const split=workflowState.split,'
            'plan=split.plan;if(!plan)return;', page)
        self.assertGreaterEqual(len(re.findall(
            r'id="[^"]*[Cc]ancel[^"]*"', page)), 2)

    def test_format_upgrade_plan_matrix_and_stale_collision(self):
        complete_name = "Complete Pool BSP3.BSPOOL"
        complete = os.path.join(self.temp.name, complete_name)
        fixture.write_bsp3(complete, complete=True)
        plan = web.plan_format_upgrade(complete_name, self.temp.name)
        self.assertTrue(plan["eligible"])
        self.assertEqual(plan["source_format"], "BSP3")
        self.assertEqual(plan["output_name"], "Complete Pool BSP4.bspool")

        # Automatic naming never overwrites either a pool or a stale sidecar.
        collision = os.path.join(self.temp.name, plan["output_name"])
        with open(collision, "wb") as handle:
            handle.write(b"existing destination")
        moved = web.plan_format_upgrade(complete_name, self.temp.name)
        self.assertEqual(moved["output_name"], "Complete Pool BSP4-2.bspool")
        with self.assertRaisesRegex(
                organizer.PoolError, "automatic output changed"):
            web.execute_format_upgrade({
                "source": complete_name,
                "planToken": plan["plan_token"],
            }, self.temp.name)
        with open(collision, "rb") as handle:
            self.assertEqual(handle.read(), b"existing destination")

        paused = web.plan_format_upgrade(self.source_name, self.temp.name)
        self.assertFalse(paused["eligible"])
        self.assertIn("Finish or resume", " ".join(paused["blockers"]))

        provisional_name = "provisional.bspool"
        fixture.write_custom_bsp3(
            os.path.join(self.temp.name, provisional_name),
            [1, 2], [[fixture.TAG], [fixture.TAG]],
            "abababababababab", ["tag_route collect"],
            complete=True, coverage_complete=False)
        provisional = web.plan_format_upgrade(
            provisional_name, self.temp.name)
        self.assertFalse(provisional["eligible"])
        self.assertIn("provisional", " ".join(
            provisional["blockers"]).lower())

        bsp2_name = "legacy-bsp2.bspool"
        fixture.write_bsp2(
            os.path.join(self.temp.name, bsp2_name), complete=True)
        bsp2 = web.plan_format_upgrade(bsp2_name, self.temp.name)
        self.assertFalse(bsp2["eligible"])
        self.assertIn("cannot be losslessly updated", " ".join(
            bsp2["blockers"]))

        original_binary = web._native_pool_binary
        web._native_pool_binary = lambda: ""
        try:
            missing = web.plan_format_upgrade(complete_name, self.temp.name)
        finally:
            web._native_pool_binary = original_binary
        self.assertFalse(missing["eligible"])
        self.assertIn("native seed-pool helper is missing",
                      " ".join(missing["blockers"]))

    def test_format_upgrade_automatic_name_reserves_every_pool_sidecar(self):
        protected = (
            "", ".manifest", ".state", ".criteria.cfg", ".attached",
            ".organizer-summary.json",
        )
        for number, suffix in enumerate(protected):
            with self.subTest(suffix=suffix or "(pool)"):
                source_name = "Sidecar Collision %d BSP3.bspool" % number
                fixture.write_bsp3(
                    os.path.join(self.temp.name, source_name), complete=True)
                expected = "Sidecar Collision %d BSP4.bspool" % number
                with open(os.path.join(self.temp.name, expected) + suffix,
                          "wb") as handle:
                    handle.write(b"reserved pool artifact")
                plan = web.plan_format_upgrade(source_name, self.temp.name)
                self.assertEqual(
                    plan["output_name"],
                    "Sidecar Collision %d BSP4-2.bspool" % number)

    def test_format_upgrade_long_name_keeps_marker_and_links_are_blocked(self):
        long_name = ("%s BSP3.bspool" % ("a" * 170))
        fixture.write_bsp3(
            os.path.join(self.temp.name, long_name), complete=True)
        long_plan = web.plan_format_upgrade(long_name, self.temp.name)
        self.assertTrue(long_plan["eligible"])
        self.assertTrue(
            os.path.splitext(long_plan["output_name"])[0].endswith("BSP4"))
        self.assertLessEqual(
            len(os.path.splitext(long_plan["output_name"])[0]), 160)

        source_name = "linked-original BSP3.bspool"
        alias_name = "linked-alias BSP3.bspool"
        source = os.path.join(self.temp.name, source_name)
        alias = os.path.join(self.temp.name, alias_name)
        fixture.write_bsp3(source, complete=True)
        os.link(source, alias)
        linked = web.plan_format_upgrade(alias_name, self.temp.name)
        self.assertFalse(linked["eligible"])
        self.assertIn(
            "filesystem link", " ".join(linked["blockers"]).lower())

    def test_format_upgrade_accepts_completed_zero_record_bsp3(self):
        name = "No Matches BSP3.bspool"
        source = os.path.join(self.temp.name, name)
        identity = fixture.write_empty_bsp3(source)
        source_bytes = self.read_bytes(source)
        source_reader = organizer.BSPoolReader(source)
        self.assertEqual(source_reader.records, 0)
        self.assertEqual(list(source_reader.iter_records()), [])

        plan = web.plan_format_upgrade(name, self.temp.name)
        self.assertTrue(plan["eligible"])
        self.assertEqual(plan["records"], 0)
        result = web.run_format_upgrade({
            "source": name,
            "planToken": plan["plan_token"],
        }, self.temp.name)
        output = os.path.join(self.temp.name, result["output"])
        upgraded = organizer.BSPoolReader(output)
        self.assertEqual(upgraded.schema, 4)
        self.assertEqual(upgraded.encoding, "adaptive-events-v1")
        self.assertTrue(upgraded.complete)
        self.assertTrue(upgraded.coverage_complete)
        self.assertEqual(upgraded.records, 0)
        self.assertEqual(list(upgraded.iter_records()), [])
        self.assertEqual("%016x" % upgraded.family_id, identity["family"])
        self.assertEqual("%016x" % upgraded.lineage_id, identity["lineage"])
        self.assertEqual(
            "%016x" % upgraded.header.integer("stage_hash", 16),
            identity["stage"])
        self.assertEqual(self.read_bytes(source), source_bytes)
        self.assertFalse(any(
            item.startswith(".u-") for item in os.listdir(self.temp.name)))

    def test_format_upgrade_accepts_extended_header_bsp3(self):
        base = os.path.join(self.temp.name, "extended-base.bspool")
        fixture.write_bsp3(base, complete=True)
        for header_bytes in (16 * 1024, organizer.HEADER_MAX_BYTES):
            with self.subTest(header_bytes=header_bytes):
                name = "Extended Header %d BSP3.bspool" % header_bytes
                source = os.path.join(self.temp.name, name)
                fixture.expand_bsp3_header(
                    base, source, header_bytes=header_bytes)
                source_records = [
                    (record.rank,
                     tuple(item.raw for item in record.occurrences))
                    for record in organizer.BSPoolReader(
                        source).iter_records()
                ]

                plan = web.plan_format_upgrade(name, self.temp.name)
                self.assertTrue(plan["eligible"])
                result = web.run_format_upgrade({
                    "source": name,
                    "planToken": plan["plan_token"],
                }, self.temp.name)
                upgraded = organizer.BSPoolReader(
                    os.path.join(self.temp.name, result["output"]))
                self.assertEqual(upgraded.schema, 4)
                self.assertEqual(upgraded.header_bytes, header_bytes)
                self.assertEqual([
                    (record.rank,
                     tuple(item.raw for item in record.occurrences))
                    for record in upgraded.iter_records()
                ], source_records)

    def test_format_upgrade_preserves_source_and_historical_metadata(self):
        name = "Historical Pool BSP3.BSPOOL"
        source = os.path.join(self.temp.name, name)
        fixture.write_out_of_order_bsp3(
            source, overlapping=True, long_opaque=True)
        with open(source, "rb") as handle:
            source_bytes = handle.read()
        source_reader = organizer.BSPoolReader(source)
        source_records = {
            record.rank: tuple(sorted(
                occurrence.raw for occurrence in record.occurrences))
            for record in source_reader.iter_records()
        }
        source_criteria = organizer._reader_criteria(source_reader)
        source_stage = source_reader.header.integer("stage_hash", 16)

        plan = web.plan_format_upgrade(name, self.temp.name)
        result = web.run_format_upgrade({
            "source": name,
            "planToken": plan["plan_token"],
        }, self.temp.name)
        output = os.path.join(self.temp.name, result["output"])
        upgraded = organizer.BSPoolReader(output)
        output_records = {
            record.rank: tuple(sorted(
                occurrence.raw for occurrence in record.occurrences))
            for record in upgraded.iter_records()
        }
        with open(source, "rb") as handle:
            self.assertEqual(handle.read(), source_bytes)
        self.assertEqual(output_records, source_records)
        self.assertEqual(upgraded.schema, 4)
        self.assertEqual(upgraded.encoding, "adaptive-events-v1")
        self.assertTrue(upgraded.complete)
        self.assertTrue(upgraded.coverage_complete)
        self.assertEqual(upgraded.records, len(source_records))
        self.assertEqual(upgraded.family_id, source_reader.family_id)
        self.assertEqual(upgraded.lineage_id, source_reader.lineage_id)
        self.assertEqual(
            upgraded.header.integer("stage_hash", 16), source_stage)
        self.assertEqual(
            organizer._reader_criteria(upgraded), source_criteria)
        self.assertTrue(result["normalized_historical_order"])
        self.assertTrue(os.path.isfile(output + ".manifest"))
        self.assertFalse(any(
            item.startswith(".u-")
            for item in os.listdir(self.temp.name)))

        current = web.plan_format_upgrade(
            result["output"], self.temp.name)
        self.assertEqual(current["status"], "current")
        self.assertFalse(current["eligible"])
        self.assertEqual(current["output_name"], "")

    def test_format_upgrade_recovers_only_a_proven_bsp3_header_prefix(self):
        name = "Recoverable Header BSP3.bspool"
        source = os.path.join(self.temp.name, name)
        fixture.write_bsp3(source, complete=True)
        clean = organizer.BSPoolReader(source)
        expected = [
            (record.rank, tuple(item.raw for item in record.occurrences))
            for record in clean.iter_records()
        ]
        block_offset = clean.blocks[0].offset
        with open(source, "r+b") as handle:
            handle.seek(block_offset)
            handle.write(b"DAMAGED!")
        source_bytes = self.read_bytes(source)

        plan = web.plan_format_upgrade(name, self.temp.name)
        result = web.run_format_upgrade({
            "source": name,
            "planToken": plan["plan_token"],
        }, self.temp.name)
        output = os.path.join(self.temp.name, result["output"])
        actual = [
            (record.rank, tuple(item.raw for item in record.occurrences))
            for record in organizer.BSPoolReader(output).iter_records()
        ]
        self.assertEqual(actual, expected)
        self.assertEqual(
            result["reconstructed_bsp3_header_prefixes"], 1)
        self.assertEqual(self.read_bytes(source), source_bytes)
        with open(output + ".manifest", encoding="utf-8") as handle:
            manifest = handle.read()
        self.assertIn(
            "upgrade_reconstructed_bsp3_header_prefixes 1", manifest)
        self.assertIn(
            "upgrade_first_reconstructed_block 0", manifest)
        self.assertIn(
            "upgrade_first_reconstructed_byte %d" % block_offset,
            manifest)
        self.assertIn(
            "upgrade_first_damaged_prefix 44414d4147454421",
            manifest)

    def test_format_upgrade_rejects_record_digest_mismatch(self):
        name = "digest-mismatch BSP3.bspool"
        source = os.path.join(self.temp.name, name)
        fixture.write_bsp3(source, complete=True)
        source_bytes = self.read_bytes(source)
        plan = web.plan_format_upgrade(name, self.temp.name)
        output = os.path.join(self.temp.name, plan["output_name"])
        original_upgrade = web._run_native_upgrade

        def mismatched_digest(source_path, staged_path, cancel_check=None):
            result = original_upgrade(
                source_path, staged_path, cancel_check=cancel_check)
            manifest_path = staged_path + ".manifest"
            with open(manifest_path, encoding="utf-8") as handle:
                manifest = handle.read()
            manifest, changed = re.subn(
                r"(?m)^record_metadata_digest [0-9a-f]{16}$",
                "record_metadata_digest 0000000000000000",
                manifest, count=1)
            self.assertEqual(changed, 1)
            with open(manifest_path, "w", encoding="utf-8",
                      newline="\n") as handle:
                handle.write(manifest)
            return result

        web._run_native_upgrade = mismatched_digest
        try:
            with self.assertRaisesRegex(
                    web.FormatUpdateFailedSafely,
                    "expected complete BSP4") as caught:
                web.execute_format_upgrade({
                    "source": name,
                    "planToken": plan["plan_token"],
                }, self.temp.name)
        finally:
            web._run_native_upgrade = original_upgrade
        payload = web.error_payload(caught.exception)
        self.assertEqual(payload["publication_state"], "not_published")
        self.assertEqual(payload["error_code"], "format_update_failed")
        self.assertFalse(os.path.lexists(output))
        self.assertFalse(os.path.lexists(output + ".manifest"))
        self.assertEqual(self.read_bytes(source), source_bytes)
        self.assertFalse(any(
            item.startswith(".u-") for item in os.listdir(self.temp.name)))

    def test_format_upgrade_rejects_valid_but_different_records(self):
        name = "semantic-source BSP3.bspool"
        source = os.path.join(self.temp.name, name)
        fixture.write_bsp3(source, complete=True)
        source_bytes = self.read_bytes(source)
        alternate = os.path.join(
            self.temp.name, "semantic-alternate BSP3.bspool")
        fixture.write_custom_bsp3(
            alternate, [11, 12, 13, 14],
            [[fixture.TAG], [fixture.TAG], [fixture.TAG], [fixture.TAG]],
            "bbbbbbbbbbbbbbbb",
            ["tag_route collect",
             "tag tag_negative 3 small 3 small 1"],
            complete=True)
        plan = web.plan_format_upgrade(name, self.temp.name)
        output = os.path.join(self.temp.name, plan["output_name"])
        original_upgrade = web._run_native_upgrade

        def different_upgrade(_source, staged_path, cancel_check=None):
            return original_upgrade(
                alternate, staged_path, cancel_check=cancel_check)

        web._run_native_upgrade = different_upgrade
        try:
            with self.assertRaisesRegex(
                    organizer.PoolError, "changed a seed rank"):
                web.execute_format_upgrade({
                    "source": name,
                    "planToken": plan["plan_token"],
                }, self.temp.name)
        finally:
            web._run_native_upgrade = original_upgrade
        self.assertFalse(os.path.lexists(output))
        self.assertFalse(os.path.lexists(output + ".manifest"))
        self.assertEqual(self.read_bytes(source), source_bytes)
        self.assertFalse(any(
            item.startswith(".u-") for item in os.listdir(self.temp.name)))

    def test_format_upgrade_record_comparison_rejects_metadata_only_change(
            self):
        source = os.path.join(self.temp.name, "metadata-source.bspool")
        alternate = os.path.join(
            self.temp.name, "metadata-alternate.bspool")
        staged = os.path.join(self.temp.name, "metadata-staged.bspool")
        fixture.write_bsp3(source, complete=True)
        fixture.write_custom_bsp3(
            alternate, [0, 1, 2, 3],
            [[fixture.TAG], [fixture.TAG], [fixture.VOUCHER],
             [fixture.UNKNOWN]],
            "bbbbbbbbbbbbbbbb",
            ["tag_route collect",
             "tag tag_negative 3 small 3 small 1"],
            complete=True)
        web._run_native_upgrade(alternate, staged)

        source_reader = organizer.BSPoolReader(source)
        staged_reader = organizer.BSPoolReader(staged)
        with self.assertRaisesRegex(
                organizer.PoolError, "rank or its recorded filter metadata"):
            web._verify_upgrade_record_equivalence(
                source_reader, staged_reader)

    def test_format_upgrade_record_comparison_closes_streams_on_mismatch(
            self):
        closed = []

        class TrackedRecords:
            def __init__(self, label, records):
                self.label = label
                self.records = iter(records)

            def __iter__(self):
                return self

            def __next__(self):
                return next(self.records)

            def close(self):
                closed.append(self.label)

        class TrackedReader:
            def __init__(self, label, records):
                self.label = label
                self.items = records
                self.records = len(records)

            def iter_records(self, cancel_check=None):
                return TrackedRecords(self.label, self.items)

        occurrence = organizer.Occurrence.decode(fixture.TAG)
        source = TrackedReader(
            "source", [organizer.Record(1, (occurrence,))])
        staged = TrackedReader(
            "staged", [organizer.Record(2, (occurrence,))])
        with self.assertRaisesRegex(
                organizer.PoolError, "changed a seed rank"):
            web._verify_upgrade_record_equivalence(source, staged)
        self.assertCountEqual(closed, ["source", "staged"])

    def test_format_upgrade_cancellation_never_publishes_partial_output(self):
        name = "cancel-source.bspool"
        source = os.path.join(self.temp.name, name)
        fixture.write_bsp3(source, complete=True)
        with open(source, "rb") as handle:
            source_bytes = handle.read()
        plan = web.plan_format_upgrade(name, self.temp.name)
        entered = threading.Event()
        finished = []
        original_upgrade = web._run_native_upgrade

        def blocked_upgrade(_source, output, cancel_check=None):
            with open(output, "wb") as handle:
                handle.write(b"private partial output")
            entered.set()
            while not cancel_check():
                threading.Event().wait(0.01)
            raise web.OperationCancelled(
                "format update cancelled safely; no new pool was published")

        def run():
            try:
                web.run_format_upgrade({
                    "source": name,
                    "planToken": plan["plan_token"],
                }, self.temp.name)
            except organizer.PoolError as exc:
                finished.append(str(exc))

        web._run_native_upgrade = blocked_upgrade
        thread = threading.Thread(target=run)
        try:
            thread.start()
            self.assertTrue(entered.wait(5))
            self.assertEqual(
                web.cancel_operation("upgrade")["state"], "cancelling")
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        finally:
            web._run_native_upgrade = original_upgrade
            web.cancel_operation("upgrade")
            thread.join(timeout=5)
        self.assertEqual(len(finished), 1)
        self.assertIn("cancelled", finished[0])
        self.assertFalse(os.path.exists(os.path.join(
            self.temp.name, plan["output_name"])))
        self.assertFalse(any(
            item.startswith(".u-")
            for item in os.listdir(self.temp.name)))
        with open(source, "rb") as handle:
            self.assertEqual(handle.read(), source_bytes)
        self.assertEqual(
            web.cancel_operation("upgrade")["state"], "idle")

    def test_format_upgrade_final_prelink_cancellation_publishes_nothing(self):
        name = "prelink-cancel-source.bspool"
        source = os.path.join(self.temp.name, name)
        fixture.write_bsp3(source, complete=True)
        source_bytes = self.read_bytes(source)
        plan = web.plan_format_upgrade(name, self.temp.name)
        output = os.path.join(self.temp.name, plan["output_name"])
        cancelled = threading.Event()
        original_guard = organizer.pool_writer_guard

        @contextlib.contextmanager
        def cancel_when_output_lock_is_held(path):
            with original_guard(path):
                if os.path.abspath(path) == os.path.abspath(output):
                    cancelled.set()
                yield

        organizer.pool_writer_guard = cancel_when_output_lock_is_held
        try:
            with self.assertRaises(web.OperationCancelled):
                web.execute_format_upgrade({
                    "source": name,
                    "planToken": plan["plan_token"],
                }, self.temp.name, cancel_check=cancelled.is_set)
        finally:
            organizer.pool_writer_guard = original_guard
        self.assertTrue(cancelled.is_set())
        self.assertFalse(os.path.lexists(output))
        self.assertFalse(os.path.lexists(output + ".manifest"))
        self.assertEqual(self.read_bytes(source), source_bytes)
        self.assertFalse(any(
            item.startswith(".u-") for item in os.listdir(self.temp.name)))

    def test_format_upgrade_postlink_interruptions_return_committed_pool(self):
        original_publish = web._publish_upgrade_file
        for point in ("manifest", "pool"):
            with self.subTest(point=point):
                name = "postlink-%s BSP3.bspool" % point
                source = os.path.join(self.temp.name, name)
                fixture.write_bsp3(source, complete=True)
                source_bytes = self.read_bytes(source)
                plan = web.plan_format_upgrade(name, self.temp.name)
                output = os.path.join(self.temp.name, plan["output_name"])
                interrupted = []

                def interrupt_after_successful_link(source_path, final_path):
                    original_publish(source_path, final_path)
                    is_target = (
                        (point == "manifest"
                         and final_path == output + ".manifest")
                        or (point == "pool" and final_path == output))
                    if is_target:
                        interrupted.append(final_path)
                        raise KeyboardInterrupt(
                            "synthetic interruption after os.link committed")

                web._publish_upgrade_file = interrupt_after_successful_link
                try:
                    result = web.execute_format_upgrade({
                        "source": name,
                        "planToken": plan["plan_token"],
                    }, self.temp.name)
                finally:
                    web._publish_upgrade_file = original_publish
                self.assertEqual(interrupted, [
                    output + (".manifest" if point == "manifest" else "")])
                self.assertEqual(result["output"], plan["output_name"])
                upgraded = organizer.BSPoolReader(output)
                self.assertEqual(upgraded.schema, 4)
                self.assertTrue(upgraded.complete)
                self.assertEqual(upgraded.records, 4)
                self.assertTrue(os.path.isfile(output + ".manifest"))
                self.assertEqual(self.read_bytes(source), source_bytes)
                self.assertFalse(any(
                    item.startswith(".u-")
                    for item in os.listdir(self.temp.name)))

    def test_format_upgrade_postcommit_lock_release_failure_is_success(self):
        original_guard = organizer.pool_writer_guard
        for point in ("output", "source"):
            with self.subTest(point=point):
                name = "unlock-%s BSP3.bspool" % point
                source = os.path.join(self.temp.name, name)
                fixture.write_bsp3(source, complete=True)
                source_bytes = self.read_bytes(source)
                plan = web.plan_format_upgrade(name, self.temp.name)
                output = os.path.join(
                    self.temp.name, plan["output_name"])

                @contextlib.contextmanager
                def failing_release(path):
                    with original_guard(path):
                        yield
                    target = output if point == "output" else source
                    if os.path.abspath(path) == os.path.abspath(target):
                        raise OSError(
                            "synthetic %s lock release failure" % point)

                organizer.pool_writer_guard = failing_release
                try:
                    result = web.execute_format_upgrade({
                        "source": name,
                        "planToken": plan["plan_token"],
                    }, self.temp.name)
                finally:
                    organizer.pool_writer_guard = original_guard
                self.assertEqual(result["output"], plan["output_name"])
                self.assertIn(
                    "synthetic %s lock release failure" % point,
                    result["publication_warning"])
                upgraded = organizer.BSPoolReader(output)
                self.assertEqual(upgraded.schema, 4)
                self.assertTrue(upgraded.complete)
                self.assertTrue(os.path.isfile(output + ".manifest"))
                self.assertEqual(self.read_bytes(source), source_bytes)
                self.assertFalse(any(
                    item.startswith(".u-")
                    for item in os.listdir(self.temp.name)))

    def test_operation_cancellation_error_code_is_type_based(self):
        cancelled = web.error_payload(
            web.OperationCancelled("the user stopped this operation"))
        stale = web.error_payload(
            web.FormatPlanStale("the checked destination changed"))
        ordinary = web.error_payload(
            organizer.PoolError("a filter named cancellation-example failed"))
        failed = web.error_payload(
            web.FormatUpdateFailedSafely("native helper rejected the source"))
        damaged = web.error_payload(
            web.FormatSourceDamaged("BSP3 block differs"))
        self.assertEqual(cancelled["error_code"], "operation_cancelled")
        self.assertEqual(stale["error_code"], "format_plan_stale")
        self.assertEqual(failed["error_code"], "format_update_failed")
        self.assertEqual(failed["publication_state"], "not_published")
        self.assertEqual(damaged["error_code"], "format_source_damaged")
        self.assertEqual(damaged["publication_state"], "not_published")
        self.assertNotIn("error_code", ordinary)

    def test_operation_shutdown_barrier_signals_drains_and_rejects_new_work(self):
        web.allow_active_operations()
        event = web._begin_operation("analysis")
        drained = threading.Event()
        waiter_started = threading.Event()

        def wait_for_cleanup():
            waiter_started.set()
            web.wait_for_active_operations(poll_seconds=0.001)
            drained.set()

        waiter = threading.Thread(target=wait_for_cleanup)
        try:
            kinds = web.begin_operation_shutdown()
            self.assertEqual(kinds, ("analysis",))
            self.assertTrue(event.is_set())
            with self.assertRaisesRegex(
                    organizer.PoolError, "Organizer is closing"):
                web._begin_operation("split")

            waiter.start()
            self.assertTrue(waiter_started.wait(2))
            self.assertFalse(drained.wait(0.05))
            web._finish_operation("analysis", event)
            self.assertTrue(drained.wait(2))
            waiter.join(timeout=2)
            self.assertFalse(waiter.is_alive())
        finally:
            web._finish_operation("analysis", event)
            if waiter.ident is not None:
                waiter.join(timeout=2)
            web.allow_active_operations()

    def test_builder_shutdown_waits_for_active_organizer_cleanup(self):
        web.allow_active_operations()
        event = web._begin_operation("upgrade")
        server_called = threading.Event()
        shutdown_returned = threading.Event()
        registry_at_server_shutdown = []
        saved_jobs = builder_web.JOBS
        builder_web.JOBS = builder_web.BuilderJobLifecycle()

        class FakeServer:
            @staticmethod
            def shutdown():
                with web.ACTIVE_OPERATION_LOCK:
                    registry_at_server_shutdown.append(
                        tuple(web.ACTIVE_OPERATIONS))
                server_called.set()

        def shutdown():
            builder_web.shutdown_when_safe(FakeServer())
            shutdown_returned.set()

        thread = threading.Thread(target=shutdown)
        try:
            thread.start()
            self.assertTrue(event.wait(2))
            self.assertFalse(server_called.wait(0.05))
            self.assertFalse(shutdown_returned.is_set())
            self.assertTrue(thread.is_alive())

            web._finish_operation("upgrade", event)
            self.assertTrue(server_called.wait(2))
            self.assertTrue(shutdown_returned.wait(2))
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(registry_at_server_shutdown, [()])
        finally:
            web._finish_operation("upgrade", event)
            if thread.ident is not None:
                thread.join(timeout=2)
            builder_web.JOBS = saved_jobs
            web.allow_active_operations()

    def test_native_upgrade_cancellation_reaps_and_unregisters_child(self):
        helper = os.path.join(self.temp.name, "fake_native_upgrade.py")
        ready = os.path.join(self.temp.name, "native-upgrade-ready")
        output = os.path.join(self.temp.name, "unused-output.bspool")
        with open(helper, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys, time\n"
                "with open(sys.argv[1], 'w', encoding='utf-8') as marker:\n"
                "    marker.write('ready')\n"
                "sys.stderr.write('fake helper started\\n')\n"
                "sys.stderr.flush()\n"
                "while True:\n"
                "    time.sleep(0.05)\n")

        original_binary = web._native_pool_binary
        original_popen = web.subprocess.Popen
        children = []

        def start_fake_helper(command, **kwargs):
            self.assertEqual(command[1], "upgrade")
            process = original_popen(
                [sys.executable, "-u", helper, command[2]], **kwargs)
            children.append(process)
            return process

        web._native_pool_binary = lambda: "fake-native-upgrade"
        web.subprocess.Popen = start_fake_helper
        try:
            with self.assertRaises(web.OperationCancelled):
                web._run_native_upgrade(
                    ready, output, cancel_check=lambda: os.path.exists(ready))
        finally:
            web.subprocess.Popen = original_popen
            web._native_pool_binary = original_binary
            for process in children:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=2)

        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        with web.ACTIVE_UPGRADE_PROCESS_LOCK:
            self.assertNotIn(children[0], web.ACTIVE_UPGRADE_PROCESSES)
            self.assertFalse(web.ACTIVE_UPGRADE_PROCESSES)

    def test_native_upgrade_classifies_committed_block_damage_before_publish(
            self):
        helper = os.path.join(self.temp.name, "fake_damaged_upgrade.py")
        with open(helper, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys\n"
                "sys.stderr.write("
                "'merge error: source.bspool is damaged: BSP3 block 560764 "
                "at byte 3725692195 does not match its committed index\\n')\n"
                "raise SystemExit(1)\n")
        original_binary = web._native_pool_binary
        original_popen = web.subprocess.Popen

        def start_fake_helper(_command, **kwargs):
            return original_popen(
                [sys.executable, "-u", helper], **kwargs)

        web._native_pool_binary = lambda: "fake-native-upgrade"
        web.subprocess.Popen = start_fake_helper
        try:
            with self.assertRaisesRegex(
                    web.FormatSourceDamaged, "block 560764"):
                web._run_native_upgrade(
                    "source.bspool", "staged.bspool")
        finally:
            web.subprocess.Popen = original_popen
            web._native_pool_binary = original_binary

    def test_native_upgrade_reports_verified_header_reconstruction(self):
        helper = os.path.join(self.temp.name, "fake_repaired_upgrade.py")
        with open(helper, "w", encoding="utf-8") as handle:
            handle.write(
                "import sys\n"
                "sys.stderr.write("
                "'upgrade: safely reconstructed 1 BSP3 block header "
                "prefix in memory (first block=7 byte=9000); "
                "source unchanged\\n')\n")
        original_binary = web._native_pool_binary
        original_popen = web.subprocess.Popen

        def start_fake_helper(_command, **kwargs):
            return original_popen(
                [sys.executable, "-u", helper], **kwargs)

        web._native_pool_binary = lambda: "fake-native-upgrade"
        web.subprocess.Popen = start_fake_helper
        try:
            result = web._run_native_upgrade(
                "source.bspool", "staged.bspool")
        finally:
            web.subprocess.Popen = original_popen
            web._native_pool_binary = original_binary
        self.assertEqual(
            result["reconstructed_bsp3_header_prefixes"], 1)

    def test_standalone_http_cancel_interrupts_active_split(self):
        self.exercise_http_split_cancel(
            web.make_handler(self.temp.name), "/api/split", "/api/cancel")

    def test_embedded_builder_http_cancel_interrupts_active_split(self):
        class UnifiedHandler(builder_web.Handler):
            pass
        UnifiedHandler.pool_dir = self.temp.name
        self.exercise_http_split_cancel(
            UnifiedHandler, "/organizer/api/split", "/organizer/api/cancel")

    def test_standalone_http_cancel_interrupts_format_update(self):
        self.exercise_http_format_cancel(
            web.make_handler(self.temp.name),
            "/api/format/plan", "/api/format/update", "/api/cancel",
            "Standalone HTTP Cancel BSP3.bspool")

    def test_embedded_http_cancel_interrupts_format_update(self):
        class UnifiedHandler(builder_web.Handler):
            pass
        UnifiedHandler.pool_dir = self.temp.name
        self.exercise_http_format_cancel(
            UnifiedHandler,
            "/organizer/api/format/plan",
            "/organizer/api/format/update",
            "/organizer/api/cancel",
            "Embedded HTTP Cancel BSP3.bspool")

    def test_standalone_http_checks_and_updates_pool_format(self):
        self.exercise_http_format_upgrade(
            web.make_handler(self.temp.name),
            "/api/format/plan", "/api/format/update",
            "Standalone HTTP BSP3.bspool")

    def test_embedded_builder_http_checks_and_updates_pool_format(self):
        class UnifiedHandler(builder_web.Handler):
            pass
        UnifiedHandler.pool_dir = self.temp.name
        self.exercise_http_format_upgrade(
            UnifiedHandler,
            "/organizer/api/format/plan",
            "/organizer/api/format/update",
            "Embedded HTTP BSP3.bspool")

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

            format_plan = self.post_json(
                base, "/api/format/plan", {"source": self.source_name})
            self.assertEqual(format_plan["source_format"], "BSP3")
            self.assertEqual(format_plan["status"], "blocked")

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
            self.assertIn("Combine seed lists", page)
            self.assertIn("Update pool format", page)
            with urlopen(base + "/organizer/api/pools", timeout=10) as response:
                pools = json.loads(response.read().decode("utf-8"))["pools"]
            self.assertIn(self.source_name, [row["name"] for row in pools])

            format_body = json.dumps({
                "source": self.source_name,
            }).encode("utf-8")
            format_request = Request(
                base + "/organizer/api/format/plan", data=format_body,
                headers={"Content-Type": "application/json"})
            with urlopen(format_request, timeout=10) as response:
                format_plan = json.loads(response.read().decode("utf-8"))
            self.assertEqual(format_plan["source_format"], "BSP3")
            self.assertEqual(format_plan["status"], "blocked")

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
