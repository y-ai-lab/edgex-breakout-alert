// The shared UI remains read-only; all prices and paper outcomes have a source.
(function(){
'use strict';
var venueMarkets=[],venueRequest=0;
function esc(x){var el=document.createElement('span');el.textContent=x==null?'不明':String(x);return el.innerHTML}
function number(x){return x==null?'不明':Number(x).toLocaleString('ja-JP',{maximumFractionDigits:8})}
function paintMarkets(){
 var node=document.getElementById('venueMarketTable'),position=node.scrollLeft;
 var needle=document.getElementById('venueSearch').value.trim().toUpperCase();
 var rows=venueMarkets.filter(function(m){return !needle||(m.ticker+' '+m.name).toUpperCase().includes(needle)});
 node.innerHTML='<table><thead><tr><th>銘柄</th><th>Omni Mark / USD</th><th>Funding / 周期</th><th>参考足</th><th>quote状態</th></tr></thead><tbody>'+rows.map(function(m){return '<tr><td>'+esc(m.ticker)+'<br><small>'+esc(m.name)+'</small></td><td>'+esc(number(m.mark_price))+'</td><td>'+esc(m.funding_rate==null?'不明':(Number(m.funding_rate)*100).toFixed(4)+'%')+' / '+esc(m.funding_interval_s==null?'不明':Number(m.funding_interval_s)/3600+'h')+'</td><td>'+(m.reference_available?'EdgeX同名銘柄 / 参考':'未対応 / 分析不可')+'</td><td>'+esc(m.quote_timestamp_status==='RECENT_INDICATIVE'?'参考値（確約なし）':m.quote_timestamp_status==='STALE'?'120秒以上古い':m.quote_timestamp_status==='FUTURE_TIMESTAMP'?'時刻不整合':'時刻不明')+'</td></tr>'}).join('')+'</tbody></table>';
 node.scrollLeft=position;
}
async function loadVenue(){
 var status=document.getElementById('venueCoverage');
 try{var j=await jf('/api/variational/markets');venueMarkets=j.markets||[];status.textContent='Omni '+j.total+'銘柄 / EdgeX同名参考足 '+j.reference_mapped+'銘柄。未対応銘柄を条件未達やREADY 0件と扱いません。参考足の取得・品質はスキャン結果を確認してください。';paintMarkets()}
 catch(e){status.textContent='Omni市場情報を取得できません。価格・対応銘柄は未確認です。';venueMarkets=[];paintMarkets()}
}
document.getElementById('venueSearch').addEventListener('input',paintMarkets);
var originalAnalyze=analyze;
analyze=async function(){
 var id=++venueRequest,ticker=document.getElementById('ticker').value.trim();
 var out=document.getElementById('venueAnalysis');out.textContent='Omniの公開価格を確認中…';
 var native=jf('/api/variational/market?ticker='+encodeURIComponent(ticker)).then(function(j){
  if(id!==venueRequest)return;var m=j.market;
  out.innerHTML='<b>'+esc(m.ticker)+' — Variational Omni</b><p class="sub">Mark '+esc(number(m.mark_price))+' USD（価格そのものの更新時刻は不明） / 公開統計取得 '+esc(new Date(m.stats_observed_ms).toLocaleString('ja-JP'))+'</p><p class="sub">参考Bid '+esc(number(m.indicative_bid))+' / Ask '+esc(number(m.indicative_ask))+'（確約価格ではありません） / quote時刻 '+esc(m.quote_updated_ms==null?'不明':new Date(m.quote_updated_ms).toLocaleString('ja-JP'))+'</p><p class="sub">以下のEntry・SL・TP・RR・チャート・数量はEdgeX参考足の値です。Omniの発注数量単位・最小数量・スプレッド・Fundingを別途確認してください。</p>';
 }).catch(function(){if(id===venueRequest)out.textContent='Omni価格を取得できません。以下はEdgeX参考足のみで、Omniの現在価格は未確認です。'});
 await originalAnalyze();await native;
};
// These controls have no routes on this server, and remain hidden/disabled.
loadLiveExecution=async function(){};
renderLiveExecution=function(){};
refreshExecutionControls=async function(){};
['executionControlUnlock','executionControlLock','executionControlOn','executionControlOff','executionControlReconcile','executionControlRefresh'].forEach(function(id){var e=document.getElementById(id);if(e){e.disabled=true;e.onclick=null}});
var source=document.getElementById('capitalSource');if(source){source.value='manual';source.dispatchEvent(new Event('change'))}
checkApiConnections=async function(){
 var st=document.getElementById('apiCheckStatus'),out=document.getElementById('apiCheckResults'),button=document.getElementById('checkApi');
 button.disabled=true;st.textContent='公開API・参考足を確認中…';
 var paths=[['Variational公開市場','/api/variational/markets'],['参考足スキャン','/api/screener?limit=1'],['通知設定','/api/push/config'],['保存・監視','/health']];
 var results=await Promise.all(paths.map(async function(x){try{var j=await jf(x[1]);var text=x[0]==='Variational公開市場'?j.total+'銘柄':x[0]==='参考足スキャン'?j.scanned+' / '+j.universe+'銘柄を参考足で分析':x[0]==='通知設定'?(j.enabled?'利用可能（この端末で購読してください）':'未設定'):j.collector&&j.collector.last_error?'監視の取得エラーあり':j.collector&&j.collector.last_success_ms?'監視データ保存済み':'初回収集中';return '<div class="apiCheckRow"><b>'+esc(x[0])+'</b><div class="sub">'+esc(text)+'</div></div>'}catch(e){return '<div class="apiCheckRow"><b>'+esc(x[0])+'</b><span class="bad">取得失敗</span></div>'}}));
 out.innerHTML=results.join('');st.textContent='取引・署名・口座APIには接続していません。';button.disabled=false;
};
document.getElementById('checkApi').onclick=function(){checkApiConnections();loadVenue();loadPaperExecution()};
loadVenue();
})();
