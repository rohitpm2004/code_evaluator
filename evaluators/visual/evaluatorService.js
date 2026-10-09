// Visual evaluator orchestrator.
//
// Batch 2 (this commit): resource lifecycle is now leak-proof.
//   - V-05/V-06: every browser/context/server is released in a single `finally`,
//     even when the reference screenshot or navigation throws.
//   - V-16/V-25: all artifacts (screenshots) live in a per-job temp dir that is
//     deleted in `finally`; nothing is written into the source tree anymore
//     (the old `final_scores.json` write is gone — results are returned instead).
//
// Scoring / rubric / vision correctness is addressed in Batch 3.
import crypto from "crypto";
import { parseRubricWithSelectors } from "./rubricService.js";
import { scanStudentFolders } from "./scannerService.js";
import { runDynamicDomChecks } from "./domService.js";
import runBehaviorChecks from "./behaviourService.js";
import buildVisionPrompt from "./utils/promptBuilder.js";
import {
  computeDomScore,
  computeBehaviorScore,
  manualReviewItems,
  manualReviewDetail,
  assembleScore,
  clampScore,
  buildDomBreakdown,
  buildBehaviorBreakdown
} from "./scoring.js";
import { readSourceText, computeCodeScore } from "./codeService.js";
import fs from "fs/promises";
import os from "os";
import path from "path";
import OpenAI from "openai";
import { getBrowserPool } from "./browserPool.js";
import { startStaticServer } from "./localServerService.js";
import { assertSafeUrl } from "./utils/urlGuard.js";
import { cloneRepo, deleteRepo } from "../react/repoService.js";
import logger from "../../config/logger.js";

// V-42: lazy init so a missing OPENAI_API_KEY doesn't crash the server at boot.
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

// Consistent capture geometry for student vs reference (V-23/V-26).
const VIEWPORT = { width: 1366, height: 900 };

// Reference-screenshot cache (V-29): the reference design is identical across all
// submissions of an assignment, so render it once per (assignmentId, expectedUrl)
// and reuse the PNG buffer. Stores in-flight promises to dedupe concurrent jobs.
const EXPECTED_CACHE = new Map(); // key -> Promise<Buffer>
const EXPECTED_CACHE_MAX = 20;

function setExpectedCache(key, val) {
  if (EXPECTED_CACHE.size >= EXPECTED_CACHE_MAX) {
    EXPECTED_CACHE.delete(EXPECTED_CACHE.keys().next().value); // evict oldest
  }
  EXPECTED_CACHE.set(key, val);
}

// Evaluation cache (assignmentId + sha256(sourceText)):
// Guarantees 100% deterministic score reproduction when the same code is evaluated.
const EVALUATION_CACHE = new Map();
const EVALUATION_CACHE_MAX = 50;

async function renderExpectedScreenshot(context, expectedUrl) {
  // V-03: re-validate right before navigating (defense in depth vs DNS rebinding).
  await assertSafeUrl(expectedUrl);
  const page = await context.newPage();
  try {
    await page.goto(expectedUrl, { waitUntil: "networkidle", timeout: 30000 }); // V-24
    // V-39: a shortener/redirect may land on a different host that bypassed the
    // initial guard (e.g. tinyurl -> internal IP). Re-validate the final URL.
    const finalUrl = page.url();
    if (finalUrl !== expectedUrl) await assertSafeUrl(finalUrl);
    await page.evaluate(() => document.fonts && document.fonts.ready).catch(() => {});
    return await page.screenshot({ fullPage: false }); // V-26
  } finally {
    await page.close().catch(() => {});
  }
}


async function generateVisualAIFeedback({ studentName, rubric, score, domBreakdown, behaviorBreakdown, codeBreakdown, visualBreakdown, unifiedBreakdown, sourceText }) {
  const openai = getOpenAI();
  if (!openai) return null;

  try {
    const criteriaSummary = (unifiedBreakdown || []).map((b, i) => {
      const status = b.awarded === b.max ? "PASSED" : (b.awarded > 0 ? "PARTIAL" : "FAILED");
      return `Criterion ${i + 1}: "${b.item}" [${status}] (${b.awarded}/${b.max} marks) - automated finding: ${b.reason || ""}`;
    }).join("\n");

    const prompt = `You are an expert, objective code evaluator assessing a student's HTML/CSS/JavaScript project.
Candidate Submission: ${studentName || "Student"}
Overall Automated Score: ${score?.total ?? 0}/${score?.maxTotal ?? 100} (${score?.normalized ?? 0}%)

Rubric Criteria & Automated Test Results:
${criteriaSummary}

Candidate's Actual Source Code:
${(sourceText || "").slice(0, 15000)}

Your evaluation guidelines:
1. "reconciliation": Inspect the candidate's code against each criterion in the EXACT order listed above:
   - "reason": 1-2 concise, technical sentences citing the candidate's actual code (mentioning specific variables, element IDs/classes, or functions) explaining why this result was achieved.
2. "summary": 2-3 concise, honest, and educational sentences summarizing what the candidate achieved, citing what worked well and what specifically was missing or had gaps in their code. Include one concrete actionable tip to improve.
3. "strengths": Array of strings for criteria that passed or excelled. Each string MUST start with "[<Exact Criterion Name>] " and give a 1-2 sentence technical explanation citing what the candidate specifically implemented in their HTML, CSS, or JS (citing actual element tags, classes, functions, or APIs).
4. "issues": Array of strings for criteria that have bugs, gaps, or lost marks. Each string MUST start with "[<Exact Criterion Name>] " and explain specifically what was missing, incorrect, or incomplete in their code. If and only if the candidate has 100% flawless implementation, provide 1 subtle best-practice suggestion or leave empty.

Return STRICT JSON only matching this format:
{
  "summary": "...",
  "strengths": ["...", "..."],
  "issues": ["..."],
  "reconciliation": [
    {
      "criterionIndex": 1,
      "item": "<Exact Criterion Name>",
      "reason": "..."
    }
  ]
}`;

    const model = process.env.OPENAI_MODEL || "deepseek-v4-flash";
    const res = await openai.chat.completions.create({
      model,
      temperature: 0,
      response_format: { type: "json_object" },
      messages: [{ role: "user", content: prompt }]
    });

    const parsed = JSON.parse(res.choices?.[0]?.message?.content ?? "{}");
    if (parsed.summary && Array.isArray(parsed.strengths)) {
      return parsed;
    }
  } catch (err) {
    logger.warn(`Visual AI feedback generation failed (${err.message}) — using deterministic fallback.`);
  }
  return null;
}

export function clearEvaluationCache() {
  EVALUATION_CACHE.clear();
  EXPECTED_CACHE.clear();
  logger.info("Cleared EVALUATION_CACHE and EXPECTED_CACHE");
}

export async function evaluateStudentsWithVision({
  jobId,
  assignmentId,
  studentId,
  studentName,
  repoPath,
  rubricText,
  expectedUrl,
  entryFile = null,
  skipCache = false
}) {
  if (!repoPath || !rubricText) {
    throw new Error("Missing required inputs");
  }

  const rubric = await parseRubricWithSelectors(rubricText);
  const student = await scanStudentFolders(repoPath, entryFile);

  const results = [];
  const name = studentName;

  // Missing required files: bail out BEFORE spinning up a server/browser (V-06).
  if (student.flags.length > 0) {
    const summary = `Missing required files for evaluation: ${student.flags.join(", ")}`;
    results.push({
      name,
      score: 0,
      feedback: {
        summary,
        strengths: [],
        issues: [summary],
        breakdown: []
      },
      manualCorrection: true
    });
    return results;
  }

  // Source-code checks ("code" rubric items) read the actual files on disk —
  // no browser needed, so run them independently of the render pipeline below.
  const sourceText = await readSourceText(student);
  const sourceHash = crypto.createHash("sha256").update(sourceText || "").digest("hex");
  const rubricHash = crypto.createHash("sha256").update(rubricText || "").digest("hex").slice(0, 10);
  const evalCacheKey = `${assignmentId || ""}::${rubricHash}::${sourceHash}`;

  if (!skipCache && process.env.DISABLE_EVALUATION_CACHE !== "true" && EVALUATION_CACHE.has(evalCacheKey)) {
    try {
      const raw = await EVALUATION_CACHE.get(evalCacheKey);
      if (raw && raw.score !== undefined && !raw.error) {
        logger.info(`Evaluation cache hit for ${name} (assignment: ${assignmentId}, hash: ${sourceHash.slice(0, 8)})`);
        const cachedResult = JSON.parse(JSON.stringify(raw));
        const originalName = cachedResult.name;
        cachedResult.name = name;
        cachedResult.studentId = studentId;
        if (cachedResult.feedback?.summary) {
          if (originalName && originalName !== name) {
            cachedResult.feedback.summary = cachedResult.feedback.summary.replaceAll(originalName, name);
          }
          cachedResult.feedback.summary = cachedResult.feedback.summary.replace(/\b(Candidate|Student)\b/g, name);
        }
        return [cachedResult];
      }
    } catch {
      EVALUATION_CACHE.delete(evalCacheKey);
    }
  }

  let resolveJob;
  const inFlightPromise = new Promise((resolve) => {
    resolveJob = resolve;
  });
  if (EVALUATION_CACHE.size >= EVALUATION_CACHE_MAX) {
    EVALUATION_CACHE.delete(EVALUATION_CACHE.keys().next().value);
  }
  EVALUATION_CACHE.set(evalCacheKey, inFlightPromise);

  let workDir = null;
  let browserPool = null;
  let browser = null;
  let context = null;
  let server = null;

  try {
    const { score: codeScore, breakdown: codeBreakdown } = await computeCodeScore(rubric, sourceText);

    // Per-job artifact dir (V-16/V-25) — unique, outside the source tree, always cleaned up.
    workDir = await fs.mkdtemp(
      path.join(os.tmpdir(), `visual-${jobId || studentId || "job"}-`)
    );

    browserPool = await getBrowserPool();
    const started = await startStaticServer(student.basePath);
    server = started.server;
    const localUrl = started.url;

    browser = await browserPool.borrow();
    context = await browser.newContext({ viewport: VIEWPORT }); // V-23

    // ---- Reference (expected) screenshot, cached per assignment (V-29) ----
    // Skip if expectedUrl is absent, a localhost/private address, or any other
    // URL that fails the safety guard — fall through to code-only AI scoring.
    const isUnsafeOrMissing = (
      !expectedUrl ||
      /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i.test(expectedUrl) ||
      expectedUrl === 'https://example.com'
    );
    const cacheKey = `${assignmentId || ""}::${expectedUrl}`;
    let expectedImg = null;
    if (!isUnsafeOrMissing) {
      let expectedPromise = EXPECTED_CACHE.get(cacheKey);
      if (!expectedPromise) {
        expectedPromise = renderExpectedScreenshot(context, expectedUrl);
        setExpectedCache(cacheKey, expectedPromise);
      }
      try {
        expectedImg = await expectedPromise;
      } catch (err) {
        EXPECTED_CACHE.delete(cacheKey); // don't cache a failure
        // If the URL guard blocks it (private IP, DNS SSRF, etc.) or it simply
        // can't load, log a warning and continue with code-only AI evaluation.
        logger.warn(`renderExpectedScreenshot failed for expectedUrl (${expectedUrl}): ${err.message} — skipping reference screenshot, falling back to code-only scoring.`);
        expectedImg = null;
      }
    } else {
      logger.info(`expectedUrl is absent or unsafe (${expectedUrl}) — skipping reference screenshot, using code-only AI scoring.`);
    }

    // student.html comes from globby, which always normalizes to forward
    // slashes; student.basePath comes from path.join, which uses backslashes
    // on Windows. Normalize basePath first or the .replace below silently
    // no-ops and relativeHtml stays a full absolute path.
    const normalizedBasePath = student.basePath.replace(/\\/g, "/");
    const relativeHtml = student.html
      .replace(/\\/g, "/")
      .replace(normalizedBasePath, "");
    const url = `${localUrl}${relativeHtml}`;

    const page = await context.newPage();
    try {
      const responsePage = await page.goto(url, { waitUntil: "networkidle", timeout: 30000 }); // V-24
      await page.evaluate(() => document.fonts && document.fonts.ready).catch(() => {}); // V-24
      logger.debug(`Opening student url: ${url} status: ${responsePage?.status()}`);

      const screenshotPath = path.join(workDir, `${studentId || name}.png`);
      await page.screenshot({ path: screenshotPath, fullPage: false }); // V-26

      const domResults = await runDynamicDomChecks(page, rubric);
      const behaviorResults = await runBehaviorChecks(page, rubric);

      // Deterministic, single-counted scores (V-07/V-21).
      const domScore = computeDomScore(rubric, domResults);
      const behaviorScore = computeBehaviorScore(rubric, behaviorResults);

      // V-40: if the page rendered blank or errored, don't waste a vision call on
      // an empty screenshot (and don't silently score it) — flag for manual review.
      const httpStatus = responsePage?.status() ?? 0;
      const bodyText = (await page
        .evaluate(() => (document.body ? document.body.innerText : ""))
        .catch(() => "")) || "";
      const blank = bodyText.trim().length < 3;
      const badStatus = httpStatus >= 400;
      if (blank || badStatus) {
        const score = assembleScore({ rubric, domScore, behaviorScore, visualScore: 0, codeScore });
        const summary = badStatus
          ? `The page returned HTTP ${httpStatus} — it may not be a built/hosted site. Needs manual review.`
          : `The page rendered blank (no visible content). If this is an unbuilt React/Vue app, evaluate the built/hosted site instead. Needs manual review.`;
        
        const domBreakdown = buildDomBreakdown(rubric, domResults);
        const behaviorBreakdown = buildBehaviorBreakdown(rubric, behaviorResults);
        const unifiedBreakdown = [
          ...domBreakdown.map(b => ({ item: b.item, awarded: 0, max: b.max, reason: "Page failed to render or returned an error status." })),
          ...behaviorBreakdown.map(b => ({ item: b.item, awarded: 0, max: b.max, reason: "Page failed to render or returned an error status." })),
          ...(codeBreakdown || []).map(b => ({ item: b.item, awarded: b.awarded, max: b.max, reason: b.awarded === b.max ? "Code checks passed." : "Code checks failed." }))
        ];

        results.push({
          name,
          studentId,
          score: score.total,
          ...score,
          manualReviewItems: manualReviewItems(rubric),
          manualReviewDetail: manualReviewDetail(rubric),
          domBreakdown,
          behaviorBreakdown,
          codeBreakdown,
          feedback: {
            summary,
            strengths: [],
            issues: [summary],
            breakdown: unifiedBreakdown
          },
          manualCorrection: true,
          blankPage: true
        });
        return results; // finally blocks still run cleanup
      }

      const maxVisual = rubric
        .filter(r => r.type === "visual")
        .reduce((s, r) => s + (Number(r.weight) || 0), 0);
      let visualScore = 0;
      let visionFeedback = "";
      let visualBreakdown = [];

      if (maxVisual > 0) {
        try {
          const prompt = buildVisionPrompt(rubric, domResults, behaviorResults);
          const model = process.env.OPENAI_MODEL || "deepseek-v4-flash";
          const isVisionModel = !model.includes("deepseek");

          let messages;
          if (isVisionModel) {
            const studentImage = await fs.readFile(screenshotPath);
            const imageContent = [
              { type: "text", text: prompt },
              {
                type: "image_url",
                image_url: {
                  url: `data:image/png;base64,${studentImage.toString("base64")}`
                }
              }
            ];
            // Only attach reference image if we successfully loaded it
            if (expectedImg) {
              imageContent.push({
                type: "image_url",
                image_url: {
                  url: `data:image/png;base64,${expectedImg.toString("base64")}`
                }
              });
            }
            messages = [{ role: "user", content: imageContent }];
          } else {
            messages = [
              {
                role: "user",
                content: `${prompt}\n\nSTUDENT SOURCE CODE (HTML & CSS):\n${sourceText.slice(0, 4000)}`
              }
            ];
          }


          const aiRes = await getOpenAI().chat.completions.create({
            model,
            temperature: 0,
            response_format: { type: "json_object" },
            messages
          });

          const raw = aiRes.choices?.[0]?.message?.content ?? "{}";
          visionFeedback = raw;
          const parsed = JSON.parse(raw);
          const rawVisual = Number(parsed.visualScore) || 0;
          visualScore = clampScore(rawVisual, maxVisual);
          visualBreakdown = Array.isArray(parsed.breakdown) ? parsed.breakdown : [];
        } catch (err) {
          logger.warn(`Visual scoring AI call failed (${err.message}) — setting visualScore to 0`);
          visualScore = 0;
        }
      }

      const score = assembleScore({ rubric, domScore, behaviorScore, visualScore, codeScore });
      const needsManual = manualReviewItems(rubric);
 
      const domBreakdown = buildDomBreakdown(rubric, domResults);
      const behaviorBreakdown = buildBehaviorBreakdown(rubric, behaviorResults);
      
      const strengths = [];
      const issues = [];

      domBreakdown.forEach(b => {
        if (b.awarded === b.max) {
          strengths.push(`[${b.item}] Perfect DOM layout (earned ${b.max}/${b.max} marks)`);
        } else if (b.awarded > 0) {
          issues.push(`[${b.item}] Partially correct DOM structure. Some required elements are missing (earned ${b.awarded}/${b.max} marks)`);
        } else {
          issues.push(`[${b.item}] Missing/Incorrect DOM layout. Expected elements not found (earned 0/${b.max} marks)`);
        }
      });

      behaviorBreakdown.forEach(b => {
        if (b.awarded === b.max) {
          strengths.push(`[${b.item}] Perfect dynamic interactivity (earned ${b.max}/${b.max} marks)`);
        } else {
          issues.push(`[${b.item}] Dynamic interaction failed: functionality check did not pass (earned 0/${b.max} marks)`);
        }
      });

      (codeBreakdown || []).forEach(b => {
        if (b.awarded === b.max) {
          strengths.push(`[${b.item}] Proper code quality & API checks passed (earned ${b.max}/${b.max} marks)`);
        } else {
          issues.push(`[${b.item}] Code requirement failed: missing expected methods or patterns (earned 0/${b.max} marks)`);
        }
      });

      visualBreakdown.forEach(b => {
        if (b.max === 0) return; // Skip 0-weight/placeholder visual items
        if (b.awarded === b.max) {
          strengths.push(`[Visual: ${b.item}] Design looks correct and matches reference layout (earned ${b.max}/${b.max} marks)`);
        } else {
          issues.push(`[Visual: ${b.item}] Design layout mismatch: ${b.reason || "differs from expected reference"} (earned ${b.awarded}/${b.max} marks)`);
        }
      });

      const unifiedBreakdown = [
        ...domBreakdown.map(b => ({
          item: b.item,
          awarded: b.awarded,
          max: b.max,
          reason: b.awarded === b.max ? "Perfect implementation. All element checks passed." : "Incorrect element tags, classes, or missing DOM items."
        })),
        ...behaviorBreakdown.map(b => ({
          item: b.item,
          awarded: b.awarded,
          max: b.max,
          reason: b.awarded === b.max ? "Perfect functionality. All interaction tests passed." : "Incorrect implementation. Interaction check failed."
        })),
        ...(codeBreakdown || []).map(b => ({
          item: b.item,
          awarded: b.awarded,
          max: b.max,
          reason: b.awarded === b.max ? "Correct source code constructs and API usage." : "Missing required JavaScript functions, setInterval, or Date api."
        })),
        ...visualBreakdown.filter(b => b.max > 0).map(b => ({
          item: b.item,
          awarded: b.awarded,
          max: b.max,
          reason: b.reason || "Visual layout style differences."
        }))
      ];

      let summaryText = "";
      if (score.total === score.maxTotal) {
        summaryText = "Excellent work! Your submission meets all requirements. The DOM layout, behavior functions, and code quality checks are 100% correct.";
      } else if (score.total === 0) {
        summaryText = "None of the rubric criteria passed. Your page is either completely blank, has wrong DOM element tags/IDs, or did not implement the required functionality.";
      } else {
        summaryText = `Your submission passed partially with a score of ${score.total}/${score.maxTotal}. Please review the strengths and issues below for concrete areas of improvement.`;
      }

      const visionFeedbackText = typeof visionFeedback === 'object' ? (visionFeedback.feedback || "") : (typeof visionFeedback === 'string' ? visionFeedback : "");
      if (visionFeedbackText.trim()) {
        summaryText += " Visual Evaluation Feedback: " + visionFeedbackText;
      }

      const aiFeedback = await generateVisualAIFeedback({
        studentName: name,
        rubric,
        score,
        domBreakdown,
        behaviorBreakdown,
        codeBreakdown,
        visualBreakdown,
        unifiedBreakdown,
        sourceText
      });

      let finalSummary = (aiFeedback?.summary || summaryText).replace(/\b(Candidate|Student)\b/g, name);
      const finalStrengths = (aiFeedback?.strengths && aiFeedback.strengths.length > 0) ? aiFeedback.strengths : strengths;
      const finalIssues = (aiFeedback?.issues && aiFeedback.issues.length > 0) ? aiFeedback.issues : issues;

      // AI reconciliation enriches feedback reasons; numeric marks are kept 100% deterministic from automated tests.
      const recList = aiFeedback?.reconciliation || aiFeedback?.breakdown || [];
      const finalBreakdown = unifiedBreakdown.map((autoItem, idx) => {
        const rec = recList.find(r =>
          (r.criterionIndex !== undefined && Number(r.criterionIndex) === idx + 1) ||
          r.item === autoItem.item ||
          autoItem.item.toLowerCase().includes(String(r.item || "").toLowerCase()) ||
          String(r.item || "").toLowerCase().includes(autoItem.item.toLowerCase())
        );

        if (rec) {
          return {
            ...autoItem,
            reason: rec.reason || autoItem.reason
          };
        }

        return autoItem;
      });

      results.push({
        name,
        studentId,
        score: score.total, // backwards-compatible field
        ...score, // domScore, behaviorScore, visualScore, codeScore, total, maxTotal, normalized, pendingManualPoints
        manualReviewItems: needsManual,
        manualReviewDetail: manualReviewDetail(rubric),
        domBreakdown,
        behaviorBreakdown,
        codeBreakdown,
        visualBreakdown,
        feedback: {
          summary: finalSummary,
          strengths: finalStrengths,
          issues: finalIssues,
          breakdown: finalBreakdown
        },
        manualCorrection: needsManual.length > 0
      });
    } catch (err) {
      EVALUATION_CACHE.delete(evalCacheKey);
      if (typeof resolveJob === 'function') resolveJob(null);
      results.push({
        name,
        score: 0,
        error: err.message,
        manualCorrection: true
      });
    } finally {
      await page.close().catch(() => {});
    }
  } finally {
    // Unconditional resource release (V-05/V-06/V-25)
    if (context) await context.close().catch(() => {});
    if (browser && browserPool) browserPool.return(browser);
    if (server) server.close();
    if (workDir) await fs.rm(workDir, { recursive: true, force: true }).catch(() => {});

    // Settle in-flight cache promise unconditionally so concurrent/future jobs never hang
    if (results.length > 0 && results[0].score !== undefined && !results[0].error && !results[0].blankPage) {
      if (typeof resolveJob === 'function') resolveJob(results[0]);
    } else {
      EVALUATION_CACHE.delete(evalCacheKey);
      if (typeof resolveJob === 'function') resolveJob(null);
    }
  }

  return results;
}

/**
 * Universal adapter for workers/callers expecting evaluateVisualProject
 */
export async function evaluateVisualProject(payload, jobId, githubReport) {
  const rawPath = payload?.repoUrl || payload?.projectPath || "";
  const rubricText = typeof payload?.rubric === 'string'
    ? payload.rubric
    : (payload?.rubricText || JSON.stringify(payload?.rubric || {}));
  const expectedUrl = payload?.expectedUrl || process.env.DEFAULT_EXPECTED_URL || null;

  const isRemote = typeof rawPath === 'string' && (rawPath.startsWith('http://') || rawPath.startsWith('https://') || rawPath.startsWith('git@'));
  let localPath = rawPath;
  let didClone = false;

  if (isRemote) {
    localPath = await cloneRepo(rawPath);
    didClone = true;
  }

  try {
    return await evaluateStudentsWithVision({
      jobId,
      assignmentId: payload?.assignmentId || jobId,
      studentId: payload?.studentId || "student",
      studentName: payload?.studentName || "Student",
      repoPath: localPath,
      rubricText,
      expectedUrl,
      entryFile: payload?.entryFile || null,
      skipCache: !!(payload?.skipCache || payload?.reEvaluate || payload?.isReEvaluation)
    });
  } finally {
    if (didClone && localPath) {
      await deleteRepo(localPath);
    }
  }
}
