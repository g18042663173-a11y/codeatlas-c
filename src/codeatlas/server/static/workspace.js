/* Presentation glue only. Knowledge, review and execution stay in the existing API. */
let agentInFlight = false;
let lastRunQuestion = '';
let workspaceCorpus = '';
const narrowWorkspace = window.matchMedia('(max-width: 900px)');
const icon = name => '<img src="/static/icons/'+name+'.svg" alt="">';

function closeNavigation(returnFocus=false){
  const wasOpen=$('#sidebar').classList.contains('open');
  $('#sidebar').classList.remove('open');
  $('#menuToggle').setAttribute('aria-expanded','false');
  $('#sidebar').inert=narrowWorkspace.matches;
  if(returnFocus&&wasOpen)$('#menuToggle').focus();
}
function openNavigation(){
  $('#sidebar').inert=false;
  $('#sidebar').classList.add('open');
  $('#menuToggle').setAttribute('aria-expanded','true');
  $('#closeNavigation').focus();
}
function setAgentBusy(busy){
  agentInFlight=busy;
  $('#agentRun').disabled=busy;
  $('#workspaceRun').disabled=busy;
  $('#agentRun').textContent=busy?'正在诊断…':'运行 Agent';
  $('#workspaceRun').innerHTML=(busy?'正在诊断…':'开始诊断')+icon('chevron-right');
  $('#agentOut').setAttribute('aria-busy',String(busy));
}
function fillSuggestion(question){
  $('#workspaceQ').value=question;
  $('#workspaceQ').focus();
}
async function runWorkspace(){
  if(agentInFlight)return;
  const q=$('#workspaceQ').value.trim();
  if(!q){$('#workspaceError').textContent='先写下你想查的代码问题。';$('#workspaceQ').focus();return;}
  $('#workspaceError').textContent='';
  $('#agentQ').value=q;
  // The workspace is a diagnosis entry, never implicitly attached to a previous session.
  $('#agentSession').value='';
  $('#agentMode').value='auto';
  show('agent');
  await doAgent();
}
function renderWorkspace(){
  if(!stats)return;
  const repo=(stats.repository||'').split('/').pop(),cjson=repo.toLowerCase()==='cjson';
  const suggestions=cjson?[
    'cJSON_Parse 如何处理尾随逗号？','深拷贝复制哪些节点，哪些选择引用？','cJSON_AddItemToObject 的失败路径？'
  ]:repo.toLowerCase()==='lwip'?[
    'netif_add 如何初始化接口？','pbuf_free 如何释放数据包？','tcp_input 的调用链和影响范围？'
  ]:['从哪些入口理解这个仓库？','核心函数有哪些调用者？','查找函数的错误处理路径'];
  if(workspaceCorpus!==repo){
    $('#suggestionLinks').innerHTML=suggestions.map(q=>'<button type="button" class="text-link" data-action="suggest" data-question="'+esc(q)+'">'+esc(q)+icon('chevron-right')+'</button>').join('');
    if(!workspaceCorpus&&!cjson)$('#workspaceQ').value=suggestions[0];
    workspaceCorpus=repo;
  }
  if(currentRun){
    const historical=Boolean(stats.knowledge_set_id&&currentRun.knowledge_set_id!==stats.knowledge_set_id);
    const refused=Boolean(currentRun.evidence?.refused),n=(currentRun.evidence_registry||[]).filter(c=>c.level==='A'||c.level==='B').length;
    const note=historical?'运行版本已变更；保留历史证据，核对当前源码前请重新诊断。':refused?'当前快照没有足够的 A/B 级证据；本次未生成知识卡。':'共找到 '+n+' 条 A/B 级证据；展开运行记录查看源码范围与只读工具轨迹。';
    $('#recentRun').innerHTML='<div class="recent-run">'+label(historical?'历史运行':refused?'证据不足':'本次运行')+'<div class="recent-question">'+esc(lastRunQuestion)+label(currentRun.execution_mode==='model'?'模型辅助 · 待核对':'确定性证据')+'</div><p class="recent-description">'+esc(note)+'</p><button class="text-link" data-action="last-run">查看记录'+icon('chevron-right')+'</button></div>';
  }else if(cjson){
    $('#recentRun').innerHTML='<div class="recent-run">'+label('演示示例')+'<div class="recent-question">cJSON_Delete 会释放引用节点的 child 吗？'+label('A 源码核验入口')+'</div><p class="recent-description">是否递归释放 child，取决于引用标志与 child 是否为空。跳过 child 不等于不释放当前节点。</p><button class="text-link" data-action="example-source">查看证据'+icon('chevron-right')+'</button></div>';
  }else{
    $('#recentRun').innerHTML='<div class="empty">还没有本页运行记录。输入问题后，查证过程和源码引用会出现在这里。</div>';
  }
}
function feedbackHtml(run){
  const tags=(run.evidence_registry||[]).map(x=>x.tag);
  return '<div class="card feedback-box"><h3>这次查证有帮助吗？</h3><p class="subtle">反馈只进入本地待处理队列，不会自动修改知识或批准卡片。</p><div class="row"><select id="feedbackTag" aria-label="错误证据标签"><option value="">不指定证据</option>'+tags.map(t=>'<option value="'+esc(t)+'">'+esc(t)+'</option>').join('')+'</select><input id="feedbackComment" aria-label="反馈说明" placeholder="可选：哪里有误，或还缺什么"></div><div class="card-actions"><button class="plain" data-action="feedback" data-verdict="helpful">有帮助</button><button class="plain danger" data-action="feedback" data-verdict="incorrect">有错误</button><button class="plain" data-action="feedback" data-verdict="incomplete">不完整</button></div><div id="feedbackState" class="subtle" role="status"></div></div>';
}
async function readEvidenceSource(path,start,end,expectedSet,target){
  start=Number(start);end=Number(end);
  const pageEnd=Math.min(end,start+299);
  const requestId=String(Number(target.dataset.requestId||0)+1);
  target.dataset.requestId=requestId;
  target.classList.add('source-reader');
  target.innerHTML='<div class="empty" role="status">正在读取当前冻结源码…</div>';
  try{
    const result=await request('/api/source/read',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,line_start:start,line_end:pageEnd})});
    if(expectedSet&&result.__responseSet!==expectedSet)throw new Error('代码快照已切换，不能用当前源码替代旧运行证据。请重新诊断');
    if(result.error)throw new Error('源码读取失败：'+result.error);
    if(requestId!==target.dataset.requestId)return;
    const actualStart=result.line_start??start,actualEnd=result.line_end??pageEnd;
    const next=actualEnd===pageEnd&&actualEnd<end?'<p class="subtle">当前显示第 '+actualStart+'–'+actualEnd+' 行；每次最多读取 300 行。</p><button class="plain" data-action="source-page" data-path="'+esc(path)+'" data-start="'+(actualEnd+1)+'" data-end="'+end+'" data-set="'+esc(expectedSet||result.__responseSet||'')+'">读取下一段</button>':'';
    target.innerHTML='<div class="card"><h3>源码核验</h3><div class="ref">'+esc(path)+':'+esc(actualStart)+'–'+esc(actualEnd)+' · '+esc(result.__responseSet||'当前版本')+'</div><pre class="source-view">'+esc(result.text||result.code||'无源码正文')+'</pre>'+next+'</div>';
  }catch(e){if(requestId===target.dataset.requestId)target.innerHTML='<div class="callout warn" role="alert">'+esc(e.message)+'</div>';}
}
async function showExampleSource(){
  const target=$('#recentSource');
  target.innerHTML='<div class="empty">正在定位 cJSON_Delete 的实际源码…</div>';
  try{
    const resolved=await request('/api/symbol/cJSON_Delete'),node=resolved.matches?.[0];
    if(!node)throw new Error('当前仓库未找到该示例符号');
    await readEvidenceSource(node.path,node.line_start,node.line_end,resolved.__responseSet,target);
  }catch(e){target.innerHTML='<div class="callout warn">'+esc(e.message)+'</div>';}
}
async function handleWorkspaceAction(button){
  const d=button.dataset;
  switch(d.action){
    case 'suggest': fillSuggestion(d.question); break;
    case 'last-run': show('agent'); if(currentRun)renderAgent(currentRun); break;
    case 'example-source': await showExampleSource(); break;
    case 'source-page': await readEvidenceSource(d.path,d.start,d.end,d.set,button.closest('.source-reader')); break;
    case 'source': {
      let target=button.closest('.card').querySelector('.source-slot');
      if(!target){target=document.createElement('div');target.className='source-slot';button.closest('.card').append(target);}
      await readEvidenceSource(d.path,d.start,d.end,d.set,target); break;
    }
    case 'anchor': prepareAnchor(d.id); break;
    case 'saved-card': openSavedCard(d.id); break;
    case 'import': await importExample(d.id); break;
    case 'bind': await approveCard(d.id); break;
    case 'experiments': await attachExperiments(d.id); break;
    case 'add-qa': await addQa(d.id); break;
    case 'qa-review': await reviewQa(Number(d.id),d.verdict==='true'); break;
    case 'review': await reviewCard(d.id,d.verdict); break;
    case 'wiki': await openWiki(d.id); break;
    case 'wiki-source': await readEvidenceSource(d.path,d.start,d.end,d.set,$('#wikiSource')); break;
    case 'session': await openSession(d.id); break;
    case 'candidate': await decideCandidate(d.id,d.verdict); break;
    case 'feedback': await submitFeedback(d.verdict); break;
  }
}
document.addEventListener('click',event=>{
  const page=event.target.closest('[data-page]');
  if(page){event.preventDefault();show(page.dataset.page);return;}
  const button=event.target.closest('button[data-action]');
  if(button&&!button.disabled)handleWorkspaceAction(button);
  if(narrowWorkspace.matches&&$('#sidebar').classList.contains('open')&&!event.target.closest('#sidebar,#menuToggle'))closeNavigation();
});
$('#workspaceForm').addEventListener('submit',event=>{event.preventDefault();runWorkspace();});
$('#workspaceQ').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();runWorkspace();}});
$('#agentQ').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.isComposing){event.preventDefault();doAgent();}});
$('#menuToggle').addEventListener('click',()=>$('#sidebar').classList.contains('open')?closeNavigation(true):openNavigation());
$('#closeNavigation').addEventListener('click',()=>closeNavigation(true));
document.addEventListener('keydown',event=>{
  if(event.key==='Escape')closeNavigation(true);
  if(event.key==='Tab'&&narrowWorkspace.matches&&$('#sidebar').classList.contains('open')){
    const targets=Array.from($('#sidebar').querySelectorAll('button,summary,a[href]')).filter(el=>!el.disabled);
    const first=targets[0],last=targets.at(-1);
    if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
    else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
  }
});
narrowWorkspace.addEventListener('change',()=>closeNavigation());
closeNavigation();
init();
