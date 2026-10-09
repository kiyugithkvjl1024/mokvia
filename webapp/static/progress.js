"use strict";

(function (root) {
  const PURPOSE = "この記録は、週次で振り返り、月次の実績整理にまとめます。将来は評価資料・職務経歴書・面接・ライフプランの根拠として再利用します。";
  const FLOW = "今日の変化 → 週次で振り返り・補完 → 月次実績台帳 → 用途別に再利用";
  function body(benefit, evidence) {
    const sections = [];
    if (String(benefit || "").trim()) sections.push("## メリット・学び\n\n" + String(benefit).trim());
    if (String(evidence || "").trim()) sections.push("## 証拠\n\n" + String(evidence).trim());
    return sections.join("\n\n") + (sections.length ? "\n" : "");
  }
  function section(value, heading) {
    const lines = String(value || "").split(/\r?\n/); const start = lines.indexOf(heading);
    if (start < 0) return "";
    const end = lines.findIndex((line, index) => index > start && line.startsWith("## "));
    return lines.slice(start + 1, end < 0 ? lines.length : end).join("\n").trim();
  }
  function parse(value) { return {benefit: section(value, "## メリット・学び"), evidence: section(value, "## 証拠")}; }
  function originFields(kind, id) { return {project_id: kind === "project" ? id : "", goal_id: kind === "goal" ? id : "", area_id: kind === "area" ? id : ""}; }
  function selectOrigin(select, kind, id) {
    const value = kind && id ? kind + ":" + id : "";
    if (value && !Array.from(select.options || []).some((option) => option.value === value)) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = ({project: "Project", goal: "Goal", area: "Area"}[kind] || kind) + ": " + id + "（アーカイブ済み）";
      select.append(option);
    }
    select.value = value;
  }
  function range(kind, today, weekStart) {
    const start = kind === "daily" ? today : weekStart;
    const date = new Date(start + "T00:00:00Z");
    date.setUTCDate(date.getUTCDate() + (kind === "daily" ? -1 : 6));
    return kind === "daily" ? {from: date.toISOString().slice(0, 10), to: today} : {from: weekStart, to: date.toISOString().slice(0, 10)};
  }
  function render(list, items, kind, dates, editButton) {
    list.replaceChildren();
    function row(item) {
      const element = document.createElement("li"); element.className = "entity-row"; element.dataset.progressId = item.id;
      const title = document.createElement("strong"); title.textContent = item.title || "タイトルなし";
      const meta = document.createElement("p"); meta.className = "entity-meta";
      meta.textContent = [item.occurred_on, item.origin ? item.origin.kind + ": " + item.origin.title : "未分類"].join(" · ");
      if (kind === "daily") element.append(title, meta, editButton(item));
      else { const link = document.createElement("a"); link.href = "/reviews/weekly?tab=daily&progress=" + encodeURIComponent(item.id); link.textContent = "Dailyで編集"; element.append(title, meta, link); }
      return element;
    }
    if (kind !== "daily") { for (const item of items) list.append(row(item)); return items.length; }
    let count = 0;
    for (const [date, label] of [[dates.from, "昨日"], [dates.to, "今日"]]) {
      const group = document.createElement("li"); group.className = "progress-day-group"; group.dataset.progressDate = date;
      const heading = document.createElement("h3"); heading.textContent = label + "（" + date + "）";
      const entries = document.createElement("ul"); entries.className = "entity-list";
      const selected = items.filter(item => item.occurred_on === date); count += selected.length;
      for (const item of selected) entries.append(row(item));
      if (!selected.length) { const empty = document.createElement("li"); empty.className = "entity-empty"; empty.textContent = label + "の記録はありません。"; entries.append(empty); }
      group.append(heading, entries); list.append(group);
    }
    return count;
  }
  root.ProgressUI = Object.freeze({PURPOSE, FLOW, body, parse, originFields, selectOrigin, range, render});
})(window);
