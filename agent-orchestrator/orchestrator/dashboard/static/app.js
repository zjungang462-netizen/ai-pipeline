'use strict';
const $=id=>document.getElementById(id);
const labels={pending:'等待执行',running:'正在运行',recovering:'恢复中',paused:'已暂停',waiting_approval:'等待审批',waiting_external:'等待外部条件',waiting_owner:'等待本人处理',done:'流程完成',failed:'执行失败',timeout:'已超时',cancelled:'已取消',awaiting_approval:'等待审批',stale:'需重新执行',interrupted:'已中断',queued:'排队中',completed:'讨论结束',applied:'已生效',rejected:'已拒绝'};
const stageLabels={...labels,done:'已完成'};
const stages={requirements:'需求分析',design:'方案设计',code_review:'代码审查',repair_plan:'修复方案',coding:'代码实现',testing:'测试验证',review:'独立审查',final_review:'终审复核',self_tests:'回归验证',environment:'环境检查',acceptance:'人工验收',architect:'架构师',developer:'开发者',skeptic:'红队质疑者',reviewer:'审查者',consensus:'圆桌共识总结'};
const adapterLabels={openai_compat:'兼容 API',cli_codex:'Codex CLI',cli_qwen:'Qwen CLI',cli_claude:'Claude Code',cli_qoder:'Qoder CLI',cli_agy:'反重力 CLI',cli_dsh:'DSH CLI',cli_grok:'Grok 容器',cli_doubao:'豆包 容器',fake:'离线模拟'};
const kinds={recovery_captured:'候选恢复点已保存',recovery_applied:'验证后恢复候选文件',recovery_rejected:'恢复点校验拒绝',cli_retry_exhausted:'CLI 重试次数已耗尽',attempt_start:'开始尝试',attempt_done:'产物已归档',attempt_fail:'本次尝试失败',stage_deferred:'阶段进入等待',deferred_resumed:'按校验后的回执恢复',deferred_recovered:'从封存记录恢复等待状态',deferred_rejected:'等待回执校验拒绝',model_call_started:'等待模型响应',precheck:'执行前检查',worktree_error:'隔离测试工作区创建失败',test_start:'开始执行测试',test_pass:'真实测试通过',test_fail:'真实测试失败',test_feedback:'测试反馈至编码',review_feedback:'审查反馈至编码',repair_cycle:'进入修复循环',review_pass:'审查 JSON 通过',review_exhausted:'审查未通过',policy_approved:'预授权审批已生效',approved:'人工审批已生效',recovery_hint:'准备有界重试',rejected:'约束校验未通过',interrupted:'发现未完成尝试',control_receipt:'执行指令收据',cancelled:'运行已取消',discussion_reserved:'已预留会诊预算',discussion_settled:'会诊用量已结算',inflight_reserved:'已结算中断调用预留'};
const pendingRequests=new Map();
let selectGeneration=0,scopePanelKey='';
const expandedMessages=new Set();
const collapsedDays=new Set();
const rememberedFeedViews=new Map();
let unseenEvents=0,historyBusy=false,historyExhausted=false;
let historyViewing=false,latestFeedCursor=0,latestBusy=false,feedRequestGeneration=0;
let renderedFeed=[];
const pendingFeedEvents=new Map();
let taskProjectsFresh=true,taskProjectsGeneration=0;
let csrf='',projects=[],runs=[],current=null,stream=null,events=new Map(),tab='all',parent=null,confirmation=null,reloadTimer=null,firstCursor=0;
let runCursor=null,hasOlderRuns=false,runBoundary=null,runsInitialised=false,listBusy=false;
let listQueue=Promise.resolve(),refreshRequest=null,olderRequest=null;
let listGeneration=0,listFilters={q:'',project_id:'',status:'all',sort:'newest'},previewRuns=null;
function readListFilters(){return {q:$('search').value.trim(),project_id:$('project-filter').value,status:$('status-filter').value||'all',sort:$('time-sort').value||'newest'};}
function writeListURL(key){const params=new URLSearchParams(location.search);for(const name of ['q','project_id','status','sort']){const value=listFilters[name];if(value&&(name!=='status'||value!=='all')&&(name!=='sort'||value!=='newest'))params.set(name,value);else params.delete(name);}if(key!==undefined)params.set('run',key);const query=params.toString();window.history.replaceState(null,'',location.pathname+(query?'?'+query:''));}
function restoreListFilters(){const params=new URLSearchParams(location.search);$('search').value=params.get('q')||'';const allProjects=el('option','','全部项目');allProjects.value='';$('project-filter').replaceChildren(allProjects,...projects.map(p=>{const option=el('option','',p.name);option.value=p.id;return option;}));$('project-filter').value=params.get('project_id')||'';if($('project-filter').value!==(params.get('project_id')||'')){const option=el('option','','未登记项目');option.value=params.get('project_id');$('project-filter').append(option);$('project-filter').value=option.value;}$('status-filter').value=params.get('status')||'all';$('time-sort').value=params.get('sort')==='oldest'?'oldest':'newest';listFilters=readListFilters();}
function changeListFilters(){const next=readListFilters();if(JSON.stringify(next)===JSON.stringify(listFilters)){renderRuns();return refresh();}previewRuns=previewRuns||runs;listFilters=next;collapsedDays.clear();listGeneration++;listQueue=Promise.resolve();refreshRequest=null;olderRequest=null;listBusy=false;runs=[];runCursor=null;hasOlderRuns=false;runBoundary=null;runsInitialised=false;writeListURL();renderRuns();updateMoreRuns();return refresh();}
const id=()=>crypto.randomUUID();
const num=n=>Number(n||0).toLocaleString('zh-CN');
const time=t=>new Date(t*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});
function el(tag,cls,text){const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;}
function show(node,yes=true){node.classList.toggle('hidden',!yes);}
function toast(text){$('toast').textContent=text;show($('toast'));setTimeout(()=>show($('toast'),false),5500);}
async function api(path,options={}){const headers={...(options.headers||{})};const requestKey=path+':'+JSON.stringify(options.body||null);if(options.body){headers['Content-Type']='application/json';headers['X-Workbench-CSRF']=csrf;const key=options.idem||pendingRequests.get(requestKey)||id();pendingRequests.set(requestKey,key);headers['Idempotency-Key']=key;}const response=await fetch('/api'+path,{...options,headers,body:options.body?JSON.stringify(options.body):undefined});const data=await response.json();if(response.ok||response.status<500)pendingRequests.delete(requestKey);if(!response.ok){const error=Error(data.error||('请求失败 '+response.status));error.status=response.status;throw error;}return data;}
function applyProjects(){const previous=$('project-select').value;const choose=el('option','','请选择项目配置');choose.value='';$('project-select').replaceChildren(choose,...projects.map(p=>{const o=el('option','',p.name+(p.unavailable?' · 配置不可用':p.execution_scope?' · '+p.execution_scope.label:''));o.value=p.id;o.disabled=!!p.unavailable;return o;}));if(previous&&projects.some(p=>p.id===previous))$('project-select').value=previous;}
async function connect(){try{const token=new URLSearchParams(location.hash.slice(1)).get('token')||$('token').value;if(token){const result=await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});const data=await result.json();if(!result.ok)throw Error(data.error);csrf=data.csrf;window.history.replaceState(null,'',location.pathname+location.search);}else csrf=(await api('/session')).csrf;show($('login'),false);show($('app'));projects=(await api('/projects')).projects;taskProjectsFresh=true;applyProjects();restoreListFilters();await refresh();const saved=new URLSearchParams(location.search).get('run');if(saved){try{await select(saved);}catch(error){toast(error.message);}}}catch(error){show($('login'));show($('app'),false);$('login-error').textContent=error.message;}}
function setConnection(ok,text){$('connection').textContent=text;$('connection-dot').style.background=ok?'#97c892':'#c5a36a';}
// 同一查询的列表请求串行执行；切换查询后独立排队，房间和 SSE 保持原生命周期。
function compareRuns(a,b){const timeDifference=Number(b.created)-Number(a.created);const direction=listFilters.sort==='oldest'?-1:1;if(timeDifference)return timeDifference*direction;return (a.id===b.id?0:a.id<b.id?1:-1)*direction;}
function mergeRuns(items){const byId=new Map(runs.map(run=>[run.id,run]));items.forEach(run=>byId.set(run.id,run));runs=[...byId.values()].sort(compareRuns);}
function updateMoreRuns(){const button=$('more-runs');show(button,hasOlderRuns);button.disabled=listBusy;button.textContent=listBusy?'正在加载任务…':listFilters.sort==='oldest'?'加载后续任务':'加载更早任务';}
function queueList(operation){const generation=listGeneration;const result=listQueue.then(async()=>{if(generation!==listGeneration)return;listBusy=true;updateMoreRuns();try{return await operation();}finally{if(generation===listGeneration){listBusy=false;updateMoreRuns();}}});listQueue=result.catch(()=>{});return result;}
async function runPage(cursor=null){const generation=listGeneration,params=new URLSearchParams({limit:'100',q:listFilters.q,project_id:listFilters.project_id,status:listFilters.status,sort:listFilters.sort});if(cursor)params.set('cursor',cursor);let page;try{page=await api('/runs?'+params.toString());}catch(error){if(generation!==listGeneration){const stale=Error('查询已切换');stale.stale=true;throw stale;}throw error;}if(generation!==listGeneration){const stale=Error('查询已切换');stale.stale=true;throw stale;}if(!Array.isArray(page.runs)||typeof page.has_more!=='boolean'||(page.has_more?typeof page.next_cursor!=='string'||!page.next_cursor:page.next_cursor!==null))throw Error('任务分页响应无效，请重试');return page;}
function refresh(){
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
   if(!page.runs.length||seen.has(page.next_cursor)||page.next_cursor===cursor)throw Error('任务分页未推进，请重试');
   seen.add(page.next_cursor);cursor=page.next_cursor;
  }
  // 无筛选时沿用已加载区间和旧续页位置，避免刷新顺带载入未请求的历史。
  if(unfiltered&&boundary&&!includeNewTail){mergeRuns(staged);previewRuns=null;}
  else{runs=[];mergeRuns(staged);previewRuns=null;runCursor=lastPage.next_cursor;hasOlderRuns=lastPage.has_more;runBoundary=runs.at(-1)||null;runsInitialised=true;}
  renderRuns();setConnection(true,'本地服务已连接');
 }).catch(error=>{if(generation!==listGeneration||error.stale)return;setConnection(false,'任务列表刷新失败');toast(error.message);}).finally(()=>{if(generation===listGeneration&&refreshRequest===request)refreshRequest=null;});
 refreshRequest=request;
 return request;
}
function loadOlderRuns(){
 if(olderRequest)return olderRequest;
 if(!hasOlderRuns)return Promise.resolve();
 const generation=listGeneration;
 const request=queueList(async()=>{
  if(!hasOlderRuns)return;const cursor=runCursor,page=await runPage(cursor);
  if(page.has_more&&(!page.runs.length||page.next_cursor===cursor))throw Error('任务分页未推进，请重试');
  mergeRuns(page.runs);runCursor=page.next_cursor;hasOlderRuns=page.has_more;
  if(page.runs.length)runBoundary=page.runs.at(-1);renderRuns();
 }).catch(error=>{if(generation===listGeneration&&!error.stale)toast('加载更早任务失败，可重试：'+error.message);}).finally(()=>{if(generation===listGeneration&&olderRequest===request)olderRequest=null;});
 olderRequest=request;
 return request;
}
$('more-runs').onclick=loadOlderRuns;
function createdDate(value){const stamp=Number(value);return Number.isFinite(stamp)&&stamp>0?new Date(stamp*1000):null;}
function dayKey(value){const date=createdDate(value);return date?[date.getFullYear(),date.getMonth()+1,date.getDate()].join('-'):'unknown';}
function dayLabel(value){const date=createdDate(value);if(!date)return '时间未记录';const today=new Date(),yesterday=new Date();yesterday.setDate(today.getDate()-1);const day=d=>d.toLocaleDateString('zh-CN');return day(date)===day(today)?'今天 · '+day(date):day(date)===day(yesterday)?'昨天 · '+day(date):day(date);}
function renderRuns(){
 const query=$('search').value.trim().toLowerCase(),filter=$('status-filter').value,projectFilter=$('project-filter').value;
 const source=previewRuns===null?runs:previewRuns;
 const visible=(previewRuns===null?source:source.filter(r=>(!query||String(r.task||'').toLowerCase().includes(query)||String(r.run_id||'').toLowerCase().includes(query))&&(!projectFilter||r.project_id===projectFilter)&&(filter==='all'||(filter==='failed'?['failed','timeout','cancelled'].includes(r.lifecycle):filter==='running'?['pending','running','recovering'].includes(r.lifecycle):r.lifecycle===filter)))).slice().sort(compareRuns);
 const nodes=[],groups=new Map();
 for(const r of visible){const key=dayKey(r.created);if(!groups.has(key))groups.set(key,[]);groups.get(key).push(r);}
 const list=$('run-list'),position=list.scrollTop;
 for(const [key,items] of groups){
  const folded=collapsedDays.has(key),header=el('button','run-day'+(folded?' collapsed':''));header.type='button';header.ariaExpanded=String(!folded);header.dataset.day=key;header.title=folded?'展开此日期任务':'折叠此日期任务';
  header.append(el('span','day-label',dayLabel(items[0].created)),el('span','day-count',String(items.length)+(items.some(r=>r.id===current?.id)?' · 当前':'')));
  header.onclick=()=>{if(collapsedDays.has(key))collapsedDays.delete(key);else collapsedDays.add(key);renderRuns();};nodes.push(header);
  if(folded)continue;
 for(const r of items){
  const button=el('button','run-card'+(current?.id===r.id?' active':''));button.title=(r.task||r.run_id)+'\n'+r.run_id;button.dataset.runId=r.id;button.dataset.created=String(r.created||'');button.ariaCurrent=current?.id===r.id?'true':'false';
  button.append(el('div','run-name',r.task||r.run_id));
  const project=projects.find(p=>p.id===r.project_id);button.append(el('div','run-project',project?.name||'本地项目'));
  const meta=el('div','run-meta'),date=createdDate(r.created),stamp=el('time','run-time',date?date.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'时间未知');if(date){stamp.dateTime=date.toISOString();stamp.title=date.toLocaleString('zh-CN');}
  meta.append(el('span','run-status '+r.lifecycle,labels[r.lifecycle]||r.lifecycle),stamp);button.append(meta);button.onclick=()=>select(r.id);nodes.push(button);
 }
 }
 $('run-list').replaceChildren(...nodes);$('run-count').textContent=String(visible.length)+(hasOlderRuns?' +':'');$('run-count').title='当前已加载的匹配任务数';
 if(!visible.length)$('run-list').append(el('p','empty','暂无匹配任务'));
 list.scrollTop=position;
 const filters=[];if(listFilters.q)filters.push('关键词：'+listFilters.q);if(listFilters.project_id)filters.push(projects.find(p=>p.id===listFilters.project_id)?.name||'项目筛选');if(listFilters.status!=='all')filters.push(labels[listFilters.status]||listFilters.status);
 $('filter-summary').textContent=filters.join(' · ');show($('active-filters'),filters.length>0);$('clear-filters').disabled=filters.length===0;
 $('collapse-days').disabled=!visible.length||[...groups.keys()].every(key=>collapsedDays.has(key));$('expand-days').disabled=![...groups.keys()].some(key=>collapsedDays.has(key));
}

async function select(key){
 const generation=++selectGeneration;++feedRequestGeneration;
 historyBusy=false;latestBusy=false;updateFeedNavigation();
 let selected,history;
 // 两次读取都成功后再替换房间，失败时保留原动态与实时连接。
 try{selected=await api(`/runs/${encodeURIComponent(key)}/snapshot`);if(generation!==selectGeneration)return;
  history=await api(`/runs/${encodeURIComponent(key)}/events?before=${(selected.last_cursor||0)+1}&limit=100`);if(generation!==selectGeneration)return;
 }catch(error){if(generation===selectGeneration)toast('任务读取失败，可重试：'+error.message);return;}
 rememberFeedView();++feedRequestGeneration;if(reloadTimer){clearTimeout(reloadTimer);reloadTimer=null;}if(stream)stream.close();
 current=selected;$('app').classList.toggle('sidebar-open',false);$('sidebar-toggle').ariaExpanded='false';writeListURL(key);
 events.clear();pendingFeedEvents.clear();renderedFeed=[];unseenEvents=0;historyBusy=false;historyExhausted=false;historyViewing=false;latestBusy=false;latestFeedCursor=current.last_cursor||0;firstCursor=latestFeedCursor+1;
 show($('welcome'),false);show($('room'));renderSnapshot();renderRuns();
 history.events.forEach(e=>events.set(e.cursor,e));firstCursor=history.events[0]?.cursor||firstCursor;historyExhausted=history.events.length===0;renderFeed(true);openStream(key,current.last_cursor);
}
function openStream(key,cursor){
 const source=new EventSource(`/api/runs/${encodeURIComponent(key)}/stream?after=${cursor}`);stream=source;
 const owns=()=>current?.id===key&&stream===source;
 source.onopen=()=>{if(owns())setConnection(true,'实时动态已连接');};source.onerror=()=>{if(owns())setConnection(false,'重连实时动态…');};
 source.addEventListener('update',event=>{
  if(!owns())return;let value;try{value=JSON.parse(event.data);}catch{return;}
  if(!value||!Number.isInteger(value.cursor)||value.cursor<1||typeof value.kind!=='string'||!value.payload||typeof value.payload!=='object'||Array.isArray(value.payload))return;
  latestFeedCursor=Math.max(latestFeedCursor,value.cursor);
  if(feedNearEnd()&&!historyViewing&&!pendingFeedEvents.size){events.set(value.cursor,value);trimEvents(events);firstCursor=events.size?Math.min(...events.keys()):firstCursor;renderFeed(true);}
  else{if(!events.has(value.cursor)&&!pendingFeedEvents.has(value.cursor))unseenEvents++;pendingFeedEvents.set(value.cursor,value);trimEvents(pendingFeedEvents);updateFeedNavigation();}
  if(!reloadTimer)reloadTimer=setTimeout(async()=>{reloadTimer=null;try{const updated=await api(`/runs/${encodeURIComponent(key)}/snapshot`);if(!owns())return;current=updated;renderSnapshot();refresh();}catch{}},600);
 });
}
function renderCLIActivity(r){const call=r.cli_activity?.[0];const box=$('cli-activity');if(!call){show(box,false);return;}const state=call.status==='running'?'处理中':call.status==='stopped'?'调用已停止，等待归档':'进程已退出，等待阶段确认';const quiet=Math.max(0,Math.floor(Date.now()/1000-(call.last_output_at||Date.now()/1000)));box.textContent=`${call.channel} · ${state} · ${call.tool||'等待模型输出'}${call.file?' · '+call.file:''} · 本次已报告 ${num(call.known_tokens)} tokens（${call.usage_complete?'待结算':'仅完成响应下界'}） · 缓存读取 ${num(call.cache_read_tokens)} · ${quiet} 秒无新输出。活动记录不代表任务完成。`;show(box);}
function stagePlan(scope){return (scope.stages||[]).map(s=>(stages[s.name]||s.name)+'（'+({code:'开发',report:'报告',review:'审查',test:'固定测试'}[s.kind]||s.kind)+' / '+(adapterLabels[s.adapter_type]||s.adapter_type)+'）').join(' → ');}
function scopeText(scope){return scope.summary+' 实际工作目录：'+scope.workdir+'。执行阶段：'+stagePlan(scope)+'。任务描述中的路径不会自动切换工作目录或增加阶段。';}
function renderExecutionScope(r){const scope=r.execution_scope,box=$('execution-scope');show(box,!!scope);if(!scope)return;box.textContent=scope.matches_run_config===false?'无法核实当前配置与原运行一致，以下是当前登记范围，不能据此推断旧运行交付：'+scopeText(scope):scopeText(scope);}
function taskScopeText(project,scope){if(project?.unavailable)return '配置不可用：'+(project.unavailable||'配置文件无法读取')+'。请修复或重新登记配置后刷新工作台；不可用配置不能创建任务。';if(!scope)return $('project-select').value?'尚未取得此配置的执行范围，请刷新工作台后再创建。':'请先选择项目配置，核对实际目录和阶段后启动。';if(parent)return '派生任务将使用基于当前固定 commit 的独立工作区，不使用父配置的工作目录，也不会改写父任务产物。新任务使用新的预算。执行阶段：'+stagePlan(scope)+'。';return scopeText(scope);}
function renderTaskScope(){const project=projects.find(p=>p.id===$('project-select').value);const scope=project?.execution_scope;const box=$('task-scope');box.textContent=taskScopeText(project,scope);const usable=!!scope&&!project?.unavailable&&typeof project?.config_hash==='string';$('task-submit').disabled=!usable||!taskProjectsFresh;$('task-submit').textContent=scope?.mode==='fixed_tests'?'启动固定测试':scope?.mode==='simulation'?'启动演示流程':'创建并启动';}
function renderWaitContext(r){const box=$('wait-context'),items=Object.entries(r.pending_waits||{});box.replaceChildren(...items.map(([name,wait])=>{const section=el('section','notice');section.append(el('strong','',`${stages[name]||name} · ${labels[wait.outcome]||wait.outcome}`),el('p','',wait.reason||''));if(wait.condition)section.append(el('p','',`恢复条件：${wait.condition}`));if(wait.resume_action)section.append(el('p','',`恢复后：${wait.resume_action}`));if(wait.next_check_at)section.append(el('p','',`下次允许检查：${new Date(wait.next_check_at*1000).toLocaleString('zh-CN')}`));return section;}));show(box,items.length>0);}
async function loadTaskProjects(derive){const generation=++taskProjectsGeneration;try{const list=(await api('/projects')).projects;if(generation!==taskProjectsGeneration)return;projects=list;applyProjects();if(derive)$('project-select').value=current?.project_id||'';taskProjectsFresh=true;renderTaskScope();if(typeof renderModelSettings==='function')renderModelSettings();}catch(error){if(generation!==taskProjectsGeneration)return;taskProjectsFresh=false;toast('无法刷新项目配置：'+error.message);renderTaskScope();}}
function renderSnapshot(){const r=current;if(!r)return;if(typeof renderBudgetAlerts==='function')renderBudgetAlerts(r);renderCLIActivity(r);renderExecutionScope(r);$('breadcrumb').textContent=(projects.find(p=>p.id===r.project_id)?.name||'本地项目')+' / 任务房间';$('run-identifier').textContent=r.run_id.toUpperCase();$('task-title').textContent=r.task||r.run_id;$('task-title').title=r.task;$('task-description').textContent=r.task||r.run_id;$('lifecycle').className='pill '+r.lifecycle;$('lifecycle').textContent=r.lifecycle==='done'&&r.execution_scope?.mode==='fixed_tests'&&r.execution_scope.matches_run_config!==false?'固定测试已通过':labels[r.lifecycle]||r.lifecycle;const values=Object.values(r.stages);$('stage-summary').textContent=`${values.filter(s=>s.status==='done').length} / ${values.length} 个阶段完成`;$('updated').textContent='同步于 '+new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});for(const action of ['pause','resume','cancel'])show($(action),r.capabilities[action]);show($('derive'),!!r.capabilities.derive);show($('discuss'),!!r.capabilities.discussion);$('derive').disabled=!r.capabilities.derive||['running','pending','recovering'].includes(r.lifecycle);$('discuss').disabled=!r.capabilities.discussion||['running','pending','recovering'].includes(r.lifecycle);const active=r.threads?.some(t=>['queued','running'].includes(t.status));if(active){for(const a of ['pause','resume','cancel','derive'])$(a).disabled=true;}else for(const a of ['pause','resume','cancel'])$(a).disabled=false;
 const notice=r.config_integrity==='changed'?'运行绑定的配置已变化，不能用新配置批准或继续旧任务。':r.lifecycle==='paused'?'已在检查点暂停。原任务有效期和预算保持不变。':r.lifecycle==='waiting_approval'?'请核对具体产物后批准。聊天留言不会代替审批。':!r.managed?'历史运行只读导入；本页面不会接管原进程或改写历史结论。':r.lifecycle==='cancelled'?'执行已停止。已有提交和已发生用量保留，取消不会撤销修改。':'';$('notice').textContent=notice;show($('notice'),!!notice);const critical=r.config_integrity==='changed'||r.lifecycle==='waiting_approval';$('scope-summary').textContent=critical?'需关注 · '+(r.config_integrity==='changed'?'运行配置已变化':'有产物等待审批'):'流程说明 · '+(r.execution_scope?.matches_run_config===false?'历史配置绑定待核实':r.execution_scope?.label||'执行范围');const panelKey=r.id+':'+critical;if(scopePanelKey!==panelKey){$('scope-panel').open=critical;scopePanelKey=panelKey;}
 $('stage-cards').replaceChildren(...Object.entries(r.stages).map(([name,s],i)=>{const box=el('article','stage-card'),line=el('div','stage-line');line.append(el('span','stage-number',String(i+1).padStart(2,'0')),el('span','',stages[name]||name),el('span','pill '+s.status,stageLabels[s.status]||s.status));box.append(line,el('div','stage-sub',`Attempt ${s.current_attempt??'—'} · 反馈轮次 ${r.rounds[name]||0}`));if(s.error)box.append(el('div','stage-error',s.error));if(r.pending_approvals[name]&&r.capabilities.approve){const button=el('button','approve','核对并批准此产物');button.onclick=()=>confirmAction('批准当前产物',`批准 ${stages[name]||name} 的第 ${s.current_attempt} 次尝试。后端将再次校验输入和产物哈希。`,note=>sendCommand('approve',{stage:name,target:{attempt:s.current_attempt,inputs_hash:s.inputs_hash,outputs_hash:s.outputs_hash},...(note?{note}:{})}),{withNote:true});box.append(button);}if(r.code_stages?.includes(name)){const diff=el('button','evidence-ref','查看代码差异 ↗');diff.onclick=()=>openDiff(name);box.append(diff);}if(r.capabilities.rerun){const rerun=el('button','evidence-ref','从此阶段重跑');rerun.onclick=()=>confirmAction('从阶段重跑','将使该阶段及下游产物失效。费用、总轮次和有效期不会重置。',()=>sendCommand('rerun',{stage:name,revision:r.revision}));box.append(rerun);}return box;}));
 const u=r.usage||{};$('reported').textContent=num(u.reported_tokens??u.tokens);$('reserved').textContent=num(u.reserved_tokens);$('token-total').textContent=num(u.tokens)+' tokens';$('token-progress').value=Math.min(100,(u.tokens||0)/(r.budget?.max_tokens||12000)*100);if($('model-calls')){$('model-calls').textContent=u.model_calls!=null?(r.budget?.max_model_calls?`${num(u.model_calls)} / ${num(r.budget.max_model_calls)}`:num(u.model_calls)):'—';}if($('unknown-calls')){$('unknown-calls').textContent=r.budget?.max_unknown_calls?`${num(u.unknown_calls||0)} / ${num(r.budget.max_unknown_calls)}`:num(u.unknown_calls||0);}if($('duration-total')){const sec=Math.round(Number(u.duration_s||0));$('duration-total').textContent=sec>=60?`${Math.floor(sec/60)}m ${sec%60}s`:`${sec}s`;}$('deadline').textContent=r.deadline?'任务有效期至 '+new Date(r.deadline*1000).toLocaleString('zh-CN'):'未记录运行有效期';$('artifact-count').textContent=r.artifacts.length+' 项';$('artifact-list').replaceChildren(...r.artifacts.map(a=>{const b=el('button','artifact-button',`${a.stage} · #${a.attempt} · ${a.name}`);b.title=a.id;b.onclick=()=>openArtifact(a.id);return b;}));if(!r.artifacts.length)$('artifact-list').append(el('p','empty','尚无已归档产物'));
 $('thread-list').replaceChildren(...r.threads.map(t=>{const item=el('div','thread-item');item.append(el('strong','',t.question),el('p','muted',`${labels[t.status]||t.status} · 上限 ${num(t.budget)} tokens`));if(t.result?.usage)item.append(el('p','muted',`已计入分析 ${num(t.result.usage.tokens)} tokens`));if(t.result?.reason)item.append(el('p','muted',t.result.reason));return item;}));if(!r.threads.length)$('thread-list').append(el('p','empty','未发起角色会诊'));}
function copyText(text,label='已复制到剪贴板'){if(navigator.clipboard?.writeText){navigator.clipboard.writeText(text).then(()=>toast(label)).catch(()=>toast('复制失败，请手动选择复制'));}else toast('当前环境不支持自动复制');}
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
   const btn=el('button','md-code-copy','复制');btn.type='button';btn.onclick=()=>copyText(codeText,'代码已复制');
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
 if(source==='human')return {cls:'role-human',avatar:'你',tag:'人工留言'};
 if(source==='tool_result')return {cls:'role-tool',avatar:'测',tag:'执行记录'};
 if(role==='consensus')return {cls:'role-consensus',avatar:'共',tag:'共识结论'};
 if(role==='skeptic')return {cls:'role-skeptic',avatar:'质',tag:'红队质疑'};
 if(role==='architect')return {cls:'role-architect',avatar:'架',tag:'架构师'};
 if(['review','code_review','final_review','reviewer'].includes(role))return {cls:'role-review',avatar:'审',tag:'审查角色'};
 if(['design','repair_plan','requirements'].includes(role))return {cls:'role-plan',avatar:'案',tag:'方案角色'};
 if(['coding','developer'].includes(role))return {cls:'role-code',avatar:'码',tag:'实现角色'};
 return {cls:'role-agent',avatar:'AI',tag:({agent_response:'角色回复',system_summary:'系统摘要'})[source]||'角色回复'};
}
function renderStageStepper(r){
 const bar=$('stage-stepper');if(!bar)return;
 const entries=Object.entries(r.stages||{});
 if(!entries.length){show(bar,false);return;}
 bar.replaceChildren(...entries.map(([name,s],idx)=>{
  const step=el('div','stepper-item '+s.status);
  const dot=el('span','stepper-dot',s.status==='done'?'✓':s.status==='failed'?'!':s.status==='awaiting_approval'||s.status==='waiting_approval'?'审':String(idx+1));
  const info=el('div','stepper-info');
  info.append(el('span','stepper-name',stages[name]||name),el('span','stepper-state',stageLabels[s.status]||s.status));
  step.append(dot,info);
  if(r.pending_approvals?.[name]&&r.capabilities?.approve){
   const approveBtn=el('button','stepper-approve','批准进入下一步');approveBtn.type='button';
   approveBtn.onclick=()=>confirmAction('批准当前产物',`批准 ${stages[name]||name} 的第 ${s.current_attempt} 次尝试。后端将再次校验输入和产物哈希。`,note=>sendCommand('approve',{stage:name,target:{attempt:s.current_attempt,inputs_hash:s.inputs_hash,outputs_hash:s.outputs_hash},...(note?{note}:{})}),{withNote:true});
   step.append(approveBtn);
  }
  return step;
 }));
 show(bar,true);
}
const renderSnapshotBase=renderSnapshot;renderSnapshot=function(){renderSnapshotBase();if(current){renderWaitContext(current);renderStageStepper(current);const archBtn=$('archive-run');if(archBtn){archBtn.textContent=current.archived?'取消归档':'归档';archBtn.disabled=['pending','running','recovering'].includes(current.lifecycle);}}};
function feedNearEnd(){const feed=$('feed');return feed.scrollHeight-feed.scrollTop-feed.clientHeight<80;}
function trimEvents(map,keepOldest=false){while(map.size>500)map.delete(keepOldest?Math.max(...map.keys()):Math.min(...map.keys()));}
function rememberFeedView(){if(!current)return;rememberedFeedViews.set(current.id+':'+tab,{top:$('feed').scrollTop,follow:feedNearEnd()&&!historyViewing});while(rememberedFeedViews.size>32)rememberedFeedViews.delete(rememberedFeedViews.keys().next().value);}
function updateFeedNavigation(){
 const pending=unseenEvents>0;$('jump-latest').textContent=pending?`${unseenEvents} 条新动态 · 查看最新 ↓`:'回到最新动态 ↓';$('jump-latest').disabled=latestBusy;
 show($('new-events-bar'),!!current&&(pending||historyViewing||!feedNearEnd()));
 $('older-events').disabled=historyBusy||latestBusy||historyExhausted;$('older-events').textContent=historyBusy?'正在加载更早动态…':historyExhausted?'已到最早动态':'查看更早动态';
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
 $('events').replaceChildren(...renderedFeed.map(item=>item.node));if(!all.length)$('events').append(el('p','empty',tab==='chat'?'暂无消息与会诊':'等待第一条已持久化的动态…'));
 if(scroll)$('feed-end').scrollIntoView({block:'end'});
 else if(anchor&&renderedFeed.some(item=>item.node===anchor.node))feed.scrollTop=top+anchor.node.getBoundingClientRect().top-feedTop-offset;
 else feed.scrollTop=top;
 updateFeedNavigation();
}
async function loadEarlierEvents(){
 if(!current||historyBusy||latestBusy||historyExhausted)return;const key=current.id,generation=selectGeneration,request=++feedRequestGeneration;historyBusy=true;updateFeedNavigation();
 try{const value=await api(`/runs/${encodeURIComponent(key)}/events?before=${firstCursor}&limit=100`);if(generation!==selectGeneration||request!==feedRequestGeneration||current?.id!==key)return;
  if(!value.events.length){historyExhausted=true;return;}
  value.events.forEach(e=>events.set(e.cursor,e));trimEvents(events,true);firstCursor=Math.min(...events.keys());historyViewing=true;renderFeed(false);
 }catch(error){if(generation===selectGeneration&&request===feedRequestGeneration)toast(error.message);}finally{if(generation===selectGeneration&&request===feedRequestGeneration){historyBusy=false;updateFeedNavigation();}}
}
async function jumpLatest(){
 if(!current||latestBusy)return;const key=current.id,generation=selectGeneration,request=++feedRequestGeneration;latestBusy=true;historyBusy=false;updateFeedNavigation();
 try{const snapshot=await api(`/runs/${encodeURIComponent(key)}/snapshot`);if(generation!==selectGeneration||request!==feedRequestGeneration)return;
  const end=Math.max(latestFeedCursor,snapshot.last_cursor||0);const history=await api(`/runs/${encodeURIComponent(key)}/events?before=${end+1}&limit=100`);if(generation!==selectGeneration||request!==feedRequestGeneration)return;
  events.clear();history.events.forEach(e=>events.set(e.cursor,e));pendingFeedEvents.forEach(e=>events.set(e.cursor,e));pendingFeedEvents.clear();trimEvents(events);
  latestFeedCursor=Math.max(end,events.size?Math.max(...events.keys()):0);firstCursor=events.size?Math.min(...events.keys()):latestFeedCursor+1;unseenEvents=0;historyViewing=false;historyExhausted=!events.size;renderFeed(true);
 }catch(error){if(generation===selectGeneration&&request===feedRequestGeneration)toast('最新动态读取失败：'+error.message);}finally{if(generation===selectGeneration&&request===feedRequestGeneration){latestBusy=false;updateFeedNavigation();}}
}
function renderEvent(e){
 const data=e.payload;
 if(e.kind==='discussion_stream'){
  const theme=roleTheme(data.role,'agent_response');
  const m=el('article','message streaming '+theme.cls);
  m.append(el('div','avatar',theme.avatar));
  const content=el('div','message-content'),head=el('div','message-head');
  head.append(el('span','message-role',stages[data.role]||data.role),el('span','source-label live-badge',data.finished?'流式会诊已完成':'实时流式输出'),el('time','',time(e.created)));
  const body=el('div','message-body');
  if(data.text)body.append(renderMarkdown(data.text));else body.append(el('span','muted','正在思考并生成建议…'));
  if(!data.finished)body.append(el('span','stream-cursor'));
  content.append(head,body,el('div','message-meta-bar',null));
  content.lastChild.append(el('span','meta-chip','会诊实时预览 · 最终以落盘归档为准'));
  m.append(content);return m;
 }
 if(e.kind==='cli_progress'){
  const item=el('div','event event-cli');item.append(el('span','event-icon cli','⚡'));
  const body=el('div');const rep=data._repeat>1?` · 已刷新 ${data._repeat} 次`:'';
  body.append(el('div','event-title','CLI 活动 · '+(data.tool||'处理模型响应')+rep),el('div','event-detail',(data.file?data.file+' · ':'')+'已报告下界 '+num(data.known_tokens)+' tokens；非完成凭据'));
  item.append(body,el('time','',time(e.created)));return item;
 }
 if(e.kind==='message'){
  const theme=roleTheme(data.role,data.source);
  const m=el('article','message '+theme.cls+(data.source==='human'?' human':data.source==='tool_result'?' tool':''));
  m.append(el('div','avatar',theme.avatar));
  const content=el('div','message-content'),head=el('div','message-head');
  const sourceText=({human:'人工留言',agent_response:theme.tag,tool_result:'真实执行记录',system_summary:'离线示例'})[data.source]||theme.tag;
  head.append(el('span','message-role',stages[data.role]||data.role),el('span','source-label '+theme.cls,sourceText));
  const modelRef=(data.refs||[]).find(r=>r.returned_model);
  const cliRef=(data.refs||[]).find(r=>r.adapter_channel?.startsWith('cli_'));
  const roundRef=(data.refs||[]).find(r=>r.round);
  if(roundRef)head.append(el('span','meta-chip',`第 ${roundRef.round}/${roundRef.total_rounds} 轮`));
  if(cliRef){
   head.append(el('span','meta-chip',`${adapterLabels[cliRef.adapter_channel]||cliRef.adapter_channel}${cliRef.tool_version?' '+cliRef.tool_version:''}`));
   if(cliRef.cli_model||cliRef.requested_model)head.append(el('span','meta-chip model-chip-tag',cliRef.cli_model||cliRef.requested_model));
  }else if(modelRef){
   head.append(el('span','meta-chip model-chip-tag',modelRef.returned_model));
  }
  head.append(el('time','',time(e.created)));
  content.append(head);

  const rawBody=String(data.body||'');
  if(data.source==='tool_result'&&rawBody.length>260){
   const messageKey=(current?.id||'')+':'+e.cursor;
   const preview=el('div','message-preview',rawBody.split('\n').filter(Boolean).slice(0,3).join('\n').slice(0,420));
   const details=el('details','message-log'),summary=el('summary','','查看完整记录');
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
    const toggle=el('button','expand-toggle',expanded?'收起内容 ⌃':'展开完整报告 ⌄');toggle.type='button';
    toggle.onclick=()=>{
     expanded=!expanded;
     if(expanded)expandedMessages.add(messageKey);else expandedMessages.delete(messageKey);
     while(expandedMessages.size>500)expandedMessages.delete(expandedMessages.values().next().value);
     bodyWrap.classList.toggle('collapsible',!expanded);
     toggle.textContent=expanded?'收起内容 ⌃':'展开完整报告 ⌄';
    };
    bodyWrap.append(toggle);
   }
   content.append(bodyWrap);
  }

  const footer=el('div','message-footer');
  if(data.thread_id)footer.append(el('span','meta-chip',data.role==='consensus'?'圆桌共识 · 可一键采纳执行':'会诊建议 · 未改变任务结论'));
  if(data.thread_id&&rawBody.trim()){
   const pendingStage=Object.keys(current?.pending_approvals||{})[0];
   if(pendingStage&&current?.capabilities?.approve){
    const s=current.stages?.[pendingStage];
    const adoptAppr=el('button','evidence-ref adopt-btn','采纳为审批附注 ↗');adoptAppr.type='button';
    adoptAppr.onclick=()=>{
     confirmAction('批准当前产物（已带入讨论结论）',`批准 ${stages[pendingStage]||pendingStage} 的第 ${s?.current_attempt} 次尝试，并把下方修订指令注入下游阶段。`,note=>sendCommand('approve',{stage:pendingStage,target:{attempt:s.current_attempt,inputs_hash:s.inputs_hash,outputs_hash:s.outputs_hash},...(note?{note}:{})}),{withNote:true});
     if($('confirm-note'))$('confirm-note').value=rawBody.slice(0,3800);
    };
    footer.append(adoptAppr);
   }
   const adoptTask=el('button','evidence-ref','基于结论新建任务 ↗');adoptTask.type='button';
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
   if(ref.artifact_id){const b=el('button','evidence-ref','查看执行证据 ↗');b.type='button';b.onclick=()=>openArtifact(ref.artifact_id);footer.append(b);}
  }
  if(rawBody.trim()){
   const cp=el('button','text-button copy-msg-btn','复制');cp.type='button';cp.onclick=()=>copyText(rawBody,'消息内容已复制');footer.append(cp);
  }
  if(footer.children.length)content.append(footer);
  m.append(content);return m;
 }
 const isFail=e.kind==='engine_event'&&['attempt_fail','test_fail','review_exhausted','rejected','worktree_error','cli_retry_exhausted'].includes(data.kind);
 const isDone=e.kind==='engine_event'&&['attempt_done','test_pass','review_pass','approved','policy_approved'].includes(data.kind);
 const iconChar=e.kind==='command_receipt'||isDone?'✓':isFail?'!':'·';
 const iconCls=isFail?'event-icon fail':isDone||e.kind==='command_receipt'?'event-icon ok':'event-icon';
 const item=el('div','event'+(isFail?' event-fail':isDone?' event-ok':''));item.append(el('span',iconCls,iconChar));const body=el('div');let title='';let detail='';if(e.kind==='engine_event'){title=(data.stage?(stages[data.stage]||data.stage)+' · ':'')+(kinds[data.kind]||data.kind);detail=data.reason||data.error||'';if(data.n)title+=' #'+data.n;if(data.exit_code!==undefined)detail='退出码 '+data.exit_code+(detail?' · '+detail:'');}else if(e.kind==='command_queued'){title='操作已排队 · '+data.intent;detail='等待执行者确认，尚未生效。';}else if(e.kind==='command_receipt'){title='操作'+(labels[data.status]||data.status)+' · '+data.intent;detail=data.result?.reason||(data.result?.target?(data.result.stage+' · Attempt '+data.result.target.attempt+' · 输入与产物哈希已验证'):data.result?.lifecycle?(labels[data.result.lifecycle]||data.result.lifecycle):JSON.stringify(data.result||{}));}else if(e.kind==='snapshot_changed'){title='运行状态 · '+(labels[data.lifecycle]||data.lifecycle);}else if(e.kind==='discussion_queued'){title='角色会诊已排队';detail=data.question;}else if(e.kind==='discussion_status'){title='会诊 · '+(labels[data.status]||data.status);detail=data.result?.reason||data.result?.note||'';}else if(e.kind==='run_created'){title='任务已创建';detail=data.task;}else{title=e.kind;detail=data.reason||'';}body.append(el('div','event-title',title));if(detail)body.append(el('div','event-detail',String(detail).slice(0,1400)));item.append(body,el('time','',time(e.created)));return item;
}
async function sendCommand(intent,payload={}){if(!current)return;try{const receipt=await api(`/runs/${encodeURIComponent(current.id)}/commands`,{method:'POST',body:{intent,payload}});toast('指令已排队，收到收据后才算生效。');if(intent==='pause'){show($('notice'));$('notice').textContent='暂停请求已提交，等待当前调用或测试归档后到达检查点。';}return receipt;}catch(e){toast(e.message);}}
function confirmAction(title,body,fn,opts={}){$('confirm-title').textContent=title;$('confirm-body').textContent=body;const wrap=$('confirm-note-wrap'),input=$('confirm-note');if(wrap&&input){input.value='';show(wrap,!!opts.withNote);}confirmation=fn;$('confirm-dialog').showModal();}
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
async function openArtifact(identifier){try{const encoded=identifier.split('/').map(encodeURIComponent).join('/');const a=await api(`/runs/${encodeURIComponent(current.id)}/artifacts/${encoded}`);$('artifact-tag').textContent=`${a.stage} / ATTEMPT ${a.attempt}`;$('artifact-title').textContent=a.name;$('artifact-integrity').textContent=({valid:'内容哈希校验通过。',changed:'警告：文件内容已经变化，不是原审批证据。',missing:'原产物缺失。'})[a.integrity]+(a.truncated?' 显示已截断。':'');$('artifact-body').textContent=a.body;show($('artifact-body'),true);if($('artifact-diff-view'))show($('artifact-diff-view'),false);$('artifact-dialog').showModal();}catch(e){toast(e.message);}}
async function openDiff(stage){try{const d=await api(`/runs/${encodeURIComponent(current.id)}/diff/${encodeURIComponent(stage)}`);$('artifact-tag').textContent='代码产物 · SHA 校验';$('artifact-title').textContent='代码差异 · '+(stages[stage]||stage);$('artifact-integrity').textContent='比较 '+(d.base||'空树').slice(0,12)+' → '+d.head.slice(0,12)+(d.truncated?' · 显示已截断':'');const text=d.body||'相对所选基线没有代码差异';$('artifact-body').textContent=text;if(d.body&&$('artifact-diff-view')){renderUnifiedDiff(d.body);show($('artifact-body'),false);show($('artifact-diff-view'),true);}else{show($('artifact-body'),true);if($('artifact-diff-view'))show($('artifact-diff-view'),false);}$('artifact-dialog').showModal();}catch(e){toast(e.message);}}
function newTask(derive=false){parent=derive?current:null;if($('unlimited-tokens'))$('unlimited-tokens').checked=true;if($('model-settings'))$('model-settings').replaceChildren();$('task-dialog-title').textContent=derive?'创建关联的新任务':'新建任务';$('task-input').value='';$('project-select').disabled=derive;if(derive)$('project-select').value=current?.project_id||'';else $('project-select').value='';$('task-note').textContent=derive?'使用当前固定 commit 创建独立工作区；原任务、预算、审批和历史结论保留。新任务使用新的预算。':'执行内容由所选配置决定，请先核对工作目录与执行范围。';taskProjectsFresh=false;renderTaskScope();$('task-dialog').showModal();loadTaskProjects(derive);if(typeof loadTaskTemplates==='function')loadTaskTemplates(derive);}
$('sidebar-toggle').onclick=()=>{const open=$('app').classList.toggle('sidebar-open');$('sidebar-toggle').ariaExpanded=String(open);};$('evidence-toggle').onclick=()=>{const open=$('room').classList.toggle('evidence-visible');$('evidence-toggle').ariaExpanded=String(open);};
$('evidence-close').onclick=()=>{$('room').classList.toggle('evidence-visible',false);$('evidence-toggle').ariaExpanded='false';};
$('collapse-days').onclick=()=>{(previewRuns||runs).forEach(r=>collapsedDays.add(dayKey(r.created)));renderRuns();};
$('expand-days').onclick=()=>{collapsedDays.clear();renderRuns();};
$('clear-filters').onclick=()=>{$('search').value='';$('project-filter').value='';$('status-filter').value='all';return changeListFilters();};
$('feed').onscroll=updateFeedNavigation;$('jump-latest').onclick=jumpLatest;
$('connect').onclick=connect;$('refresh').onclick=refresh;$('search').oninput=changeListFilters;$('status-filter').onchange=changeListFilters;$('time-sort').onchange=changeListFilters;$('project-filter').onchange=changeListFilters;$('project-select').onchange=renderTaskScope;$('new-run').onclick=()=>newTask();$('welcome-new').onclick=()=>newTask();$('derive').onclick=()=>newTask(true);
$('pause').onclick=()=>sendCommand('pause');$('resume').onclick=()=>{const entry=Object.entries(current?.stages||{}).find(([,s])=>['waiting_external','waiting_owner'].includes(s.status));return sendCommand('resume',entry&&current.lifecycle!=='paused'?{wait_target:[entry[0],entry[1].current_attempt,entry[1].outputs_hash]}:{});};$('cancel').onclick=()=>confirmAction('停止当前执行？','将终止此任务所属的执行进程。已有提交、已发生用量和未知预留仍保留；这不会撤销已应用修改。',()=>sendCommand('cancel'));
if($('export-report'))$('export-report').onclick=async()=>{if(!current)return;const btn=$('export-report');btn.disabled=true;try{const data=await api(`/runs/${encodeURIComponent(current.id)}/export-report`);const blob=new Blob([data.markdown||''],{type:'text/markdown;charset=utf-8'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=data.filename||'report.md';document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);toast(`已导出交付报告：${data.filename}`);}catch(e){toast(e.message);}finally{btn.disabled=false;}};
if($('archive-run'))$('archive-run').onclick=async()=>{if(!current)return;const btn=$('archive-run');btn.disabled=true;try{const next=!current.archived;await api(`/runs/${encodeURIComponent(current.id)}/archive`,{method:'POST',body:{archived:next}});current.archived=next;renderSnapshot();await refresh();toast(next?'任务已归档隐藏（磁盘证据完好保留，可在状态筛选「已归档」中查看）':'任务已恢复至主列表');}catch(e){toast(e.message);}finally{btn.disabled=false;}};
$('confirm-form').onsubmit=event=>{event.preventDefault();const note=$('confirm-note')&&!$('confirm-note-wrap')?.classList.contains('hidden')?$('confirm-note').value.trim():'';$('confirm-dialog').close();if(confirmation)confirmation(note);};document.querySelectorAll('.close-dialog').forEach(button=>button.onclick=()=>button.closest('dialog').close());
$('task-form').onsubmit=async event=>{event.preventDefault();const button=event.submitter||$('task-submit');const derive=!!parent;const project=projects.find(p=>p.id===(derive?current?.project_id:$('project-select').value));if(!taskProjectsFresh){toast('项目配置尚未刷新完成，请稍后再试。');renderTaskScope();return;}if(!project||project.unavailable||!project.execution_scope||typeof project.config_hash!=='string'){toast(project?.unavailable?'配置不可用：'+project.unavailable+'。请修复或重新登记配置后刷新工作台。':'无法确认配置执行范围，请刷新后重试。');renderTaskScope();return;}button.disabled=true;try{const data=await api('/runs',{method:'POST',body:{project_id:project.id,task:$('task-input').value,parent_id:derive?parent.id:null,expected_config_hash:project.config_hash,...(typeof executionOptions==='function'?{execution_options:executionOptions()}:{} )}});$('task-dialog').close();await refresh();await select(data.id);}catch(e){toast(e.message);if(e.status===409)await loadTaskProjects(derive);else renderTaskScope();}};
$('message-form').onsubmit=async event=>{event.preventDefault();if(!current||!$('message').value.trim())return;const body=$('message').value;try{await api(`/runs/${encodeURIComponent(current.id)}/messages`,{method:'POST',body:{body}});$('message').value='';}catch(e){toast(e.message);}};
function syncDiscussionSeatOptions(){const defaultTool=current?.models?.some(m=>m.type==='openai_compat')?'openai_compat':(current?.models?.some(m=>m.type==='cli_agy')?'cli_agy':'openai_compat');for(const r of ['architect','developer','skeptic','reviewer']){const sel=$('adapter-'+r),dl=$('dl-models-'+r);if(!dl)continue;const tool=sel?.value||defaultTool;const models=typeof catalogModelsFor==='function'?catalogModelsFor(tool):[];dl.replaceChildren(...models.map(m=>{const o=document.createElement('option');const id=typeof m==='string'?m:(m?.id||'');o.value=id;if(m&&typeof m==='object'&&m.label&&m.label!==id)o.textContent=m.label;return o;}).filter(o=>o.value));}}
for(const r of ['architect','developer','skeptic','reviewer']){if($('adapter-'+r))$('adapter-'+r).onchange=syncDiscussionSeatOptions;}
$('discuss').onclick=async()=>{if(typeof loadModelCatalog==='function')await loadModelCatalog(false);syncDiscussionSeatOptions();$('discussion-dialog').showModal();};$('discussion-form').onsubmit=async event=>{event.preventDefault();const roles=[],role_models={},role_adapters={};for(const r of ['architect','developer','skeptic','reviewer']){if($('role-'+r)?.checked){roles.push(r);const mv=($('model-'+r)?.value||'').trim();if(mv)role_models[r]=mv;const av=($('adapter-'+r)?.value||'').trim();if(av)role_adapters[r]=av;}}if(!roles.length){toast('请至少勾选 1 个讨论角色');return;}const files=($('discussion-files')?.value||'').split(/[,，\n]/).map(s=>s.trim()).filter(Boolean).slice(0,4);const rounds=Number($('discussion-rounds')?.value||2);const consensus=!!$('discussion-consensus')?.checked;const button=event.submitter;button.disabled=true;try{await api(`/runs/${encodeURIComponent(current.id)}/discussions`,{method:'POST',body:{question:$('question').value,roles,budget:Number($('discussion-budget').value),rounds,role_models,role_adapters,context_files:files,consensus}});$('discussion-dialog').close();toast(`已发起 ${rounds} 轮多模型圆桌对话（${roles.length} 个席位）。`);}catch(e){toast(e.message);}finally{button.disabled=false;}};
document.querySelectorAll('[data-tab]').forEach(button=>button.onclick=()=>{rememberFeedView();tab=button.dataset.tab;document.querySelectorAll('[data-tab]').forEach(b=>{b.classList.toggle('active',b===button);b.ariaSelected=String(b===button);});const view=rememberedFeedViews.get(current?.id+':'+tab);renderFeed(!!view?.follow);if(!view?.follow)$('feed').scrollTop=view?.top||0;updateFeedNavigation();});
$('older-events').onclick=loadEarlierEvents;
if($('artifact-copy'))$('artifact-copy').onclick=()=>copyText($('artifact-body').textContent,'产物全文已复制');
$('help-button').onclick=()=>toast('聊天建议不会覆盖测试与审批结论。暂停在检查点生效，取消不撤销提交。仅本机单用户；可写范围和预算由登记配置决定。');
setInterval(()=>{if(csrf)refresh();},7000);connect();
