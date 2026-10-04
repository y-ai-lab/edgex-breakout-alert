// Run the complete shipped script against only IDs that exist in its HTML.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),markup=html.split('<script>')[0];
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];new Function(script);
const nodes={},all=[],timers=new Map(),requests=[],storage=new Map();let clock=2_000_000,timerId=0;
storage.set('edgexJournal','[{"ticker":"KEEPUSDC"}]');storage.set('edgexMarketHistory','[{"time_ms":1}]');
function element(tag,attributes=''){
 const attrs=Object.fromEntries(Array.from(attributes.matchAll(/([\w-]+)="([^"]*)"/g),m=>[m[1],m[2]]));
 const classes=new Set((attrs.class||'').split(/\s+/)),dataset={};
 Object.keys(attrs).filter(k=>k.startsWith('data-')).forEach(k=>dataset[k.slice(5)]=attrs[k]);
 let content='',children=[];
 const el={tagName:tag.toUpperCase(),dataset,value:attrs.value||'',textContent:'',disabled:false,open:false,
  classList:{add(...xs){xs.forEach(x=>classes.add(x))},remove(...xs){xs.forEach(x=>classes.delete(x))},contains:x=>classes.has(x),toggle(x,on){on?classes.add(x):classes.delete(x)}},
  addEventListener(){},scrollIntoView(){},click(){if(this.onclick)this.onclick.call(this)},
  querySelectorAll(selector){return children.filter(x=>selector.startsWith('.')&&x.classList.contains(selector.slice(1)))},
  querySelector(selector){return this.querySelectorAll(selector)[0]||null},
  get children(){return children},get innerHTML(){return content},set innerHTML(value){
   content=String(value);children=Array.from(content.matchAll(/<(button|td|input|div)\b([^>]*)>/g),m=>element(m[1],m[2]));
   children.forEach(x=>{if(x.id)nodes[x.id]=x});
  }};
 el.id=attrs.id;if(el.id)nodes[el.id]=el;return el;
}
for(const m of markup.matchAll(/<([a-z]+)\b([^>]*)>/g))all.push(element(m[1],m[2]));
for(const id of ['stage','direction'])nodes[id].value='ALL';
assert.equal(all.filter(x=>x.dataset.tab).length,4);
assert.deepEqual(all.filter(x=>x.dataset.tab).map(x=>x.dataset.tab),['dashboard','screen','analysis','api']);
for(const id of ['compare','journal','savePlan','compareSelected','strategyComparisonStatus','readinessHistoryStatus'])assert(!nodes[id],id);
for(const id of ['entryNowZone','nearCandidates','paperExecutionBody','apiCheckResults','backgroundPush','equity','riskPct'])assert(nodes[id],id);
const ready={ticker:'TESTUSDC',stage:'READY',direction:'LONG',score:90,rr:2.5,entry_reference:100,stop_loss:90,take_profit:125,latest_15m_time_ms:900000,setup_id:'ready',action:'ENTER',data_age_seconds:0};
ready.entry_band={version:1,status:'COMPATIBLE',direction:'LONG',min_rr:2,structural_stop:90,structural_target:125,confirmation_level:99,rr_boundary:90+35/3,rr_entry_band:{lower:90,upper:90+35/3},compatible_entry_band:{lower:99,upper:90+35/3}};
const near={...ready,ticker:'NEXTUSDC',stage:'CONFIRMATION_WAIT',setup_id:'next',reason:'retest seen; waiting for 15M confirmation'};
const market={universe:2,scanned:2,coverage_pct:100,matched:2,snapshot_age_seconds:0,results:[ready,near],ready_candidates:[ready],qualified_near_candidates:[near],summary:{ready_count:1,qualified_near_count:1,near_signal_count:1,direction_counts:{LONG:2},average_score:90}};
const paper={mode:'PAPER_ONLY',real_orders_enabled:false,eligible_for_live_promotion:false,automatic_promotion:false,source:'CURRENT_READY_ONLY',account:{last_cycle_ms:clock,paused:false,collector_stale:false,cash_usdc:10000,policy:{}},tracking:{state:'WAITING_READY',active_status_counts:{PENDING:0,OPEN:0,AMBIGUOUS:0},data_issue_count:0,data_issue_counts:{}},metrics:{resolved:0,status_counts:{},sample_status:'INSUFFICIENT SAMPLE'},latest:[]};
let failures=false,invalid=false,stale=false,releaseOld=null;
const context={console,URL,AbortController,Uint32Array,crypto:require('crypto').webcrypto,
 Date:class extends Date{static now(){return clock}},navigator:{userAgent:'test',vibrate(){}},
 location:{href:'https://test/',origin:'https://test'},history:{replaceState(){}},
 localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,String(v)),removeItem:k=>storage.delete(k)},
 setTimeout(fn,delay){const id=++timerId;timers.set(id,{fn,delay});return id},clearTimeout:id=>timers.delete(id),setInterval(){},
 requestAnimationFrame(){},alert(){throw new Error('Unexpected alert')},
 document:{title:'',visibilityState:'visible',getElementById:id=>nodes[id]||null,addEventListener(){},createElement:tag=>element(tag),
  querySelector(selector){return this.querySelectorAll(selector)[0]||null},
  querySelectorAll(selector){if(selector==='[data-tab]')return all.filter(x=>x.dataset.tab);if(selector.startsWith('[data-tab='))return all.filter(x=>x.dataset.tab===selector.match(/"([^"]+)"/)[1]);if(selector.startsWith('.'))return all.filter(x=>x.classList.contains(selector.slice(1)));return []}},
 fetch:async(url,options={})=>{
  requests.push({url,method:options.method||'GET'});
  if(releaseOld&&url==='/health'){const wait=releaseOld;releaseOld=null;await wait.promise;return {ok:true,json:async()=>({ok:false})}}
  if(failures&&url.startsWith('/api/analyze'))throw new Error('offline');
  let data;
  if(url==='/health')data=invalid?{}:{ok:true,version:'19.0.17',storage:{db_exists:true}};
  else if(url.startsWith('/api/screener'))data={...market,snapshot_age_seconds:stale?120:0};
  else if(url.startsWith('/api/analyze'))data={...ready,ticker:'BTCUSDC',data_age_seconds:stale?900:0};
  else if(url.startsWith('/api/push/config'))data={enabled:true,notification_policy:'READY_ONLY',daily_summary_enabled:false};
  else if(url.startsWith('/api/push/events'))data={events:[]};
  else if(url.startsWith('/api/paper-execution'))data=paper;
  else if(url.startsWith('/api/watchlist'))data={tickers:[]};
  else if(url.startsWith('/api/risk'))data={size:1,max_loss:10,target_profit:25,risk_budget:10,notional:100,rr:2.5};
  else if(url.startsWith('/api/chart'))data={analysis:{...ready,ticker:'TESTUSDC'},series:{HOUR_4:[],MINUTE_15:[]}};
  else throw new Error('Unexpected API load: '+url);
  return {ok:true,json:async()=>data};
 }};
context.window={...context,matchMedia:()=>({matches:false}),addEventListener(){},devicePixelRatio:1};
vm.createContext(context);vm.runInContext(script,context);
async function settle(){for(let i=0;i<8;i++)await new Promise(setImmediate)}
(async()=>{
 await settle();const initial=Array.from(timers.values()).find(t=>t.delay===80);assert(initial);initial.fn();await settle();
 assert(nodes.entryNowContent.innerHTML.includes('TESTUSDC'));assert(nodes.nearCandidates.innerHTML.includes('NEXTUSDC'));
 assert(nodes.nearCandidates.innerHTML.includes('まだ待つ'));assert(nodes.nearCandidates.innerHTML.includes('15分足の確認待ち'));
 assert(nodes.nearCandidates.innerHTML.includes('Entry目安'));assert(nodes.nearCandidates.innerHTML.includes('SL'));assert(nodes.nearCandidates.innerHTML.includes('TP'));
 assert.equal(storage.get('edgexJournal'),'[{"ticker":"KEEPUSDC"}]');assert.equal(storage.get('edgexMarketHistory'),'[{"time_ms":1}]');
 assert(!requests.some(r=>/shadow-v2|strategy-comparison|replay|readiness-history|server-history|server-paper-signals|opportunity/.test(r.url)));
 context.tab('api');await settle();assert(nodes.api.classList.contains('active'));assert(nodes.apiCheckStatus.textContent.includes('正常 5 / 要確認 0 / 失敗 0'));assert(nodes.paperExecutionStatus.textContent.includes('PAPER ONLY'));
 assert(nodes.paperExecutionStatus.textContent.includes('現行READY待ち'));
 const originalTracking=paper.tracking;
 for(const [state,text,warn] of [['WAITING_FILL','模擬約定待ち',false],['TRACKING','追跡中',false],['DATA_INCOMPLETE','履歴復旧待ち',true],['PAUSED','模擬口座停止',true],['AMBIGUOUS','損益未確定',true],['NOT_STARTED','初回更新待ち',true],['COLLECTOR_STALE','更新を確認',true]]){
  paper.tracking={...originalTracking,state};
  context.renderPaperExecution(paper);await context.checkApiConnections();
  assert(nodes.paperExecutionStatus.textContent.includes(text),state);
  assert(nodes.apiCheckResults.innerHTML.includes(text),state);
  assert(nodes.apiCheckStatus.textContent.includes(warn?'正常 4 / 要確認 1':'正常 5 / 要確認 0'),state);
 }
 // The single displayed rejection does not hide an older active order's gap.
 paper.tracking={...originalTracking,state:'DATA_INCOMPLETE',data_issue_count:1,data_issue_counts:{HISTORY_GAP:1},active_status_counts:{PENDING:0,OPEN:1,AMBIGUOUS:0}};
 paper.latest=[{ticker:'NEWUSDC',status:'REJECTED',reason:'ACTIVE_POSITION_DATA_INCOMPLETE'}];
 context.renderPaperExecution(paper);await context.checkApiConnections();
 assert(nodes.apiCheckStatus.textContent.includes('要確認 1'));assert(nodes.paperExecutionSummary.innerHTML.includes('1件'));assert(nodes.paperExecutionStatus.classList.contains('warn'));
 for(const bad of [null,{...originalTracking,state:'UNKNOWN'},{...originalTracking,data_issue_count:-1},{...originalTracking,active_status_counts:{PENDING:0,OPEN:null,AMBIGUOUS:0}}]){
  paper.tracking=bad;context.renderPaperExecution(paper);await context.checkApiConnections();assert(nodes.apiCheckStatus.textContent.includes('要確認 1'));assert(nodes.paperExecutionStatus.textContent.includes('状態を確認'));
 }
 paper.tracking=originalTracking;paper.latest=[];context.renderPaperExecution(paper);assert(!nodes.paperExecutionStatus.classList.contains('warn'));
 const diagnostic=requests.filter(r=>['/health','/api/analyze?ticker=BTCUSDC','/api/paper-execution?limit=1'].includes(r.url));assert(diagnostic.length>=3);assert(diagnostic.every(r=>r.method==='GET'));
 failures=true;await context.checkApiConnections();assert(nodes.apiCheckStatus.textContent.includes('失敗 1'));assert(nodes.apiCheckResults.innerHTML.includes('取得・応答内容を確認できません'));assert(!nodes.checkApi.disabled);
 failures=false;invalid=true;await context.checkApiConnections();assert(nodes.apiCheckStatus.textContent.includes('失敗 1'));invalid=false;
 stale=true;await context.checkApiConnections();assert(nodes.apiCheckStatus.textContent.includes('要確認 2'));stale=false;
 let release;releaseOld={promise:new Promise(r=>release=r)};const old=context.checkApiConnections();await settle();await context.checkApiConnections();release();await old;assert(nodes.apiCheckStatus.textContent.includes('正常 5 / 要確認 0 / 失敗 0'));
 const button=nodes.nearCandidates.querySelectorAll('.nearCandidateCard')[0];clock+=120000;button.click();assert(nodes.nearCandidates.innerHTML.includes('データ鮮度を確認'));assert(!requests.some(r=>r.url.startsWith('/api/chart')));
 context.refreshGlobalEntryAge();assert(nodes.dashSummary.innerHTML.includes('鮮度を確認'));assert(!nodes.dashSummary.innerHTML.includes('エントリー可能'));
 await context.scan(false);assert(nodes.nearCandidates.innerHTML.includes('NEXTUSDC'));const fresh=nodes.nearCandidates.querySelectorAll('.nearCandidateCard')[0];fresh.click();await settle();assert(nodes.analysis.classList.contains('active'));assert(nodes.analysisStatus.innerHTML.includes('エントリー可能'));
 assert(nodes.analysisOut.innerHTML.includes('確認価格とRRの両立'));assert(nodes.analysisOut.innerHTML.includes('両立する範囲'));assert(nodes.analysisOut.innerHTML.includes('エントリー可否は上の状態'));
 context.openRiskCalculator();assert(nodes.risk.open);assert(nodes.analysis.classList.contains('active'));
 context.tab('journal');assert(nodes.dashboard.classList.contains('active'));context.tab('compare');assert(nodes.dashboard.classList.contains('active'));
 // Market-derived strings are escaped, including candidate HTML attributes.
 context.renderNearCandidates({...market,_client_rendered_ms:clock,qualified_near_candidates:[{...near,ticker:'<img src=x onerror=bad()>',reason:'<script>bad()</script>'}]});
 assert(!nodes.nearCandidates.innerHTML.includes('<img'));assert(!nodes.nearCandidates.innerHTML.includes('<script>bad'));
 nodes.checkApi.click();await settle();assert(nodes.apiCheckStatus.textContent.includes('正常 5'));
 console.log('Complete UI boot, four tabs, candidates, stale rejection, analysis/risk, GET API checks, failures/races and retained local data: OK');
})().catch(e=>{console.error(e);process.exitCode=1});
