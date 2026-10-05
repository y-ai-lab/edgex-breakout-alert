// Exercise the complete shipped BTC script, not a separate implementation.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const nodes={},timers=new Map(),requests=[];let clock=2_000_000,timerId=0,fail=false,invalid=false,held=null,chartCalls=0;
function node(id){const classes=new Set(),listeners={};return {id,dataset:{},innerHTML:'',textContent:'',disabled:false,clientWidth:600,clientHeight:300,listeners,
 get className(){return [...classes].join(' ')},set className(s){classes.clear();String(s).split(/\s+/).forEach(x=>classes.add(x))},
 classList:{add:x=>classes.add(x),contains:x=>classes.has(x),toggle(x,on){on?classes.add(x):classes.delete(x)}},
 addEventListener:(event,fn)=>listeners[event]=fn,getBoundingClientRect:()=>({left:10}),
 getContext:()=>new Proxy({},{get:()=>()=>chartCalls++,set:()=>true})};}
for(const m of html.matchAll(/id="([^"]+)"/g))nodes[m[1]]=node(m[1]);
const buttons=['HOUR_4','HOUR_1','MINUTE_15','MINUTE_5'].map(interval=>{const n=node(interval);n.dataset.interval=interval;return n;});
const document={hidden:false,getElementById:id=>nodes[id],querySelectorAll:()=>buttons,addEventListener(event,fn){this[event]=fn;}};
const intervals=['HOUR_4','HOUR_1','MINUTE_15','MINUTE_5'];
function response(){
 const frames=Object.fromEntries(intervals.map(interval=>[interval,{interval,trend:'UP',structure:'UP',close:100,closed_ms:clock-5000,resistance:{price:103},support:{price:99},pivots:[],series:Array.from({length:60},(_,i)=>({time_ms:clock-(60-i)*300000,open:100,high:102,low:98,close:101}))}]));
 const p={mode:'BREAKOUT',side:'LONG',state:'TRIGGERED_SHADOW',trigger:100,entry:100.1,stop:98.9,target:103,rr:2.416,entry_basis:'SIGNAL_CLOSE_REFERENCE',higher_trend:'DOWN',reasons:[],signal:{created_ms:clock-5000+1,detection_lag_ms:5000}};
 return {ticker:'BTCUSDC',mode:'SHADOW_ONLY',real_orders_enabled:false,changes_live_rules:false,automatic_promotion:false,eligible_for_live_promotion:false,now_ms:clock,snapshot_age_seconds:0,stale:false,latest:{status:'OBSERVING',mode:'SHADOW_ONLY',real_orders_enabled:false,observed_ms:clock,frames,plans:[p],errors:{}},metrics:{signals:1,resolved:0,open:1,sample_status:'INSUFFICIENT SAMPLE'},groups:{},signals:[],recorded_observations:1};
}
let lastResponse=response();
const context={document,console,AbortController,Date:class extends Date{static now(){return clock}},
 setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay,interval:false});return id;},clearTimeout:id=>timers.delete(id),
 setInterval(fn,delay){const id=++timerId;timers.set(id,{fn,delay,interval:true});return id;},
 async fetch(url,options){requests.push({url,options});if(held&&url==='/api/btc-wave'){const pending=held;held=null;await pending.promise;return {ok:true,json:async()=>pending.data};}
  if(fail)throw Error('unavailable');
  const data=url==='/api/btc-wave'?(lastResponse=response()):{snapshot_age_seconds:0,ready_candidates:[]};
  if(invalid&&url==='/api/btc-wave')data.real_orders_enabled=true;
  return {ok:true,json:async()=>data};}
};
context.window={devicePixelRatio:1,addEventListener(){}};vm.createContext(context);vm.runInContext(script,context);
async function settle(){for(let i=0;i<12;i++)await new Promise(setImmediate);}
function tick(){[...timers.values()].filter(t=>t.interval&&t.delay===1000).forEach(t=>t.fn());}
(async()=>{
 await settle();assert(nodes.waveStatus.textContent.includes('検証条件成立 1'));assert(nodes.plans.innerHTML.includes('triggered'));assert(!nodes.plans.innerHTML.includes('エントリー可能'));
 assert(nodes.frames.innerHTML.includes('4H'));assert(nodes.researchStatus.textContent.includes('INSUFFICIENT SAMPLE'));assert(nodes.productionEntry.textContent.includes('現在READYなし'));assert(chartCalls>0);
 assert(requests.every(x=>x.options.cache==='no-store'));assert(requests.every(x=>x.url==='/api/btc-wave'||x.url==='/api/screener?limit=1'));
 const before=chartCalls;buttons[1].onclick();assert(chartCalls>before);
 nodes.waveChart.listeners.pointermove({clientX:200});assert(nodes.chartTooltip.textContent.includes('O '));
 clock+=114999;tick();assert(nodes.plans.innerHTML.includes('triggered'));clock++;tick();assert(!nodes.plans.innerHTML.includes('class="plan triggered"'));assert(nodes.waveStatus.textContent.includes('次の波'));
 clock+=5000;tick();assert(nodes.waveStatus.textContent.includes('データ鮮度'));assert(nodes.productionEntry.textContent.includes('データ鮮度'));assert(!nodes.plans.innerHTML.includes('class="plan triggered"'));
 await context.loadBTC();assert(nodes.waveStatus.textContent.includes('検証条件成立'));
 for(const bad of [{...lastResponse,snapshot_age_seconds:-1},{...lastResponse,stale:true},{...lastResponse,latest:{...lastResponse.latest,status:'DATA_WAIT'}},{...lastResponse,latest:{...lastResponse.latest,plans:null}}]){
  context.renderBTC(bad);assert(!nodes.plans.innerHTML.includes('class="plan triggered"'));
 }
 context.renderBTC(lastResponse,-1);assert(nodes.waveStatus.textContent.includes('データ鮮度'));
 context.renderProduction({snapshot_age_seconds:0,ready_candidates:[{ticker:'ETHUSDC',stage:'READY'}]});assert(nodes.productionEntry.textContent.includes('本番ENTRY'));assert(nodes.productionEntry.textContent.includes('ETHUSDC'));
 context.renderProduction({snapshot_age_seconds:0,ready_candidates:[{ticker:'BTCUSDC',stage:'READY'}]},120000);assert(nodes.productionEntry.textContent.includes('データ鮮度'));
 // Escape record text; arbitrary returned metadata never becomes HTML.
 const malicious=structuredClone(lastResponse);malicious.signals=[{mode:'RANGE',side:'LONG',result:{status:'<img src=x onerror=bad()>'}}];context.renderBTC(malicious);assert(!nodes.signals.innerHTML.includes('<img'));assert(nodes.signals.innerHTML.includes('&lt;img'));
 fail=true;await context.loadBTC();await context.loadProduction();assert(nodes.waveMeta.textContent.includes('取得失敗'));assert(!nodes.plans.innerHTML.includes('class="plan triggered"'));assert(nodes.productionEntry.textContent.includes('データ鮮度'));assert(!nodes.refresh.disabled);fail=false;
 invalid=true;await context.loadBTC();assert(nodes.waveMeta.textContent.includes('取得失敗'));invalid=false;
 let release;held={promise:new Promise(r=>release=r),data:{...response(),snapshot_age_seconds:999}};const old=context.loadBTC();await settle();await context.loadBTC();release();await old;assert(nodes.waveStatus.textContent.includes('検証条件成立'));
 document.hidden=false;document.visibilitychange();await settle();assert(nodes.waveStatus.textContent.includes('検証条件成立'));
 console.log('BTC shipped JS full boot, charts, boundary freshness, delayed signals, failures/races, XSS and production separation: PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
