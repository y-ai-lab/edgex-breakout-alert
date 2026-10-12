// Ship-code tests: stale lists, debounced filters, safe buttons and reset state.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];new Function(script);
let now=1000000,timer=0,scans=0,analyzes=0,saves=0;const timers=new Map(),nodes={};
function element(id){let content='',writes=0,buttons=[];return {id,value:'',hidden:false,textContent:'',listeners:{},addEventListener(k,f){this.listeners[k]=f},get writes(){return writes},get innerHTML(){return content},set innerHTML(h){content=h;writes++;buttons=[...h.matchAll(/<button\b([^>]*)>/g)].map(m=>{const a=Object.fromEntries([...m[1].matchAll(/([\w-]+)="([^"]*)"/g)].map(x=>[x[1],x[2]]));return {dataset:{marketAnalyze:a['data-market-analyze'],marketWatch:a['data-market-watch']},click(){this.onclick()},focus(){}}})},querySelectorAll(sel){return buttons.filter(x=>sel==='[data-market-watch]'?x.dataset.marketWatch:sel==='[data-market-analyze]'?x.dataset.marketAnalyze:false)}}}
for(const id of ['screenBody','marketCards','screenEmpty','screenFreshness','summary','q','stage','direction','minScore','minRR','ticker','scan','force','autoScan','presetNear','presetReady','presetReset','watchOnly'])nodes[id]=element(id);
nodes.stage.value=nodes.direction.value='ALL';nodes.minScore.value='0';
const e=v=>String(v==null?'—':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const row={ticker:'BTCUSDC',stage:'READY',direction:'LONG',score:80,current_price:100,change_24h_pct:1,volume_ratio:2,rr:2.5,reason:'ALL_READY',data_age_seconds:0};
const ctx={console,Date:class extends Date{static now(){return now}},document:{getElementById:id=>nodes[id]},rows:[row],watch:new Set(),showWatch:false,lastMarketPayload:{snapshot_age_seconds:0,_client_rendered_ms:now},globalEntrySnapshot:{snapshot_age_seconds:0,_client_rendered_ms:now},
 entryFreshness(j){if(!j||!Number.isFinite(j.snapshot_age_seconds)||!j._client_rendered_ms)return {stale:true,age:null};let age=j.snapshot_age_seconds+(now-j._client_rendered_ms)/1000;return {stale:age>=120,age}},lifecycleEscape:e,card:(k,v)=>k+':'+v,fmt:v=>v==null?'—':String(v),pct:String,stageJa:String,directionJa:String,planStage:r=>r.stage,planReason:r=>r.reason,planAction:r=>r.stage==='READY'?'ENTER':'WAIT',actionJa:String,
 setTimeout(fn,ms){let id=++timer;timers.set(id,{fn,ms});return id},clearTimeout:id=>timers.delete(id),scan(){scans++},saveWatch(){saves++},tab:n=>ctx.currentTab=n,analyze(){analyzes++}}
vm.createContext(ctx);vm.runInContext(script.slice(script.indexOf('function summaryView('),script.indexOf('function syncAutoJournalButton(')),ctx);
ctx.marketRowsFilterKey=ctx.marketFilterKey();ctx.renderRows();
const testcase=process.argv[3];
if(testcase==='stale_list'){
 assert(nodes.marketCards.innerHTML.includes('isReady'));now+=119999;ctx.renderRows();assert(nodes.marketCards.innerHTML.includes('isReady'));now++;ctx.renderRows();assert(!nodes.marketCards.innerHTML.includes('isReady'));assert(nodes.marketCards.innerHTML.includes('isStale'));assert(!nodes.screenBody.innerHTML.includes('badge READY'));assert(nodes.screenFreshness.textContent.includes('鮮度'));ctx.globalEntrySnapshot._client_rendered_ms=now;ctx.renderRows();assert(!nodes.marketCards.innerHTML.includes('isReady'),'Fresh global ENTRY must not refresh an old filtered list');ctx.lastMarketPayload._client_rendered_ms=now;ctx.renderRows();assert(nodes.marketCards.innerHTML.includes('isReady'));
}else if(testcase==='stale_summary'){
 ctx.summaryView({ready_count:1,near_signal_count:0,direction_counts:{},average_score:1});assert(nodes.summary.innerHTML.includes('エントリー可能'));now+=120000;ctx.summaryView({ready_count:1});assert(!nodes.summary.innerHTML.includes('エントリー可能'));assert(nodes.summary.innerHTML.includes('鮮度を確認'));
}else if(testcase==='unchanged_dom'){
 const before=[nodes.screenBody.writes,nodes.marketCards.writes];now+=10000;ctx.renderRows();assert.deepEqual([nodes.screenBody.writes,nodes.marketCards.writes],before,'Age ticks cannot rebuild unchanged tables/cards');assert(nodes.screenFreshness.textContent.includes('10秒'));
}else if(testcase==='debounced_filter'){
 nodes.q.value='B';nodes.q.listeners.input();nodes.q.value='BT';nodes.q.listeners.input();nodes.q.value='BTC';nodes.q.listeners.input();assert.equal(timers.size,1);assert.equal(scans,0);assert.equal(nodes.marketCards.innerHTML,'');assert(nodes.screenFreshness.textContent.includes('絞り込み'));const t=[...timers.values()][0];assert.equal(t.ms,300);t.fn();assert.equal(scans,1);ctx.marketRowsFilterKey=ctx.marketFilterKey();ctx.renderRows();assert(nodes.marketCards.innerHTML.includes('BTCUSDC'));
}else if(testcase==='enter_and_change'){
 nodes.q.value='ETH';nodes.q.listeners.input();nodes.q.listeners.keydown({key:'Enter'});assert.equal(scans,1);assert.equal(timers.size,0);nodes.stage.value='READY';nodes.stage.listeners.change();assert.equal(timers.size,1);assert.equal(nodes.marketCards.innerHTML,'');
}else if(testcase==='favorite_and_analyze'){
 nodes.marketCards.querySelectorAll('[data-market-watch]')[0].click();assert(ctx.watch.has('BTCUSDC'));assert.equal(saves,1);assert(nodes.marketCards.innerHTML.includes('aria-pressed="true"'));nodes.marketCards.querySelectorAll('[data-market-analyze]')[0].click();assert.equal(nodes.ticker.value,'BTCUSDC');assert.equal(ctx.currentTab,'analysis');assert.equal(analyzes,1);
}else if(testcase==='reset_favorites'){
 const a=script.indexOf('document.getElementById("presetReset").onclick=');vm.runInContext(script.slice(a,script.indexOf('\ndocument.getElementById("watchOnly").onclick=',a)),ctx);
 ctx.showWatch=true;ctx.renderRows();assert(!nodes.screenEmpty.hidden);nodes.presetReset.onclick();assert.equal(ctx.showWatch,false);assert.equal(nodes.q.value,'');assert.equal(nodes.watchOnly.textContent,'★のみ');assert.equal(scans,1);assert(nodes.marketCards.innerHTML.includes('BTCUSDC'));
}else if(testcase==='escaped_rows'){
 ctx.rows=[{...row,ticker:'<img src=x onerror=bad()>',reason:'<script>bad()</script>',stage:'READY" onmouseover="bad'}];ctx.renderRows();for(const n of [nodes.screenBody,nodes.marketCards]){assert(!n.innerHTML.includes('<img'));assert(!n.innerHTML.includes('<script>'));assert(!n.innerHTML.includes('onmouseover="bad'));assert(n.innerHTML.includes('&lt;img'))}
}else throw Error(testcase);
assert.equal((html.match(/<button\b[^>]*\bdata-tab="/g)||[]).length,4);console.log(testcase+': OK');
