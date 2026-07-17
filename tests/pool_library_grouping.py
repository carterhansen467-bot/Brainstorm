#!/usr/bin/env python3
"""Focused regression coverage for the standalone builder's pool library."""

import json
import os
import sys
import tempfile


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_builder as core
import pool_builder_web as web


def write_pool(directory, name, fields, state_done=None):
    lines = ["BRAINSTORM_SEED_POOL 3", "header_bytes 2048"]
    lines.extend("%s %s" % item for item in fields.items())
    lines.append("end")
    data = ("\n".join(lines) + "\n").encode("ascii")
    assert len(data) < 2048
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(data)
        handle.write(b"\0" * (2048 - len(data)))
    if state_done is not None:
        with open(path + ".state", "w", encoding="ascii") as handle:
            handle.write("done %d\n" % state_done)
    return path


def main():
    with tempfile.TemporaryDirectory(prefix="bs-pool-library-") as directory:
        write_pool(directory, "root.bspool", {
            "family_id": "fam-a", "lineage_id": "line-root",
            "segment_id": "seg-root", "snapshot_id": "snap-current",
            "records": 20, "complete": 0, "coverage_complete": 0,
            "range_start": 0, "range_end": 100, "space": "natural",
            "label": "Root search", "tag": "tag_negative 1 4 1",
        }, state_done=0)
        write_pool(directory, "filtered.bspool", {
            "family_id": "fam-a", "lineage_id": "line-filter",
            "segment_id": "seg-filter", "parent_segment_id": "seg-root",
            "parent_snapshot_id": "snap-old", "parent_records": 12,
            "records": 3, "complete": 1, "coverage_complete": 0,
            "refilter_depth": 1, "space": "natural",
        })
        # Same segment text in another family must never be treated as parent.
        write_pool(directory, "wrong-family.bspool", {
            "family_id": "fam-b", "lineage_id": "line-wrong",
            "segment_id": "seg-root", "records": 99,
            "complete": 1, "coverage_complete": 1,
        })
        write_pool(directory, "legacy.bspool", {
            "pool_id": "old-pool", "records": 5,
            "complete": 1, "coverage_complete": 1,
        })

        pools, groups = core.read_pool_library(directory)
        by_name = {pool["name"]: pool for pool in pools}
        root = by_name["root.bspool"]
        child = by_name["filtered.bspool"]
        legacy = by_name["legacy.bspool"]

        assert root["status"] == "paused" and root["resumable"]
        assert child["status"] == "provisional"
        assert child["parent_name"] == "root.bspool"
        assert child["parent_current_records"] == 20
        assert child["update_available"] and child["new_records"] == 8
        assert legacy["legacy"] and legacy["status"] == "complete"

        family_a = next(group for group in groups if group["family_id"] == "fam-a")
        assert len(family_a["lineages"]) == 2
        assert {lineage["lineage_id"] for lineage in family_a["lineages"]} == {
            "line-root", "line-filter",
        }
        legacy_group = next(group for group in groups if group["legacy"])
        assert legacy_group["lineages"][0]["pools"][0]["name"] == "legacy.bspool"
        json.dumps({"pools": pools, "pool_groups": groups})

        old_dir = core.POOL_DIR
        try:
            core.POOL_DIR = directory
            web_pools, web_groups = web.pool_library()
        finally:
            core.POOL_DIR = old_dir
        assert len(web_pools) == 4 and len(web_groups) == 3
        assert "<optgroup" in web.PAGE
        assert "document.activeElement === sel" in web.PAGE

    print("pool library grouping: ok")


if __name__ == "__main__":
    main()
