// DOM checks. V-22: honor `check.condition` instead of only testing existence.
// Supported conditions: "exists" (default), "visible", "textContains", "attr",
// "updatesOverTime", "matchesNow".

// Parse a rubric-supplied format string (e.g. "DD/MM/YYYY", "hh:mm:ss A") into
// a regex + the token order, so a displayed value can be checked against the
// real current date/time rather than just "some text is present".
function buildFormatRegex(format) {
  const tokenMap = [
    ["YYYY", "(\\d{4})"],
    ["MM", "(\\d{1,2})"],
    ["DD", "(\\d{1,2})"],
    ["HH", "(\\d{1,2})"],
    ["hh", "(\\d{1,2})"],
    ["mm", "(\\d{1,2})"],
    ["ss", "(\\d{1,2})"],
    ["A", "(AM|PM|am|pm)"]
  ];
  let pattern = "";
  const tokens = [];
  let i = 0;
  while (i < format.length) {
    const hit = tokenMap.find(([tok]) => format.slice(i, i + tok.length) === tok);
    if (hit) {
      pattern += hit[1];
      tokens.push(hit[0]);
      i += hit[0].length;
    } else {
      if (format[i] === '/' || format[i] === '-' || format[i] === '.') {
        pattern += "[-/.\\s]";
      } else {
        pattern += format[i].replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      }
      i++;
    }
  }
  return { regex: new RegExp(pattern), tokens };
}

/**
 * Does `text` (as parsed via `format`) match the real current date/time?
 * Date components must match exactly; time is allowed a small tolerance
 * (page load + evaluation latency between render and this check).
 */
export function valueMatchesNow(text, format) {
  if (!format) return false;
  const { regex, tokens } = buildFormatRegex(format);
  const m = regex.exec(text || "");
  if (!m) return false;

  const parts = {};
  tokens.forEach((tok, idx) => {
    parts[tok] = m[idx + 1];
  });

  const now = new Date();
  if (parts.YYYY && Number(parts.YYYY) !== now.getFullYear()) return false;
  if (parts.MM && Number(parts.MM) !== now.getMonth() + 1) return false;
  if (parts.DD && Number(parts.DD) !== now.getDate()) return false;

  if (parts.HH || parts.hh) {
    let hour = Number(parts.HH ?? parts.hh);
    if (parts.hh && parts.A) {
      const ampm = parts.A.toUpperCase();
      if (ampm === "PM" && hour !== 12) hour += 12;
      if (ampm === "AM" && hour === 12) hour = 0;
    }
    const minute = parts.mm ? Number(parts.mm) : 0;
    const nowMinutes = now.getHours() * 60 + now.getMinutes();
    const valMinutes = hour * 60 + minute;
    const diff = Math.min(Math.abs(nowMinutes - valMinutes), 1440 - Math.abs(nowMinutes - valMinutes));
    if (diff > 2) return false; // tolerate a couple minutes of check latency
  }

  return true;
}

// Confirmed live (2026-07-08, Digital Clock batch test): the rubric parser
// generates a bare ID selector ("#container") for element names the rubric
// doesn't specify id-vs-class for, but real submissions commonly implement
// them as classes ("class='container'") — an ID selector can never match a
// class attribute, so every student failed a check they'd actually satisfied.
//
// Confirmed live again the same day on a different student: the parser wrote
// "#toggleBtn" (camelCase) for a rubric line about a toggle button, but the
// real element was "id='toggle-btn'" (kebab-case) — a working toggle scored 0
// purely on naming-convention mismatch. Rather than rely on the model always
// guessing the exact spelling AND the exact id/class convention, widen a bare
// single id/class selector across both attribute types and the common naming
// conventions (camelCase, kebab-case, snake_case). Selectors that are already
// more specific (combinators, attribute selectors, tag names) are untouched.
function toCamelCase(name) {
  return name.replace(/[-_]+(.)?/g, (_, c) => (c ? c.toUpperCase() : ""));
}
function toKebabCase(name) {
  return name.replace(/([a-z0-9])([A-Z])/g, "$1-$2").replace(/_/g, "-").toLowerCase();
}
function toSnakeCase(name) {
  return toKebabCase(name).replace(/-/g, "_");
}

export function widenSelector(selector) {
  const bareIdOrClass = /^([.#])([\w-]+)$/.exec((selector || "").trim());
  if (!bareIdOrClass) return selector;
  const [, , name] = bareIdOrClass;
  const variants = new Set([name, toCamelCase(name), toKebabCase(name), toSnakeCase(name)]);
  const selectors = [];
  for (const v of variants) selectors.push(`#${v}`, `.${v}`);
  return selectors.join(", ");
}

export async function resolveElement(page, selector) {
  if (!selector) return null;
  const rawSel = String(selector).trim();

  // 1. Try widened selector directly (handles exact id/class with camelCase, kebab-case, snake_case)
  try {
    const el = await page.$(widenSelector(rawSel));
    if (el) return el;
  } catch {}

  // 2. Extract keywords from selector: e.g. #toggle-btn -> ["toggle", "btn"], #calc-screen -> ["calc", "screen"]
  const clean = rawSel.replace(/^[.#]/, '').replace(/[^a-zA-Z0-9_-]/g, ' ');
  const tokens = clean.split(/[-_\s]+/).filter(t => t.length >= 3);

  // Try substring attribute selectors on id, class, name, and data attributes for each token
  for (const token of tokens) {
    try {
      const el = await page.$(`[id*="${token}" i], [class*="${token}" i], [name*="${token}" i], [data-testid*="${token}" i]`);
      if (el) return el;
    } catch {}
  }

  // 3. Specific semantic fallback: search for button, input, or element containing the token's text or aria-label
  for (const token of tokens) {
    if (token.length < 3) continue;
    try {
      const el = await page.$(`button:has-text("${token}"), [aria-label*="${token}" i], [placeholder*="${token}" i], [title*="${token}" i]`);
      if (el) return el;
    } catch {}
  }

  return null;
}

async function evaluateCheck(page, check) {
  let el = await resolveElement(page, check.selector);

  const condition = (check.condition || "exists").toLowerCase();

  // If element not found directly, check if the condition is satisfied globally on the page
  if (!el) {
    if (condition === "matchesnow") {
      const now = new Date();
      const currentYear = String(now.getFullYear());
      const currentDay = String(now.getDate());
      const currentDayPadded = String(now.getDate()).padStart(2, '0');
      const currentMonthNum = String(now.getMonth() + 1);
      const currentMonthPadded = String(now.getMonth() + 1).padStart(2, '0');
      const months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
      const monthName = months[now.getMonth()];
      const bodyText = (await page.evaluate(() => document.body ? document.body.innerText : "")).toLowerCase();
      
      const hasYear = bodyText.includes(currentYear);
      const hasMonth = bodyText.includes(monthName) ||
        new RegExp(`(^|[^0-9])${currentMonthPadded}([^0-9]|$)`).test(bodyText) ||
        new RegExp(`(^|[^0-9])${currentMonthNum}([^0-9]|$)`).test(bodyText);
      const hasDay = new RegExp(`(^|[^0-9])${currentDayPadded}([^0-9]|$)`).test(bodyText) ||
        new RegExp(`(^|[^0-9])${currentDay}([^0-9]|$)`).test(bodyText);

      if (hasYear && hasMonth && hasDay) {
        return true;
      }
    } else if (condition === "textcontains" && check.expected) {
      const bodyText = (await page.evaluate(() => document.body ? document.body.innerText : "")).toLowerCase();
      if (bodyText.includes(String(check.expected).toLowerCase())) {
        return true;
      }
    }
    return false;
  }

  switch (condition) {
    case "exists": {
      const isContainer = /card|container|wrapper|box|panel/i.test(check.selector);
      if (isContainer) {
        const childCount = await el.evaluate(node => node.children.length).catch(() => 1);
        const text = ((await el.textContent()) || "").trim();
        if (childCount === 0 && text.length === 0) {
          return false;
        }
      }
      return true;
    }

    case "visible": {
      const isVisible = await el.isVisible();
      if (!isVisible) return false;
      const isContainer = /card|container|wrapper|box|panel/i.test(check.selector);
      if (isContainer) {
        const childCount = await el.evaluate(node => node.children.length).catch(() => 1);
        const text = ((await el.textContent()) || "").trim();
        if (childCount === 0 && text.length === 0) {
          return false;
        }
      }
      return true;
    }

    case "textcontains": {
      const text = (await el.textContent()) || "";
      return check.expected
        ? text.toLowerCase().includes(String(check.expected).toLowerCase())
        : text.trim().length > 0;
    }

    case "attr": {
      const [attr, value] = String(check.expected || "").split("=");
      const actual = await el.getAttribute(attr);
      if (actual === null) return false;
      return value ? actual.includes(value) : true;
    }

    case "updatesovertime": {
      const before = ((await el.textContent()) || "").trim();
      if (!before) return false;
      // Poll up to 3500ms (covers 1s interval even with event-loop/system jitter)
      const start = Date.now();
      while (Date.now() - start < 3500) {
        await page.waitForTimeout(200);
        const elAgain = (await resolveElement(page, check.selector)) || (await page.$(widenSelector(check.selector))) || el;
        const current = elAgain ? ((await elAgain.textContent()) || "").trim() : "";
        if (current && current !== before) {
          return true;
        }
      }
      return false;
    }

    case "matchesnow": {
      const text = ((await el.textContent()) || "").trim();
      if (valueMatchesNow(text, check.expected)) return true;

      // Tolerant check: verify that text actually contains real current date components (Year, Month, and Day)
      const now = new Date();
      const currentYear = String(now.getFullYear());
      const currentDay = String(now.getDate());
      const currentDayPadded = String(now.getDate()).padStart(2, '0');
      const currentMonthNum = String(now.getMonth() + 1);
      const currentMonthPadded = String(now.getMonth() + 1).padStart(2, '0');
      const months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
      const monthName = months[now.getMonth()];

      const lower = text.toLowerCase();
      const hasYear = lower.includes(currentYear);
      const hasMonth = lower.includes(monthName) ||
        new RegExp(`(^|[^0-9])${currentMonthPadded}([^0-9]|$)`).test(lower) ||
        new RegExp(`(^|[^0-9])${currentMonthNum}([^0-9]|$)`).test(lower);
      const hasDay = new RegExp(`(^|[^0-9])${currentDayPadded}([^0-9]|$)`).test(lower) ||
        new RegExp(`(^|[^0-9])${currentDay}([^0-9]|$)`).test(lower);

      if (hasYear && hasMonth && hasDay) {
        return true;
      }

      // Tolerant check for Time components (Hour and Minute with ±1 min tolerance)
      const curHours24 = now.getHours();
      const curHours12 = curHours24 % 12 || 12;
      const curMins = now.getMinutes();
      const possibleMins = [curMins, (curMins + 59) % 60, (curMins + 1) % 60];
      const hoursPattern = `(^|[^0-9])(${curHours24}|${String(curHours24).padStart(2, '0')}|${curHours12}|${String(curHours12).padStart(2, '0')})([^0-9]|$)`;
      const minsPattern = `(^|[^0-9])(${possibleMins.flatMap(m => [m, String(m).padStart(2, '0')]).join('|')})([^0-9]|$)`;
      const hasTime = new RegExp(hoursPattern).test(lower) && new RegExp(minsPattern).test(lower);

      if (hasTime && (lower.includes(':') || /am|pm/.test(lower))) {
        return true;
      }

      return false;
    }

    default:
      return false;
  }
}

export async function runDynamicDomChecks(page, rubric) {
  const results = {};

  for (const item of rubric) {
    if (item.type !== "dom" || !item.checks) continue;
    for (let i = 0; i < item.checks.length; i++) {
      const check = item.checks[i];
      const indexedKey = `${item.description} :: ${i} :: ${check.selector}`;
      const unindexedKey = `${item.description} :: ${check.selector}`;
      try {
        const passed = await evaluateCheck(page, check);
        results[indexedKey] = passed;
        results[unindexedKey] = passed;
      } catch {
        results[indexedKey] = false;
        results[unindexedKey] = false;
      }
    }
  }

  return results;
}
