"use strict";

/* Cycle management card presentation; mutations stay in the app's preview flow. */
(function attachRoadmapCycleUI(global) {
  function createOutcomeCard(outcome, lane, position, laneLengths, actions) {
    const {title, actionButton, edit, move} = actions;
    const moveTrigger = actionButton("移動", () => { editor.hidden = false; moveTrigger.setAttribute("aria-expanded", "true"); targetLane.focus(); });
    moveTrigger.className += " roadmap-move-trigger";
    moveTrigger.setAttribute("aria-expanded", "false");
    const card = global.TaskCard.createTaskCard({task: outcome, metadata: [(lane === "next" ? "Next " : "Later ") + position], actions: [moveTrigger, actionButton("編集", edit)]});
    card.className += " roadmap-outcome-card";

    const editor = document.createElement("div"), targetLane = document.createElement("select"), targetPosition = document.createElement("select");
    editor.className = "roadmap-move-editor";
    editor.hidden = true;
    const field = (name, control) => { const label = document.createElement("label"); label.textContent = name; label.append(control); editor.append(label); };
    for (const [value, text] of [["next", "Next"], ["later", "Later"]]) { const option = document.createElement("option"); option.value = value; option.textContent = text; targetLane.append(option); }
    targetLane.value = lane;
    targetLane.setAttribute("aria-label", title + "の移動先");
    const positions = () => { const selected = targetLane.value; targetPosition.replaceChildren(); const count = laneLengths[selected] + (selected === lane ? 0 : 1); for (let index = 1; index <= count; index += 1) { const option = document.createElement("option"); option.value = String(index); option.textContent = String(index); targetPosition.append(option); } targetPosition.value = String(selected === lane ? position : count); };
    positions();
    targetLane.addEventListener("change", positions);
    field("移動先", targetLane);
    field("順番", targetPosition);
    const close = () => { targetLane.value = lane; positions(); editor.hidden = true; moveTrigger.setAttribute("aria-expanded", "false"); moveTrigger.focus(); };
    const save = actionButton("保存", () => { if (targetLane.value === lane && Number(targetPosition.value) === position) { close(); return; } return move(targetLane.value, Number(targetPosition.value), moveTrigger); }, true);
    const cancel = actionButton("キャンセル", close), buttons = document.createElement("div");
    buttons.className = "card-actions";
    buttons.append(save, cancel);
    editor.append(buttons);
    card.append(editor);
    return card;
  }
  global.RoadmapCycleUI = {createOutcomeCard};
})(window);
