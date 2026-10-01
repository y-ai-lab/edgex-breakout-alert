const CACHE="edgex-terminal-shell-v1";
const ASSETS=["/manifest.webmanifest","/app-icon.svg"];
self.addEventListener("install",event=>{
  event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(ASSETS)).then(()=>self.skipWaiting()));
});
self.addEventListener("activate",event=>{
  event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))).then(()=>self.clients.claim()));
});
self.addEventListener("fetch",event=>{
  const url=new URL(event.request.url);
  if(url.origin!==location.origin)return;
  if(ASSETS.includes(url.pathname)){
    event.respondWith(caches.match(event.request).then(cached=>cached||fetch(event.request)));
  }
});

self.addEventListener("notificationclick",event=>{
  event.notification.close();
  const target=(event.notification.data&&event.notification.data.url)||"/?tab=dashboard";
  event.waitUntil(clients.matchAll({type:"window",includeUncontrolled:true}).then(windows=>{
    for(const client of windows){
      if("focus" in client){
        client.navigate(target);
        return client.focus();
      }
    }
    if(clients.openWindow)return clients.openWindow(target);
  }));
});

self.addEventListener("push",event=>{
  let data={title:"EdgeX 分析ターミナル",body:"市場データが更新されました。",url:"/?tab=dashboard",tag:"edgex-update"};
  try{
    if(event.data){
      const parsed=event.data.json();
      data={...data,...parsed};
    }
  }catch(_e){
    if(event.data)data.body=event.data.text();
  }
  event.waitUntil(self.registration.showNotification(data.title,{
    body:data.body,
    tag:data.tag||"edgex-update",
    icon:"/app-icon.svg",
    badge:"/app-icon.svg",
    data:{url:data.url||"/?tab=dashboard"}
  }));
});
