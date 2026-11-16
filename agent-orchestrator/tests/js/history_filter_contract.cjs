'use strict';
// 独立验收替身：模拟支持过滤绑定游标的真实服务端，不启动模型。
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),html=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/index.html'),'utf8'),source=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/app.js'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
class Element{
 constructor(tag='div'){this.tagName=tag;this.children=[];this._text='';this._classes=new Set();this.value='';this.disabled=false;this.dataset={};this.style={};this.scrollTop=0;this.scrollHeight=100;this.clientHeight=100;this.attributes={};this.hidden=false;this.classList={toggle:(n,v)=>{v=v===undefined?!this._classes.has(n):v;v?this._classes.add(n):this._classes.delete(n);return v;},add:(...ns)=>ns.forEach(n=>this._classes.add(n)),remove:(...ns)=>ns.forEach(n=>this._classes.delete(n)),contains:n=>this._classes.has(n)};}
 set className(v){this._classes=new Set(String(v).split(/\s+/).filter(Boolean));}get className(){return [...this._classes].join(' ');}
 set textContent(v){this._text=String(v??'');this.children=[];}get textContent(){return this._text+this.children.map(c=>typeof c==='string'?c:c.textContent).join('');}
 append(...ns){this.children.push(...ns);}appendChild(n){this.append(n);return n;}replaceChildren(...ns){this._text='';this.children=[...ns];}
 setAttribute(n,v){this.attributes[n]=String(v);}getAttribute(n){return this.attributes[n]??null;}removeAttribute(n){delete this.attributes[n];}
 addEventListener(n,f){this['on'+n]=f;}scrollIntoView(){}showModal(){this.open=true;}close(){this.open=false;}
 async click(){if(!this.disabled&&this.onclick)return await this.onclick({target:this,preventDefault(){}});}
}
const turns=async(n=50)=>{for(let i=0;i<n;i++)await new Promise(r=>setImmediate(r));};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const row=n=>({id:'run-'+String(n).padStart(5,'0'),run_id:'run-'+String(n).padStart(5,'0'),project_id:n%2?'project-b':'project-a',created:n,task:(n<=157?'needle 历史 ':'head 最新 ')+String(n).padStart(5,'0'),lifecycle:n%3?'done':'failed',managed:false});
const snap=r=>({...r,revision:1,stages:{verify:{status:'done',current_attempt:1}},pending_approvals:{},usage:{tokens:0},rounds:{},capabilities:{},threads:[],artifacts:[],code_stages:[],budget:{max_tokens:1000},last_cursor:0});
async function boot(search=''){
 assert.ok(ids.includes('project-filter'),'界面需要项目筛选 project-filter');
 const nodes=new Map(ids.map(id=>[id,new Element()]));nodes.get('status-filter').value='all';nodes.get('project-filter').value='';
 const location={href:'http://local/'+search,pathname:'/',search,hash:''};
 const state={rows:Array.from({length:407},(_,i)=>row(407-i)),requests:[],streams:[],intervals:[],tokens:new Map(),hold:null,failQ:null};
 const response=(v,status=200)=>({ok:status<400,status,json:async()=>v});
 function page(u){
  const q=(u.searchParams.get('q')||'').trim().toLowerCase(),p=u.searchParams.get('project_id')||'',s=u.searchParams.get('status')||'all';
  const signature=JSON.stringify([q,p,s]),cursor=u.searchParams.get('cursor');let after=null;
  if(cursor){const bound=state.tokens.get(cursor);assert.ok(bound,'前端不得制造游标');assert.equal(bound.signature,signature,'不得跨筛选复用游标');after=bound.after;}
  const filtered=state.rows.filter(r=>(!q||r.task.toLowerCase().includes(q)||r.run_id.toLowerCase().includes(q))&&(!p||r.project_id===p)&&(s==='all'||r.lifecycle===s));
  let start=after===null?0:filtered.findIndex(r=>r.id===after)+1;
  const limit=Math.min(100,Number(u.searchParams.get('limit')||50)),runs=filtered.slice(start,start+limit),more=start+runs.length<filtered.length;
  let token=null;if(more){token='opaque-'+signature+'-'+runs.at(-1).id;state.tokens.set(token,{signature,after:runs.at(-1).id});}
  return {runs,has_more:more,next_cursor:token};
 }
 const fetch=async address=>{
  const u=new URL(String(address),'http://local');state.requests.push(u.pathname+u.search);
  if(u.pathname==='/api/session')return response({csrf:'local-fixture'});
  if(u.pathname==='/api/projects')return response({projects:[{id:'project-a',name:'项目甲'},{id:'project-b',name:'项目乙'}]});
  if(u.pathname==='/api/runs'){
   const q=u.searchParams.get('q')||'';const value=page(u);
   if(state.failQ===q){state.failQ=null;return response({error:'模拟筛选失败'},503);}
   if(state.hold&&state.hold.q===q){const held=state.hold;state.hold=null;held.started.resolve();await held.release.promise;}
   return response(value);
  }
  const hit=u.pathname.match(/^\/api\/runs\/([^/]+)\/(snapshot|events)$/);
  if(hit){if(hit[2]==='events')return response({events:[],last_cursor:0});const r=state.rows.find(r=>r.id===decodeURIComponent(hit[1]));return r?response(snap(r)):response({error:'不存在'},404);}
  throw Error('未允许请求：'+u.pathname);
 };
 class EventSource{constructor(url){this.url=url;this.closed=false;state.streams.push(this);}close(){this.closed=true;}addEventListener(){}}
 const history={replaceState(a,b,target){const u=new URL(target,location.href);Object.assign(location,{href:u.href,pathname:u.pathname,search:u.search,hash:u.hash});}};
 const document={getElementById:id=>{assert.ok(nodes.has(id),'DOM 不存在 '+id);return nodes.get(id);},createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),querySelectorAll:()=>[]};
 const context=vm.createContext({document,location,history,window:{history},fetch,EventSource,URL,URLSearchParams,Map,Set,console,AbortController,AbortSignal,TextEncoder,TextDecoder,performance,crypto:{randomUUID:()=>String(state.requests.length).padStart(16,'0')},setInterval:f=>{state.intervals.push(f);return 1;},setTimeout:(f,delay=0)=>delay<=1000?setImmediate(f):null,clearTimeout:h=>{if(h)clearImmediate(h);},setImmediate});
 vm.runInContext(source,context,{filename:'app.js'});await turns();
 const cards=()=>nodes.get('run-list').children.filter(n=>n.className.split(/\s+/).includes('run-card'));
 const shown=()=>cards().map(n=>{const m=n.textContent.match(/(\d{5})/);assert.ok(m);return 'run-'+m[1];});
 const query=async(q,p='',s='all')=>{nodes.get('search').value=q;nodes.get('project-filter').value=p;nodes.get('status-filter').value=s;nodes.get('search').oninput();await turns();};
 return {state,nodes,location,cards,shown,query,more:async()=>{await nodes.get('more-runs').click();await turns();},refresh:async()=>{await nodes.get('refresh').click();await turns();}};
}
async function test(name,fn){let timer;try{await Promise.race([fn(),new Promise((_,reject)=>timer=setTimeout(()=>reject(Error('场景超过 6 秒')),6000))]);console.log('PASS '+name);}catch(e){console.error('FAIL '+name+'\n'+e.stack);process.exitCode=1;}finally{clearTimeout(timer);}}
(async()=>{
 await test('服务端搜索能找到首页以外记录并连续分页',async()=>{const b=await boot();assert.equal(b.shown().length,100);await b.query('needle');assert.equal(b.shown().length,100);assert.ok(b.shown().every(id=>Number(id.slice(4))<=157));await b.more();assert.equal(b.shown().length,157);assert.equal(new Set(b.shown()).size,157);assert.ok(b.state.requests.some(p=>p.includes('q=needle')));});
 await test('项目和状态都发送服务端并在切换时重置游标',async()=>{const b=await boot();await b.more();await b.query('needle','project-a','done');const expected=b.state.rows.filter(r=>r.task.includes('needle')&&r.project_id==='project-a'&&r.lifecycle==='done').map(r=>r.id);assert.deepEqual(b.shown(),expected);const u=new URL('http://local'+b.state.requests.filter(p=>p.startsWith('/api/runs?')).at(-1));assert.equal(u.searchParams.get('project_id'),'project-a');assert.equal(u.searchParams.get('status'),'done');assert.equal(u.searchParams.has('cursor'),false);});
 await test('旧查询晚到不能覆盖新查询且不阻塞新请求',async()=>{const b=await boot();const held={q:'needle',started:deferred(),release:deferred()};b.state.hold=held;b.nodes.get('search').value='needle';b.nodes.get('search').oninput();await held.started.promise;await b.query('head');assert.equal(b.shown().length,100);assert.ok(b.shown().every(id=>Number(id.slice(4))>157));held.release.resolve();await turns();assert.ok(b.shown().every(id=>Number(id.slice(4))>157));});
 await test('URL 恢复筛选和范围外房间，选任务继续保留筛选参数',async()=>{const b=await boot('?q=needle&project_id=project-a&status=done&run=run-00407');assert.equal(b.nodes.get('search').value,'needle');assert.equal(b.nodes.get('project-filter').value,'project-a');assert.equal(b.nodes.get('status-filter').value,'done');assert.ok(b.nodes.get('task-title').textContent.includes('00407'));assert.equal(b.state.streams.length,1);await b.cards()[0].click();await turns();const u=new URL(b.location.href);assert.equal(u.searchParams.get('q'),'needle');assert.equal(u.searchParams.get('project_id'),'project-a');assert.equal(u.searchParams.get('status'),'done');});
 await test('列表筛选与刷新不重建当前 SSE，状态变化移除不匹配记录',async()=>{const b=await boot();await b.cards()[0].click();await turns();const selected=b.nodes.get('task-title').textContent;await b.query('needle','','done');const id=b.shown()[0];b.state.rows.find(r=>r.id===id).lifecycle='failed';await b.refresh();assert.ok(!b.shown().includes(id));assert.equal(b.nodes.get('task-title').textContent,selected);assert.equal(b.state.streams.length,1);assert.equal(b.state.streams[0].closed,false);});
 await test('查询失败可刷新重试，清空条件不复用过滤游标',async()=>{const b=await boot();b.state.failQ='needle';await b.query('needle');await b.refresh();assert.ok(b.shown().length>0);await b.more();assert.equal(b.shown().length,157);await b.query('');assert.equal(b.shown().length,100);await b.more();assert.equal(b.shown().length,200);});
 if(!process.exitCode)console.log('HISTORY_FILTER_CONTRACT_OK');
})();
