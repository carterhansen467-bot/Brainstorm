#!/usr/bin/env python3
"""Safety regression for completed-pool deletion in the Builder."""

import json
import os
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_builder as core
import pool_builder_web as web


def write_pool(path, complete=True, range_end=core.SEEDSPACE):
    text = "\n".join((
        "BRAINSTORM_SEED_POOL 2",
        "modelver 6",
        "charset 123456789ABCDEFGHIJKLMNPQRSTUVWXYZ",
        "seedspace %d" % core.SEEDSPACE,
        "space natural",
        "range_start 0",
        "range_end %d" % range_end,
        "catalog_hash 1111111111111111",
        "criteria_hash 2222222222222222",
        "pool_id fixture-delete",
        "legendary j_perkeo 1 small 1 small 0 charm",
        "records 1",
        "complete %d" % int(complete),
        "coverage_complete %d" % int(complete),
        "end",
        "",
    )).encode("ascii")
    with open(path, "wb") as handle:
        handle.write(text.ljust(1024, b"\0"))
        handle.write(b"record-data")


with tempfile.TemporaryDirectory(prefix="bs_pool_delete_") as pool_dir:
    name = "finished.bspool"
    path = os.path.join(pool_dir, name)
    write_pool(path)
    for suffix in (".state", ".manifest", ".criteria.cfg", ".attached"):
        with open(path + suffix, "w", encoding="utf-8") as handle:
            handle.write("sidecar %s\n" % suffix)
    unrelated = os.path.join(pool_dir, "keep.txt")
    with open(unrelated, "w", encoding="utf-8") as handle:
        handle.write("keep\n")

    plan = core.pool_delete_plan(name, pool_dir)
    assert [item["name"] for item in plan["files"]] == [
        name, name + ".state", name + ".manifest",
        name + ".criteria.cfg", name + ".attached",
    ]
    assert all(os.path.isabs(item["path"]) for item in plan["files"])
    complete_info = core.PoolInfo(path).as_dict()
    assert complete_info["attachment_base_eligible"]
    assert complete_info["attachment_blockers"] == []

    # A stale confirmation must never delete a newly changed/replaced pool.
    with open(path + ".manifest", "a", encoding="utf-8") as handle:
        handle.write("changed\n")
    try:
        core.delete_completed_pool(name, plan["token"], pool_dir)
    except ValueError as exc:
        assert "changed after confirmation" in str(exc)
    else:
        raise AssertionError("stale deletion token was accepted")
    assert os.path.exists(path)

    # A running job's source or destination is protected even with a fresh plan.
    plan = core.pool_delete_plan(name, pool_dir)
    for protected in (path, path + ".state"):
        try:
            core.pool_delete_plan(name, pool_dir, [protected])
        except ValueError as exc:
            assert "active Builder input or output" in str(exc)
        else:
            raise AssertionError("active pool artifact was deletable")

    class ActiveRunner:
        output = os.path.join(pool_dir, "active-output.bspool")
        input_pool = path
        inputs = ()

        @staticmethod
        def done():
            return False

    web.JOB["runner"] = ActiveRunner()
    try:
        try:
            web.plan_pool_deletion(name, pool_dir)
        except ValueError as exc:
            assert "active Builder input or output" in str(exc)
        else:
            raise AssertionError("web deletion ignored its active refilter input")
    finally:
        web.JOB["runner"] = None

    assert web.organizer_web.SPLIT_LOCK.acquire(False)
    try:
        try:
            web.plan_pool_deletion(name, pool_dir)
        except ValueError as exc:
            assert "organizer split" in str(exc)
        else:
            raise AssertionError("web deletion raced an organizer split")
    finally:
        web.organizer_web.SPLIT_LOCK.release()

    result = core.delete_completed_pool(name, plan["token"], pool_dir)
    assert set(result["removed"]) == {item["name"] for item in plan["files"]}
    assert not any(os.path.lexists(item["path"]) for item in plan["files"])
    assert os.path.isfile(unrelated)

    incomplete = os.path.join(pool_dir, "paused.bspool")
    write_pool(incomplete, complete=False)
    try:
        core.pool_delete_plan("paused.bspool", pool_dir)
    except ValueError as exc:
        assert "Only completed" in str(exc)
    else:
        raise AssertionError("incomplete pool was deletable")

    limited = os.path.join(pool_dir, "limited.bspool")
    write_pool(limited, range_end=100_000_000)
    limited_info = core.PoolInfo(limited).as_dict()
    assert not limited_info["attachment_base_eligible"]
    assert "does not cover every natural-space rank" in " ".join(
        limited_info["attachment_blockers"])

    for unsafe in ("../outside.bspool", "nested/pool.bspool", "not-a-pool.txt"):
        try:
            core.pool_delete_plan(unsafe, pool_dir)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe pool name was accepted: %s" % unsafe)

    # Exercise the browser's actual two-request contract, including the hard
    # server-side explicit-confirmation gate.
    api_name = "api-finished.bspool"
    api_path = os.path.join(pool_dir, api_name)
    write_pool(api_path)
    with open(api_path + ".state", "w", encoding="utf-8") as handle:
        handle.write("done 1\n")
    old_pool_dir = web.Handler.pool_dir
    web.Handler.pool_dir = pool_dir
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    def post(route, value):
        body = json.dumps(value).encode("utf-8")
        request = Request(base + route, data=body, method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        api_plan = post("/api/delete-plan", {"name": api_name})
        assert api_plan["token"]
        refused = post("/api/delete", {"name": api_name,
                                        "token": api_plan["token"]})
        assert "explicit confirmation" in refused["error"]
        assert os.path.isfile(api_path)
        deleted = post("/api/delete", {"name": api_name,
                                        "token": api_plan["token"],
                                        "confirmed": True})
        assert api_name in deleted["removed"]
        assert not os.path.exists(api_path)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        web.Handler.pool_dir = old_pool_dir

# The browser contract is deliberately two-step and visibly destructive.
for marker in ("/api/delete-plan", 'parsed.path == "/api/delete"',
               "Delete completed pool…", "This cannot be undone.",
               "data-pool"):
    assert marker in web.PAGE or marker in open(web.__file__, encoding="utf-8").read()

print("pool builder completed-pool deletion: ok")
