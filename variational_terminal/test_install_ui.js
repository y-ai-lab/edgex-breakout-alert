const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const script=fs.readFileSync(__dirname+'/install.js','utf8');
function environment({ua='Chrome Android',platform='',touch=0,standalone=false,loading=false}={}){
 const events={},domEvents={},elements={};
 for(const id of ['pwaInstall','pwaInstallButton','pwaInstallStatus','pwaInstallHelp','pwaInstallGuide','pwaInstallSteps','pwaInstallClose','installApp'])elements[id]={hidden:id==='pwaInstallGuide',disabled:false,textContent:'',open:false,addEventListener(){},showModal(){this.open=true},close(){this.open=false},scrollIntoView(){}};
 const display={matches:standalone,addEventListener(t,fn){events['display:'+t]=fn}};
 const context={window:{matchMedia:()=>display,addEventListener(t,fn){events[t]=fn}},navigator:{userAgent:ua,platform,maxTouchPoints:touch,standalone:false},document:{readyState:loading?'loading':'complete',getElementById:id=>elements[id],addEventListener(t,fn){domEvents[t]=fn}}};
 vm.runInNewContext(script,context);
 return {events,domEvents,elements,display};
}
function promptEvent(outcome='accepted',fail=false){return {prevented:false,calls:0,preventDefault(){this.prevented=true},async prompt(){this.calls++;if(fail)throw Error('cancelled')},userChoice:Promise.resolve({outcome})}}
(async()=>{
 const a=environment();assert.match(a.elements.pwaInstallButton.textContent,/追加方法/);assert.equal(a.elements.pwaInstallButton.disabled,false);
 const e=promptEvent();a.events.beforeinstallprompt(e);assert.equal(e.prevented,true);assert.equal(e.calls,0);assert.match(a.elements.pwaInstallButton.textContent,/インストール/);
 await a.elements.pwaInstallButton.onclick();assert.equal(e.calls,1);assert.equal(a.elements.pwaInstallButton.disabled,false);assert.match(a.elements.pwaInstallStatus.textContent,/処理中/); // accepted is not installed
 a.events.appinstalled();assert.equal(a.elements.pwaInstallButton.disabled,true);assert.match(a.elements.pwaInstallStatus.textContent,/アイコン/);
 const b=environment();b.events.beforeinstallprompt(promptEvent('dismissed'));await b.elements.pwaInstallButton.onclick();assert.equal(b.elements.pwaInstallGuide.open,true);assert.equal(b.elements.pwaInstallButton.disabled,false);
 const c=environment();c.events.beforeinstallprompt(promptEvent('accepted',true));await c.elements.pwaInstallButton.onclick();assert.equal(c.elements.pwaInstallGuide.open,true);assert.equal(c.elements.pwaInstallButton.disabled,false);
 const ios=environment({ua:'iPhone Safari'});await ios.elements.pwaInstallButton.onclick();assert.match(ios.elements.pwaInstallSteps.textContent,/Safari.*共有/);ios.elements.pwaInstallClose.onclick();assert.equal(ios.elements.pwaInstallGuide.open,false);assert.equal(ios.elements.pwaInstallGuide.hidden,true);
 const ipad=environment({ua:'Macintosh Safari',platform:'MacIntel',touch:5});ipad.elements.pwaInstallHelp.onclick();assert.match(ipad.elements.pwaInstallSteps.textContent,/Safari/);
 const embedded=environment({ua:'Android; wv) Line/15'});embedded.elements.pwaInstallHelp.onclick();assert.match(embedded.elements.pwaInstallSteps.textContent,/アプリ内ブラウザ/);
 const pc=environment({ua:'Desktop Edge'});pc.elements.pwaInstallHelp.onclick();assert.match(pc.elements.pwaInstallSteps.textContent,/PC.*Chrome・Edge/);
 const installed=environment({standalone:true});assert.equal(installed.elements.pwaInstall.hidden,true);
 const fallback=environment();delete fallback.elements.pwaInstallGuide.showModal;fallback.elements.pwaInstallHelp.onclick();assert.equal(fallback.elements.pwaInstallGuide.hidden,false);fallback.elements.pwaInstallClose.onclick();assert.equal(fallback.elements.pwaInstallGuide.hidden,true);
 const early=environment({loading:true});const ep=promptEvent();early.events.beforeinstallprompt(ep);early.domEvents.DOMContentLoaded();await early.elements.pwaInstallButton.onclick();assert.equal(ep.calls,1);
 const race=environment();let resolve;const rp=promptEvent();rp.userChoice=new Promise(r=>resolve=r);race.events.beforeinstallprompt(rp);const first=race.elements.pwaInstallButton.onclick();await race.elements.pwaInstallButton.onclick();assert.equal(rp.calls,1);race.events.appinstalled();resolve({outcome:'accepted'});await first;assert.equal(race.elements.pwaInstallButton.disabled,true);
 console.log('Install interaction, fallback, lifecycle and platform checks passed');
})().catch(e=>{console.error(e);process.exitCode=1});
