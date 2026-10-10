/* Shared notification preferences; saves never send a notification. */
(function(root){
'use strict';
const kinds=Object.freeze({focus_reminder:'Focus督促',focus_anomaly:'Focus状態異常',break_finished:'休憩終了'});
function sameIntent(a,b){return a.version===b.version&&a.quiet_enabled===b.quiet_enabled&&a.quiet_start===b.quiet_start&&a.quiet_end===b.quiet_end&&a.timezone===b.timezone&&Object.keys(kinds).every(k=>a.types[k]===b.types[k]);}
root.NotificationSettings=Object.freeze({sameIntent,kinds});
if(!root.document)return;
document.addEventListener('DOMContentLoaded',()=>{
 const screen=document.getElementById('task-list-workflow');if(!screen)return;
 const panel=document.createElement('details');panel.id='notification-settings';panel.className='notification-settings';
 const summary=document.createElement('summary');summary.textContent='通知設定';
 const hint=document.createElement('p');hint.textContent='既存の通知を時間帯と種類でOFFにできます。休憩の自動完了は続きます。Focusの既存時間帯・Task単位の通知OFFも維持します。';
 const form=document.createElement('form'),fields=document.createElement('fieldset'),message=document.createElement('p');fields.disabled=true;message.setAttribute('role','status');
 function input(label,type,name){const row=document.createElement('label'),element=document.createElement('input'),text=document.createElement('span');element.type=type;element.name=name;element.id='notification-'+name;text.textContent=label;row.append(element,text);fields.append(row);return element;}
 const quiet=input('指定時間帯は通知OFF','checkbox','quiet');
 const times=document.createElement('div');times.className='notification-times';
 const start=input('開始（含む）','time','start'),end=input('終了（含まない）','time','end');start.required=end.required=true;times.append(start.parentElement,end.parentElement);fields.append(times);
 const zone=input('タイムゾーン（空欄は通知処理サーバー／コンテナーのOS設定）','text','timezone');zone.maxLength=128;zone.placeholder='例: Asia/Tokyo';
 const boxes={};for(const [key,label]of Object.entries(kinds))boxes[key]=input(label+'を通知','checkbox',key);
 const save=document.createElement('button');save.type='submit';save.textContent='通知設定を保存';save.dataset.mutation='';
 const reload=document.createElement('button');reload.type='button';reload.className='secondary';reload.textContent='保存状態を再読込';fields.append(save);form.append(fields,reload);panel.append(summary,hint,form,message);screen.append(panel);
 let value=null,busy=false;
 const gated=()=>typeof mutationIsGated==='function'&&mutationIsGated();
 async function request(body){const response=await fetch('/api/v1/notifications/settings',{method:body?'POST':'GET',cache:'no-store',headers:body?{'Content-Type':'application/json',[document.querySelector('meta[name="mutation-marker"]')?.content||'X-GTD-Web']:'1'}:{},...(body?{body:JSON.stringify(body)}:{})});const data=await response.json();if(!response.ok)throw new Error(data.error?.code||'invalid_request');return data;}
 function render(data){value=data;quiet.checked=data.quiet_enabled;start.value=data.quiet_start;end.value=data.quiet_end;zone.value=data.timezone==='local'?'':data.timezone;for(const key of Object.keys(kinds))boxes[key].checked=data.types[key];}
 async function refresh(){if(busy)return;busy=true;fields.disabled=true;try{render(await request());message.textContent='保存済みの通知設定です。';}catch(error){value=null;message.textContent='設定を確認できません。再読込してください。';}finally{busy=false;fields.disabled=!value;reload.disabled=false;}}
 panel.addEventListener('toggle',()=>{if(panel.open&&!value)void refresh();});reload.addEventListener('click',()=>void refresh());
 form.addEventListener('submit',event=>{
  event.preventDefault();if(busy||!value||gated()){message.textContent='操作結果の確認が終わってから保存してください。';return;}
  if(!form.reportValidity())return;if(start.value===end.value){message.textContent='開始と終了は異なる時刻を指定してください。';return;}
  const intended={...value,quiet_enabled:quiet.checked,quiet_start:start.value,quiet_end:end.value,timezone:zone.value.trim()||'local',types:Object.fromEntries(Object.keys(kinds).map(key=>[key,boxes[key].checked]))};
  busy=true;fields.disabled=true;
  void(async()=>{
   try{const saved=await request(intended);if(saved.revision!==intended.revision+1||!sameIntent(saved,intended))throw new Error('readback');render(saved);message.textContent='通知設定を保存しました。OFF中の通知は後からまとめて送信しません。';}
   catch(error){
    // A lost POST response is reconciled by reading; never retry a write.
    try{const saved=await request();render(saved);message.textContent=saved.revision===intended.revision+1&&sameIntent(saved,intended)?'保存済みの設定を照合しました。':error.message==='invalid_timezone'?'タイムゾーンを確認してください。この実行環境が対応するIANA名（Asia/Tokyo等）または空欄を指定してください。現在の保存設定を表示しています。':'保存結果が一致しません。現在の設定を確認してから操作してください。';}
    catch(readError){value=null;message.textContent='結果を確認できません。再送せず保存状態を再読込してください。';}
   }finally{busy=false;fields.disabled=!value;reload.disabled=false;}
  })();
 });
});
})(globalThis);
