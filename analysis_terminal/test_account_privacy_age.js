// Exercise shipped private-view code with stalled fetch/body and scoped fake keys.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];new Function(script);
const source=script.slice(script.indexOf('// Private account values'),script.indexOf('async function checkApiConnections('));
let now=100000,id=0,timers=new Map(),requests=[],handler=null;
const nodes={};for(const name of ['accountOrdersCheck','accountOrdersStatus','accountOrdersData','accountViewData','accountViewStatus','accountViewBalances','accountViewPositions','accountViewTrades','accountViewCash','accountViewTradesMore','accountViewCashMore','accountViewOpen','accountViewLock','api'])nodes[name]={hidden:true,innerHTML:'',textContent:'',classList:{contains:()=>true},querySelector:()=>null};
const history={items:[],complete:false,next_cursor:'c'.repeat(32),observed_ms:100000,window_start_ms:1,window_end_ms:99999};
const asset={source:'EDGEX_TRADING_ACCOUNT',read_only:true,positions_scope:'ALL_ACCOUNT_POSITIONS',snapshot_age_seconds:0,observed_ms:now,balance:{equity_usdc:'100',available_usdc:'90'},positions:[],histories:{positions:history,collateral:history}};
const orders={source:'EDGEX_ACTIVE_CONDITIONAL_ORDERS',read_only:true,complete:true,snapshot_age_seconds:0,observed_ms:now,items:[]};
const clone=v=>JSON.parse(JSON.stringify(v));
function response(url){return {ok:true,json:async()=>url==='/api/account/session'?{view_token:'a'.repeat(43),read_only:true,expires_in_seconds:600}:url==='/api/account/conditional-orders'?clone(orders):clone(asset)}}
const context={console,AbortController,Date:class extends Date{static now(){return now}},document:{visibilityState:'visible',getElementById:k=>nodes[k],addEventListener(){}},setInterval(){},setTimeout(fn,ms){const n=++id;timers.set(n,{fn,ms});return n},clearTimeout:n=>timers.delete(n),syncAnalysisCapital(){},apiCapitalMode:()=>false,lifecycleEscape:String,card:(k,v)=>k+':'+v,currentPushSubscription:async()=>({toJSON:()=>({endpoint:'synthetic-device-only'})}),fetch:async(url,opt)=>{requests.push({url,opt});return url==='/api/account/logout'?{ok:true}:handler?handler(url,opt):response(url)}};
const pageListeners={},visibilityHandlers=[];context.window={addEventListener:(n,f)=>(pageListeners[n]??=[]).push(f)};context.document.addEventListener=(n,f)=>{if(n==='visibilitychange')visibilityHandlers.push(f)};vm.createContext(context);vm.runInContext(source,context);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
async function settle(){for(let i=0;i<4;i++)await new Promise(setImmediate)}
function timeout(){const t=[...timers.values()].find(t=>t.ms===8000);assert(t);t.fn()}
function session(){context.accountViewSession={token:'a'.repeat(43),expires:now+600000}}
const testcase=process.argv[3];
let capital=null;context.syncAnalysisCapital=value=>{capital=value};
function snapshot(age=0){return {...clone(asset),snapshot_age_seconds:age,_received_ms:now}}
function seed(age=0){session();context.accountViewSnapshot=snapshot(age);context.renderAccountPositions(context.accountViewSnapshot);context.refreshAccountPrivacyAge()}
function fireExpiry(){const id=context.accountPrivateExpiryTimer,t=timers.get(id);assert(t);timers.delete(id);t.fn()}
function privateHidden(){assert.equal(context.accountViewSnapshot,null);assert.equal(nodes.accountViewBalances.innerHTML,'');assert.equal(nodes.accountViewPositions.innerHTML,'');assert.equal(capital,null)}
(async()=>{
 if(testcase==='exact_boundary'){
  seed();assert.equal(timers.get(context.accountPrivateExpiryTimer).ms,30000);now+=29999;context.refreshAccountPrivacyAge();assert(context.accountViewSnapshot);assert.equal(timers.get(context.accountPrivateExpiryTimer).ms,1);now++;fireExpiry();privateHidden();assert(context.accountViewSession);
 }else if(testcase==='server_age_remaining'){
  seed(29.5);assert.equal(timers.get(context.accountPrivateExpiryTimer).ms,500);now+=500;fireExpiry();privateHidden();
 }else if(testcase==='independent_orders_expiry'){
  seed();context.accountOrdersSnapshot={...clone(orders),snapshot_age_seconds:20,_received_ms:now};context.renderAccountOrders(context.accountOrdersSnapshot);context.refreshAccountPrivacyAge();assert.equal(timers.get(context.accountPrivateExpiryTimer).ms,10000);
  now+=10000;fireExpiry();assert(context.accountViewSnapshot);assert.equal(context.accountOrdersSnapshot,null);assert.equal(nodes.accountOrdersData.innerHTML,'');assert.equal(timers.get(context.accountPrivateExpiryTimer).ms,20000);
 }else if(testcase==='session_deadline_clears_history'){
  seed();context.accountViewSession.expires=now+123;context.accountViewHistory.positions=clone(history);nodes.accountViewTrades.innerHTML='synthetic private history';context.refreshAccountPrivacyAge();assert.equal(timers.get(context.accountPrivateExpiryTimer).ms,123);now+=123;fireExpiry();privateHidden();assert.equal(context.accountViewSession,null);assert.equal(nodes.accountViewTrades.innerHTML,'');assert.equal(context.accountPrivateExpiryTimer,null);
 }else if(testcase==='lock_cancels_expiry'){
  seed();const id=context.accountPrivateExpiryTimer;context.lockAccountView();assert(!timers.has(id));assert.equal(context.accountPrivateExpiryTimer,null);privateHidden();
 }else if(testcase==='new_snapshot_reschedules'){
  seed();const old=context.accountPrivateExpiryTimer;now+=5000;context.accountViewSnapshot=snapshot();context.refreshAccountPrivacyAge();assert(!timers.has(old));assert.equal(timers.get(context.accountPrivateExpiryTimer).ms,30000);
 }else if(testcase==='invalid_metadata_not_retained'){
  for(const age of [null,undefined,-1,NaN,Infinity,'0']){seed();context.accountViewSnapshot.snapshot_age_seconds=age;context.refreshAccountPrivacyAge();privateHidden()}
 }else if(testcase==='delayed_expiry_never_refreshes_age'){
  seed();now+=45000;fireExpiry();privateHidden();assert(context.accountViewSession);
 }else if(testcase==='clock_reverse_locks'){
  seed();now--;context.refreshAccountPrivacyAge();privateHidden();assert.equal(context.accountViewSession,null);assert(nodes.accountViewStatus.textContent.includes('時刻'));
 }else if(testcase==='pagehide_and_cache_restore'){
  seed();nodes.accountViewTrades.innerHTML='synthetic private history';pageListeners.pagehide.forEach(fn=>fn({persisted:true}));privateHidden();assert.equal(nodes.accountViewTrades.innerHTML,'');assert.equal(context.accountViewSession,null);pageListeners.pageshow.forEach(fn=>fn({persisted:true}));assert.equal(context.accountPrivateExpiryTimer,null);
 }else if(testcase==='visible_resume_checks_expiry'){
  seed();now+=30000;visibilityHandlers.forEach(fn=>fn());privateHidden();
 }else if(testcase==='expiry_cancels_inflight_read'){
  seed();const gate=deferred();handler=()=>gate.promise;context.accountViewSession.expires=now+1000;context.refreshAccountPrivacyAge();const pending=context.refreshAccountPositions();await settle();now+=1000;fireExpiry();await pending;privateHidden();assert.equal(context.accountViewBusy,false);assert.equal(context.accountReadTickets.size,0);gate.resolve(response('/api/account/positions'));await settle();privateHidden();
 }else if(testcase==='initial_load_arms_timer'){
  session();await context.loadAccountView();assert(context.accountViewSnapshot);assert(context.accountPrivateExpiryTimer);assert.equal(timers.get(context.accountPrivateExpiryTimer).ms,30000);
 }else if(testcase==='clock_reverse_during_load_no_history_leak'){
  seed();now--;await context.loadAccountView();privateHidden();assert.equal(context.accountViewSession,null);assert.equal(Object.keys(context.accountViewHistory).length,0);assert.equal(nodes.accountViewTrades.innerHTML,'');
 }else if(testcase==='order_load_arms_timer'){
  session();await context.checkAccountOrders();assert(context.accountOrdersSnapshot);assert(context.accountPrivateExpiryTimer);assert.equal(timers.get(context.accountPrivateExpiryTimer).ms,30000);
 }else if(testcase==='zero_polling_when_locked'){
  context.refreshAccountPrivacyAge();assert.equal(context.accountPrivateExpiryTimer,null);assert.equal(timers.size,0);assert.equal(requests.length,0);
 }else throw Error(testcase);
 assert.equal(context.accountReadTickets.size,0);assert(!requests.some(r=>/createOrder|cancelOrder|\/arm|\/pause|withdraw/.test(r.url)));console.log(testcase+': OK');
})().catch(e=>{console.error(e);process.exitCode=1});
