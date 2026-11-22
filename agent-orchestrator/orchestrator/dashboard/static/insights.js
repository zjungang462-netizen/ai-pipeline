'use strict';
let taskTemplates=[],templateGeneration=0,failureAfter=null,failureDone=false,failureBusy=false;
const failureCounts=new Map(),failureSeen=new Set();
function renderBudgetAlerts(run){
 const box=$('budget-alert'),alerts=run.budget_alerts||[];
 box.textContent=alerts.map(a=>a.dimension==='tokens'?`token 额度已使用 ${Math.round(a.ratio*100)}%（含预留）`:a.dimension==='wall_clock'?`运行期限剩余 ${Math.ceil(a.remaining_s)} 秒`:`计量时长额度已使用 ${Math.round(a.ratio*100)}%`).join('；');
 if(alerts.length)box.textContent+='。请核对当前进度；提示不会增加预算。';show(box,alerts.length>0);
 show($('token-progress'),!!run.budget?.max_tokens);
 const modelBox=$('active-models');
 const callLimits=[];
 if(run.budget?.max_model_calls)callLimits.push(`模型调用上限 ${num(run.budget.max_model_calls)} 次`);
 if(run.budget?.max_unknown_calls)callLimits.push(`未知用量上限 ${num(run.budget.max_unknown_calls)} 次`);
 modelBox.textContent=(run.models||[]).map(m=>`${m.name}: ${m.model||m.effective_model||'工具默认'}${m.reasoning_effort?' · 思考 '+m.reasoning_effort:''}${m.thinking?' · thinking '+m.thinking:''}${m.review_json_mode?' · JSON审查':''}${m.observed_models?.length?' · 实报模型 '+m.observed_models.join(', '):''}${m.observed_backend_endpoints?.length?' · 实报连接 '+m.observed_backend_endpoints.join(', '):m.backend_endpoint?' · 连接配置 '+m.backend_endpoint:''}`).join('；')+(run.budget?.unlimited_tokens?'；本次不限制 token，仍受工具上限、时长和轮次约束。':'')+(callLimits.length?'；'+callLimits.join('，')+'。':'')+(run.usage?.qoder_credits!=null?`；Qoder 已报告 ${run.usage.qoder_credits} credits；token 计数不完整，不能按免费调用理解。`:'');
 show(modelBox,!!modelBox.textContent);
}

const ADAPTER_TYPE_LABELS={
 openai_compat:'兼容 API',cli_agy:'反重力 CLI',cli_codex:'Codex CLI',cli_claude:'Claude CLI',
 cli_grok:'Grok 容器',cli_doubao:'豆包容器',cli_qwen:'Qwen CLI',cli_qoder:'Qoder CLI',cli_dsh:'DSH CLI',fake:'离线模拟'
};
const MODEL_PRESETS={
 openai_compat:['deepseek-chat','deepseek-reasoner','gpt-4.1','gpt-4o','claude-sonnet-4-20250514','qwen-plus'],
 cli_agy:['gemini-3.8-flash-high','gemini-3.8-pro-high','gemini-3.8-flash-low','claude-sonnet-4-6'],
 cli_codex:['gpt-5.4','gpt-5.3-codex','o3'],
 cli_claude:['claude-sonnet-4-6','claude-opus-4-6','deepseek-v4-pro[1M]'],
 cli_grok:['grok-code-fast-1','grok-4'],
 cli_doubao:['doubao-seed-code','doubao-pro-32k'],
 cli_qwen:['qwen3-coder-plus','qwen-plus'],
 cli_qoder:['Ultimate','Performance','Efficient']
};
let liveModelCatalog=null,catalogLoading=null;
function catalogEntryFor(type){return liveModelCatalog?.adapters?.[type]||null;}
function catalogModelsFor(type){
 const live=catalogEntryFor(type)?.models;
 if(Array.isArray(live)&&live.length)return live;
 return (MODEL_PRESETS[type]||[]).map(id=>({id,label:id}));
}
async function loadModelCatalog(force=false){
 if(!force&&liveModelCatalog)return liveModelCatalog;
 if(catalogLoading&&!force)return catalogLoading;
 catalogLoading=(async()=>{
  try{
   const data=await api('/models/catalog'+(force?'?refresh=1':''));
   liveModelCatalog=data;
   return data;
  }catch(error){
   if(force)toast('刷新本机可用模型失败：'+error.message);
   return liveModelCatalog;
  }finally{catalogLoading=null;}
 })();
 return catalogLoading;
}

function renderModelSettings(){
 const project=projects.find(p=>p.id===$('project-select').value),box=$('model-settings');box.replaceChildren();
 if(project&&!liveModelCatalog&&!catalogLoading&&typeof api==='function'){
  loadModelCatalog(false).then(cat=>{if(cat&&$('project-select').value===project.id)renderModelSettings();});
 }
 for(const model of project?.models||[]){
  const card=el('div','adapter-card');
  const head=el('div','adapter-card-head');
  const left=el('div','adapter-card-title-group');
  left.append(el('span','adapter-card-name',model.name));
  const stageNames=(model.stages||[]).map(s=>(typeof stages!=='undefined'&&stages[s])||s);
  if(stageNames.length)left.append(el('span','adapter-stage-pill','负责：'+stageNames.join(' / ')));
  const entry=catalogEntryFor(model.type);
  const typeText=(ADAPTER_TYPE_LABELS[model.type]||model.type)+(entry?.source==='live_cli'||entry?.source==='live_api'?` · 实探 ${entry.models.length} 模型`:'');
  head.append(left,el('span','adapter-type-pill',typeText));
  card.append(head);

  const currentVal=model.model||model.effective_model||'';
  const discovered=catalogModelsFor(model.type);
  const seen=new Set();
  const optionsList=[];
  if(currentVal){seen.add(currentVal);const found=discovered.find(m=>m.id===currentVal);optionsList.push({id:currentVal,label:found?found.label:currentVal});}
  for(const item of discovered){if(!seen.has(item.id)){seen.add(item.id);optionsList.push(item);}}
  const presetIds=optionsList.map(m=>m.id);

  const pickerRow=el('div','model-picker-row');
  const presetLabel=el('label','',`选择系统可用模型（共 ${optionsList.length} 个）`);
  const presetSelect=el('select','model-preset-select');
  presetSelect.append(Object.assign(el('option','','自定义 / 工具默认'),{value:''}));
  for(const item of optionsList){
   const text=item.label&&item.label!==item.id?`${item.label} · ${item.id}`:item.id;
   presetSelect.append(Object.assign(el('option','',text),{value:item.id}));
  }
  presetSelect.value=presetIds.includes(currentVal)?currentVal:'';
  presetLabel.append(presetSelect);

  const inputLabel=el('label','','模型标识（可手动输入）');
  const input=el('input');input.value=currentVal;input.placeholder='留空使用已登录工具默认模型';input.maxLength=160;input.dataset.adapter=model.name;input.dataset.option='model';
  inputLabel.append(input);
  pickerRow.append(presetLabel,inputLabel);
  card.append(pickerRow);

  const chips=[];
  if(optionsList.length){
   const chipWrap=el('div','model-chips');
   for(const item of optionsList.slice(0,6)){
    const chip=el('button','model-chip'+(item.id===currentVal?' active':''),item.id);
    chip.type='button';chip.title=item.label||item.id;
    chip.onclick=()=>{input.value=item.id;presetSelect.value=item.id;chips.forEach(c=>c.classList.toggle('active',c.textContent===item.id));};
    chips.push(chip);chipWrap.append(chip);
   }
   card.append(chipWrap);
  }
  presetSelect.onchange=()=>{if(presetSelect.value){input.value=presetSelect.value;}chips.forEach(c=>c.classList.toggle('active',c.textContent===input.value.trim()));};
  input.oninput=()=>{const v=input.value.trim();presetSelect.value=presetIds.includes(v)?v:'';chips.forEach(c=>c.classList.toggle('active',c.textContent===v));};

  const extraGrid=el('div','adapter-extra-grid');
  for(const [field,enabled,values,title] of [['reasoning_effort',model.supports_effort,['cli_qoder','cli_claude'].includes(model.type)?['low','medium','high','xhigh','max']:['none','low','medium','high','xhigh'],'思考强度'],['thinking',model.supports_thinking,model.type==='cli_qoder'?['auto','adaptive','disabled']:['enabled','disabled'],'深度思考']]){
   if(!enabled)continue;const fieldLabel=el('label','',title),select=el('select');select.dataset.adapter=model.name;select.dataset.option=field;
   for(const value of ['',...values]){const option=el('option','',value||'沿用配置/默认');option.value=value;select.append(option);}select.value=model[field]||'';fieldLabel.append(select);extraGrid.append(fieldLabel);
  }
  if(model.type==='openai_compat'){const label=el('label','','结构化审查 JSON'),select=el('select');select.dataset.adapter=model.name;select.dataset.option='review_json_mode';
   for(const [value,text] of [['','沿用配置'],['true','启用'],['false','禁用']]){const option=el('option','',text);option.value=value;select.append(option);}select.value=model.review_json_mode?'true':'';label.append(select);extraGrid.append(label);
  }
  if(extraGrid.children.length)card.append(extraGrid);
  box.append(card);
 }
}
function executionOptions(){
 const models={};for(const node of $('model-settings').querySelectorAll('[data-adapter]')){
  if(!node.value.trim())continue;models[node.dataset.adapter]??={};models[node.dataset.adapter][node.dataset.option]=node.dataset.option==='review_json_mode'?node.value==='true':node.value.trim();
 }
 return {models,unlimited_tokens:$('unlimited-tokens').checked};
}
const previousProjectChange=$('project-select').onchange;
$('project-select').onchange=()=>{previousProjectChange();renderModelSettings();};
const previousTaskSubmit=$('task-form').onsubmit;
// 实际创建仍由原表单路径提交；此处只提供白名单选项读取。
async function loadTaskTemplates(derive){
 const generation=++templateGeneration;
 $('task-template').replaceChildren(Object.assign(el('option','','不使用模板'),{value:''}));
 $('task-template').disabled=derive;$('template-save').disabled=derive;if(derive)return;
 try{const data=await api('/templates');if(generation!==templateGeneration||parent)return;taskTemplates=data.templates;
  $('task-template').append(...taskTemplates.map(t=>{const option=el('option','',t.name+(t.usable?'':' · 配置变化/不可用'));option.value=t.id;option.disabled=!t.usable;return option;}));
 }catch(error){if(generation===templateGeneration)toast('无法读取任务模板：'+error.message);}
}
$('task-template').onchange=()=>{
 const t=taskTemplates.find(t=>t.id===$('task-template').value);if(!t)return;
 const p=projects.find(p=>p.id===t.project_id);
 if(!t.usable||!p||p.config_hash!==t.config_hash){toast('模板配置已变化，请刷新并核对执行范围。');return;}
 $('project-select').value=t.project_id;$('task-input').value=t.task;renderTaskScope();renderModelSettings();
};
$('template-save').onclick=async()=>{
 const project=projects.find(p=>p.id===$('project-select').value);
 if(parent||!taskProjectsFresh||!project||project.unavailable){toast('请刷新并选择可用项目配置。');return;}
 const button=$('template-save');button.disabled=true;
 try{await api('/templates',{method:'POST',body:{project_id:project.id,name:$('template-name').value,task:$('task-input').value,expected_config_hash:project.config_hash}});await loadTaskTemplates(false);toast('模板已保存；使用模板后仍需核对并启动。');}
 catch(error){toast(error.message);}finally{button.disabled=false;}
};
function compareText(data){
 const rows=[`配置${data.same_config?'一致':'不同或未记录，不能直接判断优劣'}`];
 for(const [key,label] of [['tokens','预算计量 tokens'],['reported_tokens','已报告 tokens'],['reserved_tokens','预留 tokens'],['duration_s','计量秒数']]){
  const delta=data.delta[key];rows.push(`${label}差值（对照−基准）：${delta==null?'未知':num(delta)}`);
 }
 rows.push(`基准：${labels[data.left.lifecycle]||data.left.lifecycle}；对照：${labels[data.right.lifecycle]||data.right.lifecycle}；实际账单费用未报告。`);return rows.join('\n');
}
$('insights-open').onclick=()=>{
 for(const id of ['compare-left','compare-right'])$(id).replaceChildren(...runs.map(r=>{const option=el('option','',r.run_id+' · '+(r.task||'').slice(0,60));option.value=r.id;return option;}));
 if(runs.length>1)$('compare-right').value=runs[1].id;
 for(const id of ['lessons-project','route-project','arena-project']){
  const sel=$(id);if(!sel)continue;
  sel.replaceChildren(...projects.filter(p=>!p.unavailable).map(p=>{const o=el('option','',p.name);o.value=p.id;return o;}));
 }
 $('insights-dialog').showModal();
};
$('compare-run').onclick=async()=>{
 const params=new URLSearchParams({left:$('compare-left').value,right:$('compare-right').value});
 try{const result=await api('/compare?'+params);$('compare-result').textContent=compareText(result);show($('compare-result'));}catch(error){toast(error.message);}
};
$('failures-next').onclick=async()=>{
 if(failureBusy)return;
 if(failureDone){failureAfter=null;failureDone=false;failureCounts.clear();failureSeen.clear();$('failure-results').replaceChildren();}
 failureBusy=true;$('failures-next').disabled=true;
 try{
  const params=new URLSearchParams({limit:'50'});if(failureAfter)params.set('after',failureAfter);
  const page=await api('/failures?'+params);
  if(page.has_more&&(!page.next_after||page.next_after===failureAfter))throw Error('扫描未推进，请重试');
  for(const row of page.failures){if(failureSeen.has(row.id))continue;failureSeen.add(row.id);
   for(const key of row.categories)failureCounts.set(key,(failureCounts.get(key)||0)+1);
   const button=el('button','artifact-button',row.run_id+' · '+row.categories.join(' / '));button.onclick=()=>{$('insights-dialog').close();select(row.id);};$('failure-results').append(button);
  }
  failureAfter=page.next_after;failureDone=!page.has_more;
  $('failure-summary').textContent=`已累计 ${failureSeen.size} 条失败记录（类别可重叠）：`+[...failureCounts].map(([k,v])=>k+' '+v).join('；')+(page.errors.length?`；本页 ${page.errors.length} 条元数据不可读`:'')+(failureDone?'；已扫描至当前末尾':'；还有历史页');
  $('failures-next').textContent=failureDone?'重新扫描失败记录':'扫描下一页失败记录';
 }catch(error){toast(error.message);}finally{failureBusy=false;$('failures-next').disabled=false;}
};

// 自优化看板标签页 + 经验沉淀 + 模型路由推荐 + 可视化新增项目
let latestLessonsProposal=null;
if(typeof document.querySelectorAll==='function'){
 document.querySelectorAll('[data-insights-tab]').forEach(btn=>{
  btn.onclick=()=>{
   const target=btn.dataset.insightsTab;
   document.querySelectorAll('[data-insights-tab]').forEach(b=>b.classList.toggle('active',b===btn));
   for(const name of ['usage','lessons','routing','arena']){
    const pane=$('insights-pane-'+name);if(pane)show(pane,name===target);
   }
  };
 });
}
if($('lessons-scan'))$('lessons-scan').onclick=async()=>{
 const pid=$('lessons-project')?.value;if(!pid){toast('请先选择可用项目');return;}
 const minOcc=Math.max(1,Number($('lessons-min-occ')?.value||2));
 $('lessons-scan').disabled=true;
 try{
  const data=await api(`/projects/${encodeURIComponent(pid)}/lessons?min_occurrences=${minOcc}`);
  latestLessonsProposal=data.proposal;
  const list=$('lessons-list'),status=$('lessons-status');list.replaceChildren();
  const items=data.proposal?.items||[];
  const approvedItems=data.approved?.items||[];
  status.textContent=`已扫描 ${data.proposal?.scanned_runs||0} 次历史运行：发现 ${items.length} 条高频待审批经验；当前已生效 ${approvedItems.length} 条。`;
  show(status,true);
  if($('lessons-approve'))$('lessons-approve').disabled=!items.length;
  for(const item of items){
   const card=el('div','opt-card');
   card.append(el('div','opt-card-title',`[待审批 · 出现 ${item.occurrences} 次] ${item.source}`),el('div','opt-card-body',item.checklist));
   list.append(card);
  }
  for(const item of approvedItems){
   const card=el('div','opt-card approved');
   card.append(el('div','opt-card-title',`[已生效 · 出现 ${item.occurrences} 次] ${item.source}`),el('div','opt-card-body',item.checklist));
   list.append(card);
  }
  if(!items.length&&!approvedItems.length)list.append(el('p','empty','暂无达到阈值的高频失败经验。'));
 }catch(error){toast(error.message);}finally{$('lessons-scan').disabled=false;}
};
if($('lessons-approve'))$('lessons-approve').onclick=async()=>{
 const pid=$('lessons-project')?.value;if(!pid||!latestLessonsProposal)return;
 $('lessons-approve').disabled=true;
 try{
  const res=await api(`/projects/${encodeURIComponent(pid)}/lessons/approve`,{method:'POST',body:{proposal:latestLessonsProposal}});
  toast(`已批准 ${res.items} 条失败经验，将自动注入后续任务提示词。`);
  $('lessons-scan').onclick();
 }catch(error){toast(error.message);}finally{$('lessons-approve').disabled=false;}
};
if($('route-scan'))$('route-scan').onclick=async()=>{
 const pid=$('route-project')?.value;if(!pid){toast('请先选择可用项目');return;}
 const minRuns=Math.max(1,Number($('route-min-runs')?.value||1));
 $('route-scan').disabled=true;
 try{
  const data=await api(`/projects/${encodeURIComponent(pid)}/route-recommend?min_runs=${minRuns}`);
  const box=$('route-results');box.replaceChildren();
  const entries=Object.entries(data.recommendations||{});
  if(!entries.length){box.append(el('p','empty',`已扫描 ${data.scanned_metrics||0} 条终态 metrics，暂无可用模型样本。`));return;}
  const kindLabels={code:'代码阶段 (code)',report:'报告/方案阶段 (report)',review:'审查阶段 (review)'};
  for(const [kind,rec] of entries){
   const card=el('div','opt-card');
   const recText=rec.recommended?`推荐：${ADAPTER_TYPE_LABELS[rec.recommended.tool]||rec.recommended.tool} / ${rec.recommended.model}`:'暂不自动推荐（样本不足）';
   card.append(el('div','opt-card-title',`${kindLabels[kind]||kind} · ${recText}`),el('div','opt-card-sub',rec.reason||''));
   const wrap=el('div','md-table-wrap'),tbl=el('table','md-table'),thead=el('thead'),hr=el('tr');
   for(const h of ['状态','工具','模型','样本','整条通过率','阶段通过率','中位耗时','中位 Tokens','未知调用']){hr.append(el('th','',h));}
   thead.append(hr);tbl.append(thead);
   const tbody=el('tbody');
   for(const c of rec.candidates||[]){
    const isTop=rec.recommended&&rec.recommended.tool===c.tool&&rec.recommended.model===c.model;
    const tr=el('tr');
    const st=isTop?'推荐':c.eligible?'候选':'样本不足';
    for(const cell of [st,ADAPTER_TYPE_LABELS[c.tool]||c.tool,c.model,String(c.runs),(c.run_pass_rate*100).toFixed(0)+'%',(c.stage_pass_rate*100).toFixed(0)+'%',c.median_duration_s!=null?c.median_duration_s+'s':'—',c.median_reported_tokens!=null?num(c.median_reported_tokens):'—',String(c.unknown_calls||0)]){
     tr.append(el('td','',cell));
    }
    tbody.append(tr);
   }
   tbl.append(tbody);wrap.append(tbl);card.append(wrap);
   box.append(card);
  }
 }catch(error){toast(error.message);}finally{$('route-scan').disabled=false;}
};
const PM_PRESET_STAGES={
 review_plan_fix:[
  {name:'code_review',kind:'report',adapter_type:'cli_agy',model:'gemini-4-neon-high',approval:false,feedback_to:'',prompt:''},
  {name:'repair_plan',kind:'report',adapter_type:'cli_agy',model:'gemini-3.8-flash-high',approval:true,feedback_to:'',prompt:''},
  {name:'coding',kind:'code',adapter_type:'cli_agy',model:'gemini-3.8-flash-high',approval:false,feedback_to:'',prompt:''},
  {name:'final_review',kind:'review',adapter_type:'openai_compat',model:'deepseek-chat',approval:false,feedback_to:'coding',prompt:''},
 ],
 multi_agent_5:[
  {name:'lead',kind:'report',adapter_type:'cli_agy',model:'gemini-pro-orchestrator-neon',approval:false,feedback_to:'',prompt:'你是技术指挥官。请拆解任务目标、明确模块边界与验收标准。\n\n任务：{task}\n\n上游产物：{upstream}'},
  {name:'design',kind:'report',adapter_type:'cli_agy',model:'claude-opus-5-5-high',approval:true,feedback_to:'',prompt:'你是系统架构师。请基于指挥拆解输出具体文件改动方案与边界条件。\n\n任务：{task}\n\n上游产物：{upstream}'},
  {name:'coding',kind:'code',adapter_type:'cli_agy',model:'gemini-3.8-flash-high',approval:false,feedback_to:'',prompt:''},
  {name:'cross_review',kind:'review',adapter_type:'cli_agy',model:'claude-sonnet-5-5-high',approval:false,feedback_to:'coding',prompt:''},
  {name:'final_review',kind:'review',adapter_type:'openai_compat',model:'deepseek-chat',approval:false,feedback_to:'coding',prompt:''},
 ],
 review_only:[
  {name:'code_review',kind:'report',adapter_type:'cli_agy',model:'gemini-4-neon-high',approval:false,feedback_to:'',prompt:''},
 ],
 standard_dev:[
  {name:'design',kind:'report',adapter_type:'openai_compat',model:'deepseek-chat',approval:true,feedback_to:'',prompt:''},
  {name:'coding',kind:'code',adapter_type:'cli_agy',model:'gemini-3.8-flash-high',approval:false,feedback_to:'',prompt:''},
  {name:'review',kind:'review',adapter_type:'openai_compat',model:'deepseek-chat',approval:false,feedback_to:'coding',prompt:''},
 ],
};
const TEST_PRESET_LABELS={
 pytest:'pytest（python -m pytest -q）',
 python_unittest:'unittest（python -m unittest discover -s tests）',
 npm_test:'npm test',
 npm_run_build:'npm run build',
};
let pmDraftStages=[];
async function loadProjectIntoEditor(pid,returnToTask){
 const def=await api(`/projects/${encodeURIComponent(pid)}/stages`);
 const baseName=def.name||'custom-flow';
 if($('pm-name'))$('pm-name').value=def.managed?baseName:(baseName.endsWith('-custom')?baseName:`${baseName}-custom`).slice(0,48);
 if($('pm-workdir'))$('pm-workdir').value=def.workdir||'';
 if($('pm-proxy'))$('pm-proxy').value=def.proxy_url||'';
 if($('pm-max-duration'))$('pm-max-duration').value=String(def.max_duration_s||900);
 if($('pm-max-calls'))$('pm-max-calls').value=String(def.max_model_calls||15);
 if($('pm-patch-probe'))$('pm-patch-probe').checked=def.allow_patch_probe!==false;
 pmDraftStages=(def.stages||[]).map(s=>({...s}));
 pmReturnToTask=!!returnToTask;
 renderProjectStageEditor();
 renderRegisteredProjectsList();
 if(!$('project-dialog')?.open)$('project-dialog')?.showModal();
 if(!liveModelCatalog&&typeof api==='function')loadModelCatalog(false).then(()=>{if($('project-dialog')?.open)renderProjectStageEditor();});
}
function renderRegisteredProjectsList(){
 const box=$('pm-registered-list');if(!box)return;
 box.replaceChildren();
 if(!projects.length){box.append(el('p','empty','当前暂无已登记项目。'));return;}
 for(const p of projects){
  const row=el('div','pm-reg-row');
  const info=el('div','pm-reg-info');
  info.append(el('strong','',p.name),el('span','muted',p.unavailable?`不可用：${p.unavailable}`:(p.execution_scope?.workdir||p.config||'')));
  const actions=el('div','pm-stages-actions');
  if(!p.unavailable){
   const editBtn=el('button','secondary small','载入编辑');editBtn.type='button';
   editBtn.onclick=()=>loadProjectIntoEditor(p.id,false).catch(e=>toast(e.message));
   actions.append(editBtn);
  }
  const delBtn=el('button','danger-outline small','移除登记');delBtn.type='button';
  delBtn.onclick=async()=>{
   delBtn.disabled=true;
   try{
    await api(`/projects/${encodeURIComponent(p.id)}/remove`,{method:'POST',body:{delete_config_file:true}});
    projects=(await api('/projects')).projects;
    if(typeof applyProjects==='function')applyProjects();
    if(typeof restoreListFilters==='function')restoreListFilters();
    renderRegisteredProjectsList();
    toast(`已安全移除项目 ${p.name} 的登记（未删除代码目录或历史运行）。`);
   }catch(e){toast(e.message);}finally{delBtn.disabled=false;}
  };
  actions.append(delBtn);
  row.append(info,actions);
  box.append(row);
 }
}
function resetProjectDraftFromPreset(){
 const preset=$('pm-preset')?.value||'review_plan_fix';
 pmDraftStages=(PM_PRESET_STAGES[preset]||PM_PRESET_STAGES.review_plan_fix).map(s=>({...s}));
 renderProjectStageEditor();
}
function populateProjectModelSelect(sel,type,preferred){
 const models=catalogModelsFor(type);
 sel.replaceChildren(Object.assign(el('option','','自定义 / 工具默认'),{value:''}));
 for(const m of models){
  const text=m.label&&m.label!==m.id?`${m.label} · ${m.id}`:m.id;
  sel.append(Object.assign(el('option','',text),{value:m.id}));
 }
 const ids=models.map(m=>m.id);
 if(preferred&&ids.includes(preferred))sel.value=preferred;
 else if(models.length)sel.value=models[0].id;
 else sel.value='';
}
function renderProjectStageEditor(){
 const list=$('pm-stage-list');
 if(!list)return;
 if(!pmDraftStages.length)pmDraftStages=PM_PRESET_STAGES.review_plan_fix.map(s=>({...s}));
 list.replaceChildren();
 pmDraftStages.forEach((st,idx)=>{
  const card=el('div','pm-node-card');
  const head=el('div','pm-node-head');
  const numBadge=el('span','stage-number',String(idx+1).padStart(2,'0'));
  const nameInp=el('input','pm-node-name');nameInp.value=st.name;nameInp.maxLength=32;nameInp.placeholder='节点英文名（如 lead / coding）';
  nameInp.oninput=()=>{st.name=nameInp.value.trim();};
  const kindSel=el('select','pm-node-kind');
  for(const [k,v] of [['report','方案/报告 (report)'],['code','代码实现 (code)'],['review','独立审查 (review)'],['test','确定性测试 (test)']]){
   kindSel.append(Object.assign(el('option','',v),{value:k}));
  }
  kindSel.value=st.kind;
  kindSel.onchange=()=>{st.kind=kindSel.value;if(st.kind!=='review'&&st.kind!=='test')st.feedback_to='';if(st.kind==='test'&&!st.test_preset)st.test_preset='pytest';renderProjectStageEditor();};
  const ops=el('div','pm-node-ops');
  const upBtn=el('button','icon-button','↑');upBtn.type='button';upBtn.title='上移';upBtn.disabled=idx===0;
  upBtn.onclick=()=>{const t=pmDraftStages[idx-1];pmDraftStages[idx-1]=pmDraftStages[idx];pmDraftStages[idx]=t;renderProjectStageEditor();};
  const downBtn=el('button','icon-button','↓');downBtn.type='button';downBtn.title='下移';downBtn.disabled=idx===pmDraftStages.length-1;
  downBtn.onclick=()=>{const t=pmDraftStages[idx+1];pmDraftStages[idx+1]=pmDraftStages[idx];pmDraftStages[idx]=t;renderProjectStageEditor();};
  const delBtn=el('button','icon-button','×');delBtn.type='button';delBtn.title='删除节点';delBtn.disabled=pmDraftStages.length<=1;
  delBtn.onclick=()=>{pmDraftStages.splice(idx,1);renderProjectStageEditor();};
  ops.append(upBtn,downBtn,delBtn);
  head.append(numBadge,nameInp,kindSel,ops);

  const body=el('div','pm-node-grid');
  if(st.kind==='test'){
   const presetSel=el('select','pm-test-preset');
   for(const [k,v] of Object.entries(TEST_PRESET_LABELS)){
    presetSel.append(Object.assign(el('option','',v),{value:k}));
   }
   presetSel.value=st.test_preset||'pytest';
   st.test_preset=presetSel.value;
   presetSel.onchange=()=>{st.test_preset=presetSel.value;};
   body.append(presetSel,el('span','muted','白名单确定性命令（不执行任意 shell）'));
  }else{
   const typeSel=el('select','pm-stage-type');
   for(const [k,v] of Object.entries(ADAPTER_TYPE_LABELS)){
    if(k==='fake'||k==='cli_dsh')continue;
    const entry=catalogEntryFor(k);
    const badge=entry?(entry.installed?`（可用${entry.source==='live_cli'||entry.source==='live_api'?'·'+entry.models.length:''}）`:'（未检测到）'):'';
    typeSel.append(Object.assign(el('option','',v+badge),{value:k}));
   }
   typeSel.value=st.adapter_type||'cli_agy';
   const modelSel=el('select','pm-stage-model-select');
   const modelInp=el('input','pm-stage-model');
   modelInp.maxLength=160;modelInp.placeholder='手动输入模型 ID（可选）';
   populateProjectModelSelect(modelSel,typeSel.value,st.model);
   modelInp.value=st.model||modelSel.value||'';
   st.model=modelInp.value;
   typeSel.onchange=()=>{st.adapter_type=typeSel.value;populateProjectModelSelect(modelSel,typeSel.value,'');modelInp.value=modelSel.value;st.model=modelInp.value;};
   modelSel.onchange=()=>{if(modelSel.value){modelInp.value=modelSel.value;st.model=modelSel.value;}};
   modelInp.oninput=()=>{const v=modelInp.value.trim();st.model=v;const ids=catalogModelsFor(typeSel.value).map(m=>m.id);modelSel.value=ids.includes(v)?v:'';};
   body.append(typeSel,modelSel,modelInp);
  }

  const foot=el('div','pm-node-foot');
  if(st.kind!=='test'){
   const apprLabel=el('label','pm-inline-check');
   const apprChk=el('input');apprChk.type='checkbox';apprChk.checked=!!st.approval;
   apprChk.onchange=()=>{st.approval=apprChk.checked;};
   apprLabel.append(apprChk,el('span','','完成后需人工审批'));
   foot.append(apprLabel);
  }
  if((st.kind==='review'||st.kind==='test')&&idx>0){
   const fbWrap=el('label','pm-inline-fb','不通过时打回：');
   const fbSel=el('select');
   fbSel.append(Object.assign(el('option','','不自动打回'),{value:''}));
   for(const prev of pmDraftStages.slice(0,idx)){
    if(prev.name)fbSel.append(Object.assign(el('option','',prev.name),{value:prev.name}));
   }
   fbSel.value=st.feedback_to||'';
   fbSel.onchange=()=>{st.feedback_to=fbSel.value;};
   fbWrap.append(fbSel);foot.append(fbWrap);
  }
  card.append(head,body,foot);
  if(st.kind!=='test'){
   const promptDetails=el('details','pm-node-prompt');
   promptDetails.append(el('summary','','自定义本节点提示词（留空使用角色默认）'));
   const promptTa=el('textarea');promptTa.rows=2;promptTa.maxLength=4000;promptTa.value=st.prompt||'';promptTa.placeholder='支持 {task}、{upstream}、{feedback} 占位符';
   promptTa.oninput=()=>{st.prompt=promptTa.value;};
   promptDetails.append(promptTa);
   card.append(promptDetails);
  }
  list.append(card);
 });
}
if($('pm-preset'))$('pm-preset').onchange=resetProjectDraftFromPreset;
if($('pm-add-stage'))$('pm-add-stage').onclick=()=>{
 if(pmDraftStages.length>=12){toast('最多支持 12 个动态节点');return;}
 const n=pmDraftStages.length+1;
 pmDraftStages.push({name:`stage_${n}`,kind:'report',adapter_type:'cli_agy',model:catalogModelsFor('cli_agy')[0]?.id||'gemini-3.8-flash-high',approval:false,feedback_to:'',prompt:''});
 renderProjectStageEditor();
};
if($('refresh-catalog-task'))$('refresh-catalog-task').onclick=async()=>{
 $('refresh-catalog-task').disabled=true;
 try{await loadModelCatalog(true);renderModelSettings();toast('已刷新本机可用模型目录');}finally{$('refresh-catalog-task').disabled=false;}
};
if($('apply-route-recommend'))$('apply-route-recommend').onclick=async()=>{
 const pid=$('project-select')?.value;
 if(!pid){toast('请先选择项目配置');return;}
 const project=projects.find(p=>p.id===pid);
 if(!project||!project.models?.length){toast('当前项目没有可配置的模型节点');return;}
 $('apply-route-recommend').disabled=true;
 try{
  const data=await api(`/projects/${encodeURIComponent(pid)}/route-recommend?min_runs=1`);
  const recs=data.recommendations||{};
  let applied=0;
  for(const model of project.models){
   const kinds=(model.stages||[]).map(s=>s.kind).filter(Boolean);
   const matchKind=kinds.find(k=>recs[k]?.recommended&&recs[k].recommended.tool===model.type);
   const rec=matchKind?recs[matchKind].recommended:null;
   if(!rec||!rec.model)continue;
   const inp=$('model-settings')?.querySelector(`input[data-adapter="${CSS.escape(model.name)}"][data-option="model"]`);
   if(inp){
    inp.value=rec.model;
    inp.dispatchEvent(new Event('input'));
    applied++;
   }
  }
  toast(applied?`已按历史真实运行数据自动填充 ${applied} 个节点的推荐模型`:'历史终态样本不足或与当前节点工具不匹配，暂无可自动填充项。');
 }catch(error){toast('获取历史推荐失败：'+error.message);}finally{$('apply-route-recommend').disabled=false;}
};
if($('refresh-catalog-pm'))$('refresh-catalog-pm').onclick=async()=>{
 $('refresh-catalog-pm').disabled=true;
 try{await loadModelCatalog(true);renderProjectStageEditor();toast('已刷新本机可用模型目录');}finally{$('refresh-catalog-pm').disabled=false;}
};
let pmReturnToTask=false;
if($('edit-project-nodes'))$('edit-project-nodes').onclick=async()=>{
 const pid=$('project-select')?.value;
 if(!pid){toast('请先在上方选择一个项目配置');return;}
 $('edit-project-nodes').disabled=true;
 try{await loadProjectIntoEditor(pid,true);}catch(error){toast('读取项目节点失败：'+error.message);}finally{$('edit-project-nodes').disabled=false;}
};
if($('project-manage-open'))$('project-manage-open').onclick=()=>{
 pmReturnToTask=false;
 resetProjectDraftFromPreset();renderRegisteredProjectsList();$('project-dialog')?.showModal();
 if(!liveModelCatalog&&typeof api==='function')loadModelCatalog(false).then(()=>{if($('project-dialog')?.open)renderProjectStageEditor();});
};
if($('project-form'))$('project-form').onsubmit=async event=>{
 event.preventDefault();
 const btn=$('pm-submit');btn.disabled=true;
 try{
  const stagesPayload=pmDraftStages.map(st=>st.kind==='test'?({
   name:(st.name||'').trim(),
   kind:'test',
   test_preset:st.test_preset||'pytest',
   feedback_to:st.feedback_to||'',
  }):({
   name:(st.name||'').trim(),
   kind:st.kind||'report',
   adapter_type:st.adapter_type||'cli_agy',
   model:(st.model||'').trim(),
   approval:!!st.approval,
   feedback_to:st.kind==='review'?(st.feedback_to||''):'',
   prompt:(st.prompt||'').trim(),
  }));
  const created=await api('/projects/register',{method:'POST',body:{
   name:$('pm-name').value.trim(),
   workdir:$('pm-workdir').value.trim(),
   stages:stagesPayload,
   proxy_url:$('pm-proxy').value.trim(),
   max_duration_s:Number($('pm-max-duration')?.value||900),
   max_model_calls:Number($('pm-max-calls')?.value||15),
   allow_patch_probe:!!$('pm-patch-probe').checked,
  }});
  $('project-dialog').close();
  projects=(await api('/projects')).projects;
  if(typeof applyProjects==='function')applyProjects();
  if(typeof restoreListFilters==='function')restoreListFilters();
  if(pmReturnToTask&&$('project-select')){
   $('project-select').value=created.id;
   if(typeof renderTaskScope==='function')renderTaskScope();
   renderModelSettings();
  }
  pmReturnToTask=false;
  toast(`项目 ${created.name}（${stagesPayload.length} 个动态节点）已热加载，可直接启动任务。`);
 }catch(error){toast(error.message);}finally{btn.disabled=false;}
};
function renderDiffLinesInto(box,text){
 box.replaceChildren();
 for(const line of String(text||'').replace(/\r\n/g,'\n').split('\n')){
  let cls='diff-ctx';
  if(line.startsWith('diff --git')||line.startsWith('--- ')||line.startsWith('+++ '))cls='diff-file';
  else if(line.startsWith('@@'))cls='diff-hunk';
  else if(line.startsWith('+'))cls='diff-add';
  else if(line.startsWith('-'))cls='diff-del';
  box.append(el('div','diff-line '+cls,line||' '));
 }
}
if($('arena-run'))$('arena-run').onclick=async()=>{
 const pid=$('arena-project')?.value;
 const task=($('arena-task')?.value||'').trim();
 if(!pid){toast('请先选择 Git 项目');return;}
 if(!task){toast('请输入竞技场编码任务');return;}
 const btn=$('arena-run'),status=$('arena-status'),box=$('arena-results');
 btn.disabled=true;status.textContent='正在两个隔离 git worktree 中并行执行方案 A 与方案 B，请稍候…';show(status,true);box.replaceChildren();
 try{
  const res=await api(`/projects/${encodeURIComponent(pid)}/arena`,{method:'POST',body:{
   task,
   test_preset:$('arena-test-preset')?.value||'',
   candidates:[
    {label:'A',adapter_type:$('arena-type-a')?.value||'cli_agy',model:($('arena-model-a')?.value||'').trim()},
    {label:'B',adapter_type:$('arena-type-b')?.value||'cli_agy',model:($('arena-model-b')?.value||'').trim()},
   ],
  }});
  status.textContent=`竞技场 ${res.arena_id} 完成 · 基线 ${(res.base_sha||'').slice(0,12)} · 请对比差异后人工决定是否采纳。`;
  for(const c of res.candidates||[]){
   const card=el('div','arena-card');
   const head=el('div','arena-card-head');
   const testBadge=c.test_exit_code===null?'未测':(c.test_exit_code===0?'测试通过':'测试失败('+c.test_exit_code+')');
   head.append(el('strong','',`方案 ${c.label} · ${ADAPTER_TYPE_LABELS[c.adapter_type]||c.adapter_type} / ${c.model||'默认'}`),
               el('span','meta-chip',`${c.ok?'完成':'失败'} · ${testBadge} · ${c.duration_s}s · ${num(c.tokens||0)} tok`));
   card.append(head);
   if(c.error)card.append(el('p','form-note',`错误：${c.error}`));
   if(c.changed_files?.length)card.append(el('p','form-note',`改动文件：${c.changed_files.join(', ')}`));
   const diffBox=el('div','diff-view arena-diff');
   if(c.diff)renderDiffLinesInto(diffBox,c.diff);
   else diffBox.append(el('div','diff-line diff-ctx','（无代码改动）'));
   card.append(diffBox);
   if(c.commit_sha){
    const applyBtn=el('button','primary small',`采纳方案 ${c.label}（ff-only 合并）`);applyBtn.type='button';
    applyBtn.onclick=async()=>{
     applyBtn.disabled=true;
     try{
      const r=await api(`/projects/${encodeURIComponent(pid)}/arena/apply`,{method:'POST',body:{
       arena_id:res.arena_id,label:c.label,expected_base_sha:res.base_sha,
      }});
      toast(`已采纳方案 ${c.label}，工作区已安全快进至 ${(r.head_sha||'').slice(0,12)}。`);
     }catch(err){toast(err.message);}finally{applyBtn.disabled=false;}
    };
    card.append(applyBtn);
   }
   box.append(card);
  }
 }catch(error){status.textContent='竞技场执行失败：'+error.message;toast(error.message);}finally{btn.disabled=false;}
};
