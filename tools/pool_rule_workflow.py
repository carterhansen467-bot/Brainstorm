#!/usr/bin/env python3
"""Stream saved source-membership and tag rules into reviewed BSP4 pools.

Preview stores exact counts and source/recipe fingerprints, never a per-seed
assignment list. Publication re-evaluates the same pinned recipe, verifies all
counts, and stages every pool and its report before publishing without overwrite.
Only the Python standard library is required. An optional native helper
validates, counts, and copies original groups without a per-seed Python scan.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import asdict

try:
    import brainstorm_pool_organizer as organizer
    import brainstorm_pool_builder as builder
except ImportError:
    from tools import brainstorm_pool_organizer as organizer
    from tools import brainstorm_pool_builder as builder


PoolError = organizer.PoolError
VERSION = 1
MAX_RECIPE_BYTES = 48 * 1024
MAX_PLAN_BYTES = 2 * 1024 * 1024


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def _fingerprint(value):
    return hashlib.sha256(_json(value).encode("ascii")).hexdigest()


def _loads_json(text, limit):
    if not isinstance(text, str):
        raise PoolError("Saved JSON must be text.")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise PoolError("Saved JSON must contain valid Unicode text.") from None
    if size > limit:
        raise PoolError("Saved JSON is too large (maximum %d bytes)." % limit)

    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PoolError("Saved JSON repeats the field %s." % key)
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=unique_fields,
                           parse_constant=lambda value: (_ for _ in ()).throw(
                               PoolError("Saved JSON contains a non-finite number.")))
        # Escaped unpaired surrogates and overflowing numeric literals can
        # survive json.loads despite invalid Unicode/non-finite raw values.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return value
    except (UnicodeEncodeError, ValueError, RecursionError) as exc:
        raise PoolError("Cannot read saved JSON: %s" % exc) from None


def _load_json(path, limit):
    with open(path, "rb") as handle:
        content = handle.read(limit + 1)
    if len(content) > limit:
        raise PoolError("Saved JSON is too large (maximum %d bytes)." % limit)
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise PoolError("Saved JSON must use UTF-8.") from None
    return _loads_json(text, limit)


def loads_recipe(text):
    """Validate raw saved workflow JSON before a UI installs its settings.

    The wrapper is mandatory. Duplicate or unknown fields, future versions,
    invalid Unicode, non-finite values, and oversized/deep input are errors;
    no unsupported settings are silently removed.
    """
    return normalize_recipe(_loads_json(text, MAX_RECIPE_BYTES))


def _tag_rules():
    try:
        import pool_tag_rules
    except ImportError:
        from tools import pool_tag_rules
    return pool_tag_rules


def _source_pin(reader):
    return {
        "path": reader.path,
        "file_identity": asdict(reader._source_identity),
        "snapshot_id": reader.snapshot_token,
        "membership_digest": "%016x" % reader.membership_digest,
        "metadata_digest": "%016x" % reader.metadata_digest,
        "records": reader.records,
        "header_hash": _fingerprint(reader.header.lines),
    }


def _source_definitions(reader, kind):
    if not reader.is_composite:
        raise PoolError("This pool has no saved source memberships. Choose a combined pool.")
    definitions = (reader.composite_operands if kind == "inputs"
                   else reader.composite_branches)
    return {"%016x" % key: value for key, value in definitions.items()}


def _memberships(record, kind):
    return {"%016x" % (item.operand_id if kind == "inputs" else item.provenance_id)
            for item in record.occurrences
            if (item.is_operand if kind == "inputs" else item.is_provenance)}


def describe_source(reader, cancel_check=None, progress=None, *, count_records=True):
    """Describe recoverable direct inputs and retained original branches.

    Exact counts require a full traversal. A header-only description lists the
    recorded groups immediately, with current counts explicitly unknown. Direct
    input original counts are historical; removed seeds are never inferred.
    """
    organizer._check_cancel(cancel_check)
    counts = {"inputs": collections.Counter(), "branches": collections.Counter()}
    overlaps = {"inputs": 0, "branches": 0}
    processed = 0
    with reader._open_source_snapshot(cancel_check):
        if reader.is_composite and count_records:
            for record in reader.iter_records(cancel_check=cancel_check):
                for kind in counts:
                    memberships = _memberships(record, kind)
                    counts[kind].update(memberships)
                    overlaps[kind] += int(len(memberships) > 1)
                processed += 1
                if processed % organizer.CANCEL_CHECK_RECORDS == 0:
                    organizer._check_cancel(cancel_check)
                    if progress:
                        progress(processed, reader.records)
    rows = {}
    for kind in counts:
        rows[kind] = []
        if not reader.is_composite:
            continue
        for token, definition in sorted(_source_definitions(reader, kind).items()):
            row = definition.as_dict()
            if kind == "inputs":
                row["original_records"] = definition.records
                row["missing_records"] = (max(0, definition.records - counts[kind][token])
                                          if count_records else None)
            row.update({"id": token, "kind": kind,
                        "records": counts[kind][token] if count_records else None})
            rows[kind].append(row)
    if progress and count_records:
        progress(reader.records, reader.records)
    return {
        "source": organizer.source_summary(reader),
        "source_pin": _source_pin(reader),
        "can_separate": reader.is_composite,
        "direct_inputs": rows["inputs"],
        "original_sources": rows["branches"],
        "overlap_records": overlaps if count_records else None,
        "counts_pending": not count_records,
        "engine": "python",
        "note": ("Seeds shared by selected sources are copied to each output. "
                 "Only memberships still present in this pool can be recovered. "
                 "Earlier intermediate groups may no longer be recorded."),
    }


def normalize_recipe(recipe):
    if not isinstance(recipe, dict):
        raise PoolError("A saved workflow must be a JSON object.")
    try:
        recipe_bytes = len(_json(recipe).encode("ascii"))
    except (ValueError, TypeError, RecursionError):
        raise PoolError("A saved workflow must contain finite, bounded JSON data.") from None
    if recipe_bytes > MAX_RECIPE_BYTES:
        raise PoolError("The saved workflow is too large (maximum 48 KiB).")
    if type(recipe.get("version")) is not int or recipe.get("version") != VERSION:
        raise PoolError("Unsupported saved workflow version.")
    mode = recipe.get("mode")
    if mode == "separate_sources":
        unknown = set(recipe) - {"version", "mode", "source_kind", "source_ids"}
        kind = recipe.get("source_kind", "inputs")
        if kind not in ("inputs", "branches"):
            raise PoolError("Source type must be inputs or branches.")
        source_ids = recipe.get("source_ids", [])
        if not isinstance(source_ids, list) or any(
                not isinstance(item, str) or not re.fullmatch(r"[0-9a-fA-F]{16}", item)
                for item in source_ids):
            raise PoolError("Choose source IDs from the detected source list.")
        ids = sorted({item.lower() for item in source_ids})
        if len(ids) != len(source_ids):
            raise PoolError("A source was selected more than once.")
        result = {"version": VERSION, "mode": mode,
                  "source_kind": kind, "source_ids": ids}
    elif mode == "second_tag":
        unknown = set(recipe) - {"version", "mode", "rule"}
        result = {"version": VERSION, "mode": mode,
                  "rule": _tag_rules().normalize_recipe(recipe.get("rule"))}
    else:
        raise PoolError("Choose Separate sources or Second tag.")
    if unknown:
        raise PoolError("Unknown saved workflow field: %s" % sorted(unknown)[0])
    return result


def _clean_prefix(prefix):
    if not isinstance(prefix, str) or len(prefix) > 80 or any(
            ord(ch) < 32 or ch in '/\\:*?"<>|' for ch in prefix):
        raise PoolError("Use a short output prefix without path separators or special filename characters.")
    return prefix.strip().strip(". ")


def _filename(label, key, prefix):
    stem = label.split(" [", 1)[0]
    if stem.lower().endswith(".bspool"):
        stem = stem[:-7]
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "-", stem).strip("-.")[:80] or "pool"
    if prefix:
        stem = re.sub(r"[^A-Za-z0-9._+-]+", "-", prefix).strip("-.") + "-" + stem
    return "%s-%s.bspool" % (stem, _fingerprint(key)[:10])


def _destination_sort_key(key, label):
    match = re.fullmatch(r"a([0-9]+)([sb])-(negative|rare)", key)
    if match:
        return (0, int(match[1]), int(match[2] == "b"), match[3])
    return (1, label.casefold(), key)


class _Classifier:
    def __init__(self, reader, recipe):
        self.recipe = recipe
        self.tag = None
        self.selected = {}
        self.selected_ids = set()
        if recipe["mode"] == "separate_sources":
            definitions = _source_definitions(reader, recipe["source_kind"])
            ids = recipe["source_ids"] or sorted(definitions)
            missing = set(ids) - set(definitions)
            if missing:
                raise PoolError("A selected source is no longer recorded in this pool: %s" % sorted(missing)[0])
            self.selected = {token: definitions[token] for token in ids}
            self.selected_ids = set(ids)
        else:
            self.tag = _tag_rules().TagClassifier(reader, recipe["rule"])

    def classify(self, record):
        if self.tag:
            result = self.tag.classify(record)
            return [(item.key, item.label) for item in result.destinations], result.exclusion
        kind = self.recipe["source_kind"]
        ids = sorted(_memberships(record, kind) & self.selected_ids)
        return [("source:%s:%s" % (kind, token),
                 self.selected[token].label or self.selected[token].pool_id or token)
                for token in ids], None if ids else "outside_selected_sources"


def _scan(reader, classifier, prefix, cancel_check=None, progress=None, consume=None):
    counts = collections.Counter()
    exclusions = collections.Counter()
    labels = {}
    copied = overlaps = processed = 0
    for record in reader.iter_records(cancel_check=cancel_check):
        destinations, exclusion = classifier.classify(record)
        if len({key for key, _label in destinations}) != len(destinations):
            raise PoolError("The rule produced duplicate destinations for one seed.")
        if destinations:
            copied += 1
            overlaps += int(len(destinations) > 1)
        else:
            exclusions[exclusion or "no_matching_destination"] += 1
        for key, label in destinations:
            if key in labels and labels[key] != label:
                raise PoolError("The rule produced conflicting names for one destination.")
            labels[key] = label
            counts[key] += 1
            organizer._check_split_output_limit(len(counts))
            if consume:
                consume(record, key, label)
        processed += 1
        if processed % organizer.CANCEL_CHECK_RECORDS == 0:
            organizer._check_cancel(cancel_check)
            if progress:
                progress(processed, reader.records)
    organizer._check_cancel(cancel_check)
    if processed != reader.records:
        raise PoolError("The source record count changed; preview the pool again.")
    if progress:
        progress(processed, reader.records)
    outputs = _output_rows(counts, labels, prefix)
    return {"outputs": outputs, "source_records": processed,
            "copied_records": copied, "excluded_records": processed - copied,
            "overlap_records": overlaps, "output_memberships": sum(counts.values()),
            "exclusions": dict(sorted(exclusions.items()))}


def _output_rows(counts, labels, prefix):
    return [{"key": key, "category_id": key, "label": labels[key],
                "records": counts[key], "name": _filename(labels[key], key, prefix)}
               for key in sorted(counts, key=lambda key: _destination_sort_key(key, labels[key]))]


def preview(reader, recipe, prefix="", cancel_check=None, progress=None, *, native_helper=None):
    """Return a serializable, source-pinned reviewed plan with exact counts."""
    organizer._check_cancel(cancel_check)
    recipe = normalize_recipe(recipe)
    prefix = _clean_prefix(prefix)
    classifier = _Classifier(reader, recipe)
    result, engine = None, "python"
    with reader._open_source_snapshot(cancel_check):
        if native_helper is not None and recipe["mode"] == "separate_sources":
            try:
                import pool_rule_native
            except ImportError:
                from tools import pool_rule_native
            try:
                result = pool_rule_native.preview_sources(
                    reader, recipe, native_helper, cancel_check, progress)
            except organizer.NativeSplitUnsupported:
                pass
            else:
                counts, labels = {}, {}
                for token, count in result.pop("counts").items():
                    if not count:
                        continue
                    key = "source:%s:%s" % (recipe["source_kind"], token)
                    definition = classifier.selected[token]
                    counts[key] = count
                    labels[key] = definition.label or definition.pool_id or token
                organizer._check_split_output_limit(len(counts))
                result["outputs"] = _output_rows(counts, labels, prefix)
                engine = "native"
        if result is None:
            result = _scan(reader, classifier, prefix, cancel_check, progress)
    result.update({"workflow_version": VERSION, "recipe": recipe,
                   "recipe_id": _fingerprint(recipe), "prefix": prefix,
                   "source_pin": _source_pin(reader),
                   "source": organizer.source_summary(reader), "engine": engine})
    if classifier.tag:
        result["coverage"] = classifier.tag.describe_coverage()
    result["plan_id"] = _fingerprint(result)
    return result


class _HeaderView:
    def __init__(self, reader, header_bytes):
        self.reader = reader
        self.header_bytes = header_bytes

    def __getattr__(self, name):
        return getattr(self.reader, name)


def _header_builder(reader, key, label, plan, minimum_size=0):
    # Match the writer's bounded ASCII header-label contract. Display names in
    # the UTF-8 report and encoded composite dictionaries remain intact.
    label = label.replace("\r", " ").replace("\n", " ").replace("\0", " ")
    label = label.encode("ascii", "replace").decode("ascii")[:135]
    category = "rules:%s:%s" % (plan["recipe_id"], key)
    extra = ("workflow_schema 1\nworkflow_recipe %s\nworkflow_recipe_id %s\n"
             "workflow_plan_id %s\nworkflow_destination %s\n" % (
                 organizer._header_token(_json(plan["recipe"])), plan["recipe_id"],
                 plan["plan_id"], organizer._header_token(key))).encode("ascii")

    def build(size, records, data_bytes, membership, metadata):
        raw, identity = organizer.build_output_header(
            _HeaderView(reader, size), category, label, records, data_bytes,
            membership, metadata, schema=4, coverage_complete=False)
        # Tag/source classification narrows the parent membership beyond its
        # retained native criteria. Complete copying of those selected seeds
        # does not prove exhaustive coverage for the broader native predicate.
        # The core flag survives older native refilters; source/parent coverage
        # remain untouched as historical facts in build_output_header.
        text = raw.rstrip(b"\0")
        if not text.endswith(b"end\n"):
            raise PoolError("Cannot extend the derived pool header.")
        text = text[:-4] + extra + b"end\n"
        if len(text) > size:
            raise PoolError("Saved workflow exceeds the pool header capacity.")
        return text.ljust(size, b"\0"), identity

    # Leave room for final uint64 counters and retained composite dictionaries.
    for size in organizer.COMPOSITE_HEADER_SIZES:
        if size < minimum_size:
            continue
        try:
            build(size, organizer.MASK64, organizer.MASK64,
                  organizer.MASK64, organizer.MASK64)
        except PoolError as exc:
            if "header" not in str(exc):
                raise
            continue
        return size, lambda records, data_bytes, membership, metadata: build(
            size, records, data_bytes, membership, metadata)
    raise PoolError("The source metadata and saved workflow exceed the maximum pool header size.")


def report_filename(plan):
    """Return the audit filename shared by preview and publication."""
    prefix = _clean_prefix(plan.get("prefix", ""))
    return (re.sub(r"[^A-Za-z0-9._+-]+", "-", prefix) + "-" if prefix else "") + \
        "rules-report-%s.json" % plan["plan_id"][:12]


def publish(reader, plan, output_dir, cancel_check=None, progress=None, *, native_helper=None):
    """Publish exactly one reviewed plan; collisions or failures preserve inputs."""
    organizer._check_cancel(cancel_check)
    if not isinstance(plan, dict) or plan.get("workflow_version") != VERSION:
        raise PoolError("Preview this workflow before creating pools.")
    pinned = dict(plan)
    plan_id = pinned.pop("plan_id", None)
    if not isinstance(plan_id, str) or _fingerprint(pinned) != plan_id:
        raise PoolError("The reviewed plan changed; preview it again.")
    recipe = normalize_recipe(plan.get("recipe"))
    if recipe != plan["recipe"] or _fingerprint(recipe) != plan.get("recipe_id"):
        raise PoolError("The saved rules changed; preview them again.")
    prefix = _clean_prefix(plan.get("prefix", ""))
    if prefix != plan.get("prefix") or _source_pin(reader) != plan.get("source_pin"):
        raise PoolError("The source or output settings changed; preview again.")
    rows = plan.get("outputs")
    if not isinstance(rows, list) or not rows:
        raise PoolError("This preview has no output pools. Adjust the rules or source selection.")
    organizer._check_split_output_limit(len(rows))
    output_dir = os.path.abspath(os.fspath(output_dir))
    os.makedirs(output_dir, exist_ok=True)
    expected = {}
    paths = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("key"), str)
                or not isinstance(row.get("label"), str)
                or type(row.get("records")) is not int or not 0 < row["records"] <= reader.records):
            raise PoolError("The reviewed output list is invalid; preview again.")
        key = row["key"]
        filename = _filename(row["label"], key, prefix)
        if key in expected or filename != row.get("name"):
            raise PoolError("The reviewed output names changed; preview again.")
        expected[key] = row
        paths[key] = os.path.join(output_dir, filename)
    if len(set(os.path.normcase(path) for path in paths.values())) != len(paths):
        raise PoolError("Two outputs have the same filename; change the prefix.")
    report_path = os.path.join(output_dir, report_filename(plan))
    classifier = _Classifier(reader, recipe)
    writers = {}
    publications = []
    report_stage = None
    native_stages = None
    linked = False
    try:
        with ExitStack() as stack:
            source_handle = stack.enter_context(reader._open_source_snapshot(cancel_check))
            for path in sorted(list(paths.values()) + [report_path]):
                if os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.realpath(reader.path)):
                    raise PoolError("An output would replace the source pool.")
                stack.enter_context(organizer.pool_writer_guard(path))
                if os.path.lexists(path):
                    raise PoolError("Output already exists: %s. Choose another prefix or folder." % path)
                if path != report_path:
                    for suffix in builder.POOL_DELETE_SUFFIXES:
                        if suffix and os.path.lexists(path + suffix):
                            raise PoolError("A companion file already exists: %s. Choose another prefix or folder." % (path + suffix))

            def consume(record, key, label):
                if key not in expected or expected[key]["label"] != label:
                    raise PoolError("Rules produced an output missing from the preview; preview again.")
                if key not in writers:
                    size, builder = _header_builder(reader, key, label, plan)
                    writers[key] = organizer.BSP4OutputWriter(
                        reader, key, label, paths[key], header_bytes=size,
                        header_builder=builder)
                writers[key].add(record)

            if native_helper is not None and recipe["mode"] == "separate_sources":
                try:
                    import pool_rule_native
                except ImportError:
                    from tools import pool_rule_native
                # A digest-only check cannot establish the composite set
                # expression. Reused preview readers have this proof. A fresh
                # reader first tries the same semantic native preview.
                if not reader._composite_metadata_verified:
                    try:
                        pool_rule_native.preview_sources(
                            reader, recipe, native_helper, cancel_check, progress)
                    except organizer.NativeSplitUnsupported:
                        pass
                reader._verify_all_payloads(cancel_check)
                try:
                    native_stages = pool_rule_native.stage_sources(
                        reader, plan, paths, native_helper,
                        header_factory=lambda key, label: _header_builder(
                            reader, key, label, plan, minimum_size=reader.header_bytes),
                        cancel_check=cancel_check, progress=progress)
                except organizer.NativeSplitUnsupported:
                    pass
            if native_stages is not None:
                outputs = native_stages.outputs
                publications.extend(native_stages.publications)
            else:
                actual = _scan(reader, classifier, prefix, cancel_check, progress, consume)
                if any(actual[name] != plan.get(name) for name in actual):
                    raise PoolError("Results differ from the reviewed counts; preview again.")
                outputs = []
                for key in sorted(writers):
                    organizer._check_cancel(cancel_check)
                    output = writers[key].finalize()
                    # Verify every staged file before any destination is visible.
                    staged = organizer.BSPoolReader(
                        writers[key].temp_path, cancel_check=cancel_check)
                    if (staged.records != expected[key]["records"]
                            or staged.snapshot_token != output["snapshot_id"]
                            or staged.membership_digest != writers[key].membership_digest
                            or staged.metadata_digest != writers[key].metadata_digest):
                        raise PoolError("A staged pool failed verification; no pools were published.")
                    outputs.append(output)
                    publications.append((writers[key].temp_path, paths[key]))
            report = dict(plan)
            report.update({"outputs": outputs, "reviewed_outputs": rows,
                           "report_path": report_path, "completed": True,
                           "engine": "native" if native_stages is not None else "python"})
            descriptor, report_stage = tempfile.mkstemp(
                prefix=".pool-rules-report-", suffix=".tmp", dir=output_dir)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(report, handle, indent=2, ensure_ascii=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            publications.append((report_stage, report_path))
            organizer._check_cancel(cancel_check)
            reader._assert_source_unchanged(source_handle)
            organizer.seed_pool_mutations.link_many_no_overwrite(publications)
            linked = True
            organizer._check_cancel(cancel_check)
        # Keep stages until the snapshot context closes so late source changes
        # can roll back only the exact files created by this publication.
        for staged, _final in publications:
            try:
                organizer.seed_pool_mutations.remove(staged, missing_ok=True)
            except OSError:
                pass
        return report, True
    except BaseException:
        if linked:
            for staged, final in reversed(publications):
                organizer.seed_pool_mutations.rollback_link(staged, final)
        for writer in writers.values():
            writer.abort()
        if report_stage:
            organizer.seed_pool_mutations.remove(report_stage, missing_ok=True)
        raise
    finally:
        if native_stages is not None:
            native_stages.cleanup()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    detect = sub.add_parser("sources", help="List recorded source memberships")
    detect.add_argument("pool")
    inspect = sub.add_parser("preview", help="Preview a saved workflow")
    inspect.add_argument("pool")
    inspect.add_argument("recipe")
    inspect.add_argument("plan")
    inspect.add_argument("--prefix", default="")
    create = sub.add_parser("publish", help="Create pools from a reviewed preview")
    create.add_argument("pool")
    create.add_argument("plan")
    create.add_argument("output_dir")
    args = parser.parse_args(argv)
    try:
        reader = organizer.BSPoolReader(args.pool, verify_payloads=False)
        if args.command == "sources":
            result = describe_source(reader)
        elif args.command == "preview":
            recipe = _load_json(args.recipe, MAX_RECIPE_BYTES)
            result = preview(reader, recipe, args.prefix)
            # CLI previews are shareable JSON files. Refuse existing paths,
            # including the source, rather than replacing a pool accidentally.
            destination = os.path.abspath(args.plan)
            if not destination.lower().endswith(".json"):
                raise PoolError("Save the preview to a new .json file.")
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            with organizer.pool_writer_guard(destination):
                if os.path.lexists(destination):
                    raise PoolError("Preview file already exists; choose a new .json filename.")
                descriptor, temporary = tempfile.mkstemp(
                    prefix=".pool-rules-preview-", suffix=".tmp",
                    dir=os.path.dirname(destination))
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                        json.dump(result, handle, indent=2, ensure_ascii=True)
                        handle.write("\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                    organizer.seed_pool_mutations.link_no_overwrite(temporary, destination)
                finally:
                    organizer.seed_pool_mutations.remove(temporary, missing_ok=True)
        else:
            plan = _load_json(args.plan, MAX_PLAN_BYTES)
            result, _completed = publish(reader, plan, args.output_dir)
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return 0
    except (OSError, ValueError, PoolError) as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
