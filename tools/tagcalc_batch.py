"""Streaming batch interface for the tester's tag growth model (standard library)."""

import argparse
from collections import deque
from contextlib import contextmanager, ExitStack
import csv
from functools import lru_cache
import heapq
import itertools
import json
import math
import multiprocessing
import os
from pathlib import Path
import sys
import time

try:
    import tagcalc as model
except ImportError:
    from tools import tagcalc as model


MODEL_VERSION = "tagcalc-1-exact"
MAX_LINE_BYTES = 16 * 1024 * 1024


def position(value, *, tag=False):
    if isinstance(value, dict):
        value = value.get("position")
    if not isinstance(value, str):
        raise ValueError("Enter a location such as A4S or A4B.")
    ante, blind = model.parse_shop_exit(value)
    if not 1 <= ante <= 38 or (tag and blind == 2):
        raise ValueError("Use a location in Antes 1–38%s." %
                         (" at Small or Big" if tag else ""))
    return ante, blind


def position_text(pos):
    return "A%d%s" % (pos[0], ("S", "B", "Boss")[pos[1]])


def baseline_for_second_tag(value):
    ante, blind = position(value, tag=True)
    return ante, blind + 1


def starting_position(row, second_tag=None, baseline_copy=None):
    """Row settings take priority; command-line settings are pool defaults."""
    row_second = row.get("second_tag")
    row_copy = row.get("baseline_copy")
    if row_second:
        derived = baseline_for_second_tag(row_second)
        if row_copy and position(row_copy) != derived:
            raise ValueError("baseline_copy disagrees with second_tag: %s requires %s." %
                             (row_second, position_text(derived)))
        return derived, position_text(position(row_second, tag=True))
    if row_copy:
        return position(row_copy), None
    if second_tag:
        derived = baseline_for_second_tag(second_tag)
        if baseline_copy and position(baseline_copy) != derived:
            raise ValueError("--baseline-copy disagrees with --second-tag.")
        return derived, position_text(position(second_tag, tag=True))
    if baseline_copy:
        return position(baseline_copy), None
    raise ValueError("Missing second_tag. Set the row's second_tag or use --second-tag A4B "
                     "for this pool (A4S → A4B first copy; A4B → A4Boss).")


def _locations(value, name):
    if isinstance(value, str):
        if value.strip().startswith("["):
            value = json.loads(value)
        else:
            value = value.replace(";", ",").split(",") if value.strip() else []
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ValueError(name + " must be an array or comma-separated list of locations.")
    return value


def _declared_coverage(row):
    coverage = row.get("tag_coverage")
    if coverage is None:
        return
    if (not isinstance(coverage, dict) or coverage.get("complete") is not True
            or coverage.get("start") != "A1S" or coverage.get("end") not in ("A38B", "A39B")):
        raise ValueError("Tag data must cover both Negative and Rare from A1S through A38B.")


def tags_for_row(row):
    """Manual tag lists assert completeness; occurrence-only old exports do not."""
    _declared_coverage(row)
    if "negative_locations" in row or "rare_locations" in row:
        if not {"negative_locations", "rare_locations"} <= set(row):
            raise ValueError("Supply both negative_locations and rare_locations (empty arrays are allowed).")
        tokens = []
        for key, prefix in (("negative_locations", "n"), ("rare_locations", "r")):
            for value in _locations(row[key], key):
                tokens.append(prefix + value)
        tags = [model.parse_tag_token(token) for token in tokens]
    elif "tag_string" in row or "tags" in row:
        value = row.get("tag_string", row.get("tags"))
        if isinstance(value, list) and all(isinstance(x, str) for x in value):
            value = ",".join(value)
        if not isinstance(value, str):
            raise ValueError("tags must contain a string such as r7b,n10b or an array of those tokens.")
        tags = model.parse_tags(value)
    elif "occurrences" in row:
        raise ValueError("This is an old pool-record export; its occurrences may omit tags. "
                         "Use the .bspool input directly, adding --snapshot native_search.cfg "
                         "if full A1S–A38B tag coverage has not been recorded.")
    else:
        raise ValueError("Missing tags. A seed code alone cannot supply tag locations; "
                         "use a recorded .bspool or a CSV with seed,second_tag,tags.")
    selected = {}
    ignored = 0
    for tag in tags:
        ante, blind = tag["real_ante"], tag["blind"]
        if not 1 <= ante <= 39:
            raise ValueError("Tag Antes must be 1–38; only Ante 39 may be supplied and ignored.")
        if ante == 39:
            ignored += 1
            continue
        key = ante, blind
        previous = selected.get(key)
        if previous and previous["kind"] != tag["kind"]:
            raise ValueError("Conflicting Negative and Rare tags at %s." % position_text(key))
        selected[key] = tag
    return tuple(selected[key] for key in sorted(selected)), ignored


def prepare_row(row):
    if not isinstance(row, dict):
        raise ValueError("Each seed record must be an object.")
    seed = row.get("seed")
    if not isinstance(seed, str) or not seed.strip():
        raise ValueError("Each row needs a nonempty seed string.")
    tags, ignored = tags_for_row(row)
    negative = [position_text((t["real_ante"], t["blind"])) for t in tags if t["kind"] == "neg"]
    rare = [position_text((t["real_ante"], t["blind"])) for t in tags if t["kind"] == "rare"]
    # Keep the complete original record under input, so reserved result names
    # never overwrite user labels, ranks, scores, or unknown metadata fields.
    value = {"type": "seed", "seed": seed.strip(), "input": row,
             "negative_locations": negative, "rare_locations": rare,
             "tag_string": ",".join(("n" if t["kind"] == "neg" else "r") +
                                    position_text((t["real_ante"], t["blind"])) for t in tags),
             "tag_coverage": {"start": "A1S", "end": "A38B", "complete": True},
             "ignored_ante39_tags": ignored}
    for key in ("pool", "pool_label", "source_labels", "original_source_labels", "labels", "rank"):
        if key in row:
            value[key] = row[key]
    return value, tags


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field: " + key)
        result[key] = value
    return result


def _load_json(line):
    return json.loads(line, object_pairs_hook=_json_object,
                      parse_constant=lambda token: (_ for _ in ()).throw(ValueError("Invalid JSON number " + token)))


def _ndjson_rows(handle, metadata):
    exported = False
    finished = False
    count = 0
    for line_number, line in enumerate(iter(lambda: handle.readline(MAX_LINE_BYTES + 1), ""), 1):
        if len(line.encode("utf-8")) > MAX_LINE_BYTES:
            raise ValueError("Input line %d exceeds 16 MiB." % line_number)
        if not line.strip():
            continue
        try:
            row = _load_json(line)
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid JSON on line %d: %s" % (line_number, error)) from error
        if not isinstance(row, dict):
            raise ValueError("Line %d must contain an object." % line_number)
        kind = row.get("type")
        if finished:
            raise ValueError("Unexpected data after the completed tag export.")
        if kind == "tag_export_header":
            if count or exported:
                raise ValueError("A tag export must start with exactly one header.")
            if type(row.get("version")) is not int or row["version"] != 1:
                raise ValueError("Unsupported tag export version; use version 1.")
            if "range" in row and row["range"] != {"start": "A1S", "end": "A38B"}:
                raise ValueError("Tag export must cover A1S through A38B.")
            metadata.append(row)
            exported = True
        elif kind == "tag_export_complete":
            if "version" in row and (type(row["version"]) is not int or row["version"] != 1):
                raise ValueError("Unsupported tag export completion version.")
            if not exported or type(row.get("records")) is not int or row["records"] != count:
                raise ValueError("Tag export completion count does not match its seed records.")
            finished = True
        else:
            if kind not in (None, "seed"):
                raise ValueError("Unsupported input record type: %r" % kind)
            if exported:
                if "tag_coverage" not in row:
                    raise ValueError("A typed tag export must include complete coverage for every seed.")
                _declared_coverage(row)
            count += 1
            yield row
    if exported and not finished:
        raise ValueError("The tag export is incomplete (missing completion record). Export it again.")


@contextmanager
def input_rows(path, snapshot=None, helper=None):
    path = Path(path).resolve()
    metadata = []
    if path.suffix.lower() == ".bspool":
        try:
            import pool_tag_export
        except ImportError:
            from tools import pool_tag_export
        with pool_tag_export.open_pool(str(path), snapshot_path=snapshot, helper=helper,
                                       phase=lambda phase: print(phase.replace("_", " ") + "…", file=sys.stderr)) as source:
            metadata.append(source.metadata)
            yield source.iter_records(), metadata
        return
    if snapshot:
        raise ValueError("--snapshot is for .bspool input; CSV/NDJSON already supplies the tag locations.")
    before = path.stat()
    metadata.append({"input_path": str(path), "input_bytes": before.st_size})
    with path.open(encoding="utf-8-sig", newline="") as handle:
        if path.suffix.lower() == ".csv":
            reader = csv.DictReader(handle)
            fields = reader.fieldnames
            if not fields or len(set(fields)) != len(fields) or "seed" not in fields:
                raise ValueError("CSV needs unique column names including seed.")
            def csv_rows():
                for number, row in enumerate(reader, 2):
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("CSV row %d has the wrong number of columns." % number)
                    yield row
            rows = csv_rows()
        else:
            rows = _ndjson_rows(handle, metadata)
        # Read the prelude before consumers write their metadata header.
        first = next(rows, None)
        yield itertools.chain(() if first is None else (first,), rows), metadata
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("The input changed during scoring; run again from a stable file.")


@lru_cache(maxsize=1024)
def _cached_score(baseline, placements):
    try:
        import tagcalc_engine
    except ImportError:
        from tools import tagcalc_engine
    tags = [{"kind": kind, "real_ante": ante, "blind": blind,
             "label": "%s A%d%s" % (kind.upper(), ante, model.BLIND_SHORT[blind])}
            for kind, ante, blind in placements]
    return tagcalc_engine.optimize(tags, baseline)


def score_job(job):
    baseline, placements = job
    # The seed's starting pair and all earlier tags have already been consumed.
    future = tuple(t for t in placements if (t[1], t[2]) > baseline)
    return _cached_score(tuple(baseline), future)


def _jobs(rows, args):
    for index, row in enumerate(rows, 1):
        value, tags, job, error = None, None, None, None
        try:
            value, tags = prepare_row(row)
            if not args.export_only:
                baseline, second = starting_position(row, args.second_tag, args.baseline_copy)
                value["baseline_copy"] = position_text(baseline)
                if second:
                    value["second_tag"] = second
                job = baseline, tuple((t["kind"], t["real_ante"], t["blind"]) for t in tags)
            elif row.get("second_tag") or args.second_tag or row.get("baseline_copy") or args.baseline_copy:
                baseline, second = starting_position(row, args.second_tag, args.baseline_copy)
                value["baseline_copy"] = position_text(baseline)
                if second:
                    value["second_tag"] = second
        except (ValueError, TypeError) as exc:
            error = str(exc)
        yield index, row, value, job, error


def evaluated_rows(rows, args):
    if args.workers == 1 or args.export_only:
        for index, row, value, job, error in _jobs(rows, args):
            yield index, row, value, (score_job(job) if job is not None else None), error
        return
    # Process only normalized placements in workers; keep source metadata in the
    # parent. Bound pending work so a million-record input does not become a
    # million-future in-memory queue. Input order gives deterministic score ties.
    # Spawn gives the same module/import behavior on Windows and macOS. Pool's
    # public terminate method lets Ctrl+C stop active scoring, not merely cancel
    # jobs that have not started yet.
    executor = multiprocessing.get_context("spawn").Pool(args.workers)
    complete = False
    try:
        pending = deque()
        def finish(item):
            index, row, value, future, error = item
            return index, row, value, (future.get() if future else None), error
        for index, row, value, job, error in _jobs(rows, args):
            pending.append((index, row, value,
                            executor.apply_async(score_job, (job,)) if job is not None else None, error))
            if len(pending) >= args.workers * 2:
                yield finish(pending.popleft())
        while pending:
            yield finish(pending.popleft())
        complete = True
    finally:
        if complete:
            executor.close()
        else:
            executor.terminate()
        executor.join()


def retain_leader(leaders, by_seed, value, index, limit):
    """Exact top K distinct seeds, with memory bounded by K, not input size."""
    entry = value["score"], -index, value
    seed = value["seed"]
    previous = by_seed.get(seed)
    if previous is not None:
        if entry[:2] > previous[:2]:
            leaders[leaders.index(previous)] = entry
            heapq.heapify(leaders)
            by_seed[seed] = entry
    elif len(leaders) < limit:
        heapq.heappush(leaders, entry)
        by_seed[seed] = entry
    elif entry[:2] > leaders[0][:2]:
        evicted = heapq.heapreplace(leaders, entry)
        del by_seed[evicted[2]["seed"]]
        by_seed[seed] = entry


def _write_json(handle, value):
    handle.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


def _csv_safe(value):
    # Labels and seed strings can be arbitrary imported data. Keep spreadsheet
    # formula characters inert in CSV; the NDJSON keeps exact original values.
    text = str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else text


def _voucher_plan(fillers):
    return [{"voucher": "Hieroglyph" if i == 0 else "Petroglyph",
             "before_real_ante": point if point <= 38 else None,
             "after_real_ante": point - 1,
             "timing": "after real Ante %d / before real Ante %d" % (point - 1, point)
                       if point <= 38 else "after real Ante 38"}
            for i, point in enumerate(fillers)]


def batch(args):
    output = Path(args.output_dir or (Path(args.input).stem + "-tagcalc-" + time.strftime("%Y%m%d-%H%M%S"))).resolve()
    if output.exists():
        raise ValueError("Output folder already exists. Choose a new --output-dir; previous results are kept.")
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    summary = {"model_version": MODEL_VERSION, "status": "running", "input": str(Path(args.input).resolve()),
               "output_dir": str(output), "records": 0, "exported": 0, "scored": 0, "invalid": 0,
               "no_valid_route": 0, "top": args.top, "workers": args.workers,
               "reference_seed": args.reference_seed, "reference_score": args.reference_score,
               "real_ante_range": [1, 38], "filler_antes": 2,
               "score_unit": "final spectral count / baseline BP+BS (T0=1)",
               "probability_constants": {name: getattr(model, name) for name in (
                   "C_NORMAL", "C_FINAL", "NORMAL_BP_SHARE", "NORMAL_BS_SHARE",
                   "P_BS_ETERNAL", "P_RARE_EDITION_WASTE", "P_RARE_NAT_NEG")},
               "baseline_default": args.baseline_copy, "second_tag_default": args.second_tag,
               "source_metadata": []}
    leaders = []
    leaders_by_seed = {}
    last_progress = started
    try:
        with ExitStack() as stack:
            rows, metadata = stack.enter_context(input_rows(args.input, args.snapshot))
            summary["source_metadata"] = metadata
            streams = {name: stack.enter_context((output / name).open("x", encoding="utf-8", newline=""))
                       for name in ("tags.ndjson", "scores.ndjson", "errors.ndjson")}
            _write_json(streams["tags.ndjson"], {"type": "tag_export_header", "version": 1,
                                                "metadata": metadata, "model_version": MODEL_VERSION})
            for index, row, value, result, error in evaluated_rows(rows, args):
                summary["records"] += 1
                if value is not None:
                    value["input_index"] = index
                    _write_json(streams["tags.ndjson"], value)
                    summary["exported"] += 1
                if error:
                    summary["invalid"] += 1
                    scored = {"seed": row.get("seed"), "input_index": index, "status": "invalid",
                              "error": error, "input": row}
                    _write_json(streams["errors.ndjson"], scored)
                elif args.export_only:
                    scored = dict(value, status="exported")
                else:
                    score, route, fillers = result
                    if score <= 0 or not route:
                        summary["no_valid_route"] += 1
                        scored = dict(value, status="no_valid_route", score=None, route=[])
                    else:
                        if not math.isfinite(score):
                            raise ValueError("Non-finite model score at input row %d." % index)
                        summary["scored"] += 1
                        scored = dict(value, status="scored", score=score, route=route,
                                      fillers=list(fillers), vouchers=_voucher_plan(fillers),
                                      beats_reference=(score > args.reference_score
                                                       if args.reference_score is not None else None))
                        retain_leader(leaders, leaders_by_seed, scored, index, args.top)
                _write_json(streams["scores.ndjson"], scored)
                now = time.monotonic()
                if now - last_progress >= 2:
                    elapsed = now - started
                    print("%d processed · %d scored · %d invalid · %.1f seeds/s" %
                          (summary["records"], summary["scored"], summary["invalid"],
                           summary["records"] / elapsed), file=sys.stderr, flush=True)
                    last_progress = now
        # Exit the source context first: it validates the final pinned source
        # identity. A failing late check must leave an incomplete tag export.
        with (output / "tags.ndjson").open("a", encoding="utf-8") as handle:
            _write_json(handle, {"type": "tag_export_complete", "version": 1, "records": summary["exported"]})
        # No leaderboard is published until the input's final checksum/trailer
        # and source identity checks have succeeded.
        with (output / "leaderboard.ndjson").open("x", encoding="utf-8") as ndjson, \
                (output / "leaderboard.csv").open("x", encoding="utf-8-sig", newline="") as csvfile:
            fields = ("position", "seed", "score", "beats_reference", "baseline_copy", "second_tag",
                      "pool", "hieroglyph", "petroglyph", "redeem_plan",
                      "negative_locations", "rare_locations", "metadata")
            writer = csv.DictWriter(csvfile, fieldnames=fields)
            writer.writeheader()
            for rank, (_, _, value) in enumerate(sorted(leaders, key=lambda x: (-x[0], -x[1])), 1):
                ranked = dict(value, leaderboard_position=rank)
                _write_json(ndjson, ranked)
                csvrow = {"position": rank, "seed": value["seed"], "score": format(value["score"], ".12g"),
                          "beats_reference": value["beats_reference"], "baseline_copy": value["baseline_copy"],
                          "second_tag": value.get("second_tag", ""),
                          "pool": value.get("pool", value.get("pool_label", "")),
                          "negative_locations": ",".join(value["negative_locations"]),
                          "rare_locations": ",".join(value["rare_locations"]),
                          "hieroglyph": value["vouchers"][0]["timing"],
                          "petroglyph": value["vouchers"][1]["timing"],
                          "redeem_plan": " | ".join("%d. %s + %s; redeem at %s%s" %
                              (i, step["neg_label"], step["rare_label"], step["redeem_shop_desc"],
                               " (final)" if step["final"] else "")
                              for i, step in enumerate(value["route"], 1)),
                          "metadata": json.dumps(value["input"], ensure_ascii=False)}
                writer.writerow({key: _csv_safe(val) for key, val in csvrow.items()})
        summary["leaderboard_records"] = len(leaders)
        summary["status"] = "completed_with_errors" if summary["invalid"] else "complete"
    except BaseException as error:
        summary["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        summary["error"] = str(error) or type(error).__name__
        raise
    finally:
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
                                           encoding="utf-8")
    print("%d seeds processed; %d scored; %d without a route; %d invalid.\nResults: %s" %
          (summary["records"], summary["scored"], summary["no_valid_route"], summary["invalid"], output))
    return 2 if summary["invalid"] else 0


def parser():
    result = argparse.ArgumentParser(description="Score seed batches and preserve tag placements and labels.")
    result.add_argument("--input", help="CSV, NDJSON/JSONL, or .bspool input")
    result.add_argument("--output-dir", help="New folder for exported tags, scores, and leaderboard")
    result.add_argument("--second-tag", help="Default starting second tag for this pool, e.g. A4S or A4B")
    result.add_argument("--baseline-copy", help="Default first-copy shop; usually derived from --second-tag")
    result.add_argument("--tags", help="Single-seed tags, e.g. r7b,n10b,r12s")
    result.add_argument("--top", type=int, help="Leaderboard size (batch default 1000; single-seed routes default 1)")
    result.add_argument("--workers", type=int, default=1, help="Scoring processes (default 1)")
    result.add_argument("--snapshot", help="Matching native_search.cfg to record full .bspool tag locations")
    result.add_argument("--export-only", action="store_true", help="Export locations and metadata without scoring")
    result.add_argument("--reference-score", type=float, help="Flag scores above this normalized multiplier, e.g. 721.77")
    result.add_argument("--reference-seed", help="Label for the reference score, e.g. 5MSXV6")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        args.top = args.top if args.top is not None else (1000 if args.input else 1)
        if args.top < 1 or not 1 <= args.workers <= 256:
            raise ValueError("--top must be positive; --workers must be between 1 and 256.")
        if args.reference_score is not None and (not math.isfinite(args.reference_score) or args.reference_score < 0):
            raise ValueError("--reference-score must be a finite, nonnegative number.")
        if args.second_tag:
            baseline_for_second_tag(args.second_tag)
        if args.baseline_copy:
            position(args.baseline_copy)
        if args.second_tag and args.baseline_copy:
            starting_position({}, args.second_tag, args.baseline_copy)
        if args.input:
            if args.tags is not None:
                raise ValueError("Use --input for a batch or --tags for one seed.")
            return batch(args)
        if args.snapshot or args.export_only or args.output_dir:
            raise ValueError("--snapshot, --export-only and --output-dir need --input.")
        second = args.second_tag
        if not second and not args.baseline_copy:
            second = input("Starting second tag (A4S → A4B first copy; A4B → A4Boss): ").strip()
        baseline, _ = starting_position({}, second, args.baseline_copy)
        tags_text = args.tags if args.tags is not None else input("All Negative/Rare tags, Antes 1–38 (r7b,n10b,…): ").strip()
        tags, _ = tags_for_row({"tags": tags_text})
        canonical = ",".join(("n" if t["kind"] == "neg" else "r") +
                             position_text((t["real_ante"], t["blind"])) for t in tags)
        model.run_optimizer(position_text(baseline), canonical, args.top)
        return 0
    except KeyboardInterrupt:
        print("Stopped. Partial output is marked interrupted in summary.json.", file=sys.stderr)
        return 130
    except Exception as error:
        print("Error: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
