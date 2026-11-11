self.addEventListener("install",event=>{self.skipWaiting()});
self.addEventListener("activate",event=>{event.waitUntil(self.clients.claim())});
self.addEventListener("push",event=>{
 let data={};try{data=event.data?event.data.json():{}}catch(e){data={body:event.data?.text()||"有新的監控通知"}}
 const title=(data.title||"台股當沖決策中心").slice(0,100);
 const body=(data.body||"請登入查看監控狀態").slice(0,250);
 event.waitUntil(self.registration.showNotification(title,{body,icon:"/push-icon.svg",badge:"/push-icon.svg",tag:data.tag||"tw-daytrade",data:{url:"/"}}));
});
self.addEventListener("notificationclick",event=>{
 event.notification.close();
 event.waitUntil(self.clients.matchAll({type:"window",includeUncontrolled:true}).then(async windows=>{
  for(const w of windows){if(w.url.startsWith(self.location.origin)&&"focus"in w)return w.focus()}
  return self.clients.openWindow("/");
 }));
});
