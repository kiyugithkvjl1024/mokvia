(function (root) {
  'use strict';
  function captureMetadata(body) {
    const match = /^\s*<!-- mokvia-capture-v1 (.+) -->\r?\n/.exec(String(body || ''));
    if (!match) return null;
    try {
      const value = JSON.parse(match[1]);
      const hosts = {outlook:['outlook.office.com','outlook.office365.com','outlook.live.com'], teams:['teams.microsoft.com','teams.cloud.microsoft']};
      const url = new URL(value.source.url);
      if (value.version !== 1 || !hosts[value.source.kind]?.includes(url.hostname) || url.protocol !== 'https:' || url.username || url.password || url.port || /[\s\\]/.test(value.source.url)) return null;
      return value;
    } catch (_) { return null; }
  }
  function renderSource(entity, anchorId = "clarify-form") {
    document.querySelector('.local-capture-source')?.remove();
    const value=captureMetadata(entity?.body); if (!value) return;
    const el=(tag,text)=>{const node=document.createElement(tag);node.textContent=text;return node;};
    const panel=el('aside',''); panel.className='local-capture-source'; panel.setAttribute('aria-label','取り込み元');
    const link=el('a',value.source.kind==='outlook'?'Outlook 元メッセージ':'Teams 元メッセージ');
    link.href=value.source.url; link.target='_blank'; link.rel='noopener noreferrer'; panel.append(link);
    panel.append(el('p','初回受付: '+value.accepted_at));
    if (value.destination==='today') panel.append(el('p','対応予定日: '+(entity.frontmatter.action_date||'未設定')+'（Task編集や移動で変更できます。期限とは別です）'));
    const anchor=typeof anchorId==='string'?document.getElementById(anchorId):anchorId;
    if (anchor) anchor.prepend(panel);
  }
  function renderDetail(entity, content, body) {
    if (!captureMetadata(entity?.body)) return;
    body.textContent=entity.body.replace(/^\s*<!-- mokvia-capture-v1 .+ -->\r?\n/, '');
    renderSource(entity, content);
  }
  root.LocalCapture = {captureMetadata, renderSource, renderDetail};
  if (typeof module !== 'undefined') module.exports = {captureMetadata};
  if (!root.document) return;
  document.addEventListener('DOMContentLoaded', () => {
    const request = async path => { const response = await fetch(path); if (!response.ok) throw Error('読み込みに失敗しました'); return response.json(); };
    const el = (tag, text) => { const node=document.createElement(tag); node.textContent=text; return node; };
    if (['/inbox','/tasks','/status'].includes(location.pathname)) {
      const panel=el('details',''); panel.className='local-capture-status';
      const summary=el('summary','メッセージの取り込み'); panel.append(summary);
      const status=el('p','確認中です。'); status.setAttribute('role','status'); panel.append(status);
      const issues=el('ul',''); panel.append(issues);
      const refresh=el('button','取り込み状態を再読込'); refresh.type='button'; panel.append(refresh);
      (document.querySelector('main')||document.body).prepend(panel);
      const load=async () => {
        try {
          const value=await request('/api/v1/capture-import/status'); issues.replaceChildren();
          if (!value.enabled) { status.textContent='未設定です。起動時に専用受渡フォルダーを指定すると有効になります。'; return; }
          status.textContent=`取り込み済み ${value.tracked}件 / 安定待ち ${value.deferred}件 / 要確認 ${value.failed}件${value.blocked?'。取り込み停止中です。':''}。最終確認: ${value.checked_at||'起動待ち'}`;
          for (const issue of value.issues||[]) issues.append(el('li',`${issue.file}: ${issue.code}`));
          if (value.pending) issues.append(el('li',`保存結果の確認待ち ${value.pending}件。再送せず元のTaskと履歴を確認してください。`));
        } catch (_) { status.textContent='取り込み状態を読み込めません。再読込してください。'; }
      };
      refresh.addEventListener('click',load); load();
    }
    if (location.pathname==='/clarify') {
      const id=new URLSearchParams(location.search).get('id');
      if (id && /^task-capture-[0-9a-f]{32}$/.test(id)) request('/api/v1/entities/tasks/'+encodeURIComponent(id)).then(renderSource).catch(()=>{});
    }
  });
})(typeof globalThis==='undefined'?this:globalThis);
