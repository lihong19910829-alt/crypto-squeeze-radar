const state = {
  summary: null,
  history: [],
  tweets: [],
  xPreview: null,
  selectedSymbol: null,
};

const $ = (selector) => document.querySelector(selector);

document.addEventListener("DOMContentLoaded", () => {
  $("#refreshBtn").addEventListener("click", loadDashboard);
  $("#symbolSelect").addEventListener("change", (event) => {
    state.selectedSymbol = event.target.value;
    renderChart();
  });
  loadDashboard();
});

async function loadDashboard() {
  const embedded = window.RADAR_DATA;
  let summary;
  let history;
  let tweets;
  let xPreview;

  if (embedded) {
    ({ summary, history, tweets, xPreview } = embedded);
  } else {
    try {
      [summary, history, tweets, xPreview] = await Promise.all([
        fetchJson("/api/summary"),
        fetchJson("/api/history?limit=320"),
        fetchJson("/api/tweets"),
        fetchJson("/api/x-preview"),
      ]);
    } catch (error) {
      showLoadError(error);
      return;
    }
  }

  state.summary = summary;
  state.history = history;
  state.tweets = tweets;
  state.xPreview = xPreview;
  state.selectedSymbol = state.selectedSymbol || summary.top?.[0]?.symbol || history[0]?.symbol || "";
  renderAll();
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) throw new Error(`请求失败：${url}`);
  return response.json();
}

function showLoadError(error) {
  $("#lastUpdated").textContent = "数据读取失败，请先运行 python main.py 和 python export_dashboard_data.py";
  $("#signalRows").innerHTML = "";
  $("#riskBars").innerHTML = `<div class="empty">${escapeHtml(error.message)}</div>`;
  $("#tweetList").innerHTML = `<div class="empty">暂无可展示数据</div>`;
  $("#xPreview").innerHTML = `<div class="empty">暂无 X 预览数据</div>`;
}

function renderAll() {
  renderSummary();
  renderSignals();
  renderRiskBars();
  renderSymbolOptions();
  renderChart();
  renderHeatCards();
  renderTweets();
  renderXPreview();
}

function renderSummary() {
  const summary = state.summary || {};
  $("#lastUpdated").textContent = summary.last_updated
    ? `最新数据：${formatTime(summary.last_updated)}`
    : "暂无监控数据";
  $("#maxRisk").textContent = summary.max_risk ?? "--";
  $("#publishCandidates").textContent = summary.publish_candidates ?? "--";
  $("#tweetCount").textContent = summary.tweet_count ?? "--";
  $("#xMode").textContent = summary.x_preview?.post_to_x ? "Live" : "Dry run";
  $("#xThreshold").textContent = `阈值 ${summary.x_preview?.min_risk_score ?? 70}`;
  $("#postState").textContent = summary.x_preview?.post_to_x ? "Live posting" : "Dry run";

  const coinCount = summary.coins?.length ?? 0;
  $("#radarCoinCount").textContent = coinCount || "--";
  $("#radarPulseValue").textContent = summary.max_risk ?? "--";
  $("#radarCandidateCount").textContent = summary.publish_candidates ?? "--";
  $("#radarSummary").textContent = summary.last_updated
    ? `已同步 ${coinCount} 个交易对，当前最高风险评分 ${summary.max_risk ?? "--"}，仅展示异常监控与风险提示。`
    : "正在同步最新风险评分、OI 变化和清算数据。";
}

function renderSignals() {
  const rows = state.summary?.top || [];
  $("#signalRows").innerHTML = rows
    .map((item) => {
      const scoreClass = scoreTone(item.risk_score);
      return `
        <tr>
          <td><strong>${escapeHtml(item.coin || coinFromSymbol(item.symbol))}</strong><br><small>${escapeHtml(item.symbol)}</small></td>
          <td><span class="score ${scoreClass}">${item.risk_score ?? 0}</span></td>
          <td>${renderTags(item.anomaly_tag)}</td>
          <td>${formatNumber(item.price, 4)}</td>
          <td>${formatPercent((item.funding_rate || 0) * 100)}</td>
          <td>${formatPercent(item.oi_change_1h)}</td>
          <td>${formatPercent(item.oi_change_24h)}</td>
        </tr>
      `;
    })
    .join("");
}

function renderRiskBars() {
  const rows = (state.summary?.coins || []).slice(0, 20);
  $("#riskBars").innerHTML =
    rows
      .map((item) => {
        const score = item.risk_score || 0;
        return `
          <div class="bar-row">
            <strong>${escapeHtml(item.coin || coinFromSymbol(item.symbol))}</strong>
            <div class="bar-track"><div class="bar-fill ${scoreTone(score)}" style="width:${score}%"></div></div>
            <span>${score}</span>
          </div>
        `;
      })
      .join("") || `<div class="empty">暂无风险评分数据</div>`;
}

function renderSymbolOptions() {
  const symbols = [...new Set(state.history.map((item) => item.symbol))].filter(Boolean);
  if (!symbols.includes(state.selectedSymbol)) state.selectedSymbol = symbols[0] || "";
  $("#symbolSelect").innerHTML = symbols
    .map((symbol) => `<option value="${escapeHtml(symbol)}" ${symbol === state.selectedSymbol ? "selected" : ""}>${escapeHtml(symbol)}</option>`)
    .join("");
}

function renderChart() {
  const svg = $("#riskChart");
  const rows = state.history.filter((item) => item.symbol === state.selectedSymbol);
  if (!rows.length) {
    svg.innerHTML = `<text x="380" y="140" text-anchor="middle" fill="#687386">暂无历史趋势数据</text>`;
    return;
  }

  const width = 760;
  const height = 280;
  const pad = { left: 44, right: 18, top: 18, bottom: 34 };
  const innerW = width - pad.left - pad.right;
  const innerH = height - pad.top - pad.bottom;
  const maxY = Math.max(100, ...rows.map((item) => item.risk_score || 0));
  const points = rows.map((item, index) => {
    const x = pad.left + (rows.length === 1 ? innerW / 2 : (index / (rows.length - 1)) * innerW);
    const y = pad.top + innerH - ((item.risk_score || 0) / maxY) * innerH;
    return { x, y, item };
  });
  const path = points.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x} ${point.y}`).join(" ");
  const grid = [0, 25, 50, 75, 100]
    .map((tick) => {
      const y = pad.top + innerH - (tick / 100) * innerH;
      return `<line class="grid-line" x1="${pad.left}" x2="${width - pad.right}" y1="${y}" y2="${y}"></line><text x="12" y="${y + 4}" fill="#687386" font-size="12">${tick}</text>`;
    })
    .join("");
  const dots = points
    .map((point) => `<circle class="dot" cx="${point.x}" cy="${point.y}" r="4"><title>${formatTime(point.item.timestamp_utc)}: ${point.item.risk_score}</title></circle>`)
    .join("");

  svg.innerHTML = `
    ${grid}
    <line class="axis" x1="${pad.left}" x2="${width - pad.right}" y1="${height - pad.bottom}" y2="${height - pad.bottom}"></line>
    <line class="axis" x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${height - pad.bottom}"></line>
    <path class="line" d="${path}"></path>
    ${dots}
    <text x="${pad.left}" y="${height - 8}" fill="#687386" font-size="12">${escapeHtml(state.selectedSymbol)}</text>
  `;
}

function renderHeatCards() {
  const rows = (state.summary?.coins || []).slice(0, 12);
  $("#heatCards").innerHTML =
    rows
      .map(
        (item) => `
        <div class="heat-card">
          <strong>${escapeHtml(item.symbol)}</strong>
          <div class="mini-stats">
            <span>Funding ${formatPercent((item.funding_rate || 0) * 100)}</span>
            <span>OI ${formatCompact(item.open_interest)}</span>
            <span>多头清算 ${formatUsd(item.long_liquidation)}</span>
            <span>空头清算 ${formatUsd(item.short_liquidation)}</span>
          </div>
        </div>
      `,
      )
      .join("") || `<div class="empty">暂无杠杆热度数据</div>`;
}

function renderTweets() {
  $("#tweetList").innerHTML =
    state.tweets
      .map(
        (tweet) => `
        <div class="tweet-card">
          <strong>${escapeHtml(tweet.coin)} · ${tweet.risk_score}/100 · ${escapeHtml(tweet.risk_level)}</strong>
          <pre>${escapeHtml(tweet.tweet)}</pre>
        </div>
      `,
      )
      .join("") || `<div class="empty">暂无推文草稿</div>`;
}

function renderXPreview() {
  const preview = state.xPreview || {};
  const items = preview.items || [];
  $("#xPreview").innerHTML =
    `
      <div class="preview-card">
        <strong>${preview.post_to_x ? "真实发布开关开启" : "Dry run 预览"}</strong>
        <span>最低评分：${preview.min_risk_score ?? 70} · 候选：${items.length}</span>
      </div>
    ` +
    (items.length
      ? items
          .map(
            (item) => `
            <div class="preview-card">
              <strong>${escapeHtml(item.coin)} · ${escapeHtml(item.status)}</strong>
              <span>${escapeHtml(item.error || "已通过发布前检查")}</span>
            </div>
          `,
          )
          .join("")
      : `<div class="empty">本轮没有达到自动发布阈值的信号</div>`);
}

function renderTags(value) {
  return String(value || "正常")
    .split("、")
    .filter(Boolean)
    .map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`)
    .join("");
}

function scoreTone(score) {
  if (score >= 81) return "extreme";
  if (score >= 61) return "danger";
  return "";
}

function coinFromSymbol(symbol = "") {
  return symbol.replace("USDT", "");
}

function formatTime(value) {
  if (!value) return "--";
  return new Date(value).toLocaleString("zh-CN", { hour12: false });
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined) return "N/A";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
  return `${Number(value).toFixed(2)}%`;
}

function formatUsd(value) {
  if (value === null || value === undefined) return "N/A";
  return `$${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 0 })}`;
}

function formatCompact(value) {
  if (value === null || value === undefined) return "N/A";
  return Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 2 }).format(Number(value));
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}
