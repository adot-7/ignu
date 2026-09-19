(() => {
  "use strict";

  const STAGES = ["ingest", "github", "profile", "graph", "rank"];
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
    last_run: null,
  };
  const pipelineEvents = [];

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
    dashboardState.last_run = payload.last_run || null;
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
    if (!STAGES.includes(stage)) return;
    const stats = stageStats[stage];
    if (Object.prototype.hasOwnProperty.call(stats, status)) stats[status] += 1;
    stats.last = status || stats.last;
    renderStage(stage);
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
    body.innerHTML = rows.map((row) => {
      const reasons = asArray(row.reasons).slice(0, 2).join(" · ") || "rank movement";
      return `<tr><td>${escapeHtml(displayName(row.name))}</td><td class="rank-number">#${asNumber(row.baseline_rank)}</td><td class="rank-number">#${asNumber(row.ignu_rank)}</td><td class="gap-number">${asNumber(row.gap)}</td><td class="reason-cell" title="${escapeHtml(reasons)}">${escapeHtml(reasons)}</td></tr>`;
    }).join("");
  }

  function showToast(message, isError = false) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.toggle("error", isError);
    toast.classList.add("visible");
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => toast.classList.remove("visible"), 3600);
  }

  function init() {
    renderState();
    renderFeed();
    STAGES.forEach(renderStage);
    void loadState();
    connectEvents();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
