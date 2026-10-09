"use strict";

/* Shared Task card and explicit operation primitives. */
(function attachTaskCard(global) {
  const fieldsOf = (task) => task && task.frontmatter && typeof task.frontmatter === "object" ? task.frontmatter : task || {};
  const text = (task) => String(fieldsOf(task).title || task.id || "タイトルなし");
  const date = (task) => String(fieldsOf(task).action_date || "");
  function scheduledDate(task) {
    const value = String(fieldsOf(task).scheduled_start || ""), instant = new Date(value);
    if (!value || Number.isNaN(instant.getTime())) return "";
    const values = Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(instant).filter((part) => part.type !== "literal").map((part) => [part.type, part.value]));
    return values.year + "-" + values.month + "-" + values.day;
  }
  function focusDate(task) { return fieldsOf(task).status === "scheduled" ? scheduledDate(task) : date(task); }
  function focusMoveFields(task, actionDate) {
    const fields = fieldsOf(task); if (fields.status !== "scheduled") return {action_date: actionDate}; if (!actionDate) return {scheduled_start: "", scheduled_end: ""};
    const parts = (value) => { const instant = new Date(value); if (Number.isNaN(instant.getTime())) return null; return Object.fromEntries(new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"}).formatToParts(instant).filter((part) => part.type !== "literal").map((part) => [part.type, part.value])); };
    const start = parts(fields.scheduled_start); if (!start || !/^\d{4}-\d{2}-\d{2}$/.test(actionDate)) return null;
    const date = (value) => value.year + "-" + value.month + "-" + value.day, timestamp = (value, day) => day + "T" + value.hour + ":" + value.minute + ":" + value.second + "+09:00", utc = (day) => { const [year, month, dayOfMonth] = day.split("-").map(Number); return Date.UTC(year, month - 1, dayOfMonth); }, calendar = (value, from) => new Date(Date.UTC(Number(value.year), Number(value.month) - 1, Number(value.day)) + utc(actionDate) - utc(from)).toISOString().slice(0, 10);
    const result = {scheduled_start: timestamp(start, actionDate)}, end = parts(fields.scheduled_end); if (end) result.scheduled_end = timestamp(end, calendar(end, date(start))); return result;
  }
  function applyFocusMove(fields, changes) {
    const previous = new Map(Object.keys(changes).map((key) => [key, {has: Object.prototype.hasOwnProperty.call(fields, key), value: fields[key]}]));
    for (const [key, value] of Object.entries(changes)) { if (value) fields[key] = value; else delete fields[key]; }
    return () => { for (const [key, value] of previous) { if (value.has) fields[key] = value.value; else delete fields[key]; } };
  }
  function classifyFocusTask(task, today) {
    const fields = fieldsOf(task);
    const scheduled = focusDate(task);
    if (fields.status === "next" || fields.status === "scheduled") return scheduled ? (scheduled === today ? "today" : "dated") : "undated";
    return null;
  }
  function sortFocusTasks(tasks) {
    return [...tasks].sort((left, right) => focusDate(left).localeCompare(focusDate(right)) || String(fieldsOf(left).due || "").localeCompare(String(fieldsOf(right).due || "")) || text(left).localeCompare(text(right), "ja") || String(left.id).localeCompare(String(right.id)));
  }
  function focusSections(tasks, today) {
    return ["today", "undated", "dated"].map((key) => ({key, tasks: sortFocusTasks((tasks || []).filter((task) => classifyFocusTask(task, today) === key))}));
  }
  function isOverdueActionDate(task, today) { return classifyFocusTask(task, today) === "dated" && date(task) < today; }
  function nextIsoDate(value) { const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || "")); if (!match) return ""; const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) + 1)); return date.toISOString().slice(0, 10); }
  function attachPointerDrag(handle, options) {
    let drag = null; const threshold = Number(options.threshold) > 0 ? Number(options.threshold) : 8, motion = global.ProjectKanbanMotion;
    const clear = () => { if (!drag) return; if (typeof document.removeEventListener === "function") { document.removeEventListener("pointermove", documentMove, true); document.removeEventListener("pointerup", documentEnd, true); document.removeEventListener("pointercancel", documentCancel, true); } motion.stopAutoScroll(drag); motion.cancelPointerGesture(drag); handle.classList.remove("task-card-drag-active"); drag = null; };
    const fallback = (event) => Boolean(drag && drag.active && drag.pointerId === event.pointerId && (!handle.hasPointerCapture || !handle.hasPointerCapture(event.pointerId)));
    const move = (event) => {
      const result = motion.movePointerGesture(drag, event, {threshold});
      if (!result.tracking || !result.active) return;
      if (result.activated) {
        handle.classList.add("task-card-drag-active");
        if (typeof options.onActivate === "function") options.onActivate(event);
        const tick = () => { if (!drag || !drag.active) return; motion.autoScroll(drag, tick); if (typeof options.onTrack === "function") options.onTrack({clientX: drag.lastX, clientY: drag.lastY, pointerId: drag.pointerId}); };
        motion.beginAutoScroll(drag, tick);
      }
      drag.lastX = event.clientX; drag.lastY = event.clientY;
      if (typeof options.onTrack === "function") options.onTrack(event);
    };
    const end = (event) => { if (!drag || drag.pointerId !== event.pointerId) return; const active = drag.active, target = active && options.resolveTarget ? options.resolveTarget(event) : null, gated = options.isGated && options.isGated(); motion.finishPointerGesture(drag, event, {activationProperty: "__taskCardSuppressActivation"}); clear(); if (active && target && !gated) options.onDrop(target, event); else if (active && typeof options.onCancel === "function") options.onCancel(event); };
    const cancel = (event) => { const active = Boolean(drag && drag.active); if (!drag || drag.pointerId !== event.pointerId) return; clear(); if (active && typeof options.onCancel === "function") options.onCancel(event); };
    const documentMove = (event) => { if (fallback(event)) move(event); };
    const documentEnd = (event) => { if (fallback(event)) end(event); };
    const documentCancel = (event) => { if (fallback(event)) cancel(event); };
    handle.addEventListener("pointerdown", (event) => {
      if (options.acceptStart && !options.acceptStart(event)) return;
      handle.__taskCardSuppressActivation = false;
      drag = motion.beginPointerGesture(event, handle, {isGated: options.isGated, captureOnStart: false});
      if (drag && typeof document.addEventListener === "function") { document.addEventListener("pointermove", documentMove, true); document.addEventListener("pointerup", documentEnd, true); document.addEventListener("pointercancel", documentCancel, true); }
    });
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", cancel);
  }
  function optimisticRelocate(card, target, motion, cards = [card]) {
    const origin = card.parentNode, originIndex = origin ? [...origin.children].indexOf(card) : -1, before = motion ? motion.capture(cards) : new Map(); let restored = false;
    target.append(card); if (motion) motion.flip(before);
    return () => { if (restored || !origin) return; restored = true; const current = motion ? motion.capture(cards) : new Map(); origin.insertBefore(card, origin.children[originIndex] || null); if (motion) motion.flip(current); };
  }
  function appendDragGrip(handle) {
    handle.textContent = "";
    const svg = document.createElementNS ? document.createElementNS("http://www.w3.org/2000/svg", "svg") : document.createElement("svg"), path = document.createElementNS ? document.createElementNS("http://www.w3.org/2000/svg", "path") : document.createElement("path");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("aria-hidden", "true"); path.setAttribute("d", "M8 5h2v2H8zm6 0h2v2h-2zM8 11h2v2H8zm6 0h2v2h-2zM8 17h2v2H8zm6 0h2v2h-2z"); svg.append(path); handle.append(svg);
    return handle;
  }
  function appendProjectIcon(link) {
    const icon = document.createElementNS ? document.createElementNS("http://www.w3.org/2000/svg", "svg") : document.createElement("svg"), path = document.createElementNS ? document.createElementNS("http://www.w3.org/2000/svg", "path") : document.createElement("path");
    icon.setAttribute("class", "task-card-project-icon"); icon.setAttribute("viewBox", "0 0 24 24"); icon.setAttribute("aria-hidden", "true"); path.setAttribute("d", "M3 6.5A1.5 1.5 0 0 1 4.5 5H10l2 2h7.5A1.5 1.5 0 0 1 21 8.5v9A1.5 1.5 0 0 1 19.5 19h-15A1.5 1.5 0 0 1 3 17.5z"); icon.append(path); link.append(icon);
  }
  function appendProjectStrip(card, project) {
    if (!project || !project.id || !project.title) return;
    const link = document.createElement("a"), copy = document.createElement("span"), label = document.createElement("span"), title = document.createElement("span"), chevron = document.createElement("span");
    link.className = "task-card-project"; link.setAttribute("href", project.href || "#"); link.setAttribute("aria-label", "Project詳細を開く: " + project.title);
    appendProjectIcon(link); copy.className = "task-card-project-copy"; label.className = "task-card-project-label"; label.textContent = "PROJECT"; title.className = "task-card-project-title"; title.textContent = project.title; copy.append(label, title); chevron.className = "task-card-project-chevron"; chevron.setAttribute("aria-hidden", "true"); chevron.textContent = "›";
    link.append(copy, chevron); card.append(link);
  }
  function createTaskCard({task, href, metadata = [], project = null, handleLabel = "", onHandlePointerDown, onHandleActivate, actions = []}) {
    const card = document.createElement("li"); card.className = "task-card"; card.dataset.taskId = task.id;
    const title = document.createElement(href ? "a" : "span"); title.className = "task-card-title"; if (href) title.setAttribute("href", href); title.textContent = text(task); card.append(title);
    appendProjectStrip(card, project);
    if (metadata.length) { const meta = document.createElement("p"); meta.className = "task-card-meta"; meta.textContent = metadata.filter(Boolean).join(" · "); card.append(meta); }
    if (handleLabel) { const handle = document.createElement("button"); handle.type = "button"; handle.className = "task-card-handle touch-target-48"; handle.setAttribute("aria-label", handleLabel); appendDragGrip(handle); if (onHandlePointerDown) handle.addEventListener("pointerdown", onHandlePointerDown); if (onHandleActivate) { handle.setAttribute("aria-haspopup", "dialog"); handle.addEventListener("click", (event) => { if (handle.__taskCardSuppressActivation) { handle.__taskCardSuppressActivation = false; event.preventDefault(); return; } onHandleActivate(event); }); } card.append(handle); }
    if (actions.length) { const actionRow = document.createElement("div"); actionRow.className = "card-actions"; actionRow.append(...actions); card.append(actionRow); }
    return card;
  }
  function operations(kind, value, handlers) {
    const result = [], add = (key, label, group) => { if (typeof handlers[key] === "function") result.push({key, label, group, run: handlers[key]}); };
    if (kind === "meeting") {
      if (value.unavailable) return result;
      add(value.done ? "reopen" : "complete", value.done ? "完了取消" : "完了", "状態");
      if (value.editable) add("actual", "実績時刻", "その他");
      return result;
    }
    if (["next", "scheduled"].includes(value.status)) { add("start", "開始", "状態"); if (value.startBlocked && result.length) { result[result.length - 1].disabled = true; result[result.length - 1].reason = value.startBlocked; } add("complete", "完了", "状態"); }
    if (value.status === "doing") { add("interrupt", "中断", "状態"); add("complete", "完了", "状態"); }
    if (value.status === "done" && value.timer_kind !== "break" && value.title !== "5分休憩") add("continue", "続きのTaskを作る", "状態");
    if (["next", "scheduled"].includes(value.status)) {
      add("today", "今日やる", "対応予定日"); add("dated", "日付を選ぶ", "対応予定日"); add("undated", "予定日なし", "対応予定日");
    }
    add("due", "期限", "その他");
    add("details", "詳細編集", "その他");
    return result;
  }
  function dropOperations(model) { return model.filter(action => action.key !== "actual" && !action.disabled); }
  function operationDock(model, pointerPosition) {
    return global.ProjectKanbanMotion.createDestinationDock(dropOperations(model).map(action => ({key: action.key, label: action.label, group: action.group})), pointerPosition, "ここにドロップ");
  }
  function openOperations(model, trigger, isGated = () => false) {
    if (isGated() || !model.length) return;
    const dialog = document.createElement("dialog"), title = document.createElement("h2"), cancel = document.createElement("button");
    dialog.className = "task-operations-dialog"; title.textContent = "操作"; dialog.setAttribute("aria-label", "操作"); dialog.append(title);
    for (const group of ["状態", "対応予定日", "その他"]) {
      const actions = model.filter(action => action.group === group); if (!actions.length) continue;
      const section = document.createElement(group === "その他" ? "details" : "section"), heading = document.createElement(group === "その他" ? "summary" : "h3");
      heading.textContent = group; section.append(heading); const row = document.createElement("div"); row.className = "task-operations-row";
      for (const action of actions) { const button = document.createElement("button"); button.type = "button"; button.textContent = action.label; button.dataset.operation = action.key; button.disabled = Boolean(action.disabled); if (action.reason) { button.title = action.reason; const note = document.createElement("p"); note.textContent = action.reason; section.append(note); } button.addEventListener("click", () => { if (isGated() || action.disabled) return; dialog.close(); return action.run(); }); row.append(button); }
      section.append(row); dialog.append(section);
    }
    cancel.type = "button"; cancel.textContent = "キャンセル"; cancel.addEventListener("click", () => dialog.close()); dialog.append(cancel);
    dialog.addEventListener("close", () => { dialog.remove(); if (trigger && trigger.isConnected) trigger.focus(); }); document.body.append(dialog); dialog.showModal();
  }
async function completeUnstarted(entity, trigger, ctx) {
  if (ctx.mutationIsGated()) return;
  try {
    const detail = await ctx.apiRequest(ctx.entityDetailPath("tasks", entity.id)), fm = ctx.safeFrontmatter(detail);
    if (fm.status !== ctx.safeFrontmatter(entity).status || fm.status === "doing" || fm.work_started_at || fm.started_at || fm.work_ended_at) throw new ctx.RequestFailure("reconciliation");
    const fields = {status: "done"};
    await ctx.previewMutation({action: "update", kind: "tasks", id: detail.id, base_hash: detail.content_hash, fields}, async () => { await ctx.showCompleted(); }, trigger);
  } catch (error) { ctx.showRequestError(error); }
}
const pendingContinuations = new Set();
function continuationOperation(detail, actionDate) {
  const fm = fieldsOf(detail);
  if (fm.status !== "done" || (fm.timer_kind === "break" || fm.title === "5分休憩") || detail.archived === true) throw new Error("invalid_continuation_source");
  if (actionDate && !/^\d{4}-\d{2}-\d{2}$/.test(actionDate)) throw new Error("invalid_continuation_date");
  const fields = {title: fm.title, status: "next", continuation_of: detail.id};
  for (const key of ["project_id", "area_id", "due", "contexts", "estimated_minutes", "depends_on"]) if (fm[key]) fields[key] = fm[key];
  if (actionDate) fields.action_date = actionDate;
  const body = String(detail.body || "").replace(/^\[gtd-focus-monitor\] 通知停止期限: (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+09:00)(?:\n|$)/gm, (line, stamp) => stamp.slice(0, 4) === "0000" || Number.isNaN(Date.parse(stamp)) || new Date(Date.parse(stamp) + 9 * 3600000).toISOString().slice(0, 19) !== stamp.slice(0, 19) ? line : "");
  return {action: "create", kind: "tasks", fields, body};
}
async function createContinuation(entity, actionDate, trigger, ctx) {
  if (ctx.mutationIsGated() || pendingContinuations.has(entity.id)) return;
  pendingContinuations.add(entity.id);
  try {
    const detail = await ctx.apiRequest(ctx.entityDetailPath("tasks", entity.id));
    if (detail.id !== entity.id || detail.kind !== "tasks") throw new ctx.RequestFailure("reconciliation");
    await ctx.previewMutation(continuationOperation(detail, actionDate), async () => {}, trigger);
  } catch (error) { ctx.showRequestError(error); }
  finally { pendingContinuations.delete(entity.id); }
}
function openContinuation(entity, trigger, today, ctx) {
  if (ctx.mutationIsGated() || pendingContinuations.has(entity.id)) return;
  const dialog = document.createElement("dialog"), title = document.createElement("h2"), date = document.createElement("input"), dateSection = document.createElement("section");
  dialog.className = "task-operations-dialog"; dialog.setAttribute("aria-label", "続きのTaskを作る"); title.textContent = "続きのTaskを作る"; dialog.append(title);
  const note = document.createElement("p"); note.textContent = "元のTaskと実績を残して、続きのTaskを作ります。"; dialog.append(note);
  let submitted = false;
  const submit = value => { if (submitted || ctx.mutationIsGated()) return; submitted = true; dialog.close(); return createContinuation(entity, value, trigger, ctx); };
  const button = (label, run) => { const element = document.createElement("button"); element.type = "button"; element.textContent = label; element.addEventListener("click", run); return element; };
  date.type = "date"; date.setAttribute("aria-label", "コピー先の日付"); date.value = today; dateSection.hidden = true; dateSection.append(date, button("この日付で作る", () => { if (date.value && date.reportValidity()) return submit(date.value); }));
  dialog.append(button("今日やる", () => submit(today)), button("日付を選ぶ", () => { dateSection.hidden = false; date.focus(); }), button("予定日なし", () => submit("")), dateSection, button("キャンセル", () => dialog.close()));
  dialog.addEventListener("close", () => { dialog.remove(); if (trigger && trigger.isConnected) trigger.focus(); }); document.body.append(dialog); dialog.showModal();
}
function focusOperations(entity, trigger, today, allTasks, card, ctx) {
  card.className += " task-operation-card";
  const fm = ctx.safeFrontmatter(entity), blocked = ctx.taskStartBlockReason(entity, allTasks);
  const model = global.TaskCard.operations("task", {...fm, startBlocked: blocked}, {
    start: () => ctx.prepareTaskWorkflow("start", entity, trigger, "tasks"),
    interrupt: () => ctx.prepareTaskWorkflow("interrupt", entity, trigger, "tasks"),
    complete: () => fm.status === "doing" ? ctx.prepareTaskWorkflow("complete", entity, trigger, "tasks") : completeUnstarted(entity, trigger, ctx),
    continue: () => openContinuation(entity, trigger, today, ctx),
    today: () => ctx.moveFocusTask(entity, today, trigger),
    dated: () => ctx.openFocusMoveMenu(entity, trigger, today, true),
    undated: () => ctx.moveFocusTask(entity, "", trigger),
    due: () => ctx.openFocusDueDialog(entity, trigger),
    details: () => (card.querySelector(".task-card-title") || card.querySelector(".entity-title"))?.click(),
  });
  if (fm.status === "done") {
    if (ctx.workSessionEligible(entity)) {
      const edit = document.createElement("button"); edit.type = "button"; edit.textContent = "実績を編集"; edit.setAttribute("data-work-session-task-id", entity.id); edit.addEventListener("click", () => ctx.openWorkSessionEditor(entity, edit)); (card.querySelector(".card-actions") || card).append(edit);
    }
    const handle = operationHandle(model, trigger, ctx.mutationIsGated); card.append(handle); ctx.bind(handle, model, entity);
  }
  return model;
}
function operationHandle(model, trigger, isGated) {
  const handle = document.createElement("button"); handle.type = "button"; handle.className = "task-card-handle touch-target-48"; handle.setAttribute("aria-label", "Taskをドラッグして操作"); appendDragGrip(handle);
  handle.addEventListener("click", (event) => { if (handle.__taskCardSuppressActivation) { handle.__taskCardSuppressActivation = false; event.preventDefault(); return; } openOperations(model, trigger, isGated); });
  return handle;
}
function bindFocusOperations(handle, model, entity, ctx) {
  global.TaskCard.attachPointerDrag(handle, {threshold: 8, isGated: ctx.mutationIsGated, resolveTarget: ctx.resolveFocusDropSection,
    onActivate: (event) => { ctx.clearFocusDragDock(); ctx.setDock(global.TaskCard.operationDock(model, event)); ctx.setArchiveTarget(!["next","scheduled"].includes(ctx.safeFrontmatter(entity).status)); },
    onTrack: ctx.emphasizeFocusDropTarget, onCancel: () => { ctx.clearFocusDragDock(); ctx.clearFocusDropTargets(); ctx.setArchiveTarget(true); },
    onDrop: (target) => { ctx.clearFocusDragDock(); ctx.clearFocusDropTargets(); ctx.setArchiveTarget(true); if (target === "archive" && ["next","scheduled"].includes(ctx.safeFrontmatter(entity).status)) { void ctx.archiveFocusTask(entity, handle); return; } const action = global.TaskCard.dropOperations(model).find(value => value.key === target); if (action && !action.disabled) action.run(); }
  });
}

  global.TaskCard = {classifyFocusTask, sortFocusTasks, focusSections, isOverdueActionDate, nextIsoDate, focusMoveFields, applyFocusMove, attachPointerDrag, optimisticRelocate, appendDragGrip, createTaskCard, continuationOperation, createContinuation, operationHandle, focusOperations, bindFocusOperations, operations, dropOperations, operationDock, openOperations};
})(window);
