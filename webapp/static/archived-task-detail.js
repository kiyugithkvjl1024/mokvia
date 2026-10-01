"use strict";

(() => {
  let view = null;

  function safeReturnPath(value) {
    try {
      const parsed = new URL(value, location.origin);
      return parsed.origin === location.origin && ["/tasks", "/inbox", "/projects", "/search"].includes(parsed.pathname)
        ? parsed.pathname + parsed.search
        : "/tasks";
    } catch (_error) {
      return "/tasks";
    }
  }

  function render(detail, state, returnPath) {
    if (!view) {
      view = document.createElement("article");
      view.className = "form-panel archived-task-detail";
      view.setAttribute("aria-label", "保管済みTaskの本文");
      state.after(view);
    }
    const title = document.createElement("h2");
    const body = document.createElement("pre");
    const back = document.createElement("a");
    title.textContent = (detail.frontmatter || {}).title || detail.id;
    body.className = "archived-task-body";
    body.textContent = detail.body || "";
    back.className = "button-link";
    const safeReturn = safeReturnPath(returnPath);
    back.href = safeReturn;
    back.textContent = safeReturn.startsWith("/search") ? "検索結果に戻る" : "Task一覧に戻る";
    view.replaceChildren(title, body, back);
    state.textContent = "保管済みTaskを表示しています。";
  }

  window.ArchivedTaskDetail = Object.freeze({render});
})();
