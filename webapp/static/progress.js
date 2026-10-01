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
  root.ProgressUI = Object.freeze({PURPOSE, FLOW, body, parse, originFields, selectOrigin});
})(window);
