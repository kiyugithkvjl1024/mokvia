"use strict";

(function attachFocusCompleted(root) {
  const ISO_TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;

  function validTimestamp(value, secondsRequired) {
    if (typeof value !== "string") return null;
    const match = ISO_TIMESTAMP.exec(value);
    if (!match) return null;
    const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const monthDays = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (year < 1 || month < 1 || month > 12 || day < 1 || day > monthDays[month - 1] || hour > 23 || minute > 59 || (match[6] !== undefined && second > 59) || (secondsRequired && (match[6] === undefined || value.includes(".")))) return null;
    const timestamp = Date.parse(value);
    return Number.isNaN(timestamp) ? null : new Date(timestamp);
  }

  function completionDate(entity) {
    const fields = entity && entity.frontmatter && typeof entity.frontmatter === "object" ? entity.frontmatter : {};
    if (Object.prototype.hasOwnProperty.call(fields, "work_ended_at")) return fields.work_ended_at === "" ? validTimestamp(fields.completed_at, false) : validTimestamp(fields.work_ended_at, true);
    return validTimestamp(fields.completed_at, false);
  }

  function isToday(entity, today) {
    const timestamp = completionDate(entity);
    if (!timestamp || !/^\d{4}-\d{2}-\d{2}$/.test(today || "")) return false;
    const parts = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(timestamp);
    const value = Object.fromEntries(parts.filter((part) => ["year", "month", "day"].includes(part.type)).map((part) => [part.type, part.value]));
    return String(value.year).padStart(4, "0") + "-" + value.month + "-" + value.day === today;
  }

  function normalizeMode(mode) { return mode === "all" ? "all" : "today"; }

  function select(entities, today, mode) {
    const completed = (Array.isArray(entities) ? entities : []).filter((entity) => entity && entity.kind === "tasks" && entity.archived !== true && entity.frontmatter && entity.frontmatter.status === "done");
    return normalizeMode(mode) === "all" ? completed : completed.filter((entity) => isToday(entity, today));
  }

  function workSessionTimestamp(value) {
    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return value + ":00+09:00";
    return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(value) ? value + "+09:00" : null;
  }

  function tokyoDateTimeLocal(value, parsedTimestamp) {
    const date = parsedTimestamp(value); if (!date) return "";
    const values = {};
    for (const part of new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"}).formatToParts(date)) if (["year", "month", "day", "hour", "minute", "second"].includes(part.type)) values[part.type] = part.value;
    return String(values.year).padStart(4, "0") + "-" + values.month + "-" + values.day + "T" + values.hour + ":" + values.minute + ":" + values.second;
  }

  function tokyoNowDateTimeLocal(now) {
    const value = now instanceof Date ? now.toISOString() : new Date().toISOString();
    return tokyoDateTimeLocal(value, (candidate) => validTimestamp(candidate, false));
  }

  function taskSwitchWorkEnd(value, active, now) {
    const workEndedAt = workSessionTimestamp(value), ended = validTimestamp(workEndedAt, true), fields = active && active.frontmatter && typeof active.frontmatter === "object" ? active.frontmatter : {}, started = validTimestamp(fields.work_started_at, true), current = typeof now === "number" ? now : Date.now();
    let error = !ended ? "終了日時を秒まで入力してください。" : ended.getTime() > current ? "未来の時刻は指定できません。" : !started ? "開始日時の記録がないため、現在時刻で切り替えます。" : ended.getTime() < started.getTime() ? "終了日時は実行中Taskの開始日時以後にしてください。" : "";
    const calendarIdentity = ["calendar_id", "calendar_event_id", "calendar_event_url", "calendar_event_kind"].every((key) => typeof fields[key] === "string" && fields[key]);
    if (!error && calendarIdentity && ended.getTime() === started.getTime()) error = "Calendar連携Taskの終了日時は開始日時より後にしてください。";
    return {work_ended_at: error ? null : workEndedAt, error};
  }

  function taskSwitchTiming(mode, active, now) {
    const fields = active && active.frontmatter && typeof active.frontmatter === "object" ? active.frontmatter : {}, enabled = mode !== "start_break" && !!validTimestamp(fields.work_started_at, true);
    return {enabled, value: enabled ? tokyoNowDateTimeLocal(now) : "", note: mode === "start_break" ? "" : enabled ? "未編集なら現在時刻で切り替えます。日時を編集すると次Taskの開始日時にも使います。" : "開始日時の記録がないため、現在時刻で切り替えます。"};
  }

  function configureTaskSwitchTime(elements, mode, active, now) {
    const timing = taskSwitchTiming(mode, active, now); elements.field.hidden = !timing.enabled; elements.input.value = timing.value; elements.input.dataset.initialValue = elements.input.value; elements.note.textContent = timing.note; elements.note.hidden = !timing.note; elements.error.textContent = ""; elements.error.hidden = true;
  }

  function readTaskSwitchTime(elements, active, now) {
    if (elements.field.hidden || elements.input.value === elements.input.dataset.initialValue) { elements.error.textContent = ""; elements.error.hidden = true; return {work_ended_at: null, error: ""}; }
    const result = taskSwitchWorkEnd(elements.input.value, active, now); elements.error.textContent = result.error; elements.error.hidden = !result.error; if (result.error) elements.input.focus(); return result;
  }

  function validateWorkSession(elements, detail, parsedTimestamp, frontmatter, setHidden) {
    const start = workSessionTimestamp(elements.workSessionStart.value), end = workSessionTimestamp(elements.workSessionEnd.value), now = Date.now(), startDate = parsedTimestamp(start), endDate = parsedTimestamp(end), fields = frontmatter(detail);
    let startError = !startDate ? "開始日時を秒まで入力してください。" : startDate.getTime() > now ? "未来の時刻は保存できません。" : "", endError = !endDate ? "終了日時を秒まで入力してください。" : endDate.getTime() > now ? "未来の時刻は保存できません。" : "";
    if (!startError && !endError && endDate.getTime() < startDate.getTime()) endError = "終了日時は開始日時以後にしてください。";
    const calendarIdentity = ["calendar_id", "calendar_event_id", "calendar_event_url", "calendar_event_kind"].every((key) => typeof fields[key] === "string" && fields[key]);
    if (!startError && !endError && calendarIdentity && endDate.getTime() === startDate.getTime()) endError = "Calendar連携Taskの終了日時は開始日時より後にしてください。";
    for (const [element, message] of [[elements.workSessionStartError, startError], [elements.workSessionEndError, endError]]) { element.textContent = message; setHidden(element, !message); }
    return !startError && !endError ? {work_started_at: start, work_ended_at: end} : null;
  }

  function bindTodayAddDialog(byId, gated, busy) {
    const dialog = byId("focus-today-add-dialog"), open = byId("focus-today-add-open"), cancel = byId("focus-today-add-cancel"), input = byId("focus-today-add-title"), error = byId("focus-today-add-error");
    function clearError() { if (error) { error.textContent = ""; error.hidden = true; } }
    function failed() { if (error) { error.textContent = "追加を確認できませんでした。入力は保持しています。"; error.hidden = false; } }
    function close() { dialog.close(); }
    function dismiss() { if (busy()) return; close(); open.focus(); }
    if (open) open.addEventListener("click", () => { if (gated()) return; clearError(); dialog.showModal(); input.focus(); });
    if (cancel) cancel.addEventListener("click", dismiss);
    if (dialog) dialog.addEventListener("cancel", (event) => { event.preventDefault(); dismiss(); });
    return {close, clearError, failed};
  }
  function reviewSaved(status, applied) { status.textContent = "Reviewを保存しました: " + applied.path; status.hidden = false; }
  root.FocusCompleted = Object.freeze({bindTodayAddDialog, reviewSaved, isToday, normalizeMode, select, workSessionTimestamp, tokyoDateTimeLocal, tokyoNowDateTimeLocal, taskSwitchWorkEnd, taskSwitchTiming, configureTaskSwitchTime, readTaskSwitchTime, validateWorkSession});
})(window);
