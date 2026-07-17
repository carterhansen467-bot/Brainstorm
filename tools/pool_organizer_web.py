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
        shutil.rmtree(stage, ignore_errors=True)


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
.privacy{margin-bottom:18px;padding:12px 15px;border:1px solid #3c3650;border-radius:12px;background:#191624cc;color:#cac5d6;font-size:13px}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px;align-items:start}.stack{display:grid;gap:18px}
.card{padding:21px;border:1px solid var(--line);border-radius:18px;background:linear-gradient(180deg,#191c28f5,#141722f5);box-shadow:var(--shadow)}
.head{display:flex;gap:12px;margin-bottom:17px}.step{flex:0 0 auto;display:grid;place-items:center;width:30px;height:30px;border:1px solid #443963;border-radius:9px;background:#29213b;color:#c7baff;font-weight:800}
h2{margin:0;font-size:17px}.copy{margin:3px 0 0;color:var(--muted);font-size:13px}.field{display:grid;gap:6px;margin-top:12px}label,.label{font-size:12px;font-weight:750;color:#cbc6d5}
select,input[type=text]{width:100%;min-height:42px;padding:9px 11px;border:1px solid #3b4159;border-radius:10px;background:#0f1119;color:var(--text)}
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
[hidden]{display:none!important}@media(max-width:850px){.grid{grid-template-columns:1fr}.side{position:static;grid-row:1}}@media(max-width:580px){.top{display:grid}.two,.source{grid-template-columns:1fr}.ambrow{grid-template-columns:1fr}.card{padding:17px}}
</style></head><body><main class="app">
<header class="top"><div class="brand"><div class="mark">B</div><div><h1>Seed Pool Organizer</h1><p class="sub">Inspect, export, and split recorded seed locations without rerunning the search.</p></div></div><div class="local">Running locally</div></header>
<div class="privacy"><strong>Your pools stay on this computer.</strong> This page only talks to Brainstorm's local organizer. Outputs are published into <code>seed_pools</code> so the mod can see them immediately.</div>
<div class="grid"><div class="stack">
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
</section></aside></div></main>
<script>
const $=id=>document.getElementById(id);let inspected=null,plan=null,choices={},page=0;const PAGE_SIZE=100;
const esc=v=>String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
const fmt=n=>Number(n||0).toLocaleString();
async function api(path,data){const opt=data?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)}:{};const r=await fetch(path,opt);const v=await r.json();if(!r.ok||v.error)throw Error(v.error||`Request failed (${r.status})`);return v}
function fail(e){$("error").textContent=e.message||String(e)}function clear(){$("error").textContent="";$("result").innerHTML=""}
async function loadPools(){clear();const v=await api("/api/pools");const old=$("source").value;$("source").innerHTML=v.pools.length?v.pools.map(p=>`<option value="${esc(p.name)}" ${p.error?"disabled":""}>${esc(p.name)}${p.error?" · unreadable":` · ${fmt(p.records)} seeds${p.complete?"":" · paused"}`}</option>`).join(""):'<option value="">No .bspool files found</option>';if([...$("source").options].some(o=>o.value===old))$("source").value=old}
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
$("inspectBtn").onclick=inspect;$("refreshBtn").onclick=loadPools;$("allBtn").onclick=()=>document.querySelectorAll(".cat").forEach(x=>x.checked=true);$("noneBtn").onclick=()=>document.querySelectorAll(".cat").forEach(x=>x.checked=false);$("planBtn").onclick=prepare;$("saveBtn").onclick=savePlan;$("loadBtn").onclick=()=>$("loadFile").click();$("loadFile").onchange=e=>e.target.files[0]&&loadPlanFile(e.target.files[0]);$("splitBtn").onclick=split;$("policy").onchange=()=>{$("remainderField").hidden=$("policy").value!=="remainder";renderStatus()};$("exportBtn").onclick=()=>{if(!inspected)return;location.href=`/api/export?source=${encodeURIComponent($("source").value)}&snapshot=${encodeURIComponent(inspected.source.snapshot_id)}`};document.addEventListener("change",e=>{if(e.target.classList.contains("cat")){plan=null;choices={};$("plan").hidden=true;$("saveBtn").disabled=true;$("splitBtn").disabled=true}});loadPools();
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
            raise organizer.PoolError("choice plan request is too large")
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
            elif parsed.path == "/api/split":
                if not SPLIT_LOCK.acquire(False):
                    raise organizer.PoolError("another organizer split is still running")
                try:
                    report = execute_split(data.get("source", ""), data, self.pool_dir)
                finally:
                    SPLIT_LOCK.release()
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
