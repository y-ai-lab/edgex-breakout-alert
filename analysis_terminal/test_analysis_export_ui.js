// Shipped export functions; clipboard/share are local synthetic stubs.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];new Function(script);
let clock=Date.UTC(2026,9,12,19),id=0,copies=[],shares=[],frames=[],focused=null,unavailable=false;const timers=new Map(),nodes={};
for(const m of html.matchAll(/id="([^"]+)"/g)){const name=m[1];nodes[name]={value:'',innerHTML:'',textContent:'',hidden:true,disabled:false,attrs:{},setAttribute(k,v){this.attrs[k]=v},addEventListener(){},getContext:()=>({clearRect(){}}),focus(){focused=name},select(){this.selected=true},setSelectionRange(a,b){this.range=[a,b]}}}
nodes.copyPlan.innerHTML='<span>Copy tile children</span>';nodes.copyPlan.textContent='プランをコピー';nodes.ticker.value='BTC';
const row=(ticker='BTCUSDC')=>({ticker,stage:'READY',direction:'LONG',action:'ENTER',data_age_seconds:0,entry_reference:100,stop_loss:90,take_profit:125,tp1_2r:120,rr:2.5,_client_received_ms:clock});
const ctx={Date:class extends Date{static now(){return clock}},URL,Number,Math,Promise,Object,AbortController,document:{getElementById:k=>nodes[k],visibilityState:'visible',addEventListener(){}},setTimeout(fn,ms){const key=++id;timers.set(key,{fn,ms});return key},clearTimeout:k=>timers.delete(k),setInterval(){},requestAnimationFrame:fn=>frames.push(fn),
 location:{href:'https://terminal.test/?tab=api&token=SYNTHETIC_SECRET&endpoint=PRIVATE#PRIVATE_HASH'},navigator:{clipboard:{writeText:async text=>{copies.push(text);return ctx.clipReply(text)}}},clipReply:async()=>{},lastAnalysis:null,quickRiskVersion:0,manualCalcVersion:0,syncAnalysisWatch(){},renderReadyActionCard(){},clearAnalysisCharts(){},openRiskCalculator(){},alert(){throw Error('Unexpected alert')},
 marketTransportUnavailable:()=>unavailable,marketConnectionMismatched:()=>false,fmt:v=>v==null?'—':String(v),directionJa:String,reasonJa:v=>v||'reason',stageJa:String,updateUrl(){throw Error('URL must not be rewritten by share')}};
vm.createContext(ctx);vm.runInContext(script.slice(script.indexOf('function planAction('),script.indexOf('function reasonJa(')),ctx);vm.runInContext(script.slice(script.indexOf('var analysisRequestVersion='),script.indexOf('document.getElementById("dashScan").onclick=')),ctx);ctx.lastAnalysis=row();
const settle=async()=>{for(let i=0;i<4;i++)await new Promise(setImmediate)},defer=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject}},copy=()=>nodes.copyPlan.onclick(),share=()=>nodes.sharePlan.onclick(),name=process.argv[3];
(async()=>{
 if(name==='snapshot_stamp_and_ready_warning'){
  const r=ctx.lastAnalysis,before=JSON.stringify(r),text=ctx.planText(r);assert(text.includes('分析取得: '));assert(text.includes('JST'));assert(text.includes('取得時点ではREADY'));assert(text.includes('利用時は最新'));assert(text.includes('RR: 2.5（価格差基準・費用未計上）'));assert(text.includes('第1利確 TP1: 120'));assert.equal(JSON.stringify(r),before);
 }else if(name==='nonready_and_invalid_first_target'){
  const r={...ctx.lastAnalysis,stage:'RR_WAIT',action:'SKIP',tp1_2r:130};const text=ctx.planText(r);assert(text.includes('未成立の参考値'));assert(!text.includes('取得時点ではREADY'));assert(!text.includes('第1利確 TP1'));assert(text.includes('最終利確 TP: 125'));assert(ctx.planText({...r,_client_received_ms:undefined}).includes('分析取得: 未確認'));
 }else if(name==='canonical_link_and_private_data'){
  const r={...ctx.lastAnalysis,equity_usdc:987654,size:123456,token:'DO_NOT_COPY',setup_id:'PRIVATE_ID'};const text=ctx.planText(r),url=new URL(ctx.planLink(r));assert.equal(url.pathname,'/');assert.deepEqual([...url.searchParams.keys()],['tab','ticker']);assert.equal(url.searchParams.get('ticker'),'BTCUSDC');assert.equal(url.hash,'');assert(!text.includes('SYNTHETIC_SECRET'));assert(!text.includes('PRIVATE'));assert(!text.includes('987654'));assert(!text.includes('123456'));assert(!text.includes('DO_NOT_COPY'));assert(!ctx.planText(r,false).includes('terminal.test'));
 }else if(name==='invalid_links_not_shared'){
  for(const href of ['javascript:bad','https://user:password@terminal.test/','NOT_A_URL']){ctx.location.href=href;assert.equal(ctx.planLink(ctx.lastAnalysis),null)}ctx.location.href='https://terminal.test/';assert.equal(ctx.planLink({...row(),ticker:'BAD\nKEY'}),null);
 }else if(name==='copy_success_preserves_children'){
  const original=nodes.copyPlan.innerHTML;await copy();assert.equal(copies.length,1);assert.equal(nodes.copyPlan.innerHTML,original);assert.equal(nodes.copyPlan.textContent,'プランをコピー');assert(nodes.analysisExportStatus.textContent.includes('コピーしました'));assert(nodes.analysisCopyFallback.hidden);assert.equal(nodes.copyPlan.attrs['aria-busy'],'false');
 }else if(name==='denied_clipboard_selected_fallback'){
  ctx.clipReply=async()=>{throw Error('NotAllowedError')};await copy();assert(!nodes.analysisCopyFallback.hidden);assert(nodes.analysisCopyText.value.includes('BTCUSDC'));assert.equal(focused,'analysisCopyText');assert(nodes.analysisCopyText.selected);assert.equal(nodes.analysisCopyText.range[1],nodes.analysisCopyText.value.length);assert(nodes.analysisExportStatus.textContent.includes('端末のコピー操作'));assert(!nodes.analysisExportStatus.textContent.includes('コピーしました'));
 }else if(name==='missing_clipboard_and_literal_text'){
  ctx.navigator.clipboard=null;await ctx.exportAnalysisValue(ctx.lastAnalysis,'<img src=x onerror=bad()>',nodes.copyPlan);assert.equal(copies.length,0);assert.equal(nodes.analysisCopyText.value,'<img src=x onerror=bad()>');assert.equal(nodes.analysisCopyFallback.innerHTML,'');
 }else if(name==='clipboard_deadline_retry_late_completion'){
  const d=defer();ctx.clipReply=()=>d.promise;const pending=copy();const timeout=[...timers.values()].find(t=>t.ms===5000);assert(timeout);timeout.fn();await pending;assert(!nodes.analysisCopyFallback.hidden);assert.equal(ctx.analysisExportPending,null);ctx.clipReply=async()=>{};await copy();const good=nodes.analysisExportStatus.textContent;assert(nodes.analysisCopyFallback.hidden);d.resolve();await settle();assert.equal(nodes.analysisExportStatus.textContent,good);assert.equal(timers.size,0);
 }else if(name==='changed_symbol_no_late_fallback'){
  const d=defer();ctx.clipReply=()=>d.promise;const old=copy();ctx.invalidateAnalysisInput();ctx.lastAnalysis=row('ETHUSDC');d.reject(Error('denied'));await old;assert(nodes.analysisCopyFallback.hidden);assert.equal(nodes.analysisCopyText.value,'');assert.equal(nodes.analysisExportStatus.textContent,'');
 }else if(name==='stale_before_copy_no_platform_action'){
  clock+=120000;await copy();assert.equal(copies.length,0);assert.equal(ctx.lastAnalysis,null);assert(nodes.copyPlan.disabled);assert(nodes.analysisCopyFallback.hidden);
 }else if(name==='expiry_during_copy_no_success_or_fallback'){
  const d=defer();ctx.clipReply=()=>d.promise;const pending=copy();clock+=120000;d.reject(Error('denied'));await pending;assert.equal(ctx.lastAnalysis,null);assert.equal(nodes.analysisExportStatus.textContent,'');assert.equal(nodes.analysisCopyText.value,'');
 }else if(name==='disconnect_and_close_clear_selected_text'){
  ctx.clipReply=async()=>{throw Error('denied')};await copy();unavailable=true;ctx.selectAnalysisCopy();assert(ctx.lastAnalysis===null);assert(nodes.analysisCopyFallback.hidden);assert.equal(nodes.analysisCopyText.value,'');unavailable=false;ctx.lastAnalysis=row();await copy();nodes.analysisCopyClose.onclick();assert(nodes.analysisCopyFallback.hidden);assert.equal(nodes.analysisCopyText.value,'');
 }else if(name==='duplicate_copy_and_share_single_flight'){
  const d=defer();ctx.clipReply=()=>d.promise;const pending=copy();await copy();await share();assert.equal(copies.length,1);assert.equal(shares.length,0);d.resolve();await pending;assert.equal(ctx.analysisExportPending,null);
 }else if(name==='native_share_snapshot_and_clean_url'){
  ctx.navigator.share=async data=>shares.push(data);await share();assert.equal(shares.length,1);assert.equal(copies.length,0);assert.equal(shares[0].url,'https://terminal.test/?tab=analysis&ticker=BTCUSDC');assert(!shares[0].text.includes('SYNTHETIC_SECRET'));assert(!shares[0].text.includes('terminal.test'));assert(shares[0].text.includes('JST'));assert(nodes.analysisExportStatus.textContent.includes('共有先へ渡しました'));
 }else if(name==='share_cancel_no_copy'){
  ctx.navigator.share=async()=>{throw Object.assign(Error('cancel'),{name:'AbortError'})};await share();assert.equal(copies.length,0);assert(nodes.analysisExportStatus.textContent.includes('キャンセル'));assert(nodes.analysisCopyFallback.hidden);
 }else if(name==='unavailable_share_copies_full_link_once'){
  await share();assert.equal(copies.length,1);assert.equal((copies[0].match(/https:\/\/terminal.test/g)||[]).length,1);assert(copies[0].includes('最新の分析を開く'));
 }else if(name==='failed_share_manual_copy_fallback'){
  ctx.navigator.share=async()=>{throw Error('share unavailable')};ctx.clipReply=async()=>{throw Error('denied')};await share();assert(!nodes.analysisCopyFallback.hidden);assert(nodes.analysisCopyText.value.includes('?tab=analysis&ticker=BTCUSDC'));assert(!nodes.analysisCopyText.value.includes('SYNTHETIC_SECRET'));
 }else if(name==='failed_share_after_expiry_no_copy'){
  const d=defer();ctx.navigator.share=()=>d.promise;const pending=share();clock+=120000;d.reject(Error('share unavailable'));await pending;assert.equal(copies.length,0);assert(nodes.analysisCopyFallback.hidden);assert.equal(ctx.lastAnalysis,null);
 }else if(name==='old_share_cannot_finish_new_copy'){
  const a=defer(),b=defer();ctx.navigator.share=()=>a.promise;const old=share();ctx.invalidateAnalysisInput();ctx.lastAnalysis=row('ETHUSDC');ctx.clipReply=()=>b.promise;const next=copy();a.resolve();await old;assert.equal(nodes.copyPlan.attrs['aria-busy'],'true');assert.equal(nodes.analysisExportStatus.textContent,'取得時点の参考プランを準備中…');b.resolve();await next;assert(nodes.analysisExportStatus.textContent.includes('ETHUSDC'));
 }else if(name==='numeric_tile_copy_preserves_value'){
  await ctx.exportAnalysisValue(ctx.lastAnalysis,'100.1234',nodes.copyPlan);assert.deepEqual(copies,['100.1234']);assert(!nodes.analysisExportStatus.textContent.includes('エントリー可能'));
 }else if(name==='manual_selection_cannot_extend_freshness'){
  ctx.clipReply=async()=>{throw Error('denied')};await copy();const stamp=ctx.lastAnalysis._client_received_ms;clock+=119999;nodes.analysisCopySelect.onclick();assert.equal(ctx.lastAnalysis._client_received_ms,stamp);clock++;nodes.analysisCopySelect.onclick();assert(nodes.analysisCopyFallback.hidden);assert.equal(nodes.analysisCopyText.value,'');
 }else throw Error(name);
 assert.equal((html.match(/<button\b[^>]*\bdata-tab="/g)||[]).length,4);assert(!ctx.analysisExportPending);console.log(name+': OK');
})().catch(e=>{console.error(e);process.exitCode=1});
