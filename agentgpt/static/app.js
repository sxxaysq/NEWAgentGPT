const $ = (id) => document.getElementById(id);
const labels = {ready:'待开始',running:'执行中',responding:'回答中',pausing:'正在暂停',paused:'已暂停',blocked:'等待必要信息',stopping:'正在停止',stopped:'已停止',completed:'已完成',failed:'执行失败',interrupted:'可恢复'};
let user, selected, thread, state, eventSource, catalog = [], registerMode = false;
let defaults = {temperature:0.3,max_tokens:2000,language:'简体中文',max_tool_rounds:3,max_replans:8,memory_scope:'',enabled_tools:['builtin__web_search','builtin__web_fetch','builtin__private_search','builtin__private_read']};
let settingSessions = AgentSessionSettings.create(defaults);
let liveId = '', liveText = '', messageSignature = '', toolLines = [];
const cursors = new Map();
const followupOption=document.createElement('option');followupOption.value='followup';
followupOption.textContent='追加提问';$('chat-mode').replaceChildren(followupOption);
const blockedPanel=document.createElement('div');
blockedPanel.id='blocked-panel';blockedPanel.hidden=true;
const blockedTitle=document.createElement('strong');blockedTitle.textContent='需要你补充信息';
const blockedQuestion=document.createElement('p');blockedQuestion.id='blocked-question';
blockedPanel.append(blockedTitle,blockedQuestion);$('chat-form').before(blockedPanel);
const provideOption=document.createElement('option');provideOption.value='provide_input';
provideOption.textContent='补充信息并继续';provideOption.hidden=true;$('chat-mode').append(provideOption);
const replanLabel=document.createElement('label');replanLabel.textContent='重规划次数上限';
const replanInput=document.createElement('input');replanInput.id='max-replans';replanInput.type='number';
replanInput.min='1';replanInput.max='32';replanInput.value='8';replanLabel.append(replanInput);
$('max-tool-rounds').parentElement.after(replanLabel);

async function api(path, options = {}) {
  const config = {...options, credentials:'same-origin', headers:{...options.headers}};
  if (config.body && !(config.body instanceof FormData)) {
    config.headers['Content-Type'] = 'application/json';
    config.body = JSON.stringify(config.body);
  }
  const response = await fetch(path, config);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail || response.statusText);
    throw new Error(detail);
  }
  return response.json();
}

function toast(text) {
  $('toast').textContent = text; $('toast').hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => $('toast').hidden = true, 5000);
}
function guarded(fn) { return async (...args) => { try { await fn(...args); } catch (error) { toast(error.message); } }; }
function safeURL(value) { return /^https?:\/\//i.test(value) || /^\/artifacts\/[a-f0-9]{32}\.png$/.test(value) || /^\/api\/evidence\/(doc_|web_)[a-f0-9]+(?:\?offset=\d+&length=\d+)?$/.test(value); }

function inline(parent, text) {
  const pattern = /(!?)\[([^\]]*)\]\(([^\s)]+)\)|(https?:\/\/[^\s<>()]+)/g;
  let offset = 0, match;
  while ((match = pattern.exec(text))) {
    parent.append(document.createTextNode(text.slice(offset, match.index)));
    const url=match[4]||match[3], label=match[4]||match[2];
    if (!safeURL(url)) parent.append(document.createTextNode(match[0]));
    else if (match[1]) { const image = document.createElement('img'); image.src=url; image.alt=label; image.loading='lazy'; parent.append(image); }
    else { const link = document.createElement('a'); link.href=url; link.textContent=label; link.target='_blank'; link.rel='noopener noreferrer'; parent.append(link); }
    offset = pattern.lastIndex;
  }
  parent.append(document.createTextNode(text.slice(offset)));
}

function markdown(text) {
  const root = document.createElement('div');
  text=String(text||'').replace(/<think>[\s\S]*?<\/think>/g,'');
  if(text.includes('</think>'))text=text.split('</think>').at(-1);
  let code = null;
  for (const line of String(text || '').split('\n')) {
    if (/^\s*```/.test(line)) {
      if (code) code = null;
      else { code=document.createElement('pre'); root.append(code); }
      continue;
    }
    if (code) { code.append(document.createTextNode(line+'\n')); continue; }
    let tag='p', content=line;
    const heading=line.match(/^(#{1,6})\s+(.+)/);
    if (heading) { tag=heading[1].length<=2?'h3':'h4'; content=heading[2]; }
    else if (/^\s*[-*]\s+/.test(line)) { tag='li'; content=line.replace(/^\s*[-*]\s+/, ''); }
    const item=document.createElement(tag); inline(item,content); root.append(item);
  }
  return root;
}

async function refreshHistory() {
  const rows=await api('/api/threads'); $('history').replaceChildren();
  for (const item of rows) {
    const button=document.createElement('button');
    button.className=item.id===selected?'selected':'';
    button.append(document.createTextNode(item.title));
    const status=document.createElement('small'); status.textContent=labels[item.status]||item.status; button.append(status);
    button.onclick=guarded(() => loadThread(item.id)); $('history').append(button);
  }
}

function renderState() {
  if (!state) return;
  $('session-title').textContent=state.title||thread?.title||'正在规划任务';
  $('session-goal').textContent=state.goal||thread?.goal||'';
  const status=state.status||thread?.status||'ready';
  $('run-status').textContent=labels[status]||status;
  const active=['running','responding','pausing','stopping'].includes(status);
  $('pause').disabled=!active || ['pausing','stopping'].includes(status);
  $('resume').disabled=active || status==='completed' || status==='blocked';
  $('stop').disabled=['completed','stopped','ready'].includes(status);
  $('tasks').replaceChildren();
  const tasks=state.tasks||[];
  $('task-count').textContent=`${tasks.filter(t=>t.status==='completed').length}/${tasks.length} · 重规划 ${state.replan_count||0} 次`;
  for (const [index, task] of tasks.entries()) {
    const card=document.createElement('div'); card.className='task-card '+task.status;
    const number=document.createElement('span'); number.className='task-number'; number.textContent=task.status==='completed'?'✓':index+1;
    const content=document.createElement('div');const title=document.createElement('span'); title.textContent=task.title;
    const taskStatus=document.createElement('small');taskStatus.className='task-status';
    taskStatus.textContent=({pending:'待执行',running:'执行中',awaiting_review:'等待评估',completed:'已验收',failed:'待修订',blocked:'需要补充',cancelled:'不再需要'})[task.status]||task.status;
    if(task.outcome==='insufficient_evidence')taskStatus.textContent='已检索 · 资料不足';
    content.append(title,taskStatus);
    if(task.source_scope&&task.source_scope!=='auto'){const source=document.createElement('small');source.className='task-feedback';source.textContent='资料来源：'+({private:'私有资料',web:'公开网络',both:'私有资料＋公开网络',none:'无需检索'})[task.source_scope]+(task.source_reason?' · '+task.source_reason:'');content.append(source);}
    if(task.feedback){const feedback=document.createElement('small');feedback.className='task-feedback';feedback.textContent=task.feedback;content.append(feedback);}
    if(task.acceptance_criteria?.length){const details=document.createElement('details');const summary=document.createElement('summary');summary.textContent='完成标准';details.append(summary);for(const criterion of task.acceptance_criteria){const p=document.createElement('p');p.textContent=criterion;details.append(p);}content.append(details);}
    card.append(number,content); $('tasks').append(card);
  }
  const signature=JSON.stringify(state.messages||[]);
  if (signature!==messageSignature) {
    messageSignature=signature; $('messages').replaceChildren();
    for (const message of state.messages||[]) {
      const card=document.createElement('article'); card.className='message '+message.role;
      const role=document.createElement('div'); role.className='role'; role.textContent=message.role==='human'?'你':({feedback:'执行反馈',final_answer:'最终答复',clarification:'等待补充',budget_stop:'预算上限',replan_error:'计划调整'})[message.kind]||'AGENTGPT';
      if(message.kind)card.classList.add(message.kind);
      card.append(role,markdown(message.kind==='replan_error'?'计划还需调整，正在重新规划。':message.text)); $('messages').append(card);
    }
  }
  blockedPanel.hidden=status!=='blocked';
  provideOption.hidden=status!=='blocked';
  if(status==='blocked'){
    const question=(state.messages||[]).filter(m=>m.kind==='clarification').at(-1);
    blockedQuestion.textContent=question?.text||'请补充继续执行所需的信息。';
    if($('chat-mode').value!=='followup'||!blockedPanel.dataset.ready)$('chat-mode').value='provide_input';
    blockedPanel.dataset.ready='yes';$('chat').placeholder='回答上方问题，提交后继续重规划…';
  }else{
    delete blockedPanel.dataset.ready;
    if($('chat-mode').value==='provide_input')$('chat-mode').value='followup';
    $('chat').placeholder='继续提问，助手会规划任务、执行并核实结果…';
  }
  if (!active) { $('live-output').hidden=true; liveId=''; liveText=''; }
}

function subscribe(threadId, initialCursor=0) {
  if (eventSource) eventSource.close();
  const after=initialCursor; cursors.set(threadId, after);
  eventSource=new EventSource(`/api/threads/${threadId}/events?after=${after}`);
  eventSource.onopen=()=>{$('connection-state').textContent='已连接';};
  eventSource.onerror=(event)=>{if(!event.data)$('connection-state').textContent='连接中断，正在重连…';};
  const consume=(kind,handler)=>eventSource.addEventListener(kind,(event)=>{
    if (!event.data || selected!==threadId) return;
    const eventId=Number(event.lastEventId); if(eventId<=(cursors.get(threadId)||0))return; cursors.set(threadId,eventId);
    try { handler(JSON.parse(event.data)); } catch (error) { console.error(error); }
  });
  consume('state',data=>{state=data;liveId='';liveText='';$('live-output').hidden=true;renderState();});
  consume('recovering',()=>{liveId='';liveText='';$('live-output').hidden=true;$('connection-state').textContent='正在恢复运行';});
  consume('replan',()=>{}); consume('request',()=>{});
  consume('token',data=>{
    if (liveId!==data.message_id) {liveId=data.message_id;liveText='';}
    liveText+=data.text; $('live-output').hidden=false; $('live-text').textContent=liveText;
    $('live-node').textContent=data.node==='respond'?'正在回答':'正在执行当前任务';
  });
  consume('tool',showToolEvent);
  consume('error',data=>{ $('run-error').textContent=data.message;$('run-error').hidden=false;state.status='failed';renderState(); guarded(async()=>{const result=await api(`/api/threads/${threadId}`);if(selected===threadId)renderPending(result.pending_tools||[]);})(); });
  consume('settled',()=>guarded(async()=>{const result=await api(`/api/threads/${threadId}`);if(selected===threadId){thread=result.thread;state=result.state;state.status=thread.status;renderState();renderPending(result.pending_tools||[]);await refreshHistory();}})());
}

function showToolEvent(data) {
  const name=data.name||'MCP 工具', kind=data.event||'';
  if (['tool-started','tool-finished','tool-error'].includes(kind)) {
    toolLines.push(`${kind==='tool-started'?'调用':kind==='tool-error'?'失败':'完成'} · ${name}`);
    $('tool-log').replaceChildren();
    for (const line of toolLines.slice(-12)) {const p=document.createElement('p');p.textContent=line;$('tool-log').append(p);}
  }
}

async function loadThread(id) {
  selected=id;sessionStorage.setItem(`active-thread:${user?.id}`,id); const result=await api(`/api/threads/${id}`); thread=result.thread; state=result.state;
  state.status=thread.status; defaults=settingSessions.load(thread.settings);
  $('welcome').hidden=true; $('workspace').hidden=false; $('run-error').hidden=true; $('live-output').hidden=true;
  liveId=''; liveText=''; messageSignature=''; toolLines=[]; $('tool-log').textContent='等待工具调用';
  for(const activity of result.tool_activity||[])showToolEvent(activity);
  $('export-md').href=`/api/threads/${id}/export?format=md`; $('export-json').href=`/api/threads/${id}/export?format=json`;
  $('revoke-share').hidden=!thread.share_token;
  renderState(); renderPending(result.pending_tools||[]); if(result.live?.text){liveId=result.live.message_id;liveText=result.live.text;$('live-output').hidden=false;$('live-text').textContent=liveText;} subscribe(id,result.event_cursor||0); await refreshHistory();
}

function newSession() {
  if(eventSource)eventSource.close();sessionStorage.removeItem(`active-thread:${user?.id}`);selected=null;thread=null;state=null;
  defaults=settingSessions.newSession();
  $('welcome').hidden=false;$('workspace').hidden=true;$('goal').focus();
  guarded(refreshHistory)();
}

async function sendCommand(operation,text='') {
  if(!selected)return;
  const pendingKey=`pending-command:${user?.id}:${selected}`; const saved=JSON.parse(sessionStorage.getItem(pendingKey)||'null');
  const body={operation,text,request_id:saved&&saved.operation===operation&&saved.text===text?saved.request_id:(crypto.randomUUID?.()||`${Date.now()}-${Math.random().toString(36).slice(2)}`)};
  if(operation==='provide_input')body.question_id=(state.messages||[]).filter(m=>m.kind==='clarification').at(-1)?.id;
  sessionStorage.setItem(pendingKey,JSON.stringify(body));
  await api(`/api/threads/${selected}/commands`,{method:'POST',body}); sessionStorage.removeItem(pendingKey);
  if(operation==='pause'||operation==='stop'){state.status=operation==='pause'?'pausing':'stopping';renderState();}
}

async function initialize() {
  user=await api('/api/auth/me');$('user-email').textContent=user.email;
  const tools=await api('/api/tools');catalog=tools.tools;
  settingSessions=AgentSessionSettings.create(tools.default_settings||defaults);defaults=settingSessions.newSession();
  $('model-label').textContent=tools.model;$('mcp-label').textContent=`MCP · ${Object.keys(tools.servers).length} 个服务`;
  await refreshHistory();$('connection-state').textContent='已连接';
  const previous=sessionStorage.getItem(`active-thread:${user.id}`);if(previous)await loadThread(previous);
}

$('auth-dialog').addEventListener('cancel',event=>event.preventDefault());
$('auth-toggle').onclick=()=>{registerMode=!registerMode;$('auth-title').textContent=registerMode?'创建你的账号':'欢迎回来';$('auth-submit').textContent=registerMode?'创建账号':'登录';$('auth-toggle').textContent=registerMode?'已有账号？返回登录':'还没有账号？创建账号';$('auth-error').textContent='';};
$('auth-form').onsubmit=async event=>{
  event.preventDefault();$('auth-submit').disabled=true;$('auth-error').textContent='';
  try {await api('/api/auth/'+(registerMode?'register':'login'),{method:'POST',body:{email:$('email').value,password:$('password').value}});$('auth-dialog').close();await initialize();}
  catch(error){$('auth-error').textContent=error.message;}
  finally{$('auth-submit').disabled=false;}
};
$('logout').onclick=guarded(async()=>{await api('/api/auth/logout',{method:'POST'});location.reload();});
$('new-session').onclick=newSession;
document.querySelectorAll('[data-example]').forEach(button=>button.onclick=()=>{$('goal').value=button.dataset.example;$('goal').focus();});
$('goal-form').onsubmit=guarded(async event=>{
  event.preventDefault();const button=event.submitter;button.disabled=true;
  try {const result=await api('/api/threads',{method:'POST',body:{goal:$('goal').value.trim(),settings:defaults}});await loadThread(result.thread.id);await sendCommand('start');}
  finally{button.disabled=false;}
});
$('pause').onclick=guarded(()=>sendCommand('pause'));
$('resume').onclick=guarded(()=>sendCommand(state.status==='ready'?'start':'continue'));
$('stop').onclick=guarded(()=>sendCommand('stop'));
$('summarize').onclick=guarded(()=>sendCommand('summarize'));
$('chat-form').onsubmit=guarded(async event=>{event.preventDefault();const text=$('chat').value.trim();if(!text)return;const button=event.submitter;button.disabled=true;try{await sendCommand($('chat-mode').value,text);$('chat').value='';}finally{button.disabled=false;}});
$('share').onclick=guarded(async()=>{
  const result=await api(`/api/threads/${selected}/share`,{method:'POST'});const url=location.origin+result.path;
  try {await navigator.clipboard.writeText(url);toast('分享链接已复制');}catch{prompt('复制只读分享链接',url);}
  $('revoke-share').hidden=false;
});
$('revoke-share').onclick=guarded(async()=>{await api(`/api/threads/${selected}/share`,{method:'DELETE'});$('revoke-share').hidden=true;toast('分享已撤销');});
document.querySelectorAll('[data-close]').forEach(button=>button.onclick=()=>$(button.dataset.close).close());
$('settings-button').onclick=()=>{
  $('temperature').value=defaults.temperature;$('max-tokens').value=defaults.max_tokens;$('language').value=defaults.language;$('max-tool-rounds').value=defaults.max_tool_rounds;$('max-replans').value=defaults.max_replans||8;$('memory-scope').value=defaults.memory_scope||'';
  $('tool-options').replaceChildren();
  for(const tool of catalog){const label=document.createElement('label');const input=document.createElement('input');input.type='checkbox';input.value=tool.name;input.checked=defaults.enabled_tools.includes(tool.name)&&tool.available;input.disabled=!tool.available;label.append(input,document.createTextNode(' '+tool.name.replace('builtin__','')));const small=document.createElement('small');small.textContent=tool.reason||tool.description;label.append(small);$('tool-options').append(label);}
  $('settings-dialog').showModal();
};
$('settings-form').onsubmit=guarded(async event=>{
  event.preventDefault();const options={knowledge_base:kbInput.value.trim(),memory_scope:$('memory-scope').value.trim(),temperature:Number($('temperature').value),max_tokens:Number($('max-tokens').value),language:$('language').value,max_tool_rounds:Number($('max-tool-rounds').value),max_replans:Number($('max-replans').value),enabled_tools:[...$('tool-options').querySelectorAll('input:checked')].map(input=>input.value)};
  if(selected)await api(`/api/threads/${selected}/settings`,{method:'PATCH',body:options});defaults=settingSessions.save(options,selected);$('settings-dialog').close();toast('设置已保存');
});
const automaticSearch=document.createElement('button');automaticSearch.type='button';automaticSearch.textContent='启用自动检索（本地＋联网）';
automaticSearch.onclick=()=>{for(const input of $('tool-options').querySelectorAll('input')){if(['builtin__private_search','builtin__private_read','builtin__web_search','builtin__web_fetch'].includes(input.value)&&!input.disabled)input.checked=true;}};
$('tool-options').before(automaticSearch);
const toolHint=document.createElement('p');toolHint.className='muted';toolHint.textContent='勾选表示允许使用，模型会按问题语义选择工具，并非每次全部调用。当前会话的设置不会覆盖新目标默认值。';$('tool-options').before(toolHint);
async function listDocuments(){
  const documents=await api('/api/documents');$('documents').replaceChildren();
  for(const doc of documents){
    const row=document.createElement('div');row.className='document-row';
    const name=document.createElement('div');name.textContent=doc.title;
    const small=document.createElement('small');
    const status={processing:'处理中',ready:'可检索',failed:'入库失败'}[doc.status]||'待迁移';
    small.textContent=`${doc.kb||'默认知识库'} · v${doc.version||1} · ${status} · ${doc.characters} 字符${doc.error?' · '+doc.error:''}`;
    if(doc.status!=='ready'&&doc.active_version)small.append('（上一版本仍可检索）');
    name.append(small);row.append(name);
    const update=document.createElement('button');update.textContent='更新版本';
    update.onclick=()=>{const input=document.createElement('input');input.type='file';input.accept='.pdf,.txt,.md,.csv,.json';input.onchange=guarded(async()=>{if(!input.files[0])return;const data=new FormData();data.append('file',input.files[0]);data.append('document_id',doc.id);await api('/api/documents',{method:'POST',body:data});await listDocuments();});input.click();};row.append(update);
    if(doc.status==='failed'){const retry=document.createElement('button');retry.textContent='重试';retry.onclick=guarded(async()=>{await api(`/api/documents/${doc.id}/retry`,{method:'POST'});await listDocuments();});row.append(retry);}
    const button=document.createElement('button');button.textContent='删除';button.onclick=guarded(async()=>{await api(`/api/documents/${doc.id}`,{method:'DELETE'});await listDocuments();});row.append(button);$('documents').append(row);
  }
}
$('knowledge-button').onclick=guarded(async()=>{await listDocuments();$('knowledge-dialog').showModal();});
$('upload-form').onsubmit=guarded(async event=>{event.preventDefault();const data=new FormData();data.append('file',$('document-file').files[0]);data.append('kb',$('upload-kb').value.trim());await api('/api/documents',{method:'POST',body:data});$('document-file').value='';await listDocuments();toast('资料已保存，后台正在解析并建立索引');});

const kbLabel=document.createElement('label');kbLabel.textContent='本轮知识库（空白为默认）';
const kbInput=document.createElement('input');kbInput.id='knowledge-base';kbInput.maxLength=80;kbLabel.append(kbInput);$('memory-scope').parentElement.after(kbLabel);
const uploadKb=document.createElement('input');uploadKb.id='upload-kb';uploadKb.placeholder='上传到知识库（空白为默认）';uploadKb.maxLength=80;$('document-file').before(uploadKb);
const openSettings=$('settings-button').onclick;$('settings-button').onclick=()=>{kbInput.value=defaults.knowledge_base||'';openSettings();};
kbInput.oninput=()=>{defaults.knowledge_base=kbInput.value.trim();};
const evidenceDialog=document.createElement('dialog');evidenceDialog.style.maxWidth='900px';evidenceDialog.style.width='85vw';
const evidenceClose=document.createElement('button');evidenceClose.textContent='关闭';evidenceClose.onclick=()=>evidenceDialog.close();
const evidenceTitle=document.createElement('h3');const evidenceText=document.createElement('pre');evidenceText.style.whiteSpace='pre-wrap';
evidenceDialog.append(evidenceClose,evidenceTitle,evidenceText);document.body.append(evidenceDialog);
document.addEventListener('click',event=>{const a=event.target.closest('a');if(!a||!a.getAttribute('href')?.startsWith('/api/evidence/'))return;event.preventDefault();guarded(async()=>{
  const item=await api(a.getAttribute('href'));evidenceTitle.textContent=item.title||'证据原文';
  evidenceText.textContent=item.source_type==='document'?`文档版本 ${item.version} · ${item.location.page?'PDF第'+item.location.page+'页':item.location.section||'文本段落'}${item.location.location_missing?'（旧资料缺少原始页码）':''}\n字符位置 ${item.location.start}—${item.location.end}\n\n${item.text}`:
    `${item.url}\n抓取时间：${item.fetched_at||''}\n\n`+(item.paragraphs||[]).map(p=>`[段落${p.paragraph}${p.page?' / PDF第'+p.page+'页':''}] ${p.text}`).join('\n\n');evidenceDialog.showModal();
})();});
setInterval(()=>{if($('knowledge-dialog').open)guarded(listDocuments)();},3000);

(async()=>{
  if(location.pathname.startsWith('/s/')){
    document.body.classList.add('shared');$('welcome').hidden=true;$('workspace').hidden=false;
    try {const data=await api('/api/shared/'+location.pathname.slice(3));state=data.state;thread={title:data.title};$('model-label').textContent='只读分享';renderState();}
    catch(error){$('run-error').textContent=error.message;$('run-error').hidden=false;}
    return;
  }
  try{await initialize();}catch{$('model-label').textContent='AgentGPT · 任务助手';$('auth-dialog').showModal();}
})();

async function listMemories(){
  const data=await api('/api/memories'); $('memory-enabled').checked=!!data.preference.enabled;
  const lite=data.mode==='lite'; $('memory-retry').hidden=lite;
  $('memory-status').textContent=!data.available?'自动记忆服务未启用；仍可管理已保存的记忆。':!data.preference.enabled?'已关闭自动记忆与召回；仍可管理已保存的记忆。':lite?'自动保存你明确表达的长期偏好，并在后续会话中使用。临时要求仅用于当前对话；未保存的信息可在新消息中重新提供。':`自动抽取：待处理 ${(data.jobs.queued||0)+(data.jobs.running||0)} · 已完成 ${data.jobs.done||0} · 失败 ${data.jobs.failed||0}。索引更新可能稍有延迟。`;
  $('memory-items').replaceChildren();
  for(const item of data.items){
    const row=document.createElement('div');row.className='document-row';
    const content=document.createElement('div');content.append(document.createTextNode(item.content));
    const meta=document.createElement('small');meta.textContent=`${item.scope||'全局'} · v${item.version} · ${item.source_thread?'来自会话 '+item.source_thread.slice(0,8):'手动保存'}`;content.append(meta);
    const edit=document.createElement('button');edit.textContent='修改';edit.onclick=guarded(async()=>{const value=prompt('修改记忆',item.content);if(value?.trim()){await api('/api/memories/'+item.id,{method:'PATCH',body:{content:value.trim(),version:item.version}});await listMemories();}});
    const remove=document.createElement('button');remove.textContent='删除';remove.onclick=guarded(async()=>{await api(`/api/memories/${item.id}?version=${item.version}`,{method:'DELETE'});await listMemories();});
    row.append(content,edit,remove);$('memory-items').append(row);
  }
}
$('memory-button').onclick=guarded(async()=>{await listMemories();$('memory-dialog').showModal();});
$('memory-enabled').onchange=guarded(async()=>{await api('/api/memory-preference',{method:'PATCH',body:{enabled:$('memory-enabled').checked}});await listMemories();});
$('memory-form').onsubmit=guarded(async event=>{event.preventDefault();const scope=$('memory-new-scope').value.trim();await api('/api/memories',{method:'POST',body:{content:$('memory-content').value.trim(),scope,kind:scope?'project':'preference'}});$('memory-content').value='';await listMemories();});
function renderPending(items){
  let panel=$('pending-tools'); if(!panel){panel=document.createElement('div');panel.id='pending-tools';$('run-error').after(panel);}panel.replaceChildren();
  for(const item of items){
    const card=document.createElement('div');card.className='task-card';const title=document.createElement('p');title.textContent=`调用结果待核实：${item.tool||item.call_key}`;
    const details=document.createElement('pre');details.textContent=JSON.stringify(item.arguments||{},null,2);card.append(title,details);
    for(const [action,label] of [['record_success','补录已成功结果'],['record_failure','标记已失败'],['retry','确认重新调用']]){
      const button=document.createElement('button');button.textContent=label;button.onclick=guarded(async()=>{
        let result='';if(action==='retry'){if(!confirm('该工具可能已经执行。确认允许再次调用？'))return;}else{result=prompt('填写你已核实的执行结果');if(!result?.trim())return;}
        await api(`/api/threads/${selected}/resolve-tool`,{method:'POST',body:{call_key:item.call_key,action,result}});await loadThread(selected);toast('核实已保存，点击继续恢复。');
      });card.append(button);
    }panel.append(card);
  }
}

$('memory-retry').onclick=guarded(async()=>{await api('/api/memory-jobs/retry',{method:'POST'});await listMemories();});
