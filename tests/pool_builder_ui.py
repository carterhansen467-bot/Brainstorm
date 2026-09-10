#!/usr/bin/env python3
"""Builder requirement fidelity and browser workflow regressions."""

import json
import os
import shutil
import subprocess
import sys
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import pool_builder_web as web


class Snapshot:
    def usable_legendaries(self):
        return ["j_perkeo"]

    def usable_tags(self):
        return [("tag_negative", 1), ("tag_rare", 1)]

    def usable_vouchers(self):
        return [("v_overstock_norm", "")]


class Labels(HTMLParser):
    """Collect actual browser controls and their visible label associations."""

    def __init__(self, html):
        super().__init__()
        self.label_depth = 0
        self.label_targets = set()
        self.controls = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "label":
            self.label_depth += 1
            if attrs.get("for"):
                self.label_targets.add(attrs["for"])
        if tag in ("input", "select"):
            self.controls.append((attrs, self.label_depth > 0))

    def handle_endtag(self, tag):
        if tag == "label":
            self.label_depth -= 1

    def unlabelled(self):
        return [attrs for attrs, wrapped in self.controls
                if not wrapped and not attrs.get("aria-label")
                and attrs.get("id") not in self.label_targets]


class BuilderUI(unittest.TestCase):
    def test_requested_tag_windows_and_counts_are_preserved(self):
        criteria = web.criteria_from_json({"rules": [{
            "key": "tag_negative", "min": 20, "minPhase": "big",
            "max": 39, "maxPhase": "small", "count": 25,
        }]}, Snapshot())
        self.assertIn("tag tag_negative 20 big 39 small 25\n",
                      criteria.text("binary", 100))

    def test_inactive_legendary_controls_do_not_block_a_tag_search(self):
        criteria = web.criteria_from_json({
            "legendary": "", "legMin": 3, "legMax": 3,
            "legMinPhase": "boss", "legMaxPhase": "small",
            "rules": [{"key": "tag_rare"}],
        }, Snapshot())
        self.assertEqual(criteria.legendary, "")
        self.assertEqual(len(criteria.tag_rules), 1)

    def test_unrepresentable_requirements_are_rejected(self):
        invalid_rules = [
            {"min": 7, "max": 6},
            {"min": 0, "max": 6},
            {"min": 1, "max": 40},
            {"min": 1, "max": 2, "count": 5},
            {"min": 1, "max": 2, "count": 1.5},
            {"min": 1, "max": 2, "count": True},
        ]
        for rule in invalid_rules:
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                web.criteria_from_json({"rules": [
                    {"key": "tag_rare", **rule}]}, Snapshot())
        with self.assertRaisesRegex(ValueError, "At most 16"):
            web.criteria_from_json({"rules": [
                {"key": "tag_rare"}] * 17}, Snapshot())
        with self.assertRaisesRegex(ValueError, "Voucher end Ante"):
            web.criteria_from_json({"voucherRules": [{
                "key": "v_overstock_norm", "min": 1, "max": 9,
            }]}, Snapshot())

    def test_static_controls_have_labels(self):
        self.assertEqual(Labels(web.PAGE.split("<script>", 1)[0]).unlabelled(), [])

    def test_browser_workflows(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for browser JavaScript checks")
        script = web.PAGE.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        harness = r'''
const assert = require("node:assert/strict");
const nodes = new Map();
function el(id) {
  if (!nodes.has(id)) nodes.set(id, {
    value:"", textContent:"", disabled:false, dataset:{}, options:[],
    selectedOptions:[], focus(){}, appendChild(){}, querySelector(){return null;}
  });
  return nodes.get(id);
}
globalThis.document = {getElementById:el, querySelectorAll:()=>[]};
// Switching to a saved pool explains the actual full-pool scope; switching
// back restores the user's chosen test-build size.
el("count").value = "100000000";
el("inputPool").value = "AS1-Complete.bspool";
el("inputPool").selectedOptions = [{dataset:{space:"total",composite:"1"}}];
syncSourceControls(); updateSpaceHint();
assert.equal(el("count").value,"0");
assert.equal(el("count").disabled,true);
assert.equal(el("space").value,"total");
assert.equal(el("optAll").textContent,"All saved seeds in this pool");
syncSourceControls(); // status polls must not replace the remembered scope
el("inputPool").value = "";
syncSourceControls(); updateSpaceHint();
assert.equal(el("count").value,"100000000");
assert.equal(el("count").disabled,false);

// Failures from ordinary library actions are visible without opening the
// unrelated distributed-merge panel or starting a scan.
globalThis.fetch = async()=>{throw new Error("fixture request failed");};
await deletePool("missing.bspool");
assert.match(el("libraryError").textContent,/fixture request failed/);
assert.equal(el("mergeError").textContent,"");
el("libraryError").textContent="";
await changePoolAttachment("missing.bspool","accelerator");
assert.match(el("libraryError").textContent,/fixture request failed/);
await stopJob();
assert.match(el("error").textContent,/Could not pause/);
assert.equal(el("btnStop").disabled,false);

// File and attachment controls remain available behind disclosures, while
// search is offered only for an eligible input pool. Names remain escaped.
const pool={name:'AS1 <test>.bspool',records:12,bytes:1024,criteria:[],
  complete:true,coverage_complete:true,refilter_eligible:true,
  attachment_accelerator_eligible:true,attachment_authoritative_eligible:true};
const card=renderPoolCard(pool,new Set(),false);
assert.match(card,/AS1 &lt;test&gt;\.bspool/);
assert.match(card,/Search this pool/);
assert.match(card,/<details[^>]*><summary>File details<\/summary>/);
assert.match(card,/data-role="accelerator"/);
assert.match(card,/data-role="authoritative"/);
assert.doesNotMatch(renderPoolCard({...pool,refilter_eligible:false},new Set(),false),/pool-search/);

// Exercise actual dynamic-control templates, including the tag-rule limit.
const dynamic=[];
let tagCount=0;
document.createElement=()=>({innerHTML:"",querySelector:()=>({focus(){}})});
document.querySelectorAll=selector=>selector==="#rules .rule"?Array(tagCount):[];
for(const id of ["rules","voucherRules","voucherExclusions"])
  el(id).appendChild=div=>{dynamic.push(div.innerHTML);if(id==="rules")tagCount++;};
CAT={tags:[{key:"tag_rare",name:"Rare Tag"}],vouchers:[{key:"v_overstock_norm",name:"Overstock"}]};
updateSummary=()=>{};
addRule("tag_rare");
addVoucherRule("v_overstock_norm");
el("voucherRules").querySelector=()=>({});
addVoucherExclusion("v_overstock_norm");
tagCount=16;
const prior=dynamic.length;
addRule("tag_rare");
refreshTagButton();
assert.equal(dynamic.length,prior);
assert.equal(el("btnAddTag").disabled,true);
console.log(JSON.stringify(dynamic));
'''
        result = subprocess.run(
            [node, "-"], input="globalThis.addEventListener=()=>{};\n" + script
            + "\n(async()=>{\n" + harness
            + "\n})().catch(e=>{console.error(e);process.exitCode=1;});\n",
            text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        dynamic = json.loads(result.stdout)
        self.assertEqual(len(dynamic), 3)
        for fragment in dynamic:
            self.assertEqual(Labels(fragment).unlabelled(), [])


if __name__ == "__main__":
    unittest.main()
