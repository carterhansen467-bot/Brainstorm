#!/usr/bin/env python3
"""Focused scalability and cancellation tests for web record exports."""

import errno
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.client import IncompleteRead
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import brainstorm_pool_organizer as organizer
import pool_builder_web as builder_web
import pool_organizer_web as web

FIXTURE_PATH = os.path.join(ROOT, "tests", "pool_organizer.py")
SPEC = importlib.util.spec_from_file_location(
    "pool_record_export_fixture", FIXTURE_PATH)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)


class RecordExportWebRegression(unittest.TestCase):
    def setUp(self):
        web.allow_active_operations()
        with web.READER_CACHE_LOCK:
            web.READER_CACHE.clear()
        self.temp = tempfile.TemporaryDirectory(
            prefix="brainstorm-record-export-web-")
        self.source_name = "record export source.bspool"
        self.source = os.path.join(self.temp.name, self.source_name)
        self.identity = fixture.write_bsp3(self.source, complete=True)

    def tearDown(self):
        web.cancel_operation("export")
        web.wait_for_active_operations()
        with web.READER_CACHE_LOCK:
            web.READER_CACHE.clear()
        self.temp.cleanup()

    def handlers(self):
        class UnifiedHandler(builder_web.Handler):
            pass
        UnifiedHandler.pool_dir = self.temp.name
        return (
            ("standalone", web.make_handler(self.temp.name),
             "/api/export", "/api/cancel"),
            ("builder", UnifiedHandler,
             "/organizer/api/export", "/organizer/api/cancel"),
        )

    @staticmethod
    def post_json(base, path, value):
        request = Request(
            base + path, data=json.dumps(value).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_compact_export_is_logically_equivalent(self):
        reader = web.verified_source_reader(
            self.source_name, self.temp.name)
        records = list(reader.iter_records())
        expected = [
            json.loads(json.dumps({
                "seed": reader.seed(record.rank),
                "rank": record.rank,
                "occurrences": [
                    item.as_dict() for item in record.occurrences],
            }))
            for record in records
        ]

        encoded = b"".join(web.iter_record_export(reader))
        actual = [
            json.loads(line) for line in encoded.decode("utf-8").splitlines()]
        self.assertEqual(actual, expected)
        first_expected = json.dumps(
            expected[0], sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(encoded.splitlines()[0], first_expected)
        self.assertNotIn(b'": ', encoded)

    def test_large_export_is_batched_near_one_mib(self):
        class ManyRecords:
            records = 35000

            @staticmethod
            def seed(rank):
                return "%08d" % rank

            def iter_records(self, cancel_check=None):
                for rank in range(self.records):
                    if cancel_check is not None and cancel_check():
                        raise web.OperationCancelled("operation cancelled")
                    yield organizer.Record(rank, tuple())

        chunks = list(web.iter_record_export(ManyRecords()))
        self.assertGreater(len(chunks), 1)
        self.assertLess(len(chunks), 10)
        self.assertEqual(
            sum(chunk.count(b"\n") for chunk in chunks),
            ManyRecords.records)
        self.assertTrue(all(
            len(chunk) <= web.RECORD_EXPORT_CHUNK_BYTES
            for chunk in chunks))
        for chunk in chunks[:-1]:
            self.assertGreater(
                len(chunk), web.RECORD_EXPORT_CHUNK_BYTES - 128)
        self.assertEqual(
            chunks[0].splitlines()[0],
            b'{"occurrences":[],"rank":0,"seed":"00000000"}')

    def test_inspection_projects_size_and_page_confirms_only_huge_exports(self):
        report = web.run_inspect(self.source_name, self.temp.name)
        projection = report["source"]["record_export"]
        expected = web.record_export_projection(
            report["source"]["records"],
            report["source"]["committed_data_bytes"])
        self.assertEqual(projection, expected)
        self.assertFalse(projection["huge"])

        huge_records = (
            web.RECORD_EXPORT_HUGE_BYTES
            // web.RECORD_EXPORT_BASE_BYTES + 1)
        huge = web.record_export_projection(huge_records, 0)
        self.assertTrue(huge["huge"])
        self.assertGreaterEqual(
            huge["estimated_bytes"], web.RECORD_EXPORT_HUGE_BYTES)
        self.assertIn("Huge export: about", web.PAGE)
        self.assertIn("info.huge&&!confirm", web.PAGE)
        self.assertIn('id="exportCancelBtn" hidden', web.PAGE)
        self.assertIn('id="recordExportFrame"', web.PAGE)
        self.assertIn("/api/export/status?request_id=", web.PAGE)
        self.assertNotIn(
            "location.href=apiPath(`/api/export", web.PAGE)

    def test_standalone_and_builder_share_batched_response_metadata(self):
        reader = web.verified_source_reader(
            self.source_name, self.temp.name)
        expected_values = [
            json.loads(json.dumps({
                "seed": reader.seed(record.rank),
                "rank": record.rank,
                "occurrences": [
                    item.as_dict() for item in record.occurrences],
            }))
            for record in reader.iter_records()
        ]
        projection = web.record_export_projection(
            reader.records, reader.data_bytes)
        bodies = []
        for index, (label, handler, export_path,
                    _cancel_path) in enumerate(self.handlers()):
            with self.subTest(handler=label):
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                thread = threading.Thread(
                    target=server.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%d" % server.server_address[1]
                try:
                    request_id = "successful-export-%d" % index
                    url = "%s%s?source=%s&snapshot=%s&request_id=%s" % (
                        base, export_path, quote(self.source_name),
                        self.identity["snapshot"], request_id)
                    with urlopen(url, timeout=10) as response:
                        body = response.read()
                        headers = response.headers
                    bodies.append(body)
                    self.assertIn(
                        "attachment", headers["Content-Disposition"])
                    self.assertEqual(
                        headers["X-Brainstorm-Record-Count"], "4")
                    self.assertEqual(
                        int(headers["X-Brainstorm-Estimated-Bytes"]),
                        projection["estimated_bytes"])
                    self.assertEqual(
                        headers["X-Brainstorm-Export-Warning"], "none")
                    self.assertIsNone(headers.get("Content-Length"))
                    self.assertEqual(
                        headers["Transfer-Encoding"].lower(), "chunked")
                    self.assertEqual(
                        [json.loads(line) for line in body.splitlines()],
                        expected_values)
                    self.assertEqual(
                        web.record_export_status(request_id)["state"],
                        "completed")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
        self.assertEqual(bodies[0], bodies[1])

    def test_http_export_cancellation_reaches_both_handlers(self):
        original_export = web.iter_record_export
        try:
            for index, (label, handler, export_path,
                        cancel_path) in enumerate(self.handlers()):
                entered = threading.Event()
                headers_received = threading.Event()
                cancellation_seen = threading.Event()
                responses = []
                incomplete = []
                unexpected = []

                def blocked_export(
                        _reader, cancel_check=None,
                        chunk_bytes=web.RECORD_EXPORT_CHUNK_BYTES):
                    del chunk_bytes
                    if not callable(cancel_check):
                        raise AssertionError(
                            "HTTP export did not propagate cancel_check")
                    entered.set()
                    yield b'{"rank":0}\n'
                    while not cancel_check():
                        time.sleep(0.005)
                    cancellation_seen.set()
                    raise web.OperationCancelled("operation cancelled")

                web.iter_record_export = blocked_export
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                server_thread = threading.Thread(
                    target=server.serve_forever, daemon=True)
                server_thread.start()
                base = "http://127.0.0.1:%d" % server.server_address[1]
                request_id = "cancelled-export-%d" % index
                url = "%s%s?source=%s&snapshot=%s&request_id=%s" % (
                    base, export_path, quote(self.source_name),
                    self.identity["snapshot"], request_id)

                def download():
                    try:
                        with urlopen(url, timeout=10) as response:
                            headers_received.set()
                            responses.append(response.read())
                    except IncompleteRead as exc:
                        incomplete.append(exc.partial)
                    except BaseException as exc:
                        unexpected.append(exc)

                download_thread = threading.Thread(target=download)
                download_thread.start()
                try:
                    self.assertTrue(
                        entered.wait(5), "%s export never started" % label)
                    self.assertTrue(
                        headers_received.wait(5),
                        "%s export never committed headers" % label)
                    cancelled = self.post_json(base, cancel_path, {
                        "operation": "export",
                    })
                    self.assertEqual(cancelled["state"], "cancelling")
                    self.assertTrue(
                        cancellation_seen.wait(5),
                        "%s export never observed cancellation" % label)
                    download_thread.join(timeout=5)
                    self.assertFalse(download_thread.is_alive())
                    self.assertEqual(unexpected, [])
                    self.assertEqual(responses, [])
                    self.assertEqual(incomplete, [b'{"rank":0}\n'])
                    self.assertEqual(
                        web.record_export_status(request_id)["state"],
                        "cancelled")
                    for _ in range(200):
                        if web.cancel_operation("export")["state"] == "idle":
                            break
                        time.sleep(0.005)
                    self.assertEqual(
                        web.cancel_operation("export")["state"], "idle")
                finally:
                    web.cancel_operation("export")
                    download_thread.join(timeout=5)
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=5)
        finally:
            web.iter_record_export = original_export

    def test_disconnect_closes_export_and_unregisters_operation(self):
        class OneRecordReader:
            snapshot_token = self.identity["snapshot"]
            records = 1
            data_bytes = 0

            @staticmethod
            def seed(_rank):
                return "11111111"

            @staticmethod
            def iter_records(cancel_check=None):
                if cancel_check is not None and cancel_check():
                    raise web.OperationCancelled("operation cancelled")
                yield organizer.Record(0, tuple())

        class BrokenWriter:
            @staticmethod
            def write(_value):
                raise BrokenPipeError(errno.EPIPE, "client disconnected")

        class MemoryHandler:
            def __init__(self):
                self.status = None
                self.headers = {}
                self.wfile = BrokenWriter()
                self.close_connection = False

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers[name] = value

            @staticmethod
            def end_headers():
                pass

        original_reader = web.verified_source_reader
        handler = MemoryHandler()
        web.verified_source_reader = (
            lambda _name, _pool_dir=None, cancel_check=None:
            OneRecordReader())
        try:
            result = web.serve_record_export(
                handler,
                urlparse("/api/export?source=fake.bspool&snapshot=%s"
                         % self.identity["snapshot"]),
                self.temp.name)
        finally:
            web.verified_source_reader = original_reader
        self.assertIsNone(result)
        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.close_connection)
        self.assertNotIn("Content-Length", handler.headers)
        self.assertEqual(handler.headers["Transfer-Encoding"], "chunked")
        self.assertEqual(
            web.cancel_operation("export")["state"], "idle")

    def test_preheader_failure_is_reported_without_page_navigation(self):
        for index, (label, handler, export_path,
                    _cancel_path) in enumerate(self.handlers()):
            with self.subTest(handler=label):
                server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                thread = threading.Thread(
                    target=server.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%d" % server.server_address[1]
                request_id = "failed-preflight-%d" % index
                url = ("%s%s?source=../outside.bspool&snapshot=x"
                       "&request_id=%s") % (
                           base, export_path, request_id)
                try:
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(url, timeout=10)
                    self.assertEqual(getattr(raised.exception, "code", None), 400)
                    status_path = (
                        "/api/export/status" if label == "standalone"
                        else "/organizer/api/export/status")
                    with urlopen(
                            "%s%s?request_id=%s" % (
                                base, status_path, request_id),
                            timeout=10) as response:
                        status = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(status["state"], "failed")
                    self.assertIn("choose a pool", status["error"])
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
