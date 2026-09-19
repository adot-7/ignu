(() => {
  "use strict";

  const STAGES = ["ingest", "github", "profile", "graph", "rank", "team", "memory"];
  const SKILLS = [
    ["frontend", "Frontend"],
    ["backend", "Backend"],
    ["ml_ai", "ML / AI"],
    ["data", "Data"],
    ["mobile", "Mobile"],
    ["devops_cloud", "DevOps"],
    ["design_product", "Design"],
    ["pitch_comms", "Pitch"],
  ];
  const DEFAULT_COUNTERS = {
    rows: 0,
    with_github: 0,
    with_github_pct: 0,
    aliases: 0,
    admitted: 0,
    waitlist: 0,
    decline: 0,
    needs_human: 0,
  };
  const stageStats = Object.fromEntries(
    STAGES.map((stage) => [stage, { ok: 0, skip: 0, error: 0, start: 0, last: "idle" }]),
  );
  const dashboardState = {
    counters: { ...DEFAULT_COUNTERS },
    spend: 0,
    budget: 6,
    disagreements: [],
    teams: [],
    last_run: null,
    scoring: {},
  };
  const pipelineEvents = [];
  const chatMessages = [
    {
      role: "assistant",
      answer: "Ready when you are.",
      prompt: "Who moved most between the baseline and ignu rank?",
      time: "now",
    },
  ];

  let eventSource = null;
  let reconnectTimer = null;
  let reconnectAttempt = 0;
  let toastTimer = null;

  const $ = (id) => document.getElementById(id);
  const maskEnabled = new URLSearchParams(window.location.search).get("mask") === "1";

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    })[character]);
  }

  function asNumber(value, fallback = 0) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }

  function clamp(value, minimum = 0, maximum = 1) {
    return Math.max(minimum, Math.min(maximum, asNumber(value)));
  }

  function asArray(value) {
    return Array.isArray(value) ? value : [];
  }

  function displayName(name) {
    const raw = String(name || "Unknown participant").trim();
    if (!maskEnabled) return raw || "Unknown participant";
    const parts = raw.split(/\s+/).filter(Boolean);
    if (parts.length < 2) return parts[0] || "Unknown";
    return `${parts[0]} ${parts[parts.length - 1].charAt(0)}.`;
  }

  function formatTime(value) {
    if (!value) return "now";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "now";
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function formatRelative(value) {
    if (!value) return "now";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "now";
    const seconds = Math.max(0, Math.round((Date.now() - date.getTime()) / 1000));
    if (seconds < 5) return "now";
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.round(seconds / 60);
    return `${minutes}m ago`;
  }

  function normaliseState(payload) {
    if (!payload || typeof payload !== "object") return;
    const counters = payload.counters && typeof payload.counters === "object" ? payload.counters : {};
    Object.assign(dashboardState.counters, DEFAULT_COUNTERS, counters);
    const spend = payload.spend && typeof payload.spend === "object" ? payload.spend.usd : payload.spend;
    dashboardState.spend = asNumber(spend);
    dashboardState.budget = asNumber(payload.budget, dashboardState.budget || 6);
    dashboardState.disagreements = asArray(payload.disagreements);
    dashboardState.teams = asArray(payload.teams);
    dashboardState.last_run = payload.last_run || null;
    dashboardState.scoring = payload.scoring && typeof payload.scoring === "object" ? payload.scoring : {};
  }

  async function fetchJson(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = null;
    }
    if (!response.ok) {
      const error = new Error(`Request failed: ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  async function loadState() {
    try {
      const payload = await fetchJson("/api/state");
      normaliseState(payload);
      renderState();
    } catch (error) {
      showToast("State is unavailable; showing the empty-safe dashboard.", true);
      renderState();
    }
  }

  function renderState() {
    const counters = dashboardState.counters;
    $("counter-rows").textContent = String(asNumber(counters.rows));
    const githubPercent = asNumber(counters.with_github_pct, counters.rows ? asNumber(counters.with_github) / asNumber(counters.rows) * 100 : 0);
    $("counter-github").textContent = `${githubPercent.toFixed(githubPercent % 1 ? 1 : 0)}%`;
    $("counter-aliases").textContent = String(asNumber(counters.aliases));
    $("counter-admitted").textContent = String(asNumber(counters.admitted));
    $("counter-waitlist").textContent = String(asNumber(counters.waitlist));
    $("counter-decline").textContent = String(asNumber(counters.decline));
    $("counter-needs-human").textContent = String(asNumber(counters.needs_human));
    $("nav-disagreement-count").textContent = String(dashboardState.disagreements.length);

    const spend = dashboardState.spend.toFixed(2);
    const budget = dashboardState.budget.toFixed(2);
    $("spend-value").innerHTML = `$${spend} <em>/ $${budget}</em>`;

    const lastRun = dashboardState.last_run;
    $("run-id").textContent = lastRun?.run_id || "waiting for a run";
    $("last-run").textContent = lastRun
      ? `${lastRun.status || "updated"} · ${formatRelative(lastRun.ts)}`
      : "No pipeline run yet";

    renderDisagreements();
    renderTeams();
    renderRanking();
  }

  function setConnectionState(isLive) {
    const indicator = $("live-indicator");
    const label = $("live-label");
    indicator.classList.toggle("offline", !isLive);
    label.textContent = isLive ? "live" : "reconnecting";
  }

  function setPipelineState(isLive) {
    const status = $("pipeline-status");
    const panelStatus = status.closest(".panel-status");
    panelStatus.classList.toggle("live", isLive);
    status.textContent = isLive ? "receiving events" : "waiting for events";
  }

  function connectEvents() {
    if (eventSource) eventSource.close();
    if (reconnectTimer) {
      window.clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    setConnectionState(false);
    const source = new EventSource("/events");
    eventSource = source;
    source.onopen = () => {
      if (eventSource !== source) return;
      reconnectAttempt = 0;
      setConnectionState(true);
      setPipelineState(true);
    };
    source.addEventListener("heartbeat", () => {
      if (eventSource !== source) return;
      setConnectionState(true);
      setPipelineState(true);
    });
    source.addEventListener("pipeline", (message) => {
      if (eventSource !== source) return;
      try {
        receivePipelineEvent(JSON.parse(message.data));
      } catch (_error) {
        showToast("Received an unreadable pipeline event.", true);
      }
    });
    source.onerror = () => {
      if (eventSource !== source) return;
      setConnectionState(false);
      setPipelineState(false);
      source.close();
      eventSource = null;
      scheduleReconnect();
    };
  }

  function scheduleReconnect() {
    if (reconnectTimer) return;
    const delay = Math.min(10000, 500 * (2 ** reconnectAttempt));
    reconnectAttempt = Math.min(reconnectAttempt + 1, 5);
    reconnectTimer = window.setTimeout(() => {
      reconnectTimer = null;
      connectEvents();
    }, delay);
  }

  function receivePipelineEvent(event) {
    if (!event || typeof event !== "object") return;
    const stage = String(event.stage || "").toLowerCase();
    const status = String(event.status || "").toLowerCase();
    if (STAGES.includes(stage)) {
      const stats = stageStats[stage];
      if (Object.prototype.hasOwnProperty.call(stats, status)) stats[status] += 1;
      stats.last = status || stats.last;
      renderStage(stage);
    }
    pipelineEvents.unshift(event);
    pipelineEvents.splice(15);
    if (event.run_id) {
      $("run-id").textContent = String(event.run_id);
      $("last-run").textContent = `${status || "updated"} · ${formatRelative(event.ts)}`;
    }
    setConnectionState(true);
    setPipelineState(true);
    renderFeed();
  }

  function renderStage(stage) {
    const card = document.querySelector(`[data-stage="${stage}"]`);
    if (!card) return;
    const stats = stageStats[stage];
    card.classList.toggle("active", stats.last === "ok" || stats.last === "start");
    card.classList.toggle("has-error", stats.last === "error");
    card.querySelector(".stage-state").textContent = stats.last || "idle";
    card.querySelector(".stage-ok").textContent = `${stats.ok} ok`;
    card.querySelector(".stage-skip").textContent = `${stats.skip} skip`;
    card.querySelector(".stage-error").textContent = `${stats.error} err`;
  }

  function renderFeed() {
    const container = $("feed-list");
    if (!pipelineEvents.length) {
      container.innerHTML = '<div class="empty-state compact-empty"><span class="empty-glyph">⌁</span><strong>Waiting for the pipeline</strong><p>Stage events will appear here as the run progresses.</p></div>';
      return;
    }
    container.innerHTML = pipelineEvents.map((event) => {
      const status = ["ok", "skip", "error", "start"].includes(event.status) ? event.status : "skip";
      const stage = escapeHtml(event.stage || "stage");
      const person = event.person_id ? `<span class="feed-person">${escapeHtml(String(event.person_id).slice(0, 12))}</span>` : "";
      return `<div class="feed-row"><span class="event-dot ${status}"></span><div class="feed-copy"><div class="feed-line"><span class="feed-stage">${stage}</span>${person}<span class="feed-time">${escapeHtml(formatTime(event.ts))}</span></div><p class="feed-msg">${escapeHtml(event.msg || "pipeline event")}</p></div></div>`;
    }).join("");
  }

  function renderDisagreements() {
    const body = $("disagreement-body");
    const rows = dashboardState.disagreements;
    $("disagreement-summary").textContent = `${rows.length} surfaced`;
    if (!rows.length) {
      body.innerHTML = '<tr><td class="table-placeholder" colspan="5">No disagreement rows yet. The comparison appears after ranking.</td></tr>';
      return;
    }
    body.innerHTML = rows.map((row, index) => {
      const reasons = asArray(row.reasons).slice(0, 2).join(" · ") || "rank movement";
      const personId = escapeHtml(row.person_id || `row-${index}`);
      return `<tr data-person-id="${personId}" tabindex="0" aria-label="Open ${escapeHtml(displayName(row.name))} details"><td>${escapeHtml(displayName(row.name))}</td><td class="rank-number">#${asNumber(row.baseline_rank)}</td><td class="rank-number">#${asNumber(row.ignu_rank)}</td><td class="gap-number">${asNumber(row.gap)}</td><td class="reason-cell" title="${escapeHtml(reasons)}">${escapeHtml(reasons)}</td></tr>`;
    }).join("");
  }

  function memberNames(team) {
    if (Array.isArray(team.members)) {
      return team.members.map((member) => {
        if (typeof member === "string") return member;
        return member?.name || member?.id || "Unknown participant";
      });
    }
    return asArray(team.member_ids).map((member) => String(member).slice(0, 12));
  }

  function renderTeams() {
    const container = $("team-list");
    const teams = dashboardState.teams;
    $("team-summary").textContent = `${teams.length} formed`;
    if (!teams.length) {
      container.innerHTML = '<div class="empty-state compact-empty"><span class="empty-glyph">⌘</span><strong>No teams formed yet</strong><p>Admitted solos will appear with their coverage map.</p></div>';
      return;
    }
    container.innerHTML = teams.map((team, index) => {
      const coverage = team.coverage && typeof team.coverage === "object" ? team.coverage : {};
      const memberMarkup = memberNames(team).map((name) => `<span class="member-chip">${escapeHtml(displayName(name))}</span>`).join("");
      const coverageMarkup = SKILLS.map(([key, label]) => {
        const value = Math.round(clamp(coverage[key]) * 100);
        return `<div class="coverage-row"><span class="coverage-label">${escapeHtml(label)}</span><span class="coverage-track"><span class="coverage-fill" style="width:${value}%"></span></span><span class="coverage-value">${value}%</span></div>`;
      }).join("");
      const teamName = team.id || `team-${String(index + 1).padStart(2, "0")}`;
      return `<article class="team-card"><div class="team-card-header"><span class="team-id">${escapeHtml(teamName)}</span><span class="team-balance">balance ${Math.round(clamp(team.balance) * 100)}%</span></div><div class="member-list">${memberMarkup || '<span class="member-chip">No members yet</span>'}</div><div class="coverage-list">${coverageMarkup}</div>${team.why ? `<p class="team-why">${escapeHtml(team.why)}</p>` : ""}</article>`;
    }).join("");
  }

  function labelForWeight(key) {
    return String(key).replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function renderRanking() {
    const container = $("ranking-content");
    const scoring = dashboardState.scoring || {};
    const weights = scoring.weights && typeof scoring.weights === "object" ? scoring.weights : {};
    const thresholds = scoring.thresholds && typeof scoring.thresholds === "object" ? scoring.thresholds : {};
    const entries = Object.entries(weights).filter(([, value]) => Number.isFinite(Number(value)));
    if (!entries.length && !Object.keys(thresholds).length) {
      container.innerHTML = '<div class="empty-state compact-empty"><span class="empty-glyph">↗</span><strong>Scoring is loading</strong><p>Weights and thresholds will be shown here.</p></div>';
      return;
    }
    const weightsMarkup = entries.map(([key, value]) => {
      const percentage = Math.round(clamp(value, 0, 1) * 100);
      return `<div class="weight-row"><span class="weight-name">${escapeHtml(labelForWeight(key))}</span><span class="weight-track"><span class="weight-fill" style="width:${percentage}%"></span></span><span class="weight-value">${percentage}%</span></div>`;
    }).join("");
    const thresholdMarkup = Object.entries(thresholds).map(([key, value]) => `<div class="threshold"><span>${escapeHtml(labelForWeight(key))}</span><strong>${asNumber(value).toFixed(2)}</strong></div>`).join("");
    const baseline = scoring.baseline && typeof scoring.baseline === "object" ? scoring.baseline : {};
    const keywords = asArray(baseline.keywords);
    const threshold = scoring.disagreement_threshold;
    container.innerHTML = `<div class="weights">${weightsMarkup || '<p class="drawer-empty">No component weights configured.</p>'}</div><div class="thresholds">${thresholdMarkup || '<div class="threshold"><span>thresholds</span><strong>—</strong></div>'}</div><p class="rank-footnote">Baseline watches <code>${keywords.length || 0} configured signals</code>${threshold !== undefined ? ` · disagreement at <code>${escapeHtml(threshold)}</code> ranks` : ""}.</p>`;
  }

  function normaliseSkills(profile) {
    const source = profile?.skills;
    if (Array.isArray(source)) {
      return source.map((item) => ({
        skill: item?.skill || item?.name || "skill",
        confidence: clamp(item?.confidence ?? item?.score),
      }));
    }
    if (source && typeof source === "object") {
      return Object.entries(source).map(([skill, confidence]) => ({ skill, confidence: clamp(confidence) }));
    }
    return [];
  }

  function renderEvidence(evidence) {
    const items = asArray(evidence);
    if (!items.length) return '<p class="drawer-empty">No evidence attached yet.</p>';
    return `<ul class="evidence-list">${items.map((item) => {
      const url = item?.source_url || item?.url || "";
      const claim = item?.claim || item?.text || "Observed signal";
      const kind = item?.kind || "source";
      const confidence = item?.confidence === undefined ? "" : ` · ${Math.round(clamp(item.confidence) * 100)}% confidence`;
      const source = url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(url)}</a>` : `<span class="evidence-meta">source unavailable</span>`;
      return `<li class="evidence-item">${source}<p>${escapeHtml(claim)}</p><span class="evidence-meta">${escapeHtml(kind)}${confidence}</span></li>`;
    }).join("")}</ul>`;
  }

  function renderHistory(history) {
    const items = asArray(history);
    if (!items.length) return '<p class="drawer-empty">No prior verdicts recorded.</p>';
    return `<ul class="history-list">${items.map((item) => `<li class="history-item"><span class="history-meta">${escapeHtml(item?.at || item?.ts || "recorded")}</span><p><strong>${escapeHtml(item?.decision || "verdict")}</strong>${item?.score === undefined ? "" : ` · score ${asNumber(item.score).toFixed(2)}`}${item?.reason ? ` · ${escapeHtml(item.reason)}` : ""}</p></li>`).join("")}</ul>`;
  }

  function renderDrawer(row, detail, errorMessage = "") {
    const profile = detail?.profile || row?.profile || detail || {};
    const verdict = detail?.verdict || row || {};
    const name = detail?.person?.name || detail?.name || row?.name || "Participant";
    $("drawer-name").textContent = displayName(name);
    const skills = normaliseSkills(profile);
    const skillMarkup = skills.length
      ? `<div class="skill-list">${skills.map((item) => { const value = Math.round(item.confidence * 100); return `<div class="skill-row"><span>${escapeHtml(labelForWeight(item.skill))}</span><span class="skill-track"><span class="skill-fill" style="width:${value}%"></span></span><span class="skill-value">${value}%</span></div>`; }).join("")}</div>`
      : '<p class="drawer-empty">Skills will appear after profile synthesis.</p>';
    const evidence = profile.evidence || detail?.evidence || row?.evidence || [];
    const history = detail?.verdict_history || detail?.history || row?.verdict_history || row?.history || [];
    const badges = [profile.evidence_level, verdict.decision, verdict.eligibility].filter(Boolean).map((badge) => `<span class="detail-badge">${escapeHtml(badge)}</span>`).join("");
    const score = verdict.score === undefined ? "" : `<span class="detail-badge">score ${asNumber(verdict.score).toFixed(2)}</span>`;
    const profileSummary = profile.summary || detail?.summary || "No profile summary is available for this row yet.";
    $("drawer-body").innerHTML = `${errorMessage ? `<div class="detail-section"><p class="drawer-empty">${escapeHtml(errorMessage)}</p></div>` : ""}<section class="detail-section"><p class="detail-label">Profile summary</p><p class="detail-summary">${escapeHtml(profileSummary)}</p><div class="detail-badges">${badges}${score}</div></section><section class="detail-section"><p class="detail-label">Skills</p>${skillMarkup}</section><section class="detail-section"><p class="detail-label">Evidence</p>${renderEvidence(evidence)}</section><section class="detail-section"><p class="detail-label">Verdict history</p>${renderHistory(history)}</section>`;
  }

  function openDrawer(row) {
    const drawer = $("person-drawer");
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    $("drawer-name").textContent = displayName(row.name);
    $("drawer-body").innerHTML = '<div class="empty-state compact-empty"><span class="empty-glyph">⌁</span><strong>Loading context</strong><p>Gathering the attached profile and sources.</p></div>';
    fetchJson(`/api/person/${encodeURIComponent(row.person_id)}`)
      .then((detail) => renderDrawer(row, detail || {}))
      .catch((error) => renderDrawer(row, null, error.status === 404 ? "Person detail is not available in this run yet." : "Person detail could not be loaded; showing the disagreement row."));
  }

  function closeDrawer() {
    const drawer = $("person-drawer");
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
  }

  function markdownLite(value) {
    return escapeHtml(value || "")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\n/g, "<br>");
  }

  function renderSources(sources) {
    const items = asArray(sources);
    if (!items.length) return "";
    const links = items.map((source) => {
      const item = typeof source === "string" ? { url: source, label: source } : (source || {});
      const url = item.url || item.source_url || "";
      const label = item.label || item.title || item.claim || url || "source";
      return url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>` : `<span class="evidence-meta">${escapeHtml(label)}</span>`;
    }).join("");
    return `<div class="answer-sources"><span class="answer-sources-label">sources</span>${links}</div>`;
  }

  function renderChat() {
    const history = $("chat-history");
    history.innerHTML = chatMessages.map((message) => {
      const user = message.role === "user";
      const body = user ? escapeHtml(message.answer || "") : markdownLite(message.answer || "");
      const prompt = message.prompt ? ` <button class="prompt-chip" type="button" data-prompt="${escapeHtml(message.prompt)}">“${escapeHtml(message.prompt.toLowerCase())}”</button>` : "";
      const trace = !user && message.trace && asArray(message.trace).length ? `<details class="tool-trace"><summary>tool trace</summary><pre>${escapeHtml(JSON.stringify(message.trace, null, 2))}</pre></details>` : "";
      const sources = !user ? renderSources(message.sources) : "";
      return `<div class="chat-message ${user ? "user-message" : "assistant-message"}"><div class="message-avatar">${user ? "↗" : "✦"}</div><div class="message-body"><p class="${user ? "" : "answer-markdown"}">${body}${prompt}</p>${trace}${sources}<span class="message-time">${escapeHtml(message.time || "now")}</span></div></div>`;
    }).join("");
    history.scrollTop = history.scrollHeight;
  }

  async function submitQuestion(event) {
    event.preventDefault();
    const input = $("chat-question");
    const button = document.querySelector(".send-button");
    const question = input.value.trim();
    if (!question || button.disabled) return;
    chatMessages.push({ role: "user", answer: question, time: "now" });
    input.value = "";
    renderChat();
    button.disabled = true;
    try {
      const payload = await fetchJson("/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, channel: "dashboard" }),
      });
      chatMessages.push({
        role: "assistant",
        answer: payload?.answer || payload?.message || "The agent returned no answer.",
        trace: payload?.tool_trace || payload?.trace || payload?.tools,
        sources: payload?.sources,
        time: "now",
      });
    } catch (error) {
      const answer = error.status === 404
        ? "The context assistant is not connected yet. The pipeline view is still available."
        : "I could not reach the context assistant. Try again after the next run.";
      chatMessages.push({ role: "assistant", answer, time: "now" });
      showToast(error.status === 404 ? "The /ask endpoint is not available yet." : "The assistant request failed.", true);
    } finally {
      button.disabled = false;
      renderChat();
    }
  }

  async function resetDemo() {
    try {
      await fetchJson("/api/reset", { method: "POST" });
      pipelineEvents.splice(0);
      STAGES.forEach((stage) => Object.assign(stageStats[stage], { ok: 0, skip: 0, error: 0, start: 0, last: "idle" }));
      STAGES.forEach(renderStage);
      await loadState();
      showToast("Demo state reset.");
    } catch (error) {
      showToast(error.status === 404 ? "Reset is not available until the demo runner is installed." : "Reset could not be completed.", true);
    }
  }

  function showToast(message, isError = false) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.classList.add("visible");
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 3600);
  }

  function bindUi() {
    $("chat-form").addEventListener("submit", submitQuestion);
    $("reset-demo").addEventListener("click", resetDemo);
    $("drawer-close").addEventListener("click", closeDrawer);
    $("drawer-backdrop").addEventListener("click", closeDrawer);
    $("disagreement-body").addEventListener("click", (event) => {
      const row = event.target.closest("tr[data-person-id]");
      if (!row) return;
      const disagreement = dashboardState.disagreements.find((item) => String(item.person_id) === row.dataset.personId);
      if (disagreement) openDrawer(disagreement);
    });
    $("disagreement-body").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const row = event.target.closest("tr[data-person-id]");
      if (!row) return;
      event.preventDefault();
      const disagreement = dashboardState.disagreements.find((item) => String(item.person_id) === row.dataset.personId);
      if (disagreement) openDrawer(disagreement);
    });
    $("chat-history").addEventListener("click", (event) => {
      const prompt = event.target.closest("[data-prompt]");
      if (!prompt) return;
      $("chat-question").value = prompt.dataset.prompt || "";
      $("chat-question").focus();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeDrawer();
    });
  }

  function init() {
    bindUi();
    renderState();
    renderFeed();
    renderChat();
    STAGES.forEach(renderStage);
    void loadState();
    connectEvents();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
