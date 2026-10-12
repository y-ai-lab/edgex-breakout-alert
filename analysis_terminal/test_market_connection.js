// Exercise actual transport/freshness functions with delayed HTTP and browser events.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];new Function(script);
const kind=process.argv[3];let clock=1000000,timer=0,invalidations=0,analysisInvalidations=0,scans=0;
const timers=new Map(),requests=[],nodes=Object.fromEntries(['marketConnection','marketConnectionText','marketConnectionRetry'].map(id=>[id,{dataset:{},textContent:'',disabled:false}]));
const response=(age=0)=>({snapshot_age_seconds:age,ready_candidates:[],qualified_near_candidates:[],results:[]});
const ctx={AbortController,Number,Math,Date:class extends Date{static now(){return clock}},navigator:{onLine:true},document:{visibilityState:'visible',getElementById:id=>nodes[id]},
 entryHeartbeatBusy:false,marketScanQueued:null,autoScanTimer:null,analysisAbortController:null,
 setTimeout(fn,ms){const k=++timer;timers.set(k,{fn,ms});return k},clearTimeout:k=>timers.delete(k),
 jf:async(url,opt)=>{requests.push({url,opt});return ctx.reply(url,opt)},reply:async()=>response(),refreshGlobalEntryAge(){invalidations++},refreshAnalysisAge(){analysisInvalidations++},
 scan:async()=>scans++,renderGlobalEntryBar:j=>ctx.globalEntrySnapshot=j,renderEntryNow(){}};
vm.createContext(ctx);
vm.runInContext(script.slice(script.indexOf('var marketConnectionEpoch='),script.indexOf('function scenarioRiskParams(')),ctx);
vm.runInContext(script.slice(script.indexOf('function entryFreshness('),script.indexOf('function renderGlobalEntryBar(')),ctx);
vm.runInContext(script.slice(script.indexOf('function analysisFresh('),script.indexOf('function clearAnalysisView(')),ctx);
vm.runInContext(script.slice(script.indexOf('async function refreshEntryHeartbeat('),script.indexOf('var marketConnectionEpoch=')),ctx);
const snap=()=>({snapshot_age_seconds:0,_client_rendered_ms:clock,_client_connection_epoch:ctx.marketConnectionEpoch});
const analysis=()=>({_client_received_ms:clock,_client_connection_epoch:ctx.marketConnectionEpoch,data_age_seconds:0});
const settle=async()=>{for(let i=0;i<5;i++)await new Promise(setImmediate)};
(async()=>{
 if(kind==='success_age'){
  const j=await ctx.freshScreenerRequest('/api/screener?limit=3');assert.equal(j._client_connection_epoch,0);assert.equal(nodes.marketConnection.dataset.state,'CONNECTED');assert(nodes.marketConnectionText.textContent.includes('0秒前'));clock+=119999;ctx.renderMarketConnection();assert.equal(nodes.marketConnection.dataset.state,'CONNECTED');clock++;ctx.renderMarketConnection();assert.equal(nodes.marketConnection.dataset.state,'STALE');
 }else if(kind==='failure_suppresses_fresh'){
  await ctx.freshScreenerRequest('/api/screener?limit=3');const old=snap(),r=analysis();assert(!ctx.entryFreshness(old).stale);assert(ctx.analysisFresh(r));ctx.reply=async()=>{throw Error('HTTP 503')};await assert.rejects(ctx.freshScreenerRequest('/api/screener?limit=3'));assert.equal(nodes.marketConnection.dataset.state,'ERROR');assert.equal(ctx.entryFreshness(old).age,0);assert(ctx.entryFreshness(old).stale);assert(!ctx.analysisFresh(r));assert(!nodes.marketConnectionText.textContent.includes('0件'));
 }else if(kind==='offline_no_http'){
  await ctx.freshScreenerRequest('/api/screener?limit=3');const old=snap();ctx.navigator.onLine=false;ctx.handleMarketOffline();assert.equal(nodes.marketConnection.dataset.state,'OFFLINE');assert(nodes.marketConnectionRetry.disabled);assert(ctx.entryFreshness(old).stale);const count=requests.length,epoch=ctx.marketConnectionEpoch;await assert.rejects(ctx.freshScreenerRequest('/api/screener?limit=3'));assert.equal(requests.length,count);assert.equal(ctx.marketConnectionEpoch,epoch);assert(invalidations>0&&analysisInvalidations>0);
 }else if(kind==='late_response_never_restores'){
  let release;ctx.reply=()=>new Promise(r=>release=r);let pending=ctx.freshScreenerRequest('/api/screener?limit=3');ctx.navigator.onLine=false;ctx.handleMarketOffline();assert(requests[0].opt.signal.aborted);ctx.navigator.onLine=true;ctx.document.visibilityState='hidden';ctx.handleMarketOnline();release(response());await assert.rejects(pending,/SUPERSEDED/);assert.equal(ctx.marketConnectionState.status,'RECONNECTING');assert.equal(ctx.marketConnectionState.success_ms,null);assert.equal(requests.length,1);ctx.reply=async()=>response();await ctx.refreshEntryHeartbeat();assert.equal(requests.length,1);ctx.document.visibilityState='visible';await ctx.refreshEntryHeartbeat();assert.equal(requests.length,2);assert.equal(ctx.marketConnectionState.status,'CONNECTED');
 }else if(kind==='old_filtered_snapshot_stays_stale'){
  await ctx.freshScreenerRequest('/api/screener?limit=3');const old=snap();ctx.navigator.onLine=false;ctx.handleMarketOffline();ctx.navigator.onLine=true;ctx.handleMarketOnline();await settle();assert.equal(ctx.marketConnectionState.status,'CONNECTED');assert(!ctx.entryFreshness(ctx.globalEntrySnapshot).stale);assert(ctx.entryFreshness(old).stale);assert.equal(old.snapshot_age_seconds,0);
 }else if(kind==='queued_reconnect_preserves_force'){
  ctx.entryHeartbeatBusy=true;ctx.marketScanQueued=true;ctx.handleMarketOnline();assert.equal(ctx.marketScanQueued,true);assert.equal(requests.length,0);ctx.marketScanQueued=null;ctx.handleMarketOnline();assert.equal(ctx.marketScanQueued,false);assert.equal(requests.length,0);assert.equal(ctx.marketConnectionState.status,'RECONNECTING');
 }else if(kind==='retry_uses_current_filters'){
  ctx.autoScanTimer=1;ctx.handleMarketOnline();await settle();assert.equal(scans,1);assert.equal(requests.length,0);assert.equal(ctx.marketConnectionState.status,'RECONNECTING');
 }else if(kind==='timeout_recovery_and_original_age'){
  ctx.reply=(url,opt)=>new Promise((_,reject)=>opt.signal.addEventListener('abort',()=>reject(Error('timeout'))));const pending=ctx.freshScreenerRequest('/api/screener?limit=3');timers.values().next().value.fn();await assert.rejects(pending);assert.equal(timers.size,0);assert.equal(ctx.marketConnectionState.status,'ERROR');ctx.reply=async()=>response(120);let j=await ctx.freshScreenerRequest('/api/screener?limit=3');assert.equal(j.snapshot_age_seconds,120);assert.equal(nodes.marketConnection.dataset.state,'STALE');assert(ctx.entryFreshness({...j,_client_rendered_ms:clock}).stale);
 }else if(kind==='invalid_age_not_connected'){
  for(const age of [null,-1,Infinity,'0']){ctx.reply=async()=>response(age);await assert.rejects(ctx.freshScreenerRequest('/api/screener?limit=3'));assert.equal(ctx.marketConnectionState.status,'ERROR');assert.equal(ctx.marketConnectionState.success_ms,null)}
 }else if(kind==='bindings_and_scope'){
  const listeners={};ctx.window={addEventListener:(n,fn)=>listeners[n]=fn};const line=script.split('\n').find(x=>x.startsWith("window.addEventListener('offline'"));vm.runInContext(line,ctx);assert.equal(listeners.offline,ctx.handleMarketOffline);assert.equal(listeners.online,ctx.handleMarketOnline);await nodes.marketConnectionRetry.onclick();assert.equal(requests.length,1);assert.equal((html.match(/<button\b[^>]*\bdata-tab="/g)||[]).length,4);
 }else throw Error(kind);
 assert(requests.every(r=>r.url.startsWith('/api/screener')&&(!r.opt.method||r.opt.method==='GET')));console.log(kind+': OK');
})().catch(e=>{console.error(e);process.exitCode=1});
