// Offline controller tests. Real layout and DOM behavior are checked in the browser.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const base=path.join(__dirname,'../src/codeatlas/server/static');
const html=fs.readFileSync(path.join(base,'index.html'),'utf8');

function app(){
  const nodes=new Map();
  function node(id){if(!nodes.has(id))nodes.set(id,{
    value:'',textContent:'',innerHTML:'',disabled:false,dataset:{},attributes:{},events:{},inert:false,
    classList:{toggle(){},add(){},remove(){},contains(){return false;}},
    addEventListener(type,fn){this.events[type]=fn;},setAttribute(k,v){this.attributes[k]=v;},
    removeAttribute(k){delete this.attributes[k];},focus(){},querySelectorAll(){return [];},
    insertAdjacentHTML(_,s){this.innerHTML+=s;}
  });return nodes.get(id);}
  const context=vm.createContext({console,AbortController,setTimeout,clearTimeout,
    window:{matchMedia:()=>({matches:false,addEventListener(){}})},
    document:{querySelector:node,querySelectorAll:()=>[],getElementById:id=>node('#'+id),addEventListener(){},createElement:()=>node('created')},
    fetch:async()=>{throw Error('unexpected network');}
  });
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
  vm.runInContext(fs.readFileSync(path.join(base,'workspace.js'),'utf8').replace(/\ninit\(\);\s*$/,''),context);
  const evaluate=code=>vm.runInContext(code,context);
  evaluate('loadDashboard=()=>Promise.resolve();');
  return {context,node,evaluate};
}
const run={id:'run-test',knowledge_set_id:'set-1',execution_mode:'rule',events:[],latency_ms:1,
  evidence:{refused:false},evidence_registry:[],draft_preview:null};
function assertNoButtonHandler(markup){
  for(const button of markup.match(/<button\b[^>]*>/g)||[]){
    const attrs=Array.from(button.matchAll(/([^\s=]+)="([^"]*)"/g),m=>m[1]);
    assert.ok(!attrs.includes('onclick'));
  }
}

test('suggestions only fill text and composition Enter does not execute',()=>{
  const a=app(); a.evaluate("fillSuggestion('parse_value 如何工作？')");
  assert.equal(a.node('#workspaceQ').value,'parse_value 如何工作？');
  assert.equal(a.evaluate('agentInFlight'),false);
  a.node('#workspaceQ').events.keydown({key:'Enter',isComposing:true,preventDefault(){throw Error('IME intercepted');}});
});
test('workspace and Agent share one in-flight request; no implicit session or save',async()=>{
  const a=app();let finish,calls=[];
  a.context.fetch=async(url,options)=>{calls.push({url,body:JSON.parse(options.body)});await new Promise(r=>finish=r);return{ok:true,headers:{get:()=>null},json:async()=>run};};
  a.node('#workspaceQ').value='parse_value';a.node('#agentSession').value='old-session';
  const first=a.evaluate('runWorkspace()');await a.evaluate('runWorkspace()');await a.evaluate('doAgent()');
  assert.equal(calls.length,1);assert.equal(calls[0].body.save_draft,false);assert.equal(calls[0].body.session_id,null);
  assert.equal(a.node('#agentRun').disabled,true);finish();await first;
  assert.equal(a.node('#agentRun').disabled,false);assert.equal(a.node('#workspaceRun').disabled,false);
  assert.equal(a.node('#saveDraft').disabled,true);
});
test('failure and timeout leave input and unlock both controls without retry',async()=>{
  for(const name of ['Error','AbortError']){
    const a=app();let calls=0;
    a.context.fetch=async()=>{calls++;const e=new Error('offline');e.name=name;throw e;};
    a.node('#agentQ').value='keep this question';await a.evaluate('doAgent()');
    assert.equal(calls,1);assert.equal(a.node('#agentQ').value,'keep this question');
    assert.equal(a.node('#workspaceRun').disabled,false);assert.match(a.node('#agentOut').innerHTML,/未自动重试/);
  }
});
test('source from a different snapshot is never shown as old run evidence',async()=>{
  const a=app();a.context.fetch=async()=>({ok:true,headers:{get:()=> 'new-set'},json:async()=>({text:'WRONG_NEW_SOURCE'})});
  await a.evaluate("readEvidenceSource('mini.c',1,2,'old-set',document.querySelector('#slot'))");
  assert.match(a.node('#slot').innerHTML,/代码快照已切换/);assert.doesNotMatch(a.node('#slot').innerHTML,/WRONG_NEW_SOURCE/);
});
test('dynamic identifiers are escaped data, not executable inline JS',()=>{
  const a=app();const evil="x' onclick='alert(1)' <script>";
  a.context.evil=evil;
  const citation=a.evaluate("cite({level:'A',title:evil,provenance:{path:evil,usr:evil,line_start:1,line_end:2}},true)");
  assert.ok(!citation.includes('<script>'));assert.ok(citation.includes('data-id="x&#39;'));
  assertNoButtonHandler(citation);
  const card=a.evaluate("reviewHtml({id:evil,title:evil,status:'pending',anchors:[],qas:[{id:1,status:'pending'}]})");
  assertNoButtonHandler(card);
  const candidate=a.evaluate("candidateHtml({id:evil,status:'proposed',payload:{}})");
  assertNoButtonHandler(candidate);
});
test('long source ranges are paged at 300 lines and keep the original snapshot',async()=>{
  const a=app(),calls=[];
  a.context.fetch=async(_,options)=>{const body=JSON.parse(options.body);calls.push(body);return{ok:true,headers:{get:()=> 'set-1'},json:async()=>({text:'source body',line_start:body.line_start,line_end:body.line_end})};};
  await a.evaluate("readEvidenceSource('tcp_in.c',50,780,'set-1',document.querySelector('#slot'))");
  assert.equal(calls[0].line_end,349);
  assert.match(a.node('#slot').innerHTML,/data-start="350"/);
  assert.match(a.node('#slot').innerHTML,/data-set="set-1"/);
  assert.match(a.node('#slot').innerHTML,/tcp_in.c:50–349/);
  await a.evaluate("readEvidenceSource('tcp_in.c',650,780,'set-1',document.querySelector('#slot'))");
  assert.equal(calls[1].line_end,780);
  assert.doesNotMatch(a.node('#slot').innerHTML,/source-page/);
});
test('source errors do not render a successful source panel',async()=>{
  const a=app();a.context.fetch=async()=>({ok:true,headers:{get:()=> 'set-1'},json:async()=>({error:'line_out_of_range'})});
  await a.evaluate("readEvidenceSource('mini.c',99,100,'set-1',document.querySelector('#slot'))");
  assert.match(a.node('#slot').innerHTML,/源码读取失败/);
  assert.doesNotMatch(a.node('#slot').innerHTML,/<h3>源码核验/);
});
test('initial example and historical run are explicitly labeled',()=>{
  const a=app();a.evaluate("stats={repository:'https://github.com/DaveGamble/cJSON',knowledge_set_id:'current'};renderWorkspace()");
  assert.match(a.node('#recentRun').innerHTML,/演示示例/);
  a.context.testRun={...run,knowledge_set_id:'old'};a.evaluate("currentRun=testRun;lastRunQuestion='old question';renderWorkspace()");
  assert.match(a.node('#recentRun').innerHTML,/历史运行/);assert.match(a.node('#recentRun').innerHTML,/重新诊断/);
});
