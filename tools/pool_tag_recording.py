"""Record physical tag placements without filtering or replacing a source pool."""

import os
import re
import tempfile
from contextlib import ExitStack

try:
    import brainstorm_pool_builder as builder
    import brainstorm_pool_organizer as organizer
    import pool_rule_native as native
    import pool_rule_workflow as workflow
    import pool_tag_rules as tags
except ImportError:
    from tools import brainstorm_pool_builder as builder
    from tools import brainstorm_pool_organizer as organizer
    from tools import pool_rule_native as native
    from tools import pool_rule_workflow as workflow
    from tools import pool_tag_rules as tags


def recording_range(recipe):
    recipe = workflow.normalize_recipe(recipe)
    if recipe["mode"] != "second_tag":
        raise organizer.PoolError("Choose Second tag before recording tag placements.")
    rule = recipe["rule"]
    ranges = [tags._range_pair(rule["range"])]
    ranges.extend(pair for _, pair in tags._condition_requirements(rule.get("condition")))
    return min(pair[0] for pair in ranges), max(pair[1] for pair in ranges)


def _read_result(text):
    if not isinstance(text, str) or len(text) > 8192:
        raise organizer.PoolError("Tag recording returned an invalid result.")
    lines = text.splitlines()
    if (not lines or lines[0] != "BRAINSTORM_TAG_RECORDING_RESULT 1"
            or lines[-1] != "end"):
        raise organizer.PoolError("Tag recording returned an incomplete result.")
    counts = {"source_records", "output_records"}
    digests = {prefix + "_" + kind + "_digest"
               for prefix in ("source", "output") for kind in ("membership", "metadata")}
    result = {}
    for line in lines[1:-1]:
        parts = line.split()
        if (len(parts) != 2 or parts[0] not in counts | digests
                or parts[0] in result):
            raise organizer.PoolError("Tag recording returned a malformed field.")
        key, value = parts
        if not re.fullmatch(r"[0-9]{1,20}" if key in counts else r"[0-9a-f]{16}", value):
            raise organizer.PoolError("Tag recording returned a malformed number.")
        result[key] = int(value, 10 if key in counts else 16)
        if result[key] > organizer.MASK64:
            raise organizer.PoolError("Tag recording counter overflowed.")
    if set(result) != counts | digests or result["source_records"] != result["output_records"]:
        raise organizer.PoolError("Tag recording did not retain every source seed.")
    return result


def _verify(reader, helper, cancel_check, progress):
    if reader.is_composite:
        return native.preview_sources(reader, workflow.normalize_recipe({
            "version": 1, "mode": "separate_sources", "source_kind": "inputs"}),
            helper, cancel_check, progress)["counts"]
    summary = helper.summarize(reader.path, cancel_check=cancel_check)
    if not summary or summary["records"] != reader.records:
        raise organizer.PoolError("Cannot verify the tag-recording pool.")
    reader.accept_native_verification(int(summary["membership_digest"], 16),
                                      int(summary["metadata_digest"], 16))
    return None


def record_tags(reader, recipe, output_dir, snapshot_path, helper, prefix="",
                cancel_check=None, progress=None, phase=None):
    """Publish a new, verified copy with complete Negative/Rare range evidence.

    The supplied Snapshot is copied privately before evaluation. Its exact
    profile/catalog must match the pool. The source and final name are pinned
    through publication; only the newly linked file can be rolled back.
    """
    organizer._check_cancel(cancel_check)
    first, last = recording_range(recipe)
    prefix = workflow._clean_prefix(prefix)
    if not callable(getattr(helper, "record_tags", None)):
        raise organizer.PoolError("Install the latest full package to record tag placements.")
    if reader.schema not in (3, 4) or not reader.complete or not reader.occurrence_metadata_complete:
        raise organizer.PoolError("Tag recording needs a finished BSP3/BSP4 pool with saved metadata.")
    if reader._repaired_bsp3_headers or reader.blocks.physical_rank_order() != (True, True):
        raise organizer.PoolError("Upgrade this pool to BSP4 before recording tag placements.")
    stem = prefix or os.path.splitext(os.path.basename(reader.path))[0]
    stem = re.sub(r"[^A-Za-z0-9._+-]+", "-", stem).strip("-.")[:100] or "pool"
    filename = "%s-tag-data-%s-%s.bspool" % (
        stem, tags.position_token(first).lower(), tags.position_token(last).lower())
    output_dir = os.path.abspath(os.fspath(output_dir))
    output = os.path.join(output_dir, filename)
    os.makedirs(output_dir, exist_ok=True)
    directory = tempfile.TemporaryDirectory(prefix=".pool-tag-data-", dir=output_dir)
    stage = os.path.join(directory.name, filename)
    linked = False
    def set_phase(value):
        if phase:
            phase(value)
    try:
        with ExitStack() as stack:
            source_handle = stack.enter_context(reader._open_source_snapshot(cancel_check))
            stack.enter_context(organizer.pool_writer_guard(output))
            if os.path.realpath(output) == os.path.realpath(reader.path):
                raise organizer.PoolError("The tag-data copy must have a new filename.")
            if any(os.path.lexists(output + suffix) for suffix in builder.POOL_DELETE_SUFFIXES):
                raise organizer.PoolError("The tag-data filename already exists. Choose another output prefix.")
            private_snapshot = os.path.join(directory.name, "snapshot.cfg")
            try:
                with open(snapshot_path, "rb") as source_snapshot:
                    content = source_snapshot.read(16 * 1024 * 1024 + 1)
            except OSError:
                raise organizer.PoolError(
                    "A matching game profile snapshot is needed. Open the profile used to build "
                    "this pool in Balatro, toggle Ctrl+A on and off, then try again.") from None
            if len(content) > 16 * 1024 * 1024:
                raise organizer.PoolError("The game profile snapshot is too large.")
            with open(private_snapshot, "wb") as handle:
                handle.write(content)
            builder.Snapshot(private_snapshot).current_model_copy()
            if builder.catalog_hash_file(private_snapshot) != "%016x" % reader.catalog_hash:
                raise organizer.PoolError(
                    "The current game profile snapshot differs from the one used to build this pool. "
                    "Use its original native_search.cfg or refresh the snapshot from the matching "
                    "Balatro profile. No seeds were changed.")
            set_phase("verifying_source")
            memberships = _verify(reader, helper, cancel_check, progress)
            set_phase("recording_tags")
            response = helper.record_tags(private_snapshot, reader.path, stage, first, last,
                                          cancel_check=cancel_check, progress=progress)
            organizer._check_cancel(cancel_check)
            result = _read_result(response)
            if (result["source_records"] != reader.records
                    or result["source_membership_digest"] != reader.membership_digest
                    or result["source_metadata_digest"] != reader.metadata_digest):
                raise organizer.PoolError("Tag recording used a different source snapshot.")
            set_phase("verifying_output")
            recorded = organizer.BSPoolReader(stage, verify_payloads=False, cancel_check=cancel_check)
            if (recorded.schema != 4 or not recorded.complete or recorded.records != reader.records
                    or recorded.modelver != reader.modelver or recorded.catalog_hash != reader.catalog_hash
                    or recorded.charset != reader.charset or recorded.criteria_hash != reader.criteria_hash
                    or recorded.coverage_complete != reader.coverage_complete
                    or recorded.composite_branches != reader.composite_branches
                    or recorded.composite_operands != reader.composite_operands
                    or recorded.composite_expression != reader.composite_expression
                    or recorded.range_start != reader.range_start or recorded.range_end != reader.range_end):
                raise organizer.PoolError("Tag recording changed the source membership or pool history.")
            if _verify(recorded, helper, cancel_check, progress) != memberships:
                raise organizer.PoolError("Tag recording changed original-pool memberships.")
            if (result["output_membership_digest"] != recorded.membership_digest
                    or result["output_metadata_digest"] != recorded.metadata_digest):
                raise organizer.PoolError("The recorded tag-data copy failed digest verification.")
            # BSP4 hashes canonical ranks independently of metadata/blocks.
            # BSP3 hashes encoded blocks; native recording separately verifies
            # its canonical source-rank digest against the BSP4 output digest.
            if reader.schema == 4 and reader.membership_digest != recorded.membership_digest:
                raise organizer.PoolError("Tag recording changed the source seed ranks.")
            organizer._check_cancel(cancel_check)
            reader._assert_source_unchanged(source_handle)
            organizer.seed_pool_mutations.link_many_no_overwrite([(stage, output)])
            linked = True
            organizer._check_cancel(cancel_check)
        return {"source": filename, "path": output, "records": reader.records,
                "range": {"start": tags.position_token(first), "end": tags.position_token(last)},
                "snapshot_id": recorded.snapshot_token, "completed": True, "engine": "native"}
    except BaseException as exc:
        if linked:
            organizer.seed_pool_mutations.rollback_link(stage, output)
        if isinstance(exc, organizer.NativeSplitUnsupported):
            raise organizer.PoolError(
                "This helper cannot record tag placements for this pool. "
                "Install the latest full package and use a finished BSP4 copy.") from None
        raise
    finally:
        try:
            directory.cleanup()
        except OSError:
            pass
