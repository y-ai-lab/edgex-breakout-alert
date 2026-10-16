const assert=require('node:assert/strict'),fs=require('fs'),vm=require('vm');
const handlers={},deleted=[],cached=[],offline={offline:true};let fail=false;
const context={URL,location:{origin:'https://variational.example'},self:{addEventListener(t,fn){handlers[t]=fn},skipWaiting:()=>Promise.resolve()},clients:{claim:()=>Promise.resolve()},
 caches:{open:async()=>({addAll:async items=>cached.push(...items)}),keys:async()=>['variational-terminal-shell-v1','variational-terminal-shell-v2','unrelated-cache'],delete:async key=>deleted.push(key),match:async key=>key==='/offline.html'?offline:undefined},fetch:async()=>{if(fail)throw Error('offline');return {online:true}}};
context.self.clients=context.clients;
vm.runInNewContext(fs.readFileSync(__dirname+'/sw.js','utf8'),context);
function event(request){return {request,respondWith(p){this.response=p},waitUntil(p){this.work=p}}}
(async()=>{
 const i=event();handlers.install(i);await i.work;assert(cached.includes('/icons/icon-512.png'));assert(cached.includes('/offline.html'));assert(!cached.includes('/'));assert(!cached.some(x=>x.startsWith('/api/')));
 const a=event();handlers.activate(a);await a.work;assert.deepEqual(deleted,['variational-terminal-shell-v1']);
 for(const req of [{url:'https://variational.example/api/screener',method:'GET',mode:'cors'},{url:'https://variational.example/api/push/subscribe',method:'POST',mode:'cors'},{url:'https://other.example/',method:'GET',mode:'navigate'}]){const e=event(req);handlers.fetch(e);assert.equal(e.response,undefined)}
 let n=event({url:'https://variational.example/?tab=analysis',method:'GET',mode:'navigate'});handlers.fetch(n);assert.equal((await n.response).online,true);
 fail=true;n=event({url:'https://variational.example/btc',method:'GET',mode:'navigate'});handlers.fetch(n);assert.equal(await n.response,offline);
 assert(handlers.push&&handlers.notificationclick);console.log('Offline fallback, cache isolation and notification handlers passed');
})().catch(e=>{console.error(e);process.exitCode=1});
// An already installed v1 worker must not return its old SVG-only manifest for
// the versioned manifest URL rendered by the upgraded app.
(async()=>{
 const h={};const legacy={URL,location:{origin:'https://variational.example'},self:{addEventListener(t,fn){h[t]=fn}},
  caches:{match:async req=>req.url.endsWith('/manifest.webmanifest')?{legacy:true}:undefined},fetch:async()=>({fresh:true})};
 vm.runInNewContext(fs.readFileSync(__dirname+'/../analysis_terminal/sw.js','utf8'),legacy);
 const request={url:'https://variational.example/manifest.webmanifest?v=1.0.1',method:'GET',mode:'cors'};
 const e=event(request);h.fetch(e);assert.equal((await e.response).fresh,true);
})().catch(e=>{console.error(e);process.exitCode=1});
