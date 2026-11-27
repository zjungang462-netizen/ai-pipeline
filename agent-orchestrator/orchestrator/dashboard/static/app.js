'use strict';
const $=id=>document.getElementById(id);
const wbI18n=globalThis.WorkbenchI18n||null;
const fill=(value,vars)=>String(value??'').replace(/\{(\w+)\}/g,(match,key)=>vars&&Object.prototype.hasOwnProperty.call(vars,key)?String(vars[key]):match);
function t(key,fallback,vars){return fill(wbI18n?wbI18n.t(key,fallback,vars):(fallback||key),vars);}
function uiStatus(value){return wbI18n?wbI18n.status(value,labels[value]):(labels[value]||value);}
function uiStageStatus(value){return wbI18n?wbI18n.status(value,stageLabels[value]):(stageLabels[value]||value);}
function uiStage(value){return wbI18n?wbI18n.stage(value,stages[value]):(stages[value]||value);}
function uiAdapter(value){return wbI18n?wbI18n.adapter(value,adapterLabels[value]):(adapterLabels[value]||value);}
function uiEvent(value){return wbI18n?wbI18n.event(value,kinds[value]):(kinds[value]||value);}
function uiEl(tag,cls,key,fallback,vars){const node=el(tag,cls,t(key,fallback,vars));node.dataset.i18n=key;node.dataset.i18nOriginal=fallback;node.dataset.i18nVars=JSON.stringify(vars||{});return node;}
function uiError(value){
 if(value?.code==='error.network')return t('error.network',locale()==='en'?'Unable to reach the local workbench. Check that the service is running, then retry.':'无法连接本地工作台，请确认服务已启动后重试。');
 if(value?.code==='error.response')return t('error.response',locale()==='en'?'The local workbench returned an unreadable response. Please retry.':'本地工作台返回了无法解析的响应，请重试。');
 const text=value&&typeof value==='object'?(value.message||String(value)):String(value??'');return wbI18n?wbI18n.error(text):text;
}
function locale(){return wbI18n?wbI18n.locale():'zh-CN';}
const labels={pending:'等待执行',running:'正在运行',recovering:'恢复中',paused:'已暂停',waiting_approval:'等待审批',waiting_external:'等待外部条件',waiting_owner:'等待本人处理',done:'流程完成',failed:'执行失败',timeout:'已超时',cancelled:'已取消',awaiting_approval:'等待审批',stale:'需重新执行',interrupted:'已中断',queued:'排队中',completed:'讨论结束',applied:'已生效',rejected:'已拒绝'};
const stageLabels={...labels,done:'已完成'};
const stages={requirements:'需求分析',design:'方案设计',code_review:'代码审查',repair_plan:'修复方案',coding:'代码实现',testing:'测试验证',review:'独立审查',final_review:'终审复核',self_tests:'回归验证',environment:'环境检查',acceptance:'人工验收',architect:'架构师',developer:'开发者',skeptic:'红队质疑者',reviewer:'审查者',consensus:'圆桌共识总结',lead:'技术指挥',cross_review:'交叉审查'};
const adapterLabels={openai_compat:'兼容 API',cli_codex:'Codex CLI',cli_qwen:'Qwen CLI',cli_claude:'Claude Code',cli_qoder:'Qoder CLI',cli_agy:'反重力 CLI',cli_dsh:'DSH CLI',cli_grok:'Grok 容器',cli_doubao:'豆包 容器',fake:'离线模拟',command:'确定性命令'};
const kinds={recovery_captured:'候选恢复点已保存',recovery_applied:'验证后恢复候选文件',recovery_rejected:'恢复点校验拒绝',cli_retry_exhausted:'CLI 重试次数已耗尽',attempt_start:'开始尝试',attempt_done:'产物已归档',attempt_fail:'本次尝试失败',stage_deferred:'阶段进入等待',deferred_resumed:'按校验后的回执恢复',deferred_recovered:'从封存记录恢复等待状态',deferred_rejected:'等待回执校验拒绝',model_call_started:'等待模型响应',precheck:'执行前检查',worktree_error:'隔离测试工作区创建失败',test_start:'开始执行测试',test_pass:'真实测试通过',test_fail:'真实测试失败',test_feedback:'测试反馈至编码',review_feedback:'审查反馈至编码',repair_cycle:'进入修复循环',review_pass:'审查 JSON 通过',review_exhausted:'审查未通过',policy_approved:'预授权审批已生效',approved:'人工审批已生效',recovery_hint:'准备有界重试',rejected:'约束校验未通过',interrupted:'发现未完成尝试',control_receipt:'执行指令收据',cancelled:'运行已取消',discussion_reserved:'已预留会诊预算',discussion_settled:'会诊用量已结算',inflight_reserved:'已结算中断调用预留'};
const scopeLabels={fixed_tests:'固定测试流程',simulation:'含模拟执行器的流程',development:'代码开发流程',analysis:'分析与验证流程'};
const scopeSummaries={fixed_tests:'只执行配置中的固定测试命令；不调用模型、不执行开发阶段。测试通过只证明这些命令通过。',simulation:'包含模拟执行器；模拟阶段用于演示，不能作为真实业务开发交付。',development:'按配置调用开发执行器；完成范围以代码差异、真实测试和审查证据为准。',analysis:'此配置没有代码开发阶段；报告或测试通过不能代表已修改业务实现。'};
function scopeLabel(scope){return wbI18n?wbI18n.scopeLabel(scope):(scope?.label||scopeLabels[scope?.mode]||scope?.mode||'');}
function scopeSummary(scope){return wbI18n?wbI18n.scopeSummary(scope):(scope?.summary||scopeSummaries[scope?.mode]||'');}
const pendingRequests=new Map();
let selectGeneration=0,scopePanelKey='';
const expandedMessages=new Set();
const collapsedDays=new Set();
const rememberedFeedViews=new Map();
let unseenEvents=0,historyBusy=false,historyExhausted=false;
let historyViewing=false,latestFeedCursor=0,latestBusy=false,feedRequestGeneration=0;
let feedFollowing=true,toastTimer=0,connectionState={ok:null,key:'status.connecting',text:'连接中'};
let renderedFeed=[];
const pendingFeedEvents=new Map();
let taskProjectsFresh=true,taskProjectsGeneration=0;
let csrf='',projects=[],runs=[],current=null,stream=null,events=new Map(),tab='all',parent=null,confirmation=null,confirmationMeta=null,reloadTimer=null,firstCursor=0;
let runCursor=null,hasOlderRuns=false,runBoundary=null,runsInitialised=false,listBusy=false;
let listQueue=Promise.resolve(),refreshRequest=null,olderRequest=null,listRefreshFailed=false;
let listGeneration=0,listFilters={q:'',project_id:'',status:'all',sort:'newest'},previewRuns=null;
function readListFilters(){return {q:$('search').value.trim(),project_id:$('project-filter').value,status:$('status-filter').value||'all',sort:$('time-sort').value||'newest'};}
function writeListURL(key){const params=new URLSearchParams(location.search);for(const name of ['q','project_id','status','sort']){const value=listFilters[name];if(value&&(name!=='status'||value!=='all')&&(name!=='sort'||value!=='newest'))params.set(name,value);else params.delete(name);}if(key!==undefined)params.set('run',key);const query=params.toString();window.history.replaceState(null,'',location.pathname+(query?'?'+query:''));}
function restoreListFilters(){const params=new URLSearchParams(location.search);$('search').value=params.get('q')||'';const allProjects=el('option','',t('sidebar.allProjects','全部项目'));allProjects.value='';$('project-filter').replaceChildren(allProjects,...projects.map(p=>{const option=el('option','',p.name);option.value=p.id;return option;}));$('project-filter').value=params.get('project_id')||'';if($('project-filter').value!==(params.get('project_id')||'')){const option=el('option','',t('list.unregisteredProject','未登记项目'));option.value=params.get('project_id');$('project-filter').append(option);$('project-filter').value=option.value;}$('status-filter').value=params.get('status')||'all';$('time-sort').value=params.get('sort')==='oldest'?'oldest':'newest';listFilters=readListFilters();}
function changeListFilters(){const next=readListFilters();if(JSON.stringify(next)===JSON.stringify(listFilters)){renderRuns();return refresh();}previewRuns=previewRuns||runs;listFilters=next;collapsedDays.clear();listGeneration++;listQueue=Promise.resolve();refreshRequest=null;olderRequest=null;listBusy=false;listRefreshFailed=false;runs=[];runCursor=null;hasOlderRuns=false;runBoundary=null;runsInitialised=false;writeListURL();renderRuns();updateMoreRuns();return refresh();}
function id(){
 if(globalThis.crypto?.randomUUID)return globalThis.crypto.randomUUID();
 const bytes=new Uint8Array(16);
 if(globalThis.crypto?.getRandomValues)globalThis.crypto.getRandomValues(bytes);
 else for(let i=0;i<bytes.length;i++)bytes[i]=Math.floor(Math.random()*256);
 bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;
 const hex=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');
 return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}
const num=n=>Number(n||0).toLocaleString(locale());
const time=t=>new Date(t*1000).toLocaleTimeString(locale(),{hour:'2-digit',minute:'2-digit',second:'2-digit'});
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function show(node,yes=true){node.classList.toggle('hidden',!yes);}
let toastState=null;
function renderToastCopy(){const state=toastState;if(!state)return;const vars=Object.fromEntries(Object.entries(state.vars||{}).map(([key,value])=>[key,value&&typeof value==='object'&&typeof value.message==='string'?uiError(value):value]));const box=$('toast'),text=state.key?t(state.key,state.fallback,vars):uiError(state.text);box.replaceChildren(el('span','',text));if(state.action){const action=state.action,label=action.labelKey?t(action.labelKey,action.labelFallback):action.label,button=el('button','toast-action',label);button.type='button';button.onclick=()=>{show(box,false);action.run();};box.append(button);}}
function toast(text,action,options={}){toastState={text,action,...options};renderToastCopy();const box=$('toast');show(box);clearTimeout(toastTimer);toastTimer=setTimeout(()=>show(box,false),action?15000:5500);}
function toastT(key,fallback,vars,action){return toast(t(key,fallback,vars),action,{key,fallback,vars});}
async function fetchJson(address,options={}){
 let response;
 try{response=await fetch(address,options);}
 catch(cause){const error=Error(t('error.network',locale()==='en'?'Unable to reach the local workbench. Check that the service is running, then retry.':'无法连接本地工作台，请确认服务已启动后重试。'));error.code='error.network';error.cause=cause;throw error;}
 let data;
 try{data=await response.json();}
 catch(cause){const error=Error(t('error.response',locale()==='en'?'The local workbench returned an unreadable response. Please retry.':'本地工作台返回了无法解析的响应，请重试。'));error.code='error.response';error.status=response.status;error.cause=cause;throw error;}
 return {response,data};
}
async function api(path,options={}){
 const headers={...(options.headers||{})},requestKey=path+':'+JSON.stringify(options.body||null);
 if(options.body){headers['Content-Type']='application/json';headers['X-Workbench-CSRF']=csrf;const key=options.idem||pendingRequests.get(requestKey)||id();pendingRequests.set(requestKey,key);headers['Idempotency-Key']=key;}
 const {response,data}=await fetchJson('/api'+path,{...options,headers,body:options.body?JSON.stringify(options.body):undefined});
 if(response.ok||response.status<500)pendingRequests.delete(requestKey);
 if(!response.ok){const error=Error(data.error||t('error.request','请求失败 {status}',{status:response.status}));error.status=response.status;error.data=data;throw error;}
 return data;
}
function applyProjects(){const previous=$('project-select').value;const choose=el('option','',t('task.selectProject','请选择项目配置'));choose.value='';$('project-select').replaceChildren(choose,...projects.map(p=>{const suffix=p.unavailable?t('task.configUnavailableSuffix',' · 配置不可用'):p.execution_scope?' · '+scopeLabel(p.execution_scope):'';const o=el('option','',p.name+suffix);o.value=p.id;o.disabled=!!p.unavailable;return o;}));if(previous&&projects.some(p=>p.id===previous))$('project-select').value=previous;}
async function connect(){try{const token=new URLSearchParams(location.hash.slice(1)).get('token')||$('token').value;if(token){const {response:result,data}=await fetchJson('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});if(!result.ok)throw Error(data.error);csrf=data.csrf;window.history.replaceState(null,'',location.pathname+location.search);}else csrf=(await api('/session')).csrf;show($('login'),false);show($('app'));projects=(await api('/projects')).projects;taskProjectsFresh=true;applyProjects();restoreListFilters();await refresh();const saved=new URLSearchParams(location.search).get('run');if(saved){try{await select(saved);}catch(error){toast(uiError(error));}}}catch(error){show($('login'));show($('app'),false);$('login-error').textContent=uiError(error);}}
function setConnection(ok,text,key){connectionState={ok,text,key};$('connection').textContent=key?t(key,text):text;$('connection-dot').style.background=ok?'#97c892':'#c5a36a';}
// 同一查询的列表请求串行执行；切换查询后独立排队，房间和 SSE 保持原生命周期。
function compareRuns(a,b){const timeDifference=Number(b.created)-Number(a.created);const direction=listFilters.sort==='oldest'?-1:1;if(timeDifference)return timeDifference*direction;return (a.id===b.id?0:a.id<b.id?1:-1)*direction;}
function mergeRuns(items){const byId=new Map(runs.map(run=>[run.id,run]));items.forEach(run=>byId.set(run.id,run));runs=[...byId.values()].sort(compareRuns);}
function updateMoreRuns(){const button=$('more-runs');show(button,hasOlderRuns);button.disabled=listBusy;button.textContent=listBusy?t('list.loading','正在加载任务…'):listFilters.sort==='oldest'?t('list.loadLater','加载后续任务'):t('list.loadOlder','加载更早任务');}
function queueList(operation){const generation=listGeneration;const result=listQueue.then(async()=>{if(generation!==listGeneration)return;listBusy=true;updateMoreRuns();try{return await operation();}finally{if(generation===listGeneration){listBusy=false;updateMoreRuns();}}});listQueue=result.catch(()=>{});return result;}
async function runPage(cursor=null){const generation=listGeneration,params=new URLSearchParams({limit:'100',q:listFilters.q,project_id:listFilters.project_id,status:listFilters.status,sort:listFilters.sort});if(cursor)params.set('cursor',cursor);let page;try{page=await api('/runs?'+params.toString());}catch(error){if(generation!==listGeneration){const stale=Error(t('list.queryChanged','查询已切换'));stale.stale=true;throw stale;}throw error;}if(generation!==listGeneration){const stale=Error(t('list.queryChanged','查询已切换'));stale.stale=true;throw stale;}if(!Array.isArray(page.runs)||typeof page.has_more!=='boolean'||(page.has_more?typeof page.next_cursor!=='string'||!page.next_cursor:page.next_cursor!==null))throw Error(t('list.invalidPage','任务分页响应无效，请重试'));return page;}
function refresh(options={}){
 const background=options?.background===true;
 if(refreshRequest)return refreshRequest;
 const generation=listGeneration;
 const request=queueList(async()=>{
  const boundary=runBoundary,staged=[],seen=new Set();let cursor=null,lastPage=null;
  const includeNewTail=listFilters.sort==='oldest'&&runsInitialised&&!hasOlderRuns;
  const unfiltered=!listFilters.q&&!listFilters.project_id&&listFilters.status==='all';
  // 重新读取已加载区间，并保留最后一页完整结果，使续页游标与列表末尾一致。
  // 整个区间读取成功后替换列表，移除已不符合条件的旧摘要。
  for(;;){const page=await runPage(cursor);lastPage=page;staged.push(...page.runs.filter(run=>includeNewTail||!unfiltered||!boundary||compareRuns(run,boundary)<=0));
   if(!boundary||!page.has_more||(!includeNewTail&&page.runs.some(run=>compareRuns(run,boundary)>=0)))break;
   if(!page.runs.length||seen.has(page.next_cursor)||page.next_cursor===cursor)throw Error(t('list.stalled','任务分页未推进，请重试'));
   seen.add(page.next_cursor);cursor=page.next_cursor;
  }
  // 无筛选时沿用已加载区间和旧续页位置，避免刷新顺带载入未请求的历史。
  if(unfiltered&&boundary&&!includeNewTail){mergeRuns(staged);previewRuns=null;}
  else{runs=[];mergeRuns(staged);previewRuns=null;runCursor=lastPage.next_cursor;hasOlderRuns=lastPage.has_more;runBoundary=runs.at(-1)||null;runsInitialised=true;}
  renderRuns();listRefreshFailed=false;setConnection(true,'本地服务已连接','status.connected');
 }).catch(error=>{
  if(generation!==listGeneration||error.stale)return;
  const notify=!background||!listRefreshFailed,offline=error.code==='error.network';
  listRefreshFailed=true;setConnection(false,offline?'无法连接本地服务':'任务列表刷新失败',offline?'status.offline':'status.listFailed');
  if(notify)toastT('list.refreshFailed','任务列表刷新失败：{error}',{error},{labelKey:'action.retry',labelFallback:'重试',run:()=>refresh()});
 }).finally(()=>{if(generation===listGeneration&&refreshRequest===request)refreshRequest=null;});
 refreshRequest=request;
 return request;
}
function loadOlderRuns(){
 if(olderRequest)return olderRequest;
 if(!hasOlderRuns)return Promise.resolve();
 const generation=listGeneration;
 const request=queueList(async()=>{
  if(!hasOlderRuns)return;const cursor=runCursor,page=await runPage(cursor);
  if(page.has_more&&(!page.runs.length||page.next_cursor===cursor))throw Error(t('list.stalled','任务分页未推进，请重试'));
  mergeRuns(page.runs);runCursor=page.next_cursor;hasOlderRuns=page.has_more;
  if(page.runs.length)runBoundary=page.runs.at(-1);renderRuns();
 }).catch(error=>{if(generation===listGeneration&&!error.stale)toastT('list.loadOlderFailed','加载更早任务失败，可重试：{error}',{error:uiError(error)});}).finally(()=>{if(generation===listGeneration&&olderRequest===request)olderRequest=null;});
 olderRequest=request;
 return request;
}
$('more-runs').onclick=loadOlderRuns;
function createdDate(value){const stamp=Number(value);return Number.isFinite(stamp)&&stamp>0?new Date(stamp*1000):null;}
function dayKey(value){const date=createdDate(value);return date?[date.getFullYear(),date.getMonth()+1,date.getDate()].join('-'):'unknown';}
function dayLabel(value){const date=createdDate(value);if(!date)return t('list.timeUnknown','时间未记录');const today=new Date(),yesterday=new Date();yesterday.setDate(today.getDate()-1);const day=d=>d.toLocaleDateString(locale());return day(date)===day(today)?t('list.today','今天 · {date}',{date:day(date)}):day(date)===day(yesterday)?t('list.yesterday','昨天 · {date}',{date:day(date)}):day(date);}
function renderRuns(){
 const query=$('search').value.trim().toLowerCase(),filter=$('status-filter').value,projectFilter=$('project-filter').value;
 const source=previewRuns===null?runs:previewRuns;
 const visible=(previewRuns===null?source:source.filter(r=>(!query||String(r.task||'').toLowerCase().includes(query)||String(r.run_id||'').toLowerCase().includes(query))&&(!projectFilter||r.project_id===projectFilter)&&(filter==='all'||(filter==='failed'?['failed','timeout','cancelled'].includes(r.lifecycle):filter==='running'?['pending','running','recovering'].includes(r.lifecycle):r.lifecycle===filter)))).slice().sort(compareRuns);
 const nodes=[],groups=new Map();
 for(const r of visible){const key=dayKey(r.created);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(r);}
 const list=$('run-list'),position=list.scrollTop;
 for(const [key,items] of groups){
  const folded=collapsedDays.has(key),header=el('button','run-day'+(folded?' collapsed':''));header.type='button';header.ariaExpanded=String(!folded);header.dataset.day=key;header.title=folded?t('list.expandDay','展开此日期任务'):t('list.collapseDay','折叠此日期任务');
  header.append(el('span','day-label',dayLabel(items[0].created)),el('span','day-count',String(items.length)+(items.some(r=>r.id===current?.id)?' · '+t('list.current','当前'):'')));
  header.onclick=()=>{if(collapsedDays.has(key))collapsedDays.delete(key);else collapsedDays.add(key);renderRuns();};nodes.push(header);
  if(folded)continue;
 for(const r of items){
  const button=el('button','run-card'+(current?.id===r.id?' active':''));button.title=(r.task||r.run_id)+'\n'+r.run_id;button.dataset.runId=r.id;button.dataset.created=String(r.created||'');button.ariaCurrent=current?.id===r.id?'true':'false';
  button.append(el('div','run-name',r.task||r.run_id));
  const project=projects.find(p=>p.id===r.project_id);button.append(el('div','run-project',project?.name||t('list.localProject','本地项目')));
  const meta=el('div','run-meta'),date=createdDate(r.created),stamp=el('time','run-time',date?date.toLocaleTimeString(locale(),{hour:'2-digit',minute:'2-digit'}):t('list.timeUnknown','时间未知'));if(date){stamp.dateTime=date.toISOString();stamp.title=date.toLocaleString(locale());}
  meta.append(el('span','run-status '+r.lifecycle,uiStatus(r.lifecycle)),stamp);button.append(meta);button.onclick=()=>select(r.id);nodes.push(button);
 }
 }
 $('run-list').replaceChildren(...nodes);$('run-count').textContent=String(visible.length)+(hasOlderRuns?' +':'');$('run-count').title=t('list.loadedCount','当前已加载的匹配任务数');
 if(!visible.length)$('run-list').append(el('p','empty',t('list.empty','暂无匹配任务')));
 list.scrollTop=position;
 const filters=[];if(listFilters.q)filters.push(t('list.keyword','关键词：{value}',{value:listFilters.q}));if(listFilters.project_id)filters.push(projects.find(p=>p.id===listFilters.project_id)?.name||t('list.projectFilter','项目筛选'));if(listFilters.status!=='all')filters.push(uiStatus(listFilters.status));
 $('filter-summary').textContent=filters.join(' · ');show($('active-filters'),filters.length>0);$('clear-filters').disabled=filters.length===0;
 $('collapse-days').disabled=!visible.length||[...groups.keys()].every(key=>collapsedDays.has(key));$('expand-days').disabled=![...groups.keys()].some(key=>collapsedDays.has(key));
 updateWelcomeStats();
}
function updateWelcomeStats(){
 if($('stat-projects'))$('stat-projects').textContent=String(projects.length);
 if($('stat-runs'))$('stat-runs').textContent=String(runs.length)+(hasOlderRuns?'+':'');
 if($('stat-done')){
  const doneCnt=runs.filter(r=>r.lifecycle==='done').length;
  const waitCnt=runs.filter(r=>['waiting_approval','waiting_owner','waiting_external'].includes(r.lifecycle)).length;
  $('stat-done').textContent=`${doneCnt} / ${waitCnt}`;
 }
}

async function select(key){
 const generation=++selectGeneration;++feedRequestGeneration;
 historyBusy=false;latestBusy=false;updateFeedNavigation();
 let selected,history;
 // 两次读取都成功后再替换房间，失败时保留原动态与实时连接。
 try{selected=await api(`/runs/${encodeURIComponent(key)}/snapshot`);if(generation!==selectGeneration)return;
  history=await api(`/runs/${encodeURIComponent(key)}/events?before=${(selected.last_cursor||0)+1}&limit=100`);if(generation!==selectGeneration)return;
 }catch(error){if(generation===selectGeneration)toastT('task.readFailed','任务读取失败，可重试：{error}',{error:uiError(error)});return;}
 rememberFeedView();++feedRequestGeneration;if(reloadTimer){clearTimeout(reloadTimer);reloadTimer=null;}if(stream)stream.close();
 current=selected;$('app').classList.toggle('sidebar-open',false);$('sidebar-toggle').ariaExpanded='false';writeListURL(key);
 events.clear();pendingFeedEvents.clear();renderedFeed=[];unseenEvents=0;historyBusy=false;historyExhausted=false;historyViewing=false;feedFollowing=true;latestBusy=false;latestFeedCursor=current.last_cursor||0;firstCursor=latestFeedCursor+1;
 show($('welcome'),false);show($('room'));renderSnapshot();renderRuns();
 history.events.forEach(e=>events.set(e.cursor,e));firstCursor=history.events[0]?.cursor||firstCursor;historyExhausted=history.events.length===0;renderFeed(true);openStream(key,current.last_cursor);
}
function openStream(key,cursor){
 const source=new EventSource(`/api/runs/${encodeURIComponent(key)}/stream?after=${cursor}`);stream=source;
 const owns=()=>current?.id===key&&stream===source;
 source.onopen=()=>{if(owns())setConnection(true,'实时动态已连接','status.live');};source.onerror=()=>{if(owns())setConnection(false,'重连实时动态…','status.reconnecting');};
 source.addEventListener('update',event=>{
  if(!owns())return;let value;try{value=JSON.parse(event.data);}catch{return;}
  if(!value||!Number.isInteger(value.cursor)||value.cursor<1||typeof value.kind!=='string'||!value.payload||typeof value.payload!=='object'||Array.isArray(value.payload))return;
  latestFeedCursor=Math.max(latestFeedCursor,value.cursor);
  if(feedFollowing&&!historyViewing&&!pendingFeedEvents.size){events.set(value.cursor,value);trimEvents(events);firstCursor=events.size?Math.min(...events.keys()):firstCursor;renderFeed(true);}
  else{if(!events.has(value.cursor)&&!pendingFeedEvents.has(value.cursor))unseenEvents++;pendingFeedEvents.set(value.cursor,value);trimEvents(pendingFeedEvents);updateFeedNavigation();}
  if(!reloadTimer)reloadTimer=setTimeout(async()=>{reloadTimer=null;try{const updated=await api(`/runs/${encodeURIComponent(key)}/snapshot`);if(!owns())return;current=updated;renderSnapshot();refresh({background:true});}catch{}},600);
 });
}
function renderCLIActivity(r){const call=r.cli_activity?.[0];const box=$('cli-activity');if(!call){show(box,false);return;}const state=call.status==='running'?t('cli.running','处理中'):call.status==='stopped'?t('cli.stopped','调用已停止，等待归档'):t('cli.exited','进程已退出，等待阶段确认');const quiet=Math.max(0,Math.floor(Date.now()/1000-(call.last_output_at||Date.now()/1000)));box.textContent=`${call.channel} · ${state} · ${call.tool||t('cli.waitingOutput','等待模型输出')}${call.file?' · '+call.file:''} · ${t('cli.reported','本次已报告 {tokens} tokens',{tokens:num(call.known_tokens)})}（${call.usage_complete?t('cli.settling','待结算'):t('cli.lowerBound','仅完成响应下界')}） · ${t('cli.cache','缓存读取 {tokens}',{tokens:num(call.cache_read_tokens)})} · ${t('cli.quiet','{seconds} 秒无新输出。活动记录不代表任务完成。',{seconds:quiet})}`;show(box);}
function diagnoseStageError(msg){
 const text=String(msg||'');
 if(!text)return null;
 if(/401|403|unauthorized|api[_\s-]?key|认证|鉴权|未登录|token.*invalid/i.test(text))
  return {category:t('stage.error.auth','鉴权与凭据错误'),hint:t('stage.error.authHint','请检查对应适配器配置指定的环境变量（如 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY）或本机 CLI 登录状态。')};
 if(/version|版本|preflight|未验证|不支持.*版本|patch_probe/i.test(text))
  return {category:t('stage.error.version','适配器版本/兼容性门禁'),hint:t('stage.error.versionHint','可先运行 python -m orchestrator.cli doctor 检查工具版本，或在配置中核对适配器类型与版本白名单。')};
 if(/timeout|timed out|超时|deadline/i.test(text))
  return {category:t('stage.error.timeout','执行或响应超时'),hint:t('stage.error.timeoutHint','可调大对应阶段的 timeout_s、缩短输入上下文或降低模型思考强度（reasoning_effort）。')};
 if(/429|rate[_\s-]?limit|quota|余额|限流|budget/i.test(text))
  return {category:t('stage.error.budget','配额/限流/预算上限'),hint:t('stage.error.budgetHint','请检查服务商速率限制或适当提高 [run_budget] 中的 max_tokens / max_duration_s。')};
 if(/json|schema|review_verdict|协议/i.test(text))
  return {category:t('stage.error.protocol','结构化输出协议校验未通过'),hint:t('stage.error.protocolHint','建议启用「结构化审查 JSON」或调整审查模型参数，确保输出合法 JSON 对象。')};
 if(/zero_diff|零净变更|empty_run|未产生.*修改/i.test(text))
  return {category:t('stage.error.zeroDiff','零净变更拦截'),hint:t('stage.error.zeroDiffHint','编码阶段未对白名单文件产生实际改动；请补充明确的文件修改目标后重试。')};
 return {category:t('stage.error.generic','阶段执行异常'),hint:t('stage.error.genericHint','请展开下方执行证据或查看 attempt 日志定位具体原因，修正配置后可点击「从此阶段重跑」。')};
}
function stagePlan(scope){return (scope.stages||[]).map(s=>uiStage(s.name)+' ('+t('scope.kind.'+s.kind,{code:'开发',report:'报告',review:'审查',test:'固定测试'}[s.kind]||s.kind)+' / '+uiAdapter(s.adapter_type)+')').join(' → ');}
function scopeText(scope){return scopeSummary(scope)+' '+t('scope.workdir','实际工作目录：{path}。',{path:scope.workdir})+t('scope.stages','执行阶段：{stages}。',{stages:stagePlan(scope)})+t('scope.taskPaths','任务描述中的路径不会自动切换工作目录或增加阶段。');}
function renderExecutionScope(r){const scope=r.execution_scope,box=$('execution-scope');show(box,!!scope);if(!scope)return;box.textContent=scope.matches_run_config===false?t('scope.mismatch','无法核实当前配置与原运行一致，以下是当前登记范围，不能据此推断旧运行交付：{scope}',{scope:scopeText(scope)}):scopeText(scope);}
function taskScopeText(project,scope){if(project?.unavailable)return t('scope.unavailable','配置不可用：{error}。请修复或重新登记配置后刷新工作台；不可用配置不能创建任务。',{error:uiError(project.unavailable||'配置文件无法读取')});if(!scope)return $('project-select').value?t('scope.missing','尚未取得此配置的执行范围，请刷新工作台后再创建。'):t('scope.select','请先选择项目配置，核对实际目录和阶段后启动。');if(parent)return t('scope.derived','派生任务将使用基于当前固定 commit 的独立工作区，不使用父配置的工作目录，也不会改写父任务产物。新任务使用新的预算。执行阶段：{stages}。',{stages:stagePlan(scope)});return scopeText(scope);}
function renderTaskScope(){const project=projects.find(p=>p.id===$('project-select').value);const scope=project?.execution_scope;const box=$('task-scope');box.textContent=taskScopeText(project,scope);const usable=!!scope&&!project?.unavailable&&typeof project?.config_hash==='string';$('task-submit').disabled=!usable||!taskProjectsFresh;$('task-submit').textContent=scope?.mode==='fixed_tests'?t('task.startTests','启动固定测试'):scope?.mode==='simulation'?t('task.startDemo','启动演示流程'):t('task.submit','创建并启动');}
function renderWaitContext(r){const box=$('wait-context'),items=Object.entries(r.pending_waits||{});box.replaceChildren(...items.map(([name,wait])=>{const section=el('section','notice');section.append(el('strong','',`${uiStage(name)} · ${uiStatus(wait.outcome)}`),el('p','',wait.reason||''));if(wait.condition)section.append(el('p','',t('wait.recovery','恢复条件：{value}',{value:wait.condition})));if(wait.resume_action)section.append(el('p','',t('wait.resume','恢复后：{value}',{value:wait.resume_action})));if(wait.next_check_at)section.append(el('p','',t('wait.nextCheck','下次允许检查：{value}',{value:new Date(wait.next_check_at*1000).toLocaleString(locale())})));return section;}));show(box,items.length>0);}
async function loadTaskProjects(derive){const generation=++taskProjectsGeneration;try{const list=(await api('/projects')).projects;if(generation!==taskProjectsGeneration)return;projects=list;applyProjects();if(derive)$('project-select').value=current?.project_id||'';taskProjectsFresh=true;renderTaskScope();if(typeof renderModelSettings==='function')renderModelSettings();}catch(error){if(generation!==taskProjectsGeneration)return;taskProjectsFresh=false;toast(t('task.projectsRefreshFailed','无法刷新项目配置：{error}',{error:uiError(error)}));renderTaskScope();}}
function renderSnapshot(){
 const r=current;if(!r)return;
 if(typeof renderBudgetAlerts==='function')renderBudgetAlerts(r);renderCLIActivity(r);renderExecutionScope(r);
 $('breadcrumb').textContent=t('snapshot.breadcrumb','{project} / 任务房间',{project:projects.find(p=>p.id===r.project_id)?.name||t('list.localProject','本地项目')});
 $('run-identifier').textContent=r.run_id.toUpperCase();$('task-title').textContent=r.task||r.run_id;$('task-title').title=r.task;$('task-description').textContent=r.task||r.run_id;
 $('lifecycle').className='pill '+r.lifecycle;$('lifecycle').textContent=r.lifecycle==='done'&&r.execution_scope?.mode==='fixed_tests'&&r.execution_scope.matches_run_config!==false?t('snapshot.fixedPassed','固定测试已通过'):uiStatus(r.lifecycle);
 const values=Object.values(r.stages);$('stage-summary').textContent=t('snapshot.stagesDone','{done} / {total} 个阶段完成',{done:values.filter(s=>s.status==='done').length,total:values.length});$('updated').textContent=t('snapshot.synced','同步于 {time}',{time:new Date().toLocaleTimeString(locale(),{hour:'2-digit',minute:'2-digit'})});
 for(const action of ['pause','resume','cancel'])show($(action),r.capabilities[action]);show($('derive'),!!r.capabilities.derive);show($('discuss'),!!r.capabilities.discussion);$('derive').disabled=!r.capabilities.derive||['running','pending','recovering'].includes(r.lifecycle);$('discuss').disabled=!r.capabilities.discussion||['running','pending','recovering'].includes(r.lifecycle);const active=r.threads?.some(t=>['queued','running'].includes(t.status));if(active){for(const a of ['pause','resume','cancel','derive'])$(a).disabled=true;}else for(const a of ['pause','resume','cancel'])$(a).disabled=false;
 const notice=r.config_integrity==='changed'?t('notice.configChanged','运行绑定的配置已变化，不能用新配置批准或继续旧任务。'):r.lifecycle==='paused'?t('notice.paused','已在检查点暂停。原任务有效期和预算保持不变。'):r.lifecycle==='waiting_approval'?t('notice.approval','请核对具体产物后批准。聊天留言不会代替审批。'):!r.managed?t('notice.history','历史运行只读导入；本页面不会接管原进程或改写历史结论。'):r.lifecycle==='cancelled'?t('notice.cancelled','执行已停止。已有提交和已发生用量保留，取消不会撤销修改。'):'';$('notice').textContent=notice;show($('notice'),!!notice);
 const critical=r.config_integrity==='changed'||r.lifecycle==='waiting_approval';$('scope-summary').textContent=critical?t('scope.attention','需关注 · ')+(r.config_integrity==='changed'?t('scope.criticalChanged','运行配置已变化'):t('scope.criticalApproval','有产物等待审批')):t('scope.workflow','流程说明 · ')+(r.execution_scope?.matches_run_config===false?t('scope.historyBinding','历史配置绑定待核实'):scopeLabel(r.execution_scope)||t('scope.execution','执行范围'));const panelKey=r.id+':'+critical;if(scopePanelKey!==panelKey){$('scope-panel').open=critical;scopePanelKey=panelKey;}
 $('stage-cards').replaceChildren(...Object.entries(r.stages).map(([name,s],i)=>{
  const box=el('article','stage-card'),line=el('div','stage-line');box.dataset.stageCard=name;line.append(el('span','stage-number',String(i+1).padStart(2,'0')),el('span','',uiStage(name)),el('span','pill '+s.status,uiStageStatus(s.status)));box.append(line,el('div','stage-sub',t('stage.attempt','Attempt {attempt} · 反馈轮次 {rounds}',{attempt:s.current_attempt??'—',rounds:r.rounds[name]||0})));
  if(s.error){box.append(el('div','stage-error',s.error));const diag=diagnoseStageError(s.error);if(diag){const tip=el('div','stage-error-hint');tip.append(el('strong','',`[${diag.category}] `),el('span','',diag.hint));box.append(tip);}}
  if(r.pending_approvals[name]&&r.capabilities.approve){const button=el('button','approve',t('stage.approve','核对并批准此产物'));button.onclick=()=>confirmAction(t('stage.approveTitle','批准当前产物'),t('stage.approveBody','批准 {stage} 的第 {attempt} 次尝试。后端将再次校验输入和产物哈希。',{stage:uiStage(name),attempt:s.current_attempt}),note=>sendCommand('approve',{stage:name,target:{attempt:s.current_attempt,inputs_hash:s.inputs_hash,outputs_hash:s.outputs_hash},...(note?{note}:{})}),{withNote:true,titleKey:'stage.approveTitle',bodyKey:'stage.approveBody',bodyVars:{stage:name,attempt:s.current_attempt}});box.append(button);}
  if(r.code_stages?.includes(name)){const diff=el('button','evidence-ref',t('stage.diff','查看代码差异 ↗'));diff.onclick=()=>openDiff(name);box.append(diff);}
  if(r.capabilities.rerun){const rerun=el('button','evidence-ref',t('stage.rerun','从此阶段重跑'));rerun.onclick=()=>confirmAction(t('stage.rerunTitle','从阶段重跑'),t('stage.rerunBody','将使该阶段及下游产物失效。费用、总轮次和有效期不会重置。'),()=>sendCommand('rerun',{stage:name,revision:r.revision}),{titleKey:'stage.rerunTitle',bodyKey:'stage.rerunBody'});box.append(rerun);}
  return box;
 }));
 const u=r.usage||{};$('reported').textContent=num(u.reported_tokens??u.tokens);$('reserved').textContent=num(u.reserved_tokens);$('token-total').textContent=num(u.tokens)+' tokens';$('token-progress').value=Math.min(100,(u.tokens||0)/(r.budget?.max_tokens||12000)*100);if($('model-calls')){$('model-calls').textContent=u.model_calls!=null?(r.budget?.max_model_calls?`${num(u.model_calls)} / ${num(r.budget.max_model_calls)}`:num(u.model_calls)):'—';}if($('unknown-calls')){$('unknown-calls').textContent=r.budget?.max_unknown_calls?`${num(u.unknown_calls||0)} / ${num(r.budget.max_unknown_calls)}`:num(u.unknown_calls||0);}if($('duration-total')){const sec=Math.round(Number(u.duration_s||0));$('duration-total').textContent=sec>=60?`${Math.floor(sec/60)}m ${sec%60}s`:`${sec}s`;}$('deadline').textContent=r.deadline?t('usage.deadline','任务有效期至 {time}',{time:new Date(r.deadline*1000).toLocaleString(locale())}):t('usage.deadlineUnknown','未记录运行有效期');$('artifact-count').textContent=t('usage.artifacts','{count} 项',{count:r.artifacts.length});$('artifact-list').replaceChildren(...r.artifacts.map(a=>{const b=el('button','artifact-button',`${uiStage(a.stage)} · #${a.attempt} · ${a.name}`);b.title=a.id;b.onclick=()=>openArtifact(a.id);return b;}));if(!r.artifacts.length)$('artifact-list').append(el('p','empty',t('usage.noArtifacts','尚无已归档产物')));
 $('thread-list').replaceChildren(...r.threads.map(t=>{const item=el('div','thread-item');item.append(el('strong','',t.question),el('p','muted',`${uiStatus(t.status)} · ${t('thread.budget','上限 {tokens} tokens',{tokens:num(t.budget)})}`));if(t.result?.usage)item.append(el('p','muted',t('thread.counted','已计入分析 {tokens} tokens',{tokens:num(t.result.usage.tokens)})));if(t.result?.reason)item.append(el('p','muted',t.result.reason));return item;}));if(!r.threads.length)$('thread-list').append(el('p','empty',t('thread.empty','未发起角色会诊')));
}
function copyText(text,key='copy.default',fallback='已复制到剪贴板'){if(navigator.clipboard?.writeText){navigator.clipboard.writeText(text).then(()=>toastT(key,fallback)).catch(()=>toastT('copy.failed','复制失败，请手动选择复制'));}else toastT('copy.unsupported','当前环境不支持自动复制');}
function appendInlineMd(parent,text){
 const re=/(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^)\n]+\))/g;let last=0,m;
 while((m=re.exec(text))!==null){
  if(m.index>last)parent.append(document.createTextNode(text.slice(last,m.index)));
  const tok=m[0];
  if(tok.startsWith('`'))parent.append(el('code','md-inline-code',tok.slice(1,-1)));
  else if(tok.startsWith('**'))parent.append(el('strong','',tok.slice(2,-2)));
  else{
   const lm=tok.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
   if(lm){
    const label=lm[1],href=lm[2].trim();
    if(/^https?:\/\//i.test(href)){
     const a=el('a','md-link',label);a.href=href;a.target='_blank';a.rel='noopener noreferrer';parent.append(a);
    }else{
     const code=el('code','md-inline-code',label);code.title=href;parent.append(code);
    }
   }else parent.append(document.createTextNode(tok));
  }
  last=m.index+tok.length;
 }
 if(last<text.length)parent.append(document.createTextNode(text.slice(last)));
}
function renderMarkdown(raw){
 const root=el('div','md-body');
 const lines=String(raw||'').replace(/\r\n/g,'\n').split('\n');
 let i=0;
 while(i<lines.length){
  const line=lines[i];
  const fence=line.match(/^(\s*)```([\w.+-]*)\s*$/);
  if(fence){
   const indent=fence[1],lang=fence[2]||'text',codeLines=[];i++;
   while(i<lines.length&&!/^\s*```\s*$/.test(lines[i])){
    codeLines.push(indent&&lines[i].startsWith(indent)?lines[i].slice(indent.length):lines[i]);i++;
   }
   if(i<lines.length)i++;
   const box=el('div','md-code-block'),head=el('div','md-code-head');
   head.append(el('span','md-code-lang',lang));
   const codeText=codeLines.join('\n');
   const btn=el('button','md-code-copy',t('feed.copy','复制'));btn.type='button';btn.onclick=()=>copyText(codeText,'copy.code','代码已复制');
   head.append(btn);
   const pre=el('pre'),code=el('code','',codeText);pre.append(code);box.append(head,pre);root.append(box);
   continue;
  }
  if(!line.trim()){i++;continue;}
  if(/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)){root.append(el('hr','md-hr'));i++;continue;}
  if(line.includes('|')&&i+1<lines.length&&/^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$/.test(lines[i+1])){
   const splitRow=row=>row.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>c.trim());
   const headers=splitRow(line);i+=2;
   const wrap=el('div','md-table-wrap'),tbl=el('table','md-table'),thead=el('thead'),tr=el('tr');
   for(const hcell of headers){const th=el('th');appendInlineMd(th,hcell);tr.append(th);}
   thead.append(tr);tbl.append(thead);
   const tbody=el('tbody');
   while(i<lines.length&&lines[i].includes('|')&&lines[i].trim()){
    const rowEl=el('tr');
    for(const cell of splitRow(lines[i])){const td=el('td');appendInlineMd(td,cell);rowEl.append(td);}
    tbody.append(rowEl);i++;
   }
   tbl.append(tbody);wrap.append(tbl);root.append(wrap);continue;
  }
  const h=line.match(/^(#{1,4})\s+(.+)$/);
  if(h){const hn=el('h'+Math.min(4,h[1].length+1),'md-h');appendInlineMd(hn,h[2].trim());root.append(hn);i++;continue;}
  if(/^>\s?/.test(line)){
   const bq=el('blockquote','md-quote'),ql=[];
   while(i<lines.length&&/^>\s?/.test(lines[i])){ql.push(lines[i].replace(/^>\s?/,''));i++;}
   appendInlineMd(bq,ql.join('\n'));root.append(bq);continue;
  }
  if(/^\s*[-*+]\s+/.test(line)){
   const ul=el('ul','md-list');
   while(i<lines.length&&/^\s*[-*+]\s+/.test(lines[i])){const li=el('li');appendInlineMd(li,lines[i].replace(/^\s*[-*+]\s+/,''));ul.append(li);i++;}
   root.append(ul);continue;
  }
  if(/^\s*\d+[.)]\s+/.test(line)){
   const ol=el('ol','md-list');
   while(i<lines.length&&/^\s*\d+[.)]\s+/.test(lines[i])){const li=el('li');appendInlineMd(li,lines[i].replace(/^\s*\d+[.)]\s+/,''));ol.append(li);i++;}
   root.append(ol);continue;
  }
  const pl=[];
  while(i<lines.length&&lines[i].trim()&&!/^(\s*```|#{1,4}\s|>\s?|\s*[-*+]\s+|\s*\d+[.)]\s+|(-{3,}|\*{3,}|_{3,})\s*$)/.test(lines[i])){pl.push(lines[i]);i++;}
  const p=el('p','md-p');appendInlineMd(p,pl.join('\n'));root.append(p);
 }
 return root;
}
function roleTheme(role,source){
 if(source==='human')return {cls:'role-human',avatar:t('role.humanAvatar','你'),tag:t('role.human','人工留言')};
 if(source==='tool_result')return {cls:'role-tool',avatar:t('role.toolAvatar','测'),tag:t('role.tool','执行记录')};
 if(role==='consensus')return {cls:'role-consensus',avatar:t('role.consensusAvatar','共'),tag:t('role.consensus','共识结论')};
 if(role==='skeptic')return {cls:'role-skeptic',avatar:t('role.skepticAvatar','质'),tag:t('role.skeptic','红队质疑')};
 if(role==='architect')return {cls:'role-architect',avatar:t('role.architectAvatar','架'),tag:t('role.architect','架构师')};
 if(['review','code_review','final_review','reviewer'].includes(role))return {cls:'role-review',avatar:t('role.reviewAvatar','审'),tag:t('role.review','审查角色')};
 if(['design','repair_plan','requirements'].includes(role))return {cls:'role-plan',avatar:t('role.planAvatar','案'),tag:t('role.plan','方案角色')};
 if(['coding','developer'].includes(role))return {cls:'role-code',avatar:t('role.codeAvatar','码'),tag:t('role.code','实现角色')};
 return {cls:'role-agent',avatar:'AI',tag:t(source==='system_summary'?'role.systemSummary':'role.agentResponse',source==='system_summary'?'系统摘要':'角色回复')};
}
function renderStageStepper(r){
 const bar=$('stage-stepper');if(!bar)return;
 const entries=Object.entries(r.stages||{});
 if(!entries.length){show(bar,false);return;}
 const nodes=[];
 entries.forEach(([name,s],idx)=>{
  if(idx>0){
   const prevStatus=entries[idx-1][1]?.status;
   const conn=el('span','stepper-connector'+(prevStatus==='done'?' done':''),'→');
   conn.ariaHidden='true';
   nodes.push(conn);
  }
  const step=el('div','stepper-item '+s.status);
  const dot=el('span','stepper-dot',s.status==='done'?'✓':s.status==='failed'?'!':s.status==='awaiting_approval'||s.status==='waiting_approval'?t('stepper.reviewMark','审'):String(idx+1));
  const info=el('div','stepper-info');
  const attemptTag=s.current_attempt?` · #${s.current_attempt}`:'';
  info.append(el('span','stepper-name',uiStage(name)),el('span','stepper-state',uiStageStatus(s.status)+attemptTag));
  step.append(dot,info);
  if(r.pending_approvals?.[name]&&r.capabilities?.approve){
   const approveBtn=el('button','stepper-approve',t('stepper.approve','批准进入下一步'));approveBtn.type='button';
   approveBtn.onclick=()=>confirmAction(t('stage.approveTitle','批准当前产物'),t('stage.approveBody','批准 {stage} 的第 {attempt} 次尝试。后端将再次校验输入和产物哈希。',{stage:uiStage(name),attempt:s.current_attempt}),note=>sendCommand('approve',{stage:name,target:{attempt:s.current_attempt,inputs_hash:s.inputs_hash,outputs_hash:s.outputs_hash},...(note?{note}:{})}),{withNote:true,titleKey:'stage.approveTitle',bodyKey:'stage.approveBody',bodyVars:{stage:name,attempt:s.current_attempt}});
   step.append(approveBtn);
  }
  step.title=t('stepper.locate','点击定位右侧阶段执行证据');
  step.onclick=e=>{
   if(e?.target?.classList?.contains?.('stepper-approve'))return;
   $('room').classList.toggle('evidence-visible',true);
   $('evidence-toggle').ariaExpanded='true';
   const cards=$('stage-cards')?.children||[];
   for(const c of cards){
    const match=c.dataset?.stageCard===name;
    c.classList?.toggle?.('stage-card-focus',match);
    if(match&&typeof c.scrollIntoView==='function')c.scrollIntoView({behavior:'smooth',block:'nearest'});
   }
  };
  nodes.push(step);
 });
 bar.replaceChildren(...nodes);
 show(bar,true);
}
const renderSnapshotBase=renderSnapshot;renderSnapshot=function(){renderSnapshotBase();if(current){renderWaitContext(current);renderStageStepper(current);const archBtn=$('archive-run');if(archBtn){archBtn.textContent=current.archived?t('room.unarchive','取消归档'):t('room.archive','归档');archBtn.disabled=['pending','running','recovering'].includes(current.lifecycle);}}if(feedFollowing&&!historyViewing)pinFeed();};
function feedNearEnd(){const feed=$('feed');return feed.scrollHeight-feed.scrollTop-feed.clientHeight<80;}
function trimEvents(map,keepOldest=false){while(map.size>500)map.delete(keepOldest?Math.max(...map.keys()):Math.min(...map.keys()));}
function pinFeed(){$('feed-end').scrollIntoView({block:'end'});}
function rememberFeedView(){if(!current)return;rememberedFeedViews.set(current.id+':'+tab,{top:$('feed').scrollTop,follow:feedFollowing&&!historyViewing});while(rememberedFeedViews.size>32)rememberedFeedViews.delete(rememberedFeedViews.keys().next().value);}
function updateFeedNavigation(){
 const pending=unseenEvents>0;$('jump-latest').textContent=pending?t('feed.newCount','{count} 条新动态 · 查看最新 ↓',{count:unseenEvents}):t('feed.backLatest','回到最新动态 ↓');$('jump-latest').disabled=latestBusy;
 show($('new-events-bar'),!!current&&(pending||historyViewing||!feedNearEnd()));
 $('older-events').disabled=historyBusy||latestBusy||historyExhausted;$('older-events').textContent=historyBusy?t('feed.loadingEarlier','正在加载更早动态…'):historyExhausted?t('feed.earliest','已到最早动态'):t('room.older','查看更早动态');
}
function collapseStreamingDiscussion(list){
 const finalised=new Set();
 for(const e of list){
  if(e.kind==='message'&&e.payload?.thread_id&&e.payload?.role){
   const turnRef=(e.payload.refs||[]).find(r=>r&&r.turn);
   finalised.add(e.payload.thread_id+':'+(turnRef?.turn||e.payload.role));
  }
 }
 const streams=new Map(),out=[];
 for(const e of list){
  if(e.kind==='discussion_turn_start'||e.kind==='discussion_token'||e.kind==='discussion_turn_end'){
   const key=(e.payload?.thread_id||'')+':'+(e.payload?.turn||e.payload?.role||'');
   if(finalised.has(key))continue;
   let rec=streams.get(key);
   if(!rec){rec={cursor:e.cursor,created:e.created,kind:'discussion_stream',payload:{thread_id:e.payload?.thread_id||'',role:e.payload?.role||'developer',turn:e.payload?.turn||'',text:'',finished:false,status:'running'}};streams.set(key,rec);out.push(rec);}
   if(e.kind==='discussion_token'&&typeof e.payload?.delta==='string')rec.payload.text+=e.payload.delta;
   if(e.kind==='discussion_turn_end'){rec.payload.finished=true;rec.payload.status=e.payload?.status||'done';}
   rec.created=e.created;
   continue;
  }
  if(e.kind==='cli_progress'){
   const prev=out[out.length-1];
   const sig=(e.payload?.stage||'')+':'+(e.payload?.tool||'')+':'+(e.payload?.file||'');
   if(prev&&prev.kind==='cli_progress'&&prev._sig===sig){
    prev.created=e.created;
    prev.payload={...e.payload,_repeat:(prev.payload._repeat||1)+1};
    continue;
   }
   out.push({cursor:e.cursor,created:e.created,kind:e.kind,payload:{...e.payload,_repeat:1},_sig:sig});
   continue;
  }
  out.push(e);
 }
 return out;
}
function renderFeed(scroll=false){
 const feed=$('feed'),top=feed.scrollTop,feedTop=typeof feed.getBoundingClientRect==='function'?feed.getBoundingClientRect().top:0;
 const anchor=renderedFeed.find(item=>typeof item.node.getBoundingClientRect==='function'&&item.node.getBoundingClientRect().bottom>feedTop),offset=anchor?anchor.node.getBoundingClientRect().top-feedTop:0;
 const old=new Map(renderedFeed.map(item=>[item.cursor,item]));
 const all=collapseStreamingDiscussion([...events.values()].sort((a,b)=>a.cursor-b.cursor).filter(e=>tab==='all'||e.kind==='message'||e.kind.startsWith('discussion')));
 renderedFeed=all.map(e=>{const signature=JSON.stringify(e),saved=old.get(e.cursor);return saved?.signature===signature?saved:{cursor:e.cursor,signature,node:renderEvent(e)};});
 $('events').replaceChildren(...renderedFeed.map(item=>item.node));if(!all.length)$('events').append(el('p','empty',tab==='chat'?t('feed.emptyChat','暂无消息与会诊'):t('feed.emptyAll','等待第一条已持久化的动态…')));
 if(scroll)$('feed-end').scrollIntoView({block:'end'});
 else if(anchor&&renderedFeed.some(item=>item.node===anchor.node))feed.scrollTop=top+anchor.node.getBoundingClientRect().top-feedTop-offset;
 else feed.scrollTop=top;
 updateFeedNavigation();
}
async function loadEarlierEvents(){
 if(!current||historyBusy||latestBusy||historyExhausted)return;const key=current.id,generation=selectGeneration,request=++feedRequestGeneration;historyBusy=true;updateFeedNavigation();
 try{const value=await api(`/runs/${encodeURIComponent(key)}/events?before=${firstCursor}&limit=100`);if(generation!==selectGeneration||request!==feedRequestGeneration||current?.id!==key)return;
  if(!value.events.length){historyExhausted=true;return;}
  value.events.forEach(e=>events.set(e.cursor,e));trimEvents(events,true);firstCursor=Math.min(...events.keys());historyViewing=true;feedFollowing=false;renderFeed(false);
 }catch(error){if(generation===selectGeneration&&request===feedRequestGeneration)toast(uiError(error));}finally{if(generation===selectGeneration&&request===feedRequestGeneration){historyBusy=false;updateFeedNavigation();}}
}
async function jumpLatest(){
 if(!current||latestBusy)return;const key=current.id,generation=selectGeneration,request=++feedRequestGeneration;latestBusy=true;historyBusy=false;updateFeedNavigation();
 try{const snapshot=await api(`/runs/${encodeURIComponent(key)}/snapshot`);if(generation!==selectGeneration||request!==feedRequestGeneration)return;
  const end=Math.max(latestFeedCursor,snapshot.last_cursor||0);const history=await api(`/runs/${encodeURIComponent(key)}/events?before=${end+1}&limit=100`);if(generation!==selectGeneration||request!==feedRequestGeneration)return;
  events.clear();history.events.forEach(e=>events.set(e.cursor,e));pendingFeedEvents.forEach(e=>events.set(e.cursor,e));pendingFeedEvents.clear();trimEvents(events);
  latestFeedCursor=Math.max(end,events.size?Math.max(...events.keys()):0);firstCursor=events.size?Math.min(...events.keys()):latestFeedCursor+1;unseenEvents=0;historyViewing=false;feedFollowing=true;historyExhausted=!events.size;renderFeed(true);
 }catch(error){if(generation===selectGeneration&&request===feedRequestGeneration)toastT('feed.latestFailed','最新动态读取失败：{error}',{error:uiError(error)});}finally{if(generation===selectGeneration&&request===feedRequestGeneration){latestBusy=false;updateFeedNavigation();}}
}
function displayBody(data){
 let body=String(data?.body||'');
 if(wbI18n&&wbI18n.isEnglish()&&data?.source==='tool_result'){
  body=body.replace(/^命令：/,'Command: ').replace(/\n退出码：/g,'\nExit code: ').replace(/\n耗时：/g,'\nDuration: ').replace(/（显示已截断，查看原始产物）/g,'(display truncated; open the original artifact)');
 }
 return body;
}
function renderEvent(e){
 const data=e.payload;
 if(e.kind==='discussion_stream'){
  const theme=roleTheme(data.role,'agent_response');
  const m=el('article','message streaming '+theme.cls);
  m.append(el('div','avatar',theme.avatar));
  const content=el('div','message-content'),head=el('div','message-head');
  head.append(el('span','message-role',uiStage(data.role)),el('span','source-label live-badge',data.finished?t('feed.streamDone','流式会诊已完成'):t('feed.streaming','实时流式输出')),el('time','',time(e.created)));
  const body=el('div','message-body');
  if(data.text)body.append(renderMarkdown(data.text));else body.append(el('span','muted',t('feed.thinking','正在思考并生成建议…')));
  if(!data.finished)body.append(el('span','stream-cursor'));
  content.append(head,body,el('div','message-meta-bar',null));
  content.lastChild.append(el('span','meta-chip',t('feed.preview','会诊实时预览 · 最终以落盘归档为准')));
  m.append(content);return m;
 }
 if(e.kind==='cli_progress'){
  const item=el('div','event event-cli');item.append(el('span','event-icon cli','⚡'));
  const body=el('div');const rep=data._repeat>1?t('feed.cliRefreshed',' · 已刷新 {count} 次',{count:data._repeat}):'';
  body.append(el('div','event-title',t('feed.cli','CLI 活动 · {tool}',{tool:data.tool||t('feed.cliDefault','处理模型响应')})+rep),el('div','event-detail',t('feed.cliDetail','{file}已报告下界 {tokens} tokens；非完成凭据',{file:data.file?data.file+' · ':'',tokens:num(data.known_tokens)})));
  item.append(body,el('time','',time(e.created)));return item;
 }
 if(e.kind==='message'){
  const theme=roleTheme(data.role,data.source);
  const m=el('article','message '+theme.cls+(data.source==='human'?' human':data.source==='tool_result'?' tool':''));
  m.append(el('div','avatar',theme.avatar));
  const content=el('div','message-content'),head=el('div','message-head');
  const sourceText=({human:t('role.human','人工留言'),agent_response:theme.tag,tool_result:t('feed.realRecord','真实执行记录'),system_summary:t('feed.offlineExample','离线示例')})[data.source]||theme.tag;
  head.append(el('span','message-role',uiStage(data.role)),el('span','source-label '+theme.cls,sourceText));
  const modelRef=(data.refs||[]).find(r=>r.returned_model);
  const cliRef=(data.refs||[]).find(r=>r.adapter_channel?.startsWith('cli_'));
  const roundRef=(data.refs||[]).find(r=>r.round);
  if(roundRef)head.append(el('span','meta-chip',t('feed.round','第 {round}/{total} 轮',{round:roundRef.round,total:roundRef.total_rounds})));
  if(cliRef){
   head.append(el('span','meta-chip',`${uiAdapter(cliRef.adapter_channel)}${cliRef.tool_version?' '+cliRef.tool_version:''}`));
   if(cliRef.cli_model||cliRef.requested_model)head.append(el('span','meta-chip model-chip-tag',cliRef.cli_model||cliRef.requested_model));
  }else if(modelRef){
   head.append(el('span','meta-chip model-chip-tag',modelRef.returned_model));
  }
  head.append(el('time','',time(e.created)));
  content.append(head);

  const rawBody=displayBody(data);
  if(data.source==='tool_result'&&rawBody.length>260){
   const messageKey=(current?.id||'')+':'+e.cursor;
   const preview=el('div','message-preview',rawBody.split('\n').filter(Boolean).slice(0,3).join('\n').slice(0,420));
   const details=el('details','message-log'),summary=el('summary','',t('feed.fullRecord','查看完整记录'));
   details.open=expandedMessages.has(messageKey);
   details.ontoggle=()=>{
    if(details.isConnected===false)return;
    if(details.open)expandedMessages.add(messageKey);else expandedMessages.delete(messageKey);
    while(expandedMessages.size>500)expandedMessages.delete(expandedMessages.values().next().value);
   };
   const bodyEl=el('div','message-body');
   bodyEl.append(el('pre','tool-output',rawBody));
   details.append(summary,bodyEl);
   content.append(preview,details);
  }else{
   const bodyWrap=el('div','message-body-wrap');
   const bodyEl=el('div','message-body');
   if(data.source==='tool_result'){
    const pre=el('pre','tool-output',rawBody);bodyEl.append(pre);
   }else{
    bodyEl.append(renderMarkdown(rawBody));
   }
   bodyWrap.append(bodyEl);
   const isLong=data.source!=='human'&&rawBody.length>900;
   if(isLong){
    const messageKey=(current?.id||'')+':'+e.cursor;
    let expanded=expandedMessages.has(messageKey);
    bodyWrap.classList.toggle('collapsible',!expanded);
    const toggle=el('button','expand-toggle',expanded?t('feed.collapse','收起内容 ⌃'):t('feed.expand','展开完整报告 ⌄'));toggle.type='button';
    toggle.onclick=()=>{
     expanded=!expanded;
     if(expanded)expandedMessages.add(messageKey);else expandedMessages.delete(messageKey);
     while(expandedMessages.size>500)expandedMessages.delete(expandedMessages.values().next().value);
     bodyWrap.classList.toggle('collapsible',!expanded);
     toggle.textContent=expanded?t('feed.collapse','收起内容 ⌃'):t('feed.expand','展开完整报告 ⌄');
    };
    bodyWrap.append(toggle);
   }
   content.append(bodyWrap);
  }

  const footer=el('div','message-footer');
  if(data.thread_id)footer.append(el('span','meta-chip',data.role==='consensus'?t('feed.consensus','圆桌共识 · 可一键采纳执行'):t('feed.suggestion','会诊建议 · 未改变任务结论')));
  if(data.thread_id&&rawBody.trim()){
   const pendingStage=Object.keys(current?.pending_approvals||{})[0];
   if(pendingStage&&current?.capabilities?.approve){
    const s=current.stages?.[pendingStage];
    const adoptAppr=el('button','evidence-ref adopt-btn',t('feed.adoptNote','采纳为审批附注 ↗'));adoptAppr.type='button';
    adoptAppr.onclick=()=>{
     confirmAction(t('confirm.adoptTitle','批准当前产物（已带入讨论结论）'),t('confirm.adoptBody','批准 {stage} 的第 {attempt} 次尝试，并把下方修订指令注入下游阶段。',{stage:uiStage(pendingStage),attempt:s?.current_attempt}),note=>sendCommand('approve',{stage:pendingStage,target:{attempt:s.current_attempt,inputs_hash:s.inputs_hash,outputs_hash:s.outputs_hash},...(note?{note}:{})}),{withNote:true,titleKey:'confirm.adoptTitle',bodyKey:'confirm.adoptBody',bodyVars:{stage:pendingStage,attempt:s?.current_attempt}});
     if($('confirm-note'))$('confirm-note').value=rawBody.slice(0,3800);
    };
    footer.append(adoptAppr);
   }
   const adoptTask=el('button','evidence-ref',t('feed.newTask','基于结论新建任务 ↗'));adoptTask.type='button';
   adoptTask.onclick=()=>{
    newTask(false);
    if(current?.project_id&&$('project-select'))$('project-select').value=current.project_id;
    if($('task-input'))$('task-input').value=`基于多模型圆桌共识执行：\n${rawBody.slice(0,6000)}`;
    renderTaskScope();
    if(typeof renderModelSettings==='function')renderModelSettings();
   };
   footer.append(adoptTask);
  }
  for(const ref of data.refs||[]){
   if(ref.artifact_id){const b=el('button','evidence-ref',t('feed.viewEvidence','查看执行证据 ↗'));b.type='button';b.onclick=()=>openArtifact(ref.artifact_id);footer.append(b);}
  }
  if(rawBody.trim()){
   const cp=el('button','text-button copy-msg-btn',t('feed.copy','复制'));cp.type='button';cp.onclick=()=>copyText(rawBody,'copy.message','消息内容已复制');footer.append(cp);
  }
  if(footer.children.length)content.append(footer);
  m.append(content);return m;
 }
 const isFail=e.kind==='engine_event'&&['attempt_fail','test_fail','review_exhausted','rejected','worktree_error','cli_retry_exhausted'].includes(data.kind);
 const isDone=e.kind==='engine_event'&&['attempt_done','test_pass','review_pass','approved','policy_approved'].includes(data.kind);
 const iconChar=e.kind==='command_receipt'||isDone?'✓':isFail?'!':'·';
 const iconCls=isFail?'event-icon fail':isDone||e.kind==='command_receipt'?'event-icon ok':'event-icon';
 const item=el('div','event'+(isFail?' event-fail':isDone?' event-ok':''));item.append(el('span',iconCls,iconChar));const body=el('div');let title='';let detail='';
 if(e.kind==='engine_event'){title=(data.stage?uiStage(data.stage)+' · ':'')+uiEvent(data.kind);detail=data.reason||data.error||'';if(data.n)title+=' #'+data.n;if(data.exit_code!==undefined)detail=t('event.exitCode','退出码 {code}',{code:data.exit_code})+(detail?' · '+detail:'');}
 else if(e.kind==='command_queued'){title=t('event.queued','操作已排队 · {intent}',{intent:data.intent});detail=t('event.queuedDetail','等待执行者确认，尚未生效。');}
 else if(e.kind==='command_receipt'){title=t('event.receipt','操作{status} · {intent}',{status:uiStatus(data.status),intent:data.intent});detail=data.result?.reason||(data.result?.target?t('event.receiptDetail','{stage} · Attempt {attempt} · 输入与产物哈希已验证',{stage:uiStage(data.result.stage),attempt:data.result.target.attempt}):data.result?.lifecycle?uiStatus(data.result.lifecycle):JSON.stringify(data.result||{}));}
 else if(e.kind==='snapshot_changed'){title=t('event.snapshot','运行状态 · {status}',{status:uiStatus(data.lifecycle)});}
 else if(e.kind==='discussion_queued'){title=t('event.discussionQueued','角色会诊已排队');detail=data.question;}
 else if(e.kind==='discussion_status'){title=t('event.discussion','会诊 · {status}',{status:uiStatus(data.status)});detail=data.result?.reason||data.result?.note||'';}
 else if(e.kind==='run_created'){title=t('event.created','任务已创建');detail=data.task;}
 else{title=e.kind;detail=data.reason||'';}
 body.append(el('div','event-title',title));if(detail)body.append(el('div','event-detail',String(detail).slice(0,1400)));item.append(body,el('time','',time(e.created)));return item;
}
async function sendCommand(intent,payload={}){if(!current)return;try{const receipt=await api(`/runs/${encodeURIComponent(current.id)}/commands`,{method:'POST',body:{intent,payload}});toastT('command.queuedToast','指令已排队，收到收据后才算生效。');if(intent==='pause'){show($('notice'));$('notice').textContent=t('command.pauseNotice','暂停请求已提交，等待当前调用或测试归档后到达检查点。');}return receipt;}catch(e){toast(uiError(e));}}
const confirmationFallbacks={
 'stage.approveTitle':'批准当前产物','stage.approveBody':'批准 {stage} 的第 {attempt} 次尝试。后端将再次校验输入和产物哈希。',
 'stage.rerunTitle':'从阶段重跑','stage.rerunBody':'将使该阶段及下游产物失效。费用、总轮次和有效期不会重置。',
 'confirm.adoptTitle':'批准当前产物（已带入讨论结论）','confirm.adoptBody':'批准 {stage} 的第 {attempt} 次尝试，并把下方修订指令注入下游阶段。',
 'confirm.cancelTitle':'停止当前执行？','confirm.cancelBody':'将终止此任务所属的执行进程。已有提交、已发生用量和未知预留仍保留；这不会撤销已应用修改。'
};
function renderConfirmationCopy(){if(!confirmationMeta)return;const vars={...confirmationMeta.bodyVars};if(vars.stage)vars.stage=uiStage(vars.stage);$('confirm-title').textContent=confirmationMeta.titleKey?t(confirmationMeta.titleKey,confirmationFallbacks[confirmationMeta.titleKey]):confirmationMeta.title;$('confirm-body').textContent=confirmationMeta.bodyKey?t(confirmationMeta.bodyKey,confirmationFallbacks[confirmationMeta.bodyKey],vars):confirmationMeta.body;}
function confirmAction(title,body,fn,opts={}){confirmationMeta={title,body,...opts};renderConfirmationCopy();const wrap=$('confirm-note-wrap'),input=$('confirm-note');if(wrap&&input){input.value='';show(wrap,!!opts.withNote);}confirmation=fn;$('confirm-dialog').showModal();}
function renderUnifiedDiff(text){
 const box=$('artifact-diff-view');box.replaceChildren();
 const lines=String(text||'').replace(/\r\n/g,'\n').split('\n');
 for(const line of lines){
  let cls='diff-ctx';
  if(line.startsWith('diff --git')||line.startsWith('--- ')||line.startsWith('+++ '))cls='diff-file';
  else if(line.startsWith('@@'))cls='diff-hunk';
  else if(line.startsWith('+'))cls='diff-add';
  else if(line.startsWith('-'))cls='diff-del';
  box.append(el('div','diff-line '+cls,line||' '));
 }
}
let artifactView=null;
function renderArtifactChrome(){if(!artifactView)return;if(artifactView.type==='artifact'){const a=artifactView.data;$('artifact-tag').textContent=`${uiStage(a.stage)} / ATTEMPT ${a.attempt}`;$('artifact-title').textContent=a.name;$('artifact-integrity').textContent=t('artifact.'+a.integrity,{valid:'内容哈希校验通过。',changed:'警告：文件内容已经变化，不是原审批证据。',missing:'原产物缺失。'}[a.integrity]||a.integrity)+(a.truncated?t('artifact.truncated',' 显示已截断。'):'');}else{const d=artifactView.data,stage=artifactView.stage;$('artifact-tag').textContent=t('diff.tag','代码产物 · SHA 校验');$('artifact-title').textContent=t('diff.title','代码差异 · {stage}',{stage:uiStage(stage)});$('artifact-integrity').textContent=t('diff.compare','比较 {base} → {head}',{base:(d.base||t('diff.emptyTree','空树')).slice(0,12),head:d.head.slice(0,12)})+(d.truncated?t('diff.truncated',' · 显示已截断'):'');if(!d.body)$('artifact-body').textContent=t('diff.empty','相对所选基线没有代码差异');}}
async function openArtifact(identifier){try{const encoded=identifier.split('/').map(encodeURIComponent).join('/');const a=await api(`/runs/${encodeURIComponent(current.id)}/artifacts/${encoded}`);artifactView={type:'artifact',data:a};renderArtifactChrome();$('artifact-body').textContent=a.body;show($('artifact-body'),true);if($('artifact-diff-view'))show($('artifact-diff-view'),false);$('artifact-dialog').showModal();}catch(e){toast(uiError(e));}}
async function openDiff(stage){try{const d=await api(`/runs/${encodeURIComponent(current.id)}/diff/${encodeURIComponent(stage)}`);artifactView={type:'diff',data:d,stage};renderArtifactChrome();$('artifact-body').textContent=d.body||t('diff.empty','相对所选基线没有代码差异');if(d.body&&$('artifact-diff-view')){renderUnifiedDiff(d.body);show($('artifact-body'),false);show($('artifact-diff-view'),true);}else{show($('artifact-body'),true);if($('artifact-diff-view'))show($('artifact-diff-view'),false);}$('artifact-dialog').showModal();}catch(e){toast(uiError(e));}}
function renderTaskDialogCopy(){const derive=!!parent;$('task-dialog-title').textContent=derive?t('task.deriveTitle','创建关联的新任务'):t('task.newTitle','新建任务');$('task-note').textContent=derive?t('task.deriveNote','使用当前固定 commit 创建独立工作区；原任务、预算、审批和历史结论保留。新任务使用新的预算。'):t('task.newNote','执行内容由所选配置决定，请先核对工作目录与执行范围。');}
function newTask(derive=false){parent=derive?current:null;if($('unlimited-tokens'))$('unlimited-tokens').checked=true;if($('model-settings'))$('model-settings').replaceChildren();renderTaskDialogCopy();$('task-input').value='';$('project-select').disabled=derive;if(derive)$('project-select').value=current?.project_id||'';else $('project-select').value='';taskProjectsFresh=false;renderTaskScope();$('task-dialog').showModal();loadTaskProjects(derive);if(typeof loadTaskTemplates==='function')loadTaskTemplates(derive);}
$('sidebar-toggle').onclick=()=>{const open=$('app').classList.toggle('sidebar-open');$('sidebar-toggle').ariaExpanded=String(open);};$('evidence-toggle').onclick=()=>{const open=$('room').classList.toggle('evidence-visible');$('evidence-toggle').ariaExpanded=String(open);};
$('evidence-close').onclick=()=>{$('room').classList.toggle('evidence-visible',false);$('evidence-toggle').ariaExpanded='false';};
$('collapse-days').onclick=()=>{(previewRuns||runs).forEach(r=>collapsedDays.add(dayKey(r.created)));renderRuns();};
$('expand-days').onclick=()=>{collapsedDays.clear();renderRuns();};
$('clear-filters').onclick=()=>{$('search').value='';$('project-filter').value='';$('status-filter').value='all';return changeListFilters();};
$('feed').onscroll=()=>{feedFollowing=historyViewing?false:feedNearEnd();updateFeedNavigation();};$('jump-latest').onclick=jumpLatest;
$('connect').onclick=connect;$('refresh').onclick=refresh;$('search').oninput=changeListFilters;$('status-filter').onchange=changeListFilters;$('time-sort').onchange=changeListFilters;$('project-filter').onchange=changeListFilters;$('project-select').onchange=renderTaskScope;$('new-run').onclick=()=>newTask();$('welcome-new').onclick=()=>newTask();$('derive').onclick=()=>newTask(true);
$('pause').onclick=()=>sendCommand('pause');$('resume').onclick=()=>{const entry=Object.entries(current?.stages||{}).find(([,s])=>['waiting_external','waiting_owner'].includes(s.status));return sendCommand('resume',entry&&current.lifecycle!=='paused'?{wait_target:[entry[0],entry[1].current_attempt,entry[1].outputs_hash]}:{});};$('cancel').onclick=()=>confirmAction(t('confirm.cancelTitle','停止当前执行？'),t('confirm.cancelBody','将终止此任务所属的执行进程。已有提交、已发生用量和未知预留仍保留；这不会撤销已应用修改。'),()=>sendCommand('cancel'),{titleKey:'confirm.cancelTitle',bodyKey:'confirm.cancelBody'});
if($('export-report'))$('export-report').onclick=async()=>{if(!current)return;const btn=$('export-report');btn.disabled=true;try{const data=await api(`/runs/${encodeURIComponent(current.id)}/export-report`);const blob=new Blob([data.markdown||''],{type:'text/markdown;charset=utf-8'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=data.filename||'report.md';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toastT('task.exported','已导出交付报告：{filename}',{filename:data.filename});}catch(e){toast(uiError(e));}finally{btn.disabled=false;}};
if($('archive-run'))$('archive-run').onclick=async()=>{if(!current)return;const btn=$('archive-run');btn.disabled=true;try{const next=!current.archived;await api(`/runs/${encodeURIComponent(current.id)}/archive`,{method:'POST',body:{archived:next}});current.archived=next;renderSnapshot();await refresh();toastT(next?'task.archived':'task.restored',next?'任务已归档隐藏（磁盘证据完好保留，可在状态筛选「已归档」中查看）':'任务已恢复至主列表');}catch(e){toast(uiError(e));}finally{btn.disabled=false;}};
$('confirm-form').onsubmit=event=>{event.preventDefault();const note=$('confirm-note')&&!$('confirm-note-wrap')?.classList.contains('hidden')?$('confirm-note').value.trim():'';$('confirm-dialog').close();if(confirmation)confirmation(note);};document.querySelectorAll('.close-dialog').forEach(button=>button.onclick=()=>button.closest('dialog').close());
$('task-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter||$('task-submit');const derive=!!parent;const project=projects.find(p=>p.id===(derive?current?.project_id:$('project-select').value));if(!taskProjectsFresh){toastT('task.configPending','项目配置尚未刷新完成，请稍后再试。');renderTaskScope();return;}if(!project||project.unavailable||!project.execution_scope||typeof project.config_hash!=='string'){toastT(project?.unavailable?'task.configUnavailable':'task.scopeUnknown',project?.unavailable?'配置不可用：{error}。请修复或重新登记配置后刷新工作台。':'无法确认配置执行范围，请刷新后重试。',project?.unavailable?{error:uiError(project.unavailable)}:{});renderTaskScope();return;}button.disabled=true;try{const data=await api('/runs',{method:'POST',body:{project_id:project.id,task:$('task-input').value,parent_id:derive?parent.id:null,expected_config_hash:project.config_hash,...(typeof executionOptions==='function'?{execution_options:executionOptions()}:{} )}});$('task-dialog').close();await refresh();await select(data.id);}catch(e){if(e.status===409&&e.data?.run_id){await loadTaskProjects(derive);$('task-dialog').close();toast(uiError(e),{labelKey:'task.conflictAction',labelFallback:'查看该任务',run:()=>select(e.data.run_id)});return;}toast(uiError(e));if(e.status===409)await loadTaskProjects(derive);else renderTaskScope();}};
$('message-form').onsubmit=async event=>{event.preventDefault();if(!current||!$('message').value.trim())return;const body=$('message').value;try{await api(`/runs/${encodeURIComponent(current.id)}/messages`,{method:'POST',body:{body}});$('message').value='';}catch(e){toast(uiError(e));}};
function syncDiscussionSeatOptions(){const defaultTool=current?.models?.some(m=>m.type==='openai_compat')?'openai_compat':(current?.models?.some(m=>m.type==='cli_agy')?'cli_agy':'openai_compat');for(const r of ['architect','developer','skeptic','reviewer']){const sel=$('adapter-'+r),dl=$('dl-models-'+r);if(!dl)continue;const tool=sel?.value||defaultTool;const models=typeof catalogModelsFor==='function'?catalogModelsFor(tool):[];dl.replaceChildren(...models.map(m=>{const o=document.createElement('option');const id=typeof m==='string'?m:(m?.id||'');o.value=id;if(m&&typeof m==='object'&&m.label&&m.label!==id)o.textContent=m.label;return o;}).filter(o=>o.value));}}
for(const r of ['architect','developer','skeptic','reviewer']){if($('adapter-'+r))$('adapter-'+r).onchange=syncDiscussionSeatOptions;}
$('discuss').onclick=async()=>{if(typeof loadModelCatalog==='function')await loadModelCatalog(false);syncDiscussionSeatOptions();$('discussion-dialog').showModal();};$('discussion-form').onsubmit=async event=>{event.preventDefault();const roles=[],role_models={},role_adapters={};for(const r of ['architect','developer','skeptic','reviewer']){if($('role-'+r)?.checked){roles.push(r);const mv=($('model-'+r)?.value||'').trim();if(mv)role_models[r]=mv;const av=($('adapter-'+r)?.value||'').trim();if(av)role_adapters[r]=av;}}if(!roles.length){toastT('discussion.selectRole','请至少勾选 1 个讨论角色');return;}const files=($('discussion-files')?.value||'').split(/[,，\n]/).map(s=>s.trim()).filter(Boolean).slice(0,4);const rounds=Number($('discussion-rounds')?.value||2);const consensus=!!$('discussion-consensus')?.checked;const button=event.submitter;button.disabled=true;try{await api(`/runs/${encodeURIComponent(current.id)}/discussions`,{method:'POST',body:{question:$('question').value,roles,budget:Number($('discussion-budget').value),rounds,role_models,role_adapters,context_files:files,consensus}});$('discussion-dialog').close();toastT('discussion.started','已发起 {rounds} 轮多模型圆桌对话（{seats} 个席位）。',{rounds,seats:roles.length});}catch(e){toast(uiError(e));}finally{button.disabled=false;}};
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{rememberFeedView();tab=button.dataset.tab;document.querySelectorAll('[data-tab]').forEach(b=>{b.classList.toggle('active',b===button);b.ariaSelected=String(b===button);});const view=rememberedFeedViews.get(current?.id+':'+tab);feedFollowing=!!view?.follow;renderFeed(feedFollowing);if(!feedFollowing)$('feed').scrollTop=view?.top||0;updateFeedNavigation();});
$('older-events').onclick=loadEarlierEvents;
if($('artifact-copy'))$('artifact-copy').onclick=()=>copyText($('artifact-body').textContent,'copy.artifact','产物全文已复制');
const THEMES=[{id:'light',key:'theme.light',label:'◐ 日间'},{id:'dark',key:'theme.dark',label:'☾ 暗黑'},{id:'kiroween',key:'theme.kiroween',label:'🎃 Kiroween'}];
function themeLabel(index){const theme=THEMES[index];return t(theme.key,theme.label);}
let currentThemeIndex=0;
function applyTheme(index){
 currentThemeIndex=((index%THEMES.length)+THEMES.length)%THEMES.length;
 const theme=THEMES[currentThemeIndex];
 if(typeof document!=='undefined'&&document.documentElement?.setAttribute){
  if(theme.id==='light')document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme',theme.id);
 }
 if($('theme-toggle')){$('theme-toggle').textContent=themeLabel(currentThemeIndex);$('theme-toggle').title=t('theme.toggleTitle','切换日间 / 暗黑 / Kiroween 主题');}
 try{if(typeof localStorage!=='undefined')localStorage.setItem('wb_theme',theme.id);}catch{}
}
try{if(typeof localStorage!=='undefined'){const saved=localStorage.getItem('wb_theme');const idx=THEMES.findIndex(x=>x.id===saved);if(idx>=0)applyTheme(idx);}}catch{}
if($('theme-toggle')){$('theme-toggle').textContent=themeLabel(currentThemeIndex);$('theme-toggle').title=t('theme.toggleTitle','切换日间 / 暗黑 / Kiroween 主题');}
if($('theme-toggle'))$('theme-toggle').onclick=()=>applyTheme(currentThemeIndex+1);
async function launchQuickDemo(){
 newTask(false);
 await loadTaskProjects(false);
 // 演练文案只对赛事模板成立；找不到模板时不回退到任意项目。
 const target=taskProjectsFresh && projects.find(p=>!p.unavailable && p.verified_showcase===true);
 if(!target){$('project-select').value='';renderTaskScope();$('task-dialog').close();toastT('demo.missing','未登记演练模板 kiroween-demo.example.toml，请先在项目管理中登记后再试。');return;}
 $('project-select').value=target.id;if($('unlimited-tokens'))$('unlimited-tokens').checked=!!target.unlimited_tokens;renderTaskScope();
 $('task-input').value=t('demo.task','【Kiroween 五阶段闭环演练 · 离线脚本】实现 normalize(value)：去除首尾空白、按空白分词、以连字符连接并转小写。观察第一次错误实现被真实测试打回，再查看修复 diff、脚本审查结论与人工审批。');
}
if($('welcome-quick-demo'))$('welcome-quick-demo').onclick=()=>launchQuickDemo();
if($('welcome-project'))$('welcome-project').onclick=()=>$('project-manage-open')?.click();
if($('welcome-insights'))$('welcome-insights').onclick=()=>$('insights-open')?.click();
let cmdIndex=0,cmdItems=[];
function buildCmdItems(q){
 const term=String(q||'').trim().toLowerCase();
 const base=[
  {tag:'ACTION',title:t('cmd.newTask','＋ 新建流水线任务'),sub:t('cmd.newTaskSub','创建并启动新的多阶段任务'),run:()=>newTask(false)},
  {tag:'DEMO',title:t('cmd.demo','⚡ 一键载入五阶段演练模板'),sub:t('cmd.demoSub','自动选择可用配置并填入标准验收任务描述'),run:()=>launchQuickDemo()},
  {tag:'BUILDER',title:t('cmd.builder','⚙ 动态编排多节点项目'),sub:t('cmd.builderSub','可视化增删排序 1~12 个模型与确定性测试节点'),run:()=>$('project-manage-open')?.click()},
  {tag:'ARENA',title:t('cmd.arena','⚔️ 打开用量看板与双模型竞技场'),sub:t('cmd.arenaSub','对比运行指标、失败经验库与双模型隔离比拼'),run:()=>$('insights-open')?.click()},
  {tag:'THEME',title:t('cmd.theme','🎨 切换界面主题（日间 / 暗黑 / Kiroween 紫金）'),sub:t('cmd.themeSub','当前主题：{theme}',{theme:themeLabel(currentThemeIndex)}),run:()=>applyTheme(currentThemeIndex+1)},
  ...(current?.capabilities?.discussion?[
   {tag:'ROOM',title:t('cmd.discuss','💬 发起多模型圆桌会诊'),sub:t('cmd.discussSub','围绕当前任务开启多角色交叉质询'),run:()=>$('discuss')?.click()}
  ]:[]),
  ...(current?[
   {tag:'EXPORT',title:t('cmd.export','📥 导出当前任务 Markdown 报告'),sub:t('cmd.exportSub','下载完整执行证据与用量报告'),run:()=>$('export-report')?.click()}
  ]:[])
 ];
 const runItems=runs.slice(0,30).map(r=>({tag:uiStatus(r.lifecycle),title:r.task||r.run_id,sub:t('cmd.taskId','任务 ID: {id}',{id:r.run_id}),run:()=>select(r.id)}));
 return [...base,...runItems].filter(item=>!term||item.title.toLowerCase().includes(term)||item.sub.toLowerCase().includes(term)||item.tag.toLowerCase().includes(term)).slice(0,18);
}
function renderCmdList(){
 const box=$('cmd-list');if(!box)return;
 cmdItems=buildCmdItems($('cmd-input')?.value||'');
 if(cmdIndex>=cmdItems.length)cmdIndex=0;
 box.replaceChildren(...cmdItems.map((item,idx)=>{
  const row=el('button','cmd-item'+(idx===cmdIndex?' active':''));row.type='button';
  const left=el('div','cmd-item-main');
  left.append(el('span','cmd-item-title',item.title),el('span','cmd-item-sub',item.sub));
  row.append(left,el('span','cmd-item-tag',item.tag));
  row.onclick=()=>{$('cmd-dialog').close();item.run();};
  return row;
 }));
 if(!cmdItems.length)box.append(el('p','empty',t('cmd.empty','没有匹配的命令或任务')));
}
function openCmdPalette(){
 if(!$('cmd-dialog'))return;
 $('cmd-input').value='';cmdIndex=0;renderCmdList();
 $('cmd-dialog').showModal();
 if(typeof $('cmd-input').focus==='function')$('cmd-input').focus();
}
if($('cmd-open'))$('cmd-open').onclick=openCmdPalette;
if($('cmd-input')){
 $('cmd-input').oninput=()=>{cmdIndex=0;renderCmdList();};
 $('cmd-input').onkeydown=e=>{
  if(e.key==='ArrowDown'){e.preventDefault();cmdIndex=Math.min(cmdItems.length-1,cmdIndex+1);renderCmdList();}
  else if(e.key==='ArrowUp'){e.preventDefault();cmdIndex=Math.max(0,cmdIndex-1);renderCmdList();}
  else if(e.key==='Enter'&&cmdItems[cmdIndex]){e.preventDefault();const chosen=cmdItems[cmdIndex];$('cmd-dialog').close();chosen.run();}
 };
}
if(typeof document!=='undefined'&&typeof document.addEventListener==='function'){
 document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){
   if(!$('app').classList.contains('hidden')){e.preventDefault();openCmdPalette();}
  }
 });
}
$('help-button').onclick=()=>toastT('help.toast','聊天建议不会覆盖测试与审批结论。暂停在检查点生效，取消不撤销提交。仅本机单用户；可写范围和预算由登记配置决定。');
function rerenderLanguage(){
 applyTheme(currentThemeIndex);applyProjects();restoreListFilters();renderRuns();updateMoreRuns();setConnection(connectionState.ok,connectionState.text,connectionState.key);
 if(current){const scroll=$('feed').scrollTop;renderSnapshot();renderedFeed=[];renderFeed(false);$('feed').scrollTop=scroll;}
 renderTaskScope();renderTaskDialogCopy();renderToastCopy();
 if($('confirm-dialog').open)renderConfirmationCopy();
 if($('cmd-dialog').open)renderCmdList();
 if($('artifact-dialog').open)renderArtifactChrome();
 if(typeof refreshInsightsLanguage==='function')refreshInsightsLanguage();
}
if(typeof document!=='undefined'&&typeof document.addEventListener==='function')document.addEventListener('workbench:languagechange',rerenderLanguage);
setInterval(()=>{if(csrf)refresh({background:true});},7000);connect();
