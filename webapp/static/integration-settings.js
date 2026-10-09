(function(root){
'use strict';if(!root.document)return;
document.addEventListener('DOMContentLoaded',()=>{
 if(location.pathname!=='/calendar')return;
 const screen=document.getElementById('local-calendar-screen');if(!screen)return;
 const panel=document.createElement('details'),summary=document.createElement('summary'),hint=document.createElement('p'),rows=document.createElement('div'),message=document.createElement('p');
 panel.className='integration-settings';summary.textContent='連携設定';hint.textContent='有効にしても、未設定の連携は未接続です。認証や同期を自動で開始しません。';message.setAttribute('role','status');panel.append(summary,hint,rows,message);screen.append(panel);
 const labels={outlook:'Outlook',google_calendar:'Google Calendar',timetracker_nx:'TimeTracker NX'},states={connected:'接続設定済み',unconnected:'未接続',disabled:'無効',unavailable:'この環境では利用不可',reauth:'再認証が必要',error:'状態を確認できません'};
 const request=async(path,body)=>{const response=await fetch('/api/v1/'+path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json',[document.querySelector('meta[name="mutation-marker"]')?.content||'X-GTD-Web']:'1'}:{},...(body?{body:JSON.stringify(body)}:{})});const value=await response.json();if(!response.ok){const error=new Error('連携状態を確認してください。');error.code=value.error?.code||'invalid_request';throw error;}return value;};
 let revision=0,busy=false;
 const nxDetails=document.createElement('details'),nxSummary=document.createElement('summary'),nxHint=document.createElement('p'),nxRoot=document.createElement('div');nxSummary.textContent='TimeTracker NXへ確定実績を反映';nxDetails.append(nxSummary,nxHint,nxRoot);screen.append(nxDetails);
 const nxApi={listActuals:async()=>{const data=await request('actuals');return data.actuals;},registerTask:data=>request('integrations/task',{id:'timetracker_nx',...data}),previewActuals:references=>request('integrations/actual-preview',{id:'timetracker_nx',references}),applyActuals:async(references,token)=>{const value=await request('integrations/actual-apply',{id:'timetracker_nx',references,token});screen.dispatchEvent(new Event('actual-reflection-updated'));return value;},reconcile:async reference_id=>{const value=await request('integrations/actual-reconcile',{id:'timetracker_nx',reference_id});screen.dispatchEvent(new Event('actual-reflection-updated'));return value.status;}};
 let nx=null;
 async function refresh(){
  const state=await request('integrations/status');revision=state.revision;rows.replaceChildren();
  for(const plugin of state.plugins){
   const label=document.createElement('label'),box=document.createElement('input'),text=document.createElement('span');box.type='checkbox';box.checked=plugin.enabled;box.disabled=busy;box.setAttribute('aria-label',labels[plugin.id]+'を有効にする');text.textContent=(plugin.label||labels[plugin.id]||plugin.id)+' — '+states[plugin.connection_state]+(plugin.configured?'（設定済み）':'（未設定）');label.className='integration-setting-row';label.append(box,text);rows.append(label);
   if(plugin.id==='timetracker_nx'){
    const ready=plugin.enabled&&plugin.available&&plugin.configured;
    nxHint.textContent=ready?'モック接続を設定済みです。確定実績をプレビューして明示的に反映します。':'保存済み実績と反映状態は保持しています。反映には連携の有効化と会社での接続設定が必要です。';
    nxRoot.hidden=!ready;
    if(ready&&!nx){nx=root.MokviaTimeTrackerNX.mount(nxRoot,nxApi);}else if(ready&&nx){nx.reload();}
   }
   box.addEventListener('change',()=>{
    if(busy)return;busy=true;for(const input of rows.querySelectorAll('input'))input.disabled=true;
    void (async()=>{try{await request('integrations/settings',{id:plugin.id,enabled:box.checked,revision});message.textContent='連携設定を保存しました。保存済み予定・実績・反映状態は保持しています。';}catch(error){message.textContent='設定結果を確認できません。再送せず再読み込みしてください。';}finally{busy=false;try{await refresh();await actuals();}catch(error){message.textContent='状態を確認できません。再送しません。';}document.dispatchEvent(new Event('integration-settings-updated'));}})();
   });
  }
 }
 const actualPanel=document.createElement('details'),actualSummary=document.createElement('summary'),actualRows=document.createElement('div');actualSummary.textContent='実績の反映状態';actualPanel.append(actualSummary,actualRows);panel.append(actualPanel);
 const reflectionLabels={new:'未反映',update:'更新あり',synced:'反映済み',unknown:'結果不明・照合が必要',conflict:'競合・確認が必要',failed:'失敗'};
 async function actuals(){const data=await request('actuals');actualRows.replaceChildren();for(const [plugin,reflections] of Object.entries(data.reflection))for(const reflection of reflections){const actual=data.actuals.find(row=>row.reference_id===reflection.reference_id),row=document.createElement('p');row.textContent=actual.title+' — '+labels[plugin]+' '+reflectionLabels[reflection.state]+(reflection.availability==='ready'?'':'（'+(states[reflection.availability]||'未接続')+'）');actualRows.append(row);}}
 screen.addEventListener('actual-reflection-updated',()=>{if(actualPanel.open)void actuals().catch(()=>{message.textContent='実績状態を確認できません。';});});
 screen.addEventListener('outlook-import-updated',()=>{if(actualPanel.open)void actuals().catch(()=>{message.textContent='実績状態を確認できません。';});if(nx&&!nxRoot.hidden)nx.reload();});
 panel.addEventListener('toggle',()=>{if(panel.open)void refresh().catch(()=>{message.textContent='連携状態を確認できません。';});});actualPanel.addEventListener('toggle',()=>{if(actualPanel.open)void actuals().catch(()=>{message.textContent='実績状態を確認できません。';});});
 void refresh().catch(()=>{message.textContent='連携状態を確認できません。';});
});
})(globalThis);
