"""Durable pool scoring jobs with compact SQLite checkpoints and native workers.

Sources are pinned snapshots. A resume verifies their identities and the exact
model/settings before reusing completed scores. Finished leaderboards are only
published after every source stream has validated its final metadata/digests.
"""

import copy
import csv
import bisect
import hashlib
import heapq
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager

try:
    import brainstorm_pool_organizer as organizer
    import pool_rule_workflow as workflow
    import pool_tag_export as tag_export
    import pool_tag_recording as recording
    import pool_tag_rules as tags
    import tagcalc_batch as batch
except ImportError:
    from tools import brainstorm_pool_organizer as organizer
    from tools import pool_rule_workflow as workflow
    from tools import pool_tag_export as tag_export
    from tools import pool_tag_recording as recording
    from tools import pool_tag_rules as tags
    from tools import tagcalc_batch as batch


VERSION = 1
ACTIVE = {"queued", "preparing", "running", "finalizing", "cancelling"}
MODEL_VERSION = "pool-native-tagcalc-1"


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _integer(value, name, first, last):
    if type(value) is not int or not first <= value <= last:
        raise organizer.PoolError("%s must be between %d and %d." % (name, first, last))
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise organizer.PoolError(name + " must be true or false.")
    return value


def _binary_model(binary):
    # Frozen applications ship bytecode, not adjacent Python source files.
    # The native binary is the calculator; explicit contracts and actual model
    # constants pin Python normalization without hashing temporary bundle paths.
    result = {"version": MODEL_VERSION, "batch_model": batch.MODEL_VERSION,
              "constants": {name: getattr(batch.model, name) for name in (
                  "C_NORMAL", "C_FINAL", "NORMAL_BP_SHARE", "NORMAL_BS_SHARE",
                  "P_BS_ETERNAL", "P_RARE_EDITION_WASTE", "P_RARE_NAT_NEG")}}
    digest = hashlib.sha256()
    with open(binary, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    result["native"] = digest.hexdigest()
    return result


def _location(value):
    try:
        return batch.position_text(batch.position(value, tag=True))
    except ValueError as error:
        raise organizer.PoolError(str(error)) from None


def _baseline(value):
    try:
        return batch.position_text(batch.position(value))
    except ValueError as error:
        raise organizer.PoolError(str(error)) from None


def _placements(raw):
    return tuple(("neg" if raw[index + 1] == 0 else "rare",
                  raw[index] // 2 + 1, raw[index] % 2)
                 for index in range(0, len(raw), 2))


def _tag_bytes(placements):
    return bytes(part for slot, kind in placements for part in (slot, int(kind == "rare")))


def _before(point):
    return "Before original Ante %d" % point if point <= 38 else "After original Ante 38"


@contextmanager
def _worker_pool(workers, stop):
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        yield executor
    except BaseException:
        stop.set()  # Interrupt native requests before waiting for worker exit.
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


class ScoreService:
    """Thread-safe JSON-facing service; no server/UI dependencies at import time.

    ``results(scope=...)`` uses ``combined`` or a pool_id from status. Downloads
    are exact filenames supplied in status.downloads, never arbitrary paths.
    Only one job runs per service, with an OS writer guard per checkpoint to
    prevent two applications from resuming the same job concurrently.
    """

    def __init__(self, pool_dir, native_helper=None, snapshot_path=None):
        self.pool_dir = os.path.realpath(os.path.abspath(os.fspath(pool_dir)))
        self.root = os.path.join(self.pool_dir, ".score-jobs")
        self.helper = native_helper
        self.snapshot_path = snapshot_path
        self._lock = threading.RLock()
        self._threads = {}
        self._events = {}
        self._current = {}
        self._closed = False

    def _source(self, name):
        if (not isinstance(name, str) or not name or name != os.path.basename(name)
                or "/" in name or "\\" in name or not name.lower().endswith(".bspool")):
            raise organizer.PoolError("Choose a .bspool from the Seed Pool folder.")
        path = os.path.join(self.pool_dir, name)
        if not os.path.isfile(path) or os.path.commonpath((self.pool_dir, os.path.realpath(path))) != self.pool_dir:
            raise organizer.PoolError("The selected pool is missing or points outside the Seed Pool folder.")
        return path

    def _folder(self, job_id, create=False):
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise organizer.PoolError("Invalid scoring job ID.")
        if create:
            os.makedirs(self.root, exist_ok=True)
        if os.path.islink(self.root) or os.path.realpath(self.root) != self.root:
            raise organizer.PoolError("The scoring jobs folder must not be a symbolic link.")
        folder = os.path.join(self.root, job_id)
        if os.path.islink(folder) or os.path.realpath(folder) != folder:
            raise organizer.PoolError("Invalid scoring job folder.")
        if create:
            os.mkdir(folder)
        elif not os.path.isdir(folder):
            raise organizer.PoolError("This scoring job no longer exists.")
        return folder

    def _artifact(self, job_id, filename):
        folder = self._folder(job_id)
        if not isinstance(filename, str) or filename != os.path.basename(filename) or "/" in filename or "\\" in filename:
            raise organizer.PoolError("Invalid scoring artifact.")
        path = os.path.join(folder, filename)
        if os.path.islink(path) or os.path.realpath(path) != path:
            raise organizer.PoolError("Scoring artifacts must not be symbolic links.")
        return path

    def _load(self, job_id):
        path = self._artifact(job_id, "summary.json")
        value = workflow._load_json(path, 32 * 1024 * 1024)
        if value.get("version") != VERSION or value.get("job_id") != job_id:
            raise organizer.PoolError("Unsupported or mismatched scoring checkpoint.")
        return value

    def _save(self, job):
        job["updated_at"] = time.time()
        job["completed_records"] = sum(pool["completed_records"] for pool in job["pools"])
        job["scored"] = sum(pool["scored"] for pool in job["pools"])
        job["no_valid_route"] = sum(pool["no_valid_route"] for pool in job["pools"])
        organizer.atomic_json(self._artifact(job["job_id"], "summary.json"), job)
        with self._lock:
            self._current[job["job_id"]] = self._public(job)

    @staticmethod
    def _public(job):
        # Original headers/dictionaries remain in the durable summary download;
        # polling status must not repeatedly transfer megabytes of provenance.
        result = {key: copy.deepcopy(value) for key, value in job.items() if key != "pools"}
        result["pools"] = [{key: copy.deepcopy(value) for key, value in pool.items() if key != "metadata"}
                           for pool in job["pools"]]
        return result

    def describe(self, source):
        reader = organizer.BSPoolReader(self._source(source), verify_payloads=False)
        second, _rule, reason = tag_export._second_tag(reader)
        baseline = batch.position_text(batch.baseline_for_second_tag(second)) if second else None
        return {"source": source, "label": reader.header.one("label", required=False) or source,
                "records": reader.records, "complete": reader.complete,
                "second_tag": second, "second_tag_reason": reason, "baseline_copy": baseline,
                "coverage": tags.describe_recorded_coverage(reader)}

    def _request(self, value):
        if not isinstance(value, dict) or set(value) - {
                "pools", "workers", "top", "record_missing", "export_all", "reference_score", "reference_seed"}:
            raise organizer.PoolError("Invalid scoring job settings.")
        selections = value.get("pools")
        if not isinstance(selections, list) or not 1 <= len(selections) <= 32:
            raise organizer.PoolError("Choose between 1 and 32 pools.")
        request = {"workers": _integer(value.get("workers", 1), "Workers", 1, 16),
                   "top": _integer(value.get("top", 1000), "Leaderboard size", 1, 10000),
                   "record_missing": _boolean(value.get("record_missing", False), "Record missing tags"),
                   "export_all": _boolean(value.get("export_all", False), "Export all rows"), "pools": []}
        reference = value.get("reference_score")
        if reference is not None and (isinstance(reference, bool) or not isinstance(reference, (int, float))
                                       or not math.isfinite(reference) or reference <= 0):
            raise organizer.PoolError("Reference score must be a finite positive number.")
        label = value.get("reference_seed")
        if label is not None and (not isinstance(label, str) or len(label) > 120 or any(ord(c) < 32 for c in label)):
            raise organizer.PoolError("Reference seed must be at most 120 printable characters.")
        request.update(reference_score=reference, reference_seed=label)
        pools, seen = [], set()
        for number, selection in enumerate(selections):
            if not isinstance(selection, dict) or set(selection) - {"source", "second_tag", "baseline_copy", "second_tag_type"}:
                raise organizer.PoolError("Invalid pool scoring settings.")
            source = selection.get("source")
            reader = organizer.BSPoolReader(self._source(source), verify_payloads=False)
            if source in seen:
                raise organizer.PoolError("Choose each pool only once per scoring job.")
            seen.add(source)
            if not reader.complete or reader.schema not in (3, 4):
                raise organizer.PoolError("Scoring needs finished BSP3/BSP4 event pools.")
            second, _rule, _reason = tag_export._second_tag(reader)
            manual = _location(selection["second_tag"]) if selection.get("second_tag") else None
            first_copy = _baseline(selection["baseline_copy"]) if selection.get("baseline_copy") else None
            if second and manual and manual != second["position"]:
                raise organizer.PoolError("The supplied second tag conflicts with this pool's saved rule.")
            chosen = second["position"] if second else manual
            expected = batch.position_text(batch.baseline_for_second_tag(chosen)) if chosen else None
            if first_copy and expected and first_copy != expected:
                raise organizer.PoolError("The first-copy position conflicts with the second tag.")
            first_copy = first_copy or expected
            if not first_copy:
                raise organizer.PoolError("%s has no saved second tag. Choose its second tag or first-copy position." % source)
            kind = selection.get("second_tag_type") or None
            if kind not in (None, "negative", "rare"):
                raise organizer.PoolError("Second tag type must be Negative or Rare.")
            if second and kind and kind != second["tag"]:
                raise organizer.PoolError("Second tag type conflicts with this pool's saved rule.")
            normalized = {"source": source, "second_tag": chosen, "baseline_copy": first_copy,
                          "second_tag_type": second["tag"] if second else kind}
            request["pools"].append(normalized)
            pools.append({**normalized, "pool_id": "p%03d" % number,
                          "label": reader.header.one("label", required=False) or source,
                          "records": reader.records, "source_pin": workflow._source_pin(reader),
                          "completed_records": 0, "scored": 0, "no_valid_route": 0,
                          "status": "queued", "error": None, "metadata": None,
                          "evidence_path": None, "evidence_pin": None})
        return request, pools

    def _require_helper(self):
        binary = getattr(self.helper, "binary", None)
        if not binary or not os.path.isfile(binary):
            raise organizer.PoolError("Native scoring needs the latest full Brainstorm package and pool helper.")
        return os.path.abspath(binary)

    def start(self, request):
        with self._lock:
            self._idle()
            settings, pools = self._request(request)
            model = _binary_model(self._require_helper())
            if settings["record_missing"] and not self.snapshot_path:
                raise organizer.PoolError("A matching game profile snapshot is needed to record missing tags.")
            job_id = uuid.uuid4().hex
            self._folder(job_id, create=True)
            job = {"version": VERSION, "job_id": job_id, "status": "queued", "phase": "queued",
                   "created_at": time.time(), "updated_at": time.time(), "request": settings,
                   "model": model, "settings_id": workflow._fingerprint({"request": settings, "model": model}),
                   "pools": pools, "total_records": sum(pool["records"] for pool in pools),
                   "completed_records": 0, "scored": 0, "no_valid_route": 0,
                   "error": None, "downloads": []}
            self._save(job)
            self._launch(job)
            return self.status(job_id)

    def _idle(self):
        if self._closed:
            raise organizer.PoolError("Scoring is shutting down.")
        if any(thread.is_alive() for thread in self._threads.values()):
            raise organizer.PoolError("A scoring job is already running. Wait or pause it first.")
        self._threads.clear()
        self._events.clear()
        self._current.clear()

    def _launch(self, job):
        guard = organizer.pool_writer_guard(self._artifact(job["job_id"], "scores.sqlite"))
        try:
            guard.__enter__()
        except ValueError:
            raise organizer.PoolError("This scoring job is already running in another application.") from None
        try:
            self._save(job)
            event = threading.Event()
            self._events[job["job_id"]] = event
            thread = threading.Thread(target=self._run, args=(job, event, guard), daemon=True,
                                      name="pool-score-" + job["job_id"][:8])
            self._threads[job["job_id"]] = thread
            thread.start()
        except BaseException:
            guard.__exit__(None, None, None)
            raise

    def status(self, job_id):
        self._folder(job_id)
        with self._lock:
            job = copy.deepcopy(self._current.get(job_id))
        live = self._threads.get(job_id)
        if job is None or (not (live and live.is_alive()) and not job.get("persistence_error")):
            job = self._public(self._load(job_id))
        if job["status"] in ACTIVE and not (live and live.is_alive()):
            try:
                with organizer.pool_writer_guard(self._artifact(job_id, "scores.sqlite")):
                    job["status"] = "interrupted"
                    job["phase"] = "interrupted"
            except ValueError:
                job["running_elsewhere"] = True
        elif job["status"] in ACTIVE and self._events.get(job_id) and self._events[job_id].is_set():
            job["status"] = "cancelling"
            job["phase"] = "cancelling"
        job["can_resume"] = job["status"] in ("interrupted", "failed")
        return job

    def list_jobs(self, limit=100):
        """Return at most the latest 100 job summaries, without raw metadata."""
        limit = _integer(limit, "Job history limit", 1, 100)
        if not os.path.isdir(self.root):
            return []
        if os.path.islink(self.root):
            raise organizer.PoolError("The scoring jobs folder must not be a symbolic link.")
        jobs = []
        with os.scandir(self.root) as entries:
            candidates = heapq.nlargest(limit, ((entry.stat(follow_symlinks=False).st_mtime, entry.name)
                                                for entry in entries if re.fullmatch(r"[0-9a-f]{32}", entry.name)
                                                and entry.is_dir(follow_symlinks=False)))
        for _modified, name in candidates:
            try:
                jobs.append(self.status(name))
            except (organizer.PoolError, OSError):
                continue
        return sorted(jobs, key=lambda job: job["created_at"], reverse=True)

    def cancel(self, job_id):
        with self._lock:
            job = self.status(job_id)
            event = self._events.get(job_id)
            if job.get("running_elsewhere"):
                raise organizer.PoolError("This scoring job is running in another application. Pause it there first.")
            if event and job["status"] in ACTIVE and self._threads[job_id].is_alive():
                event.set()
                current = self._current[job_id]
                current["status"] = "cancelling"
                current["phase"] = "cancelling"
                return self.status(job_id)
            return job

    def resume(self, job_id):
        with self._lock:
            self._idle()
            job = self._load(job_id)
            if job["status"] == "completed":
                raise organizer.PoolError("This scoring job is already complete.")
            if job["model"] != _binary_model(self._require_helper()):
                raise organizer.PoolError("The scoring model changed. Start a new job to keep scores comparable.")
            self._validate_settings(job)
            self._validate_sources(job)
            # Acquire the cross-application guard before changing persisted status.
            job.update(status="queued", phase="queued", error=None, downloads=[])
            self._launch(job)
            return self.status(job_id)

    @staticmethod
    def _validate_settings(job):
        # Pool rows retain effective settings for progress and result rendering.
        # They must agree with the pinned request before completed scores can
        # be reused; checking only the request's fingerprint misses edits to
        # these duplicated baselines, source names, or pool identifiers.
        try:
            request = job["request"]
            valid = job["settings_id"] == workflow._fingerprint({"request": request, "model": job["model"]})
            valid = valid and len(job["pools"]) == len(request["pools"])
            for number, (pool, selection) in enumerate(zip(job["pools"], request["pools"])):
                valid = valid and pool["pool_id"] == "p%03d" % number
                valid = valid and all(pool[key] == selection[key] for key in
                                      ("source", "second_tag", "baseline_copy", "second_tag_type"))
                valid = valid and pool["records"] == pool["source_pin"]["records"]
            valid = valid and job["total_records"] == sum(pool["records"] for pool in job["pools"])
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise organizer.PoolError("The checkpoint settings changed. Start a new job.")

    def _validate_sources(self, job):
        for pool in job["pools"]:
            reader = organizer.BSPoolReader(self._source(pool["source"]), verify_payloads=False)
            if workflow._source_pin(reader) != pool["source_pin"]:
                raise organizer.PoolError("%s changed since this job started. Start a new job." % pool["source"])
            if pool["evidence_path"]:
                path = self._evidence_path(job, pool)
                evidence = organizer.BSPoolReader(path, verify_payloads=False)
                if workflow._source_pin(evidence) != pool["evidence_pin"]:
                    raise organizer.PoolError("Recorded tag data changed. Start a new job.")

    def _evidence_path(self, job, pool):
        folder = self._folder(job["job_id"])
        path = os.path.abspath(os.path.join(folder, pool["evidence_path"]))
        if os.path.commonpath((folder, os.path.realpath(path))) != folder or not os.path.isfile(path):
            raise organizer.PoolError("Recorded tag data is missing or points outside this job.")
        return path

    def _database(self, job):
        connection = sqlite3.connect(self._artifact(job["job_id"], "scores.sqlite"), timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript("""
          CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS scores (pool_id TEXT NOT NULL, rank INTEGER NOT NULL,
            seed TEXT NOT NULL, score REAL, tags BLOB NOT NULL, fillers TEXT NOT NULL,
            first_tag_type TEXT, PRIMARY KEY(pool_id,rank));
          CREATE INDEX IF NOT EXISTS best_scores ON scores(score DESC,seed,pool_id);
          CREATE TABLE IF NOT EXISTS pool_progress (pool_id TEXT PRIMARY KEY,
            records INTEGER NOT NULL,scored INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS leaders (scope TEXT NOT NULL, position INTEGER NOT NULL,
            value TEXT NOT NULL, PRIMARY KEY(scope,position));
        """)
        prior = connection.execute("SELECT value FROM metadata WHERE key='settings_id'").fetchone()
        if prior and prior[0] != job["settings_id"]:
            connection.close()
            raise organizer.PoolError("Score checkpoint belongs to different settings.")
        connection.execute("INSERT OR IGNORE INTO metadata VALUES ('settings_id',?)", (job["settings_id"],))
        for pool in job["pools"]:
            connection.execute("INSERT OR IGNORE INTO pool_progress VALUES (?,0,0)", (pool["pool_id"],))
        connection.commit()
        return connection

    def _counts(self, connection, pool):
        total, scored = connection.execute("SELECT records,scored FROM pool_progress WHERE pool_id=?", (pool["pool_id"],)).fetchone()
        pool.update(completed_records=total, scored=scored, no_valid_route=total - scored)

    def _prepare(self, job, event):
        job.update(status="preparing", phase="verifying_sources")
        self._save(job)
        self._validate_sources(job)
        readers = []
        for pool in job["pools"]:
            organizer._check_cancel(event.is_set)
            original = organizer.BSPoolReader(self._source(pool["source"]), verify_payloads=False)
            if job["request"]["record_missing"] and not pool["evidence_path"]:
                folder = os.path.join(self._folder(job["job_id"]), pool["pool_id"])
                os.makedirs(folder, exist_ok=True)
                def phase(value):
                    job["phase"] = value
                    pool["status"] = value
                    self._save(job)
                result = recording.record_tags(original, tag_export.EXPORT_RECIPE, folder,
                                                self.snapshot_path, self.helper, prefix=uuid.uuid4().hex,
                                                cancel_check=event.is_set, phase=phase)
                pool["evidence_path"] = os.path.relpath(result["path"], self._folder(job["job_id"]))
                evidence = organizer.BSPoolReader(result["path"], verify_payloads=False)
                pool["evidence_pin"] = workflow._source_pin(evidence)
            else:
                evidence = (organizer.BSPoolReader(self._evidence_path(job, pool), verify_payloads=False)
                            if pool["evidence_path"] else original)
            stream = tag_export.TagExport(evidence, original)
            pool["metadata"] = stream.metadata
            pool["status"] = "ready"
            self._save(job)
            readers.append(stream)
        return readers

    def _run(self, job, event, guard):
        connection = None
        scorers = []
        stop = threading.Event()
        cancelled = lambda: event.is_set() or stop.is_set()
        try:
            readers = self._prepare(job, event)
            if _binary_model(self._require_helper()) != job["model"]:
                raise organizer.PoolError("The scoring model changed while preparing this job. Start a new job.")
            connection = self._database(job)
            connection.set_progress_handler(lambda: int(event.is_set()), 10000)
            try:
                from pool_score_native import NativeScorer
            except ImportError:
                from tools.pool_score_native import NativeScorer
            local = threading.local()
            scorer_lock = threading.Lock()
            def score(baseline, placements):
                organizer._check_cancel(cancelled)
                if not hasattr(local, "scorer"):
                    local.scorer = NativeScorer(self._require_helper(), cancel_check=cancelled)
                    local.scorer.__enter__()
                    local.cache = OrderedDict()
                    with scorer_lock:
                        scorers.append(local.scorer)
                future = tuple(item for item in placements if (item[1], item[2]) > baseline)
                key = baseline, future
                result = local.cache.get(key)
                if result is None:
                    value, _route, fillers = local.scorer.score(baseline, future, details=False)
                    result = value, (), tuple(fillers)
                    local.cache[key] = result
                    if len(local.cache) > 1024:
                        local.cache.popitem(last=False)
                else:
                    local.cache.move_to_end(key)
                organizer._check_cancel(cancelled)
                return result
            with ExitStack() as sources, _worker_pool(job["request"]["workers"], stop) as executor:
                for stream in readers:
                    sources.enter_context(stream.original_reader._open_source_snapshot(event.is_set))
                    if stream.reader is not stream.original_reader:
                        sources.enter_context(stream.reader._open_source_snapshot(event.is_set))
                job.update(status="running", phase="scoring")
                self._save(job)
                for pool, stream in zip(job["pools"], readers):
                    pool["status"] = "running"
                    self._counts(connection, pool)
                    baseline = batch.position(pool["baseline_copy"])
                    completed = connection.execute("SELECT rank FROM scores WHERE pool_id=? ORDER BY rank", (pool["pool_id"],))
                    previous = next(completed, None)
                    pending = deque()
                    written = 0
                    last_save = time.monotonic()
                    def finish(item):
                        nonlocal written, last_save
                        rank, seed, encoded, first_type, future = item
                        value, route, fillers = future.result()
                        if not isinstance(value, (int, float)) or not math.isfinite(value):
                            raise organizer.PoolError("Native scoring returned a non-finite score.")
                        stored = float(value) if value > 0 else None
                        connection.execute("INSERT INTO scores VALUES (?,?,?,?,?,?,?)",
                                           (pool["pool_id"], rank, seed, stored, encoded, _json(fillers), first_type))
                        connection.execute("UPDATE pool_progress SET records=records+1,scored=scored+? WHERE pool_id=?",
                                           (int(stored is not None), pool["pool_id"]))
                        written += 1
                        if written % 128 == 0 or time.monotonic() - last_save >= 1:
                            connection.commit()
                            self._counts(connection, pool)
                            self._save(job)
                            last_save = time.monotonic()
                    traversed = 0
                    for record in stream.reader.iter_records(cancel_check=event.is_set):
                        traversed += 1
                        if previous is not None and previous[0] < record.rank:
                            raise organizer.PoolError("Checkpoint contains a seed absent from its pinned pool.")
                        if previous is not None and previous[0] == record.rank:
                            previous = next(completed, None)
                            continue
                        evidence = stream._evidence._record_evidence(record)
                        if stream._second_classifier:
                            outcome = stream._second_classifier.classify(record)
                            if not outcome.destination or outcome.destination.key != stream._second_tag["key"]:
                                raise organizer.PoolError("A seed disagrees with the pool's saved second tag.")
                        first_type = None
                        if stream._second_classifier:
                            first, last = tags._range_pair(stream._second_classifier.recipe["range"])
                            eligible = [(slot, kind) for slot, kind in evidence if first <= slot <= last]
                            if eligible:
                                kinds = {kind for slot, kind in eligible if slot // 2 == eligible[0][0] // 2}
                                first_type = "both" if len(kinds) > 1 else next(iter(kinds))
                        encoded = _tag_bytes([(slot, kind) for slot, kind in evidence if slot <= 75])
                        pending.append((record.rank, stream.reader.seed(record.rank), encoded, first_type,
                                        executor.submit(score, baseline, _placements(encoded))))
                        if len(pending) >= job["request"]["workers"] * 2:
                            finish(pending.popleft())
                    while pending:
                        finish(pending.popleft())
                    if traversed != pool["records"] or previous is not None:
                        raise organizer.PoolError("Scoring did not traverse the complete pinned pool.")
                    connection.commit()
                    self._counts(connection, pool)
                    if pool["completed_records"] != pool["records"]:
                        raise organizer.PoolError("The checkpoint does not contain every source seed.")
                    pool["status"] = "scored"
                    self._save(job)
            # All source contexts and final digest checks have now succeeded.
            organizer._check_cancel(event.is_set)
            for scorer in scorers:
                scorer.close()
            scorers.clear()
            job.update(status="finalizing", phase="leaderboards")
            self._save(job)
            with NativeScorer(self._require_helper(), cancel_check=event.is_set) as leader_scorer:
                details_cache = OrderedDict()
                self._leaders(job, readers, connection, leader_scorer, event, details_cache)
                if job["request"]["export_all"]:
                    self._export_all(job, readers, connection, leader_scorer, event, details_cache)
            self._validate_sources(job)
            if _binary_model(self._require_helper()) != job["model"]:
                raise organizer.PoolError("The scoring model changed during this job. Start a new job.")
            organizer._check_cancel(event.is_set)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            for pool in job["pools"]:
                pool["status"] = "completed"
            job.update(status="completed", phase="completed", error=None)
            job["downloads"] = self._downloads(job)
            self._save(job)
        except BaseException as error:
            if connection:
                connection.set_progress_handler(None, 0)
                try:
                    connection.commit()
                    for pool in job["pools"]:
                        self._counts(connection, pool)
                except sqlite3.Error:
                    connection.rollback()
            state = "interrupted" if event.is_set() else "failed"
            job.update(status=state, phase=state, error=str(error) or type(error).__name__, downloads=[])
            for pool in job["pools"]:
                if pool["status"] != "scored":
                    pool.update(status=state, error=job["error"])
            try:
                self._save(job)
            except (OSError, sqlite3.Error):
                # A full/unavailable disk must still expose failure in the live
                # UI; the last durable manifest remains safely resumable.
                job["persistence_error"] = True
                with self._lock:
                    self._current[job["job_id"]] = self._public(job)
        finally:
            for scorer in scorers:
                try:
                    scorer.__exit__(None, None, None)
                except Exception:
                    pass
            if connection:
                connection.close()
            guard.__exit__(None, None, None)

    def _best(self, connection, scope, top):
        if scope == "combined":
            return connection.execute("""WITH unique_seeds AS (
              SELECT *,row_number() OVER(PARTITION BY seed ORDER BY score DESC,pool_id,rank) AS picked
              FROM scores WHERE score IS NOT NULL)
              SELECT pool_id,rank,seed,score,tags,fillers,first_tag_type FROM unique_seeds
              WHERE picked=1 ORDER BY score DESC,seed,pool_id LIMIT ?""", (top,))
        return connection.execute("""SELECT pool_id,rank,seed,score,tags,fillers,first_tag_type
          FROM scores WHERE pool_id=? AND score IS NOT NULL ORDER BY score DESC,seed,rank LIMIT ?""", (scope, top))

    def _result(self, job, pool, row, position, route=None):
        _pool, rank, seed, score, encoded, fillers, first_type = row
        points = json.loads(fillers)
        locations = _placements(encoded)
        result = {"position": position, "rank": rank, "seed": seed, "score": score,
                  "status": "scored" if score is not None else "no_valid_route",
                  "pool_id": pool["pool_id"], "pool_label": pool["label"],
                  "second_tag": pool["second_tag"], "second_tag_type": pool["second_tag_type"],
                  "first_tag_type": first_type, "baseline_copy": pool["baseline_copy"],
                  "negative_locations": ["A%d%s" % (ante, "B" if blind else "S") for kind, ante, blind in locations if kind == "neg"],
                  "rare_locations": ["A%d%s" % (ante, "B" if blind else "S") for kind, ante, blind in locations if kind == "rare"],
                  "route": route, "fillers": points,
                  "beats_reference": score > job["request"]["reference_score"] if score is not None and job["request"]["reference_score"] is not None else None}
        for index, name in enumerate(("hieroglyph", "petroglyph")):
            point = points[index] if len(points) > index else None
            result[name + "_before_ante"] = point
            result[name + "_label"] = _before(point) if point is not None else ""
        return result

    @staticmethod
    def _details(scorer, pool, row, cache):
        baseline = batch.position(pool["baseline_copy"])
        placements = tuple(item for item in _placements(row[4]) if (item[1], item[2]) > baseline)
        key = baseline, placements
        result = cache.get(key)
        if result is None:
            result = scorer.score(baseline, placements)
            cache[key] = result
            # Detailed routes are substantially larger than compact scores.
            # Reuse repeated winners/export rows without retaining every route.
            if len(cache) > 128:
                cache.popitem(last=False)
        else:
            cache.move_to_end(key)
        score, route, fillers = result
        if (score if score > 0 else None) != row[3] or list(fillers) != json.loads(row[5]):
            raise organizer.PoolError("A checkpoint score differs from the current native result.")
        return route

    def _leaders(self, job, readers, connection, scorer, event, details_cache):
        connection.execute("DELETE FROM leaders")
        pools = {pool["pool_id"]: pool for pool in job["pools"]}
        streams = {pool["pool_id"]: stream for pool, stream in zip(job["pools"], readers)}
        lookups = {}
        block_cache = OrderedDict()
        def original_record(pool_id, rank):
            stream = streams[pool_id]
            reader = stream.reader
            if pool_id not in lookups:
                blocks = sorted(reader.blocks, key=lambda block: block.first_rank)
                maximum = -1
                prefix = []
                for block in blocks:
                    maximum = max(maximum, block.last_rank)
                    prefix.append(maximum)
                lookups[pool_id] = blocks, [block.first_rank for block in blocks], prefix
            blocks, starts, prefix = lookups[pool_id]
            found = None
            with reader._open_source_snapshot(event.is_set) as handle:
                for index in range(bisect.bisect_left(prefix, rank), bisect.bisect_right(starts, rank)):
                    block = blocks[index]
                    if block.last_rank < rank:
                        continue
                    key = pool_id, block.offset
                    records = block_cache.get(key)
                    if records is None:
                        records = reader._read_block_records(handle, block)
                        block_cache[key] = records
                        while len(block_cache) > 2:
                            block_cache.popitem(last=False)
                    else:
                        block_cache.move_to_end(key)
                    for record in records:
                        if record.rank == rank:
                            if found is not None:
                                raise organizer.PoolError("A winning rank occurs twice in its tag-data pool.")
                            found = record
            if found is None:
                raise organizer.PoolError("A winning seed is missing from its pinned tag-data pool.")
            occurrences = []
            for item in found.occurrences:
                occurrences.append({**item.as_dict(), "raw_hex": item.raw.hex()})
            branch_ids = sorted({item.provenance_id for item in found.occurrences if item.provenance_id is not None})
            operand_ids = sorted({item.operand_id for item in found.occurrences if item.operand_id is not None})
            return {"seed": reader.seed(rank), "rank": rank, "occurrences": occurrences,
                    "source_labels": [reader.composite_operands[key].label for key in operand_ids],
                    "original_source_labels": [reader.composite_branches[key].label for key in branch_ids],
                    "source_snapshot_id": stream.original_reader.snapshot_token,
                    "tag_data_snapshot_id": reader.snapshot_token,
                    "tag_coverage": {"start": "A1S", "end": "A38B", "complete": True}}
        for scope in ["combined", *pools]:
            ndjson = self._artifact(job["job_id"], scope + "-leaderboard.ndjson")
            csv_path = self._artifact(job["job_id"], scope + "-leaderboard.csv")
            with open(ndjson, "w", encoding="utf-8") as output, open(csv_path, "w", encoding="utf-8-sig", newline="") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=("position", "seed", "score", "pool", "first_copy", "second_tag", "hieroglyph", "petroglyph"))
                writer.writeheader()
                for position, row in enumerate(self._best(connection, scope, job["request"]["top"]), 1):
                    organizer._check_cancel(event.is_set)
                    pool = pools[row[0]]
                    route = self._details(scorer, pool, row, details_cache)
                    value = self._result(job, pool, row, position, route)
                    original = original_record(row[0], row[1])
                    value.update(input=original, source_labels=original["source_labels"],
                                 original_source_labels=original["original_source_labels"])
                    connection.execute("INSERT INTO leaders VALUES (?,?,?)", (scope, position, _json(value)))
                    output.write(_json(value) + "\n")
                    values = {"position": position, "seed": value["seed"], "score": value["score"],
                              "pool": value["pool_label"], "first_copy": value["baseline_copy"],
                              "second_tag": value["second_tag"] or "", "hieroglyph": value["hieroglyph_label"],
                              "petroglyph": value["petroglyph_label"]}
                    writer.writerow({key: batch._csv_safe(value) for key, value in values.items()})
            connection.commit()

    def _export_all(self, job, readers, connection, scorer, event, details_cache):
        job["phase"] = "exporting_tags"
        self._save(job)
        for pool, stream in zip(job["pools"], readers):
            organizer._check_cancel(event.is_set)
            path = self._artifact(job["job_id"], pool["pool_id"] + "-tags.ndjson")
            if os.path.exists(path):
                os.unlink(path)  # Private, incomplete artifact from this same job.
            tag_export.write_ndjson(stream, path, cancel_check=event.is_set)
        path = self._artifact(job["job_id"], "all-scores.ndjson")
        job["phase"] = "exporting_scores"
        self._save(job)
        pools = {pool["pool_id"]: pool for pool in job["pools"]}
        with open(path, "w", encoding="utf-8") as output:
            for row in connection.execute("SELECT pool_id,rank,seed,score,tags,fillers,first_tag_type FROM scores ORDER BY pool_id,rank"):
                organizer._check_cancel(event.is_set)
                pool = pools[row[0]]
                route = self._details(scorer, pool, row, details_cache)
                output.write(_json(self._result(job, pool, row, None, route)) + "\n")

    def _downloads(self, job):
        result = [{"filename": "summary.json", "kind": "summary", "label": "Job summary"},
                  {"filename": "scores.sqlite", "kind": "checkpoint", "label": "Score checkpoint"}]
        for scope in ["combined"] + [pool["pool_id"] for pool in job["pools"]]:
            for extension in ("csv", "ndjson"):
                result.append({"filename": scope + "-leaderboard." + extension,
                               "kind": "leaderboard_" + ("json" if extension == "ndjson" else "csv"),
                               "label": ("Combined" if scope == "combined" else scope) + " leaderboard " + extension.upper(),
                               "pool_id": None if scope == "combined" else scope})
        if job["request"]["export_all"]:
            result.append({"filename": "all-scores.ndjson", "kind": "scores", "label": "All scores"})
            result.extend({"filename": pool["pool_id"] + "-tags.ndjson", "kind": "tags", "label": pool["label"] + " tag data", "pool_id": pool["pool_id"]} for pool in job["pools"])
        return result

    def results(self, job_id, scope="combined", offset=0, limit=100, pool_id=None):
        job = self.status(job_id)
        scope = pool_id or scope
        if scope not in ["combined"] + [pool["pool_id"] for pool in job["pools"]]:
            raise organizer.PoolError("Choose a leaderboard from this job.")
        offset = _integer(offset, "Page offset", 0, 10000)
        limit = _integer(limit, "Page size", 1, 100)
        result = {"job_id": job_id, "pool_id": None if scope == "combined" else scope,
                  "scope": scope, "status": job["status"], "total": 0, "offset": offset, "limit": limit, "rows": []}
        if job["status"] != "completed":
            return result
        with sqlite3.connect(self._artifact(job_id, "scores.sqlite")) as connection:
            result["total"] = connection.execute("SELECT count(*) FROM leaders WHERE scope=?", (scope,)).fetchone()[0]
            result["rows"] = [json.loads(row[0]) for row in connection.execute(
                "SELECT value FROM leaders WHERE scope=? ORDER BY position LIMIT ? OFFSET ?", (scope, limit, offset))]
        return result

    def download(self, job_id, filename):
        job = self.status(job_id)
        if (not isinstance(filename, str) or job["status"] != "completed"
                or filename not in {row["filename"] for row in job["downloads"]}):
            raise organizer.PoolError("This completed scoring job does not provide that download.")
        path = self._artifact(job_id, filename)
        if not os.path.isfile(path):
            raise organizer.PoolError("The scoring download is missing.")
        return path

    def shutdown(self, timeout=5):
        with self._lock:
            self._closed = True
            events, threads = list(self._events.values()), list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + timeout
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in threads if thread is not threading.current_thread()):
            raise organizer.PoolError("Scoring is still stopping; wait before closing the application.")
