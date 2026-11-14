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

  let targetTotal = 100;
  try {
    const directParse = JSON.parse(text.trim());
    const candidateList = Array.isArray(directParse) ? directParse : (directParse?.items || directParse?.criteria || directParse?.rubric);
    if (Array.isArray(candidateList)) {
      const wSum = candidateList.reduce((s, it) => s + (Number(it.weight || it.max_marks || it.points) || 0), 0);
      if (wSum > 0) targetTotal = wSum;
      if (candidateList.length > 0 && candidateList.every(i => i && i.type && Array.isArray(i.checks))) {
        const normalized = normalizeRubric(candidateList, targetTotal);
        RUBRIC_CACHE.set(cacheKey, normalized);
        return JSON.parse(JSON.stringify(normalized));
      }
    }
  } catch {}

  const prompt = `
Convert the rubric into STRICT JSON.

TOTAL WEIGHT CONSTRAINT: The total sum of weights across all items in the returned JSON MUST EQUAL EXACTLY ${targetTotal}.
If you split a criterion into multiple checks (e.g. both a "code" check for DOCTYPE and a "dom" check for structure), divide the weight proportionally so the total never exceeds ${targetTotal}.

VERY IMPORTANT CLASSIFICATION RULES — this tool has FIVE ways to verify a
criterion. Pick the one that actually matches; almost everything is
verifiable one of these ways, so "manual" should be RARE.

- "dom" → HTML structure/elements in the rendered page. Conditions:
    "exists" | "visible" | "textContains" | "attr" (one-time static checks)
    "style" → verifies computed CSS properties directly via browser engine (e.g. "uses flexbox layout", "header has dark background", "card has border-radius"). Give "property" (e.g. "display") and "expected" (e.g. "flex, inline-flex" or "10px").
    "updatesOverTime" → proves a value is actually LIVE by reading it twice
      ~1 second apart and requiring it to change (e.g. "clock updates every
      second", "counter increments").
    "matchesNow" → proves a displayed value is factually correct by comparing
      it to the real current date/time. "expected" MUST be the display
      format using tokens YYYY, MM, DD, HH (24h), hh (12h), mm, ss, A
      (AM/PM) — e.g. "DD/MM/YYYY" or "hh:mm:ss A".
    CRITICAL SELECTOR RULE: NEVER guess arbitrary class names or IDs (like .poster-card, .photo-item, #myButton) unless they are explicitly named in the rubric text. Instead, write PURPOSE-DRIVEN semantic selectors using standard HTML5 tags and broad attribute matches:
    - Semantic tags: header, nav, main, section, article, figure, figcaption, footer, h1, h2, h3, img, button, form, input.
    - Flexible classes: [class*="card"], .card, article, figure or [class*="gallery"], main, .container
    - Multiple options separated by commas: header, [role="banner"], nav
- "behavior" → an interaction. Modes:
    (default) action:"click" → clicks a link or button. Navigates or opens a new URL/tab.
    action:"type" → types text into an <input> or <textarea>. Give "selector" and "value" (e.g. "Buy milk").
    action:"submit" → submits a form. Give "selector" (e.g. "form, #todo-form").
    mode:"stateChange" → the click changes something ON THE SAME PAGE without
      navigating (a toggle button, a format switcher, an item addition, a modal/accordion). Give
      "targetSelector" (the element whose text or state is expected to change) — the
      check passes if that element's content changes after the click.
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
      independent of the exact filename: checks = [{ "kind": "filesLinked", "target": "css" }] (only add "target": "js" if the rubric explicitly involves JavaScript).
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

CSS Flexbox / Grid Layout (verifies real computed styling deterministically) →
{ "type": "dom", "description": "Navigation links use Flexbox layout", "weight": 10,
  "checks": [{ "selector": "nav, header nav, .navbar", "condition": "style", "property": "display", "expected": "flex, inline-flex" }] }

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

  const normalized = normalizeRubric(parsed, targetTotal);
  if (RUBRIC_CACHE.size >= RUBRIC_CACHE_MAX) {
    RUBRIC_CACHE.delete(RUBRIC_CACHE.keys().next().value);
  }
  RUBRIC_CACHE.set(cacheKey, normalized);
  return JSON.parse(JSON.stringify(normalized));
}
