import crypto from "crypto";
import OpenAI from "openai";
import { RubricParseError, normalizeRubric } from "./rubricSchema.js";

export { RubricParseError, normalizeRubric };

// V-42: lazy init — constructing the client at import time crashes the whole
// server on boot when OPENAI_API_KEY is unset (even for unrelated evaluators).
let _openai;
function getOpenAI() {
  if (!_openai) {
    _openai = new OpenAI({
      apiKey: process.env.OPENAI_API_KEY,
      baseURL: process.env.OPENAI_BASE_URL || undefined,
    });
  }
  return _openai;
}

const RUBRIC_CACHE = new Map();
const RUBRIC_CACHE_MAX = 50;

export async function parseRubricWithSelectors(text) {
  if (!text || typeof text !== "string") {
    throw new RubricParseError("Rubric text is required");
  }

  const cacheKey = crypto.createHash("sha256").update(text.trim()).digest("hex");
  if (RUBRIC_CACHE.has(cacheKey)) {
    return JSON.parse(JSON.stringify(RUBRIC_CACHE.get(cacheKey)));
  }

  const prompt = `
Convert the rubric into STRICT JSON.

VERY IMPORTANT CLASSIFICATION RULES — this tool has FIVE ways to verify a
criterion. Pick the one that actually matches; almost everything is
verifiable one of these ways, so "manual" should be RARE.

- "dom" → HTML structure/elements in the rendered page. Conditions:
    "exists" | "visible" | "textContains" | "attr" (one-time static checks)
    "updatesOverTime" → proves a value is actually LIVE by reading it twice
      ~1 second apart and requiring it to change (e.g. "clock updates every
      second", "counter increments").
    "matchesNow" → proves a displayed value is factually correct by comparing
      it to the real current date/time. "expected" MUST be the display
      format using tokens YYYY, MM, DD, HH (24h), hh (12h), mm, ss, A
      (AM/PM) — e.g. "DD/MM/YYYY" or "hh:mm:ss A".
- "behavior" → a click. TWO modes:
    (default) the click NAVIGATES to a new URL/tab (e.g. an external link).
    mode:"stateChange" → the click changes something ON THE SAME PAGE without
      navigating (a toggle button, a format switcher, a show/hide). Give
      "targetSelector" (the element whose text is expected to change) — the
      check passes if that element's text is different after the click.
- "visual" → subjective layout/design/appearance judged from a SCREENSHOT
  (alignment, colors, spacing, typography, responsiveness). Only for things
  visibly different between two static images.
- "code" → verified against the STUDENT'S ACTUAL SOURCE FILES (html/css/js),
  not the rendered page. THREE kinds of checks:
    pattern checks → deterministic: does the source contain/match this
      pattern (e.g. "uses setInterval() correctly" → check the source calls
      setInterval(...); "uses Date() object" → check for "new Date(").
      checks = [{ "pattern": "setInterval(" }, { "pattern": "new Date(" }]
      (optionally "kind": "regex" instead of a plain substring match)
    filesLinked check → for "files linked correctly" / "proper project
      structure" style criteria. DO NOT pattern-match the literal filename
      (e.g. checking the source contains "styles.css") — that's checking the
      wrong thing: index.html doesn't reference its own filename, and a
      student who names their file "style.css" instead of "styles.css" would
      wrongly fail despite linking it correctly. Instead check for a REAL
      <link rel="stylesheet" href="*.css"> / <script src="*.js"> tag,
      independent of the exact filename: checks = [{ "kind": "filesLinked",
      "target": "css" }, { "kind": "filesLinked", "target": "js" }]
    quality check → subjective judgment (e.g. "code quality", "meaningful
      variable names", "proper indentation") — GPT reads the actual source
      text and judges it directly. checks = [{ "kind": "quality" }]
  Use "code" for ANYTHING about how the JS/HTML/CSS is written, which APIs it
  calls, or whether resources are linked — that is exactly what source-code
  checking is for.
- "manual" → LAST RESORT ONLY, for a criterion none of the above can cover.
  Do not use it just because a criterion is hard — dom/behavior/code between
  them cover structure, live values, in-page interaction, and source
  correctness/quality. Misclassifying something as "manual" that could have
  been "code" or "dom" produces an avoidable 0 that looks like a bug.

- Each item must have:
  description (string)
  weight (number)
  type ("dom" | "behavior" | "visual" | "code" | "manual")

EXAMPLES:

Favicon (static structure) →
{ "type": "dom", "description": "Has a favicon", "weight": 5,
  "checks": [{ "selector": "link[rel='icon']", "condition": "exists" }] }

Live clock (must be observed changing) →
{ "type": "dom", "description": "Time updates every second", "weight": 10,
  "checks": [{ "selector": "#time", "condition": "updatesOverTime" }] }

Date is factually correct (not just present) →
{ "type": "dom", "description": "Displays current date in DD/MM/YYYY format", "weight": 10,
  "checks": [{ "selector": "#date, .date, [id*='date']", "condition": "matchesNow", "expected": "DD/MM/YYYY" }] }

Twitter click (navigates to an external URL) →
{ "type": "behavior", "description": "Twitter link opens twitter.com", "weight": 5,
  "checks": [{ "action": "click", "selector": "a[href*='twitter']", "expected": "twitter.com" }] }

In-page toggle (no navigation — same-page state change) →
{ "type": "behavior", "description": "12/24-hour toggle switches format correctly", "weight": 15,
  "checks": [{ "action": "click", "selector": "#toggleBtn", "mode": "stateChange", "targetSelector": "#time" }] }

Uses required JS APIs (objective — check the actual source) →
{ "type": "code", "description": "Uses Date() object and setInterval() correctly", "weight": 15,
  "checks": [{ "pattern": "new Date(" }, { "pattern": "setInterval(" }] }

Files linked correctly (check the actual <link>/<script> tags, not filenames) →
{ "type": "code", "description": "Proper project structure — files linked correctly", "weight": 5,
  "checks": [{ "kind": "filesLinked", "target": "css" }, { "kind": "filesLinked", "target": "js" }] }

Code quality (subjective — GPT reads the actual source) →
{ "type": "code", "description": "Proper indentation, meaningful variable names, readable code", "weight": 5,
  "checks": [{ "kind": "quality" }] }

Return a JSON object of the form: { "items": [ ...rubric items... ] }

Rubric:
${text}
`;

  const model = process.env.OPENAI_MODEL || "deepseek-chat";
  const response = await getOpenAI().chat.completions.create({
    model,
    temperature: 0,
    response_format: { type: "json_object" },
    messages: [{ role: "user", content: prompt }]
  });

  const raw = response.choices?.[0]?.message?.content ?? "";

  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    throw new RubricParseError(`Rubric JSON parse failed: ${err.message}`);
  }

  const normalized = normalizeRubric(parsed);
  if (RUBRIC_CACHE.size >= RUBRIC_CACHE_MAX) {
    RUBRIC_CACHE.delete(RUBRIC_CACHE.keys().next().value);
  }
  RUBRIC_CACHE.set(cacheKey, normalized);
  return JSON.parse(JSON.stringify(normalized));
}
