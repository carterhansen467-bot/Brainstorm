#!/usr/bin/env python3
"""Focused builder regression for human Ante/blind/source criteria."""

import os
import sys

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
assert "legendary j_perkeo 1 big 3 boss 0 shop\n" in text
assert "tag tag_charm 2 big 4 small 2\n" in text
assert "soul_depth any\n" in text
assert core.tag_location_count(2, "big", 4, "small") == 4
assert "A1 Big through A3 Boss" in criteria.summary()

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
                   "rminphase", "rmaxphase", 'value="settable"'):
    assert element_id in web.PAGE

print("pool builder exact locations: ok")
