const pageTitles = {
  dashboard: "知识能力概览",
  query: "可信变更方案生成",
  results: "变更结果与审计",
  ingest: "知识采集与加工",
  review: "知识审核队列",
  library: "知识资产库",
};

const changeStatusLabels = {
  DRAFT: "草稿", BLOCKED: "已阻断", READY_FOR_APPROVAL: "待提交审批",
  WAITING_APPROVAL: "等待审批", APPROVED: "已批准", EXECUTING: "执行中",
  VERIFYING: "验证中", SUCCEEDED: "变更成功", ROLLED_BACK: "已安全回退",
  FAILED: "执行失败", REJECTED: "已拒绝",
};

const changeStageDefinitions = [
  ["环境感知", "实时快照"], ["经验复用", "APPROVED 知识"],
  ["分层决策", "风险与约束"], ["方案生成", "步骤与回退"],
  ["工具验证", "硬门禁"], ["人工审批", "摘要绑定"],
  ["反馈沉淀", "待审核候选"],
];

let activeChangeSession = null;
let changePollTimer = null;
let changeExecutionSubmitting = false;
let changeFailureMode = "";
let changeCases = [];
let selectedChangeCaseId = "dc-route-failover";

const statusLabels = {
  DRAFT: "草稿",
  PENDING_REVIEW: "待审核",
  APPROVED: "已批准",
  REJECTED: "已驳回",
  SUPERSEDED: "已替代",
};

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function api(path, options = {}) {
  const headers = options.body instanceof FormData
    ? { ...(options.headers || {}) }
    : { "Content-Type": "application/json", ...(options.headers || {}) };
  const response = await fetch(path, {
    ...options,
    headers,
  });
  const payload = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(payload.error || payload.detail || `HTTP ${response.status}`);
  return payload;
}

function toast(message, error = false) {
  const element = document.getElementById("toast");
  element.textContent = message;
  element.className = `toast show${error ? " error" : ""}`;
  clearTimeout(window.toastTimer);
  window.toastTimer = setTimeout(() => element.className = "toast", 3600);
}

function setBusy(button, busy, label = "处理中……") {
  if (!button) return;
  if (busy) {
    button.dataset.originalText = button.textContent;
    button.textContent = label;
    button.disabled = true;
  } else {
    button.textContent = button.dataset.originalText || button.textContent;
    button.disabled = false;
  }
}

function renderChangeStages(session) {
  const target = document.getElementById("change-stages");
  if (!target) return;
  const run = session?.runs?.generate;
  const ticket = session?.package?.ticket;
  const status = ticket?.status || "";
  let doneThrough = -1;
  let current = session ? 0 : -1;
  let failed = -1;
  if (run?.status === "RUNNING") {
    const succeededSteps = (run.steps || []).filter(step => step.status === "SUCCEEDED").length;
    doneThrough = Math.min(succeededSteps - 1, 3);
    current = Math.min(succeededSteps, 3);
  }
  if (ticket) { doneThrough = 4; current = 5; }
  if (["APPROVED", "EXECUTING", "VERIFYING"].includes(status)) { doneThrough = 5; current = 6; }
  if (["SUCCEEDED", "ROLLED_BACK"].includes(status) && session.package.feedback) { doneThrough = 6; current = -1; }
  if (status === "BLOCKED") { doneThrough = 3; current = -1; failed = 4; }
  if (status === "REJECTED") { doneThrough = 4; current = -1; failed = 5; }
  if (run?.status === "FAILED" && !ticket) { current = -1; failed = Math.max(doneThrough + 1, 0); }
  target.innerHTML = changeStageDefinitions.map(([name, detail], index) => {
    const state = index === failed ? "failed" : index <= doneThrough ? "done" : index === current ? "current" : "";
    const mark = state === "done" ? "✓" : state === "failed" ? "!" : String(index + 1);
    return `<div class="change-stage ${state}"><span>${mark}</span><strong>${name}</strong><small>${detail}</small></div>`;
  }).join("");
}

function routeNextHop(network, tableId, destination) {
  const routes = network?.state?.route_tables?.[tableId]?.routes || [];
  return routes.find(route => route.destination === destination)?.next_hop || "unknown";
}

function summarizeStepIds(items) {
  if (!items?.length) return "无";
  if (items.length <= 6) return items.join("、");
  return `${items.slice(0, 4).join("、")} 等 ${items.length} 步`;
}

function renderChangeTopology(network, targetId = "change-topology") {
  const target = document.getElementById(targetId);
  if (!target) return;
  if (!network) {
    target.innerHTML = `<div class="change-empty"><div><h2>正在准备模拟网络</h2><p>不会读取真实云资源。</p></div></div>`;
    return;
  }
  const state = network.state || {};
  const routeTables = Object.entries(state.route_tables || {});
  const [tableAId, tableA = {}] = routeTables[0] || ["route-table-a", {}];
  const [tableBId, tableB = {}] = routeTables[1] || ["route-table-b", {}];
  const targetRoute = [...(tableA.routes || []), ...(tableB.routes || [])]
    .find(route => route.type !== "local") || {};
  const destination = targetRoute.destination || "unknown";
  const hopA = routeNextHop(network, tableAId, destination);
  const hopB = routeNextHop(network, tableBId, destination);
  const nextHops = Object.entries(state.next_hops || {});
  const [sourceHopId, sourceHop = {}] = nextHops.find(([, hop]) => hop.status !== "UP") || nextHops[0] || ["source", {}];
  const [targetHopId, targetHop = {}] = nextHops.find(([, hop]) => hop.status === "UP") || nextHops[1] || ["target", {}];
  const targetActive = hopA === targetHopId || hopB === targetHopId;
  const services = Object.values(state.services || {});
  const ports = services[0]?.ports || [443, 5432];
  target.innerHTML = `
    <span class="topology-context">${escapeHtml(state.region)} / ${escapeHtml(state.vpc?.id)} · 展示 2 / ${routeTables.length} 张路由表</span>
    <div class="topology-line a ${hopA === targetHopId ? "active" : "degraded"}"></div>
    <div class="topology-line b ${hopB === targetHopId ? "active" : "degraded"}"></div>
    <div class="topology-node route-a"><div><strong>${escapeHtml(tableAId)}</strong><small>${escapeHtml(tableA.availability_zone)} · 下一跳 ${escapeHtml(hopA)}</small></div></div>
    <div class="topology-node route-b"><div><strong>${escapeHtml(tableBId)}</strong><small>${escapeHtml(tableB.availability_zone)} · 下一跳 ${escapeHtml(hopB)}</small></div></div>
    <div class="topology-node destination"><div><strong>目标业务网段</strong><small>${escapeHtml(destination)} · ${escapeHtml(ports.join(" / "))}</small></div></div>
    <div class="topology-node primary degraded"><div><strong>${escapeHtml(sourceHopId)}</strong><small>${escapeHtml(sourceHop.status)} · 容量 ${escapeHtml(sourceHop.capacity_utilization_percent)}%</small></div></div>
    <div class="topology-node standby ${targetActive ? "active" : ""}"><div><strong>${escapeHtml(targetHopId)}</strong><small>${escapeHtml(targetHop.status)} · 容量 ${escapeHtml(targetHop.capacity_utilization_percent)}%</small></div></div>`;
}

function renderChangeCases() {
  const target = document.getElementById("change-case-catalog");
  if (!target) return;
  document.getElementById("change-case-count").textContent = `${changeCases.length} 个 APPROVED 案例`;
  target.innerHTML = changeCases.map(item => `
    <button type="button" role="radio" aria-checked="${item.case_id === selectedChangeCaseId}"
      class="change-case-card ${item.case_id === selectedChangeCaseId ? "selected" : ""}"
      data-case-id="${escapeHtml(item.case_id)}">
      <span class="case-card-meta"><em>${escapeHtml(item.category)}</em><b>${escapeHtml(item.risk_level)} · ${item.risk_score}</b></span>
      <strong>${escapeHtml(item.label)}</strong>
      <small>${escapeHtml(item.description)}</small>
      <span class="case-card-evidence">K${item.knowledge_card_id} · ${escapeHtml(item.knowledge_status)} · ${item.execution_step_count} 步 · ${escapeHtml(item.ticket_id)}</span>
    </button>`).join("");
  target.querySelectorAll(".change-case-card").forEach(button => button.addEventListener("click", () => {
    selectedChangeCaseId = button.dataset.caseId;
    renderChangeCases();
  }));
  const selected = changeCases.find(item => item.case_id === selectedChangeCaseId);
  const failureSelect = document.getElementById("change-failure");
  if (failureSelect && selected) {
    const previous = failureSelect.value;
    failureSelect.innerHTML = `<option value="">正常闭环</option>`
      + (selected.failure_injection_points || []).map((point, index) =>
        `<option value="${escapeHtml(point.step_id)}">第 ${index + 1} 步失败：${escapeHtml(point.label)}</option>`
      ).join("");
    failureSelect.value = (selected.failure_injection_points || []).some(point => point.step_id === previous)
      ? previous : "";
  }
}

async function loadChangeCases() {
  const payload = await api("/api/change-cases");
  changeCases = payload.cases || [];
  if (!changeCases.some(item => item.case_id === selectedChangeCaseId) && changeCases.length) {
    selectedChangeCaseId = changeCases[0].case_id;
  }
  renderChangeCases();
}

function renderChangeTimeline(session) {
  const target = document.getElementById("change-timeline");
  if (!target) return;
  const items = [];
  [session.runs?.generate, session.runs?.execute].filter(Boolean).forEach(run => {
    (run.events || []).forEach(event => items.push({
      title: event.event_type,
      detail: event.payload?.message || event.payload?.error || `${run.task_type} · ${run.status}`,
      time: event.created_at,
      tone: event.event_type.includes("fail") ? "fail" : event.event_type.includes("approval") ? "warn" : "",
      order: event.id || 0,
    }));
  });
  (session.package?.audit || []).forEach(audit => items.push({
    title: audit.action,
    detail: `${audit.actor} · ${JSON.stringify(audit.detail)}`,
    time: audit.created_at,
    tone: audit.action.includes("REJECT") || audit.action.includes("BLOCK") ? "fail" : audit.action.includes("APPROVAL") ? "warn" : "",
    order: 100000 + (audit.id || 0),
  }));
  items.sort((a, b) => String(a.time || "").localeCompare(String(b.time || "")) || a.order - b.order);
  const latest = items.slice(-30);
  target.innerHTML = latest.length ? latest.map(item => `
    <div class="timeline-row"><span class="timeline-dot ${item.tone}"></span><div><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.detail)}</small></div><time>${escapeHtml((item.time || "").slice(11, 19))}</time></div>`).join("")
    : `<p class="muted-copy">等待编排事件。</p>`;
  target.scrollTop = target.scrollHeight;
}

function renderChangeFeedback(session) {
  const feedbackTarget = document.getElementById("change-feedback");
  const publishButton = document.getElementById("change-publish-feedback");
  if (!feedbackTarget || !publishButton) return;
  const packageData = session?.package;
  if (packageData?.feedback) {
    const feedback = packageData.feedback;
    const published = session.published_feedback;
    feedbackTarget.innerHTML = `<div class="feedback-result"><strong>${escapeHtml(changeStatusLabels[feedback.outcome] || feedback.outcome)}</strong>
      <p>计划 / 应用 / 回退：${feedback.planned_steps} / ${feedback.applied_steps} / ${feedback.rollback_steps}</p>
      <p>${feedback.lessons.map(escapeHtml).join("；")}</p>
      <p>${published ? `已进入主知识库 K${published.knowledge_card_id}，状态 ${published.status}` : `隔离候选 K${feedback.knowledge_candidate_id}，尚未进入主知识库`}</p></div>`;
    publishButton.hidden = false;
    publishButton.textContent = published ? `查看审核队列中的 K${published.knowledge_card_id}` : "送入知识审核队列";
    publishButton.className = published ? "button primary" : "button secondary";
  } else {
    feedbackTarget.innerHTML = `<p class="muted-copy">执行完成后生成带日志证据的知识候选。</p>`;
    publishButton.hidden = true;
  }
}

function renderChangeResults(session) {
  const empty = document.getElementById("result-empty");
  const workspace = document.getElementById("result-workspace");
  const resultCount = document.getElementById("result-count");
  const ticket = session?.package?.ticket;
  const terminalStatuses = ["SUCCEEDED", "ROLLED_BACK", "FAILED", "REJECTED", "BLOCKED"];
  const ready = Boolean(ticket && terminalStatuses.includes(ticket.status));
  resultCount.textContent = ready ? "1" : "0";
  empty.hidden = ready;
  workspace.hidden = !ready;
  if (!ready) return;

  const execution = session.package.execution;
  const executionValidations = (session.package.validations || []).filter(item => item.phase === "EXECUTION");
  const executionPassed = executionValidations.filter(item => item.status === "PASS").length;
  const outcome = execution?.outcome || ticket.status;
  const applied = execution?.applied_steps || [];
  const rolledBack = execution?.rollback_steps || [];
  const beforeHash = execution?.before_state_hash || ticket.environment_snapshot_hash;
  const afterHash = execution?.after_state_hash || session.network?.state_hash || "—";
  const stateMessage = outcome === "ROLLED_BACK" && beforeHash === afterHash
    ? "回退后状态哈希与变更前一致"
    : outcome === "SUCCEEDED" ? "目标下一跳已生效" : "未形成成功执行结果";

  document.getElementById("result-status").textContent = changeStatusLabels[ticket.status] || ticket.status;
  document.getElementById("result-ticket-id").textContent = `${ticket.ticket_id} · R${ticket.revision}`;
  document.getElementById("result-outcome").textContent = changeStatusLabels[outcome] || outcome;
  document.getElementById("result-step-score").textContent = `应用 ${applied.length} · 回退 ${rolledBack.length}`;
  document.getElementById("result-validation-score").textContent = executionValidations.length ? `${executionPassed} / ${executionValidations.length}` : "—";
  document.getElementById("result-network-version").textContent = session.network ? `v${session.network.version}` : "—";
  document.getElementById("result-hash-match").textContent = stateMessage;
  document.getElementById("result-network-hash").textContent = session.network?.state_hash || "—";
  document.getElementById("result-network-hash").title = session.network?.state_hash || "";
  const badge = document.getElementById("result-outcome-badge");
  badge.textContent = changeStatusLabels[outcome] || outcome;
  badge.className = `tag ${outcome === "SUCCEEDED" ? "APPROVED" : outcome === "ROLLED_BACK" ? "PENDING_REVIEW" : "REJECTED"}`;

  document.getElementById("result-summary").innerHTML = `
    <div class="result-summary-row"><span>变更对象</span><strong>${escapeHtml(ticket.region)} / ${escapeHtml(ticket.vpc_id)}</strong></div>
    <div class="result-summary-row"><span>已应用步骤</span><strong title="${escapeHtml(applied.join("、"))}">${escapeHtml(summarizeStepIds(applied))}</strong></div>
    <div class="result-summary-row"><span>回退步骤</span><strong title="${escapeHtml(rolledBack.join("、"))}">${escapeHtml(rolledBack.length ? summarizeStepIds(rolledBack) : "未触发")}</strong></div>
    <div class="result-summary-row"><span>变更前哈希</span><code title="${escapeHtml(beforeHash)}">${escapeHtml(beforeHash)}</code></div>
    <div class="result-summary-row"><span>变更后哈希</span><code title="${escapeHtml(afterHash)}">${escapeHtml(afterHash)}</code></div>`;
  document.getElementById("result-execution-validations").innerHTML = executionValidations.length
    ? executionValidations.map(item => `<div class="validation-row"><span class="${escapeHtml(item.status)}">${escapeHtml(item.status)}</span><div><strong>${escapeHtml(item.validator)}</strong><small>${escapeHtml(item.message)}</small></div></div>`).join("")
    : `<p class="muted-copy">本次结果没有执行期验证记录。</p>`;
  renderChangeTopology(session.network, "result-topology");
  renderChangeTimeline(session);
  renderChangeFeedback(session);
}

function renderChangeSession(session) {
  activeChangeSession = session;
  if (session?.case?.case_id) {
    selectedChangeCaseId = session.case.case_id;
    renderChangeCases();
  }
  renderChangeStages(session);
  const empty = document.getElementById("change-empty");
  const workspace = document.getElementById("change-workspace");
  if (!session) {
    empty.hidden = false;
    workspace.hidden = true;
    renderChangeResults(null);
    return;
  }
  empty.hidden = true;
  workspace.hidden = false;
  const packageData = session.package;
  const ticket = packageData?.ticket;
  const generateRun = session.runs?.generate;
  const executeRun = session.runs?.execute;
  const validations = packageData?.validations || [];
  const passed = validations.filter(item => item.status === "PASS").length;
  const outcome = packageData?.execution?.outcome;
  document.getElementById("change-status").textContent = ticket ? (changeStatusLabels[ticket.status] || ticket.status) : "生成中";
  document.getElementById("change-run-state").textContent = executeRun ? `执行 Run · ${executeRun.status}` : `生成 Run · ${generateRun?.status || "QUEUED"}`;
  document.getElementById("change-risk").textContent = ticket ? `${ticket.risk_score} / 100` : "—";
  document.getElementById("change-validation-score").textContent = validations.length ? `${passed} / ${validations.length}` : "—";
  document.getElementById("change-outcome").textContent = outcome ? (changeStatusLabels[outcome] || outcome) : "未执行";
  document.getElementById("change-network-version").textContent = session.network ? `快照 v${session.network.version} · ${session.operations.length} 条操作日志` : "正在初始化";
  document.getElementById("change-network-hash").textContent = session.network?.state_hash || "—";
  document.getElementById("change-network-hash").title = session.network?.state_hash || "";
  renderChangeTopology(session.network);
  if (ticket) {
    document.getElementById("change-confirmation").placeholder = `APPROVE ${ticket.ticket_id}`;
    document.getElementById("change-ticket-id").textContent = `${ticket.ticket_id} · R${ticket.revision}`;
    document.getElementById("change-risk-badge").textContent = `${ticket.risk_level}风险`;
    document.getElementById("change-risk-badge").className = "tag REJECTED";
    document.getElementById("change-ticket-title").textContent = ticket.title;
    document.getElementById("change-ticket-summary").textContent = ticket.summary;
    const facts = [
      ["环境", `${ticket.region} / ${ticket.vpc_id}`], ["影响业务", ticket.affected_services.join("、")],
      ["执行规模", `${ticket.plan_steps.length} 个可逆步骤 / ${ticket.change_window.duration_minutes} 分钟窗口`],
      ["环境快照", `v${ticket.environment_snapshot_version} / ${ticket.environment_snapshot_hash}`],
      ["不可变计划", ticket.plan_hash], ["生成模式", ticket.generator_mode],
    ];
    document.getElementById("change-ticket-facts").innerHTML = facts.map(([key, value]) => `<dt>${key}</dt><dd title="${escapeHtml(value)}">${escapeHtml(value)}</dd>`).join("");
    document.getElementById("change-plan").innerHTML = ticket.plan_steps.map((step, index) => `
      <div class="plan-step"><span>${index + 1}</span><div><strong>${escapeHtml(step.phase)} · ${escapeHtml(step.route_table_id)}</strong><small>${escapeHtml(step.destination)} · ${escapeHtml(step.from_next_hop)} → ${escapeHtml(step.to_next_hop)}</small></div><code>${escapeHtml(step.availability_zone)}</code></div>`).join("");
    document.getElementById("change-knowledge").innerHTML = ticket.knowledge_references.map(reference => `
      <div class="evidence-row"><span>K${reference.card_id}</span><div><strong>${escapeHtml(reference.title)}</strong><small>${escapeHtml(reference.status)} · 相关度 ${Number(reference.score).toFixed(2)} · ${escapeHtml(reference.evidence_locator)}</small></div></div>`).join("");
  }
  document.getElementById("change-validation-badge").textContent = validations.length ? `${passed}/${validations.length} PASS` : "等待校验";
  document.getElementById("change-validation-badge").className = `tag ${passed === validations.length && validations.length ? "APPROVED" : validations.length ? "REJECTED" : ""}`;
  document.getElementById("change-validations").innerHTML = validations.length ? validations.map(item => `
    <div class="validation-row"><span class="${escapeHtml(item.status)}">${escapeHtml(item.status)}</span><div><strong>${escapeHtml(item.validator)}</strong><small>${escapeHtml(item.message)} · ${escapeHtml(item.phase)}</small></div></div>`).join("") : `<p class="muted-copy">正在运行资源、路由、容量、窗口和知识状态检查。</p>`;

  const waiting = ticket?.status === "WAITING_APPROVAL" && executeRun?.status === "WAITING_APPROVAL";
  document.getElementById("change-approval-panel").hidden = !waiting;
  renderChangeResults(session);
}

function scheduleChangePoll() {
  clearTimeout(changePollTimer);
  const generateStatus = activeChangeSession?.runs?.generate?.status;
  const executeStatus = activeChangeSession?.runs?.execute?.status;
  const active = ["QUEUED", "RUNNING", "WAITING_APPROVAL"].includes(generateStatus)
    || ["QUEUED", "RUNNING"].includes(executeStatus);
  if (active) changePollTimer = setTimeout(pollChangeSession, 550);
}

async function maybeSubmitChangeExecution(session) {
  if (changeExecutionSubmitting || session.runs?.execute || session.package?.ticket?.status !== "READY_FOR_APPROVAL") return session;
  changeExecutionSubmitting = true;
  try {
    return await api(`/api/change-demos/${encodeURIComponent(session.session_id)}/execute`, {
      method: "POST",
      body: JSON.stringify({
        actor: document.getElementById("change-requester").value.trim() || "demo-operator",
        inject_failure: changeFailureMode,
      }),
    });
  } finally { changeExecutionSubmitting = false; }
}

async function pollChangeSession() {
  if (!activeChangeSession) return;
  try {
    const previousStatus = activeChangeSession.package?.ticket?.status;
    let session = await api(`/api/change-demos/${encodeURIComponent(activeChangeSession.session_id)}`);
    session = await maybeSubmitChangeExecution(session);
    renderChangeSession(session);
    const currentStatus = session.package?.ticket?.status;
    if (!["SUCCEEDED", "ROLLED_BACK", "FAILED", "REJECTED", "BLOCKED"].includes(previousStatus)
      && ["SUCCEEDED", "ROLLED_BACK", "FAILED", "REJECTED", "BLOCKED"].includes(currentStatus)) {
      toast(`变更已结束：${changeStatusLabels[currentStatus] || currentStatus}，可进入“变更结果”查看证据`);
    }
    scheduleChangePoll();
  } catch (error) { toast(error.message, true); }
}

async function loadLatestChangeSession() {
  const payload = await api("/api/change-demos/latest");
  let session = payload.session;
  if (session) session = await maybeSubmitChangeExecution(session);
  renderChangeSession(session);
  scheduleChangePoll();
}

function cardHtml(card, reviewMode = false) {
  const issues = (card.quality_issues || []).map(escapeHtml).join("；") || "通过基础质量检查";
  const versions = (card.applicable_versions || []).map(v => `<span class="tag">${escapeHtml(v)}</span>`).join("");
  const actions = reviewMode ? `
    <button class="button primary small" onclick="reviewCard(${card.id}, 'approve')">批准</button>
    <button class="button danger small" onclick="reviewCard(${card.id}, 'reject')">驳回</button>
    <button class="button secondary small" onclick="reviewCard(${card.id}, 'supersede')">替代旧版</button>` : "";
  return `<article class="knowledge-card">
    <div class="card-top">
      <div><h3>K${card.id} · ${escapeHtml(card.title || "无标题")}</h3><p>${escapeHtml(card.summary)}</p></div>
      <span class="quality ${Number(card.quality_score) < 65 ? "low" : ""}">${Number(card.quality_score).toFixed(0)}</span>
    </div>
    <div class="card-meta">
      <span class="tag ${escapeHtml(card.status)}">${statusLabels[card.status] || escapeHtml(card.status)}</span>
      <span class="tag">${escapeHtml(card.comparison_label)}</span>
      <span class="tag">${escapeHtml(card.object_name || card.knowledge_type)}</span>${versions}
    </div>
    <p title="${issues}">质量：${issues}</p>
    <div class="card-actions">
      <button class="button secondary small" onclick="showDetail(${card.id})">查看证据与详情</button>${actions}
    </div>
  </article>`;
}

async function refreshStats() {
  const data = await api("/api/stats");
  document.getElementById("metric-documents").textContent = data.documents;
  document.getElementById("metric-cards").textContent = data.cards;
  document.getElementById("metric-approved").textContent = data.statuses.APPROVED || 0;
  const pending = (data.statuses.PENDING_REVIEW || 0) + (data.statuses.DRAFT || 0);
  document.getElementById("metric-pending").textContent = pending;
  document.getElementById("review-count").textContent = pending;
  const max = Math.max(1, ...Object.values(data.statuses));
  document.getElementById("lifecycle-bars").innerHTML = Object.entries(data.statuses).map(([status, count]) => `
    <div class="bar-row"><span>${statusLabels[status] || status}</span>
      <div class="bar-track"><div class="bar-fill" style="width:${(count / max) * 100}%"></div></div>
      <strong>${count}</strong></div>`).join("");
}

async function refreshHealth() {
  const data = await api("/api/health");
  const configured = data.config.api_configured;
  document.getElementById("api-dot").classList.toggle("ok", configured);
  document.getElementById("api-status").textContent = configured ? "API 已配置" : "等待填写 API";
  document.getElementById("model-name").textContent = data.config.model;
}

async function loadRecent() {
  const data = await api("/api/cards?limit=5");
  const target = document.getElementById("recent-cards");
  target.classList.toggle("empty", !data.cards.length);
  target.innerHTML = data.cards.length ? data.cards.map(card => cardHtml(card)).join("") : "暂无知识卡片";
}

async function loadReviewQueue() {
  const [pending, drafts] = await Promise.all([
    api("/api/cards?status=PENDING_REVIEW&limit=200"),
    api("/api/cards?status=DRAFT&limit=200"),
  ]);
  const cards = [...pending.cards, ...drafts.cards];
  const target = document.getElementById("review-queue");
  target.classList.toggle("empty", !cards.length);
  target.innerHTML = cards.length ? cards.map(card => cardHtml(card, true)).join("") : "当前没有待审核知识";
}

async function loadLibrary() {
  const query = document.getElementById("library-query").value.trim();
  const status = document.getElementById("library-status").value;
  const data = query
    ? await api("/api/search", { method: "POST", body: JSON.stringify({ query, status, top_k: 50 }) })
    : await api(`/api/cards?status=${encodeURIComponent(status)}&limit=500`);
  const cards = query ? data.hits.map(hit => hit.card) : data.cards;
  const target = document.getElementById("library-cards");
  target.classList.toggle("empty", !cards.length);
  target.innerHTML = cards.length ? cards.map(card => cardHtml(card)).join("") : "没有匹配知识";
}

async function refreshAll() {
  try {
    await Promise.all([refreshHealth(), refreshStats(), loadRecent(), loadReviewQueue(), loadLibrary()]);
  } catch (error) {
    toast(error.message, true);
  }
}

window.showDetail = async function showDetail(id) {
  try {
    const card = await api(`/api/cards/${id}`);
    const field = (label, value) => `<dt>${label}</dt><dd>${Array.isArray(value) ? value.map(escapeHtml).join("\n") : escapeHtml(value)}</dd>`;
    document.getElementById("dialog-content").innerHTML = `
      <p class="eyebrow">KNOWLEDGE CARD K${card.id}</p><h2>${escapeHtml(card.title)}</h2>
      <div class="card-meta"><span class="tag ${card.status}">${statusLabels[card.status]}</span><span class="tag">质量 ${card.quality_score}</span><span class="tag">${escapeHtml(card.comparison_label)}</span></div>
      <dl class="detail-grid">
        ${field("摘要", card.summary)}${field("适用场景", card.scenario)}${field("对象", `${card.object_type} ${card.object_name}`)}
        ${field("适用版本", card.applicable_versions)}${field("前置条件", card.prerequisites)}${field("操作步骤", card.procedure_steps)}
        ${field("风险", card.risks)}${field("回退", card.rollback_steps)}${field("验证", card.validation_steps)}
        ${field("原文证据", card.evidence_quote)}${field("证据位置", card.evidence_locator)}${field("来源", card.source_ref)}
        ${field("比较判断", `${card.comparison_label} (${card.comparison_confidence})：${card.comparison_reason}`)}
        ${field("质量问题", card.quality_issues)}${field("审核", `${card.reviewer || "未审核"} ${card.review_comment || ""}`)}
      </dl>`;
    document.getElementById("detail-dialog").showModal();
  } catch (error) { toast(error.message, true); }
};

window.reviewCard = async function reviewCard(id, action) {
  const reviewer = document.getElementById("reviewer").value.trim();
  const comment = document.getElementById("review-comment").value.trim();
  if (!reviewer) return toast("请先填写审核人", true);
  let supersedesId = null;
  if (action === "supersede") {
    const raw = prompt("请输入要被替代的旧知识卡片 ID：");
    if (!raw) return;
    supersedesId = Number(raw);
  }
  try {
    await api(`/api/cards/${id}/review`, {
      method: "POST",
      body: JSON.stringify({ action, reviewer, comment, supersedes_id: supersedesId }),
    });
    toast(`K${id} 审核完成`);
    await refreshAll();
  } catch (error) { toast(error.message, true); }
};

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(item => item.classList.remove("active"));
  document.querySelectorAll(".page").forEach(page => page.classList.remove("active"));
  button.classList.add("active");
  const page = button.dataset.page;
  document.getElementById(`page-${page}`).classList.add("active");
  document.getElementById("page-title").textContent = pageTitles[page];
  if (["query", "results"].includes(page)) loadLatestChangeSession().catch(error => toast(error.message, true));
}));

document.getElementById("refresh-button").addEventListener("click", async () => {
  await refreshAll();
  if (document.getElementById("page-query").classList.contains("active")
    || document.getElementById("page-results").classList.contains("active")) {
    await loadLatestChangeSession();
  }
});
document.getElementById("library-search").addEventListener("click", () => loadLibrary().catch(error => toast(error.message, true)));
document.getElementById("dialog-close").addEventListener("click", () => document.getElementById("detail-dialog").close());

document.getElementById("change-generate").addEventListener("click", async event => {
  const button = event.currentTarget;
  changeFailureMode = document.getElementById("change-failure").value;
  setBusy(button, true, "正在生成……");
  try {
    const session = await api("/api/change-demos", {
      method: "POST",
      body: JSON.stringify({
        requested_by: document.getElementById("change-requester").value.trim() || "demo-operator",
        use_model: document.getElementById("change-use-model").checked,
        case_id: selectedChangeCaseId,
      }),
    });
    renderChangeSession(session);
    toast("已创建隔离变更演示，正在感知环境");
    scheduleChangePoll();
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

async function decideChange(decision, button) {
  if (!activeChangeSession) return;
  const actor = document.getElementById("change-approver").value.trim();
  if (!actor) return toast("请填写审批人", true);
  setBusy(button, true, decision === "APPROVED" ? "正在提交审批……" : "正在拒绝……");
  try {
    const session = await api(`/api/change-demos/${encodeURIComponent(activeChangeSession.session_id)}/decision`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        actor,
        comment: document.getElementById("change-approval-comment").value.trim(),
        confirmation: document.getElementById("change-confirmation").value,
      }),
    });
    renderChangeSession(session);
    toast(decision === "APPROVED" ? "审批已绑定，开始灰度执行" : "变更已拒绝，模拟网络保持不变");
    scheduleChangePoll();
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
}

document.getElementById("change-approve").addEventListener("click", event => decideChange("APPROVED", event.currentTarget));
document.getElementById("change-reject").addEventListener("click", event => decideChange("REJECTED", event.currentTarget));
document.getElementById("change-publish-feedback").addEventListener("click", async event => {
  if (!activeChangeSession) return;
  if (activeChangeSession.published_feedback) {
    document.querySelector('.nav-item[data-page="review"]').click();
    await refreshAll();
    await window.showDetail(activeChangeSession.published_feedback.knowledge_card_id);
    return;
  }
  const button = event.currentTarget;
  setBusy(button, true, "正在沉淀……");
  try {
    const session = await api(`/api/change-demos/${encodeURIComponent(activeChangeSession.session_id)}/publish-feedback`, {
      method: "POST",
      body: JSON.stringify({ actor: document.getElementById("change-approver").value.trim() || "demo-operator" }),
    });
    renderChangeSession(session);
    await refreshAll();
    toast(`执行经验已进入知识审核队列 K${session.published_feedback.knowledge_card_id}`);
  } catch (error) { toast(error.message, true); }
  finally {
    if (activeChangeSession?.published_feedback) {
      button.disabled = false;
      button.textContent = `查看审核队列中的 K${activeChangeSession.published_feedback.knowledge_card_id}`;
      button.dataset.originalText = button.textContent;
    } else {
      setBusy(button, false);
    }
  }
});

document.getElementById("upload-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  const files = [...document.getElementById("source-files").files];
  if (!files.length) return;
  const results = [];
  setBusy(button, true, `正在处理 1/${files.length}……`);
  try {
    for (let index = 0; index < files.length; index += 1) {
      button.textContent = `正在处理 ${index + 1}/${files.length}……`;
      document.getElementById("ingest-result").textContent =
        `正在上传并解析：${files[index].name}\n首次 OCR 可能需要下载模型，请稍候……`;
      const form = new FormData();
      form.append("file", files[index], files[index].name);
      const result = await api("/api/ingest-file", {
        method: "POST",
        body: form,
      });
      results.push(result);
      document.getElementById("ingest-result").textContent = JSON.stringify(results, null, 2);
    }
    toast(`已完成 ${results.length} 个文档的知识加工`);
    document.getElementById("source-files").value = "";
    await refreshAll();
  } catch (error) {
    document.getElementById("ingest-result").textContent =
      `${JSON.stringify(results, null, 2)}\n错误：${error.message}`;
    toast(error.message, true);
  } finally { setBusy(button, false); }
});

document.getElementById("ingest-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  setBusy(button, true, "正在抽取和比较……");
  try {
    const result = await api("/api/ingest-text", {
      method: "POST",
      body: JSON.stringify({
        source_name: document.getElementById("source-name").value,
        source_ref: document.getElementById("source-ref").value,
        content: document.getElementById("source-content").value,
      }),
    });
    document.getElementById("ingest-result").textContent = JSON.stringify(result, null, 2);
    toast("知识加工完成，请进入审核队列");
    await refreshAll();
  } catch (error) {
    document.getElementById("ingest-result").textContent = `错误：${error.message}`;
    toast(error.message, true);
  } finally { setBusy(button, false); }
});

document.getElementById("query-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  const agentMode = button?.dataset?.mode === "agent";
  setBusy(button, true, "正在检索并生成……");
  try {
    const result = await api(agentMode ? "/api/agent-query" : "/api/query", {
      method: "POST",
      body: JSON.stringify({ question: document.getElementById("query-question").value }),
    });
    const answer = document.getElementById("answer-content");
    answer.classList.remove("empty");
    answer.textContent = result.answer;
    const agentMeta = document.getElementById("answer-agent-meta");
    agentMeta.textContent = result.agent
      ? `只读 Agent：${result.agent.steps}/${result.agent.max_steps} 步，${result.agent.tool_calls.length} 次工具调用，候选 K${result.agent.selected_card_ids.join(", K") || "无"}`
      : "直接检索模式";
    document.getElementById("answer-sources").innerHTML = (result.sources || []).map(source => `
      <div class="source-item"><strong>[K${source.card_id}] ${escapeHtml(source.title)}</strong>
      <p>${escapeHtml(source.evidence_locator)} · ${escapeHtml(source.source_ref)}</p>
      <p>“${escapeHtml(source.evidence_quote)}”</p></div>`).join("");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

renderChangeStages(null);
refreshAll();
loadChangeCases().catch(error => toast(error.message, true));
loadLatestChangeSession().catch(error => toast(error.message, true));
