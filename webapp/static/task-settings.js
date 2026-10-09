"use strict";

(function taskSettingsModule(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.TaskSettings = api;
})(typeof globalThis === "object" ? globalThis : this, function createTaskSettings() {
  const GROUPS = Object.freeze([
    Object.freeze({id: "basic", label: "基本"}),
    Object.freeze({id: "date_time", label: "日付・時刻"}),
    Object.freeze({id: "start_conditions", label: "開始条件"}),
    Object.freeze({id: "work_info", label: "作業情報"}),
    Object.freeze({id: "body", label: "本文"}),
  ]);
  const FIELD_NAMES = Object.freeze([
    "title", "status", "project_id", "area_id", "contexts", "estimated_minutes",
    "due", "action_date", "available_from", "waiting_for", "scheduled_start",
    "scheduled_end", "depends_on",
  ]);
  const mountedSessions = new WeakMap();
  const mountedDetails = new WeakSet();

  function own(value, key) {
    return Boolean(value) && Object.prototype.hasOwnProperty.call(value, key);
  }

  function availableFromParts(value) {
    if (typeof value !== "string" || !value) return {date: "", time: ""};
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return {date: value, time: ""};
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value) && /(?:\+09:00)$/.test(value)) {
      return {date: value.slice(0, 10), time: value.slice(11, 16)};
    }
    const instant = new Date(value);
    if (Number.isNaN(instant.getTime())) return {date: "", time: ""};
    const tokyo = new Date(instant.getTime() + 9 * 60 * 60 * 1000).toISOString();
    return {date: tokyo.slice(0, 10), time: tokyo.slice(11, 16)};
  }

  function availableFromValue(date, time) {
    if (!date) return "";
    return time ? `${date}T${time.slice(0, 5)}:00+09:00` : date;
  }

  function formatAvailableFrom(value) {
    const parts = availableFromParts(value);
    return parts.date + (parts.time ? ` ${parts.time}` : "");
  }

  function tokyoDate(now) {
    return new Date(now.getTime() + 9 * 60 * 60 * 1000).toISOString().slice(0, 10);
  }

  function availableFromIsFuture(value, now = new Date()) {
    if (!value) return false;
    return value.length === 10 ? value > tokyoDate(now) : new Date(value) > now;
  }

  function contextsForInput(value) {
    return typeof value === "string" && value.startsWith("[") && value.endsWith("]") ? value.slice(1, -1) : "";
  }

  function contextsForField(value) {
    if (!value.trim()) return "";
    const tokens = value.split(",").map((item) => item.trim());
    const forbidden = [" ", "\t", "\r", "\n", ",", "[", "]", "\"", "'"];
    if (tokens.some((token) => !token || forbidden.some((character) => token.includes(character)))) throw new Error("invalid_contexts");
    return `[${tokens.join(", ")}]`;
  }

  function contextsForDisplay(value) {
    const input = contextsForInput(value);
    return input ? input.split(",").map((item) => item.trim()).filter(Boolean).map((item) => item.startsWith("@") ? item : `@${item}`) : [];
  }

  function normalizeDraft(draft, normalizeTimestamp = (value) => value) {
    const result = {...draft};
    result.contexts = contextsForField(result.contexts || "");
    for (const name of ["estimated_minutes", "due", "action_date"]) result[name] = (result[name] || "").trim();
    result.scheduled_start = normalizeTimestamp(result.scheduled_start || "");
    result.scheduled_end = normalizeTimestamp(result.scheduled_end || "");
    return result;
  }

  function dependencyIds(value) {
    if (typeof value !== "string" || !value.startsWith("[") || !value.endsWith("]")) return [];
    return [...new Set(value.slice(1, -1).split(",").map((item) => item.trim()).filter(Boolean))];
  }

  function serializeDependencyIds(ids) {
    return `[${ids.join(", ")}]`;
  }

  function taskStatusLabel(status) {
    return ({inbox: "Inbox", planned: "計画中", next: "次にやる", doing: "実行中", waiting: "待機", scheduled: "予定", someday: "いつかやる", done: "完了"})[status] || status || "状態不明";
  }

  function dependencyLabel(entity, fallback) {
    const fm = entity && entity.frontmatter || {};
    return `${fm.title || fallback}（${fm.status === "done" ? "完了" : (fm.status || "状態不明")}）`;
  }

  function taskDependencyProgress(entity, tasks) {
    const ids = dependencyIds(entity && entity.frontmatter && entity.frontmatter.depends_on);
    const byId = new Map((tasks || []).map((task) => [task.id, task]));
    const unfinished = ids.filter((id) => ((byId.get(id) || {}).frontmatter || {}).status !== "done");
    return {total: ids.length, complete: ids.length - unfinished.length, unfinished: unfinished.map((id) => dependencyLabel(byId.get(id), id))};
  }

  function taskStartBlockReason(entity, tasks, now = new Date()) {
    const fm = entity && entity.frontmatter || {};
    if (availableFromIsFuture(fm.available_from, now)) return `着手可能${availableFromParts(fm.available_from).time ? "日時は " : "日は "}${formatAvailableFrom(fm.available_from)} です`;
    return taskDependencyProgress(entity, tasks).unfinished.length ? "未完了の前提Taskがあります" : "";
  }

  function taskMeta(entity, projects, includeProject = true, formatTimestamp = (value) => value) {
    const fm = entity && entity.frontmatter || {}, parts = [], project = projects && projects.get(fm.project_id);
    if (includeProject && project) parts.push((project.frontmatter || {}).title || project.id);
    parts.push(...contextsForDisplay(fm.contexts));
    if (fm.estimated_minutes) parts.push(`${fm.estimated_minutes}分`);
    if (fm.action_date) parts.push(`対応予定日 ${fm.action_date}`);
    if (fm.due) parts.push(`期限 ${fm.due}`);
    if (fm.available_from) parts.push(`着手可能 ${formatAvailableFrom(fm.available_from)}`);
    if (fm.status === "next" && !fm.action_date) parts.push("いつでも着手可");
    if (fm.scheduled_start) parts.push(`予定 ${formatTimestamp(fm.scheduled_start)}${fm.scheduled_end ? `〜${formatTimestamp(fm.scheduled_end)}` : ""}`);
    return parts;
  }

  function clarifyDraft(detail) {
    const entity = detail && typeof detail === "object" ? detail : {};
    const fm = entity.frontmatter && typeof entity.frontmatter === "object" ? entity.frontmatter : entity;
    const available = availableFromParts(fm.available_from);
    const draft = {};
    for (const name of FIELD_NAMES) draft[name] = typeof fm[name] === "string" ? fm[name] : "";
    draft.available_date = available.date;
    draft.available_time = available.time;
    draft.body = typeof entity.body === "string" ? entity.body : "";
    return draft;
  }

  function groupDefaults(draft) {
    const value = draft || {};
    return {
      basic: true,
      date_time: ["next", "scheduled"].includes(value.status) || Boolean(
        value.due || value.action_date || value.available_from || value.available_date ||
        value.available_time || value.scheduled_start || value.scheduled_end
      ),
      start_conditions: value.status === "waiting" || Boolean(
        value.available_from || value.available_date || value.available_time || value.waiting_for || value.depends_on
      ),
      work_info: Boolean(value.contexts || value.estimated_minutes),
      body: Boolean(value.body),
    };
  }

  function createSession({initial = {}, draft = initial} = {}) {
    let current = {...initial, ...draft};
    const userOpen = new Set();
    const userClosed = new Set();
    let automatic = groupDefaults(current);
    return Object.freeze({
      draft() { return {...current}; },
      isOpen(id) {
        if (userOpen.has(id)) return true;
        if (userClosed.has(id)) return false;
        return Boolean(automatic[id]);
      },
      setOpen(id, open, byUser = false) {
        if (!GROUPS.some((group) => group.id === id)) return;
        if (byUser) {
          (open ? userOpen : userClosed).add(id);
          (open ? userClosed : userOpen).delete(id);
        }
        automatic = {...automatic, [id]: Boolean(open)};
      },
      update(patch = {}) {
        current = {...current, ...patch};
        automatic = groupDefaults(current);
        return {...current};
      },
    });
  }

  function serialize({initial = {}, draft = {}} = {}) {
    const fields = {};
    for (const name of FIELD_NAMES) {
      if (name === "depends_on" && !own(draft, name) && !own(initial, name)) continue;
      if (name === "available_from") {
        const hasParts = own(draft, "available_date") || own(draft, "available_time");
        fields[name] = hasParts
          ? availableFromValue(draft.available_date || "", draft.available_time || "")
          : (own(draft, name) ? draft[name] : (initial[name] || ""));
      } else fields[name] = own(draft, name) ? draft[name] : (initial[name] || "");
    }
    return {fields, body: own(draft, "body") ? draft.body : ""};
  }

  function syncClarifyConditions(draft = {}) {
    return {
      waitingVisible: draft.status === "waiting" || Boolean(draft.waiting_for),
      scheduledVisible: draft.status === "scheduled" || Boolean(draft.scheduled_start || draft.scheduled_end),
      actionDateConstrained: ["inbox", "planned"].includes(draft.status),
    };
  }

  function guardActionDateSubmit(status, input, form) {
    const invalid = ["inbox", "planned"].includes(status) && Boolean((input.value || "").trim());
    input.setCustomValidity(invalid ? "Inbox・plannedへ保存するには対応予定日を空にしてください。" : "");
    if (invalid && form && typeof form.reportValidity === "function") form.reportValidity();
    return !invalid;
  }

  function syncClarifyAdvanced(session, draft) {
    if (session && typeof session.update === "function") session.update(draft || {});
    return GROUPS.reduce((result, group) => ({...result, [group.id]: session.isOpen(group.id)}), {});
  }

  function mount(rootElement, session) {
    const detailsById = new Map();
    for (const group of GROUPS) {
      const details = rootElement && rootElement.querySelector
        ? rootElement.querySelector(`[data-task-settings-group="${group.id}"]`)
        : null;
      if (!details) continue;
      detailsById.set(group.id, details);
      mountedSessions.set(details, session);
      if (!mountedDetails.has(details)) {
        details.addEventListener("toggle", () => {
          const active = mountedSessions.get(details);
          if (active && details.open !== active.isOpen(group.id)) active.setOpen(group.id, details.open, true);
        });
        mountedDetails.add(details);
      }
    }
    const controller = Object.freeze({
      sync() {
        for (const [id, details] of detailsById) details.open = session.isOpen(id);
        return controller;
      },
    });
    return controller.sync();
  }

  function mountForm(adapter) {
    const initialEntity = adapter.initial && adapter.initial.frontmatter
      ? adapter.initial
      : {frontmatter: adapter.initial || {}, body: adapter.body || ""};
    const initialDraft = clarifyDraft(initialEntity);
    if (typeof adapter.writeDraft === "function") adapter.writeDraft(initialDraft);
    const session = createSession({initial: initialDraft});
    const view = mount(adapter.root, session);
    return Object.freeze({
      session,
      view,
      sync() {
        const draft = adapter.readDraft();
        session.update(draft);
        view.sync();
        return draft;
      },
      serialize() {
        const draft = adapter.readDraft();
        session.update(draft);
        view.sync();
        return serialize({initial: initialEntity.frontmatter || {}, draft});
      },
    });
  }

  async function confirmBlockedNext(operation, request, isCurrent, knownTask) {
    if (!operation || operation.action !== "update" || operation.kind !== "tasks" || !operation.fields || operation.fields.status !== "next" || operation.confirm_blocked_next === true) return operation;
    if (operation.fields.project_id === "" || (own(operation.fields, "depends_on") && !dependencyIds(operation.fields.depends_on).length)) return operation;
    if (knownTask && knownTask.id === operation.id && knownTask.content_hash === operation.base_hash && !dependencyIds((knownTask.frontmatter || {}).depends_on).length) return operation;
    const detail = await request("/api/v1/entities/tasks/" + encodeURIComponent(operation.id));
    const fields = detail.frontmatter || {}, dependencies = dependencyIds(fields.depends_on);
    if (fields.status === "next" || !fields.project_id || !dependencies.length) return operation;
    const snapshot = await request("/api/v1/snapshot"), byId = new Map(snapshot.entities.map((entity) => [entity.id, entity]));
    if (dependencies.every((id) => byId.has(id) && byId.get(id).frontmatter.status === "done")) return operation;
    if (isCurrent && !isCurrent()) return null;
    if (!globalThis.confirm("前提Taskが未完了です。Nextとして計画に残しますが、すべての前提が完了するまで開始できません。続けますか？")) return null;
    return {...operation, confirm_blocked_next: true};
  }

  function previewNeedsConfirmation(previewResponse) {
    const operation = previewResponse && previewResponse.operation;
    if (operation && (operation.action === "archive" || operation.action === "cycle_close")) return true;
    if (Array.isArray(previewResponse && previewResponse.effects)) return previewResponse.effects.some((effect) => effect && (effect.role === "project_archived" || effect.role === "task_archived"));
    return Boolean(previewResponse && previewResponse.proposed && previewResponse.proposed.archived === true);
  }

  return Object.freeze({
    GROUPS,
    confirmBlockedNext,
    previewNeedsConfirmation,
    availableFromParts,
    availableFromValue,
    formatAvailableFrom,
    availableFromIsFuture,
    contextsForInput,
    contextsForField,
    contextsForDisplay,
    normalizeDraft,
    dependencyIds,
    serializeDependencyIds,
    dependencyLabel,
    taskDependencyProgress,
    taskStartBlockReason,
    taskStatusLabel,
    taskMeta,
    createSession,
    serialize,
    mount,
    mountForm,
    clarifyDraft,
    syncClarifyConditions,
    guardActionDateSubmit,
    syncClarifyAdvanced,
  });
});
