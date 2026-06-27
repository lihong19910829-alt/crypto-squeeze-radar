const state = {
  summary: null,
  history: [],
  tweets: [],
  xPreview: null,
  selectedSymbol: null,
  chartRange: "24",
};

const $ = (selector) => document.querySelector(selector);

document.addEventListener("DOMContentLoaded", () => {
  $("#refreshBtn").addEventListener("click", handleRefresh);
  $("#symbolSelect").addEventListener("change", (event) => {
    selectSymbol(event.target.value);
  });
  $("#riskBars").addEventListener("click", (event) => {
    const row = event.target.closest("[data-symbol]");
    if (row) selectSymbol(row.dataset.symbol, { scrollToChart: true });
  });
  document.querySelectorAll("[data-range]").forEach((button) => {
    button.addEventListener("click", () => {
      state.chartRange = button.dataset.range;
      renderRangeTabs();
      renderChart();
    });
  });
  loadDashboard();
});

function handleRefresh() {
  if (window.location.protocol === "file:") {
    window.location.reload();
    return;
  }
  loadDashboard({ forceRefresh: true });
}

async function loadDashboard(options = {}) {
  const embedded = options.forceRefresh ? await loadStaticData() : window.RADAR_DATA;
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
        fetchJson("/api/history?limit=5000"),
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

async function loadStaticData() {
  try {
    const response = await fetch(`./data.js?t=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`data.js ${response.status}`);
    const text = await response.text();
    const jsonText = text
      .replace(/^window\.RADAR_DATA\s*=\s*/, "")
      .replace(/;\s*$/, "");
    const parsed = JSON.parse(jsonText);
    window.RADAR_DATA = parsed;
    return parsed;
  } catch (error) {
    console.warn("Static data refresh failed, using embedded data", error);
    return window.RADAR_DATA;
  }
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
  renderRangeTabs();
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
  $("#xMode").textContent = summary.x_preview?.post_to_x ? "Live" : "只读";
  $("#xThreshold").textContent = `阈值 ${summary.x_preview?.min_risk_score ?? 70}`;
  const isLocalFile = window.location.protocol === "file:";
  $("#refreshBtn").textContent = isLocalFile ? "重载本地数据" : "刷新数据";
  $("#postState").textContent = isLocalFile ? "本地静态数据" : summary.x_preview?.post_to_x ? "X 发布开启" : "只读监控";
  $("#postState").title = isLocalFile
    ? "当前直接打开本地页面；点击按钮会重新读取磁盘上的 data.js。"
    : "当前通过本地服务读取数据。";

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
          <td>${formatFirstAlert(item.first_alert_at)}</td>
          <td>${formatNumber(item.first_alert_price, 4)}</td>
          <td>${renderAlertMove(item)}</td>
          <td>${renderOutcome(item.outcome_probability)}</td>
        </tr>
      `;
    })
    .join("");
}

function renderRiskBars() {
  const rows = state.summary?.coins || [];
  $("#riskBars").innerHTML =
    rows
      .map((item) => {
        const score = item.risk_score || 0;
        const symbol = item.symbol || "";
        const active = symbol === state.selectedSymbol ? " active" : "";
        return `
          <button class="bar-row${active}" type="button" data-symbol="${escapeHtml(symbol)}" aria-pressed="${active ? "true" : "false"}">
            <strong>${escapeHtml(item.coin || coinFromSymbol(item.symbol))}</strong>
            <div class="bar-track"><div class="bar-fill ${scoreTone(score)}" style="width:${score}%"></div></div>
            <span>${score}</span>
          </button>
        `;
      })
      .join("") || `<div class="empty">暂无风险评分数据</div>`;
}

function renderSymbolOptions() {
  const symbols = [
    ...new Set([
      ...(state.summary?.coins || []).map((item) => item.symbol),
      ...state.history.map((item) => item.symbol),
    ]),
  ].filter(Boolean);
  if (!symbols.includes(state.selectedSymbol)) state.selectedSymbol = symbols[0] || "";
  $("#symbolSelect").innerHTML = symbols
    .map((symbol) => `<option value="${escapeHtml(symbol)}" ${symbol === state.selectedSymbol ? "selected" : ""}>${escapeHtml(symbol)}</option>`)
    .join("");
}

function selectSymbol(symbol, options = {}) {
  if (!symbol || symbol === state.selectedSymbol) return;
  state.selectedSymbol = symbol;
  const select = $("#symbolSelect");
  if (select) select.value = symbol;
  renderRiskBars();
  renderChart();
  if (options.scrollToChart) $("#history")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderRangeTabs() {
  document.querySelectorAll("[data-range]").forEach((button) => {
    const active = button.dataset.range === state.chartRange;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function renderChart() {
  const svg = $("#riskChart");
  const allRows = state.history
    .filter((item) => item.symbol === state.selectedSymbol)
    .sort((a, b) => new Date(a.timestamp_utc) - new Date(b.timestamp_utc));
  const rows = filterRowsByRange(allRows);
  if (!rows.length) {
    svg.innerHTML = `<text x="380" y="160" text-anchor="middle" fill="#687386">暂无历史趋势数据</text>`;
    return;
  }

  const width = 760;
  const height = 320;
  const pad = { left: 44, right: 18, top: 18, bottom: 58 };
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
  const tickPoints = getTimeTicks(points);
  const xTicks = tickPoints
    .map((point, index) => {
      const y1 = pad.top;
      const y2 = height - pad.bottom;
      const anchor = index === 0 ? "start" : index === tickPoints.length - 1 ? "end" : "middle";
      return `
        <line class="grid-line vertical" x1="${point.x}" x2="${point.x}" y1="${y1}" y2="${y2}"></line>
        <text class="x-tick" x="${point.x}" y="${height - 32}" text-anchor="${anchor}">${formatAxisTime(point.item.timestamp_utc)}</text>
      `;
    })
    .join("");
  const dots = points
    .map((point) => `<circle class="dot" cx="${point.x}" cy="${point.y}" r="4"><title>${formatTime(point.item.timestamp_utc)}: ${point.item.risk_score}</title></circle>`)
    .join("");

  svg.innerHTML = `
    ${grid}
    ${xTicks}
    <line class="axis" x1="${pad.left}" x2="${width - pad.right}" y1="${height - pad.bottom}" y2="${height - pad.bottom}"></line>
    <line class="axis" x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${height - pad.bottom}"></line>
    <path class="line" d="${path}"></path>
    ${dots}
    <text x="${pad.left}" y="${height - 10}" fill="#687386" font-size="12">${escapeHtml(state.selectedSymbol)} · ${rows.length}/${allRows.length} 条</text>
  `;
}

function filterRowsByRange(rows) {
  if (!rows.length || state.chartRange === "all") return rows;
  const hours = Number(state.chartRange);
  const latest = getLatestTimestamp(rows);
  if (!latest || !Number.isFinite(hours)) return rows;
  const cutoff = latest - hours * 60 * 60 * 1000;
  const scoped = rows.filter((item) => new Date(item.timestamp_utc).getTime() >= cutoff);
  return scoped.length >= 2 ? scoped : rows.slice(-Math.min(rows.length, 12));
}

function getLatestTimestamp(rows) {
  return rows.reduce((latest, item) => {
    const value = new Date(item.timestamp_utc).getTime();
    return Number.isFinite(value) ? Math.max(latest, value) : latest;
  }, 0);
}

function getTimeTicks(points) {
  if (points.length <= 4) return points;
  const indexes = [0, Math.round((points.length - 1) / 3), Math.round(((points.length - 1) * 2) / 3), points.length - 1];
  return [...new Set(indexes)].map((index) => points[index]);
}

function renderHeatCards() {
  const rows = state.summary?.coins || [];
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

function renderOutcome(outcome) {
  if (!outcome) return `<span class="muted">待积累</span>`;
  return `
    <span class="outcome-label">${escapeHtml(outcome.direction || "中性观察")}</span>
    <small>${escapeHtml(outcome.horizon || "1-6h")} · 涨 ${outcome.up_probability ?? "--"}% / 跌 ${outcome.down_probability ?? "--"}%</small>
  `;
}

function renderAlertMove(item) {
  const gain = item.max_gain_since_first_alert_pct;
  const drawdown = item.max_drawdown_since_first_alert_pct;
  if (gain === null || gain === undefined || drawdown === null || drawdown === undefined) {
    return `<span class="muted">--</span>`;
  }
  return `
    <span class="move-up">最高 ${formatSignedPercent(gain)}</span>
    <small>最低 ${formatSignedPercent(drawdown)}</small>
  `;
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

function formatAxisTime(value) {
  if (!value) return "--";
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

function formatFirstAlert(value) {
  if (!value) return "--";
  return formatTime(value);
}

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined) return "N/A";
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function formatPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
  return `${Number(value).toFixed(2)}%`;
}

function formatSignedPercent(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
  const number = Number(value);
  return `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`;
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
