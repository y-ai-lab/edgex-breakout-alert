// Exercise shipped private-view code with stalled fetch/body and scoped fake keys.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const source=script.slice(script.indexOf('// Private account values'),script.indexOf('async function checkApiConnections('));
let now=100000,id=0,timers=new Map(),requests=[],handler=null;
const nodes={};for(const name of ['accountOrdersCheck','accountOrdersStatus','accountOrdersData','accountViewData','accountViewStatus','accountViewBalances','accountViewPositions','accountViewTrades','accountViewCash','accountViewTradesMore','accountViewCashMore','accountViewOpen','accountViewLock','api'])nodes[name]={hidden:true,innerHTML:'',textContent:'',classList:{contains:()=>true},querySelector:()=>null};
const history={items:[],complete:false,next_cursor:'synthetic-cursor',window_start_ms:1,window_end_ms:99999};
const asset={source:'EDGEX_TRADING_ACCOUNT',read_only:true,positions_scope:'ALL_ACCOUNT_POSITIONS',snapshot_age_seconds:0,observed_ms:now,balance:{equity_usdc:'100',available_usdc:'90'},positions:[],histories:{positions:history,collateral:history}};
const orders={source:'EDGEX_ACTIVE_CONDITIONAL_ORDERS',read_only:true,complete:true,snapshot_age_seconds:0,observed_ms:now,items:[]};
const clone=v=>JSON.parse(JSON.stringify(v));
function response(url){return {ok:true,json:async()=>url==='/api/account/session'?{view_token:'a'.repeat(43),read_only:true,expires_in_seconds:600}:url==='/api/account/conditional-orders'?clone(orders):clone(asset)}}
const context={console,AbortController,Date:class extends Date{static now(){return now}},document:{visibilityState:'visible',getElementById:k=>nodes[k],addEventListener(){}},setInterval(){},setTimeout(fn,ms){const n=++id;timers.set(n,{fn,ms});return n},clearTimeout:n=>timers.delete(n),syncAnalysisCapital(){},apiCapitalMode:()=>false,lifecycleEscape:String,card:(k,v)=>k+':'+v,currentPushSubscription:async()=>({toJSON:()=>({endpoint:'synthetic-device-only'})}),fetch:async(url,opt)=>{requests.push({url,opt});return url==='/api/account/logout'?{ok:true}:handler?handler(url,opt):response(url)}};
vm.createContext(context);vm.runInContext(source,context);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
async function settle(){for(let i=0;i<4;i++)await new Promise(setImmediate)}
function timeout(){const t=[...timers.values()].find(t=>t.ms===8000);assert(t);t.fn()}
function session(){context.accountViewSession={token:'a'.repeat(43),expires:now+600000}}
const testcase=process.argv[3];
(async()=>{
 if(testcase==='fetch_deadline'||testcase==='body_deadline'||testcase==='session_deadline'){
  const gate=deferred();if(testcase!=='session_deadline')session();
  handler=async url=>testcase==='body_deadline'?{ok:true,json:()=>gate.promise}:gate.promise;
  const pending=context.loadAccountView();await settle();timeout();await pending;
  assert.equal(context.accountViewBusy,false);assert.equal(context.accountReadTickets.size,0);assert(nodes.accountViewStatus.textContent.includes('8秒'));assert.equal(context.accountViewSnapshot,null);
  handler=null;await context.loadAccountView();assert(!nodes.accountViewData.hidden);const current=context.accountViewSnapshot;
  gate.resolve(testcase==='body_deadline'?clone(asset):response('/api/account/overview'));await settle();assert.strictEqual(context.accountViewSnapshot,current);
 }else if(testcase==='delayed_timer_boundary'||testcase==='clock_reverse'){
  session();const gate=deferred();handler=()=>gate.promise;const pending=context.loadAccountView();await settle();now+=testcase==='clock_reverse'?-1:8000;
  gate.resolve(response('/api/account/overview'));await pending;assert.equal(context.accountViewSnapshot,null);assert.equal(context.accountViewBusy,false);assert(nodes.accountViewStatus.textContent.includes('8秒'));
 }else if(testcase==='lock_and_retry_view'||testcase==='lock_and_retry_orders'){
  session();const old=deferred(),fresh=deferred();handler=()=>old.promise;
  const isOrders=testcase.endsWith('orders');const pending=isOrders?context.checkAccountOrders():context.loadAccountView();await settle();context.lockAccountView();
  assert.equal(context.accountViewBusy,false);assert.equal(context.accountOrdersBusy,false);session();handler=()=>fresh.promise;
  const next=isOrders?context.checkAccountOrders():context.loadAccountView();await settle();await pending;
  assert.equal(isOrders?context.accountOrdersBusy:context.accountViewBusy,true,'Old finally cannot unlock a newer request');
  old.resolve(response('/api/account/overview'));await settle();assert.equal(isOrders?context.accountOrdersBusy:context.accountViewBusy,true);
  fresh.resolve(response(isOrders?'/api/account/conditional-orders':'/api/account/overview'));await next;
  assert.equal(isOrders?context.accountOrdersBusy:context.accountViewBusy,false);assert(isOrders?context.accountOrdersSnapshot:context.accountViewSnapshot);
 }else if(testcase==='history_timeout_preserves_cursor'){
  session();context.accountViewHistory.positions=clone(history);const before=clone(context.accountViewHistory.positions);handler=()=>new Promise(()=>{});
  const pending=context.moreAccountHistory('positions');await settle();timeout();await pending;
  assert.deepEqual(clone(context.accountViewHistory.positions),before);assert.equal(context.accountViewBusy,false);assert(context.accountViewSession);assert(nodes.accountViewStatus.textContent.includes('保持'));
 }else if(testcase==='session_expires_before_body'){
  session();const gate=deferred();handler=()=>({ok:true,json:()=>gate.promise});const pending=context.accountRequest('/api/account/positions');const rejected=assert.rejects(pending,/ACCOUNT_LOCKED/);
  await settle();context.accountViewSession.expires=now;gate.resolve(clone(asset));await rejected;assert.equal(context.accountReadTickets.size,0);
 }else if(testcase==='duplicate_load_single_flight'){
  session();const gate=deferred();handler=()=>gate.promise;const pending=context.loadAccountView();await settle();const n=requests.length;await context.loadAccountView();assert.equal(requests.length,n);gate.resolve(response('/api/account/overview'));await pending;assert.equal(context.accountViewBusy,false);
 }else if(testcase==='late_rejection_no_unhandled'){
  session();const gate=deferred();handler=()=>gate.promise;const pending=context.loadAccountView();await settle();timeout();await pending;gate.reject(Error('synthetic late failure'));await settle();assert.equal(context.accountReadTickets.size,0);assert.equal(context.accountViewSnapshot,null);
 }else if(testcase==='authorization_bound_to_original_session'){
  session();const gate=deferred();handler=()=>gate.promise;const pending=context.accountRequest('/api/account/positions');const rejected=assert.rejects(pending,/ACCOUNT_LOCKED/);await settle();context.accountViewSession={token:'b'.repeat(43),expires:now+600000};gate.resolve(response('/api/account/positions'));await rejected;assert.equal(requests[0].opt.headers.Authorization,'Bearer '+'a'.repeat(43));
 }else throw Error(testcase);
 assert.equal(context.accountReadTickets.size,0);assert.equal(timers.size,0);
 assert(!requests.some(r=>/createOrder|cancelOrder|withdraw|\/arm|\/pause|execution/.test(r.url)));
 assert(!source.includes('localStorage')&&!source.includes('sessionStorage'));console.log(testcase+': OK');
})().catch(e=>{console.error(e);process.exitCode=1});
