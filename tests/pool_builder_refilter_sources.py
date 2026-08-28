#!/usr/bin/env python3
"""Focused Builder refilter-source compatibility and path regressions."""

import os
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_builder as core
import pool_builder_web as web


CATALOG = "1111111111111111"


def write_pool(directory, name, schema=3, encoding=None, records=4,
               model=core.MODEL_VERSION, catalog=CATALOG, complete=True,
               paused=False):
    if encoding is None:
        encoding = core.POOL_ENCODINGS[schema]
    header_bytes = 8192 if schema in core.POOL_EXTENDED_HEADER_SCHEMAS else 1024
    lines = [
        "BRAINSTORM_SEED_POOL %d" % schema,
        "modelver %d" % model,
        "encoding %s" % encoding,
        "header_bytes %d" % header_bytes,
        "charset 123456789ABCDEFGHIJKLMNPQRSTUVWXYZ",
        "seedspace %d" % core.SEEDSPACE,
        "space natural",
        "range_start 0",
        "range_end %d" % core.SEEDSPACE,
        "catalog_hash %s" % catalog,
        "criteria_hash 2222222222222222",
        "pool_id fixture-%s" % name,
        "records %d" % records,
        "complete %d" % int(complete),
        "coverage_complete %d" % int(complete),
        "end",
        "",
    ]
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write("\n".join(lines).encode("ascii").ljust(
            header_bytes, b"\0"))
    if paused:
        with open(path + ".state", "w", encoding="ascii") as handle:
            handle.write("done 0\n")
    return path


def compatibility_matrix():
    with tempfile.TemporaryDirectory(
            prefix="bs-refilter-sources-") as directory:
        write_pool(directory, "valid-v3.BSPOOL")
        write_pool(directory, "paused.bspool", records=2,
                   complete=False, paused=True)
        write_pool(directory, "old-model.bspool", model=5)
        write_pool(directory, "foreign-profile.bspool",
                   catalog="aaaaaaaaaaaaaaaa")
        write_pool(directory, "empty.bspool", records=0)
        write_pool(directory, "wrong-v3-encoding.bspool",
                   encoding="adaptive-events-v1")
        write_pool(directory, "missing-v2-encoding.bspool", schema=2,
                   encoding="")
        with open(os.path.join(directory, "damaged.bspool"), "wb") as handle:
            handle.write(b"not a seed pool")

        pools, _groups = core.read_pool_library(directory, CATALOG)
        by_name = {pool["name"]: pool for pool in pools}
        assert "valid-v3.BSPOOL" in by_name
        assert by_name["valid-v3.BSPOOL"]["refilter_eligible"]
        assert by_name["valid-v3.BSPOOL"]["schema"] == 3
        assert by_name["paused.bspool"]["status"] == "paused"
        assert by_name["paused.bspool"]["refilter_eligible"]

        assert not by_name["old-model.bspool"]["refilter_eligible"]
        assert "route model 5" in " ".join(
            by_name["old-model.bspool"]["refilter_blockers"])
        assert not by_name["foreign-profile.bspool"]["refilter_eligible"]
        assert "different profile/catalog" in " ".join(
            by_name["foreign-profile.bspool"]["refilter_blockers"])
        assert not by_name["empty.bspool"]["refilter_eligible"]
        assert "no committed seed records" in " ".join(
            by_name["empty.bspool"]["refilter_blockers"])
        assert not by_name["wrong-v3-encoding.bspool"]["refilter_eligible"]
        assert "incompatible encoding" in " ".join(
            by_name["wrong-v3-encoding.bspool"]["refilter_blockers"])
        assert not by_name["missing-v2-encoding.bspool"]["refilter_eligible"]
        assert "requires encoding delta-varint-blocks-v1" in " ".join(
            by_name["missing-v2-encoding.bspool"]["refilter_blockers"])
        assert not by_name["damaged.bspool"]["refilter_eligible"]
        assert "schema (missing) is not supported" in " ".join(
            by_name["damaged.bspool"]["refilter_blockers"])

        assert "p.refilter_eligible === true" in web.PAGE
        assert '" disabled"' in web.PAGE
        assert "unavailable:" in web.PAGE


def custom_pool_directory_start(snapshot_path):
    snapshot = core.Snapshot(snapshot_path)
    catalog = core.catalog_hash_file(snapshot.current_model_copy())
    with tempfile.TemporaryDirectory(
            prefix="bs-refilter-custom-") as directory, \
            tempfile.TemporaryDirectory(
                prefix="bs-refilter-decoy-") as decoy:
        source = write_pool(
            directory, "CUSTOM.BSPOOL", catalog=catalog, records=3)
        calls = []

        class FakeRunner:
            def __init__(self, snapshot_path, criteria_text, output,
                         input_pool=None, temporary=False):
                self.output = output
                self.input_pool = input_pool
                self.temporary = temporary
                calls.append((snapshot_path, criteria_text, output, input_pool))

            def done(self):
                return False

        old_runner = core.Runner
        old_pool_dir = core.POOL_DIR
        old_jobs = web.JOBS
        core.Runner = FakeRunner
        core.POOL_DIR = decoy
        web.JOBS = web.BuilderJobLifecycle()
        try:
            web.start_job("build", {
                "legendary": snapshot.usable_legendaries()[0],
                "inputPool": "CUSTOM.BSPOOL",
                "name": "custom-output",
                "count": 1,
            }, snapshot, directory)
        finally:
            core.Runner = old_runner
            core.POOL_DIR = old_pool_dir
            web.JOBS = old_jobs

        assert len(calls) == 1
        _snapshot_path, _criteria, output, input_pool = calls[0]
        assert os.path.abspath(input_pool) == os.path.abspath(source)
        assert os.path.dirname(os.path.abspath(output)) == \
            os.path.abspath(directory)
        assert not output.startswith(os.path.abspath(decoy) + os.sep)

        original_normcase = core.os.path.normcase
        core.os.path.normcase = lambda value: original_normcase(value).lower()
        old_jobs = web.JOBS
        web.JOBS = web.BuilderJobLifecycle()
        try:
            try:
                web.start_job("build", {
                    "legendary": snapshot.usable_legendaries()[0],
                    "inputPool": "CUSTOM.BSPOOL",
                    "name": "custom",
                    "count": 1,
                }, snapshot, directory)
            except ValueError as exc:
                assert "cannot overwrite its own input" in str(exc)
            else:
                raise AssertionError(
                    "Windows case-only input/output alias was accepted")
        finally:
            core.os.path.normcase = original_normcase
            web.JOBS = old_jobs


if __name__ == "__main__":
    snapshot_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        ROOT, "native_search.cfg")
    compatibility_matrix()
    custom_pool_directory_start(snapshot_path)
    print("pool builder refilter sources: ok")
