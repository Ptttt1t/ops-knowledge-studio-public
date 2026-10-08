const pageTitles = {
  dashboard: "工作台概览",
  "real-change": "变更草案",
  ingest: "知识采集与加工",
  review: "知识审核队列",
  "semantic-governance": "相似知识治理",
  library: "知识资产库",
  graph: "知识关系图",
};

let realChangeRecommendations = [];
let activeRealChangeDraft = null;
let realChangePollTimer = null;
let activeRealChangeEvaluation = null;
let realChangeEvaluationPollTimer = null;
let semanticGovernancePollTimer = null;
let knowledgeGraphData = { nodes: [], edges: [], meta: {} };
let knowledgeGraphSelectedId = "";
let knowledgeGraphFrame = null;
let knowledgeGraphTransform = { yaw: -0.42, pitch: -0.2, scale: 1, panX: 0, panY: 0 };
let knowledgeGraphProjection = [];
let knowledgeGraphHoveredId = "";
let knowledgeGraphInteraction = null;
let knowledgeGraphResizeObserver = null;
let platformConfig = {};
let safeShutdownRequested = false;
let safeShutdownPollTimer = null;
const accessTokenKey = "ops-knowledge-studio-access-token";
let accessToken = sessionStorage.getItem(accessTokenKey) || "";

const statusLabels = {
  DRAFT: "草稿",
  PENDING_REVIEW: "待审核",
  APPROVED: "已批准",
  REJECTED: "已驳回",
  SUPERSEDED: "已替代",
  PARTIAL: "部分已审核",
  EMPTY: "空包",
};

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function api(path, options = {}) {
  const headers = options.body instanceof FormData
    ? { ...(options.headers || {}) }
    : { "Content-Type": "application/json", ...(options.headers || {}) };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  const response = await fetch(path, {
    ...options,
    headers,
  });
  const payload = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (response.status === 401) showAuthGate(payload.error || "访问令牌无效");
  if (!response.ok) throw new Error(payload.error || payload.detail || `HTTP ${response.status}`);
  return payload;
}

function showAuthGate(message = "") {
  document.getElementById("auth-gate").hidden = false;
  document.getElementById("auth-error").textContent = message;
  document.getElementById("access-token").focus();
}

function hideAuthGate() {
  document.getElementById("auth-gate").hidden = true;
  document.getElementById("auth-error").textContent = "";
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

function renderSafeShutdown(status = {}) {
  const overlay = document.getElementById("safe-shutdown-overlay");
  const activeModels = Number(status.active_model_requests || 0);
  const activeWork = Number(status.active_work_requests || 0);
  const workers = Number(status.runtime_workers || 0)
    + (status.semantic_worker_running ? 1 : 0);
  overlay.hidden = false;
  document.getElementById("safe-shutdown-title").textContent = "正在安全退出";
  document.getElementById("safe-shutdown-message").textContent =
    `已停止接收新任务；等待模型请求 ${activeModels}、在途操作 ${activeWork}、后台 Worker ${workers} 排空。请勿关闭终端或强制按 Ctrl+C。`;
}

function renderSafeShutdownComplete() {
  clearTimeout(safeShutdownPollTimer);
  const overlay = document.getElementById("safe-shutdown-overlay");
  overlay.hidden = false;
  overlay.classList.add("complete");
  document.getElementById("safe-shutdown-title").textContent = "平台已安全退出";
  document.getElementById("safe-shutdown-message").textContent =
    "模型请求与后台任务已经排空，服务连接已关闭。现在可以关闭此页面。";
}

async function pollSafeShutdown() {
  clearTimeout(safeShutdownPollTimer);
  try {
    const status = await api("/api/admin/safe-shutdown");
    renderSafeShutdown(status);
    safeShutdownPollTimer = window.setTimeout(pollSafeShutdown, 400);
  } catch (_error) {
    if (safeShutdownRequested) renderSafeShutdownComplete();
  }
}

function cardHtml(card, reviewMode = false) {
  const issues = (card.quality_issues || []).map(escapeHtml).join("；") || "通过基础质量检查";
  const versions = (card.applicable_versions || []).map(v => `<span class="tag">${escapeHtml(v)}</span>`).join("");
  const actions = reviewMode ? `
    <button class="button primary small" onclick="reviewCard(${card.id}, 'approve')">批准</button>
    <button class="button danger small" onclick="reviewCard(${card.id}, 'reject')">驳回</button>
    <button class="button secondary small" onclick="reviewCard(${card.id}, 'supersede')">替代旧版</button>` : "";
  const deleteAction = `<button class="button danger small" onclick="deleteCard(${card.id})">删除</button>`;
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
      <button class="button secondary small" onclick="showDetail(${card.id})">查看证据与详情</button>${actions}${deleteAction}
    </div>
  </article>`;
}

function caseBundleHtml(bundle, reviewMode = false) {
  const counts = Object.entries(bundle.status_counts || {})
    .map(([status, count]) => `${statusLabels[status] || escapeHtml(status)} ${count}`)
    .join(" · ") || "暂无子卡";
  const roles = (bundle.roles || [])
    .map(role => `<span class="tag">${escapeHtml(role)}</span>`)
    .join("");
  const total = Number(bundle.card_count);
  const reviewable = Number(bundle.reviewable_count);
  const approved = Number((bundle.status_counts || {}).APPROVED || 0);
  const rejected = Number((bundle.status_counts || {}).REJECTED || 0);
  const canApprove = reviewMode && reviewable > 0 && reviewable + approved === total;
  const canReject = reviewMode && reviewable > 0 && reviewable + rejected === total;
  const actions = `${canApprove ? `
    <button class="button primary small" data-bundle-action="approve" data-case-id="${escapeHtml(bundle.case_id)}">整包批准</button>` : ""}${canReject ? `
    <button class="button danger small" data-bundle-action="reject" data-case-id="${escapeHtml(bundle.case_id)}">整包驳回</button>` : ""}`;
  const rebuildAction = platformConfig.demo_management?.rebuild_enabled ? `
    <button class="button warning small" data-bundle-action="rebuild" data-case-id="${escapeHtml(bundle.case_id)}">重建当前案例</button>` : "";
  return `<article class="case-bundle">
    <div class="bundle-heading">
      <div><p class="eyebrow">CHANGE CASE BUNDLE</p><h3>${escapeHtml(bundle.title || bundle.source_name)}</h3></div>
      <span class="bundle-count">${Number(bundle.card_count)}<small>张原子卡</small></span>
    </div>
    <div class="card-meta">
      <span class="tag ${escapeHtml(bundle.status)}">${statusLabels[bundle.status] || escapeHtml(bundle.status)}</span>
      <span class="tag">${escapeHtml(bundle.extraction_strategy)}</span>${roles}
    </div>
    <p>${escapeHtml(bundle.source_name)} · ${escapeHtml(counts)}</p>
    <details><summary>查看包结构与来源标识</summary>
      <p class="bundle-identity">${escapeHtml(bundle.case_id)}<br>${escapeHtml(bundle.source_ref)}</p>
    </details>
    <div class="card-actions">
      <button class="button secondary small" data-bundle-action="detail" data-case-id="${escapeHtml(bundle.case_id)}">展开整包</button>${actions}${rebuildAction}
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
      <progress class="bar-track" value="${count}" max="${max}" aria-label="${escapeHtml(statusLabels[status] || status)}"></progress>
      <strong>${count}</strong></div>`).join("");
}

async function refreshHealth() {
  const [data, memory] = await Promise.all([
    api("/api/health"),
    api("/api/memory/status?probe=true"),
  ]);
  const configured = data.config.api_configured;
  platformConfig = data.config || {};
  const resetEnabled = Boolean(platformConfig.demo_management?.full_reset_enabled);
  document.getElementById("clear-knowledge-button").disabled = !resetEnabled;
  document.getElementById("knowledge-reset-note").textContent = resetEnabled
    ? "清空前自动备份；保留上传原文件和模型配置，便于重新导入对比。"
    : "实验环境需在 .env 设置 DEMO_MODE=true、DEMO_FULL_RESET_ENABLED=true，并重启服务。";
  const shutdownButton = document.getElementById("safe-shutdown-button");
  shutdownButton.hidden = !platformConfig.demo_management?.safe_shutdown_enabled;
  document.getElementById("api-dot").classList.toggle("ok", configured);
  document.getElementById("api-status").textContent = configured ? "API 已配置" : "等待填写 API";
  document.getElementById("model-name").textContent = data.config.model;
  document.getElementById("forget-token").hidden = !data.config.access_token_required;
  const healthy = memory.health === "OK";
  const badge = document.getElementById("memory-health-badge");
  badge.textContent = healthy ? "服务可用" : memory.health;
  badge.className = `tag ${healthy ? "APPROVED" : "REJECTED"}`;
  document.getElementById("memory-config").textContent = memory.configured ? "Vanilla 已启用" : memory.enabled ? "等待 API Key" : "未启用";
  document.getElementById("memory-synced-cards").textContent = memory.stats?.statuses?.SUCCEEDED || 0;
  document.getElementById("memory-links").textContent = memory.stats?.memory_links || 0;
  document.getElementById("memory-policy").textContent = "仅本地 APPROVED";
  document.getElementById("memory-sync").disabled = !memory.configured;
}

async function loadRecent() {
  const data = await api("/api/cards?limit=5");
  const target = document.getElementById("recent-cards");
  target.classList.toggle("empty", !data.cards.length);
  target.innerHTML = data.cards.length ? data.cards.map(card => cardHtml(card)).join("") : "暂无知识卡片";
}

async function loadReviewQueue() {
  const [pending, drafts, bundleData] = await Promise.all([
    api("/api/cards?status=PENDING_REVIEW&limit=200"),
    api("/api/cards?status=DRAFT&limit=200"),
    api("/api/knowledge-case-bundles?limit=200"),
  ]);
  const bundles = (bundleData.case_bundles || [])
    .filter(bundle => Number(bundle.reviewable_count) > 0);
  const bundledIds = new Set(bundles.flatMap(bundle => bundle.card_ids || []).map(Number));
  const cards = [...pending.cards, ...drafts.cards]
    .filter(card => !bundledIds.has(Number(card.id)));
  const target = document.getElementById("review-queue");
  const content = [
    ...bundles.map(bundle => caseBundleHtml(bundle, true)),
    ...cards.map(card => cardHtml(card, true)),
  ];
  target.classList.toggle("empty", !content.length);
  target.innerHTML = content.length ? content.join("") : "当前没有待审核知识";
}

const semanticProposalStatusLabels = {
  PENDING_REVIEW: "待确认",
  ACCEPTED: "已合并",
  REJECTED: "已排除",
  DEFERRED: "暂缓",
  STALE: "已失效",
  SUPERSEDED: "已窄化替代",
};

function semanticCardComparisonHtml(card, side, removable = false) {
  if (!card) return `<div class="semantic-source-card missing">来源卡片已不存在</div>`;
  const operation = card.generalized_operation || card.procedure_steps?.join("\n") || "";
  const validation = card.validation || card.validation_steps?.join("\n") || "";
  return `<div class="semantic-source-card">
    <div class="semantic-source-head"><span>${side}</span><strong>K${card.id} · ${escapeHtml(card.title)}</strong></div>
    <p>${escapeHtml(card.summary || "无摘要")}</p>
    <dl>
      <dt>案例包</dt><dd>${escapeHtml(card.case_id || "—")}${card.is_representative ? " · 簇代表" : ""}</dd>
      <dt>通用化操作</dt><dd>${escapeHtml(operation || "—")}</dd>
      <dt>验证</dt><dd>${escapeHtml(validation || "—")}</dd>
      <dt>适用阶段</dt><dd>${escapeHtml((card.applicable_phases || []).join("、") || "—")}</dd>
      <dt>来源</dt><dd>${escapeHtml(card.source_name || card.source_ref || "—")}</dd>
    </dl>
    <button class="button small secondary" type="button" data-open-card="${card.id}">查看原卡</button>
    ${removable ? `<button class="button small danger" type="button" data-semantic-remove-member="${card.id}">从本次簇移除</button>` : ""}
  </div>`;
}

function semanticDifferencesHtml(proposal) {
  const differences = proposal.member_differences || {};
  const labels = {
    title: "标题", generalized_operation: "通用化操作", validation: "验证",
    rollback: "回退", impact_analysis: "影响分析", object: "操作对象",
  };
  const entries = Object.entries(differences);
  if (!entries.length) return `<p class="semantic-difference-empty">成员关键字段未发现结构化差异。</p>`;
  return `<details class="semantic-differences"><summary>查看成员差异（${entries.length} 类）</summary>
    ${entries.map(([field, values]) => `<div><strong>${escapeHtml(labels[field] || field)}</strong><ul>${(values || []).map(item =>
      `<li><span>K${item.card_id} · ${escapeHtml(item.case_id || "—")}</span>${escapeHtml(item.value || "—")}</li>`).join("")}</ul></div>`).join("")}
  </details>`;
}

function semanticProposalHtml(proposal) {
  const merge = proposal.proposed_merge || {};
  const editable = ["PENDING_REVIEW", "DEFERRED"].includes(proposal.status);
  const signals = proposal.signals || {};
  const shared = (signals.shared_core_tokens || []).slice(0, 12).join("、") || "无稳定共享锚点";
  const canonical = proposal.canonical_card;
  const isCluster = proposal.proposal_type === "CLUSTER";
  const members = isCluster ? (proposal.member_cards || []) : [proposal.left_card, proposal.right_card].filter(Boolean);
  const memberCount = Number(proposal.member_count || members.length);
  const caseCount = Number(proposal.case_count || memberCount);
  const recommended = proposal.recommended_merge_mode || "FULL_EQUIVALENT";
  const editor = editable ? `<div class="semantic-merge-editor">
    ${isCluster ? `<div class="semantic-mode-guidance"><strong>推荐：${recommended === "FULL_EQUIVALENT" ? "完整等价合并" : "建立共享核心"}</strong><span>完整合并适用于整张卡语义等价；共享核心保留各案例差异证据。</span></div>` : ""}
    <div class="semantic-editor-grid">
      <label>canonical 标题<input data-merge-field="title" value="${escapeHtml(merge.title || "")}"></label>
      <label>适用阶段<input data-merge-field="applicable_phases" value="${escapeHtml((merge.applicable_phases || []).join("、"))}"></label>
      <label class="span-2">摘要<textarea rows="2" data-merge-field="summary">${escapeHtml(merge.summary || "")}</textarea></label>
      <label class="span-2">通用化操作<textarea rows="4" data-merge-field="generalized_operation">${escapeHtml(merge.generalized_operation || "")}</textarea></label>
      <label>验证<textarea rows="3" data-merge-field="validation">${escapeHtml(merge.validation || "")}</textarea></label>
      <label>回退<textarea rows="3" data-merge-field="rollback">${escapeHtml(merge.rollback || "")}</textarea></label>
      <label class="span-2">影响分析<textarea rows="2" data-merge-field="impact_analysis">${escapeHtml(merge.impact_analysis || "")}</textarea></label>
      <label class="span-2">人工意见<input data-merge-comment placeholder="说明合并或排除依据"></label>
    </div>
    <div class="semantic-proposal-actions">
      ${isCluster ? `<button class="button primary" type="button" data-semantic-action="ACCEPT" data-merge-mode="SHARED_CORE">建立共享核心</button>
      <button class="button secondary" type="button" data-semantic-action="ACCEPT" data-merge-mode="FULL_EQUIVALENT">确认完全等价合并</button>`
      : `<button class="button primary" type="button" data-semantic-action="ACCEPT" data-merge-mode="FULL_EQUIVALENT">确认合并</button>`}
      <button class="button danger" type="button" data-semantic-action="REJECT">不是同一知识</button>
      <button class="button secondary" type="button" data-semantic-action="DEFER">暂缓</button>
    </div>
  </div>` : canonical ? `<div class="semantic-canonical-result">
    <strong>已生成 canonical 卡 K${canonical.id}：${escapeHtml(canonical.title)}</strong>
    <button class="button small secondary" type="button" data-open-card="${canonical.id}">查看 canonical 卡</button>
  </div>` : `<p class="muted-copy">该候选已结束，不再改变检索状态。</p>`;
  return `<article class="panel semantic-proposal" data-proposal-id="${escapeHtml(proposal.proposal_id)}" data-proposal-type="${escapeHtml(proposal.proposal_type || "PAIR")}">
    <div class="semantic-proposal-head">
      <div><p class="eyebrow">${escapeHtml(proposal.proposal_id)}</p><h2>${semanticProposalStatusLabels[proposal.status] || escapeHtml(proposal.status)}${isCluster ? ` · ${memberCount} 张卡 / ${caseCount} 个案例` : ""}</h2></div>
      <div class="semantic-score"><strong>${Math.round(Number(proposal.similarity_score || 0) * 100)}%</strong><small>${isCluster ? "簇平均相似度" : "候选相似度"}</small></div>
    </div>
    <p class="semantic-reason">${escapeHtml(proposal.reason || "")}</p>
    <div class="semantic-signal-strip"><span>共享锚点：${escapeHtml(shared)}</span>${isCluster
      ? `<span>最低 ${Math.round(Number(proposal.minimum_score || 0) * 100)}%</span><span>平均 ${Math.round(Number(proposal.average_score || 0) * 100)}%</span><span>最高 ${Math.round(Number(proposal.maximum_score || 0) * 100)}%</span><span>complete-link</span>`
      : `<span>包含度 ${escapeHtml(signals.token_containment ?? "—")}</span><span>文本顺序 ${escapeHtml(signals.sequence_similarity ?? "—")}</span>`}</div>
    <div class="semantic-source-grid">
      ${members.map((card, index) => semanticCardComparisonHtml(
        card,
        card?.is_representative ? "簇代表" : `成员 ${index + 1}`,
        editable && isCluster && memberCount > 2,
      )).join("")}
    </div>
    ${isCluster ? semanticDifferencesHtml(proposal) : ""}
    ${editor}
  </article>`;
}

function renderSemanticGovernanceSummary(data) {
  const counts = data.counts || {};
  document.getElementById("semantic-proposal-count").textContent = counts.PENDING_REVIEW || 0;
  document.getElementById("semantic-pending-count").textContent = counts.PENDING_REVIEW || 0;
  document.getElementById("semantic-accepted-count").textContent = counts.ACCEPTED || 0;
  document.getElementById("semantic-rejected-count").textContent = counts.REJECTED || 0;
  const lastScan = data.last_scan || {};
  document.getElementById("semantic-last-scan").textContent = lastScan.completed_at
    ? new Date(lastScan.completed_at).toLocaleString("zh-CN", { hour12: false }) : "—";
  document.getElementById("semantic-last-scan-detail").textContent = lastScan.status
    ? `${lastScan.status} · ${lastScan.created_proposal_count || 0} 个新增候选` : "尚未运行";
  const enabled = Boolean(data.enabled);
  const badge = document.getElementById("semantic-governance-badge");
  badge.textContent = enabled ? (data.worker_running ? "后台扫描中" : "已启用") : "默认关闭";
  badge.className = `synthetic-stamp ${enabled ? "" : "disabled"}`;
  document.getElementById("semantic-scan-now").disabled = !enabled;
  document.getElementById("semantic-governance-policy").textContent = enabled
    ? `本地算法 ${data.policy?.algorithm_version || "—"} · 阈值 ${data.policy?.minimum_score ?? "—"} · 不自动合并 · 不向模型外发正文`
    : "请设置 SEMANTIC_GOVERNANCE_ENABLED=true 并重启服务；已有候选仍可继续人工处理。";
}

async function loadSemanticGovernanceSummary() {
  const data = await api("/api/semantic-governance");
  renderSemanticGovernanceSummary(data);
  return data;
}

async function loadSemanticGovernance() {
  const status = document.getElementById("semantic-proposal-status").value;
  const data = await api(`/api/semantic-merge-proposals?status=${encodeURIComponent(status)}&limit=100`);
  renderSemanticGovernanceSummary(data);
  const target = document.getElementById("semantic-proposal-list");
  target.classList.toggle("empty", !data.proposals.length);
  target.innerHTML = data.proposals.length
    ? data.proposals.map(semanticProposalHtml).join("")
    : "当前筛选状态下没有相似知识候选";
}

async function loadLibrary() {
  const query = document.getElementById("library-query").value.trim();
  const status = document.getElementById("library-status").value;
  const [data, bundleData] = await Promise.all([
    query
      ? api("/api/search", { method: "POST", body: JSON.stringify({ query, status, top_k: 50 }) })
      : api(`/api/cards?status=${encodeURIComponent(status)}&limit=500`),
    api("/api/knowledge-case-bundles?limit=500"),
  ]);
  const cards = query ? data.hits.map(hit => hit.card) : data.cards;
  const visibleCardIds = new Set(cards.map(card => Number(card.id)));
  const bundles = (bundleData.case_bundles || []).filter(bundle => {
    const bundleIds = (bundle.card_ids || []).map(Number);
    return query
      ? bundleIds.some(cardId => visibleCardIds.has(cardId))
      : Number((bundle.status_counts || {})[status] || 0) > 0;
  });
  const bundledIds = new Set(bundles.flatMap(bundle => bundle.card_ids || []).map(Number));
  const standaloneCards = cards.filter(card => !bundledIds.has(Number(card.id)));
  const target = document.getElementById("library-cards");
  const content = [
    ...bundles.map(bundle => caseBundleHtml(bundle)),
    ...standaloneCards.map(card => cardHtml(card)),
  ];
  target.classList.toggle("empty", !content.length);
  target.innerHTML = content.length ? content.join("") : "没有匹配知识";
}

const graphRelationLabels = {
  SOURCE_OF: "来源生成",
  DESCRIBES: "描述对象",
  CONTAINS: "案例包含",
  DUPLICATE_OF: "重复于",
  CONFLICTS_WITH: "冲突于",
  CANDIDATE_VERSION_OF: "候选新版本",
  SUPERSEDES: "替代",
  RELATED_TO: "相关于",
  VALIDATES: "验证",
  ROLLBACK_OF: "回退对应",
  CHILD_OF: "章节属于",
  SIMILAR_TO: "待确认相似",
  CROSS_PHASE_SIMILAR: "跨阶段相似",
  MERGED_INTO: "已合并到",
  INSTANCE_OF: "案例实例属于",
};

const graphClusterLabels = {
  CASE_BUNDLE: "案例包",
  CASE_CONTEXT: "案例上下文",
  PRECHECK: "变更前检查",
  IMPLEMENTATION: "变更实施",
  VALIDATION: "变更验证",
  ROLLBACK: "变更回退",
  EXECUTION_OUTCOME: "执行结果",
  PROCEDURE_OTHER: "其他步骤",
  OTHER_CARD: "其他知识卡",
  BUSINESS_OBJECT: "业务对象",
  SOURCE_DOCUMENT: "来源文档",
};

function graphHash(value) {
  let hash = 2166136261;
  for (const character of String(value)) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function graphNodeRadius(node) {
  return { case: 15, card: 10, object: 12, source: 9 }[node.kind] || 9;
}

function graphShortLabel(value, maxLength = 8) {
  const text = String(value || "未命名");
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}…` : text;
}

function graphCompactLabel(node) {
  if (node.kind === "card") return `K${node.entity_id}`;
  if (node.kind === "case") return "案例包";
  if (node.kind === "source") return "来源";
  if (node.kind === "object") return "对象";
  return graphShortLabel(node.label, 7);
}

function resetKnowledgeGraphTransform() {
  knowledgeGraphTransform = { yaw: -0.42, pitch: -0.2, scale: 1, panX: 0, panY: 0 };
  drawKnowledgeGraph();
}

function zoomKnowledgeGraph(factor) {
  knowledgeGraphTransform.scale = Math.min(2.4, Math.max(0.55, knowledgeGraphTransform.scale * factor));
  drawKnowledgeGraph();
}

function rotateKnowledgeGraph(deltaYaw) {
  knowledgeGraphTransform.yaw += deltaYaw;
  drawKnowledgeGraph();
}

function graphNodeSearchText(node) {
  return [
    node.label, node.summary, node.status, node.knowledge_type,
    node.object_type, node.object_name, node.source_type, node.source_ref,
    node.unit_role, node.procedure_group, node.cluster, node.entity_id,
  ].filter(Boolean).join(" ").toLocaleLowerCase("zh-CN");
}

function knowledgeGraphFocus() {
  const query = document.getElementById("graph-query")?.value.trim().toLocaleLowerCase("zh-CN") || "";
  const matched = new Set(
    knowledgeGraphData.nodes
      .filter(node => query && graphNodeSearchText(node).includes(query))
      .map(node => node.id)
  );
  const focusIds = knowledgeGraphSelectedId
    ? new Set([knowledgeGraphSelectedId])
    : matched;
  const neighborIds = new Set(focusIds);
  knowledgeGraphData.edges.forEach(edge => {
    if (focusIds.has(edge.source)) neighborIds.add(edge.target);
    if (focusIds.has(edge.target)) neighborIds.add(edge.source);
  });
  return { matched, focusIds, neighborIds, hasFocus: focusIds.size > 0 };
}

function applyKnowledgeGraphEmphasis() {
  drawKnowledgeGraph();
}

function renderKnowledgeGraphDetail(node) {
  knowledgeGraphSelectedId = node?.id || "";
  const title = document.getElementById("graph-detail-title");
  const summary = document.getElementById("graph-detail-summary");
  const facts = document.getElementById("graph-detail-facts");
  const neighbors = document.getElementById("graph-neighbors");
  const openButton = document.getElementById("graph-open-card");
  facts.replaceChildren();
  neighbors.replaceChildren();
  openButton.hidden = true;
  openButton.dataset.cardId = "";
  if (!node) {
    title.textContent = "选择一个节点";
    summary.textContent = "点击图中节点，可查看它的治理状态、来源以及与周边知识的真实关系。";
    applyKnowledgeGraphEmphasis();
    return;
  }

  title.textContent = node.label;
  summary.textContent = node.summary || ({
    case: "结构化变更单案例包",
    object: "知识卡描述的业务对象",
    source: "知识卡保留的来源文档",
  }[node.kind] || "治理型知识节点");
  const addFact = (label, value) => {
    if (value === undefined || value === null || value === "") return;
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = label;
    detail.textContent = String(value);
    facts.append(term, detail);
  };
  addFact("节点类型", { card: "知识卡", case: "案例包", object: "业务对象", source: "来源文档" }[node.kind]);
  addFact("空间簇", graphClusterLabels[node.cluster] || node.cluster);
  if (node.kind === "card") {
    addFact("卡片编号", `K${node.entity_id}`);
    addFact("治理状态", statusLabels[node.status] || node.status);
    addFact("知识类型", node.knowledge_type);
    addFact("质量评分", node.quality_score);
    addFact("结构角色", node.unit_role);
    addFact("比较判断", node.comparison_label);
    openButton.hidden = false;
    openButton.dataset.cardId = String(node.entity_id);
  } else if (node.kind === "case") {
    addFact("案例标识", node.entity_id);
  } else if (node.kind === "object") {
    addFact("对象类型", node.object_type);
    addFact("对象名称", node.label);
  } else if (node.kind === "source") {
    addFact("来源类型", node.source_type);
    addFact("来源标识", node.source_ref);
  }

  const nodeById = new Map(knowledgeGraphData.nodes.map(item => [item.id, item]));
  const connected = knowledgeGraphData.edges
    .filter(edge => edge.source === node.id || edge.target === node.id)
    .map(edge => ({
      edge,
      other: nodeById.get(edge.source === node.id ? edge.target : edge.source),
    }))
    .filter(item => item.other);
  if (connected.length) {
    const heading = document.createElement("h3");
    heading.textContent = `直接关系（${connected.length}）`;
    neighbors.append(heading);
    connected.slice(0, 30).forEach(({ edge, other }) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "graph-neighbor";
      const relation = graphRelationLabels[edge.relation_type] || edge.relation_type;
      button.textContent = `${relation} · ${other.label}`;
      button.addEventListener("click", () => renderKnowledgeGraphDetail(other));
      neighbors.append(button);
    });
  }
  drawKnowledgeGraph();
}

function resizeKnowledgeGraphCanvas() {
  const canvas = document.getElementById("knowledge-graph-canvas");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const ratio = Math.min(2, window.devicePixelRatio || 1);
  const width = Math.round(rect.width * ratio);
  const height = Math.round(rect.height * ratio);
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
    drawKnowledgeGraph();
  }
}

function projectKnowledgeGraphNode(node, width, height) {
  const { yaw, pitch, scale, panX, panY } = knowledgeGraphTransform;
  const cosYaw = Math.cos(yaw);
  const sinYaw = Math.sin(yaw);
  const cosPitch = Math.cos(pitch);
  const sinPitch = Math.sin(pitch);
  const rotatedX = node.x * cosYaw - node.z * sinYaw;
  const yawZ = node.x * sinYaw + node.z * cosYaw;
  const rotatedY = node.y * cosPitch - yawZ * sinPitch;
  const depth = node.y * sinPitch + yawZ * cosPitch;
  const cameraDistance = 900;
  const perspective = Math.min(1.65, Math.max(0.55, cameraDistance / (cameraDistance + depth)));
  return {
    node,
    x: width / 2 + panX + rotatedX * perspective * scale,
    y: height / 2 + panY + rotatedY * perspective * scale,
    depth,
    perspective,
    radius: graphNodeRadius(node) * perspective * Math.sqrt(scale),
  };
}

function graphNodeColors(node) {
  const palette = {
    case: { fill: "#1f6b4f", stroke: "#174f3a" },
    card: { fill: "#fffef9", stroke: "#1f6b4f" },
    object: { fill: "#f4ead2", stroke: "#b88924" },
    source: { fill: "#e7f0f4", stroke: "#39738d" },
  };
  const cardClusters = {
    CASE_CONTEXT: { fill: "#d9f0ea", stroke: "#277a68" },
    PRECHECK: { fill: "#e4eef8", stroke: "#3b6f9f" },
    IMPLEMENTATION: { fill: "#dff0df", stroke: "#3e7d4b" },
    VALIDATION: { fill: "#eee7f8", stroke: "#70529b" },
    ROLLBACK: { fill: "#f8e6dd", stroke: "#b35f3f" },
    EXECUTION_OUTCOME: { fill: "#eceff0", stroke: "#65747a" },
    PROCEDURE_OTHER: { fill: "#f7f0d9", stroke: "#9a782e" },
  };
  const colors = node.kind === "card" && cardClusters[node.cluster]
    ? cardClusters[node.cluster]
    : palette[node.kind] || palette.card;
  if (["PENDING_REVIEW", "DRAFT"].includes(node.status)) return { ...colors, stroke: "#b88924" };
  if (node.status === "REJECTED") return { ...colors, stroke: "#aa3d32" };
  return colors;
}

function graphEdgeColor(edge, active) {
  if (active) return "#1f6b4f";
  if (edge.relation_type === "MERGED_INTO") return "#1f6b4f";
  if (edge.relation_type === "INSTANCE_OF") return "#2f7b78";
  if (edge.relation_type === "CROSS_PHASE_SIMILAR") return "#70529b";
  if (edge.explicit) return "#b88924";
  return "#aeb8b1";
}

function drawKnowledgeGraphNode(ctx, projected, state) {
  const { node, x, y, radius, perspective } = projected;
  const colors = graphNodeColors(node);
  const selected = node.id === knowledgeGraphSelectedId;
  const hovered = node.id === knowledgeGraphHoveredId;
  const matched = state.matched.has(node.id);
  const dimmed = state.hasFocus && !state.neighborIds.has(node.id);
  const opacity = dimmed ? 0.12 : Math.min(1, 0.56 + perspective * 0.35);

  ctx.save();
  ctx.globalAlpha = opacity;
  ctx.translate(x, y);
  ctx.fillStyle = colors.fill;
  ctx.strokeStyle = colors.stroke;
  ctx.lineWidth = selected ? 4 : hovered || matched ? 3 : 2;
  if (selected || hovered) {
    ctx.shadowColor = "rgba(31, 107, 79, .28)";
    ctx.shadowBlur = 12;
  }
  ctx.beginPath();
  if (node.kind === "case") {
    const size = radius * 1.55;
    ctx.roundRect(-size, -size, size * 2, size * 2, Math.max(2, radius * 0.25));
  } else if (node.kind === "object") {
    ctx.moveTo(0, -radius * 1.25);
    ctx.lineTo(radius * 1.25, 0);
    ctx.lineTo(0, radius * 1.25);
    ctx.lineTo(-radius * 1.25, 0);
    ctx.closePath();
  } else if (node.kind === "source") {
    ctx.rect(-radius, -radius, radius * 2, radius * 2);
  } else {
    ctx.arc(0, 0, radius, 0, Math.PI * 2);
  }
  ctx.fill();
  ctx.stroke();
  ctx.shadowBlur = 0;

  const showPersistentLabel = ["card", "case"].includes(node.kind);
  const compactLabel = showPersistentLabel || selected || hovered || matched ? graphCompactLabel(node) : "";
  if (compactLabel) {
    ctx.font = `${node.kind === "card" ? 10 : 9}px "Segoe UI", "Microsoft YaHei", sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(251, 252, 248, .96)";
    ctx.fillStyle = "#1f2923";
    const labelY = radius + 7;
    ctx.strokeText(compactLabel, 0, labelY);
    ctx.fillText(compactLabel, 0, labelY);
  }
  ctx.restore();
}

function drawKnowledgeGraphArrow(ctx, source, target, color, opacity) {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.hypot(dx, dy);
  if (distance < 4) return;
  const unitX = dx / distance;
  const unitY = dy / distance;
  const endX = target.x - unitX * (target.radius + 3);
  const endY = target.y - unitY * (target.radius + 3);
  const size = 4.5;
  ctx.save();
  ctx.globalAlpha = opacity;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(endX, endY);
  ctx.lineTo(endX - unitX * size - unitY * size * 0.7, endY - unitY * size + unitX * size * 0.7);
  ctx.lineTo(endX - unitX * size + unitY * size * 0.7, endY - unitY * size - unitX * size * 0.7);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

function drawKnowledgeGraph() {
  const canvas = document.getElementById("knowledge-graph-canvas");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const ratio = canvas.width / rect.width || 1;
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, rect.width, rect.height);
  const state = knowledgeGraphFocus();
  knowledgeGraphProjection = knowledgeGraphData.nodes
    .map(node => projectKnowledgeGraphNode(node, rect.width, rect.height))
    .sort((left, right) => right.depth - left.depth);
  const projectedById = new Map(knowledgeGraphProjection.map(item => [item.node.id, item]));

  knowledgeGraphData.edges.forEach(edge => {
    const source = projectedById.get(edge.source);
    const target = projectedById.get(edge.target);
    if (!source || !target) return;
    const connected = state.focusIds.has(edge.source) || state.focusIds.has(edge.target);
    const dimmed = state.hasFocus && !connected;
    const color = graphEdgeColor(edge, connected);
    const depthOpacity = Math.min(0.9, Math.max(0.2, (source.perspective + target.perspective) * 0.28));
    const opacity = dimmed ? 0.045 : edge.explicit ? Math.max(0.58, depthOpacity) : depthOpacity;
    ctx.save();
    ctx.globalAlpha = opacity;
    ctx.strokeStyle = color;
    ctx.lineWidth = connected ? 2.1 : edge.explicit ? 1.45 : 1;
    if (["SIMILAR_TO", "CROSS_PHASE_SIMILAR", "CANDIDATE_VERSION_OF", "CONFLICTS_WITH"].includes(edge.relation_type)) {
      ctx.setLineDash([4, 5]);
    }
    ctx.beginPath();
    ctx.moveTo(source.x, source.y);
    ctx.lineTo(target.x, target.y);
    ctx.stroke();
    ctx.restore();
    drawKnowledgeGraphArrow(ctx, source, target, color, opacity);
  });
  knowledgeGraphProjection.forEach(projected => drawKnowledgeGraphNode(ctx, projected, state));
}

function findKnowledgeGraphNodeAt(x, y) {
  for (let index = knowledgeGraphProjection.length - 1; index >= 0; index -= 1) {
    const projected = knowledgeGraphProjection[index];
    const hitRadius = Math.max(11, projected.radius + 5);
    if (Math.hypot(x - projected.x, y - projected.y) <= hitRadius) return projected.node;
  }
  return null;
}

function hideKnowledgeGraphTooltip() {
  const tooltip = document.getElementById("knowledge-graph-tooltip");
  if (tooltip) tooltip.hidden = true;
}

function showKnowledgeGraphTooltip(node, point) {
  const tooltip = document.getElementById("knowledge-graph-tooltip");
  const stage = document.getElementById("knowledge-graph-stage");
  if (!tooltip || !stage || !node) return hideKnowledgeGraphTooltip();
  const kind = { card: "知识卡", case: "案例包", object: "业务对象", source: "来源文档" }[node.kind] || "知识节点";
  const title = document.createElement("strong");
  title.textContent = node.label;
  const meta = document.createElement("span");
  meta.textContent = node.kind === "card" ? `${kind} · K${node.entity_id}` : kind;
  tooltip.replaceChildren(title, meta);
  tooltip.hidden = false;
  const maxLeft = Math.max(12, stage.clientWidth - Math.min(280, tooltip.offsetWidth) - 12);
  const maxTop = Math.max(12, stage.clientHeight - tooltip.offsetHeight - 12);
  tooltip.style.left = `${Math.min(maxLeft, Math.max(12, point.x + 16))}px`;
  tooltip.style.top = `${Math.min(maxTop, Math.max(12, point.y + 16))}px`;
}

function knowledgeGraphCanvasPoint(event) {
  const rect = event.currentTarget.getBoundingClientRect();
  return { x: event.clientX - rect.left, y: event.clientY - rect.top };
}

function bindKnowledgeGraphInteraction() {
  const canvas = document.getElementById("knowledge-graph-canvas");
  if (!canvas) return;
  canvas.onpointerdown = event => {
    const point = knowledgeGraphCanvasPoint(event);
    knowledgeGraphInteraction = {
      pointerId: event.pointerId,
      startX: point.x,
      startY: point.y,
      yaw: knowledgeGraphTransform.yaw,
      pitch: knowledgeGraphTransform.pitch,
      panX: knowledgeGraphTransform.panX,
      panY: knowledgeGraphTransform.panY,
      pan: event.shiftKey,
      moved: false,
    };
    canvas.setPointerCapture(event.pointerId);
    canvas.classList.add("is-dragging");
    hideKnowledgeGraphTooltip();
  };
  canvas.onpointermove = event => {
    const point = knowledgeGraphCanvasPoint(event);
    if (knowledgeGraphInteraction?.pointerId === event.pointerId) {
      const dx = point.x - knowledgeGraphInteraction.startX;
      const dy = point.y - knowledgeGraphInteraction.startY;
      if (Math.hypot(dx, dy) > 3) knowledgeGraphInteraction.moved = true;
      if (knowledgeGraphInteraction.pan) {
        knowledgeGraphTransform.panX = knowledgeGraphInteraction.panX + dx;
        knowledgeGraphTransform.panY = knowledgeGraphInteraction.panY + dy;
      } else {
        knowledgeGraphTransform.yaw = knowledgeGraphInteraction.yaw + dx * 0.008;
        knowledgeGraphTransform.pitch = Math.max(-1.25, Math.min(1.25, knowledgeGraphInteraction.pitch + dy * 0.008));
      }
      drawKnowledgeGraph();
      return;
    }
    const hovered = findKnowledgeGraphNodeAt(point.x, point.y);
    const nextHoveredId = hovered?.id || "";
    if (nextHoveredId !== knowledgeGraphHoveredId) {
      knowledgeGraphHoveredId = nextHoveredId;
      canvas.style.cursor = hovered ? "pointer" : "grab";
      drawKnowledgeGraph();
    }
    if (hovered) showKnowledgeGraphTooltip(hovered, point);
    else hideKnowledgeGraphTooltip();
  };
  const finish = event => {
    if (!knowledgeGraphInteraction || knowledgeGraphInteraction.pointerId !== event.pointerId) return;
    const point = knowledgeGraphCanvasPoint(event);
    const moved = knowledgeGraphInteraction.moved;
    knowledgeGraphInteraction = null;
    canvas.classList.remove("is-dragging");
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    if (!moved) {
      const node = findKnowledgeGraphNodeAt(point.x, point.y);
      renderKnowledgeGraphDetail(node);
    }
  };
  canvas.onpointerup = finish;
  canvas.onpointercancel = finish;
  canvas.onpointerleave = () => {
    if (!knowledgeGraphInteraction) {
      knowledgeGraphHoveredId = "";
      hideKnowledgeGraphTooltip();
      drawKnowledgeGraph();
    }
  };
  canvas.onwheel = event => {
    event.preventDefault();
    zoomKnowledgeGraph(event.deltaY < 0 ? 1.1 : 0.91);
  };
  canvas.onkeydown = event => {
    if (["+", "="].includes(event.key)) {
      event.preventDefault();
      zoomKnowledgeGraph(1.15);
      return;
    }
    if (event.key === "-") {
      event.preventDefault();
      zoomKnowledgeGraph(0.87);
      return;
    }
    if (event.key.toLocaleLowerCase() === "r") {
      event.preventDefault();
      resetKnowledgeGraphTransform();
      return;
    }
    if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp"].includes(event.key) || !knowledgeGraphData.nodes.length) return;
    event.preventDefault();
    const currentIndex = Math.max(0, knowledgeGraphData.nodes.findIndex(node => node.id === knowledgeGraphSelectedId));
    const direction = ["ArrowRight", "ArrowDown"].includes(event.key) ? 1 : -1;
    const nextIndex = (currentIndex + direction + knowledgeGraphData.nodes.length) % knowledgeGraphData.nodes.length;
    renderKnowledgeGraphDetail(knowledgeGraphData.nodes[nextIndex]);
  };
}

function runKnowledgeGraphLayout() {
  if (knowledgeGraphFrame) cancelAnimationFrame(knowledgeGraphFrame);
  const nodes = knowledgeGraphData.nodes;
  const nodeById = new Map(nodes.map(node => [node.id, node]));
  const clusterTotals = {};
  const clusterSeen = {};
  nodes.forEach(node => {
    const cluster = node.cluster || "OTHER_CARD";
    clusterTotals[cluster] = (clusterTotals[cluster] || 0) + 1;
  });
  nodes.forEach(node => {
    const cluster = node.cluster || "OTHER_CARD";
    const index = clusterSeen[cluster] || 0;
    clusterSeen[cluster] = index + 1;
    const total = Math.max(1, clusterTotals[cluster]);
    const centroid = node.cluster_centroid || { x: 0, y: 0, z: 0 };
    const jitter = (graphHash(node.id) % 1000) / 1000;
    const vertical = 1 - 2 * ((index + 0.5) / total);
    const ring = Math.sqrt(Math.max(0, 1 - vertical * vertical));
    const angle = (index + jitter) * Math.PI * (3 - Math.sqrt(5));
    const radius = Math.min(96, 28 + Math.sqrt(total) * 14);
    node.clusterX = Number(centroid.x) || 0;
    node.clusterY = Number(centroid.y) || 0;
    node.clusterZ = Number(centroid.z) || 0;
    node.x = node.clusterX + Math.cos(angle) * ring * radius;
    node.y = node.clusterY + vertical * radius * 0.72;
    node.z = node.clusterZ + Math.sin(angle) * ring * radius;
    node.vx = 0;
    node.vy = 0;
    node.vz = 0;
  });
  let iteration = 0;
  const maxIterations = nodes.length > 260 ? 85 : 125;
  const step = () => {
    const alpha = Math.max(0.08, 1 - iteration / maxIterations);
    knowledgeGraphData.edges.forEach(edge => {
      const source = nodeById.get(edge.source);
      const target = nodeById.get(edge.target);
      if (!source || !target) return;
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const dz = target.z - source.z;
      const distance = Math.max(1, Math.hypot(dx, dy, dz));
      const sameCluster = source.cluster === target.cluster;
      const ideal = sameCluster ? 80 : edge.relation_type === "CONTAINS" ? 150 : edge.explicit ? 205 : 180;
      const strength = sameCluster ? 0.0018 : 0.00058;
      const force = (distance - ideal) * strength * alpha;
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      const fz = (dz / distance) * force;
      source.vx += fx;
      source.vy += fy;
      source.vz += fz;
      target.vx -= fx;
      target.vy -= fy;
      target.vz -= fz;
    });
    for (let left = 0; left < nodes.length; left += 1) {
      for (let right = left + 1; right < nodes.length; right += 1) {
        const first = nodes[left];
        const second = nodes[right];
        let dx = second.x - first.x;
        let dy = second.y - first.y;
        let dz = second.z - first.z;
        let distanceSquared = dx * dx + dy * dy + dz * dz;
        if (distanceSquared > 42000) continue;
        if (distanceSquared < 1) {
          dx = ((graphHash(first.id + second.id) % 11) - 5) || 1;
          dy = ((graphHash(second.id + first.id) % 11) - 5) || -1;
          dz = ((graphHash(first.id + ":z:" + second.id) % 11) - 5) || 1;
          distanceSquared = dx * dx + dy * dy + dz * dz;
        }
        const distance = Math.sqrt(distanceSquared);
        const minimum = graphNodeRadius(first) + graphNodeRadius(second) + 34;
        const force = Math.min(0.52, (minimum * minimum) / distanceSquared) * alpha;
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        const fz = (dz / distance) * force;
        first.vx -= fx;
        first.vy -= fy;
        first.vz -= fz;
        second.vx += fx;
        second.vy += fy;
        second.vz += fz;
      }
    }
    nodes.forEach(node => {
      const centroidStrength = node.kind === "card" ? 0.0042 : 0.0034;
      node.vx += (node.clusterX - node.x) * centroidStrength * alpha;
      node.vy += (node.clusterY - node.y) * centroidStrength * alpha;
      node.vz += (node.clusterZ - node.z) * centroidStrength * alpha;
      node.vx *= 0.84;
      node.vy *= 0.84;
      node.vz *= 0.84;
      node.x = Math.min(520, Math.max(-520, node.x + node.vx));
      node.y = Math.min(360, Math.max(-360, node.y + node.vy));
      node.z = Math.min(500, Math.max(-500, node.z + node.vz));
    });
    drawKnowledgeGraph();
    iteration += 1;
    if (iteration < maxIterations) knowledgeGraphFrame = requestAnimationFrame(step);
    else knowledgeGraphFrame = null;
  };
  drawKnowledgeGraph();
  knowledgeGraphFrame = requestAnimationFrame(step);
}

function renderKnowledgeGraph() {
  const canvas = document.getElementById("knowledge-graph-canvas");
  const empty = document.getElementById("knowledge-graph-empty");
  canvas.setAttribute("aria-label", `知识资产三维关系图，共 ${knowledgeGraphData.nodes.length} 个节点、${knowledgeGraphData.edges.length} 条关系。拖动旋转，Shift 加拖动平移，滚轮缩放，方向键选择节点。`);
  empty.hidden = knowledgeGraphData.nodes.length > 0;
  resetKnowledgeGraphTransform();
  renderKnowledgeGraphDetail(null);
  bindKnowledgeGraphInteraction();
  if (knowledgeGraphResizeObserver) knowledgeGraphResizeObserver.disconnect();
  knowledgeGraphResizeObserver = new ResizeObserver(resizeKnowledgeGraphCanvas);
  knowledgeGraphResizeObserver.observe(canvas);
  resizeKnowledgeGraphCanvas();
  if (!knowledgeGraphData.nodes.length) return drawKnowledgeGraph();
  runKnowledgeGraphLayout();
}

async function loadKnowledgeGraph() {
  const status = document.getElementById("graph-status").value;
  knowledgeGraphData = await api(`/api/knowledge-graph?status=${encodeURIComponent(status)}&limit=160`);
  knowledgeGraphSelectedId = "";
  knowledgeGraphHoveredId = "";
  knowledgeGraphInteraction = null;
  hideKnowledgeGraphTooltip();
  const meta = knowledgeGraphData.meta || {};
  document.getElementById("graph-node-count").textContent = meta.node_count || 0;
  document.getElementById("graph-edge-count").textContent = meta.edge_count || 0;
  document.getElementById("graph-card-count").textContent = meta.nodes_by_kind?.card || 0;
  document.getElementById("graph-relation-count").textContent = meta.explicit_relation_count || 0;
  renderKnowledgeGraph();
}

async function refreshAll() {
  try {
    await refreshHealth();
    await Promise.all([
      refreshStats(), loadRecent(), loadReviewQueue(), loadLibrary(),
      loadSemanticGovernanceSummary(),
    ]);
  } catch (error) {
    toast(error.message, true);
  }
}

window.showDetail = async function showDetail(id) {
  try {
    const card = await api(`/api/cards/${id}`);
    const field = (label, value) => `<dt>${label}</dt><dd>${Array.isArray(value) ? value.map(escapeHtml).join("\n") : escapeHtml(value)}</dd>`;
    const sourceEvidence = (card.source_items || []).map(item =>
      `${item.output_field}[${item.output_index}] ← ${item.source_pointer} · chars ${item.char_start}-${item.char_end} · sha256 ${String(item.source_hash || "").slice(0, 16)}…`
    );
    const structuralCoverage = card.lineage?.structural_source_coverage_status || card.lineage?.content_coverage_status;
    const coverage = structuralCoverage
      ? `${structuralCoverage} (${sourceEvidence.length}/${card.lineage.expected_source_items})`
      : "LEGACY_NOT_EVALUATED";
    document.getElementById("dialog-content").innerHTML = `
      <p class="eyebrow">KNOWLEDGE CARD K${card.id}</p><h2>${escapeHtml(card.title)}</h2>
      <div class="card-meta"><span class="tag ${card.status}">${statusLabels[card.status]}</span><span class="tag">${escapeHtml(card.card_type || "LEGACY")}</span><span class="tag">质量 ${card.content_quality ?? card.quality_score}</span><span class="tag">${escapeHtml(card.dedup_status || card.comparison_label)}</span><span class="tag">${escapeHtml(card.publish_status || "CANDIDATE")}</span></div>
      <dl class="detail-grid">
        ${field("摘要", card.summary)}${field("适用场景", card.scenario)}${field("对象", `${card.object_type} ${card.object_name}`)}
        ${field("适用版本", card.applicable_versions)}${field("前置条件", card.prerequisites)}${field("操作步骤", card.procedure_steps)}
        ${field("风险", card.risks)}${field("回退", card.rollback_steps)}${field("验证", card.validation_steps)}
        ${field("通用化操作", card.generalized_operation || "")}${field("影响分析", card.impact_analysis || "")}${field("适用阶段", card.applicable_phases || [])}
        ${field("Operation Sections", JSON.stringify(card.operation_sections || [], null, 2))}${field("检索启用", card.retrieval_enabled !== false ? "是" : "否")}
        ${field("原文证据", card.evidence_quote)}${field("证据位置", card.evidence_locator)}${field("来源", card.source_ref)}
        ${field("逐源覆盖", coverage)}${field("结构证据矩阵", sourceEvidence)}
        ${field("比较判断", `${card.comparison_label} (${card.comparison_confidence})：${card.comparison_reason}`)}
        ${field("质量问题", card.quality_issues)}${field("审核", `${card.reviewer || "未审核"} ${card.review_comment || ""}`)}
      </dl>
      <div class="card-actions"><button class="button danger" onclick="deleteCard(${card.id})">删除这张知识卡片</button></div>`;
    document.getElementById("detail-dialog").showModal();
  } catch (error) { toast(error.message, true); }
};

window.reviewCard = async function reviewCard(id, action) {
  const comment = document.getElementById("review-comment").value.trim();
  let supersedesId = null;
  if (action === "supersede") {
    const raw = prompt("请输入要被替代的旧知识卡片 ID：");
    if (!raw) return;
    supersedesId = Number(raw);
  }
  try {
    await api(`/api/cards/${id}/review`, {
      method: "POST",
      body: JSON.stringify({ action, comment, supersedes_id: supersedesId }),
    });
    toast(`K${id} 审核完成`);
    await refreshAll();
  } catch (error) { toast(error.message, true); }
};

async function showCaseBundle(caseId) {
  const bundle = await api(`/api/knowledge-case-bundles/${encodeURIComponent(caseId)}`);
  const coverage = bundle.extraction_report?.structural_source_coverage || bundle.extraction_report?.content_coverage || {};
  const cards = bundle.cards || [];
  const report = bundle.extraction_report?.card_build_report || {};
  const rebuild = report.rebuild || {};
  const rebuildSummary = rebuild.is_rebuild ? `
      <dt>最近重建</dt><dd>Generation ${escapeHtml(rebuild.previous_generation)} → ${escapeHtml(rebuild.current_generation)} · ${escapeHtml(rebuild.previous_card_count)} → ${escapeHtml(rebuild.new_card_count)} 张卡</dd>
      <dt>重建后类型</dt><dd>CASE_CONTEXT ${escapeHtml(report.case_context_count ?? 0)} · PROCEDURE_STEP ${escapeHtml(report.procedure_unit_count ?? 0)} · EXECUTION_OUTCOME ${escapeHtml(report.execution_outcome_count ?? 0)}</dd>
      <dt>重建诊断</dt><dd>Semantic Reuse ${escapeHtml(report.semantic_reuse_count ?? 0)} · Skipped Sections ${escapeHtml(report.skipped_child_section_count ?? 0)}</dd>` : "";
  const rebuildAction = platformConfig.demo_management?.rebuild_enabled ? `
      <button class="button warning" data-bundle-action="rebuild" data-case-id="${escapeHtml(bundle.case_id)}">重建当前案例</button>` : "";
  document.getElementById("dialog-content").innerHTML = `
    <p class="eyebrow">CHANGE CASE BUNDLE</p><h2>${escapeHtml(bundle.title)}</h2>
    <div class="card-meta"><span class="tag ${escapeHtml(bundle.status)}">${statusLabels[bundle.status] || escapeHtml(bundle.status)}</span><span class="tag">${cards.length} 张原子卡</span><span class="tag">Generation ${escapeHtml(bundle.build_generation || 1)}</span><span class="tag">覆盖 ${escapeHtml(coverage.status || "未评估")}</span></div>
    <dl class="detail-grid">
      <dt>案例包 ID</dt><dd>${escapeHtml(bundle.case_id)}</dd>
      <dt>来源</dt><dd>${escapeHtml(bundle.source_ref)}</dd>
      <dt>内容哈希</dt><dd>${escapeHtml(bundle.source_checksum)}</dd>
      <dt>原子卡状态</dt><dd>${escapeHtml(Object.entries(bundle.status_counts || {}).map(([key, value]) => `${statusLabels[key] || key} ${value}`).join(" · "))}</dd>
      <dt>结构来源覆盖</dt><dd>${escapeHtml(`${coverage.accounted_source_items ?? coverage.mapped_source_items ?? "-"}/${coverage.expected_source_items ?? "-"} 条源记录`)}</dd>
      <dt>语义内容覆盖</dt><dd>${escapeHtml(bundle.extraction_report?.semantic_content_coverage?.status || "未评估")}</dd>
      <dt>Builder Version</dt><dd>${escapeHtml(bundle.builder_version || report.builder || "-")}</dd>
      <dt>Card Model Version</dt><dd>${escapeHtml(bundle.card_model_version || report.card_model_version || "-")}</dd>
      ${rebuildSummary}
    </dl>
    <div class="card-actions">${rebuildAction}</div>
    <details><summary>查看 card_build_report</summary><pre>${escapeHtml(JSON.stringify(report, null, 2))}</pre></details>
    <div class="bundle-card-list">${cards.map(card => cardHtml(card)).join("")}</div>`;
  document.getElementById("detail-dialog").showModal();
}

async function reviewCaseBundle(caseId, action) {
  const label = action === "approve" ? "批准" : "驳回";
  if (!window.confirm(`确认${label}整个变更案例包？该操作会一次性审核包内全部原子卡。`)) return;
  const comment = document.getElementById("review-comment").value.trim();
  await api(`/api/knowledge-case-bundles/${encodeURIComponent(caseId)}/review`, {
    method: "POST",
    body: JSON.stringify({ action, comment }),
  });
  toast(`案例包已整包${label}`);
  await refreshAll();
}

async function rebuildCaseBundle(caseId) {
  if (!window.confirm("将删除当前案例已生成的知识卡、审核状态、索引和缓存，并使用当前版本逻辑重新生成。原始上传 JSON 不会删除。\n\n确认重建当前案例？")) return;
  const result = await api(`/api/knowledge-case-bundles/${encodeURIComponent(caseId)}/rebuild`, {
    method: "POST",
    body: JSON.stringify({ confirmation: "REBUILD_CURRENT_CASE" }),
  });
  const report = result.card_build_report || {};
  const rebuild = report.rebuild || {};
  toast(`重建完成：${rebuild.previous_card_count ?? "-"} → ${rebuild.new_card_count ?? result.extracted_cards} 张卡`);
  await refreshAll();
  await showCaseBundle(caseId);
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-bundle-action]");
  if (!button) return;
  const caseId = button.dataset.caseId;
  const action = button.dataset.bundleAction;
  const task = action === "detail"
    ? showCaseBundle(caseId)
    : action === "rebuild"
    ? rebuildCaseBundle(caseId)
    : reviewCaseBundle(caseId, action);
  task.catch(error => toast(error.message, true));
});

window.deleteCard = async function deleteCard(id) {
  if (!window.confirm(`确认永久删除知识卡片 K${id}？关联关系和本地索引会同步清理。`)) return;
  try {
    await api(`/api/cards/${id}`, { method: "DELETE" });
    document.getElementById("detail-dialog").close();
    toast(`K${id} 已删除`);
    await refreshAll();
  } catch (error) { toast(error.message, true); }
};

const realChangeStatusLabels = {
  GENERATING: "生成中", DRAFT: "草案", BLOCKED: "硬门禁阻断",
  GENERATION_FAILED: "生成失败", READY_FOR_REVIEW: "待人工审核",
  REVIEW_APPROVED: "审核通过", REJECTED: "已驳回",
};

function realChangeLines(id) {
  return document.getElementById(id).value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
}

function realChangeRequest() {
  let parameters;
  try {
    parameters = JSON.parse(document.getElementById("real-change-parameters").value || "{}");
  } catch (error) {
    throw new Error(`已知参数不是合法 JSON：${error.message}`);
  }
  if (!parameters || Array.isArray(parameters) || typeof parameters !== "object") {
    throw new Error("已知参数必须是 JSON 对象");
  }
  const scenario = document.getElementById("real-change-scenario").value.trim();
  return {
    goal: document.getElementById("real-change-goal").value.trim(),
    scenario,
    scenario_reference: {
      name_cn: scenario,
      name_en: document.getElementById("real-change-scenario-name-en").value.trim(),
      unique_id: document.getElementById("real-change-scenario-unique-id").value.trim(),
    },
    region: document.getElementById("real-change-region").value.trim(),
    services: realChangeLines("real-change-services"),
    objects: realChangeLines("real-change-objects"),
    current_state: document.getElementById("real-change-current-state").value.trim(),
    target_state: document.getElementById("real-change-target-state").value.trim(),
    window: {
      start: document.getElementById("real-change-window-start").value.trim(),
      end: document.getElementById("real-change-window-end").value.trim(),
    },
    impact_scope: document.getElementById("real-change-impact").value.trim(),
    constraints: realChangeLines("real-change-constraints"),
    parameters,
    validation_requirements: realChangeLines("real-change-validations"),
    requester: document.getElementById("real-change-requester").value.trim() || "shared-operator",
  };
}

function renderRealChangeRecommendations(payload) {
  realChangeRecommendations = payload.candidates || [];
  const target = document.getElementById("real-change-recommendations");
  const count = document.getElementById("real-change-recommend-count");
  count.textContent = `${realChangeRecommendations.length} 个可用案例`;
  count.className = `tag ${realChangeRecommendations.length ? "APPROVED" : "REJECTED"}`;
  target.classList.toggle("empty", !realChangeRecommendations.length);
  target.innerHTML = realChangeRecommendations.length ? realChangeRecommendations.map((item, index) => `
    <label class="real-change-candidate">
      <input type="checkbox" value="${escapeHtml(item.case_id)}" ${index === 0 ? "checked" : ""}>
      <span><strong>${escapeHtml(item.title)}</strong>
      <small><code>${escapeHtml(item.case_id)}</code> · ${item.card_count} 张卡 · 匹配 ${escapeHtml((item.matched_roles || []).join(" / ") || "案例上下文")}</small>
      <small>${escapeHtml(item.reason)}</small></span>
    </label>`).join("") : "没有通过 APPROVED、完整性和 Profile 一致性检查的推荐案例。";
  document.getElementById("real-change-retrieval-diagnostic").textContent = JSON.stringify({
    retrieval: payload.retrieval || {},
    rejected: payload.rejected || [],
  }, null, 2);
  document.getElementById("real-change-generate").disabled = !realChangeRecommendations.length;
}

function selectedRealChangeCases() {
  return [...document.querySelectorAll("#real-change-recommendations input:checked")].map(item => item.value);
}

function realChangeValidationRows(validation) {
  const gates = validation?.gates || {
    schema_profile_consistent: !validation?.hard_failures?.includes("SCHEMA_PROFILE_OR_ADAPTER_CONFLICT"),
    required_parameters_complete: !validation?.hard_failures?.includes("REQUIRED_FIELDS_MISSING"),
    task_views_reconciled: !validation?.hard_failures?.includes("TASK_GROUP_RECONCILIATION_FAILED"),
    provenance_coverage_complete: !validation?.hard_failures?.includes("PROVENANCE_COVERAGE_INCOMPLETE"),
  };
  const labels = {
    schema_profile_consistent: "Schema / Profile 一致",
    required_parameters_complete: "必填参数齐全",
    four_phase_complete: "四阶段安全内容完整",
    task_views_reconciled: "任务双视图完全对账",
    provenance_coverage_complete: "引用覆盖率 100%",
    approved_sources_unchanged: "引用仍为 APPROVED 且未漂移",
    execution_result_sanitized: "无历史执行结果泄漏",
    procedure_non_generated_fields_sanitized: "步骤核验与更新字段保持未填写",
    no_unattributed_specific_values: "无无来源具体值",
  };
  return Object.entries(gates).map(([key, passed]) => `
    <div class="validation-row"><span class="${passed ? "PASS" : "FAIL"}">${passed ? "PASS" : "FAIL"}</span>
    <div><strong>${escapeHtml(labels[key] || key)}</strong><small>${passed ? "已通过确定性校验" : "当前 revision 未满足该硬条件"}</small></div></div>`).join("");
}

function renderRealChangeDraft(draft) {
  activeRealChangeDraft = draft;
  const panel = document.getElementById("real-change-draft-panel");
  panel.hidden = !draft;
  if (!draft) return;
  const revision = draft.revision || {};
  const validation = revision.validation || {};
  const provenance = revision.provenance || {};
  const modelRouting = revision.model_usage?.model_routing || {};
  const status = String(draft.status || "");
  document.getElementById("real-change-current").textContent = `${draft.draft_id} · ${realChangeStatusLabels[status] || status}`;
  document.getElementById("real-change-draft-id").textContent = draft.draft_id;
  const statusBadge = document.getElementById("real-change-draft-status");
  statusBadge.textContent = realChangeStatusLabels[status] || status;
  statusBadge.className = `tag ${status === "REVIEW_APPROVED" || status === "READY_FOR_REVIEW" ? "APPROVED" : status === "GENERATING" ? "PENDING_REVIEW" : "REJECTED"}`;
  document.getElementById("real-change-revision").textContent = draft.current_revision ? `R${draft.current_revision}` : "等待模型";
  document.getElementById("real-change-draft-profile").textContent = draft.profile_id || "—";
  document.getElementById("real-change-selected-cases").textContent = (draft.selected_case_ids || []).join("、") || "—";
  document.getElementById("real-change-content-hash").textContent = revision.content_hash ? `${revision.content_hash.slice(0, 16)}…` : "—";
  const message = document.getElementById("real-change-generation-message");
  message.classList.toggle("error", ["BLOCKED", "GENERATION_FAILED", "REJECTED"].includes(status));
  const routingSummary = modelRouting.selected_model
    ? ` 实际模型：${modelRouting.selected_model}${modelRouting.fallback_used ? "（主模型容量不足，已自动切换）" : ""}。`
    : "";
  message.textContent = status === "GENERATING"
    ? "模型正在生成独立规范化草案；协议错误最多修复一次，不会输出规则兜底草案。"
    : draft.model_error || (validation.passed
      ? `当前 revision 已通过生成阶段硬校验，等待人工复核。${routingSummary}`
      : `当前 revision 被硬门禁阻断，请检查缺失项或引用。${routingSummary}`);
  const editor = document.getElementById("real-change-normalized");
  editor.value = revision.normalized ? JSON.stringify(revision.normalized, null, 2) : "";
  editor.disabled = !revision.normalized;
  document.getElementById("real-change-save").disabled = !revision.normalized;
  const validationBadge = document.getElementById("real-change-validation-badge");
  validationBadge.textContent = revision.validation ? (validation.passed ? "全部通过" : `${(validation.hard_failures || []).length} 项阻断`) : "等待生成";
  validationBadge.className = `tag ${validation.passed ? "APPROVED" : revision.validation ? "REJECTED" : ""}`;
  document.getElementById("real-change-validation").innerHTML = revision.validation
    ? realChangeValidationRows(validation) : `<p class="muted-copy">等待模型输出和确定性装配。</p>`;
  document.getElementById("real-change-provenance").innerHTML = revision.provenance ? `
    <p><strong>${(provenance.items || []).length}</strong> 个输出引用 · <strong>${(provenance.card_snapshots || []).length}</strong> 张卡片快照</p>
    <p>缺失项：${escapeHtml((validation.missing_fields || []).join("、") || "无")}</p>
    <p>需求哈希：<code>${escapeHtml(provenance.request_hash || "—")}</code></p>` : "等待生成。";
  document.getElementById("real-change-json").textContent = revision.change ? JSON.stringify(revision.change, null, 2) : "尚未生成";
  document.getElementById("real-change-approve").disabled = status !== "READY_FOR_REVIEW";
  document.getElementById("real-change-reject").disabled = !revision.normalized || ["REVIEW_APPROVED", "REJECTED"].includes(status);
  document.getElementById("real-change-export").disabled = status !== "REVIEW_APPROVED";
}

async function loadRealChangeDraft(draftId) {
  const draft = await api(`/api/change-drafts/${encodeURIComponent(draftId)}`);
  renderRealChangeDraft(draft);
  return draft;
}

function scheduleRealChangePoll(draftId) {
  clearTimeout(realChangePollTimer);
  realChangePollTimer = window.setTimeout(async () => {
    try {
      const draft = await loadRealChangeDraft(draftId);
      if (draft.status === "GENERATING") scheduleRealChangePoll(draftId);
      else toast(draft.status === "GENERATION_FAILED" ? "草案生成失败，已保留错误与检索诊断" : "真实变更草案生成完成", draft.status === "GENERATION_FAILED");
    } catch (error) { toast(error.message, true); }
  }, 1200);
}

function downloadJson(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
  const href = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = href;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(href), 1000);
}

function renderRealChangeEvaluation(evaluation) {
  activeRealChangeEvaluation = evaluation;
  const status = evaluation?.status || "未启动";
  const badge = document.getElementById("real-change-evaluation-status");
  badge.textContent = status;
  badge.className = `tag ${status === "COMPLETED" ? "APPROVED" : status === "FAILED" ? "REJECTED" : "PENDING_REVIEW"}`;
  document.getElementById("real-change-evaluation-report").textContent = evaluation
    ? JSON.stringify(evaluation.report && Object.keys(evaluation.report).length ? evaluation.report : {
      evaluation_id: evaluation.evaluation_id,
      held_out_case_id: evaluation.held_out_case_id,
      status: evaluation.status,
      error: evaluation.error || undefined,
    }, null, 2) : "等待启动留一案例评测。";
  document.getElementById("real-change-evaluation-download").disabled = status !== "COMPLETED";
}

function scheduleRealChangeEvaluationPoll(evaluationId) {
  clearTimeout(realChangeEvaluationPollTimer);
  realChangeEvaluationPollTimer = window.setTimeout(async () => {
    try {
      const evaluation = await api(`/api/change-evaluations/${encodeURIComponent(evaluationId)}`);
      renderRealChangeEvaluation(evaluation);
      if (["QUEUED", "RUNNING"].includes(evaluation.status)) scheduleRealChangeEvaluationPoll(evaluationId);
      else toast(evaluation.status === "COMPLETED" ? "留一案例盲测完成" : "留一案例盲测失败", evaluation.status !== "COMPLETED");
    } catch (error) { toast(error.message, true); }
  }, 1500);
}

async function loadRealChangeWorkbench() {
  const [profileStatus, bundles, drafts, evaluations] = await Promise.all([
    api("/api/change-schema-profile"),
    api("/api/knowledge-case-bundles?status=APPROVED&limit=200"),
    api("/api/change-drafts?limit=1"),
    api("/api/change-evaluations?limit=1"),
  ]);
  const profile = profileStatus.active_profile;
  const enabled = Boolean(profileStatus.feature_enabled);
  document.getElementById("real-change-feature").textContent = enabled ? "已启用" : "默认关闭";
  document.getElementById("real-change-profile").textContent = profile ? `${profile.profile_id} · v${profile.version}` : "未激活";
  document.getElementById("real-change-profile-case").textContent = profile?.source_case_id || "—";
  document.getElementById("real-change-warning-text").textContent = profileStatus.warning || "只生成草案；不注册、不调用任何真实执行工具。";
  const featureBadge = document.getElementById("real-change-feature-badge");
  featureBadge.textContent = enabled && profile ? "可开始试验" : !enabled ? "REAL_CHANGE_GENERATION_ENABLED=false" : "缺少 Profile";
  featureBadge.className = `tag ${enabled && profile ? "APPROVED" : "REJECTED"}`;
  document.getElementById("real-change-recommend").disabled = !(enabled && profile);
  const eligibleBundles = (bundles.case_bundles || []).filter(item => item.status === "APPROVED" && Number(item.card_count || 0) > 0);
  const select = document.getElementById("real-change-held-out");
  select.innerHTML = eligibleBundles.length ? eligibleBundles.map(item =>
    `<option value="${escapeHtml(item.case_id)}">${escapeHtml(item.title)} · ${escapeHtml(item.case_id)}</option>`
  ).join("") : `<option value="">没有 APPROVED 案例包</option>`;
  document.getElementById("real-change-evaluate").disabled = !(enabled && profile && eligibleBundles.length > 1);
  const latestDraft = (drafts.drafts || [])[0];
  if (latestDraft?.draft_id) await loadRealChangeDraft(latestDraft.draft_id);
  const latestEvaluation = (evaluations.evaluations || [])[0];
  if (latestEvaluation) {
    renderRealChangeEvaluation(latestEvaluation);
    if (["QUEUED", "RUNNING"].includes(latestEvaluation.status)) scheduleRealChangeEvaluationPoll(latestEvaluation.evaluation_id);
  }
}

document.querySelectorAll(".nav-item").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach(item => { item.classList.remove("active"); item.setAttribute("aria-current", "false"); });
  document.querySelectorAll(".page").forEach(page => page.classList.remove("active"));
  button.classList.add("active");
  button.setAttribute("aria-current", "page");
  const page = button.dataset.page;
  document.getElementById(`page-${page}`).classList.add("active");
  document.getElementById("page-title").textContent = pageTitles[page];
  if (page === "real-change") loadRealChangeWorkbench().catch(error => toast(error.message, true));
  if (page === "semantic-governance") loadSemanticGovernance().catch(error => toast(error.message, true));
  if (page === "graph") loadKnowledgeGraph().catch(error => toast(error.message, true));
}));

document.getElementById("refresh-button").addEventListener("click", async () => {
  await refreshAll();
  if (document.getElementById("page-graph").classList.contains("active")) {
    await loadKnowledgeGraph();
  }
  if (document.getElementById("page-real-change").classList.contains("active")) {
    await loadRealChangeWorkbench();
  }
  if (document.getElementById("page-semantic-governance").classList.contains("active")) {
    await loadSemanticGovernance();
  }
});
document.getElementById("safe-shutdown-button").addEventListener("click", async event => {
  if (!window.confirm("安全退出会立即停止接收新任务，并等待当前模型请求和后台 Worker 排空后关闭平台。\n\n确认安全退出？")) return;
  const button = event.currentTarget;
  setBusy(button, true, "正在进入排空状态……");
  try {
    const status = await api("/api/admin/safe-shutdown", {
      method: "POST",
      body: JSON.stringify({ confirmation: "SAFE_SHUTDOWN" }),
    });
    safeShutdownRequested = true;
    renderSafeShutdown(status);
    pollSafeShutdown();
  } catch (error) {
    setBusy(button, false);
    toast(error.message, true);
  }
});
document.getElementById("library-search").addEventListener("click", () => loadLibrary().catch(error => toast(error.message, true)));
document.getElementById("semantic-proposal-status").addEventListener("change", () => loadSemanticGovernance().catch(error => toast(error.message, true)));
document.getElementById("semantic-scan-now").addEventListener("click", async event => {
  const button = event.currentTarget;
  setBusy(button, true, "正在扫描……");
  try {
    const report = await api("/api/semantic-merge-proposals/scan", {
      method: "POST", body: JSON.stringify({}),
    });
    toast(`扫描完成：形成 ${report.cluster_candidate_count || report.candidate_count || 0} 个跨案例知识簇，新增 ${report.created_cluster_count || report.created_proposal_count || 0} 个`);
    await loadSemanticGovernance();
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});
document.getElementById("semantic-proposal-list").addEventListener("click", async event => {
  const openCard = event.target.closest("[data-open-card]");
  if (openCard) {
    await window.showDetail(Number(openCard.dataset.openCard));
    return;
  }
  const removeButton = event.target.closest("[data-semantic-remove-member]");
  if (removeButton) {
    const proposalElement = removeButton.closest("[data-proposal-id]");
    const proposalId = proposalElement?.dataset.proposalId;
    const memberCardId = Number(removeButton.dataset.semanticRemoveMember);
    const comment = proposalElement?.querySelector("[data-merge-comment]")?.value.trim() || "";
    if (!proposalId || !memberCardId) return;
    if (!comment) {
      toast("移除知识簇成员前请填写人工意见，说明该成员为何不属于本次簇。", true);
      return;
    }
    if (!window.confirm(`确认从本次候选簇移除 K${memberCardId}？原候选会保留为审计记录，剩余成员将形成新的待审核簇。`)) return;
    setBusy(removeButton, true, "正在窄化……");
    try {
      const result = await api(`/api/semantic-merge-proposals/${encodeURIComponent(proposalId)}/decision`, {
        method: "POST",
        body: JSON.stringify({
          decision: "REMOVE_MEMBER",
          member_card_id: memberCardId,
          comment,
        }),
      });
      toast(`已移除 K${memberCardId}，新候选 ${result.proposal_id} 保留 ${result.member_count} 张卡`);
      await loadSemanticGovernance();
      if (document.getElementById("page-graph").classList.contains("active")) await loadKnowledgeGraph();
    } catch (error) { toast(error.message, true); }
    finally { setBusy(removeButton, false); }
    return;
  }
  const actionButton = event.target.closest("[data-semantic-action]");
  if (!actionButton) return;
  const proposalElement = actionButton.closest("[data-proposal-id]");
  const proposalId = proposalElement?.dataset.proposalId;
  const proposalType = proposalElement?.dataset.proposalType || "PAIR";
  const decision = actionButton.dataset.semanticAction;
  const mergeMode = actionButton.dataset.mergeMode || "";
  if (!proposalId || !decision) return;
  if (decision === "ACCEPT") {
    const message = mergeMode === "SHARED_CORE"
      ? "确认后将建立可检索的共享核心 canonical 卡，各案例成员保留为证据但退出普通检索。是否继续？"
      : `确认后将生成 canonical 卡，${proposalType === "CLUSTER" ? "知识簇成员" : "两张原卡"}保留审计证据但立即退出所有检索。是否继续？`;
    if (!window.confirm(message)) return;
  }
  if (decision === "REJECT" && !window.confirm(`确认${proposalType === "CLUSTER" ? "该知识簇不应合并" : "这两张卡不是同一知识"}？相同内容版本下不会再次推送。`)) return;
  const mergedCard = {};
  proposalElement.querySelectorAll("[data-merge-field]").forEach(input => {
    const field = input.dataset.mergeField;
    mergedCard[field] = field === "applicable_phases"
      ? input.value.split(/[、,，]/).map(item => item.trim()).filter(Boolean)
      : input.value.trim();
  });
  const comment = proposalElement.querySelector("[data-merge-comment]")?.value.trim() || "";
  setBusy(actionButton, true, "正在提交……");
  try {
    const payload = { decision, comment };
    if (decision === "ACCEPT") {
      payload.merged_card = mergedCard;
      payload.merge_mode = mergeMode;
    }
    const result = await api(`/api/semantic-merge-proposals/${encodeURIComponent(proposalId)}/decision`, {
      method: "POST", body: JSON.stringify(payload),
    });
    toast(decision === "ACCEPT"
      ? `${mergeMode === "SHARED_CORE" ? "共享核心已建立" : "合并完成"}，已生成 canonical 卡 K${result.canonical_card_id}`
      : decision === "REJECT" ? "已记录为非同一知识" : "候选已暂缓");
    await Promise.all([loadSemanticGovernance(), loadLibrary()]);
    if (document.getElementById("page-graph").classList.contains("active")) await loadKnowledgeGraph();
  } catch (error) { toast(error.message, true); }
  finally { setBusy(actionButton, false); }
});
document.getElementById("graph-reload").addEventListener("click", () => loadKnowledgeGraph().catch(error => toast(error.message, true)));
document.getElementById("graph-status").addEventListener("change", () => loadKnowledgeGraph().catch(error => toast(error.message, true)));
document.getElementById("graph-query").addEventListener("input", applyKnowledgeGraphEmphasis);
document.getElementById("graph-rotate-left").addEventListener("click", () => rotateKnowledgeGraph(-0.28));
document.getElementById("graph-rotate-right").addEventListener("click", () => rotateKnowledgeGraph(0.28));
document.getElementById("graph-zoom-in").addEventListener("click", () => zoomKnowledgeGraph(1.2));
document.getElementById("graph-zoom-out").addEventListener("click", () => zoomKnowledgeGraph(0.83));
document.getElementById("graph-reset").addEventListener("click", () => {
  resetKnowledgeGraphTransform();
  runKnowledgeGraphLayout();
});
document.getElementById("graph-open-card").addEventListener("click", event => {
  const cardId = Number(event.currentTarget.dataset.cardId);
  if (cardId) window.showDetail(cardId);
});
document.getElementById("dialog-close").addEventListener("click", () => document.getElementById("detail-dialog").close());

document.getElementById("real-change-request-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = document.getElementById("real-change-recommend");
  setBusy(button, true, "正在检索完整案例包……");
  try {
    const result = await api("/api/change-drafts/recommend", {
      method: "POST",
      body: JSON.stringify({ request: realChangeRequest() }),
    });
    renderRealChangeRecommendations(result);
    toast(result.candidates?.length ? "案例包推荐完成，请确认参考案例" : "没有满足硬条件的参考案例", !result.candidates?.length);
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

document.getElementById("real-change-recommendations").addEventListener("change", event => {
  if (!event.target.matches('input[type="checkbox"]')) return;
  const selected = selectedRealChangeCases();
  if (selected.length > 3) {
    event.target.checked = false;
    toast("最多只能确认 3 个完整案例包", true);
  }
  document.getElementById("real-change-generate").disabled = selectedRealChangeCases().length < 1;
});

document.getElementById("real-change-generate").addEventListener("click", async event => {
  const button = event.currentTarget;
  const selected = selectedRealChangeCases();
  if (!selected.length) return toast("请至少确认 1 个推荐案例包", true);
  let request;
  try { request = realChangeRequest(); }
  catch (error) { return toast(error.message, true); }
  if (!Object.keys(request.parameters || {}).length && !window.confirm(
    "本次 request.parameters 为空。若变更需要新的 IP、账号、端口、阈值或资源规格，模型没有这些新值可用，也不会被允许从参考案例复制。\n\n确认本次变更确实不需要具体参数并继续？"
  )) return;
  setBusy(button, true, "正在创建生成任务……");
  try {
    const result = await api("/api/change-drafts", {
      method: "POST",
      body: JSON.stringify({
        request,
        selected_case_ids: selected,
        mode: "normal",
      }),
    });
    renderRealChangeDraft(result.draft);
    toast("真实 ChangeOrder 草案生成任务已启动");
    scheduleRealChangePoll(result.draft.draft_id);
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

document.getElementById("real-change-save").addEventListener("click", async event => {
  if (!activeRealChangeDraft) return;
  const button = event.currentTarget;
  let normalized;
  try { normalized = JSON.parse(document.getElementById("real-change-normalized").value); }
  catch (error) { return toast(`草案不是合法 JSON：${error.message}`, true); }
  setBusy(button, true, "正在创建新 Revision……");
  try {
    const draft = await api(`/api/change-drafts/${encodeURIComponent(activeRealChangeDraft.draft_id)}`, {
      method: "PATCH",
      body: JSON.stringify({ normalized }),
    });
    renderRealChangeDraft(draft);
    toast(`已创建 R${draft.current_revision}，旧审批已失效`);
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

async function reviewRealChangeDraft(decision, button) {
  if (!activeRealChangeDraft) return;
  setBusy(button, true, decision === "APPROVED" ? "正在复核引用……" : "正在驳回……");
  try {
    const draft = await api(`/api/change-drafts/${encodeURIComponent(activeRealChangeDraft.draft_id)}/review`, {
      method: "POST",
      body: JSON.stringify({
        decision,
        reviewer: document.getElementById("real-change-reviewer").value.trim() || "shared-operator",
        comment: document.getElementById("real-change-review-comment").value.trim(),
      }),
    });
    renderRealChangeDraft(draft);
    toast(decision === "APPROVED" ? "当前 Revision 已通过人工审核，可以导出" : "当前 Revision 已驳回");
  } catch (error) { toast(error.message, true); }
  finally {
    setBusy(button, false);
    if (activeRealChangeDraft) renderRealChangeDraft(activeRealChangeDraft);
  }
}

document.getElementById("real-change-approve").addEventListener("click", event => reviewRealChangeDraft("APPROVED", event.currentTarget));
document.getElementById("real-change-reject").addEventListener("click", event => reviewRealChangeDraft("REJECTED", event.currentTarget));
document.getElementById("real-change-export").addEventListener("click", async event => {
  if (!activeRealChangeDraft) return;
  const button = event.currentTarget;
  setBusy(button, true, "正在复核并导出……");
  try {
    const exported = await api(`/api/change-drafts/${encodeURIComponent(activeRealChangeDraft.draft_id)}/export`);
    downloadJson("change_order_draft.json", exported.change_order);
    downloadJson("provenance_report.json", {
      draft_id: exported.draft_id,
      revision: exported.revision,
      provenance: exported.provenance_report,
      validation: exported.validation_report,
    });
    toast("已下载真实草案 JSON 和伴随溯源报告");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

document.getElementById("real-change-evaluate").addEventListener("click", async event => {
  const heldOutCaseId = document.getElementById("real-change-held-out").value;
  if (!heldOutCaseId) return toast("请选择隐藏目标案例包", true);
  const button = event.currentTarget;
  setBusy(button, true, "正在启动盲测……");
  try {
    const result = await api("/api/change-evaluations", {
      method: "POST",
      body: JSON.stringify({ held_out_case_id: heldOutCaseId }),
    });
    renderRealChangeEvaluation(result.evaluation);
    scheduleRealChangeEvaluationPoll(result.evaluation.evaluation_id);
    toast("留一案例盲测已启动，目标内容已隔离");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

document.getElementById("real-change-evaluation-download").addEventListener("click", () => {
  if (activeRealChangeEvaluation?.status === "COMPLETED") {
    downloadJson("evaluation_report.json", activeRealChangeEvaluation.report);
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
    const memory = result.memory_retrieval;
    const memoryMeta = document.getElementById("answer-memory-meta");
    if (memory) {
      memoryMeta.textContent = `长期记忆：${memory.status} · 召回 ${memory.memory_hits || 0} 条 · 映射 ${memory.mapped_approved_cards || 0} 张已批准卡片 · 新增候选 K${(memory.semantic_added_card_ids || []).join(", K") || "无"}`;
    } else {
      const semanticSources = (result.sources || []).filter(source => source.retrieval_channel === "mindmemos_semantic").length;
      memoryMeta.textContent = `长期记忆语义来源：${semanticSources} 条`;
    }
    document.getElementById("answer-sources").innerHTML = (result.sources || []).map(source => `
      <div class="source-item"><strong>[K${source.card_id}] ${escapeHtml(source.title)}</strong>
      <p>${source.retrieval_channel === "mindmemos_semantic" ? "MindMemOS 语义召回" : "本地词法召回"} · ${escapeHtml(source.evidence_locator)} · ${escapeHtml(source.source_ref)}</p>
      <p>“${escapeHtml(source.evidence_quote)}”</p></div>`).join("");
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});

async function bootstrap() {
  await refreshAll();
  clearInterval(semanticGovernancePollTimer);
  semanticGovernancePollTimer = window.setInterval(() => {
    loadSemanticGovernanceSummary().catch(() => {});
  }, 60000);
}

document.getElementById("auth-form").addEventListener("submit", async event => {
  event.preventDefault();
  const candidate = document.getElementById("access-token").value.trim();
  if (!candidate) return;
  accessToken = candidate;
  try {
    await api("/api/health");
    sessionStorage.setItem(accessTokenKey, accessToken);
    document.getElementById("access-token").value = "";
    hideAuthGate();
    await bootstrap();
  } catch (error) {
    accessToken = "";
    sessionStorage.removeItem(accessTokenKey);
    showAuthGate(error.message);
  }
});

document.getElementById("forget-token").addEventListener("click", () => {
  accessToken = "";
  sessionStorage.removeItem(accessTokenKey);
  showAuthGate("访问令牌已从当前标签页清除");
});

document.getElementById("memory-sync").addEventListener("click", async event => {
  const button = event.currentTarget;
  setBusy(button, true, "正在同步……");
  try {
    const result = await api("/api/memory/sync", {
      method: "POST",
      body: JSON.stringify({}),
    });
    const succeeded = (result.results || []).filter(item => ["SUCCEEDED", "ALREADY_SYNCED"].includes(item.status)).length;
    const failed = (result.results || []).filter(item => item.status === "FAILED").length;
    toast(`长期记忆同步完成：${succeeded} 张成功，${failed} 张失败`, failed > 0);
    await refreshHealth();
  } catch (error) {
    toast(error.message, true);
  } finally {
    setBusy(button, false);
  }
});

if (accessToken) {
  bootstrap().catch(error => showAuthGate(error.message));
} else {
  bootstrap()
    .then(() => hideAuthGate())
    .catch(error => showAuthGate(error.message));
}

document.querySelectorAll("[data-go-page]").forEach(button => button.addEventListener("click", () => {
  document.querySelector(`.nav-item[data-page="${button.dataset.goPage}"]`).click();
}));

document.getElementById("clear-knowledge-button").addEventListener("click", async event => {
  if (!window.confirm("清空所有来源记录、知识卡、案例包、审核、关系和导入缓存？\n\n系统会先备份数据库。上传原文件与模型配置保留；旧草案/评测失效，SchemaProfile 停用。外部记忆仅断开本地引用并保留待清理队列。此操作不能在页面撤销，恢复需停服使用备份。")) return;
  const button = event.currentTarget;
  setBusy(button, true, "正在备份并清空…");
  try {
    const result = await api("/api/admin/clear-knowledge", {
      method: "POST", body: JSON.stringify({confirmation: "CLEAR_ALL_KNOWLEDGE"}),
    });
    window.alert(`知识库已清空，可重新导入资料。\n备份目录：${result.backup_directory}\n外部记忆待清理：${result.pending_memory_retirements} 条`);
    window.location.reload();
  } catch (error) { toast(error.message, true); }
  finally { setBusy(button, false); }
});
