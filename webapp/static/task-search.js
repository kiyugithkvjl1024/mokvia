"use strict";

(() => {
  if (location.pathname !== "/search") return;
  document.getElementById("task-search-workflow").hidden = false;
  document.getElementById("unavailable-route").hidden = true;
  const form = document.getElementById("task-search-form");
  const input = document.getElementById("task-search-input");
  const archived = document.getElementById("task-search-archived");
  const status = document.getElementById("task-search-state");
  const list = document.getElementById("task-search-results");
  const paging = document.getElementById("task-search-paging");
  const retry = document.getElementById("task-search-retry");
  const params = new URLSearchParams(location.search);
  const submitted = {
    q: params.get("q") || "",
    includeArchived: params.get("include_archived") === "true",
    offset: Number(params.get("offset") || 0) || 0,
  };
  input.value = submitted.q;
  archived.checked = submitted.includeArchived;
  let loadGeneration = 0;

  const searchPath = (criteria, nextOffset = 0) => {
    const next = new URLSearchParams();
    if (criteria.q) next.set("q", criteria.q);
    if (criteria.includeArchived) next.set("include_archived", "true");
    if (nextOffset) next.set("offset", String(nextOffset));
    return "/search" + (next.size ? "?" + next : "");
  };
  const navigate = (criteria, nextOffset = 0) => location.assign(searchPath(criteria, nextOffset));
  const safeReturn = () => searchPath(submitted, submitted.offset);
  const statusLabel = {inbox: "Inbox", planned: "計画済み", next: "次にやる", doing: "実行中", waiting: "待機", scheduled: "予定", someday: "いつかやる", done: "完了"};

  const render = (payload) => {
    list.replaceChildren();
    for (const item of payload.items) {
      const li = document.createElement("li");
      const link = document.createElement("a");
      const meta = document.createElement("p");
      const frontmatter = item.frontmatter || {};
      link.href = "/clarify?id=" + encodeURIComponent(item.id) + "&return_to=" + encodeURIComponent(safeReturn());
      link.textContent = frontmatter.title || item.id;
      meta.className = "muted";
      meta.textContent = [
        item.id,
        statusLabel[frontmatter.status] || "状態不明",
        item.project_title || frontmatter.project_id || "Projectなし",
        item.archived ? "保管済み" : "",
      ].filter(Boolean).join(" · ");
      li.append(link, meta);
      list.append(li);
    }
    status.textContent = payload.total ? payload.total + "件" : "該当するTaskはありません。";
    paging.replaceChildren();
    if (submitted.offset > 0) {
      const previous = document.createElement("button");
      previous.type = "button";
      previous.className = "secondary";
      previous.textContent = "前へ";
      previous.onclick = () => navigate(submitted, Math.max(0, submitted.offset - payload.limit));
      paging.append(previous);
    }
    if (submitted.offset + payload.items.length < payload.total) {
      const next = document.createElement("button");
      next.type = "button";
      next.className = "secondary";
      next.textContent = "次へ";
      next.onclick = () => navigate(submitted, submitted.offset + payload.limit);
      paging.append(next);
    }
  };

  const load = async () => {
    const generation = ++loadGeneration;
    const query = new URLSearchParams({
      q: submitted.q,
      include_archived: String(submitted.includeArchived),
      offset: String(submitted.offset),
    });
    status.textContent = "読み込み中です。";
    retry.hidden = true;
    try {
      const response = await fetch("/api/v1/tasks/search?" + query, {headers: {Accept: "application/json"}});
      if (!response.ok) throw new Error("search failed");
      const payload = await response.json();
      if (generation === loadGeneration) render(payload);
    } catch (_error) {
      if (generation !== loadGeneration) return;
      status.textContent = "検索を読み込めませんでした。再試行してください。";
      retry.hidden = false;
    }
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    navigate({q: input.value, includeArchived: archived.checked}, 0);
  });
  retry.addEventListener("click", load);
  load();
})();
