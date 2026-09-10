"""Organizer presentation for source recovery and saved tag rules.

Kept separate from the legacy location-split editor so both workflows have
their own state. All decisions and publication are validated by the backend.
"""

STYLE = r'''
.hint{color:#aaa6bc}.rule-layout{max-width:1050px}.rule-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.rule-fields .field{margin:0}.rule-explanation{line-height:1.65;color:var(--muted);font-size:13px}
.rule-explanation ol{padding-left:21px;margin:8px 0}.rule-explanation li{margin:5px 0}
.rule-sources{display:grid;gap:8px;margin-top:14px;max-height:380px;overflow:auto}
.rule-sources .choicecard{align-items:center}.rule-sources .choicecard>span{flex:1}
.rule-sources .count{flex:0 0 auto}.rule-toolbar{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}
.rule-condition{border-left:2px solid #7763ae;padding:10px 0 10px 14px;margin:12px 0}
.rule-condition .condition-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.rule-condition select,.rule-condition input{width:auto;max-width:100%}
.rule-condition .condition-count{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:10px 0}
.rule-condition .condition-count input,.rule-condition .condition-count select{width:100%;min-width:0}
.rule-condition input[type=number]{padding:10px;border:1px solid #3c4155;border-radius:8px;background:#0d1019;color:#eeeaf6;font:inherit}.rule-condition label{font-size:12px;color:var(--muted)}.rule-condition .condition-not{display:flex;align-items:center;gap:5px}
.rule-condition .condition-not input{width:16px;height:16px}.rule-empty{padding:14px;color:var(--muted);border:1px dashed #41455b;border-radius:10px}
.rule-status{min-height:26px;margin-top:12px;color:var(--muted);font-size:13px;line-height:1.5}
.rule-status:empty{display:none}.rule-coverage{font-size:12px;color:var(--muted);line-height:1.6}
.rule-coverage code{white-space:normal}.rule-outputs{max-height:450px;overflow:auto}
.rule-help{max-width:75ch}.rule-counts{display:flex;gap:20px;flex-wrap:wrap;margin:14px 0}.rule-counts b{display:block;font-size:20px}.rule-counts span{font-size:12px;color:var(--muted)}
.toolnav{flex-wrap:wrap}.toolnav button{white-space:normal}button:focus-visible,select:focus-visible,input:focus-visible,summary:focus-visible{outline:3px solid #b7a3f4;outline-offset:3px}
@media(max-width:680px){.rule-fields,.rule-condition .condition-count{grid-template-columns:1fr}.rule-condition{padding-left:9px}.toolnav{display:grid;grid-template-columns:repeat(2,minmax(0,1fr))}.rule-sources .choicecard{flex-wrap:wrap}}
@media(prefers-reduced-motion:reduce){.spinner{animation:none}*{scroll-behavior:auto!important}}
'''

WORKSPACE = r'''
<div id="rulesWorkspace" class="stack rule-layout" role="tabpanel" aria-labelledby="restoreModeBtn" hidden>
 <section class="card">
  <div class="head"><span class="step">1</span><div><h2 id="ruleTitle">Separate original pools</h2><p class="copy rule-help" id="ruleIntro">Recover saved groups such as L1 and L2 from a combined Complete pool.</p></div></div>
  <fieldset id="ruleInputs" class="choicegroup">
   <div class="rule-fields"><div class="field"><label for="ruleSource">Source pool</label><select id="ruleSource"><option value="">Loading pools…</option></select></div>
   <div class="field"><label for="rulePrefix">Output name prefix</label><input id="rulePrefix" type="text" maxlength="80" placeholder="For example, AS1"><span class="hint">Each destination adds its group or second-tag location to this name.</span></div></div>
   <div class="rule-toolbar"><button id="ruleDetectBtn">Find original pools</button><button class="ghost" id="ruleRefreshBtn">Refresh list</button></div>
   <div id="restoreSettings">
    <p class="hint rule-help">Membership is read from this file, even if the input files were deleted. A seed that belonged to two selected groups is copied to both.</p>
    <div id="ruleSourceGroups" hidden>
     <div class="field"><label for="ruleSourceKind">Which saved groups?</label><select id="ruleSourceKind"><option value="inputs">Inputs to the latest combine</option><option value="branches">Earlier recorded source groups</option></select></div>
     <p class="hint" id="ruleHistoryHint"></p>
     <div class="rule-toolbar"><button class="small ghost" id="ruleSelectAll">Select all</button><button class="small ghost" id="ruleSelectNone">Clear selection</button></div>
     <div class="rule-sources" id="ruleSources"></div>
    </div>
   </div>
   <div id="tagSettings" hidden>
    <div class="rule-fields"><div class="field"><label for="ruleStart">Start checking at</label><select id="ruleStart"></select></div><div class="field"><label for="ruleEnd">Stop checking at</label><select id="ruleEnd"></select></div></div>
    <p class="hint">Both endpoints are included. Set the range for this pool; L1, L2, and Other can each use a different range.</p>
    <div class="rule-explanation rule-help"><strong>How the second tag is chosen</strong><ol>
     <li>Find the first Ante with a Negative or Rare tag in the range.</li>
     <li>If only one type appears in that Ante, choose the next tag of the opposite type.</li>
     <li>If both appear in that Ante, choose the first Negative or Rare in a later Ante.</li>
    </ol>Each new pool is named for the chosen second tag, such as A5 Small Rare. Seeds with no qualifying second tag are left out of the new pools.</div>
    <details class="advanced"><summary>Optional tag conditions</summary><div class="advancedbody">
     <p class="hint rule-help">Apply extra count and range conditions before choosing the second tag. Use All for AND, Any for OR, and Not to reverse a condition.</p>
     <div id="ruleConditionEditor"></div><button class="small" id="ruleAddConditions">Add conditions</button>
    </div></details>
    <details class="advanced"><summary>Save or load rules</summary><div class="advancedbody">
     <div class="field"><label for="ruleName">Rule name</label><input id="ruleName" type="text" maxlength="160" value="Second Negative / Rare tag"></div>
     <p class="hint">Settings are kept separately for each pool while this page is open. Save a rules file to reuse them later or on another computer.</p>
     <div class="rule-toolbar"><button id="ruleSaveBtn" class="ghost">Save rules</button><button id="ruleLoadBtn" class="ghost">Load rules</button><input type="file" id="ruleLoadFile" accept="application/json,.json" hidden></div>
    </div></details>
   </div>
   <details class="advanced" id="ruleDataDetails" hidden><summary>Available source data</summary><div id="ruleData" class="advancedbody rule-coverage"></div></details>
  </fieldset>
  <div class="rule-toolbar"><button id="rulePreviewBtn" class="go">Preview new pools</button><button id="ruleCancelBtn" class="cancel" hidden>Cancel</button></div>
  <div class="rule-status" id="ruleStatus" role="status" aria-live="polite"></div><div class="error" id="ruleError" role="alert"></div>
 </section>
 <section class="card" id="ruleReview" hidden>
  <div class="head"><span class="step">2</span><div><h2>Review and create</h2><p class="copy">These files will be created alongside your source pool. Existing files are never replaced.</p></div></div>
  <div class="rule-counts" id="ruleCounts"></div><div id="ruleExclusions" class="hint"></div>
  <div class="manifest rule-outputs" id="ruleManifest"></div>
  <div class="rule-toolbar"><button class="go" id="ruleCreateBtn">Create these pools</button></div>
 </section>
 <section class="card" id="ruleDone" hidden><h2>Pools created</h2><div class="result" id="ruleResults"></div><div class="rule-toolbar"><button id="ruleNextBtn">Sort a new pool by second tag</button></div></section>
</div>
'''

SCRIPT = r'''
const ruleState={mode:"restore",description:null,plan:null,busy:false,condition:null,revision:0,drafts:{},activeSource:"",runId:0};
const rulePositions=[];
for(let a=1;a<=39;a++)for(const b of ["S","B"])rulePositions.push({value:`A${a}${b}`,label:`Ante ${a} ${b==="S"?"Small":"Big"}`});
function rulePositionOptions(value){return rulePositions.map(p=>`<option value="${p.value}"${value===p.value?" selected":""}>${p.label}</option>`).join("")}
$("ruleStart").innerHTML=rulePositionOptions("A3S");$("ruleEnd").innerHTML=rulePositionOptions("A7B");
function ruleLoadPools(){
 const picker=$("ruleSource"),prior=picker.value;
 picker.innerHTML=workflowState.pools.length?workflowState.pools.map(p=>`<option value="${esc(p.name)}"${p.error?" disabled":""}>${esc(p.name)} · ${fmt(p.records)} seeds${p.error?" · unreadable":""}</option>`).join(""):'<option value="">No pools available</option>';
 if([...picker.options].some(p=>p.value===prior))picker.value=prior;
 if(prior&&picker.value!==prior)ruleSourceChanged();
 $("rulePreviewBtn").disabled=ruleState.busy||!picker.value;$("ruleDetectBtn").disabled=ruleState.busy||!picker.value;
 if(!$("rulePrefix").value)$("rulePrefix").value=picker.value.replace(/\.bspool$/i,"").slice(0,65)+(ruleState.mode==="restore"?"-separated":"-sorted");
}
function ruleInvalidate(){ruleState.revision++;ruleState.plan=null;$("ruleReview").hidden=true;$("ruleError").textContent="";$("ruleDone").hidden=true;}
function rememberTagDraft(){if(ruleState.mode==="tag"&&ruleState.activeSource)ruleState.drafts[ruleState.activeSource]=readTagRule()}
function restoreTagDraft(){const rule=ruleState.drafts[$("ruleSource").value]||{range:{start:"A3S",end:"A7B"},name:"Second Negative / Rare tag"};$("ruleStart").value=rule.range.start;$("ruleEnd").value=rule.range.end;$("ruleName").value=rule.name;ruleState.condition=rule.condition?JSON.parse(JSON.stringify(rule.condition)):null;renderRuleCondition()}
function ruleSourceChanged(){rememberTagDraft();ruleInvalidate();ruleState.description=null;$("ruleSourceGroups").hidden=true;$("ruleDataDetails").hidden=true;$("ruleStatus").textContent="";$("rulePrefix").value=$("ruleSource").value.replace(/\.bspool$/i,"").slice(0,65)+(ruleState.mode==="restore"?"-separated":"-sorted");ruleState.activeSource=$("ruleSource").value;if(ruleState.mode==="tag")restoreTagDraft()}
function setRuleMode(mode){
 const changed=ruleState.mode!==mode;if(changed){rememberTagDraft();ruleInvalidate()}ruleState.mode=mode;
 const restoring=mode==="restore";$("rulesWorkspace").setAttribute("aria-labelledby",restoring?"restoreModeBtn":"tagModeBtn");
 $("ruleTitle").textContent=restoring?"Separate original pools":"Sort by second tag";
 $("ruleIntro").textContent=restoring?"Recover saved groups such as L1 and L2 from a combined Complete pool.":"Choose a separated pool, set its range, and create pools by the second Negative or Rare tag.";
 $("restoreSettings").hidden=!restoring;$("tagSettings").hidden=restoring;
 $("ruleDetectBtn").textContent=restoring?"Find original pools":"Check recorded data";
 $("rulePreviewBtn").textContent="Preview new pools";ruleLoadPools();ruleState.activeSource=$("ruleSource").value;if(changed)$("rulePrefix").value=$("ruleSource").value.replace(/\.bspool$/i,"").slice(0,65)+(mode==="restore"?"-separated":"-sorted");if(mode==="tag"&&changed)restoreTagDraft();
}
function renderRuleSources(){
 const d=ruleState.description,kind=$("ruleSourceKind").value;if(!d)return;
 const rows=kind==="inputs"?d.direct_inputs:d.original_sources;
 $("ruleSources").innerHTML=(rows||[]).length?rows.map(r=>`<label class="choicecard"><input class="rule-origin" type="checkbox" value="${esc(r.id)}" checked><span><b>${esc(r.label||r.pool_id||r.id)}</b></span><span class="count">${fmt(r.records)} seeds</span></label>`).join(""):'<p class="rule-empty">No groups of this kind are recorded. Try the other grouping, or select a combined pool.</p>';
 $("ruleHistoryHint").textContent=kind==="inputs"?"These are the pools used in the most recent combine. Counts describe seeds still present in this file.":"These original source groups were retained through combines. In a simple combine, they may match the latest inputs. Intermediate groups that were not retained cannot be recovered.";
 document.querySelectorAll(".rule-origin").forEach(x=>x.onchange=ruleInvalidate);
}
function renderRuleData(d){
 $("ruleDataDetails").hidden=false;
 const source=d.source||{};
 $("ruleData").innerHTML=`<p>${fmt(source.records)} recorded seeds. ${source.complete?"Finished pool.":"Only the saved portion of this pool is available."}</p><p>Tag rules require recorded Negative and Rare placements throughout every range they check. Missing data is reported before any files are created.</p>`;
 const rows=d.direct_inputs||[];
 if(rows.length)$("ruleData").innerHTML+=`<p>Latest inputs: ${rows.map(r=>esc(r.label||r.id)).join(", ")}.</p>`;
 const coverage=d.coverage;
 if(coverage){if(!coverage.metadata_complete)$("ruleData").innerHTML+='<p>Some placements were not recorded. This pool cannot prove complete tag counts.</p>';
 else for(const row of coverage.sources||[])$("ruleData").innerHTML+=`<p><strong>${esc(row.label||"Recorded tag windows")}</strong><br>${["negative","rare"].map(tag=>`${tag==="negative"?"Negative":"Rare"}: ${(row.tags?.[tag]||[]).map(r=>`${esc(r.start)} through ${esc(r.end)}`).join(", ")||"not recorded"}`).join("<br>")}</p>`;}
 if(d.notices)for(const note of d.notices)$("ruleData").innerHTML+=`<p>${esc(typeof note==="string"?note:note.text||note.message||"")}</p>`;
}
async function ruleRun(message,callback){
 if(ruleState.busy)return;const runId=++ruleState.runId;ruleState.busy=true;$("ruleInputs").disabled=true;$("rulePreviewBtn").disabled=true;$("ruleCreateBtn").disabled=true;$("ruleCancelBtn").hidden=false;$("ruleCancelBtn").disabled=false;$("ruleError").textContent="";$("ruleStatus").textContent=message;
 const started=performance.now();
 const timer=setInterval(async()=>{try{const p=await api("/api/progress?operation=rules");if(ruleState.busy&&runId===ruleState.runId&&p.state==="running")$("ruleStatus").textContent=`${message} ${p.records_total?`${fmt(p.records_done)} of ${fmt(p.records_total)} seeds · `:""}${fmtDuration((performance.now()-started)/1000)} elapsed.`}catch(_e){}},700);
 try{return await callback()}catch(e){$("ruleError").textContent=e.message||String(e);$("ruleStatus").textContent=e.code==="operation_cancelled"?"Cancelled.":"Stopped. Review the message above before trying again.";ruleState.plan=null;$("ruleReview").hidden=true}
 finally{clearInterval(timer);ruleState.busy=false;$("ruleInputs").disabled=false;$("rulePreviewBtn").disabled=!$("ruleSource").value;$("ruleCreateBtn").disabled=!ruleState.plan||ruleState.plan.can_create===false;$("ruleCancelBtn").hidden=true;$("ruleDetectBtn").disabled=!$("ruleSource").value;}
}
async function detectRuleSources(){
 ruleInvalidate();return ruleRun("Reading saved pool data…",async()=>{
  const d=await api("/api/rules/describe",{source:$("ruleSource").value});ruleState.description=d;$("ruleSourceGroups").hidden=false;
  if(!(d.direct_inputs||[]).length&&(d.original_sources||[]).length)$("ruleSourceKind").value="branches";
  renderRuleSources();renderRuleData(d);$("ruleStatus").textContent=`Read ${fmt((d.source||{}).records)} seeds. Choose the groups or range to use.`;return d;
 });
}
function defaultRuleCount(){return {count:{tag:"negative",range:{start:$("ruleStart").value,end:$("ruleEnd").value},min:1}}}
function conditionNode(value,remove,depth=0){
 const outer=document.createElement("div");outer.className="rule-condition";
 let negated=false,inner=value;while(inner.not){negated=!negated;inner=inner.not;}
 const head=document.createElement("div");head.className="condition-head";outer.append(head);
 const notLabel=document.createElement("label");notLabel.className="condition-not";const not=document.createElement("input");not.type="checkbox";not.checked=negated;notLabel.append(not,document.createTextNode("Not"));head.append(notLabel);
  const commit=()=>{if(not.checked){delete value.all;delete value.any;delete value.count;value.not=inner}else{delete value.not;delete value.all;delete value.any;delete value.count;Object.assign(value,inner)}ruleInvalidate()};
 // Keep the editable inner value separate from its wrapper.
 inner=JSON.parse(JSON.stringify(inner));not.onchange=commit;
 if(inner.count){
  const label=document.createElement("strong");label.textContent="Tag count";head.append(label);
  const fields=document.createElement("div");fields.className="condition-count";outer.append(fields);
  const addField=(title,control)=>{const label=document.createElement("label");label.append(document.createTextNode(title),control);fields.append(label)};
  const tag=document.createElement("select");tag.innerHTML='<option value="negative">Negative</option><option value="rare">Rare</option>';tag.value=inner.count.tag;tag.onchange=()=>{inner.count.tag=tag.value;commit()};addField("Tag",tag);
  for(const [key,title] of [["min","At least"],["max","At most (optional)"]]){const input=document.createElement("input");input.type="number";input.min="0";input.max="78";input.step="1";input.value=inner.count[key]??"";input.oninput=()=>{if(input.value==="")delete inner.count[key];else inner.count[key]=Number(input.value);commit()};addField(title,input)}
  for(const [key,title] of [["start","From"],["end","Through"]]){const select=document.createElement("select");select.innerHTML=rulePositionOptions(inner.count.range[key]);select.onchange=()=>{inner.count.range[key]=select.value;commit()};addField(title,select)}
 }else{
  const operation=document.createElement("select");operation.setAttribute("aria-label","Condition group operator");operation.innerHTML='<option value="all">All of these (AND)</option><option value="any">Any of these (OR)</option>';operation.value=inner.any?"any":"all";head.append(operation);
  let children=inner[operation.value];const body=document.createElement("div");outer.append(body);
  const render=()=>{body.replaceChildren();children.forEach((child,index)=>body.append(conditionNode(child,()=>{children.splice(index,1);commit();render()},depth+1)))};
  operation.onchange=()=>{inner={[operation.value]:children};commit()};
  const buttons=document.createElement("div");buttons.className="rule-toolbar";outer.append(buttons);
  for(const [title,child] of [["Add condition",()=>defaultRuleCount()],["Add group",()=>({any:[defaultRuleCount()]})]]){const b=document.createElement("button");b.className="small ghost";b.textContent=title;b.disabled=depth>=11;b.onclick=()=>{children.push(child());commit();render()};buttons.append(b)}
  render();
 }
 if(remove){const b=document.createElement("button");b.className="small ghost";b.textContent="Remove";b.setAttribute("aria-label","Remove this condition");b.onclick=remove;head.append(b)}
 // Nested editors mutate their own objects; preserve the linked inner tree.
 const bind=()=>{if(not.checked){Object.keys(value).forEach(k=>delete value[k]);value.not=inner}else{Object.keys(value).forEach(k=>delete value[k]);Object.assign(value,inner)}};bind();
 return outer;
}
function renderRuleCondition(){const box=$("ruleConditionEditor");box.replaceChildren();$("ruleAddConditions").hidden=!!ruleState.condition;if(ruleState.condition)box.append(conditionNode(ruleState.condition,()=>{ruleState.condition=null;ruleInvalidate();renderRuleCondition()}))}
function readTagRule(){const rule={version:1,name:$("ruleName").value||"Second Negative / Rare tag",range:{start:$("ruleStart").value,end:$("ruleEnd").value}};if(ruleState.condition)rule.condition=JSON.parse(JSON.stringify(ruleState.condition));return rule}
function readRuleRecipe(){
 if(ruleState.mode==="tag")return {version:1,mode:"second_tag",rule:readTagRule()};
 const ids=[...document.querySelectorAll(".rule-origin:checked")].map(x=>x.value);
 if(!ruleState.description)throw Error("Find original pools first, then choose which groups to recover.");
 if(!ids.length)throw Error("Select at least one saved group.");
 return {version:1,mode:"separate_sources",source_kind:$("ruleSourceKind").value,source_ids:ids};
}
const ruleReasonLabels={condition_not_met:"Optional conditions not met",no_eligible_tags:"No Negative or Rare in range",missing_opposite_tag:"No later tag of the opposite type",outside_selected_sources:"Outside the selected groups",no_tags:"No Negative or Rare tag in range",no_second_tag:"No qualifying second tag",same_ante_only:"Both types occur only in the same Ante",condition_failed:"Optional conditions not met",no_opposite_tag:"No later opposite tag",no_later_tag:"No tag in a later Ante"};
function renderRulePreview(plan){
 ruleState.plan=plan;const outputs=plan.outputs||[];$("ruleReview").hidden=false;
 $("ruleCounts").innerHTML=`<div><b>${fmt(outputs.length)}</b><span>new pools</span></div><div><b>${fmt(plan.copied_records??0)}</b><span>seeds copied</span></div><div><b>${fmt(plan.excluded_records??plan.unmatched_count??0)}</b><span>left out of new pools</span></div>`;
 const reasons=plan.exclusions||plan.exclusion_counts||{};$("ruleExclusions").textContent=Object.entries(reasons).filter(([,n])=>n).map(([key,n])=>`${ruleReasonLabels[key]||key.replace(/_/g," ")}: ${fmt(n)}`).join(" · ");
 $("ruleManifest").innerHTML=outputs.map(o=>`<div class="manifestrow"><div><b>${esc(o.name||o.filename)}</b><small>${esc(o.label||"")}</small></div><span class="count">${fmt(o.records)} seed${o.records===1?"":"s"}</span></div>`).join("")||'<p class="rule-empty">No seeds qualify. Change the range or conditions and preview again.</p>';
 $("ruleCreateBtn").disabled=!outputs.length||plan.can_create===false;$("ruleCreateBtn").hidden=!outputs.length;
 $("ruleStatus").textContent="Preview ready. Check the destinations before creating files.";
 if((plan.collisions||[]).length){$("ruleError").textContent=`These outputs already exist: ${plan.collisions.join(", ")}. Change the output prefix and preview again.`}
 if(plan.overlap_records)$("ruleExclusions").textContent+=`${$("ruleExclusions").textContent?" · ":""}${fmt(plan.overlap_records)} overlapping seed${plan.overlap_records===1?"":"s"} will be copied to each matching group.`;
}
async function previewRules(){
 ruleInvalidate();return ruleRun("Checking every seed against your rules…",async()=>{
  const recipe=readRuleRecipe();const data={source:$("ruleSource").value,recipe,prefix:$("rulePrefix").value};
  if(ruleState.description)data.snapshot=ruleState.description.source.snapshot_id;
  const plan=await api("/api/rules/preview",data);renderRulePreview(plan);
 });
}
async function createRulePools(){
 const plan=ruleState.plan;if(!plan)return;
 return ruleRun("Creating and verifying the new pools…",async()=>{
  const report=await api("/api/rules/publish",{source:$("ruleSource").value,planToken:plan.plan_token});ruleState.plan=null;$("ruleReview").hidden=true;$("ruleDone").hidden=false;
  const outputs=report.outputs||[];$("ruleResults").innerHTML=outputs.map(o=>`<div class="output"><b>${esc(o.name||o.filename||(o.path||"").split(/[\\/]/).pop())}</b><span>${fmt(o.records)} seed${o.records===1?"":"s"}</span></div>`).join("");
  $("ruleStatus").textContent=`Created ${outputs.length} pools. Your source pool was kept.`;
  const first=outputs[0];ruleState.nextSource=first?(first.name||first.filename||(first.path||"").split(/[\\/]/).pop()):"";
  try{await loadPools(true)}catch(e){$("ruleError").textContent=`Pools were created, but the list could not refresh: ${e.message}. Use Refresh list.`}
 });
}
async function saveRuleFile(){try{const result=await api("/api/rules/validate",{document:JSON.stringify({version:1,mode:"second_tag",rule:readTagRule()})}),recipe=result.recipe;const a=document.createElement("a"),url=URL.createObjectURL(new Blob([JSON.stringify(recipe,null,2)+"\n"],{type:"application/json"}));a.href=url;a.download=(recipe.rule.name||"tag-rules").replace(/[^A-Za-z0-9_-]+/g,"-")+".json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(e){$("ruleError").textContent=e.message}}
async function loadRuleFile(file){
 const revision=ruleState.revision;
 try{if(file.size>49152)throw Error("Rules files must be smaller than 48 KB.");const result=await api("/api/rules/validate",{document:await file.text()}),rule=result.recipe.rule;
  if(revision!==ruleState.revision||ruleState.busy)throw Error("Settings changed while the file was loading. Load the rules again.");
  showMode("tag");$("ruleStart").value=rule.range.start;$("ruleEnd").value=rule.range.end;$("ruleName").value=rule.name||"Loaded tag rules";ruleState.condition=rule.condition||null;ruleInvalidate();renderRuleCondition();rememberTagDraft();$("ruleStatus").textContent="Rules loaded. Preview to check them against this pool.";
 }catch(e){$("ruleError").textContent=e.message||String(e)}finally{$("ruleLoadFile").value=""}
}
$("ruleSource").onchange=ruleSourceChanged;$("rulePrefix").oninput=ruleInvalidate;
$("ruleStart").onchange=ruleInvalidate;$("ruleEnd").onchange=ruleInvalidate;$("ruleName").oninput=ruleInvalidate;
$("ruleDetectBtn").onclick=detectRuleSources;$("ruleRefreshBtn").onclick=async()=>{ruleSourceChanged();try{await loadPools(true)}catch(e){$("ruleError").textContent=e.message}};
$("ruleSourceKind").onchange=()=>{ruleInvalidate();renderRuleSources()};
$("ruleSelectAll").onclick=()=>{document.querySelectorAll(".rule-origin").forEach(x=>x.checked=true);ruleInvalidate()};$("ruleSelectNone").onclick=()=>{document.querySelectorAll(".rule-origin").forEach(x=>x.checked=false);ruleInvalidate()};
$("rulePreviewBtn").onclick=previewRules;$("ruleCreateBtn").onclick=createRulePools;
$("ruleCancelBtn").onclick=async()=>{$("ruleCancelBtn").disabled=true;try{await api("/api/cancel",{operation:"rules"});$("ruleStatus").textContent="Cancelling…"}catch(e){$("ruleError").textContent=e.message;$("ruleCancelBtn").disabled=false}};
$("ruleAddConditions").onclick=()=>{ruleState.condition={all:[defaultRuleCount()]};ruleInvalidate();renderRuleCondition()};
$("ruleSaveBtn").onclick=saveRuleFile;$("ruleLoadBtn").onclick=()=>$("ruleLoadFile").click();$("ruleLoadFile").onchange=e=>e.target.files[0]&&loadRuleFile(e.target.files[0]);
$("ruleNextBtn").onclick=()=>{showMode("tag");if(ruleState.nextSource)$("ruleSource").value=ruleState.nextSource;ruleSourceChanged();$("ruleTitle").scrollIntoView({block:"start"})};
renderRuleCondition();
'''
