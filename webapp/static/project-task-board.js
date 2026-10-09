"use strict";

/* Dependency-free Project Task detail, planning, and bounded reorder helpers. */
(function attachProjectTaskBoard(global) {
  let clientKey = 0;
  const periodDateValid = (value) => { if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return false; const date = new Date(value + "T00:00:00Z"); return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value; };
  const projectPeriod = Object.freeze({
    error(start, end) { if (!start && !end) return ""; if (!start || !end) return "目安期間の開始日と終了日は両方入力してください。"; if (!periodDateValid(start) || !periodDateValid(end)) return "目安期間の日付が正しくありません。"; return end < start ? "目安期間の終了日は開始日以後にしてください。" : ""; },
    label(frontmatter) { const start = frontmatter && frontmatter.planned_start_date || "", end = frontmatter && frontmatter.planned_end_date || ""; return start && end ? start + " 〜 " + end : "時期未設定"; },
    validate(startInput, endInput) { if (!startInput || !endInput) return ""; const message = this.error(startInput.value, endInput.value); startInput.setCustomValidity(message); endInput.setCustomValidity(message); return message; },
  });
  const fieldsOf = (entity) => entity && entity.frontmatter && typeof entity.frontmatter === "object" ? entity.frontmatter : {};
  const projectEstimatedPeriod = (project) => { const fields = fieldsOf(project), start = fields.planned_start_date, end = fields.planned_end_date; return periodDateValid(start) && periodDateValid(end) && end >= start ? [start, end] : null; };
  const sortRoadmapProjectsByEstimatedPeriod = (projects) => [...projects].sort((left, right) => { const leftPeriod = projectEstimatedPeriod(left), rightPeriod = projectEstimatedPeriod(right); if (leftPeriod && rightPeriod) return leftPeriod[0].localeCompare(rightPeriod[0]) || leftPeriod[1].localeCompare(rightPeriod[1]); if (leftPeriod) return -1; if (rightPeriod) return 1; return 0; });
  const idList = (value) => {
    const values = Array.isArray(value) ? value : typeof value === "string" && value.startsWith("[") && value.endsWith("]") ? value.slice(1, -1).split(",") : [];
    return values.map((item) => String(item).trim().replace(/^['"]|['"]$/g, "")).filter(Boolean);
  };
  const primaryParent = (task, fields = fieldsOf) => idList(fields(task).depends_on)[0] || "";
  const fitZoom = (viewportWidth, viewportHeight, contentWidth, contentHeight) => {
    const widthRatio = Number(viewportWidth) / Math.max(1, Number(contentWidth));
    const heightRatio = Number(viewportHeight) / Math.max(1, Number(contentHeight));
    return Math.max(50, Math.min(150, Math.floor(Math.min(widthRatio, heightRatio) * 100)));
  };
  const centerScrollLeft = (viewportWidth, scrollWidth, cardLeft, cardWidth) => Math.max(0, Math.min(Math.max(0, Number(scrollWidth) - Number(viewportWidth)), Number(cardLeft) + Number(cardWidth) / 2 - Number(viewportWidth) / 2));
  const positivePosition = (value) => { const position = Number(value); return Number.isInteger(position) && position > 0 ? position : Number.MAX_SAFE_INTEGER; };
  const orderedTasks = (tasks, fields) => [...tasks].sort((left, right) => positivePosition(fields(left).project_position) - positivePosition(fields(right).project_position) || String(left.id).localeCompare(String(right.id)));

  function treePayload(projectId, items) {
    return {mode: "tree", project_id: projectId, tasks: items.map((item) => {
      const task = {key: String(item.key || "").trim(), title: String(item.title || "").trim(), parent_key: item.parent_key ? String(item.parent_key).trim() : null};
      if (!task.parent_key && item.status === "planned") task.status = "planned";
      return task;
    })};
  }

  function buildForest(tasks, frontmatter = fieldsOf) {
    const byId = new Map(tasks.map((task) => [task.id, task])), children = new Map(), roots = [];
    for (const task of tasks) { const parentId = primaryParent(task, frontmatter); if (parentId && byId.has(parentId)) { const siblings = children.get(parentId) || []; siblings.push(task); children.set(parentId, siblings); } else roots.push(task); }
    roots.splice(0, roots.length, ...orderedTasks(roots, frontmatter));
    for (const [parent, siblings] of children) children.set(parent, orderedTasks(siblings, frontmatter));
    return {roots, children};
  }

  function supportTasks(tasks, frontmatter = fieldsOf) {
    const referenced = new Set();
    for (const task of tasks) { const id = primaryParent(task, frontmatter); if (id) referenced.add(id); }
    return tasks.filter((task) => {
      const fields = frontmatter(task), dependencies = idList(fields.depends_on);
      return Boolean(fields.project_position) || dependencies.length > 0 || referenced.has(task.id);
    });
  }

  function collapseContinuationChains(tasks, frontmatter = fieldsOf) {
    const byId = new Map(tasks.map((task) => [task.id, task])), previous = new Map(), next = new Map(), invalid = new Set();
    for (const task of tasks) { const parent = String(frontmatter(task).continuation_of || "").trim(); if (!parent) continue; previous.set(task.id, parent); if (!byId.has(parent)) { invalid.add(task.id); continue; } const successors = next.get(parent) || []; successors.push(task.id); next.set(parent, successors); }
    const neighbors = (id) => [previous.get(id), ...(next.get(id) || [])].filter(Boolean);
    const invalidateComponent = (seed) => { const pending = [seed]; while (pending.length) { const id = pending.pop(); if (invalid.has(id)) continue; invalid.add(id); pending.push(...neighbors(id)); } };
    for (const [id, successors] of next) if (successors.length > 1) invalidateComponent(id);
    for (const task of tasks) { const seen = new Set(), path = []; let id = task.id; while (previous.has(id)) { if (seen.has(id)) { for (const value of path) invalidateComponent(value); break; } seen.add(id); path.push(id); id = previous.get(id); } }
    for (const id of [...invalid]) invalidateComponent(id);
    const groups = [], hiddenIds = new Set();
    for (const task of tasks) {
      if (!previous.has(task.id) || next.has(task.id) || invalid.has(task.id)) continue;
      const chain = [task.id]; let id = task.id;
      while (previous.has(id)) { id = previous.get(id); if (invalid.has(id)) { chain.length = 0; break; } chain.unshift(id); }
      if (chain.length < 2) continue;
      const historyIds = chain.slice(0, -1); groups.push({latestId: task.id, historyIds}); historyIds.forEach((value) => hiddenIds.add(value));
    }
    return {groups, hiddenIds};
  }

  function dependencyEdges(tasks, frontmatter = fieldsOf) {
    const ids = new Set(tasks.map((task) => task.id)), edges = [], seen = new Set();
    for (const task of tasks) for (const source of idList(frontmatter(task).depends_on).slice(1)) if (source !== task.id && ids.has(source)) { const key = source + "\u0000" + task.id; if (!seen.has(key)) { seen.add(key); edges.push({from: source, to: task.id}); } }
    return edges;
  }

  function planEditorItems(tasks, frontmatter = fieldsOf) {
    const continuations = collapseContinuationChains(tasks, frontmatter), continuationCounts = new Map(continuations.groups.map((group) => [group.latestId, group.historyIds.length])), editable = tasks.filter((task) => !continuations.hiddenIds.has(task.id) && ["planned", "next", "doing"].includes(frontmatter(task).status)), editableIds = new Set(editable.map((task) => task.id));
    const forest = buildForest(editable, frontmatter), result = [];
    const append = (task) => { const fields = frontmatter(task), parent = primaryParent(task, frontmatter), continuationCount = continuationCounts.get(task.id) || 0; result.push({key: task.id, id: task.id, base_hash: task.content_hash, title: fields.title || "", parent_key: editableIds.has(parent) ? parent : null, status: fields.status, locked: fields.status === "doing", ...(continuationCount ? {continuation_count: continuationCount} : {})}); for (const child of forest.children.get(task.id) || []) append(child); };
    for (const root of forest.roots) append(root);
    return result;
  }

  function indentPx(depth) { return Math.min(64, Math.max(0, Number(depth) || 0) * 16); }
  function moveOperation(task, projectId, status, parentId, position) { return {action: "project_task_move", kind: "tasks", id: task.id, base_hash: task.content_hash, project_id: projectId, status, primary_parent_id: parentId || null, position}; }

  function createPlanEditor(initial = []) {
    const archived = new Map();
    const rowFor = (item) => ({key: String(item.key || `client-${++clientKey}`), ...(item.id ? {id: String(item.id)} : {}), ...(item.base_hash ? {base_hash: String(item.base_hash)} : {}), ...(Number(item.continuation_count) > 0 ? {continuation_count: Number(item.continuation_count)} : {}), title: String(item.title || ""), parent_key: item.parent_key || null, status: item.parent_key ? (item.status === "doing" || item.status === "done" ? item.status : "planned") : ["next", "doing", "done"].includes(item.status) ? item.status : "planned", locked: item.locked === true || ["doing", "done"].includes(item.status)});
    const rows = (initial.length ? initial : [{}]).map(rowFor); let baseline = "";
    const byKey = () => new Map(rows.map((row) => [row.key, row]));
    const siblingRows = (row) => rows.filter((candidate) => candidate.parent_key === row.parent_key);
    const ordered = () => {
      const children = new Map();
      for (const row of rows) { const siblings = children.get(row.parent_key) || []; siblings.push(row); children.set(row.parent_key, siblings); }
      const result = [], seen = new Set();
      const append = (row) => { if (seen.has(row.key)) return; seen.add(row.key); result.push(row); for (const child of children.get(row.key) || []) append(child); };
      for (const root of children.get(null) || []) append(root);
      for (const row of rows) append(row);
      return result;
    };
    const state = () => JSON.stringify({rows: ordered().map((row) => ({key: row.key, id: row.id || "", base_hash: row.base_hash || "", title: row.title, parent_key: row.parent_key || null, status: row.status, locked: row.locked === true})), archives: [...archived.keys()]});
    baseline = state();
    return {
      get rows() { return ordered(); },
      updateTitle(key, title) { const row = byKey().get(key); if (row) row.title = String(title || ""); },
      setStatus(key, status) { const row = byKey().get(key); if (!row || row.parent_key || !["planned", "next"].includes(status)) return false; row.status = status; return true; },
      add(key) { const anchor = byKey().get(key), row = {key: `client-${++clientKey}`, title: "", parent_key: anchor ? anchor.parent_key : null, status: "planned"}; const index = anchor ? rows.indexOf(anchor) + 1 : rows.length; rows.splice(index, 0, row); return row; },
      addSibling(key) { return this.add(key); },
      addChild(key) { const anchor = byKey().get(key); if (!anchor || anchor.locked) return null; const row = {key: `client-${++clientKey}`, title: "", parent_key: anchor.key, status: "planned"}; rows.splice(rows.indexOf(anchor) + 1, 0, row); return row; },
      addRoot() { const row = {key: `client-${++clientKey}`, title: "", parent_key: null, status: "planned"}; rows.push(row); return row; },
      remove(key) { if (rows.length === 1) { rows[0].title = ""; return false; } const row = byKey().get(key), children = rows.filter((child) => child.parent_key === key); if (!row || row.locked || children.some((child) => child.locked)) return false; for (const child of children) child.parent_key = row.parent_key; const index = rows.indexOf(row); rows.splice(index, 1); if (row.id) archived.set(row.id, {row, index, children}); return true; },
      undoArchive(id) { const pending = archived.get(id); if (!pending) return false; archived.delete(id); rows.splice(Math.min(pending.index, rows.length), 0, pending.row); for (const child of pending.children) if (child.parent_key === pending.row.parent_key) child.parent_key = pending.row.key; return true; },
      move(key, direction) { const row = byKey().get(key); if (!row || row.locked) return false; const siblings = siblingRows(row), index = siblings.indexOf(row), target = siblings[index + (direction < 0 ? -1 : 1)]; if (!target || target.locked) return false; const left = rows.indexOf(row), right = rows.indexOf(target); rows[left] = target; rows[right] = row; return true; },
      indent(key) { const row = byKey().get(key); if (!row) return false; const siblings = siblingRows(row), previous = siblings[siblings.indexOf(row) - 1]; if (!previous) return false; row.parent_key = previous.key; row.status = "planned"; return true; },
      outdent(key) { const row = byKey().get(key), parent = row && byKey().get(row.parent_key); if (!row || !parent) return false; row.parent_key = parent.parent_key || null; row.status = "planned"; return true; },
      payload(projectId) { return treePayload(projectId, ordered()); },
      updatePayload(projectId, baseHash) {
        return {action: "project_task_plan_update", kind: "projects", id: projectId, base_hash: baseHash, nodes: ordered().filter((row) => row.status !== "done").map((row) => ({key: row.key, ...(row.id ? {id: row.id, base_hash: row.base_hash} : {}), title: row.title.trim(), parent_key: row.parent_key || null, status: row.status})), archives: [...archived.values()].map(({row}) => ({id: row.id, base_hash: row.base_hash}))};
      },
      archivedRows() { return [...archived.values()].map(({row}) => row); },
      isDirty() { return state() !== baseline; },
      markClean() { baseline = state(); },
      load(items) { archived.clear(); rows.splice(0, rows.length, ...(items.length ? items : [{}]).map(rowFor)); baseline = state(); },
      reset() { archived.clear(); rows.splice(0, rows.length, {key: `client-${++clientKey}`, title: "", parent_key: null, status: "planned"}); baseline = state(); },
    };
  }

  function button(label, callback) { const value = document.createElement("button"); value.type = "button"; value.textContent = label; value.setAttribute("aria-label", label); value.addEventListener("click", callback); return value; }
  function mountPlanEditor(container) {
    const model = createPlanEditor(); let focusKey = "", focusStatus = "", focusArchiveId = "", selectedKey = "";
    const render = () => {
      container.replaceChildren(); const rows = model.rows; if (!rows.some((row) => row.key === selectedKey)) selectedKey = rows[0] && rows[0].key;
      const children = new Map(); for (const row of rows) { const parent = rows.some((candidate) => candidate.key === row.parent_key) ? row.parent_key : null, siblings = children.get(parent) || []; siblings.push(row); children.set(parent, siblings); }
      const tree = document.createElement("ul"); tree.className = "project-plan-editor-tree";
      const appendNode = (row, host) => {
        const node = document.createElement("li"), card = document.createElement("div"); node.className = "project-plan-editor-node"; card.className = "project-plan-row" + (selectedKey === row.key ? " is-selected" : "") + (row.locked ? " is-locked" : ""); card.dataset.planKey = row.key; card.style.marginInlineStart = "0px"; card.style.maxWidth = "100%";
        const label = document.createElement("label"), labelCopy = document.createElement("span"), input = document.createElement("input"); labelCopy.className = "visually-hidden"; labelCopy.textContent = "Taskタイトル"; input.value = row.title; input.maxLength = 300; input.setAttribute("required", ""); input.setAttribute("aria-label", "Taskタイトル"); input.disabled = row.locked; input.addEventListener("focus", () => { if (selectedKey !== row.key) { selectedKey = row.key; render(); const replacement = container.querySelector(`[data-plan-key="${row.key}"] input`); if (replacement) replacement.focus(); } }); input.addEventListener("input", () => model.updateTitle(row.key, input.value)); label.append(labelCopy, input);
        if (row.continuation_count) { const history = document.createElement("span"); history.className = "project-plan-continuation-count"; history.textContent = "中断 " + row.continuation_count + "回（履歴）"; card.append(history); }
        const status = document.createElement("div"); status.className = "project-plan-status"; status.setAttribute("role", "group"); status.setAttribute("aria-label", "Taskの状態");
        if (!row.parent_key) for (const value of [["planned", "計画"], ["next", "次にやる"]]) { const choice = button(value[1], () => { model.setStatus(row.key, value[0]); focusKey = row.key; focusStatus = value[0]; render(); }); choice.setAttribute("aria-pressed", String(row.status === value[0])); status.append(choice); }
        const controls = document.createElement("div"); controls.className = "project-plan-row-controls";
        const add = button("兄弟を追加", () => { const added = model.addSibling(row.key); selectedKey = focusKey = added.key; render(); });
        const child = button("子を追加", () => { const added = model.addChild(row.key); if (added) { selectedKey = focusKey = added.key; render(); } }); child.disabled = row.locked;
        const remove = button("アーカイブ", () => { if (model.remove(row.key) && row.id) focusArchiveId = row.id; render(); }); remove.className = "danger-secondary"; remove.disabled = rows.length === 1 || row.locked || (children.get(row.key) || []).some((candidate) => candidate.locked);
        const up = button("上へ", () => { model.move(row.key, -1); focusKey = row.key; render(); });
        const down = button("下へ", () => { model.move(row.key, 1); focusKey = row.key; render(); });
        const indent = button("インデント", () => { model.indent(row.key); focusKey = row.key; render(); });
        const outdent = button("アウトデント", () => { model.outdent(row.key); focusKey = row.key; render(); });
        const siblings = rows.filter((candidate) => candidate.parent_key === row.parent_key), siblingIndex = siblings.indexOf(row);
        up.disabled = siblingIndex <= 0 || siblings[siblingIndex - 1].locked; down.disabled = siblingIndex === siblings.length - 1 || siblings[siblingIndex + 1].locked; indent.disabled = siblingIndex <= 0; outdent.disabled = !row.parent_key;
        controls.append(add, child, remove, up, down, indent, outdent); card.append(label); if (row.locked) { const fixed = document.createElement("span"); fixed.className = "project-plan-locked"; fixed.textContent = row.status === "done" ? "完了・変更不可" : "進行中・変更不可"; card.append(fixed); } else if (selectedKey === row.key) { if (!row.parent_key) card.append(status); card.append(controls); } node.append(card); const childRows = children.get(row.key) || []; if (childRows.length) { node.classList.add("project-plan-branch"); const list = document.createElement("ul"); list.className = "project-plan-children" + (childRows.length > 1 ? " project-plan-siblings" : ""); for (const childRow of childRows) appendNode(childRow, list); node.append(list); } host.append(node); if (focusKey === row.key) { const target = focusStatus ? [...status.querySelectorAll("button")].find((choice) => choice.getAttribute("aria-pressed") === "true") : input; if (target) target.focus(); }
      };
      for (const row of children.get(null) || []) appendNode(row, tree); container.append(tree);
      const archivedRows = model.archivedRows(); if (archivedRows.length) { const pending = document.createElement("section"), heading = document.createElement("p"), list = document.createElement("ul"); let archiveFocus = null; pending.className = "project-plan-archive-pending"; pending.setAttribute("role", "status"); pending.setAttribute("aria-live", "polite"); heading.textContent = "保存時にアーカイブ"; for (const row of archivedRows) { const item = document.createElement("li"), title = document.createElement("span"), undo = button("元に戻す", () => { model.undoArchive(row.id); selectedKey = focusKey = row.key; render(); }); title.textContent = row.title || row.id; item.append(title, undo); list.append(item); if (focusArchiveId === row.id) archiveFocus = undo; } pending.append(heading, list); container.append(pending); if (archiveFocus) archiveFocus.focus(); }
      focusKey = ""; focusStatus = ""; focusArchiveId = "";
    };
    render();
    return {get rows() { return model.rows; }, payload: (projectId) => model.payload(projectId), updatePayload: (projectId, baseHash) => model.updatePayload(projectId, baseHash), isDirty: () => model.isDirty(), markClean: () => model.markClean(), load(items) { model.load(items); render(); }, reset() { model.reset(); render(); }, addRoot() { const row = model.addRoot(); selectedKey = focusKey = row.key; render(); }, focusFirst() { const input = container.querySelector("input"); if (input) input.focus(); }};
  }

  function revealArchive(card, archive) { card.classList.add("archive-revealed"); archive.hidden = false; archive.setAttribute("aria-hidden", "false"); archive.setAttribute("tabindex", "0"); }
  function blocksCardDrag(event, card) { const control = event.target && event.target !== card && event.target.closest && event.target.closest("a,button,input,select,textarea"); return control && !control.classList.contains("project-task-drag-handle"); }
  function attachGesture(card, options) {
    let start = null;
    card.addEventListener("pointerdown", (event) => { if ((options.isGated && options.isGated()) || blocksCardDrag(event, card)) return; start = {id: event.pointerId, x: event.clientX, y: event.clientY}; if (card.setPointerCapture) card.setPointerCapture(event.pointerId); });
    card.addEventListener("pointermove", (event) => { if (!start || start.id !== event.pointerId) return; const x = event.clientX - start.x, y = event.clientY - start.y; if (x < -24 && Math.abs(x) > Math.abs(y)) event.preventDefault(); });
    const finish = (event) => { if (!start || start.id !== event.pointerId) return; const x = event.clientX - start.x, y = event.clientY - start.y; if (x < -48 && Math.abs(y) < 24) options.onArchiveReveal(); if (card.releasePointerCapture) card.releasePointerCapture(event.pointerId); start = null; };
    card.addEventListener("pointerup", finish); card.addEventListener("pointercancel", () => { start = null; });
  }

  function attachReorder(card, options) {
    card.setAttribute("tabindex", "0"); card.setAttribute("aria-label", "Taskを並べ替え。Alt+上下キーでも移動できます");
    if (options.archiveGesture !== false) attachGesture(card, {isGated: options.isGated, onArchiveReveal: options.onArchiveReveal || (() => {})});
    card.addEventListener("keydown", (event) => {
      if (event.key === "ArrowLeft" && !event.altKey) { event.preventDefault(); options.onArchiveReveal(); return; }
      if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key) || (options.isGated && options.isGated())) return;
      event.preventDefault(); options.onMove(event.key === "ArrowUp" ? -1 : 1);
    });
    let drag = null;
    const cleanup = () => { if (!drag) return; const motion = global.ProjectKanbanMotion; if (motion) motion.stopAutoScroll(drag); if (drag.marker) drag.marker.remove(); card.classList.remove("is-dragging"); drag = null; };
    card.addEventListener("pointerdown", (event) => {
      if ((options.isGated && options.isGated()) || blocksCardDrag(event, card)) return;
      drag = {id: event.pointerId, startX: event.clientX, startY: event.clientY, lastY: event.clientY, active: false, marker: null, autoFrame: 0}; if (card.setPointerCapture) card.setPointerCapture(event.pointerId);
    });
    card.addEventListener("pointermove", (event) => {
      if (!drag || drag.id !== event.pointerId) return; const x = event.clientX - drag.startX, y = event.clientY - drag.startY;
      if (!drag.active) { if (Math.abs(y) < 8 || Math.abs(y) <= Math.abs(x)) return; drag.active = true; drag.before = global.ProjectKanbanMotion ? global.ProjectKanbanMotion.capture(options.cards()) : new Map(); drag.originalIndex = options.cards().indexOf(card); drag.marker = document.createElement("li"); drag.marker.className = "project-task-drop-marker"; card.classList.add("is-dragging"); if (global.ProjectKanbanMotion) { const tick = () => { if (drag && drag.active) global.ProjectKanbanMotion.autoScroll(drag, tick); }; global.ProjectKanbanMotion.beginAutoScroll(drag, tick); } }
      event.preventDefault(); drag.lastY = event.clientY; const siblings = options.cards().filter((value) => value !== card), index = global.ProjectKanbanMotion ? global.ProjectKanbanMotion.insertionIndex(siblings.map((value) => value.getBoundingClientRect()), event.clientY) : siblings.length; options.list.insertBefore(drag.marker, siblings[index] || null);
    });
    card.addEventListener("pointerup", (event) => {
      if (!drag || drag.id !== event.pointerId) return; const state = drag; if (card.releasePointerCapture) card.releasePointerCapture(event.pointerId);
      if (!state.active) { cleanup(); return; }
      const listChildren = [...options.list.children], position = options.cards().filter((value) => value !== card && value.parentNode === options.list && listChildren.indexOf(value) < listChildren.indexOf(state.marker)).length + 1;
      const restore = () => { const motion = global.ProjectKanbanMotion, before = motion ? motion.capture(options.cards()) : new Map(), remaining = options.cards().filter((value) => value !== card && value.parentNode === options.list); options.list.insertBefore(card, remaining[state.originalIndex] || null); if (motion) motion.flip(before); };
      if (options.isGated && options.isGated()) { cleanup(); restore(); return; }
      options.list.insertBefore(card, state.marker); cleanup(); if (global.ProjectKanbanMotion) global.ProjectKanbanMotion.flip(state.before); options.onMoveTo(position, restore);
    });
    card.addEventListener("pointercancel", cleanup);
  }

  async function archiveTask(ctx, task, focus) { try { const fresh = await ctx.apiRequest(ctx.entityDetailPath("tasks", task.id)); await ctx.previewMutation({action: "archive", kind: "tasks", id: fresh.id, base_hash: fresh.content_hash}, () => {}, focus); } catch (error) { ctx.showRequestError(error); } }
  function moveTask(ctx, task, status, siblings, position, restore) {
    if (ctx.mutationIsGated()) { if (restore) restore(); return; }
    const token = ctx.beginTaskPreparation ? ctx.beginTaskPreparation() : null; if (ctx.beginTaskPreparation && token === null) { if (restore) restore(); return; }
    void (async () => { try { const fresh = await ctx.apiRequest(ctx.entityDetailPath("tasks", task.id)); if (ctx.mutationIsGated() && token === null) { if (restore) restore(); return; } await ctx.previewMutation(moveOperation(fresh, ctx.detail.id, status, primaryParent(fresh, ctx.safeFrontmatter), position), () => {}, ctx.elements.projectDetailHeading, token, {onFailure: restore}); } catch (error) { if (restore) restore(); ctx.showRequestError(error); } finally { if (token !== null) ctx.finishTaskPreparation(token); } })();
  }

  function changeTaskStatus(ctx, task, select, previousStatus) {
    if (ctx.mutationIsGated()) { select.value = previousStatus; select.focus(); return; }
    const token = ctx.beginTaskPreparation ? ctx.beginTaskPreparation() : null; if (ctx.beginTaskPreparation && token === null) { select.value = previousStatus; select.focus(); return; }
    const restore = () => { select.value = previousStatus; select.focus(); };
    void (async () => { try { const fresh = await ctx.apiRequest(ctx.entityDetailPath("tasks", task.id)); const operation = {action: "update", kind: "tasks", id: fresh.id, base_hash: fresh.content_hash, fields: {status: select.value}}; await ctx.previewMutation(operation, () => {}, select, token, {onFailure: restore, onCancel: restore}); } catch (error) { restore(); ctx.showRequestError(error); } finally { if (token !== null) ctx.finishTaskPreparation(token); } })();
  }

  function taskStatusSelect(ctx, task, className) {
    const fields = ctx.safeFrontmatter(task), label = document.createElement("label"), select = document.createElement("select"); label.className = className; label.textContent = "状態"; select.setAttribute("aria-label", "Taskの状態");
    for (const status of ["planned", "next"]) { const option = document.createElement("option"); option.value = status; option.textContent = status === "next" ? "Next" : "planned"; if (status === fields.status) option.selected = true; select.append(option); }
    select.addEventListener("change", () => changeTaskStatus(ctx, task, select, fields.status)); label.append(select); return {label, select};
  }

  function taskCard(ctx, task, options = {}) {
    const fields = ctx.safeFrontmatter(task), shared = global.TaskCard;
    const metadata = [fields.action_date ? "対応予定日 " + fields.action_date : "", fields.due ? "期限 " + fields.due : ""].filter(Boolean);
    const card = shared.createTaskCard({task, href: ctx.clarifyHref(task.id), metadata, handleLabel: "Taskをドラッグして並べ替え", actions: options.actions || []});
    card.className += " entity-row project-task-card";
    const title = card.children.find ? card.children.find((child) => child.tagName === "A") : card.querySelector("a");
    if (options.onOpen) title.addEventListener("click", (event) => options.onOpen(event, task.id));
    const handle = card.children.find ? card.children.find((child) => child.className.includes("task-card-handle")) : card.querySelector(".task-card-handle");
    handle.className += " project-task-drag-handle";
    handle.setAttribute("aria-label", "Taskをドラッグして並べ替え。EnterまたはSpaceでカードへ移動後、Alt+上下キーで移動できます");
    handle.setAttribute("title", "Enterでカードに移動後、Alt+上下キーで並べ替え");
    handle.addEventListener("keydown", (event) => {
      if (!["Enter", " "].includes(event.key)) return;
      event.preventDefault(); card.focus();
    });
    if (options.reorder === false) handle.remove();
    if (options.statusSelect) card.append(taskStatusSelect(ctx, task, "project-standalone-status").label);
    const archive = document.createElement("button"); archive.type = "button"; archive.className = "project-task-archive"; archive.textContent = "アーカイブ"; archive.hidden = true; archive.setAttribute("aria-hidden", "true"); archive.setAttribute("tabindex", "-1"); archive.addEventListener("click", () => archiveTask(ctx, task, title)); card.append(archive); return {card, archive};
  }

  function statusGroup(ctx, status, tasks) {
    const details = document.createElement("details"), summary = document.createElement("summary"), body = document.createElement("div"); details.className = "project-task-group"; details.dataset.status = status; details.id = "project-task-group-" + status; details.open = status !== "done"; summary.textContent = ctx.taskStatusLabel(status) + " " + tasks.length + "件"; body.className = "project-task-group-body";
    const parentIds = [...new Set(tasks.map((task) => primaryParent(task, ctx.safeFrontmatter)))];
    for (const parentId of parentIds) {
      const siblings = orderedTasks(tasks.filter((task) => primaryParent(task, ctx.safeFrontmatter) === parentId), ctx.safeFrontmatter), list = document.createElement("ul"); list.className = "entity-list project-task-sibling-list"; const cards = [];
      for (const task of siblings) { const value = taskCard(ctx, task, {statusSelect: ["planned", "next"].includes(status), reorder: false, onOpen: ctx.openTaskDetail}); cards.push(value.card); list.append(value.card); attachGesture(value.card, {isGated: ctx.mutationIsGated, onArchiveReveal: () => revealArchive(value.card, value.archive)}); }
      body.append(list);
    }
    details.append(summary, body); return details;
  }

  const statusMenus = new Set(); let statusMenuOutsideReady = false;
  function registerStatusMenu(menu, summary) {
    statusMenus.add(menu);
    menu.addEventListener("keydown", (event) => { if (event.key !== "Escape") return; event.preventDefault(); menu.open = false; summary.focus(); });
    if (!statusMenuOutsideReady) { document.addEventListener("pointerdown", (event) => { for (const candidate of statusMenus) if (candidate.open && !candidate.contains(event.target)) candidate.open = false; }); statusMenuOutsideReady = true; }
  }

  function appendForestTask(ctx, forest, byId, continuationGroups, host, task, depth, visited) {
    if (visited.has(task.id)) return null; visited.add(task.id); const fields = ctx.safeFrontmatter(task), actions = [];
    if (!["doing", "done"].includes(fields.status) && ctx.prepareTaskWorkflow) { const start = ctx.actionButton("開始", (event) => { event.stopPropagation(); ctx.prepareTaskWorkflow("start", task, start, "projects"); }, true), reason = ctx.taskStartBlockReason(task, ctx.startTasks); start.addEventListener("dblclick", (event) => { event.preventDefault(); event.stopPropagation(); }); if (reason) { start.textContent = "開始できません"; start.dataset.dependencyBlocked = "true"; start.disabled = true; start.setAttribute("aria-disabled", "true"); start.title = reason; } actions.push(start); }
    const rendered = taskCard(ctx, task, {reorder: false, onOpen: ctx.openTaskDetail, actions}), item = rendered.card, archive = rendered.archive, children = forest.children.get(task.id) || []; item.className += " project-tree-node"; item.style.marginInlineStart = "0px";
    const card = document.createElement("div"); card.className = "project-tree-card"; card.append(...[...item.children]); item.append(card); let content = card;
    if (fields.status !== "done") { const cardLink = card.querySelector(".task-card-title"); if (cardLink) cardLink.className += " project-tree-card-link"; }
    if (fields.status === "done") { const details = document.createElement("details"), summary = document.createElement("summary"); details.dataset.taskId = task.id; details.open = false; summary.append(...[...card.children]); details.append(summary); card.append(details); content = details; }
    const dependencyIds = idList(fields.depends_on), dependencyNextEligible = fields.status === "waiting" && dependencyIds.length > 0 && !fields.waiting_for;
    if (["planned", "next"].includes(fields.status) || dependencyNextEligible) {
      const actions = document.createElement("div"), statusPill = document.createElement("span"), menu = document.createElement("details"), summary = document.createElement("summary"), change = button(fields.status === "next" ? "計画にする" : "Nextにする", () => {
        menu.open = false; summary.focus(); const backStatus = dependencyIds.length ? "waiting" : "planned", select = {value: fields.status === "next" ? backStatus : "next", focus() { summary.focus(); }}; changeTaskStatus(ctx, task, select, fields.status);
      });
      actions.className = "project-tree-root-actions"; statusPill.className = "project-tree-status-pill"; statusPill.textContent = fields.status === "next" ? "Next" : fields.status === "planned" ? "計画" : ctx.taskStatusLabel(fields.status); menu.className = "project-tree-status-menu"; summary.textContent = "…"; summary.setAttribute("aria-label", "状態を変更"); menu.append(summary, change); registerStatusMenu(menu, summary); actions.append(statusPill, menu); content.append(actions);
    }
    else if (fields.status !== "done" && !["planned", "next"].includes(fields.status)) { const statusPill = document.createElement("span"); statusPill.className = "project-tree-status-pill"; statusPill.textContent = ctx.taskStatusLabel(fields.status); content.append(statusPill); }
    const continuation = continuationGroups.get(task.id);
    if (continuation) { const details = document.createElement("details"), summary = document.createElement("summary"), list = document.createElement("ul"); details.className = "project-tree-continuations"; summary.textContent = "中断 " + continuation.historyIds.length + "回"; list.className = "project-tree-continuation-list"; for (const id of continuation.historyIds) { const prior = byId.get(id), item = document.createElement("li"), link = document.createElement("a"); link.setAttribute("href", ctx.clarifyHref(id)); link.dataset.taskLinkId = id; link.textContent = prior ? ctx.safeFrontmatter(prior).title || id : id; if (ctx.openTaskDetail) link.addEventListener("click", (event) => ctx.openTaskDetail(event, id)); item.append(link); list.append(item); } details.append(summary, list); content.append(details); }
    if (children.length) { item.classList.add("project-tree-branch"); const list = document.createElement("ul"); list.className = "entity-list project-tree-children" + (children.length > 1 ? " project-tree-siblings" : ""); appendForestLevel(ctx, forest, byId, continuationGroups, list, children, depth + 1, visited); item.append(list); }
    host.append(item); return {card: item, archive};
  }

  function appendForestLevel(ctx, forest, byId, continuationGroups, host, tasks, depth, visited) {
    for (const task of tasks) appendForestTask(ctx, forest, byId, continuationGroups, host, task, depth, visited);
  }

  function renderedScale(rendered, layout) { const scale = Number(rendered) / Number(layout); return Number.isFinite(scale) && scale > 0 ? scale : 1; }

  function treeCardRect(node, treeRect, scaleX = 1, scaleY = 1) {
    const card = node && node.querySelector && node.querySelector(".project-tree-card"), rect = card && card.getBoundingClientRect ? card.getBoundingClientRect() : node && node.getBoundingClientRect ? node.getBoundingClientRect() : null;
    if (!rect) return null;
    const left = (rect.left - treeRect.left) / scaleX, top = (rect.top - treeRect.top) / scaleY, width = rect.width / scaleX, height = rect.height / scaleY;
    return {left, top, right: left + width, bottom: top + height, width, height};
  }

  function expandedRect(rect, amount = 8) { return {left: rect.left - amount, top: rect.top - amount, right: rect.right + amount, bottom: rect.bottom + amount}; }

  function pathSegments(coords) {
    const compact = coords.filter((coord, index) => !index || coord.x !== coords[index - 1].x || coord.y !== coords[index - 1].y), segments = [];
    for (let index = 1; index < compact.length; index += 1) segments.push({from: compact[index - 1], to: compact[index]});
    return {coords: compact, segments};
  }

  function segmentHitsRect(segment, rect) {
    const {from, to} = segment;
    if (from.x === to.x) return from.x > rect.left && from.x < rect.right && Math.max(Math.min(from.y, to.y), rect.top) < Math.min(Math.max(from.y, to.y), rect.bottom);
    if (from.y === to.y) return from.y > rect.top && from.y < rect.bottom && Math.max(Math.min(from.x, to.x), rect.left) < Math.min(Math.max(from.x, to.x), rect.right);
    return true;
  }

  function segmentsOverlap(first, second) {
    const a = first.from, b = first.to, c = second.from, d = second.to;
    if (a.x === b.x && c.x === d.x && a.x === c.x) return Math.max(Math.min(a.y, b.y), Math.min(c.y, d.y)) < Math.min(Math.max(a.y, b.y), Math.max(c.y, d.y));
    if (a.y === b.y && c.y === d.y && a.y === c.y) return Math.max(Math.min(a.x, b.x), Math.min(c.x, d.x)) < Math.min(Math.max(a.x, b.x), Math.max(c.x, d.x));
    return false;
  }

  function segmentsCross(first, second) {
    if (segmentsOverlap(first, second)) return true;
    const a = first.from, b = first.to, c = second.from, d = second.to;
    if (a.x === b.x && c.y === d.y) return a.x > Math.min(c.x, d.x) && a.x < Math.max(c.x, d.x) && c.y > Math.min(a.y, b.y) && c.y < Math.max(a.y, b.y);
    if (a.y === b.y && c.x === d.x) return c.x > Math.min(a.x, b.x) && c.x < Math.max(a.x, b.x) && a.y > Math.min(c.y, d.y) && a.y < Math.max(c.y, d.y);
    return false;
  }

  function routePath(start, end, obstacles, occupied, bounds) {
    const ys = new Set([start.y, end.y, (start.y + end.y) / 2, bounds.top, bounds.bottom]), xs = new Set([start.x, end.x, (start.x + end.x) / 2, bounds.left, bounds.right]);
    for (const rect of obstacles) { ys.add(rect.top - 8); ys.add(rect.bottom + 8); xs.add(rect.left - 8); xs.add(rect.right + 8); }
    const candidates = [], add = (coords) => candidates.push(pathSegments(coords));
    for (const y of ys) {
      add([start, {x: start.x, y}, {x: end.x, y}, end]);
      for (const x of xs) add([start, {x, y: start.y}, {x, y}, {x, y: end.y}, end]);
    }
    for (const x of xs) add([start, {x, y: start.y}, {x, y: end.y}, end]);
    const score = (route) => {
      const blocked = route.segments.reduce((count, segment) => count + obstacles.filter((rect) => segmentHitsRect(segment, rect)).length, 0);
      const overlap = route.segments.reduce((count, segment) => count + occupied.filter((other) => segmentsOverlap(segment, other)).length, 0);
      const crossings = route.segments.reduce((count, segment) => count + occupied.filter((other) => segmentsCross(segment, other)).length, 0);
      const length = route.segments.reduce((total, segment) => total + Math.abs(segment.from.x - segment.to.x) + Math.abs(segment.from.y - segment.to.y), 0);
      return [blocked, overlap, crossings, length];
    };
    const compare = (left, right) => { const a = score(left), b = score(right); for (let index = 0; index < a.length; index += 1) if (a[index] !== b[index]) return a[index] - b[index]; return 0; }, clear = candidates.filter((route) => score(route)[0] === 0);
    if (clear.length) return clear.sort(compare)[0];
    const fallback = [pathSegments([start, {x: bounds.left, y: start.y}, {x: bounds.left, y: end.y}, end]), pathSegments([start, {x: bounds.right, y: start.y}, {x: bounds.right, y: end.y}, end])];
    return fallback.sort(compare)[0];
  }

  function svgPath(coords) {
    return coords.map((coord, index) => index ? coord.x === coords[index - 1].x ? "V" + Math.round(coord.y) : "H" + Math.round(coord.x) : "M" + Math.round(coord.x) + "," + Math.round(coord.y)).join(" ");
  }

  function drawDependencyLines(tree, tasks, frontmatter = fieldsOf) {
    const canvas = tree && tree.parentNode; if (!canvas) return;
    const previous = canvas.querySelector && canvas.querySelector(".project-tree-dependency-lines"); if (previous) previous.remove();
    const edges = dependencyEdges(tasks, frontmatter); if (!edges.length || typeof document.createElementNS !== "function" || !tree.getBoundingClientRect) return;
    const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"), marker = document.createElementNS(ns, "marker"), path = document.createElementNS(ns, "path"), defs = document.createElementNS(ns, "defs"), renderedTreeRect = tree.getBoundingClientRect(), layoutWidth = Number(tree.offsetWidth) || Number(tree.scrollWidth) || renderedTreeRect.width, layoutHeight = Number(tree.offsetHeight) || Number(tree.scrollHeight) || renderedTreeRect.height, scaleX = renderedScale(renderedTreeRect.width, layoutWidth), scaleY = renderedScale(renderedTreeRect.height, layoutHeight), treeRect = {...renderedTreeRect, width: renderedTreeRect.width / scaleX, height: renderedTreeRect.height / scaleY};
    svg.classList.add("project-tree-dependency-lines"); svg.setAttribute("aria-label", "追加前提線"); svg.setAttribute("role", "img"); svg.setAttribute("width", String(Math.max(tree.scrollWidth || 0, treeRect.width))); svg.setAttribute("height", String(Math.max(tree.scrollHeight || 0, treeRect.height))); svg.setAttribute("viewBox", `0 0 ${Math.max(tree.scrollWidth || 0, treeRect.width)} ${Math.max(tree.scrollHeight || 0, treeRect.height)}`);
    svg.style.left = (tree.offsetLeft || 0) + "px"; svg.style.top = (tree.offsetTop || 0) + "px";
    marker.id = "project-tree-dependency-arrow"; marker.setAttribute("markerWidth", "8"); marker.setAttribute("markerHeight", "8"); marker.setAttribute("refX", "7"); marker.setAttribute("refY", "4"); marker.setAttribute("orient", "auto"); path.setAttribute("d", "M0,0 L8,4 L0,8 z"); path.setAttribute("fill", "currentColor"); marker.append(path); defs.append(marker); svg.append(defs);
    const cards = new Map(); for (const task of tasks) { const node = tree.querySelector(`[data-task-id="${task.id}"]`), rect = treeCardRect(node, renderedTreeRect, scaleX, scaleY); if (rect) cards.set(task.id, rect); }
    const allRects = [...cards.values()].map((rect) => expandedRect(rect)), outer = allRects.length ? {left: Math.min(...allRects.map((rect) => rect.left)) - 16, top: Math.min(...allRects.map((rect) => rect.top)) - 16, right: Math.max(...allRects.map((rect) => rect.right)) + 16, bottom: Math.max(...allRects.map((rect) => rect.bottom)) + 16} : {left: -16, top: -16, right: treeRect.width + 16, bottom: treeRect.height + 16}, occupied = [];
    for (let index = 0; index < edges.length; index += 1) { const edge = edges[index], from = cards.get(edge.from), to = cards.get(edge.to); if (!from || !to) continue; const offset = (index - (edges.length - 1) / 2) * 8, downward = to.top >= from.top, start = {x: Math.max(from.left + 12, Math.min(from.right - 12, from.left + from.width / 2 + offset)), y: downward ? from.bottom : from.top}, end = {x: Math.max(to.left + 12, Math.min(to.right - 12, to.left + to.width / 2 + offset)), y: downward ? to.top : to.bottom}, approach = {x: end.x, y: end.y + (downward ? -12 : 12)}, obstacles = [...cards].filter(([id]) => id !== edge.from && id !== edge.to).map(([, rect]) => expandedRect(rect)), routeToApproach = routePath(start, approach, obstacles, occupied, outer), route = pathSegments([...routeToApproach.coords, end]), line = document.createElementNS(ns, "path"); line.setAttribute("d", svgPath(route.coords)); line.setAttribute("marker-end", "url(#project-tree-dependency-arrow)"); svg.append(line); occupied.push(...route.segments); }
    if (canvas.parentNode && canvas.parentNode.dataset && canvas.parentNode.dataset.showDependencyLines === "false") svg.style.display = "none";
    canvas.append(svg);
  }

  function renderProjectDetailLineage(ctx) {
    const container = typeof document.getElementById === "function" && document.getElementById("project-detail-lineage"); if (!container) return;
    container.replaceChildren(); const entities = ctx.snapshot && Array.isArray(ctx.snapshot.entities) ? ctx.snapshot.entities : [], byId = new Map(entities.filter((entity) => entity && entity.archived !== true).map((entity) => [entity.id, entity])), fm = ctx.safeFrontmatter(ctx.detail), outcome = byId.get(fm.roadmap_outcome_id), directGoal = byId.get(fm.goal_id), area = byId.get(fm.area_id), link = (href, text) => { const node = document.createElement("a"); node.href = href; node.textContent = text; return node; };
    if (outcome && outcome.kind === "roadmap_outcomes") { const outcomeFm = ctx.safeFrontmatter(outcome), goal = byId.get(outcomeFm.goal_id), line = document.createElement("p"), label = document.createElement("span"); label.textContent = "上位"; line.append(label, document.createTextNode(": "), link("/roadmap?outcome=" + encodeURIComponent(outcome.id), outcomeFm.title || outcome.id)); if (goal && goal.kind === "goals") line.append(document.createTextNode(" → "), link("/projects?level=goals&id=" + encodeURIComponent(goal.id), "Goal: " + (ctx.safeFrontmatter(goal).title || goal.id))); container.append(line); }
    else if (directGoal && directGoal.kind === "goals") { const line = document.createElement("p"), label = document.createElement("span"); label.textContent = "上位"; line.append(label, document.createTextNode(": "), link("/projects?level=goals&id=" + encodeURIComponent(directGoal.id), "Goal: " + (ctx.safeFrontmatter(directGoal).title || directGoal.id))); container.append(line); }
    if (area && area.kind === "areas") { const line = document.createElement("p"), label = document.createElement("span"); label.textContent = "Area"; line.append(label, document.createTextNode(": "), link("/projects?level=areas&id=" + encodeURIComponent(area.id), ctx.safeFrontmatter(area).title || area.id)); container.append(line); }
    container.hidden = !container.children.length;
  }

  function renderRoadmapProjectCard(ctx) {
    const {project, outcome, safeFrontmatter, roadmapTitle, roadmapAreaTitle, period, onOpen} = ctx, fm = safeFrontmatter(project), link = document.createElement("a"), title = document.createElement("strong"), meta = document.createElement("p"), summary = document.createElement("p"); link.className = "roadmap-project-card"; link.href = "/projects?level=projects&id=" + encodeURIComponent(project.id); title.textContent = roadmapTitle(project); meta.className = "muted"; meta.textContent = (fm.status || "未設定") + " / " + roadmapAreaTitle(fm.area_id) + (fm.planned_start_date && fm.planned_end_date ? " / 目安期間: " + period.label(fm) : ""); if (project.body) { summary.className = "roadmap-project-summary"; summary.textContent = project.body.replace(/\s+/g, " ").trim().slice(0, 120); } link.append(title, meta); if (summary.textContent) link.append(summary); link.addEventListener("click", (event) => { if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return; event.preventDefault(); onOpen(outcome.id, project.id); }); return link;
  }

  function renderRoadmapOutcomeCard(ctx) {
    const {item, areaOptions, areaTone, appendAreaDot, areaLabel, onOpen, onDrag} = ctx, node = document.createElement("a"), primaryArea = item.areaIds[0] || "__unassigned__", title = document.createElement("strong"), state = document.createElement("span"), date = document.createElement("span"), projects = document.createElement("span"), area = document.createElement("span"); node.className = "roadmap-year-outcome"; node.setAttribute("href", "/roadmap?outcome=" + encodeURIComponent(item.id)); node.classList.add("roadmap-area-accent-" + areaTone(primaryArea, areaOptions)); state.className = "roadmap-outcome-state"; state.textContent = item.state; title.className = "roadmap-outcome-title"; title.textContent = item.title; date.className = "roadmap-outcome-date"; date.textContent = item.targetDate || "時期未設定"; projects.className = "roadmap-outcome-projects"; projects.textContent = "Project " + item.projectCount + "件"; area.className = "roadmap-outcome-area"; appendAreaDot(area, primaryArea, areaOptions); area.append(document.createTextNode(areaLabel(primaryArea, areaOptions) + (item.areaIds.length > 1 ? " +" + (item.areaIds.length - 1) : ""))); node.append(state, title, date, projects, area); node.setAttribute("aria-label", item.state + "、" + item.title + "、" + (item.targetDate || "時期未設定") + "、Project " + item.projectCount + "件、" + areaLabel(primaryArea, areaOptions) + "の詳細を表示"); node.addEventListener("click", (event) => { if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return; event.preventDefault(); onOpen(node); }); onDrag(node, item); return node;
  }

  function centerInitialTree(projectId, tree) {
    if (!projectId || typeof document === "undefined" || typeof document.getElementById !== "function") return;
    const viewport = document.getElementById("project-plan-viewport");
    if (!viewport || viewport.dataset.centeredProjectId === projectId) return;
    const apply = () => { const card = tree.querySelector(".project-tree-card"); if (!card || !viewport.getBoundingClientRect || !card.getBoundingClientRect) return; const viewportRect = viewport.getBoundingClientRect(), cardRect = card.getBoundingClientRect(), cardLeft = cardRect.left - viewportRect.left + viewport.scrollLeft; viewport.scrollLeft = centerScrollLeft(viewport.clientWidth, viewport.scrollWidth, cardLeft, cardRect.width); };
    viewport.dataset.centeredProjectId = projectId;
    if (typeof global.requestAnimationFrame === "function") global.requestAnimationFrame(apply); else apply();
  }

  function renderTaskRegions(ctx, tasks) {
    const {elements} = ctx;
    elements.projectOtherTaskGroups.replaceChildren(); for (const menu of statusMenus) if (elements.projectPlanTree.contains(menu)) statusMenus.delete(menu);
    const startTasks = (ctx.snapshot && Array.isArray(ctx.snapshot.entities) ? ctx.snapshot.entities : tasks).filter((entity) => entity && entity.kind === "tasks" && entity.archived !== true), treeContext = {...ctx, startTasks};
    const visibleStatuses = ["planned", "next", "doing", "waiting", "scheduled", "someday"], support = supportTasks(tasks, ctx.safeFrontmatter), supportIds = new Set(support.map((task) => task.id)), continuations = collapseContinuationChains(tasks, ctx.safeFrontmatter), continuationGroups = new Map(continuations.groups.map((group) => [group.latestId, group])), forestTasks = support.filter((task) => { const status = ctx.safeFrontmatter(task).status; return !continuations.hiddenIds.has(task.id) && (visibleStatuses.includes(status) || (status === "done" && elements.projectPlanDoneToggle && elements.projectPlanDoneToggle.checked)); }), forest = buildForest(forestTasks, ctx.safeFrontmatter), byId = new Map(tasks.map((task) => [task.id, task])); elements.projectPlanTree.replaceChildren(); const visited = new Set(); appendForestLevel(treeContext, forest, byId, continuationGroups, elements.projectPlanTree, forest.roots, 0, visited); if (!elements.projectPlanTree.children.length) ctx.appendListText(elements.projectPlanTree, "計画ツリーはありません。"); drawDependencyLines(elements.projectPlanTree, forestTasks, ctx.safeFrontmatter);
    const standalone = tasks.filter((task) => !supportIds.has(task.id) && (visibleStatuses.includes(ctx.safeFrontmatter(task).status) || (ctx.safeFrontmatter(task).status === "done" && elements.projectOtherDoneToggle && elements.projectOtherDoneToggle.checked)));
    for (const status of [...visibleStatuses, "done"]) { const matching = standalone.filter((task) => ctx.safeFrontmatter(task).status === status); if (matching.length) elements.projectOtherTaskGroups.append(statusGroup(ctx, status, matching)); }
    if (!elements.projectOtherTaskGroups.children.length) ctx.appendListText(elements.projectOtherTaskGroups, "単独Taskはありません。");
  }

  function refreshTaskPresentation(ctx, source) {
    if (!source || !Array.isArray(source.entities) || !ctx || !ctx.detail || !ctx.elements) return false;
    renderTaskRegions({...ctx, snapshot: source}, ctx.projectTasks(source, ctx.detail.id)); return true;
  }

  function renderDetail(ctx) {
    const {detail, snapshot, elements} = ctx, fm = ctx.safeFrontmatter(detail), tasks = ctx.projectTasks(snapshot, detail.id);
    const openTaskDetail = (event, id) => openTaskDetailSheet({event, elements, id, detail: (value) => ctx.apiRequest(ctx.entityDetailPath("tasks", value)), snapshot: () => ctx.apiRequest("/api/v1/snapshot"), render: ({content, detail: task, source, onEdit}) => renderTaskDetail({content, detail: task, snapshot: source, safeFrontmatter: ctx.safeFrontmatter, taskStatusLabel: ctx.taskStatusLabel, clarifyHref: ctx.clarifyHref, onEdit}), edit: ctx.openTaskEditor, refresh: (_task, source) => refreshTaskPresentation(treeContext, source), failed: ctx.showRequestError});
    const treeContext = {...ctx, openTaskDetail};
    renderProjectDetailLineage(ctx);
    elements.projectDetailHeading.textContent = fm.title || detail.id; if (elements.projectDetailPeriod) elements.projectDetailPeriod.textContent = "目安期間: " + (fm.planned_start_date && fm.planned_end_date ? fm.planned_start_date + " 〜 " + fm.planned_end_date : "時期未設定"); elements.projectDetailState.textContent = "状態: " + ctx.projectStatusLabel(fm.status); if (elements.projectDetailStatus) elements.projectDetailStatus.value = fm.status || "not_started"; if (global.MarkdownPreview && global.MarkdownPreview.load) global.MarkdownPreview.load(elements.projectSupportBody, detail); else elements.projectSupportBody.value = detail.body || "";
    renderTaskRegions(treeContext, tasks);
    centerInitialTree(detail.id, elements.projectPlanTree);
  }

  function setupViewport(tree) {
    const viewport = document.getElementById("project-plan-viewport"), canvas = document.getElementById("project-plan-canvas");
    if (!viewport || !canvas || !tree || viewport.dataset.ready === "true") return;
    viewport.hidden = false; viewport.dataset.ready = "true";
    let zoom = 100, pan = null;
    const zoomValue = document.getElementById("project-plan-zoom-value"), zoomOut = document.getElementById("project-plan-zoom-out"), zoomIn = document.getElementById("project-plan-zoom-in"), zoomReset = document.getElementById("project-plan-zoom-reset"), zoomFit = document.getElementById("project-plan-zoom-fit"), toolbar = viewport.querySelector(".project-tree-toolbar"); let maximized = false;
    const maximize = document.getElementById("project-plan-maximize") || (() => { if (!toolbar) return null; const button = document.createElement("button"); button.id = "project-plan-maximize"; button.className = "secondary"; button.type = "button"; button.textContent = "最大化"; button.setAttribute("aria-expanded", "false"); button.setAttribute("aria-pressed", "false"); button.setAttribute("aria-controls", "project-plan-viewport"); toolbar.insertBefore(button, toolbar.children[0] || null); return button; })();
    const dependencies = document.getElementById("project-plan-dependency-toggle") || (() => { if (!toolbar) return null; const button = document.createElement("button"); button.id = "project-plan-dependency-toggle"; button.className = "secondary"; button.type = "button"; button.setAttribute("aria-controls", "project-plan-viewport"); toolbar.append(button); return button; })();
    const desktop = () => !global.matchMedia || global.matchMedia("(min-width: 1024px)").matches;
    const draw = () => { if (desktop()) { canvas.style.zoom = ""; canvas.style.transform = `scale(${zoom / 100})`; } else { canvas.style.transform = "none"; canvas.style.zoom = String(zoom / 100); } if (zoomValue) zoomValue.textContent = zoom + "%"; };
    const setZoom = (value) => { zoom = Math.max(50, Math.min(150, value)); draw(); };
    const fit = () => { if (!desktop()) return; const toolbar = viewport.querySelector(".project-tree-toolbar"), availableHeight = viewport.clientHeight - (toolbar ? toolbar.offsetHeight : 0); setZoom(fitZoom(viewport.clientWidth, availableHeight, canvas.scrollWidth, canvas.scrollHeight)); viewport.scrollLeft = 0; viewport.scrollTop = 0; };
    const setMaximized = (value, restoreFocus = false) => { maximized = Boolean(value); if (document.body && document.body.classList) document.body.classList.toggle("project-tree-maximized", maximized); if (maximize) { maximize.setAttribute("aria-expanded", String(maximized)); maximize.setAttribute("aria-pressed", String(maximized)); maximize.textContent = maximized ? "元に戻す" : "最大化"; if (restoreFocus) maximize.focus({preventScroll: true}); } };
    const setDependencies = (value) => { const visible = Boolean(value); viewport.dataset.showDependencyLines = String(visible); if (dependencies) { dependencies.setAttribute("aria-pressed", String(visible)); dependencies.textContent = visible ? "追加前提線: 表示" : "追加前提線: 非表示"; } const lines = canvas.querySelector && canvas.querySelector(".project-tree-dependency-lines"); if (lines) lines.style.display = visible ? "" : "none"; };
    if (zoomOut) zoomOut.addEventListener("click", () => setZoom(zoom - 10));
    if (zoomIn) zoomIn.addEventListener("click", () => setZoom(zoom + 10));
    if (zoomReset) zoomReset.addEventListener("click", () => setZoom(100));
    if (zoomFit) zoomFit.addEventListener("click", fit);
    if (maximize) maximize.addEventListener("click", () => setMaximized(!maximized, maximized));
    if (dependencies) dependencies.addEventListener("click", () => setDependencies(viewport.dataset.showDependencyLines === "false"));
    viewport.addEventListener("pointerdown", (event) => { const interactive = event.target && event.target.closest && event.target.closest("a,button,input,select,textarea,summary"); if (!desktop() || interactive) return; pan = {id: event.pointerId, x: event.clientX, y: event.clientY, left: viewport.scrollLeft, top: viewport.scrollTop}; if (viewport.setPointerCapture) viewport.setPointerCapture(event.pointerId); });
    viewport.addEventListener("pointermove", (event) => { if (!pan || pan.id !== event.pointerId) return; viewport.scrollLeft = pan.left - (event.clientX - pan.x); viewport.scrollTop = pan.top - (event.clientY - pan.y); });
    const stopPan = (event) => { if (!pan || (event && pan.id !== event.pointerId)) return; if (viewport.releasePointerCapture && pan) viewport.releasePointerCapture(pan.id); pan = null; };
    viewport.addEventListener("pointerup", stopPan); viewport.addEventListener("pointercancel", stopPan);
    viewport.addEventListener("keydown", (event) => { if (event.key === "Escape" && maximized) { event.preventDefault(); setMaximized(false, true); return; } if (!desktop() || (event.target !== viewport && event.target !== canvas)) return; const key = event.key; if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "+", "-", "0", "f", "F"].includes(key)) return; event.preventDefault(); if (key === "ArrowLeft") viewport.scrollLeft -= 48; if (key === "ArrowRight") viewport.scrollLeft += 48; if (key === "ArrowUp") viewport.scrollTop -= 48; if (key === "ArrowDown") viewport.scrollTop += 48; if (key === "+") setZoom(zoom + 10); if (key === "-") setZoom(zoom - 10); if (key === "0") setZoom(100); if (["f", "F"].includes(key)) fit(); });
    setDependencies(viewport.dataset.showDependencyLines !== "false"); draw();
  }

  function taskDetailLine(label, value) { const node = document.createElement("p"), strong = document.createElement("strong"); strong.textContent = label + ": "; node.append(strong); const text = document.createElement("span"); text.textContent = value || "未設定"; node.append(text); return node; }
  function renderTaskContinuations({content, detail, snapshot, safeFrontmatter, taskStatusLabel, clarifyHref}) {
    content.replaceChildren();
    const section = document.createElement("section"), heading = document.createElement("p"), label = document.createElement("strong"), list = document.createElement("ul");
    section.className = "task-continuations"; section.setAttribute("aria-label", "続きTask"); heading.className = "task-continuations-heading"; label.textContent = "続きTask"; heading.append(label);
    const children = new Map();
    for (const entity of Array.isArray(snapshot && snapshot.entities) ? snapshot.entities : []) {
      if (entity.kind !== "tasks" || !entity.id) continue;
      const parent = safeFrontmatter(entity).continuation_of;
      if (!parent) continue;
      const siblings = children.get(parent) || []; siblings.push(entity); children.set(parent, siblings);
    }
    const visited = new Set([detail.id]), pending = [...(children.get(detail.id) || [])];
    while (pending.length) {
      const entity = pending.shift();
      if (visited.has(entity.id)) continue;
      visited.add(entity.id);
      pending.push(...(children.get(entity.id) || []));
      if (entity.archived === true) continue;
      const fields = safeFrontmatter(entity), item = document.createElement("li"), link = document.createElement("a"), status = document.createElement("span");
      link.href = clarifyHref(entity.id); link.textContent = fields.title || entity.id;
      status.className = "task-continuation-status"; status.textContent = taskStatusLabel(fields.status);
      item.append(link, status); list.append(item);
    }
    if (list.children.length) section.append(heading, list);
    else { const empty = document.createElement("p"); empty.className = "task-continuation-empty muted"; empty.textContent = "続きTaskはありません。"; section.append(heading, empty); }
    content.append(section);
  }
  function renderTaskDetail({content, detail, snapshot, safeFrontmatter, taskStatusLabel, clarifyHref, onEdit}) { const fm = safeFrontmatter(detail), project = (snapshot.entities || []).find((entity) => entity.kind === "projects" && entity.id === fm.project_id), projectFm = project ? safeFrontmatter(project) : {}; content.replaceChildren(); const heading = document.createElement("h4"), dates = [["対応予定日", fm.action_date], ["期限", fm.due], ["開始可能日", fm.available_from], ["予定開始", fm.scheduled_start], ["予定終了", fm.scheduled_end]].filter((item) => item[1]), body = document.createElement("pre"), edit = document.createElement("a"), continuations = document.createElement("div"); heading.textContent = fm.title || detail.id; body.textContent = detail.body || "本文はありません。"; edit.className = "button-link task-detail-edit"; edit.setAttribute("href", clarifyHref(detail.id)); edit.textContent = "Taskを編集"; if (onEdit) edit.addEventListener("click", onEdit); renderTaskContinuations({content: continuations, detail, snapshot, safeFrontmatter, taskStatusLabel, clarifyHref}); content.append(heading, taskDetailLine("状態", taskStatusLabel(fm.status)), taskDetailLine("Project", projectFm.title || fm.project_id), ...dates.map(([label, value]) => taskDetailLine(label, value)), continuations, body, edit); global.LocalCapture?.renderDetail(detail, content, body); }

  let projectDetailHome = null, projectDetailSheet = null, taskDetailSheet = null, roadmapResize = null;
  function restoreProjectDetail(elements) { const home = projectDetailHome; if (!home) return; const panel = elements.projectDetailPanel; if (home.placeholder.parentNode) home.placeholder.parentNode.insertBefore(panel, home.placeholder); home.placeholder.remove(); elements.projectDetailBack.textContent = home.backText; elements.projectDetailBack.href = home.backHref; elements.projectDetailBack.onclick = home.backOnClick; if (elements.projectDetailEdit) elements.projectDetailEdit.hidden = home.editHidden; panel.hidden = home.panelHidden; elements.projectListing.hidden = home.listingHidden; projectDetailHome = null; }
  function mountProjectDetail({elements, content, detail, snapshot, render, backText, backHref, onBack}) { restoreProjectDetail(elements); const panel = elements.projectDetailPanel, placeholder = document.createComment("project-detail-home"); if (!panel.parentNode) throw new Error("Project詳細の復元先がありません。"); panel.parentNode.insertBefore(placeholder, panel); projectDetailHome = {placeholder, backText: elements.projectDetailBack.textContent, backHref: elements.projectDetailBack.href, backOnClick: elements.projectDetailBack.onclick, editHidden: elements.projectDetailEdit && elements.projectDetailEdit.hidden, panelHidden: panel.hidden, listingHidden: elements.projectListing.hidden}; content.replaceChildren(panel); render(detail, snapshot); elements.projectListing.hidden = projectDetailHome.listingHidden; if (elements.projectDetailEdit) elements.projectDetailEdit.hidden = true; if (backText) elements.projectDetailBack.textContent = backText; if (backHref) elements.projectDetailBack.href = backHref; if (onBack) elements.projectDetailBack.onclick = (event) => { event.preventDefault(); onBack(); }; }
  function capturePanel(elements) {
    return {children: [...elements.roadmapOutcomeDetailContent.children].map((node) => ({node, hidden: node.hidden})), heading: elements.roadmapOutcomeDetailHeading.textContent, panelHidden: elements.roadmapOutcomeDetailPanel.hidden, backdropHidden: elements.roadmapDetailBackdrop.hidden, panelScrollTop: elements.roadmapOutcomeDetailPanel.scrollTop, bodyOpen: document.body.classList.contains("roadmap-detail-open")};
  }
  function restorePanel(elements, state) {
    for (const item of state.children) item.node.hidden = item.hidden;
    elements.roadmapOutcomeDetailHeading.textContent = state.heading; elements.roadmapOutcomeDetailPanel.scrollTop = state.panelScrollTop;
    elements.roadmapOutcomeDetailPanel.hidden = state.panelHidden; elements.roadmapDetailBackdrop.hidden = state.backdropHidden;
    if (!state.bodyOpen) document.body.classList.remove("roadmap-detail-open");
  }
  function openProjectDetailSheet({event, elements, id, render, detail, snapshot, mounted, failed, push = true}) {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0 || !id) return false;
    event.preventDefault(); if (projectDetailSheet || taskDetailSheet) return true;
    const host = document.createElement("section"), state = {...capturePanel(elements), request: 1, id, trigger: event.currentTarget, elements, render, mounted, host, closing: false};
    projectDetailSheet = state; host.className = "project-detail-sheet";
    for (const item of state.children) item.node.hidden = true;
    elements.roadmapOutcomeDetailContent.append(host); elements.roadmapOutcomeDetailPanel.hidden = false; elements.roadmapDetailBackdrop.hidden = false; document.body.classList.add("roadmap-detail-open");
    elements.roadmapOutcomeDetailHeading.textContent = "Project詳細"; host.textContent = "Project詳細を読み込み中です。"; elements.roadmapOutcomeDetailHeading.focus();
    if (push) global.history.pushState({projectDetailId: id}, "", global.location.href);
    Promise.all([detail(id), snapshot()]).then(([value, source]) => {
      if (projectDetailSheet !== state || state.closing) return;
      if (!value || value.id !== id || value.kind !== "projects" || value.archived === true || !source || !Array.isArray(source.entities)) throw new Error("Project詳細を読み込めませんでした。");
      mounted(value, source); mountProjectDetail({elements, content: host, detail: value, snapshot: source, render, backText: "Projectを編集", backHref: "/projects?level=projects&id=" + encodeURIComponent(id)});
      elements.roadmapOutcomeDetailPanel.scrollTop = 0; elements.roadmapOutcomeDetailHeading.focus();
    }).catch((error) => { if (projectDetailSheet !== state || state.closing) return; restoreProjectDetail(elements); host.textContent = "Project詳細を読み込めませんでした。"; failed(error); }); return true;
  }
  function renderTaskDetailSheet(elements, state) { state.render({content: state.host, detail: state.detail, source: state.source, onEdit: (event) => openTaskEditSheet({event, elements, mount: state.editMount})}); elements.roadmapOutcomeDetailHeading.textContent = "Task"; }
  function openTaskEditSheet({event, elements, mount, push = true}) { if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0 || !taskDetailSheet || taskDetailSheet.edit || typeof mount !== "function") return false; event.preventDefault(); const state = taskDetailSheet, edit = {trigger: event.currentTarget, closing: false, cleanup: null, session: {}}; state.edit = edit; try { edit.cleanup = mount({content: state.host, detail: state.detail, source: state.source, session: edit.session, cancel: () => closeTaskEditSheet(elements, true, edit.session)}) || null; } catch (error) { state.edit = null; renderTaskDetailSheet(elements, state); throw error; } elements.roadmapOutcomeDetailHeading.textContent = "Taskを編集"; elements.roadmapOutcomeDetailPanel.scrollTop = 0; if (push) global.history.pushState({taskEditId: state.detail.id}, "", global.location.href); return true; }
  function closeTaskEditSheet(elements, updateHistory = true, expectedSession = null, restoreBlockedHistory = !updateHistory) { const state = taskDetailSheet, edit = state && state.edit; if (!edit || (expectedSession && edit.session !== expectedSession)) return false; const canClose = !edit.cleanup || typeof edit.cleanup.canClose !== "function" || edit.cleanup.canClose(); if (!canClose) { if (restoreBlockedHistory) global.history.pushState({taskEditId: state.detail.id}, "", global.location.href); return true; } if (updateHistory) { if (!edit.closing) { edit.closing = true; global.history.back(); } return true; } state.edit = null; edit.closing = true; if (typeof edit.cleanup === "function") edit.cleanup(); renderTaskDetailSheet(elements, state); elements.roadmapOutcomeDetailPanel.scrollTop = 0; const nextEdit = state.host.querySelector(".task-detail-edit"); if (nextEdit) nextEdit.focus(); else elements.roadmapOutcomeDetailHeading.focus(); return true; }
  function updateTaskDetailSheet(detail, source, expectedSession = null) { if (!taskDetailSheet || (expectedSession && (!taskDetailSheet.edit || taskDetailSheet.edit.session !== expectedSession)) || !detail || detail.id !== taskDetailSheet.detail.id || detail.kind !== "tasks" || detail.archived === true || !source || !Array.isArray(source.entities)) return false; taskDetailSheet.detail = detail; taskDetailSheet.source = source; if (typeof taskDetailSheet.refresh === "function") taskDetailSheet.refresh(detail, source); if (!taskDetailSheet.edit) renderTaskDetailSheet(taskDetailSheet.elements, taskDetailSheet); return true; }
  function visibleNode(node) { for (let current = node; current; current = current.parentNode) if (current.hidden) return false; return true; }
  function replacementTaskTrigger(state) { const roots = state.children.map((item) => item.node), outside = typeof document.querySelectorAll === "function" ? [...document.querySelectorAll("[data-task-id], [data-task-link-id]")] : []; for (const node of [...roots.flatMap((root) => [...root.querySelectorAll("[data-task-id]"), ...root.querySelectorAll("[data-task-link-id]")]), ...outside]) { if (node.dataset.taskId !== state.detail.id && node.dataset.taskLinkId !== state.detail.id) continue; const trigger = node.classList.contains("task-card-title") ? node : node.querySelector(".task-card-title") || node; if (visibleNode(trigger)) return trigger; } return null; }
  function replacementProjectTrigger(id) { if (typeof document.querySelectorAll !== "function") return null; for (const node of document.querySelectorAll("[data-project-id]")) if (node.dataset.projectId === id) { const trigger = node.querySelector("a") || node; if (visibleNode(trigger)) return trigger; } return null; }
  function openTaskDetailSheet({event, elements, id, render, edit, refresh, detail, snapshot, failed, push = true}) { if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0 || !id) return false; event.preventDefault(); if (taskDetailSheet) return true; const content = elements.roadmapOutcomeDetailContent, host = document.createElement("section"), parent = capturePanel(elements), children = parent.children, state = {...parent, id, trigger: event.currentTarget, host, closing: false, detail: null, source: null, render, refresh, editMount: edit, edit: null, elements}; host.className = "task-detail-sheet"; for (const item of children) item.node.hidden = true; content.append(host); taskDetailSheet = state; elements.roadmapOutcomeDetailPanel.hidden = false; elements.roadmapDetailBackdrop.hidden = false; document.body.classList.add("roadmap-detail-open"); elements.roadmapOutcomeDetailHeading.textContent = "Task詳細"; host.textContent = "Task詳細を読み込み中です。"; elements.roadmapOutcomeDetailHeading.focus(); if (push) global.history.pushState({taskDetailId: id}, "", global.location.href); state.ready = Promise.all([detail(id), snapshot()]).then(([value, source]) => { if (taskDetailSheet !== state || state.closing) return; if (!value || value.id !== id || value.kind !== "tasks" || value.archived === true || !source || !Array.isArray(source.entities)) throw new Error("Task詳細を読み込めませんでした。"); state.detail = value; state.source = source; renderTaskDetailSheet(elements, state); elements.roadmapOutcomeDetailPanel.scrollTop = 0; elements.roadmapOutcomeDetailHeading.focus(); }).catch((error) => { if (taskDetailSheet !== state || state.closing) return; host.textContent = "Task詳細を読み込めませんでした。"; failed(error); }); return true; }
  function closeTaskDetailSheet(elements, updateHistory = true, restoreBlockedHistory = !updateHistory) { if (!taskDetailSheet) return false; if (taskDetailSheet.edit) return closeTaskEditSheet(elements, updateHistory, null, restoreBlockedHistory); const state = taskDetailSheet; if (updateHistory) { if (!state.closing) { state.closing = true; global.history.back(); } return true; } state.closing = true; taskDetailSheet = null; state.host.remove(); restorePanel(elements, state); const focus = state.trigger && state.trigger.isConnected && visibleNode(state.trigger) ? state.trigger : replacementTaskTrigger(state) || (state.panelHidden ? elements.focusFilterSummary || elements.projectDetailHeading : elements.roadmapOutcomeDetailHeading); if (focus) focus.focus({preventScroll: true}); return true; }
  function closeProjectDetailSheet(elements, updateHistory = true) {
    if (closeTaskDetailSheet(elements, updateHistory)) return true;
    const state = projectDetailSheet; if (!state) return false;
    if (updateHistory) { if (!state.closing) { state.closing = true; global.history.back(); } return true; }
    state.closing = true; state.request += 1; restoreProjectDetail(elements); state.host.remove(); projectDetailSheet = null; restorePanel(elements, state);
    const trigger = state.trigger && state.trigger.isConnected && visibleNode(state.trigger) ? state.trigger : replacementProjectTrigger(state.id) || (state.panelHidden ? elements.projectsTab : elements.roadmapOutcomeDetailHeading);
    if (trigger) trigger.focus({preventScroll: true}); return true;
  }
  function createEntityDetailController(ctx) {
    const snapshot = () => ctx.apiRequest("/api/v1/snapshot"), detail = (kind, id) => ctx.apiRequest(ctx.entityDetailPath(kind, id));
    const openTask = (event, id, refresh, push = true) => openTaskDetailSheet({event, elements: ctx.elements, id, detail: (value) => detail("tasks", value), snapshot, render: ({content, detail: value, source, onEdit}) => renderTaskDetail({content, detail: value, snapshot: source, safeFrontmatter: ctx.safeFrontmatter, taskStatusLabel: ctx.taskStatusLabel, clarifyHref: ctx.clarifyHref, onEdit}), edit: ctx.openTaskEditor, refresh, failed: ctx.failed, push});
    const openProject = (event, id, push = true) => openProjectDetailSheet({event, elements: ctx.elements, id, render: ctx.renderProject, detail: (value) => detail("projects", value), snapshot, mounted: ctx.mountedProject, failed: ctx.failed, push});
    const refreshProject = async (source) => { const state = projectDetailSheet; if (!state) return false; const value = await detail("projects", state.id); if (projectDetailSheet !== state) return false; if (!value || value.id !== state.id || value.kind !== "projects" || value.archived === true) throw new Error("Project詳細を読み込めませんでした。"); state.mounted(value, source); state.render(value, source); if (projectDetailHome) state.elements.projectListing.hidden = projectDetailHome.listingHidden; state.elements.projectDetailBack.textContent = "Projectを編集"; state.elements.projectDetailBack.href = "/projects?level=projects&id=" + encodeURIComponent(state.id); if (state.elements.projectDetailEdit) state.elements.projectDetailEdit.hidden = true; state.elements.roadmapOutcomeDetailHeading.focus(); return true; };
    const bindTaskCard = (card, entity, refresh) => { const link = card && card.querySelector(".task-card-title"); if (link) link.addEventListener("click", (event) => openTask(event, entity.id, refresh)); return link; }, bindProjectLink = (link, entity) => { if (link) link.addEventListener("click", (event) => openProject(event, entity.id)); return link; };
    return {openTask, openProject, refreshProject, bindTaskCard, bindProjectLink, t: bindTaskCard, p: bindProjectLink};
  }
  // Mount the normal screen itself: its forms, listeners and mutation flow remain singleton.
  function createReviewListController(ctx) {
    const {elements} = ctx;
    let active = null, navigationGeneration = 0;
    const eligible = (event) => !event.defaultPrevented && event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
    function currentHref() { return active ? active.href : null; }
    function updateHref(href) {
      if (!active || !ctx.screenFor(href)) return false;
      active.href = href; global.history.replaceState({reviewList: href}, "", global.location.href); return true;
    }
    function mount(state, href, screen) {
      state.href = href; state.screen = screen;
      const node = screen.node, placeholder = document.createComment("review-screen-home");
      node.parentNode.insertBefore(placeholder, node); state.home = {placeholder, hidden: node.hidden};
      state.host.append(node); node.hidden = false;
      ctx.activate(screen, href);
      elements.roadmapOutcomeDetailHeading.textContent = screen.title;
      elements.roadmapOutcomeDetailPanel.scrollTop = 0;
      void screen.load();
    }
    function unmount(state) {
      ctx.deactivate(state.screen);
      const {placeholder, hidden} = state.home;
      if (placeholder.parentNode) placeholder.parentNode.insertBefore(state.screen.node, placeholder);
      placeholder.remove(); state.screen.node.hidden = hidden;
    }
    function blocked(updateHistory) {
      if (!ctx.busy() && ctx.canClose(elements.roadmapOutcomeDetailContent)) return false;
      if (!updateHistory) global.history.pushState(taskDetailSheet ? {taskDetailId: taskDetailSheet.id} : projectDetailSheet ? {projectDetailId: projectDetailSheet.id} : {reviewList: active.href}, "", global.location.href);
      if (ctx.busy()) active.status.textContent = "保存処理中です。完了するまでお待ちください。";
      return true;
    }
    function open(event, href, push = true) {
      const screen = ctx.screenFor(href);
      if (!eligible(event) || !screen) return false;
      event.preventDefault();
      if (active) {
        if (active.closing || active.href === href) return true;
        if (blocked(true) || taskDetailSheet) return true;
        if (projectDetailSheet) { if (!href.includes("id=")) return true; closeProjectDetailSheet(elements, false); }
        unmount(active); mount(active, href, screen); updateHref(href); return true;
      }
      const host = document.createElement("section"), status = document.createElement("p"), state = {...capturePanel(elements), trigger: event.currentTarget, href, originHref: href, host, status, scrollY: global.scrollY || 0, closing: false};
      host.className = "review-screen-workspace"; state.feedback = [elements.error, elements.notice].filter(Boolean).map(node => { const placeholder = document.createComment("review-feedback-home"); node.parentNode.insertBefore(placeholder, node); elements.roadmapOutcomeDetailContent.parentNode.insertBefore(node, elements.roadmapOutcomeDetailContent); return {node, placeholder}; }); status.setAttribute("role", "status"); host.append(status);
      for (const item of state.children) item.node.hidden = true;
      active = state; elements.roadmapOutcomeDetailContent.append(host);
      elements.roadmapOutcomeDetailPanel.hidden = false; elements.roadmapDetailBackdrop.hidden = false; document.body.classList.add("roadmap-detail-open");
      mount(state, href, screen); elements.roadmapOutcomeDetailHeading.focus();
      // Keep the Review URL and its live Notes/checks behind the workspace.
      if (push) global.history.pushState({reviewList: href}, "", global.location.href);
      return true;
    }
    function close(updateHistory = true) {
      if (!active) return false;
      if (active.closing && updateHistory) return true;
      if ((!active.closing || ctx.busy()) && blocked(updateHistory)) return true;
      if (closeProjectDetailSheet(elements, updateHistory)) return true;
      const state = active;
      if (updateHistory) { state.closing = true; global.history.back(); return true; }
      ++navigationGeneration; unmount(state); for (const {node, placeholder} of state.feedback) { placeholder.parentNode.insertBefore(node, placeholder); placeholder.remove(); } active = null; state.host.remove(); restorePanel(elements, state);
      elements.roadmapOutcomeDetailPanel.removeAttribute("data-expanded"); elements.roadmapOutcomeDetailPanel.style.removeProperty("--roadmap-sheet-height"); elements.roadmapOutcomeDetailPanel.style.removeProperty("--roadmap-detail-width");
      let trigger = state.trigger;
      if (!trigger || !trigger.isConnected) trigger = [...elements.reviewGuideSteps.querySelectorAll("a")].find((link) => link.getAttribute("href") === state.originHref);
      if (trigger) trigger.focus({preventScroll: true}); if (typeof global.scrollTo === "function") global.scrollTo(0, state.scrollY);
      return true;
    }
    function handlePopState(state) {
      const navigation = ++navigationGeneration;
      if (!active) return !!(state && state.reviewList && open({button: 0, preventDefault() {}}, state.reviewList, false));
      const event = {button: 0, preventDefault() {}}, taskId = state && (state.taskDetailId || state.taskEditId);
      if (taskId) {
        if (taskDetailSheet && taskDetailSheet.id === taskId) {
          if (!state.taskEditId && taskDetailSheet.edit) return close(false);
          if (state.taskEditId && !taskDetailSheet.edit && taskDetailSheet.detail) openTaskEditSheet({event, elements, mount: taskDetailSheet.editMount, push: false});
          return true;
        }
        ctx.details.openTask(event, taskId, () => active && active.screen.load(), false);
        const task = taskDetailSheet;
        if (state.taskEditId && task) void task.ready.then(() => { if (navigationGeneration === navigation && taskDetailSheet === task && task.detail) openTaskEditSheet({event: {button: 0, preventDefault() {}}, elements, mount: task.editMount, push: false}); });
        return true;
      }
      if (state && state.projectDetailId) { if (taskDetailSheet) { if (blocked(false)) return true; if (taskDetailSheet.edit) closeTaskEditSheet(elements, false); closeTaskDetailSheet(elements, false); } if (!projectDetailSheet) ctx.details.openProject(event, state.projectDetailId, false); return true; }
      if (state && state.reviewList) {
        active.closing = false;
        if (taskDetailSheet || projectDetailSheet) {
          if (blocked(false)) return true;
          if (taskDetailSheet && taskDetailSheet.edit) closeTaskEditSheet(elements, false);
          closeProjectDetailSheet(elements, false);
        }
        if (active.href !== state.reviewList) open(event, state.reviewList, false);
        return true;
      }
      if (taskDetailSheet || projectDetailSheet) {
        if (blocked(false)) return true;
        if (taskDetailSheet && taskDetailSheet.edit) closeTaskEditSheet(elements, false);
        closeProjectDetailSheet(elements, false);
      }
      return close(false);
    }
    async function refresh(source) {
      if (!active || active.closing) return false;
      await ctx.refresh(active.screen, source); await ctx.details.refreshProject(source); return true;
    }
    return Object.freeze({open, close, refresh, currentHref, updateHref, handlePopState});
  }
  function restoreRoadmapDetail(elements) { restoreProjectDetail(elements); }
  function mountRoadmapDetail({elements, detail, snapshot, outcomeId, render, query, onBack}) { mountProjectDetail({elements, content: elements.roadmapOutcomeDetailContent, detail, snapshot, render, backText: "Outcomeへ戻る", backHref: query(outcomeId), onBack}); }
  function beginRoadmapDetailResize(event, elements) { if (global.matchMedia && global.matchMedia("(max-width: 720px)").matches) return false; event.preventDefault(); const panel = elements.roadmapOutcomeDetailPanel, width = panel.getBoundingClientRect && panel.getBoundingClientRect().width; roadmapResize = {id: event.pointerId, x: event.clientX, width: Number.isFinite(width) && width > 0 ? width : 520}; elements.roadmapDetailHandle.setPointerCapture(event.pointerId); return true; }
  function moveRoadmapDetailResize(event, elements) { if (!roadmapResize || event.pointerId !== roadmapResize.id) return false; const width = Math.min(Math.max(360, (global.innerWidth || 0) - 32), Math.max(360, roadmapResize.width + roadmapResize.x - event.clientX)); elements.roadmapOutcomeDetailPanel.style.setProperty("--roadmap-detail-width", width + "px"); event.preventDefault(); return true; }
  function endRoadmapDetailResize(event, elements) { if (!roadmapResize || (event && event.pointerId !== roadmapResize.id)) return false; if (elements.roadmapDetailHandle.hasPointerCapture(roadmapResize.id)) elements.roadmapDetailHandle.releasePointerCapture(roadmapResize.id); roadmapResize = null; return true; }
  function closeRoadmapDetail(elements) { while (taskDetailSheet) { const edit = taskDetailSheet.edit; closeTaskDetailSheet(elements, false, false); if (taskDetailSheet && edit && taskDetailSheet.edit === edit) return false; } restoreProjectDetail(elements); endRoadmapDetailResize(null, elements); elements.roadmapOutcomeDetailPanel.style.removeProperty("--roadmap-sheet-height"); elements.roadmapOutcomeDetailPanel.style.removeProperty("--roadmap-detail-width"); elements.roadmapOutcomeDetailPanel.classList.remove("is-dragging"); return true; }

  global.ProjectTaskBoard = Object.freeze({treePayload, buildForest, supportTasks, collapseContinuationChains, dependencyEdges, planEditorItems, primaryParent, fitZoom, centerScrollLeft, indentPx, moveOperation, createPlanEditor, mountPlanEditor, attachReorder, attachGesture, renderDetail, renderTaskRegions, refreshTaskPresentation, renderTaskDetail, renderTaskContinuations, drawDependencyLines, renderProjectDetailLineage, renderRoadmapProjectCard, renderRoadmapOutcomeCard, setupViewport, mountProjectDetail, restoreProjectDetail, openProjectDetailSheet, openTaskDetailSheet, openTaskEditSheet, closeTaskEditSheet, updateTaskDetailSheet, closeTaskDetailSheet, closeProjectDetailSheet, createEntityDetailController, createReviewListController, mountRoadmapDetail, restoreRoadmapDetail, beginRoadmapDetailResize, moveRoadmapDetailResize, endRoadmapDetailResize, closeRoadmapDetail, projectPeriod, sortRoadmapProjectsByEstimatedPeriod});
})(window);
