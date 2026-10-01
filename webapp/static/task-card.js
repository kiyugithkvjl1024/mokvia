"use strict";

/* Shared, behavior-free compact Task card primitives. */
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
  global.TaskCard = {classifyFocusTask, sortFocusTasks, focusSections, isOverdueActionDate, nextIsoDate, focusMoveFields, applyFocusMove, attachPointerDrag, optimisticRelocate, appendDragGrip, createTaskCard};
})(window);
