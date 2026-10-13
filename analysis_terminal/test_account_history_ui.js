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
context.window={addEventListener(){}};vm.createContext(context);vm.runInContext(source,context);
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}}
async function settle(){for(let i=0;i<4;i++)await new Promise(setImmediate)}
function timeout(){const t=[...timers.values()].find(t=>t.ms===8000);assert(t);t.fn()}
function session(){context.accountViewSession={token:'a'.repeat(43),expires:now+600000}}
const testcase=process.argv[3];
const row=(key='a',time=10)=>({record_key:key.repeat(64),time_ms:time,type:'BUY_POSITION',status:'L2_APPROVED',ticker:'TESTUSDC',price:null});
const seed={...history,items:[row()],window_end_ms:99999};
function setup(){session();context.accountViewHistory.positions=clone(seed);context.renderAccountHistory('positions')}
function page(extra={}){return {...clone(seed),items:[row('b',11)],complete:true,next_cursor:null,...extra}}
async function rejectPage(value){const before=clone(context.accountViewHistory.positions);handler=()=>({ok:true,json:async()=>clone(value)});await context.moreAccountHistory('positions');assert.deepEqual(clone(context.accountViewHistory.positions),before);assert(nodes.accountViewTrades.innerHTML.includes('再試行'));assert.equal(nodes.accountViewTradesMore.textContent,'再試行');assert.equal(nodes.accountViewTradesMore.disabled,false);assert.equal(context.accountViewBusy,false)}
(async()=>{
 setup();
 if(testcase==='valid_append'){
  handler=()=>({ok:true,json:async()=>page()});await context.moreAccountHistory('positions');assert.equal(context.accountViewHistory.positions.items.length,2);assert.equal(nodes.accountViewTradesMore.hidden,true);assert(nodes.accountViewTrades.innerHTML.includes('末尾まで取得'));
 }else if(testcase==='empty_final'){
  handler=()=>({ok:true,json:async()=>page({items:[]})});await context.moreAccountHistory('positions');assert.equal(context.accountViewHistory.positions.items.length,1);assert.equal(context.accountViewHistory.positions.complete,true);
 }else if(testcase==='empty_partial'){
  context.accountViewHistory.positions={...clone(seed),items:[]};context.renderAccountHistory('positions');assert(!nodes.accountViewTrades.innerHTML.includes('指定期間の履歴はありません'));assert(nodes.accountViewTrades.innerHTML.includes('続きは未確認'));assert.equal(nodes.accountViewTradesMore.hidden,false);
  handler=()=>({ok:true,json:async()=>page({items:[],complete:false,next_cursor:'d'.repeat(32)})});await context.moreAccountHistory('positions');assert.equal(context.accountViewHistory.positions.next_cursor,'d'.repeat(32));assert(!nodes.accountViewTrades.innerHTML.includes('指定期間の履歴はありません'));
 }else if(testcase==='invalid_flags'){
  for(const change of [{complete:'true'},{complete:undefined},{complete:false,next_cursor:null},{complete:true,next_cursor:'d'.repeat(32)},{next_cursor:undefined},{complete:false,next_cursor:'bad'}])await rejectPage(page(change));
 }else if(testcase==='invalid_window'){
  for(const change of [{window_start_ms:2},{window_end_ms:100000},{window_start_ms:'1'},{window_end_ms:0},{window_end_ms:Infinity}])await rejectPage(page(change));
 }else if(testcase==='duplicate_within_page')await rejectPage(page({items:[row('b'),row('b')]}));
 else if(testcase==='duplicate_across_pages')await rejectPage(page({items:[row('a')]}));
 else if(testcase==='malformed_key_or_time'){
  for(const item of [null,row('x'),{...row('b'),record_key:''},{...row('b'),time_ms:'10'},row('b',99999),row('b',0),{...row('b'),record_key:undefined}])await rejectPage(page({items:[item]}));
 }else if(testcase==='stale_observation'){
  for(const t of [99999,0,undefined,'100000',null])await rejectPage(page({observed_ms:t}));
 }else if(testcase==='oversized_page')await rejectPage(page({items:Array.from({length:51},(_,i)=>({...row(),record_key:i.toString(16).padStart(64,'0')}))}));
 else if(testcase==='stagnant_cursor')await rejectPage(page({complete:false,next_cursor:seed.next_cursor,items:[]}));
 else if(testcase==='revisited_cursor'){
  handler=()=>({ok:true,json:async()=>page({complete:false,next_cursor:'d'.repeat(32)})});await context.moreAccountHistory('positions');await rejectPage(page({items:[row('c')],complete:false,next_cursor:seed.next_cursor}));
 }else if(testcase==='replaced_history_ignored'){
  const gate=deferred();handler=()=>({ok:true,json:()=>gate.promise});const pending=context.moreAccountHistory('positions');await settle();const newHistory={...clone(seed),items:[row('c')]};context.accountViewHistory.positions=newHistory;gate.resolve(page());await pending;assert.strictEqual(context.accountViewHistory.positions,newHistory);
 }else if(testcase==='auth_failure_locks'){
  handler=()=>({ok:false,status:403});await context.moreAccountHistory('positions');assert.equal(context.accountViewSession,null);assert.equal(nodes.accountViewTrades.innerHTML,'');assert.equal(nodes.accountViewTradesMore.disabled,false);
 }else if(testcase==='timeout_preserves_cursor'){
  const old=clone(context.accountViewHistory.positions);handler=()=>new Promise(()=>{});const pending=context.moreAccountHistory('positions');await settle();assert(nodes.accountViewTradesMore.disabled);assert.equal(nodes.accountViewTradesMore.textContent,'取得中…');timeout();await pending;assert.deepEqual(clone(context.accountViewHistory.positions),old);assert.equal(nodes.accountViewTradesMore.disabled,false);assert.equal(nodes.accountViewTradesMore.textContent,'再試行');
 }else if(testcase==='failure_notice_survives_balance_refresh'){
  await rejectPage(page({complete:'true'}));context.renderAccountPositions({...clone(asset),_received_ms:now});assert(nodes.accountViewTrades.innerHTML.includes('取得済み分とページ番号は保持'));handler=()=>({ok:true,json:async()=>page()});await context.moreAccountHistory('positions');assert(!nodes.accountViewTrades.innerHTML.includes('再試行'));
 }else if(testcase==='expired_cursor_reloads_overview'||testcase==='server_failure_retries_same_cursor'){
  handler=()=>({ok:false,status:testcase==='expired_cursor_reloads_overview'?409:503});await context.moreAccountHistory('positions');
  assert.equal(context.accountViewHistory.positions.items.length,1);assert.equal(nodes.accountViewTradesMore.disabled,false);
  const before=requests.length;
  if(testcase==='expired_cursor_reloads_overview'){
   assert.equal(nodes.accountViewTradesMore.textContent,'履歴を再取得');handler=()=>({ok:true,json:async()=>({...clone(asset),histories:{positions:clone(seed)}})});await context.moreAccountHistory('positions');assert.equal(requests[before].url,'/api/account/overview');assert(!context.accountHistoryRestart.positions);assert.equal(nodes.accountViewTradesMore.textContent,'続きを読む');
  }else{
   assert.equal(nodes.accountViewTradesMore.textContent,'再試行');handler=()=>({ok:true,json:async()=>page()});await context.moreAccountHistory('positions');assert(requests[before].url.includes(seed.next_cursor));assert.equal(context.accountViewHistory.positions.items.length,2);
  }
 }else if(testcase==='initial_invalid_not_zero'){
  context.accountViewHistory.positions=page({complete:'true',items:[]});context.renderAccountHistory('positions');assert(nodes.accountViewTrades.textContent.includes('0件とは判断'));assert(nodes.accountViewTradesMore.hidden);assert(!nodes.accountViewTrades.innerHTML.includes('指定期間の履歴はありません'));
 }else if(testcase==='initial_load_button_released'){
  handler=()=>({ok:true,json:async()=>({...clone(asset),histories:{positions:clone(seed),collateral:clone(seed)}})});await context.loadAccountView();assert.equal(nodes.accountViewTradesMore.disabled,false);assert.equal(nodes.accountViewTradesMore.hidden,false);
 }else if(testcase==='duplicate_clicks'){
  const gate=deferred();handler=()=>({ok:true,json:()=>gate.promise});const pending=context.moreAccountHistory('positions');await settle();const n=requests.length;await context.moreAccountHistory('positions');assert.equal(requests.length,n);gate.resolve(page());await pending;assert.equal(context.accountViewHistory.positions.items.length,2);
 }else if(testcase==='cumulative_cap_not_claimed_complete'){
  context.accountViewHistory.positions={...clone(seed),items:Array.from({length:1000},(_,i)=>({...row(),record_key:i.toString(16).padStart(64,'0')}))};await rejectPage(page());
 }else if(testcase==='unexpected_fields_not_copied'){
  handler=()=>({ok:true,json:async()=>page({unexpected:'do-not-copy',_seen_cursors:['evil']})});await context.moreAccountHistory('positions');assert.equal(context.accountViewHistory.positions.unexpected,undefined);assert(!context.accountViewHistory.positions._seen_cursors.includes('evil'));
 }else throw Error(testcase);
 assert.equal(context.accountViewBusy,false);assert.equal(context.accountReadTickets.size,0);assert([...timers.keys()].every(k=>k===context.accountPrivateExpiryTimer),'Only the private expiry timer may remain');assert(!requests.some(r=>/createOrder|cancelOrder|withdraw|execution/.test(r.url)));console.log(testcase+': OK');
})().catch(e=>{console.error(e);process.exitCode=1});
