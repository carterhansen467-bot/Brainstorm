#!/usr/bin/env python3
"""Actual native scoring jobs, durable resume, and publication safety."""

import importlib.util
import copy
import csv
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import brainstorm_pool_organizer as organizer
import pool_rule_workflow as workflow
import pool_score_workflow as scoring
import pool_score_native as native
from pool_organizer import descriptor, write_custom_bsp3


MARKER = b"\x82BSTAG\x01\x00\x4b"


def tag(kind, ante, phase):
    return descriptor(1, "tag_" + kind, ante, phase, 0, 0, 0)


EVENTS = [MARKER, tag("negative", 3, 1), tag("rare", 5, 2),
          tag("negative", 7, 2), tag("rare", 12, 1), b"\x93kept"]


class ScoreWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.binary = Path(os.environ.get("BRAINSTORM_TEST_POOL_BINARY") or ROOT / "native" /
                          ("brainstorm_seed_pool.exe" if os.name == "nt" else "brainstorm_seed_pool"))
        if not cls.binary.is_file():
            if os.environ.get("CI"):
                raise AssertionError("Build the native pool helper before scoring tests")
            raise unittest.SkipTest("Native pool helper is not built")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pool score jobs ")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.helper = SimpleNamespace(binary=str(self.binary))
        self.service = scoring.ScoreService(self.folder, self.helper)
        self.addCleanup(self.service.shutdown)

    def fixture(self, name="L1", ranks=(0, 1, 2), events=None):
        path = self.folder / (name + ".bspool")
        write_custom_bsp3(str(path), list(ranks), events or [EVENTS for _ in ranks],
                         workflow._fingerprint(name)[:16], ["tag_route observe"], range_end=max(ranks) + 100)
        reader = organizer.BSPoolReader(path, verify_payloads=False)
        text = "\n".join("label " + name if line.startswith("label ") else line
                         for line in reader.header.text.splitlines()) + "\n"
        with open(path, "r+b") as handle:
            handle.write(text.encode("ascii").ljust(reader.header_bytes, b"\0"))
        return path

    def request(self, *paths, **settings):
        return {"pools": [{"source": path.name, "second_tag": "A5B", "second_tag_type": "rare"}
                          for path in paths], "workers": 2, "top": 10, **settings}

    def wait(self, service, job_id, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = service.status(job_id)
            if job["status"] not in scoring.ACTIVE:
                # A last status write precedes the thread's final close/guard release.
                thread = service._threads.get(job_id)
                if thread:
                    thread.join(2)
                return service.status(job_id)
            time.sleep(0.01)
        self.fail("Scoring job did not finish: %r" % service.status(job_id))

    @contextmanager
    def connection(self, job):
        with closing(sqlite3.connect(self.folder / ".score-jobs" / job["job_id"] / "scores.sqlite")) as connection:
            with connection:
                yield connection

    def test_result_connections_close_after_success_and_read_failure(self):
        path = self.fixture()
        final = self.wait(self.service, self.service.start(self.request(path))["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        connect = sqlite3.connect
        opened = []
        def tracked(*args, **kwargs):
            connection = connect(*args, **kwargs)
            opened.append(connection)
            self.addCleanup(connection.close)
            return connection
        with mock.patch.object(scoring.sqlite3, "connect", side_effect=tracked):
            self.assertEqual(self.service.results(final["job_id"])["total"], 3)
        self.assertEqual(len(opened), 1)
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed database"):
            opened[-1].execute("SELECT 1")
        with self.connection(final) as connection:
            connection.execute("UPDATE leaders SET value='invalid JSON' WHERE scope='combined' AND position=1")
        with mock.patch.object(scoring.sqlite3, "connect", side_effect=tracked):
            with self.assertRaises(json.JSONDecodeError):
                self.service.results(final["job_id"])
        self.assertEqual(len(opened), 2)
        with self.assertRaisesRegex(sqlite3.ProgrammingError, "closed database"):
            opened[-1].execute("SELECT 1")

    def test_multiple_pool_scores_distinct_combined_leaderboard_and_pinned_metadata(self):
        one = self.fixture("L1", (0, 1, 2))
        two = self.fixture("L2", (1, 2, 3))
        before = one.read_bytes(), two.read_bytes()
        job = self.service.start(self.request(one, two, reference_score=1, reference_seed="reference"))
        final = self.wait(self.service, job["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertEqual((final["completed_records"], final["total_records"], final["scored"]), (6, 6, 6))
        leaders = self.service.results(job["job_id"])
        self.assertEqual(leaders["total"], 4)
        self.assertEqual(len({row["seed"] for row in leaders["rows"]}), 4)
        self.assertEqual(self.service.results(job["job_id"], "p000")["total"], 3)
        self.assertEqual(len(self.service.results(job["job_id"], offset=1, limit=2)["rows"]), 2)
        self.assertTrue(all(row["beats_reference"] for row in leaders["rows"]))
        for row in leaders["rows"]:
            self.assertEqual(row["second_tag_type"], "rare")
            self.assertEqual(row["baseline_copy"], "A5Boss")
            self.assertEqual({bytes.fromhex(item["raw_hex"]) for item in row["input"]["occurrences"]}, set(EVENTS))
            self.assertEqual(row["input"]["source_snapshot_id"],
                             final["pools"][int(row["pool_id"][1:])]["source_pin"]["snapshot_id"])
            self.assertTrue(row["hieroglyph_label"].startswith(("Before original Ante", "After original Ante")))
        with self.connection(job) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM scores").fetchone()[0], 6)
            self.assertNotIn("route", {row[1] for row in connection.execute("PRAGMA table_info(scores)")})
        artifacts = self.folder / ".score-jobs" / job["job_id"]
        self.assertFalse((artifacts / "all-scores.ndjson").exists())
        self.assertFalse((artifacts / "p000-tags.ndjson").exists())
        self.assertTrue(Path(self.service.download(job["job_id"], "combined-leaderboard.csv")).exists())
        self.assertTrue(all("metadata" not in pool for pool in final["pools"]))
        persisted = json.loads(Path(self.service.download(job["job_id"], "summary.json")).read_text(encoding="utf-8"))
        self.assertIn("header_text", persisted["pools"][0]["metadata"]["source"])
        self.assertEqual((one.read_bytes(), two.read_bytes()), before)
        # A cancel click during the final thread/handle cleanup cannot turn a
        # durably completed job back into a resumable interrupted job.
        with mock.patch.object(self.service._threads[job["job_id"]], "is_alive", return_value=True):
            self.assertEqual(self.service.cancel(job["job_id"])["status"], "completed")

    def test_saved_second_tag_is_automatic_and_contradictory_overrides_are_refused(self):
        path = self.fixture()
        reader = organizer.BSPoolReader(path)
        recipe = {"version": 1, "mode": "second_tag", "rule": {"version": 1, "range": {"start": "A3S", "end": "A7B"}}}
        report, _ = workflow.publish(reader, workflow.preview(reader, recipe), self.folder)
        derived = Path(report["outputs"][0]["path"])
        described = self.service.describe(derived.name)
        self.assertEqual(described["second_tag"]["position"], "A5B")
        self.assertEqual(described["baseline_copy"], "A5Boss")
        job = self.service.start({"pools": [{"source": derived.name}]})
        self.assertEqual(job["request"]["top"], 1000)
        final = self.wait(self.service, job["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertTrue(all(row["first_tag_type"] == "negative" for row in self.service.results(job["job_id"])["rows"]))
        for fields in ({"second_tag": "A4S"}, {"second_tag_type": "negative"}, {"baseline_copy": "A5B"}):
            with self.subTest(fields=fields), self.assertRaises(organizer.PoolError):
                self.service.start({"pools": [{"source": derived.name, **fields}]})

    def interrupted(self, path):
        reached = threading.Event()
        real = native.NativeScorer.score
        calls = [0]
        lock = threading.Lock()
        def paused(scorer, baseline, placements, details=True):
            value = real(scorer, baseline, placements, details=details)
            if not details:
                with lock:
                    calls[0] += 1
                    stopping = calls[0] >= 6
                if stopping:
                    reached.set()
                    while not scorer.cancel_check():
                        time.sleep(0.005)
                    raise native.NativeScoreCancelled("test cancellation")
            return value
        with mock.patch.object(native.NativeScorer, "score", paused):
            job = self.service.start(self.request(path))
            self.assertTrue(reached.wait(5))
            self.service.cancel(job["job_id"])
            final = self.wait(self.service, job["job_id"])
        self.assertEqual(final["status"], "interrupted", final)
        self.assertGreater(final["completed_records"], 0)
        self.assertLess(final["completed_records"], final["total_records"])
        self.assertEqual(self.service.results(job["job_id"])["rows"], [])
        return final

    def test_cancel_restart_resume_reuses_checkpoint_without_duplicate_scores(self):
        path = self.fixture(ranks=tuple(range(20)), events=[EVENTS + [tag("rare", 13 + index, 1)] for index in range(20)])
        paused = self.interrupted(path)
        self.service.shutdown()
        resumed = scoring.ScoreService(self.folder, self.helper)
        self.addCleanup(resumed.shutdown)
        self.assertTrue(resumed.list_jobs()[0]["can_resume"])
        calls = [0]
        real = native.NativeScorer.score
        def counted(scorer, baseline, placements, details=True):
            if not details:
                calls[0] += 1
            return real(scorer, baseline, placements, details=details)
        with mock.patch.object(native.NativeScorer, "score", counted):
            resumed.resume(paused["job_id"])
            final = self.wait(resumed, paused["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertEqual(calls[0], 20 - paused["completed_records"])
        with self.connection(paused) as connection:
            self.assertEqual(connection.execute("SELECT count(*),count(DISTINCT rank) FROM scores").fetchone(), (20, 20))
        self.assertEqual(len(resumed.list_jobs()), 1)

    def test_resume_rejects_changed_model_settings_or_source(self):
        path = self.fixture(ranks=tuple(range(20)), events=[EVENTS + [tag("rare", 13 + index, 1)] for index in range(20)])
        paused = self.interrupted(path)
        with mock.patch.object(scoring, "_binary_model", return_value={"version": "new"}):
            with self.assertRaisesRegex(organizer.PoolError, "model changed"):
                self.service.resume(paused["job_id"])
        status = path.stat()
        os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1000000000))
        with self.assertRaisesRegex(organizer.PoolError, "changed"):
            self.service.resume(paused["job_id"])

    def test_resume_rejects_modified_effective_settings_before_reusing_scores(self):
        path = self.fixture(ranks=tuple(range(20)), events=[EVENTS + [tag("rare", 13 + index, 1)] for index in range(20)])
        paused = self.interrupted(path)
        summary = self.folder / ".score-jobs" / paused["job_id"] / "summary.json"
        saved = json.loads(summary.read_text(encoding="utf-8"))
        changes = ({"baseline_copy": "A7Boss"}, {"second_tag": "A7B"},
                   {"second_tag_type": "negative"}, {"pool_id": "p001"},
                   {"source": "another.bspool"}, {"records": 19})
        for fields in changes:
            with self.subTest(fields=fields):
                modified = copy.deepcopy(saved)
                modified["pools"][0].update(fields)
                summary.write_text(json.dumps(modified), encoding="utf-8")
                with self.assertRaisesRegex(organizer.PoolError, "checkpoint settings changed"):
                    self.service.resume(paused["job_id"])
                self.assertFalse(any(thread.is_alive() for thread in self.service._threads.values()))
        modified = copy.deepcopy(saved)
        modified["request"]["top"] += 1
        summary.write_text(json.dumps(modified), encoding="utf-8")
        with self.assertRaisesRegex(organizer.PoolError, "checkpoint settings changed"):
            self.service.resume(paused["job_id"])
        summary.write_text(json.dumps(saved), encoding="utf-8")
        self.service.resume(paused["job_id"])
        final = self.wait(self.service, paused["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))

    def test_separate_baselines_combined_best_seed_top_limit_and_persistent_history(self):
        one = self.fixture("Earlier", (0, 1, 2))
        two = self.fixture("Later", (0, 1, 2))
        request = self.request(one, two, top=2)
        request["pools"][0]["second_tag"] = "A3S"
        request["pools"][0]["second_tag_type"] = "negative"
        final = self.wait(self.service, self.service.start(request)["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        earlier = self.service.results(final["job_id"], "p000")["rows"]
        later = self.service.results(final["job_id"], "p001")["rows"]
        self.assertEqual({row["baseline_copy"] for row in earlier}, {"A3B"})
        self.assertEqual({row["baseline_copy"] for row in later}, {"A5Boss"})
        self.assertNotEqual(earlier[0]["score"], later[0]["score"])
        combined = self.service.results(final["job_id"])
        expected = max(earlier[0]["score"], later[0]["score"])
        self.assertEqual(combined["total"], 2)
        self.assertEqual(len({row["seed"] for row in combined["rows"]}), 2)
        self.assertTrue(all(row["score"] == expected for row in combined["rows"]))
        with open(self.service.download(final["job_id"], "combined-leaderboard.csv"), encoding="utf-8-sig", newline="") as handle:
            exported = list(csv.DictReader(handle))
        self.assertEqual([row["seed"] for row in exported], [row["seed"] for row in combined["rows"]])
        self.service.shutdown()
        reopened = scoring.ScoreService(self.folder, self.helper)
        self.addCleanup(reopened.shutdown)
        self.assertEqual(reopened.list_jobs()[0]["status"], "completed")
        self.assertEqual(reopened.results(final["job_id"]), combined)

    def test_no_valid_routes_complete_with_empty_leaderboards_and_exported_rows(self):
        path = self.fixture(events=[[MARKER] for _ in range(3)])
        final = self.wait(self.service, self.service.start({
            "pools": [{"source": path.name, "baseline_copy": "A38Boss"}], "export_all": True})["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertEqual((final["completed_records"], final["scored"], final["no_valid_route"]), (3, 0, 3))
        self.assertEqual(self.service.results(final["job_id"])["total"], 0)
        self.assertEqual(self.service.results(final["job_id"], "p000")["rows"], [])
        rows = [json.loads(line) for line in Path(self.service.download(final["job_id"], "all-scores.ndjson")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["score"] is None and row["route"] == [] for row in rows))

    def test_missing_coverage_fails_without_publishing_any_leaderboard(self):
        path = self.fixture(ranks=(0, 1, 2), events=[EVENTS, EVENTS, EVENTS[1:]])
        final = self.wait(self.service, self.service.start(self.request(path, workers=1))["job_id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("lacks recorded coverage", final["error"])
        self.assertEqual(final["downloads"], [])
        self.assertEqual(self.service.results(final["job_id"])["rows"], [])
        self.assertGreater(final["completed_records"], 0)
        with self.assertRaises(organizer.PoolError):
            self.service.download(final["job_id"], "scores.sqlite")

    def test_combined_provenance_survives_deleted_originals_and_leader_hydration(self):
        one = self.fixture("L1", (0, 1))
        two = self.fixture("L2", (1, 2))
        combined = self.folder / "Complete.bspool"
        organizer.combine_pools([organizer.BSPoolReader(one), organizer.BSPoolReader(two)], str(combined))
        one.unlink()
        two.unlink()
        final = self.wait(self.service, self.service.start(self.request(combined))["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        overlap = next(row for row in self.service.results(final["job_id"])["rows"] if row["rank"] == 1)
        self.assertEqual(set(overlap["original_source_labels"]), {"L1", "L2"})
        self.assertEqual(len(overlap["source_labels"]), 2)
        self.assertEqual(len([item for item in overlap["input"]["occurrences"] if item.get("kind") == "provenance"]), 2)

    def test_advanced_export_and_path_validation(self):
        path = self.fixture()
        for name in ("../L1.bspool", "..\\L1.bspool", "/tmp/L1.bspool", "L1.txt"):
            with self.subTest(name=name), self.assertRaises(organizer.PoolError):
                self.service.describe(name)
        final = self.wait(self.service, self.service.start(self.request(path, export_all=True))["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        tags_path = Path(self.service.download(final["job_id"], "p000-tags.ndjson"))
        documents = [json.loads(line) for line in tags_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(documents[-1]["type"], "tag_export_complete")
        self.assertEqual(documents[-1]["records"], 3)
        rows = [json.loads(line) for line in Path(self.service.download(final["job_id"], "all-scores.ndjson")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 3)
        leaders = {row["seed"]: row for row in self.service.results(final["job_id"])["rows"]}
        for row in rows:
            self.assertTrue(row["route"])
            self.assertEqual(row["route"], leaders[row["seed"]]["route"])
            self.assertEqual(row["score"], row["route"][-1]["score"])
        with self.assertRaises(organizer.PoolError):
            self.service.download(final["job_id"], "../L1.bspool")
        with self.assertRaises(organizer.PoolError):
            self.service.status("../../etc/passwd")
        with self.assertRaises(organizer.PoolError):
            self.service.status({"job_id": final["job_id"]})
        with self.assertRaises(organizer.PoolError):
            self.service.download(final["job_id"], {"filename": "scores.sqlite"})
        self.service.shutdown()
        with self.assertRaisesRegex(organizer.PoolError, "shutting down"):
            self.service.start(self.request(path))

    def test_repeated_placements_use_bounded_worker_native_score_cache(self):
        path = self.fixture(ranks=tuple(range(50)))
        actual = native.NativeScorer.score
        calls, detailed = [], []
        def counted(scorer, baseline, placements, details=True):
            (detailed if details else calls).append((baseline, placements))
            return actual(scorer, baseline, placements, details=details)
        with mock.patch.object(native.NativeScorer, "score", counted):
            final = self.wait(self.service, self.service.start(self.request(path, export_all=True))["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertEqual(final["scored"], 50)
        self.assertGreaterEqual(len(calls), 1)
        self.assertLessEqual(len(calls), 2)
        self.assertEqual(len(detailed), 1)

    def test_cancel_during_detailed_export_reuses_scores_and_replaces_partial_exports(self):
        path = self.fixture(ranks=tuple(range(10)), events=[EVENTS + [tag("rare", 13 + index, 1)] for index in range(10)])
        reached, exporting = threading.Event(), threading.Event()
        actual_score, actual_export = native.NativeScorer.score, self.service._export_all
        def export(*args, **kwargs):
            exporting.set()
            return actual_export(*args, **kwargs)
        def stopped(scorer, baseline, placements, details=True):
            result = actual_score(scorer, baseline, placements, details=details)
            if details and exporting.is_set():
                reached.set()
                while not scorer.cancel_check():
                    time.sleep(0.005)
                raise native.NativeScoreCancelled("interrupt detailed export")
            return result
        with mock.patch.object(self.service, "_export_all", export), mock.patch.object(native.NativeScorer, "score", stopped):
            job = self.service.start(self.request(path, top=1, export_all=True))
            self.assertTrue(reached.wait(5))
            self.service.cancel(job["job_id"])
            paused = self.wait(self.service, job["job_id"])
        self.assertEqual(paused["status"], "interrupted", paused.get("error"))
        self.assertEqual(paused["completed_records"], 10)
        self.assertEqual(self.service.results(job["job_id"])["rows"], [])
        with self.assertRaises(organizer.PoolError):
            self.service.download(job["job_id"], "all-scores.ndjson")
        compact_calls = []
        def counted(scorer, baseline, placements, details=True):
            if not details:
                compact_calls.append(placements)
            return actual_score(scorer, baseline, placements, details=details)
        with mock.patch.object(native.NativeScorer, "score", counted):
            self.service.resume(job["job_id"])
            final = self.wait(self.service, job["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertEqual(compact_calls, [])
        rows = [json.loads(line) for line in Path(self.service.download(job["job_id"], "all-scores.ndjson")).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 10)
        self.assertEqual(len({row["seed"] for row in rows}), 10)
        self.assertTrue(all(row["route"] and row["score"] == row["route"][-1]["score"] for row in rows))

    def test_other_service_cannot_resume_active_checkpoint_and_shutdown_stops_workers(self):
        path = self.fixture()
        reached = threading.Event()
        actual = native.NativeScorer.score
        def blocked(scorer, baseline, placements, details=True):
            value = actual(scorer, baseline, placements, details=details)
            if not details:
                reached.set()
                while not scorer.cancel_check():
                    time.sleep(0.005)
                raise native.NativeScoreCancelled("stopped by shutdown")
            return value
        with mock.patch.object(native.NativeScorer, "score", blocked):
            job = self.service.start(self.request(path))
            self.assertTrue(reached.wait(5))
            other = scoring.ScoreService(self.folder, self.helper)
            self.addCleanup(other.shutdown)
            remote = other.status(job["job_id"])
            self.assertEqual(remote["status"], "running")
            self.assertTrue(remote["running_elsewhere"])
            self.assertFalse(remote["can_resume"])
            with self.assertRaisesRegex(organizer.PoolError, "Pause it there"):
                other.cancel(job["job_id"])
            with self.assertRaises((organizer.PoolError, OSError)):
                other.resume(job["job_id"])
            self.assertEqual(self.service._load(job["job_id"])["status"], "running")
            self.service.shutdown(timeout=2)
        self.assertFalse(any(thread.is_alive() for thread in self.service._threads.values()))
        self.assertEqual(self.service.status(job["job_id"])["status"], "interrupted")

    def test_model_change_during_run_cannot_publish_mixed_model_results(self):
        path = self.fixture()
        actual = native.NativeScorer.score
        identity = scoring._binary_model
        changed = [False]
        def score(scorer, baseline, placements, details=True):
            result = actual(scorer, baseline, placements, details=details)
            if not details:
                changed[0] = True
            return result
        def current(binary):
            value = identity(binary)
            if changed[0]:
                value["version"] = "changed-during-job"
            return value
        with mock.patch.object(native.NativeScorer, "score", score), mock.patch.object(scoring, "_binary_model", current):
            final = self.wait(self.service, self.service.start(self.request(path))["job_id"])
        self.assertEqual(final["status"], "failed")
        self.assertIn("model changed", final["error"])
        self.assertEqual(final["downloads"], [])
        self.assertEqual(self.service.results(final["job_id"])["rows"], [])


class RecordedScoreWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("score_recording_fixtures", ROOT / "tests/pool_tag_recording.py")
        cls.fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixtures)
        cls.fixtures.TagRecordingRegression.setUpClass()
        cls.addClassCleanup(cls.fixtures.TagRecordingRegression.doClassCleanups)

    def test_cancelled_job_retains_recorded_tag_pool_and_resume_does_not_record_again(self):
        fixture = self.fixtures.TagRecordingRegression()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.fixture(ranks=[0])
        service = scoring.ScoreService(fixture.folder, fixture.helper, fixture.snapshot)
        self.addCleanup(service.shutdown)
        reached = threading.Event()
        actual = native.NativeScorer.score
        def stopped(scorer, baseline, placements, details=True):
            result = actual(scorer, baseline, placements, details=details)
            if not details:
                reached.set()
                while not scorer.cancel_check():
                    time.sleep(0.005)
                raise native.NativeScoreCancelled("interrupt after recording")
            return result
        with mock.patch.object(native.NativeScorer, "score", stopped):
            job = service.start({"pools": [{"source": Path(source.path).name, "second_tag": "A4S"}],
                                 "record_missing": True, "top": 5})
            self.assertTrue(reached.wait(5))
            service.cancel(job["job_id"])
            paused = ScoreWorkflowTests.wait(self, service, job["job_id"])
        self.assertEqual(paused["status"], "interrupted")
        recorded = Path(service._evidence_path(paused, paused["pools"][0]))
        self.assertTrue(recorded.exists())
        self.assertEqual(fixture.helper.calls, 1)
        service.resume(job["job_id"])
        final = ScoreWorkflowTests.wait(self, service, job["job_id"])
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertEqual(fixture.helper.calls, 1)
        self.assertTrue(recorded.exists())
        self.assertEqual(service._load(job["job_id"])["pools"][0]["metadata"]["source"]["header_text"], source.header.text)
        winning = service.results(job["job_id"])["rows"][0]
        self.assertIn(MARKER.hex(), {item["raw_hex"] for item in winning["input"]["occurrences"]})
        self.assertEqual(winning["input"]["source_snapshot_id"], source.snapshot_token)


if __name__ == "__main__":
    unittest.main(verbosity=2)
