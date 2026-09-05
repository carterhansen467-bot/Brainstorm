#!/usr/bin/env python3
"""Point-and-click UI for the Brainstorm seed pool organizer.

The server binds to 127.0.0.1 and uses only the Python standard library.  It
organizes the committed checkpoint of a BSP3 or BSP4 event pool; an unfinished
writer tail is never read.  Every plan is pinned to the source snapshot id so
a paused scan cannot silently grow underneath the user's category choices.
Both event schemas are accepted by the native scanner/searcher.
"""

from __future__ import print_function

import atexit
import collections
import copy
import errno
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import brainstorm_pool_organizer as organizer
split_policy = organizer.split_policy
seed_pool_mutations = organizer.seed_pool_mutations


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
AMBIGUITY_SAMPLE_LIMIT = 500
AMBIGUITY_GROUP_LIMIT = 100
RECORD_EXPORT_CHUNK_BYTES = 1024 * 1024
# NDJSON repeats field names and human-readable occurrence labels that are
# compactly encoded in a pool.  This projection is intentionally approximate:
# a record has about 96 bytes of fixed JSON/seed/rank framing and committed
# block data commonly expands by roughly 16x when rendered as text.
RECORD_EXPORT_BASE_BYTES = 96
RECORD_EXPORT_DATA_EXPANSION = 16
RECORD_EXPORT_HUGE_BYTES = 1024 * 1024 * 1024
SPLIT_LOCK = threading.Lock()
COMBINE_LOCK = threading.Lock()
ACTIVE_OPERATION_LOCK = threading.Lock()
ACTIVE_OPERATIONS = {}
ACTIVE_OPERATIONS_CLOSING = False
RECORD_EXPORT_STATUS = collections.OrderedDict()
RECORD_EXPORT_STATUS_LIMIT = 32
ACTIVE_UPGRADE_PROCESS_LOCK = threading.Lock()
ACTIVE_UPGRADE_PROCESSES = set()
READER_CACHE_LOCK = threading.Lock()
READER_CACHE = collections.OrderedDict()
MAX_COMBINE_INPUTS = organizer.COMPOSITE_MAX_INPUTS
# A reviewed combine can contain MAX_COMBINE_INPUTS immutable readers. Keep
# the whole reviewed set so its execute pass does not evict the remaining
# readers while walking the same source order a second time, but only while
# their retained indexes fit a low-end-safe aggregate memory budget.
READER_CACHE_LIMIT = MAX_COMBINE_INPUTS
READER_CACHE_MAX_BYTES = 64 * 1024 * 1024
REVIEWED_SPLIT_CACHE_LOCK = threading.Lock()
REVIEWED_SPLIT_CACHE = collections.OrderedDict()
REVIEWED_SPLIT_CACHE_LIMIT = 8
NATIVE_SUMMARY_MIN_BYTES = 32 * 1024 * 1024
SUMMARY_CACHE_SCHEMA = 2
_POOL_BINARY_NAME = "brainstorm_seed_pool" + (".exe" if os.name == "nt" else "")
_POOL_BINARY_CANDIDATES = [
    os.path.join(APP_DIR, _POOL_BINARY_NAME),
    os.path.join(APP_DIR, "native", _POOL_BINARY_NAME),
    os.path.join(MOD_DIR, "Seed Pool Builder", _POOL_BINARY_NAME),
    os.path.join(MOD_DIR, "native", _POOL_BINARY_NAME),
]


class OperationCancelled(organizer.PoolError):
    """A user-requested cancellation that the HTTP UI can classify safely."""


class FormatPlanStale(organizer.PoolError):
    """The checked source/output contract changed before publication."""


class FormatUpdateFailedSafely(organizer.PoolError):
    """A staged format update failed before any pool could be published."""


class FormatSourceDamaged(FormatUpdateFailedSafely):
    """The native verifier found inconsistent committed source bytes."""


def _operation_cancelled(cancel_check):
    if cancel_check is not None and cancel_check():
        raise OperationCancelled("operation cancelled")


def _plan_token(kind, value):
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "%016x" % organizer.fnv64(
        ("organizer-plan:%s:" % kind).encode("ascii") + payload.encode("ascii"))


def _group_by_filter(request):
    if not isinstance(request, dict):
        return None
    value = request.get("groupByFilter")
    if value in (None, ""):
        return None
    if (not isinstance(value, str) or len(value) > 4096
            or not re.fullmatch(r"(?:tag|legendary|voucher):[^\\/\s]+", value)):
        raise organizer.PoolError("choose a valid recorded filter to organize by")
    return value


def _assignment_mode(request, default=split_policy.MODE_EXCLUSIVE):
    value = request.get("assignmentMode") if isinstance(request, dict) else None
    return split_policy.normalize_mode(value, default)


def _split_request_key(reader, selected_ids, choices, ambiguity_rules,
                       publication):
    """Canonical no-scan identity for one reviewed split request."""
    if selected_ids is None:
        selected = None
    else:
        if not isinstance(selected_ids, list) or not all(
                isinstance(item, str) for item in selected_ids):
            raise organizer.PoolError("selectedCategories must be a list")
        if not selected_ids:
            raise organizer.PoolError(
                "select at least one shown location or exact category")
        selected = tuple(sorted(set(selected_ids)))
    request = publication if isinstance(publication, dict) else {}
    assignment_mode = _assignment_mode(request)
    policy = str(request.get("unmatchedPolicy", "stop")).lower()
    if policy not in ("stop", "keep", "remainder", "omit"):
        raise organizer.PoolError("unknown unmatched-seed policy")
    if policy == "keep":
        remainder_label = "Unmatched seeds"
    elif policy == "remainder":
        remainder_label = str(
            request.get("remainderName", "Needs review")).strip()
        if not remainder_label:
            raise organizer.PoolError(
                "give the review/remainder pool a name")
    else:
        remainder_label = ""
    return (
        reader.snapshot_token,
        assignment_mode,
        _group_by_filter(request),
        selected,
        tuple(sorted(choices.items())),
        tuple(sorted(ambiguity_rules.items())),
        policy,
        remainder_label,
        sanitize_prefix(request.get("prefix"),
                        os.path.basename(reader.path)),
    )


def _remember_reviewed_split(reader, selected_ids, choices, ambiguity_rules,
                             publication, plan):
    """Retain a small immutable preflight for its immediate execute request."""
    details = plan.get("publication", {})
    token = str(details.get("plan_token") or "")
    if not token or not details.get("ready"):
        return
    identity = _reader_cache_key(reader.path)
    request_key = _split_request_key(
        reader, selected_ids, choices, ambiguity_rules, publication)
    cache_key = (token, identity)
    with REVIEWED_SPLIT_CACHE_LOCK:
        REVIEWED_SPLIT_CACHE[cache_key] = (
            request_key, copy.deepcopy(plan))
        REVIEWED_SPLIT_CACHE.move_to_end(cache_key)
        while len(REVIEWED_SPLIT_CACHE) > REVIEWED_SPLIT_CACHE_LIMIT:
            REVIEWED_SPLIT_CACHE.popitem(last=False)


def _reviewed_split_preflight(reader, selected_ids, choices, ambiguity_rules,
                              publication, reviewed_token):
    """Return an unchanged reviewed preflight, or require an exact rescan."""
    identity = _reader_cache_key(reader.path)
    cache_key = (reviewed_token, identity)
    request_key = _split_request_key(
        reader, selected_ids, choices, ambiguity_rules, publication)
    with REVIEWED_SPLIT_CACHE_LOCK:
        cached = REVIEWED_SPLIT_CACHE.get(cache_key)
        if cached is None or cached[0] != request_key:
            return None
        REVIEWED_SPLIT_CACHE.move_to_end(cache_key)
        preflight = copy.deepcopy(cached[1])
    directory = os.path.dirname(os.path.abspath(reader.path))
    details = preflight["publication"]
    # Existence is intentionally checked again immediately before staging.
    # A collision makes the cached preview stale and falls back to the exact
    # planner, preserving its blocker/report-name behavior.
    if any(os.path.exists(os.path.join(directory, row["name"]))
           for row in details["outputs"]):
        return None
    if os.path.exists(os.path.join(directory, details["report_name"])):
        return None
    return preflight


def _begin_operation(kind):
    """Register one cancellable local mutation and return its Event."""
    global ACTIVE_OPERATIONS_CLOSING
    event = threading.Event()
    with ACTIVE_OPERATION_LOCK:
        if ACTIVE_OPERATIONS_CLOSING:
            raise organizer.PoolError(
                "the local Organizer is closing; reopen it before starting "
                "another operation")
        if kind in ACTIVE_OPERATIONS:
            raise organizer.PoolError("another organizer %s is still running" % kind)
        ACTIVE_OPERATIONS[kind] = event
    return event


def _finish_operation(kind, event):
    with ACTIVE_OPERATION_LOCK:
        if ACTIVE_OPERATIONS.get(kind) is event:
            del ACTIVE_OPERATIONS[kind]


def cancel_operation(kind):
    if kind not in ("analysis", "export", "split", "combine", "upgrade"):
        raise organizer.PoolError(
            "choose analysis, export, split, combine, or upgrade to cancel")
    with ACTIVE_OPERATION_LOCK:
        event = ACTIVE_OPERATIONS.get(kind)
        if event is None:
            return {"ok": True, "operation": kind, "state": "idle"}
        event.set()
    return {"ok": True, "operation": kind, "state": "cancelling"}


def _record_export_request_id(value, required=False):
    value = str(value or "")
    if not value:
        if required:
            raise organizer.PoolError("record export request id is required")
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", value):
        raise organizer.PoolError("record export request id is invalid")
    return value


def _set_record_export_status(request_id, state, error=""):
    if not request_id:
        return
    value = {
        "ok": True,
        "request_id": request_id,
        "state": state,
        "error": str(error or ""),
    }
    with ACTIVE_OPERATION_LOCK:
        RECORD_EXPORT_STATUS[request_id] = value
        RECORD_EXPORT_STATUS.move_to_end(request_id)
        while len(RECORD_EXPORT_STATUS) > RECORD_EXPORT_STATUS_LIMIT:
            RECORD_EXPORT_STATUS.popitem(last=False)


def record_export_status(request_id):
    """Return lifecycle state for the UI's hidden download request."""
    request_id = _record_export_request_id(request_id, required=True)
    with ACTIVE_OPERATION_LOCK:
        value = RECORD_EXPORT_STATUS.get(request_id)
        if value is None:
            return {
                "ok": True,
                "request_id": request_id,
                "state": "pending",
                "error": "",
            }
        return dict(value)


def cancel_active_operations():
    """Signal every Organizer worker during application shutdown."""
    with ACTIVE_OPERATION_LOCK:
        kinds = tuple(ACTIVE_OPERATIONS)
        for event in ACTIVE_OPERATIONS.values():
            event.set()
    return kinds


def begin_operation_shutdown():
    """Atomically block new Organizer work and cancel registered workers."""
    global ACTIVE_OPERATIONS_CLOSING
    with ACTIVE_OPERATION_LOCK:
        ACTIVE_OPERATIONS_CLOSING = True
        kinds = tuple(ACTIVE_OPERATIONS)
        for event in ACTIVE_OPERATIONS.values():
            event.set()
    return kinds


def allow_active_operations():
    """Open the operation registry when a local application starts."""
    global ACTIVE_OPERATIONS_CLOSING
    with ACTIVE_OPERATION_LOCK:
        if ACTIVE_OPERATIONS:
            raise organizer.PoolError(
                "cannot reopen the Organizer while an operation is active")
        ACTIVE_OPERATIONS_CLOSING = False


def wait_for_active_operations(poll_seconds=0.05):
    """Wait until cancellable Organizer request workers have cleaned up."""
    while True:
        with ACTIVE_OPERATION_LOCK:
            if not ACTIVE_OPERATIONS:
                return
        time.sleep(poll_seconds)


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


def _reader_cache_key(path):
    status = os.stat(path)
    return (os.path.abspath(path), status.st_dev, status.st_ino,
            status.st_size, getattr(status, "st_mtime_ns",
                                    int(status.st_mtime * 1000000000)))


def _reader_cache_weight(reader):
    cached = getattr(reader, "_organizer_cache_weight", None)
    if cached is not None:
        return cached
    blocks = reader.blocks
    # PackedBlockSequence owns its complete 56-byte-per-entry buffer and
    # includes that storage in __sizeof__. Charging a transient materialized
    # Block view for every packed entry would overstate a production legacy
    # index by hundreds of megabytes and immediately evict it from this cache.
    packed_bytes = getattr(blocks, "packed_bytes", None)
    if isinstance(packed_bytes, int) and packed_bytes >= 0:
        per_block = 0
    elif blocks:
        field_names = (
            "offset", "first_record", "count", "rank_bytes",
            "metadata_bytes", "associations", "first_rank", "last_rank",
            "header_bytes", "rank_codec", "metadata_encoding", "flags")

        def block_weight(block):
            attributes = getattr(block, "__dict__", None)
            return (sys.getsizeof(block)
                    + (sys.getsizeof(attributes)
                       if attributes is not None else 0)
                    + sum(
                sys.getsizeof(getattr(block, field))
                for field in field_names))

        per_block = max(block_weight(blocks[0]),
                        block_weight(blocks[-1]))
    else:
        per_block = 0
    weight = (
        sys.getsizeof(reader)
        + sys.getsizeof(blocks)
        + len(blocks) * per_block
        + len(reader.header.text.encode("utf-8")))
    reader._organizer_cache_weight = weight
    return weight


def _trim_reader_cache():
    total = sum(_reader_cache_weight(reader)
                for reader in READER_CACHE.values())
    while (READER_CACHE
           and (len(READER_CACHE) > READER_CACHE_LIMIT
                or total > READER_CACHE_MAX_BYTES)):
        _key, reader = READER_CACHE.popitem(last=False)
        total -= _reader_cache_weight(reader)


def _evict_cached_reader_paths(paths):
    targets = {os.path.abspath(path) for path in paths}
    with READER_CACHE_LOCK:
        for key in list(READER_CACHE):
            if key[0] in targets:
                del READER_CACHE[key]


def verified_source_reader(name, pool_dir=None, cancel_check=None):
    """Reuse a structurally verified snapshot while file identity is stable.

    Payload CRC/canonical verification is deferred to the first record
    traversal, avoiding the former decode-twice path for split/combine work.
    """
    _operation_cancelled(cancel_check)
    path = resolve_source(name, pool_dir)
    before = _reader_cache_key(path)
    with READER_CACHE_LOCK:
        reader = READER_CACHE.get(before)
        if reader is not None:
            READER_CACHE.move_to_end(before)
            return reader
    reader = organizer.BSPoolReader(
        path, cancel_check=cancel_check, verify_payloads=False)
    after = _reader_cache_key(path)
    if after != before:
        raise organizer.PoolError(
            "source changed while its committed snapshot was being verified; inspect it again")
    # The callback is only needed by constructor verification. Do not retain
    # a completed request's Event in a long-lived immutable cache entry.
    reader.cancel_check = None
    with READER_CACHE_LOCK:
        for key in list(READER_CACHE):
            if key[0] == path and key != after:
                del READER_CACHE[key]
        READER_CACHE[after] = reader
        READER_CACHE.move_to_end(after)
        _trim_reader_cache()
    return reader


def _bounded_header(path):
    text = organizer.read_pool_header_text(path)
    if not text:
        raise organizer.PoolError("cannot read a bounded Brainstorm pool header")
    return organizer.PoolHeader(text)


def _verify_upgrade_record_equivalence(
        source_reader, staged_reader, cancel_check=None):
    """Compare exact BSP3/BSP4 records and hash the verified staged stream."""
    digest = organizer.fnv64(b"BSPRECM1")
    descriptor_start = organizer.fnv64(b"BSPRECD1")
    records = 0
    source_records = source_reader.iter_records(cancel_check=cancel_check)
    staged_records = staged_reader.iter_records(cancel_check=cancel_check)
    with ExitStack() as streams:
        streams.callback(source_records.close)
        streams.callback(staged_records.close)
        while True:
            source_record = next(source_records, None)
            staged_record = next(staged_records, None)
            if source_record is None or staged_record is None:
                if source_record is not staged_record:
                    raise organizer.PoolError(
                        "native BSP4 update changed the number of seed records")
                break
            if not records % 4096:
                _operation_cancelled(cancel_check)
            source_occurrences = source_record.occurrences
            staged_occurrences = staged_record.occurrences
            if (source_record.rank != staged_record.rank
                    or len(source_occurrences) != len(staged_occurrences)
                    or any(source_item.raw != staged_item.raw
                           for source_item, staged_item in zip(
                               source_occurrences, staged_occurrences))):
                raise organizer.PoolError(
                    "native BSP4 update changed a seed rank or its recorded "
                    "filter metadata")
            descriptor_digest = descriptor_start
            for occurrence in staged_occurrences:
                raw = occurrence.raw
                descriptor_digest = organizer.fnv64(
                    struct.pack("<I", len(raw)), descriptor_digest)
                descriptor_digest = organizer.fnv64(
                    raw, descriptor_digest)
            digest = organizer.fnv64(
                struct.pack(
                    "<QIQ", staged_record.rank, len(staged_occurrences),
                    descriptor_digest),
                digest)
            records += 1
        # A generator owns its pool file while yielding. POSIX permits
        # unlinking that open file, but Windows does not; ExitStack closes both
        # streams before private-stage cleanup on every early mismatch.
    _operation_cancelled(cancel_check)
    if (records != source_reader.records
            or records != staged_reader.records):
        raise organizer.PoolError(
            "record metadata digest count differs from the pool header")
    return digest


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
            encoding = header.one("encoding", required=False, default="")
            if schema == 4 and encoding != organizer.POOL_ENCODINGS[4]:
                raise organizer.PoolError(
                    "BSP4 pool has incompatible encoding %s"
                    % (encoding or "(missing)"))
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
                "metadata_capable": schema in organizer.EVENT_POOL_SCHEMAS,
                "encoding": encoding,
                "native_compatible": schema in (1, 2, 3, 4),
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


def _degraded_summary_notice(records):
    """Explain the slow exact path taken when the native helper is absent."""
    binary = _POOL_BINARY_NAME
    return {
        "kind": "warning",
        "title": "Native seed-pool helper missing; using the slow exact reader",
        "text": ("%s would normally be summarized by %s in seconds. That "
                 "helper was not found, so every inspection and preview reads "
                 "all %s records in Python instead. Results are identical, but "
                 "each step can take tens of minutes and is not cached. Put %s "
                 "in the mod's native folder, or reinstall the full package, "
                 "to restore the fast path.") % (
                     "This pool", binary, format(records, ","), binary),
    }


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
            "text": ("New pools made from this source will also be marked "
                     "provisional. They are valid lists of the currently recorded "
                     "seeds, but they do not claim that the original search range "
                     "was fully scanned."),
        })
    if not source["metadata_capable"]:
        notices.append({
            "kind": "error",
            "title": "This older pool does not contain exact match details",
            "text": ("BSP2 records which seeds matched but not the exact tag, "
                     "Legendary, or voucher location for each seed. It can be "
                     "inspected, downloaded, or combined by seed membership, but "
                     "it cannot be split into location categories. Refilter or "
                     "rescan it into BSP4 first."),
        })
    if source.get("family_id") or source.get("lineage_id"):
        notices.append({
            "kind": "info",
            "title": "New pools remain traceable to this source",
            "text": ("Every new pool records source snapshot %s, so Brainstorm can "
                     "tell exactly which saved version it came from.") %
                    source["snapshot_id"],
        })
    return notices


def _native_pool_binary():
    return next((path for path in _POOL_BINARY_CANDIDATES
                 if os.path.isfile(path) and os.access(path, os.X_OK)), "")


def _summary_identity(path):
    status = os.stat(path)
    return {
        "device": int(status.st_dev),
        "inode": int(status.st_ino),
        "bytes": int(status.st_size),
        "links": int(status.st_nlink),
        "symlink": bool(os.path.islink(path)),
        "mtime_ns": int(getattr(
            status, "st_mtime_ns", int(status.st_mtime * 1000000000))),
    }


FORMAT_UPGRADE_PROTECTED_SUFFIXES = (
    "", ".manifest", ".state", ".criteria.cfg", ".attached",
    ".organizer-summary.json")
FORMAT_UPGRADE_PRESERVED_HEADER_FIELDS = (
    "family_id", "segment_id", "stage_hash", "lineage_id", "scan_cursor",
    "refilter_depth", "source_criteria_hash", "source_records",
    "source_pool_id", "source_complete", "source_coverage_complete",
    "source_range_start", "source_range_end", "source_data_bytes",
    "source_membership_digest", "source_snapshot_id", "source_family_id",
    "source_segment_id", "source_lineage_id", "input_cursor",
    "input_record_start", "input_record_end", "parent_snapshot_id",
    "parent_segment_id", "parent_records", "parent_data_bytes",
    "parent_coverage_complete", "shard_index", "shard_total")


def _format_upgrade_output_name(source_name, root):
    """Choose a readable BSP4 filename without overwriting pool artifacts."""
    source_stem = os.path.splitext(source_name)[0]
    match = re.search(r"bsp3$", source_stem, re.IGNORECASE)
    if match:
        base = source_stem[:match.start()]
        marker = "BSP4"
    else:
        base = source_stem
        marker = "-BSP4"
    if not base:
        base = "upgraded-pool" if marker.startswith("-") else ""
    max_stem = 160
    if os.name == "nt":
        # Native helper paths use the traditional Windows CRT. Account for
        # seed_pools\\.u-XXXXXXXX\\<name>.manifest.partial.<pid>.<attempt>
        # and retain headroom below MAX_PATH rather than merely capping the
        # visible basename.
        native_stage_suffix = ".manifest.partial.4294967295.9999"
        max_filename = (
            240 - len(os.path.abspath(root)) - 1 - len(".u-XXXXXXXX")
            - 1 - max(len(native_stage_suffix), len(".writer.lock")))
        max_stem = min(max_stem, max_filename - len(".bspool"))
        if max_stem < len("BSP4-9999"):
            raise organizer.PoolError(
                "The Seed Pool folder path is too long for a safe BSP4 update "
                "on Windows. Move the mod to a shorter folder first.")
    # Truncate only the source-derived portion so every automatic filename
    # retains its BSP4 marker and collision suffix.
    for number in range(1, 10000):
        suffix = "" if number == 1 else "-%d" % number
        stem = base[:max(0, max_stem - len(marker) - len(suffix))]
        stem = (stem + marker).rstrip(" .")
        name = stem + suffix + ".bspool"
        path = os.path.join(root, name)
        if not any(os.path.lexists(path + extra)
                   for extra in FORMAT_UPGRADE_PROTECTED_SUFFIXES):
            return name
    raise organizer.PoolError(
        "could not choose an unused automatic BSP4 filename")


def plan_format_upgrade(name, pool_dir=None):
    """Inspect one bounded header and plan a non-destructive BSP3 upgrade."""
    root = _pool_root(pool_dir)
    path = resolve_source(name, root)
    identity = _summary_identity(path)
    header = _bounded_header(path)
    try:
        schema = int(header.one("BRAINSTORM_SEED_POOL"))
    except ValueError:
        raise organizer.PoolError("invalid Brainstorm pool schema")
    encoding = header.one("encoding", required=False, default="")
    records = header.integer("records")
    complete = header.integer("complete")
    coverage_complete = header.integer(
        "coverage_complete", required=False, default=complete)
    if complete not in (0, 1) or coverage_complete not in (0, 1):
        raise organizer.PoolError("complete flags must be 0 or 1")
    source_contract = {
        "modelver": header.integer("modelver"),
        "header_bytes": header.integer("header_bytes"),
        "catalog_hash": header.one("catalog_hash").lower(),
        "criteria_hash": header.one("criteria_hash").lower(),
        "space": header.one("space", required=False, default="natural"),
        "seedspace": header.integer("seedspace"),
        "range_start": header.integer("range_start"),
        "range_end": header.integer("range_end"),
        "merged_parts": header.integer(
            "merged_parts", required=False, default=0),
        "derivation_id": header.integer(
            "derivation_id", 16, required=False, default=0),
        "snapshot_id": header.integer(
            "snapshot_id", 16, required=False, default=0),
        "membership_digest": header.integer(
            "membership_digest", 16, required=False, default=0),
        "metadata_digest": header.integer(
            "metadata_digest", 16, required=False, default=0),
        "pool_id": header.one(
            "pool_id", required=False, default="-"),
        "preserved_header_fields": {
            key: header.one(key)
            for key in FORMAT_UPGRADE_PRESERVED_HEADER_FIELDS
            if header.values.get(key)
        },
    }

    blockers = []
    output_name = ""
    status = "blocked"
    if schema == 4 and encoding == organizer.POOL_ENCODINGS[4]:
        status = "current"
    elif schema == 3:
        output_name = _format_upgrade_output_name(name, root)
        if encoding != organizer.POOL_ENCODINGS[3]:
            blockers.append(
                "This BSP3 pool has incompatible encoding %s."
                % (encoding or "(missing)"))
        if not complete:
            blockers.append(
                "Finish or resume this pool first; format updates require a completed BSP3 pool.")
        elif not coverage_complete:
            blockers.append(
                "This pool has provisional search coverage and cannot be losslessly updated yet.")
        if identity["symlink"] or identity["links"] != 1:
            blockers.append(
                "This pool is a filesystem link. Select its original one-link "
                "file so the Organizer can lock it safely during the update.")
        if not _native_pool_binary():
            blockers.append(
                "The native seed-pool helper is missing; reinstall the latest full Windows package.")
        if not blockers:
            status = "upgrade_available"
    elif schema in (1, 2):
        blockers.append(
            "BSP%d does not contain BSP3 per-seed event metadata and cannot be losslessly updated to BSP4; refilter or rescan it in Build / Search."
            % schema)
    elif schema > 4:
        blockers.append(
            "This pool uses newer BSP%d data; this version of Brainstorm will not downgrade it."
            % schema)
    else:
        blockers.append("This pool schema cannot be updated to BSP4.")

    token_fields = {
        "source": name,
        "identity": identity,
        "schema": schema,
        "encoding": encoding,
        "records": records,
        "complete": complete,
        "coverage_complete": coverage_complete,
        "output_name": output_name,
        "status": status,
        "source_contract": source_contract,
    }
    return {
        "source": name,
        "source_schema": schema,
        "source_format": "BSP%d" % schema,
        "encoding": encoding,
        "records": records,
        "bytes": identity["bytes"],
        "complete": bool(complete),
        "coverage_complete": bool(coverage_complete),
        "snapshot_id": header.one(
            "snapshot_id", required=False, default=""),
        "status": status,
        "eligible": status == "upgrade_available",
        "blockers": blockers,
        "output_name": output_name,
        "source_identity": identity,
        "source_contract": source_contract,
        "plan_token": _plan_token("format-upgrade", token_fields),
    }


def _terminate_upgrade_process(process):
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            return
        process.wait(timeout=2.0)


def _terminate_registered_upgrade_processes():
    """Last-resort child cleanup if the local web application exits."""
    with ACTIVE_UPGRADE_PROCESS_LOCK:
        processes = tuple(ACTIVE_UPGRADE_PROCESSES)
    for process in processes:
        try:
            _terminate_upgrade_process(process)
        except (OSError, subprocess.SubprocessError):
            pass


atexit.register(_terminate_registered_upgrade_processes)


def _run_native_upgrade(source, output, cancel_check=None):
    """Run the native one-input adaptive merge while draining its stderr."""
    binary = _native_pool_binary()
    if not binary:
        raise organizer.PoolError(
            "native seed-pool helper is missing; reinstall the latest full package")
    lines = collections.deque(maxlen=200)
    try:
        process = subprocess.Popen(
            [binary, "upgrade", source, output],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace")
    except OSError as exc:
        raise FormatUpdateFailedSafely(
            "BSP4 update could not start: %s" % exc) from exc
    with ACTIVE_UPGRADE_PROCESS_LOCK:
        ACTIVE_UPGRADE_PROCESSES.add(process)

    def pump():
        try:
            for line in process.stderr:
                lines.append(line.rstrip("\r\n"))
        finally:
            process.stderr.close()

    reader = None
    cancelled = False
    try:
        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        while process.poll() is None:
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
    finally:
        termination_error = None
        try:
            if process.poll() is None:
                _terminate_upgrade_process(process)
        except (OSError, subprocess.SubprocessError) as exc:
            termination_error = exc
        finally:
            if reader is not None:
                reader.join(timeout=5.0)
            with ACTIVE_UPGRADE_PROCESS_LOCK:
                if process.poll() is not None:
                    ACTIVE_UPGRADE_PROCESSES.discard(process)
        if termination_error is not None:
            raise organizer.PoolError(
                "native BSP4 helper could not be stopped safely: %s"
                % termination_error)
    if cancelled:
        raise OperationCancelled(
            "format update cancelled safely; no new pool was published")
    if process.returncode:
        detail = next((line for line in reversed(lines) if line.strip()),
                      "native helper exited with code %d" % process.returncode)
        message = "BSP4 update failed: %s" % detail
        if (" is damaged: BSP" in detail
                or "cannot verify historical event block" in detail
                or "malformed committed BSP3 block" in detail):
            raise FormatSourceDamaged(
                "%s The committed source bytes are inconsistent. "
                "Brainstorm did not change the BSP3 and will not skip or "
                "guess seeds; restore a known-good copy or rerun the exact "
                "search if no backup is available." % message)
        raise FormatUpdateFailedSafely(message)
    repaired_headers = 0
    for line in lines:
        match = re.search(
            r"safely reconstructed (\d+) BSP3 block header prefix",
            line)
        if match:
            repaired_headers = int(match.group(1))
    return {
        "normalized_historical_order": any(
            "normalizing historical event block order" in line
            for line in lines),
        "reconstructed_bsp3_header_prefixes": repaired_headers,
    }


def _cleanup_upgrade_stage(stage_dir):
    """Remove private upgrade data, retrying transient Windows/AV sharing."""
    last_error = None
    for delay in (0.0, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2):
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(stage_dir)
            return ""
        except FileNotFoundError:
            return ""
        except OSError as exc:
            last_error = exc
    return (
        "Brainstorm could not remove its private staging folder after "
        "retries: %s (%s). It is safe to delete after closing the Builder."
        % (stage_dir, last_error))


def _publish_upgrade_file(staged_path, final_path):
    """Compatibility adapter for mutation-owned no-overwrite publication."""
    seed_pool_mutations.publish_no_overwrite(staged_path, final_path)


def _published_upgrade_identity(path, expected):
    try:
        actual = _summary_identity(path)
        return all(actual[key] == expected[key] for key in (
            "device", "inode", "bytes", "mtime_ns"))
    except OSError:
        return False


@contextmanager
def _format_upgrade_writer_guard(
        path, committed_check, post_commit_warnings):
    """Never turn a completed publication into a reported failure on unlock."""
    try:
        with organizer.pool_writer_guard(path):
            yield
    except Exception as exc:
        if not committed_check():
            raise
        post_commit_warnings.append(
            "The BSP4 pool is complete, but final publication cleanup for "
            "%s reported: %s" % (os.path.basename(path), exc))


def execute_format_upgrade(request, pool_dir=None, cancel_check=None):
    """Create and atomically publish a verified BSP4 copy of one BSP3 pool."""
    if not isinstance(request, dict):
        raise organizer.PoolError("format update request must be an object")
    name = request.get("source", "")
    reviewed_token = str(request.get("planToken", ""))
    if not reviewed_token:
        raise organizer.PoolError(
            "check the selected pool format before creating a BSP4 copy")
    plan = plan_format_upgrade(name, pool_dir)
    if reviewed_token != plan["plan_token"]:
        raise FormatPlanStale(
            "the source pool or automatic output changed; check its format again")
    if not plan["eligible"]:
        detail = " ".join(plan["blockers"]) or "No format update is needed."
        raise organizer.PoolError(detail)

    root = _pool_root(pool_dir)
    source = resolve_source(name, root)
    output_name = plan["output_name"]
    output = os.path.abspath(os.path.join(root, output_name))
    if os.path.commonpath((root, output)) != root:
        raise organizer.PoolError(
            "automatic BSP4 output escapes the Seed Pool folder")
    stage_dir = tempfile.mkdtemp(prefix=".u-", dir=root)
    staged_output = os.path.join(stage_dir, output_name)
    result = None
    committed = False
    post_commit_warnings = []
    try:
        _operation_cancelled(cancel_check)
        # Treat the source as immutable for the duration and share the same
        # advisory lock contract as native writers and Builder deletion.
        with _format_upgrade_writer_guard(
                source, lambda: committed, post_commit_warnings):
            if _summary_identity(source) != plan["source_identity"]:
                raise FormatPlanStale(
                    "the source pool changed after its format was checked")
            native = _run_native_upgrade(
                source, staged_output, cancel_check=cancel_check)
            _operation_cancelled(cancel_check)
            if _summary_identity(source) != plan["source_identity"]:
                raise FormatPlanStale(
                    "the source pool changed during its format update")

            # Independently decode the locked BSP3 source and the staged BSP4
            # stream in rank order. Exact rank/descriptor equality proves the
            # format update preserved every logical record; hashing that same
            # staged stream also checks the native pre-encoding manifest.
            try:
                verified_source = organizer.BSPoolReader(
                    source, cancel_check=cancel_check,
                    verify_payloads=False)
                staged = organizer.BSPoolReader(
                    staged_output, cancel_check=cancel_check,
                    verify_payloads=False)
                staged_record_metadata_digest = \
                    _verify_upgrade_record_equivalence(
                        verified_source, staged,
                        cancel_check=cancel_check)
            except organizer.PoolError:
                _operation_cancelled(cancel_check)
                raise
            if _summary_identity(source) != plan["source_identity"]:
                raise FormatPlanStale(
                    "the source pool changed while its BSP4 records were "
                    "being verified")
            staged_manifest = staged_output + ".manifest"
            if not os.path.isfile(staged_manifest):
                raise organizer.PoolError(
                    "native helper did not create its BSP4 verification "
                    "manifest")
            try:
                with open(staged_manifest, "rb") as handle:
                    manifest_bytes = handle.read(1024 * 1024 + 1)
                if len(manifest_bytes) > 1024 * 1024:
                    raise organizer.PoolError(
                        "native BSP4 verification manifest is too large")
                manifest = organizer.PoolHeader(
                    manifest_bytes.decode("utf-8", errors="replace"))
            except (OSError, UnicodeError) as exc:
                raise organizer.PoolError(
                    "cannot read native BSP4 verification manifest: %s"
                    % exc)
            repaired_offsets = sorted(
                verified_source._repaired_bsp3_headers)
            manifest_repaired = manifest.integer(
                "upgrade_reconstructed_bsp3_header_prefixes",
                required=False, default=0)
            if (native["reconstructed_bsp3_header_prefixes"]
                    != len(repaired_offsets)
                    or manifest_repaired != len(repaired_offsets)):
                raise organizer.PoolError(
                    "native BSP4 header-reconstruction audit count differs "
                    "from the independently verified source")
            if repaired_offsets:
                repaired_offset = repaired_offsets[0]
                repaired_block = next((
                    number for number, block in enumerate(
                        verified_source.blocks)
                    if block.offset == repaired_offset), None)
                with open(source, "rb") as handle:
                    damaged_prefix = organizer.handle_read(
                        handle, repaired_offset, 8).hex()
                if (repaired_block is None
                        or manifest.integer(
                            "upgrade_first_reconstructed_block")
                            != repaired_block
                        or manifest.integer(
                            "upgrade_first_reconstructed_byte")
                            != repaired_offset
                        or manifest.one(
                            "upgrade_first_damaged_prefix")
                            != damaged_prefix):
                    raise organizer.PoolError(
                        "native BSP4 header-reconstruction audit details "
                        "differ from the independently verified source")
            elif any(manifest.values.get(key) for key in (
                    "upgrade_first_reconstructed_block",
                    "upgrade_first_reconstructed_byte",
                    "upgrade_first_damaged_prefix")):
                raise organizer.PoolError(
                    "native BSP4 manifest reports unexpected "
                    "header-reconstruction details")
            staged_derivation = staged.header.integer(
                "derivation_id", 16, required=False, default=0)
            staged_snapshot = staged.header.integer(
                "snapshot_id", 16, required=False, default=0)
            preserved_fields = plan[
                "source_contract"]["preserved_header_fields"]
            if (staged.schema != 4
                    or staged.encoding != organizer.POOL_ENCODINGS[4]
                    or not staged.complete or not staged.coverage_complete
                    or staged.records != plan["records"]
                    or staged.modelver != plan["source_contract"]["modelver"]
                    or staged.header_bytes
                        != plan["source_contract"]["header_bytes"]
                    or ("%016x" % staged.catalog_hash)
                        != plan["source_contract"]["catalog_hash"]
                    or ("%016x" % staged.criteria_hash)
                        != plan["source_contract"]["criteria_hash"]
                    or staged.space_name != plan["source_contract"]["space"]
                    or staged.seedspace != plan["source_contract"]["seedspace"]
                    or staged.range_start
                        != plan["source_contract"]["range_start"]
                    or staged.range_end
                        != plan["source_contract"]["range_end"]
                    or staged.header.integer(
                        "merged_parts", required=False, default=0)
                        != plan["source_contract"]["merged_parts"]
                    or not staged_derivation
                    or staged_derivation
                        == plan["source_contract"]["derivation_id"]
                    or not staged_snapshot
                    or staged_snapshot
                        == plan["source_contract"]["snapshot_id"]
                    or manifest.integer(
                        "BRAINSTORM_SEED_POOL_MERGE") != 4
                    or manifest.integer(
                        "upgrade_source_snapshot_id", 16)
                        != plan["source_contract"]["snapshot_id"]
                    or manifest.integer(
                        "upgrade_source_membership_digest", 16)
                        != plan["source_contract"]["membership_digest"]
                    or manifest.integer(
                        "upgrade_source_metadata_digest", 16)
                        != plan["source_contract"]["metadata_digest"]
                    or manifest.integer(
                        "record_metadata_digest", 16)
                        != staged_record_metadata_digest
                    or manifest.one("upgrade_source_pool_id")
                        != plan["source_contract"]["pool_id"]
                    or any(staged.header.values.get(key, []) != [value]
                           for key, value in preserved_fields.items())):
                raise organizer.PoolError(
                    "native helper did not create the expected complete BSP4 "
                    "pool with preserved source metadata")

            final_manifest = output + ".manifest"
            staged_output_identity = _summary_identity(staged_output)
            staged_manifest_identity = (
                _summary_identity(staged_manifest)
                if os.path.isfile(staged_manifest) else None)
            source_bytes = plan["bytes"]
            output_bytes = os.path.getsize(staged_output)
            saved_bytes = max(0, source_bytes - output_bytes)
            result = {
                "source": name,
                "source_schema": plan["source_schema"],
                "source_bytes": source_bytes,
                "source_retained": True,
                "output": output_name,
                "output_schema": 4,
                "output_bytes": output_bytes,
                "records": plan["records"],
                "saved_bytes": saved_bytes,
                "saved_percent": (
                    (100.0 * saved_bytes / source_bytes)
                    if source_bytes else 0.0),
                "normalized_historical_order":
                    native["normalized_historical_order"],
                "reconstructed_bsp3_header_prefixes":
                    native["reconstructed_bsp3_header_prefixes"],
            }
            _operation_cancelled(cancel_check)
            with _format_upgrade_writer_guard(
                    output, lambda: committed, post_commit_warnings):
                if any(os.path.lexists(
                        output + suffix)
                        for suffix in FORMAT_UPGRADE_PROTECTED_SUFFIXES):
                    raise FormatPlanStale(
                        "the automatic BSP4 output now exists; check the pool format again")
                # This is the final cancellation boundary.  Once the pool
                # link/rename exists it is a complete, verified publication;
                # Windows readers may immediately open it with delete-denying
                # sharing, so post-commit cancellation must not try to roll it
                # back or claim that nothing was published.
                _operation_cancelled(cancel_check)
                try:
                    _publish_upgrade_file(staged_output, output)
                    committed = True
                except BaseException as exc:
                    committed = _published_upgrade_identity(
                        output, staged_output_identity)
                    if not committed:
                        if os.path.lexists(output):
                            raise FormatPlanStale(
                                "the automatic BSP4 output now exists; check "
                                "the pool format again") from exc
                        if (isinstance(exc, OSError)
                                and exc.errno in (
                                    errno.EPERM, errno.EXDEV,
                                    getattr(errno, "ENOTSUP", -1),
                                    getattr(errno, "EOPNOTSUPP", -1))):
                            raise organizer.PoolError(
                                "This Seed Pool folder filesystem cannot "
                                "safely publish a no-overwrite BSP4 copy. "
                                "Use a local NTFS, APFS, or standard Linux "
                                "filesystem folder.") from exc
                        raise
                if staged_manifest_identity is not None:
                    try:
                        _publish_upgrade_file(
                            staged_manifest, final_manifest)
                    except BaseException as exc:
                        if not _published_upgrade_identity(
                                final_manifest,
                                staged_manifest_identity):
                            result["publication_warning"] = (
                                "The BSP4 pool is complete, but its optional "
                                "manifest could not be published: %s" % exc)
                organizer.fsync_directory(root)
                if not os.path.isfile(final_manifest):
                    result["publication_warning"] = (
                        result.get("publication_warning")
                        or "The BSP4 pool is complete, but its optional "
                        "manifest was not created.")

        try:
            _evict_cached_reader_paths((output,))
        except Exception as exc:
            if not committed:
                raise
            post_commit_warnings.append(
                "The BSP4 pool is complete, but its Organizer cache could "
                "not be refreshed: %s" % exc)
        if post_commit_warnings and result is not None:
            prior = result.get("publication_warning", "")
            result["publication_warning"] = " ".join(
                item for item in [prior] + post_commit_warnings if item)
    except (OperationCancelled, FormatPlanStale,
            FormatUpdateFailedSafely):
        raise
    except (organizer.PoolError, OSError) as exc:
        if not committed:
            raise FormatUpdateFailedSafely(str(exc)) from exc
        raise
    finally:
        cleanup_warning = _cleanup_upgrade_stage(stage_dir)
        if cleanup_warning:
            print(cleanup_warning, file=sys.stderr)
            if committed and result is not None:
                result["cleanup_warning"] = cleanup_warning
    return result


def _summary_cache_path(path):
    return path + ".organizer-summary.json"


def _cached_summary(path, identity):
    try:
        with open(_summary_cache_path(path), "r", encoding="utf-8") as handle:
            cached = json.load(handle)
        if (not isinstance(cached, dict)
                or cached.get("cache_schema") != SUMMARY_CACHE_SCHEMA
                or cached.get("identity") != identity
                or not isinstance(cached.get("report"), dict)):
            return None
        report = cached["report"]
        if (not isinstance(report.get("source"), dict)
                or report["source"].get("records") < 0
                or report.get("organizer_schema") != 1):
            return None
        return report
    except (OSError, TypeError, ValueError):
        return None


class _InspectionPlanReader:
    """Minimal immutable reader view for a summary-backed split review."""

    def __init__(self, path, source):
        self.path = os.path.abspath(path)
        self.metadata_capable = bool(source.get("metadata_capable"))
        self.records = int(source.get("records", -1))
        self.snapshot_token = str(source.get("snapshot_id", "")).lower()

    def iter_records(self):
        raise AssertionError("summary-backed review attempted a record scan")


def _inferred_category_groups(report):
    """Return exact full-category groups when the summary proves them.

    The native summary stores marginal category counts plus the number of
    ambiguous records.  A common filter shape has one category on every
    matched record (for example the required Charm tag) and exactly one
    additional result category (for example normal or Negative Perkeo).  The
    equality checks below mathematically prove that shape; no probabilistic or
    heuristic inference is used.
    """
    try:
        source = report["source"]
        total = int(source["records"])
        unmatched = int(report["unmatched_count"])
        ambiguous = int(report["ambiguous_count"])
        rows = report["categories"]
        counts = {
            str(row["category_id"]): int(row["records"])
            for row in rows
        }
    except (KeyError, TypeError, ValueError):
        return None
    if (total < 0 or unmatched < 0 or ambiguous < 0
            or unmatched > total or ambiguous > total - unmatched
            or len(counts) != len(rows)
            or any(count < 0 or count > total for count in counts.values())):
        return None
    known = total - unmatched
    if known == 0:
        return []
    # The minimum number of category associations is one for each known
    # record, plus one more for every record known to be ambiguous. Equality
    # proves that no record has a third category.
    if sum(counts.values()) != known + ambiguous:
        return None
    universal = sorted(
        category for category, count in counts.items() if count == known)
    if not universal:
        return None
    anchor = universal[0]
    others = [(category, count) for category, count in sorted(counts.items())
              if category != anchor and count]
    if sum(count for _category, count in others) != ambiguous:
        return None
    groups = []
    anchor_only = known - ambiguous
    if anchor_only:
        groups.append({"categories": [anchor], "records": anchor_only,
                       "samples": []})
    groups.extend({
        "categories": sorted((anchor, category)),
        "records": count,
        "samples": [],
    } for category, count in others)
    return groups


def _summary_can_project(report, selected_ids, group_by_filter=None):
    if (not isinstance(selected_ids, list) or not selected_ids
            or not all(isinstance(item, str) for item in selected_ids)):
        return False
    if group_by_filter:
        filter_row = next((
            row for row in report.get("filters", [])
            if isinstance(row, dict)
            and row.get("filter_id") == group_by_filter), None)
        if filter_row is None \
                or int(filter_row.get("multiple_location_records", -1)) != 0:
            return False
        available = {
            row.get("location_id") for row in filter_row.get("locations", [])
            if isinstance(row, dict)
        }
        return set(selected_ids).issubset(available)
    selected = set(selected_ids)
    available = {
        row.get("category_id") for row in report.get("categories", [])
        if isinstance(row, dict)
    }
    if not selected.issubset(available):
        return False
    if len(selected) == 1:
        return True
    return _inferred_category_groups(report) is not None


def _choice_plan_has_individual_decisions(value):
    if value is None:
        return False
    if not isinstance(value, dict):
        return True
    choices = value.get("choices", value)
    if not isinstance(choices, dict):
        return True
    return any(bool(destination) for destination in choices.values())


def _parse_native_summary(text):
    lines = text.splitlines()
    if (not lines or lines[0] != "BRAINSTORM_POOL_SUMMARY 2"
            or lines[-1] != "end"):
        raise organizer.PoolError("native pool summary returned an invalid document")
    singular = {}
    categories = []
    filters = []
    locations = []
    provenance = {}
    operands = {}
    integer_fields = {
        "records", "ambiguous_count", "unmatched_count",
        "opaque_associations", "records_without_provenance",
        "records_without_operands",
    }
    for line in lines[1:-1]:
        parts = line.split()
        if not parts:
            continue
        key = parts[0]
        if key in integer_fields:
            if (len(parts) != 2 or key in singular
                    or not re.fullmatch(r"[0-9]+", parts[1])):
                raise organizer.PoolError("native pool summary repeats or malforms %s" % key)
            singular[key] = int(parts[1])
        elif key in (
                "membership_digest", "metadata_digest",
                "record_metadata_digest"):
            if (len(parts) != 2 or key in singular
                    or not re.fullmatch(r"[0-9a-f]{16}", parts[1])):
                raise organizer.PoolError("native pool summary malforms %s" % key)
            singular[key] = parts[1]
        elif key == "category":
            if (len(parts) != 3
                    or not re.fullmatch(r"[0-9a-f]+", parts[1])
                    or len(parts[1]) % 2
                    or not re.fullmatch(r"[0-9]+", parts[2])):
                raise organizer.PoolError("native pool summary has a malformed category")
            categories.append((bytes.fromhex(parts[1]), int(parts[2])))
        elif key == "filter":
            if (len(parts) != 6 or parts[1] not in ("1", "2", "3")
                    or not re.fullmatch(r"(?:[0-9a-f]{2})+", parts[2])
                    or not all(re.fullmatch(r"[0-9]+", value)
                               for value in parts[3:])):
                raise organizer.PoolError(
                    "native pool summary has a malformed filter")
            raw_key = bytes.fromhex(parts[2])
            if len(raw_key) > 255 \
                    or any(byte < 33 or byte > 126 for byte in raw_key):
                raise organizer.PoolError(
                    "native pool summary filter key is unsafe")
            filters.append((
                int(parts[1]), raw_key, int(parts[3]), int(parts[4]),
                int(parts[5])))
        elif key == "location":
            if (len(parts) != 6 or parts[1] not in ("1", "2", "3")
                    or not re.fullmatch(r"(?:[0-9a-f]{2})+", parts[2])
                    or not all(re.fullmatch(r"[0-9]+", value)
                               for value in parts[3:])):
                raise organizer.PoolError(
                    "native pool summary has a malformed location")
            raw_key = bytes.fromhex(parts[2])
            if len(raw_key) > 255 \
                    or any(byte < 33 or byte > 126 for byte in raw_key):
                raise organizer.PoolError(
                    "native pool summary location key is unsafe")
            filters_kind, ante, phase, location_records = (
                int(parts[1]), int(parts[3]), int(parts[4]), int(parts[5]))
            if not ante or ante > 255 or phase > 255:
                raise organizer.PoolError(
                    "native pool summary location is outside event bounds")
            locations.append((
                filters_kind, raw_key, ante, phase, location_records))
        elif key in ("provenance", "operand"):
            if (len(parts) != 3
                    or not re.fullmatch(r"[0-9a-f]{16}", parts[1])
                    or not re.fullmatch(r"[0-9]+", parts[2])):
                raise organizer.PoolError("native pool summary has malformed provenance")
            destination = provenance if key == "provenance" else operands
            if parts[1] in destination:
                raise organizer.PoolError("native pool summary repeats provenance")
            destination[parts[1]] = int(parts[2])
        else:
            raise organizer.PoolError("native pool summary has an unknown field")
    required = integer_fields | {"membership_digest", "metadata_digest"}
    if not required.issubset(singular) or (
            set(singular) - required - {"record_metadata_digest"}):
        raise organizer.PoolError("native pool summary is incomplete")
    records = singular["records"]
    if (singular["ambiguous_count"] > records
            or singular["unmatched_count"] > records
            or singular["records_without_provenance"] > records
            or singular["records_without_operands"] > records
            or any(count > records for _raw, count in categories)
            or any(covered > records or multiple > covered
                   or associations < covered
                   for _kind, _key, covered, multiple, associations in filters)
            or any(count > records
                   for _kind, _key, _ante, _phase, count in locations)
            or any(count > records for count in provenance.values())
            or any(count > records for count in operands.values())):
        raise organizer.PoolError("native pool summary count exceeds its records")
    filter_keys = [(kind, raw_key) for kind, raw_key, *_rest in filters]
    location_keys = [
        (kind, raw_key, ante, phase)
        for kind, raw_key, ante, phase, _records in locations
    ]
    if len(set(filter_keys)) != len(filter_keys):
        raise organizer.PoolError(
            "native pool summary repeats a recorded filter")
    if len(set(location_keys)) != len(location_keys):
        raise organizer.PoolError(
            "native pool summary repeats a recorded location")
    singular["categories"] = categories
    singular["filters"] = filters
    singular["locations"] = locations
    singular["provenance_counts"] = provenance
    singular["operand_counts"] = operands
    return singular


def _run_native_summary(path, cancel_check=None):
    binary = _native_pool_binary()
    if not binary:
        return None
    try:
        process = subprocess.Popen(
            [binary, "summarize", path], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace")
    except OSError:
        return None
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if cancel_check is not None and cancel_check():
                process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise organizer.PoolError("operation cancelled")
    if process.returncode:
        message = (stderr or "").strip().splitlines()
        raise organizer.PoolError(
            message[-1] if message else "native pool summary failed")
    return _parse_native_summary(stdout)


def _source_from_native_summary(reader, summary):
    reader.accept_native_verification(
        int(summary["membership_digest"], 16),
        int(summary["metadata_digest"], 16))
    return organizer.source_summary(reader)


def _report_from_native_summary(reader, summary):
    source = _source_from_native_summary(reader, summary)
    if summary["records"] != source["records"]:
        raise organizer.PoolError("native summary record count differs from pool header")
    categories = []
    seen = set()
    for raw, records in summary["categories"]:
        occurrence = organizer.Occurrence.decode(raw)
        if not occurrence.known or occurrence.category_id in seen:
            raise organizer.PoolError("native summary returned a duplicate or opaque category")
        seen.add(occurrence.category_id)
        item = occurrence.as_dict()
        item["records"] = records
        categories.append(item)
    categories.sort(key=organizer._category_row_sort_key)
    category_by_location = collections.defaultdict(list)
    for row in categories:
        category_by_location[row["location_id"]].append(row)
    location_counts = {}
    for kind, raw_key, ante, phase, records in summary.get("locations", []):
        key = raw_key.decode("ascii")
        descriptor = bytes((kind, len(raw_key))) + raw_key + bytes(
            (ante, phase, 0, 0, 0))
        occurrence = organizer.Occurrence.decode(descriptor)
        location_counts[occurrence.location_id] = records
        # A native location must be backed by at least one exact descriptor;
        # otherwise the two views disagree about the verified metadata.
        if occurrence.location_id not in category_by_location:
            raise organizer.PoolError(
                "native summary location has no exact category")
    filter_covered = {}
    filter_multiple = {}
    filter_associations = {}
    for kind, raw_key, covered, multiple, associations in summary.get(
            "filters", []):
        key = raw_key.decode("ascii")
        descriptor = bytes((kind, len(raw_key))) + raw_key + bytes(
            (1, 1, 0, 0, 0))
        occurrence = organizer.Occurrence.decode(descriptor)
        if occurrence.filter_id in filter_covered:
            raise organizer.PoolError(
                "native summary repeats a recorded filter")
        filter_covered[occurrence.filter_id] = covered
        filter_multiple[occurrence.filter_id] = multiple
        filter_associations[occurrence.filter_id] = associations
    expected_filters = {row["filter_id"] for row in categories}
    expected_locations = {row["location_id"] for row in categories}
    if (set(filter_covered) != expected_filters
            or set(location_counts) != expected_locations):
        raise organizer.PoolError(
            "native filter/location summary is incomplete")
    location_associations = collections.Counter()
    for location_id, count in location_counts.items():
        location_associations[
            category_by_location[location_id][0]["filter_id"]] += count
    for filter_id, associations in filter_associations.items():
        if (location_associations[filter_id] != associations
                or associations < filter_covered[filter_id]
                    + filter_multiple[filter_id]):
            raise organizer.PoolError(
                "native filter/location summary counts disagree")
    filters, recommended = organizer.build_filter_report(
        categories, location_counts, filter_covered, filter_multiple,
        filter_associations, reader.records, reader.is_composite)
    if (set(filter_covered) != {row["filter_id"] for row in filters}
            or set(location_counts) != {
                location["location_id"] for row in filters
                for location in row["locations"]}):
        raise organizer.PoolError(
            "native filter/location summary is incomplete")
    report = {
        "organizer_schema": 1,
        "source": source,
        "categories": categories,
        "filters": filters,
        "recommended_filter_id": recommended,
        "category_count": len(categories),
        "ambiguous_count": summary["ambiguous_count"],
        "ambiguous": [],
        "ambiguities_truncated": summary["ambiguous_count"],
        "unmatched_count": summary["unmatched_count"],
        "opaque_associations": summary["opaque_associations"],
        "records_without_provenance": (
            summary["records_without_provenance"] if source["composite"] else 0),
        "records_without_operands": (
            summary["records_without_operands"] if source["composite"] else 0),
        "provenance_counts": summary["provenance_counts"],
        "operand_counts": summary["operand_counts"],
    }
    report["notices"] = _notices(source)
    return report


def inspect_source(name, pool_dir=None, ambiguity_limit=100, cancel_check=None):
    path = resolve_source(name, pool_dir)
    identity = _summary_identity(path)
    header = _bounded_header(path)
    # Composite inspection retains the full semantic per-record validation.
    # Large ordinary event pools can use the bounded native block summary.
    summarizable = (
        identity["bytes"] >= NATIVE_SUMMARY_MIN_BYTES
        and int(header.one("BRAINSTORM_SEED_POOL"))
        in organizer.EVENT_POOL_SCHEMAS
        and not header.values.get("composite_schema"))
    # A missing helper is not a blocker here: the Lua-independent Python
    # traversal stays exact, so inspection must still work. It is orders of
    # magnitude slower on a production pool, though, and the former silent
    # fallback was indistinguishable from a hung request. Record it so the
    # page can say why the wait is long instead of spinning without a reason.
    degraded_native_summary = summarizable and not _native_pool_binary()
    use_native = summarizable and not degraded_native_summary
    if use_native:
        cached = _cached_summary(path, identity)
        if cached is not None:
            _operation_cancelled(cancel_check)
            return cached
        reader = organizer.BSPoolReader(
            path, cancel_check=cancel_check, verify_payloads=False)
        if _summary_identity(path) != identity:
            raise organizer.PoolError(
                "source changed while its committed snapshot was being "
                "structurally opened; inspect it again")
        if reader._repaired_bsp3_headers:
            # The native summary intentionally remains byte-strict. Python
            # has already completed the exceptional full CRC/rank/metadata
            # and whole-pool identity pass, so use that verified reader
            # directly instead of reopening the damaged physical prefix.
            report = organizer.analyze(
                reader, ambiguity_limit=ambiguity_limit,
                cancel_check=cancel_check)
            if _summary_identity(path) != identity:
                raise organizer.PoolError(
                    "source changed while its verified reconstructed view "
                    "was being inspected; inspect it again")
            repaired = len(reader._repaired_bsp3_headers)
            report["source"][
                "reconstructed_bsp3_header_prefixes"] = repaired
            report["notices"] = [{
                "kind": "warning",
                "title": "The original BSP3 has a damaged block header",
                "text": (
                    "Brainstorm safely reconstructed %s fixed header prefix "
                    "in memory after checking the committed index, block "
                    "checksum, ranks, metadata, snapshot, and whole-pool "
                    "identities. The original bytes remain damaged; create "
                    "the BSP4 copy now because byte-strict tools can still "
                    "reject this BSP3.") % format(repaired, ","),
            }] + _notices(report["source"])
            return report
        summary = _run_native_summary(path, cancel_check=cancel_check)
        _operation_cancelled(cancel_check)
        if summary is not None:
            if _summary_identity(path) != identity:
                raise organizer.PoolError(
                    "source changed while its committed snapshot was being summarized; inspect it again")
            report = _report_from_native_summary(reader, summary)
            organizer.atomic_json(_summary_cache_path(path), {
                "cache_schema": SUMMARY_CACHE_SCHEMA,
                "identity": identity,
                "report": report,
            })
            return report
    reader = verified_source_reader(
        name, pool_dir, cancel_check=cancel_check)
    report = organizer.analyze(
        reader, ambiguity_limit=ambiguity_limit, cancel_check=cancel_check)
    report["notices"] = _notices(report["source"])
    if degraded_native_summary:
        report["notices"].insert(
            0, _degraded_summary_notice(report["source"]["records"]))
    return report


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


def _ambiguity_rules(value, reader):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise organizer.PoolError("choice plan must be a JSON object")
    if "source_snapshot_id" in value:
        snapshot = str(value.get("source_snapshot_id", "")).lower()
        if snapshot != reader.snapshot_token:
            raise organizer.PoolError(
                "choice plan is for snapshot %s; inspect current snapshot %s" % (
                    snapshot or "(missing)", reader.snapshot_token))
    rules = value.get("ambiguity_rules", {})
    if not isinstance(rules, dict):
        raise organizer.PoolError(
            "choice plan needs an object-valued ambiguity_rules field")
    result = {}
    for key, category in rules.items():
        if (not isinstance(key, str) or not isinstance(category, str)
                or not re.fullmatch(r"[0-9a-f]{16}", key) or not category):
            raise organizer.PoolError(
                "ambiguity rule keys must be lowercase 16-hex tokens and "
                "destinations must be nonempty strings")
        result[key] = category
    return result


def _ambiguity_rule_key(candidates):
    return organizer.ambiguity_rule_key(candidates)


def _assignment_sort_key(category, available, group_by_filter):
    if not group_by_filter:
        return (category,)
    row = available.get(category, {})
    return (
        int(row.get("ante", organizer.MASK64)),
        organizer.PHASE_SORT_ORDER.get(str(row.get("phase", "")), 1000),
        str(row.get("label", "")).casefold(),
        category,
    )


def build_split_plan(reader, selected_ids=None, choice_plan=None,
                     publication=None, cancel_check=None, inspection=None):
    """Build an exact, snapshot-pinned, no-write publication preflight."""
    _operation_cancelled(cancel_check)
    request = publication if isinstance(publication, dict) else {}
    assignment_mode = _assignment_mode(request)
    group_by_filter = _group_by_filter(request)
    if isinstance(choice_plan, dict) and "group_by_filter" in choice_plan \
            and str(choice_plan.get("group_by_filter") or "") != (
                group_by_filter or ""):
        raise organizer.PoolError(
            "saved decisions use a different organizing filter")
    if isinstance(choice_plan, dict) and "assignment_mode" in choice_plan \
            and split_policy.normalize_mode(
                choice_plan.get("assignment_mode")) != assignment_mode:
        raise organizer.PoolError(
            "saved decisions use a different assignment mode")
    if not reader.metadata_capable:
        raise organizer.PoolError(
            "BSP2 has no per-seed occurrence metadata; refilter/rescan into BSP4 before splitting")
    if selected_ids is None:
        selected_filter = None
    else:
        if not isinstance(selected_ids, list) or not all(
                isinstance(item, str) for item in selected_ids):
            raise organizer.PoolError("selectedCategories must be a list")
        selected_filter = set(selected_ids)
        if not selected_filter:
            raise organizer.PoolError(
                "select at least one shown location or exact category")
    choices = _choice_document(choice_plan, reader)
    ambiguity_rules = _ambiguity_rules(choice_plan, reader)
    distribution_policy = None
    if selected_filter is not None:
        distribution_policy = split_policy.PoolSplitPolicy(
            split_policy.SplitSpec.create(
                assignment_mode, sorted(selected_filter),
                group_by_filter or "", choices, ambiguity_rules))
    elif assignment_mode == split_policy.MODE_MATCHING_COPIES:
        raise organizer.PoolError(
            "matching-copy mode requires selected destinations")
    used_choices = set()
    used_rules = set()
    ambiguous = []
    ambiguity_groups = {}
    ambiguity_groups_truncated = False
    ambiguous_count = 0
    unresolved_count = 0
    unmatched = 0
    overlap_records = 0
    opaque_associations = 0
    available = {}
    category_ids = {}
    candidate_counts = {}
    assigned_counts = {}
    pending_counts = {}
    summary_projection = False
    if inspection is not None:
        if (not isinstance(inspection, dict)
                or not isinstance(inspection.get("source"), dict)
                or str(inspection["source"].get("snapshot_id", "")).lower()
                != reader.snapshot_token
                or int(inspection["source"].get("records", -1))
                != reader.records):
            raise organizer.PoolError(
                "cached inspection does not match the selected pool snapshot")
        if group_by_filter:
            filter_row = next((
                row for row in inspection.get("filters", [])
                if isinstance(row, dict)
                and row.get("filter_id") == group_by_filter), None)
            if filter_row is None:
                raise organizer.PoolError(
                    "the selected filter is absent from this pool snapshot")
            summary_rows = {
                row["location_id"]: dict(row, category_id=row["location_id"])
                for row in filter_row.get("locations", [])
                if isinstance(row, dict)
                and isinstance(row.get("location_id"), str)
            }
        else:
            summary_rows = {
                row["category_id"]: dict(row)
                for row in inspection.get("categories", [])
                if isinstance(row, dict)
                and isinstance(row.get("category_id"), str)
            }
        if (selected_filter is not None and not choices
                and _summary_can_project(
                    inspection, selected_ids, group_by_filter)):
            available.update(summary_rows)
            opaque_associations = int(
                inspection.get("opaque_associations", 0))
            if group_by_filter:
                selected_total = 0
                for category in selected_filter:
                    row = summary_rows.get(category)
                    if row is None:
                        continue
                    count = int(row.get("records", -1))
                    if count < 0 or count > reader.records:
                        raise organizer.PoolError(
                            "cached inspection has an invalid location count")
                    candidate_counts[category] = count
                    assigned_counts[category] = count
                    selected_total += count
                if selected_total > reader.records:
                    raise organizer.PoolError(
                        "cached inspection location counts overlap unexpectedly")
                unmatched = reader.records - selected_total
                summary_projection = True
            elif len(selected_filter) == 1:
                category = next(iter(selected_filter))
                row = summary_rows.get(category)
                if row is not None:
                    count = int(row.get("records", -1))
                    if count < 0 or count > reader.records:
                        raise organizer.PoolError(
                            "cached inspection has an invalid category count")
                    candidate_counts[category] = count
                    assigned_counts[category] = count
                    unmatched = reader.records - count
                    summary_projection = True
            else:
                full_groups = _inferred_category_groups(inspection)
                if full_groups is not None:
                    unmatched = int(inspection.get("unmatched_count", 0))
                    projected = {}
                    for full_group in full_groups:
                        count = int(full_group["records"])
                        candidates = sorted(
                            selected_filter.intersection(
                                full_group["categories"]))
                        if not candidates:
                            unmatched += count
                            continue
                        for category in candidates:
                            candidate_counts[category] = \
                                candidate_counts.get(category, 0) + count
                        if len(candidates) == 1:
                            category = candidates[0]
                            assigned_counts[category] = \
                                assigned_counts.get(category, 0) + count
                            continue
                        overlap_records += count
                        if assignment_mode == split_policy.MODE_MATCHING_COPIES:
                            for category in candidates:
                                assigned_counts[category] = \
                                    assigned_counts.get(category, 0) + count
                            continue
                        ambiguous_count += count
                        rule_key = _ambiguity_rule_key(candidates)
                        destination = ambiguity_rules.get(rule_key)
                        if destination is not None:
                            used_rules.add(rule_key)
                            if destination not in candidates:
                                raise organizer.PoolError(
                                    "ambiguity rule is not one of its selected categories")
                            assigned_counts[destination] = \
                                assigned_counts.get(destination, 0) + count
                            continue
                        unresolved_count += count
                        for category in candidates:
                            pending_counts[category] = \
                                pending_counts.get(category, 0) + count
                        group = projected.get(rule_key)
                        if group is None:
                            group = {
                                "rule_key": rule_key,
                                "candidates": candidates,
                                "records": 0,
                                "unresolved_records": 0,
                                "samples": [],
                            }
                            projected[rule_key] = group
                        elif group["candidates"] != candidates:
                            raise organizer.PoolError(
                                "summary category sets produced the same rule token")
                        group["records"] += count
                        group["unresolved_records"] += count
                        for seed in full_group.get("samples", []):
                            if len(group["samples"]) < 3 \
                                    and seed not in group["samples"]:
                                group["samples"].append(seed)
                    for rule_key in sorted(projected):
                        if len(ambiguity_groups) >= AMBIGUITY_GROUP_LIMIT:
                            ambiguity_groups_truncated = True
                            break
                        ambiguity_groups[rule_key] = projected[rule_key]
                    summary_projection = True
    if not summary_projection and distribution_policy is not None:
        def policy_records():
            nonlocal opaque_associations
            for record in reader.iter_records(cancel_check=cancel_check):
                for occurrence in record.occurrences:
                    if occurrence.known:
                        if group_by_filter:
                            if occurrence.filter_id != group_by_filter:
                                continue
                            category = occurrence.location_id
                        else:
                            category = category_ids.get(occurrence.raw)
                            if category is None:
                                category = occurrence.category_id
                                category_ids[occurrence.raw] = category
                        if category in selected_filter \
                                and category not in available:
                            available[category] = occurrence.location_dict() \
                                if group_by_filter else occurrence.as_dict()
                    elif (occurrence.provenance_id is None
                          and occurrence.operand_id is None):
                        opaque_associations += 1
                yield record

        try:
            reviewed_distribution = distribution_policy.review(
                policy_records(), reader.seed, cancel_check=cancel_check,
                cancel_interval=8192,
                ambiguity_sample_limit=AMBIGUITY_SAMPLE_LIMIT,
                ambiguity_group_limit=AMBIGUITY_GROUP_LIMIT)
        except split_policy.SplitPolicyError as exc:
            raise organizer.PoolError(str(exc))
        candidate_counts = reviewed_distribution.candidates()
        assigned_counts = reviewed_distribution.destinations()
        pending_counts = reviewed_distribution.pending()
        unmatched = reviewed_distribution.unmatched_records
        overlap_records = reviewed_distribution.overlap_records
        ambiguous_count = reviewed_distribution.ambiguity_count
        unresolved_count = reviewed_distribution.unresolved_records
        ambiguous = [{
            "seed": row.seed,
            "rank": row.rank,
            "rule_key": row.rule_key,
            "candidates": list(row.candidates),
            "choice": row.choice,
            "resolved_by_rule": row.resolved_by_rule,
        } for row in reviewed_distribution.ambiguities]
        ambiguity_groups = {row.rule_key: {
            "rule_key": row.rule_key,
            "candidates": list(row.candidates),
            "records": row.records,
            "unresolved_records": row.unresolved_records,
            "samples": list(row.samples),
        } for row in reviewed_distribution.ambiguity_groups}
        ambiguity_groups_truncated = \
            reviewed_distribution.ambiguity_groups_truncated
        used_choices = set(reviewed_distribution.used_choices)
        used_rules = set(reviewed_distribution.used_rules)
        record_source = ()
    else:
        record_source = () if summary_projection else reader.iter_records(
            cancel_check=cancel_check)
    for record_index, record in enumerate(record_source):
        if record_index % 8192 == 0:
            _operation_cancelled(cancel_check)
        candidate_set = set()
        for occurrence in record.occurrences:
            if occurrence.known:
                if group_by_filter:
                    if occurrence.filter_id != group_by_filter:
                        continue
                    category = occurrence.location_id
                else:
                    category = category_ids.get(occurrence.raw)
                    if category is None:
                        category = occurrence.category_id
                        category_ids[occurrence.raw] = category
                if selected_filter is None or category in selected_filter:
                    candidate_set.add(category)
                    if category not in available:
                        available[category] = occurrence.location_dict() \
                            if group_by_filter else occurrence.as_dict()
                continue
            if (occurrence.provenance_id is None
                    and occurrence.operand_id is None):
                opaque_associations += 1
        candidates = sorted(candidate_set, key=lambda category:
                            _assignment_sort_key(
                                category, available, group_by_filter))
        for category in candidates:
            candidate_counts[category] = candidate_counts.get(category, 0) + 1
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
            category = candidates[0]
            assigned_counts[category] = assigned_counts.get(category, 0) + 1
            continue
        chosen, choice_key = organizer.choice_for_record(choices, reader, record)
        if choice_key:
            used_choices.add(choice_key)
        rule_key = _ambiguity_rule_key(candidates)
        if rule_key in ambiguity_rules:
            used_rules.add(rule_key)
            if ambiguity_rules[rule_key] not in candidates:
                raise organizer.PoolError(
                    "ambiguity rule for %s is not one of that group's "
                    "selected categories" % reader.seed(record.rank))
            if chosen is None:
                chosen = ambiguity_rules[rule_key]
        if chosen is not None and chosen not in candidates:
            raise organizer.PoolError(
                "choice for %s is not one of that seed's selected categories"
                % reader.seed(record.rank))
        ambiguous_count += 1
        overlap_records += 1
        if chosen:
            assigned_counts[chosen] = assigned_counts.get(chosen, 0) + 1
        else:
            unresolved_count += 1
            for category in candidates:
                pending_counts[category] = pending_counts.get(category, 0) + 1
            group = ambiguity_groups.get(rule_key)
            if group is None and len(ambiguity_groups) < AMBIGUITY_GROUP_LIMIT:
                group = {
                    "rule_key": rule_key,
                    "candidates": candidates,
                    "records": 0,
                    "unresolved_records": 0,
                    "samples": [],
                }
                ambiguity_groups[rule_key] = group
            elif group is None:
                ambiguity_groups_truncated = True
            elif group["candidates"] != candidates:
                raise organizer.PoolError(
                    "ambiguity candidate sets produced the same rule token")
            if group is not None:
                group["records"] += 1
                group["unresolved_records"] += 1
                if len(group["samples"]) < 3:
                    group["samples"].append(reader.seed(record.rank))
        if len(ambiguous) < AMBIGUITY_SAMPLE_LIMIT:
            ambiguous.append({
                "seed": reader.seed(record.rank),
                "rank": record.rank,
                "rule_key": rule_key,
                "candidates": candidates,
                "choice": chosen if choice_key else "",
                "resolved_by_rule": bool(
                    not choice_key and rule_key in ambiguity_rules),
            })
    _operation_cancelled(cancel_check)
    selected = set(available) if selected_filter is None else selected_filter
    unknown = sorted(selected - set(available))
    if unknown:
        raise organizer.PoolError(
            "selected category is absent from snapshot: %s" % unknown[0])
    if not selected:
        raise organizer.PoolError(
            "snapshot has no known tag/Legendary/voucher occurrence categories")
    unused = sorted(set(choices) - used_choices)
    if unused:
        raise organizer.PoolError(
            "choice plan contains a seed/rank not used by this split: %s" % unused[0])
    unused_rules = sorted(set(ambiguity_rules) - used_rules)
    if unused_rules:
        raise organizer.PoolError(
            "choice plan contains an ambiguity rule not used by this split: %s" %
            unused_rules[0])
    categories = []
    for category in sorted(
            selected, key=lambda value: _assignment_sort_key(
                value, available, group_by_filter)):
        row = dict(available[category])
        # Candidate counts can overlap. Assigned counts are projected output
        # membership after applying the current ambiguity choices.
        row["association_records"] = candidate_counts.get(category, 0)
        row["records"] = assigned_counts.get(category, 0)
        row["pending_ambiguities"] = pending_counts.get(category, 0)
        categories.append(row)
    source = dict(inspection["source"]) if summary_projection \
        else organizer.source_summary(reader)
    policy = str(request.get("unmatchedPolicy", "stop")).lower()
    if policy not in ("stop", "keep", "remainder", "omit"):
        raise organizer.PoolError("unknown unmatched-seed policy")
    remainder_label = ""
    if policy == "keep":
        remainder_label = "Unmatched seeds"
    elif policy == "remainder":
        remainder_label = str(request.get("remainderName", "Needs review")).strip()
        if not remainder_label:
            raise organizer.PoolError("give the review/remainder pool a name")
    prefix = sanitize_prefix(
        request.get("prefix"), os.path.basename(reader.path))
    outputs = []
    for row in categories:
        if not row["records"] and not row["pending_ambiguities"]:
            continue
        outputs.append({
            "category_id": row["category_id"],
            "label": row["label"],
            "name": prefix + "--" + organizer.safe_filename(row["category_id"]),
            "records": row["records"],
            "pending_ambiguities": row["pending_ambiguities"],
            "records_exact": row["pending_ambiguities"] == 0,
            "kind": "category",
        })
    if unmatched and remainder_label:
        remainder_id = "remainder:%s" % quote(remainder_label, safe="_.-")
        outputs.append({
            "category_id": remainder_id,
            "label": remainder_label,
            "name": prefix + "--" + organizer.safe_filename(remainder_id),
            "records": unmatched,
            "pending_ambiguities": 0,
            "records_exact": True,
            "kind": "unmatched",
        })
    publication_dir = os.path.dirname(os.path.abspath(reader.path))
    for output in outputs:
        output["exists"] = os.path.exists(
            os.path.join(publication_dir, output["name"]))
        output["collision_status"] = "exists" if output["exists"] else "available"
    blockers = []
    if len(outputs) > organizer.MAX_SPLIT_OUTPUTS:
        blockers.append(
            "Split would create %d non-empty pools; choose fewer categories "
            "or run separate splits (maximum %d outputs per publication)." %
            (len(outputs), organizer.MAX_SPLIT_OUTPUTS))
    if unresolved_count:
        blockers.append(
            "Choose one destination for %s seed(s) with multiple %s." % (
                format(unresolved_count, ","),
                "locations" if group_by_filter else "exact categories"))
    if unmatched and policy == "stop":
        blockers.append(
            "Choose whether to keep, name, or omit %s unmatched seed(s)." %
            format(unmatched, ","))
    report_name = os.path.basename(_unique_report_path(
        publication_dir, prefix, reader.snapshot_token))
    collisions = [row["name"] for row in outputs if row["exists"]]
    if collisions:
        blockers.append("Output already exists: %s" % collisions[0])
    output_memberships = sum(row["records"] for row in outputs)
    unique_copied_records = (
        reader.records - unmatched + (unmatched if remainder_label else 0)
        if assignment_mode == split_policy.MODE_MATCHING_COPIES
        else sum(assigned_counts.values()) +
        (unmatched if remainder_label else 0))
    token = split_policy.reviewed_plan_identity({
        "snapshot": reader.snapshot_token,
        "assignment_mode": assignment_mode,
        "group_by_filter": group_by_filter or "",
        "selected_categories": sorted(selected),
        "choices": sorted(choices.items()),
        "ambiguity_rules": sorted(ambiguity_rules.items()),
        "unmatched_policy": policy,
        "remainder_label": remainder_label,
        "output_prefix": prefix,
        "outputs": [(row["name"], row["records"], row["pending_ambiguities"])
                    for row in outputs],
        "report_name": report_name,
    })
    result = {
        "organizer_schema": 2,
        "assignment_mode": assignment_mode,
        "planning_mode": "summary_projection" if summary_projection
        else "record_scan",
        "source": source,
        "source_snapshot_id": reader.snapshot_token,
        "group_by_filter": group_by_filter or "",
        "selected_categories": sorted(selected),
        "categories": categories,
        "ambiguous_count": ambiguous_count,
        "unresolved_ambiguities": unresolved_count,
        "ambiguous": ambiguous,
        "ambiguities_truncated": ambiguous_count - len(ambiguous),
        "ambiguity_groups": [dict(
            ambiguity_groups[key],
            choice=ambiguity_rules.get(key, ""))
            for key in sorted(ambiguity_groups)],
        "ambiguity_groups_truncated": ambiguity_groups_truncated,
        "unrepresented_ambiguities": unresolved_count - sum(
            row["unresolved_records"] for row in ambiguity_groups.values()),
        "ambiguity_rules": dict(ambiguity_rules),
        "choices": dict(choices),
        "unmatched_count": unmatched,
        "overlap_records": overlap_records,
        "unique_copied_records": unique_copied_records,
        "output_memberships": output_memberships,
        "opaque_associations": opaque_associations,
        "notices": _notices(source),
        "compatibility": {
            "status": "ready",
            "format": "BSP%d" % source["schema"],
            "snapshot_pinned": True,
            "state": "finished" if source["complete"] else "paused",
            "coverage": "complete" if source["coverage_complete"] else "provisional",
            "metadata": "exact locations",
        },
        "publication": {
            "ready": not blockers,
            "blockers": blockers,
            "outputs": outputs,
            "output_count": len(outputs),
            "output_prefix": prefix,
            "report_name": report_name,
            "directory": publication_dir,
            "unmatched_policy": policy,
            "omitted_records": unmatched if policy == "omit" else 0,
            "overlap_records": overlap_records,
            "unique_copied_records": unique_copied_records,
            "output_memberships": output_memberships,
            "source_retained": True,
            "atomic_per_file": True,
            "transaction_atomic": False,
            "writer_locked": True,
            "parent_snapshot_id": reader.snapshot_token,
            "plan_token": token,
            "coverage_complete": source["coverage_complete"],
            "occurrence_metadata_complete": source["occurrence_metadata_complete"],
        },
    }
    _remember_reviewed_split(
        reader, selected_ids, choices, ambiguity_rules, request, result)
    return result


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


def execute_split(name, request, pool_dir=None, cancel_check=None):
    """Verify a pinned plan, then publish staged no-overwrite outputs."""
    _operation_cancelled(cancel_check)
    root = _pool_root(pool_dir)
    os.makedirs(root, exist_ok=True)
    reader = verified_source_reader(
        name, root, cancel_check=cancel_check)
    expected = str(request.get("snapshot", "")).lower()
    if expected != reader.snapshot_token:
        raise organizer.PoolError(
            "source changed from snapshot %s to %s; inspect it again" % (
                expected or "(missing)", reader.snapshot_token))
    selected = request.get("selectedCategories")
    assignment_mode = _assignment_mode(request)
    group_by_filter = _group_by_filter(request)
    choice_plan = request.get("choicePlan")
    reviewed_token = str(request.get("reviewedPlanToken") or "")
    choices = _choice_document(choice_plan, reader)
    ambiguity_rules = _ambiguity_rules(choice_plan, reader)
    preflight = None
    if reviewed_token:
        preflight = _reviewed_split_preflight(
            reader, selected, choices, ambiguity_rules, request,
            reviewed_token)
    if preflight is None:
        # A cache miss, changed request, changed file identity, or newly
        # occupied destination takes the original exact validation path.
        preflight = build_split_plan(
            reader, selected, choice_plan, request,
            cancel_check=cancel_check)
    if reviewed_token and reviewed_token != preflight["publication"]["plan_token"]:
        raise organizer.PoolError(
            "split choices changed after review; prepare the plan again")
    if not preflight["publication"]["ready"]:
        preflight["completed"] = False
        return preflight
    policy = preflight["publication"]["unmatched_policy"]
    prefix = sanitize_prefix(request.get("prefix"), name)
    category_rows = []
    remainder_id = None
    for output in preflight["publication"]["outputs"]:
        category_rows.append({
            "category_id": output["category_id"],
            "label": output["label"],
            "records": output["records"],
        })
        if output["kind"] == "unmatched":
            remainder_id = output["category_id"]
    report = {
        "organizer_schema": 2,
        "assignment_mode": assignment_mode,
        "source": preflight["source"],
        "source_snapshot_id": reader.snapshot_token,
        "group_by_filter": group_by_filter or "",
        "choices": choices,
        "ambiguity_rules": ambiguity_rules,
        "selected_categories": preflight["selected_categories"],
        "categories": category_rows,
        "ambiguous_count": preflight["ambiguous_count"],
        "unresolved_ambiguities": 0,
        "ambiguous": preflight["ambiguous"],
        "ambiguities_truncated": preflight["ambiguities_truncated"],
        "ambiguity_groups": preflight["ambiguity_groups"],
        "ambiguity_groups_truncated": preflight["ambiguity_groups_truncated"],
        "unrepresented_ambiguities": preflight["unrepresented_ambiguities"],
        "unmatched_count": preflight["unmatched_count"],
        "overlap_records": preflight["overlap_records"],
        "unique_copied_records": preflight["unique_copied_records"],
        "output_memberships": preflight["output_memberships"],
        "unmatched_policy": policy,
        "notices": preflight["notices"],
        "preflight": preflight["publication"],
        "outputs": [],
    }
    stage = tempfile.mkdtemp(prefix=".organizer-stage-", dir=root)
    report_path = os.path.join(root, preflight["publication"]["report_name"])
    publications = []
    committed = False
    report_write_started = False
    publish_locks = ExitStack()
    try:
        _operation_cancelled(cancel_check)
        publish_locks.enter_context(organizer.pool_writer_guard(report_path))
        if os.path.exists(report_path):
            raise organizer.PoolError(
                "publication changed after review; prepare the split plan again")
        stage_report = os.path.join(stage, "split-report.json")
        report, completed = organizer.write_prepared_split(
            reader, stage, preflight["selected_categories"], choices,
            category_rows, report, stage_report,
            remainder_id=remainder_id, cancel_check=cancel_check,
            ambiguity_rules=ambiguity_rules,
            group_by_filter=group_by_filter,
            assignment_mode=assignment_mode)
        if not completed:
            report["completed"] = False
            return report

        for output in report["outputs"]:
            _operation_cancelled(cancel_check)
            old_path = output["path"]
            final_name = prefix + "--" + os.path.basename(old_path)
            final_path = os.path.join(root, final_name)
            publish_locks.enter_context(organizer.pool_writer_guard(final_path))
            if os.path.exists(final_path):
                raise organizer.PoolError(
                    "output already exists; choose another prefix: %s" % final_name)
            publications.append((old_path, final_path, output))
        for _old_path, _final_path, _output in publications:
            _operation_cancelled(cancel_check)
        # Staging and seed_pools share a filesystem. Hard links are atomic and
        # refuse overwrite; rollback below removes only stage-owned inodes.
        seed_pool_mutations.link_many_no_overwrite(
            (old_path, final_path)
            for old_path, final_path, _output in publications)
        for _old_path, final_path, output in publications:
            output["path"] = final_path
            output["name"] = os.path.basename(final_path)
        report["completed"] = True
        _operation_cancelled(cancel_check)
        if os.path.exists(report_path):
            raise organizer.PoolError(
                "publication changed after review; prepare the split plan again")
        report["report_path"] = report_path
        report_write_started = True
        organizer.atomic_json(report_path, report)
        committed = True
        return report
    except BaseException:
        rollback_outputs = not committed
        if rollback_outputs and report_write_started and os.path.exists(report_path):
            try:
                seed_pool_mutations.remove(report_path)
            except OSError:
                # A completed report must never be left pointing at pools we
                # then remove. If report rollback fails, preserve its complete
                # output set and surface the original error.
                rollback_outputs = False
        if rollback_outputs:
            for old_path, final_path, _output in publications:
                try:
                    # The planned stage file remains available until finally,
                    # so inode identity closes the post-link/pre-bookkeeping
                    # interruption window without deleting a foreign file.
                    seed_pool_mutations.rollback_link(old_path, final_path)
                except OSError:
                    pass
        raise
    finally:
        primary_error = sys.exc_info()[0] is not None
        cleanup_error = None
        try:
            publish_locks.close()
        except BaseException as exc:
            cleanup_error = exc
        try:
            shutil.rmtree(stage, ignore_errors=True)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        # Once every output and the completed report are durable, a cleanup
        # failure must not turn success into a reported failure. Before that
        # point, surface cleanup trouble only when it would not mask the
        # original publication error.
        if cleanup_error is not None and not committed and not primary_error:
            raise cleanup_error


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


def _combine_readers(request, pool_dir=None, pin_snapshots=False,
                     cancel_check=None):
    names, operation = _ordered_combine_names(request)
    expected = request.get("snapshots", {})
    if pin_snapshots and not isinstance(expected, dict):
        raise organizer.PoolError("combined-pool snapshot pins are malformed")
    readers = []
    for name in names:
        _operation_cancelled(cancel_check)
        reader = verified_source_reader(
            name, pool_dir, cancel_check=cancel_check)
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
            "title": "Different original filters are supported",
            "text": ("The new pool uses the selected %s membership rule and "
                     "remembers which original filter or filters each seed came "
                     "from. It does not incorrectly require every original "
                     "filter to be true at once.") % context.operation.upper(),
        })
    if not context.coverage_complete:
        notices.append({
            "kind": "warning",
            "title": "The new pool will have provisional coverage",
            "text": ("Every currently committed seed is still included in the "
                     "comparison. However, at least one input is paused, covers "
                     "only part of its search range, or uses a different range. "
                     "The new pool therefore cannot claim that its search was exhaustive."),
        })
    if not context.metadata_complete:
        notices.append({
            "kind": "warning",
            "title": "Some inputs do not contain exact match details",
            "text": ("At least one older BSP2 input records which seeds matched "
                     "but not the exact tag, Legendary, or voucher location for "
                     "each seed. Those seeds remain usable in the combined pool, "
                     "but missing details cannot be recreated."),
        })
    if context.operation == "difference":
        notices.append({
            "kind": "info",
            "title": "Difference starts with the chosen base pool",
            "text": ("The first/base pool is kept, then every seed found in any "
                     "other selected pool is removed."),
        })
    return notices


def _combine_input_matrix(readers, operation):
    first = readers[0]
    rows = []
    for index, reader in enumerate(readers):
        source = organizer.source_summary(reader)
        rows.append({
            "name": os.path.basename(reader.path),
            "role": "base" if operation == "difference" and index == 0 else (
                "subtract" if operation == "difference" else "member"),
            "snapshot_id": reader.snapshot_token,
            "records": reader.records,
            "schema": reader.schema,
            "state": "finished" if reader.complete else "paused",
            "coverage": "complete" if reader.coverage_complete else "provisional",
            "metadata": "exact locations" if reader.occurrence_metadata_complete
            else "membership only",
            "modelver": reader.modelver,
            "catalog_hash": "%016x" % reader.catalog_hash,
            "space": reader.space_name,
            "charset": reader.charset,
            "seedspace": reader.seedspace,
            "range_start": reader.range_start,
            "range_end": reader.range_end,
            "family_id": source["family_id"],
            "lineage_id": source["lineage_id"],
            "composite": reader.is_composite,
            "composite_operation": reader.composite_operation,
            "composite_expression_text": source["composite_expression_text"],
            "matches_model": reader.modelver == first.modelver,
            "matches_catalog": reader.catalog_hash == first.catalog_hash,
            "matches_seed_space": (
                reader.charset == first.charset and reader.seedspace == first.seedspace),
        })
    return rows


def _combine_compatibility_checks(readers):
    first = readers[0]
    identities = [
        (reader.snapshot_id, reader.segment_id, reader.membership_digest,
         reader.records) for reader in readers]
    checks = [
        {
            "field": "rng_model",
            "label": "Game/RNG data version",
            "status": "pass" if all(reader.modelver == first.modelver
                                      for reader in readers) else "fail",
            "detail": ("The selected pools must interpret a seed using the same "
                       "Brainstorm RNG-data version."),
            "blocking": True,
        },
        {
            "field": "catalog",
            "label": "Unlocked-content catalog",
            "status": "pass" if all(reader.catalog_hash == first.catalog_hash
                                      for reader in readers) else "fail",
            "detail": ("The selected pools must have been built from the same "
                       "catalog of available Jokers, tags, vouchers, and other content."),
            "blocking": True,
        },
        {
            "field": "seed_space",
            "label": "Seed alphabet and space",
            "status": "pass" if all(
                reader.charset == first.charset and reader.seedspace == first.seedspace
                for reader in readers) else "fail",
            "detail": ("All inputs must use the same seed alphabet and space "
                       "(Natural, Vanilla-settable, or All possible)."),
            "blocking": True,
        },
        {
            "field": "snapshots",
            "label": "No input selected twice",
            "status": "pass" if len(set(identities)) == len(identities) else "fail",
            "detail": "Each selected file must represent a different recorded pool version.",
            "blocking": True,
        },
        {
            "field": "ranges",
            "label": "Search ranges",
            "status": "pass" if all(
                (reader.range_start, reader.range_end) ==
                (first.range_start, first.range_end) for reader in readers)
            else "warning",
            "detail": ("Matching search ranges can preserve complete coverage. "
                       "Different ranges may still be combined, but the new pool "
                       "will be marked provisional."),
            "blocking": False,
        },
        {
            "field": "metadata",
            "label": "Recorded match details",
            "status": "pass" if all(reader.occurrence_metadata_complete
                                      for reader in readers) else "warning",
            "detail": ("Older BSP2 pools can be compared by seed membership, "
                       "but missing per-seed match locations cannot be recreated."),
            "blocking": False,
        },
    ]
    return checks


def build_combine_plan(request, pool_dir=None, cancel_check=None):
    _operation_cancelled(cancel_check)
    names, operation, readers = _combine_readers(
        request, pool_dir, cancel_check=cancel_check)
    input_matrix = _combine_input_matrix(readers, operation)
    checks = _combine_compatibility_checks(readers)
    blockers = [row["detail"] for row in checks
                if row["blocking"] and row["status"] == "fail"]
    context = None
    if not blockers:
        try:
            context = organizer.prepare_combine(readers, operation)
        except organizer.PoolError as exc:
            blockers.append(str(exc))
    output_name = sanitize_combine_name(request.get("name"))
    root = _pool_root(pool_dir)
    output_filename = output_name + ".bspool"
    output_exists = os.path.exists(os.path.join(root, output_filename))
    if output_exists:
        blockers.append("Output already exists: %s" % output_filename)
    report_name = os.path.basename(_unique_combine_report_path(root, output_name))
    if context is not None:
        report = context.as_dict()
        notices = _combine_notices(context)
        source_names = [os.path.basename(reader.path)
                        for reader in context.readers]
    else:
        report = {
            "operation": operation,
            "inputs": [organizer.source_summary(reader) for reader in readers],
            "input_count": len(readers),
            "branches": [],
            "branch_count": 0,
            "operands": [],
            "operand_count": len(readers),
            "coverage_complete": False,
            "metadata_complete": all(
                reader.occurrence_metadata_complete for reader in readers),
            "criteria_differ": len({reader.criteria_hash for reader in readers}) > 1,
            "expression": {},
            "expression_text": "Blocked until compatibility errors are resolved",
        }
        notices = [{
            "kind": "error",
            "title": "Inputs are not compatible",
            "text": blockers[0],
        }]
        source_names = [os.path.basename(reader.path) for reader in readers]
    plan_token = _plan_token("combine", {
        "operation": operation,
        "source_names": names,
        "snapshots": [(os.path.basename(reader.path), reader.snapshot_token)
                      for reader in readers],
        "output_name": output_filename,
        "label": str(request.get("label") or output_name).strip() or output_name,
        "report_name": report_name,
        "compatible": context is not None,
    })
    report.update({
        "organizer_schema": 3,
        "source_names": source_names,
        "selected_names": names,
        "snapshots": {
            os.path.basename(reader.path): reader.snapshot_token
            for reader in readers
        },
        "compatible": context is not None,
        "compatibility": {
            "compatible": context is not None,
            "checks": checks,
            "inputs": input_matrix,
            "blockers": blockers,
        },
        "publication": {
            "ready": context is not None and not blockers,
            "blockers": blockers,
            "name": output_filename,
            "label": str(request.get("label") or output_name).strip() or output_name,
            "report_name": report_name,
            "plan_token": plan_token,
            "directory": root,
            "output_exists": output_exists,
            "atomic_per_file": True,
            "transaction_atomic": False,
            "writer_locked": True,
            "record_count": None,
            "record_count_note": "Exact result count is streamed during publication.",
        },
        "notices": notices,
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


def _unique_combine_report_path(root, output_name):
    report_path = os.path.join(root, output_name + "-combine-report.json")
    if os.path.exists(report_path):
        suffix = 2
        while os.path.exists(os.path.join(
                root, "%s-combine-report-%d.json" % (output_name, suffix))):
            suffix += 1
        report_path = os.path.join(
            root, "%s-combine-report-%d.json" % (output_name, suffix))
    return report_path


def execute_combine(request, pool_dir=None, cancel_check=None):
    _operation_cancelled(cancel_check)
    root = _pool_root(pool_dir)
    os.makedirs(root, exist_ok=True)
    _names, operation, readers = _combine_readers(
        request, root, pin_snapshots=True, cancel_check=cancel_check)
    try:
        return _execute_combine_with_readers(
            request, root, _names, operation, readers, cancel_check)
    finally:
        # Reader indexes are useful between review and execute, but a finished
        # 64-way operation must not pin all of them in the long-lived server.
        _evict_cached_reader_paths(reader.path for reader in readers)


def _execute_combine_with_readers(
        request, root, _names, operation, readers, cancel_check):
    # Recompute the plan from the pinned reader instances immediately before
    # writing, so compatibility and output semantics cannot drift between the
    # preview and publication steps.
    context = organizer.prepare_combine(readers, operation)
    output_name = sanitize_combine_name(request.get("name"))
    output_path = os.path.join(root, output_name + ".bspool")
    label = str(request.get("label") or output_name).strip() or output_name
    reviewed = request.get("reviewedPublication")
    if reviewed is not None:
        if not isinstance(reviewed, dict):
            raise organizer.PoolError("reviewed publication is malformed")
        if reviewed.get("name") != os.path.basename(output_path):
            raise organizer.PoolError(
                "output name changed after review; check compatibility again")
        expected_plan = _plan_token("combine", {
            "operation": operation,
            "source_names": _names,
            "snapshots": [(os.path.basename(reader.path), reader.snapshot_token)
                          for reader in readers],
            "output_name": os.path.basename(output_path),
            "label": label,
            "report_name": str(reviewed.get("report_name") or ""),
            "compatible": True,
        })
        if reviewed.get("plan_token") != expected_plan:
            raise organizer.PoolError(
                "combine inputs or output changed after review; check compatibility again")
        report_name = str(reviewed.get("report_name") or "")
        if not report_name or report_name != os.path.basename(report_name):
            raise organizer.PoolError("reviewed report name is malformed")
        report_path = os.path.join(root, report_name)
        if os.path.exists(report_path):
            raise organizer.PoolError(
                "publication changed after review; check compatibility again")
    else:
        report_path = _unique_combine_report_path(root, output_name)
    _operation_cancelled(cancel_check)
    result = organizer.combine_pools(
        context.readers, output_path, operation, label,
        cancel_check=cancel_check)
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
    result["report_path"] = report_path
    try:
        _operation_cancelled(cancel_check)
        organizer.atomic_json(report_path, result)
    except BaseException:
        # Publication is one user action: never leave a seemingly successful
        # pool behind when its pinned provenance report could not be saved.
        try:
            seed_pool_mutations.remove(result["path"])
        except OSError:
            pass
        raise
    return result


def _run_analysis(callback):
    event = _begin_operation("analysis")
    try:
        return callback(event.is_set)
    finally:
        _finish_operation("analysis", event)


def record_export_projection(records, committed_data_bytes):
    """Return a deliberately approximate full-record NDJSON size projection."""
    records = max(0, int(records or 0))
    committed_data_bytes = max(0, int(committed_data_bytes or 0))
    estimated = (
        records * RECORD_EXPORT_BASE_BYTES
        + committed_data_bytes * RECORD_EXPORT_DATA_EXPANSION)
    return {
        "estimated_bytes": estimated,
        "huge": estimated >= RECORD_EXPORT_HUGE_BYTES,
        "estimate": True,
    }


def _with_record_export_projection(report):
    """Attach export guidance to an inspection without changing its cache."""
    source = report.get("source") if isinstance(report, dict) else None
    if isinstance(source, dict):
        source["record_export"] = record_export_projection(
            source.get("records", 0),
            source.get("committed_data_bytes", 0))
    return report


def run_inspect(name, pool_dir=None, ambiguity_limit=100):
    return _run_analysis(lambda cancelled: _with_record_export_projection(
        inspect_source(
            name, pool_dir, ambiguity_limit, cancel_check=cancelled)))


def _cached_split_plan(request, pool_dir, cancel_check):
    """Review a projectable selection from the verified inspection cache."""
    selected = request.get("selectedCategories")
    group_by_filter = _group_by_filter(request)
    choice_plan = request.get("choicePlan")
    if _choice_plan_has_individual_decisions(choice_plan):
        return None
    path = resolve_source(request.get("source", ""), pool_dir)
    identity = _summary_identity(path)
    report = _cached_summary(path, identity)
    if report is None or not _summary_can_project(
            report, selected, group_by_filter):
        return None
    _operation_cancelled(cancel_check)
    reader = _InspectionPlanReader(path, report["source"])
    expected = str(request.get("snapshot", "")).lower()
    if expected and expected != reader.snapshot_token:
        raise organizer.PoolError(
            "source changed from snapshot %s to %s; inspect it again" % (
                expected, reader.snapshot_token))
    result = build_split_plan(
        reader, selected, choice_plan, request,
        cancel_check=cancel_check, inspection=report)
    _operation_cancelled(cancel_check)
    if _summary_identity(path) != identity:
        raise organizer.PoolError(
            "source changed while its cached assignments were being reviewed; inspect it again")
    return result


def run_split_plan(request, pool_dir=None):
    def analyze_plan(cancelled):
        cached = _cached_split_plan(request, pool_dir, cancelled)
        if cached is not None:
            return cached
        reader = verified_source_reader(
            request.get("source", ""), pool_dir, cancel_check=cancelled)
        expected = str(request.get("snapshot", "")).lower()
        if expected and expected != reader.snapshot_token:
            raise organizer.PoolError(
                "source changed from snapshot %s to %s; inspect it again" % (
                    expected, reader.snapshot_token))
        return build_split_plan(
            reader, request.get("selectedCategories"),
            request.get("choicePlan"), request, cancel_check=cancelled)
    return _run_analysis(analyze_plan)


def run_combine_plan(request, pool_dir=None):
    return _run_analysis(lambda cancelled: build_combine_plan(
        request, pool_dir, cancel_check=cancelled))


def run_split(name, request, pool_dir=None):
    """Run one split under the shared library lock and cancellation registry."""
    if not SPLIT_LOCK.acquire(False):
        raise organizer.PoolError("another organizer split is still running")
    event = None
    try:
        event = _begin_operation("split")
        return execute_split(
            name, request, pool_dir, cancel_check=event.is_set)
    finally:
        if event is not None:
            _finish_operation("split", event)
        SPLIT_LOCK.release()


def run_combine(request, pool_dir=None):
    """Run one combine under the shared library lock and cancellation registry."""
    if not COMBINE_LOCK.acquire(False):
        raise organizer.PoolError(
            "another pool combine or format update is still running")
    event = None
    try:
        event = _begin_operation("combine")
        return execute_combine(
            request, pool_dir, cancel_check=event.is_set)
    finally:
        if event is not None:
            _finish_operation("combine", event)
        COMBINE_LOCK.release()


def run_format_upgrade(request, pool_dir=None):
    """Serialize one native BSP3 upgrade with other pool-wide transforms."""
    if not COMBINE_LOCK.acquire(False):
        raise organizer.PoolError(
            "another pool combine or format update is still running")
    event = None
    try:
        event = _begin_operation("upgrade")
        return execute_format_upgrade(
            request, pool_dir, cancel_check=event.is_set)
    finally:
        if event is not None:
            _finish_operation("upgrade", event)
        COMBINE_LOCK.release()


def _record_export_line(reader, record):
    value = {
        "seed": reader.seed(record.rank),
        "rank": record.rank,
        "occurrences": [item.as_dict() for item in record.occurrences],
    }
    return (json.dumps(
        value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def iter_record_export(reader, cancel_check=None,
                       chunk_bytes=RECORD_EXPORT_CHUNK_BYTES):
    """Yield compact NDJSON in bounded batches instead of per-record writes."""
    try:
        chunk_bytes = int(chunk_bytes)
    except (TypeError, ValueError):
        raise ValueError("record export chunk size must be an integer")
    if chunk_bytes <= 0:
        raise ValueError("record export chunk size must be positive")
    _operation_cancelled(cancel_check)
    records = reader.iter_records(cancel_check=cancel_check)
    with ExitStack() as resources:
        close_records = getattr(records, "close", None)
        if close_records is not None:
            resources.callback(close_records)
        batch = bytearray()
        for record in records:
            _operation_cancelled(cancel_check)
            line = _record_export_line(reader, record)
            if batch and len(batch) + len(line) > chunk_bytes:
                _operation_cancelled(cancel_check)
                yield bytes(batch)
                batch.clear()
            if len(line) > chunk_bytes:
                # A pathological single occurrence record remains one complete
                # NDJSON line rather than being split into invalid JSON.
                _operation_cancelled(cancel_check)
                yield line
            else:
                batch.extend(line)
        _operation_cancelled(cancel_check)
        if batch:
            yield bytes(batch)


def _write_http_chunk(handler, payload):
    """Write one HTTP/1.1 chunk without materializing another payload copy."""
    handler.wfile.write(("%x\r\n" % len(payload)).encode("ascii"))
    if payload:
        handler.wfile.write(payload)
    handler.wfile.write(b"\r\n")


def serve_record_export(handler, parsed, pool_dir=None):
    """Validate, register, and stream one snapshot-pinned local download.

    The first batch is prepared before committing HTTP headers.  A validation
    or cancellation failure can therefore still return normal JSON.  The body
    uses HTTP/1.1 chunked framing and writes its terminating chunk only after a
    fully validated traversal.  A late checksum, identity, cancellation, or
    I/O failure therefore becomes an incomplete download instead of a
    plausible-looking truncated NDJSON file.
    """
    query = parse_qs(parsed.query)
    name = query.get("source", [""])[0]
    request_id = _record_export_request_id(
        query.get("request_id", [""])[0])
    event = None
    chunks = None
    response_started = False
    try:
        _set_record_export_status(request_id, "running")
        event = _begin_operation("export")
        reader = verified_source_reader(
            name, pool_dir, cancel_check=event.is_set)
        expected = query.get("snapshot", [""])[0].lower()
        if expected != reader.snapshot_token:
            raise organizer.PoolError(
                "source snapshot changed; inspect before exporting")
        projection = record_export_projection(
            reader.records, reader.data_bytes)
        chunks = iter_record_export(
            reader, cancel_check=event.is_set)
        first_chunk = next(chunks, None)
        _operation_cancelled(event.is_set)

        filename = "%s-%s-records.ndjson" % (
            os.path.splitext(name)[0], reader.snapshot_token[:8])
        filename = re.sub(r"[^A-Za-z0-9._+-]+", "-", filename)
        handler.send_response(200)
        # From this point onward a second status line would corrupt the
        # download, even if end_headers itself encounters a disconnected peer.
        response_started = True
        handler.send_header(
            "Content-Type", "application/x-ndjson; charset=utf-8")
        handler.send_header(
            "Content-Disposition",
            "attachment; filename=%s" % quote(filename))
        handler.send_header("Cache-Control", "no-store")
        handler.send_header(
            "X-Brainstorm-Record-Count", str(reader.records))
        handler.send_header(
            "X-Brainstorm-Estimated-Bytes",
            str(projection["estimated_bytes"]))
        handler.send_header(
            "X-Brainstorm-Export-Warning",
            "huge" if projection["huge"] else "none")
        handler.send_header("Transfer-Encoding", "chunked")
        # Downloads are deliberately one request per connection. Chunked
        # framing—not EOF—still determines whether the body completed.
        handler.send_header("Connection", "close")
        handler.close_connection = True
        handler.end_headers()
        if first_chunk is not None:
            _operation_cancelled(event.is_set)
            _write_http_chunk(handler, first_chunk)
        for chunk in chunks:
            _operation_cancelled(event.is_set)
            _write_http_chunk(handler, chunk)
        _write_http_chunk(handler, b"")
        flush = getattr(handler.wfile, "flush", None)
        if flush is not None:
            flush()
        _set_record_export_status(request_id, "completed")
        return projection
    except Exception as exc:
        state = "cancelled" if isinstance(exc, OperationCancelled) else "failed"
        _set_record_export_status(request_id, state, str(exc))
        if response_started:
            # Do not write the terminating zero chunk. HTTP clients can then
            # distinguish this failure from a complete close-delimited file.
            handler.close_connection = True
            return None
        raise
    finally:
        try:
            if chunks is not None:
                close_chunks = getattr(chunks, "close", None)
                if close_chunks is not None:
                    close_chunks()
        except Exception:
            # Iterator teardown is best-effort, but it must never strand the
            # cancellation registry or provoke a second HTTP response.
            handler.close_connection = True
        finally:
            if event is not None:
                _finish_operation("export", event)


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
.appnav a.active{background:#29213b;color:#ded6ff}.toolnav{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}
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
button:focus-visible,select:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #8cc8ff;outline-offset:2px}
button.go{background:linear-gradient(135deg,#27814c,#35aa62)}button.cancel{background:#522a30;border-color:#88434b;color:#ffd1d1}button.ghost{background:transparent;border-color:#3b425b;color:#d7d2df}button.small{min-height:33px;padding:5px 9px;background:#292e42;border-color:#3d435c;font-size:12px}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin-top:13px}.two{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.source{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:13px}.metric{padding:11px;border:1px solid #292e40;border-radius:11px;background:var(--card2)}
.metric span{display:block;color:var(--faint);font-size:11px}.metric b{display:block;margin-top:3px;overflow-wrap:anywhere}.mono{font:11px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}
.notice{margin-top:10px;padding:11px 12px;border:1px solid #355273;border-radius:11px;background:#142131;color:#b9dafb;font-size:12px}.notice.warning{border-color:#6a5726;background:#29230f;color:#f2d67c}.notice.error{border-color:#74383e;background:#2b171b;color:#ffb5b5}.notice strong{display:block;margin-bottom:2px;color:inherit}
.explain{margin-top:12px;padding:12px 13px;border:1px solid #343b52;border-radius:11px;background:#11151f;color:#bbb7c7;font-size:12px;line-height:1.55}.explain strong{display:block;margin-bottom:3px;color:#eee9f5}.explain ul{margin:7px 0 0;padding-left:19px}.explain li+li{margin-top:4px}
.categories{display:grid;gap:7px;max-height:370px;overflow:auto;margin-top:12px;padding-right:3px}.category{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:start;padding:10px;border:1px solid #292e40;border-radius:10px;background:var(--card2)}
.category input{width:17px;height:17px;margin-top:2px;accent-color:var(--purple)}.category b{font-size:12px}.category span{color:var(--faint);font-size:11px}.category details{margin-top:3px;color:var(--faint);font-size:10px}.category summary{cursor:pointer}.category code{display:block;margin-top:3px;overflow-wrap:anywhere;white-space:normal}.count{color:#d9d4e4!important;white-space:nowrap}
.side{position:sticky;top:18px;display:grid;gap:18px}.summary dl{margin:11px 0 0}.summary div{display:grid;grid-template-columns:95px 1fr;gap:9px;padding:9px 0;border-top:1px solid #302c40;font-size:12px}.summary dt{color:var(--faint)}.summary dd{margin:0;text-align:right;overflow-wrap:anywhere}
.actions{display:grid;gap:9px;margin-top:14px}.actions button{width:100%}.error{margin-top:10px;color:#ffadad;font-size:13px;white-space:pre-wrap}.hint{color:var(--faint);font-size:12px}
.plan{margin-top:18px}.planbar{display:flex;justify-content:space-between;gap:12px;align-items:center}.pill{padding:4px 8px;border-radius:999px;background:#3b3017;color:#f4d46b;font-size:10px;font-weight:850;text-transform:uppercase}.pill.ok{background:#163424;color:#80e4a6}
.amb{display:grid;gap:8px;margin-top:12px}.ambrow{display:grid;grid-template-columns:100px 1fr;gap:10px;align-items:center;padding:10px;border:1px solid #292e40;border-radius:10px;background:var(--card2)}.ambrow b{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}.ambrow label,.ambrow .hint{display:block}.ambrow select{min-height:38px;margin-top:6px}
.pager{display:flex;justify-content:space-between;gap:10px;align-items:center;margin-top:11px;color:var(--muted);font-size:12px}.result{margin-top:13px}.output{padding:10px 0;border-top:1px solid #302c40;font-size:12px}.output b{display:block;overflow-wrap:anywhere}.output span{color:var(--muted)}
.workstatus{display:grid;grid-template-columns:auto 1fr;gap:11px;align-items:center;margin-top:12px;padding:12px 13px;border:1px solid #44527a;border-radius:11px;background:#141b2c;color:#dbe5ff}.workstatus b,.workstatus span{display:block}.workstatus span{margin-top:3px;color:#a9b4ce;font-size:12px;line-height:1.4}.workstatus.success{border-color:#39704d;background:#13241a;color:#bdecca}.workstatus.error{border-color:#7b3d45;background:#2a171b;color:#ffc0c5}.spinner{width:20px;height:20px;border:3px solid #536181;border-top-color:#9cc7ff;border-radius:50%;animation:spin .8s linear infinite}.workstatus.success .spinner,.workstatus.error .spinner{display:none}@keyframes spin{to{transform:rotate(360deg)}}
.manifest{display:grid;gap:7px;margin-top:12px}.manifestrow{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:12px;padding:10px;border:1px solid #30364b;border-radius:10px;background:#10131c;font-size:12px}.manifestrow b,.manifestrow span{overflow-wrap:anywhere}.manifestrow small{display:block;color:var(--faint)}.manifestrow .count{text-align:right}
.checkgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:12px}.check{padding:10px;border:1px solid #30364b;border-radius:10px;background:#10131c;font-size:11px}.check b{display:block}.check.pass{border-color:#2d6845;color:#a8e7bf}.check.warning{border-color:#6a5726;color:#f2d67c}.check.fail{border-color:#74383e;color:#ffb5b5}.statehint{margin-top:10px;padding:9px 10px;border-left:3px solid #7361aa;background:#171426;color:#cfc6e7;font-size:12px}.source-retained{color:#a8e7bf}.reviewtitle{margin:18px 0 0;font-size:14px}.live{min-height:1px}
.sectionbar{display:flex;justify-content:space-between;gap:12px;align-items:center;margin-top:17px}.sectionbar h3{margin:0;font-size:14px}.sectionbar .row{margin:0}.subsection{margin-top:18px}.subsection h3{margin:0;font-size:14px}.subsection>p{margin:4px 0 0;color:var(--muted);font-size:12px}
.exporthint{margin-top:7px;text-align:right}.exporthint.warning{padding:9px 10px;border:1px solid #6a5726;border-radius:9px;background:#29230f;color:#f2d67c}
.inventory{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}.inventoryitem{padding:10px;border:1px solid #2d3245;border-radius:10px;background:#11141d}.inventoryitem b,.inventoryitem span{display:block}.inventoryitem b{font-size:12px}.inventoryitem span{margin-top:2px;color:var(--faint);font-size:11px}.inventoryitem.empty{opacity:.55}
.choicecards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}.choicecard{position:relative;display:flex;gap:9px;align-items:flex-start;padding:11px;border:1px solid #30364b;border-radius:11px;background:#10131c;cursor:pointer}.choicecard:hover{border-color:#4a526f}.choicecard:has(input:checked){border-color:#8c77d4;background:#241e36;box-shadow:inset 0 0 0 1px #8c77d4}.choicecard:has(input:disabled){opacity:.48;cursor:not-allowed}.choicecard input{flex:0 0 auto;width:17px;height:17px;margin:2px 0 0;accent-color:var(--purple)}.choicecard b,.choicecard small{display:block}.choicecard b{font-size:12px}.choicecard small{margin-top:2px;color:var(--faint);font-size:10px;line-height:1.4}
.selection-summary{margin-top:10px;padding:10px 12px;border-left:3px solid #7361aa;background:#171426;color:#cfc6e7;font-size:12px}.selection-summary strong{color:#f0ebfa}
.choicegroup{min-width:0;margin:18px 0 0;padding:0;border:0}.choicegroup legend{padding:0;color:#eee9f5;font-size:14px;font-weight:800}.choicegroup>p{margin:4px 0 0;color:var(--muted);font-size:12px}.policycards{grid-template-columns:repeat(3,minmax(0,1fr))}
.advanced{margin-top:13px;border:1px solid #30364b;border-radius:11px;background:#11141d}.advanced summary{cursor:pointer;padding:11px 12px;color:#c9c5d3;font-size:12px;font-weight:750}.advanced summary:after{content:"＋";float:right;color:var(--faint)}.advanced[open] summary:after{content:"−"}.advancedbody{padding:0 12px 12px}.advancedbody .row:first-child{margin-top:0}
.filename-preview{margin-top:8px;padding:9px 11px;border:1px dashed #3b4159;border-radius:9px;background:#0f1119;color:#bdb7ca;font-size:11px;overflow-wrap:anywhere}.filename-preview b{color:#ebe5f5}
.review-actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:15px}.review-actions .go{min-width:230px}.secondary-link{min-height:33px!important;padding:5px 9px!important}
[hidden]{display:none!important}@media(max-width:850px){.grid{grid-template-columns:1fr}.side{position:static}}@media(max-width:680px){.choicecards,.inventory,.policycards{grid-template-columns:1fr}}@media(max-width:580px){.top{display:grid}.two,.source,.combine-settings,.checkgrid{grid-template-columns:1fr}.ambrow,.manifestrow{grid-template-columns:1fr}.card{padding:17px}.appnav{width:100%}.appnav a{flex:1;text-align:center}.categories,.poolchoices{max-height:none}.sectionbar{align-items:flex-start;flex-direction:column}}
</style></head><body><main class="app">
<header class="top"><div class="brand"><div class="mark">B</div><div><h1>Seed Pool Organizer</h1><p class="sub">Create, compare, or update recorded seed pools without changing the originals.</p></div></div><div class="local">Running locally</div></header>
<nav class="appnav"><a id="builderTab" href="/">Build / Search</a><a id="organizerTab" class="active" href="/organize">Organize / Combine</a></nav>
<div class="privacy"><strong>Your pools stay on this computer.</strong> This page only talks to Brainstorm's local organizer. New pools are saved in <code>seed_pools</code> so the mod can see them immediately.</div>
<div class="toolnav" role="tablist" aria-label="Organizer operation"><button class="active" role="tab" aria-selected="true" aria-controls="splitWorkspace" id="splitModeBtn">Create pools from one pool</button><button role="tab" aria-selected="false" aria-controls="combineWorkspace" id="combineModeBtn">Combine seed lists</button><button role="tab" aria-selected="false" aria-controls="formatWorkspace" id="formatModeBtn">Update pool format</button></div>
<div class="grid" id="splitWorkspace" role="tabpanel" aria-labelledby="splitModeBtn"><div class="stack">
<section class="card"><div class="head"><span class="step">1</span><div><h2>Inspect a recorded pool</h2><p class="copy">Choose a pool to see what it contains. Inspection is read-only.</p></div></div>
 <div class="field"><label for="source">Seed pool</label><select id="source"></select></div>
 <div class="notice warning" id="nativeHelperWarning" hidden></div>
 <div class="row"><button class="go" id="inspectBtn" disabled>Loading pools…</button><button class="ghost" id="refreshBtn">Refresh list</button></div>
 <div class="workstatus" id="inspectionStatus" role="status" aria-live="polite" hidden><span class="spinner" aria-hidden="true"></span><div><b id="inspectionTitle">Inspecting pool…</b><span id="inspectionDetail">Reading the committed snapshot.</span></div></div>
 <div id="sourceInfo" hidden>
  <div class="sectionbar"><h3>Pool overview</h3><button class="ghost small secondary-link" id="exportBtn">Export all records (.ndjson)</button></div>
  <div class="hint exporthint" id="exportHint"></div>
  <div class="source" id="sourceMetrics"></div>
  <div class="subsection"><h3>Recorded data</h3><p>The types of search results available for creating new pools.</p><div class="inventory" id="filterInventory"></div></div>
  <details class="advanced"><summary>Technical pool details</summary><div class="advancedbody source" id="sourceTechnical"></div></details>
  <div id="notices"></div>
 </div>
</section>
<section class="card" id="categoryCard" tabindex="-1" hidden><div class="head"><span class="step">2</span><div><h2>Choose the new pools</h2><p class="copy">Choose a result type, a recorded target, and the locations that should each become a new pool.</p></div></div>
 <fieldset class="choicegroup"><legend>What kind of result should organize the new pools?</legend><div class="choicecards" id="filterKinds"></div>
  <details class="advanced" id="exactDetails"><summary>Advanced: split by exact recorded event metadata</summary><div class="advancedbody"><label class="choicecard" for="exactKind"><input type="radio" name="filterKind" class="filterKind" id="exactKind" value="exact"><span><b>Exact technical metadata</b><small>Separates source, occurrence number, flags, Ante, and blind. Usually unnecessary.</small></span></label></div></details>
 </fieldset>
 <div class="field" id="targetField"><label for="organizeBy" id="targetLabel">Choose a recorded target</label><select id="organizeBy"></select></div>
 <div class="selection-summary" id="organizeByInfo"><strong>Choose a recorded target.</strong></div>
 <div class="sectionbar"><div><h3 id="locationQuestion">Choose locations to create pools for</h3><div class="hint" id="locationHelp">Select all that apply. Every checked location creates one new pool.</div></div><div class="row"><button class="small" id="allBtn">Select all</button><button class="small" id="noneBtn">Clear</button></div></div>
 <div class="hint" id="selectedLocationCount" aria-live="polite"></div>
 <div class="categories" id="categories"></div>
 <details class="advanced" id="exclusiveDetails"><summary>Advanced: require each seed to go to only one pool</summary><div class="advancedbody"><label class="choicecard" for="exclusiveMode"><input type="checkbox" id="exclusiveMode"><span><b>Use an exclusive split</b><small>Overlapping seeds must be assigned to exactly one destination. Use this for legacy saved decision files.</small></span></label></div></details>
 </section>
<section class="card" id="planCard" hidden><div class="head"><span class="step">3</span><div><h2>Name and preview new pools</h2><p class="copy">Choose what to do with other seeds, then preview exact filenames and counts. Previewing does not create files.</p></div></div>
 <fieldset class="choicegroup"><legend id="unmatchedQuestion">Other seeds</legend><p id="unmatchedHelp">Seeds that do not match a selected location stay in the unchanged source pool.</p>
  <input type="hidden" id="policy" value="omit">
  <div class="choicecards"><label class="choicecard" for="otherPool"><input type="checkbox" id="otherPool"><span><b>Also create an Other seeds pool</b><small>Copies every seed that matches none of the selected destinations.</small></span></label></div>
 </fieldset>
 <div class="field" id="remainderField" hidden><label for="remainder">Other seeds pool name</label><input type="text" id="remainder" value="Other seeds"></div>
 <div class="field"><label for="prefix">New file name</label><input type="text" id="prefix"><span class="hint">This is the shared name. Brainstorm appends each selected location and a unique ID; existing files are never overwritten.</span><div class="filename-preview" id="filenamePreview"></div></div>
 <div class="row"><button id="planBtn">Preview new pools</button></div>
 <div class="workstatus" id="reviewStatus" role="status" aria-live="polite" hidden><span class="spinner" aria-hidden="true"></span><div><b id="reviewTitle">Reviewing seed assignments…</b><span id="reviewDetail">Calculating destinations from the checked locations.</span></div></div>
 </section>
<section class="card" id="reviewCard" hidden><div class="head"><span class="step">4</span><div><h2>Review files to create</h2><p class="copy">Check every filename and seed count before creating it. The original pool remains unchanged.</p></div></div>
 <div class="workstatus" id="updateStatus" role="status" aria-live="polite" hidden><span class="spinner" aria-hidden="true"></span><div><b id="updateTitle">Updating preview…</b><span id="updateDetail">Applying the selected destinations.</span></div></div>
 <div class="plan" id="plan" hidden><div class="planbar"><div><b id="planTitle">Seeds that fit more than one checked category</b><div class="hint" id="planHint"></div></div><span class="pill" id="choicePill" role="status" aria-live="polite">Choose destinations</span></div><div class="row"><button class="ghost small" id="clearRulesBtn" hidden>Clear shared decisions</button><button class="ghost small" id="clearChoicesBtn" hidden>Clear individual seed decisions</button></div><div class="amb" id="ambiguities"></div><div class="pager" id="pager"></div></div>
 <div class="review-actions"><button class="ghost" id="applyDecisionsBtn" hidden>Update preview</button></div>
 <div id="splitPublication" hidden><h3 class="reviewtitle">Files that will be created</h3><div class="statehint" id="splitPublicationState"></div><div class="manifest" id="splitManifest"></div><div class="hint" id="splitReport"></div></div>
 <div class="review-actions"><button class="go" id="splitBtn" disabled>Create these seed pools</button></div>
 <details class="advanced"><summary>Advanced: exclusive split decision files</summary><div class="advancedbody"><p class="hint">Loading an existing decision file switches to Exclusive split. Decision files are tied to this exact unchanged source snapshot.</p><div class="row"><button class="ghost" id="saveBtn" disabled>Save decisions (.json)</button><button class="ghost" id="loadBtn">Load saved decisions</button><input type="file" id="loadFile" accept="application/json,.json" hidden></div></div></details>
</section></div>
<aside class="side"><section class="card summary"><h2>Organizer summary</h2><dl>
 <div><dt>Source pool</dt><dd id="sumSource">Choose a pool</dd></div><div><dt>Recorded seeds</dt><dd id="sumRecords">—</dd></div><div><dt>Selected locations</dt><dd id="sumCategories">—</dd></div><div><dt>Overlapping seeds</dt><dd id="sumAmbiguous">—</dd></div><div><dt>Other seeds</dt><dd id="sumUnmatched">—</dd></div><div><dt>New files</dt><dd id="sumPublication">Not previewed</dd></div></dl>
 <div class="actions"><button class="cancel" id="analysisCancelBtn" hidden>Cancel preview</button><button class="cancel" id="exportCancelBtn" hidden>Cancel record export</button><button class="cancel" id="splitCancelBtn" hidden>Cancel file creation</button></div><div class="error" id="error" role="alert"></div><div class="result live" id="result" aria-live="polite"></div>
</section></aside></div>
<div class="grid" id="combineWorkspace" role="tabpanel" aria-labelledby="combineModeBtn" hidden><div class="stack">
<section class="card"><div class="head"><span class="step">1</span><div><h2>Choose two or more seed lists</h2><p class="copy">Select the pools to compare. They stay unchanged; Brainstorm creates one separate result.</p></div></div>
 <div class="row"><button class="small" id="combineAllBtn">Select all readable pools</button><button class="small" id="combineNoneBtn">Clear pool selection</button><button class="ghost" id="combineRefreshBtn">Refresh list</button></div>
 <div class="poolchoices" id="combineChoices"><div class="hint">Loading seed pools…</div></div>
</section>
<section class="card"><div class="head"><span class="step">2</span><div><h2>Choose a rule and name the result</h2><p class="copy">The compatibility check is read-only and runs before file creation.</p></div></div>
 <div class="hint">Numbered pieces of one distributed build use <a id="mergeLink" href="/">Merge distributed build parts</a><span id="standaloneMerge" hidden> the Seed Pool Builder instead.</span></div>
 <fieldset class="choicegroup"><legend>Which seeds should the new pool keep?</legend><input type="hidden" id="combineOperation" value="union"><div class="choicecards" id="combineRules">
  <label class="choicecard"><input type="radio" name="combineRule" class="combineOp" value="union" checked><span><b>Any selected pool</b><small>Union · keep a seed found in at least one selected pool.</small></span></label>
  <label class="choicecard"><input type="radio" name="combineRule" class="combineOp" value="intersection"><span><b>Every selected pool</b><small>Intersection · keep a seed only when every pool contains it.</small></span></label>
  <label class="choicecard"><input type="radio" name="combineRule" class="combineOp" value="difference"><span><b>First pool, minus the others</b><small>Difference · remove seeds also found in another selected pool.</small></span></label>
 </div></fieldset>
 <div class="combine-settings"><div class="field" id="combineBaseField" hidden><label for="combineBase">Start with this pool</label><select id="combineBase"></select><span class="hint">Seeds also present in another selected pool are removed.</span></div>
 <div class="field"><label for="combineName">New pool filename</label><input type="text" id="combineName" value="combined-pool"><span class="hint">The organizer adds <code>.bspool</code>. An existing file with the same name will not be overwritten.</span></div>
 <div class="field"><label for="combineLabel">Name shown inside Brainstorm</label><input type="text" id="combineLabel" value="Combined seed pool"></div></div>
 <div class="row"><button id="combinePlanBtn">Check compatibility and preview file</button></div><div id="combineNotices"></div>
 <details class="advanced" id="combineTechnical" hidden><summary>Technical compatibility and source history</summary><div class="advancedbody"><div class="checkgrid" id="combineChecks"></div><div id="combineBranches" class="branchlist"></div></div></details>
 <div id="combinePublication" hidden><h3 class="reviewtitle">File that will be created</h3><div class="manifest" id="combineManifest"></div><div class="hint" id="combineReport"></div><div class="review-actions"><button class="go" id="combineCreateBtn" disabled>Create combined seed pool</button></div></div>
</section></div>
<aside class="side"><section class="card summary"><h2>Combine summary</h2><dl>
 <div><dt>Rule</dt><dd id="combineSumOperation">Union</dd></div><div><dt>Selected pools</dt><dd id="combineSumInputs">Choose at least two</dd></div><div><dt>Can combine?</dt><dd id="combineSumCompatibility">Not reviewed</dd></div><div><dt>Recorded filters</dt><dd id="combineSumBranches">—</dd></div><div><dt>Membership rule</dt><dd id="combineSumExpression">—</dd></div><div><dt>Search coverage</dt><dd id="combineSumCoverage">—</dd></div><div><dt>Match details</dt><dd id="combineSumMetadata">—</dd></div></dl>
 <div class="actions"><button class="cancel" id="combineAnalysisCancelBtn" hidden>Cancel compatibility check</button><button class="cancel" id="combineCancelBtn" hidden>Cancel file creation</button></div><div class="error" id="combineError" role="alert"></div><div class="result live" id="combineResult" aria-live="polite"></div>
</section></aside></div>
<div class="grid" id="formatWorkspace" role="tabpanel" aria-labelledby="formatModeBtn" hidden><div class="stack">
<section class="card"><div class="head"><span class="step">1</span><div><h2>Check or update a pool</h2><p class="copy">Choose a <code>.bspool</code> file from Brainstorm's <code>seed_pools</code> folder. Checking reads only its header. A supported update creates a separate BSP4 copy; the original file is never changed.</p></div></div>
 <div class="field"><label for="formatSource">Seed pool</label><select id="formatSource"></select></div>
 <div class="row"><button class="go" id="formatCheckBtn" disabled>Loading pools…</button><button class="ghost" id="formatRefreshBtn">Refresh list</button></div>
 <div class="workstatus" id="formatStatus" role="status" aria-live="polite" hidden><span class="spinner" aria-hidden="true"></span><div><b id="formatStatusTitle">Checking pool format…</b><span id="formatStatusDetail">Reading the saved pool header.</span></div></div>
 <div class="hint" id="formatElapsed" aria-hidden="true" hidden></div>
</section>
<section class="card" id="formatPlanCard" hidden><div class="head"><span class="step">2</span><div><h2 id="formatPlanTitle">Pool format</h2><p class="copy" id="formatPlanCopy"></p></div></div>
 <div class="source" id="formatMetrics"></div><div id="formatNotices"></div>
</section></div>
<aside class="side"><section class="card summary"><h2>Format summary</h2><dl>
 <div><dt>Selected pool</dt><dd id="formatSumSource">Choose a pool</dd></div><div><dt>Current format</dt><dd id="formatSumCurrent">—</dd></div><div><dt>Recorded seeds</dt><dd id="formatSumRecords">—</dd></div><div><dt>Update status</dt><dd id="formatSumStatus">Not checked</dd></div><div><dt>New file</dt><dd id="formatSumOutput">—</dd></div></dl>
 <div class="actions"><button class="go" id="formatUpdateBtn" disabled>Create BSP4 copy</button><button class="cancel" id="formatCancelBtn" hidden>Cancel update</button></div><div class="error" id="formatError" role="alert"></div><div class="result live" id="formatResult" aria-live="polite"></div>
</section></aside></div></main>
<iframe id="recordExportFrame" title="Record export download" hidden></iframe>
<script>
const $=id=>document.getElementById(id);
const UNIFIED=location.pathname.startsWith("/organize");
const apiPath=path=>UNIFIED?"/organizer"+path:path;
class OrganizerWorkflowState{
 constructor(){this.pools=[];this.split={inspection:null,source:"",plan:null,choices:{},rules:{},reviewedFingerprint:"",running:false};this.combine={plan:null,reviewedFingerprint:"",running:false};this.format={plan:null,running:false};this.export={request:"",timer:0}}
 setPools(value){this.pools=value}
 resetSplitInspection(){Object.assign(this.split,{inspection:null,source:"",plan:null,choices:{},rules:{},reviewedFingerprint:""})}
 acceptInspection(value,source){this.resetSplitInspection();this.split.inspection=value;this.split.source=source}
 invalidateSplitReview(){this.split.reviewedFingerprint=""}
 clearSplitPlan(clearDecisions=false){this.split.plan=null;this.invalidateSplitReview();if(clearDecisions){this.split.choices={};this.split.rules={}}}
 reviewSplit(value,fingerprint){this.split.plan=value;this.split.choices={...(value.choices||{})};this.split.rules={...(value.ambiguity_rules||{})};this.split.reviewedFingerprint=fingerprint}
 completeSplit(){this.clearSplitPlan(true)}
 startSplit(){this.split.running=true}
 finishSplit(){this.split.running=false}
 invalidateCombine(){this.combine.plan=null;this.combine.reviewedFingerprint=""}
 reviewCombine(value,fingerprint){this.combine.plan=value;this.combine.reviewedFingerprint=fingerprint}
 startCombine(){this.combine.running=true}
 finishCombine(){this.combine.running=false}
 reviewFormat(value){this.format.plan=value}
 resetFormat(){this.format.plan=null}
 startFormat(){this.format.running=true}
 finishFormat(){this.format.running=false}
 startExport(request){this.export.request=request}
 scheduleExport(timer){this.export.timer=timer}
 finishExport(){clearTimeout(this.export.timer);this.export.timer=0;this.export.request=""}
}
const workflowState=new OrganizerWorkflowState();
const FILTER_KINDS=[
 {id:"legendary",label:"Legendary",plural:"Legendaries"},
 {id:"tag",label:"Tag",plural:"Tags"},
 {id:"voucher",label:"Voucher",plural:"Vouchers"},
];
const esc=v=>String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const fmt=n=>Number(n||0).toLocaleString();
const fmtBytes=n=>{n=Number(n||0);if(n>=1073741824)return `${(n/1073741824).toFixed(2)} GiB`;if(n>=1048576)return `${(n/1048576).toFixed(2)} MiB`;if(n>=1024)return `${(n/1024).toFixed(1)} KiB`;return `${fmt(n)} bytes`};
async function api(path,data){const opt=data?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data),cache:"no-store"}:{cache:"no-store"};const r=await fetch(apiPath(path),opt);let v;try{v=await r.json()}catch(_e){throw Error(`The local Organizer returned an unreadable response (${r.status}).`)}if(!r.ok||v.error){const error=Error(v.error||`Request failed (${r.status})`);error.code=v.error_code||"";error.publicationState=v.publication_state||"";throw error}return v}
function renderNativeHelperWarning(v){
 const box=$("nativeHelperWarning");if(!box)return;
 const min=v.native_summary_min_bytes||0;
 const slow=(v.pools||[]).filter(p=>!p.error&&p.bytes>=min);
 if(v.native_summary!==false||!slow.length){box.hidden=true;box.innerHTML="";return}
 box.hidden=false;
 box.innerHTML=`<strong>Native seed-pool helper missing — large pools will be very slow</strong>`
  +`Brainstorm could not find its seed-pool helper, so ${slow.length} listed pool(s) must be read record by record in Python. `
  +`Inspecting or previewing one is exact but can take tens of minutes per step, and the result is not cached. `
  +`Put the helper in the mod's <code>native</code> folder, or reinstall the full package, to restore the fast path.`;
}
function fail(e){$("error").textContent=e.message||String(e)}
function clear(){$("error").textContent="";$("result").innerHTML=""}
function inspectionState(kind,title,detail){const box=$("inspectionStatus");box.hidden=false;box.className=`workstatus${kind?" "+kind:""}`;$("inspectionTitle").textContent=title;$("inspectionDetail").textContent=detail}
function reviewState(kind,title,detail){const box=$("reviewStatus");box.hidden=false;box.className=`workstatus${kind?" "+kind:""}`;$("reviewTitle").textContent=title;$("reviewDetail").textContent=detail}
function updateState(kind,title,detail){const box=$("updateStatus");box.hidden=false;box.className=`workstatus${kind?" "+kind:""}`;$("updateTitle").textContent=title;$("updateDetail").textContent=detail}
function formatState(kind,title,detail){const box=$("formatStatus");box.hidden=false;box.className=`workstatus${kind?" "+kind:""}`;$("formatStatusTitle").textContent=title;$("formatStatusDetail").textContent=detail}
function renderNoticeList(id,rows){$(id).innerHTML=(rows||[]).map(n=>`<div class="notice ${esc(n.kind)}"><strong>${esc(n.title)}</strong>${esc(n.text)}</div>`).join("")}
function notices(rows){renderNoticeList("notices",rows)}
function sourceMetrics(s){return `<div class="metric"><span>File format</span><b>BSP${fmt(s.schema)}</b></div><div class="metric"><span>Recorded seeds</span><b>${fmt(s.records)}</b></div><div class="metric"><span>Pool state</span><b>${s.complete?"Finished":"Paused / incomplete"}</b></div><div class="metric"><span>Search coverage</span><b>${s.coverage_complete?"Complete":"Provisional"}</b></div><div class="metric"><span>Per-seed details</span><b>${s.metadata_capable?"Exact match details recorded":"Seed membership only"}</b></div>`}
function sourceTechnical(s){const range=Number(s.range_end)>Number(s.range_start)?`${fmt(s.range_start)}–${fmt(Number(s.range_end)-1)}`:"Not recorded";return `<div class="metric"><span>Snapshot ID</span><b class="mono">${esc(s.snapshot_id)}</b></div><div class="metric"><span>Source history ID</span><b class="mono">${esc(s.lineage_id||"legacy / unrecorded")}</b></div><div class="metric"><span>Pool / family ID</span><b class="mono">${esc(s.pool_id||"—")} / ${esc(s.family_id||"—")}</b></div><div class="metric"><span>Recorded byte size</span><b>${fmtBytes(s.committed_data_bytes)}</b></div><div class="metric"><span>Seed space / rank range</span><b>${esc(s.space||"unknown")} · ${range}</b></div><div class="metric"><span>Search model</span><b>Model ${fmt(s.modelver)} · catalog ${esc(s.catalog_hash||"—")}</b></div>`}
function recordExportInfo(s){return s&&s.record_export?s.record_export:{estimated_bytes:0,huge:false}}
function renderRecordExport(s){const info=recordExportInfo(s),size=fmtBytes(info.estimated_bytes);$("exportHint").className=`hint exporthint${info.huge?" warning":""}`;$("exportHint").textContent=info.huge?`Huge export: about ${size} for ${fmt(s.records)} records. This text download may take a long time and needs substantial free disk space.`:`Projected full-record download: about ${size}. Actual size varies with the number of recorded matches.`}
function finishRecordExport(state,error){workflowState.finishExport();$("exportBtn").disabled=false;$("exportCancelBtn").hidden=true;$("exportCancelBtn").disabled=false;$("exportCancelBtn").textContent="Cancel record export";if(state==="completed")$("result").textContent="Record export completed and was fully verified.";else if(state==="cancelled")$("result").textContent="Record export cancelled. Discard any partial browser download.";else{fail(Error(error||"Record export failed. Discard any partial browser download."));$("result").textContent="Record export did not complete; no complete download was reported."}}
async function pollRecordExport(id){if(id!==workflowState.export.request)return;try{const value=await api(`/api/export/status?request_id=${encodeURIComponent(id)}`);if(id!==workflowState.export.request)return;if(["completed","cancelled","failed"].includes(value.state)){finishRecordExport(value.state,value.error);return}}catch(e){if(id===workflowState.export.request){finishRecordExport("failed",e.message);return}}workflowState.scheduleExport(setTimeout(()=>pollRecordExport(id),400))}
function startRecordExport(){const inspection=workflowState.split.inspection;if(!inspection||workflowState.export.request)return;const info=recordExportInfo(inspection.source),size=fmtBytes(info.estimated_bytes);if(info.huge&&!confirm(`This full record export is projected to be about ${size}. It may take a long time and use substantial disk space. Continue?`))return;clear();const id=(globalThis.crypto&&crypto.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random().toString(16).slice(2)}`).replace(/[^A-Za-z0-9_-]/g,"");workflowState.startExport(id);$("exportBtn").disabled=true;$("exportCancelBtn").hidden=false;$("result").textContent=`Record export started (projected size about ${size}). Your pool remains unchanged.`;$("recordExportFrame").src=apiPath(`/api/export?source=${encodeURIComponent(workflowState.split.source)}&snapshot=${encodeURIComponent(inspection.source.snapshot_id)}&request_id=${encodeURIComponent(id)}`);workflowState.scheduleExport(setTimeout(()=>pollRecordExport(id),200))}
async function cancelRecordExport(){$("exportCancelBtn").disabled=true;$("exportCancelBtn").textContent="Cancelling…";try{const value=await api("/api/cancel",{operation:"export"});$("result").textContent=value.state==="idle"?"The record export had already finished; checking its final status…":"Record export cancellation requested. Any partial browser download must be discarded."}catch(e){fail(e);$("exportCancelBtn").disabled=false;$("exportCancelBtn").textContent="Cancel record export"}}
function kindInfo(kind){return FILTER_KINDS.find(row=>row.id===kind)||{id:kind,label:"Result",plural:"Results"}}
function filterInventoryHTML(filters){return FILTER_KINDS.map(kind=>{const rows=(filters||[]).filter(row=>row.kind===kind.id),names=rows.map(row=>row.label).join(", ");return `<div class="inventoryitem${rows.length?"":" empty"}"><b>${esc(kind.plural)} · ${fmt(rows.length)}</b><span>${rows.length?esc(names):"Not recorded in this pool"}</span></div>`}).join("")}
function categoryHTML(c,exact=false){const id=exact?c.category_id:c.location_id;const label=c.location_label||c.label;const details=exact?`<details><summary>Technical match details</summary>${esc(c.label)}<code>${esc(c.category_id)}</code></details>`:"";return `<label class="category"><input type="checkbox" class="cat" value="${esc(id)}" checked><span><b>${esc(label)}</b>${details}</span><span class="count">${fmt(c.records)} seeds</span></label>`}
function selectedKind(){return document.querySelector('input[name="filterKind"]:checked')?.value||""}
function selectedFilter(){const inspection=workflowState.split.inspection;if(!inspection)return null;const id=$("organizeBy").value;if(!id||id==="__exact__")return null;return (inspection.filters||[]).find(row=>row.filter_id===id)||null}
function groupByFilter(){const row=selectedFilter();return row?row.filter_id:""}
function assignmentMode(){return $("exclusiveMode").checked?"exclusive":"matching_copies"}
function renderFilterKinds(){
 const inspection=workflowState.split.inspection;if(!inspection)return;const filters=inspection.filters||[],recommended=filters.find(row=>row.filter_id===inspection.recommended_filter_id),firstAvailable=FILTER_KINDS.find(kind=>filters.some(row=>row.kind===kind.id)),initialKind=recommended?.kind||firstAvailable?.id||((inspection.categories||[]).length?"exact":"");
 $("filterKinds").innerHTML=FILTER_KINDS.map(kind=>{const count=filters.filter(row=>row.kind===kind.id).length,checked=initialKind===kind.id;return `<label class="choicecard"><input type="radio" name="filterKind" class="filterKind" value="${kind.id}" ${checked?"checked":""} ${count?"":"disabled"}><span><b>${kind.label}</b><small>${count?`${fmt(count)} recorded ${count===1?"target":"targets"}`:"Not recorded in this pool"}</small></span></label>`}).join("");
 $("exactKind").disabled=!(inspection.categories||[]).length;$("exactKind").checked=initialKind==="exact";renderFilterOptions();
}
function renderFilterOptions(){
 const inspection=workflowState.split.inspection;if(!inspection)return;const kind=selectedKind(),exact=kind==="exact",previous=$("organizeBy").value,rows=(inspection.filters||[]).filter(row=>row.kind===kind),recommended=rows.find(row=>row.filter_id===inspection.recommended_filter_id);
 $("targetField").hidden=exact;
 if(exact){$("organizeBy").innerHTML='<option value="__exact__">Exact technical metadata</option>';$("organizeBy").value="__exact__";$("exactDetails").open=true}
 else{
  const info=kindInfo(kind);$("targetLabel").textContent=`Choose a ${info.label.toLowerCase()}`;
  $("organizeBy").innerHTML=rows.length?rows.map(row=>`<option value="${esc(row.filter_id)}">${esc(row.label)} · ${fmt(row.location_count)} location${row.location_count===1?"":"s"}${row.filter_id===inspection.recommended_filter_id?" · recommended":""}</option>`).join(""):'<option value="">No recorded targets</option>';
  if(rows.some(row=>row.filter_id===previous))$("organizeBy").value=previous;else if(recommended)$("organizeBy").value=recommended.filter_id;
 }
 workflowState.clearSplitPlan(true);$("reviewCard").hidden=true;$("plan").hidden=true;$("saveBtn").disabled=true;renderInspectLocations();invalidateSplitReview("Selection changed — preview the new pools");
}
function updateUnmatchedCopy(){
 const filter=selectedFilter(),exact=selectedKind()==="exact",name=filter?.label||"the chosen exact event";
 $("unmatchedQuestion").textContent="Other seeds";
 $("unmatchedHelp").textContent=exact?"Seeds that match none of the checked exact events stay in the unchanged source pool.":`Seeds without ${name} at a checked location stay in the unchanged source pool.`;
}
function updateFilenamePreview(){
 const raw=$("prefix").value||"new-pool",prefix=raw.replace(/[^A-Za-z0-9._+-]+/g,"-").replace(/^[-.]+|[-.]+$/g,"").slice(0,64)||"new-pool",category=document.querySelector(".cat:checked")?.value||selectedFilter()?.filter_id||"selected-location",suffix=category.replace(/[^A-Za-z0-9._+-]+/g,"-").replace(/^[-.]+|[-.]+$/g,"").slice(0,42)||"selected-location";
 $("filenamePreview").innerHTML=`<b>Filename pattern:</b> ${esc(prefix)}--${esc(suffix)}-<span aria-label="unique ID">[unique-id]</span>.bspool`;
}
function updateLocationSelectionSummary(){
 const boxes=[...document.querySelectorAll(".cat")],checked=boxes.filter(box=>box.checked).length;$("selectedLocationCount").textContent=boxes.length?`${fmt(checked)} of ${fmt(boxes.length)} locations selected · ${fmt(checked)} new location pool${checked===1?"":"s"}`:"No locations available";$("sumCategories").textContent=boxes.length?`${fmt(checked)} of ${fmt(boxes.length)}`:"—";$("planBtn").disabled=!checked;updateFilenamePreview();
}
function renderInspectLocations(){
 const inspection=workflowState.split.inspection;if(!inspection)return;const value=$("organizeBy").value,exact=value==="__exact__",filter=selectedFilter();let rows=[];
 if(exact)rows=inspection.categories||[];else if(filter)rows=filter.locations||[];
 $("categories").innerHTML=rows.length?rows.map(row=>categoryHTML(row,exact)).join(""):'<div class="hint">Choose a recorded filter to see its Ante and blind locations.</div>';
 if(filter){
  const repeated=filter.multiple_location_records?(assignmentMode()==="exclusive"?`${fmt(filter.multiple_location_records)} seeds appear at more than one recorded location and will need one destination each.`:`${fmt(filter.multiple_location_records)} seeds appear at more than one recorded location and will be copied into each matching pool.`):"No seeds overlap when every location is selected.";
  $("organizeByInfo").innerHTML=`<strong>${esc(filter.label)}</strong> is recorded in ${fmt(filter.covered_records)} of ${fmt(inspection.source.records)} seeds across ${fmt(filter.location_count)} location${filter.location_count===1?"":"s"}. ${esc(repeated)}`;
  $("sumAmbiguous").textContent=fmt(filter.multiple_location_records);$("sumUnmatched").textContent=fmt(filter.unmatched_records);
  $("locationQuestion").textContent=`Choose ${filter.label} locations to create pools for`;
 }else if(exact){
  $("organizeByInfo").innerHTML=`<strong>Exact technical metadata</strong> Each distinct source, occurrence, flags, Ante, and blind combination is shown separately.`;
  $("sumAmbiguous").textContent=fmt(inspection.ambiguous_count);$("sumUnmatched").textContent=fmt(inspection.unmatched_count);$("locationQuestion").textContent="Choose exact events to create pools for";
 }else{
  $("organizeByInfo").innerHTML="<strong>No target selected.</strong> Choose a recorded result type and target.";
  $("sumCategories").textContent="—";$("sumAmbiguous").textContent="—";$("sumUnmatched").textContent="—";$("locationQuestion").textContent="Choose locations to create pools for";
 }
 updateUnmatchedCopy();updateLocationSelectionSummary();
}

function resetSplitInspection(message="Choose and inspect a pool"){
 workflowState.resetSplitInspection();
 $("exclusiveMode").checked=false;$("exclusiveDetails").open=false;$("otherPool").checked=false;$("policy").value="omit";$("remainderField").hidden=true;
 $("sourceInfo").hidden=true;$("categoryCard").hidden=true;$("planCard").hidden=true;$("reviewCard").hidden=true;$("plan").hidden=true;$("splitPublication").hidden=true;$("reviewStatus").hidden=true;$("updateStatus").hidden=true;
 $("categories").innerHTML="";$("filterKinds").innerHTML="";$("organizeBy").innerHTML="";$("filterInventory").innerHTML="";$("sourceTechnical").innerHTML="";$("saveBtn").disabled=true;$("splitBtn").disabled=true;
 $("sumSource").textContent=message;$("sumRecords").textContent="—";$("sumCategories").textContent="—";$("sumAmbiguous").textContent="—";$("sumUnmatched").textContent="—";$("sumPublication").textContent="Not previewed";
}
function invalidateSplitReview(message="Selections changed — preview the new pools again"){
 workflowState.invalidateSplitReview();$("splitPublication").hidden=true;$("reviewStatus").hidden=true;$("updateStatus").hidden=true;$("splitBtn").disabled=true;$("sumPublication").textContent=message;$("result").innerHTML="";
 if(workflowState.split.plan)$("applyDecisionsBtn").hidden=assignmentMode()!=="exclusive";
}
function invalidateSplitSelection(){workflowState.clearSplitPlan(true);$("reviewCard").hidden=true;$("plan").hidden=true;$("saveBtn").disabled=true;$("sumAmbiguous").textContent="Preview to calculate";$("sumUnmatched").textContent="Preview to calculate";updateLocationSelectionSummary();invalidateSplitReview("Location selection changed — preview the new pools")}
function resetFormatPlan(message="Choose a pool and check its format"){
 workflowState.resetFormat();$("formatPlanCard").hidden=true;$("formatUpdateBtn").disabled=true;$("formatResult").innerHTML="";$("formatError").textContent="";
 $("formatStatus").hidden=true;$("formatStatus").className="workstatus";
 $("formatSumSource").textContent=message;$("formatSumCurrent").textContent="—";$("formatSumRecords").textContent="—";$("formatSumStatus").textContent="Not checked";$("formatSumOutput").textContent="—";
}
async function loadPools(preserve=false){
 if(!preserve){clear();resetSplitInspection();invalidateCombine();if(!workflowState.format.running)resetFormatPlan()}
 const priorSource=$("source").value,priorFormat=$("formatSource").value;const selectedCombine=new Set(combineSelected());
 const inspectButton=$("inspectBtn"),formatButton=$("formatCheckBtn");inspectButton.disabled=true;inspectButton.textContent="Loading pools…";formatButton.disabled=true;formatButton.textContent="Loading pools…";
 try{
  const v=await api("/api/pools");workflowState.setPools(v.pools||[]);
  renderNativeHelperWarning(v);
  $("source").innerHTML=workflowState.pools.length?workflowState.pools.map(p=>`<option value="${esc(p.name)}" ${p.error?"disabled":""}>${esc(p.name)}${p.error?" · unreadable":` · ${fmt(p.records)} seeds · ${p.complete?"finished":"paused"} · ${p.coverage_complete?"complete":"provisional"} coverage`}</option>`).join(""):'<option value="">No .bspool files found</option>';
  $("formatSource").innerHTML=workflowState.pools.length?workflowState.pools.map(p=>`<option value="${esc(p.name)}" ${p.error?"disabled":""}>${esc(p.name)}${p.error?" · unreadable":` · BSP${p.schema} · ${fmt(p.records)} seeds · ${p.complete?"finished":"paused"}`}</option>`).join(""):'<option value="">No .bspool files found</option>';
  if([...$("source").options].some(o=>o.value===priorSource))$("source").value=priorSource;
  if([...$("formatSource").options].some(o=>o.value===priorFormat))$("formatSource").value=priorFormat;
  renderCombineChoices(selectedCombine);
  inspectButton.disabled=!workflowState.pools.some(p=>!p.error);inspectButton.textContent="Inspect pool";
  $("formatSource").disabled=workflowState.format.running;formatButton.disabled=workflowState.format.running||!workflowState.pools.some(p=>!p.error);formatButton.textContent="Check pool format";
 }catch(e){
  workflowState.setPools([]);$("source").innerHTML='<option value="">Pool list could not be loaded</option>';$("formatSource").innerHTML='<option value="">Pool list could not be loaded</option>';renderCombineChoices(selectedCombine);
  inspectButton.disabled=true;inspectButton.textContent="Inspect unavailable";$("formatSource").disabled=true;formatButton.disabled=true;formatButton.textContent="Check unavailable";
  inspectionState("error","Seed pool list failed to load",e.message||String(e));if(!workflowState.format.running){formatState("error","Seed pool list failed to load",e.message||String(e));fail(e)}throw e;
 }
}
async function inspect(){
 clear();const source=$("source").value;
 if(!source){inspectionState("error","No seed pool selected","Wait for the pool list to finish loading, then choose a pool.");return}
 resetSplitInspection(`Inspecting ${source}`);
 const button=$("inspectBtn"),refresh=$("refreshBtn"),picker=$("source"),row=workflowState.pools.find(p=>p.name===source);
 const started=performance.now();
 button.disabled=true;refresh.disabled=true;picker.disabled=true;$("analysisCancelBtn").hidden=false;button.textContent=row?`Inspecting ${fmt(row.records)} seeds…`:"Inspecting pool…";
 inspectionState("",`Inspecting ${source}`,row&&row.records>=32000000?`Verifying a large pool (${fmt(row.records)} committed seeds). First inspection can take several seconds; cached inspections are immediate.`:"Reading and verifying the committed snapshot.");
 const timer=setInterval(()=>{const seconds=Math.floor((performance.now()-started)/1000);$("inspectionDetail").textContent=`Still working — ${seconds}s elapsed. The source pool is not being changed.`},1000);
 // Yield once so the busy state paints before a CPU-heavy local request.
 await new Promise(resolve=>setTimeout(resolve,40));
 try{
 const v=await api("/api/inspect",{source});workflowState.acceptInspection(v,source);
  $("sourceInfo").hidden=false;$("sourceMetrics").innerHTML=sourceMetrics(v.source);$("sourceTechnical").innerHTML=sourceTechnical(v.source);$("filterInventory").innerHTML=filterInventoryHTML(v.filters);renderRecordExport(v.source);notices(v.notices);$("categoryCard").hidden=false;$("planCard").hidden=!v.source.metadata_capable;
  $("prefix").value=source.replace(/\.bspool$/i,"")+"-organized";renderFilterKinds();
  $("sumSource").textContent=source;$("sumRecords").textContent=fmt(v.source.records);$("sumPublication").textContent="Preview needed";$("reviewCard").hidden=true;$("plan").hidden=true;$("splitPublication").hidden=true;$("saveBtn").disabled=true;$("splitBtn").disabled=true;
  const elapsed=(performance.now()-started)/1000,typeCount=FILTER_KINDS.filter(kind=>(v.filters||[]).some(row=>row.kind===kind.id)).length;inspectionState("success","Inspection complete",`${fmt(v.source.records)} seeds with ${fmt((v.filters||[]).length)} recorded target${(v.filters||[]).length===1?"":"s"} across ${fmt(typeCount)} result type${typeCount===1?"":"s"} loaded in ${elapsed<0.1?"under 0.1":elapsed.toFixed(1)}s.`);
  $("categoryCard").scrollIntoView({behavior:"smooth",block:"start"});$("categoryCard").focus({preventScroll:true});
 }catch(e){resetSplitInspection();inspectionState("error","Inspection failed",e.message||String(e));fail(e)
 }finally{clearInterval(timer);$("analysisCancelBtn").hidden=true;button.disabled=false;refresh.disabled=false;picker.disabled=false;button.textContent="Inspect pool"}
}
function selected(){return [...document.querySelectorAll(".cat:checked")].map(x=>x.value).sort()}
function cleanChoiceMap(value){return Object.fromEntries(Object.entries(value||{}).filter(([,destination])=>destination))}
function choiceDoc(){const split=workflowState.split;return {source_snapshot_id:split.inspection.source.snapshot_id,assignment_mode:assignmentMode(),group_by_filter:groupByFilter(),choices:cleanChoiceMap(split.choices),ambiguity_rules:{...split.rules},selected_categories:selected()}}
function splitRequest(){const split=workflowState.split;return {source:split.source,snapshot:split.inspection.source.snapshot_id,assignmentMode:assignmentMode(),groupByFilter:groupByFilter(),selectedCategories:selected(),choicePlan:choiceDoc(),unmatchedPolicy:$("policy").value,remainderName:$("remainder").value,prefix:$("prefix").value}}
function splitFingerprint(){if(!workflowState.split.inspection)return "";const request=splitRequest();return JSON.stringify({source:request.source,snapshot:request.snapshot,assignmentMode:request.assignmentMode,groupByFilter:request.groupByFilter,categories:request.selectedCategories,choices:Object.entries(request.choicePlan.choices).sort(),rules:Object.entries(request.choicePlan.ambiguity_rules).sort(),policy:request.unmatchedPolicy,remainder:request.remainderName,prefix:request.prefix})}
async function prepare(fromReview=false){
 clear();const split=workflowState.split;if(!split.inspection)return;const button=$("planBtn"),applyButton=$("applyDecisionsBtn"),activeButton=fromReview?applyButton:button,request=splitRequest(),fingerprint=splitFingerprint(),row=workflowState.pools.find(p=>p.name===split.source),started=performance.now(),setState=fromReview?updateState:reviewState,detailNode=fromReview?$("updateDetail"):$("reviewDetail");button.disabled=true;applyButton.disabled=true;$("analysisCancelBtn").hidden=false;activeButton.textContent=fromReview?"Updating preview…":"Building preview…";if(fromReview)$("reviewStatus").hidden=true;else $("updateStatus").hidden=true;setState("","Building output preview",row?`Calculating destinations for ${fmt(row.records)} recorded seeds. The source pool is not being changed.`:"Calculating destinations from the selected locations.");
 const timer=setInterval(()=>{const seconds=Math.floor((performance.now()-started)/1000);detailNode.textContent=`Still reviewing — ${seconds}s elapsed. You can cancel safely; the source pool is not being changed.`},1000);
 await new Promise(resolve=>setTimeout(resolve,40));
 try{const v=await api("/api/plan",request);if(fingerprint!==splitFingerprint())throw Error("Selections changed while the preview was running. Preview the current choices again.");workflowState.reviewSplit(v,fingerprint);renderPlan();renderSplitPublication();$("saveBtn").disabled=assignmentMode()!=="exclusive";const elapsed=(performance.now()-started)/1000;setState("success",fromReview?"Preview updated":"Preview ready",v.planning_mode==="summary_projection"?`Reused the verified inspection totals; no full rescan was needed. Finished in ${elapsed<0.1?"under 0.1":elapsed.toFixed(1)}s.`:`Finished the exact record preview in ${elapsed<0.1?"under 0.1":elapsed.toFixed(1)}s.`);$("reviewCard").scrollIntoView({behavior:"smooth",block:"start"})}catch(e){invalidateSplitReview();setState("error","Preview failed",e.message||String(e));fail(e)}finally{clearInterval(timer);$("analysisCancelBtn").hidden=true;button.disabled=false;applyButton.disabled=false;button.textContent="Preview new pools";applyButton.textContent="Update preview"}
}
function unresolved(){const split=workflowState.split;if(!split.plan)return 0;const newlyResolved=(split.plan.ambiguity_groups||[]).reduce((total,g)=>total+(split.rules[g.rule_key]?g.unresolved_records:0),0);return Math.max(0,split.plan.unresolved_ambiguities-newlyResolved)}
function categoryName(id){const split=workflowState.split,rows=split.plan?split.plan.categories:(split.inspection?split.inspection.categories:[]);const c=rows.find(x=>x.category_id===id);return c?c.label:id}
function renderPlan(){
 const split=workflowState.split,plan=split.plan;if(!plan)return;$("reviewCard").hidden=false;const exclusive=plan.assignment_mode==="exclusive";$("plan").hidden=!exclusive;$("applyDecisionsBtn").hidden=!exclusive;const groups=plan.ambiguity_groups||[];
 const noun=plan.group_by_filter?"locations":"exact categories";
 const groupRows=groups.map(g=>{const id=`rule-${g.rule_key}`,description=`${id}-description`,examples=(g.samples||[]).length?` Example seeds: ${esc(g.samples.join(", "))}.`:"";return `<div class="ambrow"><b>${fmt(g.unresolved_records)} seed${g.unresolved_records===1?"":"s"}</b><div><label for="${id}">Which new pool should contain these seeds?</label><span class="hint" id="${description}">They contain the chosen result at ${esc(g.candidates.map(categoryName).join(" and "))}.${examples}</span><select id="${id}" aria-describedby="${description}" data-rule="${esc(g.rule_key)}"><option value="">Choose a destination…</option>${g.candidates.map(c=>`<option value="${esc(c)}" ${split.rules[g.rule_key]===c?"selected":""}>${esc(categoryName(c))}</option>`).join("")}</select></div></div>`}).join("");
 $("ambiguities").innerHTML=groupRows||'<div class="notice"><strong>No overlap decisions needed</strong>Every seed has one destination under the current choices.</div>';
 document.querySelectorAll("[data-rule]").forEach(s=>s.onchange=()=>{if(s.value)split.rules[s.dataset.rule]=s.value;else delete split.rules[s.dataset.rule];invalidateSplitReview("Destinations changed — update the preview");renderStatus()});
  const overflow=plan.unrepresented_ambiguities||0;$("pager").innerHTML=overflow?`The choices above cover ${fmt(plan.unresolved_ambiguities-overflow)} seeds. ${fmt(overflow)} more seeds have another combination of matching ${noun}; apply these destinations to see the next group.`:plan.ambiguities_truncated?`Each choice applies to the full seed count shown; example seeds are only a sample.`:"";
 $("clearRulesBtn").hidden=!Object.keys(split.rules).length;$("clearChoicesBtn").hidden=!Object.keys(split.choices).length;
 renderStatus();
}
function renderStatus(){
 const split=workflowState.split,plan=split.plan;if(!plan)return;const left=unresolved(),reviewed=split.reviewedFingerprint===splitFingerprint();
 $("planTitle").textContent=plan.ambiguous_count?`Seeds found at more than one selected ${plan.group_by_filter?"location":"exact event"}`:"Seed destinations";
 const noun=plan.group_by_filter?"location":"exact event",plural=plan.group_by_filter?"locations":"exact events";const overrideText=Object.keys(split.choices).length?` ${fmt(Object.keys(split.choices).length)} saved individual-seed decision(s) are active.`:"";const ruleText=Object.keys(split.rules).length?` ${fmt(Object.keys(split.rules).length)} shared destination decision(s) are active.`:"";$("planHint").textContent=(plan.unmatched_count?`${fmt(plan.unmatched_count)} seed(s) do not match a selected ${noun}; the option in Step 3 decides whether they are copied to an extra pool. Every seed stays in the original.`:`Every source seed matches at least one selected ${noun}.`)+overrideText+ruleText;
 $("choicePill").textContent=left?`${fmt(left)} destinations needed`:(reviewed?"Preview current":"Update preview");$("choicePill").className="pill"+(!left&&reviewed?" ok":"");$("applyDecisionsBtn").hidden=plan.assignment_mode!=="exclusive"||reviewed;
 $("sumAmbiguous").textContent=fmt(plan.overlap_records||0);$("sumUnmatched").textContent=fmt(plan.unmatched_count);
 const ready=reviewed&&plan.publication&&plan.publication.ready,count=plan.publication?.output_count||0;$("splitBtn").disabled=split.running||!ready;$("splitBtn").textContent=ready?`Create ${fmt(count)} seed pool${count===1?"":"s"}`:"Create these seed pools";$("sumPublication").textContent=ready?`${fmt(count)} file${count===1?"":"s"} ready`:reviewed?(plan.publication.blockers[0]||"Blocked"):"Preview needed";
}
function renderSplitPublication(){
 const split=workflowState.split,plan=split.plan;if(!plan||split.reviewedFingerprint!==splitFingerprint())return;const publication=plan.publication;$("splitPublication").hidden=false;
 const blockers=publication.blockers||[];$("splitPublicationState").innerHTML=publication.ready?`<strong>Ready to create ${fmt(publication.output_count)} new pool file${publication.output_count===1?"":"s"}.</strong> <span class="source-retained">The original source pool will remain unchanged.</span> Existing files will not be overwritten. If creation reports an error or you cancel it, unfinished new files are removed.`:`<strong>More decisions are needed before files can be created.</strong> ${blockers.map(esc).join(" ")}`;
 $("splitManifest").innerHTML=publication.outputs.length?publication.outputs.map(o=>`<div class="manifestrow"><span><b>${esc(o.name)}</b><small>${esc(o.label)} · ${o.kind==="unmatched"?"extra pool for other seeds":plan.group_by_filter?"one selected location":"one exact event"} · ${o.collision_status==="available"?"filename available":"filename already exists"}</small></span><span class="count">${fmt(o.records)}${o.records_exact?" seeds":" assigned + "+fmt(o.pending_ambiguities)+" awaiting a destination"}</span></div>`).join(""):'<div class="notice warning"><strong>No new pool files would be created</strong>The current selections do not produce a nonempty output.</div>';
 const memberships=`${fmt(publication.overlap_records||0)} overlapping seed(s); ${fmt(publication.unique_copied_records||0)} unique copied seed(s); ${fmt(publication.output_memberships||0)} total output memberships.`;$("splitReport").textContent=`A small audit report named ${publication.report_name} will also record the assignment mode and source snapshot. The new pools will have ${publication.coverage_complete?"complete":"provisional"} search coverage. ${memberships} ${publication.omitted_records?fmt(publication.omitted_records)+" other seed(s) will remain only in the source pool.":"Every source seed is represented in at least one new file."}`;
 renderStatus();
}
function download(name,type,text){const a=document.createElement("a");a.href=URL.createObjectURL(new Blob([text],{type}));a.download=name;document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},0)}
function savePlan(){if(!workflowState.split.plan)return;download(`${$("prefix").value||"seed-pool"}-choices.json`,"application/json",JSON.stringify(choiceDoc(),null,2)+"\n")}
function clearDecisionSet(kind){const split=workflowState.split;if(kind==="rules")split.rules={};else split.choices={};workflowState.clearSplitPlan();$("reviewCard").hidden=true;$("plan").hidden=true;$("saveBtn").disabled=true;invalidateSplitReview(kind==="rules"?"Shared destinations cleared — preview again":"Individual seed decisions cleared — preview again")}
function loadPlanFile(file){const r=new FileReader();r.onload=()=>{try{const v=JSON.parse(r.result),split=workflowState.split;if(!split.inspection)throw Error("Inspect the source pool first.");if(String(v.source_snapshot_id||"").toLowerCase()!==split.inspection.source.snapshot_id)throw Error("Those saved decisions belong to a different version of this source pool.");if(String(v.group_by_filter||"")!==groupByFilter())throw Error("Those saved decisions use a different organizing filter.");if(v.assignment_mode&&v.assignment_mode!=="exclusive")throw Error("That file is not an exclusive split decision file.");$("exclusiveMode").checked=true;$("exclusiveDetails").open=true;split.choices={...(v.choices||{})};split.rules={...(v.ambiguity_rules||{})};invalidateSplitReview("Saved decisions loaded — reviewing the exclusive split");prepare()}catch(e){fail(e)}};r.readAsText(file)}
async function split(){
 const split=workflowState.split,plan=split.plan;if(!plan||split.reviewedFingerprint!==splitFingerprint()||!plan.publication.ready)return;clear();workflowState.startSplit();$("splitBtn").disabled=true;$("splitBtn").textContent="Creating new seed pools…";$("splitCancelBtn").hidden=false;
 try{const request=splitRequest();request.reviewedPlanToken=plan.publication.plan_token;const v=await api("/api/split",request);if(!v.completed){workflowState.reviewSplit(v,splitFingerprint());renderPlan();renderSplitPublication();throw Error("More decisions are required before the new pools can be created.")}$("result").innerHTML=`<div class="notice"><strong>Created ${fmt(v.outputs.length)} new seed pool(s)</strong>The original source was kept unchanged. The new pools are ready in the seed_pools folder and will appear in Brainstorm's pool selector. Audit report: ${esc(v.report_path)}</div>`+v.outputs.map(o=>`<div class="output"><b>${esc(o.name)}</b><span>${fmt(o.records)} seeds</span></div>`).join("");workflowState.completeSplit();$("reviewCard").hidden=true;$("plan").hidden=true;$("splitPublication").hidden=true;$("saveBtn").disabled=true;$("splitBtn").disabled=true;$("sumPublication").textContent="Created — preview again to make another split";await loadPools(true)}catch(e){fail(e)}finally{workflowState.finishSplit();$("splitCancelBtn").hidden=true;$("splitBtn").textContent="Create these seed pools";renderStatus()}
}
async function cancelSplit(){$("splitCancelBtn").disabled=true;$("splitCancelBtn").textContent="Cancelling…";try{await api("/api/cancel",{operation:"split"})}catch(e){fail(e)}finally{$("splitCancelBtn").disabled=false;$("splitCancelBtn").textContent="Cancel file creation"}}
async function cancelAnalysis(){$("analysisCancelBtn").disabled=true;$("analysisCancelBtn").textContent="Cancelling…";try{await api("/api/cancel",{operation:"analysis"})}catch(e){fail(e)}finally{$("analysisCancelBtn").disabled=false;$("analysisCancelBtn").textContent="Cancel preview"}}

function combineSelected(){return [...document.querySelectorAll(".combinePick:checked")].map(x=>x.value)}
function renderCombineChoices(selectedValues=new Set(combineSelected())){
 $("combineChoices").innerHTML=workflowState.pools.length?workflowState.pools.map(p=>{const state=p.error?"Unreadable":p.complete?(p.coverage_complete?"Finished · complete search coverage":"Finished · provisional search coverage"):"Paused · committed seeds only";const extra=p.composite?` · already combined from ${fmt(p.composite_operand_count||p.composite_branch_count)} input pools · ${fmt(p.composite_branch_count)} recorded filters`:p.criteria_hash?` · filter ID ${esc(p.criteria_hash.slice(0,8))}`:"";return `<label class="poolchoice"><input type="checkbox" class="combinePick" value="${esc(p.name)}" ${selectedValues.has(p.name)?"checked":""} ${p.error?"disabled":""}><span><b>${esc(p.name)}</b><small>${esc(state)} · ${esc(p.space||"?")} seed space${extra}</small></span><span class="count">${p.error?"—":fmt(p.records)} seeds</span></label>`}).join(""):'<div class="hint">No .bspool files found.</div>';
 document.querySelectorAll(".combinePick").forEach(input=>input.onchange=()=>{updateCombineBase();invalidateCombine()});updateCombineBase();
}
function updateCombineBase(){const names=combineSelected(),old=$("combineBase").value,operation=$("combineOperation").value,labels={union:"Any selected pool",intersection:"Every selected pool",difference:"First pool minus others"};$("combineBase").innerHTML=names.map(name=>`<option value="${esc(name)}">${esc(name)}</option>`).join("");if(names.includes(old))$("combineBase").value=old;$("combineBaseField").hidden=operation!=="difference";$("combineSumInputs").textContent=names.length?`${fmt(names.length)} selected`:"Choose at least two";$("combineSumOperation").textContent=labels[operation]||operation;$("combinePlanBtn").disabled=workflowState.combine.running||names.length<2||!$("combineName").value.trim()}
function invalidateCombine(message="Not reviewed"){
 workflowState.invalidateCombine();$("combineCreateBtn").disabled=true;$("combineBranches").innerHTML="";$("combineChecks").innerHTML="";$("combineTechnical").hidden=true;$("combinePublication").hidden=true;$("combineNotices").innerHTML="";$("combineSumCompatibility").textContent=message;$("combineSumBranches").textContent="—";$("combineSumExpression").textContent="—";$("combineSumCoverage").textContent="—";$("combineSumMetadata").textContent="—";$("combineResult").innerHTML="";
}
function combineRequest(withPins){const value={sources:combineSelected(),operation:$("combineOperation").value,base:$("combineBase").value,name:$("combineName").value,label:$("combineLabel").value},plan=workflowState.combine.plan;if(withPins&&plan){value.snapshots=plan.snapshots;value.reviewedPublication=plan.publication}return value}
function combineFingerprint(){return JSON.stringify(combineRequest(false))}
function renderCombinePlan(v,fingerprint=combineFingerprint()){
 workflowState.reviewCombine(v,fingerprint);renderNoticeList("combineNotices",v.notices);$("combineSumInputs").textContent=`${fmt(v.input_count)} selected`;$("combineSumCompatibility").textContent=v.compatible?"Yes":"No · see explanation";$("combineSumBranches").textContent=`${fmt(v.branch_count)} retained`;$("combineSumExpression").textContent=v.expression_text;$("combineSumCoverage").textContent=v.coverage_complete?"Complete":"Provisional";$("combineSumMetadata").textContent=v.metadata_complete?"Exact details retained":"Some inputs have seeds only";
 $("combineTechnical").hidden=false;
 $("combineChecks").innerHTML=v.compatibility.checks.map(c=>`<div class="check ${esc(c.status)}"><b>${esc(c.label)} · ${esc(c.status)}</b>${esc(c.detail)}</div>`).join("");
 const inputs=v.compatibility.inputs.map(o=>{const role=o.role==="base"?"START WITH":o.role==="subtract"?"REMOVE MATCHES FROM":"INPUT";return `<div class="branch"><b>${role} · ${esc(o.name)}</b><span>${fmt(o.records)} seeds · ${esc(o.state)} pool · ${esc(o.coverage)} search coverage · ${esc(o.metadata)} · ${esc(o.space)} seed space · snapshot ID ${esc(o.snapshot_id.slice(0,8))}${o.composite?` · prior rule ${esc(o.composite_expression_text||o.composite_operation)}`:""}</span></div>`}).join("");
 const sources=(v.branches||[]).map(b=>`<div class="branch"><b>Recorded source filter · ${esc(b.label||b.pool_id||b.branch_id)}</b><span>${b.criteria.length?`Original requirements: ${b.criteria.map(esc).join(" · ")}`:"This older pool does not contain a readable description of its original requirements."}</span></div>`).join("");
 $("combineBranches").innerHTML=`<div class="notice ${v.compatible?"":"error"}"><strong>Seed-membership rule</strong>${esc(v.expression_text)}. This rule compares whether each seed is present in the selected pools; it does not rerun their original search filters.</div><div class="hint">Selected input pools</div>${inputs}${sources?'<div class="hint">Recorded filter history carried into the new pool</div>'+sources:""}`;
 const publication=v.publication;$("combinePublication").hidden=false;$("combineManifest").innerHTML=`<div class="manifestrow"><span><b>${esc(publication.name)}</b><small>${esc(publication.label)} · ${esc(v.operation.toUpperCase())} · ${publication.output_exists?"filename already exists":"filename available"}</small></span><span class="count">Seed count calculated while the file is created</span></div>`;$("combineReport").textContent=`A small audit report named ${publication.report_name} will record the selected input versions and the membership rule. The input pools remain unchanged, and an existing output file is never overwritten.`;
 $("combineCreateBtn").disabled=workflowState.combine.running||!publication.ready||workflowState.combine.reviewedFingerprint!==combineFingerprint();
}
async function checkCombine(){$("combineError").textContent="";$("combineResult").innerHTML="";const button=$("combinePlanBtn"),request=combineRequest(false),fingerprint=combineFingerprint();button.disabled=true;$("combineAnalysisCancelBtn").hidden=false;button.textContent="Checking selected pools…";try{const v=await api("/api/combine/plan",request);if(fingerprint!==combineFingerprint())throw Error("The selected pools or rule changed during the check. Check the current choices again.");renderCombinePlan(v,fingerprint)}catch(e){invalidateCombine("Compatibility check failed");$("combineError").textContent=e.message||String(e)}finally{$("combineAnalysisCancelBtn").hidden=true;button.textContent="Check compatibility and preview file";updateCombineBase()}}
async function createCombine(){
 const combine=workflowState.combine;if(!combine.plan||combine.reviewedFingerprint!==combineFingerprint()||!combine.plan.publication.ready)return;$("combineError").textContent="";workflowState.startCombine();$("combineCreateBtn").disabled=true;$("combineCreateBtn").textContent="Creating combined seed pool…";$("combineCancelBtn").hidden=false;
 try{const v=await api("/api/combine",combineRequest(true));renderNoticeList("combineNotices",v.notices);const empty=v.records?"":" The rule produced an empty pool, so it cannot be searched in-game.";$("combineResult").innerHTML=`<div class="notice"><strong>Created ${esc(v.name)}</strong>The new pool contains ${fmt(v.records)} unique seed(s) using the ${esc(v.operation)} rule.${esc(empty)} The selected input pools were kept unchanged. Audit report: ${esc(v.report_path)}</div>`;await loadPools(true);workflowState.invalidateCombine();$("combinePublication").hidden=true;$("combineTechnical").hidden=true;$("combineCreateBtn").disabled=true;$("combineSumCompatibility").textContent="Created"}catch(e){$("combineError").textContent=e.message||String(e);invalidateCombine("Check again after this failure")}finally{workflowState.finishCombine();$("combineCancelBtn").hidden=true;$("combineCreateBtn").textContent="Create combined seed pool";updateCombineBase()}
}
async function cancelCombine(){$("combineCancelBtn").disabled=true;$("combineCancelBtn").textContent="Cancelling…";try{await api("/api/cancel",{operation:"combine"})}catch(e){$("combineError").textContent=e.message||String(e)}finally{$("combineCancelBtn").disabled=false;$("combineCancelBtn").textContent="Cancel file creation"}}
async function cancelCombineAnalysis(){$("combineAnalysisCancelBtn").disabled=true;$("combineAnalysisCancelBtn").textContent="Cancelling…";try{await api("/api/cancel",{operation:"analysis"})}catch(e){$("combineError").textContent=e.message||String(e)}finally{$("combineAnalysisCancelBtn").disabled=false;$("combineAnalysisCancelBtn").textContent="Cancel compatibility check"}}
function renderFormatPlan(v){
 workflowState.reviewFormat(v);$("formatPlanCard").hidden=false;$("formatSumSource").textContent=v.source;$("formatSumCurrent").textContent=v.source_format;$("formatSumRecords").textContent=fmt(v.records);$("formatSumOutput").textContent=v.output_name||"No new file needed";
 $("formatMetrics").innerHTML=`<div class="metric"><span>File format</span><b>${esc(v.source_format)}</b></div><div class="metric"><span>Encoding</span><b>${esc(v.encoding||"(missing)")}</b></div><div class="metric"><span>Recorded seeds</span><b>${fmt(v.records)}</b></div><div class="metric"><span>File size</span><b>${fmtBytes(v.bytes)}</b></div><div class="metric"><span>Pool state</span><b>${v.complete?"Finished":"Paused / incomplete"}</b></div><div class="metric"><span>Search coverage</span><b>${v.coverage_complete?"Complete":"Provisional"}</b></div>`;
 if(v.status==="current"){
  $("formatPlanTitle").textContent="Already current";$("formatPlanCopy").textContent=`${v.source} already uses BSP4. No format update is needed.`;$("formatNotices").innerHTML='<div class="notice"><strong>Current adaptive format</strong>This pool already has BSP4 rank and metadata compression and can be used directly.</div>';$("formatSumStatus").textContent="Already current";$("formatUpdateBtn").disabled=true;
 }else if(v.eligible){
  $("formatPlanTitle").textContent="BSP4 update available";$("formatPlanCopy").textContent=`This lossless update recompresses ${fmt(v.records)} recorded seeds without rescanning.`;$("formatNotices").innerHTML=`<div class="notice"><strong>${esc(v.source_format)} → BSP4</strong>Source data, criteria, family/lineage history, and per-seed match details are preserved. The copy receives its own derivative identity and will be named <b>${esc(v.output_name)}</b>.</div><div class="notice"><strong>Original BSP3 will be kept</strong>The update creates a separate file and never renames, replaces, or deletes the selected source.</div>`;$("formatSumStatus").textContent="Update available";$("formatUpdateBtn").disabled=workflowState.format.running;
 }else{
  const detail=(v.blockers||[]).join(" ")||"This pool cannot be updated.";$("formatPlanTitle").textContent="Format update unavailable";$("formatPlanCopy").textContent=detail;$("formatNotices").innerHTML=`<div class="notice warning"><strong>Cannot create a BSP4 copy</strong>${esc(detail)}</div>`;$("formatSumStatus").textContent="Blocked";$("formatUpdateBtn").disabled=true;
 }
}
async function checkFormat(){
 if(workflowState.format.running)return;
 const source=$("formatSource").value;if(!source){formatState("error","No seed pool selected","Wait for the pool list to load, then choose a pool.");return}
 resetFormatPlan(`Checking ${source}`);const button=$("formatCheckBtn"),refresh=$("formatRefreshBtn"),picker=$("formatSource");button.disabled=true;refresh.disabled=true;picker.disabled=true;button.textContent="Checking format…";formatState("",`Checking ${source}`,"Reading only the bounded pool header.");
 try{const v=await api("/api/format/plan",{source});renderFormatPlan(v);if(v.status==="current")formatState("success","Already current",`${source} uses BSP4 (${v.encoding}). No update is needed.`);else if(v.eligible)formatState("success","BSP4 update available",`${fmt(v.records)} seeds can be recompressed into ${v.output_name} without rescanning.`);else formatState("error","Format update unavailable",(v.blockers||[]).join(" ")||"This pool cannot be updated.")}
 catch(e){resetFormatPlan("Format check failed");formatState("error","Format check failed",e.message||String(e));$("formatError").textContent=e.message||String(e)}
 finally{button.disabled=false;refresh.disabled=false;picker.disabled=false;button.textContent="Check pool format"}
}
async function updateFormat(){
 const plan=workflowState.format.plan;if(!plan||!plan.eligible)return;const requested=plan,button=$("formatUpdateBtn"),picker=$("formatSource"),refresh=$("formatRefreshBtn"),check=$("formatCheckBtn"),started=performance.now();let refreshFailed=false;workflowState.startFormat();$("formatError").textContent="";$("formatResult").innerHTML="";button.disabled=true;button.textContent="Updating to BSP4…";picker.disabled=true;refresh.disabled=true;check.disabled=true;$("formatCancelBtn").hidden=false;formatState("",`Creating ${requested.output_name}`,`Reading and recompressing ${fmt(requested.records)} seeds. Large pools may take several minutes. The original BSP3 is not being changed.`);
 $("formatElapsed").hidden=false;const timer=setInterval(()=>{const seconds=Math.floor((performance.now()-started)/1000);$("formatElapsed").textContent=`Still working — ${seconds}s elapsed. The original BSP3 is unchanged, and no partial output is visible.`},1000);
 try{
  const v=await api("/api/format/update",{source:requested.source,planToken:requested.plan_token});workflowState.resetFormat();
  const sizeChange=v.output_bytes<v.source_bytes?`${v.saved_percent.toFixed(1)}% smaller`:v.output_bytes===v.source_bytes?"the same size":"larger for this data";const repairedOrder=v.normalized_historical_order?" Historical block order was repaired while updating.":"";const repairedHeaders=v.reconstructed_bsp3_header_prefixes?` ${fmt(v.reconstructed_bsp3_header_prefixes)} damaged BSP3 block header prefix${v.reconstructed_bsp3_header_prefixes===1?" was":"es were"} safely reconstructed from the committed index, checksums, and whole-pool identities; the source remains unchanged.`:"";const warnings=[v.publication_warning,v.cleanup_warning].filter(Boolean).map(text=>`<div class="notice warning"><strong>Cleanup note</strong>${esc(text)}</div>`).join("");
  $("formatResult").innerHTML=`<div class="notice"><strong>BSP4 copy created</strong>${esc(v.output)} contains ${fmt(v.records)} seeds. ${fmtBytes(v.source_bytes)} → ${fmtBytes(v.output_bytes)} (${esc(sizeChange)}). The original ${esc(v.source)} was kept.${esc(repairedOrder)}${esc(repairedHeaders)}</div>${warnings}`;formatState("success","BSP4 copy created",`${v.output} is complete and ready in seed_pools.`);$("formatSumStatus").textContent="Created";$("formatSumOutput").textContent=v.output;
  try{await loadPools(true)}catch(refreshError){refreshFailed=true;const detail=refreshError.message||String(refreshError);$("formatError").textContent=`The BSP4 copy was created, but the pool list could not refresh: ${detail}`;formatState("success","BSP4 copy created",`${v.output} is ready. Use Refresh list to reload the pool selector.`)}
 }catch(e){
  const message=e.message||String(e),cancelled=e.code==="operation_cancelled",stale=e.code==="format_plan_stale",failedSafely=e.publicationState==="not_published";
  if(stale)resetFormatPlan("Check the selected pool again");
  const title=cancelled?"Update cancelled safely":e.code==="format_source_damaged"?"Source pool is damaged":failedSafely?"Update failed safely":"Update result not confirmed";
  const detail=cancelled?"No new pool was published; the original BSP3 was not changed.":failedSafely?`No BSP4 copy was published, and the original BSP3 was not changed. ${message}`:`The Organizer could not confirm whether a BSP4 copy was published. Refresh the pool list before trying again. The original pool was not changed. ${message}`;
  formatState(cancelled?"success":"error",title,detail);
  $("formatError").textContent=cancelled?"":message;
 }finally{clearInterval(timer);$("formatElapsed").hidden=true;$("formatElapsed").textContent="";workflowState.finishFormat();$("formatCancelBtn").hidden=true;button.textContent="Create BSP4 copy";button.disabled=!workflowState.format.plan||!workflowState.format.plan.eligible;picker.disabled=refreshFailed;refresh.disabled=false;check.disabled=refreshFailed||!workflowState.pools.some(p=>!p.error)}
}
async function cancelFormat(){$("formatCancelBtn").disabled=true;$("formatCancelBtn").textContent="Cancelling…";try{await api("/api/cancel",{operation:"upgrade"})}catch(e){$("formatError").textContent=e.message||String(e)}finally{$("formatCancelBtn").disabled=false;$("formatCancelBtn").textContent="Cancel update"}}
function showMode(mode){const split=mode==="split",combine=mode==="combine",formatMode=mode==="format";$("splitWorkspace").hidden=!split;$("combineWorkspace").hidden=!combine;$("formatWorkspace").hidden=!formatMode;$("splitModeBtn").classList.toggle("active",split);$("combineModeBtn").classList.toggle("active",combine);$("formatModeBtn").classList.toggle("active",formatMode);$("splitModeBtn").setAttribute("aria-selected",String(split));$("combineModeBtn").setAttribute("aria-selected",String(combine));$("formatModeBtn").setAttribute("aria-selected",String(formatMode))}

$("inspectBtn").onclick=inspect;$("refreshBtn").onclick=()=>loadPools(false);$("source").onchange=()=>resetSplitInspection("Selection changed — inspect this pool");
$("organizeBy").onchange=()=>{workflowState.clearSplitPlan(true);renderInspectLocations();$("reviewCard").hidden=true;$("plan").hidden=true;$("saveBtn").disabled=true;invalidateSplitReview("Recorded target changed — preview the new pools")};
$("allBtn").onclick=()=>{document.querySelectorAll(".cat").forEach(x=>x.checked=true);invalidateSplitSelection()};$("noneBtn").onclick=()=>{document.querySelectorAll(".cat").forEach(x=>x.checked=false);invalidateSplitSelection()};
$("planBtn").onclick=()=>prepare(false);$("applyDecisionsBtn").onclick=()=>prepare(true);$("saveBtn").onclick=savePlan;$("loadBtn").onclick=()=>$("loadFile").click();$("loadFile").onchange=e=>e.target.files[0]&&loadPlanFile(e.target.files[0]);$("clearRulesBtn").onclick=()=>clearDecisionSet("rules");$("clearChoicesBtn").onclick=()=>clearDecisionSet("choices");$("splitBtn").onclick=split;$("analysisCancelBtn").onclick=cancelAnalysis;$("splitCancelBtn").onclick=cancelSplit;
$("exclusiveMode").onchange=()=>{workflowState.clearSplitPlan(!$("exclusiveMode").checked);$("reviewCard").hidden=true;$("plan").hidden=true;$("saveBtn").disabled=true;renderInspectLocations();invalidateSplitReview($("exclusiveMode").checked?"Exclusive split selected — preview the new pools":"Matching copies selected — preview the new pools")};
$("otherPool").onchange=()=>{$("policy").value=$("otherPool").checked?"remainder":"omit";$("remainderField").hidden=!$("otherPool").checked;invalidateSplitReview()};$("remainder").oninput=()=>invalidateSplitReview();$("prefix").oninput=()=>{updateFilenamePreview();invalidateSplitReview()};
$("exportBtn").onclick=startRecordExport;$("exportCancelBtn").onclick=cancelRecordExport;
$("splitModeBtn").onclick=()=>showMode("split");$("combineModeBtn").onclick=()=>showMode("combine");$("formatModeBtn").onclick=()=>showMode("format");$("combinePlanBtn").onclick=checkCombine;$("combineCreateBtn").onclick=createCombine;$("combineAnalysisCancelBtn").onclick=cancelCombineAnalysis;$("combineCancelBtn").onclick=cancelCombine;$("combineRefreshBtn").onclick=()=>loadPools(false);
$("formatCheckBtn").onclick=checkFormat;$("formatUpdateBtn").onclick=updateFormat;$("formatCancelBtn").onclick=cancelFormat;$("formatRefreshBtn").onclick=()=>{if(workflowState.format.running)return;resetFormatPlan();loadPools(false)};$("formatSource").onchange=()=>{if(!workflowState.format.running)resetFormatPlan("Selection changed — check this pool")};
$("combineAllBtn").onclick=()=>{document.querySelectorAll(".combinePick:not(:disabled)").forEach(x=>x.checked=true);updateCombineBase();invalidateCombine("Selection changed")};$("combineNoneBtn").onclick=()=>{document.querySelectorAll(".combinePick").forEach(x=>x.checked=false);updateCombineBase();invalidateCombine("Selection changed")};
document.querySelectorAll(".combineOp").forEach(input=>input.onchange=()=>{$("combineOperation").value=input.value;updateCombineBase();invalidateCombine("Rule changed")});$("combineBase").onchange=()=>invalidateCombine("Base changed");$("combineName").oninput=()=>{updateCombineBase();invalidateCombine("Output changed")};$("combineLabel").oninput=()=>invalidateCombine("Output changed");
document.addEventListener("change",e=>{if(e.target.classList.contains("cat"))invalidateSplitSelection();else if(e.target.classList.contains("filterKind"))renderFilterOptions()});
if(!UNIFIED){$("builderTab").hidden=true;$("organizerTab").href="/";$("mergeLink").hidden=true;$("standaloneMerge").hidden=false}
resetSplitInspection();invalidateCombine();resetFormatPlan();loadPools(true).catch(()=>{});
</script></body></html>'''


def error_payload(exc):
    value = {"error": str(exc)}
    if isinstance(exc, OperationCancelled):
        value["error_code"] = "operation_cancelled"
    elif isinstance(exc, FormatPlanStale):
        value["error_code"] = "format_plan_stale"
    elif isinstance(exc, FormatSourceDamaged):
        value["error_code"] = "format_source_damaged"
        value["publication_state"] = "not_published"
    elif isinstance(exc, FormatUpdateFailedSafely):
        value["error_code"] = "format_update_failed"
        value["publication_state"] = "not_published"
    return value


class OrganizerHandler(BaseHTTPRequestHandler):
    pool_dir = POOL_DIR
    protocol_version = "HTTP/1.1"

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
        reader = verified_source_reader(name, self.pool_dir)
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
                self._json({
                    "pools": list_sources(self.pool_dir),
                    "native_summary": bool(_native_pool_binary()),
                    "native_summary_min_bytes": NATIVE_SUMMARY_MIN_BYTES,
                })
            elif parsed.path == "/api/export":
                serve_record_export(self, parsed, self.pool_dir)
            elif parsed.path == "/api/export/status":
                query = parse_qs(parsed.query)
                self._json(record_export_status(
                    query.get("request_id", [""])[0]))
            else:
                self._json({"error": "not found"}, 404)
        except (OSError, ValueError, organizer.PoolError) as exc:
            # Export may already have sent headers only after all validation;
            # every other branch can return a regular JSON error.
            if response_started:
                return
            self._json(error_payload(exc), 400)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            data = self._body()
            if parsed.path == "/api/inspect":
                self._json(run_inspect(
                    data.get("source", ""), self.pool_dir))
            elif parsed.path == "/api/plan":
                self._json(run_split_plan(data, self.pool_dir))
            elif parsed.path == "/api/combine/plan":
                self._json(run_combine_plan(data, self.pool_dir))
            elif parsed.path == "/api/format/plan":
                self._json(plan_format_upgrade(
                    data.get("source", ""), self.pool_dir))
            elif parsed.path == "/api/split":
                self._json(run_split(
                    data.get("source", ""), data, self.pool_dir))
            elif parsed.path == "/api/combine":
                self._json(run_combine(data, self.pool_dir))
            elif parsed.path == "/api/format/update":
                self._json(run_format_upgrade(data, self.pool_dir))
            elif parsed.path == "/api/cancel":
                self._json(cancel_operation(str(data.get("operation", ""))))
            else:
                self._json({"error": "not found"}, 404)
        except (OSError, ValueError, organizer.PoolError) as exc:
            self._json(error_payload(exc), 400)


def make_handler(pool_dir):
    class BoundOrganizerHandler(OrganizerHandler):
        pass
    BoundOrganizerHandler.pool_dir = os.path.abspath(pool_dir)
    return BoundOrganizerHandler


def main():
    os.makedirs(POOL_DIR, exist_ok=True)
    allow_active_operations()
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
        begin_operation_shutdown()
        wait_for_active_operations()
        print("\nBye.")
    finally:
        begin_operation_shutdown()
        wait_for_active_operations()
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
