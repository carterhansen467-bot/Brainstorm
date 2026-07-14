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
    c.leg_second_soul = bool(data.get("legSecondSoul"))
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
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin:0; padding:24px; background:#14161f; color:#e8e6f0;
  font:15px/1.5 -apple-system, "Segoe UI", sans-serif; }
h1 { font-size:22px; margin:0 0 2px; color:#ffd54d; }
.sub { color:#9a97ad; margin-bottom:20px; font-size:13px; }
.card { background:#1d2030; border:1px solid #2c3048; border-radius:12px;
  padding:18px 20px; margin-bottom:16px; max-width:860px; }
.card h2 { font-size:15px; margin:0 0 12px; color:#8fd0ff; }
label { color:#b9b6cc; font-size:13px; }
select, input[type=number], input[type=text] { background:#12141d;
  color:#e8e6f0; border:1px solid #3a3f5c; border-radius:7px;
  padding:6px 8px; font-size:14px; }
input[type=number] { width:70px; }
.row { display:flex; flex-wrap:wrap; gap:10px 14px; align-items:center;
  margin-bottom:10px; }
button { background:#3a5ccc; color:#fff; border:0; border-radius:8px;
  padding:9px 16px; font-size:14px; font-weight:600; cursor:pointer; }
button:hover { filter:brightness(1.15); }
button:disabled { background:#2a2d40; color:#777; cursor:default; }
button.warn { background:#a33d3d; }
button.go { background:#2c8a4b; }
button.mini { padding:3px 10px; font-size:12px; background:#44486a; }
.rule { display:flex; gap:8px; align-items:center; margin-bottom:8px;
  flex-wrap:wrap; }
#bar { height:18px; background:#12141d; border-radius:9px; overflow:hidden;
  border:1px solid #3a3f5c; }
#fill { height:100%; width:0%; background:linear-gradient(90deg,#2c8a4b,#57d47c);
  transition:width .5s; }
.stats { display:flex; gap:26px; flex-wrap:wrap; margin-top:10px;
  font-size:14px; }
.stats b { color:#ffd54d; font-weight:600; }
#log { font:12px/1.5 ui-monospace, monospace; color:#8a879e;
  white-space:pre-wrap; margin-top:10px; max-height:120px; overflow:auto; }
#result { color:#c6f2d2; white-space:pre-wrap; }
#error { color:#ff9d9d; margin-top:8px; white-space:pre-wrap; }
.pool { padding:8px 0; border-top:1px solid #2c3048; font-size:13px; }
.pool b { color:#e8e6f0; }
.tagc { color:#9a97ad; }
.ok { color:#57d47c; } .part { color:#ffd54d; }
</style></head><body>
<h1>Brainstorm Seed Pool Builder</h1>
<div class="sub">Builds .bspool seed pools for the Brainstorm mod's Seed Pool
selector. Runs only on this computer; safe to close any time &mdash; a running
scan pauses at a checkpoint and the Build button resumes it.</div>

<div class="card"><h2>1 &middot; What must every seed have?</h2>
  <div class="row">
    <label>Legendary</label>
    <select id="legendary"></select>
    <span id="legRange">
      <label>between ante</label> <input type="number" id="legMin" min="1" max="39" value="1">
      <label>and</label> <input type="number" id="legMax" min="1" max="39" value="8">
      <label><input type="checkbox" id="legNeg"> Negative edition</label>
      <label title="Exclusive: seeds where Soul #1 is already this Legendary are rejected.">
        <input type="checkbox" id="legSecondSoul"> Second Soul only</label>
      <span class="tagc">Exclusive: use Soul #1 and keep its different Legendary.</span>
    </span>
  </div>
  <div id="rules"></div>
  <div class="row"><button class="mini" onclick="addRule()">+ add a tag requirement</button></div>
  <div class="row">
    <label>If a required tag appears:</label>
    <select id="route">
      <option value="collect">skip that blind to take it (recommended)</option>
      <option value="observe">play the blind anyway</option>
    </select>
  </div>
</div>

<div class="card"><h2>2 &middot; How much to scan</h2>
  <div class="row">
	<label>Input seeds</label>
	<select id="inputPool"><option value="">Balatro's seed space</option></select>
	</div>
	<div class="row">
    <label>Seed space</label>
    <select id="space">
      <option value="natural">Natural seeds &mdash; 1.79 trillion the game can deal</option>
      <option value="total">All typeable seeds &mdash; 2.90 trillion, adds 0/O and short seeds</option>
    </select>
    <span class="tagc" id="spaceHint"></span>
  </div>
  <div class="row">
    <label>Scope</label>
    <select id="count">
      <option value="0" id="optAll">Entire seed space (can take days)</option>
      <option value="10000000000">First 10 billion seeds</option>
      <option value="1000000000">First 1 billion seeds</option>
      <option value="100000000" selected>First 100 million seeds (quick test)</option>
    </select>
    <label>Threads</label>
    <select id="threads"><option value="0">Auto</option></select>
    <label>Pool name</label>
    <input type="text" id="name" placeholder="(automatic)" size="22">
  </div>
  <div class="row">
    <label>Split across computers</label>
    <select id="shardTotal">
      <option value="1">Do not split</option>
      <option value="2">2 parts</option><option value="4">4 parts</option>
      <option value="8">8 parts</option><option value="16">16 parts</option>
      <option value="32">32 parts</option><option value="64">64 parts</option>
      <option value="128">128 parts</option><option value="256">256 parts</option>
    </select>
    <label>This computer runs part</label><select id="shardIndex"><option value="1">1</option></select>
    <span class="tagc" id="shardHint"></span>
  </div>
  <div class="row">
    <button onclick="run('estimate')" id="btnEst">Estimate size &amp; time (fast)</button>
    <button class="go" onclick="run('build')" id="btnBuild">Build pool</button>
    <button class="warn" onclick="stopJob()" id="btnStop" disabled>Pause scan</button>
  </div>
  <div id="error"></div>
</div>

<div class="card" id="progressCard" style="display:none"><h2 id="progTitle">Progress</h2>
  <div id="bar"><div id="fill"></div></div>
  <div class="stats">
    <span>Scanned <b id="sScan">0</b></span>
    <span>Matches <b id="sMatch">0</b></span>
    <span>Speed <b id="sRate">-</b></span>
    <span>Time left <b id="sEta">-</b></span>
  </div>
  <div id="log"></div>
  <div id="result"></div>
</div>

<div class="card"><h2>3 &middot; Merge distributed parts</h2>
  <div class="row">
    <span class="tagc">Check two or more completed pools below. Their criteria and ranges are validated before merging.</span>
  </div>
  <div class="row">
    <label>Merged pool name</label><input type="text" id="mergeName" value="merged-pool" size="22">
    <button id="btnMerge" onclick="mergePools()">Merge selected parts</button>
  </div>
  <div id="mergeError" class="part"></div>
</div>

<div class="card"><h2>Your seed pools <span class="tagc">(drop the .bspool file
into a friend's Mods/Brainstorm/seed_pools/ folder to share it)</span></h2>
  <div id="pools" class="tagc">none yet</div>
</div>

<script>
let CAT = null, lastRunning = false;
const $ = id => document.getElementById(id);

function fmt(n){ return Number(n).toLocaleString(); }
function fmtBytes(n){ const u=["B","KB","MB","GB","TB"]; let i=0;
  while(n>=1024 && i<u.length-1){ n/=1024; i++; } return n.toFixed(1)+" "+u[i]; }
function fmtSecs(s){ s=Math.round(s);
  if(s<90) return s+"s";
  if(s<5400) return Math.floor(s/60)+"m "+(s%60)+"s";
  if(s<172800) return Math.floor(s/3600)+"h "+Math.floor(s%3600/60)+"m";
  return (s/86400).toFixed(1)+" days"; }

function addRule(key){
  const div = document.createElement("div");
  div.className = "rule";
  const opts = CAT.tags.map(t=>`<option value="${t.key}">${t.name}</option>`).join("");
  div.innerHTML = `<select class="rkey">${opts}</select>
    <label>at least</label><input type="number" class="rcount" min="1" max="16" value="1">
    <label>time(s), antes</label><input type="number" class="rmin" min="1" max="39" value="1">
    <label>to</label><input type="number" class="rmax" min="1" max="39" value="8">
    <button class="mini" onclick="this.parentNode.remove()">remove</button>`;
  if (key) div.querySelector(".rkey").value = key;
  $("rules").appendChild(div);
}

function criteria(){
  const rules = [...document.querySelectorAll("#rules .rule")].map(r=>({
    key: r.querySelector(".rkey").value,
    count: +r.querySelector(".rcount").value,
    min: +r.querySelector(".rmin").value,
    max: +r.querySelector(".rmax").value }));
  return { legendary: $("legendary").value,
    legMin:+$("legMin").value, legMax:+$("legMax").value,
    legNeg:$("legNeg").checked, legSecondSoul:$("legSecondSoul").checked,
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

function updateShard(){
  const total = +$("shardTotal").value || 1;
  const old = Math.min(+$("shardIndex").value || 1, total);
  $("shardIndex").innerHTML = Array.from({length:total}, (_,i)=>
    `<option value="${i+1}">${i+1}</option>`).join("");
  $("shardIndex").value = old;
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
  const r = await fetch("/api/run", {method:"POST",
    body: JSON.stringify({kind, criteria: criteria()})});
  const j = await r.json();
  if (j.error) $("error").textContent = j.error;
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
  const r = await fetch("/api/state");
  const j = await r.json();
  if (!CAT){
    CAT = j.catalog;
    $("legendary").innerHTML = `<option value="">(none)</option>` +
      CAT.legendaries.map(l=>`<option value="${l.key}">${l.name}</option>`).join("");
    if (CAT.legendaries.some(l=>l.key==="j_perkeo")) $("legendary").value="j_perkeo";
    const th = $("threads");
    for (let i=1;i<=CAT.cpus;i++) th.add(new Option(i,i));
  }
	const selectedPool = $("inputPool").value;
	$("inputPool").innerHTML = `<option value="">Balatro's seed space</option>` +
	  j.pools.filter(p=>p.complete).map(p=>`<option value="${p.name}" data-space="${p.space}">${p.name} (${fmt(p.records)} seeds)</option>`).join("");
	if ([...$("inputPool").options].some(o=>o.value===selectedPool)) $("inputPool").value=selectedPool;
	const fromPool = !!$("inputPool").value;
	if (fromPool) $("space").value = $("inputPool").selectedOptions[0].dataset.space || "natural";
	if (fromPool) $("shardTotal").value = "1";
	$("count").disabled = fromPool;
	$("space").disabled = fromPool;
	$("shardTotal").disabled = fromPool;
	$("shardIndex").disabled = fromPool;
	updateSpaceHint();
	updateShard();
  $("legRange").style.display = $("legendary").value ? "" : "none";
  const job = j.job;
  const running = !!job.running;
  $("btnEst").disabled = running; $("btnBuild").disabled = running;
  $("btnMerge").disabled = running;
  $("btnStop").disabled = !running;
  if (running || job.rc !== undefined){
    $("progressCard").style.display = "";
    $("progTitle").textContent = (job.kind==="estimate"?"Estimating: ":job.kind==="merge"?"Merging: ":"Building: ")
      + (job.summary||"");
    const frac = job.total ? job.scanned/job.total : 0;
    $("fill").style.width = (100*frac).toFixed(1)+"%";
    $("sScan").textContent = fmt(job.scanned||0)+" / "+fmt(job.total||0);
    $("sMatch").textContent = fmt(job.matched||0);
    $("sRate").textContent = job.rate ? fmt(Math.round(job.rate))+"/s" : "-";
    $("sEta").textContent = (job.rate && job.total)
      ? fmtSecs((job.total-job.scanned)/job.rate) : "-";
    $("log").textContent = (job.lines||[]).join("\\n");
  }
  if (lastRunning && !running && job.rc !== undefined) showResult(job);
  lastRunning = running;
  const mergeSelected = new Set([...document.querySelectorAll(".mergePick:checked")].map(x=>x.value));
  $("pools").innerHTML = j.pools.length ? j.pools.map(p=>{
    const st = p.complete ? '<span class="ok">complete</span>'
      : (p.resumable ? '<span class="part">paused &mdash; resumable</span>'
                     : '<span class="part">partial</span>');
    const idb = p.pool_id ? ` <span class="tagc">id ${p.pool_id.slice(0,8)}</span>` : "";
    const sp = p.space === "total" ? ' <span class="part">all-typeable</span>' : "";
    const lbl = (p.label && (p.label + ".bspool") !== p.name)
      ? ` <span class="tagc">&ldquo;${p.label}&rdquo;</span>` : "";
    const src = p.refilter_depth ? ` <span class="tagc">refilter ${p.refilter_depth}`
      + (p.source_pool_id && p.source_pool_id !== "-" ? ` from ${p.source_pool_id.slice(0,8)}` : "")
      + `</span>` : "";
    const enc = p.encoding === "u64le" ? ` <span class="part">legacy format</span>` : "";
    const merged = p.merged_parts ? ` <span class="tagc">merged ${p.merged_parts} parts</span>` : "";
    const range = p.range_end > p.range_start
      ? ` <span class="tagc">ranks ${fmt(p.range_start)}–${fmt(p.range_end-1)}</span>` : "";
    const pick = p.complete ? `<input type="checkbox" class="mergePick" value="${p.name}" ${mergeSelected.has(p.name)?"checked":""}> ` : "";
    return `<div class="pool">${pick}<b>${p.name}</b>${lbl}${idb}${sp}${src}${enc}${merged} &mdash; `
      + `${fmt(p.records)} seeds, ${fmtBytes(p.bytes)} &mdash; ${st}<br>`
      + `<span class="tagc">${p.criteria.join(" &middot; ")}${range}</span></div>`;
  }).join("") : "none yet";
}
addEventListener("load", ()=>{
  $("space").addEventListener("change", ()=>{updateSpaceHint(); updateShard();});
  $("count").addEventListener("change", updateShard);
  $("shardTotal").addEventListener("change", updateShard);
  $("shardIndex").addEventListener("change", updateShard);
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
