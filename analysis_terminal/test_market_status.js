const fs=require('fs'),vm=require('vm'),assert=require('assert');
const script=fs.readFileSync(process.argv[2],'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const nodes=Object.fromEntries(['nearCandidates','nearCandidatesStatus','marketDataDetails','marketDataStatus','marketDataSummary','marketDataMissing'].map(n=>[n,{textContent:'',innerHTML:'',hidden:false,querySelectorAll:()=>[]}]))
let stale=false;
const context={document:{getElementById:n=>nodes[n]},entryFreshness:()=>({stale}),fmt:(n,d)=>n==null?'—':Number(n).toFixed(d),
 lifecycleEscape:s=>String(s).replace(/</g,'&lt;'),directionJa:v=>v,stageJa:v=>v,reasonJa:v=>v,entryMetric:(k,v)=>k+':'+v,Set,Number};
vm.createContext(context);
function load(a,b){vm.runInContext(script.slice(script.indexOf(a),script.indexOf(b,script.indexOf(a))),context)}
load('function renderNearCandidates(','function stageJa(');
load('function renderMarketDataStatus(','function summaryView(');
const near={ticker:'QUALIFIED',setup_id:'a',stage:'CONFIRMATION_WAIT',rr:3,reason:'confirmation wait'};
const blocked={ticker:'BLOCKED',setup_id:'b',stage:'CONFIRMATION_WAIT',rr:.4,entry_band:{status:'NO_OVERLAP'}};
const j={ready_candidates:[],qualified_near_candidates:[near],blocked_setup_candidates:[blocked,near],summary:{near_signal_count:2,qualified_near_count:1},scan_health:{checked:2,universe:2,analysis_ready:1,data_wait_count:1,required_closed_candles:{HOUR_4:50,MINUTE_15:15},data_wait:[{ticker:'SHORT<',reasons:['INSUFFICIENT_HISTORY'],closed_candles:{HOUR_4:12,MINUTE_15:180}}]}};
const before=JSON.stringify(j);context.renderNearCandidates(j);context.renderMarketDataStatus(j);
assert(nodes.nearCandidates.innerHTML.indexOf('QUALIFIED')<nodes.nearCandidates.innerHTML.indexOf('BLOCKED'));
assert.equal((nodes.nearCandidates.innerHTML.match(/data-ticker="QUALIFIED"/g)||[]).length,1);
assert(nodes.nearCandidates.innerHTML.includes('監視のみ・条件不足'));assert(nodes.nearCandidates.innerHTML.includes('エントリー不可'));
assert(nodes.nearCandidates.innerHTML.includes('確認水準とRR条件を両立できません'));
assert(nodes.marketDataSummary.textContent.includes('確認 2 / 2'));assert(nodes.marketDataStatus.textContent.includes('指標評価可能 1 / 2'));
assert(nodes.marketDataMissing.innerHTML.includes('4H 12 / 50本'));assert(nodes.marketDataMissing.innerHTML.includes('SHORT&lt;'));
assert.equal(JSON.stringify(j),before);assert.equal(j.ready_candidates.length,0);
stale=true;context.renderNearCandidates(j);context.renderMarketDataStatus(j);
assert(!nodes.nearCandidates.innerHTML.includes('BLOCKED'));assert.equal(nodes.marketDataMissing.innerHTML,'');assert(nodes.marketDataSummary.textContent.includes('鮮度を確認'));
stale=false;context.renderMarketDataStatus({});assert(nodes.marketDataDetails.hidden);
console.log('Market availability and watch-only display: OK');
