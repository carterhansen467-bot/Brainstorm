#!/usr/bin/env python3
"""Point-and-click UI for the Brainstorm seed pool organizer.

The server binds to 127.0.0.1 and uses only the Python standard library.  It
organizes the committed checkpoint of a BSP3 pool; an unfinished writer tail
is never read.  Every plan is pinned to the source snapshot id so a paused
scan cannot silently grow underneath the user's category choices.
"""

from __future__ import print_function

import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import webbrowser
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import brainstorm_pool_organizer as organizer


# A one-file Windows build extracts Python modules to a temporary directory.
# Current releases place Organizer.exe in <mod>/Seed Pool Builder/, while
# source launches live in <mod>/tools/. Find the parent by stable mod markers
# instead of assuming either the install folder name or executable layout.
IS_FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if IS_FROZEN \
    else SCRIPT_DIR


def _looks_like_mod_dir(path):
    return (os.path.isfile(os.path.join(path, "Brainstorm_main.lua"))
            and os.path.isfile(os.path.join(path, "manifest.json")))


def _find_mod_dir():
    candidates = []
    override = os.environ.get("BRAINSTORM_MOD_DIR", "").strip()
    if override:
        candidates.append(os.path.abspath(os.path.expanduser(override)))
    current = APP_DIR
    for _ in range(4):
        candidates.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for candidate in candidates:
        if _looks_like_mod_dir(candidate):
            return candidate
    return APP_DIR if IS_FROZEN else os.path.dirname(SCRIPT_DIR)


MOD_DIR = _find_mod_dir()
POOL_DIR = os.path.join(MOD_DIR, "seed_pools")
DEFAULT_PORT = 8918
MAX_REQUEST_BYTES = 64 * 1024 * 1024
SPLIT_LOCK = threading.Lock()
COMBINE_LOCK = threading.Lock()
MAX_COMBINE_INPUTS = organizer.COMPOSITE_MAX_INPUTS


def _pool_root(pool_dir=None):
    return os.path.abspath(pool_dir or POOL_DIR)


def resolve_source(name, pool_dir=None):
    """Resolve one top-level pool name without allowing path traversal."""
    root = _pool_root(pool_dir)
    if not isinstance(name, str) or not name or name != os.path.basename(name):
        raise organizer.PoolError("choose a pool from the Seed Pool folder")
    if not name.lower().endswith(".bspool"):
        raise organizer.PoolError("the selected file is not a .bspool")
    path = os.path.abspath(os.path.join(root, name))
    if os.path.commonpath((root, path)) != root or not os.path.isfile(path):
        raise organizer.PoolError("the selected pool no longer exists")
    # Do not let a top-level symlink turn the local server into an arbitrary
    # filesystem reader.  A shared pool can be copied into seed_pools first.
    if os.path.commonpath((os.path.realpath(root), os.path.realpath(path))) \
            != os.path.realpath(root):
        raise organizer.PoolError("the selected pool points outside the Seed Pool folder")
    return path


def _bounded_header(path):
    text = organizer.read_pool_header_text(path)
    if not text:
        raise organizer.PoolError("cannot read a bounded Brainstorm pool header")
    return organizer.PoolHeader(text)


def list_sources(pool_dir=None):
    """Return cheap header-only rows; full checksum verification is inspect."""
    root = _pool_root(pool_dir)
    rows = []
    if not os.path.isdir(root):
        return rows
    for name in sorted(os.listdir(root), key=str.lower):
        if not name.lower().endswith(".bspool"):
            continue
        try:
            path = resolve_source(name, root)
            header = _bounded_header(path)
            schema = int(header.one("BRAINSTORM_SEED_POOL"))
            records = header.integer("records")
            complete = bool(header.integer("complete"))
            coverage = bool(header.integer(
                "coverage_complete", required=False, default=int(complete)))
            rows.append({
                "name": name,
                "bytes": os.path.getsize(path),
                "schema": schema,
                "records": records,
                "complete": complete,
                "coverage_complete": coverage,
                "metadata_capable": schema == 3,
                "modelver": header.integer("modelver"),
                "catalog_hash": header.one("catalog_hash"),
                "criteria_hash": header.one("criteria_hash"),
                "space": header.one("space", required=False, default="natural"),
                "range_start": header.integer("range_start"),
                "range_end": header.integer("range_end"),
                "snapshot_id": header.one(
                    "snapshot_id", required=False, default=""),
                "label": header.one("label", required=False, default=""),
                "composite": bool(header.values.get("composite_schema")),
                "composite_operation": header.one(
                    "composite_operation", required=False, default=""),
                "composite_branch_count": len(
                    header.values.get("composite_branch", [])),
                "composite_operand_count": len(
                    header.values.get("composite_operand", [])),
                "error": "",
            })
        except (OSError, ValueError, organizer.PoolError) as exc:
            rows.append({"name": name, "error": str(exc)})
    return rows


def _notices(source):
    notices = []
    if not source["complete"]:
        notices.append({
            "kind": "warning",
            "title": "Paused or incomplete source",
            "text": ("Only the %s committed seeds in snapshot %s will be used. "
                     "Any unfinished writer tail is ignored. If the scan resumes, "
                     "inspect again before using this plan.") % (
                         format(source["records"], ","), source["snapshot_id"]),
        })
    if not source["coverage_complete"]:
        notices.append({
            "kind": "warning",
            "title": "Provisional coverage",
            "text": ("Derivative pools will remain coverage-incomplete. They are "
                     "valid pools of the currently recorded seeds, not a claim that "
                     "the source search range was fully covered."),
        })
    if not source["metadata_capable"]:
        notices.append({
            "kind": "error",
            "title": "Position metadata unavailable",
            "text": ("BSP2 can be inspected and exported, but it cannot be split by "
                     "exact tag, Legendary, or voucher location. Refilter or rescan "
                     "it into BSP3 first."),
        })
    if source.get("family_id") or source.get("lineage_id"):
        notices.append({
            "kind": "info",
            "title": "Lineage is preserved",
            "text": ("Every output retains the source family, records snapshot %s as "
                     "its parent, and receives a category-specific derived lineage.")
                    % source["snapshot_id"],
        })
    return notices


def inspect_source(name, pool_dir=None, ambiguity_limit=100):
    reader = organizer.BSPoolReader(resolve_source(name, pool_dir))
    report = organizer.analyze(reader, ambiguity_limit=ambiguity_limit)
    report["notices"] = _notices(report["source"])
    return report


def _selected_categories(reader, selected_ids):
    summary = organizer.analyze(reader, ambiguity_limit=0)
    available = {row["category_id"]: row for row in summary["categories"]}
    if selected_ids is None:
        selected = set(available)
    else:
        if not isinstance(selected_ids, list) or not all(
                isinstance(item, str) for item in selected_ids):
            raise organizer.PoolError("selectedCategories must be a list")
        unknown = sorted(set(selected_ids) - set(available))
        if unknown:
            raise organizer.PoolError(
                "selected category is absent from snapshot: %s" % unknown[0])
        selected = set(selected_ids)
    if not selected:
        raise organizer.PoolError("select at least one exact category")
    return summary, available, selected


def _choice_document(value, reader):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise organizer.PoolError("choice plan must be a JSON object")
    if "choices" in value:
        snapshot = str(value.get("source_snapshot_id", "")).lower()
        if snapshot != reader.snapshot_token:
            raise organizer.PoolError(
                "choice plan is for snapshot %s; inspect current snapshot %s" % (
                    snapshot or "(missing)", reader.snapshot_token))
        value = value["choices"]
    if not isinstance(value, dict):
        raise organizer.PoolError("choice plan needs an object-valued choices field")
    choices = {}
    for key, category in value.items():
        if not isinstance(key, str) or not isinstance(category, str):
            raise organizer.PoolError("choice keys and category ids must be strings")
        if category:
            choices[key] = category
    return choices


def build_split_plan(reader, selected_ids=None, choice_plan=None):
    """Build a no-write split plan for the UI."""
    summary, available, selected = _selected_categories(reader, selected_ids)
    choices = _choice_document(choice_plan, reader)
    used_choices = set()
    ambiguous = []
    unmatched = 0
    counts = {}
    for category in selected:
        counts[category] = 0
    for record in reader.iter_records():
        candidates = organizer.record_categories(record, selected)
        for category in candidates:
            counts[category] += 1
        if not candidates:
            unmatched += 1
            continue
        if len(candidates) <= 1:
            extra, choice_key = organizer.choice_for_record(choices, reader, record)
            if extra is not None:
                used_choices.add(choice_key)
                if extra != candidates[0]:
                    raise organizer.PoolError(
                        "choice for unambiguous seed %s conflicts with its category"
                        % reader.seed(record.rank))
            continue
        chosen, choice_key = organizer.choice_for_record(choices, reader, record)
        if choice_key:
            used_choices.add(choice_key)
        if chosen is not None and chosen not in candidates:
            raise organizer.PoolError(
                "choice for %s is not one of that seed's selected categories"
                % reader.seed(record.rank))
        ambiguous.append({
            "seed": reader.seed(record.rank),
            "rank": record.rank,
            "candidates": candidates,
            "choice": chosen or "",
        })
    unused = sorted(set(choices) - used_choices)
    if unused:
        raise organizer.PoolError(
            "choice plan contains a seed/rank not used by this split: %s" % unused[0])
    categories = []
    for category in sorted(selected):
        row = dict(available[category])
        row["records"] = counts[category]
        categories.append(row)
    source = organizer.source_summary(reader)
    return {
        "organizer_schema": 1,
        "source": source,
        "source_snapshot_id": reader.snapshot_token,
        "selected_categories": sorted(selected),
        "categories": categories,
        "ambiguous_count": len(ambiguous),
        "unresolved_ambiguities": sum(not row["choice"] for row in ambiguous),
        "ambiguous": ambiguous,
        "choices": {row["seed"]: row["choice"] for row in ambiguous},
        "unmatched_count": unmatched,
        "opaque_associations": summary["opaque_associations"],
        "notices": _notices(source),
    }


def sanitize_prefix(value, source_name):
    value = str(value or "").strip()
    if not value:
        value = os.path.splitext(source_name)[0] + "-organized"
    if value.lower().endswith(".bspool"):
        value = value[:-7]
    value = re.sub(r"[^A-Za-z0-9._+-]+", "-", value).strip("-.")
    if not value:
        raise organizer.PoolError("output prefix needs a letter or number")
    return value[:64]


def _unique_report_path(pool_dir, prefix, snapshot):
    stem = "%s-%s-split-report" % (prefix, snapshot[:8])
    candidate = os.path.join(pool_dir, stem + ".json")
    suffix = 2
    while os.path.exists(candidate):
        candidate = os.path.join(pool_dir, "%s-%d.json" % (stem, suffix))
        suffix += 1
    return candidate


def execute_split(name, request, pool_dir=None):
    """Verify a pinned plan, split in staging, and atomically publish outputs."""
    root = _pool_root(pool_dir)
    os.makedirs(root, exist_ok=True)
    source_path = resolve_source(name, root)
    reader = organizer.BSPoolReader(source_path)
    expected = str(request.get("snapshot", "")).lower()
    if expected != reader.snapshot_token:
        raise organizer.PoolError(
            "source changed from snapshot %s to %s; inspect it again" % (
                expected or "(missing)", reader.snapshot_token))
    selected = request.get("selectedCategories")
    choice_plan = request.get("choicePlan")
    # Validate the whole plan before creating any files.
    build_split_plan(reader, selected, choice_plan)
    choices = _choice_document(choice_plan, reader)
    policy = str(request.get("unmatchedPolicy", "stop"))
    if policy not in ("stop", "remainder", "omit"):
        raise organizer.PoolError("unknown unmatched-seed policy")
    remainder = None
    omit = False
    if policy == "remainder":
        remainder = str(request.get("remainderName", "Needs review")).strip()
        if not remainder:
            raise organizer.PoolError("give the review/remainder pool a name")
    elif policy == "omit":
        omit = True
    prefix = sanitize_prefix(request.get("prefix"), name)
    stage = tempfile.mkdtemp(prefix=".organizer-stage-", dir=root)
    linked = []
    publish_locks = ExitStack()
    try:
        choices_path = os.path.join(stage, "choices.json")
        organizer.atomic_json(choices_path, {
            "source_snapshot_id": reader.snapshot_token,
            "choices": choices,
        })
        stage_report = os.path.join(stage, "split-report.json")
        report, completed = organizer.split_pool(
            reader, stage, selected, choices_path, stage_report,
            remainder, omit)
        report["notices"] = _notices(report["source"])
        if not completed:
            report["completed"] = False
            return report

        publications = []
        for output in report["outputs"]:
            old_path = output["path"]
            final_name = prefix + "--" + os.path.basename(old_path)
            final_path = os.path.join(root, final_name)
            publish_locks.enter_context(organizer.pool_writer_guard(final_path))
            if os.path.exists(final_path):
                raise organizer.PoolError(
                    "output already exists; choose another prefix: %s" % final_name)
            publications.append((old_path, final_path, output))
        for old_path, final_path, _output in publications:
            # Staging and seed_pools share a filesystem.  link() is atomic and
            # refuses overwrite; rollback below removes only links made here.
            os.link(old_path, final_path)
            linked.append(final_path)
        for _old_path, final_path, output in publications:
            output["path"] = final_path
            output["name"] = os.path.basename(final_path)
        report["completed"] = True
        report_path = _unique_report_path(root, prefix, reader.snapshot_token)
        report["report_path"] = report_path
        organizer.atomic_json(report_path, report)
        for old_path, _final_path, _output in publications:
            os.unlink(old_path)
        linked = []  # published files now belong to the user
        return report
    except Exception:
        for path in linked:
            try:
                os.unlink(path)
            except OSError:
                pass
        raise
    finally:
        publish_locks.close()
        shutil.rmtree(stage, ignore_errors=True)


def _combine_source_names(value):
    if (not isinstance(value, list) or len(value) < 2
            or len(value) > MAX_COMBINE_INPUTS
            or not all(isinstance(item, str) for item in value)):
        raise organizer.PoolError(
            "select between 2 and %d source pools" % MAX_COMBINE_INPUTS)
    names = []
    for name in value:
        if name in names:
            raise organizer.PoolError("select each source pool only once")
        names.append(name)
    return names


def _combine_operation(value):
    operation = str(value or "union").lower()
    if operation not in organizer.COMPOSITE_OPERATIONS:
        raise organizer.PoolError("unknown combine operation")
    return operation


def _ordered_combine_names(request):
    names = _combine_source_names(request.get("sources"))
    operation = _combine_operation(request.get("operation"))
    if operation == "difference":
        base = str(request.get("base") or "")
        if base not in names:
            raise organizer.PoolError(
                "choose which selected pool is the Difference base")
        names = [base] + [name for name in names if name != base]
    return names, operation


def _combine_readers(request, pool_dir=None, pin_snapshots=False):
    names, operation = _ordered_combine_names(request)
    expected = request.get("snapshots", {})
    if pin_snapshots and not isinstance(expected, dict):
        raise organizer.PoolError("combined-pool snapshot pins are malformed")
    readers = []
    for name in names:
        reader = organizer.BSPoolReader(resolve_source(name, pool_dir))
        if pin_snapshots:
            pinned = str(expected.get(name, "")).lower()
            if pinned != reader.snapshot_token:
                raise organizer.PoolError(
                    "%s changed from snapshot %s to %s; check compatibility again" % (
                        name, pinned or "(missing)", reader.snapshot_token))
        readers.append(reader)
    return names, operation, readers


def _combine_notices(context):
    notices = []
    if len({branch.criteria_hash for branch in context.branches}) > 1:
        notices.append({
            "kind": "info",
            "title": "Different base filters preserved",
            "text": ("The output uses %s semantics and records exactly which "
                     "source-filter branch admitted each seed. Source criteria "
                     "are not incorrectly stacked into one AND filter.") %
                    context.operation.upper(),
        })
    if not context.coverage_complete:
        notices.append({
            "kind": "warning",
            "title": "Provisional combined coverage",
            "text": ("The operation still uses every currently committed seed. "
                     "At least one source is paused/provisional or the source "
                     "ranges differ, so the output will not claim exhaustive coverage."),
        })
    if not context.metadata_complete:
        notices.append({
            "kind": "warning",
            "title": "Some exact occurrence metadata is unavailable",
            "text": ("At least one BSP2 source predates per-seed locations. Its "
                     "membership and source provenance are preserved, but exact "
                     "tag/Legendary/voucher locations cannot be reconstructed."),
        })
    if context.operation == "difference":
        notices.append({
            "kind": "info",
            "title": "Difference keeps only the base",
            "text": ("The first/base pool is kept, then every seed found in any "
                     "other selected pool is removed."),
        })
    return notices


def build_combine_plan(request, pool_dir=None):
    names, operation, readers = _combine_readers(request, pool_dir)
    context = organizer.prepare_combine(readers, operation)
    report = context.as_dict()
    report.update({
        "organizer_schema": 2,
        "source_names": [os.path.basename(reader.path)
                         for reader in context.readers],
        "selected_names": names,
        "snapshots": {
            os.path.basename(reader.path): reader.snapshot_token
            for reader in readers
        },
        "notices": _combine_notices(context),
    })
    return report


def sanitize_combine_name(value):
    value = str(value or "combined-pool").strip()
    if value.lower().endswith(".bspool"):
        value = value[:-7]
    value = re.sub(r"[^A-Za-z0-9._+-]+", "-", value).strip("-.")
    if not value:
        raise organizer.PoolError("combined pool name needs a letter or number")
    return value[:96]


def execute_combine(request, pool_dir=None):
    root = _pool_root(pool_dir)
    os.makedirs(root, exist_ok=True)
    _names, operation, readers = _combine_readers(
        request, root, pin_snapshots=True)
    # Recompute the plan from the pinned reader instances immediately before
    # writing, so compatibility and output semantics cannot drift between the
    # preview and publication steps.
    context = organizer.prepare_combine(readers, operation)
    output_name = sanitize_combine_name(request.get("name"))
    output_path = os.path.join(root, output_name + ".bspool")
    label = str(request.get("label") or output_name).strip() or output_name
    result = organizer.combine_pools(
        context.readers, output_path, operation, label)
    result["name"] = os.path.basename(output_path)
    result["notices"] = _combine_notices(context)
    if not result["records"]:
        result["notices"].append({
            "kind": "warning",
            "title": "The result is empty",
            "text": ("The operation was valid and found no shared/remaining "
                     "seeds. Keep the file as a verified result or combine it "
                     "again; an empty pool cannot be searched in-game."),
        })
    report_path = os.path.join(root, output_name + "-combine-report.json")
    if os.path.exists(report_path):
        suffix = 2
        while os.path.exists(os.path.join(
                root, "%s-combine-report-%d.json" % (output_name, suffix))):
            suffix += 1
        report_path = os.path.join(
            root, "%s-combine-report-%d.json" % (output_name, suffix))
    result["report_path"] = report_path
    try:
        organizer.atomic_json(report_path, result)
    except Exception:
        # Publication is one user action: never leave a seemingly successful
        # pool behind when its pinned provenance report could not be saved.
        try:
            os.unlink(result["path"])
        except OSError:
            pass
        raise
    return result


def iter_record_export(reader):
    for record in reader.iter_records():
        value = {
            "seed": reader.seed(record.rank),
            "rank": record.rank,
            "occurrences": [item.as_dict() for item in record.occurrences],
        }
        yield (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")


PAGE = r'''<!doctype html>
<html><head><meta charset="utf-8">
<title>Brainstorm Seed Pool Organizer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{color-scheme:dark;--bg:#0c0e14;--card:#171a25;--card2:#11141d;--line:#30364b;
 --text:#f4f1fa;--muted:#aaa6b6;--faint:#7c788a;--gold:#f4c84d;--blue:#77baff;
 --green:#56da8b;--red:#ff7979;--purple:#9b89ff;--shadow:0 18px 50px #0006}
*{box-sizing:border-box}body{margin:0;min-height:100vh;color:var(--text);
 background:radial-gradient(circle at 10% -10%,#342954 0,transparent 34rem),
 radial-gradient(circle at 95% 0,#17344b 0,transparent 30rem),var(--bg);
 font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
button,select,input{font:inherit}.app{width:min(1120px,calc(100% - 30px));margin:auto;padding:32px 0 70px}
.top{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin-bottom:22px}
.brand{display:flex;gap:14px;align-items:center}.mark{display:grid;place-items:center;width:48px;height:48px;
 border-radius:15px;background:linear-gradient(145deg,#f8d35c,#c99122);color:#211906;font-size:25px;font-weight:900}
h1{font-size:clamp(23px,3vw,32px);line-height:1.1;margin:0}.sub{margin:7px 0 0;color:var(--muted);font-size:14px}
.local{padding:8px 12px;border:1px solid #2d5943;border-radius:999px;background:#14251d;color:#a8e7bf;
 font-size:12px;font-weight:750;white-space:nowrap}.local:before{content:"";display:inline-block;width:8px;height:8px;
 margin-right:8px;border-radius:50%;background:var(--green)}
.appnav{display:flex;gap:7px;margin:-8px 0 18px;padding:5px;width:max-content;border:1px solid #34394d;border-radius:12px;background:#10131c}
.appnav a{padding:8px 13px;border-radius:8px;color:var(--muted);font-size:12px;font-weight:800;text-decoration:none}
.appnav a.active{background:#29213b;color:#ded6ff}.toolnav{display:flex;gap:8px;margin-bottom:18px}
.toolnav button{background:#171b27;border-color:#373d54;color:#aaa6b6}.toolnav button.active{background:#3a3155;border-color:#61548a;color:#eee9ff}
.privacy{margin-bottom:18px;padding:12px 15px;border:1px solid #3c3650;border-radius:12px;background:#191624cc;color:#cac5d6;font-size:13px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px;align-items:start}.stack{display:grid;gap:18px}
.card{padding:21px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(180deg,#191c28f5,#141722f5);box-shadow:var(--shadow)}
.head{display:flex;gap:12px;margin-bottom:17px}.step{flex:0 0 auto;display:grid;place-items:center;width:30px;height:30px;border:1px solid #443963;border-radius:9px;background:#29213b;color:#c7baff;font-weight:800}
h2{margin:0;font-size:17px}.copy{margin:3px 0 0;color:var(--muted);font-size:13px}.field{display:grid;gap:6px;margin-top:12px}label,.label{font-size:12px;font-weight:750;color:#cbc6d5}
select,input[type=text]{width:100%;min-height:42px;padding:9px 11px;border:1px solid #3b4159;border-radius:10px;background:#0f1119;color:var(--text)}
select[multiple]{min-height:190px}.poolchoices{display:grid;gap:7px;max-height:390px;overflow:auto;margin-top:10px;padding-right:3px}
.poolchoice{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;padding:11px;border:1px solid #292e40;border-radius:10px;background:var(--card2);cursor:pointer}
.poolchoice:hover{border-color:#4a526f}.poolchoice input{width:17px;height:17px;accent-color:var(--purple)}.poolchoice b{display:block;font-size:12px;overflow-wrap:anywhere}.poolchoice small{display:block;color:var(--faint);font-size:10px}.poolchoice .count{font-size:11px}
.combine-settings{display:grid;grid-template-columns:1fr 1fr;gap:12px}.branchlist{display:grid;gap:7px;margin-top:12px}.branch{padding:9px 10px;border:1px solid #2d3245;border-radius:9px;background:#11141d;font-size:11px}.branch b{display:block}.branch span{color:var(--faint);overflow-wrap:anywhere}
button{min-height:40px;padding:8px 14px;border:1px solid transparent;border-radius:10px;background:#3b5fd1;color:white;font-weight:750;cursor:pointer}
button:hover:not(:disabled){filter:brightness(1.12);transform:translateY(-1px)}button:disabled{background:#292d3c;color:#777482;cursor:not-allowed}
button.go{background:linear-gradient(135deg,#27814c,#35aa62)}button.ghost{background:transparent;border-color:#3b425b;color:#d7d2df}button.small{min-height:33px;padding:5px 9px;background:#292e42;border-color:#3d435c;font-size:12px}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-top:13px}.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.source{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:13px}.metric{padding:11px;border:1px solid #292e40;border-radius:11px;background:var(--card2)}
.metric span{display:block;color:var(--faint);font-size:11px}.metric b{display:block;margin-top:3px;overflow-wrap:anywhere}.mono{font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
.notice{margin-top:10px;padding:11px 12px;border:1px solid #355273;border-radius:11px;background:#142131;color:#b9dafb;font-size:12px}.notice.warning{border-color:#6a5726;background:#29230f;color:#f2d67c}.notice.error{border-color:#74383e;background:#2b171b;color:#ffb5b5}.notice strong{display:block;margin-bottom:2px;color:inherit}
.categories{display:grid;gap:7px;max-height:370px;overflow:auto;margin-top:12px;padding-right:3px}.category{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:start;padding:10px;border:1px solid #292e40;border-radius:10px;background:var(--card2)}
.category input{width:17px;height:17px;margin-top:2px;accent-color:var(--purple)}.category b{font-size:12px}.category span{color:var(--faint);font-size:11px}.count{color:#d9d4e4!important;white-space:nowrap}
.side{position:sticky;top:18px;display:grid;gap:18px}.summary dl{margin:11px 0 0}.summary div{display:grid;grid-template-columns:95px 1fr;gap:9px;padding:9px 0;border-top:1px solid #302c40;font-size:12px}.summary dt{color:var(--faint)}.summary dd{margin:0;text-align:right;overflow-wrap:anywhere}
.actions{display:grid;gap:9px;margin-top:14px}.actions button{width:100%}.error{margin-top:10px;color:#ffadad;font-size:13px;white-space:pre-wrap}.hint{color:var(--faint);font-size:12px}
.plan{margin-top:18px}.planbar{display:flex;justify-content:space-between;gap:12px;align-items:center}.pill{padding:4px 8px;border-radius:999px;background:#3b3017;color:#f4d46b;font-size:10px;font-weight:850;text-transform:uppercase}.pill.ok{background:#163424;color:#80e4a6}
.amb{display:grid;gap:8px;margin-top:12px}.ambrow{display:grid;grid-template-columns:100px 1fr;gap:10px;align-items:center;padding:10px;border:1px solid #292e40;border-radius:10px;background:var(--card2)}.ambrow b{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.ambrow select{min-height:38px}
.pager{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-top:11px;color:var(--muted);font-size:12px}.result{margin-top:13px}.output{padding:10px 0;border-top:1px solid #302c40;font-size:12px}.output b{display:block;overflow-wrap:anywhere}.output span{color:var(--muted)}
[hidden]{display:none!important}@media(max-width:850px){.grid{grid-template-columns:1fr}.side{position:static;grid-row:1}}@media(max-width:580px){.top{display:grid}.two,.source,.combine-settings{grid-template-columns:1fr}.ambrow{grid-template-columns:1fr}.card{padding:17px}.appnav{width:100%}.appnav a{flex:1;text-align:center}}
</style></head><body><main class="app">
<header class="top"><div class="brand"><div class="mark">B</div><div><h1>Seed Pool Organizer</h1><p class="sub">Inspect, split, and combine recorded seed pools without rerunning their searches.</p></div></div><div class="local">Running locally</div></header>
<nav class="appnav"><a id="builderTab" href="/">Build / Search</a><a id="organizerTab" class="active" href="/organize">Organize / Combine</a></nav>
<div class="privacy"><strong>Your pools stay on this computer.</strong> This page only talks to Brainstorm's local organizer. Outputs are published into <code>seed_pools</code> so the mod can see them immediately.</div>
<div class="toolnav"><button class="active" id="splitModeBtn">Split by recorded location</button><button id="combineModeBtn">Combine seed pools</button></div>
<div class="grid" id="splitWorkspace"><div class="stack">
<section class="card"><div class="head"><span class="step">1</span><div><h2>Inspect a recorded pool</h2><p class="copy">Both finished and paused BSP3 pools are supported. Only the committed checkpoint is read.</p></div></div>
 <div class="field"><label for="source">Seed pool</label><select id="source"></select></div>
 <div class="row"><button class="go" id="inspectBtn">Inspect pool</button><button class="ghost" id="refreshBtn">Refresh list</button></div>
 <div id="sourceInfo" hidden><div class="source" id="sourceMetrics"></div><div id="notices"></div></div>
</section>
<section class="card" id="categoryCard" hidden><div class="head"><span class="step">2</span><div><h2>Choose exact categories</h2><p class="copy">A category includes the recorded item, Ante, blind, source, occurrence, and flags.</p></div></div>
 <div class="row"><button class="small" id="allBtn">Select all</button><button class="small" id="noneBtn">Select none</button><button class="ghost" id="exportBtn">Export every seed and occurrence (.ndjson)</button></div>
 <div class="categories" id="categories"></div>
</section>
<section class="card" id="planCard" hidden><div class="head"><span class="step">3</span><div><h2>Plan the split</h2><p class="copy">Seeds in several selected categories need one explicit destination. A saved plan only works with this exact snapshot.</p></div></div>
 <div class="two"><div class="field"><label for="policy">Seeds outside selected categories</label><select id="policy"><option value="stop">Stop and ask me</option><option value="remainder">Put in a review pool</option><option value="omit">Explicitly leave them out</option></select></div>
 <div class="field" id="remainderField" hidden><label for="remainder">Review pool label</label><input type="text" id="remainder" value="Needs review"></div></div>
 <div class="field"><label for="prefix">Output filename prefix</label><input type="text" id="prefix"><span class="hint">Unique prefixes let different source pools use the same exact categories.</span></div>
 <div class="row"><button id="planBtn">Prepare split plan</button><button class="ghost" id="saveBtn" disabled>Save choice plan</button><button class="ghost" id="loadBtn">Load choice plan</button><input type="file" id="loadFile" accept="application/json,.json" hidden></div>
 <div class="plan" id="plan" hidden><div class="planbar"><div><b id="planTitle">Choices</b><div class="hint" id="planHint"></div></div><span class="pill" id="choicePill">Needs choices</span></div><div class="amb" id="ambiguities"></div><div class="pager" id="pager"></div></div>
</section></div>
<aside class="side"><section class="card summary"><h2>Organizer summary</h2><dl>
 <div><dt>Source</dt><dd id="sumSource">Choose a pool</dd></div><div><dt>Snapshot</dt><dd id="sumSnapshot">—</dd></div><div><dt>Committed</dt><dd id="sumRecords">—</dd></div><div><dt>Categories</dt><dd id="sumCategories">—</dd></div><div><dt>Ambiguous</dt><dd id="sumAmbiguous">—</dd></div><div><dt>Unmatched</dt><dd id="sumUnmatched">—</dd></div></dl>
 <div class="actions"><button class="go" id="splitBtn" disabled>Create organized pools</button></div><div class="error" id="error" role="alert"></div><div class="result" id="result"></div>
</section></aside></div>
<div class="grid" id="combineWorkspace" hidden><div class="stack">
<section class="card"><div class="head"><span class="step">1</span><div><h2>Select pools to combine</h2><p class="copy">Pools may have different base filters. Finished, paused, and previously combined pools are supported; only committed records are read.</p></div></div>
 <div class="row"><button class="small" id="combineAllBtn">Select all readable pools</button><button class="small" id="combineNoneBtn">Select none</button><button class="ghost" id="combineRefreshBtn">Refresh list</button></div>
 <div class="poolchoices" id="combineChoices"><div class="hint">Loading seed pools…</div></div>
</section>
<section class="card"><div class="head"><span class="step">2</span><div><h2>Choose the set operation</h2><p class="copy">Source filters remain separate branches, so a union means A OR B—not one accidental combined AND filter.</p></div></div>
 <div class="combine-settings"><div class="field"><label for="combineOperation">Operation</label><select id="combineOperation"><option value="union">Union · keep seeds in any selected pool</option><option value="intersection">Intersection · keep seeds in every selected pool</option><option value="difference">Difference · keep base minus the others</option></select></div>
 <div class="field" id="combineBaseField" hidden><label for="combineBase">Base pool to keep from</label><select id="combineBase"></select></div>
 <div class="field"><label for="combineName">Output filename</label><input type="text" id="combineName" value="combined-pool"></div>
 <div class="field"><label for="combineLabel">Display label</label><input type="text" id="combineLabel" value="Combined seed pool"></div></div>
 <div class="row"><button id="combinePlanBtn">Check compatibility</button></div><div id="combineNotices"></div>
 <div id="combineBranches" class="branchlist"></div>
</section></div>
<aside class="side"><section class="card summary"><h2>Combine summary</h2><dl>
 <div><dt>Operation</dt><dd id="combineSumOperation">Union</dd></div><div><dt>Inputs</dt><dd id="combineSumInputs">Choose at least two</dd></div><div><dt>Source filters</dt><dd id="combineSumBranches">—</dd></div><div><dt>Expression</dt><dd id="combineSumExpression">—</dd></div><div><dt>Coverage</dt><dd id="combineSumCoverage">—</dd></div><div><dt>Metadata</dt><dd id="combineSumMetadata">—</dd></div></dl>
 <div class="actions"><button class="go" id="combineCreateBtn" disabled>Create combined pool</button></div><div class="error" id="combineError" role="alert"></div><div class="result" id="combineResult"></div>
</section></aside></div></main>
<script>
const $=id=>document.getElementById(id);const UNIFIED=location.pathname.startsWith("/organize");
const apiPath=path=>UNIFIED?"/organizer"+path:path;
let inspected=null,plan=null,choices={},page=0,poolRows=[],combinePlan=null;const PAGE_SIZE=100;
const esc=v=>String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const fmt=n=>Number(n||0).toLocaleString();
async function api(path,data){const opt=data?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}:{};const r=await fetch(apiPath(path),opt);const v=await r.json();if(!r.ok||v.error)throw Error(v.error||`Request failed (${r.status})`);return v}
function fail(e){$("error").textContent=e.message||String(e)}function clear(){$("error").textContent="";$("result").innerHTML=""}
async function loadPools(){clear();const v=await api("/api/pools");poolRows=v.pools||[];const old=$("source").value;$("source").innerHTML=poolRows.length?poolRows.map(p=>`<option value="${esc(p.name)}" ${p.error?"disabled":""}>${esc(p.name)}${p.error?" · unreadable":` · ${fmt(p.records)} seeds${p.complete?"":" · paused"}`}</option>`).join(""):'<option value="">No .bspool files found</option>';if([...$("source").options].some(o=>o.value===old))$("source").value=old;renderCombineChoices()}
function notices(rows){$("notices").innerHTML=(rows||[]).map(n=>`<div class="notice ${esc(n.kind)}"><strong>${esc(n.title)}</strong>${esc(n.text)}</div>`).join("")}
function sourceMetrics(s){return `<div class="metric"><span>Committed seeds</span><b>${fmt(s.records)}</b></div><div class="metric"><span>Pool state</span><b>${s.complete?"Finished":"Paused / incomplete"}</b></div><div class="metric"><span>Coverage</span><b>${s.coverage_complete?"Complete":"Provisional"}</b></div><div class="metric"><span>Format</span><b>BSP${s.schema}${s.metadata_capable?" · exact metadata":" · no exact metadata"}</b></div><div class="metric"><span>Snapshot</span><b class="mono">${esc(s.snapshot_id)}</b></div><div class="metric"><span>Lineage</span><b class="mono">${esc(s.lineage_id||"legacy / unrecorded")}</b></div>`}
function categoryHTML(c){return `<label class="category"><input type="checkbox" class="cat" value="${esc(c.category_id)}" checked><span><b>${esc(c.label)}</b><br><span class="mono">${esc(c.category_id)}</span></span><span class="count">${fmt(c.records)}</span></label>`}
async function inspect(){clear();const source=$("source").value;if(!source)return;try{const v=await api("/api/inspect",{source});inspected=v;plan=null;choices={};page=0;$("sourceInfo").hidden=false;$("sourceMetrics").innerHTML=sourceMetrics(v.source);notices(v.notices);$("categoryCard").hidden=false;$("planCard").hidden=!v.source.metadata_capable;$("categories").innerHTML=v.categories.length?v.categories.map(categoryHTML).join(""):'<div class="hint">This snapshot has no known exact categories.</div>';$("prefix").value=source.replace(/\.bspool$/i,"")+"-organized";$("sumSource").textContent=source;$("sumSnapshot").textContent=v.source.snapshot_id;$("sumRecords").textContent=fmt(v.source.records);$("sumCategories").textContent=fmt(v.category_count);$("sumAmbiguous").textContent=fmt(v.ambiguous_count);$("sumUnmatched").textContent=fmt(v.unmatched_count);$("plan").hidden=true;$("saveBtn").disabled=true;$("splitBtn").disabled=true}catch(e){fail(e)}}
function selected(){return [...document.querySelectorAll(".cat:checked")].map(x=>x.value)}
function choiceDoc(){return {source_snapshot_id:inspected.source.snapshot_id,choices:{...choices},selected_categories:selected()}}
async function prepare(){clear();if(!inspected)return;try{const v=await api("/api/plan",{source:$("source").value,snapshot:inspected.source.snapshot_id,selectedCategories:selected(),choicePlan:choiceDoc()});plan=v;choices={...v.choices};page=0;renderPlan();$("saveBtn").disabled=false}catch(e){fail(e)}}
function unresolved(){return !plan?0:plan.ambiguous.filter(a=>!choices[a.seed]).length}
function categoryName(id){const c=(plan?plan.categories:inspected.categories).find(x=>x.category_id===id);return c?c.label:id}
function renderPlan(){if(!plan)return;$("plan").hidden=false;const total=plan.ambiguous.length,pages=Math.max(1,Math.ceil(total/PAGE_SIZE));page=Math.max(0,Math.min(page,pages-1));const rows=plan.ambiguous.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE);$("ambiguities").innerHTML=rows.length?rows.map(a=>`<div class="ambrow"><b>${esc(a.seed)}</b><select data-seed="${esc(a.seed)}"><option value="">Choose one destination…</option>${a.candidates.map(c=>`<option value="${esc(c)}" ${choices[a.seed]===c?"selected":""}>${esc(categoryName(c))}</option>`).join("")}</select></div>`).join(""):'<div class="notice"><strong>No category conflicts</strong>Every matching seed has one selected destination.</div>';document.querySelectorAll(".ambrow select").forEach(s=>s.onchange=()=>{choices[s.dataset.seed]=s.value;renderStatus()});$("pager").innerHTML=total?`<button class="small" id="prev" ${page===0?"disabled":""}>Previous</button><span>Seeds ${fmt(page*PAGE_SIZE+1)}–${fmt(Math.min(total,(page+1)*PAGE_SIZE))} of ${fmt(total)}</span><button class="small" id="next" ${page>=pages-1?"disabled":""}>Next</button>`:"";if($("prev"))$("prev").onclick=()=>{page--;renderPlan()};if($("next"))$("next").onclick=()=>{page++;renderPlan()};renderStatus()}
function renderStatus(){if(!plan)return;const left=unresolved();$("planTitle").textContent=`${fmt(plan.ambiguous_count)} multi-category seed${plan.ambiguous_count===1?"":"s"}`;$("planHint").textContent=plan.unmatched_count?`${fmt(plan.unmatched_count)} seed(s) are outside the selected categories; choose their policy above.`:"Every source seed matches at least one selected category.";$("choicePill").textContent=left?`${fmt(left)} choices left`:"Choices complete";$("choicePill").className="pill"+(left?"":" ok");$("sumAmbiguous").textContent=fmt(plan.ambiguous_count);$("sumUnmatched").textContent=fmt(plan.unmatched_count);const policyReady=!plan.unmatched_count||$("policy").value!=="stop";$("splitBtn").disabled=!!left||!policyReady}
function download(name,type,text){const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([text],{type}));a.download=name;document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},0)}
function savePlan(){if(!plan)return;download(`${$("prefix").value||"seed-pool"}-choices.json`,"application/json",JSON.stringify(choiceDoc(),null,2)+"\n")}
function loadPlanFile(file){const r=new FileReader();r.onload=()=>{try{const v=JSON.parse(r.result);if(!inspected)throw Error("Inspect the source pool first.");if(String(v.source_snapshot_id||"").toLowerCase()!==inspected.source.snapshot_id)throw Error("That plan belongs to a different committed snapshot.");choices={...(v.choices||{})};prepare()}catch(e){fail(e)}};r.readAsText(file)}
async function split(){clear();if(!plan)return;$("splitBtn").disabled=true;$("splitBtn").textContent="Creating pools…";try{const v=await api("/api/split",{source:$("source").value,snapshot:inspected.source.snapshot_id,selectedCategories:selected(),choicePlan:choiceDoc(),unmatchedPolicy:$("policy").value,remainderName:$("remainder").value,prefix:$("prefix").value});if(!v.completed){plan=v;choices={...v.choices};renderPlan();throw Error("The split still needs choices or an unmatched-seed policy.")}$("result").innerHTML=`<div class="notice"><strong>Created ${fmt(v.outputs.length)} organized pool(s)</strong>They are ready in seed_pools. Report: ${esc(v.report_path)}</div>`+v.outputs.map(o=>`<div class="output"><b>${esc(o.name)}</b><span>${fmt(o.records)} seeds · lineage ${esc(o.lineage_id)}</span></div>`).join("");await loadPools()}catch(e){fail(e)}finally{$("splitBtn").textContent="Create organized pools";renderStatus()}}
function renderNoticeList(id,rows){$(id).innerHTML=(rows||[]).map(n=>`<div class="notice ${esc(n.kind)}"><strong>${esc(n.title)}</strong>${esc(n.text)}</div>`).join("")}
function combineSelected(){return [...document.querySelectorAll(".combinePick:checked")].map(x=>x.value)}
function renderCombineChoices(){
 const selected=new Set(combineSelected());
 $("combineChoices").innerHTML=poolRows.length?poolRows.map(p=>{
  const state=p.error?"Unreadable":p.complete?(p.coverage_complete?"Finished":"Provisional"):"Paused / incomplete";
  const extra=p.composite?` · ${esc(p.composite_operation||"composite")} · ${fmt(p.composite_operand_count||p.composite_branch_count)} inputs · ${fmt(p.composite_branch_count)} source filters`:p.criteria_hash?` · filter ${esc(p.criteria_hash.slice(0,8))}`:"";
  return `<label class="poolchoice"><input type="checkbox" class="combinePick" value="${esc(p.name)}" ${selected.has(p.name)?"checked":""} ${p.error?"disabled":""}><span><b>${esc(p.name)}</b><small>${esc(state)} · BSP${esc(p.schema||"?")}${extra}</small></span><span class="count">${p.error?"—":fmt(p.records)}</span></label>`;
 }).join(""):'<div class="hint">No .bspool files found.</div>';
 document.querySelectorAll(".combinePick").forEach(input=>input.onchange=()=>{updateCombineBase();invalidateCombine()});
 updateCombineBase();
}
function updateCombineBase(){
 const names=combineSelected(),old=$("combineBase").value;
 $("combineBase").innerHTML=names.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join("");
 if(names.includes(old))$("combineBase").value=old;
 $("combineBaseField").hidden=$("combineOperation").value!=="difference";
 $("combineSumInputs").textContent=names.length?`${fmt(names.length)} selected`:"Choose at least two";
 $("combineSumOperation").textContent=$("combineOperation").selectedOptions[0].textContent.split(" · ")[0];
}
function invalidateCombine(){combinePlan=null;$("combineCreateBtn").disabled=true;$("combineBranches").innerHTML="";$("combineNotices").innerHTML="";$("combineSumBranches").textContent="—";$("combineSumExpression").textContent="—";$("combineSumCoverage").textContent="—";$("combineSumMetadata").textContent="—";$("combineResult").innerHTML=""}
function combineRequest(withPins){const value={sources:combineSelected(),operation:$("combineOperation").value,base:$("combineBase").value,name:$("combineName").value,label:$("combineLabel").value};if(withPins&&combinePlan)value.snapshots=combinePlan.snapshots;return value}
function renderCombinePlan(v){
 combinePlan=v;renderNoticeList("combineNotices",v.notices);$("combineSumInputs").textContent=`${fmt(v.operand_count)} exact pool snapshots`;$("combineSumBranches").textContent=fmt(v.branch_count);$("combineSumExpression").textContent=v.expression_text;$("combineSumCoverage").textContent=v.coverage_complete?"Complete":"Provisional";$("combineSumMetadata").textContent=v.metadata_complete?"Exact locations preserved":"Some membership only";
 const inputs=v.operands.map((o,index)=>`<div class="branch"><b>Input ${fmt(index+1)} · ${esc(o.label||o.pool_id||o.operand_id)}</b><span>${fmt(o.records)} seeds · snapshot ${esc(o.snapshot_id.slice(0,8))} · operand ${esc(o.operand_id.slice(0,8))}</span></div>`).join("");
 const sources=v.branches.map(b=>`<div class="branch"><b>Source filter · ${esc(b.label||b.pool_id||b.branch_id)}</b><span>source ${esc(b.branch_id.slice(0,8))} · filter ${esc(b.criteria_hash.slice(0,8))}${b.criteria.length?` · ${b.criteria.map(esc).join(" · ")}`:" · no embedded criteria"}</span></div>`).join("");
 $("combineBranches").innerHTML=`<div class="notice"><strong>Exact snapshot expression</strong>${esc(v.expression_text)}</div><div class="hint">Exact input snapshots</div>${inputs}<div class="hint">Original source filters retained per seed</div>${sources}`;
 $("combineCreateBtn").disabled=false;
}
async function checkCombine(){
 $("combineError").textContent="";$("combineResult").innerHTML="";const button=$("combinePlanBtn");button.disabled=true;button.textContent="Checking…";
 try{const v=await api("/api/combine/plan",combineRequest(false));renderCombinePlan(v)}catch(e){invalidateCombine();$("combineError").textContent=e.message||String(e)}finally{button.disabled=false;button.textContent="Check compatibility"}
}
async function createCombine(){
 if(!combinePlan)return;$("combineError").textContent="";const button=$("combineCreateBtn");button.disabled=true;button.textContent="Combining…";
 try{const v=await api("/api/combine",combineRequest(true));renderNoticeList("combineNotices",v.notices);const empty=v.records?"":" This is a verified empty result and cannot be searched in-game.";$("combineResult").innerHTML=`<div class="notice"><strong>Created ${esc(v.name)}</strong>${fmt(v.records)} unique seed(s) · ${esc(v.operation.toUpperCase())}.${esc(empty)} Report: ${esc(v.report_path)}</div>`;await loadPools();combinePlan=null}catch(e){$("combineError").textContent=e.message||String(e);button.disabled=false}finally{button.textContent="Create combined pool"}
}
function showMode(mode){const combine=mode==="combine";$("splitWorkspace").hidden=combine;$("combineWorkspace").hidden=!combine;$("splitModeBtn").classList.toggle("active",!combine);$("combineModeBtn").classList.toggle("active",combine)}
$("inspectBtn").onclick=inspect;$("refreshBtn").onclick=loadPools;$("allBtn").onclick=()=>document.querySelectorAll(".cat").forEach(x=>x.checked=true);$("noneBtn").onclick=()=>document.querySelectorAll(".cat").forEach(x=>x.checked=false);$("planBtn").onclick=prepare;$("saveBtn").onclick=savePlan;$("loadBtn").onclick=()=>$("loadFile").click();$("loadFile").onchange=e=>e.target.files[0]&&loadPlanFile(e.target.files[0]);$("splitBtn").onclick=split;$("policy").onchange=()=>{$("remainderField").hidden=$("policy").value!=="remainder";renderStatus()};$("exportBtn").onclick=()=>{if(!inspected)return;location.href=apiPath(`/api/export?source=${encodeURIComponent($("source").value)}&snapshot=${encodeURIComponent(inspected.source.snapshot_id)}`)};
$("splitModeBtn").onclick=()=>showMode("split");$("combineModeBtn").onclick=()=>showMode("combine");$("combinePlanBtn").onclick=checkCombine;$("combineCreateBtn").onclick=createCombine;$("combineRefreshBtn").onclick=loadPools;$("combineAllBtn").onclick=()=>{document.querySelectorAll(".combinePick:not(:disabled)").forEach(x=>x.checked=true);updateCombineBase();invalidateCombine()};$("combineNoneBtn").onclick=()=>{document.querySelectorAll(".combinePick").forEach(x=>x.checked=false);updateCombineBase();invalidateCombine()};$("combineOperation").onchange=()=>{updateCombineBase();invalidateCombine()};$("combineBase").onchange=invalidateCombine;
document.addEventListener("change",e=>{if(e.target.classList.contains("cat")){plan=null;choices={};$("plan").hidden=true;$("saveBtn").disabled=true;$("splitBtn").disabled=true}});
if(!UNIFIED){$("builderTab").hidden=true;$("organizerTab").href="/"}loadPools();
</script></body></html>'''


class OrganizerHandler(BaseHTTPRequestHandler):
    pool_dir = POOL_DIR

    def log_message(self, *args):
        pass

    def _json(self, value, code=200):
        body = json.dumps(value).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise organizer.PoolError("invalid request length")
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise organizer.PoolError("organizer request is too large")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except (UnicodeDecodeError, ValueError):
            raise organizer.PoolError("request body is not valid JSON")
        if not isinstance(value, dict):
            raise organizer.PoolError("request body must be a JSON object")
        return value

    def _reader_for_request(self, data):
        name = data.get("source", "")
        reader = organizer.BSPoolReader(resolve_source(name, self.pool_dir))
        expected = str(data.get("snapshot", "")).lower()
        if expected and expected != reader.snapshot_token:
            raise organizer.PoolError(
                "source changed from snapshot %s to %s; inspect it again" % (
                    expected, reader.snapshot_token))
        return reader

    def do_GET(self):
        parsed = urlparse(self.path)
        response_started = False
        try:
            if parsed.path in ("/", "/index.html"):
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                response_started = True
                self.wfile.write(body)
            elif parsed.path == "/api/pools":
                self._json({"pools": list_sources(self.pool_dir)})
            elif parsed.path == "/api/export":
                query = parse_qs(parsed.query)
                name = query.get("source", [""])[0]
                reader = organizer.BSPoolReader(resolve_source(name, self.pool_dir))
                expected = query.get("snapshot", [""])[0].lower()
                if expected != reader.snapshot_token:
                    raise organizer.PoolError(
                        "source snapshot changed; inspect before exporting")
                filename = "%s-%s-records.ndjson" % (
                    os.path.splitext(name)[0], reader.snapshot_token[:8])
                filename = re.sub(r"[^A-Za-z0-9._+-]+", "-", filename)
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Disposition", "attachment; filename=%s" % quote(filename))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                response_started = True
                for line in iter_record_export(reader):
                    self.wfile.write(line)
            else:
                self._json({"error": "not found"}, 404)
        except (OSError, ValueError, organizer.PoolError) as exc:
            # Export may already have sent headers only after all validation;
            # every other branch can return a regular JSON error.
            if response_started:
                return
            self._json({"error": str(exc)}, 400)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = self._body()
            if parsed.path == "/api/inspect":
                self._json(inspect_source(data.get("source", ""), self.pool_dir))
            elif parsed.path == "/api/plan":
                reader = self._reader_for_request(data)
                self._json(build_split_plan(
                    reader, data.get("selectedCategories"), data.get("choicePlan")))
            elif parsed.path == "/api/combine/plan":
                self._json(build_combine_plan(data, self.pool_dir))
            elif parsed.path == "/api/split":
                if not SPLIT_LOCK.acquire(False):
                    raise organizer.PoolError("another organizer split is still running")
                try:
                    report = execute_split(data.get("source", ""), data, self.pool_dir)
                finally:
                    SPLIT_LOCK.release()
                self._json(report)
            elif parsed.path == "/api/combine":
                if not COMBINE_LOCK.acquire(False):
                    raise organizer.PoolError("another pool combine is still running")
                try:
                    report = execute_combine(data, self.pool_dir)
                finally:
                    COMBINE_LOCK.release()
                self._json(report)
            else:
                self._json({"error": "not found"}, 404)
        except (OSError, ValueError, organizer.PoolError) as exc:
            self._json({"error": str(exc)}, 400)


def make_handler(pool_dir):
    class BoundOrganizerHandler(OrganizerHandler):
        pass
    BoundOrganizerHandler.pool_dir = os.path.abspath(pool_dir)
    return BoundOrganizerHandler


def main():
    os.makedirs(POOL_DIR, exist_ok=True)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", DEFAULT_PORT), OrganizerHandler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), OrganizerHandler)
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Brainstorm Seed Pool Organizer is running at %s" % url)
    print("Leave this window open while using the organizer. Ctrl+C quits.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        socket.setdefaulttimeout(30)
        sys.exit(main())
    except Exception as exc:
        print("Unexpected organizer error: %s" % exc, file=sys.stderr)
        if sys.stdin.isatty():
            input("Press Return to close...")
        sys.exit(1)
