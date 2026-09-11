#!/usr/bin/env python3
"""Browser-state regressions for the shared scoring workspace."""

from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import pool_score_ui as ui
import pool_rules_ui as rules_ui


SETUP = r'''
const assert=require('node:assert/strict'),nodes=new Map(),timers=new Map();
let nextTimer=1;
globalThis.setTimeout=callback=>{const id=nextTimer++;timers.set(id,callback);return id};
globalThis.clearTimeout=id=>timers.delete(id);
class Element{
 constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.textContent='';this.innerHTML='';this.className='';this.attributes={};this.parentNode=null}
 set id(value){this._id=value;nodes.set(value,this)}get id(){return this._id||''}
 append(...children){for(const child of children){child.parentNode=this;this.children.push(child)}}
 replaceChildren(...children){this.children=[];this.textContent='';this.innerHTML='';this.append(...children)}
 setAttribute(key,value){this.attributes[key]=String(value)}
 contains(node){return !!node&&(node===this||this.children.some(child=>child.contains(node)))}
 focus(){document.activeElement=this}scrollIntoView(){}
}
globalThis.document={activeElement:null,createElement:tag=>new Element(tag),getElementById:id=>nodes.get(id)||null};
globalThis.Option=function(text,value){const node=new Element('option');node.textContent=text;node.value=value;return node};
function $(id){if(!nodes.has(id)){const node=new Element();node.id=id}return nodes.get(id)}
const esc=value=>String(value==null?'':value).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[c]);
const fmt=value=>Number(value||0).toLocaleString('en-US');
let unified=false,api=async()=>{throw Error('Unexpected API call')};
const apiPath=path=>unified?'/organizer'+path:path;
const workflowState={pools:[]};
let loadPools=async()=>{},showMode=mode=>{$('scoreWorkspace').hidden=mode!=='score'};
const settle=async()=>{for(let i=0;i<20;i++)await Promise.resolve()};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject}};
function baseJob(overrides={}){return {job_id:'job-one',status:'completed',created_at:1700000000,updated_at:1700000001,total_records:3,completed_records:3,scored:3,no_valid_route:0,can_resume:false,error:'',request:{top:1000,workers:1},pools:[{pool_id:'p000',source:'L1.bspool',label:'L1',records:3,completed_records:3,scored:3,no_valid_route:0,status:'completed',second_tag:'A4B',second_tag_type:'rare',baseline_copy:'A4Boss'}],downloads:[],...overrides}}
$('scoreTop').value='1000';$('scoreWorkers').value='1';
'''


class Markup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.labels = []
        self.controls = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "label" and "for" in attrs:
            self.labels.append(attrs["for"])
        if tag in ("input", "select"):
            self.controls.append(attrs.get("id"))


class ScoreUIRegression(unittest.TestCase):
    def js(self, body):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for browser JavaScript checks")
        result = subprocess.run([node, "-"], text=True, capture_output=True,
                                input=SETUP + ui.SCRIPT + "\n(async()=>{\n" + body
                                + "\n})().catch(error=>{console.error(error);process.exitCode=1});\n",
                                timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)

    def test_markup_has_unique_ids_and_labeled_controls(self):
        parser = Markup()
        parser.feed(ui.WORKSPACE)
        self.assertEqual(len(parser.ids), len(set(parser.ids)))
        self.assertTrue(set(parser.controls) <= set(parser.labels))
        other = Markup()
        other.feed(rules_ui.WORKSPACE)
        self.assertFalse(set(parser.ids) & set(other.ids))
        self.assertIn('aria-labelledby="scoreModeBtn"', ui.WORKSPACE)
        self.assertIn('aria-label="Scoring leaderboard"', ui.WORKSPACE)
        self.assertIn("Score these pools", rules_ui.WORKSPACE)
        self.assertIn("scoreCreatedPools(ruleState.scoreSources", rules_ui.SCRIPT)
        self.assertNotIn("Python", ui.WORKSPACE)

    def test_per_pool_saved_second_tag_and_explicit_copy_do_not_mix(self):
        self.js(r'''
workflowState.pools=[{name:'L1.bspool',records:3},{name:'L2.bspool',records:7},{name:'L3.bspool',records:5}];
refreshScorePools(workflowState.pools);
api=async(path,data)=>{assert.equal(path,'/api/score/describe');return {source:data.source,second_tag:data.source==='L1.bspool'?{position:'A4B',tag:'rare'}:null,baseline_copy:data.source==='L1.bspool'?'A4Boss':null}};
for(const pool of workflowState.pools)scoreChoosePool(pool.name,true);
await settle();
assert.throws(scoreRequest,/Choose the second tag or first-copy shop for L2/);
Object.assign(scorePoolDraft('L2.bspool'),{mode:'second',second_tag:'A5S',second_tag_type:'negative'});
Object.assign(scorePoolDraft('L3.bspool'),{mode:'copy',baseline_copy:'A7Boss'});
$('scoreReference').value='721.77';$('scoreWorkers').value='4';$('scoreRecordMissing').checked=true;
assert.deepEqual(scoreRequest(),{pools:[{source:'L1.bspool'},{source:'L2.bspool',second_tag:'A5S',second_tag_type:'negative'},{source:'L3.bspool',baseline_copy:'A7Boss'}],workers:4,top:1000,record_missing:true,export_all:false,reference_score:721.77});
assert.equal(scoreFirstCopy('A5S'),'A5B');assert.equal(scoreFirstCopy('A5B'),'A5Boss');
scoreChoosePool('L2.bspool',false);scoreChoosePool('L2.bspool',true);
assert.equal(scorePoolDraft('L2.bspool').second_tag,'A5S');
$('scoreTop').value='1.5';assert.throws(scoreRequest,/whole number/);$('scoreTop').value='1000';
$('scoreWorkers').value='17';assert.throws(scoreRequest,/from 1 to 16/);$('scoreWorkers').value='1';
$('scoreReference').value='Infinity';assert.throws(scoreRequest,/Reference score/);
''')

    def test_source_refresh_rejects_stale_description_and_removes_unavailable_selection(self):
        self.js(r'''
const old=deferred(),fresh=deferred();let calls=0;
api=()=>++calls===1?old.promise:fresh.promise;
workflowState.pools=[{name:'L1.bspool',records:3,snapshot_id:'old'}];refreshScorePools(workflowState.pools);scoreChoosePool('L1.bspool',true);await settle();
workflowState.pools=[{name:'L1.bspool',records:4,snapshot_id:'new'}];refreshScorePools(workflowState.pools);
old.resolve({second_tag:{position:'A4B'}});await settle();
assert.equal(calls,2);assert.equal(scoreState.descriptions.has('L1.bspool'),false);
fresh.resolve({second_tag:{position:'A6S'}});await settle();
assert.equal(scoreSavedStart(scoreState.descriptions.get('L1.bspool')).copy,'A6B');
refreshScorePools([{name:'L1.bspool',records:4,complete:false}]);
assert.equal(scoreState.selected.size,0);assert.equal($('scoreStartBtn').disabled,true);
assert.match($('scoreSetupError').textContent,/unavailable/);
''')

    def test_description_concurrency_and_selection_limit_are_bounded(self):
        self.js(r'''
const waits=[];api=()=>{const wait=deferred();waits.push(wait);return wait.promise};
workflowState.pools=Array.from({length:40},(_,i)=>({name:`L${i}.bspool`,records:1}));refreshScorePools(workflowState.pools);
$('scoreSelectVisible').onclick();await settle();
assert.equal(scoreState.selected.size,32);assert.equal(waits.length,3);assert.equal(scoreState.descriptionActive,3);
waits[0].resolve({second_tag:{position:'A4B'}});await settle();assert.equal(waits.length,4);
assert.match($('scoreSetupError').textContent,/32 pools/);
''')

    def test_start_is_single_submission_and_keeps_per_pool_request(self):
        self.js(r'''
workflowState.pools=[{name:'L1.bspool',records:3}];refreshScorePools(workflowState.pools);
scoreState.descriptions.set('L1.bspool',{second_tag:{position:'A4B'}});scoreChoosePool('L1.bspool',true);
const start=deferred();let posts=0;
api=async(path,data)=>{if(path==='/api/score/start'){posts++;assert.deepEqual(data.pools,[{source:'L1.bspool'}]);return start.promise}if(path.startsWith('/api/score/results?'))return {total:0,offset:0,rows:[]};if(path==='/api/score/jobs')return {jobs:[baseJob()]};throw Error(path)};
const a=startScoreRun(),b=startScoreRun();assert.equal(posts,1);assert.equal($('scoreInputs').disabled,true);
start.resolve(baseJob());await Promise.all([a,b]);await settle();
assert.equal(posts,1);assert.equal(scoreState.jobId,'job-one');assert.equal($('scoreInputs').disabled,false);
''')

    def test_stale_job_and_result_responses_cannot_replace_selected_run(self):
        self.js(r'''
const first=deferred();api=async path=>{if(path.includes('job_id=old'))return first.promise;if(path.includes('/status?'))return baseJob({job_id:'new'});return {total:1,offset:0,rows:[{seed:'NEWSEED',score:12,position:1,hieroglyph_before_ante:12,petroglyph_before_ante:39}]}};
const old=selectScoreJob('old');await selectScoreJob('new');first.resolve(baseJob({job_id:'old'}));await old;
assert.equal(scoreState.jobId,'new');assert.equal(scoreState.job.job_id,'new');assert.match($('scoreTableBody').innerHTML,/NEWSEED/);
const stale=deferred();api=path=>path.includes('scope=p000')?stale.promise:Promise.resolve({total:1,offset:0,rows:[{seed:'COMBINED',score:9}]});
scoreState.resultPool='p000';const perPool=refreshScoreResults();scoreState.resultPool='';await refreshScoreResults();stale.resolve({total:1,offset:0,rows:[{seed:'STALEPOOL',score:99}]});await perPool;
assert.match($('scoreTableBody').innerHTML,/COMBINED/);assert.doesNotMatch($('scoreTableBody').innerHTML,/STALEPOOL/);
''')

    def test_interrupted_run_can_resume_and_active_results_are_not_claimed_final(self):
        self.js(r'''
const interrupted=baseJob({status:'interrupted',completed_records:1,scored:1,can_resume:true});
api=async(path,data)=>{if(path==='/api/score/resume'){assert.deepEqual(data,{job_id:'job-one'});return baseJob({status:'running',completed_records:1,scored:1,can_resume:false})}return {total:0,offset:0,rows:[]}};
await selectScoreJob('job-one',interrupted);
assert.equal($('scoreResumeBtn').hidden,false);assert.match($('scoreResultsStatus').textContent,/Resume to finish/);
await changeScoreRun('resume');assert.equal($('scoreCancelBtn').hidden,false);assert.equal($('scoreResumeBtn').hidden,true);
assert.match($('scoreResultsStatus').textContent,/after all seeds and source data are checked/);
assert.equal(timers.size,1);
api=async(path,data)=>{if(path==='/api/score/cancel')return baseJob({status:'cancelling'});return {total:0,offset:0,rows:[]}};
await changeScoreRun('cancel');assert.equal($('scoreCancelBtn').disabled,true);assert.match($('scoreRunStatus').textContent,/saving progress/);
''')

    def test_table_escapes_metadata_and_keeps_large_details_optional(self):
        self.js(r'''
scoreState.job=baseJob({request:{reference_score:10}});scoreState.result={total:1,offset:0,rows:[{position:1,seed:'<script>evil</script>',score:12.345,pool_label:'<img src=x>',baseline_copy:'A4Boss',second_tag:'A4B',first_tag_type:'both',second_tag_type:'rare',hieroglyph_before_ante:12,petroglyph_before_ante:39,route:[{neg_label:'<svg>',rare_label:'Rare A7B',redeem_shop_desc:'A7 Boss',final:true}]}]};
renderScoreTable();assert.match($('scoreTableBody').innerHTML,/&lt;script&gt;/);assert.doesNotMatch($('scoreTableBody').innerHTML,/<script>|<img|Show route/);
assert.match($('scoreTableBody').innerHTML,/12\.35/);assert.match($('scoreTableBody').innerHTML,/Before original A12/);assert.match($('scoreTableBody').innerHTML,/After original A38/);
for(const id of ['scoreColumnPool','scoreColumnCopy','scoreColumnTags','scoreColumnRoute'])$(id).checked=true;
renderScoreTable();assert.match($('scoreTableBody').innerHTML,/Both \(same Ante\)/);assert.match($('scoreTableBody').innerHTML,/&lt;img src=x&gt;/);assert.match($('scoreTableBody').innerHTML,/&lt;svg&gt;/);assert.match($('scoreTableBody').innerHTML,/Above reference/);
''')

    def test_finalizing_keeps_polling_until_leaderboard_is_published(self):
        self.js(r'''
api=async path=>path.includes('/status?')?baseJob({status:'finalizing',phase:'leaderboards'}):{total:0,offset:0,rows:[]};
await selectScoreJob('job-one');
assert.equal(timers.size,1);
assert.equal($('scoreCancelBtn').hidden,false);
assert.equal($('scoreStartBtn').disabled,true);
assert.match($('scoreRunStatus').textContent,/Saving leaderboards/);
assert.match($('scoreResultsStatus').textContent,/after all seeds and source data are checked/);
api=async path=>path.includes('/status?')?baseJob():{total:1,offset:0,rows:[{seed:'WINNER',score:12}]};
await pollScoreJob();
assert.equal(timers.size,0);
assert.match($('scoreTableBody').innerHTML,/WINNER/);
assert.match($('scoreResultsStatus').textContent,/Final results/);
''')

    def test_failed_cancel_request_keeps_checking_actual_job_status(self):
        self.js(r'''
scoreState.jobId='job-one';renderScoreJob(baseJob({status:'running'}));
api=async()=>{throw Error('connection lost')};
await changeScoreRun('cancel');
assert.equal(scoreState.job.status,'running');
assert.equal(timers.size,1);
assert.equal($('scoreRetryStatus').hidden,false);
assert.match($('scoreRunError').textContent,/connection lost/);
api=async path=>path.includes('/status?')?baseJob({status:'interrupted',can_resume:true}):{total:0,offset:0,rows:[]};
await pollScoreJob();
assert.equal($('scoreResumeBtn').hidden,false);
assert.equal(timers.size,0);
''')

    def test_pagination_and_downloads_use_server_scope_and_filename(self):
        self.js(r'''
unified=true;scoreState.jobId='job & one';scoreState.job=baseJob({job_id:'job & one',downloads:[{kind:'leaderboard_csv',label:'CSV',filename:'combined.csv'},{kind:'leaderboard_csv',label:'Pool CSV',filename:'p000.csv',pool_id:'p000'},{kind:'summary',label:'Summary',filename:'summary.json'}]});
scoreState.resultPool='p000';scoreState.offset=50;
api=async path=>{const query=new URL('http://local'+path).searchParams;assert.equal(query.get('job_id'),'job & one');assert.equal(query.get('scope'),'p000');assert.equal(query.get('offset'),'50');assert.equal(query.get('limit'),'50');return {total:120,offset:50,rows:Array.from({length:50},(_,i)=>({position:i+51,seed:'SEED'+i,score:1}))}};
await refreshScoreResults();assert.equal($('scorePrevPage').disabled,false);assert.equal($('scoreNextPage').disabled,false);assert.equal($('scorePageLabel').textContent,'51–100 of 120');
const links=$('scoreDownloads').children;assert.equal(links.length,2);assert.ok(links.every(link=>link.href.startsWith('/organizer/api/score/download?')));
assert.deepEqual(links.map(link=>new URL('http://local'+link.href).searchParams.get('filename')),['p000.csv','summary.json']);
''')

    def test_network_status_failure_preserves_job_and_does_not_claim_cancellation(self):
        self.js(r'''
scoreState.jobId='job-one';renderScoreJob(baseJob({status:'running'}));api=async()=>{throw Error('connection lost')};await pollScoreJob();
assert.equal(scoreState.job.status,'running');assert.match($('scoreRunError').textContent,/may still be working/);assert.equal($('scoreRetryStatus').hidden,false);assert.equal(timers.size,1);
''')

    def test_late_status_cannot_undo_cancel_or_newer_status(self):
        self.js(r'''
scoreState.jobId='job-one';renderScoreJob(baseJob({status:'running'}));
const old=deferred();api=async(path)=>path.includes('/status?')?old.promise:path.includes('/cancel')?baseJob({status:'cancelling'}):{total:0,offset:0,rows:[]};
const polling=pollScoreJob();await changeScoreRun('cancel');old.resolve(baseJob({status:'running'}));await polling;
assert.equal(scoreState.job.status,'cancelling');assert.equal($('scoreCancelBtn').disabled,true);
const stale=deferred();let calls=0;api=async path=>path.includes('/status?')?(++calls===1?stale.promise:baseJob({status:'completed'})):{total:0,offset:0,rows:[]};
const a=pollScoreJob(),b=pollScoreJob();await b;stale.resolve(baseJob({status:'running'}));await a;
assert.equal(scoreState.job.status,'completed');assert.equal(timers.size,0);
''')

    def test_keyboard_focus_survives_starting_point_changes(self):
        self.js(r'''
workflowState.pools=[{name:'L1.bspool',records:3}];refreshScorePools(workflowState.pools);
scoreState.descriptions.set('L1.bspool',{second_tag:null});scoreChoosePool('L1.bspool',true);
const mode=document.getElementById('scorePool0Mode');mode.focus();mode.value='second';mode.onchange();
assert.equal(document.activeElement,document.getElementById('scorePool0Mode'));
const second=document.getElementById('scorePool0Second');second.focus();second.value='A4S';second.onchange();
assert.equal(document.activeElement,document.getElementById('scorePool0Second'));
assert.equal(scoreRequest().pools[0].second_tag,'A4S');
''')

    def test_contextual_handoff_selects_outputs_without_combining(self):
        self.js(r'''
let mode='';showMode=value=>{mode=value};workflowState.pools=[{name:'old.bspool',records:1}];refreshScorePools(workflowState.pools);
loadPools=async()=>{workflowState.pools=[{name:'new-one.bspool',records:2},{name:'new-two.bspool',records:3},{name:'old.bspool',records:1}]};
api=async(path,data)=>{assert.equal(path,'/api/score/describe');return {second_tag:{position:data.source==='new-one.bspool'?'A4S':'A6B'}}};
await scoreCreatedPools(['new-one.bspool','new-two.bspool']);await settle();
assert.equal(mode,'score');assert.deepEqual([...scoreState.selected.keys()],['new-one.bspool','new-two.bspool']);
assert.deepEqual(scoreRequest().pools,[{source:'new-one.bspool'},{source:'new-two.bspool'}]);
''')


if __name__ == "__main__":
    unittest.main()
