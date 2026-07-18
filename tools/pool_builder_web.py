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
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brainstorm_pool_builder as core
import pool_organizer_web as organizer_web

DEFAULT_PORT = 8917
LOCK = threading.Lock()
JOB = {"runner": None, "kind": None, "started": 0.0, "summary": "", "error": "",
       "closing": False}


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
    c.leg_min_phase = str(data.get("legMinPhase", "small"))
    c.leg_max_phase = str(data.get("legMaxPhase", "big"))
    if c.leg_min_phase not in core.PHASES or c.leg_max_phase not in core.PHASES:
        raise ValueError("Unknown Legendary route point.")
    if core.route_position(c.leg_min, c.leg_min_phase) > core.route_position(
            c.leg_max, c.leg_max_phase):
        raise ValueError("Legendary route start must come before its route end.")
    if c.leg_max == core.MAX_ANTE and c.leg_max_phase == "boss":
        raise ValueError("The final supported Ante cannot end at Boss because its shop uses the next RNG Ante.")
    c.leg_source = str(data.get("legSource", "any"))
    if c.leg_source not in core.LEGENDARY_SOURCES:
        raise ValueError("Unknown Legendary pack source.")
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
        lo = clamp_int(rule.get("min", 1), 1, core.MAX_VOUCHER_ANTE)
        hi = clamp_int(rule.get("max", 8), lo, core.MAX_VOUCHER_ANTE)
        min_phase = str(rule.get("minPhase", "small"))
        max_phase = str(rule.get("maxPhase", "big"))
        if min_phase not in core.TAG_PHASES or max_phase not in core.TAG_PHASES:
            raise ValueError("Unknown tag blind location.")
        if core.route_position(lo, min_phase) > core.route_position(hi, max_phase):
            raise ValueError("%s route start must come before its route end."
                             % core.TAG_NAMES.get(key, key))
        if min_ante.get(key, 0) > hi:
            raise ValueError("%s cannot appear before ante %d"
                             % (core.TAG_NAMES.get(key, key), min_ante[key]))
        cnt = clamp_int(rule.get("count", 1), 1,
                        core.tag_location_count(lo, min_phase, hi, max_phase))
        c.tag_rules.append([key, lo, hi, cnt, min_phase, max_phase])
    usable_vouchers = snap.usable_vouchers() \
        if hasattr(snap, "usable_vouchers") else []
    voucher_keys = {key for key, _prerequisite in usable_vouchers}
    raw_voucher_rules = data.get("voucherRules", [])
    if len(raw_voucher_rules) > core.MAX_VOUCHER_RULES:
        raise ValueError("At most %d voucher targets are supported."
                         % core.MAX_VOUCHER_RULES)
    for rule in raw_voucher_rules:
        key = str(rule.get("key", ""))
        if key not in voucher_keys:
            raise ValueError("Unknown or unavailable voucher %r" % key)
        lo = clamp_int(rule.get("min", 1), 1, core.MAX_ANTE)
        hi = clamp_int(rule.get("max", 8), lo, core.MAX_ANTE)
        c.voucher_rules.append([key, lo, hi])
    raw_exclusions = data.get("voucherExclusions", [])
    if len(raw_exclusions) > core.MAX_VOUCHER_EXCLUSIONS:
        raise ValueError("At most %d voucher purchase exclusions are supported."
                         % core.MAX_VOUCHER_EXCLUSIONS)
    for value in raw_exclusions:
        key = str(value.get("key", "") if isinstance(value, dict) else value)
        if key not in voucher_keys:
            raise ValueError("Unknown or unavailable voucher exclusion %r" % key)
        if key in c.voucher_exclusions:
            raise ValueError("Each voucher purchase exclusion can only be added once.")
        c.voucher_exclusions.append(key)
    if c.voucher_exclusions and not c.voucher_rules:
        raise ValueError("A voucher purchase exclusion requires at least one voucher target.")
    if c.voucher_rules and data.get("route", "collect") != "observe" \
            and any(rule[0] in ("tag_voucher", "tag_double")
                    for rule in c.tag_rules):
        raise ValueError("Collected Voucher and Double Tags are not yet supported "
                         "with voucher targets; observe that tag or remove it.")
    if not c.predicates():
        raise ValueError("Pick a Legendary, add a tag requirement, or add a voucher target.")
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
    return core.read_pool_header(path)


def list_pools():
    return core.read_pool_library(core.POOL_DIR)[0]


def list_pool_groups():
    return core.read_pool_library(core.POOL_DIR)[1]


def pool_library():
    """Read once so flat and grouped API views describe the same snapshot."""
    return core.read_pool_library(core.POOL_DIR)


# ------------------------------------------------------------------ job ----

def job_state():
    r = JOB["runner"]
    out = {"running": False, "kind": JOB["kind"], "summary": JOB["summary"],
           "error": JOB["error"], "closing": JOB["closing"]}
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
        if JOB["closing"]:
            raise ValueError("The Builder is closing; reopen it to start another job.")
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
            if int(head.get("records", "0") or 0) <= 0:
                raise ValueError("The selected pool has no committed seeds to process yet.")
            input_pool = candidate
            crit.space = head.get("space", "natural")
            if crit.shard_total > 1:
                raise ValueError("Distributed parts apply to Balatro's seed space, not an input pool.")
        if kind == "estimate":
            sample = clamp_int(data.get("sample", core.ESTIMATE_COUNT),
                               100_000, SEEDCAP)
            out = os.path.join(tempfile.mkdtemp(prefix="bs_pool_est_"), "estimate")
            text = crit.text("count", sample, apply_shard=False,
                             checkpoint=min(core.ESTIMATE_CHECKPOINT, sample))
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
        summary = crit.summary()
        if kind == "estimate" and not input_pool:
            summary += " [%s-seed quick sample]" % format(sample, ",")
        JOB.update(runner=core.Runner(snap.current_model_copy(), text, out, input_pool),
                   kind=kind, started=time.time(), summary=summary, error="")


def start_merge_job(data):
    with LOCK:
        if JOB["closing"]:
            raise ValueError("The Builder is closing; reopen it to start another job.")
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


def shutdown_when_safe(server):
    """Pause an active child at its next checkpoint, then stop the web server."""
    time.sleep(0.2)  # let the POST response reach the browser first
    r = JOB["runner"]
    if r is not None and not r.done():
        r.stop()
        while not r.done():
            time.sleep(0.1)
    server.shutdown()


def begin_shutdown(server):
    with LOCK:
        if JOB["closing"]:
            return
        JOB["closing"] = True
    threading.Thread(target=shutdown_when_safe, args=(server,), daemon=True).start()


SEEDCAP = core.SEEDSPACE_TOTAL  # UI clamp only; the scanner enforces per-space bounds


# ------------------------------------------------------------------ http ---

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<title>Brainstorm Seed Pool Tools</title>
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
.server-tools { display:flex; gap:9px; align-items:center; }
.appnav { display:flex; gap:7px; width:max-content; margin:-9px 0 20px; padding:5px;
  border:1px solid #34394d; border-radius:12px; background:#10131c; }
.appnav a { padding:8px 13px; border-radius:8px; color:var(--muted); font-size:12px;
  font-weight:800; text-decoration:none; }
.appnav a.active { background:#29213b; color:#ded6ff; }
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
#legRange { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:12px;
  grid-column:1 / -1; padding:14px; border:1px solid var(--line-soft); border-radius:12px;
  background:#11141e; }
#rules { display:grid; gap:9px; }
.rule { display:grid; grid-template-columns:minmax(150px, 1.5fr) .55fr .55fr .65fr .55fr .65fr auto;
  gap:9px; align-items:end; padding:12px; border:1px solid var(--line-soft);
  border-radius:12px; background:#11141e; }
.voucher-list { display:grid; gap:9px; }
.voucher-rule { display:grid; grid-template-columns:minmax(190px, 1.5fr) .65fr .65fr auto;
  gap:9px; align-items:end; padding:12px; border:1px solid var(--line-soft);
  border-radius:12px; background:#11141e; }
.voucher-exclusion { display:grid; grid-template-columns:minmax(220px, 1fr) auto;
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
.pool-family { grid-column:1 / -1; display:grid; gap:12px; padding:14px;
  border:1px solid #2b3043; border-radius:15px; background:#0e1119; }
.pool-family-head { display:flex; justify-content:space-between; gap:12px; align-items:baseline; }
.pool-family-head h3 { margin:0; color:#e5dfed; font-size:14px; }
.pool-family-head span { color:var(--faint); font:10px/1.3 ui-monospace, SFMono-Regular, Menlo, monospace; }
.pool-lineage { display:grid; gap:8px; }
.pool-lineage + .pool-lineage { padding-top:12px; border-top:1px solid var(--line-soft); }
.pool-lineage-head { display:flex; flex-wrap:wrap; gap:7px; align-items:baseline; color:#bcb6ca;
  font-size:11px; font-weight:800; }
.pool-lineage-head span { color:var(--faint); font-weight:500; }
.pool-lineage-grid { display:grid; grid-template-columns:repeat(2, minmax(0,1fr)); gap:11px; }
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
.pool-relation { margin-top:8px; color:#aaa5b7; }
.pool-update { margin-top:9px; padding:8px 10px; border:1px solid #675829;
  border-radius:9px; background:#29230f; color:#f2d77a; }
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
  .appnav { width:100%; }
  .appnav a { flex:1; text-align:center; }
  .card { padding:17px; border-radius:15px; }
  .field-grid, .field-grid.three, #legRange { grid-template-columns:1fr; }
  .rule { grid-template-columns:1fr 1fr; }
  .rule .rule-field:first-child { grid-column:1 / -1; }
  .rule button { grid-column:1 / -1; }
  .voucher-rule, .voucher-exclusion { grid-template-columns:1fr 1fr; }
  .voucher-rule .rule-field:first-child, .voucher-exclusion .rule-field:first-child,
  .voucher-rule button, .voucher-exclusion button { grid-column:1 / -1; }
  .pool-grid, .pool-lineage-grid { grid-template-columns:1fr; }
  .merge-panel { grid-template-columns:1fr; }
  .library-head { display:block; }
}
</style></head><body>
<main class="app">
  <header class="topbar">
    <div class="brand">
      <div class="brand-mark" aria-hidden="true">B</div>
      <div>
        <h1>Seed Pool Tools</h1>
        <p class="sub">Build, search, organize, and combine reusable Brainstorm seed pools.</p>
      </div>
    </div>
    <div class="server-tools"><div class="local-pill" id="serverStatus">Running locally</div>
      <button type="button" class="mini ghost" id="btnClose" onclick="closeBuilder()">Close Builder</button></div>
  </header>

  <nav class="appnav"><a class="active" href="/">Build / Search</a><a href="/organize">Organize / Combine</a></nav>

  <div class="notice"><span aria-hidden="true">◆</span><div><strong>Your data stays here.</strong>
    This page only talks to the builder on this computer. Closing the browser tab leaves the local Builder running; use <strong>Close Builder</strong> when finished. An active scan will pause safely at its next checkpoint.</div></div>

  <div class="workspace">
    <div class="stack">
      <section class="card" aria-labelledby="filterTitle">
        <div class="card-head"><span class="step">1</span><div>
          <h2 id="filterTitle">Choose what seeds must contain</h2>
          <p class="card-copy">Use a Legendary, tags, vouchers, or combine them.</p>
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
            <div class="field"><label for="legMinPhase">From route point</label><select id="legMinPhase">
              <option value="small">Small Blind</option><option value="big">Big Blind</option><option value="boss">Boss Blind shop</option>
            </select></div>
            <div class="field"><label for="legMax">Through ante</label><input type="number" id="legMax" min="1" max="39" value="8"></div>
            <div class="field"><label for="legMaxPhase">Through route point</label><select id="legMaxPhase">
              <option value="small">Small Blind</option><option value="big" selected>Big Blind</option><option value="boss">Boss Blind shop</option>
            </select></div>
            <div class="field"><label for="legSource">Soul pack source</label><select id="legSource">
              <option value="any">Shop or collected tag</option><option value="shop">Shop packs only</option>
              <option value="charm">Charm Tag reward only</option><option value="ethereal">Ethereal Tag reward only</option>
            </select></div>
            <label class="check"><input type="checkbox" id="legNeg"> Require Negative</label>
          </div>
          <div class="hint full">Souls are checked chronologically across reachable shop packs and collected Charm or Ethereal rewards.</div>
        </div>

        <div class="section-label">Tag requirements</div>
        <div id="rules"><div class="empty-rules">No tags required yet.</div></div>
        <div class="button-row"><button type="button" class="mini" onclick="addRule()">＋ Add tag requirement</button></div>

        <div class="section-label">Voucher targets</div>
        <div id="voucherRules" class="voucher-list"><div class="empty-rules">No vouchers required yet.</div></div>
        <div class="button-row"><button type="button" class="mini" id="btnAddVoucher" onclick="addVoucherRule()">＋ Add voucher target</button></div>
        <div class="hint">The scanner finds the minimum-purchase route that reaches every target within its Ante window.</div>

        <details class="advanced">
          <summary>Voucher purchase exclusions</summary>
          <div class="advanced-body">
            <div id="voucherExclusions" class="voucher-list"><div class="empty-rules">No purchases excluded.</div></div>
            <div class="button-row"><button type="button" class="mini" id="btnAddVoucherExclusion" onclick="addVoucherExclusion()">＋ Add purchase exclusion</button></div>
            <div class="hint">An excluded voucher may still appear, but a matching route cannot depend on buying it. You can exclude the target itself to require finding it as an offer.</div>
          </div>
        </details>

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
            <span class="hint">Choose any pool with recorded seeds—including a paused pool—to filter its current snapshot.</span></div>
          <div class="field"><label for="space">Seed space</label>
            <select id="space">
              <option value="natural">Natural seeds · 1.79 trillion</option>
              <option value="settable">All vanilla-settable seeds · 2.32 trillion · no 0</option>
              <option value="total">All possible seeds · 2.90 trillion · includes 0</option>
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
          <div class="summary-item"><dt>Looking for</dt><dd id="sumFilter">Choose a Legendary, tag, or voucher</dd></div>
          <div class="summary-item"><dt>Source</dt><dd id="sumSource">Balatro's natural seeds</dd></div>
          <div class="summary-item"><dt>Scope</dt><dd id="sumScope">First 100 million</dd></div>
          <div class="summary-item"><dt>Compute</dt><dd id="sumThreads">Automatic threads</dd></div>
          <div class="summary-item"><dt>Output</dt><dd id="sumOutput">Automatic name</dd></div>
        </dl>
        <div class="primary-actions">
          <button class="go" onclick="run('build')" id="btnBuild">Build seed pool</button>
          <button class="ghost" onclick="run('estimate')" id="btnEst">Quick estimate (2M sample)</button>
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
    <div class="rule-field"><span class="label">From blind</span><select class="rminphase"><option value="small">Small</option><option value="big">Big</option></select></div>
    <div class="rule-field"><span class="label">Through ante</span><input type="number" class="rmax" min="1" max="39" value="8"></div>
    <div class="rule-field"><span class="label">Through blind</span><select class="rmaxphase"><option value="small">Small</option><option value="big" selected>Big</option></select></div>
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

function voucherOptions(){
  return (CAT.vouchers || []).map(v=>{
    const prerequisite = v.prerequisiteName ? ` · requires ${v.prerequisiteName}` : "";
    return `<option value="${esc(v.key)}">${esc(v.name + prerequisite)}</option>`;
  }).join("");
}

function refreshVoucherButtons(){
  if (!CAT) return;
  const targets = document.querySelectorAll("#voucherRules .voucher-rule").length;
  const exclusions = document.querySelectorAll("#voucherExclusions .voucher-exclusion").length;
  $("btnAddVoucher").disabled = !(CAT.vouchers || []).length || targets >= 8;
  $("btnAddVoucherExclusion").disabled = !targets || !(CAT.vouchers || []).length
    || exclusions >= Math.min(16, CAT.vouchers.length);
}

function addVoucherRule(key){
  if (!CAT || !(CAT.vouchers || []).length) return;
  if (document.querySelectorAll("#voucherRules .voucher-rule").length >= 8) return;
  const div = document.createElement("div");
  div.className = "voucher-rule";
  div.innerHTML = `<div class="rule-field"><span class="label">Voucher</span><select class="vkey">${voucherOptions()}</select></div>
    <div class="rule-field"><span class="label">From ante</span><input type="number" class="vmin" min="1" max="8" value="1"></div>
    <div class="rule-field"><span class="label">Through ante</span><input type="number" class="vmax" min="1" max="8" value="8"></div>
    <button type="button" class="mini" onclick="removeVoucherRule(this)">Remove</button>`;
  if (key) div.querySelector(".vkey").value = key;
  const empty = $("voucherRules").querySelector(".empty-rules");
  if (empty) empty.remove();
  $("voucherRules").appendChild(div);
  refreshVoucherButtons();
  updateSummary();
}

function removeVoucherRule(button){
  button.closest(".voucher-rule").remove();
  if (!$("voucherRules").querySelector(".voucher-rule"))
    $("voucherRules").innerHTML = '<div class="empty-rules">No vouchers required yet.</div>';
  refreshVoucherButtons();
  updateSummary();
}

function addVoucherExclusion(key){
  if (!CAT || !(CAT.vouchers || []).length
      || !$("voucherRules").querySelector(".voucher-rule")) return;
  if (document.querySelectorAll("#voucherExclusions .voucher-exclusion").length >= 16) return;
  const used = new Set([...document.querySelectorAll("#voucherExclusions .vxkey")]
    .map(select=>select.value));
  const available = CAT.vouchers.find(v=>!used.has(v.key));
  const chosen = key || (available && available.key);
  if (!chosen) return;
  const div = document.createElement("div");
  div.className = "voucher-exclusion";
  div.innerHTML = `<div class="rule-field"><span class="label">Route cannot purchase</span><select class="vxkey">${voucherOptions()}</select></div>
    <button type="button" class="mini" onclick="removeVoucherExclusion(this)">Remove</button>`;
  div.querySelector(".vxkey").value = chosen;
  const empty = $("voucherExclusions").querySelector(".empty-rules");
  if (empty) empty.remove();
  $("voucherExclusions").appendChild(div);
  refreshVoucherButtons();
  updateSummary();
}

function removeVoucherExclusion(button){
  button.closest(".voucher-exclusion").remove();
  if (!$("voucherExclusions").querySelector(".voucher-exclusion"))
    $("voucherExclusions").innerHTML = '<div class="empty-rules">No purchases excluded.</div>';
  refreshVoucherButtons();
  updateSummary();
}

function criteria(){
  const rules = [...document.querySelectorAll("#rules .rule")].map(r=>({
    key: r.querySelector(".rkey").value,
    count: +r.querySelector(".rcount").value,
    min: +r.querySelector(".rmin").value,
    minPhase: r.querySelector(".rminphase").value,
    max: +r.querySelector(".rmax").value,
    maxPhase: r.querySelector(".rmaxphase").value }));
  const voucherRules = [...document.querySelectorAll("#voucherRules .voucher-rule")].map(r=>({
    key: r.querySelector(".vkey").value,
    min: +r.querySelector(".vmin").value,
    max: +r.querySelector(".vmax").value }));
  const voucherExclusions = [...document.querySelectorAll("#voucherExclusions .voucher-exclusion")]
    .map(r=>r.querySelector(".vxkey").value);
  return { legendary: $("legendary").value,
    legMin:+$("legMin").value, legMax:+$("legMax").value,
    legMinPhase:$("legMinPhase").value, legMaxPhase:$("legMaxPhase").value,
    legSource:$("legSource").value,
    legNeg:$("legNeg").checked, legSoulDepth:$("legDepth").value,
    rules, voucherRules, voucherExclusions, route:$("route").value,
    threads:+$("threads").value, count:+$("count").value,
	space:$("space").value, inputPool:$("inputPool").value,
	shardTotal:+$("shardTotal").value, shardIndex:+$("shardIndex").value,
	name:$("name").value };
}

function spaceInfo(space){
  if (space === "settable") return {
    size:2318107019760,
    name:"All vanilla-settable seeds",
    all:"Entire vanilla-settable space (2.32 trillion — can take days)",
    hint:"Includes O and seeds under 8 characters, but excludes 0 because vanilla changes 0 to O."
  };
  if (space === "total") return {
    size:2901713047668,
    name:"All possible seeds",
    all:"Entire possible seed space (2.90 trillion — can take days)",
    hint:"Includes seeds containing 0; those require Brainstorm's Illegal Seed Input support."
  };
  return {size:1785793904896, name:"Balatro's natural seeds",
    all:"Entire natural seed space (1.79 trillion — can take days)", hint:""};
}

function updateSpaceHint(){
  const info = spaceInfo($("space").value);
  $("optAll").textContent = info.all;
  const selectedPool = $("inputPool").selectedOptions[0];
  $("spaceHint").textContent = selectedPool && selectedPool.dataset.composite === "1"
    ? "This is a composite membership set. Its source-filter branches stay recorded for organizing, but a new search uses the selected seeds plus your current filters rather than stacking every source route."
    : info.hint;
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
    const source = c.legSource === "any" ? "" : ` · ${selectedText("legSource")}`;
    pieces.push(legendary + (c.legSoulDepth === "any" ? " in either of 2 Souls" : " in the first Soul")
      + ` · A${c.legMin} ${c.legMinPhase}–A${c.legMax} ${c.legMaxPhase}${source}`);
  }
  for (const r of c.rules) {
    const tag = CAT.tags.find(t=>t.key === r.key);
    pieces.push(`${r.count}× ${(tag && tag.name) || r.key} · A${r.min} ${r.minPhase}–A${r.max} ${r.maxPhase}`);
  }
  for (const r of c.voucherRules) {
    const voucher = (CAT.vouchers || []).find(v=>v.key === r.key);
    const window = r.min === r.max ? `A${r.min}` : `A${r.min}–A${r.max}`;
    pieces.push(`${(voucher && voucher.name) || r.key} voucher · ${window}`);
  }
  if (c.voucherExclusions.length) {
    const names = c.voucherExclusions.map(key=>{
      const voucher = (CAT.vouchers || []).find(v=>v.key === key);
      return (voucher && voucher.name) || key;
    });
    pieces.push(`without purchasing ${names.join(", ")}`);
  }
  $("sumFilter").textContent = pieces.length ? pieces.join(" + ") : "Choose a Legendary, tag, or voucher";
  $("readyPill").textContent = pieces.length ? "Ready" : "Needs filter";
  $("readyPill").style.background = pieces.length ? "#173824" : "#382f16";
  $("readyPill").style.color = pieces.length ? "#86e7aa" : "#f4d46b";

  const fromPool = !!c.inputPool;
  $("sumSource").textContent = fromPool ? c.inputPool
    : spaceInfo(c.space).name;
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
  const full = spaceInfo($("space").value).size;
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
async function closeBuilder(){
  if (lastRunning && !confirm("Pause the active job at its next checkpoint and close the Builder?")) return;
  $("btnClose").disabled = true;
  try {
    const r = await fetch("/api/shutdown", {method:"POST"});
    const j = await r.json();
    if (j.error) throw new Error(j.error);
    $("serverStatus").textContent = lastRunning ? "Pausing safely" : "Closing";
    $("result").textContent = lastRunning
      ? "Pausing at the next checkpoint. This page will disconnect when it is safe to close."
      : "Builder closed. You can close this browser tab.";
  } catch (e) {
    $("serverStatus").textContent = "Builder disconnected";
    $("result").textContent = "The local Builder is closed. You can close this browser tab.";
  }
}
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
    out = j.kind === "estimate"
      ? "Estimate canceled cleanly. No seed-pool file was changed."
      : "Paused at a checkpoint. Press Build pool again (same name) to resume.";
  } else if (j.rc !== 0) {
    out = "The scanner reported a problem:\\n" + (j.lines||[]).join("\\n");
  } else if (j.kind === "estimate") {
    const scanned = +m.scanned || 1, matched = +m.matched || 0;
    out = `Sample: ${fmt(matched)} matching seeds in ${fmt(scanned)} scanned `
        + `(${(100*matched/scanned).toFixed(5)}%).`;
    if (matched > 0 && matched < 25)
      out += `\\nOnly ${fmt(matched)} matches appeared, so the size projection is rough. `
          + `Use a larger test build if you need a precise file-size estimate.`;
    if (matched === 0)
      out += `\\nNo matches appeared in this quick sample. The filter may be very rare; `
          + `use a larger test build before concluding that no matching seeds exist.`;
    if (matched > 0 && +m.projected_full_matches)
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

function inputPoolOptions(groups){
  let html = `<option value="">Balatro's seed space</option>`;
  for (const family of (groups || [])) {
    for (const lineage of family.lineages) {
      const available = lineage.pools.filter(p=>p.records > 0);
      if (!available.length) continue;
      const groupLabel = family.legacy ? family.label
        : `${family.label} · ${lineage.label}`;
      html += `<optgroup label="${esc(groupLabel)}">` + available.map(p=>{
        const state = p.status === "complete" ? ""
          : p.status === "provisional" ? " · provisional snapshot"
          : p.resumable ? " · paused snapshot" : " · incomplete snapshot";
        const update = p.update_available ? ` · +${fmt(p.new_records)} source seeds` : "";
        const composite = p.composite
          ? ` · ${p.composite_operation || "composite"} · ${fmt(p.composite_operand_count || p.composite_branch_count)} inputs · ${fmt(p.composite_branch_count)} source filters` : "";
        return `<option value="${esc(p.name)}" data-space="${esc(p.space)}" data-composite="${p.composite?"1":"0"}">${esc(p.name)} (${fmt(p.records)} seeds${state}${update}${composite})</option>`;
      }).join("") + `</optgroup>`;
    }
  }
  return html;
}

function renderPoolCard(p, mergeSelected){
  const statusText = p.status_label || (!p.complete
    ? (p.resumable ? "Paused · resumable" : "Incomplete snapshot")
    : p.coverage_complete ? "Complete" : "Provisional · source snapshot");
  const statusClass = p.status === "complete" || (p.complete && p.coverage_complete)
    ? "ok" : "part";
  const idb = p.pool_id ? ` · id ${esc(p.pool_id.slice(0,8))}` : "";
  const sp = p.space === "settable" ? " · vanilla-settable (no 0)"
    : p.space === "total" ? " · all possible (includes 0)" : "";
  const lbl = (p.label && (p.label + ".bspool") !== p.name)
    ? ` · “${esc(p.label)}”` : "";
  const src = p.refilter_depth ? ` · refilter ${p.refilter_depth}`
    + (p.source_pool_id && p.source_pool_id !== "-" ? ` from ${esc(p.source_pool_id.slice(0,8))}` : "") : "";
  const enc = p.encoding === "u64le" ? " · legacy format" : "";
  const merged = p.merged_parts ? ` · merged ${p.merged_parts} parts` : "";
  const composite = p.composite
    ? ` · ${esc((p.composite_operation || "composite").toUpperCase())} of ${fmt(p.composite_operand_count || p.composite_branch_count)} inputs · ${fmt(p.composite_branch_count)} source filters` : "";
  const range = p.range_end > p.range_start
    ? ` · ranks ${fmt(p.range_start)}–${fmt(p.range_end-1)}` : "";
  const pick = p.complete && !p.composite
    ? `<input aria-label="Select ${esc(p.name)} for merging" type="checkbox" class="mergePick" value="${esc(p.name)}" ${mergeSelected.has(p.name)?"checked":""}>`
    : "";
  const criteriaText = p.composite
    ? "Composite membership set · source routes retained as per-seed provenance"
    : p.criteria.length ? p.criteria.map(esc).join(" · ") : "No embedded criteria";
  let relation = "";
  if (p.parent_name) {
    relation = `<div class="pool-relation">Derived from ${esc(p.parent_name)}`
      + (p.parent_records ? ` at ${fmt(p.parent_records)} recorded seeds` : "") + `.</div>`;
  } else if (p.parent_segment_id) {
    relation = `<div class="pool-relation">Parent segment ${esc(p.parent_segment_id.slice(0,8))} is not in this folder.</div>`;
  }
  const update = p.update_available
    ? `<div class="pool-update">Update available: the source now has ${fmt(p.new_records)} new recorded seeds. This pool still uses its pinned snapshot; automatic incremental updating is coming later.</div>`
    : "";
  return `<article class="pool"><div class="pool-top"><div class="pool-name">${pick}<b>${esc(p.name)}</b></div>`
    + `<span class="status ${statusClass}">${esc(statusText)}</span></div>`
    + `<div class="pool-meta">${fmt(p.records)} seeds · ${fmtBytes(p.bytes)}${lbl}${idb}${sp}${src}${enc}${merged}${composite}</div>`
    + `<div class="pool-criteria">${criteriaText}${range}</div>${relation}${update}</article>`;
}

function renderPoolLibrary(groups, pools, mergeSelected){
  if (!pools.length)
    return '<div class="empty-library">No seed pools yet. Build one and it will appear here automatically.</div>';
  return (groups || []).map(family=>{
    const familyCount = family.lineages.reduce((n, lineage)=>n + lineage.pools.length, 0);
    const familyId = family.family_id
      ? `<span>family ${esc(family.family_id.slice(0,8))}</span>` : `<span>no lineage metadata</span>`;
    const lineages = family.lineages.map(lineage=>{
      const lineageId = lineage.lineage_id
        ? `<span>lineage ${esc(lineage.lineage_id.slice(0,8))}</span>` : "";
      return `<section class="pool-lineage"><div class="pool-lineage-head">${esc(lineage.label)} · ${esc(lineage.display_name)} ${lineageId}</div>`
        + `<div class="pool-lineage-grid">${lineage.pools.map(p=>renderPoolCard(p, mergeSelected)).join("")}</div></section>`;
    }).join("");
    return `<section class="pool-family"><div class="pool-family-head"><h3>${esc(family.label)} (${familyCount})</h3>${familyId}</div>${lineages}</section>`;
  }).join("");
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
    refreshVoucherButtons();
  }
	setOptions($("inputPool"), inputPoolOptions(j.pool_groups || []), true);
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
  const closing = !!job.closing;
  $("btnEst").disabled = running || closing; $("btnBuild").disabled = running || closing;
  $("btnMerge").disabled = running || closing;
  $("btnStop").disabled = !running;
  $("btnStop").textContent = job.kind === "estimate" ? "Cancel estimate" : "Pause active job";
  $("btnClose").disabled = closing;
  if (running || job.rc !== undefined){
    $("progressCard").style.display = "";
    $("progressState").textContent = closing && running ? "Pausing safely"
      : running ? (job.scanned ? "Working" : "Starting")
      : (job.rc === 0 ? "Complete" : job.rc === 130 ? "Paused" : "Stopped");
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
  const poolsHtml = renderPoolLibrary(j.pool_groups || [], j.pools, mergeSelected);
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
    pool_dir = core.POOL_DIR

    def log_message(self, *a):  # keep the terminal window calm
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _page(self, value):
        body = value.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _organizer_export(self, parsed):
        query = parse_qs(parsed.query)
        name = query.get("source", [""])[0]
        reader = organizer_web.organizer.BSPoolReader(
            organizer_web.resolve_source(name, self.pool_dir))
        expected = query.get("snapshot", [""])[0].lower()
        if expected != reader.snapshot_token:
            raise organizer_web.organizer.PoolError(
                "source snapshot changed; inspect before exporting")
        filename = "%s-%s-records.ndjson" % (
            os.path.splitext(name)[0], reader.snapshot_token[:8])
        filename = re.sub(r"[^A-Za-z0-9._+-]+", "-", filename)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=%s" %
                         quote(filename))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        for line in organizer_web.iter_record_export(reader):
            self.wfile.write(line)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                self._page(PAGE)
            elif parsed.path in ("/organize", "/organize/"):
                self._page(organizer_web.PAGE)
            elif parsed.path == "/api/ping":
                self._json({"app": "brainstorm-seed-pool-tools", "version": 1})
            elif parsed.path == "/api/state":
                tags = [{"key": k, "name": core.TAG_NAMES.get(k, k), "minAnte": ma}
                        for k, ma in self.snap.usable_tags()]
                legendaries = [{"key": k, "name": core.joker_name(k)}
                               for k in self.snap.usable_legendaries()]
                vouchers = [{"key": key, "name": core.voucher_name(key),
                             "prerequisite": prerequisite,
                             "prerequisiteName": core.voucher_name(prerequisite)
                             if prerequisite else ""}
                            for key, prerequisite in self.snap.usable_vouchers()]
                pools, pool_groups = pool_library()
                self._json({
                    "catalog": {"tags": tags, "legendaries": legendaries,
                                "vouchers": vouchers, "cpus": os.cpu_count() or 8},
                    "pools": pools,
                    "pool_groups": pool_groups,
                    "job": job_state(),
                })
            elif parsed.path == "/organizer/api/pools":
                self._json({"pools": organizer_web.list_sources(self.pool_dir)})
            elif parsed.path == "/organizer/api/export":
                self._organizer_export(parsed)
            else:
                self._json({"error": "not found"}, 404)
        except (OSError, ValueError,
                organizer_web.organizer.PoolError) as exc:
            self._json({"error": str(exc)}, 400)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "invalid request length"}, 400)
        if n < 0 or n > organizer_web.MAX_REQUEST_BYTES:
            return self._json({"error": "request is too large"}, 400)
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json({"error": "bad request"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "request must be a JSON object"}, 400)
        if parsed.path == "/api/run":
            try:
                start_job(data.get("kind", "build"), data.get("criteria", {}), self.snap)
                self._json({"ok": True})
            except (ValueError, OSError) as e:
                self._json({"error": str(e)}, 200)
        elif parsed.path == "/api/merge":
            try:
                start_merge_job(data)
                self._json({"ok": True})
            except (ValueError, OSError) as e:
                self._json({"error": str(e)}, 200)
        elif parsed.path == "/api/stop":
            r = JOB["runner"]
            if r is not None:
                r.stop()
            self._json({"ok": True})
        elif parsed.path == "/api/shutdown":
            self._json({"ok": True, "pausing": bool(
                JOB["runner"] is not None and not JOB["runner"].done())})
            begin_shutdown(self.server)
        elif parsed.path.startswith("/organizer/api/"):
            try:
                if parsed.path == "/organizer/api/inspect":
                    value = organizer_web.inspect_source(
                        data.get("source", ""), self.pool_dir)
                elif parsed.path == "/organizer/api/plan":
                    reader = organizer_web.organizer.BSPoolReader(
                        organizer_web.resolve_source(
                            data.get("source", ""), self.pool_dir))
                    expected = str(data.get("snapshot", "")).lower()
                    if expected and expected != reader.snapshot_token:
                        raise organizer_web.organizer.PoolError(
                            "source changed; inspect it again")
                    value = organizer_web.build_split_plan(
                        reader, data.get("selectedCategories"),
                        data.get("choicePlan"))
                elif parsed.path == "/organizer/api/combine/plan":
                    value = organizer_web.build_combine_plan(data, self.pool_dir)
                elif parsed.path == "/organizer/api/split":
                    if not organizer_web.SPLIT_LOCK.acquire(False):
                        raise organizer_web.organizer.PoolError(
                            "another organizer split is still running")
                    try:
                        value = organizer_web.execute_split(
                            data.get("source", ""), data, self.pool_dir)
                    finally:
                        organizer_web.SPLIT_LOCK.release()
                elif parsed.path == "/organizer/api/combine":
                    if not organizer_web.COMBINE_LOCK.acquire(False):
                        raise organizer_web.organizer.PoolError(
                            "another pool combine is still running")
                    try:
                        value = organizer_web.execute_combine(data, self.pool_dir)
                    finally:
                        organizer_web.COMBINE_LOCK.release()
                else:
                    return self._json({"error": "not found"}, 404)
                self._json(value)
            except (OSError, ValueError,
                    organizer_web.organizer.PoolError) as exc:
                self._json({"error": str(exc)}, 400)
        else:
            self._json({"error": "not found"}, 404)


def existing_builder_url(port):
    """Return the current local Builder URL when that port is already ours."""
    base = "http://127.0.0.1:%d/" % port
    try:
        with urlopen(base + "api/ping", timeout=0.75) as response:
            value = json.loads(response.read().decode("utf-8"))
        if value.get("app") == "brainstorm-seed-pool-tools":
            return base
    except Exception:
        # Builders published before /api/ping can still be recognized by
        # their state response, preventing a stale second tab/server pair.
        try:
            with urlopen(base + "api/state", timeout=0.75) as response:
                value = json.loads(response.read().decode("utf-8"))
            if isinstance(value.get("job"), dict) and isinstance(
                    value.get("catalog"), dict):
                return base
        except Exception:
            pass
    return None


def main():
    problems = core.preflight()
    if problems:
        print("\n".join(problems), file=sys.stderr)
        print("\n(Fix the above, then run this again.)", file=sys.stderr)
        input("Press Return to close...")
        return 1
    Handler.snap = core.Snapshot(core.SNAPSHOT)
    JOB["closing"] = False
    port = DEFAULT_PORT
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        existing = existing_builder_url(port)
        if existing:
            print("Brainstorm Seed Pool Builder is already running at %s" % existing)
            if "--no-browser" not in sys.argv:
                webbrowser.open(existing)
            return 0
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
            while not r.done():
                time.sleep(0.1)
        print("Bye.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        socket.setdefaulttimeout(30)
        sys.exit(main())
    except Exception as e:  # keep the double-click Terminal window readable
        print("Unexpected error: %s" % e, file=sys.stderr)
        input("Press Return to close...")
        sys.exit(1)
