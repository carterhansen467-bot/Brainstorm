#!/usr/bin/env python3
"""Builder-side automatic seed-pool attachment contract regression."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import brainstorm_pool_builder as core
import pool_builder_web as web


CATALOG = "1111111111111111"


def write_pool(path, range_end=100_000_000, criteria=None, records=7,
               catalog=CATALOG):
    criteria = criteria or [
        "tag_route collect",
        "tag tag_charm 1 small 1 small 1",
        "legendary_routes full",
        "legendary j_perkeo 1 small 1 small 0 charm",
    ]
    lines = [
        "BRAINSTORM_SEED_POOL 2",
        "modelver 6",
        "charset 123456789ABCDEFGHIJKLMNPQRSTUVWXYZ",
        "seedspace %d" % core.SEEDSPACE,
        "space natural",
        "range_start 0",
        "range_end %d" % range_end,
        "catalog_hash %s" % catalog,
        "criteria_hash 2222222222222222",
        "pool_id attachment-fixture",
        "snapshot_id 3333333333333333",
    ] + list(criteria) + [
        "records %d" % records,
        "complete 1",
        "coverage_complete 1",
        "end",
        "",
    ]
    with open(path, "wb") as handle:
        handle.write("\n".join(lines).encode("ascii").ljust(1024, b"\0"))
        handle.write(b"records")


with tempfile.TemporaryDirectory(prefix="bs_pool_attach_") as pool_dir:
    partial_name = "perkeo partial.bspool"
    partial_path = os.path.join(pool_dir, partial_name)
    write_pool(partial_path)
    partial = core.PoolInfo(partial_path).as_dict()
    assert partial["attachment_accelerator_eligible"]
    assert not partial["attachment_authoritative_eligible"]
    signature = core.pool_attachment_signature(partial)
    assert signature["predicates"] == [
        "legendary j_perkeo 1 small 1 small 0 charm 1 full",
        "tag collect tag_charm 1 small 1 small 1",
    ]
    assert len(signature["hash"]) == 64

    marker = core.attach_completed_pool(
        partial_name, "accelerator", pool_dir, CATALOG)
    assert marker["valid"] and marker["enabled"]
    assert marker["role"] == "accelerator"
    assert os.path.isfile(partial_path + ".attached")
    refreshed = core.PoolInfo(partial_path).as_dict()
    assert refreshed["attached"]
    assert refreshed["attachment_role"] == "accelerator"

    with core._pool_writer_guard(partial_path):
        try:
            core.attach_completed_pool(
                partial_name, "accelerator", pool_dir, CATALOG)
        except ValueError as exc:
            assert "currently being written" in str(exc)
        else:
            raise AssertionError("attachment ignored the native writer lock")
        try:
            core.detach_pool(partial_name, pool_dir)
        except ValueError as exc:
            assert "currently being written" in str(exc)
        else:
            raise AssertionError("detachment ignored the native writer lock")

    try:
        core.attach_completed_pool(
            partial_name, "authoritative", pool_dir, CATALOG)
    except ValueError as exc:
        assert "does not cover every natural-space rank" in str(exc)
    else:
        raise AssertionError("partial pool was attached as authoritative")

    try:
        core.attach_completed_pool(
            partial_name, "accelerator", pool_dir, "ffffffffffffffff")
    except ValueError as exc:
        assert "different profile/catalog" in str(exc)
    else:
        raise AssertionError("foreign-catalog pool was attached")

    try:
        core.attach_completed_pool(
            partial_name, "accelerator", pool_dir, CATALOG, [partial_path])
    except ValueError as exc:
        assert "active Builder" in str(exc)
    else:
        raise AssertionError("active pool was attachable")

    # Replacing or mutating the pool invalidates its bound marker.
    with open(partial_path, "ab") as handle:
        handle.write(b"changed")
    stale = core.read_pool_attachment(partial_path)
    assert not stale["valid"]
    assert "file identity changed" in " ".join(stale["blockers"])
    detached = core.detach_pool(partial_name, pool_dir)
    assert detached["detached"]
    assert os.path.isfile(partial_path)
    assert not os.path.exists(partial_path + ".attached")

    full_name = "full.bspool"
    full_path = os.path.join(pool_dir, full_name)
    write_pool(full_path, range_end=core.SEEDSPACE)
    authoritative = core.attach_completed_pool(
        full_name, "authoritative", pool_dir, CATALOG)
    assert authoritative["valid"]
    assert authoritative["role"] == "authoritative"
    write_pool(os.path.join(pool_dir, "accelerator-small.bspool"), records=3)
    write_pool(os.path.join(pool_dir, "accelerator-large.bspool"), records=9)
    core.attach_completed_pool("accelerator-small.bspool", "accelerator",
                               pool_dir, CATALOG)
    core.attach_completed_pool("accelerator-large.bspool", "accelerator",
                               pool_dir, CATALOG)

    inherited_name = "refiltered.bspool"
    inherited_path = os.path.join(pool_dir, inherited_name)
    write_pool(inherited_path, criteria=[
        "route_legendary_routes canonical_charm",
        "route_tag observe tag_rare 2 small 4 big 1",
        "route_legendary j_perkeo 1 small 2 big 0 any 2",
        "route_voucher v_overstock_norm 1 3",
        "route_voucher_exclude v_clearance_sale",
        "tag_route collect",
        "legendary_routes full",
        "tag tag_charm 1 1 1",
        "legendary j_triboulet 2 4 1",
        "soul_depth any",
    ])
    inherited = core.PoolInfo(inherited_path).as_dict()
    assert core.pool_attachment_predicates(inherited) == [
        "legendary j_perkeo 1 small 2 big 0 any 2 canonical_charm",
        "legendary j_triboulet 2 boss 4 big 1 any 0 full",
        "tag collect tag_charm 1 small 1 big 1",
        "tag observe tag_rare 2 small 4 big 1",
        "voucher v_overstock_norm 1 3",
        "voucher_exclude v_clearance_sale",
    ]

    lua = shlex.split(os.environ.get("LUAJIT", "")) or ["luajit"]
    if os.name == "nt" and lua[0].startswith("/") and shutil.which("cygpath"):
        lua[0] = subprocess.check_output(
            ["cygpath", "-w", lua[0]], text=True).strip()
    if not shutil.which(lua[0]):
        raise RuntimeError("LuaJIT is required for the attachment matcher regression")
    harness = os.path.join(pool_dir, "attachment_matcher.lua")
    with open(harness, "w", encoding="utf-8") as handle:
        handle.write(r'''
local reroll, poolDir, markerPath = arg[1], arg[2], arg[3]
local function read(path, bytes)
  local f = io.open(path, "rb"); if not f then return nil end
  local value = f:read(bytes or "*a"); f:close(); return value
end
package.loaded.nativefs = {
  read = read, write = function() end,
  getInfo = function(path)
    local f = io.open(path, "rb"); if not f then return nil end
    local size = f:seek("end"); f:close(); return {type="file", size=size}
  end,
  getDirectoryItems = function() return {"accelerator-large.bspool.attached",
    "full.bspool.attached", "accelerator-small.bspool.attached"} end,
}
package.loaded.lovely = {mod_dir = ""}
G = {FUNCS = {}}
Brainstorm = {SETTINGS = {autoreroll = {}, multiAnteSearch = {}}, AUTOREROLL = {}}
assert(loadfile(reroll))()
Brainstorm.seedPoolDir = function() return poolDir end
Brainstorm.SETTINGS.autoreroll = {
  seedPoolFile = "", searchTag = "tag_charm", searchTagAnywhere = false,
  searchLegendary = "j_perkeo", searchLegendaryAnywhere = false,
  searchNegativeLegendary = false, searchForSoul = 0,
}
local marker, reason = Brainstorm.readPoolAttachment(markerPath)
assert(marker, reason)
assert(marker.role == "authoritative")
assert(Brainstorm.attachmentMatchesActiveFilters(marker))
Brainstorm.SETTINGS.autoreroll.searchTag = ""
assert(#Brainstorm.activeAttachmentPredicates() == 1)
assert(not Brainstorm.attachmentMatchesActiveFilters(marker))
Brainstorm.SETTINGS.autoreroll.searchTag = "tag_charm"
assert(not Brainstorm.attachmentMatchesActiveFilters({predicates={
  "tag collect tag_charm 1 small 1 big 1",
  "legendary j_perkeo 1 small 1 small 1 charm 1 full"}}))
Brainstorm.SETTINGS.autoreroll.searchNegativeLegendary = true
assert(Brainstorm.attachmentMatchesActiveFilters({predicates={
  "tag collect tag_charm 1 small 1 big 1",
  "legendary j_perkeo 1 small 1 small 1 charm 1 full"}}))
Brainstorm.SETTINGS.autoreroll.searchNegativeLegendary = false
assert(not Brainstorm.attachmentMatchesActiveFilters({predicates={
  "tag collect tag_charm 1 small 1 big 1",
  "legendary j_perkeo 1 small 1 small 0 any 1 full"}}))
assert(Brainstorm.attachmentMatchesActiveFilters({predicates={
  "tag collect tag_charm 1 small 1 big 1",
  "legendary j_perkeo 1 small 1 small 0 charm 0 canonical_charm"}}))
assert(not Brainstorm.attachmentMatchesActiveFilters({predicates={
  "voucher v_overstock_norm 1 2"}}))
local selected = assert(Brainstorm.findAutomaticSeedPool())
-- The compatible authoritative pool outranks both smaller accelerators
-- (ATTACHED_SEED_POOLS.md runtime selection rule 3): its exhaustion is
-- definitive, so a miss never falls back to a full unrestricted scan.
assert(selected.pool_file == "full.bspool")
require("nativefs").getDirectoryItems = function()
  return {"full.bspool.attached"}
end
selected = assert(Brainstorm.findAutomaticSeedPool())
assert(selected.pool_file == "full.bspool")
require("nativefs").getDirectoryItems = function()
  return {"accelerator-large.bspool.attached", "accelerator-small.bspool.attached"}
end
selected = assert(Brainstorm.findAutomaticSeedPool())
assert(selected.pool_file == "accelerator-small.bspool")
Brainstorm.SETTINGS.autoreroll.searchLegendary = "j_caino"
assert(not Brainstorm.attachmentMatchesActiveFilters(marker))
Brainstorm.SETTINGS.autoreroll.searchLegendary = "j_perkeo"
Brainstorm.SETTINGS.autoreroll.searchTag = "tag_rare"
assert(not Brainstorm.attachmentMatchesActiveFilters(marker))
assert(not Brainstorm.attachmentMatchesActiveFilters({predicates={
  "tag collect tag_rare 1 small 1 big 1",
  "legendary j_perkeo 1 small 1 small 0 charm 1 full"}}))
Brainstorm.SETTINGS.autoreroll.searchTag = "tag_charm"
Brainstorm.SETTINGS.autoreroll.searchTagAnywhere = true
assert(not Brainstorm.attachmentMatchesActiveFilters(marker))
Brainstorm.SETTINGS.autoreroll.searchTagAnywhere = false
Brainstorm.AUTOREROLL.autoPoolSelection = marker
Brainstorm.SETTINGS.autoreroll.seedPoolFile = "full.bspool"
assert(Brainstorm.findAutomaticSeedPool() == nil)
local effective = assert(Brainstorm.effectiveSeedPoolSelection())
assert(effective.role == "manual" and not effective.automatic)
Brainstorm.SETTINGS.autoreroll.seedPoolFile = ""
local nativefs = require("nativefs")
local statusText = "P 7\nE pool: no seed in the pool matches the active filters\n"
local fileRead = nativefs.read
local denyAttachment = false
nativefs.read = function(path)
  if path == "status" then return statusText end
  if denyAttachment and path == markerPath then return nil end
  return fileRead(path)
end
Brainstorm.nativePaths = function() return {status="status", stop="stop", hb="hb"} end
local accelerator = {poolPath=marker.poolPath, pool_file=marker.pool_file,
  role="accelerator", automatic=true, header=marker.header,
  path=marker.path, pool_id=marker.pool_id}
Brainstorm.AUTOREROLL.autoPoolSelection = accelerator
Brainstorm.AUTOREROLL.nativeActive = true
Brainstorm.AUTOREROLL.nativeHbFrame = 0
Brainstorm.pollNativeSearch()
assert(Brainstorm.AUTOREROLL.autoPoolSelection == nil)
assert(Brainstorm.AUTOREROLL.autoPoolDisabled == nil)
assert(Brainstorm.AUTOREROLL.autoPoolTried[marker.path])
assert(Brainstorm.AUTOREROLL.searchTried == 7)
selected = assert(Brainstorm.findAutomaticSeedPool())
assert(selected.pool_file == "accelerator-small.bspool")
Brainstorm.AUTOREROLL.autoPoolDisabled = nil
Brainstorm.AUTOREROLL.autoPoolAbort = nil
Brainstorm.AUTOREROLL.autoPoolWarned = nil
Brainstorm.AUTOREROLL.autoPoolTried = nil
Brainstorm.AUTOREROLL.autoPoolSelection = marker
Brainstorm.AUTOREROLL.nativeActive = true
statusText = "P 0\nE pool: profile/unlock snapshot differs; rebuild the pool\n"
Brainstorm.pollNativeSearch()
assert(Brainstorm.AUTOREROLL.autoPoolSelection == nil)
assert(Brainstorm.AUTOREROLL.autoPoolDisabled == nil)
assert(Brainstorm.AUTOREROLL.autoPoolTried[marker.path])
assert(Brainstorm.AUTOREROLL.autoPoolAbort == nil)
Brainstorm.AUTOREROLL.autoPoolDisabled = nil
Brainstorm.AUTOREROLL.autoPoolWarned = nil
Brainstorm.AUTOREROLL.autoPoolTried = nil
Brainstorm.AUTOREROLL.autoPoolSelection = marker
Brainstorm.AUTOREROLL.nativeActive = true
Brainstorm.AUTOREROLL.nativeFailed = nil
local estimateMode = true
Brainstorm.setAttachedPoolEstimateMode = function(attached) estimateMode = attached end
statusText = "P 3\nE native helper failed unexpectedly\n"
Brainstorm.pollNativeSearch()
assert(Brainstorm.AUTOREROLL.autoPoolSelection == nil)
assert(Brainstorm.AUTOREROLL.autoPoolDisabled)
assert(Brainstorm.AUTOREROLL.nativeFailed)
assert(estimateMode == false)
Brainstorm.AUTOREROLL.autoPoolDisabled = nil
Brainstorm.AUTOREROLL.autoPoolWarned = nil
Brainstorm.AUTOREROLL.autoPoolTried = nil
Brainstorm.AUTOREROLL.autoPoolSelection = marker
Brainstorm.AUTOREROLL.nativeActive = true
statusText = "P 7\nE pool: no seed in the pool matches the active filters\n"
Brainstorm.pollNativeSearch()
assert(Brainstorm.AUTOREROLL.autoPoolAbort)
assert(Brainstorm.AUTOREROLL.autoPoolDisabled == nil)
Brainstorm.AUTOREROLL.autoPoolAbort = nil
Brainstorm.AUTOREROLL.autoPoolWarned = nil
Brainstorm.AUTOREROLL.autoPoolSelection = marker
Brainstorm.AUTOREROLL.nativeActive = true
denyAttachment = true
Brainstorm.pollNativeSearch()
assert(Brainstorm.AUTOREROLL.autoPoolAbort == nil)
assert(Brainstorm.AUTOREROLL.autoPoolDisabled == nil)
assert(Brainstorm.AUTOREROLL.autoPoolTried[marker.path])
assert(Brainstorm.AUTOREROLL.autoPoolWarned:find("changed during search", 1, true))
''')
    subprocess.run(lua + [harness, os.path.join(ROOT, "Brainstorm_reroll.lua"),
                          pool_dir, full_path + ".attached"], check=True)

    # Exercise the browser's Attach/Detach endpoints against the current
    # snapshot hash rather than trusting a client-supplied catalog identity.
    snapshot_path = os.path.join(pool_dir, "native_search.cfg")
    with open(snapshot_path, "w", encoding="utf-8") as handle:
        handle.write("modelver 6\nend\n")
    current_catalog = core.catalog_hash_file(snapshot_path)
    assert current_catalog == "7cd914eb093b8cb1"

    class Snapshot:
        @staticmethod
        def current_model_copy():
            return snapshot_path

        @staticmethod
        def usable_tags():
            return []

        @staticmethod
        def usable_legendaries():
            return []

        @staticmethod
        def usable_vouchers():
            return []

    api_name = "api.bspool"
    write_pool(os.path.join(pool_dir, api_name), catalog=current_catalog)
    old_pool_dir, old_snap = web.Handler.pool_dir, web.Handler.snap
    web.Handler.pool_dir, web.Handler.snap = pool_dir, Snapshot()
    server = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]

    def post(route, value):
        request = Request(base + route, data=json.dumps(value).encode("utf-8"),
                          method="POST", headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    try:
        attached = post("/api/attach", {"name": api_name, "role": "accelerator"})
        assert attached["valid"] and attached["role"] == "accelerator"
        detached = post("/api/detach", {"name": api_name})
        assert detached["detached"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        web.Handler.pool_dir, web.Handler.snap = old_pool_dir, old_snap

for marker in ("/api/attach", "/api/detach", "Attach to Brainstorm",
               "Remove stale attachment", "changePoolAttachment"):
    assert marker in open(web.__file__, encoding="utf-8").read()

print("pool attachment contract: ok")
