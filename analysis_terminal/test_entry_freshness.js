// Execute the actual UI functions with a controlled clock and a minimal DOM.
const fs = require('fs'), assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
new Function(script);
let now = 1_000_000, analyzed = 0, scanned = 0;
Date.now = () => now;
function element() {
  const classes = new Set();
  return {textContent:'', innerHTML:'', value:0,
    classList:{add(...xs){xs.forEach(x=>classes.add(x));}, remove(...xs){xs.forEach(x=>classes.delete(x));},
      contains(x){return classes.has(x);}, toggle(x,on){on?classes.add(x):classes.delete(x);}},
    querySelectorAll(selector){
      if(selector!=='.entryReadyCard')return [];
      if(!this.innerHTML.includes('entryReadyCard'))return [];
      if(!this.buttons)this.buttons=[{dataset:{ticker:'TESTUSDC'}}];
      return this.buttons;
    }};
}
const ids = ['globalEntryBar','globalEntryLabel','globalEntryMeta','globalEntryAction',
  'entryNowZone','entryNowBadge','entryNowContent','entryNowRescan','ticker','equity','riskPct'];
const nodes = Object.fromEntries(ids.map(x=>[x,element()])), tabButton = element();
global.document={getElementById:id=>nodes[id],querySelector:()=>tabButton,visibilityState:'visible',title:''};
global.localStorage={setItem(){},removeItem(){}};
global.navigator={vibrate(){}};
global.globalEntrySnapshot=null;global.entryHeartbeatBusy=false;global.lastReadySignature='';
global.scan=()=>scanned++;global.tab=()=>{};global.analyze=()=>analyzed++;
global.analysisRiskPlan=async()=>null;global.fmt=x=>String(x);global.directionJa=x=>x;
let nextTimer=1;const timers=new Map();
global.setTimeout=(fn,delay)=>{const id=nextTimer++;timers.set(id,{fn,delay});return id;};
global.clearTimeout=id=>timers.delete(id);
(0,eval)(script.slice(script.indexOf('function readySignature('),script.indexOf('function renderDailyConvenience(')));
const riskCalculator=global.dashboardReadyRisk;
const row={ticker:'TESTUSDC',direction:'LONG',entry_reference:100,stop_loss:90,take_profit:120,rr:2,score:90,latest_15m_time_ms:900000};
function snapshot(age=0,ready=[row]) {return {snapshot_age_seconds:age,_client_rendered_ms:now,ready_candidates:ready,qualified_near_candidates:[]};}
async function render(j){renderGlobalEntryBar(j);await renderEntryNow(j);}
function assertStale(){
  assert(nodes.globalEntryBar.classList.contains('stale'));
  assert(!nodes.entryNowZone.classList.contains('hasReady'));
  assert.equal(nodes.globalEntryLabel.textContent,'データ鮮度を確認');
  assert.equal(nodes.entryNowBadge.textContent,'確認中');
  assert(nodes.entryNowContent.innerHTML.includes('データ鮮度を確認'));
  assert(!nodes.entryNowContent.innerHTML.includes('entryReadyCard'));
  assert(!tabButton.classList.contains('entryReady'));
  assert.equal(document.title,'確認中 | EdgeX');
}
async function test(name){
  now=1_000_000;analyzed=0;scanned=0;global.dashboardReadyRisk=riskCalculator;
  if(name==='boundaries'){
    const j=snapshot(119.999);await render(j);
    assert(nodes.entryNowZone.classList.contains('hasReady'));
    assert(nodes.globalEntryBar.classList.contains('ready'));
    assert.equal(document.title,'READY TESTUSDC | EdgeX');
    await render(snapshot(120));assertStale();
    await render(snapshot(0,[]));assert.equal(nodes.entryNowBadge.textContent,'WAIT');
    assert(nodes.globalEntryBar.classList.contains('waiting'));assert.equal(document.title,'WAIT | EdgeX');
  }else if(name==='unknown'){
    for(const age of [null,undefined,NaN,Infinity,-1,'0']){
      const j=snapshot();j.snapshot_age_seconds=age;
      await render(j);assertStale();assert(!nodes.globalEntryMeta.textContent.includes('NaN'));
    }
    await render(null);assertStale();
    for(const stamp of [undefined,null,NaN,0,now+1]){
      const j=snapshot();j._client_rendered_ms=stamp;await render(j);assertStale();
    }
  }else if(name==='ageing'){
    const j=snapshot(10);await render(j);const stamp=j._client_rendered_ms;
    for(let i=1;i<=5;i++){
      now=stamp+i*10000;refreshGlobalEntryAge();
      assert.equal(entryFreshness(j).age,10+i*10);
      assert.equal(j.snapshot_age_seconds,10);assert.equal(j._client_rendered_ms,stamp);
      assert.strictEqual(globalEntrySnapshot,j);
      assert(nodes.globalEntryMeta.textContent.includes((10+i*10)+'秒前'));
    }
    now=stamp+110000;refreshGlobalEntryAge();assertStale();
    await render(snapshot());assert(nodes.entryNowZone.classList.contains('hasReady'));
  }else if(name==='expired_calculation'){
    let release;global.dashboardReadyRisk=()=>new Promise(resolve=>release=resolve);
    const j=snapshot(119);renderGlobalEntryBar(j);const pending=renderEntryNow(j);
    now+=2000;release({size:1,max_loss:10});await pending;assertStale();
  }else if(name==='expiry_timer'){
    const j=snapshot(30);await render(j);
    const timer=timers.get(entryExpiryTimer);assert.equal(timer.delay,90000);
    now+=90000;timer.fn();assertStale();assert.equal(entryExpiryTimer,null);
    await render(snapshot(119));assert.equal(timers.get(entryExpiryTimer).delay,1000);
  }else if(name==='superseded_calculation'){
    let release;global.dashboardReadyRisk=()=>new Promise(resolve=>release=resolve);
    const old=snapshot();renderGlobalEntryBar(old);const pending=renderEntryNow(old);
    await render(snapshot(0,[]));release({size:1,max_loss:10});await pending;
    assert.equal(nodes.entryNowBadge.textContent,'WAIT');
    assert(!nodes.entryNowContent.innerHTML.includes('entryReadyCard'));
    assert(!nodes.entryNowZone.classList.contains('hasReady'));assert.equal(document.title,'WAIT | EdgeX');
  }else if(name==='heartbeat_failure'){
    const j=snapshot(119);await render(j);now+=2000;
    global.jf=async()=>{throw new Error('offline');};await refreshEntryHeartbeat();assertStale();
    assert.equal(entryHeartbeatBusy,false);
    global.jf=async()=>snapshot();await refreshEntryHeartbeat();
    // The heartbeat schedules the asynchronous card render.
    await Promise.resolve();await Promise.resolve();
    assert(nodes.globalEntryBar.classList.contains('ready'));assert(nodes.entryNowZone.classList.contains('hasReady'));
  }else if(name==='clicks'){
    const j=snapshot(119);await render(j);
    const button=nodes.entryNowContent.querySelectorAll('.entryReadyCard')[0];
    now+=2000;button.onclick.call(button);assertStale();assert.equal(analyzed,0);
    nodes.entryNowRescan.onclick();assert.equal(scanned,1);
    await render(snapshot(119));now+=2000;nodes.globalEntryAction.onclick();assertStale();assert.equal(analyzed,0);
    await render(snapshot());nodes.globalEntryAction.onclick();assert.equal(analyzed,1);
  }else if(name==='visibility'){
    const j=snapshot(119);await render(j);now+=2000;
    let listener;
    document.addEventListener=(name,fn)=>{assert.equal(name,'visibilitychange');listener=fn;};
    const callback=script.match(/document\.addEventListener\("visibilitychange",function\(\)\{[\s\S]*?\}\);/g).find(code=>code.includes('refreshEntryHeartbeat'));
    assert(callback);
    (0,eval)(callback);
    document.visibilityState='hidden';listener();assert(nodes.globalEntryBar.classList.contains('ready'));
    global.jf=async()=>snapshot();document.visibilityState='visible';listener();
    assertStale(); // Expire old READY immediately, before waiting for the network.
    await Promise.resolve();await Promise.resolve();await Promise.resolve();
    assert(nodes.globalEntryBar.classList.contains('ready'));
  }else throw new Error('Unknown test '+name);
}
(async()=>{
  for(const name of (process.argv[3]?[process.argv[3]]:['boundaries','unknown','ageing','expired_calculation','expiry_timer','superseded_calculation','heartbeat_failure','clicks','visibility'])){
    await test(name);console.log('Entry freshness: '+name+' OK');
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
