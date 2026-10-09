// Shipped calculator and account functions, synthetic account and delayed responses.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const privateCode=script.slice(script.indexOf('// Private account values'),script.indexOf('async function checkApiConnections('));
const capitalCode=script.slice(script.indexOf('// Account capital and derived'),script.indexOf('function applyDeepLink('));
const scenarioCode=script.slice(script.indexOf('function scenarioRiskParams('),script.indexOf('function entryMetric('));
const quickCode=script.slice(script.indexOf('var quickRiskVersion='),script.indexOf('function renderEntryBand('));
const calcCode=script.slice(script.indexOf("document.getElementById('calc').onclick="));
let now=100000,requests=[],gate=null,failure=0,intervals=[],listeners={},storage=[];
const nodes={},allIds=[...html.matchAll(/id="([^"]+)"/g)].map(m=>m[1]);
allIds.forEach(id=>nodes[id]={value:'',readOnly:false,hidden:false,innerHTML:'',textContent:'',classList:{contains:()=>true},addEventListener:(type,fn)=>listeners[id+type]=fn});
nodes.capitalSource.value='API';nodes.riskPct.value='3';nodes.entry.value='100';nodes.stop.value='90';nodes.target.value='120';
const result={size:'0.29',max_loss:'2.9',target_profit:'5.8',notional:'29',risk_budget:'3',rr:'2',cost_adjusted_rr:'1.98',actual_risk_pct:'2.9',equity_usdc:'100',available_usdc:'80',capital_source:'EDGEX_USDC_EQUITY',read_only:true,calculation_scope:'ANALYSIS_REFERENCE_ONLY',observed_ms:100000,snapshot_age_seconds:0,fee_bps:'5',slippage_bps:'2',budget_reference_size:'0.29',budget_reference_notional_usdc:'29',budget_reference_loss_usdc:'2.9',budget_reference_within_limits:true,sizing_constraints:['ORDER_STEP_ROUNDING'],unused_risk_budget_usdc:'0.1',risk_budget_used_pct:'96.66'};
const asset={source:'EDGEX_TRADING_ACCOUNT',read_only:true,positions_scope:'ALL_ACCOUNT_POSITIONS',balance:{equity_usdc:'100',available_usdc:'80',cash_usdc:'95'},positions:[],observed_ms:100000,snapshot_age_seconds:0,_received_ms:100000};
const row={stage:'READY',entry_reference:100,stop_loss:90,take_profit:120};
const context={Date:class extends Date{static now(){return now}},Number,Math,JSON,Promise,AbortController,
 document:{visibilityState:'visible',getElementById:id=>nodes[id],querySelectorAll:()=>[],addEventListener:(name,fn)=>{const prior=listeners[name];listeners[name]=prior?()=>{prior();fn()}:fn}},
 setInterval:(fn,ms)=>intervals.push({fn,ms}),setTimeout,clearTimeout,lifecycleEscape:String,card:(k,v)=>k+':'+v+';',fmt:String,pct:String,directionJa:String,
 lastAnalysis:null,globalEntrySnapshot:null,lastMarketPayload:null,entryRenderVersion:0,renderReadyActionCard:(r,x)=>nodes.readyActionCard.innerHTML=x?'PRIVATE_SIZE':'PUBLIC_LEVELS',renderEntryNow:async()=>{},
 localStorage:{removeItem:k=>storage.push(k)},currentPushSubscription:async()=>({toJSON:()=>({keys:{auth:'SYNTHETIC'}})}),
 fetch:async(url,opt)=>{requests.push({url,opt});if(gate){const g=gate;gate=null;await g.promise}return {ok:!failure,status:failure,json:async()=>url==='/api/account/risk'?{...result}:JSON.parse(JSON.stringify(asset))}},
 jf:async(url,opt)=>{requests.push({url,opt});if(url==='/api/account/session')return {read_only:true,view_token:'a'.repeat(43),expires_in_seconds:600};if(gate){const g=gate;gate=null;await g.promise}return {...result,capital_source:undefined}}
};vm.createContext(context);vm.runInContext(privateCode+capitalCode+scenarioCode+quickCode+calcCode,context);
function setup(){context.accountViewSession={token:'a'.repeat(43),expires:now+600000};context.accountViewSnapshot=JSON.parse(JSON.stringify(asset));context.accountViewSnapshot._received_ms=now;context.accountViewSnapshot.observed_ms=100000;context.syncAnalysisCapital(context.accountViewSnapshot)}
async function tick(){await new Promise(setImmediate)}
(async()=>{
 assert(!html.includes('id="equity" type="number" value="1000"'));assert(!capitalCode.includes('setItem('));
 await assert.rejects(context.analysisRiskPlan({entry:100,stop:90}),/API/);assert.equal(requests.length,0);
 setup();assert.equal(nodes.equity.value,'100');assert(nodes.capitalStatus.textContent.includes('80.00'));assert(!nodes.capitalStatus.textContent.includes('95.00'));
 const x=await context.analysisRiskPlan({entry:100,stop:90,risk_pct:3});assert.equal(x.size,'0.29');
 let sent=requests.at(-1);assert.equal(sent.url,'/api/account/risk');assert.equal(sent.opt.headers.Authorization,'Bearer '+'a'.repeat(43));assert.equal(sent.opt.method,'POST');assert(!('equity' in JSON.parse(sent.opt.body)));assert(!('available' in JSON.parse(sent.opt.body)));
 await nodes.calc.onclick();assert(nodes.riskOut.innerHTML.includes('0.29'));assert(nodes.riskStatus.innerHTML.includes('費用込み'));
 assert(nodes.riskOut.innerHTML.includes('損失予算基準の数量'));assert(nodes.riskOut.innerHTML.includes('現行上限内の参考数量'));assert(nodes.riskOut.innerHTML.includes('未使用の損失予算'));assert(nodes.riskOut.innerHTML.includes('現在の数量・資金上限内'));
 const constrained=context.riskSizingCards({...result,budget_reference_size:'3',budget_reference_within_limits:false,sizing_constraints:['NOTIONAL_POLICY_LIMIT','<img src=x onerror=bad()>']});assert(constrained.includes('その数量を提示できません'));assert(constrained.includes('運用設定の建玉額上限'));assert(!constrained.includes('<img'));
 context.lastAnalysis=row;await context.renderQuickRisk(row);assert(nodes.quickRiskOut.innerHTML.includes('0.29'));assert.equal(nodes.readyActionCard.innerHTML,'PRIVATE_SIZE');
 context.lockAccountView();assert.equal(nodes.equity.value,'');assert.equal(nodes.riskOut.innerHTML,'');assert.equal(nodes.quickRiskOut.innerHTML,'');assert.equal(nodes.readyActionCard.innerHTML,'PUBLIC_LEVELS');assert(!nodes.capitalStatus.textContent.includes('80.00'));
 context.lastAnalysis=null;setup();let done;gate={promise:new Promise(r=>done=r)};let pending=nodes.calc.onclick();await tick();context.lockAccountView();done();await pending;assert.equal(nodes.riskOut.innerHTML,'','late calculation cannot undo lock');
 setup();now+=30000;intervals.filter(x=>x.ms===1000).forEach(x=>x.fn());assert.equal(nodes.equity.value,'');await assert.rejects(context.analysisRiskPlan({entry:100,stop:90}),/API/);
 now=100000;setup();context.accountViewSession.expires=now;intervals.filter(x=>x.ms===1000).forEach(x=>x.fn());assert.equal(nodes.equity.value,'');
 setup();gate={promise:new Promise(r=>done=r)};pending=context.analysisRiskPlan({entry:100,stop:90});await tick();nodes.capitalSource.value='MANUAL';context.changeCapitalSource();assert.equal(nodes.equity.value,'');assert.equal(nodes.equity.readOnly,false);done();await assert.rejects(pending,/再確認/);
 nodes.equity.value='500';await context.analysisRiskPlan({entry:100,stop:90});sent=requests.at(-1);assert.equal(sent.url,'/api/risk');assert.equal(JSON.parse(sent.opt.body).equity,500);assert(!sent.opt.headers.Authorization);
 nodes.capitalSource.value='API';context.changeCapitalSource();assert.equal(nodes.equity.readOnly,true);assert.equal(nodes.equity.value,'100');
 failure=403;await assert.rejects(context.analysisRiskPlan({entry:100,stop:90}),/API/);assert.equal(context.accountViewSession,null);assert.equal(nodes.equity.value,'');failure=0;
 setup();failure=503;await assert.rejects(context.analysisRiskPlan({entry:100,stop:90}),/API/);assert.equal(nodes.equity.value,'');failure=0;
 setup();gate={promise:new Promise(r=>done=r)};pending=context.analysisRiskPlan({entry:100,stop:90});await tick();asset.observed_ms=100001;context.accountViewSnapshot=JSON.parse(JSON.stringify(asset));context.accountViewSnapshot._received_ms=now;context.syncAnalysisCapital(context.accountViewSnapshot);done();await assert.rejects(pending,/再確認/);asset.observed_ms=100000;
 setup();context.accountViewSnapshot.balance.available_usdc=null;context.syncAnalysisCapital(context.accountViewSnapshot);assert.equal(nodes.equity.value,'');await assert.rejects(context.analysisRiskPlan({entry:100,stop:90}),/API/);
 setup();context.document.visibilityState='hidden';listeners.visibilitychange();assert.equal(nodes.equity.value,'');assert.equal(context.accountViewSession,null);context.document.visibilityState='visible';
 setup();gate={promise:new Promise(r=>done=r)};pending=nodes.calc.onclick();await tick();listeners.entryinput();done();await pending;assert.equal(nodes.riskOut.innerHTML,'','changed input invalidates pending plan');
 setup();context.lastAnalysis=row;gate={promise:new Promise(r=>done=r)};pending=context.renderQuickRisk(row);await tick();context.lastAnalysis={...row,entry_reference:101};await context.renderQuickRisk(context.lastAnalysis);done();await pending;assert.equal(nodes.readyActionCard.innerHTML,'PRIVATE_SIZE');
 assert(!requests.some(r=>/createOrder|withdraw|cancelOrder/.test(r.url)));assert(!requests.some(r=>/token=|equity=/.test(r.url)));
 console.log('API capital: private sizing, equity vs cash, freshness, caps labels, lock/revocation, source switches and delayed results: OK');
})().catch(e=>{console.error(e);process.exitCode=1});
