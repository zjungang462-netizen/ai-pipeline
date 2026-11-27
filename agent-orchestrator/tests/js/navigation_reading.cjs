'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
const html=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/index.html'),'utf8');
const source=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/app.js'),'utf8');
const i18nSource=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/i18n.js'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
const turns=async()=>{for(let i=0;i<25;i++)await new Promise(r=>setImmediate(r));};
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
function event(cursor,run='a'){return {cursor,kind:'message',created:1700000000+cursor,
 payload:{source:'tool_result',role:'testing',body:run+' 命令记录 '+cursor+'\n退出码：0\n'+('执行证据\n'.repeat(70))}};}
async function boot({language='zh-CN',cryptoApi}={}){
 let nodes;
 const descendants=node=>node.children.flatMap(child=>[child,...descendants(child)]);
 class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this.parent=null;this._text='';this._top=0;this.value='';this.disabled=false;this.open=false;this.dataset={};this.attributes={};this.style={};this.className='';this.classes=new Set();this.endCalls=0;this.classList={toggle:(n,v)=>{v=v===undefined?!this.classes.has(n):v;v?this.classes.add(n):this.classes.delete(n);return v;},contains:n=>this.classes.has(n)};}
  set textContent(v){this._text=String(v??'');this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
  getAttribute(name){return this.attributes[name]??this[name]??null;}setAttribute(name,value){this.attributes[name]=String(value);}removeAttribute(name){delete this.attributes[name];}
  querySelectorAll(selector){const attr=selector.match(/^\[([^\]]+)\]$/),key=attr?.[1].replace(/^data-/,'').replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());return descendants(this).filter(node=>attr?Object.prototype.hasOwnProperty.call(node.dataset,key):selector.startsWith('.')?node.classes.has(selector.slice(1)):node.tag===selector);}
  append(...items){for(const n of items){n.parent=this;this.children.push(n);}}
  replaceChildren(...items){this._text='';this.children=[];this.append(...items);}
  get scrollHeight(){return this===nodes?.get('feed')?nodes.get('events').children.length*100+40:1000;}
  get clientHeight(){return 200;}
  get scrollTop(){return this._top;}set scrollTop(v){this._top=Math.max(0,Math.min(Number(v)||0,this.scrollHeight-this.clientHeight));}
  scrollIntoView(){this.endCalls++;nodes.get('feed').scrollTop=nodes.get('feed').scrollHeight;}
  getBoundingClientRect(){if(this===nodes.get('feed'))return {top:0,bottom:200};if(this.parent===nodes.get('events')){const top=40+this.parent.children.indexOf(this)*100-nodes.get('feed').scrollTop;return {top,bottom:top+100};}return {top:0,bottom:0};}
  showModal(){this.open=true;}close(){this.open=false;}
 }
 nodes=new Map(ids.map(id=>[id,new Element()]));nodes.get('status-filter').value='all';nodes.get('time-sort').value='newest';
 const tabs=['all','chat'].map(tab=>{const n=new Element();n.dataset.tab=tab;return n;});
 const listeners={},title=new Element('title');title.textContent='流水线 · 协作工作台';
 const allNodes=()=>[...new Set([...nodes.values(),...tabs,...nodes.values().flatMap(descendants),...tabs.flatMap(descendants)])];
 const document={documentElement:new Element('html'),getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag),querySelector:selector=>selector==='title'?title:null,
  querySelectorAll:selector=>{const attr=selector.match(/^\[([^\]]+)\]$/),key=attr?.[1].replace(/^data-/,'').replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());return allNodes().filter(node=>attr?Object.prototype.hasOwnProperty.call(node.dataset,key):selector.startsWith('.')?node.classes.has(selector.slice(1)):node.tag===selector);},
  addEventListener(type,fn){(listeners[type]??=[]).push(fn);},dispatchEvent(event){for(const fn of listeners[event.type]||[])fn(event);return true;}};
 class CustomEvent{constructor(type,options={}){this.type=type;this.detail=options.detail;}}
 const stored=new Map([['wb_lang',language]]),localStorage={getItem:key=>stored.get(key)??null,setItem:(key,value)=>stored.set(key,String(value))};
 const rows=Array.from({length:8},(_,i)=>({id:'run-'+i,run_id:'run-'+i,created:1700000000+Math.floor(i/3)*86400,task:'任务 '+i,project_id:i%2?'b':'a',lifecycle:i%2?'done':'failed'}));
 const state={requests:[],streams:[],posts:[],writeHeaders:[],intervals:[],createRun:null,projects:[{id:'a',name:'项目甲'},{id:'b',name:'项目乙'}],hold:null,failHistory:false,failSnapshot:false,failNetwork:false,invalidResponse:false,data:new Map(rows.map(r=>[r.id,Array.from({length:160},(_,i)=>event(i+1,r.id))])),timers:[]};
 const location={href:'http://local/',pathname:'/',search:'',hash:''},response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
 const fetch=async(address,options={})=>{
  const u=new URL(address,'http://local');state.requests.push(u.pathname+u.search);
  if(state.failNetwork)throw new TypeError('Failed to fetch');
  if(state.invalidResponse)return {ok:true,status:200,json:async()=>{throw new SyntaxError('Unexpected token <');}};
  if(u.pathname==='/api/session')return response({csrf:'fixture'});
  if(u.pathname==='/api/projects')return response({projects:state.projects});
  if(u.pathname==='/api/runs'&&options.method==='POST'){const body=JSON.parse(options.body);state.posts.push(body);state.writeHeaders.push({...options.headers});return state.createRun?state.createRun(body):response({error:'fixture create unavailable'},500);}
  if(u.pathname==='/api/runs'){
   const q=u.searchParams.get('q')||'',project=u.searchParams.get('project_id')||'',status=u.searchParams.get('status')||'all',sort=u.searchParams.get('sort');
   const runs=rows.filter(r=>(!q||r.task.includes(q))&&(!project||r.project_id===project)&&(status==='all'||r.lifecycle===status)).sort((a,b)=>(a.created-b.created||a.id.localeCompare(b.id))*(sort==='oldest'?1:-1));
   return response({runs,has_more:false,next_cursor:null});
  }
  const match=u.pathname.match(/^\/api\/runs\/([^/]+)\/(snapshot|events)$/);assert.ok(match,'不允许请求 '+u.pathname);
  const key=decodeURIComponent(match[1]),row=rows.find(r=>r.id===key),data=state.data.get(key);
  if(match[2]==='snapshot'){if(state.failSnapshot){state.failSnapshot=false;return response({error:'fixture snapshot unavailable'},503);}return response({...row,last_cursor:data.at(-1)?.cursor||0,stages:{testing:{status:'done'}},capabilities:{},pending_approvals:{},usage:{},rounds:{},threads:[],artifacts:[],cli_activity:[],config_integrity:'valid'});}
  if(state.failHistory){state.failHistory=false;return response({error:'fixture history unavailable'},503);}
  const before=Number(u.searchParams.get('before')),events=data.filter(e=>e.cursor<before).slice(-100),held=state.hold;
  if(held&&held.before===before){state.hold=null;held.started.resolve();await held.release.promise;}
  return response({events,last_cursor:data.at(-1)?.cursor||0});
 };
 class EventSource{
  constructor(url){this.url=url;this.handlers={};state.streams.push(this);}close(){this.closed=true;}addEventListener(type,fn){this.handlers[type]=fn;}
  emit(value){this.handlers.update({data:typeof value==='string'?value:JSON.stringify(value)});}
 }
 const history={replaceState(a,b,target){const u=new URL(target,'http://local');Object.assign(location,{href:u.href,search:u.search});}};
 const context=vm.createContext({document,location,window:{history},fetch,EventSource,URL,URLSearchParams,Map,Set,console,CustomEvent,localStorage,crypto:cryptoApi||{randomUUID:()=> 'fixture'},setInterval(fn,delay){state.intervals.push({fn,delay});return state.intervals.length;},setTimeout(fn){state.timers.push(fn);return state.timers.length;},clearTimeout(){}});
 vm.runInContext(i18nSource,context);vm.runInContext(source,context);await turns();
 const cards=()=>nodes.get('run-list').children.filter(n=>n.dataset.runId),groups=()=>nodes.get('run-list').children.filter(n=>n.dataset.day);
 const open=async key=>{await vm.runInContext(`select(${JSON.stringify(key)})`,context);await turns();};
 return {nodes,tabs,state,context,document,cards,groups,open,source:()=>state.streams.at(-1),read:code=>vm.runInContext(code,context)};
}
async function test(name,fn){await fn();console.log('PASS '+name);}
(async()=>{
 await test('非安全 HTTP 上下文仍生成合法且重试一致的幂等键',async()=>{
  let counter=0;
  const variants=[{getRandomValues(bytes){for(let i=0;i<bytes.length;i++)bytes[i]=counter++&255;return bytes;}},{}];
  for(const cryptoApi of variants){
   const b=await boot({cryptoApi}),uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
   assert.match(b.read('id()'),uuid);assert.notEqual(b.read('id()'),b.read('id()'));
   b.state.createRun=()=>({ok:false,status:500,json:async()=>({error:'fixture unavailable'})});
   for(let i=0;i<2;i++)await b.read(`api('/runs',{method:'POST',body:{task:'retry'}})`).catch(()=>{});
   assert.equal(b.state.writeHeaders.length,2);
   const keys=b.state.writeHeaders.map(headers=>headers['Idempotency-Key']);
   assert.match(keys[0],uuid);assert.equal(keys[0],keys[1]);
  }
 });
 await test('日期分组可折叠，计数与当前任务标记保留，刷新不重建连接',async()=>{
  const b=await boot();await b.open('run-7');const stream=b.source();assert.equal(b.cards().length,8);
  const first=b.groups()[0];assert.ok(first.textContent.includes('当前'));first.onclick();assert.equal(b.cards().length,6);
  await b.nodes.get('refresh').onclick();await turns();assert.equal(b.cards().length,6);assert.equal(b.source(),stream);
  b.nodes.get('collapse-days').onclick();assert.equal(b.cards().length,0);assert.equal(b.nodes.get('run-count').textContent,'8');
  b.nodes.get('expand-days').onclick();assert.equal(b.cards().length,8);assert.equal(b.read('current.id'),'run-7');
 });
 await test('清空筛选保留时间方向、当前任务与 SSE，空结果可恢复',async()=>{
  const b=await boot();await b.open('run-7');const stream=b.source();b.nodes.get('time-sort').value='oldest';await b.nodes.get('time-sort').onchange();
  b.nodes.get('search').value='未匹配关键词';await b.nodes.get('search').oninput();assert.equal(b.cards().length,0);assert.equal(b.nodes.get('active-filters').classList.contains('hidden'),false);
  await b.nodes.get('clear-filters').onclick();await turns();assert.equal(b.cards().length,8);assert.equal(b.cards()[0].dataset.runId,'run-0');assert.equal(b.nodes.get('time-sort').value,'oldest');assert.equal(b.nodes.get('active-filters').classList.contains('hidden'),true);assert.equal(b.source(),stream);assert.equal(b.read('current.id'),'run-7');
 });
 await test('读旧动态时新消息不重绘正文、不跳走，重复推送仅计一次',async()=>{
  const b=await boot();await b.open('run-7');const feed=b.nodes.get('feed'),body=b.nodes.get('events'),last=body.children.at(-1);feed.scrollTop=300;feed.onscroll();const log=last.children[1].children.find(n=>n.className==='message-log');log.open=true;log.ontoggle();const before=feed.scrollTop,calls=b.nodes.get('feed-end').endCalls;
  const next=event(161,'live');b.state.data.get('run-7').push(next);b.source().emit(next);b.source().emit(next);
  assert.equal(feed.scrollTop,before);assert.equal(b.nodes.get('feed-end').endCalls,calls);assert.equal(body.children.at(-1),last);assert.equal(log.open,true);assert.equal(b.read('unseenEvents'),1);assert.equal(b.read('pendingFeedEvents.size'),1);
  await b.nodes.get('jump-latest').onclick();assert.equal(b.read('events.has(161)'),true);assert.equal(b.read('unseenEvents'),0);assert.equal(b.nodes.get('new-events-bar').classList.contains('hidden'),true);
 });
 await test('加载历史合并现有动态并保留可见锚点，重复点击只发一次',async()=>{
  const b=await boot();await b.open('run-7');const feed=b.nodes.get('feed');feed.scrollTop=330;const anchor=b.nodes.get('events').children[3],oldTop=anchor.getBoundingClientRect().top;
  const held={before:61,started:deferred(),release:deferred()};b.state.hold=held;const first=b.nodes.get('older-events').onclick();await held.started.promise;
  await b.nodes.get('older-events').onclick();held.release.resolve();await first;
  assert.equal(b.read('events.size'),160);assert.equal(b.read('events.has(160)'),true);assert.equal(anchor.getBoundingClientRect().top,oldTop);assert.equal(b.state.requests.filter(r=>r.includes('/events?before=61')).length,1);
  await b.nodes.get('older-events').onclick();assert.equal(b.nodes.get('older-events').disabled,true);assert.equal(b.nodes.get('older-events').textContent,'已到最早动态');
 });
 await test('历史读取失败可重试，晚到历史不能覆盖回到最新后的结果',async()=>{
  const b=await boot();await b.open('run-7');b.nodes.get('feed').scrollTop=300;b.state.failHistory=true;await b.nodes.get('older-events').onclick();assert.equal(b.read('events.size'),100);assert.equal(b.nodes.get('older-events').disabled,false);
  const held={before:61,started:deferred(),release:deferred()};b.state.hold=held;const earlier=b.nodes.get('older-events').onclick();await held.started.promise;await b.nodes.get('jump-latest').onclick();held.release.resolve();await earlier;
  assert.equal(b.read('events.size'),100);assert.equal(b.read('historyViewing'),false);assert.equal(b.read('firstCursor'),61);
 });
 await test('同一房间重选后旧 SSE 和坏 JSON 不再影响新房间',async()=>{
  const b=await boot();await b.open('run-7');const stale=b.source();await b.open('run-7');const count=b.read('events.size');stale.emit(event(900));stale.onerror();assert.equal(b.read('events.size'),count);assert.equal(b.read('latestFeedCursor'),160);
  b.source().emit('{bad');b.source().emit({cursor:'wrong',kind:'message',payload:{}});assert.equal(b.read('events.size'),count);
  const held={before:61,started:deferred(),release:deferred()};b.state.hold=held;const earlier=b.nodes.get('older-events').onclick();await held.started.promise;await b.open('run-6');held.release.resolve();await earlier;assert.equal(b.read('current.id'),'run-6');assert.equal(b.read('events.size'),100);assert.equal(b.read('historyBusy'),false);
 });
 await test('消息展开状态按任务隔离，切换页签保留各自阅读位置',async()=>{
  const b=await boot();await b.open('run-7');let entry=b.nodes.get('events').children.at(-1);let details=entry.children[1].children.find(n=>n.className==='message-log');details.open=true;details.ontoggle();b.nodes.get('feed').scrollTop=340;b.nodes.get('feed').onscroll();
  b.tabs[1].onclick();b.nodes.get('feed').scrollTop=620;b.nodes.get('feed').onscroll();b.tabs[0].onclick();assert.equal(b.nodes.get('feed').scrollTop,340);b.tabs[1].onclick();assert.equal(b.nodes.get('feed').scrollTop,620);
  await b.open('run-6');details=b.nodes.get('events').children.at(-1).children[1].children.find(n=>n.className==='message-log');assert.equal(details.open,false);
  await b.open('run-7');details=b.nodes.get('events').children.at(-1).children[1].children.find(n=>n.className==='message-log');assert.equal(details.open,true);
 });
 await test('大量后台动态有界缓存，返回最新重新取证且没有发写请求',async()=>{
  const b=await boot();await b.open('run-7');b.nodes.get('feed').scrollTop=340;b.nodes.get('feed').onscroll();
  for(let n=161;n<=760;n++){const next=event(n);b.state.data.get('run-7').push(next);b.source().emit(next);}
  assert.equal(b.read('events.size'),100);assert.equal(b.read('pendingFeedEvents.size'),500);assert.equal(b.read('unseenEvents'),600);
  await b.nodes.get('jump-latest').onclick();assert.ok(b.read('events.size')<=500);assert.equal(b.read('events.has(760)'),true);assert.equal(b.read('pendingFeedEvents.size'),0);assert.ok(b.state.requests.every(p=>p.startsWith('/api/')));
 });
 await test('任务读取失败保留原房间与实时连接，初次读取失败可以重试',async()=>{
  const b=await boot();b.state.failHistory=true;await b.open('run-6');assert.equal(b.read('current'),null);assert.equal(b.state.streams.length,0);assert.ok(b.nodes.get('toast').textContent.includes('任务读取失败'));
  await b.open('run-7');const stream=b.source(),node=b.nodes.get('events').children.at(-1);b.nodes.get('feed').scrollTop=340;b.nodes.get('feed').onscroll();
  for(const failure of ['failSnapshot','failHistory']){b.state[failure]=true;await b.open('run-6');assert.equal(b.read('current.id'),'run-7');assert.equal(b.source(),stream);assert.ok(!stream.closed);assert.equal(b.nodes.get('events').children.at(-1),node);assert.equal(b.nodes.get('feed').scrollTop,340);assert.ok(b.read('location.search').includes('run=run-7'));}
  stream.emit(event(161));assert.equal(b.read('pendingFeedEvents.has(161)'),true);
  await b.open('run-6');assert.equal(b.read('current.id'),'run-6');assert.equal(stream.closed,true);assert.notEqual(b.source(),stream);
 });
 await test('连续切换以最后选择为准，加载期间原房间继续接收动态',async()=>{
  const b=await boot();await b.open('run-7');const stream=b.source();b.nodes.get('feed').scrollTop=340;b.nodes.get('feed').onscroll();
  const held={before:161,started:deferred(),release:deferred()};b.state.hold=held;const first=b.open('run-6');await held.started.promise;
  stream.emit(event(161));assert.equal(b.read('pendingFeedEvents.has(161)'),true);assert.ok(!stream.closed);assert.equal(b.read('current.id'),'run-7');
  await b.open('run-5');held.release.resolve();await first;assert.equal(b.read('current.id'),'run-5');assert.equal(b.read('events.has(161)'),false);assert.equal(b.state.streams.length,2);assert.equal(stream.closed,true);
 });
 await test('布局变化后仍跟随最新动态，向上滚动后才停止',async()=>{
  const b=await boot();await b.open('run-7');const feed=b.nodes.get('feed'),before=b.read('events.size'),calls=b.nodes.get('feed-end').endCalls;
  // 阶段进度条等面板出现时没有滚动事件，但阅读位置离底部已超过 80px。
  feed.scrollTop=feed.scrollHeight-feed.clientHeight-140;
  const next=event(161);b.state.data.get('run-7').push(next);b.source().emit(next);await turns();
  assert.equal(b.read('events.size'),before+1);assert.equal(b.read('unseenEvents'),0);assert.ok(b.nodes.get('feed-end').endCalls>calls);
  feed.scrollTop=300;feed.onscroll();const more=event(162);b.state.data.get('run-7').push(more);b.source().emit(more);await turns();
  assert.equal(b.read('events.size'),before+1);assert.equal(b.read('pendingFeedEvents.size'),1);
 });
 await test('未结束任务冲突提示可直达该任务',async()=>{
  const b=await boot();b.state.projects=[{id:'a',name:'项目甲',execution_scope:{mode:'fixed_tests'},config_hash:'h1'}];
  b.state.createRun=()=>({ok:false,status:409,json:async()=>({error:'此项目已有未结束任务；请先处理或取消',run_id:'run-3'})});
  await vm.runInContext('loadTaskProjects(false)',b.context);await turns();
  b.nodes.get('project-select').value='a';b.nodes.get('task-input').value='重复演示';
  b.nodes.get('task-dialog').showModal();
  await b.nodes.get('task-form').onsubmit({preventDefault(){},submitter:null});await turns();
  assert.equal(b.nodes.get('task-dialog').open,false);
  const notice=b.nodes.get('toast');assert.ok(notice.textContent.includes('未结束任务'));
  const action=notice.children.find(n=>n.textContent==='查看该任务');assert.ok(action,'提示里应有“查看该任务”按钮');
  await action.onclick();await turns();
  assert.ok(b.state.requests.includes('/api/runs/run-3/snapshot'));assert.equal(b.read('current.id'),'run-3');
 });
 await test('演练按钮只在选中赛事模板时填入文案',async()=>{
  const b=await boot();
  b.state.projects=[{id:'a',name:'workbench.example',execution_scope:{mode:'simulation'},config_hash:'h1'}];
  await vm.runInContext('launchQuickDemo()',b.context);await turns();
  assert.equal(b.nodes.get('task-input').value,'');assert.ok(b.nodes.get('toast').textContent.includes('未登记演练模板'));
  b.state.projects=[{id:'unsafe',name:'kiroween-custom',config:'/real/kiroween-demo.example.toml',execution_scope:{mode:'development'},config_hash:'real'}];
  await vm.runInContext('launchQuickDemo()',b.context);await turns();assert.equal(b.nodes.get('project-select').value,'');
  const copy={id:'copy',config:'/custom/kiroween-demo.example.toml',config_hash:'edited',execution_scope:{mode:'simulation',stages:Array.from({length:5},()=>({adapter_type:'fake'}))},verified_showcase:false};
  b.state.projects=[copy];
  await vm.runInContext('launchQuickDemo()',b.context);await turns();assert.equal(b.nodes.get('project-select').value,'');
  b.state.projects=[copy,{id:'k',name:'kiroween-demo.example',config:'/demo/kiroween-demo.example.toml',unlimited_tokens:false,execution_scope:{mode:'simulation'},config_hash:'h2',verified_showcase:true}];
  await vm.runInContext('launchQuickDemo()',b.context);await turns();
  assert.equal(b.nodes.get('project-select').value,'k');assert.ok(b.nodes.get('task-input').value.includes('Kiroween 五阶段闭环演练'));
 });
 await test('命令面板只对支持会诊的任务列出圆桌入口',async()=>{
  const b=await boot();
  assert.equal(vm.runInContext("current={id:'run-7',capabilities:{discussion:false}};buildCmdItems('会诊').filter(i=>i.tag==='ROOM').length",b.context),0);
  assert.equal(vm.runInContext("current={id:'run-7',capabilities:{discussion:true}};buildCmdItems('会诊').filter(i=>i.tag==='ROOM').length",b.context),1);
 });
 await test('英文默认与可见语言开关保留任务原文和连接状态',async()=>{
  const b=await boot({language:'en'});assert.equal(b.read('wbI18n.language'),'en');assert.equal(b.document.documentElement.getAttribute('lang'),'en');
  assert.equal(b.nodes.get('theme-toggle').textContent,'◐ Light');
  b.nodes.get('theme-toggle').onclick();
  assert.equal(b.document.documentElement.getAttribute('data-theme'),'dark');
  assert.equal(b.read("localStorage.getItem('wb_theme')"),'dark');
  await b.open('run-7');const stream=b.source();stream.onopen();
  assert.equal(b.nodes.get('connection').textContent,'Live activity connected');
  assert.equal(b.nodes.get('task-title').textContent,'任务 7');assert.equal(b.nodes.get('task-description').textContent,'任务 7');
  assert.ok(b.cards().find(n=>n.dataset.runId==='run-7').textContent.includes('Completed'));
  b.nodes.get('language-toggle').onclick();assert.equal(b.read('wbI18n.language'),'zh-CN');
  assert.equal(b.nodes.get('connection').textContent,'实时动态已连接');assert.equal(b.nodes.get('task-title').textContent,'任务 7');assert.equal(b.source(),stream);
  b.nodes.get('language-toggle').onclick();assert.equal(b.nodes.get('connection').textContent,'Live activity connected');
 });
 await test('切换语言保持筛选、折叠、滚动、展开和所有对话框输入',async()=>{
  const b=await boot();await b.open('run-7');const stream=b.source();
  b.nodes.get('search').value='任务 7';b.nodes.get('project-filter').value='b';b.nodes.get('status-filter').value='done';b.nodes.get('time-sort').value='oldest';
  await b.nodes.get('project-filter').onchange();assert.equal(b.cards().length,1);b.groups()[0].onclick();assert.equal(b.cards().length,0);
  b.nodes.get('run-list').scrollTop=75;
  const feed=b.nodes.get('feed');feed.scrollTop=340;feed.onscroll();const entry=b.nodes.get('events').children.at(-1),details=entry.children[1].children.find(n=>n.className==='message-log');details.open=true;details.ontoggle();
  b.nodes.get('message').value='用户留言：不要翻译 src/retry.py';
  b.nodes.get('task-dialog').showModal();b.nodes.get('project-select').value='a';b.nodes.get('task-input').value='用户任务：修复 src/retry.py';b.nodes.get('template-name').value='用户模板名';
  b.nodes.get('discussion-dialog').showModal();b.nodes.get('question').value='讨论 @src/retry.py 的边界';b.nodes.get('discussion-files').value='src/retry.py, tests/test_retry.py';b.nodes.get('model-architect').value='gemini-user-model';
  vm.runInContext("confirmAction('批准当前产物','批准 testing 的第 1 次尝试。',()=>{},{withNote:true,titleKey:'stage.approveTitle',bodyKey:'stage.approveBody',bodyVars:{stage:'testing',attempt:1}})",b.context);
  b.nodes.get('confirm-note').value='审批备注：只修改 retry.py';
  b.nodes.get('cmd-dialog').showModal();b.nodes.get('cmd-input').value='run-7';
  vm.runInContext("artifactView={type:'artifact',data:{stage:'testing',attempt:2,name:'retry.py',integrity:'valid',body:'模型输出：@@ -1 +1 @@\\n-return null;\\n+return value;'}};renderArtifactChrome()",b.context);
  b.nodes.get('artifact-body').textContent='模型输出：@@ -1 +1 @@\n-return null;\n+return value;';b.nodes.get('artifact-dialog').showModal();
  vm.runInContext("toastT('task.archived','任务已归档并隐藏（磁盘证据保留；可在“已归档”筛选中找到）')",b.context);
  const inputs=['message','task-input','template-name','question','discussion-files','model-architect','confirm-note','cmd-input'].map(id=>[id,b.nodes.get(id).value]);
  const requestCount=b.state.requests.length;
  b.nodes.get('language-toggle').onclick();
  assert.equal(b.read('wbI18n.language'),'en');assert.equal(b.read('current.id'),'run-7');assert.equal(b.source(),stream);assert.equal(b.state.requests.length,requestCount,'语言切换不得请求后端');
  assert.equal(b.nodes.get('project-filter').value,'b');assert.equal(b.nodes.get('status-filter').value,'done');assert.equal(b.nodes.get('time-sort').value,'oldest');assert.equal(b.read('collapsedDays.size'),1);
  assert.equal(b.nodes.get('run-list').scrollTop,75);assert.equal(feed.scrollTop,340);
  const switchedDetails=b.nodes.get('events').children.at(-1).children[1].children.find(n=>n.className==='message-log');assert.equal(switchedDetails.open,true);
  for(const [id,value] of inputs)assert.equal(b.nodes.get(id).value,value,id+' 的用户输入不得改变');
  for(const id of ['task-dialog','discussion-dialog','confirm-dialog','cmd-dialog','artifact-dialog'])assert.equal(b.nodes.get(id).open,true,id+' 应保持打开');
  assert.equal(b.nodes.get('task-dialog-title').textContent,'New task');assert.equal(b.nodes.get('confirm-title').textContent,'Approve current artifact');
  assert.equal(b.nodes.get('artifact-tag').textContent,'Test verification / ATTEMPT 2');
  assert.equal(b.nodes.get('task-title').textContent,'任务 7');assert.equal(b.nodes.get('task-description').textContent,'任务 7');
  assert.equal(b.nodes.get('artifact-body').textContent,'模型输出：@@ -1 +1 @@\n-return null;\n+return value;');
  assert.ok(b.nodes.get('events').textContent.includes('命令记录'));assert.ok(b.nodes.get('toast').textContent.includes('Task archived and hidden'));
  b.nodes.get('language-toggle').onclick();assert.equal(b.read('wbI18n.language'),'zh-CN');
  assert.equal(b.nodes.get('task-dialog-title').textContent,'新建任务');assert.equal(b.nodes.get('confirm-title').textContent,'批准当前产物');
  for(const [id,value] of inputs)assert.equal(b.nodes.get(id).value,value,id+' 切回中文后不得改变');
  assert.ok(b.nodes.get('toast').textContent.includes('任务已归档并隐藏'));
 });
 await test('后台断网只提示一次，恢复后重新检测，手动刷新和重试可用',async()=>{
  const b=await boot(),poll=b.state.intervals.find(item=>item.delay===7000);assert.ok(poll,'应保留 7 秒后台轮询');
  b.state.failNetwork=true;poll.fn();await turns();
  const notice=b.read('toastState');assert.ok(notice);assert.ok(b.nodes.get('toast').textContent.includes('无法连接本地工作台'));assert.ok(!b.nodes.get('toast').textContent.includes('Failed to fetch'));
  assert.equal(b.nodes.get('connection').textContent,'无法连接本地服务');assert.equal(b.read('listRefreshFailed'),true);assert.ok(b.nodes.get('toast').children.some(node=>node.textContent==='重试'));
  poll.fn();await turns();assert.equal(b.read('toastState'),notice,'持续断网不得重复替换提示');
  b.state.failNetwork=false;poll.fn();await turns();assert.equal(b.nodes.get('connection').textContent,'本地服务已连接');assert.equal(b.read('listRefreshFailed'),false);
  b.state.failNetwork=true;poll.fn();await turns();assert.notEqual(b.read('toastState'),notice,'恢复后的新断网应再次提醒');
  b.state.failNetwork=false;await b.nodes.get('toast').children.find(node=>node.textContent==='重试').onclick();await turns();assert.equal(b.nodes.get('connection').textContent,'本地服务已连接');assert.equal(b.nodes.get('toast').classList.contains('hidden'),true);
  b.state.failNetwork=true;await b.nodes.get('refresh').onclick();await turns();assert.ok(b.nodes.get('toast').children.some(node=>node.textContent==='重试'),'手动刷新失败仍应提示并可重试');
 });
 await test('英文网络错误可切换语言，非法响应不泄漏解析异常',async()=>{
  const b=await boot({language:'en'}),poll=b.state.intervals.find(item=>item.delay===7000);
  b.state.failNetwork=true;poll.fn();await turns();assert.ok(b.nodes.get('toast').textContent.includes('Unable to reach the local workbench'));assert.ok(!b.nodes.get('toast').textContent.includes('Failed to fetch'));assert.equal(b.nodes.get('connection').textContent,'Local workbench unreachable');
  b.nodes.get('language-toggle').onclick();assert.ok(b.nodes.get('toast').textContent.includes('无法连接本地工作台'));assert.ok(b.nodes.get('toast').children.some(node=>node.textContent==='重试'));
  b.nodes.get('language-toggle').onclick();b.state.failNetwork=false;b.state.invalidResponse=true;await b.nodes.get('refresh').onclick();await turns();
  assert.ok(b.nodes.get('toast').textContent.includes('returned an unreadable response'));assert.ok(!b.nodes.get('toast').textContent.includes('Unexpected token'));assert.equal(b.nodes.get('connection').textContent,'Task list refresh failed');
 });
 await test('登录会话网络失败显示本地化错误而非浏览器原文',async()=>{
  const b=await boot({language:'en'});b.nodes.get('token').value='fixture-token';b.state.failNetwork=true;
  await vm.runInContext('connect()',b.context);await turns();
  assert.ok(b.nodes.get('login-error').textContent.includes('Unable to reach the local workbench'));assert.ok(!b.nodes.get('login-error').textContent.includes('Failed to fetch'));
  assert.equal(b.nodes.get('login').classList.contains('hidden'),false);assert.equal(b.nodes.get('app').classList.contains('hidden'),true);
 });
 console.log('NAVIGATION_READING_OK: 19 scenarios');
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
