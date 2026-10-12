// Effective notification state; synthetic subscription only, no registration or delivery.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];new Function(script);
let clock=Date.UTC(2026,9,12,6),timer=0,calls=[],permissions=0,subscriptions=0;const timers=new Map(),nodes={};
for(const id of ['pushStatus','pushReadinessChecks','backgroundPush','pushRecheck','pushCandidatePref','pushDailyPref','pushPrefStatus','quietStart','quietEnd','snoozeStatus'])nodes[id]={innerHTML:'',textContent:'',value:'',checked:false,disabled:false,attrs:{},setAttribute(k,v){this.attrs[k]=v},classList:{toggle(){},remove(){}}};
const preferences=()=>({candidate_alerts:true,daily_summary:false,notification_policy:'READY_ONLY',timezone:'Asia/Tokyo',snooze_until_ms:null,quiet_start:null,quiet_end:null,last_success_ms:null}),config={enabled:true,notification_policy:'READY_ONLY'},sub={endpoint:'https://example.test/PRIVATE_ENDPOINT',toJSON:()=>({endpoint:'https://example.test/PRIVATE_ENDPOINT',keys:{auth:'SYNTHETIC'}})};
const compat={supported:true,ios:false,standalone:false},reg={pushManager:{getSubscription:async()=>sub,subscribe(){subscriptions++},unsubscribe(){subscriptions++}}};
const ctx={Date:class extends Date{static now(){return clock}},Intl,Number,Promise,Object,AbortController,document:{getElementById:id=>nodes[id]},window:{Notification:true},Notification:{permission:'granted',requestPermission(){permissions++}},navigator:{onLine:true,serviceWorker:{ready:Promise.resolve(reg)}},
 setTimeout(fn,ms){const id=++timer;timers.set(id,{fn,ms});return id},clearTimeout:id=>timers.delete(id),pushPreferenceState:{},pushCompatibility:()=>({...compat}),renderPushGuide(){},lifecycleEscape:s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'),
 jf:async(url,opt)=>{calls.push({url,opt});return ctx.configReply()},configReply:async()=>({...config}),pushRequest:async(s,url,opt)=>{assert.equal(s,sub);calls.push({url,opt});return ctx.prefReply()},prefReply:async()=>preferences(),loadPushEvents:async()=>{}};
vm.createContext(ctx);vm.runInContext(script.slice(script.indexOf('async function loadPushPreferences('),script.indexOf('async function savePushPreferences(')),ctx);vm.runInContext(script.slice(script.indexOf('function renderSnoozeStatus('),script.indexOf('async function setPushSnooze(')),ctx);vm.runInContext(script.slice(script.indexOf('var pushQuietFormKey='),script.indexOf('async function toggleBackgroundPush(')),ctx);
const state=(p=preferences())=>({status:'VERIFIED',observed_ms:clock,compat:{...compat},permission:'granted',config:{...config},subscribed:true,preferences:p}),describe=s=>ctx.describePushReadiness(s,clock),defer=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}},settle=async()=>{for(let i=0;i<4;i++)await new Promise(setImmediate)},name=process.argv[3];
(async()=>{
 if(name==='fully_ready_not_delivery_proof'){
  await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'READY_TO_RECEIVE');assert(nodes.pushStatus.textContent.includes('確認済み'));assert(nodes.pushReadinessChecks.innerHTML.includes('端末への到着はこの確認だけでは検証できません'));assert(nodes.pushReadinessChecks.innerHTML.includes('画面内通知'));assert.equal(nodes.pushCandidatePref.checked,true);assert.equal(nodes.pushCandidatePref.disabled,false);
 }else if(name==='ios_install_and_unsupported'){
  compat.ios=true;await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'NEEDS_INSTALL');assert.equal(calls.length,0);assert(nodes.backgroundPush.disabled);compat.ios=false;compat.supported=false;await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'UNSUPPORTED');assert.equal(calls.length,0);
 }else if(name==='permissions_and_revocation'){
  ctx.Notification.permission='denied';await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'PERMISSION_DENIED');ctx.Notification.permission='default';ctx.renderPushReadiness();assert.equal(nodes.pushStatus.attrs['data-state'],'NEEDS_PERMISSION');ctx.Notification.permission='granted';ctx.renderPushReadiness();assert.equal(nodes.pushStatus.attrs['data-state'],'READY_TO_RECEIVE');ctx.Notification.permission='denied';ctx.renderPushReadiness();assert.equal(nodes.pushStatus.attrs['data-state'],'PERMISSION_DENIED');
 }else if(name==='no_local_subscription'){
  reg.pushManager.getSubscription=async()=>null;await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'NO_SUBSCRIPTION');assert.equal(calls.length,1);assert.equal(nodes.pushCandidatePref.disabled,true);assert(nodes.backgroundPush.textContent.endsWith('OFF'));
 }else if(name==='server_disabled'){
  ctx.configReply=async()=>({...config,enabled:false});await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'SERVER_DISABLED');assert(!nodes.pushStatus.textContent.includes('確認済み'));
 }else if(name==='preferences_off'){
  ctx.prefReply=async()=>({...preferences(),candidate_alerts:false});await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'PREFERENCES_OFF');assert(nodes.backgroundPush.textContent.endsWith('ON'));assert(nodes.pushReadinessChecks.innerHTML.includes('エントリー通知</b>：OFF'));
 }else if(name==='snooze_expiry'){
  const p={...preferences(),snooze_until_ms:clock+1000};assert.equal(describe(state(p)).status,'SNOOZED');clock+=1000;assert.equal(describe(state(p)).status,'READY_TO_RECEIVE');
 }else if(name==='quiet_overnight_boundaries'){
  const p={...preferences(),quiet_start:'23:00',quiet_end:'07:00'};
  for(const [hour,minute,expected] of [[13,59,false],[14,0,true],[21,59,true],[22,0,false]]){clock=Date.UTC(2026,9,12,hour,minute);assert.equal(ctx.pushQuietWindow(p,clock).active,expected);assert.equal(describe(state(p)).status,expected?'QUIET':'READY_TO_RECEIVE')}
 }else if(name==='quiet_day_timezone'){
  const p={...preferences(),timezone:'America/New_York',quiet_start:'09:00',quiet_end:'17:00'};clock=Date.UTC(2026,9,12,12,59);assert.equal(ctx.pushQuietWindow(p,clock).active,false);clock+=60000;assert.equal(ctx.pushQuietWindow(p,clock).active,true);clock=Date.UTC(2026,9,12,21);assert.equal(ctx.pushQuietWindow(p,clock).active,false);
 }else if(name==='quiet_disabled_and_partial'){
  for(const p of [{...preferences(),quiet_start:'00:00',quiet_end:'00:00'},{...preferences(),quiet_start:'23:00',quiet_end:null}])assert.equal(describe(state(p)).status,'READY_TO_RECEIVE');
 }else if(name==='invalid_quiet_unknown'){
  for(const p of [{...preferences(),quiet_start:'25:00',quiet_end:'07:00'},{...preferences(),timezone:'NOT_A_ZONE',quiet_start:'23:00',quiet_end:'07:00'}])assert.equal(describe(state(p)).status,'UNKNOWN');
 }else if(name==='invalid_preferences'){
  for(const p of [{...preferences(),candidate_alerts:1},{...preferences(),snooze_until_ms:-1},{...preferences(),daily_summary:true},{...preferences(),notification_policy:'SHADOW'},{...preferences(),quiet_start:99}]){ctx.prefReply=async()=>p;await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'UNKNOWN');assert.equal(nodes.pushCandidatePref.disabled,true)}
 }else if(name==='freshness_and_clock_reversal'){
  const s=state();clock+=119999;assert.equal(describe(s).status,'READY_TO_RECEIVE');clock++;assert.equal(describe(s).status,'STALE');assert.equal(describe({...s,observed_ms:clock+1}).status,'STALE');assert.equal(describe({...s,observed_ms:null}).status,'UNKNOWN');
 }else if(name==='unknown_preferences_not_off'){
  assert.equal(describe(state(null)).status,'UNKNOWN');ctx.prefReply=async()=>{throw Error('PRIVATE_ERROR')};await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'UNKNOWN');assert(nodes.pushReadinessChecks.innerHTML.includes('未確認'));assert(!nodes.pushReadinessChecks.innerHTML.includes('PRIVATE_ERROR'));
 }else if(name==='worker_timeout_and_late_subscription'){
  const d=defer();ctx.navigator.serviceWorker.ready=d.promise;const old=ctx.syncBackgroundPushButton();timers.values().next().value.fn();await old;assert.equal(nodes.pushStatus.attrs['data-state'],'UNKNOWN');assert.equal(nodes.pushRecheck.attrs['aria-busy'],'false');d.resolve(reg);await settle();assert.equal(nodes.pushStatus.attrs['data-state'],'UNKNOWN');assert.equal(calls.length,1);ctx.navigator.serviceWorker.ready=Promise.resolve(reg);await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'READY_TO_RECEIVE');
 }else if(name==='preference_timeout_and_retry'){
  const d=defer();ctx.prefReply=()=>d.promise;const old=ctx.syncBackgroundPushButton();await settle();timers.values().next().value.fn();await old;assert.equal(nodes.pushStatus.attrs['data-state'],'UNKNOWN');ctx.prefReply=async()=>preferences();await ctx.syncBackgroundPushButton();d.resolve({...preferences(),candidate_alerts:false});await settle();assert.equal(nodes.pushStatus.attrs['data-state'],'READY_TO_RECEIVE');assert.equal(nodes.pushCandidatePref.checked,true);assert.equal(timers.size,0);
 }else if(name==='superseded_response'){
  const d=defer();ctx.prefReply=()=>d.promise;const old=ctx.syncBackgroundPushButton();await settle();ctx.prefReply=async()=>({...preferences(),candidate_alerts:false});await ctx.syncBackgroundPushButton();d.resolve(preferences());await old;assert.equal(nodes.pushStatus.attrs['data-state'],'PREFERENCES_OFF');assert.equal(nodes.pushCandidatePref.checked,false);assert.equal(nodes.pushRecheck.attrs['aria-busy'],'false');
 }else if(name==='offline_no_requests'){
  ctx.navigator.onLine=false;await ctx.syncBackgroundPushButton();assert.equal(calls.length,0);assert.equal(nodes.pushStatus.attrs['data-state'],'OFFLINE');ctx.navigator.onLine=true;await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'READY_TO_RECEIVE');
 }else if(name==='read_failure_and_invalid_config'){
  for(const cfg of [null,{}, {...config,enabled:'yes'}, {...config,notification_policy:'ALL'}]){ctx.configReply=async()=>cfg;await ctx.syncBackgroundPushButton();assert.equal(nodes.pushStatus.attrs['data-state'],'UNKNOWN')}
  ctx.configReply=async()=>{throw Error('PRIVATE_ERROR')};await ctx.syncBackgroundPushButton();assert(!nodes.pushStatus.textContent.includes('PRIVATE_ERROR'));
 }else if(name==='settings_invalidation'){
  await ctx.syncBackgroundPushButton();ctx.invalidatePushReadiness();assert.equal(nodes.pushStatus.attrs['data-state'],'UNKNOWN');assert.equal(ctx.pushReadinessState.preferences,null);assert.equal(nodes.pushRecheck.attrs['aria-busy'],'false');assert(!nodes.pushReadinessChecks.innerHTML.includes('PRIVATE_ENDPOINT'));
 }else if(name==='privacy_and_escaping'){
  ctx.pushReadinessState=state({...preferences(),timezone:'<img src=x onerror=bad()>',quiet_start:'23:00',quiet_end:'07:00'});ctx.renderPushReadiness();assert(!nodes.pushReadinessChecks.innerHTML.includes('<img'));assert(!nodes.pushReadinessChecks.innerHTML.includes(sub.endpoint));assert.equal(nodes.pushStatus.attrs['data-state'],'UNKNOWN');
 }else if(name==='unsaved_quiet_draft_preserved'){
  await ctx.syncBackgroundPushButton();nodes.quietStart.value='22:30';await ctx.syncBackgroundPushButton();assert.equal(nodes.quietStart.value,'22:30');assert(nodes.pushPrefStatus.textContent.includes('未保存'));assert.equal(nodes.pushStatus.attrs['data-state'],'READY_TO_RECEIVE');ctx.prefReply=async()=>({...preferences(),quiet_start:'22:30',quiet_end:'07:00'});await ctx.syncBackgroundPushButton();assert.equal(nodes.quietStart.value,'22:30');assert(!nodes.pushPrefStatus.textContent.includes('未保存'));
 }else if(name==='quiet_edit_during_check'){
  const d=defer();ctx.prefReply=()=>d.promise;const pending=ctx.syncBackgroundPushButton();await settle();nodes.quietStart.value='21:15';d.resolve(preferences());await pending;assert.equal(nodes.quietStart.value,'21:15');assert(nodes.pushPrefStatus.textContent.includes('未保存'));assert.equal(ctx.pushReadinessState.preferences.quiet_start,null);
 }else if(name==='bindings_and_scope'){
  assert(script.includes("document.getElementById('pushRecheck').onclick=syncBackgroundPushButton"));assert(script.includes("document.getElementById('notificationSettings').addEventListener('toggle'"));assert(script.includes("window.addEventListener('offline',invalidatePushReadiness)"));assert.equal((html.match(/<button\b[^>]*\bdata-tab="/g)||[]).length,4);await ctx.loadNotificationCenter();
 }else throw Error(name);
 assert.equal(permissions,0);assert.equal(subscriptions,0);assert(calls.every(r=>r.url==='/api/push/config'||r.url.startsWith('/api/push/preferences?')));assert(calls.every(r=>!r.opt.method||r.opt.method==='GET'));console.log(name+': OK');
})().catch(e=>{console.error(e);process.exitCode=1});
