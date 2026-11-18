'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const dir=path.resolve(__dirname,'../../orchestrator/dashboard/static');
const html=fs.readFileSync(path.join(dir,'index.html'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
class Element{
 constructor(){this.children=[];this._text='';this.value='';this.disabled=false;this.style={};this.dataset={};this.classList={toggle(){}};}
 set textContent(v){this._text=String(v??'');this.children=[];}get textContent(){return this._text+this.children.map(n=>n.textContent||'').join('');}
 querySelectorAll(){return this.children.flatMap(node=>[...(node.dataset?.adapter?[node]:[]),...(node.querySelectorAll?node.querySelectorAll():[])]);}
 append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this._text='';this.children=[...nodes];}showModal(){this.open=true;}close(){this.open=false;}
}
(async()=>{
 const nodes=new Map(ids.map(id=>[id,new Element()]));
 const requests=[],messages=[];let page=0,resolveTemplates;
 const context=vm.createContext({document:{getElementById:id=>nodes.get(id),createElement:()=>new Element()},URLSearchParams,Map,Set,console,
  api:async(url,options)=>{requests.push({url,options});
   if(url==='/templates'&&!options)return new Promise(resolve=>{resolveTemplates=resolve;});
   if(url==='/templates')return {saved:true};
   if(url.startsWith('/compare'))return {same_config:false,left:{lifecycle:'failed'},right:{lifecycle:'done'},delta:{tokens:10,reported_tokens:null,reserved_tokens:0,duration_s:2}};
   if(url.startsWith('/failures')){page++;return {failures:[{id:'r1',run_id:'run1',categories:['timeout']}],errors:[],has_more:page===1,next_after:page===1?'cursor-1':null};}
   throw Error('unexpected '+url);},toast:message=>messages.push(message),select(){},renderTaskScope(){}});
 vm.runInContext(`const $=id=>document.getElementById(id);function el(tag,cls,text){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;}function show(){};const num=n=>String(n);const labels={done:'完成',failed:'失败'};let parent=null,taskProjectsFresh=true;const projects=[{id:'p',config_hash:'hash',models:[{name:'api',type:'openai_compat',model:'fixture-model',supports_effort:true,supports_thinking:true}]}],runs=[{id:'a',run_id:'a'},{id:'b',run_id:'b'}];`,context);
 vm.runInContext(fs.readFileSync(path.join(dir,'insights.js'),'utf8'),context);
 vm.runInContext(`renderBudgetAlerts({budget_alerts:[{dimension:'tokens',ratio:.85}]})`,context);
 assert.ok(nodes.get('budget-alert').textContent.includes('85%'));
 assert.ok(nodes.get('budget-alert').textContent.includes('不会增加预算'));
 const pending=vm.runInContext('loadTaskTemplates(false)',context);
 vm.runInContext('parent={id:"derive"}',context);
 resolveTemplates({templates:[{id:'t',name:'stale',usable:true}]});await pending;
 assert.equal(nodes.get('task-template').children.length,1,'stale template response must not populate derive dialog');
 vm.runInContext('parent=null',context);
 const fresh=vm.runInContext('loadTaskTemplates(false)',context);
 resolveTemplates({templates:[{id:'t',name:'常用',project_id:'p',config_hash:'hash',task:'需求',usable:true}]});await fresh;
 nodes.get('task-template').value='t';nodes.get('task-template').onchange();
 assert.equal(nodes.get('project-select').value,'p');assert.equal(nodes.get('task-input').value,'需求');
 nodes.get('unlimited-tokens').checked=true;
 const controls=nodes.get('model-settings').querySelectorAll();const jsonMode=controls.find(node=>node.dataset.option==='review_json_mode');assert.ok(jsonMode);jsonMode.value='true';
 const opts=vm.runInContext('executionOptions()',context);assert.equal(opts.models.api.review_json_mode,true);assert.equal(opts.unlimited_tokens,true);
 vm.runInContext("projects.push({id:'q',config_hash:'h',models:[{name:'qoder',type:'cli_qoder',model:'Ultimate',supports_effort:true,supports_thinking:true}]});",context);
 nodes.get('project-select').value='q';vm.runInContext('renderModelSettings()',context);
 const qc=nodes.get('model-settings').querySelectorAll();assert.ok(!qc.some(n=>n.dataset.option==='review_json_mode'));
 const effort=qc.find(n=>n.dataset.option==='reasoning_effort');assert.ok(effort.children.some(o=>o.value==='max'));
 const thinking=qc.find(n=>n.dataset.option==='thinking');assert.ok(thinking.children.some(o=>o.value==='adaptive'));assert.ok(!thinking.children.some(o=>o.value==='enabled'));
 effort.value='high';thinking.value='adaptive';const qo=vm.runInContext('executionOptions()',context);assert.equal(qo.models.qoder.thinking,'adaptive');

 vm.runInContext("projects.push({id:'c',config_hash:'h',models:[{name:'claude',type:'cli_claude',model:'',effective_model:'deepseek-v4-pro[1M]',reasoning_effort:'max',supports_effort:true,supports_thinking:false}]});",context);
 nodes.get('project-select').value='c';vm.runInContext('renderModelSettings()',context);
 const cc=nodes.get('model-settings').querySelectorAll();assert.equal(cc.find(n=>n.dataset.option==='model').value,'deepseek-v4-pro[1M]');
 assert.ok(cc.find(n=>n.dataset.option==='reasoning_effort').children.some(o=>o.value==='max'));
 assert.ok(!cc.some(n=>n.dataset.option==='review_json_mode'||n.dataset.option==='thinking'));
 assert.equal(requests.filter(r=>r.options?.method==='POST').length,0,'applying template must not execute');
 await nodes.get('compare-run').onclick();
 assert.ok(nodes.get('compare-result').textContent.includes('未知'));
 assert.ok(nodes.get('compare-result').textContent.includes('不同或未记录'));
 await nodes.get('failures-next').onclick();await nodes.get('failures-next').onclick();
 assert.equal(nodes.get('failure-results').children.length,1,'duplicate page record must not double count');
 assert.ok(nodes.get('failure-summary').textContent.includes('timeout 1'));
 assert.ok(requests.some(r=>r.url.includes('after=cursor-1')));
 // Real openStream function: old callbacks cannot override new room state; malformed events are ignored.
 const app=fs.readFileSync(path.join(dir,'app.js'),'utf8');
 const open=app.slice(app.indexOf('function openStream('),app.indexOf('function renderCLIActivity('));
 const connections=[],streams=[];
 class Source{constructor(){this.listeners={};streams.push(this);}addEventListener(name,fn){this.listeners[name]=fn;}}
 const replay=vm.createContext({EventSource:Source,Map,Math,JSON,Number,encodeURIComponent,setTimeout:()=>1,setConnection:(ok,text)=>connections.push(text),renderFeed(){},api(){},feedNearEnd:()=>true,trimEvents(){},updateFeedNavigation(){}});
 vm.runInContext("let stream=null,current={id:'old'},events=new Map(),reloadTimer=null,selectGeneration=0,latestFeedCursor=0,historyViewing=false,firstCursor=0;const pendingFeedEvents=new Map();",replay);
 vm.runInContext(open,replay);
 vm.runInContext("openStream('old',0);current={id:'new'};openStream('new',4)",replay);
 streams[0].onerror();assert.equal(connections.length,0);
 streams[1].onopen();assert.equal(connections.length,1);
 streams[0].listeners.update({data:JSON.stringify({cursor:1,kind:'message',payload:{}})});
 streams[1].listeners.update({data:'broken JSON'});
 streams[1].listeners.update({data:JSON.stringify({cursor:5,kind:'message',payload:{}})});
 assert.equal(vm.runInContext('events.size',replay),1);
 console.log('INSIGHTS_UI_OK');
})().catch(error=>{console.error(error);process.exitCode=1;});
