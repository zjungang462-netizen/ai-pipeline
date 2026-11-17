'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
const html=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/index.html'),'utf8');
const source=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/app.js'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
const turns=async()=>{for(let i=0;i<25;i++)await new Promise(r=>setImmediate(r));};
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};}
function event(cursor,run='a'){return {cursor,kind:'message',created:1700000000+cursor,
 payload:{source:'tool_result',role:'testing',body:run+' 命令记录 '+cursor+'\n退出码：0\n'+('执行证据\n'.repeat(70))}};}
async function boot(){
 let nodes;
 class Element{
  constructor(){this.children=[];this.parent=null;this._text='';this._top=0;this.value='';this.disabled=false;this.open=false;this.dataset={};this.style={};this.className='';this.classes=new Set();this.endCalls=0;this.classList={toggle:(n,v)=>{v=v===undefined?!this.classes.has(n):v;v?this.classes.add(n):this.classes.delete(n);return v;},contains:n=>this.classes.has(n)};}
  set textContent(v){this._text=String(v??'');this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
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
 const rows=Array.from({length:8},(_,i)=>({id:'run-'+i,run_id:'run-'+i,created:1700000000+Math.floor(i/3)*86400,task:'任务 '+i,project_id:i%2?'b':'a',lifecycle:i%2?'done':'failed'}));
 const state={requests:[],streams:[],hold:null,failHistory:false,data:new Map(rows.map(r=>[r.id,Array.from({length:160},(_,i)=>event(i+1,r.id))])),timers:[]};
 const location={href:'http://local/',pathname:'/',search:'',hash:''},response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
 const fetch=async(address)=>{
  const u=new URL(address,'http://local');state.requests.push(u.pathname+u.search);
  if(u.pathname==='/api/session')return response({csrf:'fixture'});
  if(u.pathname==='/api/projects')return response({projects:[{id:'a',name:'项目甲'},{id:'b',name:'项目乙'}]});
  if(u.pathname==='/api/runs'){
   const q=u.searchParams.get('q')||'',project=u.searchParams.get('project_id')||'',status=u.searchParams.get('status')||'all',sort=u.searchParams.get('sort');
   const runs=rows.filter(r=>(!q||r.task.includes(q))&&(!project||r.project_id===project)&&(status==='all'||r.lifecycle===status)).sort((a,b)=>(a.created-b.created||a.id.localeCompare(b.id))*(sort==='oldest'?1:-1));
   return response({runs,has_more:false,next_cursor:null});
  }
  const match=u.pathname.match(/^\/api\/runs\/([^/]+)\/(snapshot|events)$/);assert.ok(match,'不允许请求 '+u.pathname);
  const key=decodeURIComponent(match[1]),row=rows.find(r=>r.id===key),data=state.data.get(key);
  if(match[2]==='snapshot')return response({...row,last_cursor:data.at(-1)?.cursor||0,stages:{testing:{status:'done'}},capabilities:{},pending_approvals:{},usage:{},rounds:{},threads:[],artifacts:[],cli_activity:[],config_integrity:'valid'});
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
 const context=vm.createContext({document:{getElementById:id=>{assert.ok(nodes.has(id),id);return nodes.get(id);},createElement:()=>new Element(),querySelectorAll:selector=>selector==='[data-tab]'?tabs:[]},location,window:{history},fetch,EventSource,URL,URLSearchParams,Map,Set,console,crypto:{randomUUID:()=> 'fixture'},setInterval(){},setTimeout(fn){state.timers.push(fn);return state.timers.length;},clearTimeout(){}});
 vm.runInContext(source,context);await turns();
 const cards=()=>nodes.get('run-list').children.filter(n=>n.dataset.runId),groups=()=>nodes.get('run-list').children.filter(n=>n.dataset.day);
 const open=async key=>{await vm.runInContext(`select(${JSON.stringify(key)})`,context);await turns();};
 return {nodes,tabs,state,context,cards,groups,open,source:()=>state.streams.at(-1),read:code=>vm.runInContext(code,context)};
}
async function test(name,fn){await fn();console.log('PASS '+name);}
(async()=>{
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
  const b=await boot();await b.open('run-7');const feed=b.nodes.get('feed'),body=b.nodes.get('events'),last=body.children.at(-1);feed.scrollTop=300;const log=last.children[1].children.find(n=>n.className==='message-log');log.open=true;log.ontoggle();const before=feed.scrollTop,calls=b.nodes.get('feed-end').endCalls;
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
  const b=await boot();await b.open('run-7');let entry=b.nodes.get('events').children.at(-1);let details=entry.children[1].children.find(n=>n.className==='message-log');details.open=true;details.ontoggle();b.nodes.get('feed').scrollTop=340;
  b.tabs[1].onclick();b.nodes.get('feed').scrollTop=620;b.tabs[0].onclick();assert.equal(b.nodes.get('feed').scrollTop,340);b.tabs[1].onclick();assert.equal(b.nodes.get('feed').scrollTop,620);
  await b.open('run-6');details=b.nodes.get('events').children.at(-1).children[1].children.find(n=>n.className==='message-log');assert.equal(details.open,false);
  await b.open('run-7');details=b.nodes.get('events').children.at(-1).children[1].children.find(n=>n.className==='message-log');assert.equal(details.open,true);
 });
 await test('大量后台动态有界缓存，返回最新重新取证且没有发写请求',async()=>{
  const b=await boot();await b.open('run-7');b.nodes.get('feed').scrollTop=340;
  for(let n=161;n<=760;n++){const next=event(n);b.state.data.get('run-7').push(next);b.source().emit(next);}
  assert.equal(b.read('events.size'),100);assert.equal(b.read('pendingFeedEvents.size'),500);assert.equal(b.read('unseenEvents'),600);
  await b.nodes.get('jump-latest').onclick();assert.ok(b.read('events.size')<=500);assert.equal(b.read('events.has(760)'),true);assert.equal(b.read('pendingFeedEvents.size'),0);assert.ok(b.state.requests.every(p=>p.startsWith('/api/')));
 });
 console.log('NAVIGATION_READING_OK: 8 scenarios');
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
