#!/usr/bin/env python3
"""Quick Estimate uses/caches/cleans the real adaptive publication path."""

import inspect
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_builder as core
import pool_builder_web as web


# One helper feeds the curses, browser, and headless estimate entry points.
# Its config must exercise the same adaptive writer as a real publication.
criteria = core.Criteria()
criteria.legendary = "j_perkeo"
text = core.estimate_criteria_text(criteria, 200_000)
assert "format binary\n" in text
assert "output_schema 4\n" in text
assert "count 200000\n" in text
assert "checkpoint 200000\n" in text
assert "format count\n" not in text
assert "estimate_criteria_text(self.crit)" in inspect.getsource(
    core.App._do_estimate)
assert "core.estimate_criteria_text(crit, sample)" in inspect.getsource(
    web.start_job)


# The completed file's measured variable density wins over the deliberately
# absurd legacy heuristic. The final-file delta contributes the fixed header.
manifest = {
    "scanned": "100",
    "matched": "10",
    "seedspace": "1000",
    "projected_full_matches": "100",
    "projected_compressed_bytes": "999999",
    "bytes_per_record": "5",
    "compressed_file_bytes": "1074",
}
projected, measured = core.estimate_projected_bytes(
    manifest, 100, legacy_bytes=999999)
assert measured
assert projected == 1524  # 1,024 fixed + 100 * 5 measured bytes

context = {
    "space": "natural",
    "space_size": 1000,
    "scope_count": 500,
    "scope_label": "test",
    "input_pool": "",
}
projection = web.estimate_projection(manifest, context)
assert projection["measured_bytes"]
assert projection["full_bytes"] == 1524
assert projection["selected_bytes"] == 1274
assert projection["full_matches"] == 100
assert projection["selected_matches"] == 50

legacy, measured = core.estimate_projected_bytes(
    {"matched": "10"}, 100, legacy_bytes=4321)
assert not measured and legacy == 4321
assert "Only ${fmt(matched)} matches appeared" in web.PAGE
assert "No matches appeared in this quick sample" in web.PAGE


class FakeProcess:
    stderr = ()

    @staticmethod
    def poll():
        return 0

    @staticmethod
    def send_signal(_signal):
        raise AssertionError("a completed fake process was signaled")


# Exercise concurrent ThreadingHTTPServer-style status polls. The first poll
# caches the manifest and removes the pool plus all four sidecars; later polls
# must receive exactly that cached result rather than rereading deleted files.
estimate_dir = tempfile.mkdtemp(prefix="bs_pool_est_test_")
output = os.path.join(estimate_dir, "estimate")
old_popen = core.subprocess.Popen
core.subprocess.Popen = lambda *args, **kwargs: FakeProcess()
try:
    runner = core.Runner(
        "unused-snapshot.cfg", text, output, temporary=True)
finally:
    core.subprocess.Popen = old_popen

for suffix in ("", ".state", ".writer.lock"):
    with open(output + suffix, "wb") as handle:
        handle.write(b"temporary\n")
with open(output + ".manifest", "w", encoding="utf-8") as handle:
    for key, value in manifest.items():
        handle.write("%s %s\n" % (key, value))
    handle.write("seeds_per_second 500\nend\n")

runner.scanned = runner.total = 100
runner.matched = 10
runner.rate = 500
runner.started_at = time.monotonic() - 2.0
web.JOB.update(
    runner=runner,
    kind="estimate",
    started=time.time() - 2.0,
    summary="test",
    error="",
    closing=False,
    estimate_context=context,
)

responses = [None] * 12
barrier = threading.Barrier(len(responses))


def poll(index):
    barrier.wait()
    responses[index] = web.job_state()


threads = [threading.Thread(target=poll, args=(index,))
           for index in range(len(responses))]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join(timeout=5)
assert all(not thread.is_alive() for thread in threads)
assert all(response["manifest"] == responses[0]["manifest"]
           for response in responses)
assert all(response["estimate_projection"] == projection
           for response in responses)
assert float(responses[0]["manifest"]["pipeline_seeds_per_second"]) > 0
assert runner.result_manifest(cleanup=True) == responses[0]["manifest"]
assert not os.path.exists(estimate_dir)
for suffix in core.ESTIMATE_OUTPUT_SUFFIXES:
    assert not os.path.lexists(output + suffix)

web.JOB.update(
    runner=None,
    kind=None,
    started=0.0,
    summary="",
    error="",
    closing=False,
    estimate_context=None,
)

print("pool builder estimate pipeline: ok")
