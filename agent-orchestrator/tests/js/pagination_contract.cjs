'use strict';
// 标准库 VM 中的纯逻辑 DOM 替身；真实浏览器验收由外部执行。
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..');
const html=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/index.html'),'utf8');
const source=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/app.js'),'utf8');
const domIds=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this._text='';this._classes=new Set();this.value='';this.disabled=false;this.dataset={};this.style={};this.scrollTop=0;this.scrollHeight=100;this.clientHeight=100;this.attributes={};this.hidden=false;
    this.classList={toggle:(n,v)=>{v=v===undefined?!this._classes.has(n):v;v?this._classes.add(n):this._classes.delete(n);return v;},add:(...a)=>a.forEach(v=>this._classes.add(v)),remove:(...a)=>a.forEach(v=>this._classes.delete(v)),contains:n=>this._classes.has(n)};}
  set className(value){this._classes=new Set(String(value).split(/\s+/).filter(Boolean));}
  get className(){return [...this._classes].join(' ');}
  set textContent(value){this._text=String(value??'');this.children=[];}
  get textContent(){return this._text+this.children.map(c=>typeof c==='string'?c:c.textContent).join('');}
  append(...nodes){this.children.push(...nodes);}
  appendChild(node){this.append(node);return node;}
  replaceChildren(...nodes){this._text='';this.children=[...nodes];}
  setAttribute(name,value){this.attributes[name]=String(value);}
  getAttribute(name){return this.attributes[name]??null;}
  removeAttribute(name){delete this.attributes[name];}
  addEventListener(name,handler){this['on'+name]=handler;}
  scrollIntoView(){}
  showModal(){this.open=true;}
  close(){this.open=false;}
  async click(){if(!this.disabled&&typeof this.onclick==='function')return await this.onclick({target:this,preventDefault(){}});}
}
const makeRow=n=>({id:'run-'+String(n).padStart(5,'0'),run_id:'run-'+String(n).padStart(5,'0'),project_id:'project',created:n,task:'历史任务 '+String(n).padStart(5,'0'),lifecycle:'done',managed:false});
const turns=async(n=12)=>{for(let i=0;i<n;i++)await new Promise(r=>setImmediate(r));};
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}
function snapshot(row){return {...row,revision:1,stages:{verify:{status:'done',current_attempt:1}},pending_approvals:{},usage:{tokens:0,reported_tokens:0,reserved_tokens:0},rounds:{},capabilities:{pause:false,resume:false,cancel:false,derive:false,discussion:false,rerun:false,approve:false},threads:[],artifacts:[],code_stages:[],budget:{max_tokens:1000},last_cursor:0,config_integrity:'not_recorded'};}
async function boot(saved=''){
  const nodes=new Map(domIds.map(id=>[id,new Element()]));nodes.get('status-filter').value='all';nodes.get('more-runs').className='secondary hidden';
  const location={href:'http://local/'+(saved?'?run='+encodeURIComponent(saved):''),pathname:'/',search:saved?'?run='+encodeURIComponent(saved):'',hash:''};
  const state={data:Array.from({length:257},(_,i)=>makeRow(257-i)),requests:[],intervals:[],streams:[],failOlder:false,holdOlder:null};
  const page=(url,data=state.data)=>{const cursor=url.searchParams.get('cursor');let start=0;if(cursor){assert.ok(cursor.startsWith('after:'),'前端应原样回传服务端 cursor');const index=data.findIndex(r=>r.id===cursor.slice(6));assert.notEqual(index,-1,'不应制造游标或使用 offset');start=index+1;}
    const limit=Math.min(100,Number(url.searchParams.get('limit')||50)),runs=data.slice(start,start+limit);const more=start+runs.length<data.length;
    return {runs,has_more:more,next_cursor:more?'after:'+runs.at(-1).id:null};};
  const response=(value,status=200)=>({ok:status>=200&&status<300,status,json:async()=>value});
  const fetch=async address=>{const url=new URL(String(address),'http://local');state.requests.push(url.pathname+url.search);
    if(url.pathname==='/api/session')return response({csrf:'test-csrf'});
    if(url.pathname==='/api/projects')return response({projects:[{id:'project',name:'验收项目'}]});
    if(url.pathname==='/api/runs'){
      if(url.searchParams.has('cursor')){
        if(state.failOlder){state.failOlder=false;return response({error:'模拟分页失败'},503);}
        if(state.holdOlder){const held=state.holdOlder;state.holdOlder=null;const value=page(url);held.requested.resolve();await held.release.promise;return response(value);}
      }
      return response(page(url));
    }
    const hit=url.pathname.match(/^\/api\/runs\/([^/]+)\/(snapshot|events)$/);
    if(hit){const key=decodeURIComponent(hit[1]);if(hit[2]==='events')return response({events:[],last_cursor:0});const row=state.data.find(r=>r.id===key);return row?response(snapshot(row)):response({error:'任务不存在'},404);}
    throw Error('验收未允许的请求：'+url.pathname);
  };
  class EventSource{constructor(url){this.url=url;this.closed=false;state.streams.push(this);}close(){this.closed=true;}addEventListener(){}}
  const history={replaceState(a,b,target){const url=new URL(target,location.href);Object.assign(location,{href:url.href,pathname:url.pathname,search:url.search,hash:url.hash});}};
  const document={getElementById:id=>{assert.ok(nodes.has(id),'不存在的 DOM id: '+id);return nodes.get(id);},createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),querySelectorAll:()=>[]};
  const context=vm.createContext({document,location,window:{history},history,fetch,EventSource,URL,URLSearchParams,Map,Set,console,AbortController,AbortSignal,TextEncoder,TextDecoder,performance,crypto:{randomUUID:()=>String(state.requests.length).padStart(16,'0')},setInterval:fn=>{state.intervals.push(fn);return 1;},setTimeout:(fn,delay=0)=>delay<=1000?setImmediate(fn):null,clearTimeout:handle=>{if(handle)clearImmediate(handle);},setImmediate});
  vm.runInContext(source,context,{filename:'app.js'});await turns();
  const visible=()=>nodes.get('run-list').children.filter(n=>n.className.split(/\s+/).includes('run-card'));
  const ids=()=>visible().map(n=>{const match=n.textContent.match(/历史任务 (\d{5})/);assert.ok(match,'任务卡片应保留原任务名称');return 'run-'+match[1];});
  const more=async()=>{await nodes.get('more-runs').click();await turns();};
  const refresh=async()=>{await nodes.get('refresh').click();await turns();};
  return {state,nodes,context,visible,ids,more,refresh,interval:async()=>{state.intervals.at(-1)();await turns();}};
}
async function test(name,fn){let timer;try{await Promise.race([fn(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(Error('异步分页状态未完成，超过 5 秒')),5000);})]);console.log('PASS '+name);}catch(error){console.error('FAIL '+name+'\n'+error.stack);process.exitCode=1;}finally{clearTimeout(timer);}}
(async()=>{
  await test('257 条任务连续加载无重漏且末页停止',async()=>{const b=await boot();assert.equal(b.ids().length,100);assert.equal(b.nodes.get('more-runs').classList.contains('hidden'),false);await b.more();assert.equal(b.ids().length,200);await b.more();assert.deepEqual(b.ids(),b.state.data.map(r=>r.id));const button=b.nodes.get('more-runs');assert.ok(button.hidden||button.disabled||button.classList.contains('hidden'));});
  await test('定时刷新保留旧页、选择和实时连接',async()=>{const b=await boot();await b.more();const card=b.visible()[150];await card.click();await turns();const title=b.nodes.get('task-title').textContent;const selectedId=b.ids()[150];b.state.data.unshift(makeRow(258));await b.interval();assert.equal(b.ids().length,201);assert.ok(b.ids().includes(selectedId));assert.equal(b.nodes.get('task-title').textContent,title);assert.equal(b.state.streams.length,1);assert.equal(b.state.streams[0].closed,false);});
  await test('加载失败可按原游标重试且不清空列表',async()=>{const b=await boot();b.state.failOlder=true;await b.more();assert.equal(b.ids().length,100);assert.equal(b.nodes.get('more-runs').disabled,false);await b.more();assert.equal(b.ids().length,200);});
  await test('重复点击及刷新并发不回退游标',async()=>{const b=await boot();const hold={requested:deferred(),release:deferred()};b.state.holdOlder=hold;const a=b.nodes.get('more-runs').click();await hold.requested.promise;const again=b.nodes.get('more-runs').click();b.state.data.unshift(makeRow(258));const refreshing=b.nodes.get('refresh').click();await turns();hold.release.resolve();await Promise.all([a,again,refreshing]);await turns();assert.equal(new Set(b.ids()).size,201);await b.more();assert.deepEqual(b.ids(),b.state.data.map(r=>r.id));});
  await test('旧任务 URL 刷新仍恢复房间',async()=>{const b=await boot('run-00001');assert.equal(b.nodes.get('task-title').textContent,'历史任务 00001');assert.equal(b.state.streams.length,1);assert.equal(b.nodes.get('login').classList.contains('hidden'),true);});
  await test('搜索和状态筛选覆盖已加载历史',async()=>{const b=await boot();await b.more();b.nodes.get('search').value='00075';b.nodes.get('search').oninput();assert.deepEqual(b.ids(),['run-00075']);b.nodes.get('search').value='';b.nodes.get('search').oninput();assert.equal(b.ids().length,200);b.nodes.get('status-filter').value='running';b.nodes.get('status-filter').onchange();assert.equal(b.ids().length,0);b.nodes.get('status-filter').value='all';b.nodes.get('status-filter').onchange();assert.equal(b.ids().length,200);});
  await test('连续刷新不重置历史推进',async()=>{const b=await boot();await b.more();await b.refresh();await b.refresh();await b.more();assert.deepEqual(b.ids(),b.state.data.map(r=>r.id));});
  await test('新增超过一页后仍能继续补齐新旧历史',async()=>{const b=await boot();await b.more();b.state.data.unshift(...Array.from({length:150},(_,i)=>makeRow(407-i)));await b.refresh();assert.ok(b.ids().length>=300,'刷新不得丢弃已有历史');for(let i=0;i<8&&b.ids().length<407;i++)await b.more();assert.deepEqual(b.ids(),b.state.data.map(r=>r.id));});
  if(!process.exitCode)console.log('PAGINATION_FRONTEND_CONTRACT_OK');
})();
