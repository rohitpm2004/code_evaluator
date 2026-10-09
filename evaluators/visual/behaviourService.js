// Behavior checks. V-11: handle BOTH new-tab (target=_blank) and same-tab
// navigation — the old code only waited for a new page and so failed (and hung
// 30s on) every in-tab link. V-38: guard a missing/empty `expected`.
//
// Confirmed live (2026-07-08): a click that changes something ON THE SAME
// PAGE without navigating (a toggle button, a format switcher) always failed
// here, because the only pass condition was "did the URL change". That's a
// real capability gap, not a correctness bug in the student's code — added
// `mode: "stateChange"` below to check a target element's text/attribute
// before vs after the click instead of assuming navigation.
//
// Each check uses short timeouts and resets the page to its starting URL so one
// click's navigation doesn't break the next check.
import { widenSelector, resolveElement } from "./domService.js";

const CHECK_TIMEOUT = 5000;
const CLOCK_SELECTOR = "#time, #clock, .time, .clock";

async function runStateChangeCheck(page, check) {
  const targetSelector = check.targetSelector || check.selector;

  const btn = (await resolveElement(page, check.selector)) || (await page.$(widenSelector(check.selector)));
  if (!btn) return false;

  const beforeEl = (await resolveElement(page, targetSelector)) || (await page.$(widenSelector(targetSelector)));
  const before = beforeEl ? ((await beforeEl.textContent()) || "").trim() : null;
  const beforeBtn = ((await btn.textContent()) || "").trim();

  // Capture fallback clock target text BEFORE click
  const clockEl = await resolveElement(page, CLOCK_SELECTOR);
  const beforeClock = clockEl ? ((await clockEl.textContent()) || "").trim() : null;

  await btn.click().catch(() => {});

  // Poll up to 3000ms (covers immediate click handlers AND interval-based ticks under CPU load)
  const start = Date.now();
  while (Date.now() - start < 3000) {
    await page.waitForTimeout(150);

    const afterEl = (await resolveElement(page, targetSelector)) || (await page.$(widenSelector(targetSelector)));
    const after = afterEl ? ((await afterEl.textContent()) || "").trim() : null;
    if (before !== null && after !== null && before !== after) return true;

    const afterBtn = ((await btn.textContent()) || "").trim();
    if (beforeBtn && afterBtn && beforeBtn !== afterBtn) return true;

    const isClockCheck = 
      (targetSelector || "").toLowerCase().includes("clock") || 
      (targetSelector || "").toLowerCase().includes("time") || 
      (check.selector || "").toLowerCase().includes("clock") || 
      (check.selector || "").toLowerCase().includes("format");

    if (isClockCheck && beforeClock !== null) {
      const clockAgain = (await resolveElement(page, CLOCK_SELECTOR)) || clockEl;
      const afterClock = clockAgain ? ((await clockAgain.textContent()) || "").trim() : null;
      if (afterClock !== null) {
        // Only pass if the clock format itself changed (e.g. 12h/24h toggle, AM/PM presence),
        // NOT merely because seconds ticked forward
        const hasAmPmBefore = /am|pm/i.test(beforeClock);
        const hasAmPmAfter = /am|pm/i.test(afterClock);
        if (hasAmPmBefore !== hasAmPmAfter) return true;
        if (check.expected && afterClock.toLowerCase().includes(check.expected.toLowerCase())) return true;
      }
    }
  }

  return false;
}

export default async function runBehaviorChecks(page, rubric) {
  const results = {};
  const startUrl = page.url();

  for (const item of rubric) {
    if (item.type !== "behavior" || !item.checks) continue;

    for (let i = 0; i < item.checks.length; i++) {
      const check = item.checks[i];
      const indexedKey = `${item.description} :: ${i} :: ${check.selector}`;
      const unindexedKey = `${item.description} :: ${check.selector}`;

      const setCheckResult = (val) => {
        results[indexedKey] = val;
        results[unindexedKey] = val;
      };

      if (check.mode === "stateChange") {
        try {
          const passed = await runStateChangeCheck(page, check);
          setCheckResult(passed);
        } catch {
          setCheckResult(false);
        }
        continue;
      }

      if (check.action !== "click") {
        setCheckResult(false);
        continue;
      }

      const expected =
        typeof check.expected === "string"
          ? check.expected.replace(/^url contains\s*/i, "").trim()
          : "";

      try {
        const el = (await resolveElement(page, check.selector)) || (await page.$(widenSelector(check.selector)));
        if (!el) {
          setCheckResult(false);
          continue;
        }

        // Race a new tab against an in-tab navigation; whichever happens wins.
        const newPagePromise = page
          .context()
          .waitForEvent("page", { timeout: CHECK_TIMEOUT })
          .catch(() => null);
        const navPromise = page
          .waitForNavigation({ timeout: CHECK_TIMEOUT })
          .catch(() => null);

        await el.click().catch(() => {});

        const newPage = await newPagePromise;
        let resultUrl;

        if (newPage) {
          await newPage
            .waitForLoadState("load", { timeout: CHECK_TIMEOUT })
            .catch(() => {});
          resultUrl = newPage.url();
          await newPage.close().catch(() => {});
        } else {
          await navPromise;
          resultUrl = page.url();
        }

        const passed = expected
          ? resultUrl.includes(expected)
          : resultUrl !== startUrl; // no expected → any navigation counts as pass
        setCheckResult(passed);

        // Reset for the next check
        if (page.url() !== startUrl) {
          await page.goto(startUrl, { timeout: 10000 }).catch(() => {});
        }
      } catch {
        setCheckResult(false);
      }
    }
  }

  return results;
}

