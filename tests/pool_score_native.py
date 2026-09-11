#!/usr/bin/env python3
"""Native exact-score parity, persistent protocol, corruption, and cancellation."""

from contextlib import contextmanager
import copy
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import pool_score_native as native
import tagcalc as model
import tagcalc_engine as engine

HELPER = Path(os.environ.get("BRAINSTORM_POOL_HELPER", str(
    ROOT / "native" / ("brainstorm_seed_pool.exe" if os.name == "nt" else "brainstorm_seed_pool"))))


def placements(text):
    return tuple((tag["kind"], tag["real_ante"], tag["blind"]) for tag in model.parse_tags(text))


def tags_for(values):
    return [{"kind": kind, "real_ante": ante, "blind": blind,
             "label": "%s A%d%s" % (kind.upper(), ante, model.BLIND_SHORT[blind])}
            for kind, ante, blind in values]


@unittest.skipUnless(HELPER.is_file(), "Build native/brainstorm_seed_pool first")
class NativeParityTests(unittest.TestCase):
    def test_persistent_scorer_exact_routes_and_compact_results(self):
        examples = ["", "n8s n9b", "n5b r7s", "n5s r7b", "n5s r5b n6s r6b n7s r7b",
                    "n7s r10b n19s r28b", "n6s r8b n11s r14b n18s r23b n29s r35b",
                    "r7s n15b r19s r20s r22s n23b n25s n26s r27b n29s r36b r37s"]
        with native.NativeScorer(HELPER) as scorer:
            pid = scorer._process.pid
            for text in examples:
                with self.subTest(tags=text):
                    values = placements(text)
                    expected = engine.optimize(tags_for(values), (4, 2))
                    self.assertEqual(scorer.score((4, 2), values), expected)
                    self.assertEqual(scorer.score((4, 2), values, details=False),
                                     (expected[0], [], expected[2]))
                    self.assertEqual(scorer._process.pid, pid)
        self.assertIsNotNone(scorer._process.poll())
        scorer.close()

    def test_deterministic_randomized_routes_match_python_exactly(self):
        rng = random.Random(723501)
        with native.NativeScorer(HELPER) as scorer:
            for case in range(55):
                values = tuple((rng.choice(("neg", "rare")), slot // 2 + 1, slot % 2)
                               for slot in sorted(rng.sample(range(76), rng.randrange(2, 15))))
                baseline = (rng.randrange(1, 13), rng.randrange(3))
                with self.subTest(case=case, baseline=baseline):
                    self.assertEqual(scorer.score(baseline, values),
                                     engine.optimize(tags_for(values), baseline))

    def test_dense_twenty_tag_case_and_repeat_compact(self):
        values = placements("n5s r6b n8s r10b n11s r13b n15s r16b n18s r20b "
                            "n21s r23b n25s r26b n28s r30b n31s r33b n35s r37b")
        expected = engine.optimize(tags_for(values), (4, 2))
        with native.NativeScorer(HELPER) as scorer:
            self.assertEqual(scorer.score((4, 2), values), expected)
            for _ in range(5):
                self.assertEqual(scorer.score((4, 2), values, details=False),
                                 (expected[0], [], expected[2]))

    def test_duplicate_inputs_ante39_and_first_copy_boundaries(self):
        values = placements("n1s r4b n5s r7b n38s r38b")
        with native.NativeScorer(HELPER) as scorer:
            for baseline in ((1, 0), (4, 1), (4, 2), (38, 1), (38, 2), (40, 2)):
                expected = engine.optimize(tags_for(values), baseline)
                self.assertEqual(scorer.score(baseline, values[::-1] + values + (("neg", 39, 0),)), expected)
            with self.assertRaises(ValueError):
                scorer.score((4, 2), (("neg", 5, 0), ("rare", 5, 0)))
            self.assertEqual(scorer.score((4, 2), values), engine.optimize(tags_for(values), (4, 2)))

    def test_all_seventy_six_placements_and_maximum_pair_count(self):
        # 38 of each kind exercises all 76 placement slots and 1,444 pairs.
        # Late baselines keep the Python oracle quick while preserving those
        # bounds, the final copy boundary, and valid/no-route outcomes.
        values = tuple(("neg" if slot % 2 == 0 else "rare", slot // 2 + 1, slot % 2)
                       for slot in range(76))
        with native.NativeScorer(HELPER) as scorer:
            for baseline in ((30, 2), (37, 2), (38, 1)):
                with self.subTest(baseline=baseline):
                    self.assertEqual(scorer.score(baseline, values),
                                     engine.optimize(tags_for(values), baseline))

    def test_direct_protocol_rejects_bad_numeric_frames(self):
        frames = ["0 4 2 1 0\n", "1 0 2 1 0\n", "1 4 3 1 0\n", "1 4 2 2 0\n",
                  "1 4 2 1 77\n", "1 4 2 1 1 2 7 0\n", "1 4 2 1 1 0 39 0\n",
                  "1 4 2 1 2 0 7 0 1 7 0\n", "1 4 2 1 0 extra\n",
                  "18446744073709551616 4 2 1 0\n", "1 -4 2 1 0\n", "1 4 2 1 0",
                  "1" * 5000 + "\n", "1 4 2 1 0\n1 4 2 1 0\n"]
        for frame in frames:
            with self.subTest(frame=frame[:60]):
                result = subprocess.run([str(HELPER), "score-tags"], input=frame,
                                        text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stdout.startswith("BRAINSTORM_TAG_SCORE 1\n"))
                self.assertIn("score-tags:", result.stderr)


class AdapterFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="native score protocol ")
        self.addCleanup(self.temp.cleanup)
        self.processes = []

    @contextmanager
    def fake(self, body, handshake="BRAINSTORM_TAG_SCORE 1"):
        path = Path(self.temp.name) / "helper.py"
        path.write_text("import sys,time,json\nprint(%r,flush=True)\n" % handshake + body, encoding="utf-8")
        original = subprocess.Popen

        def start(unused_command, **options):
            process = original([sys.executable, str(path)], **options)
            self.processes.append(process)
            return process

        with mock.patch.object(native.subprocess, "Popen", start):
            try:
                yield
            finally:
                for process in self.processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)

    def test_wrong_handshake_is_fatal_and_closes_child(self):
        with self.fake("time.sleep(30)\n", handshake="BRAINSTORM_TAG_SCORE 2"):
            with self.assertRaisesRegex(native.NativeScoreError, "protocol"):
                native.NativeScorer("unused")
            self.assertIsNotNone(self.processes[-1].poll())

    def test_exited_child_before_request_is_a_native_error(self):
        with self.fake(""):
            with native.NativeScorer("unused") as scorer:
                scorer._process.wait(timeout=2)
                with self.assertRaisesRegex(native.NativeScoreError, "request|stopped"):
                    scorer.score((4, 2), (), details=False)
                self.assertTrue(scorer._closed.is_set())

    def test_cancel_before_start_does_not_create_a_child(self):
        with self.fake(""):
            with self.assertRaises(native.NativeScoreCancelled):
                native.NativeScorer("unused", lambda: True)
            self.assertEqual(self.processes, [])

    def test_cancel_during_handshake_reaps_child(self):
        cancelled = threading.Event()
        # Cancel after process creation, before accepting its handshake.
        with self.fake("time.sleep(30)\n"):
            original = native.NativeScorer._read_reply

            def cancel_at_handshake(scorer, deadline=None):
                cancelled.set()
                return original(scorer, deadline)

            with mock.patch.object(native.NativeScorer, "_read_reply", cancel_at_handshake):
                with self.assertRaises(native.NativeScoreCancelled):
                    native.NativeScorer("unused", cancelled.is_set)
            self.assertIsNotNone(self.processes[-1].poll())

    def test_concurrent_request_rejection_keeps_active_request_usable(self):
        body = ("for line in sys.stdin:\n"
                " request=int(line.split()[0])\n"
                " print(json.dumps(dict(id=request,score=-1,fillers=[5,5],route=[])),flush=True)\n")
        with self.fake(body):
            with native.NativeScorer("unused") as scorer:
                entered = threading.Event()
                release = threading.Event()
                original = scorer._read_reply

                def reading(deadline=None):
                    entered.set()
                    if not release.wait(timeout=2):
                        raise AssertionError("Concurrent request did not finish")
                    return original(deadline)

                results = []
                with mock.patch.object(scorer, "_read_reply", reading):
                    worker = threading.Thread(target=lambda: results.append(scorer.score((4, 2), ())))
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(timeout=2))
                        with self.assertRaisesRegex(native.NativeScoreError, "concurrent"):
                            scorer.score((4, 2), ())
                    finally:
                        release.set()
                        worker.join(timeout=2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(results, [(-1.0, [], (5, 5))])
                self.assertIsNone(scorer._process.poll())
                self.assertEqual(scorer.score((4, 2), ()), results[0])

    def test_corrupt_replies_never_fall_back(self):
        replies = ['{}', '{"id":2,"score":3,"fillers":[5,5],"route":[]}',
                   '{"id":1,"score":NaN,"fillers":[5,5],"route":[]}',
                   '{"id":1,"score":-2,"fillers":[5,5],"route":[]}',
                   '{"id":1,"score":3,"fillers":[4,5],"route":[]}',
                   '{"id":1,"id":1,"score":3,"fillers":[5,5],"route":[]}']
        for reply in replies:
            with self.subTest(reply=reply), self.fake("sys.stdin.readline()\nprint(%r,flush=True)\n" % reply):
                with native.NativeScorer("unused") as scorer:
                    with self.assertRaises(native.NativeScoreError):
                        scorer.score((4, 2), placements("n7s r10b"), details=False)
                    self.assertIsNotNone(scorer._process.poll())

    def test_forged_route_is_replayed_and_rejected(self):
        values = placements("n7s r10b")
        score, route, fillers = engine.optimize(tags_for(values), (4, 2))
        forged = dict(id=1, score=score, route=copy.deepcopy(route), fillers=list(fillers))
        forged["route"][0]["k"] += 1
        with self.fake("sys.stdin.readline()\nprint(%r,flush=True)\n" % json.dumps(forged)):
            with native.NativeScorer("unused") as scorer:
                with self.assertRaisesRegex(native.NativeScoreError, "route k"):
                    scorer.score((4, 2), values)

    def test_truncated_or_oversized_reply_and_bounded_stderr(self):
        bodies = ["sys.stdin.readline()\nsys.stdout.write('{');sys.stdout.flush()\n",
                  "sys.stdin.readline()\nprint('x'*200000,flush=True)\n",
                  "sys.stdin.readline()\nsys.stderr.write('z'*200000);sys.stderr.flush()\n"]
        for body in bodies:
            with self.subTest(body=body[:55]), self.fake(body):
                with native.NativeScorer("unused") as scorer:
                    with self.assertRaises(native.NativeScoreError) as caught:
                        scorer.score((4, 2), (), details=False)
                    self.assertLess(len(str(caught.exception)), 17000)
                    self.assertLessEqual(sum(map(len, scorer._stderr)), 16384)

    def test_cancellation_interrupts_blocked_result_and_reaps_process(self):
        cancelled = threading.Event()
        with self.fake("sys.stdin.readline()\ntime.sleep(30)\n"):
            with native.NativeScorer("unused", cancelled.is_set) as scorer:
                timer = threading.Timer(0.08, cancelled.set)
                timer.start()
                started = time.monotonic()
                try:
                    with self.assertRaises(native.NativeScoreCancelled):
                        scorer.score((4, 2), (), details=False)
                finally:
                    timer.join()
                self.assertLess(time.monotonic() - started, 2)
                self.assertIsNotNone(scorer._process.poll())
                self.assertTrue(all(not thread.is_alive() for thread in scorer._threads))

    def test_external_close_interrupts_active_request_and_is_idempotent(self):
        with self.fake("sys.stdin.readline()\ntime.sleep(30)\n"):
            scorer = native.NativeScorer("unused")
            failures = []

            def score():
                try:
                    scorer.score((4, 2), (), details=False)
                except native.NativeScoreError as error:
                    failures.append(error)

            worker = threading.Thread(target=score)
            worker.start()
            time.sleep(0.05)
            scorer.close()
            worker.join(timeout=2)
            scorer.close()
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertIsNotNone(scorer._process.poll())
            self.assertTrue(all(not thread.is_alive() for thread in scorer._threads))


if __name__ == "__main__":
    unittest.main()
