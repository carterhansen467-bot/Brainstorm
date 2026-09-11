#!/usr/bin/env python3
"""Exercise integrated scoring through both real local HTTP entry points."""

import csv
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import pool_organizer_web as web
import pool_builder_web as builder_web
from pool_organizer import descriptor, write_custom_bsp3


class ScoreWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="score HTTP ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = ROOT / "native" / ("brainstorm_seed_pool.exe" if os.name == "nt" else "brainstorm_seed_pool")
        if not self.binary.exists():
            self.skipTest("Build native helper first")
        self.patch = mock.patch.object(web, "_native_split_helper", lambda: web.NativeSplitHelper(str(self.binary)))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        web.allow_active_operations()
        self.addCleanup(web.shutdown_score_services)
        tag = lambda name, ante, phase: descriptor(1, "tag_" + name, ante, phase, 0, 0, 0)
        records = [[tag("negative", 3, 1), tag("rare", 5, 2), tag("negative", 12, 1), tag("rare", 20, 2),
                    b"\x82BSTAG\x01\x00\x4b", b"\x90keep-me"] for _ in range(3)]
        for name, ranks, data in (("L1.bspool", [1, 2, 3], records), ("L2.bspool", [2, 3], records[:2])):
            write_custom_bsp3(str(self.root / name), ranks, data, "1111111111111111", [])

    def request(self, url, data=None, raw=False):
        request = Request(url, data=None if data is None else json.dumps(data).encode(),
                          headers={} if data is None else {"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=15) as response:
                body = response.read()
                return response.status, body if raw else json.loads(body)
        except HTTPError as response:
            return response.code, json.load(response)

    def test_both_entrypoints_native_job_combined_results_and_downloads(self):
        for unified in (False, True):
            with self.subTest(unified=unified):
                if unified:
                    class Handler(builder_web.Handler):
                        pool_dir = str(self.root)
                else:
                    Handler = web.make_handler(str(self.root))
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%d" % server.server_port
                prefix = base + ("/organizer" if unified else "") + "/api/score/"
                try:
                    code, page = self.request(base + ("/organize" if unified else "/"), raw=True)
                    self.assertEqual(code, 200)
                    self.assertIn(b'id="scoreModeBtn"', page)
                    self.assertIn(b'id="scoreWorkspace"', page)
                    self.assertNotIn(b"/*__SCORE_", page)
                    code, description = self.request(prefix + "describe", {"source": "L1.bspool"})
                    self.assertEqual(code, 200, description)
                    self.assertEqual(description["records"], 3)
                    code, started = self.request(prefix + "start", {
                        "pools": [{"source": "L1.bspool", "second_tag": "A5B"},
                                  {"source": "L2.bspool", "baseline_copy": "A6Boss"}],
                        "workers": 2, "top": 3, "reference_score": 721.77})
                    self.assertEqual(code, 200, started)
                    job_id = started["job_id"]
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        _, status = self.request(prefix + "status?" + urlencode({"job_id": job_id}))
                        if status["status"] not in {"queued", "preparing", "running", "finalizing", "cancelling"}:
                            break
                        time.sleep(0.03)
                    self.assertEqual(status["status"], "completed", status)
                    self.assertEqual(status["completed_records"], 5)
                    self.assertEqual(status["scored"], 5)
                    _, leaders = self.request(prefix + "results?" + urlencode({"job_id": job_id, "limit": 2}))
                    self.assertEqual(leaders["total"], 3)
                    self.assertEqual(len(leaders["rows"]), 2)
                    self.assertEqual(len({row["seed"] for row in leaders["rows"]}), 2)
                    self.assertTrue(all(row["score"] > 0 and row["route"] for row in leaders["rows"]))
                    self.assertTrue(all(row["baseline_copy"] == "A5Boss" for row in leaders["rows"]))
                    code, body = self.request(prefix + "download?" + urlencode({"job_id": job_id, "filename": "combined-leaderboard.csv"}), raw=True)
                    self.assertEqual(code, 200)
                    self.assertEqual(len(list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))), 3)
                    code, error = self.request(prefix + "download?" + urlencode({"job_id": job_id, "filename": "../native_search.cfg"}))
                    self.assertEqual(code, 400, error)
                    code, error = self.request(prefix + "results?" + urlencode({"job_id": job_id, "offset": "invalid"}))
                    self.assertEqual(code, 400, error)
                    code, error = self.request(prefix + "describe", {"source": "../L1.bspool"})
                    self.assertEqual(code, 400, error)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(2)

    def test_shutdown_blocks_new_scoring_requests_and_reopen_is_clean(self):
        service = web.score_service(str(self.root))
        web.begin_operation_shutdown()
        with self.assertRaisesRegex(web.organizer.PoolError, "closing"):
            web.score_service(str(self.root))
        with self.assertRaises(web.organizer.PoolError):
            service.start({"pools": [{"source": "L1.bspool", "second_tag": "A5B"}]})
        web.allow_active_operations()
        self.assertIsNot(service, web.score_service(str(self.root)))


if __name__ == "__main__":
    unittest.main()
