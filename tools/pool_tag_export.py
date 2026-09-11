#!/usr/bin/env python3
"""Stream complete Negative/Rare evidence from A1 Small through A38 Big.

Slots are zero-based (A1S=0, A38B=75). Empty location arrays are proven
absence, never missing metadata. Named exports publish only after the source
stream verifies; stdout consumers must require the completion trailer.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from contextlib import ExitStack, contextmanager

try:
    import brainstorm_pool_organizer as organizer
    import pool_rule_workflow as workflow
    import pool_tag_recording as recording
    import pool_tag_rules as tags
except ImportError:
    from tools import brainstorm_pool_organizer as organizer
    from tools import pool_rule_workflow as workflow
    from tools import pool_tag_recording as recording
    from tools import pool_tag_rules as tags


EXPORT_VERSION = 1
EXPORT_RANGE = {"start": "A1S", "end": "A38B"}
LAST_SLOT = 75
EXPORT_RECIPE = {"version": 1, "mode": "second_tag", "rule": {
    "version": 1, "range": EXPORT_RANGE}}


def _destination(key):
    match = re.fullmatch(r"a([1-9][0-9]?)([sb])-(negative|rare)", key)
    if not match:
        raise organizer.PoolError("Saved second-tag destination is invalid.")
    position = "A" + match[1] + match[2].upper()
    slot = tags.parse_position(position)
    return {"position": position, "slot": slot, "tag": match[3],
            "ante": slot // 2 + 1, "phase": "big" if slot % 2 else "small",
            "key": key, "source": "workflow"}


def _second_tag(reader):
    """Only the current machine-readable rule identifies an exact pool result.

    Labels and filenames remain available to the caller, but cannot certify
    an earliest second tag or recover a rule discarded by a later operation.
    """
    header = reader.header
    fields = ("workflow_schema", "workflow_recipe", "workflow_recipe_id",
              "workflow_destination")
    if not any(header.values.get(key) for key in fields):
        return None, None, "No saved second-tag rule; supply an explicit second tag."
    if any(not header.values.get(key) for key in fields):
        raise organizer.PoolError("Saved tag workflow metadata is incomplete.")
    if header.one("workflow_schema") != "1":
        raise organizer.PoolError("Unsupported saved tag workflow schema.")
    recipe = workflow.loads_recipe(organizer._decode_header_token(header.one("workflow_recipe")))
    recipe_id = workflow._fingerprint(recipe)
    if header.one("workflow_recipe_id") != recipe_id:
        raise organizer.PoolError("Saved tag workflow recipe identity is inconsistent.")
    if recipe["mode"] != "second_tag":
        return None, None, "This pool saved source separation, not a second-tag result."
    key = organizer._decode_header_token(header.one("workflow_destination"))
    destination = _destination(key)
    first, last = tags._range_pair(recipe["rule"]["range"])
    if not first <= destination["slot"] <= last:
        raise organizer.PoolError("Saved second tag falls outside its rule range.")
    category = header.one("organizer_category", required=False)
    if category and category != "rules:%s:%s" % (recipe_id, key):
        raise organizer.PoolError("Saved second-tag category disagrees with its workflow.")
    return destination, recipe["rule"], None


def _describe(reader):
    result = organizer.source_summary(reader)
    result.update({"filename": os.path.basename(reader.path),
                   "label": reader.header.one("label", required=False),
                   "header_text": reader.header.text})
    return result


class TagExport:
    """Bounded-memory export and callable record iterator for batch consumers.

    ``metadata`` retains the original pool header and labels even when ``reader``
    is a temporary enriched copy. ``iter_records`` must be exhausted: the core
    reader validates final digests and source identity at the end of the stream.
    Original occurrence dictionaries are retained and every descriptor also
    includes raw_hex, including known tags and provenance markers.
    """

    def __init__(self, reader, original_reader=None):
        self.reader = reader
        self.original_reader = original_reader or reader
        for source in (reader, self.original_reader):
            if source.schema not in (3, 4) or not source.complete:
                raise organizer.PoolError("Tag export needs a finished BSP3/BSP4 event pool.")
        self._evidence = tags.TagClassifier(reader, EXPORT_RECIPE["rule"])
        second, rule, reason = _second_tag(self.original_reader)
        self._second_classifier = tags.TagClassifier(reader, rule) if rule else None
        self._second_tag = second
        self.metadata = {
            "type": "tag_export_header", "version": EXPORT_VERSION,
            "range": dict(EXPORT_RANGE), "slot_base": 0,
            "source": _describe(self.original_reader),
            "recorded_source": _describe(reader) if reader is not self.original_reader else None,
            "second_tag": second, "second_tag_reason": reason,
            "coverage_checked_per_seed": True,
        }
        self._label = self.metadata["source"]["label"] or self.metadata["source"]["filename"]

    def iter_records(self, cancel_check=None, progress=None):
        """Yield complete placement rows, preserving seeds with zero target tags."""
        processed = 0
        with self.original_reader._open_source_snapshot(cancel_check):
            for record in self.reader.iter_records(cancel_check=cancel_check):
                organizer._check_cancel(cancel_check)
                placements = tuple((slot, tag) for slot, tag in
                                   self._evidence._record_evidence(record) if slot <= LAST_SLOT)
                if self._second_classifier:
                    result = self._second_classifier.classify(record)
                    if result.destination is None or result.destination.key != self._second_tag["key"]:
                        raise organizer.PoolError(
                            "Seed rank %d disagrees with this pool's saved second-tag result." % record.rank)
                negative = [slot for slot, tag in placements if tag == "negative"]
                rare = [slot for slot, tag in placements if tag == "rare"]
                tokens = ["%s-%s" % (tags.position_token(slot), tag) for slot, tag in placements]
                occurrences = []
                for item in record.occurrences:
                    value = item.as_dict()
                    value["raw_hex"] = item.raw.hex()
                    occurrences.append(value)
                branches = sorted({item.provenance_id for item in record.occurrences
                                   if item.provenance_id is not None})
                operands = sorted({item.operand_id for item in record.occurrences
                                   if item.operand_id is not None})
                yield {
                    "type": "seed", "seed": self.reader.seed(record.rank), "rank": record.rank,
                    "negative_slots": negative, "rare_slots": rare,
                    "negative_locations": [tags.position_token(slot) for slot in negative],
                    "rare_locations": [tags.position_token(slot) for slot in rare],
                    "tags": tokens, "tag_string": " ".join(tokens),
                    "tag_coverage": {**EXPORT_RANGE, "complete": True},
                    "occurrences": occurrences, "pool_label": self._label,
                    "source_labels": [self.reader.composite_operands[key].label for key in operands],
                    "original_source_labels": [self.reader.composite_branches[key].label for key in branches],
                    "second_tag": dict(self._second_tag) if self._second_tag else None,
                }
                processed += 1
                if progress and processed % 4096 == 0:
                    progress(processed, self.reader.records)
            organizer._check_cancel(cancel_check)
            if processed != self.reader.records:
                raise organizer.PoolError("Tag export did not include every source seed.")
            if progress:
                progress(processed, self.reader.records)

    def iter_documents(self, cancel_check=None, progress=None):
        yield self.metadata
        count = 0
        for row in self.iter_records(cancel_check, progress):
            yield row
            count += 1
        yield {"type": "tag_export_complete", "version": EXPORT_VERSION, "records": count}


@contextmanager
def open_pool(path, snapshot_path=None, helper=None, temp_dir=None,
              cancel_check=None, progress=None, phase=None):
    """Open a strict export, optionally recording full range into a private copy.

    Supplying a matching snapshot explicitly records A1S–A38B for every seed;
    no preliminary whole-pool scan is required. The source is never changed.
    Without it, missing per-seed coverage raises InsufficientMetadataError.
    The temporary enriched pool exists only inside this context manager.
    """
    reader = organizer.BSPoolReader(path, verify_payloads=False, cancel_check=cancel_check)
    with ExitStack() as stack:
        stack.enter_context(reader._open_source_snapshot(cancel_check))
        if snapshot_path is None:
            yield TagExport(reader)
            return
        if helper is None:
            try:
                import pool_organizer_web as web
            except ImportError:
                from tools import pool_organizer_web as web
            helper = web._native_split_helper()
            if helper is None:
                raise organizer.PoolError("Recording missing tags requires the native pool helper.")
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="pool-tag-export-", dir=temp_dir))
        result = recording.record_tags(reader, EXPORT_RECIPE, directory, snapshot_path, helper,
                                       cancel_check=cancel_check, progress=progress, phase=phase)
        recorded = organizer.BSPoolReader(result["path"], verify_payloads=False, cancel_check=cancel_check)
        yield TagExport(recorded, reader)


def write_ndjson(export, destination, cancel_check=None, progress=None):
    """Write a complete typed NDJSON stream; existing named files are preserved."""
    def write(handle):
        for document in export.iter_documents(cancel_check, progress):
            handle.write(json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n")
    if destination == "-":
        write(sys.stdout)
        return
    target = os.path.abspath(os.fspath(destination))
    staged = None
    linked = False
    try:
        with export.original_reader._open_source_snapshot(cancel_check) as source:
            with organizer.pool_writer_guard(target):
                if os.path.lexists(target):
                    raise organizer.PoolError("Tag export output already exists; choose a new filename.")
                descriptor, staged = tempfile.mkstemp(prefix=".tag-export-", dir=os.path.dirname(target))
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    write(handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                organizer._check_cancel(cancel_check)
                export.original_reader._assert_source_unchanged(source)
                organizer.seed_pool_mutations.link_no_overwrite(staged, target)
                linked = True
                organizer._check_cancel(cancel_check)
    except BaseException:
        if linked:
            organizer.seed_pool_mutations.rollback_link(staged, target)
        raise
    finally:
        if staged and os.path.exists(staged):
            os.unlink(staged)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Finished .bspool event pool")
    parser.add_argument("output", help="New NDJSON file, or - for stdout")
    parser.add_argument("--snapshot", help="Matching native_search.cfg; records all tags into a private copy")
    args = parser.parse_args(argv)
    try:
        with open_pool(args.input, args.snapshot) as export:
            write_ndjson(export, args.output)
        return 0
    except (organizer.PoolError, OSError) as error:
        print("Tag export: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
