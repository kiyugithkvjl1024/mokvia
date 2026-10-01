"use strict";

(function attachTaskPanelEditor(global) {
  function createForm(config) {
    const elements = config.elements;
    const syncAreaPicker = (value = elements.clarifyArea.value) => {
      const collections = config.collections(), project = collections.projects.find((item) => item.id === elements.clarifyProject.value), inherited = project && config.frontmatter(project).area_id || "", allocation = global.AllocationUI;
      if (allocation && elements.clarifyArea.dataset && Object.prototype.hasOwnProperty.call(elements.clarifyArea.dataset, "allocationPicker")) allocation.configureAreaSelect({select: elements.clarifyArea, areas: collections.areas.map((area) => ({id: area.id, title: config.frontmatter(area).title || area.id})), value: project ? inherited : collections.directAreaId, disabled: Boolean(project), onChange: config.setDirectArea});
      const hint = document.getElementById("clarify-area-inherited"); if (hint) hint.textContent = project ? (inherited ? "Project由来のArea: " + (config.frontmatter(collections.areas.find((area) => area.id === inherited)).title || inherited) : "ProjectにAreaは未設定です。") : "";
    };
    const readDraft = (normalize = true) => {
      const detail = config.detail(), dependencies = config.dependencies();
      const draft = {title: elements.clarifyTitle.value, body: elements.clarifyBody.value, status: elements.clarifyStatus.value, project_id: elements.clarifyProject.value, area_id: elements.clarifyProject.value ? "" : elements.clarifyArea.value, contexts: elements.clarifyContexts.value, estimated_minutes: elements.clarifyMinutes.value, due: elements.clarifyDue.value, action_date: elements.clarifyActionDate.value, available_date: elements.clarifyAvailableFrom.value, available_time: elements.clarifyAvailableTime.value, waiting_for: elements.clarifyWaiting.value, scheduled_start: elements.clarifyStart.value, scheduled_end: elements.clarifyEnd.value};
      if (dependencies.length || Object.prototype.hasOwnProperty.call(config.frontmatter(detail), "depends_on")) draft.depends_on = config.serializeDependencies(dependencies);
      return normalize ? config.normalizeDraft(draft, config.scheduledTimestamp) : draft;
    };
    const captureDraft = () => { const draft = readDraft(false); draft.available_from = config.availableFromValue(draft.available_date, draft.available_time); delete draft.available_date; delete draft.available_time; return draft; };
    const mountSettings = (detail) => { const mounted = config.mountForm({root: elements.clarifyForm, initial: detail, writeDraft() {}, readDraft: () => readDraft(true)}); mounted.session.update(readDraft(false)); mounted.view.sync(); return mounted; };
    const restoreDraft = (detail, projects, areas, tasks, draft, resolution) => {
      config.setDetail(detail); elements.clarifyTitle.value = draft.title; elements.clarifyBody.value = draft.body; elements.clarifyStatus.value = draft.status;
      const projectExists = !draft.project_id || projects.some((project) => project.id === draft.project_id);
      config.replaceOptions(elements.clarifyProject, projects, projectExists ? draft.project_id : "", "リンクなし"); config.syncProjectLink();
      config.replaceOptions(elements.clarifyArea, areas, draft.area_id, "リンクなし"); config.setCollections(projects, areas, draft.project_id ? "" : (draft.area_id || "")); syncAreaPicker();
      elements.clarifyContexts.value = draft.contexts; elements.clarifyMinutes.value = draft.estimated_minutes; config.restoreDue(elements.clarifyDue, draft.due); elements.clarifyActionDate.value = draft.action_date; config.restoreAvailable(draft.available_from);
      elements.clarifyWaiting.value = draft.waiting_for; config.restoreScheduled(elements.clarifyStart, draft.scheduled_start); config.restoreScheduled(elements.clarifyEnd, draft.scheduled_end);
      config.setDependencies(detail, tasks, config.dependencyIds(draft.depends_on)); config.setSettings(mountSettings(detail)); config.syncAdvanced();
      elements.clarifyState.textContent = "Taskを編集中です。"; resolution.linkRemoved = !projectExists;
    };
    const populate = (detail, projects, areas = [], tasks = []) => {
      config.setDetail(detail); const fields = config.frontmatter(detail); elements.clarifyTitle.value = fields.title || ""; elements.clarifyBody.value = detail.body || ""; elements.clarifyStatus.value = config.statuses.includes(fields.status) ? fields.status : "inbox";
      config.replaceOptions(elements.clarifyProject, projects, fields.project_id, "リンクなし"); config.syncProjectLink(); config.replaceOptions(elements.clarifyArea, areas, fields.area_id, "リンクなし"); config.setCollections(projects, areas, fields.project_id ? "" : (fields.area_id || "")); syncAreaPicker();
      elements.clarifyContexts.value = config.contextsForInput(fields.contexts); elements.clarifyMinutes.value = fields.estimated_minutes || ""; config.restoreDue(elements.clarifyDue, fields.due); elements.clarifyActionDate.value = fields.action_date || ""; config.restoreAvailable(fields.available_from);
      elements.clarifyWaiting.value = fields.waiting_for || ""; config.restoreScheduled(elements.clarifyStart, fields.scheduled_start); config.restoreScheduled(elements.clarifyEnd, fields.scheduled_end); config.setDependencies(detail, tasks); config.setSettings(mountSettings(detail)); config.syncAdvanced(); elements.clarifyState.textContent = "Taskを編集中です。";
    };
    return Object.freeze({readDraft, captureDraft, mountSettings, restoreDraft, populate, syncAreaPicker});
  }

  function create(config) {
    const elements = config.elements;
    let active = null;

    function failure(state, message) {
      if (active !== state) return;
      state.status.textContent = message;
      state.status.classList.add("field-error");
    }

    function captureView() {
      const groups = elements.clarifyForm.querySelectorAll ? [...elements.clarifyForm.querySelectorAll("[data-task-settings-group]")] : [];
      return {
        model: config.captureModel(),
        groups: groups.map((group) => ({group, open: group.open})),
        stateText: elements.clarifyState.textContent,
        archiveHidden: elements.taskArchive.hidden,
        projectLinkHidden: elements.clarifyProjectLink ? elements.clarifyProjectLink.hidden : true,
        bodyMode: {
          textareaHidden: elements.clarifyBody.hidden,
          previewHidden: elements.clarifyBodyPreviewRegion && elements.clarifyBodyPreviewRegion.hidden,
          editPressed: elements.clarifyBodyEdit && elements.clarifyBodyEdit.getAttribute("aria-pressed"),
          previewPressed: elements.clarifyBodyPreview && elements.clarifyBodyPreview.getAttribute("aria-pressed"),
        },
      };
    }

    function restoreView(previous) {
      config.restoreModel(previous.model);
      for (const item of previous.groups) item.group.open = item.open;
      elements.clarifyState.textContent = previous.stateText;
      elements.taskArchive.hidden = previous.archiveHidden;
      if (elements.clarifyProjectLink) elements.clarifyProjectLink.hidden = previous.projectLinkHidden;
      elements.clarifyBody.hidden = previous.bodyMode.textareaHidden;
      if (elements.clarifyBodyPreviewRegion) {
        elements.clarifyBodyPreviewRegion.hidden = previous.bodyMode.previewHidden;
        if (!previous.bodyMode.previewHidden && global.MarkdownPreview) global.MarkdownPreview.render(elements.clarifyBodyPreviewRegion, previous.model.draft && previous.model.draft.body);
      }
      if (elements.clarifyBodyEdit && previous.bodyMode.editPressed !== null) elements.clarifyBodyEdit.setAttribute("aria-pressed", previous.bodyMode.editPressed);
      if (elements.clarifyBodyPreview && previous.bodyMode.previewPressed !== null) elements.clarifyBodyPreview.setAttribute("aria-pressed", previous.bodyMode.previewPressed);
    }

    function restoreMount(state) {
      if (active === state) active = null;
      state.cancelButton.remove();
      state.form.classList.remove("task-panel-editor");
      if (state.placeholder.parentNode) state.placeholder.parentNode.insertBefore(state.form, state.placeholder);
      state.placeholder.remove();
      restoreView(state.previous);
    }

    function mount({content, detail, source, session, cancel}) {
      if (active || !detail || detail.kind !== "tasks" || detail.archived === true || !source || !Array.isArray(source.entities)) return null;
      const form = elements.clarifyForm, home = form.parentNode, actions = elements.taskArchive.parentNode;
      if (!home || !actions) throw new Error("Task編集フォームを開けません。");
      const previous = captureView(), placeholder = document.createComment("clarify-form-home"), status = document.createElement("p"), cancelButton = document.createElement("button");
      home.insertBefore(placeholder, form);
      status.className = "muted task-panel-edit-status";
      status.setAttribute("role", "status");
      status.setAttribute("aria-live", "polite");
      status.textContent = "このパネル内で編集できます。";
      cancelButton.type = "button";
      cancelButton.className = "secondary";
      cancelButton.textContent = "キャンセル";
      const state = {detailId: detail.id, detail, session, placeholder, previous, status, cancelButton, form, phase: "editing", invalidated: false};
      cancelButton.addEventListener("click", () => {
        if (active !== state) return;
        cancel();
      });
      actions.insertBefore(cancelButton, elements.taskArchive);
      active = state;
      try {
        config.populate(detail, source);
        form.classList.add("task-panel-editor");
        elements.taskArchive.hidden = true;
        if (elements.clarifyProjectLink) elements.clarifyProjectLink.hidden = true;
        if (global.MarkdownPreview) global.MarkdownPreview.reset();
        content.replaceChildren(status, form);
        elements.clarifyTitle.focus();
      } catch (error) { restoreMount(state); throw error; }
      const cleanup = () => { if (active === state) restoreMount(state); };
      cleanup.canClose = () => {
        if (active !== state) return true;
        if (["applying", "readback"].includes(state.phase)) { failure(state, "保存処理中です。完了するまでお待ちください。"); return false; }
        if (state.phase === "preview") state.invalidated = true;
        return true;
      };
      return cleanup;
    }

    function current(detailId) { return active && active.detailId === detailId ? active : null; }
    function mutationOptions(state, operation) {
      const isCurrent = () => active === state && !state.invalidated;
      const setPhase = (phase, message = "") => { if (!isCurrent()) return false; state.phase = phase; if (message) { state.status.textContent = message; state.status.classList.remove("field-error"); } return true; };
      return {
        isCurrent,
        onPreviewStart: () => setPhase("preview", "保存内容を確認しています。"),
        onApplyStart: () => setPhase("applying", "保存しています。"),
        onReadbackStart: () => setPhase("readback", "保存結果を確認しています。"),
        localReload: async () => {
          if (!isCurrent()) throw new Error("stale_task_panel_session");
          const fresh = await config.request(config.detailPath(operation.id));
          if (!isCurrent()) throw new Error("stale_task_panel_session");
          const snapshot = await config.request("/api/v1/snapshot"), snapshotTask = snapshot && Array.isArray(snapshot.entities) ? snapshot.entities.find((entity) => entity.id === operation.id) : null;
          if (!isCurrent() || !fresh || fresh.id !== operation.id || fresh.kind !== "tasks" || fresh.archived === true || !snapshotTask || snapshotTask.kind !== "tasks" || snapshotTask.archived === true || snapshotTask.content_hash !== fresh.content_hash) throw new Error("task_panel_readback_failed");
          if (!config.accept(fresh, snapshot, state.session)) throw new Error("task_panel_readback_failed");
          state.phase = "closing";
          if (!config.close(state.session)) { state.phase = "readback"; throw new Error("task_panel_readback_failed"); }
        },
        onFailure: (error) => { if (isCurrent()) state.phase = "editing"; const code = error && error.payload && error.payload.error && error.payload.error.code; failure(state, ["conflict", "stale_preview"].includes(code) ? "別の更新があるため保存していません。入力は保持しています。最新状態を確認してください。" : "保存できませんでした。入力内容は保持しています。詳細は画面上部を確認してください。"); },
        onReadbackFailure: () => { if (active === state) state.phase = "terminal"; failure(state, "保存は受理されましたが、最新状態を確認できません。再送せず、再読み込みしてください。"); },
        onUnknown: () => { if (active === state) state.phase = "terminal"; failure(state, "保存結果を確認できません。再送せず、現在の画面を再読み込みして確認してください。"); },
        onCommittedCleanup: () => { if (active === state) state.phase = "terminal"; failure(state, "変更は適用済みですが、復旧確認が必要です。同じ操作は再送しないでください。"); },
      };
    }

    return Object.freeze({mount, current, failure, mutationOptions});
  }

  global.TaskPanelEditor = Object.freeze({create, createForm});
})(window);
