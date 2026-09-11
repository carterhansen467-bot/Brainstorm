#!/usr/bin/env python3
"""Second-tag semantics, evidence completeness, and strict recipe regressions."""

import copy
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_organizer as organizer
import pool_tag_rules as rules


def occurrence(tag, ante, phase, flags=0, source=0, ordinal=0):
    key = ("tag_" + tag).encode("ascii")
    raw = bytes((1, len(key))) + key + bytes((ante, phase, source, ordinal, flags))
    return organizer.Occurrence.decode(raw)


def recording(start="A3S", end="A7B"):
    return organizer.Occurrence.decode(b"\x82BSTAG\x01" + bytes((
        rules.parse_position(start), rules.parse_position(end))))


def record(*items, sources=()):
    return organizer.Record(17, tuple(items) + tuple(
        organizer.Occurrence.decode(organizer.provenance_descriptor(source))
        for source in sources))


def recipe(start="A3S", end="A7B", condition=None):
    result = {"version": 1, "range": {"start": start, "end": end}}
    if condition is not None:
        result["condition"] = condition
    return result


def count(tag, start="A3S", end="A7B", **bounds):
    return {"count": {"tag": tag, "range": {"start": start, "end": end}, **bounds}}


def reader(criteria=None, branches=None, complete=True):
    if criteria is None:
        criteria = ["tag tag_negative 3 small 7 big 1", "tag tag_rare 3 small 7 big 1"]
    return SimpleNamespace(
        occurrence_metadata_complete=complete,
        is_composite=branches is not None,
        composite_branches={} if branches is None else {
            source_id: organizer.CompositeBranch(
                source_id, 1, 1, False, "pool-%d" % source_id, "Source %d" % source_id,
                tuple(source_criteria)) for source_id, source_criteria in branches.items()},
        header=SimpleNamespace(lines=[
            (line.split()[0], line.split(" ", 1)[1], line) for line in criteria]))


class SecondTagTests(unittest.TestCase):
    def setUp(self):
        self.classifier = rules.TagClassifier(reader(), recipe())

    def test_opposite_tag_is_earliest_later_opposite_not_second_occurrence(self):
        result = self.classifier.classify(record(
            occurrence("negative", 3, 1), occurrence("negative", 4, 2),
            occurrence("rare", 6, 2), occurrence("rare", 5, 1)))
        self.assertEqual(result.destination.key, "a5s-rare")
        self.assertEqual(result.destination.label, "A5 Small Rare")
        self.assertEqual(result.as_dict()["destinations"][0]["ante"], 5)
        self.assertIsNone(result.exclusion)

    def test_rare_first_seeks_negative(self):
        result = self.classifier.classify(record(
            occurrence("rare", 3, 2), occurrence("negative", 5, 2)))
        self.assertEqual(result.destination.key, "a5b-negative")

    def test_first_ante_both_uses_either_type_strictly_later(self):
        for first, second in (("rare", "negative"), ("negative", "rare")):
            for later in ("rare", "negative"):
                with self.subTest(first=first, later=later):
                    result = self.classifier.classify(record(
                        occurrence(first, 3, 1), occurrence(second, 3, 2),
                        occurrence(later, 6, 2)))
                    self.assertEqual(result.destination.key, "a6b-" + later)

    def test_later_ante_both_picks_small_blind(self):
        result = self.classifier.classify(record(
            occurrence("rare", 3, 1), occurrence("negative", 3, 2),
            occurrence("negative", 5, 2), occurrence("rare", 5, 1)))
        self.assertEqual(result.destination.key, "a5s-rare")

    def test_only_same_ante_pair_is_excluded(self):
        result = self.classifier.classify(record(
            occurrence("rare", 4, 1), occurrence("negative", 4, 2)))
        self.assertEqual(result.exclusion, "same_ante_only")
        self.assertFalse(result.destinations)

    def test_one_type_only_and_no_tag_have_distinct_reasons(self):
        result = self.classifier.classify(record(
            occurrence("rare", 4, 1), occurrence("rare", 6, 2)))
        self.assertEqual(result.exclusion, "missing_opposite_tag")
        self.assertEqual(self.classifier.classify(record()).exclusion, "no_eligible_tags")

    def test_inclusive_start_big_excludes_small_in_same_ante(self):
        classifier = rules.TagClassifier(reader(), recipe("A4B", "A6S"))
        result = classifier.classify(record(
            occurrence("rare", 4, 1), occurrence("negative", 4, 2),
            occurrence("rare", 6, 1), occurrence("negative", 6, 2)))
        self.assertEqual(result.destination.key, "a6s-rare")

    def test_first_pair_at_end_has_no_later_candidate(self):
        result = self.classifier.classify(record(
            occurrence("rare", 7, 1), occurrence("negative", 7, 2),
            occurrence("rare", 8, 1)))
        self.assertEqual(result.exclusion, "same_ante_only")

    def test_physical_duplicates_with_different_attributes_count_once(self):
        condition = count("rare", min=1, max=1)
        classifier = rules.TagClassifier(reader(), recipe(condition=condition))
        items = (occurrence("rare", 3, 1), occurrence("rare", 3, 1, flags=2),
                 occurrence("rare", 3, 1, source=2, ordinal=7),
                 occurrence("negative", 6, 2))
        original = record(*items)
        self.assertEqual(classifier.classify(original).destination.key, "a6b-negative")
        self.assertEqual(original.occurrences, items)

    def test_conflicting_tag_keys_and_impossible_locations_are_rejected(self):
        for bad in (
            record(occurrence("rare", 4, 1), occurrence("negative", 4, 1)),
            record(occurrence("rare", 4, 1), occurrence("charm", 4, 1)),
            record(occurrence("rare", 4, 3)),
            record(occurrence("rare", 40, 1)),
        ):
            with self.subTest(record=bad), self.assertRaises(rules.TagRuleError):
                self.classifier.classify(bad)

    def test_many_unrelated_descriptors_do_not_change_tag_decisions(self):
        tags = (occurrence("rare", 3, 1), occurrence("negative", 6, 2))
        for index in range(8300):
            opaque = organizer.Occurrence.decode(b"\x09" + index.to_bytes(3, "little"))
            result = self.classifier.classify(record(*tags, opaque))
            self.assertEqual(result.destination.key, "a6b-negative")
        self.assertLessEqual(len(self.classifier._descriptor_cache), 8192)
        # New relevant evidence remains validated when the cache is full.
        with self.assertRaisesRegex(rules.TagRuleError, "conflicting"):
            self.classifier.classify(record(*tags, occurrence("negative", 3, 1)))


class CoverageTests(unittest.TestCase):
    def test_event_flag_does_not_claim_all_tag_windows(self):
        with self.assertRaises(rules.InsufficientMetadataError) as caught:
            rules.TagClassifier(reader(["tag tag_negative 3 7 1"]), recipe()).classify(record())
        self.assertEqual(caught.exception.missing, (
            {"tag": "rare", "range": {"start": "A3S", "end": "A7B"}},))

    def test_adjacent_current_and_inherited_windows_cover_without_gap(self):
        classifier = rules.TagClassifier(reader([
            "tag tag_negative 3 small 4 small 1",
            "route_tag observe tag_negative 4 big 7 big 1",
            "route_tag collect tag_rare 3 7 1",
        ]), recipe())
        self.assertEqual(classifier.describe_coverage()["sources"][0]["both_tags"],
                         [{"start": "A3S", "end": "A7B"}])

    def test_one_missing_small_or_big_blind_is_a_real_gap(self):
        with self.assertRaises(rules.InsufficientMetadataError) as caught:
            rules.TagClassifier(reader([
                "tag tag_negative 3 small 4 small 1",
                "tag tag_negative 5 small 7 big 1", "tag tag_rare 3 7 1",
            ]), recipe()).classify(record())
        self.assertEqual(caught.exception.missing[0]["range"],
                         {"start": "A4B", "end": "A4B"})

    def test_extra_condition_requires_coverage_outside_main_range(self):
        with self.assertRaises(rules.InsufficientMetadataError):
            rules.TagClassifier(reader(), recipe(condition=count("rare", "A1S", "A2B", max=0))).classify(record())

    def test_negation_or_short_circuit_never_interprets_unknown_as_absent(self):
        condition = {"any": [count("negative", min=1),
                             {"not": count("rare", "A1S", "A1B", min=1)}]}
        with self.assertRaises(rules.InsufficientMetadataError):
            rules.TagClassifier(reader(), recipe(condition=condition)).classify(record())

    def test_seed_can_union_only_its_own_source_coverage(self):
        classifier = rules.TagClassifier(reader(branches={
            11: ["tag tag_negative 3 7 1"],
            22: ["tag tag_rare 3 7 1"],
        }), recipe())
        items = (occurrence("negative", 3, 1), occurrence("rare", 5, 2))
        self.assertEqual(classifier.classify(record(*items, sources=(11, 22))).destination.key,
                         "a5b-rare")
        with self.assertRaises(rules.InsufficientMetadataError) as caught:
            classifier.classify(record(*items, sources=(11,)))
        self.assertEqual(caught.exception.rank, 17)
        self.assertEqual(caught.exception.missing[0]["tag"], "rare")
        # Repeat after cache population to ensure no other seed changes coverage.
        with self.assertRaises(rules.InsufficientMetadataError):
            classifier.classify(record(*items, sources=(11,)))

    def test_overlapping_branch_intervals_allow_cross_branch_coverage(self):
        classifier = rules.TagClassifier(reader(branches={
            1: ["tag tag_negative 3 5 1", "tag tag_rare 3 5 1"],
            2: ["tag tag_negative 5 7 1", "tag tag_rare 5 7 1"],
        }), recipe())
        result = classifier.classify(record(
            occurrence("negative", 4, 1), occurrence("rare", 7, 2), sources=(1, 2)))
        self.assertEqual(result.destination.key, "a7b-rare")

    def test_missing_or_unknown_provenance_is_rejected(self):
        classifier = rules.TagClassifier(reader(branches={
            1: ["tag tag_negative 3 7 1", "tag tag_rare 3 7 1"],
        }), recipe())
        for sources in ((), (99,), (1, 99)):
            with self.subTest(sources=sources), self.assertRaises(rules.TagRuleError):
                classifier.classify(record(sources=sources))

    def test_direct_pool_rejects_undeclared_provenance(self):
        with self.assertRaises(rules.TagRuleError):
            rules.TagClassifier(reader(), recipe()).classify(record(sources=(1,)))
        operand = organizer.Occurrence.decode(organizer.operand_descriptor(1))
        with self.assertRaises(rules.TagRuleError):
            rules.TagClassifier(reader(), recipe()).classify(record(operand))

    def test_missing_event_metadata_refused_even_if_criteria_exist(self):
        for source in (reader(complete=False), reader(complete=False, branches={
            1: ["tag tag_negative 3 7 1", "tag tag_rare 3 7 1"]})):
            with self.assertRaisesRegex(rules.TagRuleError, "without complete recorded"):
                rules.TagClassifier(source, recipe())

    def test_malformed_recorded_criteria_refused(self):
        for criterion in (
            "tag tag_rare 7 3 1", "tag tag_rare 3 boss 7 big 1",
            "tag tag_rare 3 7 zero", "tag tag_rare 3 7 20",
            "route_tag ignore tag_rare 3 7 1", "tag tag_rare 0 7 1",
        ):
            with self.subTest(criterion=criterion), self.assertRaises(rules.TagRuleError):
                rules.TagClassifier(reader([criterion]), recipe())


class RecordedWindowTests(unittest.TestCase):
    def test_voucher_only_recording_supplies_tag_coverage_without_changing_evidence(self):
        classifier = rules.TagClassifier(reader(["voucher v_crystal_ball 1 2"]), recipe())
        voucher = organizer.Occurrence.decode(bytes.fromhex(
            "030e765f6372797374616c5f62616c6c0100010100"))
        value = record(voucher, occurrence("negative", 3, 1),
                       occurrence("rare", 5, 2), recording())
        original = value.occurrences
        self.assertEqual(classifier.classify(value).destination.key, "a5b-rare")
        self.assertEqual(value.occurrences, original)

    def test_marker_proves_an_empty_window_but_its_header_alone_does_not(self):
        source = reader(["voucher v_crystal_ball 1 2", "tag_placement_schema 1"])
        classifier = rules.TagClassifier(source, recipe())
        self.assertEqual(classifier.classify(record(recording())).exclusion, "no_eligible_tags")
        with self.assertRaises(rules.InsufficientMetadataError):
            classifier.classify(record())
        self.assertEqual(rules.describe_recorded_coverage(source)["sources"][0]["both_tags"], [])
        self.assertEqual(classifier.describe_coverage()["sources"][0]["both_tags"], [])

    def test_adjacent_recordings_union_but_a_single_slot_gap_is_unknown(self):
        classifier = rules.TagClassifier(reader([]), recipe())
        first = recording("A3S", "A4S")
        self.assertEqual(classifier.classify(record(
            first, first, recording("A4B", "A7B"))).exclusion, "no_eligible_tags")
        with self.assertRaises(rules.InsufficientMetadataError) as caught:
            classifier.classify(record(first, recording("A5S", "A7B")))
        self.assertEqual(caught.exception.missing, (
            {"tag": "negative", "range": {"start": "A4B", "end": "A4B"}},
            {"tag": "rare", "range": {"start": "A4B", "end": "A4B"}},))

    def test_recording_unions_with_only_the_same_records_branch_windows(self):
        classifier = rules.TagClassifier(reader(branches={
            11: ["tag tag_negative 3 4 1", "tag tag_rare 3 4 1"],
            22: ["tag tag_negative 3 7 1", "tag tag_rare 3 7 1"],
            33: ["voucher v_crystal_ball 1 2"],
        }), recipe())
        suffix = recording("A5S", "A7B")
        self.assertEqual(classifier.classify(record(suffix, sources=(11,))).exclusion,
                         "no_eligible_tags")
        self.assertEqual(classifier.classify(record(sources=(22,))).exclusion,
                         "no_eligible_tags")
        with self.assertRaises(rules.InsufficientMetadataError):
            classifier.classify(record(suffix, sources=(33,)))

    def test_conditions_require_their_own_recorded_windows(self):
        classifier = rules.TagClassifier(reader([]), recipe(condition={"not":
            count("rare", "A1S", "A2B", min=1)}))
        with self.assertRaises(rules.InsufficientMetadataError) as caught:
            classifier.classify(record(recording()))
        self.assertEqual(caught.exception.missing, (
            {"tag": "rare", "range": {"start": "A1S", "end": "A2B"}},))
        self.assertEqual(classifier.classify(record(
            recording(), recording("A1S", "A2B"))).exclusion, "no_eligible_tags")

    def test_recording_cache_never_borrows_another_seeds_marker(self):
        classifier = rules.TagClassifier(reader([]), recipe())
        tags = (occurrence("negative", 3, 1), occurrence("rare", 6, 2))
        complete = record(*tags, recording())
        self.assertEqual(classifier.classify(complete).destination.key, "a6b-rare")
        # Exercise the coverage cache separately from complete descriptor reuse.
        self.assertEqual(classifier.classify(record(recording())).exclusion, "no_eligible_tags")
        for rank in (99, 188495):
            for items in (tags, tags + (recording("A4S", "A7B"),)):
                with self.assertRaises(rules.InsufficientMetadataError) as caught:
                    classifier.classify(organizer.Record(rank, items))
                self.assertEqual(caught.exception.rank, rank)

    def test_malformed_recordings_fail_even_when_header_coverage_is_complete(self):
        valid = recording().raw
        malformed = (valid[:-1], valid + b"\x00", b"\x82BSTAG",
                     b"\x82BSTAG\x00\x04\x0d", b"\x82BSTAG\x02\x04\x0d",
                     b"\x82BSTAG\x01\x0d\x04", b"\x82BSTAG\x01\x00\x4e",
                     b"\x82BSTAG\x01\xff\xff")
        classifier = rules.TagClassifier(reader(), recipe())
        self.assertEqual(classifier.classify(record(recording())).exclusion, "no_eligible_tags")
        for raw in malformed:
            for rank in (17, 199):
                with self.subTest(raw=raw, rank=rank), self.assertRaisesRegex(
                        rules.TagRuleError, "rank %d .*recording" % rank):
                    classifier.classify(organizer.Record(rank, (organizer.Occurrence.decode(raw),)))

    def test_marker_does_not_override_provenance_or_incomplete_event_metadata(self):
        classifier = rules.TagClassifier(reader(branches={11: []}), recipe())
        for sources in ((), (99,), (11, 99)):
            with self.subTest(sources=sources), self.assertRaisesRegex(rules.TagRuleError, "provenance"):
                classifier.classify(record(recording(), sources=sources))
        for source in (reader([], complete=False), reader([], branches={11: []}, complete=False)):
            with self.assertRaisesRegex(rules.TagRuleError, "without complete recorded"):
                rules.TagClassifier(source, recipe()).classify(record(recording(), sources=(11,)))

    def test_edge_slots_are_valid_and_unrelated_opaque_descriptors_remain_opaque(self):
        classifier = rules.TagClassifier(reader([]), recipe("A1S", "A39B"))
        unknown = organizer.Occurrence.decode(b"\x82OTHER\x01\x00\x4d")
        self.assertEqual(classifier.classify(record(
            unknown, recording("A1S", "A39B"))).exclusion, "no_eligible_tags")
        with self.assertRaises(rules.InsufficientMetadataError):
            classifier.classify(record(unknown))

    def test_original_voucher_only_record_needs_its_own_recording(self):
        # The tester's original raw record and its retained voucher-only branch.
        # No unrecorded tag placement is inferred from the complete-pool name.
        original = organizer.Record(188495, tuple(organizer.Occurrence.decode(bytes.fromhex(raw))
            for raw in ("030e765f6372797374616c5f62616c6c0100010100",
                        "807fb4f813310fc75a", "81a44ca67a37658710")))
        classifier = rules.TagClassifier(reader(branches={
            0x7fb4f813310fc75a: ["tag_route collect", "voucher v_crystal_ball 1 2"],
        }), recipe("A4S", "A7B"))
        with self.assertRaises(rules.InsufficientMetadataError) as caught:
            classifier.classify(original)
        self.assertEqual(caught.exception.rank, 188495)
        # A fixture recording with no eligible tags proves absence for this
        # record. This does not claim those are the tester seed's true rolls.
        annotated = organizer.Record(original.rank, original.occurrences + (recording("A4S", "A7B"),))
        self.assertEqual(classifier.classify(annotated).exclusion, "no_eligible_tags")


class EvidenceCacheTests(unittest.TestCase):
    def test_repeated_evidence_across_ranks_avoids_rechecking_conditions(self):
        classifier = rules.TagClassifier(reader(), recipe(condition={"all": [
            count("rare", min=1, max=1), {"not": count("negative", min=2)}]}))
        first = record(occurrence("rare", 3, 1), occurrence("negative", 6, 2))
        second = organizer.Record(999, tuple(organizer.Occurrence.decode(item.raw)
                                             for item in first.occurrences))
        with mock.patch.object(rules, "_condition_matches", wraps=rules._condition_matches) as evaluate:
            expected = classifier.classify(first)
            initial_calls = evaluate.call_count
            self.assertIs(classifier.classify(second), expected)
            self.assertEqual(evaluate.call_count, initial_calls)

    def test_cached_result_cannot_hide_new_conflict_or_unrecorded_source(self):
        classifier = rules.TagClassifier(reader(branches={
            11: ["tag tag_negative 3 7 1"],
            22: ["tag tag_rare 3 7 1"],
        }), recipe())
        tags = (occurrence("negative", 3, 1), occurrence("rare", 6, 2))
        self.assertEqual(classifier.classify(record(*tags, sources=(11, 22))).destination.key,
                         "a6b-rare")
        for rank in (99, 123456789):
            missing = organizer.Record(rank, record(*tags, sources=(11,)).occurrences)
            with self.assertRaises(rules.InsufficientMetadataError) as caught:
                classifier.classify(missing)
            self.assertEqual(caught.exception.rank, rank)
            conflict = organizer.Record(rank, record(
                *tags, occurrence("double", 3, 1), sources=(11, 22)).occurrences)
            with self.assertRaisesRegex(rules.TagRuleError, "rank %d .*conflicting" % rank):
                classifier.classify(conflict)
            undeclared = organizer.Record(rank, record(*tags, sources=(11, 22, 99)).occurrences)
            with self.assertRaisesRegex(rules.TagRuleError, "rank %d .*provenance" % rank):
                classifier.classify(undeclared)

    def test_descriptor_attributes_order_and_cache_limits_preserve_decisions(self):
        source = reader()
        spec = recipe(condition={"any": [count("rare", min=2), count("negative", max=1)]})
        cached = rules.TagClassifier(source, spec)
        uncached = rules.TagClassifier(source, spec)
        uncached._evidence_cache_bytes = rules.EVIDENCE_CACHE_BYTES
        for index in range(rules.EVIDENCE_CACHE_ENTRIES + 50):
            tags = (occurrence("rare", 3, 1), occurrence("negative", 6, 2))
            opaque = organizer.Occurrence.decode(b"\x09" + index.to_bytes(3, "little"))
            items = tags + (opaque, occurrence("rare", 3, 1, flags=index % 256))
            if index % 2:
                items = tuple(reversed(items))
            value = organizer.Record(index * 100003, items)
            self.assertEqual(cached.classify(value), uncached.classify(value))
        self.assertLessEqual(len(cached._evidence_cache), rules.EVIDENCE_CACHE_ENTRIES)
        self.assertLessEqual(cached._evidence_cache_bytes, rules.EVIDENCE_CACHE_BYTES)
        # A descriptor too large to retain still participates in validation.
        oversized = organizer.Occurrence.decode(b"\x09" + b"x" * rules.EVIDENCE_CACHE_BYTES)
        value = record(*tags, oversized)
        byte_limited = rules.TagClassifier(source, spec)
        self.assertEqual(byte_limited.classify(value), uncached.classify(value))
        self.assertFalse(byte_limited._evidence_cache)
        self.assertLessEqual(byte_limited._descriptor_cache_bytes, rules.EVIDENCE_CACHE_BYTES)
        # Descriptor-count limits also skip caching without skipping validation.
        crowded = tags + tuple(organizer.Occurrence.decode(b"\x09" + index.to_bytes(3, "little"))
                               for index in range(rules.EVIDENCE_CACHE_DESCRIPTORS))
        self.assertEqual(byte_limited.classify(record(*crowded)),
                         uncached.classify(record(*crowded)))
        self.assertFalse(byte_limited._evidence_cache)
        with self.assertRaisesRegex(rules.TagRuleError, "conflicting"):
            byte_limited.classify(record(*crowded, occurrence("double", 3, 1)))


class RecipeTests(unittest.TestCase):
    def test_boolean_counts_gate_classification(self):
        condition = {"all": [count("negative", min=1), {"any": [
            count("rare", min=2), {"not": count("rare", "A3S", "A4B", min=1)}]}]}
        classifier = rules.TagClassifier(reader(), recipe(condition=condition))
        self.assertEqual(classifier.classify(record(
            occurrence("negative", 3, 1), occurrence("rare", 5, 2))).destination.key,
                         "a5b-rare")
        self.assertEqual(classifier.classify(record(
            occurrence("negative", 3, 1), occurrence("rare", 4, 2))).exclusion,
                         "condition_not_met")

    def test_recipe_normalization_and_file_roundtrip(self):
        value = recipe("a3s", "A7b", count("rare", max=3))
        value["name"] = " AS1-L1 "
        untouched = copy.deepcopy(value)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "recipe.json")
            normalized = rules.save_recipe(path, value)
            self.assertEqual(rules.load_recipe(path), normalized)
            self.assertEqual(os.listdir(directory), ["recipe.json"])
        self.assertEqual(normalized["name"], "AS1-L1")
        self.assertEqual(normalized["range"]["start"], "A3S")
        self.assertEqual(value, untouched)

    def test_compiled_recipe_cannot_be_changed_by_mutating_input_or_report(self):
        value = recipe(condition=count("rare", min=1))
        classifier = rules.TagClassifier(reader(), value)
        value["condition"]["count"]["min"] = 20
        reported = classifier.recipe
        reported["condition"]["count"]["min"] = 20
        self.assertEqual(classifier.classify(record(
            occurrence("negative", 3, 1), occurrence("rare", 5, 2))).destination.key,
                         "a5b-rare")

    def test_recipe_unknown_keys_types_counts_and_locations_refused(self):
        bad_values = [
            {**recipe(), "script": "__import__('os')"},
            {**recipe(), "version": True}, {**recipe(), "version": 2},
            {**recipe(), "name": ""}, {**recipe(), "name": "one\ntwo"},
            recipe("A0S"), recipe("A40S"), recipe("A03S"), recipe("A4Boss"),
            recipe("A7B", "A3S"), recipe(condition={"all": []}),
            recipe(condition={"xor": []}),
            recipe(condition=count("rare", min=True)),
            recipe(condition=count("rare", min=2, max=1)),
            recipe(condition=count("rare", min=-1)),
            recipe(condition=count("negative", max=79)),
            recipe(condition=count("other", min=1)),
            recipe(condition=count("rare")),
        ]
        for bad in bad_values:
            with self.subTest(value=bad), self.assertRaises(rules.TagRuleError):
                rules.normalize_recipe(bad)

    def test_json_duplicate_nonfinite_deep_and_oversize_inputs_refused(self):
        for value in (
            '{"version":1,"version":1,"range":{"start":"A3S","end":"A7B"}}',
            '{"version":1,"range":{"start":"A3S","end":"A7B"},"name":NaN}',
            '[' * 2000 + ']' * 2000, ' ' * (rules.MAX_RECIPE_BYTES + 1),
        ):
            with self.subTest(prefix=value[:50]), self.assertRaises(rules.TagRuleError):
                rules.loads_recipe(value)
        nested = count("rare", min=1)
        for _ in range(14):
            nested = {"not": nested}
        with self.assertRaisesRegex(rules.TagRuleError, "deeply nested"):
            rules.normalize_recipe(recipe(condition=nested))


class EncodedPoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location(
            "tag_rule_pool_fixtures", os.path.join(ROOT, "tests", "pool_organizer.py"))
        cls.fixtures = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.fixtures)

    def test_actual_bsp3_reader_and_partial_candidate_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "pool.bspool")
            self.fixtures.write_custom_bsp3(
                path, [1, 2], [
                    [occurrence("negative", 3, 1).raw, occurrence("rare", 6, 2).raw],
                    [occurrence("rare", 4, 1).raw, occurrence("negative", 4, 2).raw],
                ], "1111111111111111", [
                    "tag_route collect", "tag tag_negative 3 small 7 big 1",
                    "tag tag_rare 3 small 7 big 1"], coverage_complete=False)
            source = organizer.BSPoolReader(path)
            self.assertFalse(source.coverage_complete)
            classifier = rules.TagClassifier(source, recipe())
            results = [classifier.classify(item) for item in source.iter_records()]
            self.assertEqual(results[0].destination.key, "a6b-rare")
            self.assertEqual(results[1].exclusion, "same_ante_only")

    def test_encoded_voucher_pool_uses_each_records_marker_not_the_output_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "recorded-tags.bspool")
            tags = [occurrence("negative", 3, 1).raw, occurrence("rare", 6, 2).raw]
            self.fixtures.write_custom_bsp3(
                path, [1, 2, 3], [tags + [recording().raw], [recording().raw], tags],
                "1111111111111111", ["voucher v_crystal_ball 1 2", "tag_placement_schema 1"])
            source = organizer.BSPoolReader(path)
            classifier = rules.TagClassifier(source, recipe())
            records = list(source.iter_records())
            self.assertEqual(classifier.classify(records[0]).destination.key, "a6b-rare")
            self.assertEqual(classifier.classify(records[1]).exclusion, "no_eligible_tags")
            with self.assertRaises(rules.InsufficientMetadataError) as caught:
                classifier.classify(records[2])
            self.assertEqual(caught.exception.rank, 3)
            self.assertEqual(rules.describe_recorded_coverage(source)["sources"][0]["both_tags"], [])

    def test_actual_composite_bsp4_uses_record_membership_after_sources_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            first = os.path.join(directory, "negative.bspool")
            second = os.path.join(directory, "rare.bspool")
            combined = os.path.join(directory, "complete.bspool")
            self.fixtures.write_custom_bsp3(
                first, [1, 2], [[occurrence("negative", 3, 1).raw]] * 2,
                "1111111111111111", ["tag tag_negative 3 7 1"])
            self.fixtures.write_custom_bsp3(
                second, [2, 3], [[occurrence("rare", 6, 2).raw]] * 2,
                "2222222222222222", ["tag tag_rare 3 7 1"])
            organizer.combine_pools(
                [organizer.BSPoolReader(first), organizer.BSPoolReader(second)], combined)
            os.unlink(first)
            os.unlink(second)
            source = organizer.BSPoolReader(combined)
            classifier = rules.TagClassifier(source, recipe())
            outcomes = {}
            for item in source.iter_records():
                try:
                    outcomes[item.rank] = classifier.classify(item).destination.key
                except rules.InsufficientMetadataError:
                    outcomes[item.rank] = "unknown"
            self.assertEqual(outcomes, {1: "unknown", 2: "a6b-rare", 3: "unknown"})
            info = rules.describe_recorded_coverage(source)
            self.assertTrue(info["metadata_complete"])
            self.assertTrue(info["checked_per_seed"])
            self.assertEqual(len(info["sources"]), 2)
            self.assertEqual([item["both_tags"] for item in info["sources"]], [[], []])


if __name__ == "__main__":
    unittest.main()
