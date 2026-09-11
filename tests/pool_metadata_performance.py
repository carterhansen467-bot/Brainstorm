#!/usr/bin/env python3
"""Bounded reuse of immutable metadata without weakening record validation."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_organizer as organizer
import pool_organizer_web as organizer_web

_spec = importlib.util.spec_from_file_location(
    "metadata_performance_fixtures", os.path.join(ROOT, "tests", "pool_organizer.py"))
fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixtures)


def occurrence(raw):
    return organizer.Occurrence.decode(raw)


def branch(value):
    return occurrence(organizer.provenance_descriptor(value))


def operand(value):
    return occurrence(organizer.operand_descriptor(value))


def reader(branches=(1, 2), operands=(10, 11), operation="union"):
    result = object.__new__(organizer.BSPoolReader)
    result.composite_branches = dict.fromkeys(branches)
    result.composite_operands = dict.fromkeys(operands)
    result.composite_expression = {
        "op": operation,
        "inputs": [{"operand": "%016x" % value} for value in operands],
    }
    return result


class MetadataReuseRegression(unittest.TestCase):
    def test_repeated_decoded_descriptors_are_parsed_once(self):
        items = (branch(1), operand(10), occurrence(b"\x90\x01\xaa"))
        for schema in (3, 4):
            with self.subTest(schema=schema):
                encode = getattr(organizer, "_encode_bsp%d_metadata" % schema)
                decode = (organizer.BSPoolReader._decode_metadata if schema == 3
                          else organizer.BSPoolReader._decode_metadata4)
                payload, associations = encode([items] * 200)
                decoded = decode(payload, 200, associations)
                with mock.patch.object(
                        organizer, "provenance_branch_id",
                        wraps=organizer.provenance_branch_id) as parse_branch, \
                        mock.patch.object(
                            organizer, "operand_id_from_descriptor",
                            wraps=organizer.operand_id_from_descriptor) as parse_operand:
                    reader()._validate_composite_metadata(decoded)
                    # Metadata decoding shares each immutable descriptor among
                    # its matching records; callers also inspect it afterwards.
                    for record in decoded:
                        for item in record:
                            item.provenance_id
                            item.is_provenance
                            item.operand_id
                            item.is_operand
                    self.assertEqual(parse_branch.call_count, len(items))
                    self.assertEqual(parse_operand.call_count, len(items))

    def test_repeated_memberships_only_evaluate_the_expression_once(self):
        subject = reader()
        items = (branch(1), operand(10))
        original = organizer.expression_matches
        top_level_calls = []

        def evaluate(expression, members):
            if expression is subject.composite_expression:
                top_level_calls.append(frozenset(members))
            return original(expression, members)

        with mock.patch.object(organizer, "expression_matches", side_effect=evaluate):
            subject._validate_composite_metadata([items] * 200)
        self.assertEqual(top_level_calls, [frozenset((10,))])

    def test_invalid_record_after_many_valid_memberships_is_still_rejected(self):
        # More distinct memberships than the bounded validation cache can keep.
        subject = reader(branches=range(600), operands=range(10, 20))
        valid = [
            (branch(index),) + tuple(operand(10 + bit) for bit in range(10)
                                    if (index + 1) & (1 << bit))
            for index in range(600)
        ]
        for invalid, error in (
                ((operand(10),), "missing branch or operand"),
                ((branch(1),), "missing branch or operand"),
                ((branch(900), operand(10)), "undeclared source branch"),
                ((branch(1), operand(99)), "undeclared set operand"),
                ((occurrence(organizer.provenance_descriptor(1)[:-1]), operand(10)),
                 "missing branch or operand"),
                ((branch(1), occurrence(organizer.operand_descriptor(10)[:-1])),
                 "missing branch or operand")):
            with self.subTest(error=error, invalid=invalid):
                with self.assertRaisesRegex(organizer.PoolError, error):
                    subject._validate_composite_metadata(valid + [invalid])

        difference = reader(branches=range(600), operands=range(10, 21))
        difference.composite_expression = {
            "op": "difference", "inputs": [
                subject.composite_expression,
                {"operand": "%016x" % 20}],
        }
        with self.assertRaisesRegex(organizer.PoolError, "does not satisfy"):
            difference._validate_composite_metadata(
                valid + [(branch(1), operand(10), operand(20))])

    def test_repeated_validation_does_not_share_header_assumptions(self):
        items = (branch(1), operand(10))
        subject = reader()
        subject._validate_composite_metadata([items] * 200)
        subject.composite_expression = {
            "op": "intersection", "inputs": [
                {"operand": "000000000000000a"},
                {"operand": "000000000000000b"}],
        }
        with self.assertRaisesRegex(organizer.PoolError, "does not satisfy"):
            subject._validate_composite_metadata([items])
        with self.assertRaisesRegex(organizer.PoolError, "invalid length"):
            occurrence(b"\x01\x03bad")


class PythonMetadataVerificationRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="brainstorm-metadata-verify-")
        paths = [os.path.join(self.temp.name, "source%d.bspool" % index)
                 for index in range(2)]
        for index, path in enumerate(paths):
            fixtures.write_custom_bsp3(
                path, [index], [[fixtures.TAG]], "%016x" % (index + 1),
                ["tag tag_negative 3 7 1"])
        self.path = os.path.join(self.temp.name, "composite.bspool")
        organizer.combine_pools(
            [organizer.BSPoolReader(path) for path in paths], self.path)

    def tearDown(self):
        self.temp.cleanup()

    def native_verified_reader(self):
        subject = organizer.BSPoolReader(self.path, verify_payloads=False)
        self.assertFalse(subject._composite_metadata_verified)
        subject.accept_native_verification(
            subject.membership_digest, subject.metadata_digest)
        self.assertTrue(subject._payload_verified)
        self.assertFalse(subject._composite_metadata_verified)
        return subject

    def test_native_digests_do_not_skip_python_semantic_verification(self):
        for consume in (lambda subject: list(subject.iter_records()),
                        lambda subject: subject._verify_all_payloads()):
            with self.subTest(consume=consume):
                subject = self.native_verified_reader()
                with mock.patch.object(
                        subject, "_validate_composite_metadata",
                        wraps=subject._validate_composite_metadata) as validate:
                    consume(subject)
                self.assertGreater(validate.call_count, 0)
                self.assertTrue(subject._composite_metadata_verified)

    def test_incomplete_or_failed_python_pass_never_promotes_semantics(self):
        subject = self.native_verified_reader()
        records = subject.iter_records()
        next(records)
        self.assertFalse(subject._composite_metadata_verified)
        records.close()
        self.assertFalse(subject._composite_metadata_verified)

        subject = self.native_verified_reader()
        # Native checksums cannot certify that every record satisfies an
        # otherwise valid composite header expression.
        subject.composite_expression["op"] = "intersection"
        with self.assertRaisesRegex(organizer.PoolError, "does not satisfy"):
            list(subject.iter_records())
        self.assertFalse(subject._composite_metadata_verified)

        subject = self.native_verified_reader()
        cancelled = False
        records = subject.iter_records(cancel_check=lambda: cancelled)
        next(records)
        cancelled = True
        with self.assertRaises(organizer.PoolError):
            list(records)
        self.assertFalse(subject._composite_metadata_verified)


class NativeSummaryCancellationRegression(unittest.TestCase):
    def test_raising_cancellation_always_stops_and_reaps_the_child(self):
        for ignores_terminate in (False, True):
            with self.subTest(ignores_terminate=ignores_terminate):
                child = mock.Mock()
                child.poll.return_value = None
                timeout = subprocess.TimeoutExpired(["test-native"], 0.1)
                child.communicate.side_effect = (
                    [timeout, timeout, ("", "")] if ignores_terminate
                    else [timeout, ("", "")])
                cancelled = organizer.PoolError("cancel from progress callback")
                with mock.patch.object(
                        organizer_web, "_native_pool_binary", return_value="test-native"), \
                        mock.patch.object(
                            organizer_web.subprocess, "Popen", return_value=child):
                    with self.assertRaises(organizer.PoolError) as error:
                        organizer_web._run_native_summary(
                            "unused.bspool", cancel_check=mock.Mock(side_effect=cancelled))
                self.assertIs(error.exception, cancelled)
                child.terminate.assert_called_once_with()
                self.assertEqual(child.kill.call_count, int(ignores_terminate))
                self.assertEqual(child.communicate.call_count,
                                 3 if ignores_terminate else 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
