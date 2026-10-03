// Execute the shipped foreground notification function with controlled time and delivery.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2]||'analysis_terminal/index.html','utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new Function(script);
const notification=script.slice(script.indexOf('async function sendCandidateNotification('),script.indexOf('\nfunction pushCandidateAlert('));
const freshness=script.slice(script.indexOf('function entryFreshness('),script.indexOf('function renderGlobalEntryBar('));
(async()=>{
 let now=1000000,deliveries=[],resolveReady;
 const snapshot=()=>({snapshot_age_seconds:0,_client_rendered_ms:now});
 const context={Date:{now:()=>now},Number,Math,candidateNotifyEnabled:true,lastMarketPayload:snapshot(),window:{Notification:true},Notification:{permission:'granted'},location:{origin:'https://test'},fmt:(n)=>String(n),navigator:{serviceWorker:{ready:Promise.resolve({showNotification:(...args)=>deliveries.push(args)})}}};
 vm.createContext(context);vm.runInContext(freshness+notification,context);
 const send=kind=>context.sendCandidateNotification({kind,ticker:'TESTUSDC',label:'新しくエントリー可能',rr:2});
 for(const kind of ['NEAR','SHADOW','RR_WAIT'])await send(kind);
 assert.equal(deliveries.length,0);
 await send('READY');assert.equal(deliveries.length,1);assert.equal(deliveries[0][0],'EdgeX エントリー可能');
 now+=120000;await send('READY');assert.equal(deliveries.length,1);
 context.lastMarketPayload=null;await send('READY');assert.equal(deliveries.length,1);
 context.lastMarketPayload=snapshot();context.candidateNotifyEnabled=false;await send('READY');assert.equal(deliveries.length,1);
 context.candidateNotifyEnabled=true;
 context.navigator.serviceWorker.ready=new Promise(r=>resolveReady=r);
 let pending=send('READY');now+=120000;resolveReady({showNotification:(...args)=>deliveries.push(args)});await pending;assert.equal(deliveries.length,1);
 context.lastMarketPayload=snapshot();context.navigator.serviceWorker.ready=new Promise(r=>resolveReady=r);
 pending=send('READY');context.lastMarketPayload=snapshot();resolveReady({showNotification:(...args)=>deliveries.push(args)});await pending;assert.equal(deliveries.length,1);
 console.log('Foreground READY-only, freshness, opt-out and async races: OK');
})().catch(e=>{console.error(e);process.exitCode=1});
