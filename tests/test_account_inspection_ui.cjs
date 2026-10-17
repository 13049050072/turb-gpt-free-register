const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync('webui/templates/account_inspections.html', 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const empty = {active:null, total:0, items:[]};
const active = {active:{id:1}, total:1, items:[{id:1, archived:'only', status:'running', workers:4}]};
async function flush() {for (let i = 0; i < 12; i++) await Promise.resolve();}
function harness() {
  const elements = new Map(), requests = [], timers = new Map(), toasts = [];
  let nextTimer = 0, accountsReloaded = 0;
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value:'4', open:false, disabled:false, textContent:'', innerHTML:'',
      classList:{contains:()=>false}, showModal(){this.open=true;}, close(){this.open=false;},
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    document:{hidden:false, getElementById:element}, SHOW_ARCHIVED_ACCOUNTS:true, AbortController,
    setTimeout(callback){const id=++nextTimer;timers.set(id,callback);return id;},
    clearTimeout(id){timers.delete(id);}, setInterval(){},
    showToast(message){toasts.push(message);}, loadAccounts(){accountsReloaded++;}, loadSummary(){},
    esc(value){return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));},
    api(url,options){return new Promise((resolve,reject)=>{
      const request={url,options,settled:false};
      request.resolve=value=>{request.settled=true;resolve(value);};
      request.reject=error=>{request.settled=true;reject(error);};
      requests.push(request);
      options?.signal?.addEventListener('abort',()=>request.reject(new Error('aborted')));
    });},
  });
  const run=source=>vm.runInContext(source,context);
  run(script);
  return {element,requests,timers,toasts,run,get accountsReloaded(){return accountsReloaded;},
    async resolve(index,value){requests[index].resolve(value);await flush();},
    async reject(index,error){requests[index].reject(error);await flush();},
    async expire(){for(const callback of [...timers.values()])callback();await flush();},
  };
}
(async()=>{
  // 范围以打开弹窗时为准；启动前/期间旧 GET 不能解锁，丢弃后立即补读。
  const h=harness();
  h.run('startAccountInspection()');
  assert.equal(h.element('inspectionScopeLabel').textContent,'已归档');
  h.run('SHOW_ARCHIVED_ACCOUNTS=false');
  const submit=h.run('submitAccountInspection({preventDefault(){}})');
  assert.deepEqual(JSON.parse(h.requests[1].options.body),{workers:4,archived:'only'});
  await h.resolve(0,empty);
  assert.equal(h.element('btnStartInspection').disabled,true);
  h.run('refreshInspectionHistory()');
  await h.resolve(1,{ok:true}); await submit;
  assert.equal(h.run('inspectionActive'),true);
  await h.resolve(2,empty);
  assert.equal(h.element('btnStartInspection').disabled,true);
  assert.equal(h.requests.length,4); // 不丢失启动完成时的刷新请求。
  await h.resolve(3,active);
  assert.match(h.element('inspectionHistoryContent').innerHTML,/已归档/);
  h.run('refreshInspectionHistory()');
  await h.reject(4,new Error('network failure'));
  assert.equal(h.run('inspectionLoading'),false);
  h.run('refreshInspectionHistory()'); await h.expire();
  assert.equal(h.run('inspectionLoading'),false);
  assert.equal(h.timers.size,0);
  h.run('refreshInspectionHistory()'); await h.resolve(6,empty);
  assert.equal(h.element('btnStartInspection').disabled,false);
  assert.equal(h.accountsReloaded,1);
  assert.match(h.element('inspectionHistoryContent').innerHTML,/暂无巡检记录/);
  h.run('refreshInspectionHistory()');
  await h.resolve(7,{active:null,total:1,items:[{
    id:8,archived:'only',status:'<img src=x>',workers:8,created_at:'2026-10-17T17:20:10',
    checked_count:44,total_count:40,skipped_count:2,invalid_count:5,
    live_success_count:3,live_failed_count:2,plan_success_count:2,plan_failed_count:1,
    error:'<script>alert(1)</script>',
  }]});
  const markup=h.element('inspectionHistoryContent').innerHTML;
  for(const pattern of [/44 \/ 40/,/跳过 2 个/,/width:100%/,/inspection-pill--unknown/,/&lt;script&gt;/])assert.match(markup,pattern);
  assert.doesNotMatch(markup,/<script>|<img/);
  assert.equal(h.element('inspectionRefresh').disabled,false);

  // POST 超时不能永久卡住，也不能假定服务端未创建任务而允许盲目重提。
  const timeout=harness();await timeout.resolve(0,empty);
  timeout.run('startAccountInspection()');
  const pending=timeout.run('submitAccountInspection({preventDefault(){}})');
  await timeout.expire();await pending;
  assert.equal(timeout.run('inspectionStarting'),false);
  assert.equal(timeout.run('inspectionUncertain'),true);
  assert.equal(timeout.element('btnStartInspection').disabled,true);
  assert.match(timeout.element('btnStartInspection').textContent,/待确认/);
  timeout.run('submitAccountInspection({preventDefault(){}})');
  assert.equal(timeout.requests.length,3);
  await timeout.resolve(2,active);
  assert.equal(timeout.run('inspectionUncertain'),false);
  assert.equal(timeout.run('inspectionActive'),true);

  // 切换页码时遇到正在加载的请求，完成后应立即读取目标页。
  const pages=harness();await pages.resolve(0,empty);
  pages.run('inspectionPage=2;refreshInspectionHistory()');
  pages.run('openInspectionHistory()');
  await pages.resolve(1,empty);
  assert.equal(pages.requests[2].url,'/api/accounts/inspections?page=1');
  await pages.resolve(2,empty);
  assert.equal(pages.run('inspectionLoading'),false);

  // POST 断连后的旧 GET 不得错误解除“状态待确认”。
  const uncertain=harness();await uncertain.resolve(0,empty);
  uncertain.run('startAccountInspection()');
  const failure=uncertain.run('submitAccountInspection({preventDefault(){}})');
  uncertain.run('refreshInspectionHistory()');
  await uncertain.reject(1,new Error('connection lost'));await failure;
  await uncertain.resolve(2,empty);
  assert.equal(uncertain.run('inspectionUncertain'),true);
  await uncertain.resolve(3,empty);
  assert.equal(uncertain.run('inspectionUncertain'),false);
  assert.equal(uncertain.element('btnStartInspection').disabled,false);
  assert.equal(uncertain.timers.size,0);

  const initial=harness();await initial.reject(0,new Error('offline'));
  assert.match(initial.element('inspectionHistoryContent').innerHTML,/暂时无法读取/);
  assert.equal(initial.element('inspectionRefresh').disabled,false);
  console.log('巡检 UI 验收通过：范围、旧响应隔离、GET/POST 超时、未知启动结果防重、分页补读、统计与转义。');
})().catch(error=>{console.error(error);process.exitCode=1;});
