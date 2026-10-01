"use strict";

const API_HEADERS = Object.freeze({"X-GTD-Web": "1"});
const MUTATION_HEADERS = Object.freeze({"X-GTD-Web": "1", "Content-Type": "application/json"});
const TASK_STATUSES = Object.freeze(["inbox", "planned", "next", "doing", "waiting", "scheduled", "someday", "done"]);
const TASK_FILTER_KEYS = Object.freeze(["status", "context", "project_id", "area_id", "max_minutes", "due_before"]);
const UNKNOWN_APPLY_MESSAGE = "保存結果を確認できません。再送せず、現在の画面を再読み込みして確認してください。";
const DIRECTION_REVIEW_QUERY = "mode=review";
const DAILY_REVIEW_BODY = "\n## Checklist\n\n- [ ] Inboxを確認した\n- [ ] Doingと今日から7日内のScheduled・dueを確認した\n- [ ] 今日の約束を整えた\n\n## Notes\n\n気づきと、確認が必要な変更候補を書く。\n";
const WEEKLY_REVIEW_BODY = "\n## Checklist\n\n- [ ] Clear: Inboxを空に近づけた\n- [ ] Current: Next、Doing、Waiting、Scheduled、Somedayを確認した\n- [ ] Creative: 各Active ProjectのNext Actionを確認した\n- [ ] Goal整合: Goalとの整合を確認した\n\n## Notes\n\n気づきと、ユーザー確認が必要な変更候補を書く。\n";
const REVIEW_STEPS = Object.freeze({
  daily: Object.freeze([
    {id: "inbox", label: "Inboxを確認した", purpose: "気がかりの見落としを防ぎ、判断が必要なものを未整理のまま残さないため。", action: "Inboxを上から確認し、意味が不明なもの、次の行動が未定のもの、今日扱う必要があるものを特定します。", href: "/inbox"},
    {id: "near_term", label: "Doingと今日から7日内のScheduled・dueを確認した", mobileLabel: "Doing・7日内を確認", purpose: "進行中の作業と直近の予定・期限の衝突や見落としを、今日の行動を決める前に見つけるため。", action: "Doingと今日から7日後までのScheduled・dueを確認し、今日対応するもの、延期・再調整するものを洗い出します。", href: "/tasks"},
    {id: "commitments", label: "今日の約束を整えた", purpose: "希望ではなく、今日実際に引き受ける量を明確にし、過剰な約束を防ぐため。", action: "候補を一つずつ「今日やる／今日はやらない」に分け、今日やるものだけをNotesへ記録します。", href: "/tasks"},
  ]),
  weekly: Object.freeze([
    {id: "clear", label: "Clear: Inboxを空に近づけた", description: "気がかりを集め、Inboxを確認します。", href: "/inbox"},
    {id: "current", label: "Current: Next、Doing、Waiting、Scheduled、Somedayを確認した", description: "現在の約束と次の行動を見直します。", href: "/tasks"},
    {id: "creative", label: "Creative: 各Active ProjectのNext Actionを確認した", description: "Projectの次の物理的行動を確認します。", href: "/projects?mode=review"},
    {id: "goal_alignment", label: "Goal整合: Goalとの整合を確認した", description: "今週の行動と方向性を照合します。", href: "/projects?mode=review"},
  ]),
});
const REVIEW_DRAFT_PREFIX = "gtd.reviewDraft.v1:";
const taskSettings = window.TaskSettings;
const availableFromParts = taskSettings.availableFromParts;
const availableFromValue = taskSettings.availableFromValue;
const {formatAvailableFrom,availableFromIsFuture,contextsForInput,contextsForField,contextsForDisplay,dependencyIds,serializeDependencyIds,dependencyLabel,taskDependencyProgress,taskStartBlockReason}=taskSettings;

function byId(id) { return document.getElementById(id); }
const elements = Object.freeze({
  captureDialog: byId("capture-dialog"), captureForm: byId("capture-form"), captureCancel: byId("cancel-capture"),
  captureOpenDesktop: byId("open-capture-desktop"), captureOpenMobile: byId("open-capture-mobile"), captureOpenInbox: byId("open-capture-inbox"), captureOpenEmpty: byId("open-capture-empty"),
  captureTitle: byId("capture-title"), captureBody: byId("capture-body"), capturePreview: byId("preview-capture"),
  inboxWorkflow: byId("inbox-workflow"), inboxReload: byId("reload-inbox"), inboxList: byId("inbox-list"), inboxState: byId("inbox-state"),
  inboxHeadingCount: byId("inbox-heading-count"), inboxCountDesktop: byId("inbox-count-desktop"), inboxCountMobile: byId("inbox-count-mobile"),
  inboxQueue: byId("inbox-queue"), inboxLanes: byId("inbox-lanes"), inboxStageSheet: byId("inbox-stage-sheet"), inboxStageTitle: byId("inbox-stage-title"), inboxStageTitleSummary: byId("inbox-stage-title-summary"), inboxStageEditTitle: byId("inbox-stage-edit-title"), inboxStageProject: byId("inbox-stage-project"), inboxStageArea: byId("inbox-stage-area"), inboxStageAreaField: byId("inbox-stage-area-field"), inboxStageContexts: byId("inbox-stage-contexts"), inboxStageMinutes: byId("inbox-stage-estimated-minutes"), inboxStageDue: byId("inbox-stage-due"), inboxStageActionDate: byId("inbox-stage-action-date"), inboxStageAvailableFrom: byId("inbox-stage-available-from"), inboxStageAvailableTime: byId("inbox-stage-available-time"), inboxStageWaiting: byId("inbox-stage-waiting"), inboxStageDependencies: byId("inbox-stage-depends-on"), inboxStageStart: byId("inbox-stage-start"), inboxStageEnd: byId("inbox-stage-end"), inboxStageBody: byId("inbox-stage-body"), inboxStageStatusButtons: byId("inbox-stage-status-buttons"), inboxStageDestinations: byId("inbox-stage-destinations"), inboxStageDestinationReadonly: byId("inbox-stage-destination-readonly"), inboxStageDetails: byId("inbox-stage-details"), inboxStageCancel: byId("inbox-stage-cancel"), inboxStageConfirm: byId("inbox-stage-confirm"),
  clarifyWorkflow: byId("clarify-workflow"), clarifyForm: byId("clarify-form"), clarifyState: byId("clarify-state"),
  clarifyReload: byId("reload-clarify"), clarifyTitle: byId("clarify-title"), clarifyBody: byId("clarify-body"), clarifyBodyEdit: byId("clarify-body-edit"), clarifyBodyPreview: byId("clarify-body-preview"), clarifyBodyPreviewRegion: byId("clarify-body-preview-region"),
  clarifyStatus: byId("clarify-status"), clarifyProject: byId("clarify-project-id"), clarifyProjectLink: byId("clarify-project-detail-link"), clarifyArea: byId("clarify-area-id"), clarifyContexts: byId("clarify-contexts"),
  clarifyMinutes: byId("clarify-estimated-minutes"), clarifyDue: byId("clarify-due"), clarifyActionDate: byId("clarify-action-date"), clarifyAvailableFrom: byId("clarify-available-from"), clarifyAvailableTime: byId("clarify-available-time"), clarifyWaiting: byId("clarify-waiting-for"),
  clarifyStart: byId("clarify-scheduled-start"), clarifyEnd: byId("clarify-scheduled-end"),
  clarifyAdvanced: byId("clarify-advanced"), clarifyWaitingField: byId("clarify-waiting-field"), clarifyScheduledFields: byId("clarify-scheduled-fields"),
  clarifyDependencySearch: byId("clarify-dependency-search"), clarifyDependencyOptions: byId("clarify-dependency-options"), clarifyDependencyChips: byId("clarify-dependency-chips"), clarifyContinuations: byId("clarify-continuations"),
  clarifyPreview: byId("preview-clarify"), taskArchive: byId("archive-task"),
  tasksWorkflow: byId("task-list-workflow"), tasksForm: byId("task-filter-form"), tasksReload: byId("reload-tasks"),
  tasksContext: byId("task-filter-context"), tasksProject: byId("task-filter-project"), tasksArea: byId("task-filter-area"), tasksMinutes: byId("task-filter-minutes"),
  tasksDue: byId("task-filter-due"), tasksClear: byId("clear-task-filters"),
  tasksChips: byId("task-filter-chips"), focusDate: byId("focus-date"), completedHeading: byId("completed-heading"), completedCount: byId("completed-count"), completedPeriodToggle: byId("completed-period-toggle"), focusPipOpen: byId("open-focus-pip"),
  focusFilterSummary: byId("focus-filter-summary"),
  focusStatusCandidates: byId("focus-status-candidates"), focusStatusNext: byId("focus-status-next"), focusStatusScheduled: byId("focus-status-scheduled"), focusStatusWaiting: byId("focus-status-waiting"), focusStatusSomeday: byId("focus-status-someday"), tasksIncompleteGroup: byId("task-incomplete-group"), tasksIncompleteHeading: byId("task-incomplete-heading"), tasksIncompleteCount: byId("task-incomplete-count"),
  tasksState: byId("task-list-state"), tasksCurrentGroup: byId("task-current-group"), tasksCurrentList: byId("task-current-list"), focusSecondaryActions: byId("focus-secondary-actions"), tasksIncompleteList: byId("task-incomplete-list"),
  focusCycleContext: byId("focus-cycle-context"), focusActionBoard: byId("focus-action-board"), focusSectionToday: byId("focus-section-today"), focusSectionDated: byId("focus-section-dated"), focusSectionUndated: byId("focus-section-undated"), focusSectionTodayList: byId("focus-section-today-list"), focusSectionDatedList: byId("focus-section-dated-list"), focusSectionUndatedList: byId("focus-section-undated-list"), focusArchiveDropTarget: byId("focus-archive-drop-target"),
  focusMoveDialog: byId("focus-move-dialog"), focusMoveToday: byId("focus-move-today"), focusMoveDated: byId("focus-move-dated"), focusMoveUndated: byId("focus-move-undated"), focusMoveDateField: byId("focus-move-date-field"), focusMoveDate: byId("focus-move-date"), focusMoveSave: byId("focus-move-save"), focusMoveCancel: byId("focus-move-cancel"),
  focusDueDialog: byId("focus-due-dialog"), focusDueDate: byId("focus-due-date"), focusDueSave: byId("focus-due-save"), focusDueClear: byId("focus-due-clear"), focusDueCancel: byId("focus-due-cancel"),
  tasksCompletedDetails: byId("task-completed-details"), tasksCompletedList: byId("task-completed-list"),
  quickStartForm: byId("quick-start-form"), quickStartTitle: byId("quick-start-title"), quickStartSuggestions: byId("quick-start-suggestions"), quickStartSubmit: byId("quick-start-submit"),
  focusTodayAddForm: byId("focus-today-add-form"), focusTodayAddTitle: byId("focus-today-add-title"),
  breakStart: byId("break-start"), notificationPause: byId("focus-notification-pause"), notificationPauseDialog: byId("notification-pause-dialog"), notificationPauseDeadline: byId("notification-pause-deadline"), notificationPauseMinutes: byId("notification-pause-minutes"), notificationPausePreset15: byId("notification-pause-preset-15"), notificationPausePreset30: byId("notification-pause-preset-30"), notificationPausePreset60: byId("notification-pause-preset-60"), notificationPauseSave: byId("notification-pause-save"), notificationPauseResume: byId("notification-pause-resume"), notificationPauseClose: byId("notification-pause-close"),
  workSessionDialog: byId("work-session-edit-dialog"), workSessionForm: byId("work-session-edit-form"), workSessionTitle: byId("work-session-edit-title"), workSessionStart: byId("work-session-edit-start"), workSessionEnd: byId("work-session-edit-end"), workSessionStartError: byId("work-session-edit-start-error"), workSessionEndError: byId("work-session-edit-end-error"), workSessionCancel: byId("work-session-edit-cancel"), workSessionPreview: byId("work-session-edit-preview"),
  projectsWorkflow: byId("projects-workflow"), projectForm: byId("project-form"), projectsReload: byId("reload-projects"),
  projectTitle: byId("project-title"), projectStatus: byId("project-status"), projectGoal: byId("project-goal-id"), projectOutcome: byId("project-roadmap-outcome-id"), projectArea: byId("project-area-id"),
  projectPeriodStart: byId("project-planned-start-date"), projectPeriodEnd: byId("project-planned-end-date"), projectBody: byId("project-body"), projectCancel: byId("cancel-project-edit"), projectArchive: byId("archive-project"),
  projectPreview: byId("preview-project"), projectsState: byId("projects-state"), projectsList: byId("projects-list"),
  projectFacts: byId("project-facts-list"), projectFactsCount: byId("project-facts-count"),
  projectFilters: byId("project-filters"), projectFilterQuery: byId("project-filter-q"), projectFilterArea: byId("project-filter-area"), projectFilterGoal: byId("project-filter-goal"), projectFilterOutcome: byId("project-filter-outcome"), projectFilterInactive: byId("project-filter-inactive"), projectKanban: byId("project-kanban"), projectListing: byId("project-listing"), projectDetailPanel: byId("project-detail"), projectDetailHeading: byId("project-detail-heading"), projectDetailBack: byId("project-detail-back"), projectDetailEdit: byId("project-detail-edit"), projectDetailPeriod: byId("project-detail-period"), projectDetailState: byId("project-status-live"), projectDetailStatus: byId("project-detail-status"), projectSupportEditor: byId("project-support-editor"), projectSupportForm: byId("project-support-form"), projectSupportBody: byId("project-support-body"), projectPlanTree: byId("project-plan-tree"), projectPlanDoneToggle: byId("project-plan-done-toggle"), projectPlanOpen: byId("project-plan-open"), projectPlanClose: byId("project-plan-close"), projectPlanRows: byId("project-plan-rows"), projectPlanAddRoot: byId("project-plan-add-root"), projectOtherTaskGroups: byId("project-other-task-groups"), projectOtherDoneToggle: byId("project-other-done-toggle"), projectOtherTaskForm: byId("project-other-task-form"), projectOtherTaskTitle: byId("project-other-task-title"), projectOtherTaskStatus: byId("project-other-task-status"), projectTaskPlanForm: byId("project-task-plan-form"), projectTaskPlanPreview: byId("project-task-plan-preview"),
  projectsView: byId("projects-view"), goalsView: byId("goals-view"), projectsTab: byId("projects-tab"), goalsTab: byId("goals-tab"), purposeTab: byId("purpose-tab"), visionsTab: byId("visions-tab"), areasTab: byId("areas-tab"), directionReviewTab: byId("direction-review-tab"),
  projectDisclosure: byId("project-form-disclosure"), goalDisclosure: byId("goal-form-disclosure"),
  goalForm: byId("goal-form"), goalsReload: byId("reload-goals"),
  goalTitle: byId("goal-title"), goalStatus: byId("goal-status"), goalVision: byId("goal-vision-id"), goalTargetDate: byId("goal-target-date"), goalBody: byId("goal-body"),
  goalCancel: byId("cancel-goal-edit"), goalArchive: byId("archive-goal"), goalPreview: byId("preview-goal"),
  goalsState: byId("goals-state"), goalsList: byId("goals-list"),
  directionOverview: byId("direction-overview"), directionCards: byId("direction-cards"), directionState: byId("direction-state"), directionReview: byId("direction-review"), valueLanternDialog: byId("value-lantern-dialog"), valueLanternOpen: byId("value-lantern-open"), valueLanternOpenCopy: document.querySelector(".value-lantern-open-copy"), valueLanternClose: byId("value-lantern-close"),
  directionRoadmapSummary: byId("direction-roadmap-summary"), directionRoadmapList: byId("direction-roadmap-list"),
  purposeView: byId("purpose-view"), purposeForm: byId("purpose-form"), purposeTitle: byId("purpose-title"), purposeBody: byId("purpose-body"), purposesList: byId("purposes-list"), purposeState: byId("purpose-state"), purposePreview: byId("preview-purpose"), purposeCancel: byId("cancel-purpose-edit"), purposeArchive: byId("archive-purpose"), purposeDisclosure: byId("purpose-form-disclosure"),
  visionsView: byId("visions-view"), visionForm: byId("vision-form"), visionTitle: byId("vision-title"), visionStatus: byId("vision-status"), visionBody: byId("vision-body"), visionsList: byId("visions-list"), visionsState: byId("visions-state"), visionPreview: byId("preview-vision"), visionCancel: byId("cancel-vision-edit"), visionArchive: byId("archive-vision"), visionDisclosure: byId("vision-form-disclosure"),
  areasView: byId("areas-view"), areaForm: byId("area-form"), areaTitle: byId("area-title"), areaHealth: byId("area-health"), areaBody: byId("area-body"), areasList: byId("areas-list"), areasState: byId("areas-state"), areaPreview: byId("preview-area"), areaCancel: byId("cancel-area-edit"), areaArchive: byId("archive-area"), areaDisclosure: byId("area-form-disclosure"), areaDetail: byId("area-detail"), areaDetailContent: byId("area-detail-content"), areaDetailClose: byId("close-area-detail"),
  reviewWorkflow: byId("review-workflow"), reviewForm: byId("review-form"), reviewsReload: byId("reload-reviews"),
  reviewHeading: byId("review-heading"), reviewTitle: byId("review-title"), reviewPeriod: byId("review-period-start"),
  reviewBody: byId("review-body"), reviewBodyLabel: byId("review-body-label"), reviewCancel: byId("cancel-review-edit"), reviewArchive: byId("archive-review"),
  reviewPreview: byId("preview-review"), reviewsState: byId("reviews-state"), reviewsList: byId("reviews-list"),
  progressForm: byId("progress-form"), progressTitle: byId("progress-title"), progressOccurredOn: byId("progress-occurred-on"), progressOrigin: byId("progress-origin"), progressBenefit: byId("progress-benefit"), progressEvidence: byId("progress-evidence"), pof: byId("progress-optional-fields"), progressCancel: byId("cancel-progress-edit"), progressPreview: byId("preview-progress"), progressState: byId("progress-state"), progressList: byId("progress-list"),
  progressReportWorkflow: byId("progress-report-workflow"), progressReportForm: byId("progress-report-form"), progressReportMonth: byId("progress-report-month"), progressReportMarkdown: byId("progress-report-markdown"), progressReportState: byId("progress-report-state"), progressReportCopy: byId("copy-progress-report"),
  weeklyReviewTab: byId("weekly-review-tab"), dailyReviewTab: byId("daily-review-tab"), reviewFacts: byId("review-facts-list"),
  reviewDisclosure: byId("review-form-disclosure"), reviewGuide: byId("review-guide"), reviewGuidePurpose: byId("review-guide-purpose"), dailyReviewProgress: byId("daily-review-progress"), reviewGuideSteps: byId("review-guide-steps"), reviewHelpLink: byId("review-help-link"), reviewChecklist: byId("review-checklist"), reviewDraftWarning: byId("review-draft-warning"),
  reviewStepControls: [byId("review-step-1"), byId("review-step-2"), byId("review-step-3"), byId("review-step-4")], reviewStepLabels: [byId("review-step-1-label"), byId("review-step-2-label"), byId("review-step-3-label"), byId("review-step-4-label")], reviewStep4Row: byId("review-step-4-row"),
  reviewHelpWorkflow:byId("review-help-workflow"),rhe:["good-example","bad-example","supplement"].map(x=>byId("review-help-"+x)),rht:byId("review-help-title"),rhp:byId("review-help-purpose"),rhi:byId("review-help-image"),rhf:byId("review-help-flow"),rhs:byId("review-help-steps"),rhk:byId("review-help-stuck"),rhd:byId("review-help-done"),rhb:byId("review-help-back"),
  roadmapWorkflow: byId("roadmap-workflow"), roadmapReload: byId("reload-roadmap"), roadmapState: byId("roadmap-state"), roadmapOverviewTab: byId("roadmap-overview-tab"), roadmapEditTab: byId("roadmap-edit-tab"), roadmapBrowseTab: byId("roadmap-browse-tab"), roadmapDetailEditTab: byId("roadmap-detail-edit-tab"), roadmapOverview: byId("roadmap-overview"), roadmapEdit: byId("roadmap-edit"), roadmapDirectionStrip: byId("roadmap-direction-strip"), roadmapAreaFilters: byId("roadmap-area-filters"), roadmapGoalFilters: byId("roadmap-goal-filters"), roadmapScaleYears: byId("roadmap-scale-years"), roadmapEndYear: byId("roadmap-end-year"), roadmapBoard: byId("roadmap-board"), roadmapDetailBackdrop: byId("roadmap-detail-backdrop"), roadmapDetailHandle: byId("roadmap-detail-handle"), roadmapDetailClose: byId("roadmap-detail-close"), roadmapOutcomeDetailPanel: byId("roadmap-outcome-detail"), roadmapOutcomeDetailHeading: byId("roadmap-outcome-detail-heading"), roadmapOutcomeDetailContent: byId("roadmap-outcome-detail-content"), roadmapActiveCycle: byId("roadmap-active-cycle"), cycleProjectTimeline: byId("cycle-project-timeline"), cycleProjectTimelineSection: byId("cycle-project-timeline-section"), roadmapNowList: byId("roadmap-now-list"), roadmapNextList: byId("roadmap-next-list"), roadmapLaterList: byId("roadmap-later-list"), roadmapPlannedList: byId("roadmap-planned-list"), roadmapHistoryList: byId("roadmap-history-list"),
  statusWorkflow: byId("status-workflow"), serviceHealth: byId("service-health"),
  statusFacts: byId("status-facts-list"), retryCurrent: byId("retry-current"),
  calendarSyncState: byId("calendar-sync-state"), calendarSyncMessage: byId("calendar-sync-message"),
  calendarSyncReason: byId("calendar-sync-reason"), calendarSyncCheckedAt: byId("calendar-sync-checked-at"),
  error: byId("error-banner"), notice: byId("notice"), unavailable: byId("unavailable-route"),
  dialog: byId("preview-dialog"), previewTitle: byId("preview-title"), previewContent: byId("preview-content"), cancelPreview: byId("cancel-preview"),
  confirmPreview: byId("confirm-preview"),
  switchDialog: byId("task-switch-dialog"), switchMessage: byId("task-switch-message"),
  switchTime: Object.freeze({field: byId("task-switch-end-field"), input: byId("task-switch-end"), error: byId("task-switch-end-error"), note: byId("task-switch-time-note")}),
  switchInterrupt: byId("switch-interrupt"), switchComplete: byId("switch-complete"), switchCancel: byId("cancel-task-switch"),
});
const cycleFocusContext = window.CycleFocusContext && window.CycleFocusContext.create({elements, safeFrontmatter, dependencyIds, clarifyHref, tokyoToday, projectDetailHref: (id) => "/projects?level=projects&id=" + encodeURIComponent(id)});
const af=elements.clarifyAvailableFrom,at=elements.clarifyAvailableTime;
const archiveDrop=elements.focusArchiveDropTarget;
const projectPlanViewport=byId("project-plan-viewport"); if (typeof window!=="undefined"&&window.ProjectTaskBoard) window.ProjectTaskBoard.setupViewport(elements.projectPlanTree);

const mutationButtons = [elements.capturePreview, elements.clarifyPreview, elements.taskArchive,
  elements.quickStartSubmit, byId("focus-today-add-submit"), elements.breakStart, elements.notificationPause, elements.notificationPauseSave, elements.notificationPauseResume,
  elements.workSessionPreview,
  elements.projectPreview, elements.projectArchive, elements.goalPreview, elements.goalArchive, elements.purposePreview, elements.purposeArchive, elements.visionPreview, elements.visionArchive, elements.areaPreview, elements.areaArchive,
  elements.reviewPreview, elements.reviewArchive, elements.progressPreview,
  byId("allocation-expand-plan"), byId("allocation-expand-submit"), byId("allocation-expand-overwrite-submit")];
const dynamicMutationButtons = [];
let activePreview = null;
let activeMutation = null;
let applyOutcomeUnknown = false;
let canonicalReloadRequired = false;
let mutationRecoveryRequired = false;
let unknownAttempt = null;
let applyInFlight = false;
let mutationPreparationInFlight = false;
const externalMutationGates = new Set();
let taskPreparationGeneration = 0;
let taskReloadInFlight = false;
let pendingTaskSwitch = null;
let currentDoingTask = null;
let currentDoingCount = 0;
let focusRenderState = null;
let focusCompletedMode = "today";
globalThis.focusCanonicalPending = false;
let focusConflictReloadInFlight = false;
let quickStartAreaId = "";
let quickStartAreas = [];
let quickStartAreaInitialized = false;
const QUICK_START_RECENT_AREAS_STORAGE_KEY = "gtd.quickStart.areaRecent.v1";
const QUICK_START_LAST_AREA_STORAGE_KEY = "gtd.quickStart.areaLastStarted.v1";
let allocationController = null;
let inboxStage=null,inboxStageProjects=[],inboxStageAreas=[],inboxStageAreaId="",inboxSettingsForm=null,inboxStageLoad=0;
let currentElapsedInterval = null;
let currentTimerWin=null;
let quickStartSuggestionsController = null;
let focusPipController = null;
let breakCompletionPending = false;
let breakCompletionReadInFlight = false;
let breakCompletionTaskId = null;
let notificationPause = null;
let notificationPauseInterval = null;
let notificationPauseTrigger = null;
let committedCleanupWarningPending = false;
let reloadCurrentRoute = null;
let reloadFocus = null;
let clarifyDetail = null;
let clarifySettingsForm = null;
let clarifyArchived = false;
let clarifyDependencyTasks = [];
let clarifyContinuationTasks = [];
let clarifyDependencyIds = [];
let clarifyAreas = [];
let clarifyProjects = [];
let clarifyDirectAreaId = "";
let taskPanelEditor = null;
let captureTrigger = null;
let projectDetail = null;
let goalDetail = null;
let reviewDetail = null;
let activeReviewKind = "weekly";
const REVIEW_TAB_KEY = "gtd.review.tab.v1";
let reviewDraftPeriod = null;
let reviewGuideMarks = [];
let progressDetail = null;
let reviewGuideStatuses = [];
let activeOutcomeTab = "projects";
let activeDirectionLevel = "overview";
const directionDetails = {purposes: null, visions: null, areas: null};
let directionSnapshot = null;
let focusStatus = "";
const FOCUS_COLLAPSE_STORAGE_KEY = "gtd.focus.action-date-board.v1";
let focusCollapsed = {};
let focusMoveContext = null;
let focusDueContext = null;
let focusBoardToday = "";
let workSessionDetail = null;
let workSessionTrigger = null;
let valueLanternTrigger = null;
let roadmapSnapshot = null;
let roadmapOutcomeDetail = null;
let roadmapView = "overview";
const ROADMAP_VIEW_STORAGE_KEY = "gtd.roadmap.view.v1";
let roadmapYearMatrixState = null;
const loadGenerations = {inbox: 0, clarify: 0, tasks: 0, projects: 0, goals: 0, reviews: 0, reviewEdit: 0, roadmap: 0, status: 0, edit: 0};

class RequestFailure extends Error {
  constructor(kind, payload = null) { super("request_failed"); this.name = "RequestFailure"; this.kind = kind; this.payload = payload; }
}

function setHidden(element, hidden) { if (element) element.hidden = hidden; }
function appliedMutationPath(applied) {
  if (applied && typeof applied.path === "string") return applied.path;
  const effects = applied && Array.isArray(applied.effects) ? applied.effects : [];
  const primary = effects.find((effect) => effect && typeof effect.path === "string");
  return primary ? primary.path : "";
}
function clearMessage(element) { element.textContent = ""; setHidden(element, true); }
function showNotice(message) { elements.notice.textContent = message; setHidden(elements.notice, false); }
function errorText(response) {
  if (!response || !response.error || typeof response.error !== "object") return "処理を完了できませんでした。";
  const parts = [];
  if (typeof response.error.code === "string") parts.push("code: " + response.error.code);
  if (typeof response.error.message === "string") parts.push("message: " + response.error.message);
  if (response.error.details !== undefined) {
    try { parts.push("details: " + JSON.stringify(response.error.details, null, 2)); }
    catch (_error) { parts.push("details: 内容を表示できません"); }
  }
  return parts.length ? parts.join("\n") : "処理を完了できませんでした。";
}
function showApiError(response) { elements.error.textContent = errorText(response); setHidden(elements.error, false); }
function showNetworkError() { elements.error.textContent = "通信または応答の読み込みに失敗しました。接続を確認してください。"; setHidden(elements.error, false); }
function showRequestError(error) { if (error instanceof RequestFailure && error.kind === "api") showApiError(error.payload); else showNetworkError(); }
function showUnknownApplyOutcome() { elements.error.textContent = UNKNOWN_APPLY_MESSAGE; setHidden(elements.error, false); }
function showCanonicalReloadRequired() {
  elements.error.textContent = committedCleanupWarningPending ?
    "変更は適用済みですが、サーバーの後処理を確認できませんでした。再送せず、画面を再読み込みして現在の状態を確認してください。" :
    "保存は完了しましたが、現在の画面を再読み込みできていません。再読み込みが成功するまで新しい変更は保存できません。";
  setHidden(elements.error, false);
}
function showMutationRecoveryRequired(committed = false) {
  elements.error.textContent = committed ?
    "変更は適用済みですが、サーバー復旧が必要です。再送せず、管理者の復旧が完了するまで新しい変更は保存できません。" :
    "サーバー復旧が必要です。管理者の復旧が完了するまで新しい変更は保存できません。";
  setHidden(elements.error, false);
}
function setMutationDisabled(disabled) { for (const button of mutationButtons) if (button) button.disabled = disabled || button.dataset.dependencyBlocked === "true" || button.dataset.cycleBlocked === "true"; if (allocationController && typeof allocationController.syncMutationGate === "function") allocationController.syncMutationGate(); }
function releaseMutationControlsIfIdle() {
  if (!applyInFlight && !mutationPreparationInFlight && !taskReloadInFlight && !applyOutcomeUnknown && !canonicalReloadRequired && !mutationRecoveryRequired && !activePreview && !externalMutationGates.size) setMutationDisabled(false);
}
function mutationIsGated(excludedExternalScope = "") {
  if (applyInFlight || mutationPreparationInFlight || taskReloadInFlight || activePreview) return true;
  if (mutationRecoveryRequired) { showMutationRecoveryRequired(committedCleanupWarningPending); if (reloadFocus) reloadFocus.focus(); return true; }
  if (applyOutcomeUnknown) { showUnknownApplyOutcome(); if (reloadFocus) reloadFocus.focus(); return true; }
  if (canonicalReloadRequired) { showCanonicalReloadRequired(); if (reloadFocus) reloadFocus.focus(); return true; }
  if (Array.from(externalMutationGates).some((scope) => scope !== excludedExternalScope)) return true;
  return false;
}
function setExternalMutationGate(scope, gated) {
  if (typeof scope !== "string" || !scope) return;
  if (gated) { externalMutationGates.add(scope); setMutationDisabled(true); }
  else { externalMutationGates.delete(scope); releaseMutationControlsIfIdle(); }
}
function gateMutationRecoveryRequired(committed = false) {
  mutationRecoveryRequired = true;
  if (committed) committedCleanupWarningPending = true;
  activePreview = null; activeMutation = null; setMutationDisabled(true); showMutationRecoveryRequired(committed);
  if (reloadFocus) reloadFocus.focus();
}
function gateUnknownApplyOutcome(attempt) {
  applyOutcomeUnknown = true; unknownAttempt = attempt; activePreview = null; activeMutation = null; setMutationDisabled(true); showUnknownApplyOutcome();
  if (reloadFocus) reloadFocus.focus();
}
function isCommittedCleanupFailure(error) {
  const failure = error instanceof RequestFailure && error.kind === "api" ? error.payload : null;
  return Boolean(failure && failure.error && failure.error.code === "mutation_committed_cleanup_failed" &&
    failure.error.details && failure.error.details.committed === true);
}
function isRecoveryRequiredFailure(error) {
  const failure = error instanceof RequestFailure && error.kind === "api" ? error.payload : null;
  return Boolean(failure && failure.error && failure.error.code === "mutation_recovery_required" &&
    failure.error.details && failure.error.details.recovery_required === true);
}
function observeMutationState(payload) {
  const state = payload && payload.mutation_state;
  if (state && state.recovery_required === true) gateMutationRecoveryRequired(committedCleanupWarningPending);
}
function nextGeneration(name) { loadGenerations[name] += 1; return loadGenerations[name]; }
function isCurrentGeneration(name, generation) { return loadGenerations[name] === generation; }
function canonicalIdentityMatches(detail, expected) {
  return Boolean(detail && expected && detail.id === expected.id && detail.kind === expected.kind && detail.path === expected.path &&
    detail.content_hash === expected.content_hash && detail.archived === expected.archived);
}
function isNotFoundFailure(error) {
  return error instanceof RequestFailure && error.kind === "api" && error.payload && error.payload.error && error.payload.error.code === "not_found";
}
async function reconcileUnknownAttempt() {
  if (!applyOutcomeUnknown || !unknownAttempt || !unknownAttempt.preview) return null;
  const preview = unknownAttempt.preview; const operation = preview.operation;
  if (Array.isArray(preview.effects)) {
    if (!operation || !preview.effects.length) throw new RequestFailure("reconciliation");
    const states = [];
    for (const effect of preview.effects) {
      const proposed = effect && effect.proposed; const before = effect && effect.before;
      if (!proposed || typeof proposed.id !== "string" || typeof proposed.kind !== "string") throw new RequestFailure("reconciliation");
      let detail;
      try { detail = await apiRequest(entityDetailPath(proposed.kind, proposed.id)); }
      catch (error) {
        if (before === null && isNotFoundFailure(error)) { states.push("not_applied"); continue; }
        throw error;
      }
      if (canonicalIdentityMatches(detail, proposed)) states.push("applied");
      else if (before && canonicalIdentityMatches(detail, before)) states.push("not_applied");
      else throw new RequestFailure("reconciliation");
    }
    if (states.every((state) => state === "applied")) return {outcome: "applied", operation, details: []};
    if (states.every((state) => state === "not_applied")) return {outcome: "not_applied", operation, details: []};
    throw new RequestFailure("reconciliation");
  }
  const proposed = preview.proposed; const before = preview.before;
  if (!operation || !proposed || typeof proposed.id !== "string" || typeof proposed.kind !== "string") throw new RequestFailure("reconciliation");
  let detail;
  try { detail = await apiRequest(entityDetailPath(proposed.kind, proposed.id)); }
  catch (error) {
    if (operation.action === "create" && before === null && isNotFoundFailure(error)) return {outcome: "not_applied", operation, detail: null};
    throw error;
  }
  if (canonicalIdentityMatches(detail, proposed)) return {outcome: "applied", operation, detail};
  if (before && canonicalIdentityMatches(detail, before)) return {outcome: "not_applied", operation, detail};
  throw new RequestFailure("reconciliation");
}
function resetAfterReconciliation(resolution) {
  if (resolution.outcome !== "applied") return;
  const operation = resolution.operation;
  if (operation.kind === "tasks" && operation.action === "create") elements.captureForm.reset();
  if (operation.kind === "tasks" && operation.action === "create_and_start") { recordQuickStartSuccess(operation.fields && operation.fields.area_id); resetQuickStart(); }
  if (operation.kind === "projects") resetProjectForm();
  if (operation.kind === "goals") resetGoalForm();
  if (operation.kind === "reviews") {
    if (operation.action === "create") clearReviewDraft(operation.review_kind, operation.fields && operation.fields.period_start);
    resetReviewForm();
  }
  if (operation.kind === "progress") resetProgressForm();
}
function completeSuccessfulReload(resolution) {
  setHidden(elements.retryCurrent, true);
  if (applyOutcomeUnknown) {
    if (!resolution) return;
    applyOutcomeUnknown = false; unknownAttempt = null;
    if (resolution.outcome === "not_applied" && resolution.operation.kind === "reviews" && resolution.detail) reviewDetail = resolution.detail;
    resetAfterReconciliation(resolution);
    if (mutationRecoveryRequired) showMutationRecoveryRequired(committedCleanupWarningPending); else clearMessage(elements.error);
    releaseMutationControlsIfIdle();
    let message = "保存結果を照合しました: " + (resolution.outcome === "applied" ? "適用済み" : "未適用");
    if (resolution.linkRemoved) message += "。選択していたProjectは現在存在しないためリンクなしにしました。";
    showNotice(message);
    return;
  }
  if (canonicalReloadRequired) {
    canonicalReloadRequired = false;
    if (mutationRecoveryRequired) showMutationRecoveryRequired(committedCleanupWarningPending);
    else if (committedCleanupWarningPending) { committedCleanupWarningPending = false; elements.error.textContent = "変更は適用済みですが、サーバー後処理は要確認です。同じ操作は再送しないでください。"; setHidden(elements.error, false); }
    else clearMessage(elements.error);
    releaseMutationControlsIfIdle();
  }
}
function showReloadFailure(error) {
  setHidden(elements.retryCurrent, false);
  if (mutationRecoveryRequired) showMutationRecoveryRequired(committedCleanupWarningPending);
  else if (applyOutcomeUnknown) showUnknownApplyOutcome();
  else if (canonicalReloadRequired) showCanonicalReloadRequired();
  else showRequestError(error);
}

async function apiRequest(path, options = {}) {
  const mutation = options.method === "POST";
  const requestOptions = {method: options.method || "GET", headers: mutation ? MUTATION_HEADERS : API_HEADERS, cache: "no-store", credentials: "same-origin"};
  if (mutation) requestOptions.body = options.body;
  let response; let payload;
  try { response = await fetch(path, requestOptions); payload = await response.json(); }
  catch (_error) { throw new RequestFailure("network"); }
  if (!response.ok || (payload && payload.error)) throw new RequestFailure("api", payload);
  observeMutationState(payload);
  return payload;
}

function safeFrontmatter(entity) { return entity && entity.frontmatter && typeof entity.frontmatter === "object" ? entity.frontmatter : {}; }
function appendListText(list, text) { const item = document.createElement("li"); item.textContent = text; list.append(item); }
function renderInboxCount(snapshot) {
  const facts = snapshot && snapshot.facts && typeof snapshot.facts === "object" ? snapshot.facts : {};
  const count = Number.isInteger(facts.inbox_count) ? facts.inbox_count : 0;
  elements.inboxHeadingCount.textContent = count ? String(count) : "";
  for (const indicator of [elements.inboxCountDesktop, elements.inboxCountMobile]) {
    indicator.textContent = String(count); setHidden(indicator, count === 0);
  }
}
function renderStatusFacts(snapshot) {
  const facts = snapshot && snapshot.facts && typeof snapshot.facts === "object" ? snapshot.facts : {};
  const counts = facts.task_status_counts && typeof facts.task_status_counts === "object" ? facts.task_status_counts : {};
  const projects = new Map((snapshot && Array.isArray(snapshot.entities) ? snapshot.entities : []).filter((entity) => entity.kind === "projects").map((entity) => [entity.id, entity]));
  elements.statusFacts.replaceChildren();
  appendListText(elements.statusFacts, "Inbox: " + (Number.isInteger(facts.inbox_count) ? facts.inbox_count : 0));
  for (const status of TASK_STATUSES) appendListText(elements.statusFacts, "Task " + status + ": " + (Number.isInteger(counts[status]) ? counts[status] : 0));
  const missing = Array.isArray(facts.active_projects_without_next_action) ? facts.active_projects_without_next_action : [];
  if (!missing.length) appendListText(elements.statusFacts, "Next Actionがないactive Project: 該当なし");
  for (const id of missing) { const project = projects.get(id); appendListText(elements.statusFacts, "Next Actionがないactive Project: " + (project ? safeFrontmatter(project).title || id : id)); }
  appendListText(elements.statusFacts, "Archive: " + (Number.isInteger(facts.archive_count) ? facts.archive_count : 0));
}
function renderReviewFacts(snapshot){const facts=snapshot&&snapshot.facts&&typeof snapshot.facts==="object"?snapshot.facts:{},counts=facts.task_status_counts&&typeof facts.task_status_counts==="object"?facts.task_status_counts:{},missing=Array.isArray(facts.active_projects_without_next_action)?facts.active_projects_without_next_action:[];elements.reviewFacts.replaceChildren();if(activeReviewKind==="daily"){appendListText(elements.reviewFacts,"Inbox "+(Number.isInteger(facts.inbox_count)?facts.inbox_count:0)+"件");appendListText(elements.reviewFacts,"実行中 "+(Number.isInteger(counts.doing)?counts.doing:0)+"件");appendListText(elements.reviewFacts,"予定 "+(Number.isInteger(counts.scheduled)?counts.scheduled:0)+"件");return;}appendListText(elements.reviewFacts,"Inbox "+(Number.isInteger(facts.inbox_count)?facts.inbox_count:0)+"件");appendListText(elements.reviewFacts,"Waiting "+(Number.isInteger(counts.waiting)?counts.waiting:0)+"件");appendListText(elements.reviewFacts,"Next ActionがないProject "+missing.length+"件");const roadmap=facts.roadmap&&typeof facts.roadmap==="object"?facts.roadmap:{},entities=Array.isArray(snapshot&&snapshot.entities)?snapshot.entities:[],byId=new Map(entities.map(entity=>[entity.id,entity])),active=byId.get(roadmap.active_cycle_id);if(active){const activeFm=safeFrontmatter(active);appendListText(elements.reviewFacts,"Active Cycle "+(activeFm.title||active.id)+"（"+(activeFm.start_date||"?")+"〜"+(activeFm.end_date||"?")+"）");for(const outcomeId of Array.isArray(roadmap.now_outcome_ids)?roadmap.now_outcome_ids:[]){const outcome=byId.get(outcomeId);appendListText(elements.reviewFacts,"Now Outcome: "+(outcome?(safeFrontmatter(outcome).title||outcome.id):outcomeId));}}else appendListText(elements.reviewFacts,"Active Cycleはありません。");const roadmapLink=document.createElement("a");roadmapLink.href="/roadmap";roadmapLink.textContent="Roadmapを確認する";const item=document.createElement("li");item.append(roadmapLink);elements.reviewFacts.append(item);}
function appendDetail(list, label, value) {
  const term = document.createElement("dt"); term.textContent = label;
  const detail = document.createElement("dd"); detail.textContent = typeof value === "string" ? value : "";
  list.append(term, detail);
}
function makeTechnicalDetails(entity) {
  const details = document.createElement("details"); details.className = "technical-details";
  const summary = document.createElement("summary"); summary.textContent = "詳細情報";
  const list = document.createElement("dl"); const fm = safeFrontmatter(entity);
  appendDetail(list, "ID", entity && entity.id); appendDetail(list, "ファイル", entity && entity.path);
  appendDetail(list, "content hash", entity && entity.content_hash); appendDetail(list, "作成日時", fm.created_at);
  details.append(summary, list); return details;
}
function makeEntityRow(entity, actions, meta, className = "") {
  const item = document.createElement("li"); item.className = "entity-row" + (className ? " " + className : "");
  const main = document.createElement("div"); main.className = "entity-row-main";
  const copy = document.createElement("div"); copy.className = "entity-row-copy";
  const heading = document.createElement("h3"); heading.className = "entity-title"; heading.textContent = safeFrontmatter(entity).title || "タイトルなし";
  copy.append(heading);
  if (meta.length) { const line = document.createElement("p"); line.className = "entity-meta"; line.textContent = meta.join(" · "); copy.append(line); }
  const actionRow = document.createElement("div"); actionRow.className = "card-actions"; actionRow.append(...actions);
  main.append(copy, actionRow); item.append(main, makeTechnicalDetails(entity)); return item;
}
function projectMap(entities) {
  return new Map((Array.isArray(entities) ? entities : []).filter((entity) => entity.kind === "projects").map((entity) => [entity.id, entity]));
}
function restoreAvailableFrom(v){[af.value,at.value]=Object.values(availableFromParts(v))}
function taskMeta(entity,projects,includeProject=true){return taskSettings.taskMeta(entity,projects,includeProject,formatWorkTimestamp)}
function makeTaskRow(entity, actions, projects, className = "") { return makeEntityRow(entity, actions, taskMeta(entity, projects), className); }
function tokyoDateTimeParts(date) {
  const values = {};
  for (const part of new Intl.DateTimeFormat("en-US-u-ca-gregory", {timeZone: "Asia/Tokyo", year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).formatToParts(date)) {
    if (["year", "month", "day", "hour", "minute"].includes(part.type)) values[part.type] = Number(part.value);
  }
  return values;
}
function formatInboxCreatedAt(value) {
  if (typeof value !== "string") return ""; const created = new Date(value); if (Number.isNaN(created.getTime())) return "";
  const then = tokyoDateTimeParts(created); const now = tokyoDateTimeParts(new Date());
  const ageInDays = Math.round((Date.UTC(now.year, now.month - 1, now.day) - Date.UTC(then.year, then.month - 1, then.day)) / 86400000);
  if (ageInDays === 0) return "今日 " + String(then.hour).padStart(2, "0") + ":" + String(then.minute).padStart(2, "0");
  if (ageInDays === 1) return "昨日";
  return then.month + "月" + then.day + "日";
}
function makeInboxRow(entity) {
  const fm = safeFrontmatter(entity); const created = formatInboxCreatedAt(fm.created_at);
  return makeEntityRow(entity, [linkButton("明確化", clarifyHref(entity.id))], created ? [created] : []);
}
function makeProjectRow(entity, actions, tasks, entities = []) {
  const fm = safeFrontmatter(entity); const nextCount = tasks.filter((task) => safeFrontmatter(task).project_id === entity.id && safeFrontmatter(task).status === "next").length;
  const byEntityId = new Map(entities.map((item) => [item.id, item])); const outcome = byEntityId.get(fm.roadmap_outcome_id); const goal = byEntityId.get(fm.goal_id || (outcome && safeFrontmatter(outcome).goal_id)); const area = byEntityId.get(fm.area_id);
  const meta = [fm.status || "状態不明", "Next " + nextCount + "件"];
  if (outcome) meta.push("Outcome " + (safeFrontmatter(outcome).title || outcome.id)); if (goal) { const vision = byEntityId.get(safeFrontmatter(goal).vision_id); meta.push("Goal " + (safeFrontmatter(goal).title || goal.id) + (vision ? " → Vision " + (safeFrontmatter(vision).title || vision.id) : "")); }
  if (area) meta.push("主Area " + (safeFrontmatter(area).title || area.id));
  return makeEntityRow(entity, actions, meta);
}
function makeGoalRow(entity, actions, projects) {
  const fm = safeFrontmatter(entity); const linked = projects.filter((project) => safeFrontmatter(project).goal_id === entity.id).length;
  return makeEntityRow(entity, actions, [fm.status || "状態不明", "Project " + linked + "件"]);
}
function makeReviewRow(entity, actions) {
  const fm = safeFrontmatter(entity); const meta = [fm.review_kind === "daily" ? "Daily" : "Weekly"];
  if (fm.period_start) meta.push(fm.period_start); if (entity.archived === true) meta.push("アーカイブ済み");
  return makeEntityRow(entity, actions, meta);
}
function linkButton(label, href) { const link = document.createElement("a"); link.className = "button-link"; link.textContent = label; link.setAttribute("href", href); return link; }
function actionButton(label, callback, mutation = false) {
  const button = document.createElement("button"); button.type = "button"; button.className = "secondary"; button.textContent = label;
  if (mutation) { button.setAttribute("data-mutation", ""); mutationButtons.push(button); dynamicMutationButtons.push(button); button.disabled = applyOutcomeUnknown || canonicalReloadRequired || applyInFlight || mutationPreparationInFlight || taskReloadInFlight || Boolean(activePreview) || Boolean(externalMutationGates.size); }
  if (mutation) button.disabled = button.disabled || mutationRecoveryRequired;
  button.addEventListener("click", callback); return button;
}
function entityDetailPath(kind, id) { return "/api/v1/entities/" + kind + "/" + encodeURIComponent(id); }
function clarifyReturnPath() {
  const candidate = new URLSearchParams(location.search || "").get("return_to");
  if (!candidate) return "/tasks";
  let parsed;
  try { parsed = new URL(candidate, location.href); }
  catch (_error) { return "/tasks"; }
  if (parsed.origin !== location.origin || !["/tasks","/inbox","/projects","/search"].includes(parsed.pathname)) return "/tasks";
  return parsed.pathname + parsed.search;
}
function navigateClarifyReturn() {
  if (typeof window !== "undefined" && window.location && typeof window.location.assign === "function") window.location.assign(clarifyReturnPath());
  else if (typeof location.assign === "function") location.assign(clarifyReturnPath());
}
function clarifyHref(id) {
  const pathname = location.pathname === "/inbox" || location.pathname === "/projects" ? location.pathname : "/tasks";
  const query = typeof location.search === "string" ? location.search : "";
  return "/clarify?id=" + encodeURIComponent(id) + "&return_to=" + encodeURIComponent(pathname + query);
}

function addPreviewSection(container, label, value) {
  const section = document.createElement("section"); section.className = "preview-section";
  const heading = document.createElement("h3"); heading.textContent = label;
  const content = document.createElement("pre"); content.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  section.append(heading, content); container.append(section);
}
function addPreviewSummaryRow(container, label, value) {
  if (value === undefined || value === null || value === "") return;
  const row = document.createElement("div"); row.className = "preview-summary-row";
  const term = document.createElement("span"); term.className = "preview-summary-label"; term.textContent = label;
  const detail = document.createElement("p"); detail.className = "preview-summary-value"; detail.textContent = String(value);
  row.append(term, detail); container.append(row);
}
function showPreview(previewResponse) {
  const action = previewResponse && previewResponse.operation && previewResponse.operation.action;
  const archive = action === "archive" || (Array.isArray(previewResponse && previewResponse.effects) && previewResponse.effects.some((effect) => effect && (effect.role === "project_archived" || effect.role === "task_archived")));
  if (elements.previewTitle) elements.previewTitle.textContent = archive ? "アーカイブする内容" : "Cycleを終了する内容";
  elements.confirmPreview.textContent = archive ? "この内容でアーカイブ" : "この内容で終了";
  elements.previewContent.replaceChildren();
  const summary = document.createElement("section"); summary.className = "preview-summary";
  const proposed = Array.isArray(previewResponse.effects) ? null : previewResponse.proposed;
  const before = Array.isArray(previewResponse.effects) ? null : previewResponse.before;
  const proposedFm = safeFrontmatter(proposed); const beforeFm = safeFrontmatter(before);
  const isRoadmap = previewResponse.operation && ["roadmap_move", "roadmap_outcome_bundle_update", "cycle_activate", "cycle_close"].includes(previewResponse.operation.action); addPreviewSummaryRow(summary, "対象", Array.isArray(previewResponse.effects) ? (isRoadmap ? "Roadmapをまとめて変更" : "Taskをまとめて変更") : ({tasks: "Task", projects: "Project", goals: "Goal", reviews: "Review", roadmap_outcomes: "Roadmap Outcome", cycles: "Cycle"}[proposed && proposed.kind] || "項目"));
  if (Array.isArray(previewResponse.effects)) {
    const roleLabels = {interrupted: "中断", continuation: "続き", completed: "完了", started: "開始", dependency_released: "依存解除", dependency_retargeted: "依存を付替", cycle_activated: "Cycleを開始", cycle_closed: "Cycleを終了", outcome_now: "Now Outcome", outcome_achieved: "Outcomeを達成", outcome_carried: "Outcomeをcarry", outcome_next: "OutcomeをNextへ戻す", outcome_later: "OutcomeをLaterへ戻す", outcome_dropped: "Outcomeを取り下げ", carryover_cycle_updated: "Carryover Cycleを更新", roadmap_moved: "Outcomeを移動", outcome_updated: "Outcomeを更新", project_created: "Projectを追加", project_updated: "Projectを更新", project_moved: "Projectを移動", project_archived: "Projectをアーカイブ", task_archived: "Taskをアーカイブ"};
    for (const effect of previewResponse.effects) {
      const effectFm = safeFrontmatter(effect && effect.proposed);
      addPreviewSummaryRow(summary, roleLabels[effect && effect.role] || (isRoadmap ? "Roadmap effect" : "変更"), effectFm.title || (effect && effect.proposed && effect.proposed.id));
    }
  } else {
    addPreviewSummaryRow(summary, "タイトル", proposedFm.title);
    if (beforeFm.status !== proposedFm.status) addPreviewSummaryRow(summary, "状態", (beforeFm.status || "なし") + " → " + (proposedFm.status || "なし"));
    if (beforeFm.contexts !== proposedFm.contexts) {
      const beforeContexts = contextsForDisplay(beforeFm.contexts).join(", ") || "なし";
      const proposedContexts = contextsForDisplay(proposedFm.contexts).join(", ") || "なし";
      addPreviewSummaryRow(summary, "Context", beforeContexts + " → " + proposedContexts);
    }
    if (beforeFm.project_id !== proposedFm.project_id) addPreviewSummaryRow(summary, "Project", (beforeFm.project_id || "リンクなし") + " → " + (proposedFm.project_id || "リンクなし"));
    addPreviewSummaryRow(summary, "保存先", proposed && proposed.path);
  }
  const technical = document.createElement("details"); technical.className = "preview-technical";
  const technicalSummary = document.createElement("summary"); technicalSummary.textContent = "正確な差分と技術情報";
  const technicalContent = document.createElement("div"); technicalContent.className = "preview-technical-content";
  if (Array.isArray(previewResponse.effects)) {
    const roleLabels = {interrupted: "中断したTask", continuation: "続きTask", completed: "完了したTask", started: "開始するTask"};
    for (const effect of previewResponse.effects) {
      const label = effect && typeof effect.role === "string" ? (roleLabels[effect.role] || "変更対象Task") + " (" + effect.role + ")" : "変更対象Task";
      addPreviewSection(technicalContent, label + " / 変更前", effect && effect.before); addPreviewSection(technicalContent, label + " / 変更後", effect && effect.proposed);
      addPreviewSection(technicalContent, label + " / 差分", effect && effect.diff); addPreviewSection(technicalContent, label + " / 保存場所", effect && effect.path_change);
    }
  } else {
    addPreviewSection(technicalContent, "変更前", previewResponse.before); addPreviewSection(technicalContent, "変更後", previewResponse.proposed);
    addPreviewSection(technicalContent, "変更差分", previewResponse.diff); addPreviewSection(technicalContent, "保存場所の変更", previewResponse.path_change);
  }
  addPreviewSection(technicalContent, "検証結果", previewResponse.validation); addPreviewSection(technicalContent, "サーバー生成プレビュー全体", previewResponse);
  technical.append(technicalSummary, technicalContent); elements.previewContent.append(summary, technical);
  elements.dialog.showModal(); elements.confirmPreview.focus();
}
function previewNeedsConfirmation(previewResponse) {
  const operation = previewResponse && previewResponse.operation;
  if (operation && (operation.action === "archive" || operation.action === "cycle_close")) return true;
  if (Array.isArray(previewResponse && previewResponse.effects)) return previewResponse.effects.some((effect) => effect && (effect.role === "project_archived" || effect.role === "task_archived"));
  return Boolean(previewResponse && previewResponse.proposed && previewResponse.proposed.archived === true);
}
function clearActivePreview() { activePreview = null; activeMutation = null; elements.previewContent.replaceChildren(); elements.dialog.close(); }
function closePreview() {
  if (applyInFlight) return;
  const mutation = activeMutation; const focus = mutation && mutation.focus; clearActivePreview();
  if (mutation && mutation.reopenCapture) { elements.captureDialog.showModal(); elements.captureTitle.focus(); }
  releaseMutationControlsIfIdle(); if (focus && !(mutation && mutation.reopenCapture)) focus.focus();
}
async function previewMutation(operation, onSuccess, focus, preparationToken = null, options = {}) {
  if (preparationToken === null) { if (mutationIsGated()) return; }
  else if (!mutationPreparationInFlight || taskPreparationGeneration !== preparationToken) return;
  clearMessage(elements.error); clearMessage(elements.notice); setMutationDisabled(true);
  try {
    if (typeof options.onPreviewStart === "function" && options.onPreviewStart() === false) return;
    const previewResponse = await apiRequest("/api/v1/mutations/preview", {method: "POST", body: JSON.stringify(operation)});
    if (preparationToken !== null && (!mutationPreparationInFlight || taskPreparationGeneration !== preparationToken)) return;
    if (typeof options.isCurrent === "function" && !options.isCurrent()) return;
    activePreview = previewResponse;
    activeMutation = {onSuccess, onFailure: options.onFailure, onReadbackFailure: options.onReadbackFailure, onUnknown: options.onUnknown, onCommittedCleanup: options.onCommittedCleanup, localReload: options.localReload, isCurrent: options.isCurrent, onApplyStart: options.onApplyStart, onReadbackStart: options.onReadbackStart, focus, reopenCapture: options.reopenCapture === true, postReloadFocus: options.postReloadFocus, redirectTo: options.redirectTo || "", backgroundReload: options.backgroundReload === true};
    if (previewNeedsConfirmation(activePreview)) showPreview(activePreview);
    else if (options.awaitApply === true) await applyPreviewMutation();
    else void applyPreviewMutation();
  } catch (error) {
    if (typeof options.isCurrent === "function" && !options.isCurrent()) return;
    clearActivePreview(); if (typeof options.onFailure === "function") options.onFailure(error); if (isRecoveryRequiredFailure(error)) gateMutationRecoveryRequired(); else showRequestError(error);
    if (options.reopenCapture === true && !elements.captureDialog.open) elements.captureDialog.showModal();
    const failureFocus = typeof options.failureFocus === "function" ? options.failureFocus() : options.failureFocus;
    if (failureFocus) failureFocus.focus(); else if (focus) focus.focus();
  }
  finally { releaseMutationControlsIfIdle(); }
}

async function applyPreviewMutation() {
  if (!activePreview || !activeMutation || applyInFlight) return;
  const completion = activeMutation;
  if (typeof completion.isCurrent === "function" && !completion.isCurrent()) { clearActivePreview(); releaseMutationControlsIfIdle(); return; }
  if (typeof completion.onApplyStart === "function" && completion.onApplyStart() === false) return;
  clearMessage(elements.error); applyInFlight = true; elements.confirmPreview.disabled = true; elements.cancelPreview.disabled = true; setMutationDisabled(true);
  const {preview_hash, ...preview} = activePreview;
  const attempt = {preview, preview_hash};
  let applyConfirmed = false;
  try {
    const applied = await apiRequest("/api/v1/mutations/apply", {method: "POST", body: JSON.stringify({preview, preview_hash})});
    applyConfirmed = true;
    clearActivePreview();
    canonicalReloadRequired = true; setMutationDisabled(true);
    if (typeof completion.onReadbackStart === "function") completion.onReadbackStart();
    await completion.onSuccess(applied);
    if (completion.redirectTo) { navigateClarifyReturn(); return; }
    if (typeof completion.localReload === "function") { await completion.localReload(applied); completeSuccessfulReload(null); }
    else if (reloadCurrentRoute && completion.backgroundReload) { void reloadCurrentRoute(); }
    else if (reloadCurrentRoute) { await reloadCurrentRoute(); const postReloadFocus = typeof completion.postReloadFocus === "function" ? completion.postReloadFocus() : null; if (postReloadFocus) postReloadFocus.focus(); else if (reloadFocus) reloadFocus.focus(); }
    else if (completion.focus) completion.focus.focus();
  } catch (error) {
    clearActivePreview();
    if (isCommittedCleanupFailure(error)) {
      canonicalReloadRequired = true; gateMutationRecoveryRequired(true);
      if (typeof completion.localReload === "function") { if (typeof completion.onReadbackStart === "function") completion.onReadbackStart(); try { await completion.localReload(null, {recovery: true}); } catch (_reloadError) {} }
      else if (reloadCurrentRoute) { await reloadCurrentRoute(); if (reloadFocus) reloadFocus.focus(); }
      else if (completion.focus) completion.focus.focus();
      if (typeof completion.onCommittedCleanup === "function") completion.onCommittedCleanup(error);
    }
    else if (applyConfirmed) { canonicalReloadRequired = true; if (typeof completion.onReadbackFailure === "function") completion.onReadbackFailure(error); showReloadFailure(error); }
    else if (isRecoveryRequiredFailure(error)) { if (typeof completion.onFailure === "function") completion.onFailure(error); gateMutationRecoveryRequired(); }
    else if (error instanceof RequestFailure && error.kind === "api") { if (typeof completion.onFailure === "function") completion.onFailure(error); showApiError(error.payload); if (completion.focus) completion.focus.focus(); }
    else { if (typeof completion.onUnknown === "function") completion.onUnknown(error); gateUnknownApplyOutcome(attempt); }
  } finally { applyInFlight = false; elements.confirmPreview.disabled = false; elements.cancelPreview.disabled = false; releaseMutationControlsIfIdle(); }
}
elements.confirmPreview.addEventListener("click", async () => { await applyPreviewMutation(); });
elements.cancelPreview.addEventListener("click", () => { if (!applyInFlight) closePreview(); });
elements.dialog.addEventListener("cancel", (event) => { if (applyInFlight) { event.preventDefault(); return; } closePreview(); });

function openValueLantern() { valueLanternTrigger = document.activeElement; elements.valueLanternDialog.showModal(); elements.valueLanternClose.focus(); }
function closeValueLantern() { if (elements.valueLanternDialog.open) elements.valueLanternDialog.close(); if (valueLanternTrigger) valueLanternTrigger.focus(); valueLanternTrigger = null; }
elements.valueLanternOpen.addEventListener("click", openValueLantern);
elements.valueLanternOpenCopy.addEventListener("click", openValueLantern);
elements.valueLanternClose.addEventListener("click", closeValueLantern);
elements.valueLanternDialog.addEventListener("close", () => { if (valueLanternTrigger) { valueLanternTrigger.focus(); valueLanternTrigger = null; } });

function syncInboxStageArea() { const project = inboxStageProjects.find((item) => item.id === elements.inboxStageProject.value); const inherited = project && safeFrontmatter(project).area_id || ""; const ui = window.AllocationUI; if (ui && ui.configureAreaSelect) ui.configureAreaSelect({select: elements.inboxStageArea, areas: inboxStageAreas.map((area) => ({id: area.id, title: safeFrontmatter(area).title || area.id})), value: project ? inherited : inboxStageAreaId, disabled: Boolean(project), onChange: (value) => { inboxStageAreaId = value; }}); setHidden(elements.inboxStageAreaField, Boolean(project)); byId("inbox-stage-area-inherited").textContent = project ? (inherited ? "Project由来のArea" : "ProjectにAreaは未設定です。") : ""; }
function applyInboxStageStatus(status){if(!inboxStage)return;inboxStage.status=status;const state=taskSettings.syncClarifyConditions({status,waiting_for:elements.inboxStageWaiting.value,scheduled_start:elements.inboxStageStart.value,scheduled_end:elements.inboxStageEnd.value});setHidden(byId("inbox-stage-waiting-field"),!state.waitingVisible);setHidden(byId("inbox-stage-scheduled-field"),!state.scheduledVisible);setHidden(byId("inbox-stage-action-date-field"),status!=="next"&&!elements.inboxStageActionDate.value);elements.inboxStageWaiting.required=false;elements.inboxStageWaiting.setCustomValidity("");elements.inboxStageStart.required=status==="scheduled";elements.inboxStageEnd.required=status==="scheduled";for(const button of elements.inboxStageStatusButtons.children)button.setAttribute("aria-pressed",String(button.dataset.status===status));if(inboxSettingsForm){inboxSettingsForm.session.update(readInboxStageDraft(false));inboxSettingsForm.view.sync();}}
function readInboxStageDraft(normalize=true){let draft={title:elements.inboxStageTitle.value,status:inboxStage.status,project_id:elements.inboxStageProject.value,area_id:elements.inboxStageProject.value?"":elements.inboxStageArea.value,contexts:elements.inboxStageContexts.value,estimated_minutes:elements.inboxStageMinutes.value,due:elements.inboxStageDue.value,action_date:elements.inboxStageActionDate.value,available_date:elements.inboxStageAvailableFrom.value,available_time:elements.inboxStageAvailableTime.value,waiting_for:elements.inboxStageWaiting.value,scheduled_start:elements.inboxStageStart.value,scheduled_end:elements.inboxStageEnd.value,body:elements.inboxStageBody.value};if(elements.inboxStageDependencies.value||Object.prototype.hasOwnProperty.call(safeFrontmatter(inboxStage.task),"depends_on"))draft.depends_on=elements.inboxStageDependencies.value;if(normalize){draft=taskSettings.normalizeDraft(draft,clarifyScheduledTimestamp);if(Object.prototype.hasOwnProperty.call(draft,"depends_on"))draft.depends_on=serializeDependencyIds(draft.depends_on.split(",").map((id)=>id.trim()).filter(Boolean));}return draft;}
async function stageInboxTask(task,status,fromDrag=false){if(!task||!status)return;const generation=++inboxStageLoad;clearInboxStage(false);elements.inboxStageConfirm.disabled=true;try{const detail=await apiRequest(entityDetailPath("tasks",task.id));if(generation!==inboxStageLoad)return;if(!detail||detail.id!==task.id||detail.kind!=="tasks"||detail.archived===true)throw new RequestFailure("reconciliation");inboxStage={task:detail,status,fromDrag};const fm=safeFrontmatter(detail),available=availableFromParts(fm.available_from);elements.inboxStageTitle.value=fm.title||"";elements.inboxStageTitleSummary.textContent=fm.title||"タイトルなし";setHidden(elements.inboxStageTitle,true);replaceOptions(elements.inboxStageProject,inboxStageProjects,fm.project_id||"","リンクなし");inboxStageAreaId=fm.area_id||"";elements.inboxStageContexts.value=contextsForInput(fm.contexts);elements.inboxStageMinutes.value=fm.estimated_minutes||"";restoreDueInput(elements.inboxStageDue,fm.due);elements.inboxStageActionDate.value=fm.action_date||"";elements.inboxStageAvailableFrom.value=available.date;elements.inboxStageAvailableTime.value=available.time;elements.inboxStageWaiting.value=fm.waiting_for||"";elements.inboxStageDependencies.value=contextsForInput(fm.depends_on);restoreScheduledInput(elements.inboxStageStart,fm.scheduled_start);restoreScheduledInput(elements.inboxStageEnd,fm.scheduled_end);elements.inboxStageBody.value=detail.body||"";elements.inboxStageStatusButtons.replaceChildren(...["next","waiting","scheduled","someday"].map((value)=>{const button=actionButton(taskStatusLabel(value),()=>applyInboxStageStatus(value));button.dataset.status=value;button.setAttribute("aria-pressed","false");return button;}));setHidden(elements.inboxStageDestinations,fromDrag);elements.inboxStageDestinationReadonly.textContent=fromDrag?"行き先: "+taskStatusLabel(status):"";setHidden(elements.inboxStageDestinationReadonly,!fromDrag);syncInboxStageArea();inboxSettingsForm=taskSettings.mountForm({root:elements.inboxStageSheet,initial:detail,writeDraft(){},readDraft:()=>readInboxStageDraft(true)});applyInboxStageStatus(status);setHidden(elements.inboxStageSheet,false);(fromDrag?elements.inboxStageCancel:elements.inboxStageEditTitle).focus();}catch(error){if(generation===inboxStageLoad)showRequestError(error);}finally{if(generation===inboxStageLoad)elements.inboxStageConfirm.disabled=false;}}
function editInboxStageTitle(){setHidden(elements.inboxStageTitle,false);elements.inboxStageTitle.focus();}
function clearInboxStage(invalidate=true){if(invalidate)inboxStageLoad++;inboxStage=null;inboxSettingsForm=null;setHidden(elements.inboxStageSheet,true);}
function saveInboxStage(){if(!inboxStage)return;const waitingMissing=inboxStage.status==="waiting"&&!elements.inboxStageWaiting.value.trim()&&!elements.inboxStageAvailableFrom.value;elements.inboxStageWaiting.setCustomValidity(waitingMissing?"待っている対象または着手可能日を入力してください。":"");if(!elements.inboxStageSheet.reportValidity())return;let serialized;try{serialized=inboxSettingsForm.serialize();}catch(_error){elements.error.textContent="Contextsは空白を含まない名前をカンマで区切ってください。";setHidden(elements.error,false);return;}const task=inboxStage.task,operation={action:"update",kind:"tasks",id:task.id,base_hash:task.content_hash,fields:serialized.fields,body:serialized.body};previewMutation(operation,()=>{clearInboxStage();showNotice("Taskを保存しました。");},elements.inboxStageTitle);}
let inboxDrag = null;
function clearInboxDropTargets() { for (const lane of (document.querySelectorAll ? document.querySelectorAll("[data-inbox-stage-status],[data-inbox-archive]") : [])) lane.classList.remove("inbox-lane-active"); }
function resolveInboxDropTarget(event) { const destination = ProjectKanbanMotion.destinationAt(inboxDrag && inboxDrag.dock, event); if (destination) return [...document.querySelectorAll("[data-inbox-stage-status],[data-inbox-archive]")].find((lane) => destination === "archive" ? lane.dataset.inboxArchive : lane.dataset.inboxStageStatus === destination) || null; const hit = typeof document.elementFromPoint === "function" ? document.elementFromPoint(event.clientX, event.clientY) : null, lane = hit && hit.closest ? hit.closest("[data-inbox-stage-status],[data-inbox-archive]") : null; return lane && (lane.dataset.inboxStageStatus || lane.dataset.inboxArchive) ? lane : null; }
function cleanupInboxDrag(keepLayer=false) {
  if (!inboxDrag) return null;
  const state = inboxDrag, captureTarget = state.captureTarget; inboxDrag = null;
  if (captureTarget) {
    captureTarget.removeEventListener("pointermove", moveInboxDrag); captureTarget.removeEventListener("pointerup", endInboxDrag); captureTarget.removeEventListener("pointercancel", cancelInboxDrag); captureTarget.removeEventListener("lostpointercapture", cancelInboxDrag);
    ProjectKanbanMotion.cancelPointerGesture(state.gesture);
  }
  if (typeof document.removeEventListener === "function") { document.removeEventListener("pointerup", endInboxDrag, true); document.removeEventListener("pointercancel", cancelInboxDrag, true); }
  ProjectKanbanMotion.stopAutoScroll(state); ProjectKanbanMotion.clearDestinationDock(state.dock); clearInboxDropTargets(); state.card.classList.remove("is-inbox-dragging"); if (!keepLayer) ProjectKanbanMotion.clearVisualDragLayer(state.layer); return state;
}
function cancelInboxDrag(event) { if (!inboxDrag || (event && typeof event.pointerId === "number" && inboxDrag.pointerId !== event.pointerId)) return; cleanupInboxDrag(); }
function beginInboxDrag(event, task, card) {
  if (typeof event.pointerId !== "number" || mutationIsGated()) return;
  if (inboxDrag) cancelInboxDrag();
  const captureTarget = event.currentTarget || card, gesture = ProjectKanbanMotion.beginPointerGesture(event, captureTarget, {isGated: mutationIsGated}); if (!gesture) return;
  inboxDrag = {task, card, captureTarget, pointerId: event.pointerId, gesture, moved: false, targetStatus: "", targetArchive: false, lastY: event.clientY, autoScroll: false, autoFrame: 0};
  captureTarget.addEventListener("pointermove", moveInboxDrag); captureTarget.addEventListener("pointerup", endInboxDrag); captureTarget.addEventListener("pointercancel", cancelInboxDrag); captureTarget.addEventListener("lostpointercapture", cancelInboxDrag);
  if (typeof document.addEventListener === "function") { document.addEventListener("pointerup", endInboxDrag, true); document.addEventListener("pointercancel", cancelInboxDrag, true); }
}
function moveInboxDrag(event) {
  if (!inboxDrag || inboxDrag.pointerId !== event.pointerId) return;
  if (!ProjectKanbanMotion.movePointerGesture(inboxDrag.gesture, event).active) return;
  inboxDrag.moved = true; inboxDrag.card.classList.add("is-inbox-dragging"); inboxDrag.lastY = event.clientY; if (typeof event.preventDefault === "function") event.preventDefault(); if (!inboxDrag.layer) inboxDrag.layer=ProjectKanbanMotion.createVisualDragLayer(safeFrontmatter(inboxDrag.task).title||inboxDrag.task.id,"inbox-drag-layer"); ProjectKanbanMotion.moveVisualDragLayer(inboxDrag.layer,event.clientX+12,event.clientY+12);
  if (!inboxDrag.dock) inboxDrag.dock = ProjectKanbanMotion.createDestinationDock([...["next", "waiting", "scheduled", "someday"].map((key) => ({key, label: taskStatusLabel(key)})), {key: "archive", label: "アーカイブ", danger: true}], event);
  clearInboxDropTargets(); const target = resolveInboxDropTarget(event); inboxDrag.targetStatus = target ? target.dataset.inboxStageStatus : ""; inboxDrag.targetArchive = Boolean(target && target.dataset.inboxArchive); if (target) target.classList.add("inbox-lane-active");
  if (!inboxDrag.autoScroll) { inboxDrag.autoScroll = true; const tick = () => { if (inboxDrag) ProjectKanbanMotion.autoScroll(inboxDrag, tick); }; ProjectKanbanMotion.beginAutoScroll(inboxDrag, tick); }
}
function endInboxDrag(event) {
  if (!inboxDrag || inboxDrag.pointerId !== event.pointerId) return;
  const target = resolveInboxDropTarget(event), state = cleanupInboxDrag(true); if (mutationIsGated()) { ProjectKanbanMotion.clearVisualDragLayer(state.layer); return; }
  if (state.moved && target) { const status = target.dataset.inboxStageStatus; ProjectKanbanMotion.settleVisualDragLayer(state.layer,target,()=>status ? stageInboxTask(state.task,status,true) : previewMutation({action:"archive",kind:"tasks",id:state.task.id,base_hash:state.task.content_hash},(applied)=>showNotice("Taskをアーカイブしました: "+applied.path),state.captureTarget)); }
  else ProjectKanbanMotion.clearVisualDragLayer(state.layer);
}
function renderInbox(entities, allEntities = entities) {
  elements.inboxList.replaceChildren(); if (elements.inboxQueue) elements.inboxQueue.replaceChildren(); const tasks = Array.isArray(entities) ? entities.filter((entity) => entity.kind === "tasks" && entity.archived !== true && safeFrontmatter(entity).status === "inbox") : []; inboxStageProjects = (allEntities || []).filter((entity) => entity.kind === "projects"); inboxStageAreas = (allEntities || []).filter((entity) => entity.kind === "areas");
  for (const entity of tasks) { elements.inboxList.append(makeInboxRow(entity)); if (elements.inboxQueue) { const card = document.createElement("li"), title = safeFrontmatter(entity).title || "タイトルなし", body = document.createElement("button"), grip = document.createElement("button"); card.className = "inbox-card"; card.dataset.taskId = entity.id; card._inboxTask = entity; body.type = "button"; body.className = "inbox-card-action"; body.textContent = title; body.addEventListener("click", () => stageInboxTask(entity, "inbox")); grip.type = "button"; grip.className = "inbox-drag-handle inbox-drag-grip"; grip.setAttribute("aria-label", "Taskをドラッグして整理: " + title); grip.setAttribute("title", "ドラッグして行き先を選ぶ"); window.TaskCard.appendDragGrip(grip); grip.addEventListener("pointerdown", (event) => beginInboxDrag(event, entity, card)); card.append(body, grip); elements.inboxQueue.append(card); } }
  elements.captureOpenEmpty.hidden = tasks.length !== 0;
  elements.inboxState.textContent = tasks.length ? tasks.length + "件あります。" : "Inboxは空です。";
}
async function loadInbox() {
  const generation = nextGeneration("inbox"); const resolving = applyOutcomeUnknown || canonicalReloadRequired; if (!resolving) clearMessage(elements.error); elements.inboxReload.disabled = true; elements.inboxState.textContent = "読み込み中です。";
  try {
    const resolution = await reconcileUnknownAttempt(); if (!isCurrentGeneration("inbox", generation)) return;
    const snapshot = await apiRequest("/api/v1/snapshot"); if (!isCurrentGeneration("inbox", generation)) return;
    if (!snapshot || !Array.isArray(snapshot.entities)) throw new RequestFailure("network"); renderInbox(snapshot.entities, snapshot.entities); renderInboxCount(snapshot); completeSuccessfulReload(resolution);
  } catch (error) { if (isCurrentGeneration("inbox", generation)) { showReloadFailure(error); elements.inboxState.textContent = "Inboxを読み込めませんでした。"; } }
  finally { if (isCurrentGeneration("inbox", generation)) elements.inboxReload.disabled = false; }
}
elements.captureForm.addEventListener("submit", async (event) => {
  event.preventDefault(); if (!elements.captureForm.reportValidity()) return;
  const operation = {action: "create", kind: "tasks", fields: {title: elements.captureTitle.value}, body: elements.captureBody.value};
  if (elements.captureDialog.open) elements.captureDialog.close();
  await previewMutation(operation, async (applied) => { elements.captureForm.reset(); showNotice("Inboxに保存しました: " + applied.path); }, elements.captureTitle, null, {reopenCapture: true});
});
elements.inboxReload.addEventListener("click", loadInbox);
if (elements.inboxStageSheet) {
  elements.inboxStageProject.addEventListener("change", syncInboxStageArea);
  elements.inboxStageEditTitle.addEventListener("click", editInboxStageTitle);
  elements.inboxStageCancel.addEventListener("click", clearInboxStage);
  elements.inboxStageSheet.addEventListener("submit", (event) => { event.preventDefault(); saveInboxStage(); });
}
function openCapture(trigger) {
  if (mutationIsGated() || elements.captureDialog.open) return;
  captureTrigger = trigger;
  elements.captureDialog.showModal(); elements.captureTitle.focus();
}
function closeCapture() {
  if (elements.captureDialog.open) elements.captureDialog.close();
  const trigger = captureTrigger; captureTrigger = null;
  if (trigger) trigger.focus();
}
for (const button of [elements.captureOpenDesktop, elements.captureOpenMobile, elements.captureOpenInbox, elements.captureOpenEmpty]) button.addEventListener("click", () => openCapture(button));
elements.captureCancel.addEventListener("click", closeCapture);
elements.captureDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeCapture(); });

function replaceOptions(select, entities, selected, emptyLabel) {
  select.replaceChildren(); const empty = document.createElement("option"); empty.value = ""; empty.textContent = emptyLabel; select.append(empty);
  for (const entity of entities) { const option = document.createElement("option"); option.value = entity.id; option.textContent = safeFrontmatter(entity).title || entity.id; select.append(option); }
  select.value = typeof selected === "string" ? selected : "";
}
function restoreDueInput(input, value) {
  const restored = typeof value === "string" ? value : "";
  input.setAttribute("type", !restored || /^\d{4}-\d{2}-\d{2}$/.test(restored) ? "date" : "text"); input.value = restored;
}
function restoreScheduledInput(input, value) {
  const restored = typeof value === "string" ? value : ""; const nativeValue = workSessionTimestamp(restored) ? restored : tokyoDateTimeLocal(restored);
  input.setAttribute("type", !restored || nativeValue ? "datetime-local" : "text"); input.value = nativeValue || restored;
}
function syncClarifyConditionalFields() {
  elements.clarifyActionDate.disabled = false;
  const state = taskSettings.syncClarifyConditions({status:elements.clarifyStatus.value,waiting_for:elements.clarifyWaiting.value,scheduled_start:elements.clarifyStart.value,scheduled_end:elements.clarifyEnd.value});
  setHidden(elements.clarifyWaitingField,!state.waitingVisible);setHidden(elements.clarifyScheduledFields,!state.scheduledVisible);
  if(clarifySettingsForm){clarifySettingsForm.session.update(readClarifyFormDraft(false));clarifySettingsForm.view.sync();}
}
function syncClarifyAdvancedDetails() {
  if(clarifySettingsForm){clarifySettingsForm.session.update(readClarifyFormDraft(false));clarifySettingsForm.view.sync();}
  syncClarifyConditionalFields();
}
function renderClarifyDependencies() {
  const selected = new Set(clarifyDependencyIds); const query = elements.clarifyDependencySearch.value.trim().toLocaleLowerCase("ja-JP");
  const candidates = clarifyDependencyTasks.filter((entity) => {
    const fm = safeFrontmatter(entity); const text = (fm.title || entity.id).toLocaleLowerCase("ja-JP");
    return (!query || text.includes(query) || entity.id.toLocaleLowerCase("ja-JP").includes(query));
  });
  elements.clarifyDependencyOptions.replaceChildren();
  for (const entity of candidates) {
    const label = document.createElement("label"); label.className = "dependency-option";
    const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = selected.has(entity.id); checkbox.value = entity.id;
    checkbox.addEventListener("change", () => { clarifyDependencyIds = checkbox.checked ? [...new Set([...clarifyDependencyIds, entity.id])] : clarifyDependencyIds.filter((id) => id !== entity.id); renderClarifyDependencies(); });
    const text = document.createElement("span"); text.textContent = dependencyLabel(entity, entity.id); label.append(checkbox, text); elements.clarifyDependencyOptions.append(label);
  }
  if (!candidates.length) { const empty = document.createElement("p"); empty.className = "muted"; empty.textContent = "選べるTaskはありません。"; elements.clarifyDependencyOptions.append(empty); }
  elements.clarifyDependencyChips.replaceChildren();
  const byId = new Map(clarifyDependencyTasks.map((entity) => [entity.id, entity]));
  for (const id of clarifyDependencyIds) {
    const chip = document.createElement("button"); chip.type = "button"; chip.className = "dependency-chip"; chip.textContent = dependencyLabel(byId.get(id), id) + "を解除"; chip.setAttribute("aria-label", dependencyLabel(byId.get(id), id) + "を前提Taskから解除");
    chip.addEventListener("click", () => { clarifyDependencyIds = clarifyDependencyIds.filter((value) => value !== id); renderClarifyDependencies(); }); elements.clarifyDependencyChips.append(chip);
  }
}
function setClarifyDependencies(detail, tasks, ids = dependencyIds(safeFrontmatter(detail).depends_on)) {
  const currentId = detail.id; clarifyDependencyTasks = tasks.filter((entity) => {
    const fm = safeFrontmatter(entity); const selected = ids.includes(entity.id);
    return entity.kind === "tasks" && entity.id !== currentId && !isBreakTask(entity) && (selected || (entity.archived !== true && fm.status !== "done"));
  });
  clarifyDependencyIds = [...new Set(ids)]; elements.clarifyDependencySearch.value = ""; renderClarifyDependencies();
}
function renderClarifyContinuations(detail, tasks) { clarifyContinuationTasks = tasks; window.ProjectTaskBoard.renderTaskContinuations({content: elements.clarifyContinuations, detail, snapshot: {entities: tasks}, safeFrontmatter, taskStatusLabel, clarifyHref}); }
function populateClarify(...args) { clarifyFormRuntime.populate(...args); renderClarifyContinuations(args[0], args[3]); }
function syncClarifyProjectDetailLink() { const link = elements.clarifyProjectLink; if (!link) return; const id = elements.clarifyProject.value; link.hidden = !id; link.setAttribute("href", "/projects?level=projects&id=" + encodeURIComponent(id)); }
function closeClarifyProjectDetail(updateHistory = true) { return P.closeProjectDetailSheet(elements, updateHistory); }
function syncClarifyAreaPicker(value = elements.clarifyArea.value) { clarifyFormRuntime.syncAreaPicker(value); }
const clarifyFormRuntime = window.TaskPanelEditor.createForm({elements, detail: () => clarifyDetail, dependencies: () => clarifyDependencyIds, frontmatter: safeFrontmatter, serializeDependencies: serializeDependencyIds, normalizeDraft: taskSettings.normalizeDraft, scheduledTimestamp: clarifyScheduledTimestamp, availableFromValue, mountForm: taskSettings.mountForm, setDetail: (value) => { clarifyDetail = value; }, replaceOptions, syncProjectLink: syncClarifyProjectDetailLink, setCollections: (projects, areas, directAreaId) => { clarifyProjects = projects; clarifyAreas = areas; clarifyDirectAreaId = directAreaId; }, collections: () => ({projects: clarifyProjects, areas: clarifyAreas, directAreaId: clarifyDirectAreaId}), setDirectArea: (value) => { clarifyDirectAreaId = value; }, restoreDue: restoreDueInput, restoreAvailable: restoreAvailableFrom, restoreScheduled: restoreScheduledInput, setDependencies: setClarifyDependencies, dependencyIds, setSettings: (value) => { clarifySettingsForm = value; }, syncAdvanced: syncClarifyAdvancedDetails, statuses: TASK_STATUSES, contextsForInput});
function captureClarifyDraft(){return clarifyFormRuntime.captureDraft();}
function readClarifyFormDraft(normalize=true){return clarifyFormRuntime.readDraft(normalize);}
function mountClarifySettings(detail){clarifySettingsForm=clarifyFormRuntime.mountSettings(detail);}
function restoreClarifyDraft(...args){clarifyFormRuntime.restoreDraft(...args);renderClarifyContinuations(args[0],args[3]);}
taskPanelEditor = window.TaskPanelEditor.create({elements, request: apiRequest, detailPath: (id) => entityDetailPath("tasks", id), captureModel: () => ({detail: clarifyDetail, draft: clarifyDetail ? captureClarifyDraft() : null, projects: clarifyProjects, areas: clarifyAreas, dependencies: clarifyDependencyTasks, continuationTasks: clarifyContinuationTasks, dependencyIds: [...clarifyDependencyIds], dependencySearch: elements.clarifyDependencySearch.value, directAreaId: clarifyDirectAreaId, settingsForm: clarifySettingsForm}), restoreModel: (previous) => { if (previous.detail && previous.draft) { restoreClarifyDraft(previous.detail, previous.projects, previous.areas, previous.dependencies, previous.draft, {linkRemoved: false}); clarifyDependencyIds = [...previous.dependencyIds]; elements.clarifyDependencySearch.value = previous.dependencySearch; renderClarifyDependencies(); } else { clarifyDetail = previous.detail; clarifyProjects = previous.projects; clarifyAreas = previous.areas; clarifyDependencyTasks = previous.dependencies; clarifyDependencyIds = [...previous.dependencyIds]; clarifyDirectAreaId = previous.directAreaId; clarifySettingsForm = previous.settingsForm; } if (previous.detail) renderClarifyContinuations(previous.detail, previous.continuationTasks); }, populate: (detail, source) => { const entities = source.entities; populateClarify(detail, entities.filter((entity) => entity.kind === "projects"), entities.filter((entity) => entity.kind === "areas"), entities.filter((entity) => entity.kind === "tasks")); }, accept: (fresh, snapshot, session) => P.updateTaskDetailSheet(fresh, snapshot, session), close: (session) => P.closeTaskEditSheet(elements, true, session)});
function mountTaskPanelEditor(options) { return taskPanelEditor.mount(options); }
async function loadClarify() {
  const generation = nextGeneration("clarify"); const resolving = applyOutcomeUnknown || canonicalReloadRequired; if (!resolving) clearMessage(elements.error); elements.clarifyReload.disabled = true;
  const id = new URLSearchParams(location.search || "").get("id");
  const draft = applyOutcomeUnknown && unknownAttempt && unknownAttempt.preview && unknownAttempt.preview.operation &&
    unknownAttempt.preview.operation.action === "update" && unknownAttempt.preview.operation.kind === "tasks" ? captureClarifyDraft() : null;
  if (!id) { if (isCurrentGeneration("clarify", generation)) { elements.clarifyState.textContent = "Taskが指定されていません。"; elements.clarifyForm.hidden = true; elements.clarifyReload.disabled = false; } return; }
  try {
    const resolution = await reconcileUnknownAttempt(); if (!isCurrentGeneration("clarify", generation)) return;
    const detail = resolution && resolution.detail ? resolution.detail : await apiRequest(entityDetailPath("tasks", id)); if (!isCurrentGeneration("clarify", generation)) return;
    const snapshot = await apiRequest("/api/v1/snapshot"); if (!isCurrentGeneration("clarify", generation)) return;
    if (detail.id !== id || detail.kind !== "tasks") throw new RequestFailure("reconciliation");
    if (detail.archived === true) {
      clarifyArchived = true; clarifyDetail = null; elements.clarifyForm.hidden = true;
      if (resolution && resolution.outcome === "applied" && resolution.operation && resolution.operation.action === "archive" && resolution.operation.kind === "tasks") { navigateClarifyReturn(); return; }
      window.ArchivedTaskDetail.render(detail, elements.clarifyState, clarifyReturnPath());
    }
    else {
      clarifyArchived = false; const projects = snapshot.entities.filter((entity) => entity.kind === "projects"); const areas = snapshot.entities.filter((entity) => entity.kind === "areas");
      const tasks = snapshot.entities.filter((entity) => entity.kind === "tasks");
      if (resolution && resolution.outcome === "not_applied" && resolution.operation.action === "update" && draft) restoreClarifyDraft(detail, projects, areas, tasks, draft, resolution);
      else populateClarify(detail, projects, areas, tasks); syncClarifyProjectDetailLink();
      elements.clarifyForm.hidden = false;
    }
    renderInboxCount(snapshot); completeSuccessfulReload(resolution);
  } catch (error) {
    if (isCurrentGeneration("clarify", generation)) { showReloadFailure(error); elements.clarifyState.textContent = "Taskを読み込めませんでした。"; }
  }
  finally { if (isCurrentGeneration("clarify", generation)) elements.clarifyReload.disabled = false; }
}
function taskFields() {
  return clarifySettingsForm.serialize().fields;
}
elements.clarifyProject.addEventListener("change", () => { syncClarifyAreaPicker(); syncClarifyProjectDetailLink(); });
if (elements.clarifyProjectLink) elements.clarifyProjectLink.addEventListener("click",(event)=>detailPanels.openProject(event,elements.clarifyProject.value));
elements.clarifyArea.addEventListener("change", () => { if (!elements.clarifyArea.disabled) clarifyDirectAreaId = elements.clarifyArea.value; });
elements.clarifyStatus.addEventListener("change", syncClarifyConditionalFields);
for (const input of [elements.clarifyWaiting, elements.clarifyStart, elements.clarifyEnd]) input.addEventListener("input", syncClarifyConditionalFields);
if (elements.clarifyDependencySearch) elements.clarifyDependencySearch.addEventListener("input", renderClarifyDependencies);
elements.clarifyForm.addEventListener("submit", async (event) => {
  event.preventDefault();if(!clarifyDetail||!taskSettings.guardActionDateSubmit(elements.clarifyStatus.value,elements.clarifyActionDate,elements.clarifyForm)||!elements.clarifyForm.reportValidity())return;let fields;
  try { fields = taskFields(); } catch (_error) { const message = "Contextsは空白を含まない名前をカンマで区切ってください。"; elements.error.textContent = message; setHidden(elements.error, false); const panel = taskPanelEditor.current(clarifyDetail.id); if (panel) taskPanelEditor.failure(panel, message); return; }
  const panel = taskPanelEditor.current(clarifyDetail.id), operation = {action: "update", kind: "tasks", id: clarifyDetail.id, base_hash: clarifyDetail.content_hash, fields, body: elements.clarifyBody.value};
  await previewMutation(operation, async (applied) => { showNotice("Taskを保存しました: " + applied.path); }, elements.clarifyTitle, null, panel ? {
    ...taskPanelEditor.mutationOptions(panel, operation),
  } : {});
});
elements.taskArchive.addEventListener("click", async () => {
  if (!clarifyDetail) return; const operation = {action: "archive", kind: "tasks", id: clarifyDetail.id, base_hash: clarifyDetail.content_hash};
  await previewMutation(operation, async () => { clarifyArchived = true; }, elements.clarifyTitle, null, {redirectTo: clarifyReturnPath()});
});
elements.clarifyReload.addEventListener("click", loadClarify);

function snapshotPath(keys, controls) {
  const params = new URLSearchParams();
  for (const key of keys) { const value = controls[key].value.trim(); if (value) params.set(key, value); }
  const query = params.toString(); return "/api/v1/snapshot" + (query ? "?" + query : "");
}
function clearDynamicMutationButtons() {
  for (const button of dynamicMutationButtons.splice(0)) { const index = mutationButtons.indexOf(button); if (index >= 0) mutationButtons.splice(index, 1); }
}
function parsedTimestamp(value) {
  if (typeof value !== "string") return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-](\d{2}):(\d{2}))$/.exec(value); if (!match) return null;
  const year = Number(match[1]); const month = Number(match[2]); const day = Number(match[3]); const hour = Number(match[4]); const minute = Number(match[5]); const second = Number(match[6]);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0); const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1] || hour > 23 || minute > 59 || second > 59) return null;
  if (match[7] !== "Z" && (Number(match[8]) > 23 || Number(match[9]) > 59)) return null;
  const date = new Date(value); return Number.isFinite(date.getTime()) ? date : null;
}
function formatWorkTimestamp(value) { const date = parsedTimestamp(value); return date ? date.toLocaleString("ja-JP") : "記録なし"; }
function formatElapsed(milliseconds) {
  const total = Math.max(0, Math.floor(milliseconds / 1000)); const hours = Math.floor(total / 3600); const minutes = Math.floor((total % 3600) / 60); const seconds = total % 60;
  return (hours ? hours + "時間 " : "") + minutes + "分 " + seconds + "秒";
}
function elapsedFor(entity, current) {
  const fm = safeFrontmatter(entity); const start = parsedTimestamp(fm.work_started_at); const end = current ? new Date() : parsedTimestamp(fm.work_ended_at);
  if (!start || !end || end.getTime() < start.getTime()) return null; return end.getTime() - start.getTime();
}
function isBreakTask(entity) { return safeFrontmatter(entity).timer_kind === "break"; }
function activeNotificationPause() {
  if (!notificationPause || !currentDoingTask || isBreakTask(currentDoingTask) || notificationPause.task_id !== currentDoingTask.id) return null;
  const deadline = parsedTimestamp(notificationPause.deadline); return deadline && deadline.getTime() > new Date().getTime() ? deadline : null;
}
function notificationPauseLabel(deadline) {
  return deadline ? "通知OFF " + new Intl.DateTimeFormat("ja-JP", {timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit", hour12: false}).format(deadline) + "まで" : "通知OFF";
}
function updateNotificationPauseView() {
  const deadline = activeNotificationPause(); const visible = Boolean(currentDoingTask && !isBreakTask(currentDoingTask));
  setHidden(elements.notificationPause, !visible); elements.notificationPause.textContent = notificationPauseLabel(deadline);
  elements.notificationPauseDeadline.textContent = deadline ? "現在の停止期限: " + new Intl.DateTimeFormat("ja-JP", {timeZone: "Asia/Tokyo", hour: "2-digit", minute: "2-digit", hour12: false}).format(deadline) + "まで" : "現在、通知は停止していません。";
  setHidden(elements.notificationPauseResume, !deadline);
  if (!deadline) clearNotificationPauseTimer();
}
function clearNotificationPauseTimer() { if (notificationPauseInterval !== null) { clearInterval(notificationPauseInterval); notificationPauseInterval = null; } }
function renderNotificationPause(facts) {
  notificationPause = facts && facts.focus_notification_pause && typeof facts.focus_notification_pause === "object" ? facts.focus_notification_pause : null;
  clearNotificationPauseTimer(); updateNotificationPauseView();
  if (activeNotificationPause()) notificationPauseInterval = setInterval(updateNotificationPauseView, 1000);
}
function closeNotificationPauseDialog(restoreFocus = true) {
  if (elements.notificationPauseDialog.open) elements.notificationPauseDialog.close();
  if (restoreFocus && notificationPauseTrigger) notificationPauseTrigger.focus();
}
function openNotificationPauseDialog() {
  if (!currentDoingTask || isBreakTask(currentDoingTask) || mutationIsGated()) return;
  notificationPauseTrigger = elements.notificationPause; elements.notificationPauseMinutes.value = "30"; updateNotificationPauseView();
  elements.notificationPauseDialog.showModal(); elements.notificationPauseMinutes.focus();
}
async function previewNotificationPause(action) {
  if (!currentDoingTask || isBreakTask(currentDoingTask) || mutationIsGated()) return;
  const operation = {action, kind: "tasks", id: currentDoingTask.id, base_hash: currentDoingTask.content_hash};
  if (action === "pause_notifications") { const minutes = Number(elements.notificationPauseMinutes.value); if (!Number.isInteger(minutes) || minutes < 1 || minutes > 1440) { elements.notificationPauseMinutes.focus(); return; } operation.minutes = minutes; }
  closeNotificationPauseDialog(false); await previewMutation(operation, async () => showNotice(action === "pause_notifications" ? "通知を停止しました。" : "通知を再開しました。"), notificationPauseTrigger);
}
function breakRemaining(entity) {
  const end = parsedTimestamp(safeFrontmatter(entity).timer_ends_at); if (!end) return null;
  return Math.max(0, end.getTime() - new Date().getTime());
}
function formatBreakRemaining(milliseconds) {
  const total = Math.max(0, Math.ceil(milliseconds / 1000));
  return String(Math.floor(total / 60)).padStart(2, "0") + ":" + String(total % 60).padStart(2, "0");
}
async function readBreakCompletion(entity) {
  if (breakCompletionReadInFlight) return;
  breakCompletionReadInFlight = true;
  try {
    const detail = await apiRequest(entityDetailPath("tasks", entity.id));
    if (detail && detail.id === entity.id && safeFrontmatter(detail).status === "done") {
      showNotice("5分休憩が完了しました。"); await loadFocus();
    }
  } catch (error) { showRequestError(error); }
  finally { breakCompletionReadInFlight = false; }
}
function taskCard(entity, actions, projects, current = false, completed = false, tasks = []) {
  const card = makeTaskRow(entity, actions, projects, current ? "current-task-card" : ""); const fm = safeFrontmatter(entity);
  detailPanels.t(card,entity,loadFocus);
  const dependency = taskDependencyProgress(entity, tasks);
  if (dependency.total) {
    const status = document.createElement("p"); status.className = "dependency-status"; status.textContent = "依存 " + dependency.complete + "/" + dependency.total + "完了" + (dependency.unfinished.length ? " / 未完了: " + dependency.unfinished.join("、") : ""); card.append(status);
  }
  const startBlockReason = taskStartBlockReason(entity, tasks);
  if (startBlockReason) { const reason = document.createElement("p"); reason.className = "dependency-status"; reason.textContent = startBlockReason; card.append(reason); }
  if (current) {
    const statusChip = document.createElement("p"); statusChip.className = "status-chip"; statusChip.textContent = isBreakTask(entity) ? "休憩中" : "実行中"; card.append(statusChip);
    if (isBreakTask(entity)) {
      const countdown = document.createElement("p"); countdown.className = "break-countdown";
      const update = () => {
        const remaining = breakRemaining(entity);
        if (remaining === null) { countdown.textContent = "終了時刻を確認できません"; return; }
        if (remaining > 0 && !breakCompletionPending) { countdown.textContent = formatBreakRemaining(remaining); return; }
        breakCompletionPending = true; countdown.textContent = "終了処理中"; readBreakCompletion(entity);
      };
      update(); card.append(countdown); return {card, update};
    }
    const started = document.createElement("p"); started.className = "availability-note"; started.textContent = "開始: " + formatWorkTimestamp(fm.work_started_at);
    const elapsed = document.createElement("p"); elapsed.className = "elapsed-time"; const update = () => { const duration = elapsedFor(entity, true); elapsed.textContent = "経過: " + (duration === null ? "記録なし" : formatElapsed(duration)); }; update(); card.append(started, elapsed); return {card, update};
  }
  if (completed) {
    const duration = elapsedFor(entity, false); const record = document.createElement("p"); record.className = "availability-note";
    record.textContent = duration === null ? "作業記録: 記録なし" : "開始: " + formatWorkTimestamp(fm.work_started_at) + " / 終了: " + formatWorkTimestamp(fm.work_ended_at) + " / 所要: " + formatElapsed(duration); card.append(record);
  }
  return {card, update: null};
}
function currentTaskProject(entity, projects) {
  const fm = safeFrontmatter(entity), projectId = fm.project_id;
  if (!projectId) return null;
  const project = projects.get(projectId), projectFm = safeFrontmatter(project);
  return {id: projectId, title: projectFm.title || projectId, href: "/projects?level=projects&id=" + encodeURIComponent(projectId), areaId: projectFm.area_id || ""};
}
function appendCurrentTaskArea(card, entity, areas) {
  const fm = safeFrontmatter(entity); if (fm.project_id || isBreakTask(entity)) return;
  const field = document.createElement("label"), select = document.createElement("select"); field.className = "current-task-area"; field.textContent = "Area"; select.dataset.currentTaskArea = "true";
  const currentAreaUi = window.AllocationUI; if (!currentAreaUi || !currentAreaUi.configureAreaSelect) return;
  const previous = fm.area_id || "", options = areas.map((area) => ({id: area.id, title: safeFrontmatter(area).title || area.id}));
  currentAreaUi.configureAreaSelect({select, areas: options, value: previous, onChange: async (value) => {
    if (mutationIsGated()) { select.value = previous; return; }
    const operation = {action: "update", kind: "tasks", id: entity.id, base_hash: entity.content_hash, fields: {area_id: value}};
    await previewMutation(operation, async () => {}, select, null, {awaitApply: true, onFailure: () => { select.value = previous; }, postReloadFocus: () => elements.tasksCurrentList.querySelector("[data-current-task-area='true']")});
  }});
  const empty = select.options ? select.options[0] : select.children[0]; if (empty && empty.value === "") empty.textContent = "指定なし";
  field.append(select); card.append(field);
}
function currentTaskCard(entity, actions, projects, areas) {
  const fm = safeFrontmatter(entity), project = isBreakTask(entity) ? null : currentTaskProject(entity, projects);
  const card = window.TaskCard.createTaskCard({task: entity, href: clarifyHref(entity.id), metadata: isBreakTask(entity) ? [] : taskMeta(entity, projects, false), project: project ? {id: project.id, title: project.title, href: project.href} : null, actions});
  detailPanels.t(card,entity,loadFocus);
  card.className += " current-task-card";
  if (project) {
    const area = areas.find((candidate) => candidate.id === project.areaId), inherited = area ? (safeFrontmatter(area).title || area.id) : (project.areaId || "未設定");
    const note = document.createElement("p"); note.className = "current-task-inherited-area"; note.textContent = "Project由来のArea: " + inherited; card.append(note);
  } else appendCurrentTaskArea(card, entity, areas);
  if (isBreakTask(entity)) {
    const countdown = document.createElement("p"); countdown.className = "break-countdown";
    const update = () => {
      const remaining = breakRemaining(entity);
      if (remaining === null) { countdown.textContent = "終了時刻を確認できません"; return; }
      if (remaining > 0 && !breakCompletionPending) { countdown.textContent = formatBreakRemaining(remaining); return; }
      breakCompletionPending = true; countdown.textContent = "終了処理中"; readBreakCompletion(entity);
    };
    update(); card.append(countdown); return {card, update};
  }
  const timing = document.createElement("div"), started = document.createElement("p"), elapsed = document.createElement("p"); timing.className = "current-task-timing"; started.className = "availability-note"; started.textContent = "開始: " + formatWorkTimestamp(fm.work_started_at); elapsed.className = "elapsed-time";
  const update = () => { const duration = elapsedFor(entity, true); elapsed.textContent = "経過: " + (duration === null ? "記録なし" : formatElapsed(duration)); };
  update(); timing.append(started, elapsed); card.append(timing); return {card, update};
}
function beginTaskPreparation() {
  if (mutationIsGated()) return null; mutationPreparationInFlight = true; taskPreparationGeneration += 1; setMutationDisabled(true); return taskPreparationGeneration;
}
function finishTaskPreparation(token) { if (taskPreparationGeneration !== token) return; mutationPreparationInFlight = false; releaseMutationControlsIfIdle(); }
function resetTaskSwitchCopy() {
  elements.switchInterrupt.textContent = "中断して切替"; elements.switchComplete.textContent = "完了して切替";
}
function openTaskSwitch(pending) {
  pendingTaskSwitch = pending;
  const title = safeFrontmatter(pending.active).title || pending.active.id;
  window.FocusCompleted.configureTaskSwitchTime(elements.switchTime, pending.mode, pending.active, new Date());
  if (pending.mode === "start_break") {
    elements.switchMessage.textContent = "現在「" + title + "」が実行中です。5分休憩の前にどうしますか？";
    elements.switchInterrupt.textContent = "中断して休憩"; elements.switchComplete.textContent = "完了して休憩";
  } else if (pending.mode === "create_and_start") {
    elements.switchMessage.textContent = "現在「" + title + "」が実行中です。「" + pending.title + "」を開始する前にどうしますか？";
    elements.switchInterrupt.textContent = "中断して開始"; elements.switchComplete.textContent = "完了して開始";
  } else {
    resetTaskSwitchCopy();
    elements.switchMessage.textContent = "現在「" + title + "」が実行中です。どの方法で切り替えるか選んでください。";
  }
  const interruptAllowed = !(pending.mode === "create_and_start" && isBreakTask(pending.active));
  elements.switchInterrupt.disabled = !interruptAllowed; elements.switchComplete.disabled = false; elements.switchCancel.disabled = false;
  elements.switchDialog.showModal(); (interruptAllowed ? elements.switchInterrupt : elements.switchComplete).focus();
}
function closeTaskSwitch(returnFocus = true) {
  const pending = pendingTaskSwitch; pendingTaskSwitch = null; if (elements.switchDialog.open) elements.switchDialog.close();
  resetTaskSwitchCopy(); elements.switchInterrupt.disabled = false; elements.switchComplete.disabled = false; elements.switchCancel.disabled = false;
  if (pending) { finishTaskPreparation(pending.token); if (returnFocus && pending.focus) pending.focus.focus(); }
}
function invalidateTaskPreparation() {
  taskPreparationGeneration += 1; mutationPreparationInFlight = false; pendingTaskSwitch = null; resetTaskSwitchCopy(); elements.switchInterrupt.disabled = false; elements.switchComplete.disabled = false; elements.switchCancel.disabled = false; if (elements.switchDialog.open) elements.switchDialog.close();
}
function workflowSuccess(action, applied) {
  const effects = applied && Array.isArray(applied.effects) ? applied.effects : []; const count = effects.length; const labels = {start: "開始", interrupt: "中断", complete: "完了"};
  const released = effects.filter((effect) => effect && effect.role === "dependency_released").length; const retargeted = effects.filter((effect) => effect && effect.role === "dependency_retargeted").length;
  const dependencySummary = released || retargeted ? "（解除 " + released + "件・付替 " + retargeted + "件）" : "";
  showNotice("Taskの" + (labels[action] || "変更") + "を保存しました（" + count + "件更新）" + dependencySummary + "。");
}
function reloadFocusOnceAfterConflict(error) {
  const payload = error && error.payload && error.payload.error; const code = payload && String(payload.code || "").toLowerCase();
  if ((code === "409" || code === "conflict" || code === "stale" || code === "stale_preview") && reloadCurrentRoute === loadFocus && !focusConflictReloadInFlight) {
    focusConflictReloadInFlight = true; void loadFocus().finally(() => { focusConflictReloadInFlight = false; });
  }
}
async function prepareTaskWorkflow(action, entity, focus, routeName) {
  const token = beginTaskPreparation(); if (token === null) return; const routeGeneration = loadGenerations[routeName]; let switchOpened = false;
  try {
    const focusRoute = routeName === "tasks";
    const startFromRenderedFocus = focusRoute && action === "start";
    const detail = startFromRenderedFocus ? entity : await apiRequest(entityDetailPath("tasks", entity.id));
    if (taskPreparationGeneration !== token || loadGenerations[routeName] !== routeGeneration) return;
    const activeTask = action === "start" ? (startFromRenderedFocus ? currentDoingTask : await apiRequest("/api/v1/snapshot").then((fresh) => {
      if (!fresh || !Array.isArray(fresh.entities)) throw new RequestFailure("reconciliation");
      return fresh.entities.find((candidate) => candidate && candidate.kind === "tasks" && candidate.archived !== true && safeFrontmatter(candidate).status === "doing") || null;
    })) : null;
    if (action === "start" && !focusRoute) currentDoingTask = activeTask;
    if (taskPreparationGeneration !== token || loadGenerations[routeName] !== routeGeneration) return;
    if (action === "start" && activeTask && activeTask.id !== detail.id) {
      switchOpened = true; openTaskSwitch({mode: "start", target: detail, active: activeTask, focus, token, routeName, routeGeneration}); return;
    }
    const operation = {action, kind: "tasks", id: detail.id, base_hash: detail.content_hash};
    await previewMutation(operation, async (applied) => { if (focusRoute) applyFocusCanonicalEffects(applied); workflowSuccess(action, applied); }, focus, token, focusRoute ? {backgroundReload: true, onFailure: reloadFocusOnceAfterConflict} : {});
  } catch (error) { if (taskPreparationGeneration === token && loadGenerations[routeName] === routeGeneration) showRequestError(error); }
  finally { if (!switchOpened) finishTaskPreparation(token); }
}
async function chooseTaskSwitch(action) {
  const pending = pendingTaskSwitch; if (!pending || taskPreparationGeneration !== pending.token) return;
  const checkedTime = window.FocusCompleted.readTaskSwitchTime(elements.switchTime, pending.active); if (checkedTime.error) return;
  elements.switchInterrupt.disabled = true; elements.switchComplete.disabled = true; elements.switchCancel.disabled = true; if (elements.switchDialog.open) elements.switchDialog.close();
  const activeResolution = {id: pending.active.id, base_hash: pending.active.content_hash, action}; if (checkedTime.work_ended_at) activeResolution.work_ended_at = checkedTime.work_ended_at;
  const operation = pending.mode === "start_break" ? {action: "start_break", kind: "tasks",
    active_resolution: activeResolution} :
    pending.mode === "create_and_start" ? {action: "create_and_start", kind: "tasks", fields: {title: pending.title, area_id: pending.area_id || ""}, body: "",
      active_resolution: activeResolution} :
    {action: "start", kind: "tasks", id: pending.target.id, base_hash: pending.target.content_hash,
      active_resolution: activeResolution};
  pendingTaskSwitch = null;
  try { await previewMutation(operation, async (applied) => {
    applyFocusCanonicalEffects(applied);
    if (pending.mode === "start_break") showNotice("5分休憩を開始しました（" + (applied && Array.isArray(applied.effects) ? applied.effects.length : 0) + "件更新）。");
    else { if (pending.mode === "create_and_start") { recordQuickStartSuccess(pending.area_id || ""); resetQuickStart(); } workflowSuccess("start", applied); }
  }, pending.focus, pending.token, {backgroundReload: true, onFailure: reloadFocusOnceAfterConflict}); }
  finally { finishTaskPreparation(pending.token); resetTaskSwitchCopy(); elements.switchInterrupt.disabled = false; elements.switchComplete.disabled = false; elements.switchCancel.disabled = false; }
}
elements.switchInterrupt.addEventListener("click", () => chooseTaskSwitch("interrupt"));
elements.switchComplete.addEventListener("click", () => chooseTaskSwitch("complete"));
elements.switchCancel.addEventListener("click", () => closeTaskSwitch(true));
elements.switchDialog.addEventListener("cancel", (event) => { if (applyInFlight) { event.preventDefault(); return; } closeTaskSwitch(true); });

function renderFilterChips() {
  elements.tasksChips.replaceChildren();
  const controls = [
    [elements.tasksContext, "Context"], [elements.tasksProject, "Project"], [elements.tasksArea, "Area"],
    [elements.tasksMinutes, "分以内"], [elements.tasksDue, "期限"],
  ];
  for (const [control, label] of controls) {
    if (!control.value.trim()) continue;
    const chip = actionButton(label + ": " + control.value + " ×", async () => { control.value = ""; await loadFocus(); });
    chip.className = "filter-chip"; elements.tasksChips.append(chip);
  }
  if (focusStatus) { const status = FOCUS_STATUS_OPTIONS.find((option) => option.value === focusStatus); const chip = actionButton("状態: " + status.label + " ×", async () => { focusStatus = ""; renderFocusStatus(); await loadFocus(); }); chip.className = "filter-chip"; elements.tasksChips.append(chip); }
}
const FOCUS_STATUS_OPTIONS = Object.freeze([
  {value: "", label: "次にやる＋予定", button: "focusStatusCandidates"},
  {value: "next", label: "次にやる", button: "focusStatusNext"},
  {value: "scheduled", label: "予定", button: "focusStatusScheduled"},
  {value: "waiting", label: "待機", button: "focusStatusWaiting"},
  {value: "someday", label: "いつかやる", button: "focusStatusSomeday"},
]);
function renderFocusStatus() {
  for (const option of FOCUS_STATUS_OPTIONS) elements[option.button].setAttribute("aria-pressed", String(option.value === focusStatus));
  elements.tasksIncompleteHeading.textContent = FOCUS_STATUS_OPTIONS.find((option) => option.value === focusStatus).label;
}
function renderFocusFilterSummary() {
  const active = [focusStatus, elements.tasksContext.value, elements.tasksProject.value, elements.tasksArea.value, elements.tasksMinutes.value, elements.tasksDue.value].filter((value) => String(value || "").trim()).length;
  elements.focusFilterSummary.textContent = "絞り込み（" + active + "件）";
}
async function selectFocusStatus(status) {
  focusStatus = status;
  renderFocusStatus(); renderFocusFilterSummary(); await loadFocus();
}
const workSessionTimestamp = window.FocusCompleted.workSessionTimestamp;
const tokyoDateTimeLocal = (value) => window.FocusCompleted.tokyoDateTimeLocal(value, parsedTimestamp);
function clarifyScheduledTimestamp(value) { const trimmed = value.trim(); return trimmed ? workSessionTimestamp(trimmed) || trimmed : ""; }
function setWorkSessionError(element, message) { element.textContent = message; setHidden(element, !message); }
function validateWorkSession() { return window.FocusCompleted.validateWorkSession(elements, workSessionDetail, parsedTimestamp, safeFrontmatter, setHidden); }
function closeWorkSessionDialog(returnFocus = true) {
  if (elements.workSessionDialog.open) elements.workSessionDialog.close();
  if (returnFocus && workSessionTrigger) workSessionTrigger.focus();
}
async function openWorkSessionEditor(entity, trigger) {
  if (mutationIsGated()) return;
  try {
    const detail = await apiRequest(entityDetailPath("tasks", entity.id)); const fm = safeFrontmatter(detail);
    if (detail.id !== entity.id || detail.kind !== "tasks" || detail.archived === true || fm.status !== "done" || fm.timer_kind === "break" || !parsedTimestamp(fm.work_started_at) || !parsedTimestamp(fm.work_ended_at)) throw new RequestFailure("reconciliation");
    workSessionDetail = detail; workSessionTrigger = trigger; elements.workSessionStart.value = tokyoDateTimeLocal(fm.work_started_at); elements.workSessionEnd.value = tokyoDateTimeLocal(fm.work_ended_at);
    setWorkSessionError(elements.workSessionStartError, ""); setWorkSessionError(elements.workSessionEndError, ""); elements.workSessionDialog.showModal(); elements.workSessionStart.focus();
  } catch (error) { showRequestError(error); trigger.focus(); }
}
function workSessionEligible(entity) {
  const fm = safeFrontmatter(entity); return entity && entity.kind === "tasks" && entity.archived !== true && fm.status === "done" && fm.timer_kind !== "break" && parsedTimestamp(fm.work_started_at) && parsedTimestamp(fm.work_ended_at);
}
for (const input of [elements.workSessionStart, elements.workSessionEnd]) input.addEventListener("blur", validateWorkSession);
elements.workSessionCancel.addEventListener("click", () => closeWorkSessionDialog());
elements.workSessionDialog.addEventListener("cancel", () => closeWorkSessionDialog());
elements.workSessionForm.addEventListener("submit", async (event) => {
  event.preventDefault(); if (!workSessionDetail || mutationIsGated()) return; const values = validateWorkSession(); if (!values) { (elements.workSessionStartError.hidden ? elements.workSessionEnd : elements.workSessionStart).focus(); return; }
  const operation = {action: "correct_work_session", kind: "tasks", id: workSessionDetail.id, base_hash: workSessionDetail.content_hash, ...values};
  const taskId = workSessionDetail.id;
  await previewMutation(operation, async (applied) => { closeWorkSessionDialog(false); showNotice("実績時刻を保存しました: " + applied.path); }, workSessionTrigger, null, {failureFocus: () => elements.workSessionDialog.open ? elements.workSessionStart : null, postReloadFocus: () => elements.tasksCompletedList.querySelector('[data-work-session-task-id="' + taskId + '"]')});
  if (activePreview) closeWorkSessionDialog(false);
});
function appendEmptyState(list, text) { const item = document.createElement("li"); item.className = "muted"; item.textContent = text; list.append(item); }
function focusToday(facts) { return facts && typeof facts.gtd_today === "string" ? facts.gtd_today : tokyoToday(); }
function focusSectionList(key) { return ({today: elements.focusSectionTodayList, dated: elements.focusSectionDatedList, undated: elements.focusSectionUndatedList})[key]; }
function focusSectionButton(key) { return ({today: elements.focusSectionToday, dated: elements.focusSectionDated, undated: elements.focusSectionUndated})[key]; }
function saveFocusCollapse() { try { localStorage.setItem(FOCUS_COLLAPSE_STORAGE_KEY, JSON.stringify(focusCollapsed)); } catch (_error) {} }
function restoreFocusCollapse() { try { const saved = JSON.parse(localStorage.getItem(FOCUS_COLLAPSE_STORAGE_KEY) || "{}"); focusCollapsed = saved && typeof saved === "object" ? saved : {}; } catch (_error) { focusCollapsed = {}; } }
function focusBoardCards() { return [elements.focusSectionTodayList, elements.focusSectionDatedList, elements.focusSectionUndatedList].flatMap((list) => [...list.querySelectorAll(".task-card")]); }
function setArchiveTarget(hidden){if(archiveDrop){archiveDrop.hidden=hidden;archiveDrop.classList.remove("focus-drop-target");}}
let focusDragDock = null;
function clearFocusDragDock() { ProjectKanbanMotion.clearDestinationDock(focusDragDock); focusDragDock = null; }
function clearFocusDropTargets(){for(const key of ["today","dated","undated"]){const section=focusSectionButton(key).closest(".focus-action-section");if(section)section.classList.remove("focus-drop-target");}if(archiveDrop)archiveDrop.classList.remove("focus-drop-target");}
function resolveFocusDropSection(event){const destination=ProjectKanbanMotion.destinationAt(focusDragDock,event);if(destination)return destination;const hit=typeof document.elementFromPoint==="function"?document.elementFromPoint(event.clientX,event.clientY):null;if(hit&&hit.closest&&hit.closest("#focus-archive-drop-target")===archiveDrop&&archiveDrop.getBoundingClientRect().bottom<=(window.innerHeight||0)-56)return "archive";const exact=hit&&hit.closest?hit.closest(".focus-action-section"):null;if(exact&&exact.dataset.focusSection)return exact.dataset.focusSection;let nearest=null,distance=Number.POSITIVE_INFINITY;for(const key of ["today","dated","undated"]){const section=focusSectionButton(key).closest(".focus-action-section");if(!section)continue;const rect=section.getBoundingClientRect(),current=Math.abs(event.clientY-(rect.top+rect.height/2));if(current<distance){nearest=key;distance=current;}}return nearest;}
function emphasizeFocusDropTarget(event){clearFocusDropTargets();const key=resolveFocusDropSection(event);if(key === "archive" && archiveDrop){archiveDrop.classList.add("focus-drop-target");return;}const section=key&&focusSectionButton(key).closest(".focus-action-section");if(section)section.classList.add("focus-drop-target");}
function optimisticFocusMove(entity, changes) {
  const card = focusBoardCards().find((value) => value.dataset.taskId === entity.id); if (!card) return () => {};
  const fields = safeFrontmatter(entity); if (!changes) return () => {};
  const restoreFields = window.TaskCard.applyFocusMove(fields, changes);
  const key = window.TaskCard.classifyFocusTask(entity, focusBoardToday || tokyoToday()), target = focusSectionList(key), restoreCard = window.TaskCard.optimisticRelocate(card, target, window.ProjectKanbanMotion, focusBoardCards());
  card.classList.add("task-card-moving"); setTimeout(() => card.classList.remove("task-card-moving"), 180);
  return () => { restoreFields(); restoreCard(); card.classList.remove("task-card-moving"); showNotice("保存に失敗したため、元の位置へ戻しました。"); };
}
async function moveFocusTask(entity, actionDate, focus) {
  if (mutationIsGated()) return; let restore = null;
  try {
    const detail = await apiRequest(entityDetailPath("tasks", entity.id)), changes = window.TaskCard.focusMoveFields(detail, actionDate); if (!changes || Object.entries(changes).every(([key, value]) => (safeFrontmatter(detail)[key] || "") === value)) return;
    restore = optimisticFocusMove(entity, changes); const operation = {action: "update", kind: "tasks", id: detail.id, base_hash: detail.content_hash, fields: changes};
    await previewMutation(operation, () => showNotice("対応予定日を保存しました。"), focus, null, {onFailure: restore});
  } catch (error) { if (restore) restore(); showRequestError(error); if (focus) focus.focus(); }
}
async function archiveFocusTask(entity,focus){try{const detail=await apiRequest(entityDetailPath("tasks",entity.id));const operation={action:"archive",kind:"tasks",id:detail.id,base_hash:detail.content_hash};await previewMutation(operation,(applied)=>showNotice("Taskをアーカイブしました: "+applied.path),focus);}catch(error){showRequestError(error);if(focus)focus.focus();}}
function tomorrowDate(today) { return window.TaskCard.nextIsoDate(today); }
function showFocusDateChoice() { elements.focusMoveDateField.hidden = false; elements.focusMoveDate.focus(); if (typeof elements.focusMoveDate.showPicker === "function") { try { elements.focusMoveDate.showPicker(); } catch (_error) {} } }
function closeFocusMoveDialog(returnFocus = true) { const context = focusMoveContext; focusMoveContext = null; if (elements.focusMoveDialog.open) elements.focusMoveDialog.close(); elements.focusMoveDateField.hidden = true; if (returnFocus && context && context.trigger) context.trigger.focus(); }
function openFocusMoveMenu(entity, trigger, today, dateOnly = false) {
  if (mutationIsGated()) return; focusMoveContext = {entity, trigger, today}; elements.focusMoveDate.value = safeFrontmatter(entity).action_date || tomorrowDate(today); elements.focusMoveDateField.hidden = true; elements.focusMoveDialog.showModal(); if (dateOnly) showFocusDateChoice(); else elements.focusMoveToday.focus();
}
function chooseFocusMove(target) {
  if (!focusMoveContext) return; const context = focusMoveContext;
  if (target === "dated") { showFocusDateChoice(); return; }
  closeFocusMoveDialog(false); void moveFocusTask(context.entity, target === "today" ? context.today : "", context.trigger);
}
async function updateFocusDue(entity, due, focus) {
  if (mutationIsGated()) return;
  try {
    const detail = await apiRequest(entityDetailPath("tasks", entity.id));
    const operation = {action: "update", kind: "tasks", id: detail.id, base_hash: detail.content_hash, fields: {due: due}};
    await previewMutation(operation, () => showNotice(due ? "期限を保存しました。" : "期限を外しました。"), focus);
  } catch (error) { showRequestError(error); if (focus) focus.focus(); }
}
function closeFocusDueDialog(returnFocus = true) {
  const context = focusDueContext; focusDueContext = null;
  if (elements.focusDueDialog.open) elements.focusDueDialog.close();
  if (returnFocus && context && context.trigger) context.trigger.focus();
}
function clearFocusDue() {
  if (!focusDueContext) return;
  const context = focusDueContext;
  closeFocusDueDialog(true);
  void updateFocusDue(context.entity, "", context.trigger);
}
function openFocusDueDialog(entity, trigger) {
  if (mutationIsGated()) return;
  focusDueContext = {entity, trigger}; elements.focusDueDate.value = safeFrontmatter(entity).due || "";
  elements.focusDueDialog.showModal(); elements.focusDueDate.focus();
}
function renderFocusBoard(tasks, projects, allTasks, facts) {
  if (!elements.focusActionBoard || !window.TaskCard) return;
  const today = focusToday(facts), sections = window.TaskCard.focusSections(tasks, today); focusBoardToday = today;
  for (const section of sections) {
    const list = focusSectionList(section.key), button = focusSectionButton(section.key); list.replaceChildren();
    const collapsed = focusCollapsed[section.key] === true; button.setAttribute("aria-expanded", String(!collapsed)); button.lastElementChild.textContent = section.tasks.length + "件"; list.hidden = collapsed;
    for (const entity of section.tasks) {
      const fm = safeFrontmatter(entity), project = projects.get(fm.project_id), meta = taskMeta(entity, projects, false); if (window.TaskCard.isOverdueActionDate(entity, today)) meta.unshift("対応予定日超過");
      const start = actionButton("開始", () => prepareTaskWorkflow("start", entity, start, "tasks"), true);
      const dateAction = actionButton("対応予定日", () => openFocusMoveMenu(entity, dateAction, today, true));
      const dueAction = actionButton("期限", () => openFocusDueDialog(entity, dueAction));
      const card = window.TaskCard.createTaskCard({task: entity, href: clarifyHref(entity.id), metadata: meta, project: project ? {id: project.id, title: safeFrontmatter(project).title || project.id, href: "/projects?level=projects&id=" + encodeURIComponent(project.id)} : null, handleLabel: "対応予定日を移動またはアーカイブ", onHandleActivate: () => openFocusMoveMenu(entity, handle, today), actions: [dateAction, dueAction, start]}); detailPanels.t(card,entity,loadFocus); const handle = card.querySelector(".task-card-handle");
      window.TaskCard.attachPointerDrag(handle, {threshold: 8, isGated: mutationIsGated, resolveTarget: resolveFocusDropSection, onActivate: (event) => { clearFocusDragDock(); focusDragDock = ProjectKanbanMotion.createDestinationDock([{key: "today", label: "今日やる"}, {key: "undated", label: "予定日なし"}, {key: "dated", label: "予定日あり"}], event); setArchiveTarget(false); }, onTrack: emphasizeFocusDropTarget, onCancel: () => { clearFocusDragDock(); clearFocusDropTargets(); setArchiveTarget(true); }, onDrop: (target) => { clearFocusDragDock(); clearFocusDropTargets(); setArchiveTarget(true); if (target === "archive") { void archiveFocusTask(entity, handle); return; } if (target === window.TaskCard.classifyFocusTask(entity, today)) return; if (target === "dated") openFocusMoveMenu(entity, handle, today, true); else void moveFocusTask(entity, target === "today" ? today : "", handle); }});
      list.append(card);
    }
    if (!section.tasks.length) appendEmptyState(list, "Taskはありません。");
  }
}
function replaceFocusEffectEntities(entities, effects, append = true) {
  const replacements = new Map((effects || []).map((effect) => effect && (effect.proposed || effect)).filter((entity) => entity && entity.kind === "tasks" && entity.id).map((entity) => [entity.id, entity]));
  if (!replacements.size) return entities;
  const seen = new Set();
  const updated = (entities || []).map((entity) => {
    const replacement = replacements.get(entity.id); if (!replacement) return entity;
    seen.add(entity.id); return replacement;
  });
  if (append) for (const entity of replacements.values()) if (!seen.has(entity.id)) updated.push(entity);
  return updated;
}
function applyFocusCanonicalEffects(applied) {
  if (!focusRenderState || !applied || !Array.isArray(applied.effects)) return;
  const effects = applied.effects;
  focusRenderState = {
    entities: replaceFocusEffectEntities(focusRenderState.entities, effects, false),
    completeEntities: replaceFocusEffectEntities(focusRenderState.completeEntities, effects),
    presetEntities: replaceFocusEffectEntities(focusRenderState.presetEntities, effects),
    facts: focusRenderState.facts,
  };
  globalThis.focusCanonicalPending = true;
  renderTasks(focusRenderState.entities, focusRenderState.completeEntities, focusRenderState.presetEntities, focusRenderState.facts);
}
function renderTasks(entities, completeEntities, presetEntities, facts) {
  clearDynamicMutationButtons(); if (currentElapsedInterval !== null) { (currentTimerWin||globalThis).clearInterval(currentElapsedInterval); currentElapsedInterval=null; }
  elements.tasksCurrentList.replaceChildren(); elements.tasksIncompleteList.replaceChildren(); elements.tasksCompletedList.replaceChildren();
  const allTasks=completeEntities.filter((entity)=>entity.kind==="tasks"),projects=projectMap(completeEntities),doingTasks=allTasks.filter((entity)=>safeFrontmatter(entity).status==="doing"); currentDoingCount=doingTasks.length; currentDoingTask=doingTasks.length===1?doingTasks[0]:null;
  if (focusPipController) focusPipController.sync({doingCount: doingTasks.length});
  elements.tasksWorkflow.dataset.focusHasCurrent=String(Boolean(currentDoingTask)); setHidden(elements.tasksCurrentGroup,!currentDoingTask); setHidden(elements.focusSecondaryActions,!currentDoingTask||isBreakTask(currentDoingTask));
  renderNotificationPause(facts);
  if (!currentDoingTask || !isBreakTask(currentDoingTask)) { breakCompletionPending = false; breakCompletionTaskId = null; }
  else if (breakCompletionTaskId !== currentDoingTask.id) { breakCompletionPending = false; breakCompletionTaskId = currentDoingTask.id; }
  elements.quickStartSubmit.textContent = "開始";
  setHidden(elements.breakStart, Boolean(currentDoingTask && isBreakTask(currentDoingTask)));
  if(currentDoingTask){
    const complete = actionButton("完了", () => prepareTaskWorkflow("complete", currentDoingTask, complete, "tasks"), true);
    const actions = [];
    if (!isBreakTask(currentDoingTask)) { const interrupt = actionButton("中断", () => prepareTaskWorkflow("interrupt", currentDoingTask, interrupt, "tasks"), true); actions.push(interrupt); }
    actions.push(complete);
    const areas=completeEntities.filter((entity)=>entity.kind==="areas"&&entity.archived!==true);
    const rendered=currentTaskCard(currentDoingTask,actions,projects,areas),actionRow=rendered.card.querySelector?.(".card-actions");(actionRow||rendered.card).append(elements.focusSecondaryActions);elements.tasksCurrentList.append(rendered.card);
    if (rendered.update && (isBreakTask(currentDoingTask) || elapsedFor(currentDoingTask, true) !== null)) { currentTimerWin=focusPipController ? focusPipController.timerWindow() : globalThis; currentElapsedInterval=currentTimerWin.setInterval(rendered.update, 1000); }
  }
  const tasks = entities.filter((entity) => entity.kind === "tasks" && (!currentDoingTask || entity.id !== currentDoingTask.id));
  if (cycleFocusContext) cycleFocusContext.renderFocusCycleContext(completeEntities, tasks, facts);
  const showLegacyList=focusStatus&&focusStatus!=="next"&&focusStatus!=="scheduled";setHidden(elements.tasksIncompleteGroup,!showLegacyList);if(elements.focusActionBoard)setHidden(elements.focusActionBoard,showLegacyList)
  renderFocusBoard(tasks, projects, allTasks, facts);
  if (focusPipController) focusPipController.sync({doingCount: doingTasks.length});
  const allowed = focusStatus ? [focusStatus] : ["next", "scheduled"];
  const incomplete = tasks.filter((entity) => allowed.includes(safeFrontmatter(entity).status));
  const completed = window.FocusCompleted.select(presetEntities, focusToday(facts), focusCompletedMode);
  for (const entity of incomplete) {
    const start = actionButton("開始", () => prepareTaskWorkflow("start", entity, start, "tasks"), true); const startBlockReason = taskStartBlockReason(entity, allTasks);
    if (startBlockReason) { start.textContent = "開始できません"; start.dataset.dependencyBlocked = "true"; start.disabled = true; start.setAttribute("aria-disabled", "true"); start.title = startBlockReason; }
    elements.tasksIncompleteList.append(taskCard(entity, [linkButton("編集", clarifyHref(entity.id)), start], projects, false, false, allTasks).card);
  }
  if (!incomplete.length) appendEmptyState(elements.tasksIncompleteList, "条件に合うTaskはありません。");
  for (const entity of completed) {
    const actions = [];
    if (workSessionEligible(entity)) { const edit = actionButton("実績を編集", () => openWorkSessionEditor(entity, edit), true); edit.className += " work-session-edit-trigger"; edit.setAttribute("data-work-session-task-id", entity.id); actions.push(edit); }
    elements.tasksCompletedList.append(taskCard(entity, actions, projects, false, true, allTasks).card);
  }
  if (!completed.length && focusCompletedMode === "today") appendEmptyState(elements.tasksCompletedList, "今日の完了済みTaskはありません。");
  if (elements.completedHeading) elements.completedHeading.textContent = focusCompletedMode === "all" ? "全期間の完了済み" : "今日の完了済み";
  if (elements.completedPeriodToggle) elements.completedPeriodToggle.textContent = focusCompletedMode === "all" ? "今日だけに戻す" : "過去の完了も表示";
  elements.completedCount.textContent = completed.length + "件"; elements.tasksIncompleteCount.textContent = incomplete.length + "件"; elements.tasksState.textContent = incomplete.length + "件";
  renderFilterChips();
}
function quickStartStorageValue(key, fallback = "") { try { return localStorage.getItem(key) || fallback; } catch (_error) { return fallback; } }
function quickStartRecentAreaIds() { try { const saved = JSON.parse(quickStartStorageValue(QUICK_START_RECENT_AREAS_STORAGE_KEY, "[]")); return Array.isArray(saved) ? saved.filter((id) => typeof id === "string") : []; } catch (_error) { return []; } }
function rememberQuickStartArea(id) { if (!id) return; const saved = [id, ...quickStartRecentAreaIds().filter((value) => value !== id)].slice(0, 3); try { localStorage.setItem(QUICK_START_RECENT_AREAS_STORAGE_KEY, JSON.stringify(saved)); } catch (_error) {} }
function recordQuickStartSuccess(areaId) { const id = typeof areaId === "string" ? areaId : ""; try { localStorage.setItem(QUICK_START_LAST_AREA_STORAGE_KEY, id); } catch (_error) {} if (id) rememberQuickStartArea(id); quickStartAreaId = id; }
function focusQuickStartAreaAfterRender(areaId) { const picker = byId("quick-start-area-picker"); if (!picker || typeof picker.querySelectorAll !== "function") return; const areas = Array.from(picker.querySelectorAll("[data-area-id]")); const target = areaId ? areas.find((button) => button.getAttribute("data-area-id") === areaId) : Array.from(picker.querySelectorAll("[data-area-more]"))[0]; if (target && typeof target.focus === "function") target.focus(); }
function renderQuickStartAreaPicker(areas) {
  quickStartAreas = areas;
  const availableIds = new Set(areas.map((area) => area.id));
  if (!quickStartAreaInitialized) { const last = quickStartStorageValue(QUICK_START_LAST_AREA_STORAGE_KEY); quickStartAreaId = availableIds.has(last) ? last : ""; quickStartAreaInitialized = true; }
  else if (quickStartAreaId && !availableIds.has(quickStartAreaId)) quickStartAreaId = "";
  const picker = byId("quick-start-area-picker"); if (!picker || !window.AllocationUI) return;
  const options = areas.map((area) => ({id: area.id, title: safeFrontmatter(area).title || area.id}));
  const render = window.AllocationUI.renderQuickStartAreaPicker || window.AllocationUI.renderAreaPicker;
  render({container: picker, areas: options, value: quickStartAreaId, recentAreaIds: quickStartRecentAreaIds(), onChange: (value) => { quickStartAreaId = value; rememberQuickStartArea(value); renderQuickStartAreaPicker(quickStartAreas); focusQuickStartAreaAfterRender(value); }});
}
if (typeof window !== "undefined" && window.QuickStartSuggestions && elements.quickStartSuggestions) {
  quickStartSuggestionsController = window.QuickStartSuggestions.create({input: elements.quickStartTitle, list: elements.quickStartSuggestions, submit: elements.quickStartSubmit, onSelect: (suggestion) => {
    if (!quickStartAreas.some((area) => area.id === suggestion.area_id)) return;
    quickStartAreaId = suggestion.area_id;
    renderQuickStartAreaPicker(quickStartAreas);
  }});
}
if (typeof window !== "undefined" && window.FocusPiP) {
  focusPipController = window.FocusPiP.create({
    current: elements.tasksCurrentGroup,
    switchDialog: elements.switchDialog,
    previewDialog: elements.dialog,
    notificationDialog:elements.notificationPauseDialog,
    trigger: elements.focusPipOpen,
    getState: () => ({doingCount: currentDoingCount}),
    onDocumentChange: () => { if (quickStartSuggestionsController) quickStartSuggestionsController.rebindDocument(); if (focusRenderState) renderTasks(focusRenderState.entities, focusRenderState.completeEntities, focusRenderState.presetEntities, focusRenderState.facts); },
  });
}
async function loadFocus() {
  invalidateTaskPreparation(); taskReloadInFlight = true; setMutationDisabled(true); const generation = nextGeneration("tasks"); const resolving = applyOutcomeUnknown || canonicalReloadRequired; if (!resolving) clearMessage(elements.error); elements.tasksReload.disabled = true;
  const controls = {status: {value: focusStatus}, context: elements.tasksContext, project_id: elements.tasksProject, area_id: elements.tasksArea, max_minutes: elements.tasksMinutes, due_before: elements.tasksDue};
  try {
    const resolution = await reconcileUnknownAttempt(); if (!isCurrentGeneration("tasks", generation)) return;
    let path = snapshotPath(TASK_FILTER_KEYS, controls); if (elements.tasksProject.value === "__unassigned__") path = path.replace("project_id=__unassigned__", "unassigned=1"); const selected = elements.tasksProject.value; const selectedArea = elements.tasksArea.value;
    const complete = await apiRequest("/api/v1/snapshot"); if (!isCurrentGeneration("tasks", generation)) return;
    const snapshot = path === "/api/v1/snapshot" ? complete : await apiRequest(path); if (!isCurrentGeneration("tasks", generation)) return;
    replaceOptions(elements.tasksProject, complete.entities.filter((entity) => entity.kind === "projects" && (elements.projectKanban ? ["not_started", "doing"] : ["not_started", "doing", "active"]).includes(safeFrontmatter(entity).status)), selected, "すべて"); if (elements.projectKanban) { const unassigned = document.createElement("option"); unassigned.value = "__unassigned__"; unassigned.textContent = "Projectなし"; elements.tasksProject.append(unassigned); elements.tasksProject.value = selected; }
    replaceOptions(elements.tasksArea, complete.entities.filter((entity) => entity.kind === "areas"), selectedArea, "すべて");
    renderQuickStartAreaPicker(complete.entities.filter((entity) => entity.kind === "areas"));
    renderFocusStatus(); renderFocusFilterSummary();
    focusRenderState = {entities: snapshot.entities, completeEntities: complete.entities, presetEntities: complete.entities, facts: complete.facts}; globalThis.focusCanonicalPending = false;
    renderTasks(snapshot.entities, complete.entities, complete.entities, complete.facts); renderInboxCount(complete); completeSuccessfulReload(resolution);
  } catch (error) { if (isCurrentGeneration("tasks", generation)) { renderFocusFilterSummary(); if (!globalThis.focusCanonicalPending) renderTasks([], [], [], {}); showReloadFailure(error); elements.tasksState.textContent = "Taskを読み込めませんでした。"; } }
  finally { if (isCurrentGeneration("tasks", generation)) { taskReloadInFlight = false; elements.tasksReload.disabled = false; releaseMutationControlsIfIdle(); } }
}
elements.tasksForm.addEventListener("submit", async (event) => { event.preventDefault(); await loadFocus(); });
if (elements.focusTodayAddForm) elements.focusTodayAddForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submittedTitle = elements.focusTodayAddTitle.value;
  if (!elements.focusTodayAddForm.reportValidity() || !submittedTitle.trim()) return;
  const token = beginTaskPreparation(); if (token === null) return;
  const operation = {action: "create", kind: "tasks", fields: {title: submittedTitle, status: "next", action_date: tokyoToday()}, body: ""};
  try {
    await previewMutation(operation, () => {
      if (elements.focusTodayAddTitle.value === submittedTitle) elements.focusTodayAddTitle.value = "";
      showNotice("今日やるTaskを追加しました。");
    }, elements.focusTodayAddTitle, token, {awaitApply: true, postReloadFocus: () => elements.focusTodayAddTitle});
  } finally { finishTaskPreparation(token); }
});
elements.quickStartForm.addEventListener("submit", async (event) => {
  event.preventDefault(); if (!elements.quickStartForm.reportValidity() || mutationIsGated()) return;
  if (currentDoingTask) {
    const token = beginTaskPreparation(); if (token === null) return;
    openTaskSwitch({mode: "create_and_start", active: currentDoingTask, title: elements.quickStartTitle.value, area_id: quickStartAreaId, focus: elements.quickStartTitle, token});
    return;
  }
  const operation = {action: "create_and_start", kind: "tasks", fields: {title: elements.quickStartTitle.value, area_id: quickStartAreaId}, body: ""};
  await previewMutation(operation, async (applied) => { applyFocusCanonicalEffects(applied); recordQuickStartSuccess(operation.fields.area_id); resetQuickStart(); workflowSuccess("start", applied); }, elements.quickStartTitle, null, {backgroundReload: true, onFailure: reloadFocusOnceAfterConflict});
});
function resetQuickStart() { elements.quickStartForm.reset(); renderQuickStartAreaPicker(quickStartAreas); }
async function prepareBreakWorkflow(focus) {
  const token = beginTaskPreparation(); if (token === null) return; let switchOpened = false;
  try {
    if (currentDoingTask && !isBreakTask(currentDoingTask)) {
      switchOpened = true; openTaskSwitch({mode: "start_break", active: currentDoingTask, focus, token}); return;
    }
    await previewMutation({action: "start_break", kind: "tasks"}, (applied) => {
      const count = applied && Array.isArray(applied.effects) ? applied.effects.length : 0; showNotice("5分休憩を開始しました（" + count + "件更新）。");
    }, focus, token);
  } catch (error) { if (taskPreparationGeneration === token) showRequestError(error); }
  finally { if (!switchOpened) finishTaskPreparation(token); }
}
elements.breakStart.addEventListener("click", () => prepareBreakWorkflow(elements.breakStart));
elements.notificationPause.addEventListener("click", openNotificationPauseDialog);
for (const [button, minutes] of [[elements.notificationPausePreset15, "15"], [elements.notificationPausePreset30, "30"], [elements.notificationPausePreset60, "60"]]) button.addEventListener("click", () => { elements.notificationPauseMinutes.value = minutes; });
elements.notificationPauseSave.addEventListener("click", () => previewNotificationPause("pause_notifications"));
elements.notificationPauseResume.addEventListener("click", () => previewNotificationPause("resume_notifications"));
elements.notificationPauseClose.addEventListener("click", () => closeNotificationPauseDialog());
elements.notificationPauseDialog.addEventListener("cancel", () => closeNotificationPauseDialog());
elements.tasksClear.addEventListener("click", async () => { elements.tasksForm.reset(); focusStatus = ""; renderFocusStatus(); renderFocusFilterSummary(); await loadFocus(); });
elements.tasksReload.addEventListener("click", loadFocus);
if (elements.completedPeriodToggle) elements.completedPeriodToggle.addEventListener("click", () => { focusCompletedMode = focusCompletedMode === "all" ? "today" : "all"; if (focusRenderState) renderTasks(focusRenderState.entities, focusRenderState.completeEntities, focusRenderState.presetEntities, focusRenderState.facts); });
for (const option of FOCUS_STATUS_OPTIONS) elements[option.button].addEventListener("click", () => selectFocusStatus(option.value));
elements.focusMoveToday.addEventListener("click", () => chooseFocusMove("today"));
elements.focusMoveDated.addEventListener("click", () => chooseFocusMove("dated"));
elements.focusMoveUndated.addEventListener("click", () => chooseFocusMove("undated"));
elements.focusMoveSave.addEventListener("click", () => { if (!focusMoveContext || !elements.focusMoveDate.value) return; const context = focusMoveContext, value = elements.focusMoveDate.value; closeFocusMoveDialog(false); void moveFocusTask(context.entity, value, context.trigger); });
elements.focusMoveCancel.addEventListener("click", () => closeFocusMoveDialog(true));
elements.focusMoveDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeFocusMoveDialog(true); });
if (elements.focusDueSave) elements.focusDueSave.addEventListener("click", () => { if (!focusDueContext || !elements.focusDueDate.value) return; const context = focusDueContext, value = elements.focusDueDate.value; closeFocusDueDialog(false); void updateFocusDue(context.entity, value, context.trigger); });
if (elements.focusDueClear) elements.focusDueClear.addEventListener("click", clearFocusDue);
if (elements.focusDueCancel) elements.focusDueCancel.addEventListener("click", () => closeFocusDueDialog(true));
if (elements.focusDueDialog) elements.focusDueDialog.addEventListener("cancel", (event) => { event.preventDefault(); closeFocusDueDialog(true); });
restoreFocusCollapse();
for (const key of ["today", "dated", "undated"]) {
  const button = focusSectionButton(key); if (button) button.addEventListener("click", () => { focusCollapsed[key] = !focusCollapsed[key]; saveFocusCollapse(); if (button.getAttribute("aria-expanded") === "true") { button.setAttribute("aria-expanded", "false"); focusSectionList(key).hidden = true; } else { button.setAttribute("aria-expanded", "true"); focusSectionList(key).hidden = false; } });
}

function resetProjectForm() { projectDetail = null; elements.projectForm.reset(); elements.projectStatus.value = "not_started"; elements.projectGoal.value = ""; if (elements.projectOutcome) elements.projectOutcome.value = ""; elements.projectArea.value = ""; setHidden(elements.projectCancel, true); setHidden(elements.projectArchive, true); elements.projectPreview.textContent = "作成"; elements.projectDisclosure.open = false; }
function populateProject(detail) { projectDetail = detail; const fm = safeFrontmatter(detail); elements.projectTitle.value = fm.title || ""; elements.projectStatus.value = fm.status === "active" ? "not_started" : (fm.status || "not_started"); elements.projectGoal.value = fm.goal_id || ""; if (elements.projectOutcome) elements.projectOutcome.value = fm.roadmap_outcome_id || ""; elements.projectArea.value = fm.area_id || ""; if (elements.projectPeriodStart) elements.projectPeriodStart.value = fm.planned_start_date || ""; if (elements.projectPeriodEnd) elements.projectPeriodEnd.value = fm.planned_end_date || ""; elements.projectBody.value = detail.body || ""; setHidden(elements.projectCancel, false); setHidden(elements.projectArchive, false); elements.projectPreview.textContent = "保存"; elements.projectDisclosure.open = true;U.p(); }
function renderProjectFacts(snapshot) {
  elements.projectFacts.replaceChildren(); const missing = snapshot.facts && Array.isArray(snapshot.facts.active_projects_without_next_action) ? snapshot.facts.active_projects_without_next_action : [];
  const projects = new Map(snapshot.entities.filter((entity) => entity.kind === "projects").map((entity) => [entity.id, entity]));
  elements.projectFactsCount.textContent = String(missing.length);
  for (const id of missing) { const project = projects.get(id); appendListText(elements.projectFacts, project ? safeFrontmatter(project).title || id : id); }
  if (!missing.length) appendListText(elements.projectFacts, "該当なし");
}
async function editOutcome(kind, id) {
  if(mutationIsGated()||!U.c())return;
  if (kind === "projects") { nextGeneration("projects"); elements.projectsReload.disabled = false; }
  else { nextGeneration("goals"); elements.goalsReload.disabled = false; }
  const generation = nextGeneration("edit");
  try { const detail = await apiRequest(entityDetailPath(kind, id)); if (!isCurrentGeneration("edit", generation)) return; if (kind === "projects") populateProject(detail); else populateGoal(detail); }
  catch (error) { if (isCurrentGeneration("edit", generation)) showRequestError(error); }
}
function projectQuery() { return new URLSearchParams(location.search || ""); }
function projectLaneStatus(entity) { const status = safeFrontmatter(entity).status; return status === "active" ? "not_started" : status; }
function canonicalProjectLane(status) {
  const snapshot = directionSnapshot && Array.isArray(directionSnapshot.entities) ? directionSnapshot : {entities: []};
  return snapshot.entities.filter((entity) => entity.kind === "projects" && entity.archived !== true && projectLaneStatus(entity) === status).sort((left, right) => {
    const leftPosition = Number(safeFrontmatter(left).kanban_position), rightPosition = Number(safeFrontmatter(right).kanban_position);
    const leftPlaced = Number.isInteger(leftPosition) && leftPosition > 0, rightPlaced = Number.isInteger(rightPosition) && rightPosition > 0;
    if (leftPlaced !== rightPlaced) return leftPlaced ? -1 : 1;
    if (leftPlaced && leftPosition !== rightPosition) return leftPosition - rightPosition;
    return String(left.path || left.id).localeCompare(String(right.path || right.id));
  });
}
function canonicalProjectDropPosition(projectId, status, visibleBeforeId, visibleAfterId) {
  const lane = canonicalProjectLane(status).filter((entity) => entity.id !== projectId);
  const beforeIndex = visibleBeforeId ? lane.findIndex((entity) => entity.id === visibleBeforeId) : -1;
  if (beforeIndex >= 0) return beforeIndex + 1;
  const afterIndex = visibleAfterId ? lane.findIndex((entity) => entity.id === visibleAfterId) : -1;
  return afterIndex >= 0 ? afterIndex + 2 : lane.length + 1;
}
function applyLocalProjectMove(id, status, position) {
  if (!directionSnapshot || !Array.isArray(directionSnapshot.entities)) return;
  const entity = directionSnapshot.entities.find((candidate) => candidate.id === id && candidate.kind === "projects");
  if (!entity) return;
  const sourceStatus = projectLaneStatus(entity);
  const destination = canonicalProjectLane(status).filter((candidate) => candidate.id !== id);
  destination.splice(Math.max(0, Math.min(destination.length, Number(position) - 1)), 0, entity);
  entity.frontmatter = {...safeFrontmatter(entity), status};
  for (const candidate of canonicalProjectLane(sourceStatus).filter((candidate) => candidate.id !== id)) candidate.frontmatter = {...safeFrontmatter(candidate), kanban_position: String(canonicalProjectLane(sourceStatus).filter((value) => value.id !== id).indexOf(candidate) + 1)};
  for (const [index, candidate] of destination.entries()) candidate.frontmatter = {...safeFrontmatter(candidate), status: candidate.id === id ? status : projectLaneStatus(candidate), kanban_position: String(index + 1)};
  renderProjects(directionSnapshot);
}
function projectListHref() { const query = projectQuery(); query.delete("id"); if (!query.get("level")) query.set("level", "projects"); return "/projects?" + query.toString(); }
function projectDetailHref(id) { const query = projectQuery(); query.set("level", "projects"); query.set("id", id); return "/projects?" + query.toString(); }
function projectFilterIds(query, key) { return [...new Set(query.getAll(key).filter(Boolean))]; }
function selectedProjectFilterBoxes(root) { return [...root.querySelectorAll('input[type="checkbox"]')].filter((box) => box.checked); }
function updateProjectFilterSummary(root) {
  const chosen = selectedProjectFilterBoxes(root);
  const summary = root.children[0];
  summary.textContent = chosen.length ? chosen.length + "件選択: " + chosen.map((box) => box.dataset.title || box.value).join("、") : "すべて";
  summary.setAttribute("aria-label", (root.dataset.label || "項目") + ": " + summary.textContent);
  const clear = root.children[1].children[0];
  if (clear && clear.className === "project-filter-clear") clear.disabled = chosen.length === 0;
}
function renderProjectFilterChoices(root, entities, chosenIds) {
  const options = root.children[1];
  const clear = document.createElement("button"); clear.type = "button"; clear.className = "project-filter-clear"; clear.textContent = "この項目をクリア";
  clear.addEventListener("click", () => { for (const box of selectedProjectFilterBoxes(root)) box.checked = false; applyProjectFilters(); root.children[0].focus(); });
  options.replaceChildren(clear, ...entities.map((entity) => {
    const label = document.createElement("label"), box = document.createElement("input"), title = document.createElement("span");
    box.type = "checkbox"; box.value = entity.id; box.checked = chosenIds.includes(entity.id);
    box.dataset.title = safeFrontmatter(entity).title || entity.id;
    title.textContent = box.dataset.title; label.append(box, title); return label;
  }));
  updateProjectFilterSummary(root);
}
function projectFilterState(snapshot) {
  const query = projectQuery(); const pick = (key) => query.get(key) || "";
  elements.projectFilterQuery.value = pick("q"); elements.projectFilterInactive.checked = query.get("show_inactive") === "1";
  renderProjectFilterChoices(elements.projectFilterArea, snapshot.entities.filter((entity) => entity.kind === "areas"), projectFilterIds(query, "area_id"));
  const goals = projectFilterIds(query, "goal_id");
  renderProjectFilterChoices(elements.projectFilterGoal, snapshot.entities.filter((entity) => entity.kind === "goals"), goals);
  renderProjectFilterChoices(elements.projectFilterOutcome, snapshot.entities.filter((entity) => entity.kind === "roadmap_outcomes"), projectFilterIds(query, "roadmap_outcome_id"));
}
const projectPeriod = window.ProjectTaskBoard.projectPeriod;
function projectMatchesFilter(entity) { const fm = safeFrontmatter(entity); const query = projectQuery(); const inactive = ["on_hold", "dropped"].includes(fm.status); if (!query.get("show_inactive") && inactive) return false; const areas = projectFilterIds(query, "area_id"), goals = projectFilterIds(query, "goal_id"), outcomes = projectFilterIds(query, "roadmap_outcome_id"); if (areas.length && !areas.includes(fm.area_id)) return false; if (goals.length && !goals.includes(fm.goal_id)) return false; if (outcomes.length && !outcomes.includes(fm.roadmap_outcome_id)) return false; const term = (query.get("q") || "").trim().toLocaleLowerCase(); return !term || (fm.title || "").toLocaleLowerCase().includes(term); }
function projectStatusLabel(status) { return ({not_started: "未着手", doing: "進行中", on_hold: "保留", completed: "完了", dropped: "中止"})[status] || status || "状態不明"; }
const taskStatusLabel=taskSettings.taskStatusLabel;
function announceProjectStatus(message) { elements.projectsState.textContent = message; }
let projectMoveSavingId = "";
function renderProjectKanban(projects, tasks, entities) {
  elements.projectKanban.replaceChildren();
  const lanes = [["not_started", "未着手"], ["doing", "進行中"], ["completed", "完了"], ["on_hold", "保留"], ["dropped", "中止"]];
  const svgNs = "http:" + String.fromCharCode(47, 47) + "www.w3.org/2000/svg";
  for (const [status, label] of lanes) {
    if (!["not_started", "doing", "completed"].includes(status) && !projectQuery().get("show_inactive")) continue;
    const lane = document.createElement("section");
    lane.className = "project-lane";
    lane.dataset.status = status;
    const list = document.createElement("div");
    list.className = "project-lane-list";
    const canonicalIds = canonicalProjectLane(status).map((value) => value.id);
    const laneProjects = projects
      .filter((entity) => projectLaneStatus(entity) === status)
      .sort((left, right) => canonicalIds.indexOf(left.id) - canonicalIds.indexOf(right.id));
    for (const project of laneProjects) {
      const card = document.createElement("article");
      card.className = "project-card";
      if (project.id === projectMoveSavingId) card.classList.add("project-card-saving");
      card.dataset.projectId = project.id;
      const projectFm = safeFrontmatter(project), projectTitle = projectFm.title || project.id;
      card.dataset.fullTitle = projectTitle;
      const title = document.createElement("a");
      title.href = projectDetailHref(project.id);
      title.textContent = projectTitle;
      detailPanels.p(title,project);
      const handle = document.createElement("button");
      handle.type = "button";
      handle.className = "project-drag-handle project-drag-grip";
      handle.setAttribute("aria-label", "Projectをドラッグして移動: " + projectTitle);
      handle.setAttribute("title", "ドラッグして移動");
      const svg = document.createElementNS ? document.createElementNS(svgNs, "svg") : document.createElement("svg");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      const path = document.createElementNS ? document.createElementNS(svgNs, "path") : document.createElement("path");
      path.setAttribute("d", "M8 5h2v2H8V5zm6 0h2v2h-2V5zM8 11h2v2H8v-2zm6 0h2v2h-2v-2zM8 17h2v2H8v-2zm6 0h2v2h-2v-2z");
      svg.append(path);
      handle.append(svg);
      handle.addEventListener("pointerdown", (event) => beginProjectDrag(event, project, card));
      card.append(title, handle); if (projectFm.planned_start_date && projectFm.planned_end_date) { const period = document.createElement("p"); period.className = "muted project-card-period"; period.textContent = "目安期間: " + projectPeriod.label(projectFm); card.append(period); }
      list.append(card);
    }
    if (!list.children.length) appendListText(list, "該当なし");
    const heading = ProjectKanbanMotion.createLaneDisclosure({status, label, count: laneProjects.length, list});
    lane.append(heading, list);
    elements.projectKanban.append(lane);
  }
}
function renderProjects(snapshot) {
  directionSnapshot=snapshot; elements.projectsList.replaceChildren(); const allProjects = snapshot.entities.filter((entity) => entity.kind === "projects" && entity.archived !== true); const tasks = snapshot.entities.filter((entity) => entity.kind === "tasks" && entity.archived !== true); if (!elements.projectKanban) { for (const entity of allProjects) elements.projectsList.append(makeProjectRow(entity, [actionButton("編集", () => editOutcome("projects", entity.id))], tasks, snapshot.entities)); elements.projectsState.textContent = allProjects.length ? allProjects.length + "件あります。" : "Projectはありません。"; renderProjectFacts(snapshot); return; } const projects = allProjects.filter(projectMatchesFilter); renderProjectKanban(projects, tasks, snapshot.entities); elements.projectsState.textContent = projects.length ? projects.length + "件あります。" : "Projectはありません。"; renderProjectFacts({entities: snapshot.entities, facts: snapshot.facts});
}
function projectTasks(snapshot, id) { return snapshot.entities.filter((entity) => entity.kind === "tasks" && entity.archived !== true && safeFrontmatter(entity).project_id === id); }
function renderProjectDetail(detail, snapshot) { const back = projectListHref(); setHidden(elements.projectListing, true); setHidden(elements.projectDetailPanel, false); elements.projectDetailBack.href = back; if (window.ProjectTaskBoard) window.ProjectTaskBoard.renderDetail({detail,snapshot,elements,safeFrontmatter,projectTasks,projectMap,makeTaskRow,linkButton,clarifyHref,actionButton,apiRequest,entityDetailPath,previewMutation,taskStatusLabel,projectStatusLabel,appendListText,mutationIsGated,beginTaskPreparation,finishTaskPreparation,prepareTaskWorkflow,taskStartBlockReason,showRequestError,openTaskEditor: mountTaskPanelEditor}); }
function applyOptimisticProjectDetailStatus(status) {
  if (!projectDetail) return () => {};
  const before = {...safeFrontmatter(projectDetail)}, show = (value) => { projectDetail.frontmatter = {...before, status: value}; elements.projectDetailStatus.value = value; elements.projectDetailState.textContent = "状態: " + projectStatusLabel(value); }; let restored = false; show(status);
  return () => { if (restored) return; restored = true; show(before.status || "not_started"); announceProjectStatus("保存に失敗したため、元の状態へ戻しました。"); };
}
async function previewProjectStatus(id, status, focus, restore = () => {}) {
  announceProjectStatus("保存中…");
  try {
    const detail = await apiRequest(entityDetailPath("projects", id));
    const position = canonicalProjectDropPosition(detail.id, status, "", "");
    await previewMutation({action: "project_move", kind: "projects", id: detail.id, base_hash: detail.content_hash, status, position}, () => {}, focus, null, {onFailure: restore});
  } catch (error) {
    restore(error); showRequestError(error); await loadProjectKanban(); announceProjectStatus("状態変更に失敗しました。再読み込みします。");
  }
}
let projectDrag = null;
function cleanupProjectDrag() {
  if (!projectDrag) return null;
  const state = projectDrag, captureTarget = state.captureTarget;
  projectDrag = null;
  if (captureTarget) {
    captureTarget.removeEventListener("pointermove", moveProjectDrag);
    captureTarget.removeEventListener("pointerup", endProjectDrag);
    captureTarget.removeEventListener("pointercancel", cancelProjectDrag);
    captureTarget.removeEventListener("lostpointercapture", cancelProjectDrag);
    ProjectKanbanMotion.cancelPointerGesture(state.gesture);
  }
  if (typeof document.removeEventListener === "function") {
    document.removeEventListener("pointerup", endProjectDrag, true);
    document.removeEventListener("pointercancel", cancelProjectDrag, true);
  }
  ProjectKanbanMotion.stopAutoScroll(state); ProjectKanbanMotion.clearDestinationDock(state.dock); ProjectKanbanMotion.clear(state); state.card.classList.remove("is-dragging");
  return state;
}
function cancelProjectDrag(event) {
  if (!projectDrag || (event && typeof event.pointerId === "number" && projectDrag.pointerId !== event.pointerId)) return;
  cleanupProjectDrag();
  announceProjectStatus("移動を取り消しました。");
}
function beginProjectDrag(event, project, card) {
  if (typeof event.pointerId !== "number" || mutationIsGated()) return;
  if (projectDrag) cancelProjectDrag();
  const captureTarget = event.currentTarget || card;
  const gesture = ProjectKanbanMotion.beginPointerGesture(event, captureTarget, {isGated: mutationIsGated}); if (!gesture) return;
  projectDrag = {project,card,captureTarget,pointerId: event.pointerId,gesture,moved:false,targetStatus:"",beforeId:"",afterId:"",lastY:event.clientY,autoScroll:false,autoFrame:0};
  captureTarget.addEventListener("pointermove", moveProjectDrag);
  captureTarget.addEventListener("pointerup", endProjectDrag);
  captureTarget.addEventListener("pointercancel", cancelProjectDrag);
  captureTarget.addEventListener("lostpointercapture", cancelProjectDrag);
  if (typeof document.addEventListener === "function") {
    document.addEventListener("pointerup", endProjectDrag, true);
    document.addEventListener("pointercancel", cancelProjectDrag, true);
  }
  announceProjectStatus("Projectを移動します。");
}
function moveProjectDrag(event) {
  if (!projectDrag || projectDrag.pointerId !== event.pointerId) return;
  if (!ProjectKanbanMotion.movePointerGesture(projectDrag.gesture, event).active) return;
  projectDrag.moved = true;
  projectDrag.card.classList.add("is-dragging");
  projectDrag.lastY = event.clientY; if (typeof event.preventDefault === "function") event.preventDefault();
  if (!projectDrag.marker) { const marker = document.createElement("div"); marker.className = "project-drag-marker project-drag-placeholder"; marker.setAttribute("aria-hidden", "true"); projectDrag.marker = marker; projectDrag.layer = ProjectKanbanMotion.createVisualDragLayer(safeFrontmatter(projectDrag.project).title || projectDrag.project.id); }
  if (!projectDrag.dock) projectDrag.dock = ProjectKanbanMotion.createDestinationDock([...elements.projectKanban.children].filter((lane) => lane.dataset.status).map((lane) => ({key: lane.dataset.status, label: projectStatusLabel(lane.dataset.status)})), event, "ここにドロップして分類の末尾へ");
  ProjectKanbanMotion.moveVisualDragLayer(projectDrag.layer, event.clientX + 12, event.clientY + 12);
  trackProjectDrop(event); if (!projectDrag.autoScroll) { projectDrag.autoScroll = true; const tick = () => { if (projectDrag) ProjectKanbanMotion.autoScroll(projectDrag, tick); }; ProjectKanbanMotion.beginAutoScroll(projectDrag, tick); }
}
function trackProjectDrop(event) {
  const destination = ProjectKanbanMotion.destinationAt(projectDrag.dock, event);
  if (!destination) { ProjectKanbanMotion.target(projectDrag, event.clientX, event.clientY); return; }
  projectDrag.targetStatus = destination; projectDrag.beforeId = ""; projectDrag.afterId = "";
  const lane = [...elements.projectKanban.children].find((value) => value.dataset.status === destination), list = lane && lane.querySelector(".project-lane-list");
  if (list) ProjectKanbanMotion.moveMarker(list, projectDrag.marker, null);
}
function restoreProjectMove(before) {
  const positions = ProjectKanbanMotion.captureProjects(elements.projectKanban.querySelectorAll(".project-card")); projectMoveSavingId="";
  for (const [value, fields] of before) value.frontmatter = fields; renderProjects(directionSnapshot);
  ProjectKanbanMotion.flipProjects(positions, elements.projectKanban.querySelectorAll(".project-card")); announceProjectStatus("保存に失敗したため、元の位置へ戻しました。");
}
function endProjectDrag(event) {
  if (!projectDrag || projectDrag.pointerId !== event.pointerId) return;
  if (projectDrag.moved) trackProjectDrop(event);
  const state = cleanupProjectDrag();
  if (mutationIsGated()) { announceProjectStatus("移動を取り消しました。"); return; }
  if (state.moved && state.targetStatus) {
    const token = beginTaskPreparation();
    if (token === null) { announceProjectStatus("移動を取り消しました。"); return; }
    const position = canonicalProjectDropPosition(state.project.id, state.targetStatus, state.beforeId, state.afterId), before = directionSnapshot.entities.filter((value) => value.kind === "projects").map((value) => [value, {...safeFrontmatter(value)}]); let restored;
    const restore = () => { if (!restored) { restored=true; restoreProjectMove(before); } }; projectMoveSavingId = state.project.id; applyLocalProjectMove(state.project.id, state.targetStatus, position); announceProjectStatus("保存中…");
    void (async () => {
      try {
        const detail = await apiRequest(entityDetailPath("projects", state.project.id));
        await previewMutation({action: "project_move", kind: "projects", id: detail.id, base_hash: detail.content_hash, status: state.targetStatus, position}, () => { projectMoveSavingId = ""; }, state.card, token, {onFailure: restore});
      } catch (error) { restore(); showRequestError(error); }
      finally { finishTaskPreparation(token); }
    })();
  }
  else announceProjectStatus("移動を取り消しました。");
}
if (typeof document.addEventListener === "function") document.addEventListener("keydown", (event) => { if (event.key === "Escape" && projectDrag) { event.preventDefault(); cancelProjectDrag(); } });
function applyOutcomeView() {
  if (activeDirectionLevel !== "projects" && activeDirectionLevel !== "goals") return;
  const goals = activeOutcomeTab === "goals"; setHidden(elements.projectsView, goals); setHidden(elements.goalsView, !goals);
  elements.projectsTab.setAttribute("aria-current", goals ? "false" : "page"); elements.goalsTab.setAttribute("aria-current", goals ? "page" : "false");
}
async function loadProjects() {
  nextGeneration("edit"); const generation = nextGeneration("projects"); const resolving = applyOutcomeUnknown || canonicalReloadRequired; if (!resolving) clearMessage(elements.error); elements.projectsReload.disabled = true;
  try { const resolution = await reconcileUnknownAttempt(); if (!isCurrentGeneration("projects", generation)) return; const snapshot = await apiRequest("/api/v1/snapshot"); if (!isCurrentGeneration("projects", generation)) return; const selected = elements.projectGoal.value; replaceOptions(elements.projectGoal, snapshot.entities.filter((entity) => entity.kind === "goals"), selected, "リンクなし"); if (elements.projectOutcome) replaceOptions(elements.projectOutcome, snapshot.entities.filter((entity) => entity.kind === "roadmap_outcomes" && entity.archived !== true), elements.projectOutcome.value, "リンクなし"); replaceOptions(elements.projectArea, snapshot.entities.filter((entity) => entity.kind === "areas"), elements.projectArea.value, "リンクなし"); replaceOptions(elements.goalVision, snapshot.entities.filter((entity) => entity.kind === "visions"), elements.goalVision.value, "リンクなし"); renderProjects(snapshot); renderGoals(snapshot); renderDirection(snapshot); renderInboxCount(snapshot); applyOutcomeView(); const requestedProjectId = new URLSearchParams(location.search || "").get("id"); if (requestedProjectId) { try { const detail = await apiRequest(entityDetailPath("projects", requestedProjectId)); if (!isCurrentGeneration("projects", generation)) return; if (!detail || detail.kind !== "projects" || detail.archived === true) throw new RequestFailure("reconciliation"); populateProject(detail); elements.projectsState.textContent = "Projectを編集しています: " + roadmapTitle(detail); } catch (_error) { if (isCurrentGeneration("projects", generation)) { resetProjectForm(); elements.projectsState.textContent = "指定されたProjectは見つかりません。"; } } } completeSuccessfulReload(resolution); }
  catch (error) { if (isCurrentGeneration("projects", generation)) { showReloadFailure(error); elements.projectsState.textContent = "Projectを読み込めませんでした。"; } }
  finally { if (isCurrentGeneration("projects", generation)) elements.projectsReload.disabled = false; }
}
elements.projectForm.addEventListener("submit", async (event) => {
  event.preventDefault(); const periodError = projectPeriod.validate(elements.projectPeriodStart, elements.projectPeriodEnd); if (!elements.projectForm.reportValidity()) { if (periodError) (periodError.includes("終了日") ? elements.projectPeriodEnd : elements.projectPeriodStart).focus(); return; }
  const fields = {title: elements.projectTitle.value, status: elements.projectStatus.value, goal_id: elements.projectGoal.value, area_id: elements.projectArea.value}; if (elements.projectOutcome) fields.roadmap_outcome_id = elements.projectOutcome.value;
  fields.planned_start_date = elements.projectPeriodStart.value; fields.planned_end_date = elements.projectPeriodEnd.value;
  const operation = projectDetail ? {action: "update", kind: "projects", id: projectDetail.id, base_hash: projectDetail.content_hash, fields, body: elements.projectBody.value} : {action: "create", kind: "projects", fields, body: elements.projectBody.value};
  await previewMutation(operation,(applied)=>{const detail=projectDetail;resetProjectForm();showNotice("Projectを保存しました: "+appliedMutationPath(applied));if(detail)loadProjectKanban()},elements.projectTitle);
});
elements.projectArchive.addEventListener("click", async () => { if (!projectDetail) return; const operation = {action: "archive", kind: "projects", id: projectDetail.id, base_hash: projectDetail.content_hash}; await previewMutation(operation, async (applied) => { resetProjectForm(); showNotice("Projectをアーカイブしました: " + appliedMutationPath(applied)); }, elements.projectTitle); });
elements.projectCancel.addEventListener("click", resetProjectForm); elements.projectsReload.addEventListener("click", loadProjects);
elements.projectForm.addEventListener("input",()=>projectPeriod.validate(elements.projectPeriodStart,elements.projectPeriodEnd));
elements.projectDetailEdit.onclick=()=>{if(!projectDetail)return;elements.projectDetailPanel.hidden=true;elements.projectListing.hidden=false;populateProject(projectDetail);elements.projectTitle.focus();};
if (elements.projectOutcome) { elements.projectGoal.addEventListener("change", () => { if (elements.projectGoal.value) elements.projectOutcome.value = ""; }); elements.projectOutcome.addEventListener("change", () => { if (elements.projectOutcome.value) elements.projectGoal.value = ""; }); }
async function openDirectionDeepLink(level, id) { if (level === "goals") return editOutcome("goals", id); if (level === "areas") return editDirection("areas", id); }
async function loadProjectKanban() {
  if (!elements.projectKanban) return loadProjects();
  const generation = nextGeneration("projects"), resolving = applyOutcomeUnknown || canonicalReloadRequired; if (!resolving) clearMessage(elements.error); elements.projectsReload.disabled = true;
  try { const resolution = await reconcileUnknownAttempt(); if (!isCurrentGeneration("projects", generation)) return; const query = projectQuery(); const snapshot = await apiRequest("/api/v1/snapshot"); if (!isCurrentGeneration("projects", generation)) return; replaceOptions(elements.projectGoal, snapshot.entities.filter((entity) => entity.kind === "goals"), elements.projectGoal.value, "リンクなし"); if (elements.projectOutcome) replaceOptions(elements.projectOutcome, snapshot.entities.filter((entity) => entity.kind === "roadmap_outcomes" && entity.archived !== true), elements.projectOutcome.value, "リンクなし"); replaceOptions(elements.projectArea, snapshot.entities.filter((entity) => entity.kind === "areas"), elements.projectArea.value, "リンクなし"); projectFilterState(snapshot); renderProjects(snapshot); renderGoals(snapshot); renderDirection(snapshot); renderInboxCount(snapshot); const id = query.get("id"); if (activeDirectionLevel === "projects" && await detailPanels.refreshProject(snapshot)) {} else if (activeDirectionLevel === "projects" && id) { const detail = await apiRequest(entityDetailPath("projects", id)); if (!isCurrentGeneration("projects", generation)) return; if (!detail || detail.archived === true) throw new RequestFailure("reconciliation"); projectDetail = detail; renderProjectDetail(detail, snapshot); elements.projectDetailHeading.focus(); } else { setHidden(elements.projectListing, false); setHidden(elements.projectDetailPanel, true); if (id) await openDirectionDeepLink(activeDirectionLevel, id); } completeSuccessfulReload(resolution); }
  catch (error) { if (isCurrentGeneration("projects", generation)) { showReloadFailure(error); elements.projectsState.textContent = "Projectを読み込めませんでした。"; } }
  finally { if (isCurrentGeneration("projects", generation)) elements.projectsReload.disabled = false; }
}
function applyProjectFilters() {
  const query = new URLSearchParams("level=projects"); const q = elements.projectFilterQuery.value.trim();
  if (q) query.set("q", q);
  for (const box of selectedProjectFilterBoxes(elements.projectFilterArea)) query.append("area_id", box.value);
  const goals = selectedProjectFilterBoxes(elements.projectFilterGoal);
  for (const box of goals) query.append("goal_id", box.value);
  if (goals.length) for (const box of selectedProjectFilterBoxes(elements.projectFilterOutcome)) box.checked = false;
  else for (const box of selectedProjectFilterBoxes(elements.projectFilterOutcome)) query.append("roadmap_outcome_id", box.value);
  for (const root of [elements.projectFilterArea, elements.projectFilterGoal, elements.projectFilterOutcome]) updateProjectFilterSummary(root);
  if (elements.projectFilterInactive.checked) query.set("show_inactive", "1");
  history.replaceState(null, "", "/projects?" + query.toString());
  if (directionSnapshot && Array.isArray(directionSnapshot.entities)) renderProjects(directionSnapshot);
}
if (elements.projectFilters) {
  let composing = false;
  elements.projectFilters.addEventListener("submit", (event) => { event.preventDefault(); applyProjectFilters(); });
  for (const control of [elements.projectFilterArea, elements.projectFilterGoal, elements.projectFilterOutcome, elements.projectFilterInactive]) control.addEventListener("change", applyProjectFilters);
  elements.projectFilterQuery.addEventListener("compositionstart", () => { composing = true; });
  elements.projectFilterQuery.addEventListener("compositionend", () => { composing = false; applyProjectFilters(); });
  elements.projectFilterQuery.addEventListener("input", (event) => { if (!composing && !event.isComposing) applyProjectFilters(); });
}
if (elements.projectOtherTaskForm) elements.projectOtherTaskForm.addEventListener("submit", async (event) => { event.preventDefault(); if (!projectDetail || !elements.projectOtherTaskForm.reportValidity()) return; await previewMutation({action: "create", kind: "tasks", fields: {title: elements.projectOtherTaskTitle.value, status: elements.projectOtherTaskStatus.value, project_id: projectDetail.id}, body: ""}, () => elements.projectOtherTaskForm.reset(), elements.projectOtherTaskTitle); });
const projectPlanEditor = typeof window !== "undefined" && window.ProjectTaskBoard && elements.projectPlanRows ? window.ProjectTaskBoard.mountPlanEditor(elements.projectPlanRows) : null;
function closeProjectPlanEditor() { elements.projectTaskPlanForm.hidden = true; elements.projectPlanTree.hidden = false; elements.projectPlanOpen.setAttribute("aria-expanded", "false"); }
if (elements.projectTaskPlanForm) elements.projectTaskPlanForm.addEventListener("submit", async (event) => { event.preventDefault(); if (!projectDetail || !projectPlanEditor || !elements.projectTaskPlanForm.reportValidity()) return; const operation = projectPlanEditor.updatePayload(projectDetail.id, projectDetail.content_hash); await previewMutation(operation, () => { projectPlanEditor.markClean(); closeProjectPlanEditor(); }, elements.projectPlanRows.querySelector("input")); });
if (elements.projectSupportForm) elements.projectSupportForm.addEventListener("submit", async (event) => { event.preventDefault(); if (!projectDetail) return; const fm = safeFrontmatter(projectDetail); await previewMutation({action: "update", kind: "projects", id: projectDetail.id, base_hash: projectDetail.content_hash, fields: {title: fm.title || "", status: fm.status || "not_started", goal_id: fm.goal_id || "", area_id: fm.area_id || "", roadmap_outcome_id: fm.roadmap_outcome_id || "", planned_start_date: fm.planned_start_date || "", planned_end_date: fm.planned_end_date || ""}, body: elements.projectSupportBody.value}, () => {}, elements.projectSupportBody); });
if (elements.projectDetailStatus) elements.projectDetailStatus.addEventListener("change", () => { if (!projectDetail) return; const status = elements.projectDetailStatus.value, restore = applyOptimisticProjectDetailStatus(status); void previewProjectStatus(projectDetail.id, status, elements.projectDetailStatus, restore); });
if (elements.projectPlanOpen) elements.projectPlanOpen.addEventListener("click", () => { if (projectPlanEditor && projectDetail && directionSnapshot && !mutationIsGated()) { if (!elements.projectTaskPlanForm.hidden) { projectPlanEditor.focusFirst(); return; } const tasks = window.ProjectTaskBoard.supportTasks(projectTasks(directionSnapshot, projectDetail.id), safeFrontmatter); projectPlanEditor.load(window.ProjectTaskBoard.planEditorItems(tasks, safeFrontmatter)); elements.projectPlanTree.hidden = true; elements.projectTaskPlanForm.hidden = false; elements.projectPlanOpen.setAttribute("aria-expanded", "true"); projectPlanEditor.focusFirst(); } });
if (elements.projectPlanAddRoot) elements.projectPlanAddRoot.addEventListener("click", () => projectPlanEditor && projectPlanEditor.addRoot());
if (elements.projectPlanClose) elements.projectPlanClose.addEventListener("click", () => { if (projectPlanEditor && projectPlanEditor.isDirty() && !window.confirm("未保存の計画変更を破棄しますか？")) return; closeProjectPlanEditor(); });
if (typeof window !== "undefined" && typeof window.addEventListener === "function") window.addEventListener("beforeunload", (event) => { if (!projectPlanEditor || elements.projectTaskPlanForm.hidden || !projectPlanEditor.isDirty()) return; event.preventDefault(); event.returnValue = ""; });
for (const toggle of [elements.projectPlanDoneToggle, elements.projectOtherDoneToggle]) if (toggle) toggle.addEventListener("change", () => { if (projectDetail && directionSnapshot) renderProjectDetail(projectDetail, directionSnapshot); });

function resetGoalForm() { goalDetail = null; elements.goalForm.reset(); elements.goalStatus.value = "active"; elements.goalVision.value = ""; elements.goalTargetDate.value = ""; setHidden(elements.goalCancel, true); setHidden(elements.goalArchive, true); elements.goalPreview.textContent = "作成"; elements.goalDisclosure.open = false; }
function populateGoal(detail) { goalDetail = detail; const fm = safeFrontmatter(detail); elements.goalTitle.value = fm.title || ""; elements.goalStatus.value = fm.status || "active"; elements.goalVision.value = fm.vision_id || ""; elements.goalTargetDate.value = fm.target_date || ""; elements.goalBody.value = detail.body || ""; setHidden(elements.goalCancel, false); setHidden(elements.goalArchive, false); elements.goalPreview.textContent = "保存"; elements.goalDisclosure.open = true; elements.goalTitle.focus();U.r(); }
function renderGoals(snapshot) {
  elements.goalsList.replaceChildren(); const goals = snapshot.entities.filter((entity) => entity.kind === "goals"); const projects = snapshot.entities.filter((entity) => entity.kind === "projects");
  for (const entity of goals) elements.goalsList.append(makeGoalRow(entity, [actionButton("編集", () => editOutcome("goals", entity.id))], projects));
  elements.goalsState.textContent = goals.length ? goals.length + "件あります。" : "Goalはありません。";
}
function directionFacts(snapshot) { return snapshot && snapshot.facts && snapshot.facts.direction && typeof snapshot.facts.direction === "object" ? snapshot.facts.direction : {}; }
function renderDirectionCard(container, label, value, href) { const link = document.createElement("a"); link.className = "direction-card"; link.setAttribute("href", href); const heading = document.createElement("h2"); heading.textContent = label; const copy = document.createElement("p"); copy.textContent = value; link.append(heading, copy); container.append(link); }
function renderDirection(snapshot) {
  directionSnapshot = snapshot;
  const facts = directionFacts(snapshot); const entities = Array.isArray(snapshot.entities) ? snapshot.entities : [];
  elements.directionCards.replaceChildren();
  const purpose = facts.purpose; const visions = facts.vision_status_counts || {}; const areas = facts.area_health_counts || {};
  const missing = Array.isArray(snapshot.facts && snapshot.facts.active_projects_without_next_action) ? snapshot.facts.active_projects_without_next_action : [];
  renderDirectionCard(elements.directionCards, "Purpose", purpose ? (purpose.excerpt || purpose.title || "登録済み") : "未登録", "/projects?level=purpose");
  renderDirectionCard(elements.directionCards, "Vision", String(visions.active || 0) + "件 active", "/projects?level=visions");
  renderDirectionCard(elements.directionCards, "Goals", "active " + String(facts.active_goal_count || 0) + "件" + (facts.next_goal_target_date ? " / 次 " + facts.next_goal_target_date : ""), "/projects?level=goals");
  renderDirectionCard(elements.directionCards, "Areas", "維持 " + String(areas.maintained || 0) + "件 / 要確認 " + String(areas.needs_attention || 0) + "件", "/projects?level=areas");
  renderDirectionCard(elements.directionCards, "Projects", "active " + String(facts.active_project_count || 0) + "件 / Nextなし " + String(missing.length) + "件", "/projects?level=projects");
  renderDirectionRoadmapSummary(snapshot);
  elements.directionState.textContent = "H5からH1を確認します。";
  renderDirectionList(elements.purposesList, elements.purposeState, entities.filter((entity) => entity.kind === "purposes"), "Purpose");
  renderDirectionList(elements.visionsList, elements.visionsState, entities.filter((entity) => entity.kind === "visions"), "Vision");
  renderDirectionList(elements.areasList, elements.areasState, entities.filter((entity) => entity.kind === "areas"), "Area", true);
  setHidden(elements.purposeDisclosure, entities.some((entity) => entity.kind === "purposes" && entity.archived !== true));
  const levelMap = {overview: null, purpose: elements.purposeView, visions: elements.visionsView, goals: elements.goalsView, areas: elements.areasView, projects: elements.projectsView};
  const directionTabs = {purpose: elements.purposeTab, visions: elements.visionsTab, goals: elements.goalsTab, areas: elements.areasTab, projects: elements.projectsTab, review: elements.directionReviewTab};
  for (const [level, tab] of Object.entries(directionTabs)) tab.setAttribute("aria-current", activeDirectionLevel === level ? "page" : "false");
  setHidden(elements.directionOverview, activeDirectionLevel !== "overview"); setHidden(elements.directionReview, activeDirectionLevel !== "review");
  for (const view of Object.values(levelMap)) if (view) setHidden(view, view !== levelMap[activeDirectionLevel]);
  if (activeDirectionLevel === "overview" || activeDirectionLevel === "review") { setHidden(elements.projectsView, true); setHidden(elements.goalsView, true); }
}
function renderDirectionRoadmapSummary(snapshot) {
  if (!elements.directionRoadmapList) return;
  const entities = Array.isArray(snapshot && snapshot.entities) ? snapshot.entities : [];
  const outcomes = new Map(entities.filter((entity) => entity.kind === "roadmap_outcomes").map((entity) => [entity.id, entity]));
  const goals = entities.filter((entity) => entity.kind === "goals" && entity.archived !== true);
  elements.directionRoadmapList.replaceChildren();
  for (const goal of goals) {
    const linked = [...outcomes.values()].filter((outcome) => safeFrontmatter(outcome).goal_id === goal.id && outcome.archived !== true);
    appendListText(elements.directionRoadmapList, (safeFrontmatter(goal).title || goal.id) + " — Outcome " + linked.length + "件");
  }
  if (!goals.length) appendListText(elements.directionRoadmapList, "Goalはありません。RoadmapはGoalを登録してから整理します。");
}
function renderDirectionList(list, state, entries, label, canReview = false) {
  list.replaceChildren(); for (const entity of entries) { const fm = safeFrontmatter(entity); const actions = [];
    const kind = entity.kind; actions.push(actionButton("編集", () => editDirection(kind, entity.id)));
    if (canReview) { const health = document.createElement("select"); for (const value of ["maintained", "needs_attention"]) { const option = document.createElement("option"); option.value = value; option.textContent = value; health.append(option); } health.value = fm.health || "maintained"; actions.push(health, actionButton("状態を確認", async () => { const detail = await apiRequest(entityDetailPath("areas", entity.id)); const detailFm = safeFrontmatter(detail); const operation = {action: "update", kind: "areas", id: detail.id, base_hash: detail.content_hash, fields: {title: detailFm.title || "", health: health.value, last_reviewed_on: tokyoToday()}, body: detail.body || ""}; await previewMutation(operation, async () => showNotice("Areaを確認しました。"), health); }, true)); }
    const meta = [label];
    if (entity.kind === "visions") meta.push(fm.status || "状態不明");
    if (entity.kind === "areas") { meta.push(fm.health || "未確認", fm.last_reviewed_on || "未確認"); if (canReview) meta.push("関連Project", "standalone Task"); }
    list.append(makeEntityRow(entity, actions, meta)); }
  state.textContent = entries.length ? entries.length + "件あります。" : label + "はありません。";
}
function tokyoToday() { const parts = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Tokyo", year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(new Date()); const value = Object.fromEntries(parts.filter((part) => part.type !== "literal").map((part) => [part.type, part.value])); return value.year + "-" + value.month + "-" + value.day; }
const PURPOSE_BODY = "## Purpose\n\n## Principles\n";
const directionControls = {
  purposes: {form: elements.purposeForm, title: elements.purposeTitle, body: elements.purposeBody, preview: elements.purposePreview, cancel: elements.purposeCancel, archive: elements.purposeArchive, disclosure: elements.purposeDisclosure, fields: () => ({title: elements.purposeTitle.value})},
  visions: {form: elements.visionForm, title: elements.visionTitle, body: elements.visionBody, preview: elements.visionPreview, cancel: elements.visionCancel, archive: elements.visionArchive, disclosure: elements.visionDisclosure, fields: () => ({title: elements.visionTitle.value, status: elements.visionStatus.value})},
  areas: {form: elements.areaForm, title: elements.areaTitle, body: elements.areaBody, preview: elements.areaPreview, cancel: elements.areaCancel, archive: elements.areaArchive, disclosure: elements.areaDisclosure, fields: () => ({title: elements.areaTitle.value, health: elements.areaHealth.value})},
};
elements.purposeBody.value = PURPOSE_BODY;
function hasActivePurpose() { return Boolean(directionSnapshot && Array.isArray(directionSnapshot.entities) && directionSnapshot.entities.some((entity) => entity.kind === "purposes" && entity.archived !== true)); }
function resetDirectionForm(kind) { const controls = directionControls[kind]; directionDetails[kind] = null; controls.form.reset(); if (kind === "purposes") controls.body.value = PURPOSE_BODY; controls.preview.textContent = "作成"; setHidden(controls.cancel, true); setHidden(controls.archive, true); controls.disclosure.open = false; if (kind === "purposes" && hasActivePurpose()) setHidden(controls.disclosure, true); }
function populateDirectionForm(kind, detail) { const controls = directionControls[kind]; const fm = safeFrontmatter(detail); directionDetails[kind] = detail; controls.title.value = fm.title || ""; controls.body.value = detail.body || ""; if (kind === "visions") elements.visionStatus.value = fm.status || "active"; if (kind === "areas") elements.areaHealth.value = fm.health || ""; controls.preview.textContent = "保存"; setHidden(controls.cancel, false); setHidden(controls.archive, false); if (kind === "purposes") setHidden(controls.disclosure, false); controls.disclosure.open = true; controls.title.focus();U.r(); }
async function editDirection(kind, id) { if(mutationIsGated()||!U.c())return;try { const detail = await apiRequest(entityDetailPath(kind, id)); populateDirectionForm(kind, detail); if (kind === "areas") renderAreaDetail(detail); } catch (error) { showRequestError(error); } }
function renderAreaDetail(detail) { const fm = safeFrontmatter(detail); const entities = directionSnapshot && Array.isArray(directionSnapshot.entities) ? directionSnapshot.entities : []; const projects = entities.filter((project) => project.kind === "projects" && safeFrontmatter(project).area_id === detail.id); const tasks = entities.filter((task) => task.kind === "tasks" && safeFrontmatter(task).area_id === detail.id && !safeFrontmatter(task).project_id); elements.areaDetailContent.replaceChildren(); const facts = document.createElement("p"); facts.textContent = "状態: " + (fm.health || "未確認") + " / 最終確認: " + (fm.last_reviewed_on || "未確認"); const body = document.createElement("pre"); body.textContent = detail.body || ""; const projectHeading = document.createElement("h3"); projectHeading.textContent = "関連Project"; const projectList = document.createElement("ul"); if (projects.length) for (const project of projects) appendListText(projectList, safeFrontmatter(project).title || project.id); else appendListText(projectList, "該当なし"); const taskHeading = document.createElement("h3"); taskHeading.textContent = "standalone Task"; const taskList = document.createElement("ul"); if (tasks.length) for (const task of tasks) appendListText(taskList, safeFrontmatter(task).title || task.id); else appendListText(taskList, "該当なし"); elements.areaDetailContent.append(facts, body, projectHeading, projectList, taskHeading, taskList); setHidden(elements.areaDetail, false); }
elements.areaDetailClose.addEventListener("click", () => { setHidden(elements.areaDetail, true); elements.areaDetailContent.replaceChildren(); });
for (const [kind, controls] of Object.entries(directionControls)) { controls.form.addEventListener("submit", async (event) => { event.preventDefault(); if (!controls.form.reportValidity()) return; const detail = directionDetails[kind]; const operation = detail ? {action: "update", kind, id: detail.id, base_hash: detail.content_hash, fields: controls.fields(), body: controls.body.value} : {action: "create", kind, fields: controls.fields(), body: controls.body.value}; await previewMutation(operation, async (applied) => { resetDirectionForm(kind); showNotice("保存しました: " + applied.path); }, controls.title); }); controls.cancel.addEventListener("click", () => resetDirectionForm(kind)); controls.archive.addEventListener("click", async () => { const detail = directionDetails[kind]; if (!detail) return; await previewMutation({action: "archive", kind, id: detail.id, base_hash: detail.content_hash}, async (applied) => { resetDirectionForm(kind); showNotice("アーカイブしました: " + applied.path); }, controls.title); }); }
async function loadGoals() {
  await loadProjects();
}
elements.goalForm.addEventListener("submit", async (event) => {
  event.preventDefault(); if (!elements.goalForm.reportValidity()) return; const fields = {title: elements.goalTitle.value, status: elements.goalStatus.value, vision_id: elements.goalVision.value, target_date: elements.goalTargetDate.value};
  const operation = goalDetail ? {action: "update", kind: "goals", id: goalDetail.id, base_hash: goalDetail.content_hash, fields, body: elements.goalBody.value} : {action: "create", kind: "goals", fields, body: elements.goalBody.value};
  await previewMutation(operation, async (applied) => { resetGoalForm(); showNotice("Goalを保存しました: " + applied.path); }, elements.goalTitle);
});
elements.goalArchive.addEventListener("click", async () => { if (!goalDetail) return; const operation = {action: "archive", kind: "goals", id: goalDetail.id, base_hash: goalDetail.content_hash}; await previewMutation(operation, async (applied) => { resetGoalForm(); showNotice("Goalをアーカイブしました: " + applied.path); }, elements.goalTitle); });
elements.goalCancel.addEventListener("click", resetGoalForm); elements.goalsReload.addEventListener("click", loadProjects);

function localDateParts(date) { return {year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate()}; }
function formatLocalDate(date) { const parts = localDateParts(date); return String(parts.year).padStart(4, "0") + "-" + String(parts.month).padStart(2, "0") + "-" + String(parts.day).padStart(2, "0"); }
function defaultReviewDate(kind) { const parts = tokyoToday().split("-").map(Number); const date = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2])); if (kind === "weekly") date.setUTCDate(date.getUTCDate() - ((date.getUTCDay() + 6) % 7)); return date.toISOString().slice(0, 10); }
function validDate(value) { const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value); if (!match) return false; const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])); return formatLocalDate(date) === value; }
function reviewDraftKey(kind = activeReviewKind, periodStart = elements.reviewPeriod.value) { return REVIEW_DRAFT_PREFIX + kind + ":" + periodStart; }
function reviewNotesFromBody(kind) { const marker = "## Notes\n\n"; const body = kind === "daily" ? DAILY_REVIEW_BODY : WEEKLY_REVIEW_BODY; return body.slice(body.indexOf(marker) + marker.length).replace(/\n$/, ""); }
function reviewCompletedStepIds() { return elements.reviewStepControls.filter((control) => control && control.checked).map((control) => control.dataset.stepId).filter(Boolean); }
function reviewBodyFromChecklist() { const checked = new Set(reviewCompletedStepIds()); const checklist = REVIEW_STEPS[activeReviewKind].map((step) => "- [" + (checked.has(step.id) ? "x" : " ") + "] " + step.label).join("\n"); const notesMarker = "## Notes\n\n"; const notes = elements.reviewBody.value.includes(notesMarker) ? elements.reviewBody.value.slice(elements.reviewBody.value.indexOf(notesMarker) + notesMarker.length).replace(/\n$/, "") : elements.reviewBody.value; return "\n## Checklist\n\n" + checklist + "\n\n## Notes\n\n" + notes + "\n"; }
function showDraftStorageWarning() { setHidden(elements.reviewDraftWarning, false); }
function persistReviewDraft(periodStart = reviewDraftPeriod || elements.reviewPeriod.value) { if (reviewDetail || !validDate(periodStart)) return; const draft = {version: 1, kind: activeReviewKind, periodStart, title: elements.reviewTitle.value, completedStepIds: reviewCompletedStepIds(), notes: elements.reviewBody.value}; try { if (typeof sessionStorage === "undefined") throw new Error("storage unavailable"); sessionStorage.setItem(reviewDraftKey(activeReviewKind, periodStart), JSON.stringify(draft)); } catch (_error) { showDraftStorageWarning(); } }
function clearReviewDraft(kind = activeReviewKind, periodStart = elements.reviewPeriod.value) { if (!REVIEW_STEPS[kind] || !validDate(periodStart)) return; try { if (typeof sessionStorage === "undefined") return; sessionStorage.removeItem(reviewDraftKey(kind, periodStart)); } catch (_error) { showDraftStorageWarning(); } }
function restoreReviewDraft(periodStart = elements.reviewPeriod.value) { try { if (typeof sessionStorage === "undefined") throw new Error("storage unavailable"); const raw = sessionStorage.getItem(reviewDraftKey(activeReviewKind, periodStart)); if (!raw) return; const draft = JSON.parse(raw); if (!draft || draft.version !== 1 || draft.kind !== activeReviewKind || draft.periodStart !== periodStart) return; elements.reviewTitle.value = typeof draft.title === "string" ? draft.title : elements.reviewTitle.value; elements.reviewBody.value = typeof draft.notes === "string" ? draft.notes : elements.reviewBody.value; const completed = new Set(Array.isArray(draft.completedStepIds) ? draft.completedStepIds : []); for (const control of elements.reviewStepControls) if (control) control.checked = completed.has(control.dataset.stepId); } catch (_error) { showDraftStorageWarning(); } }
function switchReviewDraftPeriod() { if (reviewDetail) return; const nextPeriod = elements.reviewPeriod.value; if (!validDate(nextPeriod) || nextPeriod === reviewDraftPeriod) return; if (reviewDraftPeriod) persistReviewDraft(reviewDraftPeriod); reviewDraftPeriod = nextPeriod; elements.reviewTitle.value = nextPeriod + (activeReviewKind === "daily" ? " Daily Review" : " Weekly Review"); elements.reviewBody.value = reviewNotesFromBody(activeReviewKind); configureReviewChecklist(); restoreReviewDraft(nextPeriod); syncReviewGuideMarks(); }
function configureReviewChecklist() { const steps = REVIEW_STEPS[activeReviewKind]; for (let index = 0; index < elements.reviewStepControls.length; index += 1) { const control = elements.reviewStepControls[index]; const label = elements.reviewStepLabels[index]; const step = steps[index]; if (control) { control.dataset.stepId = step ? step.id : ""; control.checked = false; } if (label) label.textContent = step ? step.label : ""; } setHidden(elements.reviewStep4Row, steps.length < 4); }
function syncDailyReviewGuideProgress() { if (activeReviewKind !== "daily" || !elements.dailyReviewProgress) return; const controls = elements.reviewStepControls.slice(0, REVIEW_STEPS.daily.length); const completed = controls.filter((control) => control && control.checked).length; elements.dailyReviewProgress.textContent = "3ステップ中" + completed + "件確認済み"; }
function syncReviewGuideMarks() { for (let index = 0; index < reviewGuideMarks.length; index += 1) { const mark = reviewGuideMarks[index]; const status = reviewGuideStatuses[index]; const control = elements.reviewStepControls[index]; if (!mark) continue; const checked = Boolean(control && control.checked); const text = checked ? "確認済み" : "未確認"; mark.textContent = text; mark.setAttribute("aria-pressed", checked ? "true" : "false"); if (status) status.textContent = "（" + text + "）"; } syncDailyReviewGuideProgress(); }
function reviewDate(value) { return typeof value === "string" ? value.slice(0, 10) : ""; }
function taskReviewDates(entity) { const fm = safeFrontmatter(entity); return [fm.due, fm.action_date, fm.scheduled_start].map(reviewDate).filter(Boolean); }
function reviewGuideFacts(snapshot, step) {
  const facts = snapshot && snapshot.facts && typeof snapshot.facts === "object" ? snapshot.facts : {};
  const entities = Array.isArray(snapshot && snapshot.entities) ? snapshot.entities : [];
  const tasks = entities.filter((entity) => entity.kind === "tasks" && safeFrontmatter(entity).status !== "done");
  const today = tokyoToday(); const end = new Date(today + "T00:00:00Z"); end.setUTCDate(end.getUTCDate() + 7); const endDate = end.toISOString().slice(0, 10);
  const title = (entity) => safeFrontmatter(entity).title || entity.id;
  if (step.id === "inbox" || step.id === "clear") { const count = Number.isInteger(facts.inbox_count) ? facts.inbox_count : 0; return [count ? "Inbox " + count + "件" : "Inboxは空です"]; }
  if (step.id === "near_term") { const items = tasks.filter((task) => safeFrontmatter(task).status === "doing" || taskReviewDates(task).some((date) => date >= today && date <= endDate)).map(title); return items.length ? items : ["進行中・7日内の予定や期限はありません"]; }
  if (step.id === "commitments") { const items = tasks.filter((task) => taskReviewDates(task).includes(today)).map(title); return items.length ? items : ["今日の約束はありません"]; }
  if (step.id === "current") {
    const counts = facts.task_status_counts && typeof facts.task_status_counts === "object" ? facts.task_status_counts : {};
    const summary = "Task: Next " + (counts.next || 0) + "件 / Doing " + (counts.doing || 0) + "件 / Waiting " + (counts.waiting || 0) + "件 / Scheduled " + (counts.scheduled || 0) + "件 / Someday " + (counts.someday || 0) + "件";
    const waiting = tasks.filter((task) => safeFrontmatter(task).status === "waiting").map((task) => "Waiting: " + title(task));
    return [summary, ...(waiting.length ? waiting : ["Waitingはありません"])];
  }
  if (step.id === "creative") {
    const projects=new Map(entities.filter(e=>e.kind==="projects"&&["not_started","doing"].includes(safeFrontmatter(e).status)).map(e=>[e.id,title(e)]));
    const missing = Array.isArray(facts.active_projects_without_next_action) ? facts.active_projects_without_next_action : [];
    const items = [...missing.map((id) => "Nextなし: " + (projects.get(id) || id)), ...tasks.filter(t=>safeFrontmatter(t).status==="someday"&&projects.has(safeFrontmatter(t).project_id)).map(t=>"Someday: "+title(t))];
    return items.length ? items : ["NextなしProject・Someday Taskはありません"];
  }
  const direction = facts.direction && typeof facts.direction === "object" ? facts.direction : {};
  const purpose = direction.purpose && (direction.purpose.title || direction.purpose.excerpt);
  const goals = entities.filter((entity) => entity.kind === "goals" && safeFrontmatter(entity).status === "active").map((entity) => "Active Goal: " + title(entity));
  return [purpose ? "Purpose: " + purpose : "Purposeは未登録です", ...(goals.length ? goals : ["Active Goalはありません"])];
}
function reviewTabFromQuery(){const query=new URLSearchParams(location.search||"");if(query.has("tab")){const tab=query.get("tab");return tab==="weekly"||tab==="daily"||tab==="allocation"?tab:"daily";}try{const saved=localStorage.getItem(REVIEW_TAB_KEY);return saved==="weekly"||saved==="daily"||saved==="allocation"?saved:"daily";}catch(_error){return "daily";}}
function persistReviewTab(tab){try{localStorage.setItem(REVIEW_TAB_KEY,tab);}catch(_error){}}
function reviewHelpKindFromQuery(){return new URLSearchParams(location.search||"").get("tab")==="weekly"?"weekly":"daily";}
function reviewGuidePurpose(kind){return kind==="daily"?"その日の判断を始める前に、未整理の気がかり、進行中の仕事、7日以内の予定・期限を確認し、今日引き受ける約束を実行可能な量に絞ります。":"約束・Project・Goalの全体を信頼できる状態に戻します。";}
function reviewDoneCondition(kind){return kind==="daily"?"Inboxを把握し、Doing・7日内の日付項目から今日対応すべき項目を特定し、実行可能な今日の約束をNotesへ記録できた。":"Clear、Current、Creative、Goal整合を確認し、次の行動を見渡せる。";}
function appendReviewStepExplanation(item, step) { const purpose = document.createElement("p"), action = document.createElement("p"); purpose.textContent = "目的: " + (step.purpose || step.description); action.textContent = "ここでやること: " + (step.action || step.description); item.append(purpose, action); }
function renderReviewGuide(snapshot) {
  elements.reviewGuideSteps.replaceChildren();reviewGuideMarks=[];reviewGuideStatuses=[];elements.reviewGuide.dataset.reviewKind = activeReviewKind;elements.reviewHelpLink.href="/help/reviews?tab="+activeReviewKind;elements.reviewGuidePurpose.textContent=reviewGuidePurpose(activeReviewKind);setHidden(elements.dailyReviewProgress,activeReviewKind!=="daily");const compact=globalThis.matchMedia&&globalThis.matchMedia("(max-width: 760px)").matches;
  REVIEW_STEPS[activeReviewKind].forEach((step, index) => {
    const item = document.createElement("li"); const title = document.createElement("h3"); title.textContent = compact && step.mobileLabel || step.label;
    const list = document.createElement("ul"); const items = reviewGuideFacts(snapshot, step);
    for (const fact of items.slice(0, 5)) appendListText(list, fact); if (items.length > 5) appendListText(list, "ほか" + (items.length - 5) + "件");
    const link = document.createElement("a"); link.href = step.href; link.textContent = "確認する";
    const control = elements.reviewStepControls[index]; const mark = document.createElement("button"); mark.type = "button"; mark.className = "secondary review-step-mark";
    reviewGuideMarks[index] = mark; mark.addEventListener("click", () => { if (!control || reviewDetail) return; control.checked = !control.checked; persistReviewDraft(); syncReviewGuideMarks(); });
    {
      const details = document.createElement("details"); details.className = "review-guide-step-details";
      const summary=document.createElement("summary");const number=document.createElement("span");number.className="review-guide-step-number";number.textContent=String(index+1);const status=document.createElement("span");status.className="review-guide-step-status";summary.append(number,title,status);reviewGuideStatuses[index]=status;
      const content = document.createElement("div"); content.className = "review-guide-step-content"; appendReviewStepExplanation(content, step); content.append(list, link, mark); details.append(summary, content); item.append(details);
    }
    elements.reviewGuideSteps.append(item);
  });
  syncReviewGuideMarks();
}
function roadmapFacts(snapshot) { return snapshot && snapshot.facts && snapshot.facts.roadmap && typeof snapshot.facts.roadmap === "object" ? snapshot.facts.roadmap : {}; }
function roadmapTitle(entity) { return entity ? (safeFrontmatter(entity).title || entity.id) : "不明"; }
function roadmapIdList(value) { return typeof value === "string" ? value.replace(/^\[|\]$/g, "").split(",").map(id => id.trim()).filter(Boolean) : []; }
function roadmapYearOf(date) { return typeof date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(date) ? Number(date.slice(0, 4)) : null; }
function roadmapUniqueIds(value, allowed) { return [...new Set(Array.isArray(value) ? value.filter((id) => typeof id === "string" && allowed.has(id)) : [])]; }
function roadmapAreaTone(areaId, areaOptions = []) { if (areaId === "__unassigned__") return "neutral"; const ids = Array.isArray(areaOptions) ? areaOptions.map((area) => area && area.id).filter(Boolean) : [], index = ids.indexOf(areaId), tones = ["mint", "cyan", "violet", "amber", "blue", "rose"]; if (index >= 0) return tones[index % tones.length]; let hash = 0; for (const char of String(areaId || "")) hash = ((hash * 31) + char.charCodeAt(0)) >>> 0; return tones[hash % tones.length]; }
function normalizeRoadmapYearMatrixState(value, snapshot, currentYear = new Date().getFullYear()) { const entities = Array.isArray(snapshot && snapshot.entities) ? snapshot.entities.filter((entity) => entity.archived !== true) : [], saved = value && typeof value === "object" ? value : {}, areas = new Set(entities.filter((entity) => entity.kind === "areas").map((entity) => entity.id)), goals = new Set(entities.filter((entity) => entity.kind === "goals" && safeFrontmatter(entity).status === "active").map((entity) => entity.id)), outcomeGoals = new Map(entities.filter((entity) => entity.kind === "roadmap_outcomes").map((entity) => [entity.id, safeFrontmatter(entity).goal_id])), goalsWithAreas = new Set(entities.filter((entity) => entity.kind === "projects" && areas.has(safeFrontmatter(entity).area_id)).map((entity) => safeFrontmatter(entity).goal_id || outcomeGoals.get(safeFrontmatter(entity).roadmap_outcome_id)).filter((id) => goals.has(id))); if ([...goals].some((id) => !goalsWithAreas.has(id))) areas.add("__unassigned__"); const latest = Math.max(currentYear + 5, ...entities.filter((entity) => (entity.kind === "goals" || entity.kind === "roadmap_outcomes") && safeFrontmatter(entity).status === "active").map((entity) => roadmapYearOf(safeFrontmatter(entity).target_date) || currentYear)); const fallback = Math.min(currentYear + 50, latest), end = Number(saved.endYear), validEnd = Number.isInteger(end) && end >= currentYear; return {areaIds: roadmapUniqueIds(saved.areaIds, areas), goalIds: roadmapUniqueIds(saved.goalIds, goals), scaleYears: Number(saved.scaleYears) === 5 ? 5 : 1, endYear: validEnd ? Math.min(currentYear + 50, end) : fallback, expandedGoalIds: roadmapUniqueIds(saved.expandedGoalIds, goals)}; }
function roadmapStoredMatrixState(snapshot) { let value = null; try { value = JSON.parse(window.localStorage.getItem(ROADMAP_VIEW_STORAGE_KEY) || "null"); } catch (_error) {} return normalizeRoadmapYearMatrixState(value, snapshot); }
function saveRoadmapMatrixState(snapshot) { roadmapYearMatrixState = normalizeRoadmapYearMatrixState(roadmapYearMatrixState, snapshot); try { window.localStorage.setItem(ROADMAP_VIEW_STORAGE_KEY, JSON.stringify(roadmapYearMatrixState)); } catch (_error) {} }
function projectRoadmapOverview(snapshot) {
  const entities = Array.isArray(snapshot && snapshot.entities) ? snapshot.entities.filter((entity) => entity.archived !== true && entity.kind !== "tasks") : [];
  const ids = new Map(entities.map((entity) => [entity.id, entity]));
  const facts = roadmapFacts(snapshot);
  const isActive = (entity) => safeFrontmatter(entity).status === "active";
  const lane = (id) => (facts.now_outcome_ids || []).includes(id) ? "Now" : (facts.next_outcome_ids || []).includes(id) ? "Next" : (facts.later_outcome_ids || []).includes(id) ? "Later" : "時期未設定";
  const areas = entities.filter((entity) => entity.kind === "areas");
  const areaById = new Map(areas.map((area) => [area.id, area]));
  const goals = entities.filter((entity) => entity.kind === "goals" && isActive(entity)).map((goal) => ({
    goal, outcomes: [], projects: entities.filter((project) => project.kind === "projects" && !safeFrontmatter(project).roadmap_outcome_id && safeFrontmatter(project).goal_id === goal.id), areaIds: [],
  }));
  const byGoal = new Map(goals.map((group) => [group.goal.id, group]));
  const outcomes = entities.filter((entity) => entity.kind === "roadmap_outcomes" && isActive(entity)).map((outcome) => {
    const frontmatter = safeFrontmatter(outcome);
    const targetDate = typeof frontmatter.target_date === "string" ? frontmatter.target_date : "";
    const year = /^\d{4}-\d{2}-\d{2}$/.test(targetDate) ? Number(targetDate.slice(0, 4)) : null;
    const item = {outcome, goal: ids.get(frontmatter.goal_id) || null, targetDate, year, projects: entities.filter((project) => project.kind === "projects" && safeFrontmatter(project).roadmap_outcome_id === outcome.id), cycles: entities.filter((cycle) => cycle.kind === "cycles" && ["active", "planned"].includes(safeFrontmatter(cycle).status) && roadmapIdList(safeFrontmatter(cycle).outcome_ids).includes(outcome.id)), state: lane(outcome.id)};
    const group = byGoal.get(frontmatter.goal_id);
    if (group) group.outcomes.push(item);
    return item;
  });
  for (const group of goals) group.areaIds = [...new Set([...group.projects, ...group.outcomes.flatMap((item) => item.projects)].map((project) => safeFrontmatter(project).area_id).filter((id) => areaById.has(id)))].sort();
  const usedAreaIds = new Set(goals.flatMap((group) => group.areaIds));
  const areaOptions = areas.filter((area) => usedAreaIds.has(area.id)).map((area) => ({id: area.id, title: roadmapTitle(area)}));
  if (goals.some((group) => group.areaIds.length === 0)) areaOptions.push({id: "__unassigned__", title: "Area未設定"});
  return {entities, goals, outcomes, areaOptions};
}
function roadmapYearFilterGoals(snapshot, state, currentYear = new Date().getFullYear()) { const p = projectRoadmapOverview(snapshot), s = normalizeRoadmapYearMatrixState(state, snapshot, currentYear); return p.goals.filter((group) => !s.areaIds.length || s.areaIds.some((id) => id === "__unassigned__" ? group.areaIds.length === 0 : group.areaIds.includes(id))); }
function projectRoadmapYearMatrix(snapshot, state, currentYear = new Date().getFullYear()) { const p = projectRoadmapOverview(snapshot), s = normalizeRoadmapYearMatrixState(state, snapshot, currentYear), years = Array.from({length: s.endYear - currentYear + 1}, (_v, i) => currentYear + i), goals = roadmapYearFilterGoals(snapshot, s, currentYear).filter((group) => !s.goalIds.length || s.goalIds.includes(group.goal.id)).map((group) => { const projects = [...new Map([...group.projects, ...group.outcomes.flatMap((outcome) => outcome.projects)].map((project) => [project.id, project])).values()], buckets = {past: [], years: Object.fromEntries(years.map((year) => [String(year), []])), outOfRange: [], unscheduled: []}; for (const outcome of group.outcomes) { const year = outcome.year, areaIds = [...new Set(outcome.projects.map((project) => safeFrontmatter(project).area_id).filter(Boolean))].sort(), item = {id: outcome.outcome.id, title: roadmapTitle(outcome.outcome), targetDate: outcome.targetDate, state: outcome.state, projectCount: outcome.projects.length, areaIds: areaIds.length ? areaIds : ["__unassigned__"], kind: "outcome"}; if (year === null) buckets.unscheduled.push(item); else if (year < currentYear) buckets.past.push(item); else if (year > s.endYear) buckets.outOfRange.push(item); else buckets.years[String(year)].push(item); } const year = roadmapYearOf(safeFrontmatter(group.goal).target_date); return {id: group.goal.id, title: roadmapTitle(group.goal), areaIds: group.areaIds, projectCount: projects.length, milestone: year === null ? null : {id: group.goal.id, title: roadmapTitle(group.goal), year, targetDate: safeFrontmatter(group.goal).target_date, kind: "milestone"}, buckets}; }); return {...p, state: s, years, goals}; }
function roadmapAreaLabel(areaId, areaOptions = []) { const entry = Array.isArray(areaOptions) ? areaOptions.find((area) => area.id === areaId) : null; return entry ? entry.title : (areaId === "__unassigned__" ? "Area未設定" : areaId || "Area未設定"); }
function roadmapAppendAreaDot(parent, areaId, areaOptions) { const dot = document.createElement("span"); dot.className = "roadmap-area-dot roadmap-area-" + roadmapAreaTone(areaId, areaOptions); dot.setAttribute("aria-hidden", "true"); parent.append(dot); return dot; }
function roadmapYearMatrixItem(item, areaOptions = []) { if (item.kind === "milestone") { const node = document.createElement("p"); node.className = "roadmap-goal-milestone"; node.textContent = "Goal milestone: " + item.title; return node; } return P.renderRoadmapOutcomeCard({item, areaOptions, areaTone: roadmapAreaTone, appendAreaDot: roadmapAppendAreaDot, areaLabel: roadmapAreaLabel, onOpen: (node) => selectRoadmapOutcome(item.id, "", node, true), onDrag: roadmapBeginYearDrag}); }
function roadmapMatrixColumns(matrix) { const years = matrix.years; if (matrix.state.scaleYears === 1) return years.map((year) => ({key: String(year), label: String(year), years: [year]})); const columns = []; for (let index = 0; index < years.length; index += 5) { const group = years.slice(index, index + 5); columns.push({key: String(group[0]), label: group.length === 1 ? String(group[0]) : group[0] + "–" + group[group.length - 1], years: group}); } return columns; }
function renderRoadmapOverview(snapshot) { const matrix = projectRoadmapYearMatrix(snapshot, roadmapYearMatrixState || roadmapStoredMatrixState(snapshot)), add = (parent, tag, text, cls) => { const node = document.createElement(tag); node.className = cls || ""; node.textContent = text; parent.append(node); return node; }, toggle = (kind, id) => { const key = kind + "Ids", ids = matrix.state[key]; roadmapYearMatrixState = {...matrix.state, [key]: ids.includes(id) ? ids.filter((value) => value !== id) : [...ids, id]}; saveRoadmapMatrixState(snapshot); renderRoadmapOverview(snapshot); };
  roadmapYearMatrixState = matrix.state; elements.roadmapDirectionStrip.replaceChildren(); elements.roadmapAreaFilters.replaceChildren(); elements.roadmapGoalFilters.replaceChildren(); elements.roadmapBoard.replaceChildren(); add(elements.roadmapDirectionStrip, "p", "Purpose: " + (matrix.entities.find((entity) => entity.kind === "purposes") ? roadmapTitle(matrix.entities.find((entity) => entity.kind === "purposes")) : "未登録") + " → Vision: " + (matrix.entities.find((entity) => entity.kind === "visions" && safeFrontmatter(entity).status === "active") ? roadmapTitle(matrix.entities.find((entity) => entity.kind === "visions" && safeFrontmatter(entity).status === "active")) : "未登録"));
  const filters = (parent, entries, key) => { const all = document.createElement("button"); all.type = "button"; all.className = "secondary roadmap-area-filter-button"; all.textContent = "すべて"; all.setAttribute("aria-pressed", String(matrix.state[key].length === 0)); all.addEventListener("click", () => { roadmapYearMatrixState = {...matrix.state, [key]: []}; saveRoadmapMatrixState(snapshot); renderRoadmapOverview(snapshot); }); parent.append(all); for (const entry of entries) { const button = document.createElement("button"); button.type = "button"; button.className = "secondary roadmap-area-filter-button"; if (key === "areaIds") { button.classList.add("roadmap-area-accent-" + roadmapAreaTone(entry.id, matrix.areaOptions)); roadmapAppendAreaDot(button, entry.id, matrix.areaOptions); } button.append(document.createTextNode(entry.title)); button.setAttribute("aria-pressed", String(matrix.state[key].includes(entry.id))); button.addEventListener("click", () => toggle(key.slice(0, -3), entry.id)); parent.append(button); } }; filters(elements.roadmapAreaFilters, matrix.areaOptions, "areaIds"); filters(elements.roadmapGoalFilters, roadmapYearFilterGoals(snapshot, matrix.state).map((group) => ({id: group.goal.id, title: roadmapTitle(group.goal)})), "goalIds");
  elements.roadmapScaleYears.value = String(matrix.state.scaleYears); elements.roadmapEndYear.replaceChildren(); for (let year = new Date().getFullYear(); year <= new Date().getFullYear() + 50; year += 1) { const option = document.createElement("option"); option.value = String(year); option.textContent = String(year); option.selected = year === matrix.state.endYear; elements.roadmapEndYear.append(option); } elements.roadmapScaleYears.onchange = () => { roadmapYearMatrixState = {...matrix.state, scaleYears: Number(elements.roadmapScaleYears.value)}; saveRoadmapMatrixState(snapshot); renderRoadmapOverview(snapshot); }; elements.roadmapEndYear.onchange = () => { roadmapYearMatrixState = {...matrix.state, endYear: Number(elements.roadmapEndYear.value)}; saveRoadmapMatrixState(snapshot); renderRoadmapOverview(snapshot); };
  if (!matrix.goals.length) { add(elements.roadmapBoard, "p", "該当するGoalはありません。", "muted"); return; } const columns = roadmapMatrixColumns(matrix), table = document.createElement("table"), head = document.createElement("thead"), row = document.createElement("tr"); table.className = "roadmap-year-matrix"; const goalHead = add(row, "th", "Goal", "roadmap-year-goal-head"); goalHead.scope = "col"; for (const column of [{key: "past", label: "過去"}, ...columns, {key: "outOfRange", label: "範囲外"}, {key: "unscheduled", label: "時期未設定"}]) { const cell = add(row, "th", column.label); cell.scope = "col"; } head.append(row); table.append(head); const body = document.createElement("tbody"), accordions = document.createElement("div"); accordions.className = "roadmap-goal-accordions"; for (const goal of matrix.goals) { const tr = document.createElement("tr"), name = add(tr, "th", "", "roadmap-year-goal"), primaryArea = goal.areaIds[0] || "__unassigned__"; roadmapAppendAreaDot(name, primaryArea, matrix.areaOptions); name.append(document.createTextNode(goal.title)); name.scope = "row"; add(name, "span", "Project " + goal.projectCount + "件", "roadmap-project-count"); const mobile = document.createElement("details"); mobile.className = "roadmap-goal-accordion"; mobile.open = matrix.state.expandedGoalIds.includes(goal.id); const summary = add(mobile, "summary", ""); roadmapAppendAreaDot(summary, primaryArea, matrix.areaOptions); summary.append(document.createTextNode(goal.title + " / Project " + goal.projectCount + "件")); mobile.addEventListener("toggle", () => { const ids = matrix.state.expandedGoalIds; roadmapYearMatrixState = {...matrix.state, expandedGoalIds: mobile.open ? [...new Set([...ids, goal.id])] : ids.filter((id) => id !== goal.id)}; saveRoadmapMatrixState(snapshot); }); const mobileBody = document.createElement("div"); mobileBody.className = "roadmap-mobile-years"; for (const column of [{key: "past", label: "過去", years: []}, ...columns, {key: "outOfRange", label: "範囲外", years: []}, {key: "unscheduled", label: "時期未設定", years: []}]) { const items = column.years.length ? column.years.flatMap((year) => goal.buckets.years[String(year)]) : goal.buckets[column.key]; const milestone = goal.milestone && (column.years.includes(goal.milestone.year) || (column.key === "past" && goal.milestone.year < matrix.years[0]) || (column.key === "outOfRange" && goal.milestone.year > matrix.years[matrix.years.length - 1]) || (column.key === "unscheduled" && goal.milestone.year === null)) ? goal.milestone : null; const cell = document.createElement("section"); cell.className = "roadmap-mobile-year"; add(cell, "h4", column.label); if (milestone) cell.append(roadmapYearMatrixItem(milestone, matrix.areaOptions)); for (const item of items) cell.append(roadmapYearMatrixItem(item, matrix.areaOptions)); if (items.length === 0 && !milestone) add(cell, "p", "—", "muted"); mobileBody.append(cell); const desktop = document.createElement("td"); if (milestone) desktop.append(roadmapYearMatrixItem(milestone, matrix.areaOptions)); for (const item of items) desktop.append(roadmapYearMatrixItem(item, matrix.areaOptions)); if (!items.length && !milestone) desktop.textContent = "—"; tr.append(desktop); } mobile.append(mobileBody); accordions.append(mobile); body.append(tr); } table.append(body); const scroll = document.createElement("div"); scroll.className = "roadmap-year-matrix-scroll"; scroll.append(table); elements.roadmapBoard.append(scroll, accordions); }
let roadmapDetailTrigger = null;
let roadmapDetailEditMode = false;
let roadmapDetailDraft = null;
let roadmapDetailScroll = 0;
let roadmapSheetStartY = null;
let roadmapSheetStartHeight = null;
let roadmapSheetPointerId = null;
let roadmapSelectedOutcomeId = "";
let roadmapSelectedProjectId = "";
let roadmapDetailRequest = 0;
const P=globalThis.window&&window.ProjectTaskBoard;
const detailPanels=P.createEntityDetailController({elements,apiRequest,entityDetailPath,safeFrontmatter,taskStatusLabel,clarifyHref,openTaskEditor:mountTaskPanelEditor,renderProject:renderProjectDetail,mountedProject:(detail,snapshot)=>{projectDetail=detail;directionSnapshot=snapshot;},failed:showRequestError});
function roadmapProjectDetailPath(id) { return entityDetailPath("projects", id); }
function roadmapQuery(outcomeId, projectId = "") { const url = new URL(window.location.href); if (outcomeId) url.searchParams.set("outcome", outcomeId); else url.searchParams.delete("outcome"); if (projectId) url.searchParams.set("project", projectId); else url.searchParams.delete("project"); return url.pathname + (url.searchParams.toString() ? "?" + url.searchParams : ""); }
function roadmapDateForYear(date, year) { const parts = typeof date === "string" ? date.split("-") : []; const month = parts.length === 3 ? Number(parts[1]) : 12, day = parts.length === 3 ? Number(parts[2]) : 31; const maxDay = new Date(Date.UTC(year, month, 0)).getUTCDate(); return String(year).padStart(4, "0") + "-" + String(month).padStart(2, "0") + "-" + String(Math.min(day, maxDay)).padStart(2, "0"); }
function roadmapPointerTarget(x, y) { const name = "elementFrom" + String.fromCharCode(80, 111, 105, 110, 116); return typeof document[name] === "function" ? document[name](x, y) : document.querySelector("td:hover, .roadmap-mobile-year:hover"); }
function roadmapDropYear(target) { const cell = target && target.closest("[data-roadmap-year], td, .roadmap-mobile-year"); if (!cell) return null; if (/^\d{4}$/.test(cell.dataset.roadmapYear || "")) return Number(cell.dataset.roadmapYear); const mobileHeading = cell.querySelector && cell.querySelector("h4"); if (mobileHeading && /^\d{4}$/.test(mobileHeading.textContent || "")) { cell.dataset.roadmapYear = mobileHeading.textContent; return Number(mobileHeading.textContent); } const row = cell.parentElement, table = row && row.closest("table"), index = row ? [...row.children].indexOf(cell) : -1, heading = table && index >= 0 ? table.querySelector("thead tr")?.children[index] : null; if (heading && /^\d{4}$/.test(heading.textContent || "")) { cell.dataset.roadmapYear = heading.textContent; return Number(heading.textContent); } return null; }
function roadmapFocusables() { return [...elements.roadmapOutcomeDetailPanel.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])')].filter((node) => !node.hidden && node.offsetParent !== null); }
function roadmapDetailDraftFromDetail(detail) { const fm = safeFrontmatter(detail); return {id: detail.id, baseHash: detail.content_hash, title: fm.title || "", goalId: fm.goal_id || "", targetDate: fm.target_date || "", body: detail.body || "", dirty: false, conflict: false, projectChanges: []}; }
function syncRoadmapDetailModeTabs() { if (elements.roadmapBrowseTab) elements.roadmapBrowseTab.setAttribute("aria-pressed", String(!roadmapDetailEditMode)); if (elements.roadmapDetailEditTab) elements.roadmapDetailEditTab.setAttribute("aria-pressed", String(roadmapDetailEditMode)); }
function roadmapConfirmDiscardDraft() { return !(roadmapDetailDraft && roadmapDetailDraft.dirty) || window.confirm("未保存の変更を破棄しますか？"); }
function roadmapRestoreDetailRoute() { if (roadmapSelectedOutcomeId) setRoadmapDetailRoute(roadmapSelectedOutcomeId, roadmapSelectedProjectId, true); else setRoadmapDetailRoute("", "", true); }
function closeRoadmapDetail(force = false, updateRoute = true) { if (!force && !roadmapConfirmDiscardDraft()) return false; if (P.closeRoadmapDetail(elements) === false) return false; roadmapDetailRequest += 1; roadmapDetailDraft = null; roadmapDetailEditMode = false; roadmapSelectedOutcomeId = ""; roadmapSelectedProjectId = ""; roadmapSheetStartY = null; roadmapSheetStartHeight = null; roadmapSheetPointerId = null; elements.roadmapOutcomeDetailPanel.style.removeProperty("--roadmap-sheet-height"); elements.roadmapOutcomeDetailPanel.hidden = true; elements.roadmapOutcomeDetailPanel.removeAttribute("data-expanded"); elements.roadmapDetailBackdrop.hidden = true; document.body.classList.remove("roadmap-detail-open"); if (updateRoute) history.pushState({}, "", roadmapQuery("")); syncRoadmapDetailModeTabs(); const trigger = roadmapDetailTrigger; roadmapDetailTrigger = null; if (trigger && trigger.isConnected) trigger.focus(); return true; }
function setRoadmapDetailRoute(outcomeId, projectId = "", replace = false) { history[replace ? "replaceState" : "pushState"]({roadmapOutcomeId: outcomeId, roadmapProjectId: projectId}, "", roadmapQuery(outcomeId, projectId)); }
function roadmapBundleOperation(detail, changes = []) { const draft = roadmapDetailDraft || {}; return {action: "roadmap_outcome_bundle_update", kind: "roadmap_outcomes", id: detail.id, base_hash: draft.baseHash || detail.content_hash, fields: {title: draft.title, goal_id: draft.goalId, target_date: draft.targetDate}, body: draft.body, project_changes: changes}; }
function roadmapAreaTitle(areaId) { const area = roadmapSnapshot && Array.isArray(roadmapSnapshot.entities) ? roadmapSnapshot.entities.find((entity) => entity.kind === "areas" && entity.id === areaId) : null; return area ? roadmapTitle(area) : (areaId || "Area未設定"); }
function roadmapRenderProjectCard(project, outcome) { return P.renderRoadmapProjectCard({project, outcome, safeFrontmatter, roadmapTitle, roadmapAreaTitle, period: projectPeriod, onOpen(outcomeId, projectId) { roadmapDetailScroll = elements.roadmapOutcomeDetailPanel.scrollTop; setRoadmapDetailRoute(outcomeId, projectId); selectRoadmapOutcome(outcomeId, projectId); }}); }
async function selectRoadmapOutcome(id, projectId = "", trigger = null, updateRoute = false) {
  if (!id || !elements.roadmapOutcomeDetailPanel) return false; if (roadmapSelectedOutcomeId && roadmapSelectedOutcomeId !== id && !roadmapConfirmDiscardDraft()) return false; const detailRequest = ++roadmapDetailRequest; if (updateRoute) setRoadmapDetailRoute(id, projectId); roadmapSelectedOutcomeId = id; roadmapSelectedProjectId = projectId; if (!roadmapDetailTrigger && trigger && trigger.classList && trigger.classList.contains("roadmap-year-outcome")) roadmapDetailTrigger = trigger; elements.roadmapOutcomeDetailPanel.hidden = false; elements.roadmapDetailBackdrop.hidden = false; document.body.classList.add("roadmap-detail-open"); P.restoreRoadmapDetail(elements); elements.roadmapOutcomeDetailContent.textContent = "詳細を読み込み中です。"; elements.roadmapOutcomeDetailHeading.focus();
  try { const detail = await apiRequest(entityDetailPath("roadmap_outcomes", id)); if (detailRequest !== roadmapDetailRequest) return; const item = projectRoadmapOverview(roadmapSnapshot).outcomes.find((value) => value.outcome.id === id); const fm = safeFrontmatter(detail); elements.roadmapOutcomeDetailHeading.textContent = roadmapTitle(detail); elements.roadmapOutcomeDetailContent.replaceChildren();
    if (projectId) { const project = await apiRequest(roadmapProjectDetailPath(projectId)); if (detailRequest !== roadmapDetailRequest) return; projectDetail = project; directionSnapshot = roadmapSnapshot; P.mountRoadmapDetail({elements,detail:project,snapshot:roadmapSnapshot,outcomeId:id,render:renderProjectDetail,query:roadmapQuery,onBack:()=>{setRoadmapDetailRoute(id);void selectRoadmapOutcome(id);}}); elements.roadmapOutcomeDetailPanel.scrollTop = 0; elements.roadmapOutcomeDetailHeading.focus(); return; }
    if (roadmapDetailEditMode && (!roadmapDetailDraft || roadmapDetailDraft.id !== detail.id)) roadmapDetailDraft = roadmapDetailDraftFromDetail(detail); if (roadmapDetailDraft && roadmapDetailDraft.id === detail.id && roadmapDetailDraft.dirty && roadmapDetailDraft.baseHash !== detail.content_hash) roadmapDetailDraft.conflict = true; syncRoadmapDetailModeTabs(); const summary = document.createElement("p"), goal = document.createElement("p"), body = document.createElement("pre"), projectsHeading = document.createElement("h4"), cards = document.createElement("div"), projects = await Promise.all((item ? item.projects : []).map((project) => apiRequest(roadmapProjectDetailPath(project.id)).catch(() => project))); if (detailRequest !== roadmapDetailRequest) return; summary.textContent = "状態: " + (fm.status || "未設定") + " / 目標日: " + (fm.target_date || "未設定"); goal.textContent = "Goal: " + (item && item.goal ? roadmapTitle(item.goal) : fm.goal_id || "未設定"); body.textContent = detail.body || "本文なし"; projectsHeading.textContent = "関連Project"; cards.className = "roadmap-project-cards"; for (const project of P.sortRoadmapProjectsByEstimatedPeriod(projects)) cards.append(roadmapRenderProjectCard(project, detail)); if (!cards.childElementCount) cards.textContent = "Area未設定（Projectを追加して設定）";
    const actions = document.createElement("div"); actions.className = "form-actions"; actions.append(actionButton("年を変更", () => roadmapChooseYear(detail), true)); if (roadmapDetailEditMode) actions.append(actionButton("保存", () => roadmapSaveDetail(detail), true)); else actions.append(actionButton("編集", () => { roadmapDetailEditMode = true; roadmapDetailDraft = roadmapDetailDraftFromDetail(detail); syncRoadmapDetailModeTabs(); selectRoadmapOutcome(id); })); elements.roadmapOutcomeDetailContent.append(summary, goal, body, projectsHeading, cards, actions);
    if (roadmapDetailEditMode && roadmapDetailDraft) roadmapRenderDetailEditor(detail, item, projects); elements.roadmapOutcomeDetailPanel.scrollTop = roadmapDetailScroll || 0; roadmapDetailScroll = 0; elements.roadmapOutcomeDetailHeading.focus();
  } catch (error) { if (detailRequest !== roadmapDetailRequest) return; P.restoreRoadmapDetail(elements); elements.roadmapOutcomeDetailContent.textContent = "Outcome詳細を読み込めませんでした。"; showRequestError(error); }
}
function roadmapProjectStatusAllowed(v){return["not_started","doing","on_hold","completed","dropped"].includes(v)}
function roadmapResolveAreaInput(value, areas) { const normalized = typeof value === "string" ? value.trim().toLowerCase() : ""; if (!normalized) return {kind: "empty", area: null}; const matches = areas.filter((area) => area.id.toLowerCase() === normalized || roadmapTitle(area).toLowerCase().includes(normalized)); return matches.length === 1 ? {kind: "one", area: matches[0]} : {kind: matches.length ? "many" : "none", area: null}; }
async function roadmapBuildProjectArchiveChange(projectId) { const taskSnapshot = await apiRequest("/api/v1/snapshot"), candidateIds = taskSnapshot.entities.filter((task) => task.kind === "tasks" && task.archived !== true && safeFrontmatter(task).project_id === projectId).map((task) => task.id), allTaskDetails = (await Promise.all(candidateIds.map((id) => apiRequest(entityDetailPath("tasks", id))))).filter((task) => task && task.kind === "tasks" && task.archived !== true && safeFrontmatter(task).project_id === projectId), unfinishedTaskDetails = allTaskDetails.filter((task) => safeFrontmatter(task).status !== "done"), project = await apiRequest(roadmapProjectDetailPath(projectId)); return {project, allTaskDetails, unfinishedTaskDetails, change: {action: "archive", id: project.id, base_hash: project.content_hash, task_cascade: allTaskDetails.map((task) => ({id: task.id, base_hash: task.content_hash}))}}; }
function roadmapRenderDetailEditor(detail, item, loadedProjects = []) {
  const draft = roadmapDetailDraft, form = document.createElement("div"); form.className = "roadmap-detail-editor";
  const field = (label, control, parent = form) => { const wrap = document.createElement("label"); wrap.textContent = label; wrap.append(control); parent.append(wrap); };
  const title = document.createElement("input"); title.value = draft.title; title.maxLength = 300; title.addEventListener("input", () => { draft.title = title.value; draft.dirty = true; }); field("Outcome名", title);
  const goal = document.createElement("select"); for (const entity of roadmapSnapshot.entities.filter((entity) => entity.kind === "goals" && entity.archived !== true)) { const option = document.createElement("option"); option.value = entity.id; option.textContent = roadmapTitle(entity); option.selected = entity.id === draft.goalId; goal.append(option); } goal.addEventListener("change", () => { draft.goalId = goal.value; draft.dirty = true; }); field("Goal", goal);
  const date = document.createElement("input"); date.type = "date"; date.value = draft.targetDate; date.addEventListener("change", () => { draft.targetDate = date.value; draft.dirty = true; }); field("目標日", date);
  const body = document.createElement("textarea"); body.rows = 6; body.value = draft.body; body.addEventListener("input", () => { draft.body = body.value; draft.dirty = true; }); field("本文", body);
  const stage = (change) => { if (change.id) draft.projectChanges = draft.projectChanges.filter((value) => value.id !== change.id); draft.projectChanges.push(change); draft.dirty = true; selectRoadmapOutcome(detail.id); };
  const areas = roadmapSnapshot.entities.filter((entity) => entity.kind === "areas" && entity.archived !== true && safeFrontmatter(entity).status !== "archived");
  const projectForm = (project, onSubmit) => {
    const projectEditor = document.createElement("form"); projectEditor.className = "roadmap-project-edit";
    const fm = safeFrontmatter(project || {}), projectTitle = document.createElement("input"), status = document.createElement("select"), areaSelect = document.createElement("select"), plannedStart = document.createElement("input"), plannedEnd = document.createElement("input"), projectBody = document.createElement("textarea"), submit = document.createElement("button");
    projectTitle.required = true; projectTitle.maxLength = 300; projectTitle.value = fm.title || "";
    for (const value of ["not_started","doing","on_hold","completed","dropped"]) { const option = document.createElement("option"); option.value = value; option.textContent = value; option.selected = value === (fm.status || "not_started"); status.append(option); }
    const unassigned = document.createElement("option"); unassigned.value = ""; unassigned.textContent = "Area未設定"; unassigned.selected = !(fm.area_id || ""); areaSelect.append(unassigned);
    for (const area of areas) { const option = document.createElement("option"); option.value = area.id; option.textContent = roadmapTitle(area); option.selected = area.id === fm.area_id; areaSelect.append(option); }
    plannedStart.type = "date"; plannedStart.value = fm.planned_start_date || ""; plannedEnd.type = "date"; plannedEnd.value = fm.planned_end_date || ""; const validatePeriod = () => projectPeriod.validate(plannedStart, plannedEnd); for (const input of [plannedStart, plannedEnd]) input.addEventListener("input", validatePeriod); projectBody.rows = 4; projectBody.value = project && project.body || "";
    field("Project名", projectTitle, projectEditor); field("状態", status, projectEditor); field("Area", areaSelect, projectEditor); field("目安期間（開始）", plannedStart, projectEditor); field("目安期間（終了）", plannedEnd, projectEditor); field("本文", projectBody, projectEditor);
    submit.type = "submit"; submit.className = "secondary"; submit.textContent = project ? "Projectの変更を追加" : "Projectを追加"; projectEditor.append(submit);
    projectEditor.addEventListener("submit", (event) => { event.preventDefault(); const periodError = validatePeriod(); if (!projectEditor.reportValidity() || periodError || !roadmapProjectStatusAllowed(status.value)) { if (periodError) (periodError.includes("終了日") ? plannedEnd : plannedStart).focus(); return; } onSubmit({title: projectTitle.value.trim(), status: status.value, area_id: areaSelect.value, planned_start_date: plannedStart.value, planned_end_date: plannedEnd.value, body: projectBody.value}); });
    return projectEditor;
  };
  const addDetails = document.createElement("details"), addSummary = document.createElement("summary"); addSummary.textContent = "＋ Projectを追加"; addDetails.append(addSummary, projectForm(null, (fields) => { if (!fields.title) return; stage({action: "create", fields: {title: fields.title, status: fields.status, area_id: fields.area_id, roadmap_outcome_id: detail.id, planned_start_date: fields.planned_start_date, planned_end_date: fields.planned_end_date}, body: fields.body}); }));
  const move = actionButton("既存Projectを移動", async () => { const candidates = roadmapSnapshot.entities.filter((entity) => entity.kind === "projects" && entity.archived !== true && safeFrontmatter(entity).roadmap_outcome_id !== detail.id); const answer = (window.prompt("移動するProjectのIDまたはタイトルの一部\n" + candidates.map((project) => project.id + " — " + roadmapTitle(project)).join("\n")) || "").trim().toLowerCase(); const matches = candidates.filter((project) => project.id.toLowerCase() === answer || roadmapTitle(project).toLowerCase().includes(answer)); if (matches.length !== 1) { if (matches.length > 1) window.alert("複数候補です。IDで選択してください:\n" + matches.map((project) => project.id + " — " + roadmapTitle(project)).join("\n")); return; } const current = await apiRequest(roadmapProjectDetailPath(matches[0].id)); stage({action: "move", id: current.id, base_hash: current.content_hash}); }, true); form.append(addDetails, move);
  const projectById = new Map(loadedProjects.map((project) => [project.id, project]));
  for (const project of item ? item.projects : []) { const current = projectById.get(project.id) || project, section = document.createElement("details"), summary = document.createElement("summary"); summary.textContent = "Projectを変更: " + roadmapTitle(current); section.append(summary, projectForm(current, async (fields) => { const fresh = await apiRequest(roadmapProjectDetailPath(project.id)); stage({action: "update", id: fresh.id, base_hash: fresh.content_hash, fields: {title: fields.title, status: fields.status, area_id: fields.area_id, planned_start_date: fields.planned_start_date, planned_end_date: fields.planned_end_date}, body: fields.body}); })); section.append(actionButton("アーカイブ", async () => { try { const archive = await roadmapBuildProjectArchiveChange(project.id); stage(archive.change); } catch (error) { showRequestError(error); } }, true)); form.append(section); }
  elements.roadmapOutcomeDetailContent.append(form);
}
function roadmapSaveDetail(detail) { const draft = roadmapDetailDraft; if (!draft) return; if (draft.conflict || draft.baseHash !== detail.content_hash) { draft.conflict = true; showNotice("Outcomeは他で更新されています。再読み込みして変更を確認してください。"); return; } previewMutation(roadmapBundleOperation(detail, draft.projectChanges || []), () => { roadmapDetailDraft = null; roadmapDetailEditMode = false; showNotice("OutcomeとProjectを保存しました。"); }, elements.roadmapOutcomeDetailHeading); }
function roadmapChooseYear(detail) { const picker = document.createElement("select"); const now = new Date().getFullYear(); for (let year = now - 5; year <= now + 50; year += 1) { const option = document.createElement("option"); option.value = String(year); option.textContent = String(year) + "年"; picker.append(option); } const dialog = document.createElement("dialog"); dialog.id = "roadmap-year-dialog"; dialog.setAttribute("aria-label", "年を変更"); const save = document.createElement("button"); save.type = "button"; save.className = "primary"; save.textContent = "変更"; save.addEventListener("click", () => { const targetDate = roadmapDateForYear(safeFrontmatter(detail).target_date, Number(picker.value)); dialog.close(); dialog.remove(); previewMutation({action: "update", kind: "roadmap_outcomes", id: detail.id, base_hash: detail.content_hash, fields: {target_date: targetDate}}, () => showNotice("年を変更しました。"), elements.roadmapOutcomeDetailHeading); }); const cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "secondary"; cancel.textContent = "キャンセル"; cancel.addEventListener("click", () => { dialog.close(); dialog.remove(); }); dialog.append(document.createTextNode("年を選択: "), picker, cancel, save); document.body.append(dialog); dialog.showModal(); picker.focus(); }
function roadmapBeginYearDrag(node, item) { let timer = null, dragging = false, scrolling = false, suppressClick = false, startX = 0, startY = 0, scrollStart = 0, scrollHost = null; const clear = (event) => { clearTimeout(timer); timer = null; if (event && node.hasPointerCapture(event.pointerId)) node.releasePointerCapture(event.pointerId); node.classList.remove("roadmap-year-dragging"); document.querySelectorAll(".roadmap-year-drop-target").forEach((value) => value.classList.remove("roadmap-year-drop-target")); };
  node.addEventListener("pointerdown", (event) => { if (event.button !== 0) return; startX = event.clientX; startY = event.clientY; scrolling = false; scrollHost = node.closest(".roadmap-outcome-detail") || document.scrollingElement; scrollStart = scrollHost ? scrollHost.scrollTop : 0; const begin = () => { dragging = true; node.classList.add("roadmap-year-dragging"); node.setPointerCapture(event.pointerId); }; if (event.pointerType === "mouse") { event.preventDefault(); node.focus({preventScroll: true}); begin(); } else timer = window.setTimeout(begin, 350); });
  node.addEventListener("pointermove", (event) => { if (!dragging && Math.hypot(event.clientX - startX, event.clientY - startY) > 8) { clearTimeout(timer); timer = null; scrolling = event.pointerType !== "mouse"; if (scrolling) { event.preventDefault(); if (scrollHost) scrollHost.scrollTop = scrollStart + startY - event.clientY; } } if (!dragging) return; event.preventDefault(); const cell = roadmapPointerTarget(event.clientX, event.clientY); document.querySelectorAll(".roadmap-year-drop-target").forEach((value) => value.classList.remove("roadmap-year-drop-target")); if (roadmapDropYear(cell)) cell.classList.add("roadmap-year-drop-target"); const scroll = node.closest(".roadmap-year-matrix-scroll"); if (scroll && event.pointerType === "mouse") { const bounds = scroll.getBoundingClientRect(); if (event.clientX > bounds.right - 40) scroll.scrollLeft += 20; if (event.clientX < bounds.left + 40) scroll.scrollLeft -= 20; } });
  node.addEventListener("pointerup", (event) => { if (!dragging) { if (scrolling) { suppressClick = true; window.setTimeout(() => { suppressClick = false; }, 0); } clear(event); scrolling = false; return; } const moved = Math.hypot(event.clientX - startX, event.clientY - startY) > 8; if (!moved) { clear(event); dragging = false; return; } suppressClick = true; const year = roadmapDropYear(roadmapPointerTarget(event.clientX, event.clientY)); clear(event); dragging = false; apiRequest(entityDetailPath("roadmap_outcomes", item.id)).then((detail) => year ? previewMutation({action: "update", kind: "roadmap_outcomes", id: detail.id, base_hash: detail.content_hash, fields: {target_date: roadmapDateForYear(safeFrontmatter(detail).target_date, year)}}, () => showNotice("年を変更しました。"), node) : roadmapChooseYear(detail)).catch(showRequestError); window.setTimeout(() => { suppressClick = false; }, 0); });
  node.addEventListener("click", (event) => { if (suppressClick) { event.preventDefault(); event.stopImmediatePropagation(); } }, true); node.addEventListener("pointercancel", (event) => { scrolling = false; dragging = false; clear(event); }); }
function setRoadmapView(view) { roadmapView = view === "edit" ? "edit" : "overview"; setHidden(elements.roadmapOverview, roadmapView !== "overview"); setHidden(elements.roadmapEdit, roadmapView !== "edit"); elements.roadmapOverviewTab.setAttribute("aria-pressed", String(roadmapView === "overview")); elements.roadmapEditTab.setAttribute("aria-pressed", String(roadmapView === "edit")); elements.roadmapState.textContent = roadmapView === "edit" ? "CycleをActiveにするか、Outcomeカードの移動から順序を変更できます。" : ""; }
function renderRoadmapOutcome(outcome, lane, position, laneLengths) {
  return window.RoadmapCycleUI.createOutcomeCard(outcome, lane, position, laneLengths, {title: roadmapTitle(outcome), actionButton, edit: () => editRoadmapOutcome(outcome.id), move: async (targetLane, targetPosition, trigger) => { const detail = await apiRequest(entityDetailPath("roadmap_outcomes", outcome.id)); await previewMutation({action: "roadmap_move", kind: "roadmap_outcomes", id: detail.id, base_hash: detail.content_hash, lane: targetLane, position: targetPosition}, () => showNotice("Roadmap Outcomeを移動しました。"), trigger); }});
}
function renderRoadmap(snapshot) {
  roadmapSnapshot = snapshot;
  renderRoadmapOverview(snapshot);
  for (const list of [elements.roadmapNowList, elements.roadmapNextList, elements.roadmapLaterList, elements.roadmapPlannedList, elements.roadmapHistoryList]) list.replaceChildren();
  elements.roadmapActiveCycle.replaceChildren(); const entities = Array.isArray(snapshot.entities) ? snapshot.entities : []; const facts = roadmapFacts(snapshot); const byId = new Map(entities.map((entity) => [entity.id, entity]));
  const active = byId.get(facts.active_cycle_id); if (active) { const fm = safeFrontmatter(active); const copy = document.createElement("p"); copy.textContent = roadmapTitle(active) + "（" + (fm.start_date || "?") + "〜" + (fm.end_date || "?") + "）"; elements.roadmapActiveCycle.append(copy); } else { const copy = document.createElement("p"), link = document.createElement("a"); copy.className = "muted"; copy.textContent = "Active Cycleはありません。planned Cycleを作成または選んで開始できます。"; link.href = "#roadmap-planned-heading"; link.textContent = "planned Cycleへ"; elements.roadmapActiveCycle.append(copy, link); }
  if (typeof cycleFocusContext !== "undefined" && cycleFocusContext) cycleFocusContext.renderCycleProjectTimeline(snapshot);
  if (elements.cycleProjectTimelineSection) setHidden(elements.cycleProjectTimelineSection, !active);
  for (const id of Array.isArray(facts.now_outcome_ids) ? facts.now_outcome_ids : []) { const outcome = byId.get(id); if (outcome) elements.roadmapNowList.append(makeEntityRow(outcome, [], ["Now"])); }
  const laneIds = {next: Array.isArray(facts.next_outcome_ids) ? facts.next_outcome_ids : [], later: Array.isArray(facts.later_outcome_ids) ? facts.later_outcome_ids : []}; const laneLengths = {next: laneIds.next.length, later: laneIds.later.length};
  for (const [lane, list] of [["next", elements.roadmapNextList], ["later", elements.roadmapLaterList]]) laneIds[lane].forEach((id, index) => { const outcome = byId.get(id); if (outcome) list.append(renderRoadmapOutcome(outcome, lane, index + 1, laneLengths)); });
  for (const id of Array.isArray(facts.planned_cycle_ids) ? facts.planned_cycle_ids : []) { const cycle = byId.get(id); if (cycle) { const start = actionButton("Activeにする", () => activateCycle(cycle.id), true); if (active) { start.disabled = true; start.dataset.cycleBlocked = "true"; start.setAttribute("title", "先にActive Cycleを終了してください。"); start.setAttribute("aria-label", roadmapTitle(cycle) + "をActiveにする（先にActive Cycleを終了してください）"); } elements.roadmapPlannedList.append(makeEntityRow(cycle, [start], ["planned"])); } }
  for (const cycle of entities.filter((entity) => entity.kind === "cycles" && ["completed", "cancelled"].includes(safeFrontmatter(entity).status))) elements.roadmapHistoryList.append(makeEntityRow(cycle, [], [safeFrontmatter(cycle).status]));
  elements.roadmapState.textContent = roadmapView === "edit" ? "CycleをActiveにするか、Outcomeカードの移動から順序を変更できます。" : "";
  populateRoadmapForms(snapshot, active, byId);
}
function optionEntries(select, entries, selected = "") { if (!select) return; select.replaceChildren(); for (const entry of entries) { const option = document.createElement("option"); option.value = entry.id; option.textContent = roadmapTitle(entry); option.selected = entry.id === selected; select.append(option); } }
function selectedValues(select) { return select ? [...select.options].filter((option) => option.selected).map((option) => option.value) : []; }
function populateRoadmapForms(snapshot, active, entitiesById) {
  const entities = snapshot.entities || []; const activeOutcomes = entities.filter((entity) => entity.kind === "roadmap_outcomes" && safeFrontmatter(entity).status === "active"); const planned = entities.filter((entity) => entity.kind === "cycles" && safeFrontmatter(entity).status === "planned");
  optionEntries(byId("roadmap-outcome-goal"), entities.filter((entity) => entity.kind === "goals" && entity.archived !== true), roadmapOutcomeDetail && safeFrontmatter(roadmapOutcomeDetail).goal_id); optionEntries(byId("cycle-outcome-ids"), activeOutcomes.filter((outcome) => !((roadmapFacts(snapshot).now_outcome_ids || []).includes(outcome.id)))); optionEntries(byId("cycle-close-carryover"), planned);
  const results = byId("cycle-close-results"); if (results) { results.replaceChildren(); const memberIds = active ? (safeFrontmatter(active).outcome_ids || "").replace(/^\[|\]$/g, "").split(",").map((id) => id.trim()).filter(Boolean) : []; for (const id of memberIds) { const field = document.createElement("div"); field.className = "field"; const label = document.createElement("label"); label.htmlFor = "cycle-close-result-" + id; label.textContent = roadmapTitle(entitiesById.get(id)); const select = document.createElement("select"); select.id = label.htmlFor; select.dataset.outcomeId = id; for (const value of ["achieved", "next", "later", "dropped", "carried"]) { const option = document.createElement("option"); option.value = value; option.textContent = value; select.append(option); } field.append(label, select); results.append(field); } }
  const close = byId("cycle-close-disclosure"); if (close) setHidden(close, !active);
}
function roadmapBody() { return byId("roadmap-outcome-body").value; }
async function editRoadmapOutcome(id) { const detail = await apiRequest(entityDetailPath("roadmap_outcomes", id)); roadmapOutcomeDetail = detail; const title = byId("roadmap-outcome-title"); title.value = safeFrontmatter(detail).title || ""; byId("roadmap-outcome-goal").value = safeFrontmatter(detail).goal_id || ""; byId("roadmap-outcome-target-date").value = safeFrontmatter(detail).target_date || ""; byId("roadmap-outcome-body").value = detail.body || ""; setHidden(byId("roadmap-outcome-lane-field"), true); setHidden(byId("cancel-roadmap-outcome-edit"), false); setHidden(byId("archive-roadmap-outcome"), false); byId("roadmap-outcome-form").closest("details").open = true; title.focus(); }
async function activateCycle(id) { const detail = await apiRequest(entityDetailPath("cycles", id)); const operation = {action: "cycle_activate", kind: "cycles", id: detail.id, base_hash: detail.content_hash}; await previewMutation(operation, () => showNotice("Cycleを開始しました。")); }
function roadmapFormHandlers() {
  const outcomeForm = byId("roadmap-outcome-form"); if (outcomeForm) outcomeForm.addEventListener("submit", async (event) => { event.preventDefault(); const title = byId("roadmap-outcome-title").value; const goalId = byId("roadmap-outcome-goal").value; const targetDate = byId("roadmap-outcome-target-date").value; const detail = roadmapOutcomeDetail; const lane = byId("roadmap-outcome-lane").value; const fields = detail ? {title, goal_id: goalId, target_date: targetDate} : {title, goal_id: goalId, target_date: targetDate, roadmap_lane: lane, roadmap_position: String((roadmapFacts(roadmapSnapshot)[lane + "_outcome_ids"] || []).length + 1)}; const operation = detail ? {action: "update", kind: "roadmap_outcomes", id: detail.id, base_hash: detail.content_hash, fields, body: roadmapBody()} : {action: "create", kind: "roadmap_outcomes", fields, body: roadmapBody()}; await previewMutation(operation, () => { roadmapOutcomeDetail = null; outcomeForm.reset(); showNotice("Outcomeを保存しました。"); }); });
  const cycleForm = byId("cycle-form"); if (cycleForm) cycleForm.addEventListener("submit", async (event) => { event.preventDefault(); const operation = {action: "create", kind: "cycles", fields: {title: byId("cycle-title").value, start_date: byId("cycle-start-date").value, end_date: byId("cycle-end-date").value, outcome_ids: "[" + selectedValues(byId("cycle-outcome-ids")).join(", ") + "]"}, body: byId("cycle-body").value}; await previewMutation(operation, () => showNotice("planned Cycleを保存しました。")); });
  const closeForm = byId("cycle-close-form"); if (closeForm) closeForm.addEventListener("submit", async (event) => { event.preventDefault(); const active = roadmapSnapshot && new Map(roadmapSnapshot.entities.map((entity) => [entity.id, entity])).get(roadmapFacts(roadmapSnapshot).active_cycle_id); if (!active) return; const detail = await apiRequest(entityDetailPath("cycles", active.id)); const outcomeResults = Object.fromEntries([...byId("cycle-close-results").querySelectorAll("select")].map((select) => [select.dataset.outcomeId, select.value])); const operation = {action: "cycle_close", kind: "cycles", id: detail.id, base_hash: detail.content_hash, status: byId("cycle-close-status").value, outcome_results: outcomeResults, retrospective: byId("cycle-close-retrospective").value}; if (operation.status === "cancelled") operation.cancellation_reason = byId("cycle-close-reason").value; if (Object.values(outcomeResults).includes("carried")) operation.carryover_cycle_id = byId("cycle-close-carryover").value; await previewMutation(operation, () => showNotice("Cycleを終了しました。")); });
  const cancel = byId("cancel-roadmap-outcome-edit"); if (cancel) cancel.addEventListener("click", () => { roadmapOutcomeDetail = null; byId("roadmap-outcome-form").reset(); setHidden(byId("roadmap-outcome-lane-field"), false); setHidden(cancel, true); setHidden(byId("archive-roadmap-outcome"), true); }); const archive = byId("archive-roadmap-outcome"); if (archive) archive.addEventListener("click", async () => { if (!roadmapOutcomeDetail) return; await previewMutation({action: "archive", kind: "roadmap_outcomes", id: roadmapOutcomeDetail.id, base_hash: roadmapOutcomeDetail.content_hash}, () => showNotice("Outcomeをアーカイブしました。")); });
}
roadmapFormHandlers();
if (elements.roadmapOverviewTab) elements.roadmapOverviewTab.addEventListener("click", () => setRoadmapView("overview"));
if (elements.roadmapEditTab) elements.roadmapEditTab.addEventListener("click", () => setRoadmapView("edit"));
if (elements.roadmapBrowseTab) elements.roadmapBrowseTab.addEventListener("click", () => { if (!roadmapConfirmDiscardDraft()) return; roadmapDetailEditMode = false; roadmapDetailDraft = null; syncRoadmapDetailModeTabs(); if (roadmapSelectedOutcomeId) selectRoadmapOutcome(roadmapSelectedOutcomeId); });
if (elements.roadmapDetailEditTab) elements.roadmapDetailEditTab.addEventListener("click", async () => { roadmapDetailEditMode = true; syncRoadmapDetailModeTabs(); if (roadmapSelectedOutcomeId) await selectRoadmapOutcome(roadmapSelectedOutcomeId); });
if (elements.roadmapDetailClose) elements.roadmapDetailClose.addEventListener("click", () => { if (!closeClarifyProjectDetail()) closeRoadmapDetail(); });
if (elements.roadmapDetailBackdrop) elements.roadmapDetailBackdrop.addEventListener("click", () => { if (!closeClarifyProjectDetail()) closeRoadmapDetail(); });
if (elements.roadmapDetailHandle) {
  const sheetPanel = elements.roadmapOutcomeDetailPanel;
  const resetSheetPointer = (event, keepState = false) => { if (event && elements.roadmapDetailHandle.hasPointerCapture(event.pointerId)) elements.roadmapDetailHandle.releasePointerCapture(event.pointerId); if (!keepState && roadmapSheetStartHeight !== null) { if (roadmapSheetStartHeight > 77) sheetPanel.dataset.expanded = "true"; else sheetPanel.removeAttribute("data-expanded"); } sheetPanel.style.removeProperty("--roadmap-sheet-height"); sheetPanel.classList.remove("is-dragging"); roadmapSheetStartY = null; roadmapSheetStartHeight = null; roadmapSheetPointerId = null; };
  const sheetHeight = () => Math.round((sheetPanel.getBoundingClientRect().height / Math.max(window.innerHeight || 1, 1)) * 100);
  elements.roadmapDetailHandle.addEventListener("pointerdown", (event) => { if (P.beginRoadmapDetailResize(event,elements)) return; event.preventDefault(); roadmapSheetStartY = event.clientY; roadmapSheetStartHeight = Math.min(96, Math.max(58, sheetPanel.dataset.expanded === "true" ? 96 : sheetHeight())); roadmapSheetPointerId = event.pointerId; sheetPanel.classList.add("is-dragging"); elements.roadmapDetailHandle.setPointerCapture(event.pointerId); });
  elements.roadmapDetailHandle.addEventListener("pointermove", (event) => { if (P.moveRoadmapDetailResize(event,elements)) return; if (roadmapSheetStartY === null || event.pointerId !== roadmapSheetPointerId) return; const next = Math.min(96, Math.max(58, roadmapSheetStartHeight - ((event.clientY - roadmapSheetStartY) / Math.max(window.innerHeight || 1, 1)) * 100)); sheetPanel.style.setProperty("--roadmap-sheet-height", next + "dvh"); event.preventDefault(); });
  elements.roadmapDetailHandle.addEventListener("pointerup", (event) => { if (P.endRoadmapDetailResize(event,elements)) return; if (roadmapSheetStartY === null || event.pointerId !== roadmapSheetPointerId) return; const delta = event.clientY - roadmapSheetStartY, startedCollapsed = roadmapSheetStartHeight <= 59; if (startedCollapsed && delta >= 72) { resetSheetPointer(event, true); closeRoadmapDetail(); return; } const expanded = delta < -24 || (!startedCollapsed && delta < 48); resetSheetPointer(event, true); if (expanded) sheetPanel.dataset.expanded = "true"; else sheetPanel.removeAttribute("data-expanded"); });
  elements.roadmapDetailHandle.addEventListener("pointercancel", (event) => { if (!P.endRoadmapDetailResize(event,elements)) resetSheetPointer(event); });
}
if (typeof document.addEventListener === "function") document.addEventListener("keydown", (event) => { if (!elements.roadmapOutcomeDetailPanel || elements.roadmapOutcomeDetailPanel.hidden) return; if (event.key === "Escape") { event.preventDefault(); if (!closeClarifyProjectDetail()) closeRoadmapDetail(); return; } if (event.key !== "Tab") return; const nodes = roadmapFocusables(); if (!nodes.length) return; const first = nodes[0], last = nodes[nodes.length - 1]; if (!nodes.includes(document.activeElement)) { event.preventDefault(); (event.shiftKey ? last : first).focus(); } else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); } });
if (typeof window !== "undefined" && typeof window.addEventListener === "function") { window.addEventListener("beforeunload", (event) => { if (!(roadmapDetailDraft && roadmapDetailDraft.dirty)) return; event.preventDefault(); event.returnValue = ""; }); window.addEventListener("popstate", () => { if (closeClarifyProjectDetail(false)) return; const query = new URL(window.location.href).searchParams, outcomeId = query.get("outcome"), projectId = query.get("project") || "", changingOutcome = outcomeId !== roadmapSelectedOutcomeId; if (changingOutcome) { if (!roadmapConfirmDiscardDraft()) { roadmapRestoreDetailRoute(); return; } roadmapDetailDraft = null; roadmapDetailEditMode = false; } if (outcomeId && roadmapSnapshot) selectRoadmapOutcome(outcomeId, projectId); else if (!outcomeId && !elements.roadmapOutcomeDetailPanel.hidden) closeRoadmapDetail(true, false); }); }
const cycleStartDate = byId("cycle-start-date"); if (cycleStartDate) cycleStartDate.addEventListener("change", () => { if (!cycleStartDate.value) return; const start = new Date(cycleStartDate.value + "T00:00:00Z"); start.setUTCDate(start.getUTCDate() + 41); byId("cycle-end-date").value = start.toISOString().slice(0, 10); });
async function loadRoadmap() {
  const generation = nextGeneration("roadmap"); const resolving = applyOutcomeUnknown || canonicalReloadRequired; if (!resolving) clearMessage(elements.error); elements.roadmapReload.disabled = true;
  try { const resolution = await reconcileUnknownAttempt(); if (!isCurrentGeneration("roadmap", generation)) return; const snapshot = await apiRequest("/api/v1/snapshot"); if (!isCurrentGeneration("roadmap", generation)) return; renderRoadmap(snapshot); const query = new URL(window.location.href).searchParams; if (query.get("outcome")) selectRoadmapOutcome(query.get("outcome"), query.get("project") || ""); renderInboxCount(snapshot); completeSuccessfulReload(resolution); }
  catch (error) { if (isCurrentGeneration("roadmap", generation)) { showReloadFailure(error); elements.roadmapState.textContent = "Roadmapを読み込めませんでした。"; } }
  finally { if (isCurrentGeneration("roadmap", generation)) elements.roadmapReload.disabled = false; }
}
if (elements.roadmapReload) elements.roadmapReload.addEventListener("click", loadRoadmap);
function resetReviewForm(){reviewDetail=null;elements.reviewForm.reset();const date=defaultReviewDate(activeReviewKind);elements.reviewPeriod.value=date;elements.reviewTitle.value=date+(activeReviewKind==="daily"?" Daily Review":" Weekly Review");reviewDraftPeriod=date;elements.reviewBody.value=reviewNotesFromBody(activeReviewKind);elements.reviewBodyLabel.textContent="メモ";configureReviewChecklist();setHidden(elements.reviewChecklist,true);restoreReviewDraft(date);syncReviewGuideMarks();setHidden(elements.reviewCancel,true);setHidden(elements.reviewArchive,true);elements.reviewPreview.textContent="作成";elements.reviewDisclosure.open=false;}
function populateReview(detail){if(detail.archived===true){reviewDetail=null;elements.reviewsState.textContent="このReviewはアーカイブされています: "+detail.id;return;}reviewDetail=detail;reviewDraftPeriod=null;const fm=safeFrontmatter(detail);elements.reviewTitle.value=fm.title||"";elements.reviewPeriod.value=fm.period_start||"";elements.reviewBody.value=detail.body||"";elements.reviewBodyLabel.textContent="Checklist / Notes";setHidden(elements.reviewChecklist,true);setHidden(elements.reviewCancel,false);setHidden(elements.reviewArchive,false);elements.reviewPreview.textContent="保存";elements.reviewDisclosure.open=true;elements.reviewTitle.focus();}
async function editReview(id) {
  if (mutationIsGated()) return; const generation = nextGeneration("reviewEdit");
  try { const detail = await apiRequest(entityDetailPath("reviews", id)); if (!isCurrentGeneration("reviewEdit", generation)) return; if (safeFrontmatter(detail).review_kind !== activeReviewKind) throw new RequestFailure("reconciliation"); populateReview(detail); }
  catch (error) { if (isCurrentGeneration("reviewEdit", generation)) showRequestError(error); }
}
function progressRange() {
  const end = activeReviewKind === "daily" ? tokyoToday() : defaultReviewDate("weekly");
  const date = new Date(end + "T00:00:00Z");
  if (activeReviewKind === "weekly") date.setUTCDate(date.getUTCDate() + 6);
  return {from: activeReviewKind === "daily" ? end : defaultReviewDate("weekly"), to: activeReviewKind === "daily" ? end : date.toISOString().slice(0, 10)};
}
function setProgressOrigins(snapshot) {
  const selected = elements.progressOrigin.value;
  elements.progressOrigin.replaceChildren();
  const empty = document.createElement("option"); empty.value = ""; empty.textContent = "未分類"; elements.progressOrigin.append(empty);
  for (const kind of ["projects", "goals", "areas"]) {
    for (const entity of snapshot.entities.filter((item) => item.kind === kind && item.archived !== true)) {
      const option = document.createElement("option"); option.value = kind.slice(0, -1) + ":" + entity.id;
      option.textContent = ({projects: "Project", goals: "Goal", areas: "Area"}[kind]) + ": " + (safeFrontmatter(entity).title || entity.id); elements.progressOrigin.append(option);
    }
  }
  elements.progressOrigin.value = selected;
}
function resetProgressForm() { progressDetail = null; elements.progressForm.reset(); elements.progressOccurredOn.value = tokyoToday(); elements.pof.open = false; elements.progressPreview.textContent = "記録する"; setHidden(elements.progressCancel, true); }
function renderProgress(items) {
  elements.progressList.replaceChildren();
  for (const item of items) {
    const row = document.createElement("li"); row.className = "entity-row";
    const title = document.createElement("strong"); title.textContent = item.title || "タイトルなし";
    const meta = document.createElement("p"); meta.className = "entity-meta"; meta.textContent = [item.occurred_on, item.origin ? item.origin.kind + ": " + item.origin.title : "未分類"].join(" · ");
    if (activeReviewKind === "daily") row.append(title, meta, actionButton("編集", () => editProgress(item.id)));
    else { const link = document.createElement("a"); link.href = "/reviews/weekly?tab=daily&progress=" + encodeURIComponent(item.id); link.textContent = "Dailyで編集"; row.append(title, meta, link); }
    elements.progressList.append(row);
  }
  elements.progressState.textContent = items.length ? items.length + "件あります。" : "記録はありません。";
}
async function editProgress(id) {
  if (mutationIsGated()) return;
  try {
    const detail = await apiRequest(entityDetailPath("progress", id)); progressDetail = detail;
    const fm = safeFrontmatter(detail); const parsed = ProgressUI.parse(detail.body);
    elements.progressTitle.value = fm.title || ""; elements.progressOccurredOn.value = fm.occurred_on || "";
    const origin = fm.project_id ? ["project", fm.project_id] : (fm.goal_id ? ["goal", fm.goal_id] : (fm.area_id ? ["area", fm.area_id] : ["", ""]));
    ProgressUI.selectOrigin(elements.progressOrigin, ...origin);
    elements.progressBenefit.value = parsed.benefit; elements.progressEvidence.value = parsed.evidence; elements.pof.open = Boolean(elements.progressBenefit.value || elements.progressEvidence.value || origin[0]); elements.progressPreview.textContent = "保存"; setHidden(elements.progressCancel, false); elements.progressTitle.focus();
  } catch (error) { showRequestError(error); }
}
async function loadProgress(snapshot) {
  setProgressOrigins(snapshot); const range = progressRange(); const response = await apiRequest("/api/v1/progress?from=" + range.from + "&to=" + range.to);
  renderProgress(Array.isArray(response.items) ? response.items : []); setHidden(elements.progressForm, activeReviewKind !== "daily");
}
elements.progressForm.addEventListener("submit", async (event) => {
  event.preventDefault(); if (!elements.progressForm.reportValidity() || mutationIsGated()) return;
  const [kind, id] = elements.progressOrigin.value.split(":"); const fields = {title: elements.progressTitle.value, occurred_on: elements.progressOccurredOn.value, ...ProgressUI.originFields(kind, id)};
  const operation = progressDetail ? {action: "update", kind: "progress", id: progressDetail.id, base_hash: progressDetail.content_hash, fields, body: ProgressUI.body(elements.progressBenefit.value, elements.progressEvidence.value)} : {action: "create", kind: "progress", fields, body: ProgressUI.body(elements.progressBenefit.value, elements.progressEvidence.value)};
  await previewMutation(operation, (applied) => { resetProgressForm(); showNotice("Progressを保存しました: " + applied.path); }, elements.progressTitle);
});
elements.progressCancel.addEventListener("click", resetProgressForm);
async function loadProgressReport() {
  try { const report = await apiRequest("/api/v1/reports/progress?month=" + elements.progressReportMonth.value); elements.progressReportMarkdown.value = report.markdown || ""; elements.progressReportState.textContent = (report.count || 0) + "件の実績です。"; }
  catch (error) { showRequestError(error); }
}
elements.progressReportForm.addEventListener("submit", (event) => { event.preventDefault(); void loadProgressReport(); });
elements.progressReportCopy.addEventListener("click", async () => {
  try { if (!navigator.clipboard || !navigator.clipboard.writeText) throw new Error("clipboard unavailable"); await navigator.clipboard.writeText(elements.progressReportMarkdown.value); showNotice("Markdownをコピーしました。"); }
  catch (_error) { elements.progressReportMarkdown.focus(); elements.progressReportMarkdown.select(); showNotice("コピーできないため、選択しました。コピーしてください。"); }
});
function renderReviews(snapshot){elements.reviewsList.replaceChildren();const reviews=snapshot.entities.filter(entity=>entity.kind==="reviews"&&safeFrontmatter(entity).review_kind===activeReviewKind);for(const entity of reviews){const actions=entity.archived===true?[]:[actionButton("編集",()=>editReview(entity.id))];elements.reviewsList.append(makeReviewRow(entity,actions));}elements.reviewsState.textContent=reviews.length?reviews.length+"件あります。":"Reviewはありません。";}
async function loadReviews(){nextGeneration("reviewEdit");const generation=nextGeneration("reviews"),resolving=applyOutcomeUnknown||canonicalReloadRequired;if(!resolving)clearMessage(elements.error);elements.reviewsReload.disabled=true;try{const resolution=await reconcileUnknownAttempt();if(!isCurrentGeneration("reviews",generation))return;const snapshot=await apiRequest("/api/v1/snapshot");if(!isCurrentGeneration("reviews",generation))return;renderReviews(snapshot);renderReviewFacts(snapshot);renderReviewGuide(snapshot);if(globalThis.ProgressUI){await loadProgress(snapshot);const progressId=new URLSearchParams(location.search||"").get("progress");if(activeReviewKind==="daily"&&progressId)await editProgress(progressId);}renderInboxCount(snapshot);completeSuccessfulReload(resolution);}catch(error){if(isCurrentGeneration("reviews",generation)){showReloadFailure(error);elements.reviewsState.textContent="Reviewを読み込めませんでした。";}}finally{if(isCurrentGeneration("reviews",generation))elements.reviewsReload.disabled=false;}}
elements.reviewForm.addEventListener("submit", async (event) => {
  event.preventDefault(); if (!elements.reviewForm.reportValidity()) return; if (!validDate(elements.reviewPeriod.value)) { elements.error.textContent = "対象日はYYYY-MM-DDの実在する日付で入力してください。"; setHidden(elements.error, false); elements.reviewPeriod.focus(); return; }
  const fields = {title: elements.reviewTitle.value, period_start: elements.reviewPeriod.value};
  const operation = reviewDetail ? {action: "update", kind: "reviews", id: reviewDetail.id, base_hash: reviewDetail.content_hash, fields, body: elements.reviewBody.value} : {action: "create", kind: "reviews", review_kind: activeReviewKind, fields, body: reviewBodyFromChecklist()};
  const wasCreate = !reviewDetail; await previewMutation(operation, async (applied) => { if (wasCreate) clearReviewDraft(operation.review_kind, operation.fields.period_start); resetReviewForm(); showNotice("Reviewを保存しました: " + applied.path); }, elements.reviewTitle);
});
elements.reviewArchive.addEventListener("click", async () => { if (!reviewDetail || reviewDetail.archived === true) return; const operation = {action: "archive", kind: "reviews", id: reviewDetail.id, base_hash: reviewDetail.content_hash}; await previewMutation(operation, async (applied) => { resetReviewForm(); showNotice("Reviewをアーカイブしました: " + applied.path); }, elements.reviewTitle); });
elements.reviewCancel.addEventListener("click", resetReviewForm); elements.reviewsReload.addEventListener("click", loadReviews);
elements.reviewPeriod.addEventListener("input", switchReviewDraftPeriod);
for (const control of [elements.reviewTitle, elements.reviewBody, ...elements.reviewStepControls]) if (control) control.addEventListener("input", () => persistReviewDraft());

function loadAllocation() { if (!allocationController) allocationController = window.AllocationUI.createController({byId, apiRequest, previewMutation, setExternalMutationGate, isMutationGated: mutationIsGated, showNotice, showRequestError}); return allocationController.load(); }

async function loadStatus() {
  const generation = nextGeneration("status"); clearMessage(elements.error);
  try { const snapshot = await apiRequest("/api/v1/snapshot"); if (!isCurrentGeneration("status", generation)) return; renderStatusFacts(snapshot); renderInboxCount(snapshot); const health = await apiRequest("/api/v1/health"); if (!isCurrentGeneration("status", generation)) return; elements.serviceHealth.textContent = "Service: " + (typeof health.status === "string" ? health.status : "応答あり"); const calendarStatus = await apiRequest("/api/v1/calendar-sync/status"); if (!isCurrentGeneration("status", generation)) return; renderCalendarSyncStatus(calendarStatus); completeSuccessfulReload(null); }
  catch (error) { if (isCurrentGeneration("status", generation)) { showRequestError(error); elements.serviceHealth.textContent = "Service statusを読み込めませんでした。"; renderCalendarSyncStatus({state: "stale", reason_code: "status_unavailable", checked_at: ""}); } }
}

const CALENDAR_SYNC_PRESENTATION = Object.freeze({
  normal: Object.freeze({label: "正常", message: "自動同期は正常に動作しています。"}),
  waiting: Object.freeze({label: "待機中", message: "設定または次の同期処理を待っています。"}),
  stopped: Object.freeze({label: "停止中", message: "自動同期タイマーは停止しています。"}),
  reauth: Object.freeze({label: "再認証が必要", message: "Google Calendar の再認証が必要です。"}),
  stale: Object.freeze({label: "確認が必要", message: "安全な最新状態を確認できません。運用手順を確認してください。"}),
});
const CALENDAR_SYNC_REASONS = Object.freeze({
  recent_success: "直近の同期に成功", setup_required: "初期設定待ち", recovery_pending: "復旧確認待ち",
  first_run_pending: "初回実行待ち", timer_stopped: "タイマー停止", reauth_required: "再認証が必要",
  run_in_progress: "同期を実行中", run_stale: "実行結果が古いか失敗", unsafe_evidence: "状態証拠を安全に読めない", status_unavailable: "状態を取得できない",
});
function renderCalendarSyncStatus(value) {
  const state = value && typeof value.state === "string" && CALENDAR_SYNC_PRESENTATION[value.state] ? value.state : "stale";
  const presentation = CALENDAR_SYNC_PRESENTATION[state];
  const reasonCode = value && typeof value.reason_code === "string" ? value.reason_code : "status_unavailable";
  elements.calendarSyncState.dataset.state = state; elements.calendarSyncState.textContent = presentation.label;
  elements.calendarSyncMessage.textContent = presentation.message;
  elements.calendarSyncReason.textContent = CALENDAR_SYNC_REASONS[reasonCode] || CALENDAR_SYNC_REASONS.status_unavailable;
  const checked = value && typeof value.checked_at === "string" ? Date.parse(value.checked_at) : NaN;
  elements.calendarSyncCheckedAt.textContent = Number.isNaN(checked) ? "確認できません" : new Date(checked).toLocaleString("ja-JP", {timeZone: "Asia/Tokyo"});
}

function showRoute(workflow,loader,reloader){setHidden(workflow,false);reloadCurrentRoute=loader;reloadFocus=elements.retryCurrent;loader();}
const REVIEW_HELP=window.ReviewHelp||{};
function showReviewHelp(){const t=new URLSearchParams(location.search).get("tab"),k=t==="progress"?t:reviewHelpKindFromQuery(),g=REVIEW_HELP[k],s=g.steps||REVIEW_STEPS[k],e=elements;e.rht.textContent=g.title;e.rhp.textContent=g.purpose||reviewGuidePurpose(k);for(const x of document.querySelectorAll?.("[data-review-help-tab]")||[])x.setAttribute("aria-current",x.dataset.reviewHelpTab===k?"page":"false");e.rhf.replaceChildren();for(const x of g.flow)appendListText(e.rhf,x);e.rhs.replaceChildren();for(const x of s){const i=document.createElement("li");i.textContent=x.label;appendReviewStepExplanation(i,x);e.rhs.append(i);}for(let i=0;i<3;i++)e.rhe[i].textContent=g.example[i];e.rhk.textContent=g.stuck;e.rhd.textContent=g.done||reviewDoneCondition(k);e.rhb.href=g.back;setHidden(e.reviewHelpWorkflow,false);}
function initializeRoute() {
  const path = location.pathname; const primaryPath = path === "/" ? "/tasks" : path;
  if (typeof document.querySelectorAll === "function") {
    for (const link of document.querySelectorAll('[data-primary-nav][href="' + primaryPath + '"]')) link.setAttribute("aria-current", "page");
  } else {
    const currentLink = document.querySelector('nav a[href="' + primaryPath + '"]'); if (currentLink) currentLink.setAttribute("aria-current", "page");
  }
  if (path === "/" || path === "/tasks") {
    elements.focusDate.textContent = new Date().toLocaleDateString("ja-JP", {timeZone: "Asia/Tokyo", month: "long", day: "numeric", weekday: "long"});
    showRoute(elements.tasksWorkflow, loadFocus, elements.tasksReload); const canRefreshFocus = () => elements.clarifyWorkflow.hidden && !(document.querySelectorAll ? document.querySelectorAll("dialog[open]").length : elements.dialog.open) && !taskReloadInFlight && !mutationIsGated(); setInterval(()=>canRefreshFocus()&&loadFocus(),6e4); if (document.addEventListener) document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && canRefreshFocus()) void loadFocus(); }); return;
  }
  if (path === "/inbox") { showRoute(elements.inboxWorkflow, loadInbox, elements.inboxReload); return; }
  if (path === "/clarify") { showRoute(elements.clarifyWorkflow, loadClarify, elements.clarifyReload); return; }
  if (path === "/projects") {
    const query = new URLSearchParams(location.search || ""); const requested = query.get("level");
    activeDirectionLevel = query.get("mode") === "review" ? "review" : (requested === "purpose" || requested === "visions" || requested === "goals" || requested === "areas" || requested === "projects" ? requested : (query.get("tab") === "goals" ? "goals" : "overview"));
    activeOutcomeTab = activeDirectionLevel === "goals" ? "goals" : "projects";
    applyOutcomeView(); showRoute(elements.projectsWorkflow, loadProjectKanban, elements.projectsReload); return;
  }
  if (path === "/roadmap") { showRoute(elements.roadmapWorkflow, loadRoadmap, elements.roadmapReload); return; }
  if (path === "/reviews/weekly") {
    const reviewTab = reviewTabFromQuery(); persistReviewTab(reviewTab);
    if (reviewTab === "allocation") {
      elements.reviewHeading.textContent = "Weekly Review";
      elements.weeklyReviewTab.setAttribute("aria-current", "false"); elements.dailyReviewTab.setAttribute("aria-current", "false");
      byId("allocation-review-tab").setAttribute("aria-current", "page"); setHidden(elements.reviewWorkflow, false); setHidden(byId("review-guide"), true); setHidden(byId("review-form-disclosure"), true); setHidden(byId("allocation-workflow"), false);
      reloadCurrentRoute = loadAllocation; reloadFocus = elements.retryCurrent; loadAllocation(); return;
    }
    activeReviewKind = reviewTab === "weekly" ? "weekly" : "daily";
    elements.reviewHeading.textContent = activeReviewKind === "daily" ? "Daily Review" : "Weekly Review";
    const guideHeading = byId("review-guide-heading"); if (guideHeading) guideHeading.textContent = activeReviewKind === "daily" ? "Daily Reviewの進め方" : "Weekly Reviewの進め方";
    elements.weeklyReviewTab.setAttribute("aria-current", activeReviewKind === "weekly" ? "page" : "false");
    elements.dailyReviewTab.setAttribute("aria-current", activeReviewKind === "daily" ? "page" : "false");
    byId("allocation-review-tab").setAttribute("aria-current", "false");
    resetReviewForm(); renderReviewGuide({}); showRoute(elements.reviewWorkflow, loadReviews, elements.reviewsReload); return;
  }
  if (path === "/reports/progress") { elements.progressReportMonth.value = tokyoToday().slice(0, 7); showRoute(elements.progressReportWorkflow, loadProgressReport, null); return; }
  if (path === "/calendar") { return; }
  if (path === "/help/reviews") { showReviewHelp(); return; }
  if (path === "/status") { setHidden(elements.statusWorkflow, false); reloadCurrentRoute = loadStatus; reloadFocus = elements.retryCurrent; loadStatus(); return; }
  setHidden(elements.unavailable, false);
}
elements.retryCurrent.addEventListener("click", async () => { if (reloadCurrentRoute) await reloadCurrentRoute(); });
initializeRoute();
