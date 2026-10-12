// Reference-only sizing UI: shipped functions, synthetic values, no account or order access.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];new Function(script);
let clock=1800000000000,id=0,calls=[],focused=null;const timers=new Map(),nodes={};
for(const key of ['capitalSource','equity','riskPct','entry','stop','target','step','minOrder','maxOrder','lev','calc','riskStatus','riskOut','quickRiskOut','capitalStatus','capitalRefresh'])nodes[key]={value:'',innerHTML:'',textContent:'',attrs:{},handlers:{},setAttribute(k,v){this.attrs[k]=v},removeAttribute(k){delete this.attrs[k]},focus(){focused=key},addEventListener(k,fn){this.handlers[k]=fn}};
Object.entries({capitalSource:'MANUAL',equity:'100',riskPct:'3',entry:'100',stop:'90',target:'120'}).forEach(([k,v])=>nodes[k].value=v);
const response=()=>({side:'LONG',risk_budget:3,theoretical_size:.3,size:.3,notional:30,max_loss:3,actual_risk_pct:3,rr:2,target_profit:6,min_order_size:null,below_min_order:false,margin_capped:false});
const ctx={Date:class extends Date{static now(){return clock}},Number,Math,JSON,Promise,AbortController,Object,document:{visibilityState:'visible',getElementById:k=>nodes[k],querySelectorAll:()=>[]},setTimeout(fn,ms){const key=++id;timers.set(key,{fn,ms});return key},clearTimeout:k=>timers.delete(k),setInterval(){},loadAccountView(){},accountViewEpoch:0,accountViewSession:null,accountViewSnapshot:null,lastAnalysis:null,quickRiskVersion:0,entryRenderVersion:0,lastMarketPayload:null,globalEntrySnapshot:null,accountFresh:()=>false,renderReadyActionCard(){},renderEntryNow(){},fmt:v=>String(v),pct:v=>String(v),card:(k,v)=>k+':'+v+';',lifecycleEscape:String,directionJa:String,accountMoney:String,
 jf:async(url,opt)=>{calls.push({url,opt});return ctx.reply(url,opt)},reply:async()=>response()};
vm.createContext(ctx);vm.runInContext(script.slice(script.indexOf('// Account capital and derived'),script.indexOf('function applyDeepLink(')),ctx);vm.runInContext(script.slice(script.indexOf("document.getElementById('calc').onclick="),script.indexOf('// Deliberate new-entry authority')),ctx);
const tick=async()=>{for(let i=0;i<4;i++)await new Promise(setImmediate)},defer=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}},calculate=()=>nodes.calc.onclick(),caseName=process.argv[3];
(async()=>{
 if(caseName==='required_fields'){
  for(const [field,value] of [['entry',''],['stop',''],['riskPct',''],['equity',''],['entry','-1'],['stop','NaN'],['equity','Infinity']]){const old=nodes[field].value;nodes[field].value=value;await calculate();assert.equal(calls.length,0);assert.equal(focused,field);assert.equal(nodes[field].attrs['aria-invalid'],'true');assert.equal(nodes.riskOut.innerHTML,'');nodes[field].value=old}
 }else if(caseName==='risk_cap'){
  nodes.riskPct.value='3.01';await calculate();assert.equal(calls.length,0);assert(nodes.riskStatus.textContent.includes('3%以下'));nodes.riskPct.value='0';await calculate();assert.equal(calls.length,0);nodes.riskPct.value='.5';ctx.reply=async()=>({...response(),risk_budget:.5,size:.05,notional:5,max_loss:.5,actual_risk_pct:.5});await calculate();assert.equal(JSON.parse(calls[0].opt.body).risk_pct,.5);
 }else if(caseName==='long_short_targets'){
  nodes.target.value='100';await calculate();assert.equal(focused,'target');assert(nodes.riskStatus.textContent.includes('より上'));assert.equal(calls.length,0);nodes.stop.value='110';nodes.target.value='120';await calculate();assert(nodes.riskStatus.textContent.includes('より下'));assert.equal(calls.length,0);nodes.target.value='80';ctx.reply=async()=>({...response(),side:'SHORT'});await calculate();assert(nodes.riskStatus.innerHTML.includes('SHORT'));
 }else if(caseName==='equal_stop'){
  nodes.stop.value='100';await calculate();assert.equal(calls.length,0);assert.equal(focused,'stop');assert(nodes.riskStatus.textContent.includes('異なる値'));
 }else if(caseName==='optional_fields'){
  for(const field of ['target','step','minOrder','maxOrder','lev']){nodes[field].value='-1';await calculate();assert.equal(calls.length,0);assert.equal(focused,field);nodes[field].value=''}await calculate();const p=JSON.parse(calls[0].opt.body);assert(!('target' in p));assert(!('leverage' in p));assert(!('min_order_size' in p));
 }else if(caseName==='order_limits'){
  nodes.minOrder.value='2';nodes.maxOrder.value='1';await calculate();assert.equal(calls.length,0);assert.equal(focused,'maxOrder');assert(nodes.riskStatus.textContent.includes('最小注文量以上'));nodes.maxOrder.value='2';await calculate();assert.equal(calls.length,1);
 }else if(caseName==='enter_and_composition'){
  let prevented=0;nodes.entry.handlers.keydown({key:'Enter',isComposing:true,preventDefault(){prevented++}});assert.equal(calls.length,0);nodes.entry.handlers.keydown({key:'Enter',isComposing:false,preventDefault(){prevented++}});await tick();assert.equal(calls.length,1);assert.equal(prevented,1);assert(nodes.riskStatus.innerHTML.includes('READY判定・注文送信ではありません'));
 }else if(caseName==='same_input_single_flight'){
  const d=defer();ctx.reply=()=>d.promise;const first=calculate();await calculate();assert.equal(calls.length,1);assert.equal(nodes.calc.attrs['aria-busy'],'true');d.resolve(response());await first;assert.equal(nodes.calc.attrs['aria-busy'],'false');assert.equal(ctx.riskCalculationPending,null);
 }else if(caseName==='input_change_discards_reply'){
  const d=defer();ctx.reply=()=>d.promise;const first=calculate();nodes.entry.value='101';nodes.entry.handlers.input();assert.equal(nodes.calc.attrs['aria-busy'],'false');d.resolve(response());await first;assert.equal(nodes.riskOut.innerHTML,'');assert(nodes.riskStatus.textContent.includes('再計算'));
 }else if(caseName==='programmatic_change_discards_reply'){
  const d=defer();ctx.reply=()=>d.promise;const first=calculate();nodes.target.value='121';d.resolve(response());await first;assert.equal(nodes.riskOut.innerHTML,'');assert(nodes.riskStatus.textContent.includes('入力が変わりました'));
 }else if(caseName==='old_finally_keeps_new_busy'){
  const a=defer(),b=defer();ctx.reply=()=>a.promise;const first=calculate();nodes.entry.value='101';nodes.entry.handlers.input();ctx.reply=()=>b.promise;const next=calculate();a.resolve(response());await first;assert.equal(nodes.calc.attrs['aria-busy'],'true');assert.equal(nodes.riskOut.innerHTML,'');b.resolve(response());await next;assert.equal(nodes.calc.attrs['aria-busy'],'false');assert(nodes.riskOut.innerHTML.includes('参考数量'));
 }else if(caseName==='timeout_retry_late_reply'){
  const d=defer();ctx.reply=()=>d.promise;const first=calculate();const timeout=[...timers.values()].find(t=>t.ms===20000);assert(timeout);timeout.fn();await first;assert(calls[0].opt.signal.aborted);assert(nodes.riskStatus.textContent.includes('時間がかか'));assert.equal(nodes.calc.attrs['aria-busy'],'false');assert.equal(nodes.riskOut.innerHTML,'');ctx.reply=async()=>response();await calculate();const good=nodes.riskOut.innerHTML;d.resolve({...response(),size:999});await tick();assert.equal(nodes.riskOut.innerHTML,good);assert.equal(timers.size,0);
 }else if(caseName==='failed_request_retry'){
  ctx.reply=async()=>{throw Error('Fixture failure')};await calculate();assert.equal(nodes.riskOut.innerHTML,'');assert.equal(nodes.calc.attrs['aria-busy'],'false');ctx.reply=async()=>response();await calculate();assert(nodes.riskOut.innerHTML.includes('参考数量'));assert.equal(timers.size,0);
 }else if(caseName==='invalid_results'){
  for(const bad of [null,{}, {...response(),size:null},{...response(),size:'Infinity'},{...response(),max_loss:4},{...response(),actual_risk_pct:4},{...response(),rr:'NaN'},{...response(),size:true}]){ctx.reply=async()=>bad;await calculate();assert.equal(nodes.riskOut.innerHTML,'');assert(nodes.riskStatus.textContent.includes('計算結果を確認できません'));assert.equal(nodes.calc.attrs['aria-busy'],'false')}
 }else if(caseName==='source_or_capital_change'){
  const d=defer();ctx.reply=()=>d.promise;const first=calculate();ctx.clearRiskDerived();d.resolve(response());await first;assert.equal(nodes.riskOut.innerHTML,'');assert.equal(nodes.calc.attrs['aria-busy'],'false');assert(!script.slice(script.indexOf('// Account capital and derived'),script.indexOf('function applyDeepLink(')).includes('setItem('));
 }else if(caseName==='clear_analysis_invalidates_calculation'){
  const d=defer();ctx.reply=()=>d.promise;const first=calculate();ctx.clearAnalysisCharts=()=>{};ctx.analysisExpiryTimer=null;ctx.syncAnalysisWatch=()=>{};['analysisOut','actionOut','readyActionCard','analysisIdentity','entryCriteria','copyPlan','sharePlan','chart4h','chart15m'].forEach(k=>nodes[k]={innerHTML:'',value:'',getContext:()=>null});
  vm.runInContext(script.slice(script.indexOf('function clearAnalysisView('),script.indexOf('function refreshAnalysisAge(')),ctx);ctx.clearAnalysisView();d.resolve(response());await first;assert.equal(nodes.riskOut.innerHTML,'');assert.equal(nodes.entry.value,'');assert.equal(nodes.calc.attrs['aria-busy'],'false');
 }else if(caseName==='decimal_strings_and_negative_net_target'){
  ctx.reply=async()=>({...response(),risk_budget:'3',size:'0.3',notional:'30',max_loss:'3',actual_risk_pct:'3',target_profit:'-0.01',rr:'2',cost_adjusted_rr:'-0.01'});await calculate();assert(nodes.riskOut.innerHTML.includes('-0.01'));assert(nodes.riskStatus.innerHTML.includes('手動の仮定資金'));assert.equal(JSON.parse(calls[0].opt.body).equity,100);
 }else if(caseName==='error_cleared_after_edit'){
  nodes.stop.value='100';await calculate();assert.equal(nodes.stop.attrs['aria-invalid'],'true');nodes.stop.value='90';nodes.stop.handlers.input();assert.equal(nodes.stop.attrs['aria-invalid'],undefined);await calculate();assert(nodes.riskOut.innerHTML.includes('参考数量'));assert.equal(nodes.riskStatus.attrs['aria-invalid'],undefined);
 }else throw Error(caseName);
 assert(calls.every(r=>r.url==='/api/risk'&&r.opt.method==='POST'&&!r.opt.headers.Authorization));assert.equal((html.match(/<button\b[^>]*\bdata-tab="/g)||[]).length,4);console.log(caseName+': OK');
})().catch(e=>{console.error(e);process.exitCode=1});
