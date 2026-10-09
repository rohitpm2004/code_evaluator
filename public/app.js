/* Central Evaluators — web UI logic (vanilla JS, no build). */

// ---------- Evaluator schemas (one source of truth for the forms) ----------
const SUBS_VISUAL = [
  { key: "studentId", ph: "id (optional)" },
  { key: "studentName", ph: "student name" },
  { key: "repoUrl", ph: "https://github.com/user/repo.git", required: true },
  { key: "entryFile", ph: "entry .html (optional)" }
];
// Author: Arma Sahar — JS evaluator submissions don't need an entry file,
// fileService.js auto-finds the student's .js file in the cloned repo.
const SUBS_JS = [
  { key: "studentId", ph: "id (optional)" },
  { key: "studentName", ph: "student name" },
  { key: "repoUrl", ph: "https://github.com/user/repo.git", required: true }
];

// Author: Arma Sahar — enabling the javascript evaluator now that
// evaluators/js/* is fixed (see jsBugs.md). visual + javascript + react +
// backend are wired up on the server for this deploy; python/fullstack
// aren't started (see server.js), so their forms stay out of this map.
const EVALUATORS = {
  visual: {
    emoji: "🎨", name: "Visual / UI",
    desc: "Compare a built site to a reference design",
    blurb: "Best for static HTML/CSS sites. The submission must be a built/hosted site, not raw React/Vue source.",
    fields: [
      { key: "expectedUrl", label: "Reference site URL", sub: "the correct / expected version", type: "url", required: true, ph: "https://reference.example.com" },
      { key: "rubricText", label: "Grading rubric", sub: "plain text — the AI turns it into checks", type: "textarea", required: true, ph: "1. Has a favicon\n2. Twitter link opens twitter.com\n3. Clean centered card layout" },
      { key: "submissions", type: "submissions", subFields: SUBS_VISUAL }
    ]
  },
  javascript: {
    emoji: "🟨", name: "JavaScript",
    desc: "Run student JS against test cases in a sandbox",
    blurb: "Best for plain script-style JS (function declarations, console.log) — ES module import/export isn't supported by the sandbox.",
    fields: [
      {
        key: "evaluationMode", label: "Evaluation mode", type: "select",
        options: [
          { v: "function", t: "Function — call one function against test cases" },
          { v: "multi-function", t: "Multi-function — call several named functions" },
          { v: "script", t: "Script — compare console.log output" }
        ]
      },
      {
        key: "entryFunction", label: "Entry function name", sub: "the function to call", ph: "isPrime", required: true,
        showIf: d => (d.evaluationMode || "function") === "function"
      },
      {
        key: "testCases", label: "Test cases", sub: "JSON array of { input, expected }", type: "textarea", required: true,
        ph: '[{"input": 2, "expected": true}, {"input": 4, "expected": false}]',
        showIf: d => (d.evaluationMode || "function") === "function"
      },
      {
        key: "functions", label: "Functions", sub: "JSON array of { name, testCases }, one entry per function", type: "textarea", required: true,
        ph: '[{"name":"sum","testCases":[{"input":[2,3],"expected":5}]}]',
        showIf: d => d.evaluationMode === "multi-function"
      },
      {
        key: "expectedLogs", label: "Expected console.log output", sub: "JSON array of strings, in order", type: "textarea", required: true,
        ph: '["Hello World", "42"]',
        showIf: d => d.evaluationMode === "script"
      },
      { key: "submissions", type: "submissions", subFields: SUBS_JS }
    ]
  },
  // Author: Arma Sahar — enabling the backend evaluator now that
  // evaluators/backend/* is fixed (see backendBugs.md). Unlike visual/js,
  // the backend queue takes one repoUrl + rubric per job — no `submissions`
  // fan-out, evaluationRouter.js sends the whole payload through as-is.
  backend: {
    emoji: "🛠️", name: "Backend / API",
    desc: "Clone a repo, run Jest/Pytest tests against a grading rubric",
    blurb: "Best for Express (Node) or FastAPI (Python) projects. One repo per submission — if the repo has no tests, they're auto-generated from your rubric criteria.",
    fields: [
      { key: "repoUrl", label: "Repository URL", sub: "the student's repo to clone and grade", type: "url", required: true, ph: "https://github.com/user/repo.git" },
      {
        key: "rubricCriteria", label: "Rubric criteria", sub: "JSON array of { name, weight } — weights are points out of 100", type: "textarea", required: true,
        ph: '[{"name":"Authentication works","weight":40},{"name":"All API endpoints work","weight":30},{"name":"Performance","weight":30}]'
      }
    ]
  },
  // Author: Arma Sahar — enabling the React evaluator now that
  // evaluators/react/* is fixed (see REACT_EVALUATOR_AUDIT.md). Same
  // request shape as backend: one repoUrl + rubric per job, no `submissions`
  // fan-out (evaluationRouter.js only fans out javascript/visual). Rubric
  // criterion names must match scoringService.js's CRITERIA_KEY_MAP
  // (components/props/state/routing/api/code structure) — the placeholder
  // below uses names that are all pre-mapped so the form works out of the box.
  react: {
    emoji: "⚛️", name: "React",
    desc: "Boot the app in a real sandbox, drive it with a real browser",
    blurb: "Best for Vite/CRA React apps with a working build script. The app is installed, built, and served inside an isolated E2B sandbox, then checked with Playwright (rendering, props, state, routing, API calls) and an AI code-structure review.",
    fields: [
      { key: "repoUrl", label: "Repository URL", sub: "the student's React app to clone, build, and run", type: "url", required: true, ph: "https://github.com/user/repo.git" },
      { key: "branch", label: "Branch", sub: "optional — defaults to the repo's default branch", ph: "main" },
      {
        key: "rubricCriteria", label: "Rubric criteria", sub: "JSON array of { name, weight } — names: components, props, state, routing, api, code structure", type: "textarea", required: true,
        ph: '[{"name":"Components render correctly","weight":20},{"name":"Props handling","weight":20},{"name":"State updates","weight":20},{"name":"Routing works","weight":20},{"name":"API integration","weight":10},{"name":"Code structure","weight":10}]'
      }
    ]
  }
};

// ---------- Plain-language error help ----------
const ERROR_HELP = [
  [/Host not in allowlist/i, "That repository host isn't allowed. Use a GitHub, GitLab, or Bitbucket URL."],
  [/Refusing to access private|private\/loopback/i, "That URL points to a private/internal address and was blocked for safety."],
  [/Disallowed URL scheme/i, "Only http:// and https:// links are allowed."],
  [/Invalid URL/i, "That doesn't look like a valid URL — check for typos."],
  [/DNS resolution failed/i, "We couldn't reach that web address. Is it spelled correctly and online?"],
  [/Rubric .*parse|RubricParseError|Rubric is empty|invalid weight|invalid type|Rubric is not/i, "We couldn't understand the rubric. Try simplifying it and check the points/criteria."],
  [/Missing required inputs|non-empty array|is required/i, "Some required fields are empty. Please fill them in."],
  [/Too many submissions/i, "Too many submissions at once — reduce the batch size."],
  [/rubricText too large/i, "The rubric text is too long — shorten it."],
  [/Unauthorized/i, "Your API key is missing or wrong. Open ⚙️ Settings and paste a valid key."],
  [/No JavaScript file found|No .*file found/i, "We couldn't find the expected source file in that repo."],
  [/Timeout|exceeded \d+ms/i, "The evaluation took too long and timed out — the site/app may be slow or stuck."],
  [/clone|git /i, "We couldn't download the repository. Make sure it's public and the URL is correct."]
];
function friendlyError(msg) {
  for (const [re, help] of ERROR_HELP) if (re.test(msg || "")) return help;
  return null;
}

const STATE = {
  waiting: { cls: "waiting", label: "⏳ In queue" },
  delayed: { cls: "waiting", label: "⏳ Waiting" },
  active: { cls: "active", label: "⚙️ Running…" },
  completed: { cls: "completed", label: "✅ Done" },
  failed: { cls: "failed", label: "❌ Failed" }
};

// ---------- tiny helpers ----------
const $ = sel => document.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const getKey = () => localStorage.getItem("ce_api_key") || "";
// Every evaluator's scores get displayed through this — round to at most 1
// decimal place (e.g. 66.66666666666667 -> 66.7) instead of showing raw
// floats. Non-numbers pass through untouched so `esc()` still handles them.
const round1 = n => (typeof n === "number" && Number.isFinite(n)) ? Math.round(n * 10) / 10 : n;

let selectedType = "visual";

// ---------- Settings drawer ----------
$("#settingsBtn").addEventListener("click", () => $("#settings").classList.toggle("hidden"));
$("#saveKey").addEventListener("click", () => {
  localStorage.setItem("ce_api_key", $("#apiKey").value.trim());
  $("#keyStatus").textContent = "Saved ✓";
});
function initSettings() {
  $("#apiKey").value = getKey();
  $("#keyStatus").textContent = getKey() ? "A key is saved in this browser." : "No key saved yet.";
}

// ---------- Evaluator picker ----------
function renderPicker() {
  $("#evaluatorPicker").innerHTML = Object.entries(EVALUATORS).map(([k, e]) => `
    <button type="button" class="pick ${k === selectedType ? "active" : ""}" data-type="${k}">
      <div class="emoji">${e.emoji}</div>
      <div class="name">${esc(e.name)}</div>
      <div class="desc">${esc(e.desc)}</div>
    </button>`).join("");
  document.querySelectorAll(".pick").forEach(btn =>
    btn.addEventListener("click", () => { selectedType = btn.dataset.type; renderPicker(); renderForm(); }));
  $("#evaluatorBlurb").textContent = EVALUATORS[selectedType].blurb;
}

// ---------- Form rendering ----------
function fieldHTML(f) {
  if (f.type === "submissions") {
    return `<div class="field" data-key="submissions">
      <label>Submissions <span class="sub">— one row per student (repo URL required)</span></label>
      <div class="subs" id="subsBox"></div>
      <button type="button" class="ghost-btn add-row" id="addSub">＋ Add submission</button>
    </div>`;
  }
  const label = `<label>${esc(f.label)}${f.sub ? ` <span class="sub">— ${esc(f.sub)}</span>` : ""}${f.required ? " *" : ""}</label>`;
  let control;
  if (f.type === "textarea") control = `<textarea name="${f.key}" placeholder="${esc(f.ph || "")}"></textarea>`;
  else if (f.type === "select") control = `<select name="${f.key}">${f.options.map(o => `<option value="${o.v}">${esc(o.t)}</option>`).join("")}</select>`;
  else control = `<input type="${f.type === "url" ? "text" : "text"}" name="${f.key}" placeholder="${esc(f.ph || "")}" />`;
  return `<div class="field" data-key="${f.key}">${label}${control}</div>`;
}

function subRowHTML(subFields) {
  const inputs = subFields.map(s => `<input name="sub_${s.key}" placeholder="${esc(s.ph)}" />`).join("");
  return `<div class="sub-row">${inputs}<button type="button" class="icon-btn rm">✕</button></div>`;
}

function renderForm() {
  const ev = EVALUATORS[selectedType];
  $("#evalForm").innerHTML = ev.fields.map(fieldHTML).join("");

  const subsField = ev.fields.find(f => f.type === "submissions");
  if (subsField) {
    const box = $("#subsBox");
    const addRow = () => box.insertAdjacentHTML("beforeend", subRowHTML(subsField.subFields));
    addRow();
    $("#addSub").addEventListener("click", addRow);
    box.addEventListener("click", e => {
      if (e.target.classList.contains("rm") && box.children.length > 1) e.target.closest(".sub-row").remove();
    });
  }
  $("#evalForm").addEventListener("input", applyConditional);
  applyConditional();
  $("#formError").classList.add("hidden");
}

function readRaw() {
  const d = {};
  $("#evalForm").querySelectorAll("[name]").forEach(el => { if (!el.name.startsWith("sub_")) d[el.name] = el.value; });
  return d;
}
function applyConditional() {
  const d = readRaw();
  EVALUATORS[selectedType].fields.forEach(f => {
    if (!f.showIf) return;
    const wrap = $(`#evalForm .field[data-key="${f.key}"]`);
    if (wrap) wrap.classList.toggle("hidden", !f.showIf(d));
  });
}

// ---------- Collect + validate ----------
function collectPayload() {
  const ev = EVALUATORS[selectedType];
  const raw = readRaw();
  const errors = [];
  const payload = { type: selectedType };

  for (const f of ev.fields) {
    if (f.type === "submissions") continue;
    if (f.showIf && !f.showIf(raw)) continue;
    let val = (raw[f.key] || "").trim();

    if (f.required && !val) { errors.push(`"${f.label}" is required.`); continue; }
    if (!val) continue;

    if (f.type === "url" && !/^https?:\/\//i.test(val)) errors.push(`"${f.label}" must start with http:// or https://`);

    // Author: Arma Sahar — "functions" (multi-function mode) is JSON too.
    if (f.key === "testCases" || f.key === "expectedLogs" || f.key === "functions") {
      try { payload[f.key] = JSON.parse(val); }
      catch { errors.push(`"${f.label}" is not valid JSON.`); }
      continue;
    }
    // Backend evaluator: the form collects a flat "rubricCriteria" array,
    // but evaluatorController.js/evaluatorService.js expect it nested as
    // payload.rubric.criteria — same shape scripts/test-backend-evaluator.mjs
    // exercises.
    if (f.key === "rubricCriteria") {
      try {
        const criteria = JSON.parse(val);
        if (!Array.isArray(criteria) || criteria.length === 0) throw new Error("empty");
        payload.rubric = { criteria };
      } catch { errors.push(`"${f.label}" must be a non-empty JSON array of { name, weight }.`); }
      continue;
    }
    payload[f.key] = val;
  }

  // submissions
  const subsField = ev.fields.find(f => f.type === "submissions");
  if (subsField) {
    const rows = [...$("#subsBox").querySelectorAll(".sub-row")].map(row => {
      const o = {};
      subsField.subFields.forEach(s => {
        const v = row.querySelector(`[name="sub_${s.key}"]`).value.trim();
        if (v) o[s.key] = v;
      });
      return o;
    }).filter(o => Object.keys(o).length);
    if (rows.length === 0) errors.push("Add at least one submission with a repo URL.");
    rows.forEach((r, i) => {
      if (!r.repoUrl) errors.push(`Submission #${i + 1} is missing a repo URL.`);
      else if (!/^https?:\/\//i.test(r.repoUrl)) errors.push(`Submission #${i + 1} repo URL must start with http(s)://`);
    });
    payload.submissions = rows;
  }

  return { payload, errors };
}

// ---------- Submit + poll ----------
$("#resetBtn").addEventListener("click", () => { renderForm(); $("#results").innerHTML = `<p class="placeholder">Cleared.</p>`; });

$("#evaluateBtn").addEventListener("click", async () => {
  const { payload, errors } = collectPayload();
  const errBox = $("#formError");
  if (errors.length) {
    errBox.innerHTML = "Please fix:<br>• " + errors.map(esc).join("<br>• ");
    errBox.classList.remove("hidden");
    return;
  }
  errBox.classList.add("hidden");

  const results = $("#results");
  results.innerHTML = `<p class="placeholder">Submitting…</p>`;

  let res, body;
  try {
    res = await fetch("/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-api-key": getKey() },
      body: JSON.stringify(payload)
    });
    body = await res.json().catch(() => ({}));
  } catch (e) {
    results.innerHTML = banner(`Couldn't reach the server: ${esc(e.message)}`);
    return;
  }

  if (!res.ok || body.success === false) {
    const msg = body.error || `Request failed (HTTP ${res.status})`;
    const help = friendlyError(msg) || (res.status === 401 ? "Your API key is missing or wrong. Open ⚙️ Settings." : null);
    results.innerHTML = banner(msg, help);
    return;
  }

  // Normalize to a list of jobs, labelled by student where possible.
  // cardKey is a STABLE id independent of jobId, since re-evaluating gets a
  // new jobId but must keep updating the same card/chip/body elements.
  const jobs = Array.isArray(body.jobs) ? body.jobs : [{ jobId: body.jobId }];
  const subs = payload.submissions || [];
  const cards = jobs.map((j, i) => ({
    cardKey: subs[i]?.studentId || subs[i]?.repoUrl || `job-${i}`,
    jobId: j.jobId,
    label: subs[i]?.studentName || subs[i]?.repoUrl || payload.repoUrl || `Job ${j.jobId}`,
    // Exact payload to resend on re-evaluate: the whole original request,
    // narrowed to just this one submission when the request fanned out.
    resubmitPayload: subs.length ? { ...payload, submissions: [subs[i]] } : payload
  }));

  results.innerHTML = cards.map(c => cardShell(c)).join("");
  cards.forEach(c => {
    $(`#reeval-${CSS.escape(c.cardKey)}`).addEventListener("click", () => reEvaluate(c));
    pollJob(c);
  });
});

function cardShell(c) {
  return `<div class="result-card" id="card-${esc(c.cardKey)}">
    <div class="result-head">
      <span class="result-title">${esc(c.label)}</span>
      <span class="result-actions">
        <span class="chip waiting" id="chip-${esc(c.cardKey)}">⏳ In queue</span>
        <button type="button" class="ghost-btn small" id="reeval-${esc(c.cardKey)}" title="Re-run this submission">🔁 Re-evaluate</button>
      </span>
    </div>
    <div id="body-${esc(c.cardKey)}"></div>
  </div>`;
}

async function reEvaluate(card) {
  const btn = $(`#reeval-${CSS.escape(card.cardKey)}`);
  const chip = $(`#chip-${CSS.escape(card.cardKey)}`);
  const bodyEl = $(`#body-${CSS.escape(card.cardKey)}`);

  btn.disabled = true;
  chip.className = "chip waiting";
  chip.textContent = "⏳ Re-submitting…";
  bodyEl.innerHTML = "";

  let res, body;
  try {
    res = await fetch("/evaluate", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-api-key": getKey() },
      body: JSON.stringify(card.resubmitPayload)
    });
    body = await res.json().catch(() => ({}));
  } catch (e) {
    chip.className = "chip failed";
    chip.textContent = "❌ Failed";
    bodyEl.innerHTML = banner(`Couldn't reach the server: ${esc(e.message)}`);
    btn.disabled = false;
    return;
  }

  if (!res.ok || body.success === false) {
    const msg = body.error || `Request failed (HTTP ${res.status})`;
    const help = friendlyError(msg) || (res.status === 401 ? "Your API key is missing or wrong. Open ⚙️ Settings." : null);
    chip.className = "chip failed";
    chip.textContent = "❌ Failed";
    bodyEl.innerHTML = banner(msg, help);
    btn.disabled = false;
    return;
  }

  const newJob = Array.isArray(body.jobs) ? body.jobs[0] : { jobId: body.jobId };
  card.jobId = newJob.jobId;
  chip.className = "chip waiting";
  chip.textContent = "⏳ In queue";
  btn.disabled = false;
  pollJob(card);
}

async function pollJob(card) {
  const chip = $(`#chip-${CSS.escape(card.cardKey)}`);
  const bodyEl = $(`#body-${CSS.escape(card.cardKey)}`);
  let tries = 0;
  const jobId = card.jobId; // capture: card.jobId may be reassigned by a later re-evaluate
  const tick = async () => {
    tries++;
    let job;
    try {
      const r = await fetch(`/jobs/${selectedType}/${encodeURIComponent(jobId)}`);
      job = await r.json();
    } catch (e) { bodyEl.innerHTML = small(`Polling error: ${esc(e.message)}`); return; }

    if (!job) { if (tries < 150) setTimeout(tick, 2000); return; }

    const st = STATE[job.state] || { cls: "active", label: job.state };
    chip.className = `chip ${st.cls}`;
    chip.textContent = st.label;

    if (job.state === "completed") { bodyEl.innerHTML = renderResult(job.returnvalue ?? job.result); return; }
    if (job.state === "failed") { bodyEl.innerHTML = renderFailure(job.failedReason); return; }
    if (tries < 150) setTimeout(tick, 2000);
  };
  tick();
}

// ---------- Result rendering ----------
// Author: Arma Sahar — the JS evaluator's feedback is a structured object
// ({ summary, strengths, issues, recommendations }, see feedbackService.js),
// not a plain string like the visual evaluator's. Turn it into readable
// text instead of falling through to a raw JSON dump. Returns null for any
// object that isn't this shape, so it never affects other evaluators.
function formatJsFeedback(fb) {
  if (!fb || typeof fb !== "object" || !("summary" in fb)) return null;
  const parts = [fb.summary];
  if (Array.isArray(fb.issues) && fb.issues.length) parts.push("Issues:\n- " + fb.issues.join("\n- "));
  if (Array.isArray(fb.recommendations) && fb.recommendations.length) parts.push("Recommendations:\n- " + fb.recommendations.join("\n- "));
  return parts.join("\n\n");
}

// Author: Arma Sahar — the backend evaluator's return shape nests the whole
// scoringService.evaluateResults() result under `score` — itself an object
// ({ score, maxScore, rubric_breakdown, pass, feedback, warnings, ... }, see
// backendBugs.md) — not a plain number like every other evaluator. Detect
// and flatten it here rather than changing evaluatorService.js's
// already-tested/committed return contract.
function isBackendScoreShape(s) {
  return s && typeof s === "object" && typeof s.score === "number" && typeof s.maxScore === "number";
}

// Author: Arma Sahar — the React evaluator's scoringService.js returns
// `rubric_breakdown` as a flat { criterionName: pointsAwarded } object (one
// entry per rubric criterion), not an array of { name, points_achieved,
// total_points } like the backend evaluator. There's no per-criterion max in
// this shape (only the overall `maxScore`), so the breakdown table shows
// awarded points without a "/max" column for these rows — showing a
// fabricated max would be worse than omitting it.
function isFlatRubricBreakdown(rb) {
  return rb && typeof rb === "object" && !Array.isArray(rb);
}

function normalizeResults(ret) {
  if (!ret) return [];
  let list = ret.result ?? ret.results ?? ret;
  if (!Array.isArray(list)) list = [list];
  return list.map(e => {
    const backendScore = isBackendScoreShape(e.score) ? e.score : null;

    const score = backendScore ? backendScore.score : (e.score ?? e.total ?? e.evaluation?.score ?? e.evaluation?.total);
    // React evaluator uses `maxScore`, not `maxTotal` — fall back to it.
    const maxTotal = backendScore ? backendScore.maxScore : (e.maxTotal ?? e.maxScore);
    // Prefer the real AI-generated feedback string (top-level `feedback`
    // from evaluatorService.js/feedbackService.js); fall back to
    // scoringService.js's canned pass/fail summary if that's missing.
    const fb = backendScore ? (e.feedback || backendScore.feedback) : (e.feedback ?? e.evaluation?.feedback);

    return {
      name: e.name ?? e.studentName ?? e.evaluation?.studentName,
      score,
      total: e.total,
      maxTotal,
      normalized: e.normalized,
      domScore: e.domScore,
      behaviorScore: e.behaviorScore,
      visualScore: e.visualScore,
      codeScore: e.codeScore,
      pendingManualPoints: e.pendingManualPoints,
      domBreakdown: e.domBreakdown,
      behaviorBreakdown: e.behaviorBreakdown,
      codeBreakdown: e.codeBreakdown,
      visualBreakdown: e.visualBreakdown ?? (Array.isArray(fb?.breakdown) ? fb.breakdown : null),
      rubricBreakdown: backendScore?.rubric_breakdown ?? null,
      flatRubricBreakdown: isFlatRubricBreakdown(e.rubric_breakdown) ? e.rubric_breakdown : null,
      warnings: backendScore?.warnings ?? e.warnings ?? null,
      manualReviewDetail: e.manualReviewDetail,
      feedback: typeof fb === "object" ? (formatJsFeedback(fb) ?? fb.feedback ?? JSON.stringify(fb, null, 2)) : fb,
      aiFeedback: e.aiFeedback ?? e.evaluation?.aiFeedback,
      error: e.error ?? e.evaluation?.error,
      manualReviewItems: e.manualReviewItems,
      status: typeof e.status === "string" ? e.status : null,
      executionLogs: e.execution_logs ?? null,
      raw: e
    };
  });
}

function renderResult(ret) {
  const items = normalizeResults(ret);
  if (!items.length) return small("Completed, but no structured result was returned.") + rawBlock(ret);

  return items.map(it => {
    let html = "";
    if (it.name && items.length > 1) html += `<div class="kv"><b>${esc(it.name)}</b></div>`;

    if (it.error) {
      const help = friendlyError(it.error);
      html += explainBox(it.error, help);
    }

    if (typeof it.score === "number") {
      const pct = round1(typeof it.normalized === "number" ? it.normalized
        : (typeof it.maxTotal === "number" && it.maxTotal > 0 ? (it.score / it.maxTotal) * 100 : null));
      html += `<div class="score"><span class="big">${esc(round1(it.score))}${it.maxTotal != null ? ` / ${esc(round1(it.maxTotal))}` : ""}</span>${pct != null ? `<span class="pct">${pct}%</span>` : ""}</div>`;
      if (pct != null) html += `<div class="bar"><span style="width:${Math.max(0, Math.min(100, pct))}%"></span></div>`;
    }
    // Evaluator status badge ("passed", "failed", "build_failed", "unsupported_project")
    if (it.status) {
      const isPassed = it.status === "pass" || it.status === "passed";
      const isBuildFailed = it.status === "build_failed";
      const isUnsupported = it.status === "unsupported_project";
      const badgeClass = isPassed ? "completed" : "failed";
      const badgeLabel = isPassed ? "✅ Pass" : isBuildFailed ? "⚠️ Build Failed" : isUnsupported ? "⚠️ Unsupported Project" : "❌ Fail";
      html += `<div class="chip ${badgeClass}" style="display:inline-block;margin:4px 0 8px">${badgeLabel}</div>`;
    }

    html += breakdownTable(it);

    if (Array.isArray(it.manualReviewDetail) && it.manualReviewDetail.length) {
      const points = it.pendingManualPoints != null ? it.pendingManualPoints : it.manualReviewDetail.reduce((s, m) => s + (m.weight || 0), 0);
      html += `<div class="explain"><b>Needs manual review (${esc(round1(points))} pts not auto-gradable):</b><ul style="margin:6px 0 0 18px;padding:0">` +
        it.manualReviewDetail.map(m => `<li>${esc(m.description)} <span style="opacity:.7">(${esc(round1(m.weight))} pts) — ${esc(m.reason)}</span></li>`).join("") +
        `</ul></div>`;
    } else if (Array.isArray(it.manualReviewItems) && it.manualReviewItems.length) {
      html += `<div class="explain"><b>Needs manual review:</b> ${esc(it.manualReviewItems.join(", "))}</div>`;
    }
    if (Array.isArray(it.warnings) && it.warnings.length) {
      html += `<div class="explain"><b>Warnings:</b><ul style="margin:6px 0 0 18px;padding:0">` +
        it.warnings.map(w => `<li>${esc(w)}</li>`).join("") + `</ul></div>`;
    }
    if (it.feedback) html += `<div class="feedback">${esc(it.feedback)}</div>`;
    if (it.aiFeedback && typeof it.aiFeedback === "string") {
      html += `<div class="feedback"><b>AI mentor feedback:</b><br>${esc(it.aiFeedback)}</div>`;
    }
    // React evaluator: install/build/Playwright logs — useful for debugging
    // why a build failed or a check didn't pass, kept collapsed by default.
    if (it.executionLogs) {
      html += `<details><summary>Build &amp; test logs</summary><pre class="raw">${esc(it.executionLogs)}</pre></details>`;
    }

    html += rawBlock(it.raw);
    return html;
  }).join("<hr style='border:none;border-top:1px solid var(--line);margin:14px 0'>");
}

// Per-criterion "why did I get/lose these points" table — pulls DOM/behavior
// per-check pass-fail detail and the vision model's own per-item breakdown
// into one place, so a score isn't just an opaque number.
function breakdownTable(it) {
  const rows = [];

  (it.domBreakdown || []).forEach(row => {
    const detail = (row.checks || [])
      .map(c => `${c.passed ? "✓" : "✗"} ${esc(c.selector)} (${esc(c.condition)})`)
      .join("<br>");
    rows.push({ type: "DOM", item: row.item, awarded: row.awarded, max: row.max, detail });
  });

  (it.behaviorBreakdown || []).forEach(row => {
    const detail = (row.checks || [])
      .map(c => `${c.passed ? "✓" : "✗"} click ${esc(c.selector)}`)
      .join("<br>");
    rows.push({ type: "Behavior", item: row.item, awarded: row.awarded, max: row.max, detail });
  });

  (it.visualBreakdown || []).forEach(row => {
    rows.push({ type: "Visual", item: row.item, awarded: row.awarded, max: row.max, detail: esc(row.reason || "") });
  });

  (it.codeBreakdown || []).forEach(row => {
    const detail = row.checks
      ? row.checks.map(c => `${c.passed ? "✓" : "✗"} ${esc(c.label || "source contains")} "${esc(c.pattern)}"`).join("<br>")
      : esc(row.reason || "");
    rows.push({ type: "Code", item: row.item, awarded: row.awarded, max: row.max, detail });
  });

  // Backend evaluator: scoringService.js's rubric_breakdown ({ name,
  // points_achieved, total_points }, one row per rubric criterion).
  (it.rubricBreakdown || []).forEach(row => {
    rows.push({ type: "Rubric", item: row.name, awarded: row.points_achieved, max: row.total_points, detail: "" });
  });

  // React evaluator: scoringService.js's rubric_breakdown is a flat
  // { criterionName: pointsAwarded } object — no per-criterion max is
  // available in this shape (only the overall maxScore, shown above in the
  // score bar), so `max` is left undefined and the Points cell just shows
  // the awarded value rather than a fabricated "/max".
  if (it.flatRubricBreakdown) {
    Object.entries(it.flatRubricBreakdown).forEach(([name, awarded]) => {
      rows.push({ type: "Rubric", item: name, awarded, max: undefined, detail: "" });
    });
  }

  if (!rows.length) return "";

  return `<table class="breakdown" style="width:100%;border-collapse:collapse;margin:10px 0;font-size:13px">
    <thead><tr style="text-align:left;opacity:.7">
      <th style="padding:4px 8px 4px 0">Type</th><th style="padding:4px 8px">Criterion</th>
      <th style="padding:4px 8px">Points</th><th style="padding:4px 0">Why</th>
    </tr></thead>
    <tbody>${rows.map(r => `<tr style="border-top:1px solid var(--line)">
      <td style="padding:4px 8px 4px 0;opacity:.7">${esc(r.type)}</td>
      <td style="padding:4px 8px">${esc(r.item)}</td>
      <td style="padding:4px 8px;white-space:nowrap">${esc(round1(r.awarded))}${r.max != null ? ` / ${esc(round1(r.max))}` : ""}</td>
      <td style="padding:4px 0">${r.detail}</td>
    </tr>`).join("")}</tbody>
  </table>`;
}

function renderFailure(reason) {
  const help = friendlyError(reason) || "The job failed. See the technical details below.";
  return explainBox(reason || "Unknown error", help);
}

// ---------- small html helpers ----------
function banner(msg, help) {
  return `<div class="banner error">${help ? `<b>${esc(help)}</b><br><span style="opacity:.8">${esc(msg)}</span>` : esc(msg)}</div>`;
}
function explainBox(msg, help) {
  return `<div class="explain">${help ? `<b>${esc(help)}</b><br>` : ""}<details><summary>Technical details</summary><pre class="raw">${esc(msg)}</pre></details></div>`;
}
function small(t) { return `<p class="kv" style="color:var(--muted)">${esc(t)}</p>`; }
function rawBlock(obj) {
  return `<details><summary>Show raw JSON</summary><pre class="raw">${esc(JSON.stringify(obj, null, 2))}</pre></details>`;
}

// ---------- boot ----------
initSettings();
renderPicker();
renderForm();
