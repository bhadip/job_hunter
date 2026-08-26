/* Job Hunter frontend */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let config = null;
let me = null;
let eventSource = null;
let statusPoll = null;

async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return resp.json();
}

/* ---------- tabs ---------- */
$$(".tab").forEach((btn) =>
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    $$(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("#tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "history") loadHistory();
  })
);

/* ---------- config warning banner ---------- */
function renderConfigWarning() {
  const missing = config.missing_keys || {};
  const problems = [];
  if (!config.secrets_loaded_from || !config.secrets_loaded_from.length) {
    problems.push(
      "No secrets file was loaded by the server (expected .venv/.secrets next to docker-compose.yml on the host)."
    );
  }
  if (!config.llm_configured) {
    problems.push(
      `LLM not configured (missing: ${(missing.llm || []).join(", ")}) - scoring and document generation will fail.`
    );
  }
  if (!config.sheets_configured) {
    problems.push(
      `Google Sheets not configured (missing: ${(missing.sheets || []).join(", ")}) - sheet writes will fail.`
    );
  }
  if (!config.telegram_configured) {
    problems.push(
      `Telegram not configured (missing: ${(missing.telegram || []).join(", ")}) - run summaries will not be sent.`
    );
  }
  if (config.auth_mode === "dev") {
    problems.push(
      "Auth is in dev mode (Cloudflare Access not configured) - do not expose this app beyond your LAN."
    );
  }
  const el = $("#config-warning");
  if (!problems.length) {
    el.style.display = "none";
    return;
  }
  el.innerHTML =
    "<b>Configuration issues detected:</b><ul>" +
    problems.map((p) => `<li>${p}</li>`).join("") +
    "</ul>Fix <code>.venv/.secrets</code> on the server, then run " +
    "<code>docker compose restart</code>. Loaded from: " +
    (config.secrets_loaded_from && config.secrets_loaded_from.length
      ? config.secrets_loaded_from.join(", ")
      : "nothing");
  el.style.display = "block";
}

/* ---------- form helpers ---------- */
function checkedValues(cls) {
  return $$("." + cls).filter((c) => c.checked).map((c) => c.value);
}
function setChecked(cls, values) {
  $$("." + cls).forEach((c) => { c.checked = (values || []).includes(c.value); });
}

function collectParams() {
  return {
    keywords: $("#f-keywords").value.split("\n").map((s) => s.trim()).filter(Boolean),
    location: $("#f-location").value.trim() || "Singapore",
    offsets: $("#f-offsets").value.split(",").map((s) => parseInt(s.trim(), 10)).filter((n) => !isNaN(n)),
    limit: parseInt($("#f-limit").value, 10) || 50,
    easy_apply: $("#f-easy-apply").checked,
    employment_types: checkedValues("f-emp"),
    experience_levels: checkedValues("f-exp"),
    distance_km: $("#f-distance").value ? parseInt($("#f-distance").value, 10) : null,
    under_10_applicants: $("#f-under10").checked,
    score_threshold: parseInt($("#f-threshold").value, 10) || 65,
    formats: checkedValues("f-format"),
    dry_run: $("#f-dry-run").checked,
  };
}

function fillForm(p) {
  if (!p) return;
  $("#f-keywords").value = (p.keywords || []).join("\n");
  $("#f-location").value = p.location || "";
  $("#f-offsets").value = (p.offsets || [0]).join(",");
  $("#f-limit").value = p.limit || 50;
  $("#f-distance").value = p.distance_km || "";
  $("#f-easy-apply").checked = !!p.easy_apply;
  $("#f-under10").checked = !!p.under_10_applicants;
  $("#f-threshold").value = p.score_threshold || 65;
  $("#f-dry-run").checked = !!p.dry_run;
  setChecked("f-emp", p.employment_types);
  setChecked("f-exp", p.experience_levels);
  setChecked("f-format", p.formats);
}

/* ---------- run ---------- */
$("#btn-run").addEventListener("click", async () => {
  $("#run-error").textContent = "";
  const params = collectParams();
  if (!params.keywords.length) {
    $("#run-error").textContent = "Enter at least one keyword.";
    return;
  }
  if (!params.formats.length) {
    $("#run-error").textContent = "Select at least one document format.";
    return;
  }
  try {
    const { run_id } = await api("/api/runs", { method: "POST", body: JSON.stringify(params) });
    openRunStream(run_id);
  } catch (e) {
    $("#run-error").textContent = e.message;
  }
});

$("#btn-save-defaults").addEventListener("click", async () => {
  try {
    await api("/api/me", {
      method: "PUT",
      body: JSON.stringify({ default_params: collectParams(), default_formats: checkedValues("f-format") }),
    });
    $("#run-error").textContent = "Defaults saved.";
  } catch (e) {
    $("#run-error").textContent = e.message;
  }
});

$("#btn-cancel").addEventListener("click", async () => {
  const runId = $("#live-run-id").textContent.replace("#", "").trim();
  if (runId) {
    try { await api(`/api/runs/${runId}/cancel`, { method: "POST" }); } catch (e) {}
  }
});

function stopStatusPoll() {
  if (statusPoll) {
    clearInterval(statusPoll);
    statusPoll = null;
  }
}

function markRunFinished() {
  $("#live-stage").textContent = "finished";
  $("#btn-cancel").style.display = "none";
  stopStatusPoll();
}

/* Fallback: poll the run status so the Cancel button can never get stuck
   visible if the SSE stream drops before the "done" event arrives. */
function startStatusPoll(runId) {
  stopStatusPoll();
  statusPoll = setInterval(async () => {
    try {
      const run = await api(`/api/runs/${runId}`);
      if (run.status && run.status !== "running") {
        markRunFinished();
      }
    } catch (e) {
      /* transient - keep polling */
    }
  }, 3000);
}

function openRunStream(runId) {
  if (eventSource) eventSource.close();
  stopStatusPoll();
  $("#live-run-id").textContent = "#" + runId;
  $("#live-stage").textContent = "";
  $("#btn-cancel").style.display = "inline-block";
  const logEl = $("#live-log");
  logEl.textContent = "";  // clear previous run's messages on re-run

  eventSource = new EventSource(`/api/runs/${runId}/events`);
  eventSource.onmessage = (ev) => {
    const data = JSON.parse(ev.data);
    if (data.type === "done") {
      logEl.textContent += "\n--- run finished ---\n";
      markRunFinished();
      eventSource.close();
      eventSource = null;
      return;
    }
    if (data.stage) $("#live-stage").textContent = data.stage;
    logEl.textContent += data.message + "\n";
    logEl.scrollTop = logEl.scrollHeight;
  };
  eventSource.onerror = () => {
    logEl.textContent += "\n[connection lost]\n";
    markRunFinished();
    eventSource.close();
    eventSource = null;
  };
  startStatusPoll(runId);
}

/* ---------- history ---------- */
async function loadHistory() {
  const runs = await api("/api/runs");
  const tbody = $("#history-table tbody");
  tbody.innerHTML = "";
  runs.forEach((run) => {
    const stats = JSON.parse(run.stats || "{}");
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${run.id}</td>` +
      `<td>${(run.started_at || "").replace("T", " ").slice(0, 19)}</td>` +
      `<td>${run.status}</td>` +
      `<td>${stats.added ?? ""}</td>` +
      `<td>${stats.scored ?? ""}</td>` +
      `<td>${stats.generated ?? ""}</td>` +
      `<td><button data-run="${run.id}" class="btn-detail">view</button></td>`;
    tbody.appendChild(tr);
  });
  $$(".btn-detail").forEach((b) =>
    b.addEventListener("click", () => loadRunDetail(b.dataset.run))
  );
}

async function loadRunDetail(runId) {
  const run = await api(`/api/runs/${runId}`);
  $("#detail-run-id").textContent = "#" + runId;
  const tbody = $("#jobs-table tbody");
  tbody.innerHTML = "";
  (run.jobs || []).forEach((job) => {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${job.score != null ? job.score + "%" : ""}</td>` +
      `<td>${job.company || ""}</td>` +
      `<td>${job.position || ""}</td>` +
      `<td>${job.status || ""}</td>` +
      `<td><a href="${job.url}" target="_blank" rel="noopener">open</a></td>`;
    tbody.appendChild(tr);
  });
}

/* ---------- profile ---------- */
async function loadProfile() {
  me = await api("/api/me");
  $("#p-name").value = me.name || "";
  $("#p-resume").value = me.master_resume || "";
  $("#p-prompt").value = me.assessment_prompt || "";
  $("#p-prompt").placeholder = config.default_assessment_prompt.slice(0, 400) + "...";
}

$("#btn-save-profile").addEventListener("click", async () => {
  try {
    await api("/api/me", {
      method: "PUT",
      body: JSON.stringify({
        name: $("#p-name").value,
        master_resume: $("#p-resume").value,
        assessment_prompt: $("#p-prompt").value,
      }),
    });
    $("#profile-status").textContent = "Saved.";
    setTimeout(() => { $("#profile-status").textContent = ""; }, 3000);
  } catch (e) {
    $("#profile-status").textContent = e.message;
  }
});

/* ---------- init ---------- */
(async function init() {
  config = await api("/api/config");
  me = await api("/api/me");
  $("#version").textContent = "v" + config.version;
  $("#footer-version").textContent = "v" + config.version;
  $("#user-email").textContent = me.email;
  $("#auth-mode").textContent = config.auth_mode === "cloudflare" ? "Cloudflare Access" : "dev mode";
  renderConfigWarning();

  let defaults = config.defaults;
  if (me.default_params) {
    try {
      const saved = JSON.parse(me.default_params);
      if (saved && saved.keywords) defaults = saved;
    } catch (e) {}
  }
  fillForm(defaults);
  await loadProfile();
})();
