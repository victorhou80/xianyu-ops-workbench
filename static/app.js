const state = {
  accounts: [],
  items: [],
  drafts: [],
  publishJobs: [],
  replyRules: [],
  messages: [],
  deliveryRules: [],
  deliveryJobs: [],
  risks: [],
  audits: [],
  summary: {},
  loginCaptures: {},
  trends: {
    filters: { keyword: "相机", item_id: "", days: 30, bucket: "day" },
    summary: {},
    buckets: [],
    items: [],
    keywords: [],
    notes: [],
  },
};

const titles = {
  dashboard: ["总览", "本地单机版；当前适配器会在总览加载后显示，高风险动作全部走队列和审计。"],
  accounts: ["账号", "管理本地账号、登录态和自动化能力开关。"],
  collector: ["采集统计", "先跑通数据模型和统计面板，后续替换真实采集适配器。"],
  trends: ["趋势分析", "按关键词或单品查看本地历史采集里的价格、销量和热度变化。"],
  publish: ["自动发布", "发布保留为核心功能，但通过草稿、队列、确认和审计执行。"],
  reply: ["自动回复", "关键词、默认回复和后续 AI 回复都在这里管理。"],
  delivery: ["自动发货", "自动发货和自动确认保留，但独立队列、独立开关、完整日志。"],
  logs: ["风险日志", "风控、暂停、失败和高危动作审计集中查看。"],
};

function $(selector) {
  return document.querySelector(selector);
}

function $all(selector) {
  return Array.from(document.querySelectorAll(selector));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.add("show");
  setTimeout(() => box.classList.remove("show"), 2600);
}

function serializeForm(form) {
  const data = {};
  new FormData(form).forEach((value, key) => {
    data[key] = value;
  });
  form.querySelectorAll('input[type="checkbox"]').forEach((item) => {
    data[item.name] = item.checked;
  });
  return data;
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function short(value, len = 44) {
  const text = String(value ?? "");
  if (text.length <= len) return esc(text);
  return `${esc(text.slice(0, len))}...`;
}

function badge(value) {
  const text = String(value ?? "");
  let cls = "info";
  if (["active", "success", "replied", "published"].includes(text)) cls = "ok";
  if (["queued", "running", "paused", "risk_paused", "confirm"].includes(text)) cls = "warn";
  if (["failed", "high"].includes(text)) cls = "bad";
  return `<span class="badge ${cls}">${esc(text)}</span>`;
}

function formatNumber(value, digits = 0) {
  const number = Number(value || 0);
  return number.toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function trendQueryFromForm(form = $("#trendForm")) {
  const data = serializeForm(form);
  return {
    keyword: String(data.keyword || "").trim(),
    item_id: String(data.item_id || "").trim(),
    days: Number(data.days || 30),
    bucket: data.bucket || "day",
  };
}

function trendUrl(filters) {
  const params = new URLSearchParams();
  if (filters.keyword) params.set("keyword", filters.keyword);
  if (filters.item_id) params.set("item_id", filters.item_id);
  params.set("days", String(filters.days || 30));
  params.set("bucket", filters.bucket || "day");
  return `/api/trends?${params.toString()}`;
}

function accountName(id) {
  const account = state.accounts.find((item) => Number(item.id) === Number(id));
  return account ? `${account.id} / ${account.name}` : (id || "-");
}

function fillAccountSelects() {
  $all(".accountSelect").forEach((select) => {
    const current = select.value;
    const allowEmpty = select.querySelector('option[value=""]');
    select.innerHTML = allowEmpty ? '<option value="">全部账号</option>' : "";
    state.accounts.forEach((account) => {
      const option = document.createElement("option");
      option.value = account.id;
      option.textContent = `${account.id} / ${account.name}`;
      select.appendChild(option);
    });
    if ([...select.options].some((option) => option.value === current)) {
      select.value = current;
    }
  });
}

function renderStats() {
  const adapter = state.summary.adapter || "unknown";
  const items = [
    ["账号", state.summary.accounts || 0],
    ["商品", state.summary.items || 0],
    ["适配器", adapter],
    ["发布队列", state.summary.queued_publish_jobs || 0],
    ["消息", state.summary.messages || 0],
    ["风险", state.summary.risk_events || 0],
  ];
  $("#stats").innerHTML = items.map(([label, value]) => `
    <div class="stat"><span>${label}</span><strong>${value}</strong></div>
  `).join("");
}

function renderDashboard() {
  $("#dashboardJobs").innerHTML = state.publishJobs.slice(0, 8).map((job) => `
    <tr>
      <td>${job.id}</td>
      <td>${badge(job.mode)}</td>
      <td>${badge(job.status)}</td>
      <td>${short(job.last_error || job.result_item_id || "-")}</td>
      <td>${short(job.updated_at, 20)}</td>
    </tr>
  `).join("") || emptyRow(5);
  $("#dashboardRisks").innerHTML = state.risks.slice(0, 8).map((risk) => `
    <tr>
      <td>${badge(risk.level)}</td>
      <td>${short(risk.event_type, 20)}</td>
      <td>${short(risk.action_taken, 26)}</td>
      <td>${short(risk.created_at, 20)}</td>
    </tr>
  `).join("") || emptyRow(4);
}

function renderAccounts() {
  $("#accountsTable").innerHTML = state.accounts.map((account) => {
    const abilities = [
      account.auto_publish_enabled ? "发布" : "",
      account.auto_reply_enabled ? "回复" : "",
      account.auto_delivery_enabled ? "发货" : "",
    ].filter(Boolean).join(" / ") || "-";
    const capture = state.loginCaptures[account.id];
    const captureText = capture
      ? `窗口 ${capture.status}，Cookie ${formatNumber(capture.cookie_count)}`
      : "";
    return `
      <tr>
        <td>${account.id}</td>
        <td>${esc(account.name)}<br><span class="muted">${esc(account.platform_user_id || "")}</span></td>
        <td>${badge(account.status)}</td>
        <td>${esc(abilities)}<br><span class="muted">日限额 ${account.daily_publish_limit}，今日 ${account.published_today}</span></td>
        <td>${account.has_login_state ? badge("encrypted") : "-" }<br><span class="muted">${short(account.login_state_hint || "", 24)}</span></td>
        <td>
          <div class="row-actions">
            <button data-login-start="${account.id}">登录向导</button>
            ${capture ? `<button data-login-save="${capture.id}">保存登录态</button><button data-login-close="${capture.id}">关闭窗口</button>` : ""}
          </div>
          ${captureText ? `<span class="muted">${esc(captureText)}</span>` : ""}
        </td>
      </tr>
    `;
  }).join("") || emptyRow(6);
}

function renderItems() {
  $("#itemsTable").innerHTML = state.items.map((item) => `
    <tr>
      <td>${short(item.title, 42)}<br><span class="muted">${esc(item.item_id)}</span></td>
      <td>¥${Number(item.price || 0).toFixed(2)}<br><span class="muted">原 ¥${Number(item.original_price || 0).toFixed(2)}</span></td>
      <td>${esc(item.region || "-")}</td>
      <td>${esc(item.seller_nickname || "-")}<br><span class="muted">${esc(item.seller_id || "")}</span></td>
      <td>${formatNumber(item.want_count)} / ${formatNumber(item.browse_count)} / ${formatNumber(item.sales_volume || item.sold_count)}</td>
      <td><div class="row-actions"><button data-draft-from="${item.id}">生成草稿</button></div></td>
    </tr>
  `).join("") || emptyRow(6);
  $("#analytics").innerHTML = `
    <div class="metric"><span>商品数</span><strong>${state.summary.items || 0}</strong></div>
    <div class="metric"><span>趋势快照</span><strong>${state.summary.market_snapshots || 0}</strong></div>
    <div class="metric"><span>均价</span><strong>¥${Number(state.summary.avg_price || 0).toFixed(2)}</strong></div>
    <div class="metric"><span>活跃账号</span><strong>${state.summary.active_accounts || 0}</strong></div>
    <div class="metric"><span>全局暂停</span><strong>${state.summary.global_kill_switch ? "已开启" : "未开启"}</strong></div>
  `;
}

function renderTrends() {
  const trends = state.trends || {};
  const summary = trends.summary || {};
  const buckets = trends.buckets || [];
  const items = trends.items || [];
  $("#trendMetrics").innerHTML = `
    <div class="metric"><span>样本快照</span><strong>${formatNumber(summary.samples)}</strong></div>
    <div class="metric"><span>商品数</span><strong>${formatNumber(summary.item_count)}</strong></div>
    <div class="metric"><span>均价</span><strong>¥${Number(summary.avg_price || 0).toFixed(2)}</strong></div>
    <div class="metric"><span>最低 / 最高</span><strong>¥${Number(summary.min_price || 0).toFixed(0)} / ¥${Number(summary.max_price || 0).toFixed(0)}</strong></div>
    <div class="metric"><span>含销量字段样本</span><strong>${formatNumber(summary.rows_with_sales)}</strong></div>
    <div class="metric"><span>时间点</span><strong>${formatNumber(buckets.length)}</strong></div>
  `;
  $("#trendKeywords").innerHTML = (trends.keywords || []).map((entry) => `
    <button class="keyword-chip" data-trend-keyword="${esc(entry.keyword)}">
      ${esc(entry.keyword)} <span>${formatNumber(entry.samples)}</span>
    </button>
  `).join("") || `<span class="muted">暂无关键词历史；先运行几次采集。</span>`;
  $("#keywordList").innerHTML = (trends.keywords || []).map((entry) => `<option value="${esc(entry.keyword)}"></option>`).join("");
  $("#trendItemsTable").innerHTML = items.map((item) => `
    <tr>
      <td>${short(item.title, 42)}<br><span class="muted">${esc(item.item_id)}</span></td>
      <td>${formatNumber(item.samples)}</td>
      <td>¥${Number(item.avg_price || 0).toFixed(2)}</td>
      <td>¥${Number(item.min_price || 0).toFixed(0)} - ¥${Number(item.max_price || 0).toFixed(0)}</td>
      <td>${formatNumber(item.want_delta)}</td>
      <td>${formatNumber(item.browse_delta)}</td>
      <td>${formatNumber(item.sales_delta || item.sold_delta)}</td>
      <td>${item.best_rank || "-"}</td>
    </tr>
  `).join("") || emptyRow(8);
  renderTrendChart(buckets);
}

function renderTrendChart(rows) {
  const svg = $("#trendChart");
  if (!rows.length) {
    svg.setAttribute("viewBox", "0 0 860 260");
    svg.innerHTML = `<text x="430" y="132" text-anchor="middle" class="chart-empty">暂无趋势数据，先运行几次采集</text>`;
    return;
  }
  const width = 860;
  const height = 260;
  const pad = { left: 54, right: 24, top: 18, bottom: 34 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const series = [
    { key: "avg_price", cls: "price", label: "均价" },
    { key: "avg_want_count", cls: "want", label: "平均想要" },
    { key: "sales_delta", cls: "sales", label: "销量增量" },
  ];
  const maxValue = Math.max(
    1,
    ...rows.flatMap((row) => series.map((item) => Number(row[item.key] || 0)))
  );
  const x = (index) => pad.left + (rows.length === 1 ? plotW / 2 : (index / (rows.length - 1)) * plotW);
  const y = (value) => pad.top + plotH - (Number(value || 0) / maxValue) * plotH;
  const lines = series.map((item) => {
    const points = rows.map((row, index) => `${x(index).toFixed(1)},${y(row[item.key]).toFixed(1)}`).join(" ");
    return `<polyline class="chart-line ${item.cls}" points="${points}" />`;
  }).join("");
  const dots = series.map((item) => rows.map((row, index) => `
    <circle class="chart-dot ${item.cls}" cx="${x(index).toFixed(1)}" cy="${y(row[item.key]).toFixed(1)}" r="3">
      <title>${esc(row.bucket)} ${item.label}: ${formatNumber(row[item.key], item.key === "avg_price" ? 2 : 0)}</title>
    </circle>
  `).join("")).join("");
  const grid = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {
    const gy = pad.top + plotH - ratio * plotH;
    return `
      <line class="chart-grid" x1="${pad.left}" y1="${gy}" x2="${width - pad.right}" y2="${gy}" />
      <text class="chart-axis" x="${pad.left - 8}" y="${gy + 4}" text-anchor="end">${formatNumber(maxValue * ratio)}</text>
    `;
  }).join("");
  const labelIndexes = rows.length <= 8 ? rows.map((_, index) => index) : [0, Math.floor(rows.length / 2), rows.length - 1];
  const xLabels = labelIndexes.map((index) => `
    <text class="chart-axis" x="${x(index)}" y="${height - 10}" text-anchor="middle">${esc(String(rows[index].bucket).slice(5))}</text>
  `).join("");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = `
    <rect class="chart-bg" x="0" y="0" width="${width}" height="${height}" />
    ${grid}
    <line class="chart-axis-line" x1="${pad.left}" y1="${height - pad.bottom}" x2="${width - pad.right}" y2="${height - pad.bottom}" />
    ${xLabels}
    ${lines}
    ${dots}
  `;
}

function renderPublish() {
  $("#draftsTable").innerHTML = state.drafts.map((draft) => `
    <tr>
      <td>${draft.id}</td>
      <td>${short(draft.title, 38)}<br><span class="muted">${accountName(draft.account_id)}</span></td>
      <td>¥${Number(draft.price || 0).toFixed(2)}</td>
      <td>${badge(draft.status)}</td>
      <td>${short(draft.address || "-")}</td>
      <td>
        <div class="row-actions">
          <button data-queue-confirm="${draft.id}">确认模式</button>
          <button data-queue-auto="${draft.id}">全自动</button>
        </div>
      </td>
    </tr>
  `).join("") || emptyRow(6);
  $("#publishJobsTable").innerHTML = state.publishJobs.map((job) => `
    <tr>
      <td>${job.id}</td>
      <td>${job.draft_id}<br><span class="muted">${accountName(job.account_id)}</span></td>
      <td>${badge(job.mode)}</td>
      <td>${badge(job.status)}</td>
      <td>${short(job.last_error || job.result_item_id || "-")}</td>
      <td>
        <div class="row-actions">
          ${job.status === "paused" ? `<button data-confirm-job="${job.id}">确认执行</button>` : ""}
          ${["queued", "running"].includes(job.status) ? `<button data-pause-job="${job.id}">暂停</button>` : ""}
        </div>
      </td>
    </tr>
  `).join("") || emptyRow(6);
}

function renderReply() {
  $("#replyRulesTable").innerHTML = state.replyRules.map((rule) => `
    <tr>
      <td>${rule.id}</td>
      <td>${rule.account_id ? accountName(rule.account_id) : "全部"}</td>
      <td>${short(rule.keyword, 20)}</td>
      <td>${short(rule.reply_text, 48)}</td>
      <td>${rule.priority}</td>
    </tr>
  `).join("") || emptyRow(5);
  $("#messagesTable").innerHTML = state.messages.map((message) => `
    <tr>
      <td>${short(message.inbound_text, 34)}<br><span class="muted">${esc(message.sender_name || "")}</span></td>
      <td>${short(message.reply_strategy || "-")}</td>
      <td>${short(message.sent_reply || message.generated_reply || "-")}</td>
      <td>${badge(message.status)}</td>
    </tr>
  `).join("") || emptyRow(4);
}

function renderDelivery() {
  $("#deliveryRulesTable").innerHTML = state.deliveryRules.map((rule) => `
    <tr>
      <td>${rule.id}</td>
      <td>${short(rule.keyword, 24)}<br><span class="muted">${accountName(rule.account_id)}</span></td>
      <td>${badge(rule.content_type)}</td>
      <td>${rule.auto_confirm ? badge("enabled") : "-"}</td>
      <td>${rule.cooldown_seconds}s</td>
    </tr>
  `).join("") || emptyRow(5);
  $("#deliveryJobsTable").innerHTML = state.deliveryJobs.map((job) => `
    <tr>
      <td>${job.id}</td>
      <td>${short(job.order_id, 22)}<br><span class="muted">${short(job.item_title, 28)}</span></td>
      <td>${badge(job.status)}</td>
      <td>${badge(job.confirm_status)}</td>
      <td>${short(job.last_error || "-")}</td>
    </tr>
  `).join("") || emptyRow(5);
}

function renderLogs() {
  $("#riskTable").innerHTML = state.risks.map((risk) => `
    <tr>
      <td>${risk.id}</td>
      <td>${risk.account_id || "-"}</td>
      <td>${badge(risk.level)}</td>
      <td>${short(risk.event_type, 20)}</td>
      <td>${short(risk.message, 48)}</td>
      <td>${short(risk.action_taken, 28)}</td>
    </tr>
  `).join("") || emptyRow(6);
  $("#auditTable").innerHTML = state.audits.map((log) => `
    <tr>
      <td>${log.id}</td>
      <td>${short(log.action, 24)}</td>
      <td>${short(log.target_type, 14)} #${esc(log.target_id || "-")}</td>
      <td>${log.account_id || "-"}</td>
      <td>${short(log.created_at, 20)}</td>
    </tr>
  `).join("") || emptyRow(5);
}

function emptyRow(colspan) {
  return `<tr><td colspan="${colspan}" class="muted">暂无数据</td></tr>`;
}

function renderAll() {
  fillAccountSelects();
  renderStats();
  renderDashboard();
  renderAccounts();
  renderItems();
  renderTrends();
  renderPublish();
  renderReply();
  renderDelivery();
  renderLogs();
  $("#killSwitch").checked = Boolean(state.summary.global_kill_switch);
  if ($(".nav.active")?.dataset.tab === "dashboard") {
    $("#pageHint").textContent = `本地单机版，当前适配器：${state.summary.adapter || "unknown"}；高风险动作全部走队列和审计。`;
  }
}

async function loadAll() {
  const [
    summary,
    accounts,
    items,
    drafts,
    publishJobs,
    replyRules,
    messages,
    deliveryRules,
    deliveryJobs,
    risks,
    audits,
    trends,
  ] = await Promise.all([
    api("/api/summary"),
    api("/api/accounts"),
    api("/api/items"),
    api("/api/publish-drafts"),
    api("/api/publish-jobs"),
    api("/api/reply-rules"),
    api("/api/messages"),
    api("/api/delivery-rules"),
    api("/api/delivery-jobs"),
    api("/api/risk-events"),
    api("/api/audit-logs"),
    api(trendUrl(state.trends?.filters || { keyword: "相机", days: 30, bucket: "day" })),
  ]);
  Object.assign(state, {
    summary,
    accounts,
    items,
    drafts,
    publishJobs,
    replyRules,
    messages,
    deliveryRules,
    deliveryJobs,
    risks,
    audits,
    trends,
  });
  renderAll();
}

async function loadTrends(filters = trendQueryFromForm()) {
  state.trends = await api(trendUrl(filters));
  renderTrends();
}

async function refreshLoginCapture(accountId) {
  const capture = state.loginCaptures[accountId];
  if (!capture) return;
  try {
    state.loginCaptures[accountId] = await api(`/api/login-capture/${capture.id}/status`, {
      method: "POST",
      body: "{}",
    });
    renderAccounts();
  } catch (error) {
    delete state.loginCaptures[accountId];
    renderAccounts();
  }
}

async function submitJson(form, path, mapper = (x) => x) {
  const payload = mapper(serializeForm(form));
  await api(path, { method: "POST", body: JSON.stringify(payload) });
  form.reset();
  await loadAll();
  toast("已保存");
}

function bindTabs() {
  $all(".nav").forEach((button) => {
    button.addEventListener("click", () => {
      const tab = button.dataset.tab;
      $all(".nav").forEach((item) => item.classList.toggle("active", item === button));
      $all(".tab").forEach((item) => item.classList.toggle("active", item.id === tab));
      $("#pageTitle").textContent = titles[tab][0];
      $("#pageHint").textContent = titles[tab][1];
    });
  });
}

function bindForms() {
  $("#refresh").addEventListener("click", () => loadAll().then(() => toast("已刷新")));
  $("#seedDemo").addEventListener("click", async () => {
    await api("/api/seed", { method: "POST", body: "{}" });
    await loadAll();
    toast("演示数据已生成");
  });
  $("#killSwitch").addEventListener("change", async (event) => {
    await api("/api/settings", {
      method: "PATCH",
      body: JSON.stringify({ global_kill_switch: event.target.checked }),
    });
    await loadAll();
    toast(event.target.checked ? "全局暂停已开启" : "全局暂停已关闭");
  });
  $("#accountForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitJson(event.currentTarget, "/api/accounts", (data) => ({
      ...data,
      daily_publish_limit: Number(data.daily_publish_limit || 5),
    }));
  });
  $("#collectorForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = serializeForm(event.currentTarget);
    await api("/api/collector/run", {
      method: "POST",
      body: JSON.stringify({
        account_id: data.account_id ? Number(data.account_id) : null,
        keyword: data.keyword,
        limit: Number(data.limit || 6),
      }),
    });
    await loadAll();
    toast("采集完成");
  });
  $("#trendForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await loadTrends(trendQueryFromForm(event.currentTarget));
    toast("趋势已更新");
  });
  $("#draftForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitJson(event.currentTarget, "/api/publish-drafts", (data) => ({
      ...data,
      account_id: Number(data.account_id),
      price: Number(data.price),
      images: [],
    }));
  });
  $("#replyRuleForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitJson(event.currentTarget, "/api/reply-rules", (data) => ({
      ...data,
      account_id: data.account_id ? Number(data.account_id) : null,
      priority: Number(data.priority || 10),
    }));
  });
  $("#messageForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitJson(event.currentTarget, "/api/messages/simulate", (data) => ({
      ...data,
      account_id: Number(data.account_id),
    }));
  });
  $("#deliveryRuleForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitJson(event.currentTarget, "/api/delivery-rules", (data) => ({
      ...data,
      account_id: Number(data.account_id),
      cooldown_seconds: Number(data.cooldown_seconds || 600),
    }));
  });
  $("#deliveryJobForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitJson(event.currentTarget, "/api/delivery-jobs/simulate", (data) => ({
      ...data,
      account_id: Number(data.account_id),
    }));
  });
}

function bindDelegatedActions() {
  document.body.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.draftFrom) {
      const item = state.items.find((entry) => Number(entry.id) === Number(button.dataset.draftFrom));
      const account = state.accounts[0];
      if (!item || !account) return toast("需要先有账号和商品");
      await api("/api/publish-drafts", {
        method: "POST",
        body: JSON.stringify({
          account_id: account.id,
          source_item_id: item.item_id,
          title: item.title,
          description: `转发布草稿：${item.title}\n来源价格：${item.price}`,
          price: item.price,
          address: item.region || "",
          images: [],
        }),
      });
      await loadAll();
      toast("已从商品生成草稿");
    }
    if (button.dataset.loginStart) {
      const accountId = Number(button.dataset.loginStart);
      const capture = await api(`/api/accounts/${accountId}/login-capture/start`, { method: "POST", body: "{}" });
      state.loginCaptures[accountId] = capture;
      renderAccounts();
      toast("登录窗口已打开，完成登录后点保存登录态");
    }
    if (button.dataset.loginSave) {
      const sessionId = button.dataset.loginSave;
      await api(`/api/login-capture/${sessionId}/save`, { method: "POST", body: "{}" });
      Object.keys(state.loginCaptures).forEach((accountId) => {
        if (state.loginCaptures[accountId].id === sessionId) delete state.loginCaptures[accountId];
      });
      await loadAll();
      toast("登录态已保存");
    }
    if (button.dataset.loginClose) {
      const sessionId = button.dataset.loginClose;
      await api(`/api/login-capture/${sessionId}/close`, { method: "POST", body: "{}" });
      Object.keys(state.loginCaptures).forEach((accountId) => {
        if (state.loginCaptures[accountId].id === sessionId) delete state.loginCaptures[accountId];
      });
      renderAccounts();
      toast("登录窗口已关闭");
    }
    if (button.dataset.trendKeyword) {
      const form = $("#trendForm");
      form.elements.keyword.value = button.dataset.trendKeyword;
      form.elements.item_id.value = "";
      await loadTrends(trendQueryFromForm(form));
      toast("已切换关键词趋势");
    }
    if (button.dataset.queueConfirm || button.dataset.queueAuto) {
      const draftId = Number(button.dataset.queueConfirm || button.dataset.queueAuto);
      const draft = state.drafts.find((entry) => Number(entry.id) === draftId);
      if (!draft) return;
      const mode = button.dataset.queueAuto ? "auto" : "confirm";
      await api("/api/publish-jobs", {
        method: "POST",
        body: JSON.stringify({ draft_id: draftId, account_id: draft.account_id, mode }),
      });
      await loadAll();
      toast(mode === "auto" ? "已进入全自动发布队列" : "已进入确认发布队列");
    }
    if (button.dataset.confirmJob) {
      await api(`/api/publish-jobs/${button.dataset.confirmJob}/confirm`, { method: "POST", body: "{}" });
      await loadAll();
      toast("发布任务已确认执行");
    }
    if (button.dataset.pauseJob) {
      await api(`/api/publish-jobs/${button.dataset.pauseJob}/pause`, { method: "POST", body: "{}" });
      await loadAll();
      toast("发布任务已暂停");
    }
  });
}

bindTabs();
bindForms();
bindDelegatedActions();
loadAll().catch((error) => toast(error.message));
setInterval(() => {
  loadAll().catch(() => {});
}, 3000);
setInterval(() => {
  Object.keys(state.loginCaptures).forEach((accountId) => refreshLoginCapture(accountId));
}, 5000);
