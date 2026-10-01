"use strict";

window.FocusPiP = (() => {
  const SIZE_KEY = "gtd-focus-pip-size";
  const SIZES = Object.freeze({small: {width: 200, height: 140}, medium: {width: 240, height: 160}, large: {width: 300, height: 210}});
  function savedSize() {
    try { const value = window.localStorage.getItem(SIZE_KEY); return Object.hasOwn(SIZES, value) ? value : "medium"; }
    catch (_error) { return "medium"; }
  }
  function supported() {
    return Boolean(window.isSecureContext && window.documentPictureInPicture && typeof window.documentPictureInPicture.requestWindow === "function");
  }
  function create({current, switchDialog, previewDialog, notificationDialog, trigger, getState, onDocumentChange}) {
    let pipWindow = null, moved = [], latest = {}, size = savedSize();
    function state(value) { latest = {...(typeof getState === "function" ? getState() : {}), ...latest, ...(value || {})}; return latest; }
    function place(node) { if (!node || !node.parentNode) return; const marker = document.createElement("span"); marker.hidden = true; marker.className = "focus-pip-placeholder"; node.parentNode.insertBefore(marker, node); moved.push({node, marker, hidden: node.hidden}); pipWindow.document.body.append(node); }
    function restore() { for (const item of moved.splice(0).reverse()) { item.node.hidden = item.hidden; if (item.marker.parentNode) item.marker.parentNode.insertBefore(item.node, item.marker); if (item.marker.parentNode) item.marker.parentNode.removeChild(item.marker); } if (typeof onDocumentChange === "function") onDocumentChange(document); }
    function renderShell() {
      const doc = pipWindow.document; doc.title = "mokvia Focus"; doc.body.dataset.focusPipWindow = "true"; doc.body.dataset.focusPipSize = size;
      const stylesheet = doc.createElement("link"); stylesheet.rel = "stylesheet"; stylesheet.href = "/assets/app.css"; doc.head.append(stylesheet);
      const toolbar = doc.createElement("div"), title = doc.createElement("strong"), opener = doc.createElement("button"); toolbar.className = "focus-pip-toolbar"; title.textContent = "Focus"; opener.type = "button"; opener.className = "secondary"; opener.textContent = "元画面"; opener.addEventListener("click", () => { if (typeof window.focus === "function") window.focus(); });
      const settings = doc.createElement("details"), summary = doc.createElement("summary"), panel = doc.createElement("div"), label = doc.createElement("label"), select = doc.createElement("select"), note = doc.createElement("p");
      settings.className = "focus-pip-settings"; summary.textContent = "設定"; label.textContent = "小窓のサイズ";
      for (const [value, text] of [["small", "小"], ["medium", "中"], ["large", "大"]]) { const option = doc.createElement("option"); option.value = value; option.textContent = text; select.append(option); }
      select.value = size;
      select.addEventListener("change", () => {
        if (!Object.hasOwn(SIZES, select.value)) return;
        size = select.value;
        doc.body.dataset.focusPipSize = size;
        try { window.localStorage.setItem(SIZE_KEY, size); } catch (_error) { /* Browser storage can be disabled. */ }
        const target = SIZES[size];
        const frameWidth = Number.isFinite(pipWindow.outerWidth - pipWindow.innerWidth) ? Math.max(0, pipWindow.outerWidth - pipWindow.innerWidth) : 0;
        const frameHeight = Number.isFinite(pipWindow.outerHeight - pipWindow.innerHeight) ? Math.max(0, pipWindow.outerHeight - pipWindow.innerHeight) : 0;
        try { pipWindow.resizeTo(target.width + frameWidth, target.height + frameHeight); } catch (_error) { /* Browser may deny or clamp resizing. */ }
      });
      note.textContent = "背後の画面が透ける透過には、このブラウザの最前面表示は対応していません。";
      label.append(select); panel.append(label, note); settings.append(summary, panel); toolbar.append(title, settings, opener); doc.body.append(toolbar);
    }
    async function open(value) {
      if (!supported()) return false;
      const currentState = state(value); if (pipWindow) { sync(currentState); return true; }
      try { pipWindow = await window.documentPictureInPicture.requestWindow({...SIZES[size], preferInitialWindowPlacement: true}); } catch (_error) { return false; }
      renderShell(); place(current); place(switchDialog); place(previewDialog); place(notificationDialog); if (typeof onDocumentChange === "function") onDocumentChange(pipWindow.document); sync(currentState);
      pipWindow.addEventListener("pagehide", () => { const closed = pipWindow; pipWindow = null; if (closed) restore(); }); return true;
    }
    function close() { if (pipWindow && typeof pipWindow.close === "function") pipWindow.close(); }
    function sync(value) { const currentState = state(value); if (!pipWindow) return; pipWindow.document.body.dataset.focusPipUnsafe = String(Number(currentState.doingCount || 0) > 1); }
    if (trigger) { trigger.hidden = !supported(); trigger.addEventListener("click", () => { void open(); }); }
    return Object.freeze({open, close, sync, timerWindow: () => pipWindow || window, isOpen: () => Boolean(pipWindow), isSupported: supported});
  }
  return Object.freeze({create});
})();
