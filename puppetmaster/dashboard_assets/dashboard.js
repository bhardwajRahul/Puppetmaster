function isEmbedSearch(search) {
  return /(?:^|[?&])embed=(1|true|yes)\b/i.test(search || "");
}

function jobHref(id, embed) {
  return "?job=" + encodeURIComponent(id) + (embed ? "&embed=1" : "");
}

const qs = new URLSearchParams(location.search);
const embedMode = isEmbedSearch(location.search);
let jobId = qs.get("job");
let activeView = qs.get("view") === "jobs" || !jobId ? "jobs" : "job";
let activeTab = "overview";
let jobFilter = "all";
let jobSearch = "";
let selectedTaskId = null;
let latestJobs = [];
let latestJob = null;
let meta = null;
let stateDirDiagnosis = null;
let lastContent = "";
let polling = false;
let taskFilter = "all";

const icons = {
  search: '<svg aria-hidden="true" viewBox="0 0 24 24"><circle cx="11" cy="11" r="6.5"/><path d="m16 16 4 4"/></svg>',
};

function statusLabel(status) {
  const value = status || "unknown";
  return `<span class="status s-${esc(value)}">${esc(value.replace(/_/g, " "))}</span>`;
}

function truncateGoal(goal, maxChars = 120) {
  if (!goal || goal.length <= maxChars) return esc(goal);
  const firstLine = goal.split("\n")[0];
  return esc(firstLine.length <= maxChars ? firstLine : firstLine.slice(0, maxChars) + "…");
}

function fmtAgo(iso) {
  const timestamp = Date.parse(iso || "");
  if (!Number.isFinite(timestamp)) return "Unknown";
  const seconds = Math.max(0, (Date.now() - timestamp) / 1000);
  if (seconds < 60) return "Just now";
  if (seconds < 3600) return Math.floor(seconds / 60) + "m ago";
  if (seconds < 86400) return Math.floor(seconds / 3600) + "h ago";
  return Math.floor(seconds / 86400) + "d ago";
}

function formatNumber(value) {
  const number = value;
  return typeof number === "number" && Number.isFinite(number) ? number.toLocaleString() : "Unknown";
}

function formatSelectedCost(actual, digits = 4) {
  const number = actual && actual.total_marginal_cost_usd;
  return typeof number === "number" && Number.isFinite(number) ? "$" + number.toFixed(digits) : "unknown";
}

function setConnection(state, label) {
  const element = document.getElementById("connection");
  if (element && element.dataset.state !== state) element.dataset.state = state;
  const text = document.getElementById("connection-label");
  if (text && text.textContent !== label) text.textContent = label;
}

function setBreadcrumb(parts) {
  document.getElementById("breadcrumb").innerHTML = parts.map((part, index) => {
    const separator = index ? '<span class="breadcrumb-separator">/</span>' : "";
    return separator + (index === parts.length - 1 ? `<strong>${esc(part)}</strong>` : `<span>${esc(part)}</span>`);
  }).join("");
}

function setEmbedContext(job) {
  const bar = document.getElementById("embed-context");
  const crumbs = document.getElementById("breadcrumb");
  if (!bar || !crumbs) return;
  if (!embedMode || !job || !job.job) {
    if (!bar.hidden) bar.hidden = true;
    if (bar.innerHTML) bar.innerHTML = "";
    crumbs.hidden = false;
    return;
  }
  const rawGoal = job.job.goal || job.job.label || job.job.title || job.job.id || "";
  const oneLiner = rawGoal.split("\n")[0];
  const cost = formatSelectedCost(actualCost(job));
  const costHtml = cost === "unknown" ? "" : `<span class="embed-cost mono">${esc(cost)}</span>`;
  const html = `${statusLabel(job.job.status)}<span class="embed-goal" title="${esc(rawGoal)}">${truncateGoal(oneLiner, 72)}</span>${costHtml}`;
  if (bar.innerHTML !== html) bar.innerHTML = html;
  bar.hidden = false;
  crumbs.hidden = true;
}

function replaceContent(html) {
  if (html === lastContent) return false;
  const active = document.activeElement;
  const focusId = active && active.id;
  const selection = active && active.tagName === "INPUT" ? [active.selectionStart, active.selectionEnd] : null;
  const container = document.getElementById("content");
  const disclosures = new Map(Array.from(container.querySelectorAll("details[data-disclosure]"), node => [node.dataset.disclosure, node.open]));
  const scrolls = [".inspector", ".map-scroll"].map(selector => {
    const node = container.querySelector(selector);
    return [selector, node && [node.scrollLeft, node.scrollTop]];
  });
  container.innerHTML = html;
  for (const node of container.querySelectorAll("details[data-disclosure]")) {
    if (disclosures.has(node.dataset.disclosure)) node.open = disclosures.get(node.dataset.disclosure);
  }
  for (const [selector, position] of scrolls) {
    const node = container.querySelector(selector);
    if (node && position) node.scrollTo(...position);
  }
  lastContent = html;
  if (focusId) {
    const restored = document.getElementById(focusId);
    if (restored) {
      restored.focus({preventScroll: true});
      if (selection && restored.setSelectionRange) restored.setSelectionRange(selection[0], selection[1]);
    }
  }
  return true;
}

function renderEmpty(title, copy) {
  return `<section class="empty-state"><h2>${esc(title)}</h2><p>${esc(copy)}</p></section>`;
}

function renderIndex() {
  setEmbedContext(null);
  setBreadcrumb(["Runs"]);
  const counts = latestJobs.reduce((result, job) => {
    result[job.status] = (result[job.status] || 0) + 1;
    return result;
  }, {});
  const statuses = Object.keys(counts).sort();
  const query = jobSearch.trim().toLowerCase();
  const shown = latestJobs.filter(job => {
    const statusMatch = jobFilter === "all" || job.status === jobFilter;
    const text = [jobHeadline(job), job.goal, job.id, job.project].filter(Boolean).join(" ").toLowerCase();
    return statusMatch && (!query || text.includes(query));
  });
  const activeCount = latestJobs.filter(job => ["running", "queued", "stitching", "in_progress"].includes(job.status)).length;
  let html = `<section class="page-heading"><div><span class="eyebrow">Agent control room</span><h1>Runs</h1><p>Live and completed work from durable local state.</p></div><div class="count-summary"><strong>${latestJobs.length}</strong> total · <strong>${activeCount}</strong> active</div></section>`;
  html += stateDirHint(stateDirDiagnosis, latestJobs.length);
  html += `<div class="toolbar"><label class="search-wrap"><span class="sr-only">Search runs</span>${icons.search}<input id="job-search" class="search" type="search" value="${esc(jobSearch)}" placeholder="Search name, job ID, or project" autocomplete="off"></label><div class="filters" aria-label="Filter runs by status"><button class="filter-button" type="button" id="filter-all" data-job-filter="all" aria-pressed="${jobFilter === "all"}">All <span class="mono">${latestJobs.length}</span></button>`;
  for (const status of statuses) {
    html += `<button class="filter-button" type="button" id="filter-${esc(status)}" data-job-filter="${esc(status)}" aria-pressed="${jobFilter === status}">${esc(status.replace(/_/g, " "))} <span class="mono">${counts[status]}</span></button>`;
  }
  html += "</div></div>";
  if (!latestJobs.length) {
    html += renderEmpty("No runs yet", "Start a Puppetmaster job in this workspace and it will appear here.");
  } else if (!shown.length) {
    html += renderEmpty("No matching runs", "Change the search or status filter to see more runs.");
  } else {
    html += '<div class="run-list">';
    for (const job of shown) {
      const href = jobHref(job.id, embedMode);
      html += `<a class="run-row" href="${href}" aria-label="Open ${esc(jobHeadline(job))}">${statusLabel(job.status)}<span class="run-name"><strong title="${esc(job.goal)}">${esc(jobHeadline(job))}</strong><span>${esc(job.id)}</span></span><span class="run-project">${esc(job.project || projectLabel(meta) || "Local workspace")}</span><time class="run-time" datetime="${esc(job.created_at || "")}">${esc(fmtAgo(job.created_at))}</time></a>`;
    }
    html += "</div>";
  }
  replaceContent(html);
}

function actualCost(job) {
  return job && job.cost && job.cost.actual_cost ? job.cost.actual_cost : null;
}

function renderDiff(diff) {
  if (!diff || !diff.unified_diff) return "";
  let lines = "";
  for (const line of diff.unified_diff.split("\n")) {
    let className = "diff-line";
    if (line.startsWith("@@")) className += " diff-hunk";
    else if (line.startsWith("+") && !line.startsWith("+++")) className += " diff-add";
    else if (line.startsWith("-") && !line.startsWith("---")) className += " diff-remove";
    lines += `<div class="${className}">${esc(line)}</div>`;
  }
  const files = Array.isArray(diff.files) ? diff.files.join(", ") : "Code changes";
  return `<div class="diff-viewer"><div class="diff-header"><span>${esc(files || "Code changes")}</span><span>${diff.truncated ? "Truncated" : "Complete diff"}</span></div><div class="diff-scroll"><div class="diff-content">${lines}</div></div>${diff.truncated ? `<div class="diff-note">${formatNumber(diff.total_chars)} total characters</div>` : ""}</div>`;
}

function taskActivity(task) {
  const activity = Array.isArray(task.activity) ? task.activity : [];
  if (!activity.length) return '<p class="inspector-empty">No activity artifacts yet.</p>';
  return `<div class="activity-list">${activity.map((item, index) => {
    const body = item.message && item.message !== item.text ? md(item.message) : esc(item.text || item.why || "Activity recorded");
    const evidence = Array.isArray(item.evidence) && item.evidence.length ? `<div class="evidence-inline">${item.evidence.map(esc).join(" · ")}</div>` : "";
    return `<article class="activity-item"><div class="activity-head"><span>${esc(item.type || "activity")}</span><span>${esc(item.status_label || item.result || "")}</span></div><div class="activity-message">${body}</div>${evidence}${renderDiff(item.diff)}${item.meta || item.why || item.reasoning_tokens != null ? `<details data-disclosure="activity-${esc(task.id)}-${index}"><summary>Runtime details</summary><pre>${esc(JSON.stringify({why: item.why, reasoning_tokens: item.reasoning_tokens, meta: item.meta}, null, 2))}</pre></details>` : ""}</article>`;
  }).join("")}</div>`;
}

function renderInspector(task) {
  if (!task) return '<div class="inspector-body inspector-empty">Select a worker to inspect its instruction, runtime, and produced evidence.</div>';
  return `<div class="inspector-body"><div class="job-title-row"><strong>${esc(task.role)}</strong>${statusLabel(task.status)}</div><p class="inspector-model mono">${esc(task.model || "Unknown model")}</p><details class="runtime-details" data-disclosure="runtime-${esc(task.id)}"><summary>Instruction & runtime</summary><dl class="definition-list"><dt>Adapter</dt><dd class="mono">${esc(task.adapter || "Unknown")}</dd><dt>Attempts</dt><dd class="mono">${formatNumber(task.attempts)}</dd><dt>Task</dt><dd class="mono">${esc(task.id)}</dd></dl><p class="instruction">${esc(task.instruction || "No instruction recorded.")}</p></details><h3>Activity and evidence</h3>${taskActivity(task)}</div>`;
}

function renderFrontier(frontier, gists) {
  if (!frontier) return "";
  const values = [];
  for (const key of ["queued", "running", "blocked", "enqueued_from_parent"]) {
    if (Number(frontier[key]) > 0) values.push(`<span class="chip">${formatNumber(frontier[key])} ${esc(key.replace(/_/g, " "))}</span>`);
  }
  const gistCounts = frontier.gists || {};
  for (const key of ["admitted", "pending", "rejected"]) {
    if (Number(gistCounts[key]) > 0) values.push(`<span class="chip">${formatNumber(gistCounts[key])} gists ${esc(key)}</span>`);
  }
  if (!values.length && !(gists || []).length) return "";
  return `<div class="frontier"><span class="eyebrow">Frontier</span>${values.join("")}</div>`;
}

function renderMap(job) {
  const tasks = (Array.isArray(job.tasks) ? job.tasks : []).filter(task => taskFilter === "all" || (taskFilter === "active" ? ["running", "queued", "in_progress", "blocked"].includes(task.status) : ["failed", "stalled"].includes(task.status)));
  if (!tasks.length) return taskFilter === "all" ? renderEmpty("Waiting for workers", "Tasks will appear here when the coordinator dispatches them.") : renderEmpty("No " + taskFilter + " workers", "Choose All to inspect the other workers in this run.");
  const hasDependencies = (job.tasks || []).some(task => Array.isArray(task.depends_on) && task.depends_on.length);
  return `<div class="map-scroll"><div class="orchestration-map" id="orchestration-map" data-edge-mode="${hasDependencies ? "dependencies" : "hub"}"><svg class="map-lines" id="map-lines" aria-hidden="true"></svg><div class="hub-node" id="coordinator-node"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M5 5h14M12 5v6M5 11h14M5 11v7m7-7v7m7-7v7"/><circle cx="5" cy="20" r="2"/><circle cx="12" cy="20" r="2"/><circle cx="19" cy="20" r="2"/></svg>Puppetmaster</div><div class="worker-grid">${tasks.map(task => {
    const dependency = hasDependencies && (task.depends_on || []).length ? `${task.depends_on.length} dependenc${task.depends_on.length === 1 ? "y" : "ies"}` : hasDependencies ? "Root task" : "Independent task";
    return `<button class="worker-node" id="worker-${esc(task.id)}" type="button" data-task-id="${esc(task.id)}" aria-pressed="${selectedTaskId === task.id}" aria-label="Inspect ${esc(task.role)} worker, ${esc(task.status)}"><strong>${esc(task.role)}</strong><span class="worker-model">${esc(task.model || task.adapter || "Model pending")}</span><span class="dependency-note">${dependency}</span>${statusLabel(task.status)}</button>`;
  }).join("")}</div></div></div>`;
}

function drawGraphEdges() {
  const map = document.getElementById("orchestration-map");
  const svg = document.getElementById("map-lines");
  if (!map || !svg || !latestJob) return;
  const mapRect = map.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${mapRect.width} ${mapRect.height}`);
  const nodes = Array.from(map.querySelectorAll(".worker-node"));
  const nodeFor = id => nodes.find(node => node.dataset.taskId === id);
  const center = element => {
    const rect = element.getBoundingClientRect();
    return {x: rect.left - mapRect.left + rect.width / 2, y: rect.top - mapRect.top + rect.height / 2};
  };
  const hub = document.getElementById("coordinator-node");
  let paths = "";
  for (const task of latestJob.tasks || []) {
    const target = nodeFor(task.id);
    if (!target) continue;
    const dependencies = Array.isArray(task.depends_on) ? task.depends_on : [];
    const sources = dependencies.length ? dependencies.map(nodeFor).filter(Boolean) : map.dataset.edgeMode === "hub" ? [hub] : [];
    for (const source of sources) {
      const a = center(source); const b = center(target); const mid = (a.y + b.y) / 2;
      const live = ["running", "in_progress"].includes(task.status) ? " live" : "";
      paths += `<path class="${live}" d="M ${a.x} ${a.y} C ${a.x} ${mid}, ${b.x} ${mid}, ${b.x} ${b.y}"/>`;
    }
  }
  svg.innerHTML = paths;
}

function renderOverview(job) {
  const selected = (job.tasks || []).find(task => task.id === selectedTaskId) || null;
  const alerts = (job.alerts || []).map(alert => `<div class="alert-row">${esc(String(alert).replace(/^- /, ""))}</div>`).join("");
  const reroutes = (job.reroutes || []).map(item => `<div class="alert-row"><strong>${esc(item.task_id)}</strong> · ${esc(item.reason || "Rerouted")}</div>`).join("");
  return `<div class="overview-grid"><section class="panel"><div class="panel-header"><h2>Worker orchestration</h2><span>${(job.tasks || []).some(task => (task.depends_on || []).length) ? "Recorded dependencies" : "Independent tasks"}</span></div><div class="map-toolbar filters" aria-label="Filter workers">${["all", "active", "failed"].map(filter => `<button id="tasks-${filter}" class="filter-button" data-task-filter="${filter}" aria-pressed="${taskFilter === filter}">${filter[0].toUpperCase() + filter.slice(1)}</button>`).join("")}</div>${renderMap(job)}${renderFrontier(job.frontier, job.artifacts && job.artifacts.gist)}${renderHighlights(job)}</section><aside class="panel inspector"><div class="panel-header"><h2>Worker inspector</h2><span>${selected ? esc(selected.id) : "No selection"}</span></div>${renderInspector(selected)}</aside></div>${alerts || reroutes ? `<section class="attention-stack" aria-label="Attention required">${alerts}${reroutes}</section>` : ""}`;
}

function renderHighlights(job) {
  const findings = (job.artifacts && job.artifacts.finding || []).slice(0, 2);
  if (!findings.length) return "";
  return `<div class="highlights"><div class="highlight-heading"><span class="eyebrow">Produced evidence</span><button id="view-evidence" class="text-button" data-tab="evidence">View all evidence →</button></div>${findings.map(item => `<article><span class="source mono">${esc(item.created_by || "Finding")}</span><p>${esc(item.statement || "")}</p></article>`).join("")}</div>`;
}

function artifactCard(item, kind) {
  const sources = [...new Set([...(item.evidence || []), ...(item.source_artifact_ids || [])])]
    .map(source => `<span class="source">${esc(source)}</span>`).join("");
  const admission = kind === "Gists" && item.admission ? `<span class="chip">${esc(item.admission)}</span>` : "";
  return `<article class="artifact-card"><div class="artifact-head"><span>${esc(item.created_by || kind)}</span><span>${esc(item.status_label || item.claim_support_status || "")}</span></div>${admission}<div class="markdown">${md(item.statement || "")}</div>${sources ? `<div class="source-list">${sources}</div>` : ""}</article>`;
}

function renderEvidence(job) {
  const groups = [["Gists", "gist"], ["Findings", "finding"], ["Risks", "risk"], ["Decisions", "decision"], ["Verifications", "verification"], ["Patches", "patch"]];
  const populated = groups.filter(([, key]) => job.artifacts && Array.isArray(job.artifacts[key]) && job.artifacts[key].length);
  if (!populated.length) return renderEmpty("No evidence yet", "Worker findings, decisions, verifications, patches, and admitted gists will appear here.");
  return `<div class="content-stack">${groups.map(([title, key], index) => {
    const items = job.artifacts && Array.isArray(job.artifacts[key]) ? job.artifacts[key] : [];
    return `<details class="panel section" data-disclosure="evidence-${key}" ${items.length && index < 2 ? "open" : ""}><summary>${title}<span class="mono">${items.length}</span></summary><div class="artifact-list">${items.length ? items.map(item => artifactCard(item, title)).join("") : '<p class="muted">None recorded.</p>'}</div></details>`;
  }).join("")}</div>`;
}

function modelTokenTotal(actual, model, row) {
  const counts = [row.tokens_in, row.tokens_out];
  for (const task of actual.tasks || []) {
    if (task.model_id !== model) continue;
    for (const key of ["cache_read_tokens", "cache_write_tokens"]) {
      if (key in task) counts.push(task[key]);
    }
  }
  return counts.every(value => typeof value === "number" && Number.isFinite(value))
    ? counts.reduce((sum, value) => sum + value, 0) : null;
}

function renderRouting(job) {
  const actual = actualCost(job);
  const byModel = actual && actual.by_model ? Object.entries(actual.by_model) : [];
  const routing = Array.isArray(job.routing_rollup) ? job.routing_rollup : [];
  let html = `<div class="accounting"><div class="accounting-item"><span>Selected-model usage cost</span><strong>${formatSelectedCost(actual)}</strong></div><div class="accounting-item"><span>Recorded token consumption</span><strong>${formatNumber(job.tokens_total)}</strong></div><div class="accounting-item"><span>Primary model</span><strong>${esc(job.primary_model || "Unknown")}</strong></div></div>`;
  if (byModel.length) {
    html += `<section class="panel"><div class="panel-header"><h2>Selected-model usage by model</h2><span>Recorded usage; may include estimates and unknown costs</span></div><div class="table-scroll"><table><thead><tr><th>Model</th><th>Calls</th><th>Tokens</th><th>Selected-model usage cost</th></tr></thead><tbody>${byModel.map(([model, row]) => `<tr><td class="mono">${esc(model)}</td><td class="mono">${formatNumber(row.calls)}</td><td class="mono">${formatNumber(modelTokenTotal(actual, model, row))}</td><td class="mono">${formatSelectedCost({total_marginal_cost_usd: row.marginal_cost_usd})}</td></tr>`).join("")}</tbody></table></div></section>`;
  }
  html += `<div class="routing-grid">${routing.map(entry => `<article class="panel routing-card"><h3>${esc(entry.role || entry.task_id || "Routing decision")}</h3><div class="routing-model">${esc(entry.model_id || "Unknown model")}</div><p class="routing-reason">${esc(entry.reason || (entry.policy ? `Policy: ${entry.policy}` : "No rationale recorded."))}</p>${entry.rejected_count ? `<details data-disclosure="routing-${esc(entry.task_id || entry.role)}"><summary>${entry.rejected_count} alternatives considered</summary>${(entry.rejected || []).map(row => `<div class="alternative"><span class="mono">${esc(row.id)}</span><span>${esc(row.reason)}</span></div>`).join("")}</details>` : ""}</article>`).join("")}</div>`;
  if (!routing.length) html += renderEmpty("No routing decisions yet", "Router selections and considered alternatives will appear here.");
  if (job.attempt_consumption || job.budget) {
    html += `<details class="panel section" data-disclosure="accounting"><summary>Attempt consumption and budget</summary><div class="artifact-list"><pre>${esc(JSON.stringify({attempt_consumption: job.attempt_consumption || null, budget: job.budget || null}, null, 2))}</pre></div></details>`;
  }
  return html;
}

function renderJob() {
  const job = latestJob;
  if (!job) return;
  const headline = job.job.label || job.job.title || job.job.id;
  setEmbedContext(job);
  setBreadcrumb(["Runs", headline]);
  if (!selectedTaskId && job.tasks && job.tasks.length) {
    const priority = job.tasks.find(task => ["failed", "stalled", "blocked", "running", "in_progress"].includes(task.status));
    selectedTaskId = (priority || job.tasks[0]).id;
  }
  const progress = job.progress || {};
  const total = Object.values(progress).reduce((sum, value) => sum + Number(value || 0), 0);
  const complete = Number(progress.complete || progress.completed || 0);
  const actual = actualCost(job);
  const evaluators = (job.evaluator_epoch || []).map(entry => `<span class="chip" title="Evaluator epoch">${esc(entry.slot_id || "evaluator")}@v${esc(entry.version)} · ${esc(entry.role || "unknown")}</span>`).join("");
  let html = `<section class="page-heading job-heading"><div><div class="job-title-row"><h1>${esc(headline)}</h1>${statusLabel(job.job.status)}</div><details class="objective" data-disclosure="objective"><summary>Objective</summary><p>${esc(job.job.goal || "No objective recorded.")}</p></details><div class="job-meta"><span class="metric"><span>Workers</span><b>${formatNumber(job.worker_count)}</b></span><span class="metric"><span>Progress</span><b>${complete}/${total}</b></span><span class="metric"><span>Tokens</span><b>${formatNumber(job.tokens_total)}</b></span><span class="metric"><span>Artifacts</span><b>${Object.values(job.artifacts || {}).reduce((sum, items) => sum + (Array.isArray(items) ? items.length : 0), 0)}</b></span>${phaseStrip(job.phase)}${job.verification_criterion || ""}${evaluators}</div></div><span class="mono muted">${esc(job.job.id)}</span></section>`;
  html += `<div class="tabs" role="tablist" aria-label="Job detail"><button class="tab-button" type="button" role="tab" id="tab-overview" aria-controls="job-panel" tabindex="${activeTab === "overview" ? 0 : -1}" data-tab="overview" aria-selected="${activeTab === "overview"}">Overview</button><button class="tab-button" type="button" role="tab" id="tab-evidence" aria-controls="job-panel" tabindex="${activeTab === "evidence" ? 0 : -1}" data-tab="evidence" aria-selected="${activeTab === "evidence"}">Evidence</button><button class="tab-button" type="button" role="tab" id="tab-routing" aria-controls="job-panel" tabindex="${activeTab === "routing" ? 0 : -1}" data-tab="routing" aria-selected="${activeTab === "routing"}">Routing</button></div><section role="tabpanel" id="job-panel" aria-labelledby="tab-${activeTab}">`;
  html += activeTab === "evidence" ? renderEvidence(job) : activeTab === "routing" ? renderRouting(job) : renderOverview(job);
  html += "</section>";
  const changed = replaceContent(html);
  if (changed && activeTab === "overview") requestAnimationFrame(drawGraphEdges);
}

async function requestJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) {
    const error = new Error(`Request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function tick() {
  if (polling) return;
  polling = true;
  try {
    if (activeView === "job") {
      latestJob = await requestJson("/api/job?id=" + encodeURIComponent(jobId));
      renderJob();
    } else {
      latestJobs = await requestJson("/api/jobs");
      renderIndex();
    }
    setConnection("live", "Live");
  } catch (error) {
    setConnection("offline", "Disconnected");
    if (error.status === 404 && activeView === "job") {
      setEmbedContext(null);
      setBreadcrumb(["Runs", "Not found"]);
      replaceContent(renderEmpty("Run not found", "This run is not present in the dashboard's current workspace state."));
    } else if (!latestJob && !latestJobs.length) {
      replaceContent(`<div class="banner danger" role="alert"><strong>Dashboard disconnected.</strong> The local server did not answer. Refresh after the server is available.</div>${renderEmpty("Unable to load runs", "Existing data has not been replaced with placeholder values.")}`);
    }
  }
  finally { polling = false; }
}

async function loadMeta() {
  try {
    meta = await requestJson("/api/meta");
    const label = projectLabel(meta);
    if (label) {
      const element = document.getElementById("project");
      element.textContent = label;
      element.title = label;
      document.title = "Puppetmaster — " + label;
    }
  } catch (_) {}
}

async function loadDiagnostics() {
  try {
    const data = await requestJson("/api/diagnostics");
    stateDirDiagnosis = data && data.diagnosis ? data.diagnosis : null;
  } catch (_) {}
}

document.addEventListener("input", event => {
  if (event.target.id === "job-search") {
    jobSearch = event.target.value;
    renderIndex();
  }
});

document.addEventListener("click", event => {
  const taskButton = event.target.closest("[data-task-filter]");
  if (taskButton) { taskFilter = taskButton.dataset.taskFilter; renderJob(); return; }
  const filter = event.target.closest("[data-job-filter]");
  if (filter) { jobFilter = filter.dataset.jobFilter; renderIndex(); return; }
  const tab = event.target.closest("[data-tab]");
  if (tab) { activeTab = tab.dataset.tab; lastContent = ""; renderJob(); return; }
  const worker = event.target.closest("[data-task-id]");
  if (worker) { selectedTaskId = worker.dataset.taskId; lastContent = ""; renderJob(); }
});

document.addEventListener("keydown", event => {
  if (!event.target.matches('[role="tab"]')) return;
  const tabs = ["overview", "evidence", "routing"];
  const current = tabs.indexOf(event.target.dataset.tab);
  const next = event.key === "ArrowRight" ? (current + 1) % 3 : event.key === "ArrowLeft" ? (current + 2) % 3 : event.key === "Home" ? 0 : event.key === "End" ? 2 : null;
  if (next == null) return;
  event.preventDefault();
  activeTab = tabs[next];
  renderJob();
  document.getElementById("tab-" + activeTab).focus();
});

document.getElementById("refresh").addEventListener("click", tick);
window.addEventListener("resize", () => { if (activeView === "job" && activeTab === "overview") requestAnimationFrame(drawGraphEdges); });

Promise.allSettled([loadMeta(), loadDiagnostics()]).then(tick);
window.setInterval(tick, 1500);
