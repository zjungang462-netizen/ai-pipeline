'use strict';
const wbInsightsI18n=globalThis.WorkbenchI18n||null;
const insFill=(value,vars)=>String(value??'').replace(/\{(\w+)\}/g,(match,key)=>vars&&Object.prototype.hasOwnProperty.call(vars,key)?String(vars[key]):match);
function it(key,fallback,vars){return insFill(wbInsightsI18n?wbInsightsI18n.t(key,fallback,vars):(fallback||key),vars);}
function insStatus(value){return wbInsightsI18n?wbInsightsI18n.status(value,typeof labels!=='undefined'?labels[value]:undefined):(typeof labels!=='undefined'&&labels[value])||value;}
function insStage(value){return wbInsightsI18n?wbInsightsI18n.stage(value,typeof stages!=='undefined'?stages[value]:undefined):(typeof stages!=='undefined'&&stages[value])||value;}
function insAdapter(value){return wbInsightsI18n?wbInsightsI18n.adapter(value,ADAPTER_TYPE_LABELS[value]):(ADAPTER_TYPE_LABELS[value]||value);}
function insArena(value){return wbInsightsI18n?wbInsightsI18n.arena(value,typeof arenaLabels!=='undefined'?arenaLabels[value]:undefined):(typeof arenaLabels!=='undefined'&&arenaLabels[value])||value;}
function insError(value){const text=value&&typeof value==='object'?(value.message||String(value)):String(value??'');return wbInsightsI18n?wbInsightsI18n.error(text):text;}
function iel(tag,cls,key,fallback,vars){const node=el(tag,cls,it(key,fallback,vars));node.dataset.i18n=key;node.dataset.i18nOriginal=fallback;node.dataset.i18nVars=JSON.stringify(vars||{});return node;}
function insToastT(key,fallback,vars){toast(it(key,fallback,vars),undefined,{key,fallback,vars});}
let taskTemplates=[],templateGeneration=0,failureAfter=null,failureDone=false,failureBusy=false,failurePageMeta={errors:0,more:false};
const failureCounts=new Map(),failureSeen=new Set();
function renderBudgetAlerts(run){
 const box=$('budget-alert'),alerts=run.budget_alerts||[];
 box.textContent=alerts.map(a=>a.dimension==='tokens'?it('budget.tokens','token 额度已使用 {percent}%（含预留）',{percent:Math.round(a.ratio*100)}):a.dimension==='wall_clock'?it('budget.wallClock','运行期限剩余 {seconds} 秒',{seconds:Math.ceil(a.remaining_s)}):it('budget.metered','计量时长额度已使用 {percent}%',{percent:Math.round(a.ratio*100)})).join('; ');
 if(alerts.length)box.textContent+=(wbInsightsI18n?.isEnglish()?'. ':'')+it('budget.hint','。请核对当前进度；提示不会增加预算。');show(box,alerts.length>0);
 show($('token-progress'),!!run.budget?.max_tokens);
 const modelBox=$('active-models');
 const callLimits=[];
 if(run.budget?.max_model_calls)callLimits.push(it('budget.modelCalls','模型调用上限 {count} 次',{count:num(run.budget.max_model_calls)}));
 if(run.budget?.max_unknown_calls)callLimits.push(it('budget.unknownCalls','未知用量上限 {count} 次',{count:num(run.budget.max_unknown_calls)}));
 const separator=wbInsightsI18n?.isEnglish()?'; ':'；';
 modelBox.textContent=(run.models||[]).map(m=>`${m.name}: ${m.model||m.effective_model||it('budget.defaultModel','工具默认')}${m.reasoning_effort?' · '+it('budget.reasoning','思考 {value}',{value:m.reasoning_effort}):''}${m.thinking?' · '+it('budget.thinking','thinking {value}',{value:m.thinking}):''}${m.review_json_mode?' · '+it('budget.jsonReview','JSON审查'):''}${m.observed_models?.length?' · '+it('budget.observedModels','实报模型 {value}',{value:m.observed_models.join(', ')}):''}${m.observed_backend_endpoints?.length?' · '+it('budget.observedEndpoints','实报连接 {value}',{value:m.observed_backend_endpoints.join(', ')}):m.backend_endpoint?' · '+it('budget.endpoint','连接配置 {value}',{value:m.backend_endpoint}):''}`).join(separator)+(run.budget?.unlimited_tokens?separator+it('budget.unlimited','本次不限制 token，仍受工具上限、时长和轮次约束。'):'')+(callLimits.length?separator+callLimits.join(wbInsightsI18n?.isEnglish()?', ':'，')+(wbInsightsI18n?.isEnglish()?'.':'。'):'')+(run.usage?.qoder_credits!=null?separator+it('budget.credits','Qoder 已报告 {value} credits；token 计数不完整，不能按免费调用理解。',{value:run.usage.qoder_credits}):'');
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
   if(force)insToastT('models.refreshFailed','刷新本机可用模型失败：{error}',{error:insError(error)});
   return liveModelCatalog;
  }finally{catalogLoading=null;}
 })();
 return catalogLoading;
}

const modelValueKey=(name,option)=>name+'\0'+option;
function modelSettingsValues(){const values={};for(const node of $('model-settings')?.querySelectorAll('[data-adapter][data-option]')||[])values[modelValueKey(node.dataset.adapter,node.dataset.option)]=node.value;return values;}
function renderModelSettings(values=null){
 const previous=values||{},project=projects.find(p=>p.id===$('project-select').value),box=$('model-settings');box.replaceChildren();
 if(project&&!liveModelCatalog&&!catalogLoading&&typeof api==='function'){
  loadModelCatalog(false).then(cat=>{if(cat&&$('project-select').value===project.id)renderModelSettings(modelSettingsValues());});
 }
 for(const model of project?.models||[]){
  const card=el('div','adapter-card');
  const head=el('div','adapter-card-head');
  const left=el('div','adapter-card-title-group');
  left.append(el('span','adapter-card-name',model.name));
  const stageNames=(model.stages||[]).map(s=>insStage(s));
  if(stageNames.length)left.append(iel('span','adapter-stage-pill','models.responsible','负责：{stages}',{stages:stageNames.join(' / ')}));
  const entry=catalogEntryFor(model.type);
  const typeNode=el('span','adapter-type-pill',insAdapter(model.type));typeNode.dataset.adapterType=model.type;
  if(entry?.source==='live_cli'||entry?.source==='live_api')typeNode.append(iel('span',null,'models.live',' · 实探 {count} 模型',{count:entry.models.length}));
  head.append(left,typeNode);
  card.append(head);

  const modelKey=modelValueKey(model.name,'model');
  const currentVal=Object.prototype.hasOwnProperty.call(previous,modelKey)?previous[modelKey]:(model.model||model.effective_model||'');
  const discovered=catalogModelsFor(model.type);
  const seen=new Set();
  const optionsList=[];
  if(currentVal){seen.add(currentVal);const found=discovered.find(m=>m.id===currentVal);optionsList.push({id:currentVal,label:found?found.label:currentVal});}
  for(const item of discovered){if(!seen.has(item.id)){seen.add(item.id);optionsList.push(item);}}
  const presetIds=optionsList.map(m=>m.id);

  const pickerRow=el('div','model-picker-row');
  const presetLabel=iel('label',null,'models.available',`选择系统可用模型（共 ${optionsList.length} 个）`,{count:optionsList.length});
  const presetSelect=el('select','model-preset-select');
  presetSelect.append(Object.assign(iel('option',null,'common.customDefault','自定义 / 工具默认'),{value:''}));
  for(const item of optionsList){
   const text=item.label&&item.label!==item.id?`${item.label} · ${item.id}`:item.id;
   presetSelect.append(Object.assign(el('option','',text),{value:item.id}));
  }
  presetSelect.value=presetIds.includes(currentVal)?currentVal:'';
  presetLabel.append(presetSelect);

  const inputLabel=iel('label',null,'models.id','模型标识（可手动输入）');
  const input=el('input');input.value=currentVal;input.placeholder=it('models.default','留空使用已登录工具默认模型');input.dataset.i18nPlaceholder='models.default';input.dataset.i18nPlaceholderOriginal='留空使用已登录工具默认模型';input.maxLength=160;input.dataset.adapter=model.name;input.dataset.option='model';
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
  for(const [field,enabled,values,key,title] of [['reasoning_effort',model.supports_effort,['cli_qoder','cli_claude'].includes(model.type)?['low','medium','high','xhigh','max']:['none','low','medium','high','xhigh'],'models.reasoning','思考强度'],['thinking',model.supports_thinking,model.type==='cli_qoder'?['auto','adaptive','disabled']:['enabled','disabled'],'models.thinking','深度思考']]){
   if(!enabled)continue;const fieldLabel=iel('label',null,key,title),select=el('select');select.dataset.adapter=model.name;select.dataset.option=field;
   for(const value of ['',...values]){const option=value?el('option','',value):iel('option',null,'common.configDefault','沿用配置/默认');option.value=value;select.append(option);}const fieldKey=modelValueKey(model.name,field);select.value=Object.prototype.hasOwnProperty.call(previous,fieldKey)?previous[fieldKey]:(model[field]||'');fieldLabel.append(select);extraGrid.append(fieldLabel);
  }
  if(model.type==='openai_compat'){const label=iel('label',null,'models.json','结构化审查 JSON'),select=el('select');select.dataset.adapter=model.name;select.dataset.option='review_json_mode';
   for(const [value,key,text] of [['','common.configDefault','沿用配置'],['true','models.enabled','启用'],['false','models.disabled','禁用']]){const option=iel('option',null,key,text);option.value=value;select.append(option);}const modeKey=modelValueKey(model.name,'review_json_mode');select.value=Object.prototype.hasOwnProperty.call(previous,modeKey)?previous[modeKey]:(model.review_json_mode?'true':'');label.append(select);extraGrid.append(label);
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
function renderTaskTemplateOptions(){
 const select=$('task-template'),previous=select.value;
 select.replaceChildren(Object.assign(iel('option',null,'task.noTemplate','不使用模板'),{value:''}));
 select.disabled=!!parent;$('template-save').disabled=!!parent;if(parent)return;
 select.append(...taskTemplates.map(t=>{const option=el('option','',t.name+(t.usable?'':it('templates.unavailable',' · 配置变化/不可用')));option.value=t.id;option.disabled=!t.usable;return option;}));
 select.value=taskTemplates.some(t=>t.id===previous)?previous:'';
}
async function loadTaskTemplates(derive){
 const generation=++templateGeneration;renderTaskTemplateOptions();if(derive)return;
 try{const data=await api('/templates');if(generation!==templateGeneration||parent)return;taskTemplates=data.templates;renderTaskTemplateOptions();}
 catch(error){if(generation===templateGeneration)insToastT('templates.loadFailed','无法读取任务模板：{error}',{error:insError(error)});}
}
$('task-template').onchange=()=>{
 const t=taskTemplates.find(t=>t.id===$('task-template').value);if(!t)return;
 const p=projects.find(p=>p.id===t.project_id);
 if(!t.usable||!p||p.config_hash!==t.config_hash){insToastT('templates.stale','模板配置已变化，请刷新并核对执行范围。');return;}
 $('project-select').value=t.project_id;$('task-input').value=t.task;renderTaskScope();renderModelSettings();
};
$('template-save').onclick=async()=>{
 const project=projects.find(p=>p.id===$('project-select').value);
 if(parent||!taskProjectsFresh||!project||project.unavailable){insToastT('templates.refresh','请刷新并选择可用项目配置。');return;}
 const button=$('template-save');button.disabled=true;
 try{await api('/templates',{method:'POST',body:{project_id:project.id,name:$('template-name').value,task:$('task-input').value,expected_config_hash:project.config_hash}});await loadTaskTemplates(false);insToastT('templates.saved','模板已保存；使用模板后仍需核对并启动。');}
 catch(error){toast(insError(error));}finally{button.disabled=false;}
};
let latestCompareData=null;
function compareText(data){
 const rows=[it(data.same_config?'compare.configSame':'compare.configDifferent',data.same_config?'配置一致':'配置不同或未记录，不能直接判断优劣')];
 for(const [key,labelKey,label] of [['tokens','compare.tokens','预算计量 tokens'],['reported_tokens','compare.reported','已报告 tokens'],['reserved_tokens','compare.reserved','预留 tokens'],['duration_s','compare.duration','计量秒数']]){
  const delta=data.delta[key];rows.push(it('compare.delta',`${label}差值（对照−基准）：{value}`,{label:it(labelKey,label),value:delta==null?it('compare.unknown','未知'):num(delta)}));
 }
 rows.push(it('compare.result','基准：{left}；对照：{right}；实际账单费用未报告。',{left:insStatus(data.left.lifecycle),right:insStatus(data.right.lifecycle)}));return rows.join('\n');
}
function renderArenaScope(){const select=$('arena-project'),button=$('arena-run'),note=$('arena-scope-note');if(!select||!button)return false;const mode=projects.find(p=>p.id===select.value)?.execution_scope?.mode;const blocked=mode!=='development';button.disabled=blocked;if(note){note.textContent=it('insights.arenaGuard','离线演练、固定测试或分析流程不会启动本机真实 CLI；请使用代码开发项目。');show(note,blocked);}return blocked;}
$('insights-open').onclick=()=>{
 for(const id of ['compare-left','compare-right'])$(id).replaceChildren(...runs.map(r=>{const option=el('option','',r.run_id+' · '+(r.task||'').slice(0,60));option.value=r.id;return option;}));
 if(runs.length>1)$('compare-right').value=runs[1].id;
 for(const id of ['lessons-project','route-project','arena-project']){
  const sel=$(id);if(!sel)continue;
  const previous=sel.value;
  const options=projects.filter(p=>!p.unavailable).map(p=>{const o=el('option','',p.name);o.value=p.id;return o;});
  sel.replaceChildren(...options);
  if(options.some(option=>option.value===previous))sel.value=previous;
 }
 renderArenaScope();$('insights-dialog').showModal();
 if(!$('insights-pane-arena')?.classList.contains('hidden'))loadArenaJobs();
};
$('compare-run').onclick=async()=>{
 const params=new URLSearchParams({left:$('compare-left').value,right:$('compare-right').value});
 try{const result=await api('/compare?'+params);latestCompareData=result;$('compare-result').textContent=compareText(result);show($('compare-result'));}catch(error){toast(insError(error));}
};
function renderFailureSummary(){
 const {errors,more}=failurePageMeta,separator=wbInsightsI18n?.isEnglish()?'; ':'；';
 $('failure-summary').textContent=it('insights.failureSummary','已累计 {total} 条失败记录（类别可重叠）：{counts}{errors}{more}',{total:failureSeen.size,counts:[...failureCounts].map(([k,v])=>k+' '+v).join(separator),errors:errors?it('insights.pageErrors','；本页 {count} 条元数据不可读',{count:errors}):'',more:failureDone?it('insights.scanComplete','；已扫描至当前末尾'):more?it('insights.moreHistory','；还有历史页'):''});
 $('failures-next').textContent=failureDone?it('insights.rescan','重新扫描失败记录'):it('insights.nextFailures','扫描下一页失败记录');
}
$('failures-next').onclick=async()=>{
 if(failureBusy)return;
 if(failureDone){failureAfter=null;failureDone=false;failurePageMeta={errors:0,more:false};failureCounts.clear();failureSeen.clear();$('failure-results').replaceChildren();}
 failureBusy=true;$('failures-next').disabled=true;
 try{
  const params=new URLSearchParams({limit:'50'});if(failureAfter)params.set('after',failureAfter);
  const page=await api('/failures?'+params);
  if(page.has_more&&(!page.next_after||page.next_after===failureAfter))throw Error(it('insights.scanStalled','扫描未推进，请重试'));
  for(const row of page.failures){if(failureSeen.has(row.id))continue;failureSeen.add(row.id);
   for(const key of row.categories)failureCounts.set(key,(failureCounts.get(key)||0)+1);
   const button=el('button','artifact-button',row.run_id+' · '+row.categories.join(' / '));button.onclick=()=>{$('insights-dialog').close();select(row.id);};$('failure-results').append(button);
  }
  failureAfter=page.next_after;failureDone=!page.has_more;
  failurePageMeta={errors:page.errors.length,more:page.has_more};renderFailureSummary();
 }catch(error){toast(insError(error));}finally{failureBusy=false;$('failures-next').disabled=false;}
};

// 自优化看板标签页 + 经验沉淀 + 模型路由推荐 + 可视化新增项目
let latestLessonsProposal=null,latestLessonsData=null,latestRouteData=null;
function renderLessons(data){
 if(!data)return;
 latestLessonsData=data;
 const list=$('lessons-list'),status=$('lessons-status');list.replaceChildren();
 const items=data.proposal?.items||[],approvedItems=data.approved?.items||[];
 status.textContent=it('lessons.status','已扫描 {runs} 次历史运行：发现 {pending} 条高频待审批经验；当前已生效 {approved} 条。',{runs:data.proposal?.scanned_runs||0,pending:items.length,approved:approvedItems.length});
 show(status,true);
 if($('lessons-approve'))$('lessons-approve').disabled=!items.length;
 for(const item of items){const card=el('div','opt-card');card.append(iel('div','opt-card-title','lessons.pending','[待审批 · 出现 {count} 次] {source}',{count:item.occurrences,source:item.source}),el('div','opt-card-body',item.checklist));list.append(card);}
 for(const item of approvedItems){const card=el('div','opt-card approved');card.append(iel('div','opt-card-title','lessons.approved','[已生效 · 出现 {count} 次] {source}',{count:item.occurrences,source:item.source}),el('div','opt-card-body',item.checklist));list.append(card);}
 if(!items.length&&!approvedItems.length)list.append(iel('p','empty','lessons.empty','暂无达到阈值的高频失败经验。'));
}
if(typeof document.querySelectorAll==='function'){
 document.querySelectorAll('[data-insights-tab]').forEach(btn=>{
  btn.onclick=()=>{
   const target=btn.dataset.insightsTab;
   document.querySelectorAll('[data-insights-tab]').forEach(b=>b.classList.toggle('active',b===btn));
   for(const name of ['usage','lessons','routing','arena']){
    const pane=$('insights-pane-'+name);if(pane)show(pane,name===target);
   }
   if(target==='arena')loadArenaJobs();
   else stopArenaView();
  };
 });
}
if($('lessons-scan'))$('lessons-scan').onclick=async()=>{
 const pid=$('lessons-project')?.value;if(!pid){insToastT('lessons.selectProject','请先选择可用项目');return;}
 const minOcc=Math.max(1,Number($('lessons-min-occ')?.value||2));
 $('lessons-scan').disabled=true;
 try{const data=await api(`/projects/${encodeURIComponent(pid)}/lessons?min_occurrences=${minOcc}`);latestLessonsProposal=data.proposal;renderLessons(data);}
 catch(error){toast(insError(error));}finally{$('lessons-scan').disabled=false;}
};
if($('lessons-approve'))$('lessons-approve').onclick=async()=>{
 const pid=$('lessons-project')?.value;if(!pid||!latestLessonsProposal)return;
 $('lessons-approve').disabled=true;
 try{
  const res=await api(`/projects/${encodeURIComponent(pid)}/lessons/approve`,{method:'POST',body:{proposal:latestLessonsProposal}});
  insToastT('lessons.approvedToast','已批准 {count} 条失败经验，将自动注入后续任务提示词。',{count:res.items});
  $('lessons-scan').onclick();
 }catch(error){toast(insError(error));}finally{$('lessons-approve').disabled=false;}
};
function renderRouteRecommendations(data){
 if(!data)return;latestRouteData=data;
 const box=$('route-results');box.replaceChildren();
 const entries=Object.entries(data.recommendations||{});
 if(!entries.length){box.append(iel('p','empty','route.empty','已扫描 {count} 条终态 metrics，暂无可用模型样本。',{count:data.scanned_metrics||0}));return;}
 const kindLabels={code:['route.code','代码阶段 (code)'],report:['route.report','报告/方案阶段 (report)'],review:['route.review','审查阶段 (review)']};
 for(const [kind,rec] of entries){
  const card=el('div','opt-card');
  const recText=rec.recommended?it('route.recommended','推荐：{tool} / {model}',{tool:insAdapter(rec.recommended.tool),model:rec.recommended.model}):it('route.insufficient','暂不自动推荐（样本不足）');
  card.append(iel('div','opt-card-title','route.card',`${kindLabels[kind]?.[1]||kind} · {recommendation}`,{kind:kindLabels[kind]?it(kindLabels[kind][0],kindLabels[kind][1]):kind,recommendation:recText}),el('div','opt-card-sub',rec.reason||''));
  const wrap=el('div','md-table-wrap'),tbl=el('table','md-table'),thead=el('thead'),hr=el('tr');
  for(const [key,fallback] of [['route.status','状态'],['route.tool','工具'],['route.model','模型'],['route.samples','样本'],['route.runPass','整条通过率'],['route.stagePass','阶段通过率'],['route.medianDuration','中位耗时'],['route.medianTokens','中位 Tokens'],['route.unknownCalls','未知调用']])hr.append(iel('th',null,key,fallback));
  thead.append(hr);tbl.append(thead);
  const tbody=el('tbody');
  for(const c of rec.candidates||[]){
   const isTop=rec.recommended&&rec.recommended.tool===c.tool&&rec.recommended.model===c.model,tr=el('tr');
   const st=isTop?it('route.top','推荐'):c.eligible?it('route.candidate','候选'):it('route.smallSample','样本不足');
   for(const cell of [st,insAdapter(c.tool),c.model,String(c.runs),(c.run_pass_rate*100).toFixed(0)+'%',(c.stage_pass_rate*100).toFixed(0)+'%',c.median_duration_s!=null?c.median_duration_s+'s':'—',c.median_reported_tokens!=null?num(c.median_reported_tokens):'—',String(c.unknown_calls||0)])tr.append(el('td','',cell));
   tbody.append(tr);
  }
  tbl.append(tbody);wrap.append(tbl);card.append(wrap);box.append(card);
 }
}
if($('route-scan'))$('route-scan').onclick=async()=>{
 const pid=$('route-project')?.value;if(!pid){insToastT('route.selectProject','请先选择可用项目');return;}
 const minRuns=Math.max(1,Number($('route-min-runs')?.value||1));
 $('route-scan').disabled=true;
 try{renderRouteRecommendations(await api(`/projects/${encodeURIComponent(pid)}/route-recommend?min_runs=${minRuns}`));}
 catch(error){toast(insError(error));}finally{$('route-scan').disabled=false;}
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
 pytest:['editor.preset.pytest','pytest（python -m pytest -q）'],
 python_unittest:['editor.preset.unittest','unittest（python -m unittest discover -s tests）'],
 npm_test:['editor.preset.npmTest','npm test'],
 npm_run_build:['editor.preset.npmBuild','npm run build'],
};
let pmDraftStages=[],pmDraftApiConfig={};
async function loadProjectIntoEditor(pid,returnToTask){
 const def=await api(`/projects/${encodeURIComponent(pid)}/stages`);
 const baseName=def.name||'custom-flow';
 if($('pm-name'))$('pm-name').value=def.managed?baseName:(baseName.endsWith('-custom')?baseName:`${baseName}-custom`).slice(0,48);
 if($('pm-workdir'))$('pm-workdir').value=def.workdir||'';
 if($('pm-proxy'))$('pm-proxy').value=def.proxy_url||'';
 if($('pm-max-duration'))$('pm-max-duration').value=String(def.max_duration_s||900);
 if($('pm-max-calls'))$('pm-max-calls').value=String(def.max_model_calls||15);
 if($('pm-patch-probe'))$('pm-patch-probe').checked=def.allow_patch_probe!==false;
 pmDraftApiConfig={api_base_url:def.api_base_url||'',api_key_env:def.api_key_env||'',api_allow_insecure_http:def.api_allow_insecure_http===true};
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
 if(!projects.length){box.append(iel('p','empty','editor.noProjects','当前暂无已登记项目。'));return;}
 for(const p of projects){
  const row=el('div','pm-reg-row');
  const info=el('div','pm-reg-info');
  info.append(el('strong','',p.name),el('span','muted',p.unavailable?it('editor.unavailable','不可用：{error}',{error:insError(p.unavailable)}):(p.execution_scope?.workdir||p.config||'')));
  const actions=el('div','pm-stages-actions');
  if(!p.unavailable){
   const editBtn=iel('button','secondary small','editor.load','载入编辑');editBtn.type='button';
   editBtn.onclick=()=>loadProjectIntoEditor(p.id,false).catch(e=>toast(insError(e)));
   actions.append(editBtn);
  }
  const delBtn=iel('button','danger-outline small','editor.remove','移除登记');delBtn.type='button';
  delBtn.onclick=async()=>{
   delBtn.disabled=true;
   try{
    await api(`/projects/${encodeURIComponent(p.id)}/remove`,{method:'POST',body:{delete_config_file:true}});
    projects=(await api('/projects')).projects;
    if(typeof applyProjects==='function')applyProjects();
    if(typeof restoreListFilters==='function')restoreListFilters();
    if(typeof renderTaskScope==='function')renderTaskScope();
    renderModelSettings();
    renderRegisteredProjectsList();
    insToastT('editor.removed','已安全移除项目 {name} 的登记（未删除代码目录或历史运行）。',{name:p.name});
   }catch(e){toast(insError(e));}finally{delBtn.disabled=false;}
  };
  actions.append(delBtn);
  row.append(info,actions);
  box.append(row);
 }
}
function resetProjectDraftFromPreset(){
 const preset=$('pm-preset')?.value||'review_plan_fix';
 pmDraftApiConfig={};
 pmDraftStages=(PM_PRESET_STAGES[preset]||PM_PRESET_STAGES.review_plan_fix).map(s=>({...s}));
 renderProjectStageEditor();
}
function populateProjectModelSelect(sel,type,preferred){
 const models=catalogModelsFor(type);
 sel.replaceChildren(Object.assign(iel('option',null,'common.customDefault','自定义 / 工具默认'),{value:''}));
 for(const m of models){
  const text=m.label&&m.label!==m.id?`${m.label} · ${m.id}`:m.id;
  sel.append(Object.assign(el('option','',text),{value:m.id}));
 }
 const ids=models.map(m=>m.id);
 if(preferred&&ids.includes(preferred))sel.value=preferred;
 else if(!preferred&&models.length)sel.value=models[0].id;
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
  const nameInp=el('input','pm-node-name');nameInp.value=st.name;nameInp.maxLength=32;nameInp.placeholder=it('editor.nodeName','节点英文名（如 lead / coding）');nameInp.dataset.i18nPlaceholder='editor.nodeName';nameInp.dataset.i18nPlaceholderOriginal='节点英文名（如 lead / coding）';
  nameInp.oninput=()=>{
   const prev=st.name,next=nameInp.value.trim();
   st.name=next;
   if(prev&&prev!==next){
    for(let j=idx+1;j<pmDraftStages.length;j++){if(pmDraftStages[j].feedback_to===prev)pmDraftStages[j].feedback_to=next;}
   }
  };
  nameInp.onchange=()=>{renderProjectStageEditor();};
  const kindSel=el('select','pm-node-kind');
  for(const [k,key,v] of [['report','editor.report','方案/报告 (report)'],['code','editor.code','代码实现 (code)'],['review','editor.reviewKind','独立审查 (review)'],['test','editor.testKind','确定性测试 (test)']]){
   kindSel.append(Object.assign(iel('option',null,key,v),{value:k}));
  }
  kindSel.value=st.kind;
  kindSel.onchange=()=>{st.kind=kindSel.value;if(st.kind!=='review'&&st.kind!=='test')st.feedback_to='';if(st.kind==='test'&&!st.test_preset)st.test_preset='pytest';renderProjectStageEditor();};
  const ops=el('div','pm-node-ops');
  const upBtn=el('button','icon-button','↑');upBtn.type='button';upBtn.title=it('editor.up','上移');upBtn.dataset.i18nTitle='editor.up';upBtn.dataset.i18nTitleOriginal='上移';upBtn.disabled=idx===0;
  upBtn.onclick=()=>{const t=pmDraftStages[idx-1];pmDraftStages[idx-1]=pmDraftStages[idx];pmDraftStages[idx]=t;renderProjectStageEditor();};
  const downBtn=el('button','icon-button','↓');downBtn.type='button';downBtn.title=it('editor.down','下移');downBtn.dataset.i18nTitle='editor.down';downBtn.dataset.i18nTitleOriginal='下移';downBtn.disabled=idx===pmDraftStages.length-1;
  downBtn.onclick=()=>{const t=pmDraftStages[idx+1];pmDraftStages[idx+1]=pmDraftStages[idx];pmDraftStages[idx]=t;renderProjectStageEditor();};
  const delBtn=el('button','icon-button','×');delBtn.type='button';delBtn.title=it('editor.delete','删除节点');delBtn.dataset.i18nTitle='editor.delete';delBtn.dataset.i18nTitleOriginal='删除节点';delBtn.disabled=pmDraftStages.length<=1;
  delBtn.onclick=()=>{pmDraftStages.splice(idx,1);renderProjectStageEditor();};
  ops.append(upBtn,downBtn,delBtn);
  head.append(numBadge,nameInp,kindSel,ops);

  const body=el('div','pm-node-grid');
  if(st.kind==='test'){
   const presetSel=el('select','pm-test-preset');
   for(const [k,[key,v]] of Object.entries(TEST_PRESET_LABELS)){
    presetSel.append(Object.assign(iel('option',null,key,v),{value:k}));
   }
   presetSel.value=st.test_preset||'pytest';
   st.test_preset=presetSel.value;
   presetSel.onchange=()=>{st.test_preset=presetSel.value;};
   body.append(presetSel,iel('span','muted','editor.command','白名单确定性命令（不执行任意 shell）'));
  }else{
   const typeSel=el('select','pm-stage-type');
   for(const [k,v] of Object.entries(ADAPTER_TYPE_LABELS)){
    if(k==='fake'||k==='cli_dsh')continue;
    const entry=catalogEntryFor(k);
    const liveCount=entry?.source==='live_cli'||entry?.source==='live_api'?entry.models.length:'';
    const badge=entry?(entry.installed?it('editor.available','（可用{count}）',{count:liveCount?wbInsightsI18n?.isEnglish()?' · '+liveCount:'·'+liveCount:''}):it('editor.notFound','（未检测到）')):'';
    typeSel.append(Object.assign(el('option','',insAdapter(k)+badge),{value:k}));
   }
   typeSel.value=st.adapter_type||'cli_agy';
   const modelSel=el('select','pm-stage-model-select');
   const modelInp=el('input','pm-stage-model');
   modelInp.maxLength=160;modelInp.placeholder=it('editor.modelId','手动输入模型 ID（可选）');modelInp.dataset.i18nPlaceholder='editor.modelId';modelInp.dataset.i18nPlaceholderOriginal='手动输入模型 ID（可选）';
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
   apprLabel.append(apprChk,iel('span',null,'editor.approval','完成后需人工审批'));
   foot.append(apprLabel);
  }
  if((st.kind==='review'||st.kind==='test')&&idx>0){
   const fbWrap=el('label','pm-inline-fb');
   const fbSel=el('select');
   fbWrap.append(iel('span','pm-inline-fb-label','editor.feedback','不通过时打回：'));
   fbSel.append(Object.assign(iel('option',null,'editor.noFeedback','不自动打回'),{value:''}));
   const prevNames=[];
   for(const prev of pmDraftStages.slice(0,idx)){
    if(prev.name){prevNames.push(prev.name);fbSel.append(Object.assign(el('option','',prev.name),{value:prev.name}));}
   }
   if(st.feedback_to&&!prevNames.includes(st.feedback_to))st.feedback_to='';
   fbSel.value=st.feedback_to||'';
   fbSel.onchange=()=>{st.feedback_to=fbSel.value;};
   fbWrap.append(fbSel);foot.append(fbWrap);
  }else if(st.feedback_to){
   st.feedback_to='';
  }
  card.append(head,body,foot);
  if(st.kind!=='test'){
   const promptDetails=el('details','pm-node-prompt');
   promptDetails.append(iel('summary',null,'editor.prompt','自定义本节点提示词（留空使用角色默认）'));
   const promptTa=el('textarea');promptTa.rows=2;promptTa.maxLength=4000;promptTa.value=st.prompt||'';promptTa.placeholder=it('editor.promptPlaceholder','支持 {task}、{upstream}、{feedback} 占位符');promptTa.dataset.i18nPlaceholder='editor.promptPlaceholder';promptTa.dataset.i18nPlaceholderOriginal='支持 {task}、{upstream}、{feedback} 占位符';
   promptTa.oninput=()=>{st.prompt=promptTa.value;};
   promptDetails.append(promptTa);
   card.append(promptDetails);
  }
  list.append(card);
 });
}
if($('pm-preset'))$('pm-preset').onchange=resetProjectDraftFromPreset;
if($('pm-add-stage'))$('pm-add-stage').onclick=()=>{
 if(pmDraftStages.length>=12){insToastT('editor.maxNodes','最多支持 12 个动态节点');return;}
 const n=pmDraftStages.length+1;
 pmDraftStages.push({name:`stage_${n}`,kind:'report',adapter_type:'cli_agy',model:catalogModelsFor('cli_agy')[0]?.id||'gemini-3.8-flash-high',approval:false,feedback_to:'',prompt:''});
 renderProjectStageEditor();
};
if($('refresh-catalog-task'))$('refresh-catalog-task').onclick=async()=>{
 $('refresh-catalog-task').disabled=true;
 try{await loadModelCatalog(true);renderModelSettings();insToastT('editor.modelsRefreshed','已刷新本机可用模型目录');}finally{$('refresh-catalog-task').disabled=false;}
};
if($('apply-route-recommend'))$('apply-route-recommend').onclick=async()=>{
 const pid=$('project-select')?.value;
 if(!pid){insToastT('editor.selectProject','请先选择项目配置');return;}
 const project=projects.find(p=>p.id===pid);
 if(!project||!project.models?.length){insToastT('editor.noModels','当前项目没有可配置的模型节点');return;}
 $('apply-route-recommend').disabled=true;
 try{
  const data=await api(`/projects/${encodeURIComponent(pid)}/route-recommend?min_runs=1`);
  const recs=data.recommendations||{};
  const stageKindByName=new Map((project.execution_scope?.stages||[]).map(s=>[s.name,s.kind]));
  const applied=[];
  const inputs=Array.from($('model-settings')?.querySelectorAll('input[data-option="model"]')||[]);
  for(const model of project.models){
   const rawStages=model.stages||[];
   const kinds=rawStages.map(s=>typeof s==='string'?stageKindByName.get(s):s?.kind).filter(Boolean);
   const matchKind=kinds.find(k=>recs[k]?.recommended&&recs[k].recommended.tool===model.type);
   const rec=matchKind?recs[matchKind].recommended:null;
   if(!rec||!rec.model)continue;
   const inp=inputs.find(el=>el.dataset.adapter===model.name&&el.dataset.option==='model');
   if(inp){
    inp.value=rec.model;
    if(typeof inp.dispatchEvent==='function'&&typeof Event==='function')inp.dispatchEvent(new Event('input'));
    else if(typeof inp.oninput==='function')inp.oninput();
    const stageNames=rawStages.map(s=>{const name=typeof s==='string'?s:(s?.name||'');return insStage(name);}).filter(Boolean).join('/')||model.name;
    applied.push(`${stageNames} → ${rec.model}`);
   }
  }
  if(applied.length)insToastT('route.applied','已按历史数据填充 {count} 个节点：{nodes}',{count:applied.length,nodes:applied.join(wbInsightsI18n?.isEnglish()?', ':'，')});
  else insToastT('route.noMatch','历史终态样本不足或与当前节点工具不匹配，暂无可自动填充项。');
 }catch(error){insToastT('route.failed','获取历史推荐失败：{error}',{error:insError(error)});}finally{$('apply-route-recommend').disabled=false;}
};
if($('refresh-catalog-pm'))$('refresh-catalog-pm').onclick=async()=>{
 $('refresh-catalog-pm').disabled=true;
 try{await loadModelCatalog(true);renderProjectStageEditor();insToastT('editor.modelsRefreshed','已刷新本机可用模型目录');}finally{$('refresh-catalog-pm').disabled=false;}
};
let pmReturnToTask=false;
if($('edit-project-nodes'))$('edit-project-nodes').onclick=async()=>{
 const pid=$('project-select')?.value;
 if(!pid){insToastT('editor.selectProject','请先在上方选择一个项目配置');return;}
 $('edit-project-nodes').disabled=true;
 try{await loadProjectIntoEditor(pid,true);}catch(error){insToastT('editor.loadFailed','读取项目节点失败：{error}',{error:insError(error)});}finally{$('edit-project-nodes').disabled=false;}
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
   ...(Number.isInteger(st.max_rounds)?{max_rounds:st.max_rounds}:{}),
  }):({
   name:(st.name||'').trim(),
   kind:st.kind||'report',
   adapter_type:st.adapter_type||'cli_agy',
   model:(st.model||'').trim(),
   ...(st.adapter_type==='openai_compat'?{
    base_url:st.base_url||'',
    api_key_env:st.api_key_env||'',
    allow_insecure_http:st.allow_insecure_http===true,
   }:{}),
   approval:!!st.approval,
   feedback_to:st.kind==='review'?(st.feedback_to||''):'',
   ...(Number.isInteger(st.max_rounds)?{max_rounds:st.max_rounds}:{}),
   prompt:(st.prompt||'').trim(),
  }));
  const created=await api('/projects/register',{method:'POST',body:{
   name:$('pm-name').value.trim(),
   workdir:$('pm-workdir').value.trim(),
   stages:stagesPayload,
   proxy_url:$('pm-proxy').value.trim(),
   ...(pmDraftApiConfig.api_base_url?{api_base_url:pmDraftApiConfig.api_base_url}:{}),
   ...(pmDraftApiConfig.api_key_env?{api_key_env:pmDraftApiConfig.api_key_env}:{}),
   ...(pmDraftApiConfig.api_allow_insecure_http?{api_allow_insecure_http:true}:{}),
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
  insToastT('editor.saved','项目 {name}（{count} 个动态节点）已热加载，可直接启动任务。',{name:created.name,count:stagesPayload.length});
 }catch(error){toast(insError(error));}finally{btn.disabled=false;}
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
const arenaTerminal=new Set(['done','failed','cancelled','interrupted']);
const arenaLabels={queued:'排队',running:'运行中',testing:'验收中',done:'完成',failed:'失败',cancelled:'已取消',interrupted:'已中断'};
let arenaView=null;
let arenaListRequest=0,arenaHistoryJobs=[],arenaHistoryProject='';

function stopArenaView(){
 arenaListRequest++;
 if(!arenaView)return;
 arenaView.stream?.close();
 clearTimeout(arenaView.retry);
 arenaView=null;
}

function arenaPath(project,job=''){
 return `/projects/${encodeURIComponent(project)}/arena${job?'/'+encodeURIComponent(job):''}`;
}

function renderArenaHistory(preferred){
 const select=$('arena-history');if(!select)return;
 const previous=preferred||select.value;
 select.replaceChildren(...arenaHistoryJobs.map(job=>Object.assign(el('option','',`${insArena(job.status)} · ${job.task.slice(0,50)}`),{value:job.arena_id})));
 if(!arenaHistoryJobs.length){select.append(Object.assign(iel('option',null,'insights.noArenaHistory','暂无竞技场记录'),{value:''}));return;}
 select.value=arenaHistoryJobs.some(job=>job.arena_id===previous)?previous:arenaHistoryJobs[0].arena_id;
}
async function loadArenaJobs(preferred){
 stopArenaView();
 const request=arenaListRequest;
 const pid=$('arena-project')?.value;
 const select=$('arena-history');
 if(!select)return;
 $('arena-results').replaceChildren();
 show($('arena-status'),false);
 show($('arena-cancel'),false);
 const previous=select.value;arenaHistoryJobs=[];arenaHistoryProject=pid;renderArenaHistory();
 if(!pid)return;
 try{
  const {jobs}=await api(arenaPath(pid));
  if(request!==arenaListRequest)return;
  arenaHistoryJobs=jobs;renderArenaHistory(preferred||previous);
  if(!jobs.length)return;
  watchArena(pid,select.value);
 }catch(error){if(request===arenaListRequest)toast(insError(error));}
}

function watchArena(pid,jobId){
 stopArenaView();
 if(!pid||!jobId)return;
 arenaView={pid,jobId,path:arenaPath(pid,jobId),stream:null,retry:null,refreshing:false,pending:false};
 loadArenaSnapshot(arenaView);
}

function retryArenaSnapshot(view){
 clearTimeout(view.retry);
 view.retry=setTimeout(()=>loadArenaSnapshot(view),1500);
}

async function loadArenaSnapshot(view){
 if(!view||view!==arenaView)return;
 if(view.refreshing){view.pending=true;return;}
 view.refreshing=true;
 try{
  const job=await api(view.path);
  if(view!==arenaView)return;
  renderArenaJob(view,job);
  if(arenaTerminal.has(job.status)){
   view.stream?.close();
   view.stream=null;
   clearTimeout(view.retry);
  }else if(!view.stream&&typeof EventSource==='function'){
   const stream=new EventSource(`/api${view.path}/events?after=${job.cursor}`);
   view.stream=stream;
   stream.addEventListener('update',()=>loadArenaSnapshot(view));
   stream.onerror=()=>{
    if(view!==arenaView)return;
    stream.close();
    view.stream=null;
    retryArenaSnapshot(view);
   };
  }else if(!view.stream){
   retryArenaSnapshot(view);
  }
 }catch(error){
  if(view!==arenaView)return;
  $('arena-status').textContent=insError(error);
  show($('arena-status'),true);
  if(!error.status||error.status>=500)retryArenaSnapshot(view);
 }finally{
  view.refreshing=false;
  if(view.pending){view.pending=false;loadArenaSnapshot(view);}
 }
}

function renderArenaJob(view,job){
 const {pid}=view;view.job=job;
 if(arenaHistoryProject===pid&&arenaHistoryJobs.some(item=>item.arena_id===job.arena_id)){
  arenaHistoryJobs=arenaHistoryJobs.map(item=>item.arena_id===job.arena_id?job:item);renderArenaHistory(job.arena_id);
 }
 const box=$('arena-results'),status=$('arena-status'),cancel=$('arena-cancel');
 const candidateStates=job.candidates.map(c=>`${c.label} ${insArena(c.status)}`).join(' · ');
 const headline=job.cancel_requested?it('arena.cancelling','正在取消'):insArena(job.status);
 status.textContent=it('arena.statusLine','{status} · {candidates} · 基线 {sha}{error}',{status:headline,candidates:candidateStates,sha:job.base_sha.slice(0,12),error:job.error?' · '+it('arena.error','错误：{error}',{error:insError(job.error)}):''});
 show(status,true);
 show(cancel,!arenaTerminal.has(job.status));
 cancel.disabled=job.cancel_requested;
 box.replaceChildren();
 if(!job.result)return;
 const res=job.result,canApply=job.status==='done';
 const statusByLabel=new Map(job.candidates.map(candidate=>[candidate.label,candidate.status]));
 if(res.applied_label)status.textContent+=it('arena.applied',' · 已采纳方案 {label}',{label:res.applied_label});
 for(const c of res.candidates||[]){
  const card=el('div','arena-card');
  const head=el('div','arena-card-head');
  const candidateStatus=statusByLabel.get(c.label);
  const candidateLabel=insArena(candidateStatus)||it(c.ok?'arena.done':'arena.failed',c.ok?'完成':'失败');
  const testBadge=c.test_exit_code===null?it('arena.notTested','未测'):c.test_exit_code===0?it('arena.testPassed','测试通过'):it('arena.testFailed','测试失败 ({code})',{code:c.test_exit_code});
  head.append(el('strong','',`${it('arena.candidateHead','方案 {label}',{label:c.label})} · ${insAdapter(c.adapter_type)} / ${c.model||it('arena.default','默认')}`),
              el('span','meta-chip',`${candidateLabel} · ${testBadge} · ${c.duration_s}s · ${num(c.tokens||0)} tok`));
  card.append(head);
  if(c.error&&candidateStatus!=='cancelled')card.append(el('p','form-note',it('arena.error','错误：{error}',{error:insError(c.error)})));
  if(c.changed_files?.length)card.append(el('p','form-note',it('arena.changedFiles','改动文件：{files}',{files:c.changed_files.join(', ')})));
  const diffBox=el('div','diff-view arena-diff');
  if(c.diff)renderDiffLinesInto(diffBox,c.diff);
  else diffBox.append(el('div','diff-line diff-ctx',it('arena.noChanges','（无代码改动）')));
  card.append(diffBox);
  if(c.diff_truncated)card.append(el('p','form-note',it('arena.truncated','差异预览已截断。')));
  if(c.apply_eligible===true&&canApply&&!res.applied_label){
   const applyBtn=el('button','primary small arena-apply-btn',it('arena.apply','采纳方案 {label}（ff-only 合并）',{label:c.label}));applyBtn.type='button';
   applyBtn.onclick=async()=>{
    applyBtn.disabled=true;
    try{
     const r=await api(`/projects/${encodeURIComponent(pid)}/arena/apply`,{method:'POST',body:{
      arena_id:res.arena_id,label:c.label,expected_base_sha:res.base_sha,
      expected_candidate_sha:c.commit_sha,expected_evidence_sha256:c.test_evidence_sha256,
     }});
     if(typeof box.querySelectorAll==='function')box.querySelectorAll('.arena-apply-btn').forEach(b=>{b.disabled=true;});
     applyBtn.textContent=it('arena.appliedButton','已采纳方案 {label} ✓',{label:c.label});
     if(view.job?.result)view.job.result.applied_label=c.label;
     insToastT('arena.appliedToast','已采纳方案 {label}，工作区已安全快进至 {sha}。',{label:c.label,sha:(r.head_sha||'').slice(0,12)});
     loadArenaSnapshot(view);
    }catch(err){applyBtn.disabled=false;toast(insError(err));}
   };
   card.append(applyBtn);
  }else if(c.apply_blocked&&candidateStatus!=='cancelled'){
   card.append(el('p','form-note',it('arena.blocked','仅供对比 · {reason}',{reason:insError(c.apply_blocked)})));
  }
  box.append(card);
 }
}

if($('arena-project'))$('arena-project').onchange=()=>{renderArenaScope();loadArenaJobs();};
if($('arena-history'))$('arena-history').onchange=()=>watchArena($('arena-project').value,$('arena-history').value);
if($('arena-cancel'))$('arena-cancel').onclick=async()=>{
 const view=arenaView;
 if(!view)return;
 const button=$('arena-cancel');
 button.disabled=true;
 try{
  await api(`${view.path}/cancel`,{method:'POST',body:{}});
  loadArenaSnapshot(view);
 }catch(error){if(view===arenaView){button.disabled=false;toast(insError(error));}}
};
$('insights-dialog')?.addEventListener?.('close',stopArenaView);
if($('arena-run'))$('arena-run').onclick=async()=>{
 const pid=$('arena-project')?.value;
 const task=($('arena-task')?.value||'').trim();
 if(!pid){insToastT('arena.selectProject','请先选择 Git 项目');return;}
 if(renderArenaScope()){insToastT('error.arenaGuard','离线演练、固定测试或分析流程不会启动本机真实 CLI；请使用代码开发项目。');return;}if(!task){insToastT('arena.enterTask','请输入竞技场编码任务');return;}
 const button=$('arena-run');
 const allowedFiles=($('arena-allowed-files')?.value||'').split(/\r?\n/).map(line=>line.trim()).filter(Boolean);
 button.disabled=true;
 try{
  const job=await api(arenaPath(pid),{method:'POST',body:{
   task,
   test_preset:$('arena-test-preset')?.value||'',
   ...(allowedFiles.length?{allowed_files:allowedFiles}:{}),
   candidates:['a','b'].map(label=>({label:label.toUpperCase(),
    adapter_type:$('arena-type-'+label)?.value||'cli_agy',model:($('arena-model-'+label)?.value||'').trim()})),
  }});
  if(pid===$('arena-project').value&&$('insights-dialog').open&&!$('insights-pane-arena').classList.contains('hidden')){
   await loadArenaJobs(job.arena_id);
  }
 }catch(error){toast(insError(error));}finally{renderArenaScope();}
};
function refreshInsightsLanguage(){
 renderArenaScope();renderTaskTemplateOptions();
 if($('model-settings')?.children.length)renderModelSettings(modelSettingsValues());
 if($('project-dialog')?.open){renderRegisteredProjectsList();renderProjectStageEditor();}
 if(latestCompareData){$('compare-result').textContent=compareText(latestCompareData);}
 if(failureSeen.size||failureDone)renderFailureSummary();
 if(latestLessonsData)renderLessons(latestLessonsData);
 if(latestRouteData)renderRouteRecommendations(latestRouteData);
 if(arenaView?.job)renderArenaJob(arenaView,arenaView.job);
 else if(arenaHistoryProject===$('arena-project')?.value)renderArenaHistory();
}
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('workbench:languagechange',refreshInsightsLanguage);
