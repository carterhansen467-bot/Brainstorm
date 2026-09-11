#!/usr/bin/env python3
"""Classify recorded tag placements without replaying or executing recipes.

Tag metadata is query evidence: a missing occurrence proves absence only inside
that tag's recorded search windows.  A composite record can use the union of
windows from its own provenance branches and explicit per-record recording
markers, never another seed's branches or markers.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

try:
    import brainstorm_pool_organizer as organizer
except ImportError:
    from tools import brainstorm_pool_organizer as organizer


RECIPE_VERSION = 1
MAX_ANTE = 39
MAX_RECIPE_BYTES = 65536
EVIDENCE_CACHE_ENTRIES = 4096
EVIDENCE_CACHE_BYTES = 8 * 1024 * 1024
EVIDENCE_CACHE_DESCRIPTORS = 256
TAG_KEYS = {"negative": "tag_negative", "rare": "tag_rare"}
TAG_RECORDING_SIGNATURE = b"\x82BSTAG"
_TAG_NAMES = {key: tag for tag, key in TAG_KEYS.items()}
EXCLUSION_LABELS = {
    "condition_not_met": "Does not meet the extra conditions",
    "no_eligible_tags": "No Negative or Rare tag in the selected range",
    "missing_opposite_tag": "No later tag of the opposite type",
    "same_ante_only": "Both types occur in the first Ante, with no later tag",
}


class TagRuleError(organizer.PoolError):
    """A recipe or its placement evidence cannot be interpreted safely."""


class InsufficientMetadataError(TagRuleError):
    """The pool did not record every location needed to apply the recipe."""

    def __init__(self, missing, rank=None):
        self.missing = tuple(missing)
        self.rank = rank
        detail = "; ".join("%s %s–%s" % (
            item["tag"].title(), item["range"]["start"],
            item["range"]["end"]) for item in self.missing)
        subject = "This pool" if rank is None else "Seed rank %d" % rank
        super().__init__(
            "%s lacks recorded coverage for %s. Use Record tag placements, "
            "then preview again, or choose a range already recorded. Missing "
            "metadata cannot be treated as a missing tag." % (subject, detail))


def _object(value, allowed, required, label):
    if not isinstance(value, dict):
        raise TagRuleError("%s must be an object" % label)
    if set(value) - set(allowed):
        raise TagRuleError("%s contains unknown fields" % label)
    if set(required) - set(value):
        raise TagRuleError("%s is missing required fields" % label)


def _integer(value, label, minimum=0, maximum=MAX_ANTE * 2):
    if (isinstance(value, bool) or not isinstance(value, int)
            or not minimum <= value <= maximum):
        raise TagRuleError("%s must be an integer from %d to %d" %
                           (label, minimum, maximum))
    return value


def parse_position(value) -> int:
    """Return the zero-based physical tag slot for an A1S/A1B location."""
    if not isinstance(value, str):
        raise TagRuleError("Tag locations must use A1S or A1B notation")
    match = re.fullmatch(r"A([1-9][0-9]?)([SB])", value.strip().upper())
    if not match or int(match[1]) > MAX_ANTE:
        raise TagRuleError("Tag locations must be A1S through A%dB" % MAX_ANTE)
    return (int(match[1]) - 1) * 2 + (match[2] == "B")


def position_token(position: int) -> str:
    return "A%d%s" % (position // 2 + 1, "B" if position % 2 else "S")


def _normalize_range(value):
    _object(value, ("start", "end"), ("start", "end"), "Tag range")
    start, end = parse_position(value["start"]), parse_position(value["end"])
    if end < start:
        raise TagRuleError("The end of a tag range must follow its start")
    return {"start": position_token(start), "end": position_token(end)}


def _range_pair(value):
    return parse_position(value["start"]), parse_position(value["end"])


def _range_dict(pair):
    return {"start": position_token(pair[0]), "end": position_token(pair[1])}


def _normalize_condition(value, depth=0, budget=None):
    if budget is None:
        budget = [256]
    budget[0] -= 1
    if depth > 12 or budget[0] < 0:
        raise TagRuleError("Extra conditions are too large or too deeply nested")
    if not isinstance(value, dict) or len(value) != 1:
        raise TagRuleError("A condition must contain one of all, any, not, or count")
    kind, child = next(iter(value.items()))
    if kind in ("all", "any"):
        if not isinstance(child, list) or not 1 <= len(child) <= 32:
            raise TagRuleError("all/any needs between 1 and 32 conditions")
        return {kind: [_normalize_condition(item, depth + 1, budget)
                       for item in child]}
    if kind == "not":
        return {kind: _normalize_condition(child, depth + 1, budget)}
    if kind != "count":
        raise TagRuleError("Unknown condition; use all, any, not, or count")
    _object(child, ("tag", "range", "min", "max"), ("tag", "range"),
            "Tag count")
    if not isinstance(child["tag"], str) or child["tag"] not in TAG_KEYS:
        raise TagRuleError("Count tags must be negative or rare")
    if not {"min", "max"}.intersection(child):
        raise TagRuleError("Tag count needs a minimum or maximum")
    result = {"tag": child["tag"], "range": _normalize_range(child["range"])}
    for bound in ("min", "max"):
        if bound in child:
            result[bound] = _integer(child[bound], "Tag count " + bound)
    if result.get("min", 0) > result.get("max", MAX_ANTE * 2):
        raise TagRuleError("Tag count maximum must be at least its minimum")
    return {kind: result}


def normalize_recipe(value) -> Dict[str, object]:
    """Validate the bounded data-only v1 second-tag recipe; return a fresh copy."""
    _object(value, ("version", "name", "range", "condition"),
            ("version", "range"), "Tag recipe")
    if type(value["version"]) is not int or value["version"] != RECIPE_VERSION:
        raise TagRuleError("Unsupported tag recipe version; expected version 1")
    result = {"version": RECIPE_VERSION, "range": _normalize_range(value["range"])}
    if "name" in value:
        name = value["name"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 120
                or any(ord(char) < 32 for char in name)):
            raise TagRuleError("Recipe names must contain 1–120 printable characters")
        result["name"] = name.strip()
    if "condition" in value:
        result["condition"] = _normalize_condition(value["condition"])
    return result


def _unique_json_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TagRuleError("Recipe JSON repeats the field %s" % key)
        result[key] = value
    return result


def loads_recipe(text) -> Dict[str, object]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_RECIPE_BYTES:
        raise TagRuleError("Recipe JSON must be at most 64 KiB")
    try:
        value = json.loads(text, object_pairs_hook=_unique_json_fields,
                           parse_constant=lambda value: (_ for _ in ()).throw(
                               TagRuleError("Recipe JSON contains " + value)))
    except (ValueError, TypeError, RecursionError) as error:
        raise TagRuleError("Invalid recipe JSON: %s" % error) from error
    return normalize_recipe(value)


def load_recipe(path) -> Dict[str, object]:
    with open(path, "rb") as handle:
        data = handle.read(MAX_RECIPE_BYTES + 1)
    if len(data) > MAX_RECIPE_BYTES:
        raise TagRuleError("Recipe JSON must be at most 64 KiB")
    try:
        return loads_recipe(data.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise TagRuleError("Recipe JSON must use UTF-8") from error


def save_recipe(path, recipe) -> Dict[str, object]:
    """Atomically save validated recipe data; no pool is touched."""
    value = normalize_recipe(recipe)
    payload = (json.dumps(value, ensure_ascii=True, indent=2) + "\n").encode("utf-8")
    if len(payload) > MAX_RECIPE_BYTES:
        raise TagRuleError("Recipe JSON must be at most 64 KiB")
    destination = os.path.abspath(os.fspath(path))
    descriptor, temporary = tempfile.mkstemp(
        prefix=".tag-recipe-", dir=os.path.dirname(destination))
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return value


def _merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def _coverage_from_criteria(criteria):
    result = {tag: [] for tag in TAG_KEYS}
    for line in criteria:
        parts = line.split()
        if not parts or parts[0] not in ("tag", "route_tag"):
            continue
        kind = parts.pop(0)
        if kind == "route_tag":
            if not parts or parts.pop(0) not in ("collect", "observe"):
                raise TagRuleError("Recorded inherited tag rule has an invalid route")
        if len(parts) == 4:
            key, first, last, count = parts
            first_phase, last_phase = "small", "big"
        elif len(parts) == 6:
            key, first, first_phase, last, last_phase, count = parts
        else:
            raise TagRuleError("Recorded tag rule has an invalid number of fields")
        if first_phase not in ("small", "big") or last_phase not in ("small", "big"):
            raise TagRuleError("Recorded tag rules must use Small or Big blinds")
        if not all(re.fullmatch(r"[0-9]+", token) for token in (first, last, count)):
            raise TagRuleError("Recorded tag rule contains an invalid number")
        interval = _range_pair(_normalize_range({
            "start": "A%s%s" % (first, first_phase[0]),
            "end": "A%s%s" % (last, last_phase[0])}))
        if not 1 <= int(count) <= interval[1] - interval[0] + 1:
            raise TagRuleError("Recorded tag rule has an impossible minimum count")
        for tag, tag_key in TAG_KEYS.items():
            if tag_key == key:
                result[tag].append(interval)
    return {tag: _merge_intervals(intervals) for tag, intervals in result.items()}


def _condition_requirements(condition):
    if not condition:
        return []
    kind, child = next(iter(condition.items()))
    if kind == "count":
        return [(child["tag"], _range_pair(child["range"]))]
    if kind == "not":
        return _condition_requirements(child)
    return [requirement for item in child for requirement in _condition_requirements(item)]


def _missing_coverage(requirements, coverage):
    missing = []
    for tag, (start, end) in requirements:
        cursor = start
        for covered_start, covered_end in coverage[tag]:
            if covered_end < cursor:
                continue
            if covered_start > end:
                break
            if covered_start > cursor:
                missing.append({"tag": tag, "range": _range_dict(
                    (cursor, min(end, covered_start - 1)))})
            cursor = max(cursor, covered_end + 1)
            if cursor > end:
                break
        if cursor <= end:
            missing.append({"tag": tag, "range": _range_dict((cursor, end))})
    return missing


def _compile_condition(condition):
    """Parse location strings once, outside the record-processing loop."""
    if not condition:
        return None
    kind, child = next(iter(condition.items()))
    if kind == "count":
        start, end = _range_pair(child["range"])
        return kind, (child["tag"], start, end,
                      child.get("min", 0), child.get("max", MAX_ANTE * 2))
    if kind == "not":
        return kind, _compile_condition(child)
    return kind, tuple(_compile_condition(item) for item in child)


def _condition_matches(condition, placements):
    if condition is None:
        return True
    kind, child = condition
    if kind == "all":
        return all(_condition_matches(item, placements) for item in child)
    if kind == "any":
        return any(_condition_matches(item, placements) for item in child)
    if kind == "not":
        return not _condition_matches(child, placements)
    target, start, end, minimum, maximum = child
    count = sum(1 for position, tag in placements
                if start <= position <= end and tag == target)
    return minimum <= count <= maximum


@dataclass(frozen=True)
class Destination:
    key: str
    label: str
    tag: str
    ante: int
    phase: str

    def as_dict(self):
        return {"key": self.key, "label": self.label, "tag": self.tag,
                "ante": self.ante, "phase": self.phase}


@dataclass(frozen=True)
class Classification:
    destinations: Tuple[Destination, ...] = ()
    exclusion: Optional[str] = None

    @property
    def destination(self):
        return self.destinations[0] if self.destinations else None

    def as_dict(self):
        return {"destinations": [item.as_dict() for item in self.destinations],
                "exclusion": self.exclusion,
                "exclusion_label": EXCLUSION_LABELS.get(self.exclusion)}


def _descriptor_info(item, rank):
    """Return physical tag/source data independently of descriptor attributes."""
    if item.raw.startswith(TAG_RECORDING_SIGNATURE):
        if (len(item.raw) != 9 or item.raw[6] != 1
                or not 0 <= item.raw[7] <= item.raw[8] < MAX_ANTE * 2):
            raise TagRuleError("Seed rank %d has an invalid tag recording marker" % rank)
        return None, None, None, None, None, (item.raw[7], item.raw[8])
    if item.kind == 1:
        if (type(item.ante) is not int or not 1 <= item.ante <= MAX_ANTE
                or type(item.phase) is not int or item.phase not in (1, 2)):
            raise TagRuleError("Seed rank %d has an invalid tag location" % rank)
        position = (item.ante - 1) * 2 + item.phase - 1
        return position, item.key, _TAG_NAMES.get(item.key), None, None, None
    return None, None, None, item.provenance_id, item.operand_id, None


def _read_sources(reader):
    sources, labels = {}, {}
    if reader.is_composite:
        for branch_id, branch in reader.composite_branches.items():
            sources[branch_id] = _coverage_from_criteria(branch.criteria)
            labels[branch_id] = branch.label or branch.pool_id or branch.token
        if not sources:
            raise TagRuleError("The combined pool has no source coverage records")
    else:
        criteria = [raw for key, _value, raw in reader.header.lines
                    if key in organizer.CRITERIA_DIRECTIVES]
        sources[None] = _coverage_from_criteria(criteria)
        labels[None] = "Selected pool"
    return sources, labels


def _describe_sources(sources, labels):
    result = []
    for source_id, coverage in sources.items():
        common = _merge_intervals(
            (max(a, c), min(b, d))
            for a, b in coverage["negative"] for c, d in coverage["rare"]
            if max(a, c) <= min(b, d))
        result.append({
            "source_id": None if source_id is None else "%016x" % source_id,
            "label": labels[source_id],
            "tags": {tag: [_range_dict(pair) for pair in intervals]
                     for tag, intervals in coverage.items()},
            "both_tags": [_range_dict(pair) for pair in common],
        })
    return result


def describe_recorded_coverage(reader):
    """Show recorded windows before the user has chosen a valid recipe.

    For legacy/mixed pools, metadata_complete=False means the declared windows
    are insufficient to establish occurrence coverage.  Composite windows are
    source-specific; eligibility must still be checked against each record.
    Per-record recording markers are intentionally not projected into these
    header windows: neither a source label nor a recording header proves that
    every seed contains the same markers.
    """
    sources, labels = _read_sources(reader)
    return {"metadata_complete": bool(reader.occurrence_metadata_complete),
            "checked_per_seed": True,
            "sources": _describe_sources(sources, labels)}


class TagClassifier:
    """Compile once per pool, then classify its immutable records in a stream."""

    def __init__(self, reader, recipe):
        self._recipe = normalize_recipe(recipe)
        self._range = _range_pair(self._recipe["range"])
        self._condition = _compile_condition(self._recipe.get("condition"))
        requirements = [(tag, self._range) for tag in TAG_KEYS]
        requirements.extend(_condition_requirements(self._recipe.get("condition")))
        self._requirements = tuple((tag, pair) for tag in TAG_KEYS for pair in
                                   _merge_intervals(interval for name, interval in
                                                    requirements if name == tag))
        if not reader.occurrence_metadata_complete:
            raise TagRuleError(
                "This pool contains seeds without complete recorded occurrences. "
                "Use an event pool (BSP3/BSP4) whose inputs all recorded metadata, "
                "or rebuild/refilter the original sources before applying tag rules.")
        self._is_composite = reader.is_composite
        self._sources, self._labels = _read_sources(reader)
        self._cache = {}
        self._descriptor_cache = {}
        self._descriptor_cache_bytes = 0
        self._results = {}
        self._evidence_cache = {}
        self._evidence_cache_bytes = 0

    @property
    def recipe(self):
        return copy.deepcopy(self._recipe)

    def describe_coverage(self):
        return {"sources": _describe_sources(self._sources, self._labels),
                "metadata_complete": True, "checked_per_seed": True,
                "required": [{"tag": tag, "range": _range_dict(pair)}
                             for tag, pair in self._requirements]}

    def _record_evidence(self, record):
        by_position = {}
        source_ids = set()
        recorded_windows = set()
        for item in record.occurrences:
            info = self._descriptor_cache.get(item.raw)
            if info is None:
                info = _descriptor_info(item, record.rank)
                charge = len(item.raw) + 256
                if (len(self._descriptor_cache) < 8192
                        and self._descriptor_cache_bytes + charge <= EVIDENCE_CACHE_BYTES):
                    self._descriptor_cache[item.raw] = info
                    self._descriptor_cache_bytes += charge
            position, key, tag, source_id, operand_id, recorded_window = info
            if position is not None:
                prior = by_position.get(position)
                if prior is not None and prior[0] != key:
                    raise TagRuleError("Seed rank %d records conflicting tags at %s" %
                                       (record.rank, position_token(position)))
                by_position[position] = (key, tag)
            elif source_id is not None:
                source_ids.add(source_id)
            elif operand_id is not None and not self._is_composite:
                raise TagRuleError("A non-composite pool contains input provenance")
            elif recorded_window is not None:
                recorded_windows.add(recorded_window)
        self._check_record_coverage(record, frozenset(source_ids),
                                    _merge_intervals(recorded_windows))
        return tuple((position, value[1]) for position, value in sorted(by_position.items())
                     if value[1] is not None)

    def _check_record_coverage(self, record, ids, recorded_windows):
        if not self._is_composite:
            if ids:
                raise TagRuleError("A non-composite pool contains source provenance")
            source_ids = (None,)
        elif not ids or ids.difference(self._sources):
            raise TagRuleError("Seed rank %d has missing or unknown source provenance" %
                               record.rank)
        else:
            source_ids = ids
        # Markers prove both target tags within exactly this record's windows.
        # Their intervals are part of the bounded cache identity, so an earlier
        # seed cannot lend coverage to one whose metadata omitted a marker.
        cache_key = (ids, recorded_windows)
        if cache_key in self._cache:
            missing = self._cache[cache_key]
        else:
            coverage = {tag: _merge_intervals(recorded_windows + tuple(
                interval for source_id in source_ids for interval in self._sources[source_id][tag]))
                for tag in TAG_KEYS}
            missing = _missing_coverage(self._requirements, coverage)
            # Bounded memory even if a large combined pool has many memberships.
            if len(self._cache) < 4096:
                self._cache[cache_key] = missing
        if missing:
            raise InsufficientMetadataError(missing, record.rank)

    def classify(self, record) -> Classification:
        # Outcomes depend on complete immutable evidence, not on seed rank.
        # Retain all descriptor bytes in the key, including provenance and
        # unrelated tags: dropping either could hide missing coverage or a
        # contradictory physical slot. Failed validation is never cached, so
        # every error continues to identify the current seed's rank.
        key = None
        if len(record.occurrences) <= EVIDENCE_CACHE_DESCRIPTORS:
            key = tuple(item.raw for item in record.occurrences)
            cached = self._evidence_cache.get(key)
            if cached is not None:
                return cached
        result = self._classify_evidence(record)
        if key is not None and len(self._evidence_cache) < EVIDENCE_CACHE_ENTRIES:
            # Charge tuple/pointer/dictionary overhead and owned descriptor
            # bytes conservatively, even when other cached keys share bytes.
            charge = 192 + len(key) * 8 + sum(33 + len(raw) for raw in key)
            if self._evidence_cache_bytes + charge <= EVIDENCE_CACHE_BYTES:
                self._evidence_cache[key] = result
                self._evidence_cache_bytes += charge
        return result

    def _classify_evidence(self, record) -> Classification:
        placements = self._record_evidence(record)
        if not _condition_matches(self._condition, placements):
            return _EXCLUDED["condition_not_met"]
        start, end = self._range
        eligible = [(position, tag) for position, tag in placements
                    if start <= position <= end]
        if not eligible:
            return _EXCLUDED["no_eligible_tags"]
        first_ante = eligible[0][0] // 2
        first_types = {tag for position, tag in eligible if position // 2 == first_ante}
        both_first = len(first_types) == 2
        for position, tag in eligible:
            if position // 2 <= first_ante or (not both_first and tag in first_types):
                continue
            cached = self._results.get((position, tag))
            if cached is not None:
                return cached
            ante = position // 2 + 1
            phase = "big" if position % 2 else "small"
            destination = Destination(
                "%s-%s" % (position_token(position).lower(), tag),
                "A%d %s %s" % (ante, phase.title(), tag.title()), tag, ante, phase)
            result = Classification((destination,))
            self._results[position, tag] = result
            return result
        return _EXCLUDED["same_ante_only" if both_first else "missing_opposite_tag"]


_EXCLUDED = {reason: Classification(exclusion=reason) for reason in EXCLUSION_LABELS}
