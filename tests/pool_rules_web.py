#!/usr/bin/env python3
"""Exercise the same rule endpoints used by both Program entry points."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import brainstorm_pool_organizer as organizer
import pool_organizer_web as web
import pool_builder_web as builder_web
import pool_rules_ui as rules_ui

spec = importlib.util.spec_from_file_location("rules_web_fixture", ROOT / "tests/pool_organizer.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class RulesWebRegression(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pool-rules-web-")
        self.root = self.temp.name
        web.allow_active_operations()
        with web.RULE_REVIEW_LOCK:
            web.RULE_REVIEWS.clear()
        with web.READER_CACHE_LOCK:
            web.READER_CACHE.clear()
        tag = lambda key, ante, phase: fixture.descriptor(1, "tag_" + key, ante, phase, 0, 0, 0)
        self.events = [
            [tag("negative", 3, 1), tag("rare", 5, 1)],
            [tag("rare", 3, 1), tag("negative", 3, 2), tag("negative", 6, 2)],
            [tag("rare", 4, 1), tag("negative", 4, 2)],
        ]
        criteria = ["tag_route observe", "tag tag_negative 3 small 7 big 1",
                    "tag tag_rare 3 small 7 big 1"]
        fixture.write_custom_bsp3(os.path.join(self.root, "L1.bspool"),
                                 [1, 2, 3], self.events,
                                 "1111111111111111", criteria)
        fixture.write_custom_bsp3(os.path.join(self.root, "L2.bspool"),
                                 [2], [self.events[1]],
                                 "2222222222222222", criteria)
        organizer.combine_pools([
            organizer.BSPoolReader(os.path.join(self.root, name))
            for name in ("L1.bspool", "L2.bspool")],
            os.path.join(self.root, "Complete.bspool"))

    def tearDown(self):
        with web.RULE_REVIEW_LOCK:
            web.RULE_REVIEWS.clear()
        self.temp.cleanup()

    def request(self, url, data):
        req = Request(url, data=json.dumps(data).encode(),
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        except HTTPError as response:
            return response.code, json.load(response)

    def test_http_recover_deleted_inputs_in_both_entry_points(self):
        os.unlink(os.path.join(self.root, "L1.bspool"))
        os.unlink(os.path.join(self.root, "L2.bspool"))
        for unified in (False, True):
            with self.subTest(unified=unified):
                if unified:
                    class Handler(builder_web.Handler):
                        pool_dir = self.root
                else:
                    Handler = web.make_handler(self.root)
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%s%s/api/rules/" % (
                    server.server_port, "/organizer" if unified else "")
                try:
                    status, description = self.request(base + "describe", {"source": "Complete.bspool"})
                    self.assertEqual(status, 200, description)
                    self.assertTrue(description["counts_pending"])
                    self.assertTrue(all(r["records"] is None for r in description["direct_inputs"]))
                    self.assertEqual(sorted(r["original_records"] for r in description["direct_inputs"]), [1, 3])
                    status, validated = self.request(base + "validate", {"document": json.dumps({
                        "version": 1, "mode": "second_tag", "rule": {
                            "version": 1, "range": {"start": "a3s", "end": "a7b"}}})})
                    self.assertEqual(status, 200, validated)
                    self.assertEqual(validated["recipe"]["rule"]["range"]["start"], "A3S")
                    status, plan = self.request(base + "preview", {
                        "source": "Complete.bspool", "prefix": "restored-" + str(unified),
                        "recipe": {"version": 1, "mode": "separate_sources", "source_kind": "inputs"}})
                    self.assertEqual(status, 200, plan)
                    self.assertEqual((plan["copied_records"], plan["output_memberships"]), (3, 4))
                    status, result = self.request(base + "publish", {
                        "source": "Complete.bspool", "planToken": plan["plan_token"]})
                    self.assertEqual(status, 200, result)
                    self.assertEqual(sorted(r["records"] for r in result["outputs"]), [1, 3])
                    status, again = self.request(base + "publish", {
                        "source": "Complete.bspool", "planToken": plan["plan_token"]})
                    self.assertEqual(status, 400)
                    self.assertIn("preview", again["error"].lower())
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)

    def test_find_lists_groups_without_payload_scan_then_preview_validates_once(self):
        request = {"source": "Complete.bspool"}
        original = organizer.BSPoolReader._read_validated_block_records
        reads = []

        def track(reader, *args, **kwargs):
            reads.append(reader.path)
            return original(reader, *args, **kwargs)

        # The compatibility path must still validate once and reuse its proof.
        # Native Preview has a separate full-payload regression suite.
        with mock.patch.object(organizer.BSPoolReader, "_read_validated_block_records", track), \
                mock.patch.object(web, "_native_split_helper", return_value=None):
            web.run_rule_describe(request, self.root)
            self.assertEqual(reads, [])
            cached = web.verified_source_reader(request["source"], self.root)
            self.assertFalse(cached._payload_verified)
            settings = dict(request, recipe={"version": 1, "mode": "separate_sources"})
            first = web.run_rule_preview(settings, self.root)
            self.assertEqual(len(reads), len(cached.blocks))
            self.assertTrue(cached._payload_verified)
            second = web.run_rule_preview(settings, self.root)
            self.assertEqual(len(reads), len(cached.blocks))
            self.assertEqual(first["outputs"], second["outputs"])
            self.assertIsNone(cached.cancel_check)

    def test_find_does_not_certify_corrupt_payload_and_preview_rejects_it(self):
        path = os.path.join(self.root, "Complete.bspool")
        source = organizer.BSPoolReader(path, verify_payloads=False)
        block = source.blocks[0]
        with open(path, "r+b") as handle:
            handle.seek(block.offset + block.header_bytes)
            original = handle.read(1)
            handle.seek(-1, os.SEEK_CUR)
            handle.write(bytes((original[0] ^ 1,)))
        description = web.run_rule_describe({"source": "Complete.bspool"}, self.root)
        self.assertTrue(description["counts_pending"])
        self.assertEqual(len(description["direct_inputs"]), 2)
        before = set(os.listdir(self.root))
        with self.assertRaises(organizer.PoolError):
            web.run_rule_preview({
                "source": "Complete.bspool", "snapshot": description["source"]["snapshot_id"],
                "recipe": {"version": 1, "mode": "separate_sources"}}, self.root)
        self.assertEqual(set(os.listdir(self.root)), before)
        self.assertFalse(web.RULE_REVIEWS)
        self.assertEqual(web.operation_progress("rules")["state"], "failed")

    def test_replaced_source_invalidates_description_and_verified_reader(self):
        request = {"source": "Complete.bspool"}
        description = web.run_rule_describe(request, self.root)
        prior = web.verified_source_reader(request["source"], self.root)
        source = os.path.join(self.root, request["source"])
        replacement = os.path.join(self.root, "replacement.bspool")
        shutil.copyfile(os.path.join(self.root, "L2.bspool"), replacement)
        os.replace(replacement, source)
        with self.assertRaisesRegex(organizer.PoolError, "changed"):
            web.run_rule_preview(dict(
                request, snapshot=description["source"]["snapshot_id"],
                recipe={"version": 1, "mode": "separate_sources"}), self.root)
        current = web.verified_source_reader(request["source"], self.root)
        self.assertIsNot(prior, current)
        self.assertEqual(current.records, 1)

    def test_mutated_payload_cannot_reuse_a_previous_preview_verification(self):
        request = {"source": "Complete.bspool", "recipe": {
            "version": 1, "mode": "separate_sources"}}
        plan = web.run_rule_preview(request, self.root)
        cached = web.verified_source_reader(request["source"], self.root)
        self.assertTrue(cached._payload_verified)
        block = cached.blocks[0]
        prior_stat = os.stat(cached.path)
        with open(cached.path, "r+b") as handle:
            handle.seek(block.offset + block.header_bytes)
            original = handle.read(1)
            handle.seek(-1, os.SEEK_CUR)
            handle.write(bytes((original[0] ^ 1,)))
        # Make the changed last-write time deterministic across platforms and
        # filesystems, while retaining the same pathname and byte length.
        os.utime(cached.path, ns=(prior_stat.st_atime_ns,
                                 prior_stat.st_mtime_ns + 1000000000))
        before = set(os.listdir(self.root))
        with self.assertRaises(organizer.PoolError):
            web.run_rule_preview(request, self.root)
        with self.assertRaises(organizer.PoolError):
            web.run_rule_publish({"source": request["source"],
                                  "planToken": plan["plan_token"]}, self.root)
        self.assertEqual(set(os.listdir(self.root)), before)

    def test_filtered_groups_distinguish_historical_sizes_from_remaining_seeds(self):
        source = organizer.BSPoolReader(os.path.join(self.root, "Complete.bspool"))
        filtered = os.path.join(self.root, "Filtered.bspool")
        writer = organizer.BSP4OutputWriter(
            source, "test-filtered-subset", "Filtered", filtered)
        try:
            for record in source.iter_records():
                if record.rank == 1:
                    writer.add(record)
            writer.finalize()
            os.replace(writer.temp_path, filtered)
        except BaseException:
            writer.abort()
            raise
        for name in ("L1.bspool", "L2.bspool", "Complete.bspool"):
            os.unlink(os.path.join(self.root, name))
        description = web.run_rule_describe({"source": "Filtered.bspool"}, self.root)
        self.assertTrue(description["counts_pending"])
        self.assertIsNone(description["overlap_records"])
        self.assertTrue(all(row["records"] is None for row in description["direct_inputs"]))
        self.assertTrue(all(row["missing_records"] is None for row in description["direct_inputs"]))
        self.assertEqual(sorted(row["original_records"] for row in description["direct_inputs"]), [1, 3])
        plan = web.run_rule_preview({
            "source": "Filtered.bspool", "snapshot": description["source"]["snapshot_id"],
            "recipe": {"version": 1, "mode": "separate_sources", "source_kind": "inputs"}}, self.root)
        self.assertEqual(plan["copied_records"], 1)
        self.assertEqual(plan["output_memberships"], 1)
        self.assertEqual([row["records"] for row in plan["outputs"]], [1])
        report = web.run_rule_publish({"source": "Filtered.bspool",
                                      "planToken": plan["plan_token"]}, self.root)
        self.assertEqual(len(report["outputs"]), 1)
        restored = organizer.BSPoolReader(report["outputs"][0]["path"])
        self.assertEqual([record.rank for record in restored.iter_records()], [1])

    def test_tag_preview_stays_bound_to_source_and_private_plan(self):
        request = {"source": "L1.bspool", "prefix": "second", "recipe": {
            "version": 1, "mode": "second_tag", "rule": {
                "version": 1, "range": {"start": "A3S", "end": "A7B"}}}}
        plan = web.run_rule_preview(request, self.root)
        self.assertEqual(plan["copied_records"], 2)
        self.assertEqual(plan["exclusions"], {"same_ante_only": 1})
        self.assertEqual({r["label"] for r in plan["outputs"]}, {"A5 Small Rare", "A6 Big Negative"})
        plan["outputs"][0]["records"] = 999
        result = web.run_rule_publish({"source": "L1.bspool", "planToken": plan["plan_token"]}, self.root)
        self.assertEqual([r["records"] for r in result["outputs"]], [1, 1])
        collision = web.run_rule_preview(request, self.root)
        self.assertFalse(collision["can_create"])
        self.assertEqual(len(collision["collisions"]), 3)
        self.assertIn(os.path.basename(result["report_path"]), collision["collisions"])
        for output in plan["outputs"]:
            os.unlink(os.path.join(self.root, output["name"]))
        report_collision = web.run_rule_preview(request, self.root)
        self.assertFalse(report_collision["can_create"])
        self.assertEqual(report_collision["collisions"], [report_collision["report_name"]])

    def test_changed_snapshot_and_foreign_preview_refused(self):
        with self.assertRaisesRegex(organizer.PoolError, "changed"):
            web.run_rule_describe({"source": "Complete.bspool", "snapshot": "bad"}, self.root)
        plan = web.run_rule_preview({"source": "Complete.bspool", "recipe": {
            "version": 1, "mode": "separate_sources"}}, self.root)
        with self.assertRaisesRegex(organizer.PoolError, "another pool"):
            web.run_rule_publish({"source": "L1.bspool", "planToken": plan["plan_token"]}, self.root)

    def test_unrecorded_range_is_not_treated_as_no_tags(self):
        with self.assertRaisesRegex(ValueError, "record|cover"):
            web.run_rule_preview({"source": "L1.bspool", "recipe": {
                "version": 1, "mode": "second_tag", "rule": {
                    "version": 1, "range": {"start": "A2S", "end": "A7B"}}}}, self.root)
        self.assertEqual(web.operation_progress("rules")["state"], "failed")
        self.assertEqual(web.cancel_operation("rules")["state"], "idle")

    def test_ui_fragment_is_packaged_once_with_accessible_controls(self):
        self.assertEqual(web.PAGE.count('id="rulesWorkspace"'), 1)
        for identifier in ("ruleStart", "ruleEnd", "ruleSource", "ruleName", "rulePrefix"):
            self.assertIn('for="%s"' % identifier, web.PAGE)
        self.assertNotIn("/*__RULE_", web.PAGE)
        self.assertIn("Optional tag conditions", web.PAGE)
        self.assertIn("Earlier recorded source groups", web.PAGE)

    def test_browser_source_counts_and_snapshot_handshake(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for browser JavaScript checks")
        setup = r'''
const assert=require("node:assert/strict"),nodes=new Map();
function $(id){if(!nodes.has(id))nodes.set(id,{value:"",textContent:"",innerHTML:"",hidden:false,disabled:false,replaceChildren(){},setAttribute(){}});return nodes.get(id)}
const esc=value=>String(value).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const fmt=value=>Number(value||0).toLocaleString("en-US"),fmtDuration=()=>"1s";
let selected=[{value:"input-id",checked:true}],api;
globalThis.document={querySelectorAll:selector=>selector.startsWith(".rule-origin")?selected:[]};
'''
        harness = r'''
$("ruleSource").value="Complete.bspool";$("ruleSourceKind").value="inputs";
const description={counts_pending:true,source:{snapshot_id:"pinned-snapshot",records:1,complete:true},
  direct_inputs:[{id:"input-id",label:"L1 <old>",records:null,original_records:300},
                 {id:"empty-id",label:"L2",records:0,original_records:99}],
  original_sources:[{id:"branch-id",label:"L1 original",records:null}]};
ruleState.description=description;renderRuleSources();
assert.match($("ruleSources").innerHTML,/300 originally/);
assert.match($("ruleSources").innerHTML,/0 seeds/);
assert.doesNotMatch($("ruleSources").innerHTML,/300 seeds|99 originally/);
assert.match($("ruleSources").innerHTML,/L1 &lt;old&gt;/);
assert.match($("ruleHistoryHint").textContent,/historical/);
$("ruleSourceKind").value="branches";renderRuleSources();
assert.match($("ruleSources").innerHTML,/Count in preview/);
assert.doesNotMatch($("ruleSources").innerHTML,/0 seeds/);
$("ruleSourceKind").value="inputs";
api=async(path,data)=>{
 assert.equal(path,"/api/rules/describe");
 assert.equal(data.source,"Complete.bspool");
 assert.equal($("ruleStatus").textContent,"Loading saved group names…");
 assert.equal($("ruleInputs").disabled,true);
 return description;
};
await detectRuleSources();
assert.match($("ruleStatus").textContent,/preview to check matching seeds/);
assert.equal($("ruleInputs").disabled,false);
api=async(path,data)=>{
 assert.equal(path,"/api/rules/preview");
 assert.equal(data.snapshot,"pinned-snapshot");
 assert.deepEqual(data.recipe.source_ids,["input-id"]);
 return {outputs:[{name:"restored.bspool",label:"L1",records:1}],copied_records:1,excluded_records:0,can_create:true};
};
await previewRules();
assert.match($("ruleManifest").innerHTML,/1 seed/);
assert.equal($("ruleReview").hidden,false);
api=async()=>{throw new Error("This source pool changed. Check its recorded data again.")};
await previewRules();
assert.match($("ruleError").textContent,/source pool changed/);
assert.equal(ruleState.plan,null);
assert.equal($("ruleReview").hidden,true);
assert.equal($("ruleCreateBtn").disabled,true);
$("ruleSource").value="Another.bspool";ruleSourceChanged();
assert.equal(ruleState.description,null);
assert.equal($("ruleSourceGroups").hidden,true);
'''
        result = subprocess.run(
            [node, "-"], input=setup + rules_ui.SCRIPT
            + "\n(async()=>{\n" + harness
            + "\n})().catch(e=>{console.error(e);process.exitCode=1});\n",
            text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_saved_rule_validation_does_not_discard_unknown_settings(self):
        recipe = {"version": 1, "mode": "second_tag", "rule": {
            "version": 1, "range": {"start": "A3S", "end": "A7B"}}}
        for invalid in (
                dict(recipe, version=2), dict(recipe, unrecognized=True),
                {"version": 1, "mode": "second_tag", "rule": {
                    "version": 1, "range": {"start": "A3S", "end": "A7B", "inclusive": False}}}):
            with self.subTest(recipe=invalid), self.assertRaises(ValueError):
                web.run_rule_validate({"document": json.dumps(invalid)})
        with self.assertRaisesRegex(ValueError, "repeats the field"):
            web.run_rule_validate({"document": '{"version":2,"version":1,"mode":"second_tag","rule":{}}'})
        recipe["rule"]["condition"] = {"not": {"not": {"count": {
            "tag": "rare", "range": {"start": "A3S", "end": "A7B"}, "min": 1}}}}
        self.assertEqual(web.run_rule_validate({"document": json.dumps(recipe)})["recipe"], recipe)

    def run_rules_browser(self, harness):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for browser JavaScript checks")
        setup = r'''
const assert=require("node:assert/strict"),nodes=new Map();
function element(){return {value:"",textContent:"",innerHTML:"",className:"",hidden:false,
 disabled:false,open:false,options:[],replaceChildren(){},setAttribute(){},
 add(option){this.options.push(option)},append(){},focus(){}}}
function $(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id)}
const esc=value=>String(value).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
const fmt=value=>Number(value||0).toLocaleString("en-US"),fmtDuration=()=>"1s";
let api,loadPools;
const workflowState={pools:[]};
globalThis.document={querySelectorAll:()=>[],createElement:()=>element()};
'''
        result = subprocess.run(
            [node, "-"], input=setup + rules_ui.SCRIPT
            + "\nrenderRuleCondition=()=>{};\n(async()=>{\n" + harness
            + "\n})().catch(e=>{console.error(e);process.exitCode=1});\n",
            text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_browser_coverage_error_keeps_rule_and_offers_recording(self):
        self.run_rules_browser(r'''
ruleState.mode="tag";ruleState.activeSource="L1.bspool";
$("ruleSource").value="L1.bspool";$("ruleStart").value="A4S";$("ruleEnd").value="A7B";
$("ruleName").value="Keep my rule";
ruleState.condition={not:{count:{tag:"rare",range:{start:"A1S",end:"A2B"},min:1}}};
const before=readTagRule();
api=async()=>{const error=Error("Seed rank 188495 lacks recorded coverage");
 error.code="tag_coverage_missing";error.rank=188495;
 error.missingCoverage=[{tag:"negative",range:{start:"A4S",end:"A7B"}},
 {tag:"rare",range:{start:"A1S",end:"A2B"}}];throw error};
await previewRules();
assert.deepEqual(readTagRule(),before);
assert.equal(ruleState.plan,null);assert.equal($("ruleReview").hidden,true);
assert.equal($("ruleCreateBtn").disabled,true);assert.equal($("ruleRecordBtn").disabled,false);
assert.match($("ruleRecordPanel").className,/warning/);
assert.match($("ruleRecordMissing").textContent,/Negative A4S.*A7B/);
assert.match($("ruleRecordMissing").textContent,/Rare A1S.*A2B/);
assert.equal($("ruleDataDetails").hidden,false);assert.equal($("ruleDataDetails").open,true);
assert.match($("ruleMissingDetail").textContent,/188,495/);
ruleInvalidate();assert.equal($("ruleRecordMissing").hidden,true);
assert.equal($("ruleMissingDetail").hidden,true);assert.equal($("ruleMissingDetail").textContent,"");
assert.doesNotMatch($("ruleRecordPanel").className,/warning/);
''')

    def test_browser_recording_selects_copy_and_preserves_rule(self):
        self.run_rules_browser(r'''
for(const listFails of [false,true]){
 ruleState.mode="tag";ruleState.activeSource="L1.bspool";
 $("ruleSource").value="L1.bspool";$("ruleSource").options=[{value:"L1.bspool"}];
 $("ruleStart").value="A4S";$("ruleEnd").value="A7B";
 $("ruleName").value="My exact settings";$("rulePrefix").value="AS1-L1";
 ruleState.condition={any:[{not:{count:{tag:"negative",range:{start:"A2B",end:"A3S"},min:1}}}]};
 ruleState.description={source:{snapshot_id:"old-snapshot"}};
 ruleState.plan={can_create:true};
 const before=readTagRule();let calls=0;
 api=async(path,data)=>{
  calls++;assert.equal(path,"/api/rules/record-tags");
  assert.equal(data.source,"L1.bspool");assert.deepEqual(data.recipe,{version:1,mode:"second_tag",rule:before});
  assert.equal(data.prefix,"AS1-L1");
  assert.equal($("ruleInputs").disabled,true);assert.equal($("ruleRecordBtn").disabled,true);
  assert.equal($("ruleCancelBtn").hidden,false);
  await recordRuleTags();assert.equal(calls,1);
  return {source:"L1-tag-data.bspool",records:1200};
 };
 loadPools=async preserve=>{
  assert.equal(preserve,true);
  if(listFails){$("ruleSource").options=[];$("ruleSource").value="";ruleSourceChanged();throw Error("list offline")}
  $("ruleSource").options.push({value:"L1-tag-data.bspool"});
 };
 await recordRuleTags();
 assert.equal(calls,1);assert.equal($("ruleSource").value,"L1-tag-data.bspool");
 assert.ok($("ruleSource").options.some(o=>o.value==="L1-tag-data.bspool"));
 assert.deepEqual(readTagRule(),before);
 assert.deepEqual(ruleState.drafts["L1.bspool"],before);
 assert.deepEqual(ruleState.drafts["L1-tag-data.bspool"],before);
 assert.equal($("rulePrefix").value,"AS1-L1");assert.equal(ruleState.description,null);
 assert.equal(ruleState.plan,null);assert.equal($("ruleReview").hidden,true);
 assert.equal($("ruleRecordBtn").disabled,false);assert.equal($("ruleCancelBtn").hidden,true);
 assert.equal($("ruleStatus").textContent,"Tag data recorded for 1,200 seeds. Preview the second-tag split.");
 if(listFails)assert.match($("ruleError").textContent,/recorded.*list.*refresh/i);
 else assert.equal($("ruleError").textContent,"");
}
const source=$("ruleSource").value,before=readTagRule();
api=async()=>{const error=Error("Stopped safely");error.code="operation_cancelled";throw error};
loadPools=async()=>{throw Error("Cancelled recording must not refresh pools")};
await recordRuleTags();assert.equal($("ruleSource").value,source);assert.deepEqual(readTagRule(),before);
assert.equal($("ruleStatus").textContent,"Cancelled.");assert.equal($("ruleInputs").disabled,false);
ruleState.mode="restore";api=async()=>{throw Error("Restore mode must not record tags")};
await recordRuleTags();
''')

    def test_browser_recording_progress_distinguishes_verification_passes(self):
        self.run_rules_browser(r'''
let poll,phase;
globalThis.setInterval=callback=>{poll=callback;return 1};globalThis.clearInterval=()=>{};
ruleState.mode="tag";ruleState.activeSource="L1.bspool";
$("ruleSource").value="L1.bspool";$("ruleStart").value="A4S";$("ruleEnd").value="A7B";
api=async(path)=>{
 if(path.startsWith("/api/progress"))return {state:"running",phase,records_done:1,records_total:12};
 assert.equal(path,"/api/rules/record-tags");
 for(const [current,label] of [["verifying_source","Checking the source pool"],
  ["recording_tags","Recording tag placements"],["verifying_output","Verifying the new tag-data pool"]]){
  phase=current;await poll();assert.ok($("ruleStatus").textContent.startsWith(label));
  assert.match($("ruleStatus").textContent,/1 of 12 seeds/);
 }
 return {source:"recorded.bspool",records:12};
};
loadPools=async()=>{};
await recordRuleTags();
assert.equal($("ruleStatus").textContent,"Tag data recorded for 12 seeds. Preview the second-tag split.");
''')

    def test_browser_recorded_ranges_are_not_a_limit_on_per_seed_data(self):
        self.run_rules_browser(r'''
$("ruleStart").value="A4S";$("ruleEnd").value="A7B";
renderRuleData({source:{records:12,complete:true},coverage:{metadata_complete:true,
 checked_per_seed:true,sources:[{label:"L1 <original>",tags:{negative:[{start:"A3S",end:"A5B"}],
 rare:[{start:"A4S",end:"A6B"}]},both_tags:[{start:"A4S",end:"A5B"}]},
 {label:"No original tag filters",tags:{negative:[],rare:[]},both_tags:[]}]}});
const html=$("ruleData").innerHTML;
assert.match(html,/Recorded filter ranges/);assert.match(html,/L1 &lt;original&gt;/);
assert.match(html,/Both tags: A4S through A5B/);assert.match(html,/vary by seed/);
assert.match(html,/Additional placements.*individual seeds/);
assert.doesNotMatch(html,/cannot prove|not recorded/);
assert.equal($("ruleStart").value,"A4S");assert.equal($("ruleEnd").value,"A7B");
''')

    def test_api_client_preserves_coverage_error_details_in_both_entry_points(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for browser JavaScript checks")
        api_script = web.PAGE.split("async function api(path,data)", 1)[1].split("\nfunction ", 1)[0]
        script = r'''
const assert=require("node:assert/strict");
let UNIFIED=false;
const apiPath=path=>UNIFIED?"/organizer"+path:path;
''' + "async function api(path,data)" + api_script + r'''
(async()=>{
 const missing=[{tag:"negative",range:{start:"A4S",end:"A7B"}}];
 for(UNIFIED of [false,true]){
  globalThis.fetch=async(path,options)=>{
   assert.equal(path,(UNIFIED?"/organizer":"")+"/api/rules/preview");
   assert.equal(JSON.parse(options.body).source,"L1.bspool");
   return {ok:false,status:400,json:async()=>({error:"Missing placements",
    error_code:"tag_coverage_missing",missing_coverage:missing,rank:188495})};
  };
  await assert.rejects(()=>api("/api/rules/preview",{source:"L1.bspool"}),error=>{
   assert.equal(error.code,"tag_coverage_missing");assert.deepEqual(error.missingCoverage,missing);
   assert.equal(error.rank,188495);return true;
  });
  globalThis.fetch=async(path,options)=>{
   assert.equal(path,(UNIFIED?"/organizer":"")+"/api/rules/record-tags");
   assert.equal(JSON.parse(options.body).source,"L1.bspool");
   return {ok:true,status:200,json:async()=>({source:"L1-tag-data.bspool",records:10})};
  };
  assert.deepEqual(await api("/api/rules/record-tags",{source:"L1.bspool"}),
   {source:"L1-tag-data.bspool",records:10});
 }
})().catch(error=>{console.error(error);process.exitCode=1});
'''
        result = subprocess.run([node, "-"], input=script, text=True,
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_http_coverage_error_is_structured_in_both_entry_points(self):
        for unified in (False, True):
            with self.subTest(unified=unified):
                if unified:
                    class Handler(builder_web.Handler):
                        pool_dir = self.root
                else:
                    Handler = web.make_handler(self.root)
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = "http://127.0.0.1:%s%s/api/rules/preview" % (
                        server.server_port, "/organizer" if unified else "")
                    status, result = self.request(url, {"source": "L1.bspool", "recipe": {
                        "version": 1, "mode": "second_tag", "rule": {"version": 1,
                        "range": {"start": "A2S", "end": "A7B"}}}})
                    self.assertEqual(status, 400, result)
                    self.assertEqual(result["error_code"], "tag_coverage_missing")
                    self.assertEqual(result["missing_coverage"], [
                        {"tag": tag, "range": {"start": "A2S", "end": "A2B"}}
                        for tag in ("negative", "rare")])
                    self.assertEqual(result["rank"], 1)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
