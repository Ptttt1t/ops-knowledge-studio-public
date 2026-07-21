const pageTitles = {
  dashboard: "知识能力概览",
  ingest: "知识采集与加工",
  review: "知识审核队列",
  query: "可信方案生成",
  library: "知识资产库",
};

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
}));

document.getElementById("refresh-button").addEventListener("click", refreshAll);
document.getElementById("library-search").addEventListener("click", () => loadLibrary().catch(error => toast(error.message, true)));
document.getElementById("dialog-close").addEventListener("click", () => document.getElementById("detail-dialog").close());

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

refreshAll();
