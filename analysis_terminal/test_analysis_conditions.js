const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[2],'utf8'),kind=process.argv[3];
const script=[...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
let now=1800000000000,saved=0,redrawn=0;
const nodes={entryCriteria:{innerHTML:'',dataset:{minRr:'2'}},analysisWatch:{disabled:true,attributes:{},setAttribute(k,v){this.attributes[k]=v}}};
class Clock extends Date {static now(){return now}}
const c={Number,Date:Clock,Set,document:{getElementById:id=>nodes[id]},lastAnalysis:null,watch:new Set(),fmt:v=>String(v),directionJa:v=>v,
 lifecycleEscape:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
 saveWatch(){saved++},renderRows(){redrawn++},refreshAnalysisAge(){if(c.lastAnalysis&&!c.analysisFresh(c.lastAnalysis)){c.lastAnalysis=null;c.renderAnalysisCriteria(null);c.syncAnalysisWatch()}}};
vm.createContext(c);
vm.runInContext(script.slice(script.indexOf('function reviewEntryConditions('),script.indexOf('var analysisRequestVersion=')),c);
vm.runInContext(script.split('\n').find(line=>line.startsWith("document.getElementById('analysisWatch').onclick=")),c);
vm.runInContext(script.slice(script.indexOf('function analysisFresh('),script.indexOf('function clearAnalysisView(')),c);
const base=()=>({ticker:'TESTUSDC',direction:'LONG',trend:'UP',stage:'READY',action:'ENTER',breakout_level:100,breakout_time_ms:now-14400000,retest_touched:true,confirmation_color_ok:true,confirmation_level_ok:true,confirmed:true,confirmation_latest_open:99,confirmation_latest_close:100,entry_reference:100,stop_loss:95,take_profit:110,rr:2,data_age_seconds:0,_client_received_ms:now});
const review=(r,m=2)=>c.reviewEntryConditions(r,m),check=(r,k,m=2)=>review(r,m).checks.find(x=>x.key===k);
if(kind==='long_short'){
 for(const side of ['LONG','SHORT']){const r=base();if(side==='SHORT')Object.assign(r,{direction:side,trend:'DOWN',stop_loss:105,take_profit:90});const before=JSON.stringify(r);assert.equal(review(r).passed,6);assert.equal(review(r).checks.length,6);assert.equal(JSON.stringify(r),before);assert.equal(review(r).changes_ready_rules,false)}
}else if(kind==='independent_failures'){
 const r={...base(),stage:'CONFIRMATION_WAIT',action:'WAIT FOR 15M CLOSE',confirmation_color_ok:false,confirmed:false,rr:1.99};const result=review(r);assert.equal(result.failed,2);assert.equal(check(r,'confirmation').state,'FAIL');assert.equal(check(r,'rr').state,'FAIL');assert.equal(check(r,'structure').state,'PASS');assert(check(r,'confirmation').detail.includes('足方向 未達 / 終値の水準突破 成立'));
 r.confirmation_color_ok=true;r.confirmation_level_ok=false;assert.equal(check(r,'confirmation').state,'FAIL');r.retest_touched=false;assert.equal(check(r,'retest').state,'FAIL');
}else if(kind==='unknown_confirmation'){
 for(const edits of [{confirmation_color_ok:null},{confirmation_level_ok:'true'},{confirmed:false},{confirmation_color_ok:true,confirmation_level_ok:false,confirmed:true}])assert.equal(check({...base(),...edits},'confirmation').state,'UNKNOWN');
}else if(kind==='invalid_structure'){
 for(const side of ['LONG','SHORT']){const r={...base(),direction:side,stop_loss:null,take_profit:null,stage:'CONFIRMATION_WAIT',entry_band:{version:1,direction:side,structural_stop:side==='LONG'?101:99,structural_target:side==='LONG'?110:90}};assert.equal(check(r,'structure').state,'FAIL');assert.equal(check(r,'rr').state,'UNKNOWN');r.entry_band.direction=side==='LONG'?'SHORT':'LONG';assert.equal(check(r,'structure').state,'UNKNOWN')}
 assert.equal(check({...base(),stop_loss:100},'structure').state,'FAIL');assert.equal(check({...base(),take_profit:100},'structure').state,'FAIL');
}else if(kind==='early_stages'){
 for(const stage of ['DATA_WAIT','TREND_WAIT','BREAKOUT_WAIT']){const r={...base(),stage};const result=review(r);assert(result.checks.slice(2).every(x=>x.state==='UNEVALUATED'));assert.equal(check(r,'breakout').state,stage==='DATA_WAIT'?'UNKNOWN':stage==='TREND_WAIT'?'UNEVALUATED':'FAIL')}
 assert.equal(check({...base(),stage:'TREND_WAIT',trend:'NEUTRAL',direction:'NONE'},'trend').state,'FAIL');assert.equal(check({...base(),trend:null},'trend').state,'UNKNOWN');
}else if(kind==='minimum_and_numbers'){
 assert.equal(check(base(),'rr',2).state,'PASS');assert.equal(check(base(),'rr',2.01).state,'FAIL');
 for(const v of [null,undefined,NaN,Infinity,-1,'2',true])assert.equal(check({...base(),rr:v},'rr').state,'UNKNOWN');
 for(const m of [null,0,NaN,Infinity,-1,'2',true])assert.equal(check(base(),'rr',m).state,'UNKNOWN');
 assert.equal(check({...base(),breakout_time_ms:1.5},'breakout').state,'UNKNOWN');assert.equal(check({...base(),stop_loss:Infinity},'structure').state,'UNKNOWN');
}else if(kind==='render_and_expiry'){
 const r={...base(),stage:'CONFIRMATION_WAIT',action:'WAIT FOR 15M CLOSE'};c.renderAnalysisCriteria(r);assert.equal((nodes.entryCriteria.innerHTML.match(/<li class="conditionCheck/g)||[]).length,6);assert(nodes.entryCriteria.innerHTML.includes('成立 6 / 6'));assert.equal(r.stage,'CONFIRMATION_WAIT');assert.equal(r.action,'WAIT FOR 15M CLOSE');assert(!nodes.entryCriteria.innerHTML.includes('ENTER'));now+=120000;c.renderAnalysisCriteria(r);assert.equal(nodes.entryCriteria.innerHTML,'');
}else if(kind==='escaping'){
 c.directionJa=()=>'<img src=x>';c.fmt=()=>'<script>bad()</script>';c.renderAnalysisCriteria(base());assert(nodes.entryCriteria.innerHTML.includes('&lt;script&gt;'));assert(!nodes.entryCriteria.innerHTML.includes('<script>'));assert(!nodes.entryCriteria.innerHTML.includes('<img'));
}else if(kind==='favorite_scope'){
 c.syncAnalysisWatch();assert(nodes.analysisWatch.disabled);nodes.analysisWatch.onclick();assert.equal(saved,0);c.lastAnalysis=base();c.syncAnalysisWatch();assert(!nodes.analysisWatch.disabled);nodes.analysisWatch.onclick();assert(c.watch.has('TESTUSDC'));assert.equal(saved,1);assert.equal(nodes.analysisWatch.attributes['aria-pressed'],'true');nodes.analysisWatch.onclick();assert(!c.watch.has('TESTUSDC'));assert.equal(saved,2);assert.equal(redrawn,2);now+=120000;nodes.analysisWatch.onclick();assert.equal(saved,2);assert(nodes.analysisWatch.disabled);assert.equal(c.lastAnalysis,null);
}else throw Error(kind);
console.log(kind+': OK');
