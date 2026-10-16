/* Install UX is independent of notification permission and trading controls. */
(function(){
'use strict';
var pending=null,prompting=false,installed=false,uiReady=false,accepted=false;
var display=window.matchMedia('(display-mode: standalone)');
function standalone(){return display.matches||navigator.standalone===true}
function ios(){return /iPhone|iPad|iPod/i.test(navigator.userAgent)||(/Mac/i.test(navigator.platform||'')&&navigator.maxTouchPoints>1)}
function sync(){
 if(!uiReady)return;
 var card=document.getElementById('pwaInstall'),button=document.getElementById('pwaInstallButton'),status=document.getElementById('pwaInstallStatus');
 card.hidden=standalone();
 button.disabled=prompting||installed;
 button.textContent=installed?'追加済み':prompting?'端末で確認してください':pending?'アプリをインストール':'アプリの追加方法';
 status.textContent=installed?'ホーム画面やアプリ一覧のアイコンから起動できます。':accepted?'端末の追加処理中です。完了するとホーム画面やアプリ一覧にアイコンが表示されます。':prompting?'端末に表示されるインストール確認を操作してください。':'ホーム画面からアプリとして起動できます。通知のON/OFFは追加後に設定できます。';
}
function closeGuide(){var dialog=document.getElementById('pwaInstallGuide');if(!dialog)return;if(dialog.open&&dialog.close)dialog.close();dialog.hidden=true}
function guide(){
 var dialog=document.getElementById('pwaInstallGuide'),steps=document.getElementById('pwaInstallSteps');
 var embedded=/FBAN|FBAV|Instagram|Line\/|; wv\)/i.test(navigator.userAgent);
 steps.textContent=embedded?'アプリ内ブラウザでは追加できない場合があります。右上メニューからSafariまたはChromeでこのページを開いてください。':ios()?'Safariでこのページを開き、共有ボタン →「ホーム画面に追加」→「追加」を選んでください。「Webアプリとして開く」が表示されたらONにしてください。':'AndroidはChromeのメニュー →「アプリをインストール」または「ホーム画面に追加」。PCはChrome・Edgeのアドレスバーのインストールアイコン、またはブラウザのメニューから追加してください。';
 dialog.hidden=false;
 if(typeof dialog.showModal==='function'){if(!dialog.open)dialog.showModal()}else{dialog.hidden=false;dialog.scrollIntoView({block:'center'})}
}
async function install(){
 if(prompting||installed)return;
 if(!pending){guide();return}
 var event=pending;pending=null;prompting=true;accepted=false;sync();
 try{
  await event.prompt();
  var choice=await event.userChoice;
  if(!installed&&choice&&choice.outcome==='accepted'){accepted=true;
   document.getElementById('pwaInstallStatus').textContent='端末の追加処理中です。完了するとホーム画面やアプリ一覧にアイコンが表示されます。';
  }else if(!installed){guide()}
 }catch(e){if(!installed)guide()}
 finally{prompting=false;sync()}
}
window.addEventListener('beforeinstallprompt',function(event){event.preventDefault();pending=event;sync()});
window.addEventListener('appinstalled',function(){installed=true;pending=null;prompting=false;closeGuide();sync()});
if(display.addEventListener)display.addEventListener('change',sync);
function init(){
 uiReady=true;
 if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js").catch(function(){});
 document.getElementById('pwaInstallButton').onclick=install;
 document.getElementById('pwaInstallHelp').onclick=guide;
 document.getElementById('pwaInstallClose').onclick=closeGuide;
 var original=document.getElementById('installApp');if(original)original.onclick=install;
 document.getElementById('pwaInstallGuide').addEventListener('click',function(e){if(e.target===this)closeGuide()});
 sync();
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init,{once:true});else init();
})();
