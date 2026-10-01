(function(root){
  'use strict';
  function shouldNotify(event,last,permission){return Boolean(event?.id&&event.text&&!event.native_active&&event.id!==last&&permission==='granted');}
  if(typeof module!=='undefined')module.exports={shouldNotify};
  if(!root.document)return;
  document.addEventListener('DOMContentLoaded',()=>{
    const button=document.createElement('button');button.type='button';button.className='secondary';button.textContent='ブラウザ通知を許可';
    const state=document.createElement('span');state.className='muted';state.setAttribute('role','status');
    const bar=document.createElement('div');bar.className='local-notification-bar';bar.append(button,state);document.querySelector('main').prepend(bar);
    button.addEventListener('click',async()=>{
      if(!('Notification'in root)){state.textContent='ブラウザ通知は利用できません';return;}
      const result=await Notification.requestPermission();state.textContent=result==='granted'?'タブを開いている間の通知を有効にしました':'通知は許可されていません';
    });
    let last='',busy=false; try{last=localStorage.getItem('gtd-local-notification-id')||'';}catch(_){}
    async function poll(){
      if(busy)return;busy=true;
      try{
        const response=await fetch('/api/v1/local-notifications',{cache:'no-store'});if(!response.ok)return;
        const event=await response.json();const notify=shouldNotify(event,last,root.Notification?.permission);
        if(event.id){last=event.id;try{localStorage.setItem('gtd-local-notification-id',last);}catch(_){}}
        state.textContent=event.native_active?'Windows通知が稼働中':'ブラウザ通知：タブを開いておいてください';
        if(notify)new Notification('mokvia',{body:event.text,tag:'gtd-local-current'});
      }catch(_){state.textContent='通知監視に接続できません';}finally{busy=false;}
    }
    setInterval(poll,10000);document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')poll();});poll();
  });
})(globalThis);
