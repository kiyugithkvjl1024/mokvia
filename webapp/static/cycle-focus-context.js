(function(root) {
  "use strict";
  function create({elements, safeFrontmatter, dependencyIds, clarifyHref, tokyoToday, projectDetailHref}) {
function focusCycleContextModel(completeEntities, visibleEntities, facts, now = new Date(), recompute = false) {
  const roadmap = facts && facts.roadmap || {}, entities = (completeEntities || []).filter((entity) => entity && entity.archived !== true);
  const byId = new Map(entities.map((entity) => [entity.id, entity]));
  const cycle = byId.get(roadmap.active_cycle_id);
  if (!cycle || cycle.kind !== "cycles") return {cycle: null, outcomes: [], suggestion: ""};
  const today = facts.mokvia_today, visibleIds = new Set((visibleEntities || []).map((entity) => entity.id));
  const baselineCandidates = Array.isArray(facts.focus_actionable_next_ids) ? new Set(facts.focus_actionable_next_ids) : null;
  const doneIds = new Set(Array.isArray(facts.focus_done_task_ids) ? facts.focus_done_task_ids : []);
  for (const entity of completeEntities || []) if (entity.kind === "tasks") { if (safeFrontmatter(entity).status === "done") doneIds.add(entity.id); else doneIds.delete(entity.id); }
  const ready = (task) => {
    const fm = safeFrontmatter(task), available = fm.available_from;
    if (!recompute && baselineCandidates) return baselineCandidates.has(task.id);
    if (fm.status !== "next" || fm.waiting_for || (fm.action_date && fm.action_date > today)) return false;
    if (available && (available.length === 10 ? available > today : new Date(available) > now)) return false;
    return dependencyIds(fm.depends_on).every((id) => doneIds.has(id));
  };
  const outcomes = (Array.isArray(roadmap.now_outcome_ids) ? roadmap.now_outcome_ids : []).map((id) => {
    const outcome = byId.get(id);
    if (!outcome || outcome.kind !== "roadmap_outcomes" || safeFrontmatter(outcome).status !== "active") return null;
    const projects = entities.filter((entity) => entity.kind === "projects" && safeFrontmatter(entity).roadmap_outcome_id === id && safeFrontmatter(entity).status === "doing").map((project) => ({
      project,
      tasks: entities.filter((entity) => entity.kind === "tasks" && safeFrontmatter(entity).project_id === project.id && visibleIds.has(entity.id) && ready(entity)),
    }));
    return {outcome, projects, suggestion: projects.length > 1 ? `このOutcomeの実行中Projectが${projects.length}件あります。今の集中先を1件選ぶことを提案します。` : ""};
  }).filter(Boolean);
  return {cycle, outcomes};
}
function renderFocusCycleContext(completeEntities, visibleEntities, facts) {
  const host = elements.focusCycleContext;
  if (!host) return;
  host.replaceChildren();
  const model = focusCycleContextModel(completeEntities, visibleEntities, facts, new Date(), globalThis.focusCanonicalPending === true);
  const heading = document.createElement("h2"); heading.textContent = "Active Cycleから次の行動へ"; host.append(heading);
  if (!model.cycle) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "Active Cycleはありません。"; host.append(empty); return; }
  const title = document.createElement("p"); title.className = "focus-cycle-title"; title.textContent = safeFrontmatter(model.cycle).title || model.cycle.id; host.append(title);
  for (const group of model.outcomes) {
    const section = document.createElement("section"), label = document.createElement("h3");
    label.textContent = safeFrontmatter(group.outcome).title || group.outcome.id; section.append(label);
    for (const item of group.projects) {
      const row = document.createElement("div"), link = document.createElement("a"); row.className = "focus-cycle-project";
      link.href = projectDetailHref(item.project.id); link.textContent = safeFrontmatter(item.project).title || item.project.id; row.append(link);
      const list = document.createElement("ul");
      for (const task of item.tasks) { const li = document.createElement("li"), taskLink = document.createElement("a"); taskLink.href = clarifyHref(task.id); taskLink.textContent = safeFrontmatter(task).title || task.id; li.append(taskLink); list.append(li); }
      if (!item.tasks.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "表示条件に合う実行可能なNext Actionはありません。"; list.append(li); }
      row.append(list); section.append(row);
    }
    if (!group.projects.length) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "実行中Projectはありません。"; section.append(empty); }
    if (group.suggestion) { const suggestion = document.createElement("p"); suggestion.className = "focus-cycle-suggestion"; suggestion.textContent = group.suggestion; section.append(suggestion); }
    host.append(section);
  }
}
function cycleProjectTimelineModel(snapshot, today) {
  const entities = Array.isArray(snapshot && snapshot.entities) ? snapshot.entities : [], facts = snapshot && snapshot.facts && snapshot.facts.roadmap || {};
  const byId = new Map(entities.filter((entity) => entity.archived !== true).map((entity) => [entity.id, entity]));
  const cycle = byId.get(facts.active_cycle_id);
  if (!cycle || cycle.kind !== "cycles") return {cycle: null, projects: [], todayPercent: null};
  const fm = safeFrontmatter(cycle), day = (value) => Date.parse(value + "T00:00:00Z") / 86400000;
  const projectDay = (value) => { if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith("0000")) return null; const parsed = Date.parse(value + "T00:00:00Z"); return Number.isFinite(parsed) && new Date(parsed).toISOString().slice(0, 10) === value ? parsed / 86400000 : null; };
  const first = day(fm.start_date), last = day(fm.end_date), days = last - first + 1;
  const todayIndex = day(today) - first;
  const todayPercent = todayIndex >= 0 && todayIndex < days ? ((todayIndex + .5) / days) * 100 : null;
  const outcomeIds = new Set(Array.isArray(facts.now_outcome_ids) ? facts.now_outcome_ids : []);
  const projects = entities.filter((entity) => entity.kind === "projects" && entity.archived !== true && outcomeIds.has(safeFrontmatter(entity).roadmap_outcome_id)).map((project) => {
    const fields = safeFrontmatter(project), start = fields.planned_start_date, end = fields.planned_end_date;
    const startDay = projectDay(start), endDay = projectDay(end), undated = startDay === null || endDay === null || endDay < startDay;
    const outside = !undated && (startDay < first || endDay > last), noOverlap = !undated && (endDay < first || startDay > last);
    const clippedStart = undated || noOverlap ? null : Math.max(first, startDay), clippedEnd = undated || noOverlap ? null : Math.min(last, endDay);
    return {project, undated, outside, startPercent: clippedStart === null ? null : ((clippedStart - first) / days) * 100, widthPercent: clippedStart === null ? null : ((clippedEnd - clippedStart + 1) / days) * 100, phase: fields.status === "not_started" ? "next" : fields.status};
  }).sort((left, right) => { if (left.undated !== right.undated) return left.undated ? 1 : -1; if (left.undated) return 0; const a = safeFrontmatter(left.project), b = safeFrontmatter(right.project); return a.planned_start_date.localeCompare(b.planned_start_date) || a.planned_end_date.localeCompare(b.planned_end_date); });
  return {cycle, projects, todayPercent};
}
function renderCycleProjectTimeline(snapshot) {
  const host = elements.cycleProjectTimeline;
  if (!host) return;
  host.replaceChildren();
  const model = cycleProjectTimelineModel(snapshot, snapshot.facts && snapshot.facts.mokvia_today || tokyoToday());
  if (!model.cycle) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "Active Cycleはありません。"; host.append(empty); return; }
  const cycleFm = safeFrontmatter(model.cycle), intro = document.createElement("p");
  const today = snapshot.facts && snapshot.facts.mokvia_today || tokyoToday();
  intro.className = "muted"; intro.textContent = (cycleFm.start_date || "?") + "〜" + (cycleFm.end_date || "?") + "を基準に表示。Projectの期間は目安です。" + (model.todayPercent === null ? "今日はCycle期間外（" + today + "）。" : "縦線は今日（" + today + "）。"); host.append(intro);
  const list = document.createElement("div"); list.className = "cycle-project-rows";
  for (const item of model.projects) {
    const fields = safeFrontmatter(item.project), row = document.createElement("div"), heading = document.createElement("div"), link = document.createElement("a"), statusTag = document.createElement("span"), track = document.createElement("div");
    row.className = "cycle-project-row"; heading.className = "cycle-project-heading"; link.href = projectDetailHref(item.project.id); link.textContent = fields.title || item.project.id;
    statusTag.className = "cycle-project-phase"; statusTag.textContent = ({doing: "実行中", next: "次の候補", completed: "完了", on_hold: "保留", dropped: "中止"})[item.phase] || item.phase; heading.append(link, statusTag); row.append(heading);
    track.className = "cycle-project-track";
    if (item.startPercent !== null) { const bar = document.createElement("span"); bar.className = "cycle-project-bar cycle-project-bar-" + item.phase; bar.style.left = item.startPercent + "%"; bar.style.width = item.widthPercent + "%"; bar.title = fields.planned_start_date + "〜" + fields.planned_end_date; track.append(bar); }
    if (model.todayPercent !== null) { const marker = document.createElement("span"); marker.className = "cycle-project-today"; marker.style.left = model.todayPercent + "%"; marker.setAttribute("aria-label", "今日"); track.append(marker); }
    row.append(track); const dates = document.createElement("p"); dates.className = "muted cycle-project-dates"; dates.textContent = item.undated ? "時期未設定" : (fields.planned_start_date + "〜" + fields.planned_end_date + (item.outside ? "（Cycle期間外）" : "")); row.append(dates); list.append(row);
  }
  if (!model.projects.length) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "このCycleに紐づくProjectはありません。"; list.append(empty); }
  host.append(list);
}
    return Object.freeze({focusCycleContextModel, renderFocusCycleContext, cycleProjectTimelineModel, renderCycleProjectTimeline});
  }
  root.CycleFocusContext = Object.freeze({create});
})(typeof window !== "undefined" ? window : globalThis);
