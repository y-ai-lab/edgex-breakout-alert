// Shipped chart lifecycle with fixed clock, canvas geometry and queued frames.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),script=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];new Function(script);
const kind=process.argv[3];let clock=1800000000000,reads=[],observed=[],frames=[],contextOps=[];
const nodes={},documentListeners={},windowListeners={};
function canvas(id){const ctx=new Proxy({},{get(_,key){return(...args)=>{contextOps.push({id,key,args});for(const n of args)if(typeof n==='number')assert(Number.isFinite(n),key)}}});return {id,clientWidth:340,clientHeight:240,width:300,height:150,listeners:{},addEventListener(n,fn){this.listeners[n]=fn},getContext:()=>ctx,getBoundingClientRect(){return {left:20,width:this.clientWidth}}}}
for(const id of ['chart4h','chart15m']){nodes[id]=canvas(id);nodes[id+'Info']={textContent:'',innerHTML:''};nodes[id+'Latest']={disabled:true}}
const c={Number,Math,Date:class extends Date{static now(){return clock}},window:{devicePixelRatio:2,addEventListener:(n,f)=>windowListeners[n]=f},document:{visibilityState:'visible',getElementById:id=>nodes[id],addEventListener:(n,f)=>documentListeners[n]=f},
 ResizeObserver:class {constructor(fn){this.fn=fn;reads.push(this)}observe(el){observed.push(el.id)}},requestAnimationFrame:fn=>frames.push(fn),lastAnalysis:null,
 fmt:v=>String(v),lifecycleEscape:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'),
 refreshAnalysisAge(){if(c.lastAnalysis&&!c.analysisFresh(c.lastAnalysis)){c.clearAnalysisCharts();c.lastAnalysis=null}},
};vm.createContext(c);
vm.runInContext(script.slice(script.indexOf('var analysisChartSnapshot='),script.indexOf('async function copyTextValue(')),c);
vm.runInContext(script.slice(script.indexOf('function analysisFresh('),script.indexOf('function clearAnalysisView(')),c);
const bar=(i,step=900000)=>({time_ms:clock-10*step+i*step,open:100+i,high:103+i,low:99+i,close:102+i,ema20:101+i,ema50:100+i});
const pack=()=>({series:{HOUR_4:[bar(0,14400000),bar(1,14400000),bar(2,14400000)],MINUTE_15:[bar(0),bar(1),bar(2)]},levels:{entry:100,stop:95,target:120}});
function remember(p=pack()){c.lastAnalysis={_client_received_ms:clock,data_age_seconds:0,ticker:'TESTUSDC'};c.rememberAnalysisCharts(c.lastAnalysis,p);return p}
function flush(){const q=frames.splice(0);q.forEach(fn=>fn())}
if(kind==='resize_dpr'){
 const p=remember(),original=JSON.stringify(p);c.redrawAnalysisCharts();assert.equal(nodes.chart4h.width,680);assert.equal(nodes.chart4h.height,480);nodes.chart4h.clientWidth=450;c.window.devicePixelRatio=3;c.scheduleAnalysisChartRedraw();flush();assert.equal(nodes.chart4h.width,1350);assert.equal(nodes.chart4h.height,720);assert.equal(JSON.stringify(p),original);assert.equal(c.lastAnalysis._client_received_ms,clock);
}else if(kind==='hidden_then_visible'){
 remember();nodes.chart4h.clientWidth=0;c.redrawAnalysisCharts();assert.equal(nodes.chart4h.width,300);nodes.chart4h.clientWidth=340;c.scheduleAnalysisChartRedraw();flush();assert.equal(nodes.chart4h.width,680);assert(contextOps.some(x=>x.id==='chart4h'&&x.key==='fillRect'));
}else if(kind==='coalesced_frames'){
 remember();for(let i=0;i<20;i++)c.scheduleAnalysisChartRedraw();assert.equal(frames.length,1);flush();assert.equal(c.analysisChartFramePending,false);c.scheduleAnalysisChartRedraw();assert.equal(frames.length,1);
}else if(kind==='expiry_never_redraws'){
 remember();c.redrawAnalysisCharts();contextOps=[];c.scheduleAnalysisChartRedraw();clock+=120000;flush();assert.equal(c.analysisChartSnapshot,null);assert(nodes.chart4hLatest.disabled);assert(!contextOps.some(x=>x.key==='fillRect'));assert.equal(nodes.chart4hInfo.textContent,'分析すると足の値が表示されます。');
}else if(kind==='superseded_and_disconnect'){
 remember();c.scheduleAnalysisChartRedraw();c.lastAnalysis={_client_received_ms:clock,data_age_seconds:0};flush();assert.equal(c.analysisChartSnapshot,null);remember();c.marketTransportUnavailable=()=>true;c.marketConnectionMismatched=()=>false;c.scheduleAnalysisChartRedraw();flush();assert.equal(c.analysisChartSnapshot,null);assert(nodes.chart15mLatest.disabled);
}else if(kind==='selection_and_latest'){
 remember();c.bindAnalysisCharts();assert(nodes.chart15mInfo.innerHTML.includes('取得時点の最新足'));c.selectAnalysisChartAt('chart15m',{clientX:30});assert.equal(c.analysisChartSelections.chart15m,0);assert(nodes.chart15mInfo.innerHTML.includes('始値 100'));assert(nodes.chart15mInfo.innerHTML.includes('終値 102'));assert(nodes.chart15mInfo.innerHTML.includes('選択した過去の足'));nodes.chart15mLatest.onclick();assert.equal(c.analysisChartSelections.chart15m,2);assert(nodes.chart15mInfo.innerHTML.includes('終値 104'));
}else if(kind==='keyboard_and_scope'){
 remember();c.bindAnalysisCharts();let prevented=0;const key=k=>nodes.chart15m.listeners.keydown({key:k,preventDefault(){prevented++}});key('Home');assert.equal(c.analysisChartSelections.chart15m,0);key('ArrowLeft');assert.equal(c.analysisChartSelections.chart15m,0);key('ArrowRight');assert.equal(c.analysisChartSelections.chart15m,1);key('End');assert.equal(c.analysisChartSelections.chart15m,2);assert.equal(c.analysisChartSelections.chart4h,2);assert.equal(prevented,4);assert.deepEqual(observed,['chart4h','chart15m']);c.document.visibilityState='hidden';documentListeners.visibilitychange();assert.equal(frames.length,0);c.document.visibilityState='visible';documentListeners.visibilitychange();windowListeners.resize();assert.equal(frames.length,1);
}else if(kind==='invalid_data_fail_closed'){
 for(const change of [v=>v.high=99,v=>v.low=105,v=>v.close=NaN,v=>v.open=-1,v=>v.time_ms=null]){const p=pack();change(p.series.MINUTE_15[1]);remember(p);assert(nodes.chart15mLatest.disabled);assert(nodes.chart15mInfo.textContent.includes('確認できません'));contextOps=[];assert.equal(c.drawChart('chart15m',p.series.MINUTE_15,p.levels,'15M'),false);assert(!contextOps.some(x=>x.id==='chart15m'&&x.key==='fillRect'))}
 for(const s of [[bar(1),bar(0)],[bar(0),bar(0)],[],null])assert(!c.chartSeriesValid(s));
}else if(kind==='time_gap_kept'){
 const p=pack();p.series.MINUTE_15[2]=bar(4);remember(p);assert(nodes.chart15mInfo.innerHTML.includes('時刻に空白'));c.redrawAnalysisCharts();const candles=contextOps.filter(x=>x.id==='chart15m'&&x.key==='fillRect');assert.equal(candles.length,3);assert((candles[2].args[0]-candles[1].args[0])>(candles[1].args[0]-candles[0].args[0])*2.9);const old=JSON.stringify(p);c.selectAnalysisChart('chart15m',1);assert.equal(JSON.stringify(p),old);
}else if(kind==='invalid_levels_and_flat_prices'){
 const p=pack();p.levels={entry:NaN,stop:Infinity,target:-1};for(const s of Object.values(p.series))for(const v of s)Object.assign(v,{open:.001,high:.001,low:.001,close:.001,ema20:null,ema50:NaN});remember(p);assert.equal(c.drawChart('chart15m',p.series.MINUTE_15,p.levels,'15M'),true);assert(contextOps.filter(x=>x.key==='fillText').every(x=>!String(x.args[0]).includes('NaN')&&!String(x.args[0]).includes('Infinity')));
}else if(kind==='escaped_readout'){
 remember();c.fmt=()=>'<img src=x>';c.renderChartReadout('chart15m');assert(nodes.chart15mInfo.innerHTML.includes('&lt;img'));assert(!nodes.chart15mInfo.innerHTML.includes('<img'));
}else if(kind==='dpr_listener'){
 const queries=[];c.window.matchMedia=query=>{const m={query,addEventListener(n,fn,opt){assert.equal(n,'change');assert(opt.once);this.fn=fn}};queries.push(m);return m};remember();c.bindAnalysisCharts();assert.equal(queries[0].query,'(resolution: 2dppx)');c.window.devicePixelRatio=3;queries[0].fn();assert.equal(frames.length,1);assert.equal(queries[1].query,'(resolution: 3dppx)');flush();assert.equal(nodes.chart15m.width,1020);
}else if(kind==='label_spacing'){
 const p=pack();p.levels={entry:104,stop:103.99,target:104.01,tp1:104.005,breakout:104.002,support:103.98,resistance:104.012};remember(p);contextOps=[];c.drawChart('chart15m',p.series.MINUTE_15,p.levels,'15M');const labels=contextOps.filter(x=>x.id==='chart15m'&&x.key==='fillText').slice(0,-2),ys=labels.map(x=>x.args[2]);assert.equal(labels.length,12);for(let i=1;i<ys.length;i++)assert(ys[i]-ys[i-1]>=12.9);assert(ys[0]>=17&&ys.at(-1)<=214);assert.equal(p.levels.entry,104);assert.equal(p.levels.target,104.01);assert.notEqual(c.chartPrice(1e-12),'0');
}else if(kind==='clear_queued_frame'){
 remember();c.scheduleAnalysisChartRedraw();c.clearAnalysisCharts();contextOps=[];flush();assert.equal(c.analysisChartSnapshot,null);assert.equal(contextOps.length,0);assert(nodes.chart4hLatest.disabled);
}else throw Error(kind);
assert.equal((html.match(/<button\b[^>]*\bdata-tab="/g)||[]).length,4);console.log(kind+': OK');
