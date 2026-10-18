const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
async function flush(){for(let i=0;i<16;i++)await Promise.resolve();}
function harness(template) {
  const source=fs.readFileSync(`webui/templates/${template}`, 'utf8');
  const loader=source.match(/async function loadAccounts\([^)]*\) \{[\s\S]*?\n\}\nasync function pollAccountPlanStatuses/)[0].replace(/\nasync function pollAccountPlanStatuses$/, '');
  const requests=[],renders=[],timers=new Map(),toasts=[];
  let nextTimer=0;
  const context=vm.createContext({
    SHOW_ARCHIVED_ACCOUNTS:false,ACCOUNTS:[],ACCOUNTS_TOTAL:0,PAGERS:{accounts:{page:1,size:20}},AbortController,
    getAccountsPlanFilter:()=>'',getAccountsCodexFilter:()=>'',getAccountsTotpFilter:()=>'',getAccountsQuery:()=>'',
    document:{getElementById:()=>null},$:()=>null,
    setTimeout(fn){timers.set(++nextTimer,fn);return nextTimer;},clearTimeout(id){timers.delete(id);},
    api(url,options){return new Promise((resolve,reject)=>{
      requests.push({url,resolve,reject});options?.signal?.addEventListener('abort',()=>reject(new Error('aborted')));
    });},
    renderAccounts(){renders.push(vm.runInContext('ACCOUNTS.map(a=>a.id).join(",")',context));},
    showToast(message){toasts.push(message);},
  });
  const run=code=>vm.runInContext(code,context);
  run('let accountsLoading=false;let accountsReloadPending=false;let accountsReloadInvalidated=false;'+loader);
  return {run,requests,renders,timers,toasts,
    async resolve(i,value){requests[i].resolve(value);await flush();},
    async reject(i){requests[i].reject(new Error('offline'));await flush();},
    async expire(){for(const fn of [...timers.values()])fn();await flush();},
  };
}
(async()=>{
  for(const template of ['index.html','index_legacy.html']) {
    // 真实 api() 在响应头已到达、读取 body 时取消，也必须抛错，不能吞掉变成成功空对象。
    const source=fs.readFileSync(`webui/templates/${template}`, 'utf8');
    const apiSource=source.match(/async function api\(url, opts\) \{[\s\S]*?\n\}/)[0];
    const controller=new AbortController();
    const apiContext=vm.createContext({fetch:async()=>({ok:true,json:async()=>{controller.abort();throw new Error('body aborted');}})});
    vm.runInContext(apiSource,apiContext);
    await assert.rejects(apiContext.api('/api/accounts',{signal:controller.signal}),/body aborted/);

    // 所有会改变账号集合或归档可见性的 mutation 都必须使正在加载的列表快照失效。
    const mutationSource=source.match(/async function deleteAccount\([^)]*\) \{[\s\S]*?\n\}\n\nasync function editAccountNote/)[0]
      .replace(/\n\nasync function editAccountNote$/, '');
    function mutationHarness(rejectAll) {
      const loads=[],requests=[];
      const context=vm.createContext({
        ACCOUNTS:[{id:2,email:'two@example.test'}],ACCOUNT_SELECTED:new Set([1,2]),SHOW_ARCHIVED_ACCOUNTS:false,
        confirm:()=>true,$:()=>({textContent:'账号操作',disabled:false}),showToast(){},loadSummary(){},updateAccountSelectionUi(){},
        loadAccounts(options){loads.push(options ? {...options} : null);},
        async api(url,options){
          requests.push({url,body:JSON.parse(options.body)});
          if(rejectAll)throw new Error('offline');
          if(url==='/api/accounts/archive-bulk')return {updated:[],updated_count:0,skipped:[{id:999}]};
          if(url==='/api/accounts/delete-bulk')return {deleted:[],deleted_count:0,skipped:[{id:999}]};
          return {ok:true};
        },
      });
      vm.runInContext(mutationSource,context);
      return {context,loads,requests};
    }
    async function exerciseMutations(h) {
      await vm.runInContext("deleteAccount(1,'one@example.test',{disabled:false})",h.context);
      await vm.runInContext("archiveOneAccount(2,true,{textContent:'归档',disabled:false})",h.context);
      vm.runInContext('ACCOUNT_SELECTED.add(2)',h.context);
      await vm.runInContext("archiveOneAccount(2,false,{textContent:'恢复',disabled:false})",h.context);
      vm.runInContext('ACCOUNT_SELECTED.add(3)',h.context);
      await vm.runInContext('archiveSelectedAccounts()',h.context);
      vm.runInContext('SHOW_ARCHIVED_ACCOUNTS=true;ACCOUNT_SELECTED.add(4)',h.context);
      await vm.runInContext('archiveSelectedAccounts()',h.context);
      vm.runInContext('ACCOUNT_SELECTED.add(5)',h.context);
      await vm.runInContext('deleteSelectedAccounts()',h.context);
    }
    const successMutations=mutationHarness(false);
    await exerciseMutations(successMutations);
    assert.deepEqual(
      successMutations.requests.map(request=>request.url),
      ['/api/accounts/1/delete','/api/accounts/2/archive','/api/accounts/2/archive','/api/accounts/archive-bulk','/api/accounts/archive-bulk','/api/accounts/delete-bulk'],
      `${template}: account mutation branches not exercised`,
    );
    assert.deepEqual(
      successMutations.requests.slice(3,5).map(request=>request.body.archived),[true,false],
      `${template}: batch archive/restore branches not exercised`,
    );
    assert.deepEqual(
      successMutations.loads,Array.from({length:6},()=>({invalidate:true})),
      `${template}: successful/partial account mutation did not invalidate list`,
    );
    const failedMutations=mutationHarness(true);
    await exerciseMutations(failedMutations);
    assert.equal(failedMutations.requests.length,6,`${template}: failure mutation branches not exercised`);
    assert.deepEqual(
      failedMutations.loads,Array.from({length:6},()=>({invalidate:true})),
      `${template}: failed account mutation did not invalidate list`,
    );

    // 删除后的刷新不能被正在加载的旧列表请求吞掉；显式失效请求会跳过旧快照。
    const h=harness(template);h.run('loadAccounts();loadAccounts({invalidate:true});loadAccounts({invalidate:true})');
    assert.equal(h.requests.length,1);
    await h.resolve(0,{items:[{id:2}],total:1});
    assert.equal(h.renders.length,0,`${template}: invalidated pre-delete list rendered`);
    assert.equal(h.requests.length,2,`${template}: post-delete refresh lost`);
    assert.equal(h.run('accountsLoading'),true);
    await h.resolve(1,{items:[],total:0});
    assert.deepEqual(h.renders,['']);assert.equal(h.run('accountsLoading'),false);
    // 连续轮询期间仍保持单 flight，但每个已完成响应都能渲染，不会被 pending 永久饿死。
    h.run('loadAccounts();loadAccounts()');
    assert.equal(h.requests.length,3);
    await h.resolve(2,{items:[{id:3}],total:1});
    assert.deepEqual(h.renders,['','3']);assert.equal(h.requests.length,4);
    h.run('loadAccounts()');
    await h.resolve(3,{items:[{id:4}],total:1});
    assert.deepEqual(h.renders,['','3','4']);assert.equal(h.requests.length,5);
    h.run('loadAccounts()');
    await h.resolve(4,{items:[{id:5}],total:1});
    assert.deepEqual(h.renders,['','3','4','5']);assert.equal(h.requests.length,6);assert.equal(h.run('accountsLoading'),true);
    await h.resolve(5,{items:[{id:6}],total:1});
    assert.deepEqual(h.renders,['','3','4','5','6']);assert.equal(h.run('accountsLoading'),false);
    // 页数缩小后自动补读最后一页，补读期间锁仍有效。
    h.run('PAGERS.accounts.page=9;loadAccounts()');
    await h.resolve(6,{items:[],total:21});
    assert.match(h.requests[7].url,/page=2&/);assert.equal(h.run('accountsLoading'),true);
    h.run('loadAccounts()');assert.equal(h.requests.length,8);
    await h.resolve(7,{items:[{id:21}],total:21});
    assert.equal(h.requests.length,9);assert.equal(h.run('accountsLoading'),true);
    await h.resolve(8,{items:[{id:21}],total:21});
    assert.equal(h.run('accountsLoading'),false);
    // 旧请求失败或超时也要释放锁，并执行已合并的刷新请求。
    h.run('loadAccounts();loadAccounts()');await h.reject(9);
    assert.equal(h.requests.length,11);await h.resolve(10,{items:[],total:0});
    h.run('loadAccounts();loadAccounts()');await h.expire();
    assert.equal(h.requests.length,13);assert.equal(h.run('accountsLoading'),true);
    await h.resolve(12,{items:[],total:0});
    assert.equal(h.timers.size,0);assert.equal(h.run('accountsLoading'),false);
  }
  console.log('新旧账号列表回归通过：删除后刷新合并、旧响应丢弃、缩页补读锁、失败/超时恢复。');
})().catch(error=>{console.error(error);process.exitCode=1;});
