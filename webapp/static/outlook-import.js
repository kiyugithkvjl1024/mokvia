(function(root){
'use strict';
function shiftActual(actual, mode, minutes){
 const delta=Math.round(minutes/5)*5*60000,start=Date.parse(actual.start),end=Date.parse(actual.end);
 const first=start+(mode==='end'?0:delta),last=end+(mode==='start'?0:delta);
 if(!Number.isFinite(first)||!Number.isFinite(last)||last-first<5*60000||last-first>7*86400000)throw Error('実績の終了は開始より5分以上後にしてください。');
 return {start:new Date(first).toISOString(),end:new Date(last).toISOString()};
}
function intervalLanes(intervals){
 const sorted=intervals.map((item,index)=>({...item,index})).sort((a,b)=>a.start-b.start||a.end-b.end),result=[];
 let group=[],last=-Infinity;
 const finish=()=>{const ends=[],rows=[];for(const item of group){let lane=ends.findIndex(end=>end<=item.start);if(lane<0)lane=ends.length;ends[lane]=item.end;rows.push({...item,lane});}for(const item of rows)result[item.index]={lane:item.lane,columns:ends.length};group=[];};
 for(const item of sorted){if(group.length&&item.start>=last){finish();last=-Infinity;}group.push(item);last=Math.max(last,item.end);}finish();return result;
}
root.OutlookTime={shiftActual,intervalLanes};if(typeof module!=='undefined')module.exports={shiftActual,intervalLanes};
if(!root.document)return;
document.addEventListener('DOMContentLoaded',()=>{
 if(location.pathname!=='/calendar')return;
 const screen=document.getElementById('local-calendar-screen');if(!screen)return;
 const form=document.createElement('form');form.className='calendar-controls outlook-import-controls';form.setAttribute('aria-label','Outlook予定の取り込み');
 const source=document.createElement('select');source.setAttribute('aria-label','取得方式');
 for(const [value,label] of [['classic','従来版Outlook（ローカル）'],['graph','Microsoft Graph']]){const option=document.createElement('option');option.value=value;option.textContent=label;source.append(option);}
 const day=offset=>new Date(Date.now()+9*3600000+offset*86400000).toISOString().slice(0,10);
 const input=(label,value)=>{const e=document.createElement('input');e.type='date';e.required=true;e.setAttribute('aria-label',label);e.value=value;return e;};
 const first=input('取り込み開始日',day(-7)),last=input('取り込み終了日（この日は含まない）',day(31));
 const button=document.createElement('button');button.type='submit';button.textContent='Outlookから取り込む';button.disabled=true;
 const status=document.createElement('p');status.setAttribute('role','status');status.textContent='設定状態を確認しています。';
 form.append(source,first,last,button);screen.insertBefore(form,screen.querySelector('.calendar-controls'));form.after(status);
 const undoButton=document.createElement('button');undoButton.type='button';undoButton.textContent='元に戻す';undoButton.hidden=true;status.after(undoButton);
 let revision=null,busy=false,enabled=false,undoToken=null;
 const retainUndo=value=>{undoToken=value.undo||null;undoButton.hidden=!undoToken;};
 const request=async(path,body)=>{const response=await fetch('/api/v1/outlook-import/'+path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json',[document.querySelector('meta[name="mutation-marker"]')?.content||'X-GTD-Web']:'1'}:{},...(body?{body:JSON.stringify(body)}:{})});const value=await response.json();if(!response.ok)throw Error(value.error?.message||'取り込みを保存できませんでした。');return value;};
 const refresh=async()=>{const value=await request('status');revision=value.revision;enabled=value.enabled;if(value.binding){source.value=value.binding.source;source.disabled=true;}button.disabled=busy||!enabled;status.textContent=enabled?(value.last_import?'前回の取り込み: '+value.last_import.at+'／新規 '+value.last_import.counts.added+'・変更 '+value.last_import.counts.changed+'・取消 '+value.last_import.counts.cancelled+'・未確認 '+value.last_import.counts.unseen+'・要確認 '+value.last_import.counts.review:'ボタンを押したときだけ予定を取り込みます。'):'未設定です。会社PC内の取得ヘルパーと専用フォルダーの設定が必要です。';};
 const update=()=>screen.dispatchEvent(new Event('outlook-import-updated'));
 const perform=async(callback)=>{if(busy)return;busy=true;button.disabled=true;try{await callback();await refresh();update();}catch(e){status.textContent=e.message+' 既存データを保持しています。再送せず状態を確認してください。';try{const value=await request('status');revision=value.revision;}catch(_){}update();}finally{busy=false;button.disabled=!enabled;}};
 form.addEventListener('submit',event=>{event.preventDefault();if(!enabled)return;const from=first.value+'T00:00:00+09:00',to=last.value+'T00:00:00+09:00';if(!(Date.parse(to)>Date.parse(from))||(Date.parse(to)-Date.parse(from))>93*86400000){status.textContent='1〜93日間の範囲を指定してください。';return;}void perform(async()=>{status.textContent='会社PCのOutlookから取得しています…';const value=await request('run',{source:source.value,from,to});status.dataset.result=JSON.stringify(value);});});
 const change=(item,fields)=>perform(async()=>{const value=await request('local',{key:item.id,revision,done:item.local.done,...fields});retainUndo(value);});
 undoButton.addEventListener('click',()=>{if(!undoToken)return;const token=undoToken;void perform(async()=>{const value=await request('local',{key:token.key,revision,done:true,undo:token});retainUndo(value);});});
 const clock=value=>new Date(Date.parse(value)+9*3600000).toISOString().slice(11,16);
 const editable=item=>!item.cancelled&&item.presence!=='unseen'&&item.local.actual?.origin!=='measured'&&!item.local.work_sessions?.length;
 const actualOf=item=>item.local.actual||{start:item.source_start,end:item.source_end};
 function editTime(item){
  const dialog=document.createElement('dialog'),heading=document.createElement('h2'),form=document.createElement('form'),message=document.createElement('p');heading.textContent='ローカル実績を調整';message.textContent='Outlookの予定は変更しません。';
  const field=(label,value)=>{const wrap=document.createElement('label'),input=document.createElement('input');wrap.textContent=label;input.type='datetime-local';input.step='60';input.required=true;input.value=new Date(Date.parse(value)+9*3600000).toISOString().slice(0,16);wrap.append(input);form.append(wrap);return input;};
  const actual=actualOf(item),first=field('実績開始（JST）',actual.start),last=field('実績終了（JST）',actual.end),save=document.createElement('button'),cancel=document.createElement('button');save.textContent='保存';save.type='submit';cancel.textContent='キャンセル';cancel.type='button';cancel.addEventListener('click',()=>dialog.close());form.append(save,cancel);dialog.append(heading,message,form);screen.append(dialog);dialog.addEventListener('close',()=>dialog.remove());
  form.addEventListener('submit',event=>{event.preventDefault();const actual={start:first.value+':00+09:00',end:last.value+':00+09:00'};if(!(Date.parse(actual.end)>Date.parse(actual.start))){message.textContent='終了は開始より後にしてください。';return;}dialog.close();void change(item,{actual});});dialog.showModal();first.focus();
 }
 function render(item,cell,day,view){
  const actual=actualOf(item),card=document.createElement('article');card.className='outlook-event '+(item.local.actual?'outlook-actual':'outlook-plan');card.dataset.outlookKey=item.id;
  const title=document.createElement('strong'),info=document.createElement('span'),sourceInfo=document.createElement('span');title.textContent=(item.local.done?'✓ ':'')+item.title;
  const origin={planned:'予定由来',manual:'修正済み',measured:'実測'}[item.local.actual?.origin];info.className='outlook-time-label';info.textContent=item.all_day?'終日':clock(actual.start)+'\n'+clock(actual.end);sourceInfo.textContent=item.local.actual?'実績・'+origin:'予定';card.append(title,info,sourceInfo);
  const menu=document.createElement('button');menu.type='button';menu.className='outlook-actions-toggle';menu.textContent='操作';menu.setAttribute('aria-label','会議の操作');menu.setAttribute('aria-haspopup','dialog');card.append(menu);
  const model=root.TaskCard.operations('meeting',{done:item.local.done,editable:editable(item)&&!item.all_day,unavailable:item.cancelled||item.presence==='unseen'},{complete:()=>change(item,{done:true}),reopen:()=>change(item,{done:false}),actual:()=>editTime(item)});
  menu.disabled=!model.length;menu.addEventListener('click',()=>root.TaskCard.openOperations(model,menu,()=>busy));
  card.title=item.title+'／'+(item.local.actual?'実績（'+origin+'）':'予定')+' '+clock(actual.start)+'–'+clock(actual.end)+'／Outlook予定 '+item.source_start+'〜'+item.source_end;
  if(item.needs_review){const note=document.createElement('span');note.textContent='変更あり・要確認';card.append(note);}
  let grid=null;
  if(view!=='month'&&!item.all_day){
   grid=cell.querySelector('.outlook-time-grid');if(!grid){const scroll=document.createElement('div');scroll.className='outlook-time-scroll';grid=document.createElement('div');grid.className='outlook-time-grid';grid.dataset.day=day;for(let hour=0;hour<24;hour++){const label=document.createElement('span');label.className='outlook-hour';label.style.top=(hour*60)+'px';label.textContent=String(hour).padStart(2,'0')+':00';grid.append(label);}scroll.append(grid);cell.append(scroll);}
   const minute=value=>(Date.parse(value)-Date.parse(day+'T00:00:00+09:00'))/60000;
   card.style.top=Math.max(0,minute(item.start))+'px';card.style.height=Math.max(50,(Date.parse(item.end)-Date.parse(item.start))/60000)+'px';grid.append(card);if(!grid.dataset.layoutPending){grid.dataset.layoutPending='true';queueMicrotask(()=>{delete grid.dataset.layoutPending;const cards=[...grid.querySelectorAll('.outlook-event')],lanes=intervalLanes(cards.map(c=>({start:parseFloat(c.style.top),end:parseFloat(c.style.top)+Math.max(parseFloat(c.style.height),root.innerWidth<=850?80:50)})));for(let index=0;index<cards.length;index++){const {lane,columns}=lanes[index];cards[index].style.left='calc(32px + (100% - 34px) * '+lane+' / '+columns+')';cards[index].style.width='calc((100% - 34px) / '+columns+' - 2px)';cards[index].style.right='auto';}});}if(grid.querySelectorAll('.outlook-event').length===1)grid.parentElement.scrollTop=Math.max(0,minute(item.start)-30);
  }else cell.append(card);
  if(item.cancelled||item.presence==='unseen')return;
  const attach=(handle,mode)=>{
   let initial=null,firstPoint=null,firstMinute=null,dock=null,next=null;
   const acceptStart=event=>handle!==card||!event.target.closest('button,a,input,select,textarea,summary,dialog');
   handle.addEventListener('pointerdown',event=>{if(!acceptStart(event))return;if(!busy&&handle.setPointerCapture)handle.setPointerCapture(event.pointerId);firstPoint={clientX:event.clientX,clientY:event.clientY};firstMinute=grid?event.clientY-grid.getBoundingClientRect().top:null;});
   handle.classList.add('outlook-drag-handle');handle.setAttribute('aria-label',mode==='move'?'会議をドラッグして完了、または実績を移動':mode==='start'?'実績開始を調整':'実績終了を調整');
   root.TaskCard.attachPointerDrag(handle,{threshold:8,acceptStart,isGated:()=>busy,resolveTarget:event=>{const destination=root.ProjectKanbanMotion.destinationAt(dock,event);if(destination)return destination;const hit=document.elementFromPoint(event.clientX,event.clientY),target=hit?.closest('.outlook-time-grid');if(!target||!grid||!editable(item))return null;const days=(Date.parse(target.dataset.day+'T00:00:00Z')-Date.parse(day+'T00:00:00Z'))/86400000;try{next=shiftActual(initial,mode,(event.clientY-target.getBoundingClientRect().top-firstMinute)+days*1440);return 'actual';}catch(_){return null;}},onActivate:event=>{initial=actualOf(item);dock=mode==='move'?root.TaskCard.operationDock(model,event):null;card.classList.add('outlook-dragging');},onTrack:event=>{root.ProjectKanbanMotion.destinationAt(dock,event);},onCancel:()=>{root.ProjectKanbanMotion.clearDestinationDock(dock);card.classList.remove('outlook-dragging');},onDrop:target=>{root.ProjectKanbanMotion.clearDestinationDock(dock);card.classList.remove('outlook-dragging');const action=root.TaskCard.dropOperations(model).find(value=>value.key===target);if(action)action.run();else if(target==='actual'&&next&&(Date.parse(next.start)!==Date.parse(initial.start)||Date.parse(next.end)!==Date.parse(initial.end)))void change(item,{actual:next});}});
  };
  const move=document.createElement('button');move.type='button';move.textContent='⋮⋮';move.className='outlook-move';card.prepend(move);attach(move,'move');attach(card,'move');
  if(grid&&editable(item))for(const mode of ['start','end']){const handle=document.createElement('button');handle.type='button';handle.className='outlook-resize outlook-resize-'+mode;handle.textContent=mode==='start'?'開始を調整':'終了を調整';card.append(handle);attach(handle,mode);}
 }
 root.IntegrationRenderers=root.IntegrationRenderers||{};root.IntegrationRenderers.outlook={render};
 root.OutlookImportUI={render,complete:item=>change(item,{done:!item.local.done})};
 document.addEventListener('integration-settings-updated',()=>{void refresh().catch(()=>{});});
 void refresh().catch(()=>{status.textContent='設定状態を読み込めませんでした。';});
});
})(globalThis);
