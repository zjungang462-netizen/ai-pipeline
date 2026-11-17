'use strict';
let taskTemplates=[],templateGeneration=0,failureAfter=null,failureDone=false,failureBusy=false;
const failureCounts=new Map(),failureSeen=new Set();
function renderBudgetAlerts(run){
 const box=$('budget-alert'),alerts=run.budget_alerts||[];
 box.textContent=alerts.map(a=>a.dimension==='tokens'?`token 额度已使用 ${Math.round(a.ratio*100)}%（含预留）`:a.dimension==='wall_clock'?`运行期限剩余 ${Math.ceil(a.remaining_s)} 秒`:`计量时长额度已使用 ${Math.round(a.ratio*100)}%`).join('；');
 if(alerts.length)box.textContent+='。请核对当前进度；提示不会增加预算。';show(box,alerts.length>0);
 show($('token-progress'),!!run.budget?.max_tokens);
 const modelBox=$('active-models');
 modelBox.textContent=(run.models||[]).map(m=>`${m.name}: ${m.model||'工具默认'}${m.reasoning_effort?' · 思考 '+m.reasoning_effort:''}${m.thinking?' · thinking '+m.thinking:''}${m.review_json_mode?' · JSON审查':''}`).join('；')+(run.budget?.unlimited_tokens?'；本次不限制 token，仍受工具上限、时长和轮次约束。':'');
 show(modelBox,!!modelBox.textContent);
}

function renderModelSettings(){
 const project=projects.find(p=>p.id===$('project-select').value),box=$('model-settings');box.replaceChildren();
 for(const model of project?.models||[]){
  const row=el('div','model-setting'),label=el('label','',model.name+' · '+model.type),input=el('input');input.value=model.model;input.placeholder='使用已登录工具默认模型';input.maxLength=160;input.dataset.adapter=model.name;input.dataset.option='model';label.append(input);row.append(label);
  for(const [field,enabled,values,title] of [['reasoning_effort',model.supports_effort,['none','low','medium','high','xhigh'],'思考强度'],['thinking',model.supports_thinking,['enabled','disabled'],'深度思考（端点需支持）']]){
   if(!enabled)continue;const fieldLabel=el('label','',title),select=el('select');select.dataset.adapter=model.name;select.dataset.option=field;
   for(const value of ['',...values]){const option=el('option','',value||'沿用配置/模型默认');option.value=value;select.append(option);}select.value=model[field]||'';fieldLabel.append(select);row.append(fieldLabel);
  }
  if(model.supports_thinking){const label=el('label','','结构化审查 JSON（端点需支持）'),select=el('select');select.dataset.adapter=model.name;select.dataset.option='review_json_mode';
   for(const [value,text] of [['','沿用配置'],['true','启用'],['false','禁用']]){const option=el('option','',text);option.value=value;select.append(option);}select.value=model.review_json_mode?'true':'';label.append(select);row.append(label);
  }box.append(row);
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
 if(runs.length>1)$('compare-right').value=runs[1].id;$('insights-dialog').showModal();
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
