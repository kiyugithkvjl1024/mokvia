/* Explicit confirmed-actual export panel. Its host resolves selected immutable
   core reference IDs; the browser never supplies an NX user or credential. */
(function () {
  'use strict';
  const messages = {
    configuration_invalid:'会社で承認したNX設定を確認してください', credential_missing:'資格情報が未注入です',
    credential_invalid:'資格情報の注入形式を確認してください', identity_required:'本人IDの確認が必要です',
    authentication_required:'資格情報の期限・権限を確認してください', redirect_blocked:'転送を停止しました。HTTPS API設定を確認してください',
    rate_limited:'NXの通信制限です。自動再試行しません', server_unavailable:'NXサーバーを確認してください',
    transport_failed:'TLS・証明書・通信を確認してください', request_rejected:'NXが要求を拒否しました。社内設定を確認してください',

    create:'新規登録', update:'変更を更新', unchanged:'変更なし（送信なし）', blocked:'反映不可',
    synced:'反映確認済み', unknown:'結果不明：再送せず照合してください', unresolved:'一致なし：NX側を確認してください',
    ambiguous:'一致候補が複数：NX側を確認してください', conflict:'NX側で変更されています',
    not_confirmed:'確定実績ではありません', overlap:'時間が重複しています', task_changed:'登録後のタスク変更は未対応です',
    category_clear_unsupported:'登録済み分類の解除はNX側で確認してください',
    category_required:'必須分類を指定してください', granularity:'社内の入力粒度と一致しません',
    cross_day:'日跨ぎ実績は分割して確定してください', unmapped_task:'タスクURLとワークアイテムIDを登録してください',
    stale_preview:'内容が変わりました。再プレビューしてください', not_self:'本人の実績のみ反映できます',
    WorkItemNotAssigned:'本人がタスクに割り当てられていません', WorkItemLocked:'タスクの実績入力がロックされています',
    InputTimeEntryLocked:'対象日の実績入力がロックされています', InputTimeEntryAlwaysLocked:'本人の実績入力がロックされています',
    ProjectLocked:'プロジェクトがロックされています', InvalidAssignment:'本人がプロジェクトに割り当てられていません',
    NotMatchActualTimeUnit:'社内の入力粒度と一致しません', TimeEntryOverlapped:'NX実績と時間が重複しています',
    FieldCannotBeEmpty:'必須項目・分類が不足しています', OperationDenied:'終了プロジェクト・社内制約を確認してください',
    rejected:'NXが登録を拒否しました', invalid_data:'入力・NX状態を確認してください'
  };
  for(const [key,value] of Object.entries(messages))messages[key.replace(/([A-Z])/g,'_$1').replace(/^_/,'').toLowerCase()]=value;
  function element(tag, text) { const el=document.createElement(tag);if(text !== undefined)el.textContent=text;return el; }
  function mount(root, api) {
    root.classList.add('nx-panel');
    let records=[], selected=new Set(), preview=null, busy=false;
    const heading=element('h2','TimeTracker NX 実績反映');
    const notice=element('p','確定した本人の実績を選び、プレビューして明示的に反映します。');
    const list=element('div');list.className='nx-records';
    const form=element('form');form.className='nx-mapping';
    const picker=element('select');picker.name='reference_id';picker.required=true;picker.setAttribute('aria-label','実績と紐づくタスク');
    const fields={};
    const addField=(name,label,type='text',required=false)=>{const wrap=element('label',label);const input=element('input');input.name=name;input.type=type;input.required=required;wrap.append(input);form.append(wrap);fields[name]=input;};
    const pickLabel=element('label','実績と紐づくタスク');pickLabel.append(picker);form.append(pickLabel);
    addField('task_url','設定済み社内ホストのタスクURL','url',true);
    addField('work_item_id','確認済みワークアイテムID（URLから推測しません）','text',true);
    addField('timeEntryCategoryId','作業分類ID（必要な場合）');
    addField('processCategoryId','工程分類ID（必要な場合）');
    const save=element('button','タスクURLとIDを登録');save.type='submit';form.append(save);
    const actions=element('div');actions.className='nx-actions';
    const refresh=element('button','実績を再読み込み');refresh.type='button';
    const previewButton=element('button','選択した確定実績をプレビュー');previewButton.type='button';
    const apply=element('button','プレビューの実績を反映');apply.type='button';apply.disabled=true;
    actions.append(refresh,previewButton,apply);
    const status=element('p');status.setAttribute('role','status');status.setAttribute('aria-live','polite');
    const result=element('div');result.className='nx-results';
    root.replaceChildren(heading,notice,list,form,actions,status,result);
    function reset(){preview=null;apply.disabled=true;}
    function control(){root.setAttribute('aria-busy',String(busy));root.querySelectorAll('button,input,select').forEach(el=>el.disabled=busy||el.dataset.resolved==='true');previewButton.disabled=busy||!selected.size;apply.disabled=busy||!preview||preview.rows.some(r=>r.action==='blocked')||!preview.rows.some(r=>['create','update'].includes(r.action));}
    async function run(work){if(busy)return;busy=true;control();try{await work();}catch(e){reset();status.textContent=messages[e.code]||'処理を確認できません。再読み込みして状態を確認してください。';}finally{busy=false;control();}}
    function rowView(row){const card=element('article');const item=records.find(r=>r.reference_id===row.id);card.append(element('h3',item?.title||row.id),element('p',messages[row.code]||messages[row.action]||messages[row.status]||'状態を確認してください'));
      if(row.payload){const dl=element('dl');for(const [label,value] of [['タスクID',row.payload.workItemId],['開始',row.payload.startTime],['終了',row.payload.finishTime]]){dl.append(element('dt',label),element('dd',value));}card.append(dl);}
      if(row.previous||row.remote){const details=element('details');details.append(element('summary','前回値とNX側の差分'));details.append(element('pre',JSON.stringify({previous:row.previous,remote:row.remote},null,2)));card.append(details);}
      if(row.code==='unknown'||['unknown','unresolved','ambiguous'].includes(row.status)){const reconcile=element('button','送信せず照合');reconcile.type='button';reconcile.addEventListener('click',()=>run(async()=>{reset();const state=await api.reconcile(row.id);status.textContent=messages[state]||'状態を確認してください';reconcile.dataset.resolved=String(state==='synced');card.querySelector('p').textContent=messages[state]||'状態を確認してください';}));card.append(reconcile);}return card;}
    async function load(){reset();records=await api.listActuals();selected.clear();list.replaceChildren();picker.replaceChildren();result.replaceChildren();for(const item of records){const wrap=element('label');wrap.className='nx-record';const box=element('input');box.type='checkbox';box.value=item.reference_id;box.disabled=!item.completed||item.provisional;box.dataset.unconfirmed=String(box.disabled);box.addEventListener('change',()=>{box.checked?selected.add(item.reference_id):selected.delete(item.reference_id);reset();control();});wrap.append(box,element('span',`${item.title}　${item.start} → ${item.end||'計測中'}　${item.completed&&!item.provisional?'確定':'未確定'}`));list.append(wrap);const option=element('option',item.title);option.value=item.reference_id;picker.append(option);}status.textContent=`実績 ${records.length} 件。未確定の実績は選択できません。`;}
    // Preserve disabled status of provisional rows after every request.
    const baseControl=control;control=()=>{baseControl();list.querySelectorAll('[data-unconfirmed="true"]').forEach(el=>el.disabled=true);};
    form.addEventListener('submit',e=>{e.preventDefault();run(async()=>{reset();const categories={};for(const key of ['timeEntryCategoryId','processCategoryId'])if(fields[key].value.trim())categories[key]=fields[key].value.trim();await api.registerTask({reference_id:picker.value,task_url:fields.task_url.value.trim(),work_item_id:fields.work_item_id.value.trim(),categories});status.textContent='タスクの対応を保存しました。実績を選択してプレビューしてください。';});});
    refresh.addEventListener('click',()=>run(load));
    previewButton.addEventListener('click',()=>run(async()=>{preview=await api.previewActuals([...selected]);result.replaceChildren(...preview.rows.map(rowView));status.textContent=preview.rows.some(r=>r.action==='blocked')?'反映できない実績があります。内容を確認してください。':'内容を確認して「プレビューの実績を反映」を押してください。';}));
    apply.addEventListener('click',()=>run(async()=>{const token=preview.token;reset();const rows=await api.applyActuals([...selected],token);result.replaceChildren(...rows.map(rowView));status.textContent='処理結果を確認してください。結果不明の実績は再送せず照合してください。';}));
    run(load);
    return {reload:()=>run(load)};
  }
  window.MokviaTimeTrackerNX={mount};
})();
