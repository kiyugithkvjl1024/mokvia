"use strict";

(function installUnsavedNavigationGuard() {
  const FORM_SELECTOR = ".form-panel, #capture-form, #work-session-edit-form, #inbox-stage-sheet, #project-task-plan-form";
  const baselineByForm = new WeakMap();
  let discardNavigation = false;

  function guardedForm(target) {
    const form = target && typeof target.closest === "function" ? target.closest("form") : null;
    return form && typeof form.matches === "function" && form.matches(FORM_SELECTOR) ? form : null;
  }

  function isActive(form) {
    return !(form && typeof form.closest === "function" && form.closest("[hidden], dialog:not([open])"));
  }

  function formValue(form) {
    return JSON.stringify(Array.from(form.elements || []).flatMap((control, index) => {
      if (!control || control.disabled || ["button", "fieldset", "reset", "submit"].includes(control.type)) return [];
      const key = control.name || control.id || String(index);
      if (control.multiple && control.options) return [[key, Array.from(control.options).filter((option) => option.selected).map((option) => option.value)]];
      if (control.type === "checkbox" || control.type === "radio") return [[key, Boolean(control.checked)]];
      return [[key, control.value]];
    }));
  }

  function hasUnsavedChanges() {
    return Array.from(document.querySelectorAll(FORM_SELECTOR)).some((form) => {
      const baseline = baselineByForm.get(form);
      return baseline !== undefined && isActive(form) && baseline !== formValue(form);
    });
  }

  function confirmDiscard() {
    return !hasUnsavedChanges() || window.confirm("未保存の変更を破棄して移動しますか？");
  }

  function resetBaseline(target) {
    const form = guardedForm(target || document.activeElement);
    if (form) baselineByForm.set(form, formValue(form));
  }

  window.U = {c: confirmDiscard, r: resetBaseline, p: () => resetBaseline(document.getElementById("project-form"))};

  document.addEventListener("focusin", (event) => {
    const form = guardedForm(event.target);
    if (form && !baselineByForm.has(form)) baselineByForm.set(form, formValue(form));
  });
  document.addEventListener("input", (event) => { guardedForm(event.target); });
  document.addEventListener("change", (event) => { guardedForm(event.target); });
  document.addEventListener("reset", (event) => {
    const form = guardedForm(event.target);
    if (form) baselineByForm.delete(form);
  });
  document.addEventListener("click", (event) => {
    const link = event.target && typeof event.target.closest === "function" ? event.target.closest("a[href]") : null;
    if (!link || event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.target || link.hasAttribute("download")) return;
    const destination = new URL(link.href, location.href);
    if (destination.origin !== location.origin || (destination.pathname === location.pathname && destination.search === location.search) || !hasUnsavedChanges()) return;
    if (!confirmDiscard()) { event.preventDefault(); return; }
    discardNavigation = true;
    setTimeout(() => { discardNavigation = false; }, 0);
  });
  window.addEventListener("beforeunload", (event) => {
    if (discardNavigation || !hasUnsavedChanges()) return;
    event.preventDefault();
    event.returnValue = "";
  });
})();
