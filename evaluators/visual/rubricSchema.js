// Pure rubric validation (V-09). No OpenAI / no I/O — unit-testable
// (see scripts/test-rubric-fallback.mjs) and importable without an API key.

/**
 * Thrown when the rubric cannot be turned into a valid structured rubric.
 * The worker treats this as PERMANENT (no retry, V-17) and flags the job for
 * manual review instead of silently scoring everyone 0 (V-09).
 */
export class RubricParseError extends Error {
  constructor(message) {
    super(message);
    this.name = "RubricParseError";
  }
}

// "code": the criterion is verified against the student's actual source
// files (read after clone, see codeService.js) rather than the rendered
// page — either deterministic pattern checks (e.g. "uses setInterval()") or,
// when a check has kind:"quality", a GPT judgment of the real source text
// (e.g. code quality/readability). Added once it became clear these criteria
// were being wrongly punted to "manual" when they were actually verifiable.
//
// "manual": a genuinely last-resort type for the rare criterion this tool
// still can't verify any other way. Earns 0 automatically and is surfaced via
// manualReviewItems()/manualReviewDetail() rather than being force-fit into
// dom/behavior/visual/code and silently failing.
export const VALID_TYPES = new Set(["dom", "behavior", "visual", "code", "manual"]);

/**
 * Accept the shapes gpt-4o realistically returns and validate them:
 *   - a bare array
 *   - an object wrapping the array under items / rubric / criteria
 * Throws RubricParseError on anything unusable — never returns [].
 */
export function normalizeRubric(parsed, targetTotal = null) {
  let arr = parsed;
  if (!Array.isArray(arr) && arr && typeof arr === "object") {
    arr = arr.items || arr.rubric || arr.criteria || null;
  }
  if (!Array.isArray(arr)) {
    throw new RubricParseError("Rubric is not a JSON array");
  }
  if (arr.length === 0) {
    throw new RubricParseError("Rubric is empty");
  }

  const items = arr.map((item, i) => {
    if (!item || typeof item !== "object") {
      throw new RubricParseError(`Rubric item ${i} is not an object`);
    }
    const { description, weight, type } = item;
    if (typeof description !== "string" || !description.trim()) {
      throw new RubricParseError(`Rubric item ${i} is missing a description`);
    }
    if (typeof weight !== "number" || !Number.isFinite(weight)) {
      throw new RubricParseError(`Rubric item ${i} has an invalid weight`);
    }
    if (!VALID_TYPES.has(type)) {
      throw new RubricParseError(`Rubric item ${i} has invalid type: ${type}`);
    }
    return {
      description: description.trim(),
      weight: Math.max(0, weight),
      type,
      checks: Array.isArray(item.checks) ? item.checks : []
    };
  });

  // If targetTotal is specified, or if parsed weights sum to > 100, normalize to target/100
  const currentTotal = items.reduce((sum, it) => sum + it.weight, 0);
  const target = targetTotal && targetTotal > 0 ? targetTotal : (currentTotal > 100 ? 100 : null);

  if (target && currentTotal > 0 && Math.abs(currentTotal - target) > 0.01) {
    const scale = target / currentTotal;
    let runningSum = 0;
    items.forEach((it, idx) => {
      if (idx === items.length - 1) {
        it.weight = Math.round((target - runningSum) * 100) / 100;
      } else {
        it.weight = Math.round((it.weight * scale) * 100) / 100;
        runningSum += it.weight;
      }
    });
  }

  return items;
}
