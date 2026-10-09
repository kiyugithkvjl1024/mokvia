"use strict";

const assert = require("node:assert/strict");

globalThis.window = globalThis;
require("../webapp/static/progress.js");

assert.equal(ProgressUI.PURPOSE, "この記録は、週次で振り返り、月次の実績整理にまとめます。将来は評価資料・職務経歴書・面接・ライフプランの根拠として再利用します。");
assert.equal(ProgressUI.FLOW, "今日の変化 → 週次で振り返り・補完 → 月次実績台帳 → 用途別に再利用");
const body = ProgressUI.body("学び", "URL");
assert.equal(body, "## メリット・学び\n\n学び\n\n## 証拠\n\nURL\n");
assert.deepEqual(ProgressUI.parse(body), {benefit: "学び", evidence: "URL"});
assert.deepEqual(ProgressUI.originFields("goal", "goal-1"), {project_id: "", goal_id: "goal-1", area_id: ""});

const archivedSelect = {
  value: "",
  options: [{value: "", textContent: "未分類"}],
  append(option) { this.options.push(option); },
};
globalThis.document = {createElement() { return {value: "", textContent: ""}; }};
ProgressUI.selectOrigin(archivedSelect, "project", "project-archived");
assert.equal(archivedSelect.value, "project:project-archived");
assert.deepEqual(
  archivedSelect.options.map((option) => [option.value, option.textContent]),
  [["", "未分類"], ["project:project-archived", "Project: project-archived（アーカイブ済み）"]],
);
assert.deepEqual(
  ProgressUI.originFields(...archivedSelect.value.split(":")),
  {project_id: "project-archived", goal_id: "", area_id: ""},
);
