'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),html=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/index.html'),'utf8'),source=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/app.js'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
class Element{
 constructor(){this.children=[];this._text='';this.className='';this.value='';this.disabled=false;this.dataset={};this.style={};this.open=false;this.classes=new Set();this.classList={toggle:(n,v)=>{v=v===undefined?!this.classes.has(n):v;v?this.classes.add(n):this.classes.delete(n);return v;},contains:n=>this.classes.has(n)};}
 set textContent(v){this._text=String(v??'');this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
 append(...items){this.children.push(...items);}replaceChildren(...items){this._text='';this.children=[...items];}scrollIntoView(){}showModal(){this.open=true;}close(){this.open=false;}
}
const turns=async()=>{for(let i=0;i<50;i++)await new Promise(r=>setImmediate(r));};
async function boot(query=''){
 const nodes=new Map(ids.map(id=>[id,new Element()]));nodes.get('status-filter').value='all';nodes.get('time-sort').value='newest';
 const rows=Array.from({length:235},(_,i)=>({id:'run-'+String(i).padStart(4,'0'),run_id:'task-'+i,project_id:'project',created:1700000000+Math.floor(i/3)*3600,task:'任务 '+i,lifecycle:'done'}));
 const state={requests:[],streams:[],hold:null},location={href:'http://local/'+query,pathname:'/',search:query,hash:''};
 const response=v=>({ok:true,status:200,json:async()=>v});
 const fetch=async address=>{
  const u=new URL(address,'http://local');state.requests.push(u);
  if(u.pathname==='/api/session')return response({csrf:'fixture'});
  if(u.pathname==='/api/projects')return response({projects:[{id:'project',name:'实际项目'}]});
  if(u.pathname==='/api/runs'){
   const direction=u.searchParams.get('sort')||'newest';
   const ordered=[...rows].sort((a,b)=>(a.created-b.created||a.id.localeCompare(b.id))*(direction==='oldest'?1:-1));
   const cursor=u.searchParams.get('cursor');let start=0;
   if(cursor){const [bound,id]=cursor.split(':');assert.equal(bound,direction,'游标必须与时间方向一致');start=ordered.findIndex(r=>r.id===id)+1;}
   const data=ordered.slice(start,start+100),has_more=start+data.length<ordered.length;
   if(state.hold&&state.hold.sort===direction){const hold=state.hold;state.hold=null;hold.started();await hold.promise;}
   return response({runs:data,has_more,next_cursor:has_more?direction+':'+data.at(-1).id:null});
  }
  if(u.pathname.endsWith('/snapshot')){const row=rows.find(r=>r.id===decodeURIComponent(u.pathname.split('/')[3]));return response({...row,stages:{verify:{status:'done'}},capabilities:{},pending_approvals:{},usage:{},rounds:{},artifacts:[],threads:[],last_cursor:0,config_integrity:'not_recorded',execution_scope:{label:'固定测试流程',mode:'fixed_tests',matches_run_config:false,summary:'仅证明测试通过',workdir:'D:/real',stages:[]}});}
  if(u.pathname.endsWith('/events'))return response({events:[{cursor:1,kind:'message',created:1700000000,payload:{source:'tool_result',role:'testing',body:'命令：真实测试\n退出码：0\n'+('详细记录\n'.repeat(100))}}]});
  throw Error('Unexpected '+u.pathname);
 };
 class EventSource{constructor(url){this.url=url;state.streams.push(this);}close(){this.closed=true;}addEventListener(){}}
 const history={replaceState(a,b,target){const u=new URL(target,'http://local');Object.assign(location,{href:u.href,search:u.search});}};
 const context=vm.createContext({document:{getElementById:id=>{assert.ok(nodes.has(id),id);return nodes.get(id);},createElement:()=>new Element(),querySelectorAll:()=>[]},location,window:{history},fetch,EventSource,URL,URLSearchParams,Map,Set,console,crypto:{randomUUID:()=> 'fixture'},setInterval(){},setTimeout(){},clearTimeout(){}});
 vm.runInContext(source,context);await turns();
 const cards=()=>nodes.get('run-list').children.filter(n=>n.className.split(' ').includes('run-card'));
 const sort=async value=>{nodes.get('time-sort').value=value;await nodes.get('time-sort').onchange();await turns();};
 return {nodes,state,location,rows,cards,sort,context};
}
(async()=>{
 let b=await boot();assert.equal(b.cards()[0].dataset.runId,'run-0234');
 assert.ok(b.nodes.get('run-list').children.some(n=>n.className==='run-day'));
 assert.ok(b.cards()[0].children.some(n=>n.className==='run-project'&&n.textContent==='实际项目'));
 assert.ok(b.cards()[0].children.at(-1).children.some(n=>n.className==='run-time'&&n.dateTime));
 await b.cards()[0].onclick();await turns();const title=b.nodes.get('task-title').textContent;
 assert.equal(b.nodes.get('scope-panel').open,false);assert.equal(b.nodes.get('task-description').textContent,title);
 assert.equal(b.nodes.get('events').children[0].children[1].children.find(n=>n.className==='message-log').open,false);
 await b.sort('oldest');assert.equal(b.cards()[0].dataset.runId,'run-0000');assert.equal(b.cards().length,100);
 assert.equal(new URL(b.location.href).searchParams.get('sort'),'oldest');
 assert.equal(b.nodes.get('task-title').textContent,title);assert.equal(b.state.streams.length,1);assert.ok(!b.state.streams[0].closed);
 await b.nodes.get('more-runs').onclick();await turns();assert.equal(b.cards().length,200);assert.equal(b.cards().at(-1).dataset.runId,'run-0199');
 assert.equal(b.nodes.get('more-runs').textContent,'加载后续任务');
 await b.nodes.get('refresh').onclick();await turns();assert.equal(b.cards().length,200);
 b.nodes.get('sidebar-toggle').onclick();assert.ok(b.nodes.get('app').classList.contains('sidebar-open'));
 b.nodes.get('evidence-toggle').onclick();assert.ok(b.nodes.get('room').classList.contains('evidence-visible'));
 b.nodes.get('evidence-close').onclick();assert.ok(!b.nodes.get('room').classList.contains('evidence-visible'));
 // 全部历史加载完后，升序刷新也须显示新建于末尾的任务。
 await b.nodes.get('more-runs').onclick();await turns();assert.equal(b.cards().length,235);
 b.rows.push({...b.rows.at(-1),id:'run-0235',run_id:'task-235',task:'任务 235',created:1800000000});
 await b.nodes.get('refresh').onclick();await turns();assert.equal(b.cards().at(-1).dataset.runId,'run-0235');
 // 更换排序时旧请求晚到不能覆盖新方向，当前房间不重连。
 let release,started;const ready=new Promise(r=>started=r),promise=new Promise(r=>release=r);b.state.hold={sort:'newest',started,promise};
 b.nodes.get('time-sort').value='newest';const pending=b.nodes.get('time-sort').onchange();await ready;
 await b.sort('oldest');release();await pending;await turns();assert.equal(b.cards()[0].dataset.runId,'run-0000');
 b=await boot('?sort=oldest&run=run-0234');assert.equal(b.cards()[0].dataset.runId,'run-0000');assert.equal(b.nodes.get('task-title').textContent,'任务 234');
 await b.sort('newest');assert.equal(new URL(b.location.href).searchParams.get('sort'),null);
 console.log('TIME_SORT_UI_OK');
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
