const state = {
  positions: [],
  tasks: [],
  health: null,
  recordsByToken: new Map(),
  selectedTokenId: null,
  positionsUnavailable: null,
  detailTasks: [],
  detailRecords: [],
  editingTaskId: null,
  focusedTimelineTaskId: null,
  filteredTimelineTaskId: null,
  historyFilterQuery: "",
  timelineSortMode: "time",
  timelinePageSize: 20,
  timelineVisibleCount: 20,
};

const TIMELINE_PAGE_SIZE_OPTIONS = [10, 20, 50, 100];

let timelineAutoLoadObserver = null;

const elements = {
  initialLoadingDialog: document.querySelector("#initialLoadingDialog"),
  banner: document.querySelector("#banner"),
  healthPanel: document.querySelector("#healthPanel"),
  lastSyncText: document.querySelector("#lastSyncText"),
  positionCount: document.querySelector("#positionCount"),
  positionList: document.querySelector("#positionList"),
  detailEmpty: document.querySelector("#detailEmpty"),
  detailView: document.querySelector("#detailView"),
  detailTitle: document.querySelector("#detailTitle"),
  detailMeta: document.querySelector("#detailMeta"),
  detailSize: document.querySelector("#detailSize"),
  detailAverageCost: document.querySelector("#detailAverageCost"),
  detailCurrentPrice: document.querySelector("#detailCurrentPrice"),
  detailPnL: document.querySelector("#detailPnL"),
  activeTaskContent: document.querySelector("#activeTaskContent"),
  taskActions: document.querySelector("#taskActions"),
  taskHistory: document.querySelector("#taskHistory"),
  historyFilterInput: document.querySelector("#historyFilterInput"),
  historyFilterResetButton: document.querySelector("#historyFilterResetButton"),
  timelineToolbar: document.querySelector("#timelineToolbar"),
  recordTimeline: document.querySelector("#recordTimeline"),
  formHint: document.querySelector("#formHint"),
  createTaskForm: document.querySelector("#createTaskForm"),
  taskFormKicker: document.querySelector("#taskFormKicker"),
  taskFormTitle: document.querySelector("#taskFormTitle"),
  taskFormSubmitButton: document.querySelector("#taskFormSubmitButton"),
  cancelEditButton: document.querySelector("#cancelEditButton"),
  taskFormStatePanel: document.querySelector("#taskFormStatePanel"),
  refreshButton: document.querySelector("#refreshButton"),
  positionItemTemplate: document.querySelector("#positionItemTemplate"),
};

bootstrap();

elements.refreshButton.addEventListener("click", () =>
  refreshDashboard({ keepSelection: true }),
);
elements.createTaskForm.addEventListener("submit", handleTaskFormSubmit);
elements.cancelEditButton.addEventListener("click", cancelEditMode);
elements.historyFilterInput.addEventListener("input", (event) => {
  state.historyFilterQuery = event.currentTarget.value;
  syncUrlState();
  renderTaskHistory(state.detailTasks, state.detailRecords);
});
elements.historyFilterResetButton.addEventListener("click", () => {
  state.historyFilterQuery = "";
  elements.historyFilterInput.value = "";
  syncUrlState();
  renderTaskHistory(state.detailTasks, state.detailRecords);
});

async function bootstrap() {
  hydrateStateFromUrl();
  setInitialLoading(true);
  try {
    await refreshDashboard({ keepSelection: false });
    window.setInterval(() => refreshHealth().catch(() => undefined), 5000);
  } catch (error) {
    showBanner(error.message || String(error), "error");
  } finally {
    setInitialLoading(false);
  }
}

function setInitialLoading(visible) {
  document.body.classList.toggle("app-booting", visible);
  const dialog = elements.initialLoadingDialog;
  if (!dialog) {
    return;
  }
  if (visible) {
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) {
        dialog.showModal();
      }
      return;
    }
    dialog.setAttribute("open", "open");
    return;
  }
  if (typeof dialog.close === "function") {
    if (dialog.open) {
      dialog.close();
    }
    return;
  }
  dialog.removeAttribute("open");
}

function hydrateStateFromUrl() {
  const params = new URLSearchParams(window.location.search);
  state.selectedTokenId = normalizeUrlParam(params.get("token"));
  state.historyFilterQuery = normalizeUrlParam(params.get("history")) || "";
  state.timelineSortMode =
    params.get("timelineSort") === "risk" ? "risk" : "time";
  state.focusedTimelineTaskId = normalizeUrlParam(params.get("focusTask"));
  state.filteredTimelineTaskId = normalizeUrlParam(params.get("filterTask"));
  if (state.filteredTimelineTaskId) {
    state.focusedTimelineTaskId = state.filteredTimelineTaskId;
  }
  elements.historyFilterInput.value = state.historyFilterQuery;
}

function syncUrlState() {
  const params = new URLSearchParams();
  if (state.selectedTokenId) {
    params.set("token", state.selectedTokenId);
  }
  const historyQuery = state.historyFilterQuery.trim();
  if (historyQuery) {
    params.set("history", historyQuery);
  }
  if (state.timelineSortMode === "risk") {
    params.set("timelineSort", state.timelineSortMode);
  }
  if (state.focusedTimelineTaskId) {
    params.set("focusTask", state.focusedTimelineTaskId);
  }
  if (state.filteredTimelineTaskId) {
    params.set("filterTask", state.filteredTimelineTaskId);
  }
  const nextSearch = params.toString();
  const nextUrl = `${window.location.pathname}${nextSearch ? `?${nextSearch}` : ""}${window.location.hash}`;
  const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  if (nextUrl !== currentUrl) {
    window.history.replaceState(null, "", nextUrl);
  }
}

function normalizeUrlParam(value) {
  return value == null || value === "" ? null : value;
}

async function refreshDashboard({ keepSelection }) {
  const previousTokenId = state.selectedTokenId;
  const previousEditingTaskId = keepSelection ? state.editingTaskId : null;
  const previousFocusedTimelineTaskId = keepSelection
    ? state.focusedTimelineTaskId
    : null;
  const previousFilteredTimelineTaskId = keepSelection
    ? state.filteredTimelineTaskId
    : null;
  showBanner("", "info", true);
  const [healthResult, positionsResult, tasksResult] = await Promise.allSettled(
    [
      fetchJson("/health"),
      fetchJson("/positions?size_threshold=0"),
      fetchJson("/tasks?include_deleted=true"),
    ],
  );

  if (healthResult.status === "fulfilled") {
    state.health = healthResult.value;
  }

  state.positionsUnavailable = null;
  if (positionsResult.status === "fulfilled") {
    state.positions = positionsResult.value;
  } else {
    state.positions = [];
    state.positionsUnavailable =
      positionsResult.reason?.message || "持仓接口当前不可用";
  }

  if (tasksResult.status === "fulfilled") {
    state.tasks = tasksResult.value;
  } else {
    throw new Error(tasksResult.reason?.message || "任务接口当前不可用");
  }

  mergeFallbackPositionCards();
  renderHealth();
  renderPositionList();
  state.editingTaskId = previousEditingTaskId;
  state.focusedTimelineTaskId = previousFocusedTimelineTaskId;
  state.filteredTimelineTaskId = previousFilteredTimelineTaskId;

  const nextTokenId =
    previousTokenId &&
    state.positions.some((position) => position.token_id === previousTokenId)
      ? previousTokenId
      : (state.positions[0]?.token_id ?? null);
  state.selectedTokenId = nextTokenId;
  if (state.selectedTokenId) {
    await loadDetail(state.selectedTokenId);
  } else {
    state.editingTaskId = null;
    syncUrlState();
    renderEmptyDetail();
  }
  elements.lastSyncText.textContent = `最近同步 ${new Date().toLocaleTimeString("zh-CN")}`;
}

async function refreshHealth() {
  state.health = await fetchJson("/health");
  renderHealth();
}

async function loadDetail(tokenId) {
  const tokenChanged = state.selectedTokenId !== tokenId;
  if (tokenChanged) {
    state.historyFilterQuery = "";
    state.timelineVisibleCount = state.timelinePageSize;
    elements.recordTimeline.scrollTop = 0;
    elements.historyFilterInput.value = "";
  }
  state.selectedTokenId = tokenId;
  const [tokenTasks, tokenRecords] = await Promise.all([
    fetchJson(
      `/tasks?include_deleted=true&token_id=${encodeURIComponent(tokenId)}`,
    ),
    fetchJson(`/records?token_id=${encodeURIComponent(tokenId)}&limit=200`),
  ]);
  const visibleRecords = filterTimelineRecords(tokenRecords);
  state.detailTasks = tokenTasks;
  state.detailRecords = visibleRecords;
  state.recordsByToken.set(tokenId, visibleRecords);
  state.tasks = mergeTasks(state.tasks, tokenTasks);
  reconcileTimelineState(visibleRecords);
  state.timelineVisibleCount = resolveTimelineVisibleCountForCurrentContext(
    visibleRecords,
  );
  elements.historyFilterInput.value = state.historyFilterQuery;
  syncUrlState();
  renderPositionList();
  renderDetail(tokenId, tokenTasks, visibleRecords);
}

function filterTimelineRecords(records) {
  const seenDryRunSignatures = new Set();
  return records.filter((record) => {
    if (record.status === "waiting") {
      return false;
    }
    if (record.status !== "dry-run") {
      return true;
    }
    const signature = [
      record.task_id,
      record.rule_name,
      record.event_type,
      record.status,
      record.message,
      record.requested_size,
      record.filled_size,
      record.trigger_price,
    ].join("|");
    if (seenDryRunSignatures.has(signature)) {
      return false;
    }
    seenDryRunSignatures.add(signature);
    return true;
  });
}

function mergeTasks(existingTasks, incomingTasks) {
  const taskMap = new Map(existingTasks.map((task) => [task.task_id, task]));
  for (const task of incomingTasks) {
    taskMap.set(task.task_id, task);
  }
  return Array.from(taskMap.values()).sort((left, right) =>
    String(right.updated_at).localeCompare(String(left.updated_at)),
  );
}

function mergeFallbackPositionCards() {
  const knownTokens = new Set(
    state.positions.map((position) => position.token_id),
  );
  for (const task of state.tasks) {
    if (knownTokens.has(task.token_id)) {
      continue;
    }
    knownTokens.add(task.token_id);
    state.positions.push({
      token_id: task.token_id,
      size: task.position_size ?? "-",
      average_cost: task.average_cost ?? "-",
      current_price: "-",
      current_value: "-",
      cash_pnl: "-",
      percent_pnl: "-",
      outcome: null,
      market: null,
      title: "未拿到真实持仓，显示任务兜底卡片",
      slug: null,
      proxy_wallet: null,
      __fallback: true,
    });
  }
  state.positions.sort((left, right) =>
    String(left.title || left.token_id).localeCompare(
      String(right.title || right.token_id),
    ),
  );
}

function renderHealth() {
  const runtime = state.health?.runtime;
  if (!runtime) {
    elements.healthPanel.innerHTML = `<div class="health-card"><span>状态</span><strong>后端未返回 runtime 快照</strong></div>`;
    return;
  }
  const cards = [
    renderHealthCard(
      "运行时",
      runtime.running ? "运行中" : "已停止",
      runtime.running ? "ok" : "warn",
    ),
    renderHealthCard("活跃任务", String(runtime.runner_count), "neutral"),
    renderHealthCard(
      "订单跟踪",
      String(runtime.tracked_order_count),
      "neutral",
    ),
    renderHealthCard(
      "Market Stale",
      formatMaybeSeconds(runtime.stale_seconds?.market),
      staleTone(runtime.stale_seconds?.market, 15),
    ),
    renderHealthCard(
      "User Stale",
      formatMaybeSeconds(runtime.stale_seconds?.user),
      staleTone(runtime.stale_seconds?.user, 30),
    ),
  ];
  elements.healthPanel.innerHTML = cards.join("");
}

function renderHealthCard(label, value, tone) {
  return `<div class="health-card health-${tone}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`;
}

function renderPositionList() {
  elements.positionCount.textContent = String(state.positions.length);
  elements.positionList.innerHTML = "";
  if (!state.positions.length) {
    elements.positionList.innerHTML = `<div class="empty-note">还没有可展示的仓位或任务。</div>`;
    return;
  }
  for (const position of state.positions) {
    const taskSummary = summarizeTasks(position.token_id);
    const node =
      elements.positionItemTemplate.content.firstElementChild.cloneNode(true);
    node.dataset.tokenId = position.token_id;
    node.classList.toggle(
      "selected",
      position.token_id === state.selectedTokenId,
    );
    node.querySelector(".position-title").textContent =
      position.title || position.market || shortToken(position.token_id);
    node.querySelector(".position-subtitle").textContent = [
      position.outcome,
      shortToken(position.token_id),
    ]
      .filter(Boolean)
      .join(" · ");
    node.querySelector(".position-size").textContent = `仓位 ${position.size}`;
    node.querySelector(".position-price").textContent =
      `现价 ${formatPriceValue(position.current_price)}`;
    node.querySelector(".status-pill").textContent = taskSummary.label;
    node.querySelector(".status-pill").className =
      `status-pill ${taskSummary.className}`;
    node.addEventListener("click", () => {
      state.focusedTimelineTaskId = null;
      return loadDetail(position.token_id).catch((error) =>
        showBanner(error.message || String(error), "error"),
      );
    });
    elements.positionList.appendChild(node);
  }
  if (state.positionsUnavailable) {
    showBanner(
      `持仓接口暂不可用，当前使用任务兜底卡片。${state.positionsUnavailable}`,
      "warn",
    );
  }
}

function summarizeTasks(tokenId) {
  const tasks = getSortedTasks(
    state.tasks.filter(
      (task) => task.token_id === tokenId && task.status !== "deleted",
    ),
  );
  const activeTask = tasks.find((task) => task.status === "active");
  if (activeTask) {
    return { label: "ACTIVE", className: "tone-ok" };
  }
  const pausedTask = tasks.find((task) => task.status === "paused");
  if (pausedTask) {
    return { label: "PAUSED", className: "tone-warn" };
  }
  if (tasks.length) {
    return { label: tasks[0].status.toUpperCase(), className: "tone-neutral" };
  }
  return { label: "NO TASK", className: "tone-muted" };
}

function renderDetail(tokenId, tokenTasks, tokenRecords) {
  const position = state.positions.find((item) => item.token_id === tokenId);
  if (!position) {
    renderEmptyDetail();
    return;
  }
  const currentTask = pickCurrentTask(tokenTasks);
  const editingTask = resolveEditingTask(tokenTasks);
  if (state.editingTaskId && editingTask == null) {
    state.editingTaskId = null;
  }

  elements.detailEmpty.classList.add("hidden");
  elements.detailView.classList.remove("hidden");
  renderDetailTitle(position, tokenId);
  elements.detailMeta.textContent = [
    position.outcome,
    position.market,
    shortToken(tokenId),
  ]
    .filter(Boolean)
    .join(" · ");
  elements.detailSize.textContent = position.size;
  elements.detailAverageCost.textContent = formatPriceValue(position.average_cost);
  elements.detailCurrentPrice.textContent = formatPriceValue(position.current_price);
  elements.detailPnL.textContent = `${position.cash_pnl} / ${formatPercentMetricValue(position.percent_pnl)}`;

  renderActiveTask(currentTask);
  renderTaskHistory(tokenTasks, tokenRecords);
  renderTimelineToolbar(tokenRecords);
  renderTimeline(tokenRecords);
  renderTaskForm(position, currentTask, editingTask);
}

function renderDetailTitle(position, tokenId) {
  const title = position.title || position.market || shortToken(tokenId);
  const eventUrl = getPolymarketEventUrl(position);
  elements.detailTitle.replaceChildren();
  if (!eventUrl) {
    elements.detailTitle.textContent = title;
    return;
  }
  const link = document.createElement("a");
  link.href = eventUrl;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.className = "detail-title-link";
  link.textContent = title;
  elements.detailTitle.appendChild(link);
}

function getPolymarketEventUrl(position) {
  const marketSlug = normalizeSlugSegment(position?.slug);
  if (!marketSlug) {
    return null;
  }
  const eventSlug =
    normalizeSlugSegment(position?.event_slug) || inferEventSlug(marketSlug);
  if (!eventSlug) {
    return `https://polymarket.com/event/${encodeURIComponent(marketSlug)}`;
  }
  return `https://polymarket.com/event/${encodeURIComponent(eventSlug)}/${encodeURIComponent(marketSlug)}`;
}

function normalizeSlugSegment(value) {
  if (value == null || value === "") {
    return null;
  }
  return String(value).trim() || null;
}

function inferEventSlug(marketSlug) {
  if (!marketSlug) {
    return null;
  }
  const binarySlug = marketSlug.match(/^(.*)-(yes|no)$/i);
  if (binarySlug) {
    return binarySlug[1];
  }
  const rangeSlug = marketSlug.match(/^(.*)-\d+-\d+$/);
  if (rangeSlug) {
    return rangeSlug[1];
  }
  return null;
}

function renderActiveTask(task) {
  elements.taskActions.innerHTML = "";
  if (!task) {
    elements.activeTaskContent.innerHTML = `<div class="empty-note">当前没有任务，你可以直接在右侧创建一套规则。</div>`;
    return;
  }
  const editableInForm = canEditTaskInForm(task);
  const rules = task.rules
    .map(
      (rule) =>
        `<li><strong>${escapeHtml(rule.name)}</strong><span>${escapeHtml(rule.kind)}</span><em>${formatRule(rule)}</em>${renderRuntimeStateInline(rule.runtime_state)}</li>`,
    )
    .join("");
  elements.activeTaskContent.innerHTML = `
    <div class="task-headline">
      <span class="status-pill tone-${task.status === "active" ? "ok" : task.status === "paused" ? "warn" : "neutral"}">${escapeHtml(task.status.toUpperCase())}</span>
      <span class="muted">${escapeHtml(task.task_id)}</span>
    </div>
    <dl class="task-meta-grid">
      <div><dt>dry-run</dt><dd>${task.dry_run ? "是" : "否"}</dd></div>
      <div><dt>slippage</dt><dd>${escapeHtml(task.slippage_bps)} bps</dd></div>
      <div><dt>仓位覆盖</dt><dd>${escapeHtml(task.position_size ?? "-")}</dd></div>
      <div><dt>均价覆盖</dt><dd>${escapeHtml(formatPriceValue(task.average_cost))}</dd></div>
    </dl>
    ${editableInForm ? "" : `<p class="muted">当前任务包含页面表单暂不支持的规则组合，无法直接原地编辑。</p>`}
    <ul class="rule-list">${rules}</ul>
  `;

  if (task.status === "active") {
    elements.taskActions.appendChild(
      actionButton(
        "暂停",
        async () => mutateTask(task.task_id, "pause"),
        "button-secondary",
      ),
    );
    elements.taskActions.appendChild(
      actionButton(
        "暂停后编辑",
        async () => startEditTask(task),
        "button-primary",
      ),
    );
  }
  if (task.status === "paused") {
    elements.taskActions.appendChild(
      actionButton(
        "恢复",
        async () => mutateTask(task.task_id, "resume"),
        "button-primary",
      ),
    );
    elements.taskActions.appendChild(
      actionButton("编辑", async () => startEditTask(task), "button-secondary"),
    );
  }
  elements.taskActions.appendChild(
    actionButton(
      "删除",
      async () => mutateTask(task.task_id, "delete"),
      "button-danger",
    ),
  );
}

function renderTaskHistory(tasks, records) {
  if (!tasks.length) {
    elements.taskHistory.innerHTML = `<div class="empty-note">没有历史任务。</div>`;
    return;
  }
  const sortedTasks = getSortedTasks(tasks);
  const filteredTasks = filterHistoryTasks(
    sortedTasks,
    state.historyFilterQuery,
  );
  if (!filteredTasks.length) {
    elements.taskHistory.innerHTML = `<div class="empty-note">没有匹配当前筛选条件的任务。</div>`;
    return;
  }
  const latestRecordByTaskId = buildLatestRecordByTaskId(records);
  const groupedTasks = groupHistoryTasks(filteredTasks, latestRecordByTaskId);
  elements.taskHistory.innerHTML = groupedTasks
    .map((group) => {
      const groupSummary = summarizeHistoryGroup(
        group.tasks,
        latestRecordByTaskId,
      );
      return `
      <details class="history-group"${group.defaultOpen ? " open" : ""}>
        <summary class="history-group-summary">
          <div class="history-group-header">
            <div class="history-group-header-meta">
              <p class="panel-kicker">${escapeHtml(group.label)}</p>
              ${renderHistoryGroupBadges(groupSummary)}
            </div>
            <span class="count-chip">${group.tasks.length}</span>
          </div>
        </summary>
        <div class="history-group-list">
          ${group.tasks.map((task) => renderHistoryTaskCard(task, latestRecordByTaskId.get(task.task_id) || null)).join("")}
        </div>
      </details>
    `;
    })
    .join("");
  for (const button of elements.taskHistory.querySelectorAll(
    ".history-action-button",
  )) {
    button.addEventListener("click", () => {
      const task = sortedTasks.find(
        (item) => item.task_id === button.dataset.taskId,
      );
      if (!task) {
        return;
      }
      startEditTask(task).catch((error) =>
        showBanner(error.message || String(error), "error"),
      );
    });
  }
  for (const button of elements.taskHistory.querySelectorAll(
    ".history-summary-jump-button",
  )) {
    button.addEventListener("click", () => {
      const taskId = button.dataset.taskId;
      if (!taskId) {
        return;
      }
      focusTimelineTask(taskId);
    });
  }
}

function renderHistoryTaskCard(task, latestRecord) {
  return `
    <article class="history-item">
      <div class="history-main">
        <div>
          <strong>${escapeHtml(task.task_id)}</strong>
          <p class="history-rules">任务规则：${task.rules.map((rule) => escapeHtml(formatRuleSummary(rule))).join(" / ")}</p>
        </div>
        <div class="history-actions">
          <span class="status-pill tone-${task.status === "active" ? "ok" : task.status === "paused" ? "warn" : task.status === "failed" ? "danger" : "neutral"}">${escapeHtml(task.status.toUpperCase())}</span>
          ${task.status === "active" || task.status === "paused" ? `<button type="button" class="button button-secondary history-action-button" data-task-id="${escapeHtml(task.task_id)}">${task.status === "active" ? "暂停后编辑" : "编辑"}</button>` : ""}
        </div>
      </div>
      <p class="muted">${new Date(task.updated_at).toLocaleString("zh-CN")}</p>
      ${renderLatestRecordSummary(latestRecord)}
      ${renderHistoryProgressDisclosure(task)}
    </article>
  `;
}

function renderLatestRecordSummary(record) {
  if (!record) {
    return `<div class="history-summary history-summary-empty">最近一条执行记录：暂无记录</div>`;
  }
  const statusMeta = getTimelineStatusMeta(record);
  return `
    <div class="history-summary history-summary-${statusMeta.tone}">
      <div class="history-summary-head">
        <strong>最近一条执行记录</strong>
        <span class="status-pill tone-${statusMeta.tone}">${escapeHtml(statusMeta.label)}</span>
      </div>
      <p>${escapeHtml(record.rule_name)} · ${escapeHtml(formatRecordMessage(record.message || "-"))}</p>
      <div class="timeline-meta">
        <span>${new Date(record.created_at).toLocaleString("zh-CN")}</span>
        <span>${escapeHtml(formatRecordEventLabel(record.event_type))}</span>
        <span>${escapeHtml(formatRecordSizeSummary(record))}</span>
        <span>事件价 ${escapeHtml(formatPriceValue(record.event_price))}</span>
      </div>
      <div class="history-summary-actions">
        <button type="button" class="button button-secondary history-summary-jump-button" data-task-id="${escapeHtml(record.task_id)}">定位时间线</button>
      </div>
    </div>
  `;
}

function groupHistoryTasks(tasks, latestRecordByTaskId) {
  const groups = [
    {
      label: "进行中",
      defaultOpen: true,
      tasks: tasks.filter(
        (task) => task.status === "active" || task.status === "paused",
      ),
    },
    {
      label: "已结束",
      defaultOpen: false,
      tasks: tasks.filter((task) =>
        ["completed", "failed", "cancelled", "deleted"].includes(task.status),
      ),
    },
  ];
  const remainingTasks = tasks.filter(
    (task) =>
      !groups.some((group) =>
        group.tasks.some((candidate) => candidate.task_id === task.task_id),
      ),
  );
  if (remainingTasks.length) {
    groups.push({ label: "其他", defaultOpen: false, tasks: remainingTasks });
  }
  return groups
    .filter((group) => group.tasks.length > 0)
    .map((group) => ({
      ...group,
      tasks: sortHistoryTasksByRisk(group.tasks, latestRecordByTaskId),
    }));
}

function summarizeHistoryGroup(tasks, latestRecordByTaskId) {
  const latestRecords = tasks
    .map((task) => latestRecordByTaskId.get(task.task_id) || null)
    .filter(Boolean);
  return {
    failedCount: latestRecords.filter((record) => record.status === "failed")
      .length,
    reviewCount: latestRecords.filter(
      (record) => record.status === "needs-review",
    ).length,
  };
}

function renderHistoryGroupBadges(summary) {
  const badges = [];
  if (summary.failedCount > 0) {
    badges.push(
      `<span class="status-pill tone-danger">failed ${summary.failedCount}</span>`,
    );
  }
  if (summary.reviewCount > 0) {
    badges.push(
      `<span class="status-pill tone-warn">review ${summary.reviewCount}</span>`,
    );
  }
  return badges.length
    ? `<div class="history-group-badges">${badges.join("")}</div>`
    : "";
}

function recordStatusTone(status) {
  if (status === "failed" || status === "needs-review") {
    return "danger";
  }
  if (status === "paused" || status === "waiting") {
    return "warn";
  }
  if (status === "completed" || status === "matched" || status === "dry-run") {
    return "ok";
  }
  return "neutral";
}

function getTimelineStatusMeta(record) {
  if (record.status === "failed" || record.status === "needs-review") {
    return { label: "失败/人工复核", tone: "danger", priority: 4 };
  }
  if (record.event_type === "system" || record.status === "paused") {
    return { label: "失败/人工复核", tone: "warn", priority: 3 };
  }
  if (record.event_type === "trade" && record.status === "confirmed") {
    return { label: "成交确认", tone: "ok", priority: 2 };
  }
  if (record.event_type === "order" || record.event_type === "attempt") {
    return { label: "下单提交", tone: "neutral", priority: 1 };
  }
  return { label: "规则触发", tone: "ok", priority: 0 };
}

function filterHistoryTasks(tasks, query) {
  const normalizedQuery = query.trim().toLowerCase();
  if (!normalizedQuery) {
    return tasks;
  }
  return tasks.filter((task) => {
    const searchable = [
      task.task_id,
      task.status,
      ...task.rules.map((rule) => rule.name),
      ...task.rules.map((rule) => rule.kind),
    ]
      .join(" ")
      .toLowerCase();
    return searchable.includes(normalizedQuery);
  });
}

function sortHistoryTasksByRisk(tasks, latestRecordByTaskId) {
  return [...tasks].sort((left, right) => {
    const leftRecord = latestRecordByTaskId.get(left.task_id);
    const rightRecord = latestRecordByTaskId.get(right.task_id);
    const leftPriority = leftRecord == null ? -1 : getTimelineStatusMeta(leftRecord).priority;
    const rightPriority = rightRecord == null ? -1 : getTimelineStatusMeta(rightRecord).priority;
    if (leftPriority !== rightPriority) {
      return rightPriority - leftPriority;
    }
    return String(right.updated_at).localeCompare(String(left.updated_at));
  });
}

function buildLatestRecordByTaskId(records) {
  const latestRecordByTaskId = new Map();
  for (const record of records) {
    if (!latestRecordByTaskId.has(record.task_id)) {
      latestRecordByTaskId.set(record.task_id, record);
    }
  }
  return latestRecordByTaskId;
}

function reconcileTimelineState(records) {
  const taskIds = new Set(records.map((record) => record.task_id));
  if (
    state.focusedTimelineTaskId &&
    !taskIds.has(state.focusedTimelineTaskId)
  ) {
    state.focusedTimelineTaskId = null;
  }
  if (
    state.filteredTimelineTaskId &&
    !taskIds.has(state.filteredTimelineTaskId)
  ) {
    state.filteredTimelineTaskId = null;
  }
  if (state.filteredTimelineTaskId) {
    state.focusedTimelineTaskId = state.filteredTimelineTaskId;
  }
}

function renderTimelineToolbar(records) {
  if (!records.length) {
    elements.timelineToolbar.classList.add("hidden");
    elements.timelineToolbar.innerHTML = "";
    return;
  }
  elements.timelineToolbar.classList.remove("hidden");
  const isRiskSort = state.timelineSortMode === "risk";
  const sortLabel = isRiskSort ? "排序：失败优先" : "排序：时间";
  const timelineState = getTimelineVisibleState(records);
  const pageSizeOptions = TIMELINE_PAGE_SIZE_OPTIONS.map(
    (size) =>
      `<option value="${size}"${size === state.timelinePageSize ? " selected" : ""}>每次 ${size}</option>`,
  ).join("");
  const loadMoreMarkup = `
    <div class="timeline-window-controls">
      <span class="muted">已显示 ${timelineState.shownCount} / ${timelineState.totalCount}</span>
      <span class="timeline-auto-load-note">${timelineState.canLoadMore ? `继续下滑将自动追加 ${timelineState.nextBatchCount} 条` : "已全部显示"}</span>
    </div>
  `;
  if (!state.focusedTimelineTaskId) {
    elements.timelineToolbar.innerHTML = `
      <div class="timeline-toolbar-main">
        <p class="muted">从历史任务点击“定位时间线”后，可以只看该任务每次计划卖出和实际成交的记录。</p>
      </div>
      <div class="timeline-toolbar-actions">
        <label class="timeline-page-size-label">
          <span class="muted">每次加载</span>
          <select class="timeline-page-size-select">${pageSizeOptions}</select>
        </label>
        <button type="button" class="button button-secondary timeline-toolbar-sort-button">${sortLabel}</button>
      </div>
      ${loadMoreMarkup}
    `;
    elements.timelineToolbar
      .querySelector(".timeline-toolbar-sort-button")
      ?.addEventListener("click", () => toggleTimelineSortMode());
    bindTimelineWindowControls();
    return;
  }
  const isFiltering =
    state.filteredTimelineTaskId === state.focusedTimelineTaskId;
  elements.timelineToolbar.innerHTML = `
    <div class="timeline-toolbar-main">
      <span class="status-pill tone-neutral">当前任务</span>
      <strong>${escapeHtml(shortIdentifier(state.focusedTimelineTaskId))}</strong>
    </div>
    <div class="timeline-toolbar-actions">
      <label class="timeline-page-size-label">
        <span class="muted">每次加载</span>
        <select class="timeline-page-size-select">${pageSizeOptions}</select>
      </label>
      <button type="button" class="button button-secondary timeline-toolbar-sort-button">${sortLabel}</button>
      <button type="button" class="button button-secondary timeline-toolbar-filter-button">${isFiltering ? "显示全部记录" : "只看当前任务"}</button>
      <button type="button" class="button button-secondary timeline-toolbar-clear-button">清除高亮</button>
    </div>
    ${loadMoreMarkup}
  `;
  elements.timelineToolbar
    .querySelector(".timeline-toolbar-sort-button")
    ?.addEventListener("click", () => toggleTimelineSortMode());
  elements.timelineToolbar
    .querySelector(".timeline-toolbar-filter-button")
    ?.addEventListener("click", () => {
      state.filteredTimelineTaskId = isFiltering
        ? null
        : state.focusedTimelineTaskId;
      state.timelineVisibleCount = getTimelineMinimumVisibleCount(
        state.detailRecords,
      );
      syncUrlState();
      renderTimelineToolbar(state.detailRecords);
      renderTimeline(state.detailRecords);
    });
  elements.timelineToolbar
    .querySelector(".timeline-toolbar-clear-button")
    ?.addEventListener("click", () => {
      state.focusedTimelineTaskId = null;
      state.filteredTimelineTaskId = null;
      state.timelineVisibleCount = state.timelinePageSize;
      syncUrlState();
      renderTimelineToolbar(state.detailRecords);
      renderTimeline(state.detailRecords);
    });
  bindTimelineWindowControls();
}

function toggleTimelineSortMode() {
  state.timelineSortMode = state.timelineSortMode === "time" ? "risk" : "time";
  state.timelineVisibleCount = resolveTimelineVisibleCountForCurrentContext(
    state.detailRecords,
  );
  syncUrlState();
  renderTimelineToolbar(state.detailRecords);
  renderTimeline(state.detailRecords);
}

function renderTimeline(records, options = {}) {
  const timelineState = getTimelineVisibleState(records);
  if (!timelineState.visibleRecords.length) {
    clearTimelineAutoLoadObserver();
    elements.recordTimeline.innerHTML = `<div class="empty-note">还没有执行记录，后续会在这里展示每次计划卖出和实际成交。</div>`;
    return;
  }
  elements.recordTimeline.innerHTML = timelineState.visibleRecords
    .map((record) => {
      const statusMeta = getTimelineStatusMeta(record);
      return `
      <article class="timeline-item${record.task_id === state.focusedTimelineTaskId ? " timeline-item-highlight" : ""}" data-task-id="${escapeHtml(record.task_id)}">
        <div class="timeline-head">
          <strong>${escapeHtml(record.rule_name)}</strong>
          <div class="timeline-tags">
            <span class="status-pill tone-${statusMeta.tone}">${escapeHtml(statusMeta.label)}</span>
          </div>
        </div>
        <p>${escapeHtml(formatRecordMessage(record.message || "-"))}</p>
        <div class="timeline-meta">
          <span>${new Date(record.created_at).toLocaleString("zh-CN")}</span>
          <span>${escapeHtml(formatRecordEventLabel(record.event_type))}</span>
          <span>${escapeHtml(formatRecordSizeSummary(record))}</span>
          <span>事件价 ${escapeHtml(formatPriceValue(record.event_price))}</span>
        </div>
      </article>
    `;
    })
    .join("");
  if (timelineState.canLoadMore) {
    elements.recordTimeline.insertAdjacentHTML(
      "beforeend",
      `<div class="timeline-auto-load-sentinel" aria-hidden="true"></div>`,
    );
  }
  bindTimelineAutoLoad(records);
  if (options.scrollFocused && state.focusedTimelineTaskId) {
    const firstMatch = elements.recordTimeline.querySelector(
      `.timeline-item[data-task-id="${CSS.escape(state.focusedTimelineTaskId)}"]`,
    );
    if (firstMatch) {
      firstMatch.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }
}

function sortTimelineRecords(records) {
  return [...records].sort((left, right) => {
    if (state.timelineSortMode === "risk") {
      const priorityDelta =
        getTimelineStatusMeta(right).priority - getTimelineStatusMeta(left).priority;
      if (priorityDelta !== 0) {
        return priorityDelta;
      }
    }
    return String(right.created_at).localeCompare(String(left.created_at));
  });
}

function focusTimelineTask(taskId) {
  state.focusedTimelineTaskId = taskId;
  state.filteredTimelineTaskId = null;
  state.timelineVisibleCount = resolveTimelineVisibleCountForTask(
    state.detailRecords,
    taskId,
  );
  syncUrlState();
  renderTimelineToolbar(state.detailRecords);
  renderTimeline(state.detailRecords, { scrollFocused: true });
}

function bindTimelineWindowControls() {
  elements.timelineToolbar
    .querySelector(".timeline-page-size-select")
    ?.addEventListener("change", (event) => {
      state.timelinePageSize = normalizeTimelinePageSize(
        Number(event.currentTarget.value),
      );
      state.timelineVisibleCount = getTimelineMinimumVisibleCount(
        state.detailRecords,
      );
      renderTimelineToolbar(state.detailRecords);
      renderTimeline(state.detailRecords);
    });
}

function bindTimelineAutoLoad(records) {
  clearTimelineAutoLoadObserver();
  const sentinel = elements.recordTimeline.querySelector(
    ".timeline-auto-load-sentinel",
  );
  if (!sentinel) {
    return;
  }
  timelineAutoLoadObserver = new IntersectionObserver(
    (entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) {
        return;
      }
      const timelineState = getTimelineVisibleState(records);
      if (!timelineState.canLoadMore) {
        clearTimelineAutoLoadObserver();
        return;
      }
      state.timelineVisibleCount = Math.min(
        timelineState.totalCount,
        timelineState.shownCount + state.timelinePageSize,
      );
      renderTimelineToolbar(records);
      renderTimeline(records);
    },
    {
      root: elements.recordTimeline,
      rootMargin: "0px 0px 160px 0px",
      threshold: 0,
    },
  );
  timelineAutoLoadObserver.observe(sentinel);
}

function clearTimelineAutoLoadObserver() {
  if (timelineAutoLoadObserver) {
    timelineAutoLoadObserver.disconnect();
    timelineAutoLoadObserver = null;
  }
}

function normalizeTimelinePageSize(value) {
  return TIMELINE_PAGE_SIZE_OPTIONS.includes(value) ? value : 20;
}

function resolveTimelineVisibleCountForCurrentContext(records) {
  return Math.max(
    state.timelineVisibleCount,
    getTimelineMinimumVisibleCount(records),
  );
}

function resolveTimelineVisibleCountForTask(records, taskId) {
  const sortedRecords = sortTimelineRecords(getVisibleTimelineRecords(records));
  const matchIndex = sortedRecords.findIndex((record) => record.task_id === taskId);
  if (matchIndex < 0) {
    return state.timelinePageSize;
  }
  return Math.ceil((matchIndex + 1) / state.timelinePageSize) * state.timelinePageSize;
}

function getTimelineMinimumVisibleCount(records) {
  if (state.filteredTimelineTaskId) {
    return state.timelinePageSize;
  }
  if (state.focusedTimelineTaskId) {
    return resolveTimelineVisibleCountForTask(
      records,
      state.focusedTimelineTaskId,
    );
  }
  return state.timelinePageSize;
}

function getVisibleTimelineRecords(records) {
  return state.filteredTimelineTaskId
    ? records.filter((record) => record.task_id === state.filteredTimelineTaskId)
    : records;
}

function getTimelineVisibleState(records) {
  const visibleRecords = sortTimelineRecords(getVisibleTimelineRecords(records));
  const totalCount = visibleRecords.length;
  if (!totalCount) {
    return {
      totalCount: 0,
      shownCount: 0,
      visibleRecords: [],
      canLoadMore: false,
      nextBatchCount: 0,
    };
  }
  const minimumVisibleCount = Math.min(
    totalCount,
    getTimelineMinimumVisibleCount(records),
  );
  const shownCount = Math.min(
    totalCount,
    Math.max(minimumVisibleCount, state.timelineVisibleCount),
  );
  state.timelineVisibleCount = shownCount;
  const remainingCount = totalCount - shownCount;
  return {
    totalCount,
    shownCount,
    visibleRecords: visibleRecords.slice(0, shownCount),
    canLoadMore: remainingCount > 0,
    nextBatchCount: Math.min(state.timelinePageSize, remainingCount),
  };
}

function shortIdentifier(value) {
  return `${value.slice(0, 8)}...${value.slice(-6)}`;
}

function renderTaskForm(position, currentTask, editingTask) {
  const form = elements.createTaskForm;
  const hasActiveTask = Boolean(currentTask && currentTask.status === "active");
  resetTaskForm();
  elements.cancelEditButton.classList.toggle("hidden", editingTask == null);
  hideTaskFormStatePanel();

  if (editingTask) {
    elements.taskFormKicker.textContent = "编辑任务";
    elements.taskFormTitle.textContent = "原地修改当前规则";
    elements.taskFormSubmitButton.textContent = "保存修改";
    fillFormFromTask(editingTask);
    setTaskFormDisabled(false);
    renderTaskFormStatePanel(editingTask);
    elements.formHint.textContent =
      "保存后任务保持 paused，请手动点击恢复生效。";
    return;
  }

  elements.taskFormKicker.textContent = "创建任务";
  elements.taskFormTitle.textContent = "快速新增一套规则";
  elements.taskFormSubmitButton.textContent = "创建任务";
  form.position_size.value = sanitizeNumber(position.size);
  form.average_cost.value = formatPriceInputValue(position.average_cost);
  if (hasActiveTask) {
    setTaskFormDisabled(true);
    elements.formHint.textContent =
      "当前 token 已有 active task。点“暂停后编辑”后可直接原地修改。";
    return;
  }
  setTaskFormDisabled(false);
  elements.formHint.textContent =
    currentTask && currentTask.status === "paused"
      ? "当前 token 下有 paused 任务。你可以点“编辑”原地修改，或直接创建新任务。"
      : "支持一次创建多条不同类型规则；protective stop 只能保留一条。";
}

async function handleTaskFormSubmit(event) {
  event.preventDefault();
  if (!state.selectedTokenId) {
    return;
  }
  const form = new FormData(event.currentTarget);
  const payload = buildTaskPayload(form);
  if (payload == null) {
    showBanner("至少填写一组规则。", "warn");
    return;
  }
  if (state.editingTaskId) {
    await fetchJson(`/tasks/${state.editingTaskId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.editingTaskId = null;
    showBanner("任务已更新，当前保持 paused，请手动恢复。", "success");
    await refreshDashboard({ keepSelection: true });
    return;
  }
  await fetchJson("/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token_id: state.selectedTokenId, ...payload }),
  });
  showBanner("任务已创建。", "success");
  await refreshDashboard({ keepSelection: true });
}

function buildTaskPayload(form) {
  const rules = buildRulesPayload(form);
  if (!rules.length) {
    return null;
  }
  return {
    dry_run: form.get("dry_run") === "on",
    slippage_bps: form.get("slippage_bps") || "50",
    position_size: normalizeNullableField(form.get("position_size")),
    average_cost: normalizePriceField(form.get("average_cost")),
    rules,
  };
}

function buildRulesPayload(form) {
  const rules = [];
  const breakevenStopSize = normalizeSizeField(
    form.get("breakeven_stop_size"),
  );
  const priceStop = normalizePriceField(form.get("price_stop"));
  const priceStopSize = normalizeSizeField(form.get("price_stop_size"));
  const takeProfit = normalizePriceField(form.get("take_profit"));
  const takeProfitSize = normalizeSizeField(form.get("take_profit_size"));
  const trailingDrawdown = normalizeRatioField(
    form.get("trailing_drawdown"),
  );
  const trailingSellSize = normalizeSizeField(
    form.get("trailing_sell_size"),
  );
  const trailingActivationPrice = normalizePriceField(
    form.get("trailing_activation_price"),
  );

  if (breakevenStopSize) {
    rules.push({ kind: "breakeven-stop", sell_size: breakevenStopSize });
  }
  if (priceStop || priceStopSize) {
    rules.push({
      kind: "price-stop",
      trigger_price: priceStop,
      sell_size: priceStopSize,
    });
  }
  if (takeProfit || takeProfitSize) {
    rules.push({
      kind: "take-profit",
      trigger_price: takeProfit,
      sell_size: takeProfitSize,
    });
  }
  if (trailingDrawdown || trailingSellSize || trailingActivationPrice) {
    rules.push({
      kind: "trailing-take-profit",
      trigger_price: trailingActivationPrice,
      drawdown_ratio: trailingDrawdown,
      sell_size: trailingSellSize,
    });
  }
  return rules;
}

async function mutateTask(taskId, action) {
  const method = action === "delete" ? "DELETE" : "POST";
  const path =
    action === "delete" ? `/tasks/${taskId}` : `/tasks/${taskId}/${action}`;
  await fetchJson(path, { method });
  if (action === "delete" || action === "resume") {
    if (state.editingTaskId === taskId) {
      state.editingTaskId = null;
    }
  }
  showBanner(
    `任务已${action === "pause" ? "暂停" : action === "resume" ? "恢复" : "删除"}。`,
    "success",
  );
  await refreshDashboard({ keepSelection: true });
}

async function startEditTask(task) {
  if (!task) {
    return;
  }
  if (task.status !== "active" && task.status !== "paused") {
    showBanner("只有 active 或 paused 任务支持编辑。", "warn");
    return;
  }
  if (!canEditTaskInForm(task)) {
    showBanner(
      "当前任务包含页面表单暂不支持的规则组合，暂时无法原地编辑。",
      "warn",
    );
    return;
  }
  if (task.status === "active") {
    await fetchJson(`/tasks/${task.task_id}/pause`, { method: "POST" });
    state.editingTaskId = task.task_id;
    showBanner(
      "任务已暂停，已进入编辑模式。保存后会继续保持 paused。",
      "success",
    );
    await refreshDashboard({ keepSelection: true });
    return;
  }
  state.editingTaskId = task.task_id;
  showBanner("已进入编辑模式。保存后任务保持 paused。", "success");
  renderDetail(state.selectedTokenId, state.detailTasks, state.detailRecords);
}

function cancelEditMode() {
  state.editingTaskId = null;
  showBanner("已取消编辑。", "success");
  renderDetail(state.selectedTokenId, state.detailTasks, state.detailRecords);
}

function resetTaskForm() {
  elements.createTaskForm.reset();
  elements.createTaskForm.slippage_bps.value = "50";
}

function setTaskFormDisabled(disabled) {
  for (const input of elements.createTaskForm.querySelectorAll("input")) {
    input.disabled = disabled;
  }
  elements.taskFormSubmitButton.disabled = disabled;
}

function fillFormFromTask(task) {
  const form = elements.createTaskForm;
  form.position_size.value = sanitizeNumber(task.position_size);
  form.average_cost.value = formatPriceInputValue(task.average_cost);
  form.slippage_bps.value = sanitizeNumber(task.slippage_bps) || "50";
  form.dry_run.checked = Boolean(task.dry_run);

  for (const rule of task.rules) {
    switch (rule.kind) {
      case "breakeven-stop":
        form.breakeven_stop_size.value = formatSizeInputValue(rule.sell_size);
        break;
      case "price-stop":
        form.price_stop.value = formatPriceInputValue(rule.trigger_price);
        form.price_stop_size.value = formatSizeInputValue(rule.sell_size);
        break;
      case "take-profit":
        form.take_profit.value = formatPriceInputValue(rule.trigger_price);
        form.take_profit_size.value = formatSizeInputValue(rule.sell_size);
        break;
      case "trailing-take-profit":
        form.trailing_drawdown.value = formatRatioInputValue(rule.drawdown_ratio);
        form.trailing_sell_size.value = formatSizeInputValue(rule.sell_size);
        form.trailing_activation_price.value = formatPriceInputValue(
          rule.trigger_price,
        );
        break;
      default:
        break;
    }
  }
}

function renderTaskFormStatePanel(task) {
  const rows = task.rules
    .map(
      (rule) => `
      <article class="runtime-state-card">
        <div class="runtime-state-head">
          <strong>${escapeHtml(rule.name)}</strong>
          <span class="status-pill tone-${rule.runtime_state.is_complete ? "ok" : rule.runtime_state.is_triggered ? "warn" : "neutral"}">${rule.runtime_state.is_complete ? "COMPLETE" : rule.runtime_state.is_triggered ? "TRIGGERED" : "IDLE"}</span>
        </div>
        <div class="runtime-state-grid">
          <span>已成交 ${escapeHtml(formatSizeValue(rule.runtime_state.sold_size))}</span>
          <span>已锁定卖出 ${escapeHtml(formatSizeValue(rule.runtime_state.locked_size))}</span>
          <span>剩余待卖 ${escapeHtml(formatSizeValue(rule.runtime_state.remaining_size))}</span>
          <span>Peak Bid ${escapeHtml(formatPriceValue(rule.runtime_state.peak_bid))}</span>
          <span>Trigger Bid ${escapeHtml(formatPriceValue(rule.runtime_state.trigger_bid))}</span>
        </div>
      </article>
    `,
    )
    .join("");
  elements.taskFormStatePanel.innerHTML = `
    <p class="panel-kicker">规则运行态</p>
    <div class="runtime-state-list">${rows}</div>
  `;
  elements.taskFormStatePanel.classList.remove("hidden");
}

function hideTaskFormStatePanel() {
  elements.taskFormStatePanel.innerHTML = "";
  elements.taskFormStatePanel.classList.add("hidden");
}

function canEditTaskInForm(task) {
  const supportedKinds = new Set([
    "breakeven-stop",
    "price-stop",
    "take-profit",
    "trailing-take-profit",
  ]);
  const ruleCounts = new Map();
  for (const rule of task.rules) {
    if (!supportedKinds.has(rule.kind)) {
      return false;
    }
    ruleCounts.set(rule.kind, (ruleCounts.get(rule.kind) || 0) + 1);
    if (ruleCounts.get(rule.kind) > 1) {
      return false;
    }
  }
  return true;
}

function pickCurrentTask(tasks) {
  const sortedTasks = getSortedTasks(
    tasks.filter((task) => task.status !== "deleted"),
  );
  return (
    sortedTasks.find((task) => task.status === "active") ||
    sortedTasks.find((task) => task.status === "paused") ||
    null
  );
}

function resolveEditingTask(tasks) {
  if (!state.editingTaskId) {
    return null;
  }
  return tasks.find((task) => task.task_id === state.editingTaskId) || null;
}

function getSortedTasks(tasks) {
  return [...tasks].sort((left, right) =>
    String(right.updated_at).localeCompare(String(left.updated_at)),
  );
}

function renderEmptyDetail() {
  elements.detailView.classList.add("hidden");
  elements.detailEmpty.classList.remove("hidden");
}

function actionButton(label, onClick, className) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${className}`;
  button.textContent = label;
  button.addEventListener("click", () =>
    onClick().catch((error) =>
      showBanner(error.message || String(error), "error"),
    ),
  );
  return button;
}

function formatRule(rule) {
  if (rule.kind === "breakeven-stop") {
    return `计划卖出 ${formatSizeValue(rule.sell_size)}`;
  }
  if (rule.kind === "trailing-take-profit") {
    return `回撤 ${formatRatioValue(rule.drawdown_ratio)} · 计划卖出 ${formatSizeValue(rule.sell_size)}${rule.trigger_price ? ` · 激活 ${formatPriceValue(rule.trigger_price)}` : ""}`;
  }
  return `触发 ${formatPriceValue(rule.trigger_price)} · 计划卖出 ${formatSizeValue(rule.sell_size)}`;
}

function renderRuntimeStateInline(runtimeState) {
  return `
    <div class="runtime-inline-grid">
      <span>已成交 ${escapeHtml(formatSizeValue(runtimeState.sold_size))}</span>
      <span>已锁定卖出 ${escapeHtml(formatSizeValue(runtimeState.locked_size))}</span>
      <span>Peak ${escapeHtml(formatPriceValue(runtimeState.peak_bid))}</span>
    </div>
  `;
}

function renderHistoryProgressDisclosure(task) {
  if (!hasTaskRuntimeState(task)) {
    return "";
  }
  const summary = summarizeTaskRuntime(task);
  return `
    <details class="history-progress-disclosure"${shouldOpenHistoryProgress(task) ? " open" : ""}>
      <summary class="history-progress-summary">规则运行态 · ${summary.armedCount} 已激活 / ${summary.triggeredCount} 已触发 / ${summary.completedCount} 已完成</summary>
      <div class="history-progress-list">
        ${task.rules.map((rule) => renderHistoryRuleProgress(rule)).join("")}
      </div>
    </details>
  `;
}

function renderHistoryRuleProgress(rule) {
  const runtimeState = rule.runtime_state;
  const statusTone = runtimeState.is_complete
    ? "ok"
    : runtimeState.is_triggered
      ? "warn"
      : isRuleArmed(runtimeState)
        ? "neutral"
        : "neutral";
  const statusLabel = runtimeState.is_complete
    ? "已完成"
    : runtimeState.is_triggered
      ? "已触发"
      : isRuleArmed(runtimeState)
        ? "已激活"
        : "空闲";
  return `
    <article class="history-progress-item">
      <div class="history-progress-head">
        <strong>${escapeHtml(rule.name)}</strong>
        <span class="status-pill tone-${statusTone}">${statusLabel}</span>
      </div>
      <div class="history-progress-grid">
        <span>已成交 ${escapeHtml(formatSizeValue(runtimeState.sold_size))}</span>
        <span>已锁定卖出 ${escapeHtml(formatSizeValue(runtimeState.locked_size))}</span>
        <span>剩余待卖 ${escapeHtml(formatSizeValue(runtimeState.remaining_size))}</span>
        <span>Trigger ${escapeHtml(formatPriceValue(runtimeState.trigger_bid))}</span>
        <span>Peak ${escapeHtml(formatPriceValue(runtimeState.peak_bid))}</span>
      </div>
    </article>
  `;
}

function formatRuleSummary(rule) {
  return `${rule.name} · ${formatRule(rule)}`;
}

function formatRecordEventLabel(eventType) {
  if (eventType === "rule") {
    return "规则检查";
  }
  if (eventType === "trade") {
    return "成交回报";
  }
  if (eventType === "order") {
    return "订单更新";
  }
  if (eventType === "system") {
    return "系统事件";
  }
  return String(eventType);
}

function formatRecordSizeSummary(record) {
  return `计划卖出 ${formatSizeValue(record.requested_size)} · 实际成交 ${formatSizeValue(record.filled_size)}`;
}

function summarizeTaskRuntime(task) {
  const runtimeRules = task.rules.filter((rule) =>
    hasMeaningfulRuntimeState(rule.runtime_state),
  );
  return {
    armedCount: runtimeRules.filter((rule) => isRuleArmed(rule.runtime_state))
      .length,
    triggeredCount: runtimeRules.filter(
      (rule) =>
        rule.runtime_state.is_triggered && !rule.runtime_state.is_complete,
    ).length,
    completedCount: runtimeRules.filter(
      (rule) => rule.runtime_state.is_complete,
    ).length,
  };
}

function shouldOpenHistoryProgress(task) {
  return task.status === "active" || task.status === "paused";
}

function hasTaskRuntimeState(task) {
  return task.rules.some((rule) =>
    hasMeaningfulRuntimeState(rule.runtime_state),
  );
}

function isRuleArmed(runtimeState) {
  return (
    hasMeaningfulRuntimeState(runtimeState) &&
    !runtimeState.is_triggered &&
    !runtimeState.is_complete
  );
}

function hasMeaningfulRuntimeState(runtimeState) {
  return (
    runtimeState.is_triggered ||
    runtimeState.is_complete ||
    runtimeState.locked_size != null ||
    runtimeState.trigger_bid != null ||
    runtimeState.peak_bid != null ||
    runtimeState.sold_size !== "0" ||
    runtimeState.remaining_size !== "0"
  );
}

function shortToken(tokenId) {
  return `${tokenId.slice(0, 8)}...${tokenId.slice(-6)}`;
}

function sanitizeNumber(value) {
  return value && value !== "-" ? value : "";
}

function normalizeNullableField(value) {
  return value == null || value === "" ? null : String(value);
}

function formatPriceValue(value) {
  return formatScaledDisplayValue(value, 100, "c");
}

function formatSizeValue(value) {
  return formatPlainDisplayValue(value, "股");
}

function formatRatioValue(value) {
  return formatScaledDisplayValue(value, 100, "%");
}

function formatPercentMetricValue(value) {
  const numberText = formatPercentMetricNumber(value);
  return numberText == null ? "-" : `${numberText}%`;
}

function formatPercentMetricNumber(value) {
  if (value == null || value === "" || value === "-") {
    return null;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return String(value);
  }
  const percentValue = Math.abs(numeric) <= 1 ? numeric * 100 : numeric;
  return formatNumberForUi(percentValue);
}

function formatPriceInputValue(value) {
  return formatScaledInputValue(value, 100);
}

function formatSizeInputValue(value) {
  return formatPlainInputValue(value);
}

function formatRatioInputValue(value) {
  return formatScaledInputValue(value, 100);
}

function normalizePriceField(value) {
  return normalizeScaledField(value, 100);
}

function normalizeSizeField(value) {
  return normalizePlainField(value);
}

function normalizeRatioField(value) {
  return normalizeScaledField(value, 100);
}

function formatPlainDisplayValue(value, suffix) {
  const numberText = formatPlainDisplayNumber(value);
  return numberText == null ? "-" : `${numberText}${suffix}`;
}

function formatPlainDisplayNumber(value) {
  if (value == null || value === "" || value === "-") {
    return null;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return String(value);
  }
  return formatNumberForUi(numeric);
}

function formatPlainInputValue(value) {
  if (value == null || value === "" || value === "-") {
    return "";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return String(value);
  }
  return trimNumericString(numeric, 6);
}

function normalizePlainField(value) {
  const normalized = normalizeNullableField(value);
  if (normalized == null) {
    return null;
  }
  const numeric = Number(normalized);
  if (!Number.isFinite(numeric)) {
    return normalized;
  }
  return trimNumericString(numeric, 6);
}

function formatScaledDisplayValue(value, scale, suffix) {
  const numberText = formatScaledDisplayNumber(value, scale);
  return numberText == null ? "-" : `${numberText}${suffix}`;
}

function formatScaledDisplayNumber(value, scale) {
  if (value == null || value === "" || value === "-") {
    return null;
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return String(value);
  }
  return formatNumberForUi(numeric * scale);
}

function formatScaledInputValue(value, scale) {
  if (value == null || value === "" || value === "-") {
    return "";
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) {
    return String(value);
  }
  return trimNumericString(numeric * scale, 2);
}

function normalizeScaledField(value, scale) {
  const normalized = normalizeNullableField(value);
  if (normalized == null) {
    return null;
  }
  const numeric = Number(normalized);
  if (!Number.isFinite(numeric)) {
    return normalized;
  }
  return trimNumericString(numeric / scale, 6);
}

function formatNumberForUi(value) {
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 2,
  }).format(value);
}

function trimNumericString(value, maxFractionDigits) {
  return Number(value)
    .toFixed(maxFractionDigits)
    .replace(/\.0+$|(?<=\.[0-9]*?)0+$/g, "")
    .replace(/\.$/, "");
}

function formatRecordMessage(message) {
  return String(message)
    .replace(
      /best bid ([0-9]+(?:\.[0-9]+)?) is establishing the trailing peak/gi,
      (_, bestBid) =>
        `best bid ${formatPriceValue(bestBid)} is establishing the trailing peak`,
    )
    .replace(
      /best bid ([0-9]+(?:\.[0-9]+)?) has not armed trailing take-profit at ([0-9]+(?:\.[0-9]+)?)/gi,
      (_, bestBid, activationPrice) =>
        `best bid ${formatPriceValue(bestBid)} has not armed trailing take-profit at ${formatPriceValue(activationPrice)}`,
    )
    .replace(
      /best bid ([0-9]+(?:\.[0-9]+)?) has not drawn down to ([0-9]+(?:\.[0-9]+)?) from peak ([0-9]+(?:\.[0-9]+)?)/gi,
      (_, bestBid, threshold, peakBid) =>
        `best bid ${formatPriceValue(bestBid)} has not drawn down to ${formatPriceValue(threshold)} from peak ${formatPriceValue(peakBid)}`,
    )
    .replace(
      /best bid ([0-9]+(?:\.[0-9]+)?) has not reached ([0-9]+(?:\.[0-9]+)?)/gi,
      (_, bestBid, threshold) =>
        `best bid ${formatPriceValue(bestBid)} has not reached ${formatPriceValue(threshold)}`,
    )
    .replace(
      /best bid ([0-9]+(?:\.[0-9]+)?) crossed ([0-9]+(?:\.[0-9]+)?)/gi,
      (_, bestBid, threshold) =>
        `best bid ${formatPriceValue(bestBid)} crossed ${formatPriceValue(threshold)}`,
    );
}

function formatMaybeSeconds(value) {
  if (value == null) {
    return "-";
  }
  return `${Number(value).toFixed(1)}s`;
}

function staleTone(value, threshold) {
  if (value == null) {
    return "neutral";
  }
  if (value >= threshold) {
    return "danger";
  }
  if (value >= threshold * 0.66) {
    return "warn";
  }
  return "ok";
}

function showBanner(message, tone, hidden = false) {
  if (hidden || !message) {
    elements.banner.classList.add("hidden");
    elements.banner.textContent = "";
    return;
  }
  elements.banner.className = `banner banner-${tone}`;
  elements.banner.textContent = message;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = `请求失败：${response.status}`;
    try {
      const payload = await response.json();
      detail = payload.detail || detail;
    } catch {
      detail = (await response.text()) || detail;
    }
    throw new Error(detail);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>'"]/g,
    (char) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "'": "&#39;",
        '"': "&quot;",
      })[char],
  );
}
