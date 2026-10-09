// Actual private-view functions with synthetic data and scoped browser keys only.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const source=script.slice(script.indexOf('// Private account values'),script.indexOf('async function checkApiConnections('));
let now=100000,epoch=0,requests=[],fail=false,statusFailure=0,release=null,timers=new Map(),timerId=0;
let orders={source:'EDGEX_ACTIVE_CONDITIONAL_ORDERS',read_only:true,complete:true,snapshot_age_seconds:0,observed_ms:now,items:[{ticker:'<img src=x>',kind:'SL',side:'SELL',status:'UNTRIGGERED',quantity:'.01',trigger_price:'50000',reduce_only:null}]};
const ids=['accountOrdersCheck','accountOrdersStatus','accountOrdersData','accountViewData','accountViewStatus','accountViewBalances','accountViewPositions','accountViewTrades','accountViewCash','accountViewTradesMore','accountViewCashMore','accountViewOpen','accountViewLock','api'];
const nodes=Object.fromEntries(ids.map(id=>[id,{hidden:false,innerHTML:'',textContent:'',classList:{contains:()=>true}}]));
let subscription={toJSON:()=>({endpoint:'private-browser-endpoint',keys:{auth:'browser-proof'}})};
const row={time_ms:99999,type:'BUY_POSITION',ticker:'<img src=x onerror=bad()>',status:'L2_APPROVED',record_key:'a',price:'123',open_quantity:'1',close_quantity:'0',realized_pnl_usdc:null,open_fee_usdc:'0.1',close_fee_usdc:'0',funding_delta_usdc:null};
const history={items:[row],complete:false,next_cursor:'c'.repeat(32),window_start_ms:1,window_end_ms:100000};
const asset={source:'EDGEX_TRADING_ACCOUNT',read_only:true,positions_scope:'ALL_ACCOUNT_POSITIONS',snapshot_age_seconds:0,observed_ms:now,
 balance:{equity_usdc:'95',available_usdc:'80',cash_usdc:'100',unrealized_pnl_usdc:'-5',initial_margin_usdc:'20',order_frozen_usdc:null},
 positions:[{ticker:'<img src=x>',direction:'SHORT',quantity:'0.01',entry_price:'60000',unrealized_pnl_usdc:'-5',margin_usdc:null,liquidation_price:null}],histories:{positions:history,collateral:{...history,items:[],complete:true,next_cursor:null}}};
const listeners={};
const context={Date:class extends Date{static now(){return now}},Number,Math,JSON,Promise,AbortController,
 document:{visibilityState:'visible',getElementById:id=>nodes[id],addEventListener:(name,fn)=>listeners[name]=fn},
 setInterval(){},setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id},clearTimeout:id=>timers.delete(id),
 syncAnalysisCapital(){},apiCapitalMode:()=>false,currentPushSubscription:async()=>subscription,lifecycleEscape:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'),card:(k,v)=>k+':'+v+';',
 fetch:async(url,opt)=>{requests.push({url,opt});if(url==='/api/account/logout')return {ok:true};if(fail)throw Error('offline');if(statusFailure)return {ok:false,status:statusFailure};if(release){const gate=release;release=null;await gate.promise}return {ok:true,json:async()=>url==='/api/account/conditional-orders'?JSON.parse(JSON.stringify(orders)):url.startsWith('/api/account/history')?{...history,items:[{...row,record_key:'b'}],complete:true,next_cursor:null}:JSON.parse(JSON.stringify(asset))}},
 jf:async(url,opt={})=>{requests.push({url,opt});if(fail)throw Error('private-error');if(url==='/api/account/session')return {view_token:'a'.repeat(43),expires_in_seconds:600,read_only:true};if(release){const gate=release;release=null;await gate.promise}if(url.startsWith('/api/account/history'))return {...history,items:[{...row,record_key:'b'}],complete:true,next_cursor:null};return JSON.parse(JSON.stringify(asset))}
};
vm.createContext(context);vm.runInContext(source,context);
async function settle(){for(let i=0;i<4;i++)await new Promise(setImmediate)}
(async()=>{
 await context.loadAccountView();assert(!nodes.accountViewData.hidden);assert(nodes.accountViewBalances.innerHTML.includes('95.00'));assert(nodes.accountViewBalances.innerHTML.includes('100.00'));assert(nodes.accountViewBalances.innerHTML.includes('未確認'));
 assert(!nodes.accountViewPositions.innerHTML.includes('<img'));assert(nodes.accountViewPositions.innerHTML.includes('&lt;img'));
 assert(!nodes.accountViewTrades.innerHTML.includes('<img'));assert(nodes.accountViewTrades.innerHTML.includes('全件未取得'));assert(!nodes.accountViewTradesMore.hidden);
 assert(nodes.accountViewCash.innerHTML.includes('履歴はありません'));assert(nodes.accountViewCashMore.hidden);
 assert(!nodes.accountViewTrades.innerHTML.includes('費用込み純利益'));
 for(const r of requests.filter(r=>r.opt.method!=='POST'))assert.equal(r.opt.headers.Authorization,'Bearer '+'a'.repeat(43));
 assert(!requests.some(r=>r.url.includes('token=')||r.url.includes('endpoint=')));
 await context.moreAccountHistory('positions');assert.equal(context.accountViewHistory.positions.items.length,2);assert(nodes.accountViewTradesMore.hidden);assert(nodes.accountViewTrades.innerHTML.includes('末尾まで取得'));
 now+=30000;context.renderAccountPositions(context.accountViewSnapshot);assert.equal(nodes.accountViewBalances.innerHTML,'');assert.equal(nodes.accountViewPositions.innerHTML,'');assert(nodes.accountViewStatus.textContent.includes('最新状態を確認できません'));
 now=100000;await context.loadAccountView();fail=true;await context.refreshAccountPositions();assert.equal(nodes.accountViewPositions.innerHTML,'');assert(nodes.accountViewTrades.innerHTML.includes('取引'));
 fail=false;await context.loadAccountView();context.document.visibilityState='hidden';listeners.visibilitychange();assert(nodes.accountViewData.hidden);assert.equal(context.accountViewSession,null);assert.equal(nodes.accountViewTrades.innerHTML,'');assert(requests.some(r=>r.url==='/api/account/logout'));
 context.document.visibilityState='visible';subscription=null;await context.loadAccountView();assert(nodes.accountViewData.hidden);assert(nodes.accountViewStatus.textContent.includes('登録済み'));
 subscription={toJSON:()=>({endpoint:'private-browser-endpoint',keys:{auth:'browser-proof'}})};
 await context.loadAccountView();now+=600000;await context.refreshAccountPositions();assert(nodes.accountViewData.hidden);assert(nodes.accountViewStatus.textContent.includes('期限'));
 now=100000;await context.loadAccountView();statusFailure=403;await context.refreshAccountPositions();assert(nodes.accountViewData.hidden);assert.equal(nodes.accountViewTrades.innerHTML,'');statusFailure=503;await context.loadAccountView();assert(nodes.accountViewStatus.textContent.includes('時間をおいて'));statusFailure=0;
 now=100000;let done;release={promise:new Promise(r=>done=r)};const pending=context.loadAccountView();await settle();context.lockAccountView();done();await pending;assert(nodes.accountViewData.hidden);assert.equal(context.accountViewSnapshot,null,'Late response cannot undo lock');
 await context.loadAccountView();await context.checkAccountOrders();assert(nodes.accountOrdersData.innerHTML.includes('&lt;img'));assert(!nodes.accountOrdersData.innerHTML.includes('<img'));assert(nodes.accountOrdersData.innerHTML.includes('未確認'));assert(nodes.accountOrdersStatus.textContent.includes('保証ではありません'));
 now+=30000;context.renderAccountOrders(context.accountOrdersSnapshot);assert.equal(nodes.accountOrdersData.innerHTML,'');now=100000;
 orders.items=[];await context.checkAccountOrders();assert(nodes.accountOrdersData.innerHTML.includes('見つかりません'));
 statusFailure=503;await context.checkAccountOrders();assert.equal(nodes.accountOrdersData.innerHTML,'');assert(nodes.accountOrdersStatus.textContent.includes('未確認'));statusFailure=0;
 release={promise:new Promise(r=>done=r)};const lateOrders=context.checkAccountOrders();await settle();context.lockAccountView();done();await lateOrders;assert.equal(nodes.accountOrdersData.innerHTML,'');assert.equal(context.accountOrdersSnapshot,null);
 assert(requests.filter(r=>r.url==='/api/account/conditional-orders').every(r=>!r.opt.method||r.opt.method==='GET'));
 assert(!source.includes('localStorage'));assert(!source.includes('sessionStorage'));
 assert(!requests.some(r=>r.url.includes('createOrder')||r.url.includes('cancelOrder')||r.url.includes('withdraw')));
 assert.equal(html.split("<script>")[0].match(/data-tab="/g).length,4);
 console.log('Private account UI: authentication, stale hiding, escaped data, missing fields, paging, logout and late-response lock: OK');
})().catch(e=>{console.error(e);process.exitCode=1});
