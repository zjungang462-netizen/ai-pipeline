'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const dir=path.resolve(__dirname,'../../orchestrator/dashboard/static');
const source=fs.readFileSync(path.join(dir,'i18n.js'),'utf8');
const html=fs.readFileSync(path.join(dir,'index.html'),'utf8');
function descendants(node){return node.children.flatMap(child=>[child,...descendants(child)]);}
function boot({search='',stored='zh-CN',throws=false}={}){
 class Element{
  constructor(tag='div'){this.tag=tag;this.children=[];this._text='';this.attributes={};this.dataset={};this.value='';}
  set textContent(value){this._text=String(value??'');this.children=[];}
  get textContent(){return this._text+this.children.map(child=>child.textContent||'').join('');}
  append(...nodes){this.children.push(...nodes);}
  getAttribute(name){return this.attributes[name]??null;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  querySelectorAll(selector){const match=selector.match(/^\[([^\]]+)\]$/),key=match?.[1].replace(/^data-/,'').replace(/-([a-z])/g,(_,letter)=>letter.toUpperCase());return descendants(this).filter(node=>match?Object.prototype.hasOwnProperty.call(node.dataset,key):node.tag===selector);}
 }
 class Event{
  constructor(type,options={}){this.type=type;this.detail=options.detail;}
 }
 class CustomEvent extends Event{}
 const document=new Element('#document');
 document.documentElement=new Element('html');
 const title=new Element('title');title.textContent='流水线 · 协作工作台';document.append(title);
 const languageButton=new Element('button');languageButton.id='language-toggle';document.append(languageButton);
 const staticCopy=new Element('span');staticCopy.textContent='让多智能体协作的每一步';staticCopy.dataset.i18n='hero.line';document.append(staticCopy);
 const dynamicCopy=new Element('button');document.append(dynamicCopy);
 const input=new Element('input');
 input.attributes.placeholder='填写任务说明';input.attributes['data-i18n-placeholder']='task.descriptionPlaceholder';input.dataset.i18nPlaceholder='task.descriptionPlaceholder';input.dataset.i18nPlaceholderOriginal='填写任务说明';
 document.append(input);
 const dialog=new Element('button');
 dialog.attributes['aria-label']='审核当前产物';dialog.attributes['data-i18n-aria']='stage.approveTitle';dialog.dataset.i18nAria='stage.approveTitle';dialog.dataset.i18nAriaLabelOriginal='审核当前产物';
 dialog.attributes.title='审核当前产物';dialog.attributes['data-i18n-title']='stage.approveTitle';dialog.dataset.i18nTitle='stage.approveTitle';dialog.dataset.i18nTitleOriginal='审核当前产物';
 document.append(dialog);
 const userNodes=['用户任务：修复 src/retry.py','模型输出：<diff>keep me</diff>','日志：exit code 7','路径：D:/work/项目/src/a.js','代码：const answer = 42;'].map(text=>{const node=new Element('pre');node.textContent=text;document.append(node);return node;});
 const listeners={};
 document.addEventListener=(type,fn)=>{(listeners[type]??=[]).push(fn);};
 document.dispatchEvent=event=>{for(const fn of listeners[event.type]||[])fn(event);return true;};
 document.getElementById=id=>id==='language-toggle'?languageButton:null;
 const storedValues=new Map([['wb_lang',stored]]);
 const localStorage={getItem(key){if(throws)throw Error('storage denied');return storedValues.get(key)??null;},setItem(key,value){if(throws)throw Error('storage denied');storedValues.set(key,String(value));}};
 const context=vm.createContext({document,location:{search},localStorage,Event,CustomEvent,URLSearchParams});
 vm.runInContext(source,context);
 return {context,i18n:context.WorkbenchI18n,document,documentElement:document.documentElement,title,languageButton,staticCopy,dynamicCopy,input,dialog,userNodes,storedValues,Event,CustomEvent};
}
(async()=>{
 const bare=vm.createContext({});
 vm.runInContext(source,bare);
 assert.equal(bare.WorkbenchI18n.language,'zh-CN','无浏览器全局对象时应使用中文默认');
 bare.WorkbenchI18n.setLanguage('en');assert.equal(bare.WorkbenchI18n.isEnglish(),true);
 bare.WorkbenchI18n.apply();bare.WorkbenchI18n.toggle();assert.equal(bare.WorkbenchI18n.language,'zh-CN');

 const chinese=boot();
 assert.equal(chinese.i18n.language,'zh-CN');
 assert.equal(chinese.documentElement.getAttribute('lang'),'zh-CN');
 assert.equal(chinese.staticCopy.textContent,'让多智能体协作的每一步');
 chinese.i18n.mark(chinese.dynamicCopy,'stage.approve','审核并通过本阶段产物');
 assert.equal(chinese.dynamicCopy.textContent,'审核并通过本阶段产物');
 chinese.i18n.setLanguage('en');
 assert.equal(chinese.staticCopy.textContent,'Make every multi-agent step');
 assert.equal(chinese.dynamicCopy.textContent,'Review & approve this artifact');
 assert.equal(chinese.input.getAttribute('placeholder'),'Describe what to complete, which files to change, and how completion will be verified…');
 assert.equal(chinese.dialog.getAttribute('aria-label'),'Approve current artifact');
 assert.equal(chinese.dialog.getAttribute('title'),'Approve current artifact');
 assert.equal(chinese.documentElement.getAttribute('lang'),'en');
 chinese.i18n.setLanguage('zh-CN');
 assert.equal(chinese.dynamicCopy.textContent,'审核并通过本阶段产物');
 assert.equal(chinese.input.getAttribute('placeholder'),'填写任务说明');
 assert.equal(chinese.dialog.getAttribute('aria-label'),'审核当前产物');
 assert.equal(chinese.dialog.getAttribute('title'),'审核当前产物');
 for(const node of chinese.userNodes)assert.equal(node.getAttribute('data-i18n'),null,'用户内容不应被标记翻译');
 assert.deepEqual(chinese.userNodes.map(node=>node.textContent),['用户任务：修复 src/retry.py','模型输出：<diff>keep me</diff>','日志：exit code 7','路径：D:/work/项目/src/a.js','代码：const answer = 42;']);

 const english=boot({search:'?lang=en',stored:'zh-CN'});
 assert.equal(english.i18n.language,'en','查询参数应优先于本地存储');
 assert.equal(english.staticCopy.textContent,'Make every multi-agent step');
 assert.ok(html.includes('data-i18n="hero.adaptersLabel"'),'适配器标签与数量文案必须使用不同键');
 assert.equal(english.i18n.t('hero.adaptersLabel'),'Adapters');
 assert.equal(english.i18n.t('hero.adapters','{count} 种',{count:10}),'10 adapters');
 english.languageButton.onclick();
 assert.equal(english.i18n.language,'zh-CN');
 assert.equal(english.storedValues.get('wb_lang'),'zh-CN','可见开关应持久化选择');
 assert.equal(english.languageButton.getAttribute('aria-pressed'),'false');
 assert.equal(english.languageButton.textContent,'中文 / EN');
 const persisted=boot({stored:'en'});assert.equal(persisted.i18n.language,'en');
 const overridden=boot({search:'?lang=zh-CN',stored:'en'});assert.equal(overridden.i18n.language,'zh-CN');
 const denied=boot({throws:true});denied.i18n.toggle();assert.equal(denied.i18n.language,'en','存储异常不得阻止切换');

 const events=[];
 chinese.document.addEventListener('workbench:languagechange',event=>events.push(event));
 assert.equal(chinese.i18n.setLanguage('en'),'en');
 assert.equal(events.length,1);assert.ok(events[0] instanceof chinese.CustomEvent,'必须派发浏览器可接受的事件对象');
 assert.equal(events[0].detail.language,'en');
 assert.equal(chinese.i18n.t('scope.workdir','实际工作目录：{path}。',{path:'D:/work/项目'}),'Actual working directory: D:/work/项目.');
 assert.equal(chinese.i18n.t('scope.attention','需关注 · ')+chinese.i18n.t('scope.criticalApproval','有产物等待审批'),'Attention · Artifact awaiting approval');
 assert.equal(chinese.i18n.t('route.card','代码阶段 (code) · {recommendation}',{kind:chinese.i18n.t('route.code','代码阶段 (code)'),recommendation:'Recommended: CLI / m'}),'Coding stage (code) · Recommended: CLI / m');
 assert.ok(!/[【】]/.test(chinese.i18n.t('demo.task')),'英文演练文案不应残留中文方头括号');
 assert.equal(chinese.i18n.error('候选未通过验收测试'),'Candidate did not pass acceptance tests');
 chinese.i18n.setLanguage('zh-CN');
 assert.equal(chinese.i18n.t('scope.workdir','实际工作目录：{path}。',{path:'D:/work/项目'}),'实际工作目录：D:/work/项目。');

 const scripts=[...html.matchAll(/<script\b([^>]*)>/g)].map(match=>match[1]).filter(attributes=>/\bsrc=/.test(attributes));
 assert.equal(scripts.length,3);
 assert.ok(/\bsrc="\/i18n\.js"/.test(scripts[0])&&/\bdefer\b/.test(scripts[0]),'i18n 必须首先 defer 加载');
 assert.ok(/\bsrc="\/app\.js"/.test(scripts[1])&&/\bdefer\b/.test(scripts[1]));
 assert.ok(/\bsrc="\/insights\.js"/.test(scripts[2])&&/\bdefer\b/.test(scripts[2]));
 console.log('I18N_CONTRACT_OK');
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
