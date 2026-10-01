"use strict";

window.QuickStartSuggestions = (() => {
  const DEBOUNCE_MS = 150;
  const REASONS = Object.freeze({
    weekday_time: "この曜日・時間帯によく開始",
    weekday: "この曜日によく開始",
    time_band: "この時間帯によく開始",
    frequent: "よく開始",
    query_match: "入力履歴に一致",
  });

  function create({input, list, submit, fetchSuggestions = fetch, onSelect}) {
    let composing = false;
    let timer = null;
    let timerOwner = null;
    let generation = 0;
    let suggestions = [];
    let activeIndex = -1;
    const ownerDocument = () => input.ownerDocument || document;
    const timerWindow = () => ownerDocument().defaultView || window;

    function close() { generation += 1; suggestions = []; activeIndex = -1; list.replaceChildren(); list.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); }
    function select(index) {
      const suggestion = suggestions[index];
      if (!suggestion) return;
      input.value = suggestion.title;
      close();
      onSelect(suggestion);
      submit.focus();
    }
    function render() {
      list.replaceChildren();
      activeIndex = -1;
      if (!suggestions.length) { list.hidden = true; input.setAttribute("aria-expanded", "false"); input.removeAttribute("aria-activedescendant"); return; }
      suggestions.forEach((suggestion, index) => {
        const option = ownerDocument().createElement("button");
        option.type = "button";
        option.className = "quick-start-suggestion";
        option.id = "quick-start-suggestion-" + index;
        const title = ownerDocument().createElement("span");
        title.className = "quick-start-suggestion-title";
        title.textContent = suggestion.title;
        option.append(title);
        const reason = REASONS[suggestion.reason_code];
        if (reason) { const label = ownerDocument().createElement("span"); label.className = "quick-start-suggestion-reason"; label.textContent = reason; option.append(label); }
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", "false");
        option.addEventListener("click", () => select(index));
        list.append(option);
      });
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }
    async function request(query, requestGeneration) {
      try {
        const response = await fetchSuggestions("/api/v1/quick-start-suggestions?q=" + encodeURIComponent(query), {method: "GET"});
        const payload = response && response.ok ? await response.json() : null;
        if (requestGeneration !== generation) return;
        suggestions = Array.isArray(payload && payload.suggestions) ? payload.suggestions.slice(0, 5).filter((item) => item && typeof item.title === "string" && (item.area_id === null || typeof item.area_id === "string") && typeof item.reason_code === "string") : [];
        render();
      } catch (_error) { if (requestGeneration === generation) close(); }
    }
    function clearTimer() { if (timer !== null && timerOwner) timerOwner.clearTimeout(timer); timer = null; timerOwner = null; }
    function schedule() {
      clearTimer();
      const query = input.value.trim();
      generation += 1;
      const requestGeneration = generation;
      timerOwner = timerWindow(); timer = timerOwner.setTimeout(() => request(query, requestGeneration), DEBOUNCE_MS);
    }
    function onInput() { if (!composing) schedule(); }
    function onPointerdown() { if (!composing) schedule(); }
    function onKeydown(event) {
      if (event.key === "Escape") { close(); return; }
      if (!suggestions.length) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        activeIndex = (activeIndex + (event.key === "ArrowDown" ? 1 : suggestions.length - 1)) % suggestions.length;
        const options = Array.from(list.children);
        options.forEach((option, index) => option.setAttribute("aria-selected", String(index === activeIndex)));
        input.setAttribute("aria-activedescendant", options[activeIndex].id);
      } else if (event.key === "Enter" && activeIndex >= 0) { event.preventDefault(); select(activeIndex); }
    }
    function onDocumentPointerdown(event) { if (!input.contains(event.target) && !list.contains(event.target)) close(); }

    let boundDocument = null;
    function rebindDocument() {
      const nextDocument = ownerDocument();
      if (boundDocument === nextDocument) return;
      clearTimer();
      if (boundDocument) boundDocument.removeEventListener("pointerdown", onDocumentPointerdown);
      boundDocument = nextDocument;
      boundDocument.addEventListener("pointerdown", onDocumentPointerdown);
    }
    input.addEventListener("focus", schedule);
    input.addEventListener("pointerdown", onPointerdown);
    input.addEventListener("input", onInput);
    input.addEventListener("compositionstart", () => { composing = true; });
    input.addEventListener("compositionend", () => { composing = false; schedule(); });
    input.addEventListener("keydown", onKeydown);
    rebindDocument();
    return {rebindDocument, destroy() { clearTimer(); if (boundDocument) boundDocument.removeEventListener("pointerdown", onDocumentPointerdown); close(); }};
  }

  return Object.freeze({create});
})();
