// Exercise the shipped polling, request timeout and latest-filter queue.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const script=fs.readFileSync(process.argv[2],'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
new Function(script);
function slice(a,b){return script.slice(script.indexOf(a),script.indexOf(b,script.indexOf(a)))}
function setup(){
 let clock=1000000,id=0;const timers=new Map(),intervals=new Map(),requests=[],renders=[];
 const nodes=Object.fromEntries(['stage','direction','minScore','minRR','q','scan','force','autoScan','screenStatus','lastScan','marketRefreshStatus','nearCandidates','nearCandidatesStatus'].map(x=>[x,{value:'',textContent:'',innerHTML:'',querySelectorAll:()=>[]}]));
 nodes.stage.value='ALL';nodes.direction.value='ALL';
 const response=(age=0,ticker='NEWUSDC')=>({snapshot_age_seconds:age,matched:1,scanned:181,universe:185,coverage_pct:97.8,results:[{ticker}],ready_candidates:[],qualified_near_candidates:[],summary:{near_signal_count:31,qualified_near_count:0}});
 const ctx={console,AbortController,URL,Number,Math,Set,
  Date:class extends Date{static now(){return clock}},
  document:{visibilityState:'visible',getElementById:x=>nodes[x]},
  watch:new Set(),autoScanTimer:null,marketScanQueued:null,entryHeartbeatBusy:false,
  globalEntrySnapshot:null,lastMarketPayload:null,rows:[],
  setTimeout(fn,delay){const key=++id;timers.set(key,{fn,delay});return key},clearTimeout:key=>timers.delete(key),
  setInterval(fn,delay){const key=++id;intervals.set(key,{fn,delay});return key},clearInterval:key=>intervals.delete(key),
  jf:async(url,opt)=>{requests.push({url,opt});return ctx.reply(url,opt)},reply:async()=>response(),
  fmt:x=>String(x),summaryView(){},renderRows(){renders.push(ctx.rows.map(x=>x.ticker))},
  renderDailyConvenience(){},processCandidateTransitions(){},
  renderGlobalEntryBar(j){ctx.globalEntrySnapshot=j},renderEntryNow(){},renderDashboard(){},
  refreshGlobalEntryAge(){ctx.aged=(ctx.aged||0)+1},
  entryFreshness(j){return {stale:!j||j.snapshot_age_seconds+(clock-j._client_rendered_ms)/1000>=120}}
 };
 vm.createContext(ctx);
 vm.runInContext(slice('async function refreshEntryHeartbeat(','function scenarioRiskParams('),ctx);
 vm.runInContext(slice('function screenerQuery(','document.getElementById("presetNear")'),ctx);
 vm.runInContext(slice('function renderNearCandidates(','function stageJa('),ctx);
 return {ctx,nodes,timers,intervals,requests,renders,response,advance:ms=>clock+=ms};
}
async function settle(){for(let i=0;i<8;i++)await new Promise(setImmediate)}
async function test(name){
 const {ctx,nodes,timers,intervals,requests,renders,response,advance}=setup();
 if(name==='default_poll'){
  assert(script.includes('applyDeepLink();startAutoScan();'));
  ctx.startAutoScan();ctx.startAutoScan();assert.equal(intervals.size,1);
  const poll=[...intervals.values()][0];assert.equal(poll.delay,30000);
  poll.fn();await settle();assert.equal(requests.length,1);
  assert.equal(ctx.rows[0].ticker,'NEWUSDC');assert.equal(renders.length,1);
  assert(nodes.autoScan.textContent.includes('ON (30秒)'));assert.equal(ctx.entryHeartbeatBusy,false);
  assert(requests.every(r=>!r.opt.method||r.opt.method==='GET'));
 }else if(name==='hidden_and_resume'){
  ctx.startAutoScan();ctx.document.visibilityState='hidden';[...intervals.values()][0].fn();await settle();assert.equal(requests.length,0);
  let listener;ctx.document.addEventListener=(n,fn)=>{listener=fn};
  const code=script.match(/document\.addEventListener\("visibilitychange",function\(\)\{[\s\S]*?\}\);/g).find(x=>x.includes('refreshEntryHeartbeat'));
  vm.runInContext(code,ctx);ctx.document.visibilityState='visible';listener();assert.equal(ctx.aged,1);await settle();
  assert.equal(requests.length,1);assert.equal(ctx.rows[0].ticker,'NEWUSDC');
 }else if(name==='restored_page'){
  ctx.startAutoScan();let listener;ctx.window={addEventListener:(n,fn)=>{assert.equal(n,'pageshow');listener=fn}};
  const code=script.split('\n').find(x=>x.startsWith("window.addEventListener('pageshow'"));vm.runInContext(code,ctx);
  listener({persisted:false});await settle();assert.equal(requests.length,0);
  listener({persisted:true});await settle();assert.equal(requests.length,1);assert.equal(ctx.aged,1);
 }else if(name==='old_cache_refresh'){
  ctx.reply=async url=>response(url.includes('force=true')?0:90);
  await ctx.scan(false);assert.equal(requests.length,2);assert(requests[1].url.includes('force=true'));
  assert.equal(ctx.globalEntrySnapshot.snapshot_age_seconds,0);
  ctx.reply=async()=>response(120);await ctx.scan(false);
  assert.equal(requests.length,4);assert.equal(ctx.globalEntrySnapshot.snapshot_age_seconds,120);
  assert(ctx.entryFreshness(ctx.globalEntrySnapshot).stale); // Never fabricate freshness.
 }else if(name==='manual_force_once'){
  ctx.reply=async()=>response(120);await ctx.scan(true);
  assert.equal(requests.length,1);assert(requests[0].url.includes('force=true'));
  assert(ctx.entryFreshness(ctx.globalEntrySnapshot).stale);
 }else if(name==='single_flight_and_latest_filters'){
  let release;ctx.reply=()=>new Promise(r=>release=r);
  const pending=ctx.scan(false);await ctx.scan(false,true);assert.equal(requests.length,1);
  nodes.q.value='BTC';nodes.stage.value='NEAR';await ctx.scan(true);await ctx.scan(false);
  ctx.reply=async()=>response(0,'BTCUSDC');release(response(0,'OLDUSDC'));await pending;await settle();
  assert.equal(requests.length,2);assert(requests[1].url.includes('force=true'));assert(requests[1].url.includes('stage=NEAR'));assert(requests[1].url.includes('q=BTC'));
  assert.equal(renders.length,1);assert.equal(ctx.rows[0].ticker,'BTCUSDC');assert.equal(nodes.q.value,'BTC');
 }else if(name==='timeout_and_next_poll_recover'){
  ctx.startAutoScan();ctx.reply=(url,opt)=>new Promise((resolve,reject)=>opt.signal.addEventListener('abort',()=>reject(new Error('timeout'))));
  const pending=ctx.scan(false,true);const timeout=[...timers.values()].find(t=>t.delay===20000);assert(timeout);timeout.fn();await pending;
  assert.equal(ctx.entryHeartbeatBusy,false);assert.equal(timers.size,0);assert.equal(ctx.aged,1);
  ctx.reply=async()=>response();[...intervals.values()][0].fn();await settle();assert.equal(ctx.rows[0].ticker,'NEWUSDC');
 }else if(name==='toggle_and_candidate_reason'){
  ctx.startAutoScan();nodes.autoScan.onclick.call(nodes.autoScan);assert.equal(intervals.size,0);assert.equal(ctx.autoScanTimer,null);
  nodes.autoScan.onclick.call(nodes.autoScan);await settle();assert.equal(intervals.size,1);assert.equal(requests.length,1);
  ctx.renderNearCandidates({...response(),_client_rendered_ms:1000000});
  assert(nodes.nearCandidatesStatus.textContent.includes('31件'));assert(nodes.nearCandidatesStatus.textContent.includes('RR条件を満たす候補 0件'));
  advance(120000);ctx.renderNearCandidates(ctx.globalEntrySnapshot);assert(nodes.nearCandidates.innerHTML.includes('データ鮮度を確認'));
  assert(!nodes.nearCandidatesStatus.textContent.includes('31件'));
 }else throw new Error(name);
}
(async()=>{for(const name of process.argv[3]?[process.argv[3]]:['default_poll','hidden_and_resume','restored_page','old_cache_refresh','manual_force_once','single_flight_and_latest_filters','timeout_and_next_poll_recover','toggle_and_candidate_reason']){await test(name);console.log('Market refresh: '+name+' OK')}})().catch(e=>{console.error(e);process.exitCode=1});
