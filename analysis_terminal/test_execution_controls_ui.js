// Actual ON/OFF functions with synthetic scopes and an acknowledgement gap.
const fs=require('fs'),vm=require('vm'),assert=require('assert'),crypto=require('crypto').webcrypto;
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const code=script.slice(script.indexOf('// Deliberate new-entry authority'));
let now=100000,requests=[],intervals=[],listeners={},loads=0,gate=null,offline=false,denied=0,sub=true,bootGate=null;
const nodes={};for(const id of ['executionControlOn','executionControlOff','executionControlUnlock','executionControlLock','executionControlRefresh','executionControlReconcile','executionControlStatus','executionControlNextStep'])nodes[id]={disabled:false,textContent:''};
let state={scope:'NEW_ENTRY_CONTROL',mode:'LIVE',armed:false,new_entries_enabled:false,worker_available:true,can_request_on:true,control_epoch:0,protective_management_enabled:true,latest_request:null};
const context={Date:class extends Date{static now(){return now}},Number,Math,JSON,Promise,AbortController,crypto,
 document:{visibilityState:'visible',getElementById:id=>nodes[id],addEventListener:(name,fn)=>listeners[name]=fn},setInterval:(fn,ms)=>intervals.push({fn,ms}),setTimeout,clearTimeout,
 loadLiveExecution:()=>loads++,currentPushSubscription:async()=>sub?{toJSON:()=>({endpoint:'SYNTHETIC',keys:{auth:'TEST_ONLY'}})}:null,
 fetch:async(url,opt={})=>{requests.push({url,opt});if(url==='/api/execution/logout')return {ok:true,json:async()=>({ok:true})};if(denied)return {ok:false,status:denied};if(offline)throw Error('offline');
  if(url==='/api/execution/session'){if(bootGate){const g=bootGate;bootGate=null;await g.promise}return {ok:true,json:async()=>({scope:'NEW_ENTRY_CONTROL',read_only:false,expires_in_seconds:120,control_token:'c'.repeat(43)})}}
  if(url==='/api/execution/control'){const r=JSON.parse(opt.body);if(r.action==='arm'||r.action==='reconcile_flat')state.latest_request={request_id:r.request_id,action:r.action,status:'QUEUED'};else{state.armed=false;state.new_entries_enabled=false;state.can_request_on=true;state.control_epoch++;state.latest_request={request_id:r.request_id,action:r.action,status:'DONE'}}
   const value=JSON.parse(JSON.stringify(state));if(gate){const g=gate;gate=null;await g.promise}return {ok:true,json:async()=>value}}
  return {ok:true,json:async()=>JSON.parse(JSON.stringify(state))};}
};vm.createContext(context);vm.runInContext(code,context);
async function tick(){await new Promise(setImmediate)}
function finishOn(){state.armed=true;state.new_entries_enabled=true;state.can_request_on=false;state.control_epoch++;state.latest_request.status='DONE'}
(async()=>{
 assert(nodes.executionControlOn.disabled);assert(nodes.executionControlOff.disabled);assert.equal(requests.length,0,'boot does not acquire control authority');
 assert(nodes.executionControlNextStep.textContent.includes('元の登録端末'));
 assert(!code.includes('localStorage'));assert(!code.includes('sessionStorage'));assert(!code.includes('accountViewSession'));assert(!code.includes('view_token'));
 await context.unlockExecutionControls();assert(!nodes.executionControlOn.disabled);assert(!nodes.executionControlOff.disabled);assert(nodes.executionControlUnlock.disabled);assert(!state.armed);
 const originalState=JSON.parse(JSON.stringify(state));state.can_request_on=false;state.active_orders=1;state.on_blockers=['OWNERSHIP_UNCERTAIN'];state.can_request_flat_reconciliation=true;
 await context.refreshExecutionControls();assert(nodes.executionControlOn.disabled);assert(!nodes.executionControlOff.disabled);
 assert(nodes.executionControlOn.textContent.includes('開始不可'));assert(nodes.executionControlStatus.textContent.includes('SL/TP'));assert(nodes.executionControlStatus.textContent.includes('終了した建玉を照合'));
 const blockedRequests=requests.filter(r=>r.url==='/api/execution/control').length;
 await context.setExecutionControl('arm');intervals.find(x=>x.ms===1000).fn();assert(nodes.executionControlStatus.textContent.includes('未照合のボット取引'));
 assert.equal(requests.filter(r=>r.url==='/api/execution/control').length,blockedRequests,'explaining a block never submits ON');
 state.can_request_flat_reconciliation=false;await context.refreshExecutionControls();assert(nodes.executionControlReconcile.disabled);assert(nodes.executionControlStatus.textContent.includes('対象が確定するまで'));assert(!nodes.executionControlNextStep.textContent.includes('押し'));
 state.on_blockers=['<img src=x onerror=bad()>'];await context.refreshExecutionControls();assert(nodes.executionControlStatus.textContent.includes('未決着・未照合'));assert(!nodes.executionControlStatus.textContent.includes('<img'));
 state.active_orders=0;state.worker_available=false;state.on_blockers=['WORKER_UNAVAILABLE'];await context.refreshExecutionControls();assert(nodes.executionControlStatus.textContent.includes('実行worker'));
 state.worker_available=true;state.on_blockers=[];await context.refreshExecutionControls();assert(nodes.executionControlStatus.textContent.includes('開始条件を確認できません'));
 state=originalState;await context.refreshExecutionControls();assert(!nodes.executionControlOn.disabled);
 await nodes.executionControlOn.onclick();assert.equal(state.latest_request.status,'QUEUED');assert(nodes.executionControlStatus.textContent.includes('照合中'));assert(nodes.executionControlOn.disabled);assert(!nodes.executionControlOff.disabled);
 let sent=requests.find(r=>r.url==='/api/execution/control');assert.equal(sent.opt.headers.Authorization,'Bearer '+'c'.repeat(43));assert.equal(JSON.parse(sent.opt.body).action,'arm');assert.match(JSON.parse(sent.opt.body).request_id,/^[0-9a-f-]{36}$/);
 finishOn();await context.refreshExecutionControls();assert(nodes.executionControlStatus.textContent.includes('ON —'));assert(nodes.executionControlOn.disabled);
 await nodes.executionControlOff.onclick();assert(!state.armed);assert(nodes.executionControlStatus.textContent.includes('OFF —'));assert(state.protective_management_enabled);
 const count=requests.filter(r=>r.url==='/api/execution/control').length;
 context.lockExecutionControls();assert(nodes.executionControlOn.disabled);assert(nodes.executionControlOff.disabled);assert.equal(requests.filter(r=>r.url==='/api/execution/control').length,count,'locking is not OFF');
 assert(nodes.executionControlOn.textContent.includes('操作ロック中'));assert(nodes.executionControlStatus.textContent.includes('残高閲覧の認証とは別'));
 await context.unlockExecutionControls();let done;gate={promise:new Promise(r=>done=r)};const pending=nodes.executionControlOn.onclick();await tick();intervals.find(x=>x.ms===1000).fn();assert(nodes.executionControlOn.disabled,'timer cannot enable ON during submit');
 await nodes.executionControlOff.onclick();assert(nodes.executionControlStatus.textContent.includes('OFF —'));done();await pending;assert(nodes.executionControlStatus.textContent.includes('OFF —'),'late ON acknowledgement cannot overwrite OFF');assert(!state.armed);
 gate={promise:new Promise(r=>done=r)};const late=nodes.executionControlOn.onclick();await tick();context.lockExecutionControls();done();await late;assert.equal(context.executionControlSession,null);assert(nodes.executionControlOn.disabled);
 state.latest_request=null;await context.unlockExecutionControls();now+=120000;intervals.find(x=>x.ms===1000).fn();assert.equal(context.executionControlSession,null);assert(nodes.executionControlOff.disabled);assert(!state.armed);
 now=100000;await context.unlockExecutionControls();context.document.visibilityState='hidden';listeners.visibilitychange();assert.equal(context.executionControlSession,null);assert(!state.armed);context.document.visibilityState='visible';
 await context.unlockExecutionControls();offline=true;await context.refreshExecutionControls();assert(nodes.executionControlOn.disabled);assert(!nodes.executionControlOff.disabled,'OFF remains available when status unknown');assert(nodes.executionControlStatus.textContent.includes('未確認')||nodes.executionControlStatus.textContent.includes('確認できません'));
 assert(nodes.executionControlOn.textContent.includes('状態未確認'));
 offline=false;await context.refreshExecutionControls();denied=403;await context.refreshExecutionControls();assert.equal(context.executionControlSession,null);assert(nodes.executionControlOff.disabled);denied=0;
 sub=false;await context.unlockExecutionControls();assert(nodes.executionControlOn.disabled);assert(nodes.executionControlStatus.textContent.includes('登録済み'));sub=true;
 bootGate={promise:new Promise(r=>done=r)};const boot=context.unlockExecutionControls();await tick();context.lockExecutionControls();done();await boot;assert.equal(context.executionControlSession,null);
 await context.unlockExecutionControls();state.armed=true;state.new_entries_enabled=false;state.mode='OFF';state.can_request_on=false;state.worker_available=false;state.latest_request=null;await context.refreshExecutionControls();assert(!nodes.executionControlStatus.textContent.includes('新規エントリー有効'));assert(nodes.executionControlOn.disabled);
 state.armed=false;state.mode='LIVE';state.worker_available=true;state.can_request_on=false;state.can_request_flat_reconciliation=true;state.latest_request=null;await context.refreshExecutionControls();assert(!nodes.executionControlReconcile.disabled);
 await nodes.executionControlReconcile.onclick();assert.equal(state.latest_request.action,'reconcile_flat');assert.equal(state.latest_request.status,'QUEUED');assert(nodes.executionControlOn.disabled);assert(nodes.executionControlReconcile.disabled);assert(!nodes.executionControlOff.disabled);assert(!state.armed);
 assert(nodes.executionControlStatus.textContent.includes('終了した建玉を照合中'));assert(!nodes.executionControlStatus.textContent.includes('ONの開始条件'));assert(nodes.executionControlNextStep.textContent.includes('繰り返さない'));
 state.latest_request.status='REFUSED';state.latest_request.code='FLAT_REVIEW_POSITION_OR_ORDERS_REMAIN';await context.refreshExecutionControls();assert(nodes.executionControlStatus.textContent.includes('建玉または残注文'));assert(!state.armed);
 assert(nodes.executionControlStatus.textContent.includes('終了後照合要求'));assert(!nodes.executionControlStatus.textContent.includes('開始要求'));assert.equal(nodes.executionControlOn.title,nodes.executionControlStatus.textContent);
 state.latest_request.status='DONE';state.latest_request.code='FLAT_REVIEW_DONE';state.can_request_flat_reconciliation=false;state.can_request_on=true;await context.refreshExecutionControls();assert(nodes.executionControlStatus.textContent.includes('現在はOFF'));assert(nodes.executionControlReconcile.disabled);assert(!nodes.executionControlOn.disabled);assert(!state.armed,'Review does not arm');
 state.armed=true;state.new_entries_enabled=true;state.can_request_on=false;await context.refreshExecutionControls();assert(nodes.executionControlStatus.textContent.startsWith('ON —'));assert(!nodes.executionControlStatus.textContent.includes('現在はOFF'));assert(nodes.executionControlStatus.textContent.includes('直近の操作結果'));assert(nodes.executionControlNextStep.textContent.includes('止める場合はOFF'));
 state.armed=false;state.new_entries_enabled=false;state.can_request_on=true;state.latest_request={action:'arm',status:'REFUSED',code:'AUTHORIZATION_LOST'};await context.refreshExecutionControls();assert(nodes.executionControlStatus.textContent.includes('開始要求'));assert(nodes.executionControlStatus.textContent.includes('操作認証'));
 state.latest_request={action:'pause',status:'REFUSED',code:'<img src=x onerror=bad()>'};await context.refreshExecutionControls();assert(nodes.executionControlStatus.textContent.includes('停止要求'));assert(!nodes.executionControlStatus.textContent.includes('<img'));
 context.lockExecutionControls();assert(nodes.executionControlReconcile.disabled);
 assert(requests.filter(r=>r.url==='/api/execution/control').every(r=>['arm','pause','reconcile_flat'].includes(JSON.parse(r.opt.body).action)));
 assert(!requests.some(r=>/token=|createOrder|cancelOrder|withdraw/.test(r.url)));
 console.log('ON/OFF UI: separate scope, deliberate actions, acknowledgement gap, OFF priority, stale state, lock/expiry and late responses: OK');
})().catch(e=>{console.error(e);process.exitCode=1});
