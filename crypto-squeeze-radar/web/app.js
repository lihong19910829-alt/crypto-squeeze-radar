const state = {
  summary: null,
  history: [],
  patterns: {},
  tweets: [],
  xPreview: null,
  selectedSymbol: null,
  chartRange: "24",
  opportunityFilter: "all",
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
  document.querySelectorAll("[data-opportunity-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.opportunityFilter = button.dataset.opportunityFilter;
      renderOpportunityFilterTabs();
      renderOpportunityScanner();
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
  let patterns;
  let tweets;
  let xPreview;

  if (embedded) {
    ({ summary, history, patterns, tweets, xPreview } = embedded);
  } else {
    try {
      [summary, history, patterns, tweets, xPreview] = await Promise.all([
        fetchJson("/api/summary"),
        fetchJson("/api/history?limit=5000"),
        fetchJson("/api/patterns"),
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
  state.patterns = hasPatternPayload(patterns) ? patterns : summary.patterns || {};
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
  renderOpportunityFilterTabs();
  renderOpportunityScanner();
  renderSignals();
  renderPatterns();
  renderRiskBars();
  renderSymbolOptions();
  renderRangeTabs();
  renderChart();
  renderHeatCards();
  renderTweets();
  renderXPreview();
}

function renderOpportunityFilterTabs() {
  document.querySelectorAll("[data-opportunity-filter]").forEach((button) => {
    const active = button.dataset.opportunityFilter === state.opportunityFilter;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}

function renderOpportunityScanner() {
  const rows = (state.summary?.top || state.summary?.coins || [])
    .slice(0, 20)
    .map((item, index) => buildOpportunityRow(item, index + 1));
  const filteredRows =
    state.opportunityFilter === "all"
      ? rows
      : rows.filter((item) => item.direction === state.opportunityFilter);

  $("#opportunityBtcRegime").textContent = state.summary?.btc_regime || "待接入";
  $("#opportunityBtcSummary").textContent =
    state.summary?.btc_summary ||
    "当前后端还没有输出 BTC 4H / 1H 大环境字段；栏目先按现有异常数据展示候选，后续接入完整短线扫描器。";

  $("#opportunityRows").innerHTML =
    filteredRows
      .map(
        (item) => `
          <tr>
            <td>${item.rank}</td>
            <td><strong>${escapeHtml(item.coin)}</strong><br><small>${escapeHtml(item.symbol)}</small></td>
            <td>${renderDirection(item.direction)}</td>
            <td><span class="score ${scoreTone(item.score)}">${item.score}</span></td>
            <td>${formatNumber(item.currentPrice, 6)}</td>
            <td>${formatSignedPercent(item.priceChange24h)}</td>
            <td>${formatUsd(item.quoteVolume24h)}</td>
            <td>${escapeHtml(item.structure)}</td>
            <td>${escapeHtml(item.volumeSignal)}</td>
            <td>${escapeHtml(item.oiSignal)}</td>
            <td>${escapeHtml(item.fundingSignal)}</td>
            <td>
              <strong>${escapeHtml(item.entryZone)}</strong>
              <small>止损 ${escapeHtml(item.stopLoss)} · 目标 ${escapeHtml(item.target1)} / ${escapeHtml(item.target2)}</small>
            </td>
            <td>${renderList(item.reasons)}</td>
            <td>${renderList(item.risks)}</td>
            <td><span class="status-chip">${escapeHtml(item.status)}</span></td>
          </tr>
        `,
      )
      .join("") || `<tr><td colspan="15"><div class="empty">当前筛选条件下暂无候选；完整扫描器接入后会保留剔除原因。</div></td></tr>`;
}

function buildOpportunityRow(item, rank) {
  const direction = inferOpportunityDirection(item);
  const currentPrice = Number(item.price || 0);
  const priceChange24h = Number(item.price_change_24h || 0);
  const quoteVolume24h = Number(item.quote_volume_24h || 0);
  const oi1h = Number(item.oi_change_1h || 0);
  const oi24h = Number(item.oi_change_24h || 0);
  const fundingPct = Number(item.funding_rate || 0) * 100;
  const riskScore = Number(item.risk_score || 0);
  const score = Math.max(0, Math.min(100, riskScore));
  const entryZone = buildEntryZone(currentPrice, direction);
  const stopLoss = buildStopLoss(currentPrice, direction);
  const target1 = buildTarget(currentPrice, direction, 1);
  const target2 = buildTarget(currentPrice, direction, 2);

  return {
    rank,
    symbol: item.symbol || "",
    coin: item.coin || coinFromSymbol(item.symbol),
    direction,
    score,
    currentPrice,
    priceChange24h,
    quoteVolume24h,
    structure: inferStructureLabel(direction, priceChange24h, item.price_position_24h),
    volumeSignal: inferVolumeSignal(item.quote_volume_change_24h),
    oiSignal: inferOiSignal(direction, oi1h, oi24h),
    fundingSignal: inferFundingSignal(direction, fundingPct),
    entryZone,
    stopLoss,
    target1,
    target2,
    reasons: buildOpportunityReasons(item, direction),
    risks: buildOpportunityRisks(item, direction),
    status: inferOpportunityStatus(direction, score, fundingPct),
  };
}

function inferOpportunityDirection(item) {
  const action = item.outcome_probability?.trade_action;
  if (action === "做多") return "LONG";
  if (action === "做空") return "SHORT";
  const fundingPct = Number(item.funding_rate || 0) * 100;
  const priceChange1h = Number(item.price_change_1h || 0);
  const oi1h = Number(item.oi_change_1h || 0);
  if (fundingPct <= -0.08 && oi1h >= 5) return "SHORT";
  if (fundingPct >= 0.08 && oi1h >= 5) return "LONG";
  if (priceChange1h > 0 && oi1h > 0 && Math.abs(fundingPct) < 0.05) return "LONG";
  if (priceChange1h < 0 && oi1h > 0 && Math.abs(fundingPct) < 0.05) return "SHORT";
  return "WATCH";
}

function inferStructureLabel(direction, priceChange24h, position24h) {
  const position = Number(position24h || 0);
  if (direction === "LONG") return position > 70 ? "15m强势 / 1H偏多" : "等待突破确认";
  if (direction === "SHORT") return position < 35 || priceChange24h < 0 ? "15m偏弱 / 1H承压" : "上涨后去杠杆观察";
  return "结构待确认";
}

function inferVolumeSignal(value) {
  const change = Number(value || 0);
  if (change >= 100) return "成交额显著放大";
  if (change >= 20) return "成交额温和放大";
  if (change <= -30) return "成交额收缩";
  return "量能一般";
}

function inferOiSignal(direction, oi1h, oi24h) {
  const label = `OI 1h ${formatSignedPercent(oi1h)} / 24h ${formatSignedPercent(oi24h)}`;
  if (Math.abs(oi1h) >= 8) return `${label}，杠杆变化较快`;
  if (direction === "LONG" && oi1h > 0) return `${label}，新多观察`;
  if (direction === "SHORT" && oi1h > 0) return `${label}，新空观察`;
  return label;
}

function inferFundingSignal(direction, fundingPct) {
  const label = `Funding ${formatSignedPercent(fundingPct)}`;
  if (Math.abs(fundingPct) >= 0.1) return `${label}，拥挤`;
  if (direction === "LONG" && fundingPct > 0) return `${label}，偏温和`;
  if (direction === "SHORT" && fundingPct < 0) return `${label}，偏温和`;
  return `${label}，中性`;
}

function buildOpportunityReasons(item, direction) {
  const reasons = [];
  if (item.anomaly_tag) reasons.push(item.anomaly_tag);
  if (item.outcome_probability?.basis) reasons.push(item.outcome_probability.basis);
  if (direction !== "WATCH") reasons.push(`${direction} 方向来自现有后验概率和 OI/Funding 映射`);
  return reasons.length ? reasons.slice(0, 3) : ["等待完整扫描器输出结构、量能和风险收益比拆解"];
}

function buildOpportunityRisks(item, direction) {
  const risks = [];
  if (Math.abs(Number(item.funding_rate || 0) * 100) >= 0.1) risks.push("Funding 较极端，可能出现反向挤压");
  if (Math.abs(Number(item.price_change_24h || 0)) >= 20) risks.push("24h 波动较大，追单风险偏高");
  if (direction === "WATCH") risks.push("结构或方向条件不足，暂不标记为交易计划");
  return risks.length ? risks.slice(0, 3) : ["BTC 大环境字段未接入，需以后端完整扫描结果为准"];
}

function inferOpportunityStatus(direction, score, fundingPct) {
  if (direction === "WATCH") return "观察";
  if (Math.abs(fundingPct) >= 0.1) return "风险过热";
  if (score >= 80) return direction === "LONG" ? "等待回踩" : "等待反抽";
  return "可关注";
}

function buildEntryZone(price, direction) {
  if (!price) return "待计算";
  const low = direction === "SHORT" ? price * 0.998 : price * 0.995;
  const high = direction === "SHORT" ? price * 1.005 : price * 1.002;
  return `${formatNumber(low, 6)} - ${formatNumber(high, 6)}`;
}

function buildStopLoss(price, direction) {
  if (!price || direction === "WATCH") return "待结构确认";
  const value = direction === "SHORT" ? price * 1.018 : price * 0.982;
  return formatNumber(value, 6);
}

function buildTarget(price, direction, level) {
  if (!price || direction === "WATCH") return "待结构确认";
  const distance = level === 1 ? 0.018 : 0.036;
  const value = direction === "SHORT" ? price * (1 - distance) : price * (1 + distance);
  return formatNumber(value, 6);
}

function renderDirection(direction) {
  const tone = direction === "LONG" ? "long" : direction === "SHORT" ? "short" : "watch";
  return `<span class="direction ${tone}">${escapeHtml(direction)}</span>`;
}

function renderList(items) {
  return `<ul class="compact-list">${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>`;
}

function renderPatterns() {
  const payload = state.patterns || state.summary?.patterns || {};
  const signals = payload.signals || {};
  const stats = payload.stats || {};
  renderPatternGroup(
    "#patternOiShortRows",
    signals.oi_4h_short_reversal || [],
    stats.oi_4h_short_reversal,
    "down",
  );
  renderPatternGroup(
    "#patternNegFundingRows",
    signals.high_neg_funding_12h_short || [],
    stats.high_neg_funding_12h_short,
    "down",
  );
  renderPatternGroup(
    "#patternShortCrowdRows",
    signals.short_crowd_high_volume_12h_short || [],
    stats.short_crowd_high_volume_12h_short,
    "down",
  );
  renderPatternGroup(
    "#patternLongRows",
    signals.strict_momentum_4h_long || [],
    stats.strict_momentum_4h_long,
    "up",
  );
}

function hasPatternPayload(payload) {
  return Boolean(payload?.signals || payload?.stats);
}

function renderPatternGroup(selector, rows, stat, oddsKey) {
  const target = $(selector);
  if (!target) return;
  const summary = stat ? renderPatternStat(stat, oddsKey) : "";
  const body = rows.length
    ? rows
        .map(
          (item, index) => `
            <tr>
              <td>${index + 1}</td>
              <td><strong>${escapeHtml(item.coin || coinFromSymbol(item.symbol))}</strong><br><small>${escapeHtml(item.symbol)}</small></td>
              <td>${renderDirection(item.entry_side || (oddsKey === "up" ? "LONG" : "SHORT"))}</td>
              <td><span class="score ${scoreTone(item.short_setup_score ?? item.pattern_score)}">${item.short_setup_score ?? item.pattern_score ?? 0}</span></td>
              <td>${formatPercent(item.oi_change_1h)}</td>
              <td>${formatSignedPercent(item.price_change_1h)}</td>
              <td>${formatPercent(item.price_position_24h)}</td>
              <td>${formatPercent((item.funding_rate || 0) * 100)}</td>
              <td>${item.evidence_sample_count ?? 0}</td>
              <td>${formatPercent(oddsKey === "up" ? item.up_probability_pct : item.down_probability_pct)}</td>
              <td>${formatSignedPercent(item.median_return_pct)}</td>
            </tr>
          `,
        )
        .join("")
    : `<tr><td colspan="11"><div class="empty">当前最新一轮没有命中这个模式</div></td></tr>`;
  target.innerHTML = summary + body;
}

function renderPatternStat(stat, oddsKey) {
  const horizon = oddsKey === "up" ? "12" : "4";
  const data = stat.horizons?.[horizon] || {};
  const odds = oddsKey === "up" ? data.up_probability_pct : data.down_probability_pct;
  return `
    <tr class="pattern-summary-row">
      <td colspan="11">
        历史命中 ${stat.total_matches ?? 0} 次，${horizon}h 可回测样本 ${data.sample_count ?? 0}，
        ${oddsKey === "up" ? "上涨" : "下跌"}概率 ${formatPercent(odds)}，
        中位数 ${formatSignedPercent(data.median_return_pct)}
      </td>
    </tr>
  `;
}

function renderConfidence(value) {
  const label = { high: "高", medium: "中", low: "低" }[value] || value || "--";
  const tone = value === "high" ? "high" : value === "medium" ? "medium" : "low";
  return `<span class="confidence ${tone}">${escapeHtml(label)}</span>`;
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
  const probabilityLabels = points.map((point) => renderProbabilityLabel(point, pad, width)).join("");
  const dots = points
    .map((point) => {
      const title = [
        formatTime(point.item.timestamp_utc),
        `风险评分 ${point.item.risk_score}`,
        probabilityTitle(point.item.outcome_probability),
      ]
        .filter(Boolean)
        .join(" · ");
      return `<circle class="dot" cx="${point.x}" cy="${point.y}" r="4"><title>${escapeHtml(title)}</title></circle>`;
    })
    .join("");

  svg.innerHTML = `
    ${grid}
    ${xTicks}
    <line class="axis" x1="${pad.left}" x2="${width - pad.right}" y1="${height - pad.bottom}" y2="${height - pad.bottom}"></line>
    <line class="axis" x1="${pad.left}" x2="${pad.left}" y1="${pad.top}" y2="${height - pad.bottom}"></line>
    <path class="line" d="${path}"></path>
    ${probabilityLabels}
    ${dots}
    <text x="${pad.left}" y="${height - 10}" fill="#687386" font-size="12">${escapeHtml(state.selectedSymbol)} · ${rows.length}/${allRows.length} 条</text>
  `;
}

function renderProbabilityLabel(point, pad, width) {
  const label = probabilityLabel(point.item.outcome_probability);
  if (!label) return "";
  const labelWidth = 82;
  const labelHeight = 18;
  const x = Math.min(Math.max(point.x, pad.left + labelWidth / 2), width - pad.right - labelWidth / 2);
  const above = point.y > pad.top + 28;
  const y = above ? point.y - 26 : point.y + 10;
  return `
    <g class="prob-label">
      <rect class="prob-label-bg" x="${x - labelWidth / 2}" y="${y}" width="${labelWidth}" height="${labelHeight}" rx="6"></rect>
      <text x="${x}" y="${y + 13}" text-anchor="middle">${escapeHtml(label)}</text>
    </g>
  `;
}

function probabilityLabel(outcome) {
  if (!outcome) return "";
  const up = outcome.up_probability ?? "--";
  const down = outcome.down_probability ?? "--";
  return `涨${up}%/跌${down}%`;
}

function probabilityTitle(outcome) {
  if (!outcome) return "";
  return `后验概率 ${outcome.horizon || "1-6h"}：涨${outcome.up_probability ?? "--"}% / 跌${outcome.down_probability ?? "--"}%`;
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
  const tradeHint = outcome.trade_action && outcome.trade_action !== "观望"
    ? `<small class="trade-hint">${escapeHtml(outcome.trade_label || `可关注${outcome.trade_action}`)}</small>`
    : "";
  return `
    <span class="outcome-label">${escapeHtml(outcome.direction || "中性观察")}</span>
    <small>${escapeHtml(outcome.horizon || "1-6h")} · 涨 ${outcome.up_probability ?? "--"}% / 跌 ${outcome.down_probability ?? "--"}%</small>
    ${tradeHint}
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

function formatSignedNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "N/A";
  const number = Number(value);
  return `${number >= 0 ? "+" : ""}${number.toFixed(0)}`;
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
