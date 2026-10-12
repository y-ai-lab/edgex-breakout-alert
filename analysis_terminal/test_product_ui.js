// Exercise shipped request/copy/freshness code with controlled latency and time.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const testcase=process.argv[3];let now=2000000,next=0,calls=[],replies=[],clipboard=[],riskReply=null;
const ids=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]),nodes={},timers=new Map(),intervals=[],frames=[];
ids.forEach(id=>nodes[id]={value:'',innerHTML:'',textContent:'',disabled:false,attrs:{},setAttribute(k,v){this.attrs[k]=v},addEventListener(){},getContext(){return {clearRect(){}}}});
nodes.ticker.value='BTC';
const row=(ticker='BTCUSDC',stage='READY')=>({ticker,stage,direction:'LONG',action:stage==='READY'?'ENTER':'WAIT FOR 15M CLOSE',data_age_seconds:0,entry_reference:100,stop_loss:90,take_profit:125,rr:2.5});
const pack=r=>({analysis:r,series:{HOUR_4:[],MINUTE_15:[]},levels:{}});
const context={console,AbortController,Date:class extends Date{static now(){return now}},document:{visibilityState:'visible',getElementById:id=>nodes[id],addEventListener(){}},
 setInterval:fn=>intervals.push(fn),setTimeout(fn,ms){let id=++next;timers.set(id,{fn,ms});return id},clearTimeout:id=>timers.delete(id),requestAnimationFrame:fn=>frames.push(fn),
 navigator:{clipboard:{writeText:async t=>clipboard.push(t)}},lastAnalysis:null,quickRiskVersion:0,manualCalcVersion:0,
 jf:async(url,opt)=>{calls.push({url,opt});let reply=replies.shift();if(reply instanceof Error)throw reply;if(typeof reply==='function')return reply();return reply},
 addRecentTicker(){},card:(k,v)=>k+':'+v+';',fmt:x=>x==null?'—':String(x),pct:String,lifecycleEscape:String,reasonJa:String,stageJa:String,directionJa:String,trendJa:String,
 renderAnalysisCriteria(){},syncAnalysisWatch(){},
 renderReadyActionCard:r=>nodes.readyActionCard.innerHTML=r?'READY':'',renderEntryBand:()=>'',renderQuickRisk(){},updateUrl(){},drawChart(){},actionClass:String,copyTextValue(){},openRiskCalculator(){},alert:()=>assert.fail('no alert'),location:{href:'https://test/'}}
vm.createContext(context);
vm.runInContext(script.slice(script.indexOf('function planAction('),script.indexOf('function reasonJa(')),context);
vm.runInContext(script.slice(script.indexOf('var analysisRequestVersion='),script.indexOf('document.getElementById("dashScan").onclick=')),context);
const settle=async()=>{await new Promise(setImmediate)},defer=()=>{let resolve;let promise=new Promise(r=>resolve=r);return {resolve,promise}};
(async()=>{
 if(testcase==='out_of_order'){
  const d=defer();replies.push(()=>d.promise,pack(row('ETHUSDC')));let old=context.analyze();await settle();nodes.ticker.value='ETH';let fresh=context.analyze();await fresh;
  d.resolve(pack(row()));await old;assert.equal(context.lastAnalysis.ticker,'ETHUSDC');assert(nodes.analysisOut.innerHTML.includes('100'));assert(!nodes.analyze.disabled);assert(calls[0].opt.signal.aborted);
 }else if(testcase==='input_change'){
  replies.push(pack(row()));await context.analyze();assert(!nodes.copyPlan.disabled);const d=defer();replies.push(()=>d.promise);let old=context.analyze();await settle();nodes.ticker.value='ETH';context.invalidateAnalysisInput();
  assert.equal(context.lastAnalysis,null);assert.equal(nodes.entry.value,'');assert(nodes.copyPlan.disabled);d.resolve(pack(row()));await old;assert.equal(nodes.analysisOut.innerHTML,'');assert(!nodes.analyze.disabled);assert(nodes.analysisStatus.textContent.includes('銘柄が変更'));
 }else if(testcase==='failure_recovery'){
  replies.push(pack(row()));await context.analyze();replies.push(new Error('offline'));await context.analyze();assert.equal(context.lastAnalysis,null);assert.equal(nodes.entry.value,'');assert(nodes.copyPlan.disabled);assert.equal(nodes.quickRiskOut.innerHTML,'');assert(!nodes.analyze.disabled);assert(nodes.analysisStatus.textContent.includes('再試行'));replies.push(pack(row()));await context.analyze();assert(!nodes.copyPlan.disabled);
 }else if(testcase==='invalid_response'){
  for(const r of [{},pack({...row(),data_age_seconds:null}),pack({...row(),data_age_seconds:900}),pack({...row(),data_age_seconds:-1})]){replies.push(r);await context.analyze();assert.equal(context.lastAnalysis,null);assert(nodes.copyPlan.disabled);assert(!nodes.analyze.disabled)}
 }else if(testcase==='expiry_copy'){
  replies.push(pack(row()));await context.analyze();await nodes.copyPlan.onclick();assert.equal(clipboard.length,1);now+=119999;assert(context.analysisFresh(context.lastAnalysis));now++;await nodes.copyPlan.onclick();assert.equal(clipboard.length,1);assert.equal(context.lastAnalysis,null);assert.equal(nodes.entry.value,'');assert.equal(nodes.readyActionCard.innerHTML,'');assert(nodes.copyPlan.disabled);assert(nodes.analysisStatus.textContent.includes('鮮度'));
 }else if(testcase==='candle_age'){
  replies.push(pack({...row(),data_age_seconds:899}));await context.analyze();now+=1000;intervals.forEach(fn=>fn());assert.equal(context.lastAnalysis,null);assert.equal(nodes.analysisOut.innerHTML,'');
 }else if(testcase==='timeout'){
  replies.push(()=>new Promise((resolve,reject)=>{const signal=calls.at(-1).opt.signal;signal.addEventListener('abort',()=>reject(Object.assign(new Error(),{name:'AbortError'})))}));let pending=context.analyze();await settle();[...timers.values()].find(x=>x.ms===30000).fn();await pending;assert(!nodes.analyze.disabled);assert(nodes.analysisStatus.textContent.includes('時間がかか'));assert.equal(context.lastAnalysis,null);
 }else if(testcase==='late_risk'){
  vm.runInContext(script.slice(script.indexOf('var quickRiskVersion='),script.indexOf('function renderEntryBand(')),context);
  context.scenarioRiskParams=()=>({});context.riskEstimateNote=()=>'';context.riskSizingCards=()=>'';
  let d=defer();context.analysisRiskPlan=()=>d.promise;let r={...row(),_client_received_ms:now};context.lastAnalysis=r;let old=context.renderQuickRisk(r);await settle();context.clearAnalysisView();d.resolve({size:1,equity_usdc:100});await old;assert.equal(nodes.quickRiskOut.innerHTML,'');assert.equal(nodes.readyActionCard.innerHTML,'');
 }else if(testcase==='market_context'){
  context.entryFreshness=j=>({stale:j.stale});vm.runInContext(script.slice(script.indexOf('function renderMarketContext('),script.indexOf('function renderMarketDataStatus(')),context);
  context.renderMarketContext({summary:{ready_count:0,qualified_near_count:0,stage_counts:{BREAKOUT_WAIT:100,TREND_WAIT:20,RR_WAIT:3,READY:0}}});assert(nodes.marketContextMessage.textContent.includes('ありません'));assert(nodes.marketContextCounts.innerHTML.includes('100'));assert(!nodes.marketContextCounts.innerHTML.includes('READY'));context.renderMarketContext({stale:true,summary:{stage_counts:{READY:1}}});assert(nodes.marketContext.hidden);assert.equal(nodes.marketContextCounts.innerHTML,'');
  context.renderMarketContext({summary:{ready_count:null,qualified_near_count:0,stage_counts:{READY:1}}});assert(nodes.marketContext.hidden);
 }else throw Error(testcase);
 assert(calls.every(x=>x.url.startsWith('/api/chart?')&&(!x.opt.method||x.opt.method==='GET')));assert(!calls.some(x=>/execution|orders|session/.test(x.url)));
 console.log(testcase+': OK');
})().catch(e=>{console.error(e);process.exitCode=1});
