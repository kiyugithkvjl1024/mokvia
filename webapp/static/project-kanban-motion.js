"use strict";

(() => {
  const LANE_DISCLOSURE_STORAGE_KEY = "mokvia.projects.status-lanes.v1";
  const reduced = () => typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const frame = (callback) => (typeof requestAnimationFrame === "function" ? requestAnimationFrame(callback) : setTimeout(callback, 0));
  const cancel = (value) => { if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(value); else clearTimeout(value); };
  function capture(nodes) { return new Map([...nodes].filter((node) => node && node.isConnected).map((node) => [node, node.getBoundingClientRect()])); }
  function captureProjects(nodes) { return new Map([...nodes].filter((node) => node && node.dataset && node.dataset.projectId).map((node) => [node.dataset.projectId, node.getBoundingClientRect()])); }
  function flip(before) {
    if (reduced()) return;
    for (const [node, previous] of before) { const next = node.getBoundingClientRect(), x = previous.left - next.left, y = previous.top - next.top; if (!x && !y) continue; node.style.transition = "transform 0s"; node.style.transform = "translate3d(" + x + "px," + y + "px,0)"; frame(() => { node.style.transition = "transform .2s cubic-bezier(.2,.8,.2,1)"; node.style.transform = ""; }); }
  }
  function flipProjects(before, nodes) {
    if (reduced()) return;
    for (const node of nodes) {
      const previous = node && node.dataset ? before.get(node.dataset.projectId) : null;
      if (!previous) continue;
      const next = node.getBoundingClientRect(), x = previous.left - next.left, y = previous.top - next.top;
      if (!x && !y) continue;
      node.style.transition = "transform 0s";
      node.style.transform = "translate3d(" + x + "px," + y + "px,0)";
      frame(() => { node.style.transition = "transform .2s cubic-bezier(.2,.8,.2,1)"; node.style.transform = ""; });
    }
  }
  function moveMarker(list, marker, before) { if (!list || !marker) return; const cards = capture(list.querySelectorAll(".project-card")); if (before) list.insertBefore(marker, before); else list.append(marker); flip(cards); }
  function threshold(start, point) { return Math.hypot(point.clientX - start.clientX, point.clientY - start.clientY) >= 8; }
  function beginPointerGesture(event, handle, options = {}) {
    if (!event || !handle || (options.isGated && options.isGated()) || (event.pointerType === "mouse" && event.button !== undefined && event.button !== 0)) return null;
    const state = {pointerId: event.pointerId, startX: event.clientX, startY: event.clientY, handle, active: false, captured: false};
    if (options.captureOnStart !== false && handle.setPointerCapture) { handle.setPointerCapture(event.pointerId); state.captured = true; }
    return state;
  }
  function movePointerGesture(state, event, options = {}) {
    if (!state || !event || state.pointerId !== event.pointerId) return {tracking: false, active: false, activated: false};
    const minimum = Number(options.threshold) > 0 ? Number(options.threshold) : 8;
    if (!state.active && Math.hypot(event.clientX - state.startX, event.clientY - state.startY) < minimum) return {tracking: true, active: false, activated: false};
    const activated = !state.active; state.active = true;
    if (!state.captured && state.handle && state.handle.setPointerCapture) { state.handle.setPointerCapture(state.pointerId); state.captured = true; }
    if (event.preventDefault) event.preventDefault();
    return {tracking: true, active: true, activated};
  }
  function finishPointerGesture(state, event, options = {}) {
    if (!state || !event || state.pointerId !== event.pointerId) return false;
    const active = state.active;
    if (active && options.activationProperty && state.handle) { state.handle[options.activationProperty] = true; if (typeof setTimeout === "function") setTimeout(() => { state.handle[options.activationProperty] = false; }, 0); }
    if (state.captured && state.handle && state.handle.releasePointerCapture) state.handle.releasePointerCapture(state.pointerId);
    state.captured = false;
    return active;
  }
  function cancelPointerGesture(state, event) {
    if (!state || (event && state.pointerId !== event.pointerId)) return false;
    const active = state.active;
    if (state.captured && state.handle && state.handle.releasePointerCapture) state.handle.releasePointerCapture(state.pointerId);
    state.captured = false;
    return active;
  }
  function insertionIndex(rects, y) { for (let index = 0; index < rects.length; index += 1) if (y < rects[index].top + rects[index].height / 2) return index; return rects.length; }
  function target(state, x, y) { const hit = document.elementFromPoint(x, y), lane = hit && hit.closest ? hit.closest(".project-lane") : null; if (!lane || !lane.dataset.status) return; const list = lane.querySelector(".project-lane-list"), cards = [...list.querySelectorAll(".project-card")].filter((card) => card !== state.card), index = insertionIndex(cards.map((card) => card.getBoundingClientRect()), y), before = cards[index] || null, after = cards[index - 1] || null; moveMarker(list, state.marker, before); document.querySelectorAll(".project-lane.is-project-drop-target").forEach((value) => value.classList.remove("is-project-drop-target")); lane.classList.add("is-project-drop-target"); state.targetStatus = lane.dataset.status; state.beforeId = before && before.dataset.projectId || ""; state.afterId = after && after.dataset.projectId || ""; }
  function clear(state) { if (!state) return; if (state.marker) state.marker.remove(); if (state.layer) state.layer.remove(); document.querySelectorAll(".project-lane.is-project-drop-target").forEach((lane) => lane.classList.remove("is-project-drop-target")); }
  function createVisualDragLayer(label, className = "project-drag-layer") { const layer = document.createElement("div"); layer.className = className; layer.setAttribute("aria-hidden", "true"); layer.textContent = label || ""; if (document.body) document.body.append(layer); return layer; }
  function moveVisualDragLayer(layer, x, y) { if (layer && layer.style) layer.style.transform = "translate3d(" + x + "px," + y + "px,0)"; }
  function clearVisualDragLayer(layer) { if (layer) layer.remove(); }
  function createDestinationDock(destinations, point, label = "ここにドロップして移動") {
    const dock = document.createElement("div"), heading = document.createElement("p"), grid = document.createElement("div");
    dock.className = "drag-destination-dock"; dock.setAttribute("role", "region"); dock.setAttribute("aria-label", label); heading.textContent = label; grid.className = "drag-destination-grid";
    for (const destination of destinations) { const target = document.createElement("div"); target.className = "drag-destination"; target.dataset.dragDestination = destination.key; target.textContent = destination.label; if (destination.danger) target.dataset.danger = "true"; grid.append(target); }
    dock.append(heading, grid); document.body.append(dock);
    const rect = dock.getBoundingClientRect(), width = window.innerWidth || 390, height = window.innerHeight || 844, below = point.clientY + 32;
    dock.style.left = Math.max(16, Math.min(width - rect.width - 16, point.clientX - rect.width / 2)) + "px";
    dock.style.top = Math.max(80, Math.min(height - rect.height - 96, below + rect.height <= height - 96 ? below : point.clientY - rect.height - 32)) + "px";
    return dock;
  }
  function destinationAt(dock, point) {
    if (!dock) return "";
    const hit = document.elementFromPoint(point.clientX, point.clientY), target = hit && hit.closest ? hit.closest("[data-drag-destination]") : null, targets = [...dock.children[1].children];
    for (const value of targets) value.dataset.active = String(value === target);
    return targets.includes(target) ? target.dataset.dragDestination : "";
  }
  function clearDestinationDock(dock) { if (dock) dock.remove(); }
  function settleVisualDragLayer(layer, target, complete) {
    if (!layer || reduced() || !target || !target.getBoundingClientRect) { clearVisualDragLayer(layer); if (complete) complete(); return; }
    const rect = target.getBoundingClientRect(); layer.classList.add("is-drag-settling"); moveVisualDragLayer(layer, rect.left + 12, rect.top + 12);
    setTimeout(() => { clearVisualDragLayer(layer); if (complete) complete(); }, 120);
  }
  function autoScroll(state, tick) {
    const panel = state.captureTarget?.closest?.(".roadmap-outcome-detail"), rect = panel ? panel.getBoundingClientRect() : {top: 0, bottom: window.innerHeight || 0}, edge = 56, y = state.lastY;
    const amount = y < rect.top + edge ? -Math.ceil((rect.top + edge - y) / 8) : y > rect.bottom - edge ? Math.ceil((y - rect.bottom + edge) / 8) : 0;
    if (amount) { if (panel) panel.scrollTop += amount; else window.scrollBy(0, amount); }
    state.autoFrame = frame(tick);
  }
  function beginAutoScroll(state, tick) { if (state.autoFrame) return; state.autoFrame = frame(tick); }
  function stopAutoScroll(state) { if (state && state.autoFrame) cancel(state.autoFrame); if (state) state.autoFrame = 0; }
  function laneDisclosureStorage() { try { return window.localStorage; } catch (_error) { return null; } }
  function readLaneDisclosureState() { try { const value = JSON.parse(laneDisclosureStorage()?.getItem(LANE_DISCLOSURE_STORAGE_KEY) || "{}"); return value && typeof value === "object" && !Array.isArray(value) ? value : {}; } catch (_error) { return {}; } }
  function saveLaneDisclosureState(value) { try { laneDisclosureStorage()?.setItem(LANE_DISCLOSURE_STORAGE_KEY, JSON.stringify(value)); } catch (_error) {} }
  function createLaneDisclosure({status, label, count, list}) {
    const button = document.createElement("button"), title = document.createElement("span"), total = document.createElement("span"), marker = document.createElement("span");
    button.type = "button"; button.className = "project-lane-heading"; list.id = "project-lane-" + status + "-list"; button.setAttribute("aria-controls", list.id);
    title.textContent = label; total.className = "project-lane-count"; total.textContent = Math.max(0, Number(count) || 0) + "件"; marker.className = "project-lane-heading-marker"; marker.textContent = "⌄"; marker.setAttribute("aria-hidden", "true"); button.append(title, total, marker);
    const apply = (collapsed) => { button.setAttribute("aria-expanded", String(!collapsed)); list.hidden = collapsed; };
    apply(readLaneDisclosureState()[status] === true);
    button.addEventListener("click", () => { const collapsed = button.getAttribute("aria-expanded") === "true"; apply(collapsed); const state = readLaneDisclosureState(); state[status] = collapsed; saveLaneDisclosureState(state); });
    return button;
  }
  window.ProjectKanbanMotion = Object.freeze({capture, captureProjects, flip, flipProjects, moveMarker, threshold, beginPointerGesture, movePointerGesture, finishPointerGesture, cancelPointerGesture, insertionIndex, target, clear, createVisualDragLayer, moveVisualDragLayer, clearVisualDragLayer, createDestinationDock, destinationAt, clearDestinationDock, settleVisualDragLayer, autoScroll, beginAutoScroll, stopAutoScroll, createLaneDisclosure, frame});
})();
