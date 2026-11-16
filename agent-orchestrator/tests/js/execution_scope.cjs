'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'../..'),html=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/index.html'),'utf8'),source=fs.readFileSync(path.join(root,'orchestrator/dashboard/static/app.js'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
class Element{
 constructor(){this.children=[];this._text='';this.value='';this.disabled=false;this.open=false;this.style={};this.dataset={};this.classes=new Set();this.classList={toggle:(n,v)=>{v=v===undefined?!this.classes.has(n):v;v?this.classes.add(n):this.classes.delete(n);},contains:n=>this.classes.has(n)};}
 set textContent(v){this._text=String(v??'');this.children=[];}get textContent(){return this._text+this.children.map(n=>n.textContent||'').join('');}
 append(...nodes){this.children.push(...nodes);}replaceChildren(...nodes){this._text='';this.children=[...nodes];}scrollIntoView(){}showModal(){this.open=true;}close(){this.open=false;}
}
const turn=async()=>{for(let i=0;i<40;i++)await new Promise(r=>setImmediate(r));};
const fixed={mode:'fixed_tests',label:'固定测试流程',summary:'只执行固定测试命令；不调用模型、不执行开发阶段。',workdir:'D:/demo/control-workspace',uses_model:false,stages:[{name:'testing',kind:'test',adapter_type:'command'}],matches_run_config:true};
const development={mode:'development',label:'代码开发流程',summary:'按配置调用开发执行器。',workdir:'D:/real/project',uses_model:true,stages:[{name:'coding',kind:'code',adapter_type:'cli_agy'}]};
async function boot({legacy=false,changed=false,unavailable=false}={}){
 const nodes=new Map(ids.map(id=>[id,new Element()]));nodes.get('status-filter').value='all';nodes.get('project-select').value='';
 const projects=[{id:'demo',name:'controls',execution_scope:legacy?undefined:fixed,config_hash:'hash-demo'},
                 {id:'code',name:'real',execution_scope:development,config_hash:'hash-code'}];
 if(unavailable)projects.push({id:'broken',name:'broken',execution_scope:null,config_hash:null,unavailable:'配置文件不存在: broken.toml'});
 const row={id:'run',run_id:'run',task:'开发预警中心',project_id:'demo',created:1,lifecycle:'done'};
 const snapshot={...row,stages:{testing:{status:'done'}},capabilities:{},pending_approvals:{},usage:{tokens:0},rounds:{},artifacts:[],threads:[],config_integrity:changed?'changed':'valid',last_cursor:0,
     execution_scope:legacy?undefined:{...fixed,matches_run_config:!changed}};
 const location={href:'http://local/',pathname:'/',search:'',hash:''};const state={requests:[],projectCalls:0,failProjects:false,staleHash:false};
 const response=(value,status=200)=>({ok:status<400,status,json:async()=>value});
 const fetch=async(url,options={})=>{state.requests.push({url,options});const u=new URL(url,'http://local');let data;
  if(u.pathname==='/api/session')data={csrf:'fixture'};
  else if(u.pathname==='/api/projects'){state.projectCalls++;if(state.failProjects)return response({error:'项目配置读取失败'},503);data={projects};}
  else if(u.pathname==='/api/runs'){
   if(options.method==='POST'){if(state.staleHash)return response({error:'配置已变化，请刷新后重试'},409);return response({...snapshot,id:'run-created',run_id:'run-created',lifecycle:'pending',task:'新建任务'},202);}
   data={runs:[row],has_more:false,next_cursor:null};}
  else if(u.pathname.endsWith('/snapshot'))data=snapshot;
  else if(u.pathname.endsWith('/events'))data={events:[]};
  else throw Error('unexpected request '+url);
  return response(data);};
 class EventSource{close(){}addEventListener(){}}
 const context=vm.createContext({document:{getElementById:id=>{assert.ok(nodes.has(id),'missing node '+id);return nodes.get(id);},createElement:()=>new Element(),querySelectorAll:()=>[]},
   location,fetch,EventSource,URL,URLSearchParams,Map,Set,console,crypto:{randomUUID:()=> 'fixture-id'},window:{history:{replaceState(){}}},setInterval:()=>1,setTimeout:()=>1,clearTimeout(){}});
 vm.runInContext(source,context);await turn();return {nodes,state};
}
const submit=node=>({preventDefault(){},submitter:node});
const posts=b=>b.state.requests.filter(r=>r.url==='/api/runs'&&r.options.method==='POST');
(async()=>{
 // 状态筛选文案与生命周期一致；阶段完成文案单独使用“已完成”。
 assert.ok(html.includes('<option value="done">流程完成</option>'),'状态筛选的完成文案应与运行生命周期一致');
 let b=await boot();
 assert.ok(b.nodes.get('run-list').children[0].textContent.includes('流程完成'),'运行卡片使用生命周期文案');
 // 打开“新建任务”时重新拉取项目配置，并携带 config_hash。
 b.nodes.get('new-run').onclick();await turn();
 assert.equal(b.nodes.get('project-select').value,'');assert.equal(b.nodes.get('task-submit').disabled,true);
 assert.ok(b.state.projectCalls>=2,'打开新建任务对话框应重新读取项目列表');
 b.nodes.get('project-select').value='demo';b.nodes.get('project-select').onchange();
 assert.ok(b.nodes.get('task-scope').textContent.includes('不执行开发阶段'));
 assert.ok(b.nodes.get('task-scope').textContent.includes('D:/demo/control-workspace'));
 assert.equal(b.nodes.get('task-submit').textContent,'启动固定测试');assert.equal(b.nodes.get('task-submit').disabled,false);
 b.nodes.get('task-input').value='验证新建任务请求';
 let prevented=false;await b.nodes.get('task-form').onsubmit({preventDefault(){prevented=true;},submitter:b.nodes.get('task-submit')});
 assert.equal(prevented,true);assert.equal(b.nodes.get('task-dialog').open,false);
 let body=JSON.parse(posts(b).at(-1).options.body);
 assert.equal(body.project_id,'demo');assert.equal(body.task,'验证新建任务请求');
 assert.equal(body.expected_config_hash,'hash-demo');assert.equal(body.parent_id,null);
 // 阶段卡片使用“已完成”，运行生命周期仍是“流程完成/固定测试已通过”。
 await b.nodes.get('run-list').children[0].onclick();await turn();
 assert.equal(b.nodes.get('lifecycle').textContent,'固定测试已通过');
 assert.ok(b.nodes.get('stage-cards').children[0].textContent.includes('已完成'));
 assert.equal(b.nodes.get('task-submit').disabled,true,'关闭对话框后不应保留可提交状态');
 // derive 对话框不展示父配置工作目录，列出阶段并携带父项目 config_hash。
 b.nodes.get('derive').onclick();await turn();
 assert.equal(b.nodes.get('project-select').value,'demo');assert.equal(b.nodes.get('project-select').disabled,true);
 const deriveText=b.nodes.get('task-scope').textContent;
 assert.ok(deriveText.includes('独立工作区'));assert.ok(deriveText.includes('当前固定 commit'));
 assert.ok(!deriveText.includes('D:/demo/control-workspace'),'derive 不得把父配置目录描述为执行目录');
 assert.ok(deriveText.includes('测试验证'));
 b.nodes.get('task-input').value='派生任务';
 await b.nodes.get('task-form').onsubmit(submit(b.nodes.get('task-submit')));
 body=JSON.parse(posts(b).at(-1).options.body);
 assert.equal(body.project_id,'demo');assert.equal(body.parent_id,'run');assert.equal(body.expected_config_hash,'hash-demo');
 // 不可用配置：可显示但禁止提交。
 b=await boot({unavailable:true});b.nodes.get('new-run').onclick();await turn();
 b.nodes.get('project-select').value='broken';b.nodes.get('project-select').onchange();
 assert.ok(b.nodes.get('task-scope').textContent.includes('配置不可用'));
 assert.equal(b.nodes.get('task-submit').disabled,true);
 const before=posts(b).length;
 await b.nodes.get('task-form').onsubmit(submit(b.nodes.get('task-submit')));
 assert.equal(posts(b).length,before,'不可用配置不得提交创建请求');
 assert.ok(b.nodes.get('toast').textContent.includes('配置不可用'));
 // 409 过期范围：toast 并刷新项目列表，不崩溃。
 b=await boot();b.nodes.get('new-run').onclick();await turn();
 b.nodes.get('project-select').value='demo';b.nodes.get('project-select').onchange();
 b.nodes.get('task-input').value='配置已变化的创建请求';b.state.staleHash=true;
 const calls=b.state.projectCalls;
 await b.nodes.get('task-form').onsubmit(submit(b.nodes.get('task-submit')));await turn();
 assert.ok(b.nodes.get('toast').textContent.includes('配置已变化'));
 assert.ok(b.state.projectCalls>calls,'409 后应刷新项目列表');
 assert.equal(b.nodes.get('task-dialog').open,true);
 // 项目列表刷新失败：toast 并禁用提交。
 b=await boot();b.state.failProjects=true;b.nodes.get('new-run').onclick();await turn();
 assert.ok(b.nodes.get('toast').textContent.includes('无法刷新项目配置'));
 assert.equal(b.nodes.get('task-submit').disabled,true);
 // 旧历史运行没有执行范围：仍不得提交。
 b=await boot({legacy:true});b.nodes.get('new-run').onclick();await turn();
 b.nodes.get('project-select').value='demo';b.nodes.get('project-select').onchange();
 assert.equal(b.nodes.get('task-submit').disabled,true);assert.ok(b.nodes.get('task-scope').textContent.includes('尚未取得'));
 // 配置已变化的旧运行不能声称固定测试已通过。
 b=await boot({changed:true});await b.nodes.get('run-list').children[0].onclick();await turn();
 assert.notEqual(b.nodes.get('lifecycle').textContent,'固定测试已通过');
 assert.ok(b.nodes.get('execution-scope').textContent.includes('不能据此推断旧运行交付'));
 console.log('EXECUTION_SCOPE_UI_OK');
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
