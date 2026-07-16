#!/usr/bin/env python3
"""Brainstorm Seed Pool Builder -- point-and-click browser UI.

Runs a tiny local-only web server (127.0.0.1, Python stdlib, nothing
installed, nothing leaves the machine) and opens the page in the default
browser. Non-terminal users double-click "Seed Pool Builder.command" in the
mod folder; everything else -- building the native scanner, reading the
game's snapshot, writing criteria, running/pausing/resuming the scan -- is
buttons on the page. Finished pools land in seed_pools/ where the in-game
Seed Pool selector picks them up, and the .bspool file is what you share.

The criteria/runner engine is shared with the curses UI
(tools/brainstorm_pool_builder.py); this file is only the web front end.
"""

import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brainstorm_pool_builder as core

DEFAULT_PORT = 8917
LOCK = threading.Lock()
JOB = {"runner": None, "kind": None, "started": 0.0, "summary": "", "error": ""}


# ------------------------------------------------------------- criteria ----

def criteria_from_json(data, snap):
    c = core.Criteria()
    legend = data.get("legendary") or ""
    legal = [""] + snap.usable_legendaries()
    if legend not in legal:
        raise ValueError("Unknown legendary %r" % legend)
    c.legendary = legend
    c.leg_min = clamp_int(data.get("legMin", 1), 1, core.MAX_ANTE)
    c.leg_max = clamp_int(data.get("legMax", 8), c.leg_min, core.MAX_ANTE)
    c.leg_neg = bool(data.get("legNeg"))
    # "1" = first Soul only, "any" = up to 2 Souls deep; "2"/legSecondSoul
    # kept for older clients (legacy exclusive second-Soul pools).
    depth_map = {"1": 1, "2": 2, "any": 0}
    if "legSoulDepth" in data:
        c.leg_soul_depth = depth_map.get(str(data.get("legSoulDepth")), 1)
    elif data.get("legSecondSoul"):
        c.leg_soul_depth = 2
    tag_keys = {k for k, _ in snap.usable_tags()}
    min_ante = dict(snap.usable_tags())
    for rule in data.get("rules", [])[: core.MAX_TAG_RULES]:
        key = rule.get("key", "")
        if key not in tag_keys:
            raise ValueError("Unknown or locked tag %r" % key)
        lo = clamp_int(rule.get("min", 1), 1, core.MAX_ANTE)
        hi = clamp_int(rule.get("max", 8), lo, core.MAX_ANTE)
        if min_ante.get(key, 0) > hi:
            raise ValueError("%s cannot appear before ante %d"
                             % (core.TAG_NAMES.get(key, key), min_ante[key]))
        cnt = clamp_int(rule.get("count", 1), 1, 2 * (hi - lo + 1))
        c.tag_rules.append([key, lo, hi, cnt])
    if not c.predicates():
        raise ValueError("Pick a legendary or add at least one tag requirement.")
    c.route_collect = data.get("route", "collect") != "observe"
    c.threads = clamp_int(data.get("threads", 0), 0, 64)
    space = data.get("space", "natural")
    if space not in (s[0] for s in core.SPACES):
        raise ValueError("Unknown seed space %r" % space)
    c.space = space
    c.shard_total = clamp_int(data.get("shardTotal", 1), 1, 256)
    if c.shard_total not in core.SHARD_COUNTS:
        raise ValueError("Distributed parts must be one of %s" % (core.SHARD_COUNTS,))
    c.shard_index = clamp_int(data.get("shardIndex", 1), 1, c.shard_total)
    name = re.sub(r"[^A-Za-z0-9._+-]", "-", str(data.get("name", "")).strip())
    if name:
        c.name, c.name_edited = name, True
    return c


def clamp_int(v, lo, hi):
    try:
        v = int(v)
    except (TypeError, ValueError):
        v = lo
    return max(lo, min(hi, v))


# ---------------------------------------------------------------- pools ----

def read_pool_header(path):
    try:
        with open(path, "rb") as f:
            raw = f.read(1024).split(b"\0", 1)[0].decode("latin-1")
    except OSError:
        return {}
    out = {"criteria": []}
    for line in raw.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] in ("records", "complete", "modelver", "pool_id", "space", "encoding",
                        "refilter_depth", "source_pool_id", "range_start", "range_end",
                        "merged_parts"):
            out[parts[0]] = parts[1] if len(parts) > 1 else ""
        elif parts[0] == "label":
            out["label"] = line.split(None, 1)[1] if len(parts) > 1 else ""
        elif parts[0] in ("tag", "route_tag", "legendary", "route_legendary",
                          "soul_depth", "tag_route"):
            out["criteria"].append(line)
        elif parts[0] == "end":
            break
    return out


def list_pools():
    pools = []
    if not os.path.isdir(core.POOL_DIR):
        return pools
    for fn in sorted(os.listdir(core.POOL_DIR)):
        if not fn.endswith(".bspool"):
            continue
        path = os.path.join(core.POOL_DIR, fn)
        head = read_pool_header(path)
        state = core.read_state(path + ".state")
        pools.append({
            "name": fn,
            "bytes": os.path.getsize(path),
            "records": int(head.get("records", "0") or 0),
            "complete": head.get("complete") == "1",
            "resumable": state.get("done") == "0",
            "criteria": head.get("criteria", []),
            "label": head.get("label", ""),
            "pool_id": head.get("pool_id", ""),
            "space": head.get("space", "natural"),
            "refilter_depth": int(head.get("refilter_depth", "0") or 0),
            "source_pool_id": head.get("source_pool_id", ""),
            "encoding": head.get("encoding", ""),
            "range_start": int(head.get("range_start", "0") or 0),
            "range_end": int(head.get("range_end", "0") or 0),
            "merged_parts": int(head.get("merged_parts", "0") or 0),
        })
    return pools


# ------------------------------------------------------------------ job ----

def job_state():
    r = JOB["runner"]
    out = {"running": False, "kind": JOB["kind"], "summary": JOB["summary"],
           "error": JOB["error"]}
    if r is None:
        return out
    out.update({
        "running": not r.done(),
        "scanned": r.scanned, "total": r.total, "matched": r.matched,
        "rate": r.rate, "elapsed": time.time() - JOB["started"],
        "lines": list(r.lines)[-8:],
        "output": os.path.basename(r.output),
    })
    if r.done():
        out["rc"] = r.returncode()
        out["manifest"] = core.read_manifest(r.output + ".manifest")
    return out


def start_job(kind, data, snap):
    with LOCK:
        r = JOB["runner"]
        if r is not None and not r.done():
            raise ValueError("A scan is already running -- stop it first.")
        crit = criteria_from_json(data, snap)
        input_name = os.path.basename(str(data.get("inputPool", "")))
        input_pool = None
        if input_name:
            candidate = os.path.join(core.POOL_DIR, input_name)
            head = read_pool_header(candidate)
            if not input_name.endswith(".bspool") or not os.path.isfile(candidate):
                raise ValueError("The selected input pool no longer exists.")
            if head.get("complete") != "1":
                raise ValueError("Finish the selected pool before filtering it again.")
            input_pool = candidate
            crit.space = head.get("space", "natural")
            if crit.shard_total > 1:
                raise ValueError("Distributed parts apply to Balatro's seed space, not an input pool.")
        if kind == "estimate":
            sample = clamp_int(data.get("sample", 100_000_000), 100_000, SEEDCAP)
            out = os.path.join(tempfile.mkdtemp(prefix="bs_pool_est_"), "estimate")
            text = crit.text("count", sample, apply_shard=False)
        else:
            os.makedirs(core.POOL_DIR, exist_ok=True)
            out = os.path.join(core.POOL_DIR, crit.pool_name() + ".bspool")
            if core.read_state(out + ".state").get("done") == "1":
                raise ValueError("That pool is already complete. Pick a different "
                                 "name, or delete the old files first.")
            count = clamp_int(data.get("count", 0), 0, SEEDCAP)
            text = crit.text("binary", count)
        if input_pool and os.path.abspath(input_pool) == os.path.abspath(out):
            raise ValueError("Choose a new output name; a pool cannot overwrite its own input.")
        JOB.update(runner=core.Runner(snap.current_model_copy(), text, out, input_pool),
                   kind=kind, started=time.time(), summary=crit.summary(),
                   error="")


def start_merge_job(data):
    with LOCK:
        r = JOB["runner"]
        if r is not None and not r.done():
            raise ValueError("A scan or merge is already running.")
        names = []
        for value in data.get("pools", []):
            name = os.path.basename(str(value))
            if name and name not in names:
                names.append(name)
        if len(names) < 2:
            raise ValueError("Select at least two completed shard pools.")
        inputs = []
        for name in names:
            path = os.path.join(core.POOL_DIR, name)
            head = read_pool_header(path)
            if not name.endswith(".bspool") or not os.path.isfile(path):
                raise ValueError("A selected pool no longer exists: %s" % name)
            if head.get("complete") != "1":
                raise ValueError("Finish %s before merging it." % name)
            inputs.append(path)
        base = re.sub(r"[^A-Za-z0-9._+-]", "-", str(data.get("name", "")).strip())
        if base.lower().endswith(".bspool"):
            base = base[:-7]
        if not base:
            base = "merged-pool"
        os.makedirs(core.POOL_DIR, exist_ok=True)
        output = os.path.join(core.POOL_DIR, base + ".bspool")
        if os.path.exists(output):
            raise ValueError("That output already exists. Pick a different merged-pool name.")
        JOB.update(runner=core.MergeRunner(inputs, output), kind="merge",
                   started=time.time(), summary="Merging %d shard pools" % len(inputs),
                   error="")


SEEDCAP = core.SEEDSPACE_TOTAL  # UI clamp only; the scanner enforces per-space bounds


# ------------------------------------------------------------------ http ---

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Brainstorm Seed Pool Builder</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {
  color-scheme: dark;
  --bg:#0c0e14; --surface:#151823; --surface-2:#1b1f2d; --surface-3:#23283a;
  --line:#30364c; --line-soft:#252a3b; --text:#f4f1fa; --muted:#a7a3b5;
  --faint:#777488; --gold:#f7c948; --gold-2:#ffe08a; --blue:#72b7ff;
  --green:#55d889; --red:#ff7474; --purple:#9d8cff; --shadow:0 18px 50px #0006;
}
* { box-sizing:border-box; }
html { scroll-behavior:smooth; }
body { margin:0; min-height:100vh; background:
  radial-gradient(circle at 12% -5%, #332852 0, transparent 32rem),
  radial-gradient(circle at 92% 8%, #15334b 0, transparent 28rem), var(--bg);
  color:var(--text); font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
button, select, input { font:inherit; }
button:focus-visible, select:focus-visible, input:focus-visible, summary:focus-visible {
  outline:3px solid #72b7ff55; outline-offset:2px; }
.app { width:min(1180px, calc(100% - 32px)); margin:0 auto; padding:34px 0 70px; }
.topbar { display:flex; justify-content:space-between; align-items:flex-start; gap:24px;
  margin-bottom:26px; }
.brand { display:flex; gap:14px; align-items:center; }
.brand-mark { width:48px; height:48px; display:grid; place-items:center; border-radius:15px;
  background:linear-gradient(145deg, #f8d35c, #c99122); color:#211906;
  font-size:27px; font-weight:900; box-shadow:0 10px 30px #e0aa2638; }
h1 { margin:0; font-size:clamp(23px, 3vw, 32px); line-height:1.1; letter-spacing:-.03em; }
.sub { color:var(--muted); max-width:720px; margin:7px 0 0; font-size:14px; }
.local-pill { display:inline-flex; align-items:center; gap:8px; white-space:nowrap;
  padding:8px 12px; border:1px solid #2d5642; border-radius:999px; color:#a8e7bf;
  background:#14251d; font-size:12px; font-weight:700; }
.local-pill:before { content:""; width:8px; height:8px; border-radius:50%; background:var(--green);
  box-shadow:0 0 0 4px #55d8891f; }
.notice { display:flex; gap:12px; align-items:flex-start; padding:13px 15px; margin-bottom:20px;
  border:1px solid #3b3551; border-radius:12px; background:#191624cc; color:#c9c4d7; font-size:13px; }
.notice strong { color:var(--gold-2); }
.workspace { display:grid; grid-template-columns:minmax(0, 1fr) 330px; gap:18px; align-items:start; }
.stack { display:grid; gap:18px; }
.card { background:linear-gradient(180deg, #181b27f5, #141722f5); border:1px solid var(--line);
  border-radius:18px; padding:22px; box-shadow:var(--shadow); }
.card-head { display:flex; align-items:flex-start; gap:13px; margin-bottom:20px; }
.step { flex:0 0 auto; display:grid; place-items:center; width:30px; height:30px; border-radius:9px;
  background:#28213a; color:#c7baff; border:1px solid #443962; font-size:13px; font-weight:800; }
.card h2 { margin:0; font-size:17px; letter-spacing:-.01em; }
.card-copy { margin:3px 0 0; color:var(--muted); font-size:13px; }
.section-label { margin:18px 0 9px; color:#d9d4e5; font-size:12px; font-weight:800;
  letter-spacing:.08em; text-transform:uppercase; }
.field-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:14px; }
.field-grid.three { grid-template-columns:repeat(3, minmax(0, 1fr)); }
.field { display:grid; gap:6px; align-content:start; min-width:0; }
.field.full { grid-column:1 / -1; }
label, .label { color:#c8c3d2; font-size:12px; font-weight:700; }
.hint { color:var(--faint); font-size:12px; line-height:1.4; }
select, input[type=number], input[type=text] { width:100%; min-height:42px; padding:9px 11px;
  color:var(--text); background:#0f111a; border:1px solid #3a4058; border-radius:10px; }
select:hover, input[type=number]:hover, input[type=text]:hover { border-color:#59617e; }
select:disabled, input:disabled { opacity:.48; cursor:not-allowed; }
.check { min-height:42px; display:flex; align-items:center; gap:9px; padding:9px 11px;
  border:1px solid #353a50; border-radius:10px; background:#11141e; color:#d9d5e1;
  font-size:13px; font-weight:600; cursor:pointer; }
.check input { width:17px; height:17px; margin:0; accent-color:var(--purple); }
#legRange { display:grid; grid-template-columns:1.6fr .7fr .7fr 1fr; gap:12px;
  grid-column:1 / -1; padding:14px; border:1px solid var(--line-soft); border-radius:12px;
  background:#11141e; }
#rules { display:grid; gap:9px; }
.rule { display:grid; grid-template-columns:minmax(160px, 1.7fr) .65fr .65fr .65fr auto;
  gap:9px; align-items:end; padding:12px; border:1px solid var(--line-soft);
  border-radius:12px; background:#11141e; }
.rule-field { display:grid; gap:5px; min-width:0; }
.empty-rules { padding:16px; border:1px dashed #34394e; border-radius:12px; text-align:center;
  color:var(--faint); font-size:13px; }
.button-row { display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-top:14px; }
button { min-height:42px; padding:9px 15px; border:1px solid transparent; border-radius:10px;
  color:white; background:#3b5fd1; font-weight:750; cursor:pointer; transition:.16s ease; }
button:hover:not(:disabled) { transform:translateY(-1px); filter:brightness(1.12); }
button:active:not(:disabled) { transform:translateY(0); }
button:disabled { background:#272b3b; color:#706e7a; cursor:not-allowed; }
button.go { background:linear-gradient(135deg, #27814c, #35a962); box-shadow:0 9px 24px #2f9a5928; }
button.warn { background:#592d33; border-color:#7b3b44; color:#ffc0c0; }
button.mini { min-height:34px; padding:6px 10px; background:#292e42; border-color:#3d435d;
  color:#d8d5e1; font-size:12px; }
button.ghost { background:transparent; border-color:#3b425c; color:#d3cfdd; }
.advanced { margin-top:16px; border:1px solid var(--line-soft); border-radius:12px; background:#11141e; }
.advanced summary { cursor:pointer; padding:12px 14px; color:#c9c5d3; font-size:13px;
  font-weight:750; list-style:none; }
.advanced summary::-webkit-details-marker { display:none; }
.advanced summary:after { content:"＋"; float:right; color:var(--faint); }
.advanced[open] summary:after { content:"−"; }
.advanced-body { padding:0 14px 14px; }
.side { position:sticky; top:18px; display:grid; gap:18px; }
.summary-card { background:linear-gradient(165deg, #201b30, #151823 58%); }
.summary-title { display:flex; justify-content:space-between; gap:10px; align-items:center; }
.summary-title h2 { font-size:16px; }
.ready-pill { padding:4px 8px; border-radius:999px; background:#382f16; color:#f4d46b;
  font-size:10px; font-weight:850; letter-spacing:.08em; text-transform:uppercase; }
.summary-list { display:grid; gap:0; margin:15px 0 0; }
.summary-item { display:grid; grid-template-columns:82px 1fr; gap:10px; padding:10px 0;
  border-top:1px solid #302b42; font-size:12px; }
.summary-item dt { color:#858095; }
.summary-item dd { margin:0; color:#e3deeb; text-align:right; overflow-wrap:anywhere; }
.primary-actions { display:grid; gap:9px; margin-top:16px; }
.primary-actions button { width:100%; }
#error, #mergeError { min-height:0; margin-top:10px; color:#ffaaaa; font-size:13px;
  white-space:pre-wrap; }
.progress-card { overflow:hidden; }
.progress-top { display:flex; justify-content:space-between; gap:14px; align-items:flex-start; }
.progress-state { color:var(--blue); font-size:12px; font-weight:800; text-transform:uppercase;
  letter-spacing:.08em; }
#bar { height:11px; margin-top:17px; background:#0d1018; border-radius:999px; overflow:hidden;
  border:1px solid #343a50; }
#fill { height:100%; width:0%; background:linear-gradient(90deg, #7c6fe5, #54b8ec, #55d889);
  transition:width .45s ease; }
.stats { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:9px; margin-top:14px; }
.stat { padding:10px; border:1px solid var(--line-soft); border-radius:10px; background:#11141e; }
.stat span { display:block; color:var(--faint); font-size:10px; font-weight:800; text-transform:uppercase;
  letter-spacing:.06em; }
.stat b { display:block; margin-top:3px; color:var(--gold-2); font-size:13px; font-weight:750;
  overflow-wrap:anywhere; }
#log { font:11px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; color:#8d899a;
  white-space:pre-wrap; margin-top:12px; max-height:130px; overflow:auto; }
#result { margin-top:12px; padding:12px; border-radius:10px; color:#bcefcf; background:#12231a;
  border:1px solid #28533a; white-space:pre-wrap; font-size:12px; }
#result:empty { display:none; }
.library { margin-top:18px; }
.library-head { display:flex; justify-content:space-between; align-items:flex-end; gap:18px; margin-bottom:16px; }
.library-head h2 { margin:0; font-size:19px; }
.library-head p { margin:4px 0 0; color:var(--muted); font-size:13px; }
.pool-grid { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:11px; }
.pool { position:relative; min-width:0; padding:15px; border:1px solid var(--line-soft);
  border-radius:13px; background:#11141e; font-size:12px; }
.pool-top { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
.pool-name { display:flex; gap:8px; align-items:flex-start; min-width:0; }
.pool-name b { color:#eeeaf4; overflow-wrap:anywhere; }
.pool-name input { margin-top:3px; accent-color:var(--purple); }
.status { flex:0 0 auto; padding:3px 7px; border-radius:999px; font-size:10px; font-weight:850;
  text-transform:uppercase; letter-spacing:.05em; }
.status.ok { background:#163424; color:#80e4a6; }
.status.part { background:#3b3017; color:#f4d46b; }
.pool-meta { margin-top:9px; color:#a19dad; }
.pool-criteria { margin-top:9px; color:#757286; line-height:1.45; overflow-wrap:anywhere; }
.empty-library { grid-column:1 / -1; padding:30px; text-align:center; border:1px dashed #34394e;
  border-radius:13px; color:var(--faint); }
.merge-panel { display:grid; grid-template-columns:1fr auto; gap:10px; align-items:end; }
.tagc { color:var(--muted); }
.ok { color:var(--green); } .part { color:var(--gold); }
@media (max-width:900px) {
  .workspace { grid-template-columns:1fr; }
  .side { position:static; grid-row:1; }
  .summary-card { order:2; }
  .progress-card { order:1; }
}
@media (max-width:650px) {
  .app { width:min(100% - 20px, 1180px); padding-top:20px; }
  .topbar { display:grid; gap:14px; }
  .local-pill { width:max-content; }
  .card { padding:17px; border-radius:15px; }
  .field-grid, .field-grid.three, #legRange { grid-template-columns:1fr; }
  .rule { grid-template-columns:1fr 1fr; }
  .rule .rule-field:first-child { grid-column:1 / -1; }
  .rule button { grid-column:1 / -1; }
  .pool-grid { grid-template-columns:1fr; }
  .merge-panel { grid-template-columns:1fr; }
  .library-head { display:block; }
}
</style></head><body>
<main class="app">
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">B</div>
      <div>
        <h1>Seed Pool Builder</h1>
        <p class="sub">Create reusable seed pools for Brainstorm without touching the command line.</p>
      </div>
    </div>
    <div class="local-pill" id="serverStatus">Running locally</div>
  </header>

  <div class="notice"><span aria-hidden="true">◆</span><div><strong>Your data stays here.</strong>
    This page only talks to the builder on this computer. Closing it safely pauses an active scan at its next checkpoint.</div></div>

  <div class="workspace">
    <div class="stack">
      <section class="card" aria-labelledby="filterTitle">
        <div class="card-head"><span class="step">1</span><div>
          <h2 id="filterTitle">Choose what seeds must contain</h2>
          <p class="card-copy">Use a Legendary, one or more tags, or combine both.</p>
        </div></div>

        <div class="section-label">Legendary joker</div>
        <div class="field-grid">
          <div class="field full"><label for="legendary">Target Legendary</label>
            <select id="legendary"></select></div>
          <div id="legRange">
            <div class="field"><label for="legDepth">Soul search depth</label>
              <select id="legDepth" title="Two Souls deep also accepts a seed whose first Soul contains a different Legendary.">
                <option value="1">First Soul only</option>
                <option value="any">First or second Soul</option>
              </select></div>
            <div class="field"><label for="legMin">From ante</label><input type="number" id="legMin" min="1" max="39" value="1"></div>
            <div class="field"><label for="legMax">Through ante</label><input type="number" id="legMax" min="1" max="39" value="8"></div>
            <label class="check"><input type="checkbox" id="legNeg"> Require Negative</label>
          </div>
          <div class="hint full">Souls are checked chronologically across reachable shop packs and collected Charm or Ethereal rewards.</div>
        </div>

        <div class="section-label">Tag requirements</div>
        <div id="rules"><div class="empty-rules">No tags required yet.</div></div>
        <div class="button-row"><button type="button" class="mini" onclick="addRule()">＋ Add tag requirement</button></div>

        <div class="section-label">Route behavior</div>
        <div class="field"><label for="route">When a required tag appears</label>
          <select id="route">
            <option value="collect">Skip the blind and collect it (recommended)</option>
            <option value="observe">Play the blind and only observe it</option>
          </select>
          <span class="hint">This changes the route used to determine which later shops and packs are reachable.</span>
        </div>
      </section>

      <section class="card" aria-labelledby="scanTitle">
        <div class="card-head"><span class="step">2</span><div>
          <h2 id="scanTitle">Set the search range</h2>
          <p class="card-copy">Start with a quick estimate, then scale up when the result looks practical.</p>
        </div></div>
        <div class="field-grid">
          <div class="field full"><label for="inputPool">Search within</label>
            <select id="inputPool"><option value="">Balatro's seed space</option></select>
            <span class="hint">Choose a finished pool to filter it again with narrower criteria.</span></div>
          <div class="field"><label for="space">Seed space</label>
            <select id="space">
              <option value="natural">Natural seeds · 1.79 trillion</option>
              <option value="total">All typeable seeds · 2.90 trillion</option>
            </select></div>
          <div class="field"><label for="count">Scan scope</label>
            <select id="count">
              <option value="0" id="optAll">Entire seed space (can take days)</option>
              <option value="10000000000">First 10 billion seeds</option>
              <option value="1000000000">First 1 billion seeds</option>
              <option value="100000000" selected>First 100 million · quick test</option>
            </select></div>
          <div class="field"><label for="threads">CPU threads</label>
            <select id="threads"><option value="0">Auto (recommended)</option></select></div>
          <div class="field"><label for="name">Pool name</label>
            <input type="text" id="name" placeholder="Generated automatically" size="22"></div>
          <div class="hint full" id="spaceHint"></div>
        </div>

        <details class="advanced">
          <summary>Advanced: split work across computers</summary>
          <div class="advanced-body field-grid">
            <div class="field"><label for="shardTotal">Number of parts</label>
              <select id="shardTotal">
                <option value="1">Do not split</option>
                <option value="2">2 parts</option><option value="4">4 parts</option>
                <option value="8">8 parts</option><option value="16">16 parts</option>
                <option value="32">32 parts</option><option value="64">64 parts</option>
                <option value="128">128 parts</option><option value="256">256 parts</option>
              </select></div>
            <div class="field"><label for="shardIndex">Part for this computer</label>
              <select id="shardIndex"><option value="1">1</option></select></div>
            <div class="hint full" id="shardHint"></div>
          </div>
        </details>
      </section>
    </div>

    <aside class="side">
      <section class="card summary-card" aria-labelledby="summaryTitle">
        <div class="summary-title"><h2 id="summaryTitle">Build summary</h2><span class="ready-pill" id="readyPill">Needs filter</span></div>
        <dl class="summary-list">
          <div class="summary-item"><dt>Looking for</dt><dd id="sumFilter">Choose a Legendary or tag</dd></div>
          <div class="summary-item"><dt>Source</dt><dd id="sumSource">Balatro's natural seeds</dd></div>
          <div class="summary-item"><dt>Scope</dt><dd id="sumScope">First 100 million</dd></div>
          <div class="summary-item"><dt>Compute</dt><dd id="sumThreads">Automatic threads</dd></div>
          <div class="summary-item"><dt>Output</dt><dd id="sumOutput">Automatic name</dd></div>
        </dl>
        <div class="primary-actions">
          <button class="go" onclick="run('build')" id="btnBuild">Build seed pool</button>
          <button class="ghost" onclick="run('estimate')" id="btnEst">Estimate size and time</button>
          <button class="warn" onclick="stopJob()" id="btnStop" disabled>Pause active job</button>
        </div>
        <div id="error" role="alert"></div>
      </section>

      <section class="card progress-card" id="progressCard" style="display:none" aria-live="polite">
        <div class="progress-top"><div><div class="progress-state" id="progressState">Working</div>
          <h2 id="progTitle">Progress</h2></div><strong id="progressPct">0%</strong></div>
        <div id="bar"><div id="fill"></div></div>
        <div class="stats">
          <div class="stat"><span>Scanned</span><b id="sScan">0</b></div>
          <div class="stat"><span>Matches</span><b id="sMatch">0</b></div>
          <div class="stat"><span>Speed</span><b id="sRate">—</b></div>
          <div class="stat"><span>Time left</span><b id="sEta">—</b></div>
        </div>
        <div id="log"></div><div id="result"></div>
      </section>
    </aside>
  </div>

  <section class="card library" aria-labelledby="libraryTitle">
    <div class="library-head"><div><h2 id="libraryTitle">Your seed pools</h2>
      <p>Completed pools appear in Brainstorm automatically. Share a pool by copying its single <code>.bspool</code> file.</p></div></div>
    <div id="pools" class="pool-grid"><div class="empty-library">No seed pools yet.</div></div>
    <details class="advanced">
      <summary>Merge distributed pool parts</summary>
      <div class="advanced-body">
        <p class="hint">Select at least two completed pools above. Their criteria and ranges are checked before merging.</p>
        <div class="merge-panel"><div class="field"><label for="mergeName">Merged pool name</label>
          <input type="text" id="mergeName" value="merged-pool" size="22"></div>
          <button id="btnMerge" onclick="mergePools()">Merge selected parts</button></div>
        <div id="mergeError" role="alert"></div>
      </div>
    </details>
  </section>
</main>

<script>
let CAT = null, lastRunning = false, lastResultKey = "";
const $ = id => document.getElementById(id);

// Replace a <select>'s options only when they actually changed, and never
// while the user has it open/focused -- rewriting innerHTML on the 1s poll
// closes the dropdown mid-click and made these menus unusable.
function setOptions(sel, html, keepValue){
  if (document.activeElement === sel) return;
  if (sel.dataset.opts === html) return;
  const old = keepValue ? sel.value : null;
  sel.dataset.opts = html;
  sel.innerHTML = html;
  if (keepValue && [...sel.options].some(o=>o.value===old)) sel.value = old;
}

function esc(value){
  return String(value == null ? "" : value).replace(/[&<>"']/g, ch=>({
    "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"
  })[ch]);
}
function fmt(n){ return Number(n).toLocaleString(); }
function fmtBytes(n){ const u=["B","KB","MB","GB","TB"]; let i=0;
  while(n>=1024 && i<u.length-1){ n/=1024; i++; } return n.toFixed(1)+" "+u[i]; }
function fmtSecs(s){ s=Math.round(s);
  if(s<90) return s+"s";
  if(s<5400) return Math.floor(s/60)+"m "+(s%60)+"s";
  if(s<172800) return Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m";
  return (s/86400).toFixed(1)+" days"; }

function addRule(key){
  if (!CAT || !CAT.tags.length) return;
  const div = document.createElement("div");
  div.className = "rule";
  const opts = CAT.tags.map(t=>`<option value="${esc(t.key)}">${esc(t.name)}</option>`).join("");
  div.innerHTML = `<div class="rule-field"><span class="label">Tag</span><select class="rkey">${opts}</select></div>
    <div class="rule-field"><span class="label">Minimum</span><input type="number" class="rcount" min="1" max="16" value="1"></div>
    <div class="rule-field"><span class="label">From ante</span><input type="number" class="rmin" min="1" max="39" value="1"></div>
    <div class="rule-field"><span class="label">Through ante</span><input type="number" class="rmax" min="1" max="39" value="8"></div>
    <button type="button" class="mini" onclick="removeRule(this)">Remove</button>`;
  if (key) div.querySelector(".rkey").value = key;
  const empty = $("rules").querySelector(".empty-rules");
  if (empty) empty.remove();
  $("rules").appendChild(div);
  updateSummary();
}

function removeRule(button){
  button.closest(".rule").remove();
  if (!$("rules").querySelector(".rule"))
    $("rules").innerHTML = '<div class="empty-rules">No tags required yet.</div>';
  updateSummary();
}

function criteria(){
  const rules = [...document.querySelectorAll("#rules .rule")].map(r=>({
    key: r.querySelector(".rkey").value,
    count: +r.querySelector(".rcount").value,
    min: +r.querySelector(".rmin").value,
    max: +r.querySelector(".rmax").value }));
  return { legendary: $("legendary").value,
    legMin:+$("legMin").value, legMax:+$("legMax").value,
    legNeg:$("legNeg").checked, legSoulDepth:$("legDepth").value,
    rules, route:$("route").value,
    threads:+$("threads").value, count:+$("count").value,
	space:$("space").value, inputPool:$("inputPool").value,
	shardTotal:+$("shardTotal").value, shardIndex:+$("shardIndex").value,
	name:$("name").value };
}

function updateSpaceHint(){
  const total = $("space").value === "total";
  $("optAll").textContent = total
    ? "Entire typeable space (2.90 trillion — can take days)"
    : "Entire seed space (1.79 trillion — can take days)";
  $("spaceHint").textContent = total
    ? "Seeds with 0/O or under 8 characters never occur naturally — they only exist typed in."
    : "";
}

function selectedText(id){
  const el = $(id);
  return el.selectedOptions.length ? el.selectedOptions[0].textContent : "";
}

function updateSummary(){
  if (!CAT) return;
  const c = criteria();
  const pieces = [];
  if (c.legendary) {
    let legendary = selectedText("legendary");
    if (c.legNeg) legendary = "Negative " + legendary;
    pieces.push(legendary + (c.legSoulDepth === "any" ? " in either of 2 Souls" : " in the first Soul"));
  }
  for (const r of c.rules) {
    const tag = CAT.tags.find(t=>t.key === r.key);
    pieces.push(`${r.count}× ${(tag && tag.name) || r.key} · antes ${r.min}–${r.max}`);
  }
  $("sumFilter").textContent = pieces.length ? pieces.join(" + ") : "Choose a Legendary or tag";
  $("readyPill").textContent = pieces.length ? "Ready" : "Needs filter";
  $("readyPill").style.background = pieces.length ? "#173824" : "#382f16";
  $("readyPill").style.color = pieces.length ? "#86e7aa" : "#f4d46b";

  const fromPool = !!c.inputPool;
  $("sumSource").textContent = fromPool ? c.inputPool
    : (c.space === "total" ? "All typeable seeds" : "Balatro's natural seeds");
  $("sumScope").textContent = fromPool ? "Entire input pool" : selectedText("count").replace(" · quick test", "");
  if (c.shardTotal > 1) $("sumScope").textContent += ` · part ${c.shardIndex} of ${c.shardTotal}`;
  $("sumThreads").textContent = c.threads ? `${c.threads} threads` : "Automatic threads";
  $("sumOutput").textContent = c.name.trim() || "Automatic name";
}

function updateShard(){
  const total = +$("shardTotal").value || 1;
  const old = Math.min(+$("shardIndex").value || 1, total);
  setOptions($("shardIndex"), Array.from({length:total}, (_,i)=>
    `<option value="${i+1}">${i+1}</option>`).join(""), false);
  if ($("shardIndex").options.length === total) $("shardIndex").value = old;
  const full = $("space").value === "total" ? 2901713047668 : 1785793904896;
  const selected = +$("count").value || full;
  const limit = Math.min(selected, full), index = +$("shardIndex").value;
  const start = Math.floor(limit*(index-1)/total), end = Math.floor(limit*index/total);
  $("shardHint").textContent = total === 1 ? ""
    : `exact ranks ${fmt(start)}–${fmt(end-1)} (${fmt(end-start)} seeds)`;
}

async function run(kind){
  $("error").textContent = "";
  $("result").textContent = "";
  try {
    const r = await fetch("/api/run", {method:"POST",
      body: JSON.stringify({kind, criteria: criteria()})});
    const j = await r.json();
    if (j.error) $("error").textContent = j.error;
    else tick();
  } catch (e) { $("error").textContent = "Could not reach the local builder. Reopen it and try again."; }
}
async function stopJob(){ await fetch("/api/stop", {method:"POST"}); }
async function mergePools(){
  $("mergeError").textContent = "";
  const pools = [...document.querySelectorAll(".mergePick:checked")].map(x=>x.value);
  const r = await fetch("/api/merge", {method:"POST", body:JSON.stringify({
    pools, name:$("mergeName").value})});
  const j = await r.json();
  if (j.error) $("mergeError").textContent = j.error;
}

function showResult(j){
  const m = j.manifest || {};
  let out = "";
  if (j.rc === 130) {
    out = "Paused at a checkpoint. Press Build pool again (same name) to resume.";
  } else if (j.rc !== 0) {
    out = "The scanner reported a problem:\\n" + (j.lines||[]).join("\\n");
  } else if (j.kind === "estimate") {
    const scanned = +m.scanned || 1, matched = +m.matched || 0;
    out = `Sample: ${fmt(matched)} matching seeds in ${fmt(scanned)} scanned `
        + `(${(100*matched/scanned).toFixed(5)}%).`;
    if (+m.projected_full_matches)
      out += `\\nFull seed space: ~${fmt(Math.round(+m.projected_full_matches))} seeds, `
          + `~${fmtBytes(+(m.projected_compressed_bytes || m.projected_u64_bytes))} compressed file.`;
    if (+m.seeds_per_second)
      out += `\\nFull scan at this speed: ~${fmtSecs((+m.seedspace || 1785793904896) / +m.seeds_per_second)}.`;
  } else if (j.kind === "merge") {
    out = `Done! ${fmt(j.matched||0)} seeds merged into seed_pools/${j.output}.\n`
        + `The source shard files were not changed.`;
  } else {
    out = `Done! ${fmt(+m.matched||j.matched)} seeds saved to seed_pools/${j.output}.\\n`
        + `It now shows up in the in-game Seed Pool selector. Share that one file `
        + `to share the pool.`;
  }
  $("result").textContent = out;
}

async function tick(){
  let j;
  try {
    const r = await fetch("/api/state");
    j = await r.json();
    $("serverStatus").textContent = "Running locally";
  } catch (e) {
    $("serverStatus").textContent = "Builder disconnected";
    return;
  }
  if (!CAT){
    CAT = j.catalog;
    $("legendary").innerHTML = `<option value="">(none)</option>` +
      CAT.legendaries.map(l=>`<option value="${esc(l.key)}">${esc(l.name)}</option>`).join("");
    if (CAT.legendaries.some(l=>l.key==="j_perkeo")) $("legendary").value="j_perkeo";
    const th = $("threads");
    for (let i=1;i<=CAT.cpus;i++) th.add(new Option(i,i));
  }
	setOptions($("inputPool"), `<option value="">Balatro's seed space</option>` +
	  j.pools.filter(p=>p.complete).map(p=>`<option value="${esc(p.name)}" data-space="${esc(p.space)}">${esc(p.name)} (${fmt(p.records)} seeds)</option>`).join(""), true);
	const fromPool = !!$("inputPool").value;
	if (fromPool) $("space").value = $("inputPool").selectedOptions[0].dataset.space || "natural";
	if (fromPool) $("shardTotal").value = "1";
	$("count").disabled = fromPool;
	$("space").disabled = fromPool;
	$("shardTotal").disabled = fromPool;
	$("shardIndex").disabled = fromPool;
	updateSpaceHint();
	updateShard();
  $("legRange").style.display = $("legendary").value ? "grid" : "none";
  updateSummary();
  const job = j.job;
  const running = !!job.running;
  $("btnEst").disabled = running; $("btnBuild").disabled = running;
  $("btnMerge").disabled = running;
  $("btnStop").disabled = !running;
  if (running || job.rc !== undefined){
    $("progressCard").style.display = "";
    $("progressState").textContent = running ? "Working" : (job.rc === 0 ? "Complete" : job.rc === 130 ? "Paused" : "Stopped");
    $("progTitle").textContent = (job.kind==="estimate"?"Estimating: ":job.kind==="merge"?"Merging: ":"Building: ")
      + (job.summary||"");
    const frac = job.total ? job.scanned/job.total : 0;
    $("fill").style.width = (100*frac).toFixed(1)+"%";
    $("progressPct").textContent = (100*frac).toFixed(frac && frac < .01 ? 2 : 1)+"%";
    $("sScan").textContent = fmt(job.scanned||0)+" / "+fmt(job.total||0);
    $("sMatch").textContent = fmt(job.matched||0);
    $("sRate").textContent = job.rate ? fmt(Math.round(job.rate))+"/s" : "-";
    $("sEta").textContent = (job.rate && job.total)
      ? fmtSecs((job.total-job.scanned)/job.rate) : "-";
    $("log").textContent = (job.lines||[]).join("\\n");
  }
  const resultKey = !running && job.rc !== undefined
    ? [job.kind, job.output, job.rc, job.scanned, job.matched].join(":") : "";
  if (resultKey && resultKey !== lastResultKey) {
    showResult(job);
    lastResultKey = resultKey;
  }
  lastRunning = running;
  const mergeSelected = new Set([...document.querySelectorAll(".mergePick:checked")].map(x=>x.value));
  const poolsHtml = j.pools.length ? j.pools.map(p=>{
    const statusText = p.complete ? "Complete" : (p.resumable ? "Paused · resumable" : "Partial");
    const statusClass = p.complete ? "ok" : "part";
    const idb = p.pool_id ? ` · id ${esc(p.pool_id.slice(0,8))}` : "";
    const sp = p.space === "total" ? " · all typeable" : "";
    const lbl = (p.label && (p.label + ".bspool") !== p.name)
      ? ` · “${esc(p.label)}”` : "";
    const src = p.refilter_depth ? ` · refilter ${p.refilter_depth}`
      + (p.source_pool_id && p.source_pool_id !== "-" ? ` from ${esc(p.source_pool_id.slice(0,8))}` : "") : "";
    const enc = p.encoding === "u64le" ? " · legacy format" : "";
    const merged = p.merged_parts ? ` · merged ${p.merged_parts} parts` : "";
    const range = p.range_end > p.range_start
      ? ` · ranks ${fmt(p.range_start)}–${fmt(p.range_end-1)}` : "";
    const pick = p.complete ? `<input aria-label="Select ${esc(p.name)} for merging" type="checkbox" class="mergePick" value="${esc(p.name)}" ${mergeSelected.has(p.name)?"checked":""}>` : "";
    const criteriaText = p.criteria.length ? p.criteria.map(esc).join(" · ") : "No embedded criteria";
    return `<article class="pool"><div class="pool-top"><div class="pool-name">${pick}<b>${esc(p.name)}</b></div>`
      + `<span class="status ${statusClass}">${statusText}</span></div>`
      + `<div class="pool-meta">${fmt(p.records)} seeds · ${fmtBytes(p.bytes)}${lbl}${idb}${sp}${src}${enc}${merged}</div>`
      + `<div class="pool-criteria">${criteriaText}${range}</div></article>`;
  }).join("") : '<div class="empty-library">No seed pools yet. Build one and it will appear here automatically.</div>';
  // Rebuild the list only when it changed: a 1s innerHTML rewrite eats the
  // click that is toggling a merge checkbox.
  if ($("pools").dataset.rendered !== poolsHtml){
    $("pools").dataset.rendered = poolsHtml;
    $("pools").innerHTML = poolsHtml;
  }
}
addEventListener("load", ()=>{
  $("space").addEventListener("change", ()=>{updateSpaceHint(); updateShard();});
  $("count").addEventListener("change", updateShard);
  $("shardTotal").addEventListener("change", updateShard);
  $("shardIndex").addEventListener("change", updateShard);
  document.addEventListener("input", updateSummary);
  document.addEventListener("change", ()=>{
    $("legRange").style.display = $("legendary").value ? "grid" : "none";
    updateSummary();
  });
  updateSpaceHint(); updateShard();
  tick(); setInterval(tick, 1000);
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    snap = None  # set at startup

    def log_message(self, *a):  # keep the terminal window calm
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            tags = [{"key": k, "name": core.TAG_NAMES.get(k, k), "minAnte": ma}
                    for k, ma in self.snap.usable_tags()]
            legendaries = [{"key": k, "name": core.joker_name(k)}
                           for k in self.snap.usable_legendaries()]
            self._json({
                "catalog": {"tags": tags, "legendaries": legendaries,
                            "cpus": os.cpu_count() or 8},
                "pools": list_pools(),
                "job": job_state(),
            })
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad request"}, 400)
        if self.path == "/api/run":
            try:
                start_job(data.get("kind", "build"), data.get("criteria", {}), self.snap)
                self._json({"ok": True})
            except (ValueError, OSError) as e:
                self._json({"error": str(e)}, 200)
        elif self.path == "/api/merge":
            try:
                start_merge_job(data)
                self._json({"ok": True})
            except (ValueError, OSError) as e:
                self._json({"error": str(e)}, 200)
        elif self.path == "/api/stop":
            r = JOB["runner"]
            if r is not None:
                r.stop()
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def main():
    problems = core.preflight()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print("\n(Fix the above, then run this again.)", file=sys.stderr)
        input("Press Return to close...")
        return 1
    Handler.snap = core.Snapshot(core.SNAPSHOT)
    port = DEFAULT_PORT
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    print("Brainstorm Seed Pool Builder is running at %s" % url)
    print("Leave this window open while a scan runs. Closing it pauses the scan")
    print("at a checkpoint; pressing Build again later resumes it. Ctrl+C quits.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        r = JOB["runner"]
        if r is not None and not r.done():
            print("\nPausing the scan at a checkpoint...")
            r.stop()
            for _ in range(300):
                if r.done():
                    break
                time.sleep(0.1)
        print("Bye.")
    return 0


if __name__ == "__main__":
    try:
        socket.setdefaulttimeout(30)
        sys.exit(main())
    except Exception as e:  # keep the double-click Terminal window readable
        print("Unexpected error: %s" % e, file=sys.stderr)
        input("Press Return to close...")
        sys.exit(1)
