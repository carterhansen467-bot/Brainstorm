#!/usr/bin/env python3
"""Pure seed-distribution policy shared by Organizer adapters.

The module deliberately knows nothing about paths, locks, staging files, or
HTTP.  It turns one immutable split specification plus a record stream into an
immutable reviewed plan, and reapplies the same specification during trusted
publication.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import (Callable, Dict, Iterable, Mapping, Optional, Sequence, Set,
                    Tuple)


MODE_EXCLUSIVE = "exclusive"
MODE_MATCHING_COPIES = "matching_copies"
ASSIGNMENT_MODES = (MODE_EXCLUSIVE, MODE_MATCHING_COPIES)

FNV64_OFFSET = 1469598103934665603
FNV64_PRIME = 1099511628211
MASK64 = (1 << 64) - 1


class SplitPolicyError(ValueError):
    """The requested distribution cannot be reviewed or applied safely."""


def _fnv64(data: bytes, value: int = FNV64_OFFSET) -> int:
    for byte in data:
        value = ((value ^ byte) * FNV64_PRIME) & MASK64
    return value


def ambiguity_rule_key(candidates: Iterable[str]) -> str:
    """Return the stable legacy token for one exact destination set."""
    values = tuple(sorted(candidates))
    payload = "\0".join(values).encode("utf-8")
    return "%016x" % _fnv64(b"ambiguity-rule\0" + payload)


def reviewed_plan_identity(value: object) -> str:
    """Return a stable, non-secret identity for reviewed plan inputs."""
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "%016x" % _fnv64(
        b"organizer-plan:split:" + payload.encode("ascii"))


def normalize_mode(value: object, default: str = MODE_EXCLUSIVE) -> str:
    mode = str(value or default).strip().lower()
    if mode not in ASSIGNMENT_MODES:
        raise SplitPolicyError("unknown seed assignment mode")
    return mode


def _checked_pairs(value: Optional[Mapping[str, str]], noun: str
                   ) -> Tuple[Tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise SplitPolicyError("%s must be an object" % noun)
    checked = []
    for key, destination in value.items():
        if not isinstance(key, str) or not key \
                or not isinstance(destination, str) or not destination:
            raise SplitPolicyError(
                "%s keys and destinations must be non-empty strings" % noun)
        checked.append((key, destination))
    return tuple(sorted(checked))


@dataclass(frozen=True)
class SplitSpec:
    """Everything needed to distribute records, independent of publication."""

    assignment_mode: str
    selected_categories: Tuple[str, ...]
    group_by_filter: str = ""
    choices: Tuple[Tuple[str, str], ...] = ()
    ambiguity_rules: Tuple[Tuple[str, str], ...] = ()
    remainder_id: str = ""

    @classmethod
    def create(
            cls, assignment_mode: object,
            selected_categories: Sequence[str], group_by_filter: str = "",
            choices: Optional[Mapping[str, str]] = None,
            ambiguity_rules: Optional[Mapping[str, str]] = None,
            remainder_id: str = "") -> "SplitSpec":
        mode = normalize_mode(assignment_mode)
        if isinstance(selected_categories, (str, bytes)):
            raise SplitPolicyError("selected categories must be a sequence")
        selected = tuple(sorted(set(selected_categories)))
        if not selected or not all(
                isinstance(category, str) and category for category in selected):
            raise SplitPolicyError("select at least one destination")
        checked_choices = _checked_pairs(choices, "seed choices")
        checked_rules = _checked_pairs(ambiguity_rules, "ambiguity rules")
        if mode == MODE_MATCHING_COPIES and (checked_choices or checked_rules):
            raise SplitPolicyError(
                "matching-copy mode does not accept exclusive destination decisions")
        if not isinstance(group_by_filter, str) \
                or not isinstance(remainder_id, str):
            raise SplitPolicyError("split identifiers must be strings")
        return cls(mode, selected, group_by_filter, checked_choices,
                   checked_rules, remainder_id)

    def identity_fields(self) -> Dict[str, object]:
        return {
            "assignment_mode": self.assignment_mode,
            "selected_categories": list(self.selected_categories),
            "group_by_filter": self.group_by_filter,
            "choices": list(self.choices),
            "ambiguity_rules": list(self.ambiguity_rules),
            "remainder_id": self.remainder_id,
        }


@dataclass(frozen=True)
class RecordDistribution:
    """The candidates and destinations for one source record."""

    candidates: Tuple[str, ...]
    destinations: Tuple[str, ...]
    choice_key: str = ""
    rule_key: str = ""
    direct_choice: str = ""
    resolved_by_rule: bool = False

    @property
    def overlap(self) -> bool:
        return len(self.candidates) > 1

    @property
    def unmatched(self) -> bool:
        return not self.candidates


@dataclass(frozen=True)
class AmbiguitySample:
    seed: str
    rank: int
    rule_key: str
    candidates: Tuple[str, ...]
    choice: str
    resolved_by_rule: bool


@dataclass(frozen=True)
class AmbiguityGroup:
    rule_key: str
    candidates: Tuple[str, ...]
    records: int
    unresolved_records: int
    samples: Tuple[str, ...]


@dataclass(frozen=True)
class ReviewedSplitPlan:
    """Immutable result of applying one SplitSpec to a source snapshot."""

    spec: SplitSpec
    candidate_counts: Tuple[Tuple[str, int], ...]
    destination_counts: Tuple[Tuple[str, int], ...]
    pending_counts: Tuple[Tuple[str, int], ...]
    unmatched_records: int
    overlap_records: int
    unique_copied_records: int
    output_memberships: int
    ambiguity_count: int
    unresolved_records: int
    ambiguities: Tuple[AmbiguitySample, ...]
    ambiguity_groups: Tuple[AmbiguityGroup, ...]
    ambiguity_groups_truncated: bool
    used_choices: Tuple[str, ...]
    used_rules: Tuple[str, ...]

    @classmethod
    def for_publication(
            cls, spec: SplitSpec, destination_counts: Mapping[str, int],
            unmatched_records: int, overlap_records: int,
            unique_copied_records: int,
            output_memberships: int) -> "ReviewedSplitPlan":
        """Rehydrate a validated adapter preview for trusted publication.

        Browser and legacy adapters serialize reviewed plans as reports. This
        restores the immutable policy value after those adapters validate the
        source, names, decisions, and numeric fields. Publication still
        reapplies the policy and compares every aggregate.
        """
        numbers = (
            unmatched_records, overlap_records, unique_copied_records,
            output_memberships)
        if any(isinstance(value, bool) or not isinstance(value, int)
               or value < 0 for value in numbers):
            raise SplitPolicyError(
                "reviewed split statistics must be nonnegative integers")
        counts = []
        for destination, count in destination_counts.items():
            if (not isinstance(destination, str) or not destination
                    or isinstance(count, bool) or not isinstance(count, int)
                    or count <= 0):
                raise SplitPolicyError(
                    "reviewed split destination counts are invalid")
            counts.append((destination, count))
        counts = tuple(sorted(counts))
        if sum(count for _destination, count in counts) != output_memberships:
            raise SplitPolicyError(
                "reviewed split memberships do not match destination counts")
        if unique_copied_records > output_memberships:
            raise SplitPolicyError(
                "reviewed split unique count exceeds output memberships")
        return cls(
            spec, (), counts, (), unmatched_records, overlap_records,
            unique_copied_records, output_memberships,
            overlap_records if spec.assignment_mode == MODE_EXCLUSIVE else 0,
            0, (), (), False, tuple(key for key, _value in spec.choices),
            tuple(key for key, _value in spec.ambiguity_rules))

    def candidates(self) -> Dict[str, int]:
        return dict(self.candidate_counts)

    def destinations(self) -> Dict[str, int]:
        return dict(self.destination_counts)

    def pending(self) -> Dict[str, int]:
        return dict(self.pending_counts)

    @property
    def unrepresented_ambiguities(self) -> int:
        represented = sum(
            group.unresolved_records for group in self.ambiguity_groups)
        return max(0, self.unresolved_records - represented)


class PoolSplitPolicy:
    """Deep interface for reviewing and reapplying seed distribution."""

    def __init__(self, spec: SplitSpec):
        self.spec = spec
        self._selected = set(spec.selected_categories)
        self._choices = dict(spec.choices)
        self._rules = dict(spec.ambiguity_rules)

    def candidates_for(self, record: object) -> Tuple[str, ...]:
        if self.spec.group_by_filter:
            locations = {}
            for occurrence in record.occurrences:
                if (not occurrence.known
                        or occurrence.filter_id != self.spec.group_by_filter
                        or occurrence.location_id not in self._selected):
                    continue
                prior = locations.get(occurrence.location_id)
                if prior is None or occurrence.location_sort_key < prior:
                    locations[occurrence.location_id] = \
                        occurrence.location_sort_key
            return tuple(sorted(locations, key=lambda location: (
                locations[location], location)))
        categories = {
            occurrence.category_id for occurrence in record.occurrences
            if occurrence.known
            and occurrence.category_id in self._selected
        }
        categories.discard(None)
        return tuple(sorted(categories))

    def _choice_for(self, seed: str, rank: int) -> Tuple[str, str]:
        rank_key = "rank:%d" % rank
        seed_choice = self._choices.get(seed, "")
        rank_choice = self._choices.get(rank_key, "")
        if seed_choice and rank_choice and seed_choice != rank_choice:
            raise SplitPolicyError(
                "choices disagree for seed %s / rank %d" % (seed, rank))
        if seed_choice:
            return seed_choice, seed
        if rank_choice:
            return rank_choice, rank_key
        return "", ""

    def distribute_candidates(
            self, candidates: Sequence[str], seed: str,
            rank: int) -> RecordDistribution:
        candidates = tuple(candidates)
        if self.spec.assignment_mode == MODE_MATCHING_COPIES:
            destinations = candidates or (
                (self.spec.remainder_id,) if self.spec.remainder_id else ())
            return RecordDistribution(candidates, tuple(destinations))

        choice, choice_key = self._choice_for(seed, rank)
        if len(candidates) > 1:
            key = ambiguity_rule_key(candidates)
            rule_choice = self._rules.get(key, "")
            if choice and choice not in candidates:
                raise SplitPolicyError(
                    "choice for %s is not one of that seed's candidates" % seed)
            if rule_choice and rule_choice not in candidates:
                raise SplitPolicyError(
                    "ambiguity rule for %s is not one of that group's candidates"
                    % seed)
            destination = choice or rule_choice
            return RecordDistribution(
                candidates, (destination,) if destination else (), choice_key,
                key, choice, bool(not choice and rule_choice))
        if len(candidates) == 1:
            if choice and choice != candidates[0]:
                raise SplitPolicyError(
                    "choice for unambiguous seed %s conflicts with its category"
                    % seed)
            return RecordDistribution(
                candidates, candidates, choice_key, direct_choice=choice)
        if choice:
            raise SplitPolicyError(
                "choice for unmatched seed %s has no selected destination" % seed)
        destinations = (self.spec.remainder_id,) \
            if self.spec.remainder_id else ()
        return RecordDistribution(candidates, destinations)

    def distribute(self, record: object, seed: str) -> RecordDistribution:
        return self.distribute_candidates(
            self.candidates_for(record), seed, record.rank)

    def review(
            self, records: Iterable[object], seed_for_rank: Callable[[int], str],
            cancel_check: Optional[Callable[[], bool]] = None,
            cancel_interval: int = 8192, ambiguity_sample_limit: int = 500,
            ambiguity_group_limit: int = 100) -> ReviewedSplitPlan:
        candidate_counts = {}  # type: Dict[str, int]
        destination_counts = {}  # type: Dict[str, int]
        pending_counts = {}  # type: Dict[str, int]
        unmatched = overlap = unique = memberships = 0
        ambiguity_count = unresolved = 0
        samples = []
        groups = {}  # type: Dict[str, Dict[str, object]]
        groups_truncated = False
        used_choices = set()  # type: Set[str]
        used_rules = set()  # type: Set[str]
        needs_seed_identity = self.spec.assignment_mode == MODE_EXCLUSIVE

        for index, record in enumerate(records):
            if cancel_check is not None and index % cancel_interval == 0 \
                    and cancel_check():
                raise SplitPolicyError("operation cancelled")
            seed = seed_for_rank(record.rank) if needs_seed_identity else ""
            distribution = self.distribute(record, seed)
            for category in distribution.candidates:
                candidate_counts[category] = \
                    candidate_counts.get(category, 0) + 1
            if distribution.unmatched:
                unmatched += 1
            if distribution.overlap:
                overlap += 1
            if distribution.destinations:
                unique += 1
                memberships += len(distribution.destinations)
                for category in distribution.destinations:
                    destination_counts[category] = \
                        destination_counts.get(category, 0) + 1
            if distribution.choice_key:
                used_choices.add(distribution.choice_key)
            if distribution.rule_key and distribution.rule_key in self._rules:
                used_rules.add(distribution.rule_key)

            if (self.spec.assignment_mode != MODE_EXCLUSIVE
                    or not distribution.overlap):
                continue
            ambiguity_count += 1
            if not distribution.destinations:
                unresolved += 1
                for category in distribution.candidates:
                    pending_counts[category] = \
                        pending_counts.get(category, 0) + 1
                group = groups.get(distribution.rule_key)
                if group is None and len(groups) < ambiguity_group_limit:
                    group = {
                        "candidates": distribution.candidates,
                        "records": 0,
                        "samples": [],
                    }
                    groups[distribution.rule_key] = group
                elif group is None:
                    groups_truncated = True
                elif group["candidates"] != distribution.candidates:
                    raise SplitPolicyError(
                        "ambiguity candidate sets produced the same rule token")
                if group is not None:
                    group["records"] += 1
                    if len(group["samples"]) < 3:
                        group["samples"].append(seed)
            if len(samples) < ambiguity_sample_limit:
                samples.append(AmbiguitySample(
                    seed, record.rank, distribution.rule_key,
                    distribution.candidates, distribution.direct_choice,
                    distribution.resolved_by_rule))

        if cancel_check is not None and cancel_check():
            raise SplitPolicyError("operation cancelled")
        unused_choices = sorted(set(self._choices) - used_choices)
        if unused_choices:
            raise SplitPolicyError(
                "choice plan contains a seed/rank not used by this split: %s"
                % unused_choices[0])
        unused_rules = sorted(set(self._rules) - used_rules)
        if unused_rules:
            raise SplitPolicyError(
                "choice plan contains an ambiguity rule not used by this split: %s"
                % unused_rules[0])

        group_rows = tuple(AmbiguityGroup(
            key, tuple(groups[key]["candidates"]),
            int(groups[key]["records"]), int(groups[key]["records"]),
            tuple(groups[key]["samples"])) for key in sorted(groups))
        return ReviewedSplitPlan(
            self.spec,
            tuple(sorted(candidate_counts.items())),
            tuple(sorted(destination_counts.items())),
            tuple(sorted(pending_counts.items())),
            unmatched, overlap, unique, memberships, ambiguity_count,
            unresolved, tuple(samples), group_rows, groups_truncated,
            tuple(sorted(used_choices)), tuple(sorted(used_rules)))
