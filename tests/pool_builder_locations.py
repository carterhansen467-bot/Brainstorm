#!/usr/bin/env python3
"""Focused builder regression for human Ante/blind/source criteria."""

import os
import inspect
import sys
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_builder as core
import pool_builder_web as web


class Snapshot:
    def usable_legendaries(self):
        return ["j_perkeo"]

    def usable_tags(self):
        return [("tag_charm", 1)]


criteria = web.criteria_from_json({
    "legendary": "j_perkeo",
    "legMin": 1,
    "legMinPhase": "big",
    "legMax": 3,
    "legMaxPhase": "boss",
    "legSource": "shop",
    "legSoulDepth": "any",
    "rules": [{
        "key": "tag_charm",
        "min": 2,
        "minPhase": "big",
        "max": 4,
        "maxPhase": "small",
        "count": 2,
    }],
}, Snapshot())

text = criteria.text("binary", 1000)
assert "legendary_routes full\n" in text
assert "legendary j_perkeo 1 big 3 boss 0 shop\n" in text
assert "tag tag_charm 2 big 4 small 2\n" in text
assert "soul_depth any\n" in text
assert core.tag_location_count(2, "big", 4, "small") == 4
assert "A1 Big through A3 Boss" in criteria.summary()

# Fast Exact is a deliberate current-stage predicate, not a legacy RNG model.
# It is visibly named/warned and serialized so resume/merge identity cannot be
# confused with an exhaustive scan.
fast = web.criteria_from_json({
    "legendary": "j_perkeo",
    "legRoutes": "canonical_charm",
}, Snapshot())
assert fast.leg_routes == "canonical_charm"
assert "legendary_routes canonical_charm\n" in fast.text("binary", 1000)
assert "fast-no-omen" in fast.pool_name()
assert "automatic Omen-purchase recovery omitted" in fast.summary()
assert core.Criteria().leg_routes == "full"

# A quick estimate must not inherit the multi-minute 100M scan or the sparse
# 16.8M build checkpoint. It does publish a disposable BSP4 sample so measured
# speed and density cover the real adaptive writer pipeline.
quick = core.estimate_criteria_text(criteria)
assert "count 2000000\n" in quick
assert "checkpoint 262144\n" in quick
assert "format binary\n" in quick
assert "output_schema 4\n" in quick
assert "checkpoint 16777216\n" in criteria.text("binary", 100_000_000)

# The middle seed-space option covers every seed vanilla can preserve while
# excluding 0, which vanilla silently remaps to O.
settable = web.criteria_from_json({
    "legendary": "j_perkeo",
    "space": "settable",
}, Snapshot())
assert settable.seedspace() == core.SEEDSPACE_SETTABLE == 2318107019760
assert "space settable\n" in settable.text("binary", 0)
assert "settable" in settable.pool_name()
assert "no 0" in settable.summary()
assert settable.shard_bounds(0) == (0, core.SEEDSPACE_SETTABLE)

# Estimate projections retain the range that was actually requested.  The
# browser can be edited after completion without changing the old result, and
# "complete" always means the chosen space rather than implicitly natural.
natural_context = web.estimate_context(criteria, {"count": 100_000_000})
assert natural_context == {
    "space": "natural",
    "space_size": core.SEEDSPACE,
    "scope_count": 100_000_000,
    "scope_label": "First 100,000,000 seeds",
    "input_pool": "",
}
settable.shard_total, settable.shard_index = 4, 2
settable_context = web.estimate_context(settable, {"count": 0})
start, end = settable.shard_bounds(0)
assert settable_context["space"] == "settable"
assert settable_context["space_size"] == core.SEEDSPACE_SETTABLE
assert settable_context["scope_count"] == end - start
assert settable_context["scope_label"] == "Entire chosen seed space — part 2 of 4"
pool_context = web.estimate_context(
    settable, {"count": 100_000_000}, "paused.bspool", 12_345)
assert pool_context["scope_count"] == 12_345
assert pool_context["input_pool"] == "paused.bspool"

# Existing callers/tests that still construct four-field tag rows retain the
# full Small-through-Big window and the compact legacy-compatible line.
legacy = core.Criteria()
legacy.tag_rules.append(["tag_charm", 1, 8, 1])
assert "tag tag_charm 1 8 1\n" in legacy.text("binary", 1000)

for bad in (
    {"legendary": "j_perkeo", "legMin": 2, "legMinPhase": "boss",
     "legMax": 2, "legMaxPhase": "small"},
    {"legendary": "j_perkeo", "legMin": 39, "legMinPhase": "small",
     "legMax": 39, "legMaxPhase": "boss"},
):
    try:
        web.criteria_from_json(bad, Snapshot())
    except ValueError:
        pass
    else:
        raise AssertionError("invalid human route window was accepted")

for element_id in ("legMinPhase", "legMaxPhase", "legSource",
                   "rminphase", "rmaxphase", 'value="settable"',
                   'id="btnClose"', "/api/shutdown", "2M sample",
                   'id="scopeModeHint"', 'id="filterCard"', "Selected scope —",
                   "Complete chosen seed space —", 'id="activeFilters"',
                   'id="filterModeHint"', "Tags-only fast path",
                   'id="legRoutes"', 'value="canonical_charm"',
                   "Automatic Omen-purchase recovery is skipped"):
    assert element_id in web.PAGE

# A new builder starts without a silently active Legendary search.  The user
# must deliberately select one, and the active-filter panel then makes the
# expensive exact-route mode visible.
assert '$("legendary").value = ""' in web.PAGE
assert '$("legendary").value="j_perkeo"' not in web.PAGE
assert "j_perkeo" not in inspect.getsource(core.App.__init__)
assert core.Criteria().legendary == ""

# Regression: completing an estimate while Balatro's seed space is selected
# must not leave the space/scope controls disabled.  Their disabled state is
# derived only from inputPool, and changing that selector synchronizes them
# immediately rather than waiting for the one-second status poll.
sync_controls = web.PAGE.split("function syncSourceControls(){", 1)[1].split(
    "function selectedText", 1)[0]
assert 'const fromPool = !!$("inputPool").value' in sync_controls
assert '$(id).disabled = fromPool' in sync_controls
assert "running" not in sync_controls
assert '$("inputPool").addEventListener("change"' in web.PAGE
sync_filters = web.PAGE.split("function syncFilterControls(){", 1)[1].split(
    "function selectedText", 1)[0]
assert 'querySelectorAll("#filterCard select, #filterCard input")' in sync_filters
assert "control.disabled = false" in sync_filters
assert "running" not in sync_filters

# Starting the launcher twice should recognize and reopen its existing local
# server, and the page's Close Builder action should actually terminate it.
server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
base = "http://127.0.0.1:%d/" % server.server_address[1]
try:
    assert web.existing_builder_url(server.server_address[1]) == base
    request = Request(base + "api/shutdown", data=b"{}", method="POST")
    with urlopen(request, timeout=5) as response:
        assert response.status == 200
    thread.join(timeout=5)
    assert not thread.is_alive(), "Close Builder left the local server running"
finally:
    server.shutdown()
    server.server_close()
    web.JOB["closing"] = False

print("pool builder exact locations: ok")
