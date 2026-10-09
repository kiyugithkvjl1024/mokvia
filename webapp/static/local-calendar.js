(function (root) {
  'use strict';
  const iso = d => d.toISOString().slice(0, 10);
  function shiftDate(value, view, step) {
    const date = new Date(value + 'T00:00:00Z');
    if (view === 'month') {
      const day = date.getUTCDate(); date.setUTCDate(1); date.setUTCMonth(date.getUTCMonth() + step);
      const last = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate(); date.setUTCDate(Math.min(day, last));
    } else date.setUTCDate(date.getUTCDate() + step * (view === 'week' ? 7 : 1));
    return iso(date);
  }
  function scheduleOperation(entity, draft) {
    if (!draft.title.trim() || !/^\d{4}-\d{2}-\d{2}$/.test(draft.date)) throw Error('タイトルと日付を入力してください。');
    const oldStatus = entity?.frontmatter?.status;
    if (['doing', 'done'].includes(oldStatus) || entity?.archived) throw Error('実行中・完了・アーカイブ済みTaskはTask画面で編集してください。');
    const fields = {title: draft.title.trim(), action_date: draft.date};
    if (draft.timed) {
      if (!/^\d\d:\d\d$/.test(draft.start) || !/^\d\d:\d\d$/.test(draft.end) || !/^\d{4}-\d{2}-\d{2}$/.test(draft.endDate)) throw Error('開始と終了を入力してください。');
      fields.scheduled_start = draft.date + 'T' + draft.start + ':00+09:00';
      fields.scheduled_end = draft.endDate + 'T' + draft.end + ':00+09:00';
      if (!(Date.parse(fields.scheduled_end) > Date.parse(fields.scheduled_start))) throw Error('終了は開始より後にしてください。');
      fields.status = 'scheduled';
    } else {
      fields.scheduled_start = ''; fields.scheduled_end = '';
      fields.status = oldStatus && oldStatus !== 'scheduled' ? oldStatus : 'next';
    }
    return entity ? {action: 'update', kind: 'tasks', id: entity.id, base_hash: entity.content_hash, fields} : {action: 'create', kind: 'tasks', fields, body: ''};
  }
  const api = {shiftDate, scheduleOperation};
  if (typeof module !== 'undefined') module.exports = api;
  root.LocalCalendar = api;
  if (!root.document) return;
  document.addEventListener('DOMContentLoaded', () => {
    if (location.pathname !== '/calendar') return;
    document.body.classList.add('local-calendar-route');
    const section = document.createElement('section'); section.id = 'local-calendar-screen'; section.className = 'panel local-calendar';
    const el = (tag, text, parent = section) => { const e = document.createElement(tag); if (text) e.textContent = text; parent.append(e); return e; };
    el('h1', 'カレンダー');
    el('p', '日本時間（JST）／予定・実績・期限を区別して表示します。ローカルTaskの表示です。既存の外部同期設定は変更しません。');
    const controls = el('div', '', section); controls.className = 'calendar-controls';
    const button = (text, handler, parent = controls) => { const b = el('button', text, parent); b.type = 'button'; b.addEventListener('click', handler); return b; };
    const view = el('select', '', controls); view.setAttribute('aria-label', '表示期間');
    for (const [value, label] of [['day','日'],['week','週'],['month','月']]) { const o = el('option', label, view); o.value = value; } view.value = 'week';
    const today = () => new Date(Date.now() + 9 * 3600000).toISOString().slice(0,10);
    const date = el('input', '', controls); date.type = 'date'; date.value = today(); date.setAttribute('aria-label', '表示日');
    button('前へ', () => {date.value = shiftDate(date.value,view.value,-1); load();});
    button('今日', () => {date.value=today();load();});
    button('次へ', () => {date.value = shiftDate(date.value,view.value,1); load();});
    button('再読込', () => load());
    button('予定を追加', () => edit(null,date.value));
    const legend = el('p', '予定（時刻指定） / 行動日（終日） / 実績（記録） / 期限'); legend.className = 'calendar-legend';
    const message = el('p'); message.setAttribute('role','status');
    const grid = el('div'); grid.className='calendar-grid';
    const dialog = el('dialog'); const form = el('form','',dialog); el('h2','予定の追加・変更',form);
    const field = (label,type) => {const wrap=el('label',label,form); const input=el('input','',wrap);input.type=type;return input;};
    const title=field('タイトル','text');title.required=true;
    const startDate=field('開始日 / 行動日（JST）','date');startDate.required=true;
    const timed=field('時刻指定','checkbox');
    const startTime=field('開始時刻（JST）','time'); const endDate=field('終了日（JST）','date'); const endTime=field('終了時刻（JST）','time');
    const error=el('p','',form);error.setAttribute('role','alert');
    const preview=el('pre','',form); preview.className='calendar-preview'; preview.hidden=true;
    const save=el('button','変更内容を確認',form);save.type='submit';
    const apply=button('確認して保存',()=>commit(),form);apply.hidden=true;
    const cancel=button('キャンセル',()=>{if(!applying)dialog.close();},form);
    let snapshot=null, editing=null, pending=null, applying=false, uncertain=false, generation=0;
    const request=async(path,body) => {const response=await fetch(path,{method:body?'POST':'GET',headers:{[document.querySelector('meta[name="mutation-marker"]')?.content||'X-GTD-Web']:'1',...(body?{'Content-Type':'application/json'}:{})},...(body?{body:JSON.stringify(body)}:{})});const value=await response.json();if(!response.ok)throw Error(value.error?.message||value.message||'保存・読み込みに失敗しました。');return value;};
    function syncTimed(){startTime.disabled=endDate.disabled=endTime.disabled=!timed.checked;}
    timed.addEventListener('change',syncTimed);
    form.addEventListener('input',()=>{pending=null;apply.hidden=true;preview.hidden=true;save.disabled=false;});
    async function edit(entity, day) {
      error.textContent='';pending=null;preview.hidden=true;apply.hidden=true;save.disabled=false;
      try {
        editing=entity ? await request('/api/v1/entities/tasks/'+encodeURIComponent(entity.id)) : null;
        const fm=editing?.frontmatter||{};title.value=fm.title||'';
        const jstInput=value=>value ? new Date(Date.parse(value)+9*3600000).toISOString().slice(0,16) : '';
        const first=jstInput(fm.scheduled_start),last=jstInput(fm.scheduled_end);
        startDate.value=first.slice(0,10)||fm.action_date||day;timed.checked=Boolean(first&&last);
        startTime.value=first.slice(11)||'09:00';endDate.value=last.slice(0,10)||startDate.value;endTime.value=last.slice(11)||'10:00';syncTimed();dialog.showModal();title.focus();
      } catch(e){message.textContent=e.message;}
    }
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(applying||uncertain)return;
      save.disabled=true;error.textContent='';
      try {
        const operation=scheduleOperation(editing,{title:title.value,date:startDate.value,timed:timed.checked,start:startTime.value,endDate:endDate.value,end:endTime.value});
        pending=await request('/api/v1/mutations/preview',operation);
        preview.textContent='次の変更を確認してください。\n'+JSON.stringify(pending.diff||{before:pending.before,after:pending.proposed},null,2);
        preview.hidden=false;apply.hidden=false;
      }catch(e){error.textContent=e.message;}finally{save.disabled=false;}
    });
    async function commit(){
      if(!pending||applying||uncertain)return;
      applying=true;apply.disabled=save.disabled=cancel.disabled=true;error.textContent='';
      const {preview_hash,...payload}=pending;
      try {await request('/api/v1/mutations/apply',{preview:payload,preview_hash});pending=null;dialog.close();message.textContent='予定を保存しました。';await load();}
      catch(e){uncertain=true;pending=null;apply.hidden=true;save.disabled=true;error.textContent='保存結果を確認できません。重複保存を防ぐため再送しません。再読込して予定を確認してください。 '+e.message;}
      finally{applying=false;apply.disabled=cancel.disabled=false;if(!uncertain)save.disabled=false;}
    }
    async function load(){
      const token=++generation;message.textContent='読み込み中…';
      try {
        const [projection, fresh]=await Promise.all([request('/api/v1/calendar?view='+view.value+'&date='+date.value),request('/api/v1/snapshot')]);if(token!==generation)return;
        snapshot=fresh;if(uncertain){uncertain=false;save.disabled=false;}grid.replaceChildren();grid.dataset.view=view.value;
        for(const day of projection.days){
          const cell=el('section','',grid);cell.className='calendar-day';const heading=el('h2',day.date+' ('+['日','月','火','水','木','金','土'][new Date(day.date+'T00:00:00Z').getUTCDay()]+')',cell);heading.className='calendar-date';
          button('＋ 予定',()=>edit(null,day.date),cell);
          for(const item of day.events){
            const row=el('div','',cell);row.className='calendar-event calendar-'+item.kind;
            const labels={planned:'予定',actual:'実績',action:'行動日',deadline:'期限',external:'外部予定'};
            const clock=value=>value.slice(11,16);
            const time=item.all_day?'':clock(item.start)+'–'+(item.end.slice(0,10)!==day.date?'24:00':clock(item.end));
            el('span',labels[item.kind]+' '+time+(item.provisional?'（計測中）':''),row);
            if(item.external){const renderer=root.IntegrationRenderers?.[item.provider_id];if(renderer){row.remove();renderer.render(item,cell,day.date,view.value);}else el('span',item.title,row);continue;}
            const link=el('a',item.title,row);link.href='/clarify?id='+encodeURIComponent(item.id);
            if(['planned','action','deadline'].includes(item.kind)&&!['doing','done'].includes(item.status))button('予定変更',()=>edit((snapshot.entities||[]).find(e=>e.id===item.id),day.date),row);
          }
        }
        message.textContent=projection.mutation_state?.recovery_required?'復旧待ちです。状態画面を確認してください。':'予定は青、実績は緑、期限は赤で表示します。';
      }catch(e){if(token===generation)message.textContent=e.message;}
    }
    section.addEventListener('outlook-import-updated',load);
    date.addEventListener('change',load);view.addEventListener('change',load);
    (document.querySelector('main')||document.body).append(section);load();
  });
})(typeof globalThis === 'undefined' ? this : globalThis);
