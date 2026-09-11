"""Native record copying for reviewed source-membership recovery workflows.

Python owns the recipe, source validation, headers, and publication. The existing
native split command copies matching records with every descriptor intact. Its
output summaries verify bytes and source-marker membership before this module
returns any stage to the caller. This module never publishes a destination.
"""

import json
import os
import re
import tempfile
from dataclasses import asdict

try:
    import brainstorm_pool_organizer as organizer
except ImportError:
    from tools import brainstorm_pool_organizer as organizer


class NativeStages:
    """Private files retained until publication or rollback has finished."""

    def __init__(self, directory, outputs, publications):
        self._directory = directory
        self.outputs = outputs
        self.publications = publications

    def cleanup(self):
        try:
            self._directory.cleanup()
        except OSError:
            # Windows antivirus may briefly retain a private staging file.
            # Cleanup must not turn a completed publication into a failure or
            # hide its original cancellation/error. No final path lives here.
            pass


def _unsupported(message):
    raise organizer.NativeSplitUnsupported(message)


def _prepare(reader, plan, destinations, header_factory):
    recipe = plan.get("recipe", {})
    if recipe.get("mode") != "separate_sources":
        _unsupported("native workflow copying supports source recovery only")
    if (reader.schema not in (3, 4) or not reader.is_composite
            or reader._repaired_bsp3_headers):
        _unsupported("this source requires the Python workflow writer")
    # A native digest check alone does not establish composite semantics.
    # Python's full preview traversal establishes this separate flag.
    if not getattr(reader, "_composite_metadata_verified", False):
        _unsupported("source memberships require Python validation first")
    if reader.blocks.physical_rank_order() != (True, True):
        _unsupported("source blocks require Python rank ordering")
    rows = plan.get("outputs")
    if not isinstance(rows, list) or not rows:
        raise organizer.PoolError("The reviewed recovery has no output pools.")
    if len(rows) > 256:
        _unsupported("native source recovery supports at most 256 outputs")
    kind = recipe.get("source_kind")
    if kind not in ("inputs", "branches"):
        raise organizer.PoolError("The reviewed source membership kind is invalid.")
    definitions = (reader.composite_operands if kind == "inputs"
                   else reader.composite_branches)
    selected = set(recipe.get("source_ids") or
                   ("%016x" % value for value in definitions))
    prepared = []
    keys = set()
    for row in rows:
        key = row.get("key", "") if isinstance(row, dict) else ""
        match = re.fullmatch(r"source:(inputs|branches):([0-9a-f]{16})", key)
        if (not match or match[1] != kind or match[2] not in selected
                or int(match[2], 16) not in definitions or key in keys
                or not isinstance(row.get("label"), str)
                or type(row.get("records")) is not int
                or not 0 < row["records"] <= reader.records):
            raise organizer.PoolError("The reviewed source output is invalid.")
        keys.add(key)
        size, build = header_factory(key, row["label"])
        if size != reader.header_bytes:
            _unsupported("saved workflow needs a different output header size")
        prepared.append((row, match[2], build))
    if keys != set(destinations):
        raise organizer.PoolError("Source recovery destinations differ from the preview.")
    folders = {os.path.dirname(os.path.abspath(path))
               for path in destinations.values()}
    if len(folders) != 1:
        raise organizer.PoolError("Source recovery outputs must share one folder.")
    pin = plan.get("source_pin", {})
    if (pin.get("path") != reader.path
            or pin.get("file_identity") != asdict(reader._source_identity)
            or pin.get("snapshot_id") != reader.snapshot_token
            or pin.get("records") != reader.records
            or pin.get("membership_digest") != "%016x" % reader.membership_digest
            or pin.get("metadata_digest") != "%016x" % reader.metadata_digest):
        raise organizer.PoolError("The source changed; preview recovery again.")
    return kind, prepared, folders.pop()


def _check_result(reader, plan, prepared, result):
    if (result["source_records"] != reader.records
            or result["source_membership_digest"] != "%016x" % reader.membership_digest
            or result["source_metadata_digest"] != "%016x" % reader.metadata_digest):
        raise organizer.PoolError("Native recovery read a different source snapshot.")
    if (result["used_choices"] or result["used_rules"]
            or "unused_choice" in result or "unused_rule" in result):
        raise organizer.PoolError("Native recovery returned unexpected decisions.")
    if (result["unmatched"] != plan.get("excluded_records")
            or result["overlap"] != plan.get("overlap_records")
            or result["unique_copied"] != plan.get("copied_records")
            or result["output_memberships"] != plan.get("output_memberships")
            or len(result["outputs"]) != len(prepared)
            or any(result["outputs"][index]["records"] != row["records"]
                   for index, (row, _token, _build) in enumerate(prepared))):
        raise organizer.PoolError("Native recovery differs from the reviewed counts.")


def _verify_stage(reader, plan, row, token, kind, path, native, identity,
                  helper, cancel_check):
    staged = organizer.BSPoolReader(path, verify_payloads=False,
                                   cancel_check=cancel_check)
    if (staged.schema != 4 or not staged.complete or staged.coverage_complete
            or staged.records != row["records"]
            or staged.data_bytes != native["data_bytes"]
            or staged.membership_digest != native["membership_digest"]
            or staged.metadata_digest != native["metadata_digest"]
            or staged.header_bytes != reader.header_bytes
            or staged.snapshot_token != identity["snapshot_id"]
            or not staged.is_composite
            or staged.composite_branches != reader.composite_branches
            or staged.composite_operands != reader.composite_operands
            or staged.composite_expression != reader.composite_expression
            or staged.occurrence_metadata_complete != reader.occurrence_metadata_complete
            or staged.header.one("workflow_recipe_id") != plan["recipe_id"]
            or staged.header.one("workflow_plan_id") != plan["plan_id"]
            or organizer._decode_header_token(staged.header.one("workflow_destination")) != row["key"]
            or json.loads(organizer._decode_header_token(staged.header.one("workflow_recipe"))) != plan["recipe"]):
        raise organizer.PoolError("A native recovery stage has incorrect metadata or identity.")
    summary = helper.summarize(path, cancel_check=cancel_check)
    organizer._check_cancel(cancel_check)
    if (summary is None or summary["records"] != row["records"]
            or int(summary["membership_digest"], 16) != native["membership_digest"]
            or int(summary["metadata_digest"], 16) != native["metadata_digest"]):
        raise organizer.PoolError("A native recovery stage failed byte verification.")
    counts = summary["operand_counts" if kind == "inputs" else "provenance_counts"]
    if (counts.get(token) != row["records"]
            or summary["records_without_provenance"]
            or summary["records_without_operands"]
            or set(summary["provenance_counts"]) -
                {"%016x" % value for value in reader.composite_branches}
            or set(summary["operand_counts"]) -
                {"%016x" % value for value in reader.composite_operands}):
        raise organizer.PoolError("A native recovery stage lost source memberships.")
    # The native writer copies all raw descriptors from each selected record;
    # it never rewrites their branch/operand sets. Thus the already-validated
    # source expression also holds in each output. Re-reading every output in
    # Python would repeat that proof once for every overlapping source.


def stage_sources(reader, plan, destinations, helper, *, header_factory,
                  cancel_check=None, progress=None):
    """Stage native copies from a Python-validated, reviewed source recovery.

    The caller validates the full plan and holds source and destination locks
    through final publication. ``header_factory(key, label)`` returns exactly
    the source header size and a builder accepting records/data bytes/digests.
    A larger required header, unsupported layout/helper, or unvalidated source
    raises ``NativeSplitUnsupported`` for a clean Python fallback. Other errors
    are failures, never fallback. Returned stages must be cleaned *after* any
    publication rollback, since rollback uses the retained stage identities.
    """
    organizer._check_cancel(cancel_check)
    kind, prepared, folder = _prepare(reader, plan, destinations, header_factory)
    directory = tempfile.TemporaryDirectory(prefix=".pool-rules-native-", dir=folder)
    try:
        paths = [os.path.join(directory.name, "output-%d.tmp" % index)
                 for index in range(len(prepared))]
        lines = [b"BRAINSTORM_SPLIT_PLAN 1", b"mode matching_copies",
                 b"header_bytes %d" % reader.header_bytes]
        for index, ((row, token, _build), path) in enumerate(zip(prepared, paths)):
            lines.append(b"output %d %s %s" % (
                index, row["key"].encode("ascii"), organizer._native_plan_path_bytes(path)))
            raw = ("81" if kind == "inputs" else "80") + token
            lines.append(b"descriptor %s %d" % (raw.encode("ascii"), index))
        lines.append(b"end")
        plan_path = os.path.join(directory.name, "split-plan.txt")
        with open(plan_path, "wb") as handle:
            handle.write(b"\n".join(lines) + b"\n")
        outputs = []
        publications = []
        with reader._open_source_snapshot(cancel_check) as source_handle:
            result = organizer.parse_native_split_result(helper.split(
                reader.path, plan_path, cancel_check=cancel_check, progress=progress))
            organizer._check_cancel(cancel_check)
            _check_result(reader, plan, prepared, result)
            for index, ((row, token, build), path) in enumerate(zip(prepared, paths)):
                organizer._check_cancel(cancel_check)
                native = result["outputs"][index]
                header, identity = build(native["records"], native["data_bytes"],
                                         native["membership_digest"], native["metadata_digest"])
                if len(header) != reader.header_bytes:
                    raise organizer.PoolError("Native recovery output header has an incorrect size.")
                with open(path, "r+b") as handle:
                    if handle.read(reader.header_bytes) != b"\0" * reader.header_bytes:
                        raise organizer.PoolError("Native recovery did not leave a blank output header.")
                    handle.seek(0)
                    handle.write(header)
                    handle.flush()
                    os.fsync(handle.fileno())
                _verify_stage(reader, plan, row, token, kind, path, native,
                              identity, helper, cancel_check)
                identity.update({"path": destinations[row["key"]],
                                 "records": row["records"], "category_id": row["key"]})
                outputs.append(identity)
                publications.append((path, destinations[row["key"]]))
            organizer._check_cancel(cancel_check)
            reader._assert_source_unchanged(source_handle)
        return NativeStages(directory, outputs, publications)
    except BaseException:
        try:
            directory.cleanup()
        except OSError:
            # Preserve the original failure; only private, unpublished stages
            # can remain when Windows still holds one of these paths open.
            pass
        raise
