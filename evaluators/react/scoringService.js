import { generateAIFeedback } from "./utils/aiFeedback.js";
import { getCachedEvaluation, setCachedEvaluation } from "../../services/evaluationCache.js";
import logger from "../../config/logger.js";
import fs from "fs/promises";
import path from "path";
import OpenAI from "openai";

let client = null;

function getGroqClient() {
  if (client) return client;
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    logger.warn("OPENAI_API_KEY not set — AI scoring will fail.");
    return null;
  }
  client = new OpenAI({
    apiKey,
    baseURL: process.env.OPENAI_BASE_URL || 'https://api.deepseek.com',
  });
  return client;
}

const ASSET_EXTS = new Set([
  ".jpg", ".jpeg", ".png", ".svg", ".webp", ".gif", ".ico"
]);

const CODE_EXTS = new Set([
  ".js", ".jsx", ".ts", ".tsx", ".css", ".html", ".json"
]);

const ALLOWED_EXTENSIONS = new Set([...CODE_EXTS, ...ASSET_EXTS]);

/**
 * Retries DeepSeek / OpenAI calls with backoff to handle concurrent rate limits (429)
 */
async function callDeepSeekWithRetry(openaiClient, params, maxRetries = 3) {
  let attempt = 0;
  while (attempt < maxRetries) {
    try {
      return await openaiClient.chat.completions.create(params);
    } catch (err) {
      attempt++;
      if (attempt < maxRetries) {
        const delay = (attempt * 1500) + Math.floor(Math.random() * 500);
        logger.warn(`AI API rate-limit/error (${err.message}). Retrying in ${delay}ms (attempt ${attempt}/${maxRetries})...`);
        await new Promise(r => setTimeout(r, delay));
      } else {
        throw err;
      }
    }
  }
}

/**
 * Recursively reads source and asset files from a directory,
 * ignoring node_modules, dist, build, and .git.
 */
async function readProjectFiles(dir, fileList = []) {
  const IGNORED_DIRS = new Set(["node_modules", "dist", "build", ".git", ".next"]);

  let entries;
  try {
    entries = await fs.readdir(dir);
  } catch {
    return fileList;
  }

  for (const entry of entries) {
    const filePath = path.join(dir, entry);
    const stat = await fs.stat(filePath).catch(() => null);
    if (!stat) continue;

    if (stat.isDirectory()) {
      if (!IGNORED_DIRS.has(entry)) {
        await readProjectFiles(filePath, fileList);
      }
    } else if (ALLOWED_EXTENSIONS.has(path.extname(entry).toLowerCase())) {
      fileList.push(filePath);
    }
  }
  return fileList;
}

/**
 * Compresses source code to drastically reduce AI token usage.
 */
function compressCode(code) {
  // Remove block comments (/* ... */) and single-line comments (// ...)
  let compressed = code.replace(/\/\*[\s\S]*?\*\/|\/\/.*/g, "");
  // Remove empty lines and excessive indentation
  compressed = compressed.replace(/^\s*[\r\n]/gm, "");
  compressed = compressed.replace(/[ \t]{2,}/g, " ");
  return compressed;
}

/**
 * Assigns priority tier to source files.
 * Tier 1 (highest): Entrypoints (App.jsx, App.tsx, main.jsx, index.jsx, etc.)
 * Tier 2: Components (src/components/**, *.jsx, *.tsx)
 * Tier 3: Hooks & State (src/hooks/**, src/context/**, src/store/**, src/redux/**)
 * Tier 4: Utils & Services (src/utils/**, src/services/**, src/api/**, src/helpers/**)
 * Tier 5 (lowest): Styles & configs (.css, .js)
 */
function getFilePriority(filePath) {
  const norm = filePath.replace(/\\/g, "/").toLowerCase();
  const base = path.basename(norm);

  // Tier 1: Main entrypoints
  if (/^(app|main|index)\.(jsx?|tsx?)$/i.test(base)) return 1;
  if (norm.includes("/src/app.") || norm.includes("/src/main.") || norm.includes("/src/index.")) return 1;

  // Tier 2: Components
  if (norm.includes("/components/") || norm.includes("/views/") || norm.includes("/pages/") || norm.includes("/containers/")) return 2;
  if (/\.(jsx|tsx)$/i.test(base)) return 2;

  // Tier 3: Hooks, Context, State
  if (norm.includes("/hooks/") || norm.includes("/context/") || norm.includes("/store/") || norm.includes("/redux/") || norm.includes("/state/")) return 3;

  // Tier 4: Utils, Services, API, helpers
  if (norm.includes("/utils/") || norm.includes("/services/") || norm.includes("/api/") || norm.includes("/helpers/")) return 4;

  // Tier 5: Everything else (.css, config, etc.)
  return 5;
}

/**
 * Reads project files with priority ranking and generates a Source Manifest
 * to prevent blind truncation while remaining within LLM token budget.
 *
 * @param {string} projectPath - Path to the cloned student repo
 * @returns {Promise<{ codeString: string, manifest: { totalFiles: number, includedFiles: string[], omittedFiles: string[] }, unsupported: boolean }>}
 */
async function getProjectCodeData(projectPath) {
  const srcDir = path.join(projectPath, "src");
  let allFiles = [];
  try {
    allFiles = await readProjectFiles(srcDir);
  } catch {
    // No src directory
  }

  if (allFiles.length === 0) {
    allFiles = await readProjectFiles(projectPath);
  }

  if (allFiles.length === 0) {
    return {
      codeString: "",
      manifest: { totalFiles: 0, includedFiles: [], omittedFiles: [] },
      unsupported: true,
    };
  }

  // Sort files by priority tier first, then alphabetical path
  allFiles.sort((a, b) => {
    const pA = getFilePriority(a);
    const pB = getFilePriority(b);
    if (pA !== pB) return pA - pB;
    return a.localeCompare(b);
  });

  const MAX_TOTAL_CHARS = 45000; // ~11,000 tokens for code
  const MAX_PER_FILE_CHARS = 6000;

  const includedFiles = [];
  const omittedFiles = [];
  let codeBody = "";
  let assetCount = 0;
  const MAX_ASSETS_LISTED = 10;

  for (const f of allFiles) {
    const relativePath = path.relative(projectPath, f).replace(/\\/g, "/");
    const ext = path.extname(f).toLowerCase();

    if (ASSET_EXTS.has(ext)) {
      if (assetCount < MAX_ASSETS_LISTED) {
        includedFiles.push(relativePath);
        codeBody += `\n--- ${relativePath} ---\n[Binary Asset / Image File Present in Repository]\n`;
        assetCount++;
      } else {
        omittedFiles.push(relativePath);
      }
      continue;
    }

    try {
      const content = await fs.readFile(f, "utf8");
      const compressed = compressCode(content);

      if (codeBody.length + Math.min(compressed.length, MAX_PER_FILE_CHARS) <= MAX_TOTAL_CHARS) {
        includedFiles.push(relativePath);
        const fileContent = compressed.slice(0, MAX_PER_FILE_CHARS);
        codeBody += `\n--- ${relativePath} ---\n${fileContent}\n`;
      } else {
        omittedFiles.push(relativePath);
      }
    } catch {
      omittedFiles.push(relativePath);
    }
  }

  let manifestHeader = `[SOURCE MANIFEST: Discovered ${allFiles.length} files. Included: ${includedFiles.length}. Omitted: ${omittedFiles.length}]\n`;
  manifestHeader += `Included files: ${includedFiles.join(", ")}\n`;
  if (omittedFiles.length > 0) {
    manifestHeader += `Omitted files (token budget cap): ${omittedFiles.join(", ")}\n`;
  }
  manifestHeader += `========================================\n`;

  return {
    codeString: manifestHeader + codeBody,
    manifest: {
      totalFiles: allFiles.length,
      includedFiles,
      omittedFiles,
    },
    unsupported: false,
  };
}

/**
 * Helper to compute evaluation score from Vitest JSON report.
 */
function scoreFromTestResults(rubric, testResults) {
  if (!testResults || testResults.error || !Array.isArray(testResults.testResults)) {
    return null;
  }
  const assertions = testResults.testResults?.[0]?.assertionResults || [];
  if (assertions.length === 0) {
    return null;
  }

  const breakdown = {};
  const reasons = {};
  const multipliers = {};
  let totalScore = 0;

  // Group tests by rubric name prefix
  const categoryStats = {};
  for (const c of rubric.criteria) {
    categoryStats[c.name] = { passed: 0, total: 0, failedDetails: [] };
  }

  for (const assertion of assertions) {
    const title = assertion.title || "";
    const status = assertion.status; // "passed" or "failed"
    
    // Find matching rubric
    let matchedCriterion = null;
    for (const c of rubric.criteria) {
      if (title.toLowerCase().startsWith(c.name.toLowerCase())) {
        matchedCriterion = c.name;
        break;
      }
    }

    if (matchedCriterion) {
      categoryStats[matchedCriterion].total++;
      if (status === "passed") {
        categoryStats[matchedCriterion].passed++;
      } else {
        const cleanedError = (assertion.failureMessages?.[0] || "Test assertion failed")
          .split('\n')[0] // Only get the first line of the error to keep it simple and clean
          .replace(/\x1B\[\d+m/g, ""); // Strip ANSI colors
        categoryStats[matchedCriterion].failedDetails.push(cleanedError);
      }
    }
  }

  const totalCriteria = rubric.criteria.length;
  const matchedCriteriaCount = Object.values(categoryStats).filter(s => s.total > 0).length;

  // If no test assertions (or fewer than all criteria) matched the rubric criteria,
  // automated unit tests are not configured for this specific assignment.
  // Fall back to AI code evaluation so the student's actual code is graded against the rubric!
  if (matchedCriteriaCount === 0 || matchedCriteriaCount < totalCriteria) {
    logger.info(`Test suite only matched ${matchedCriteriaCount}/${totalCriteria} criteria. Falling back to AI code grading.`);
    return null;
  }

  // Calculate scores
  for (const c of rubric.criteria) {
    const stats = categoryStats[c.name];
    if (stats && stats.total > 0) {
      const multiplier = stats.passed / stats.total;
      const score = Math.round(multiplier * c.weight);
      breakdown[c.name] = score;
      multipliers[c.name] = Math.round(multiplier * 10) / 10;
      
      if (stats.failedDetails.length === 0) {
        reasons[c.name] = `Passed all ${stats.total} unit tests.`;
      } else {
        reasons[c.name] = `Failed ${stats.failedDetails.length}/${stats.total} tests. Errors: ${stats.failedDetails.join('; ')}`;
      }
    } else {
      breakdown[c.name] = 0;
      multipliers[c.name] = 0.0;
      reasons[c.name] = "No tests found or executed for this criterion.";
    }
    totalScore += breakdown[c.name];
  }

  return {
    score: totalScore,
    rubric_breakdown: breakdown,
    reasons,
    multipliers
  };
}

/**
 * Scores a React submission using AI-based code analysis.
 * Reads the student's actual source code and evaluates each rubric criterion
 * using Groq AI.
 *
 * @param {Object} rubric      - { criteria: [{ name, weight, description }] }
 * @param {string} projectPath - Path to the cloned student repo
 * @param {string} githubReport - Build/Linter report from GitHub Actions
 * @param {Object} testResults - Test suite results JSON
 * @returns {Promise<Object>}  - Standard evaluation output
 */
export default async function scoreSubmission(rubric, projectPath, githubReport, testResults, options = {}) {
  const warnings = [];
  const skipCache = Boolean(options?.skipCache || options?.reEvaluate || options?.isReEvaluation);

  // Step 1: Read the student's code with priority ranking & manifest
  const { codeString, manifest, unsupported } = await getProjectCodeData(projectPath);

  if (unsupported || !codeString) {
    logger.warn("No source files found in student repo.");
    const breakdown = {};
    for (const c of rubric.criteria) {
      breakdown[c.name] = 0;
    }
    const feedback = await generateAIFeedback({
      rubric_breakdown: breakdown,
      score: 0,
      warnings: ["No source files or React entrypoint found in the repository."],
      execution_logs: githubReport || "",
    });
    return {
      score: 0,
      rubric_breakdown: breakdown,
      feedback,
      warnings: ["No source files or React entrypoint found in the repository."],
      execution_logs: githubReport || "",
      status: "unsupported_project",
      pass: false,
      is_passed: false,
    };
  }

  if (manifest.omittedFiles.length > 0) {
    warnings.push(
      `Evaluated top ${manifest.includedFiles.length} priority source files. Omitted ${manifest.omittedFiles.length} secondary files (${manifest.omittedFiles.join(", ")}) due to token limits.`
    );
  }

  // Check evaluation cache: If this exact codebase + rubric was already graded, return identical result
  if (!skipCache) {
    const cached = await getCachedEvaluation("react", rubric, codeString, githubReport, testResults);
    if (cached) {
      logger.info(`⚡ Returning cached React evaluation result: score=${cached.score}/100`);
      return cached;
    }
  }

  // Check if we can grade via Vitest test results mathematically (Option A)
  const testGrading = scoreFromTestResults(rubric, testResults);
  let breakdown = {};
  let reasons = {};
  let multipliers = {};
  let totalScore = 0;
  let gradedByTests = false;

  if (testGrading) {
    logger.info("Deterministic testResults found. Grading React project mathematically based on Vitest outcomes.");
    breakdown = testGrading.rubric_breakdown;
    reasons = testGrading.reasons;
    multipliers = testGrading.multipliers;
    totalScore = testGrading.score;
    gradedByTests = true;
  }

  if (!gradedByTests) {
    // Step 2: Build the rubric criteria description for the AI prompt
    const criteriaList = rubric.criteria
      .map(
        (c, i) =>
          `${i + 1}. "${c.name}" (weight: ${c.weight} points): ${c.description || "No description provided."}`
      )
      .join("\n");

    // Step 3: Ask AI to score each criterion
    const groq = getGroqClient();

    if (!groq) {
      logger.error("Groq client unavailable — OPENAI_API_KEY is not set.");
      throw new Error("AI grading client unavailable. Please check your API keys.");
    } else {
      const assignmentTitle = rubric.title || rubric.assignmentTitle || "React Application";
      const assignmentContext = rubric.description || rubric.assignmentDescription || "";

      const prompt = `You are an expert React instructor grading a student's assignment.

## Assignment: ${assignmentTitle}
${assignmentContext ? `## Assignment Requirements & Context:\n${assignmentContext}\n` : ""}
## GitHub Actions Build/Linter Report:
${githubReport || "No build report available."}

## Student's Compressed Source Code:
${codeString}

## Rubric Criteria to Evaluate:
${criteriaList}

## Grading Instructions:
You must be an objective, thorough instructor. Read the source code carefully to determine if the functionality requested in the rubric criteria actually exists.
Check for proper component architecture, props passing, useState state management, event handler functions, and JSX rendering.

CRITICAL RULES:
1. ASSIGNMENT RELEVANCE CHECK:
   - Verify that the submission actually attempts the assignment defined in the rubric criteria.
   - If the student submitted a completely unrelated project (e.g. an empty template, an authentication page when an image feed was requested, or a calculator), they MUST receive 0 marks for all criteria (Project Setup can get max 2 marks for basic React setup, all other criteria must be strictly 0).
   - Do NOT give credit for code that does not implement the required features.

2. BUILD FAILURE RULE:
   - If the Build Report says "Build: Failed" or "failed to compile", students can STILL get partial credit for criteria that are fully correct in the source code (Setup, Component Structure, State, Handler Logic).
   - However, criteria requiring runtime visual/rendering verification must be capped at max 5 marks if the build failed.

Assign a discrete score multiplier for EACH criterion based strictly on verified code evidence:
- 1.0 = Fully meets all exact requirements for this criterion.
- 0.9 = Mostly correct with only minor gaps or formatting issues.
- 0.8 = Good implementation but missing one specific sub-feature.
- 0.5 = Partial implementation of the exact feature (~50% of required functionality present).
- 0.2 = Bare minimum skeleton of the exact feature.
- 0.0 = Not attempted, completely missing, or irrelevant project code.

CRITICAL SCORING RULE:
Use ONLY the discrete multipliers: 1.0, 0.9, 0.8, 0.5, 0.2, or 0.0.
Do NOT use intermediate or arbitrary decimal values like 0.85, 0.92, 0.74, etc.

SKELETON vs PARTIAL CLARIFICATION:
If a criterion requires components, sub-components, or props wiring, but the code only contains an empty wrapper or placeholder text (such as "under construction" or empty JSX) with NO child component imports or prop passings, assign strictly 0.2 (skeleton) or 0.0 (unattempted), NEVER 0.5. Reserve 0.5 only when actual components and partial props/logic are genuinely written.

For EACH criterion write a detailed 2-3 sentence reasoning that:
1. States specifically what was FOUND in the source code files (cite actual code, component/function/state names, or props).
2. States specifically what is MISSING or WRONG compared to the rubric requirements.
3. Mentions if the build failed and how it affected that specific criterion.

Output STRICTLY a JSON object (no markdown, no extra text):
{
  "scores": [
    { "name": "<exact criterion name>", "multiplier": <number 0.0, 0.2, 0.5, 0.8, 0.9, or 1.0>, "reasoning": "<2-3 sentence code-specific explanation>" }
  ]
}`;

    try {
      logger.info("Sending code to Groq AI for rubric-based scoring...");

      const response = await callDeepSeekWithRetry(groq, {
        model: process.env.OPENAI_MODEL || "deepseek-chat",
        messages: [{ role: "user", content: prompt }],
        max_tokens: 3000,
        temperature: 0.0,
        seed: 42,
        response_format: { type: "json_object" },
      });

      let rawContent = response.choices[0]?.message?.content?.trim() || "";
      logger.info(`Groq AI response received (length: ${rawContent.length}): ${rawContent?.slice(0, 150)}...`);

      // Strip markdown code fences if returned
      if (rawContent.startsWith("```")) {
        rawContent = rawContent.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
      }
      // Strip any reasoning/think tags
      rawContent = rawContent.replace(/<think>[\s\S]*?<\/think>/gi, "").trim();

      let parsed = null;
      try {
        parsed = JSON.parse(rawContent);
      } catch (parseErr) {
        logger.warn(`JSON parse failed: ${parseErr.message}. Attempting regex extraction and repair...`);
        const jsonMatch = rawContent.match(/\{[\s\S]*\}/);
        if (jsonMatch) {
          try {
            parsed = JSON.parse(jsonMatch[0]);
          } catch (e2) {}
        }

        // If JSON was cut off mid-array, attempt closing it
        if (!parsed && rawContent.includes('"scores"')) {
          try {
            const lastObjEnd = rawContent.lastIndexOf("}");
            if (lastObjEnd !== -1) {
              const repaired = rawContent.slice(0, lastObjEnd + 1) + "]}";
              parsed = JSON.parse(repaired);
              logger.info("Successfully repaired truncated JSON.");
            }
          } catch (e3) {}
        }
      }

      // Normalize scores list from any structure the AI returned
      let rawList = [];
      if (Array.isArray(parsed)) {
        rawList = parsed;
      } else if (Array.isArray(parsed?.scores)) {
        rawList = parsed.scores;
      } else if (Array.isArray(parsed?.criteria)) {
        rawList = parsed.criteria;
      } else if (Array.isArray(parsed?.results)) {
        rawList = parsed.results;
      } else if (typeof parsed === "object" && parsed !== null) {
        rawList = Object.entries(parsed)
          .filter(([key]) => key !== "summary" && key !== "feedback")
          .map(([name, val]) => {
            if (typeof val === "object" && val !== null) {
              return { name, ...val };
            }
            return { name, multiplier: typeof val === "number" ? val : 0 };
          });
      }

      const cleanStr = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

      if (rawList.length > 0) {
        rawList.forEach((scoredCriteria, idx) => {
          if (!scoredCriteria) return;

          // 1. Try fuzzy name matching
          const scoredName = cleanStr(scoredCriteria.name || scoredCriteria.criterion || scoredCriteria.item);
          let matchingRubric = rubric.criteria.find((c) => {
            const rubricName = cleanStr(c.name);
            return (
              rubricName === scoredName ||
              rubricName.includes(scoredName) ||
              scoredName.includes(rubricName)
            );
          });

          // 2. Fallback to index if unmatched
          if (!matchingRubric && rubric.criteria[idx] && breakdown[rubric.criteria[idx].name] === undefined) {
            matchingRubric = rubric.criteria[idx];
          }

          if (matchingRubric && breakdown[matchingRubric.name] === undefined) {
            let rawMult = scoredCriteria.multiplier;
            if (rawMult === undefined) {
              const rawScore = scoredCriteria.score ?? scoredCriteria.awarded ?? scoredCriteria.points;
              if (typeof rawScore === "number") {
                rawMult = rawScore > 1 ? rawScore / matchingRubric.weight : rawScore;
              }
            }

            const multiplier =
              typeof rawMult === "number"
                ? Math.max(0, Math.min(1, rawMult))
                : 0.5;

            const score = Math.round(matchingRubric.weight * multiplier);
            breakdown[matchingRubric.name] = score;
            reasons[matchingRubric.name] =
              scoredCriteria.reasoning || scoredCriteria.reason || scoredCriteria.feedback || "Evaluated by code analysis.";
            multipliers[matchingRubric.name] = multiplier;
            totalScore += score;

            if (scoredCriteria.reasoning) {
              logger.info(
                `  "${matchingRubric.name}": ${score}/${matchingRubric.weight} — ${scoredCriteria.reasoning}`
              );
            }
          }
        });
      } else {
        logger.warn("AI output did not contain a recognizable scores array.");
      }

      // Check if any criteria were missed by the AI model
      const missingCriteria = rubric.criteria.filter((c) => breakdown[c.name] === undefined);
      if (missingCriteria.length > 0) {
        logger.warn(`AI omitted ${missingCriteria.length} criteria. Performing targeted evaluation for missing items...`);
        try {
          const missingList = missingCriteria
            .map((c, i) => `${i + 1}. "${c.name}" (weight: ${c.weight} points): ${c.description || "No description provided."}`)
            .join("\n");
          const retryPrompt = `You are evaluating a student's React submission. The following criteria need evaluation:

## Student Source Code:
${codeString}

## Rubric Criteria:
${missingList}

Grade each criterion strictly using discrete multipliers (1.0, 0.9, 0.8, 0.5, 0.2, or 0.0). Return STRICTLY a JSON object:
{
  "scores": [
    { "name": "<exact criterion name>", "multiplier": <number 0.0, 0.2, 0.5, 0.8, 0.9, or 1.0>, "reasoning": "<2-3 sentence explanation>" }
  ]
}`;
          const retryResponse = await callDeepSeekWithRetry(groq, {
            model: process.env.OPENAI_MODEL || "deepseek-chat",
            messages: [{ role: "user", content: retryPrompt }],
            max_tokens: 1500,
            temperature: 0.0,
            seed: 42,
            response_format: { type: "json_object" },
          });
          let retryRaw = retryResponse.choices[0]?.message?.content?.trim() || "";
          if (retryRaw.startsWith("```")) {
            retryRaw = retryRaw.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
          }
          retryRaw = retryRaw.replace(/<think>[\s\S]*?<\/think>/gi, "").trim();
          let retryParsed = null;
          try {
            retryParsed = JSON.parse(retryRaw);
          } catch {
            const m = retryRaw.match(/\{[\s\S]*\}/);
            if (m) try { retryParsed = JSON.parse(m[0]); } catch {}
          }

          const retryList = Array.isArray(retryParsed)
            ? retryParsed
            : retryParsed?.scores || retryParsed?.criteria || [];

          retryList.forEach((scoredCriteria, idx) => {
            if (!scoredCriteria) return;
            const scoredName = cleanStr(scoredCriteria.name || scoredCriteria.criterion);
            let matching = missingCriteria.find((c) => {
              const rName = cleanStr(c.name);
              return rName === scoredName || rName.includes(scoredName) || scoredName.includes(rName);
            }) || missingCriteria[idx];

            if (matching && breakdown[matching.name] === undefined) {
              const rawMult = scoredCriteria.multiplier ?? (typeof scoredCriteria.score === "number" ? scoredCriteria.score / matching.weight : 0.5);
              const multiplier = Math.max(0, Math.min(1, typeof rawMult === "number" ? rawMult : 0.5));
              const score = Math.round(matching.weight * multiplier);
              breakdown[matching.name] = score;
              reasons[matching.name] = scoredCriteria.reasoning || scoredCriteria.reason || "Evaluated by code analysis.";
              multipliers[matching.name] = multiplier;
              totalScore += score;
            }
          });
        } catch (retryErr) {
          logger.warn(`Retry for missing criteria failed: ${retryErr.message}`);
        }
      }

      // Fill in any remaining criteria that could not be evaluated
      for (const c of rubric.criteria) {
        if (breakdown[c.name] === undefined) {
          const defaultScore = 0;
          warnings.push(
            `AI did not return a score for "${c.name}" — defaulting to ${defaultScore}.`
          );
          breakdown[c.name] = defaultScore;
          reasons[c.name] = "Evaluation completed. Specific criteria breakdown unavailable.";
          multipliers[c.name] = 0.0;
        }
      }
    } catch (err) {
      logger.error(`AI scoring encountered error: ${err.message}`);
      for (const c of rubric.criteria) {
        if (breakdown[c.name] === undefined) {
          breakdown[c.name] = 0;
          reasons[c.name] = `Evaluation note: ${err.message}`;
          multipliers[c.name] = 0.0;
        }
      }
      warnings.push(`AI grading note: ${err.message}`);
    }
  }
}

  const maxScore = rubric.criteria.reduce((sum, c) => sum + c.weight, 0);
  const isBuildFailed =
    (githubReport || "").toLowerCase().includes("build: failed") ||
    (githubReport || "").toLowerCase().includes("failed to compile");
  const isPassed = totalScore >= maxScore * 0.5;

  let status;
  if (isBuildFailed && !isPassed) {
    status = "build_failed";
  } else if (isPassed) {
    status = "passed";
  } else {
    status = "failed";
  }

  // Step 4: Generate human-readable AI feedback (now with full per-criterion context)
  logger.info(`Generating feedback for total score: ${totalScore}/${maxScore} (status: ${status})`);
  const feedbackText = await generateAIFeedback({
    rubric_breakdown: breakdown,
    rubric_criteria: rubric.criteria,
    per_criterion_reasons: reasons,
    score: totalScore,
    warnings,
    execution_logs: githubReport || "",
    assignmentType: "React",
    codeSnippet: codeString || "",
  });

  // Step 5: Construct structured strengths, issues, and breakdown lists
  const strengths = [];
  const issues = [];
  const unifiedBreakdown = [];

  for (const c of rubric.criteria) {
    const awarded = breakdown[c.name] ?? 0;
    const mult = multipliers[c.name] ?? 0.0;
    const reason = reasons[c.name] || "Criterion evaluated by code analysis.";

    unifiedBreakdown.push({
      item: c.name,
      awarded,
      max: c.weight,
      reason
    });

    if (mult === 1.0) {
      strengths.push(`[${c.name}] ${reason} (earned ${awarded}/${c.weight} marks)`);
    } else {
      issues.push(`[${c.name}] ${reason} (earned ${awarded}/${c.weight} marks)`);
    }
  }

  const rubricFeedback = {
    summary: feedbackText || "Evaluation completed successfully.",
    strengths,
    issues,
    breakdown: unifiedBreakdown
  };

  const finalResult = {
    score: totalScore,
    rubric_breakdown: breakdown,
    feedback: rubricFeedback,
    rubricFeedback: rubricFeedback,
    warnings,
    execution_logs: githubReport || "",
    status,
    pass: isPassed,
    is_passed: isPassed,
  };

  // Cache result for identical submissions
  await setCachedEvaluation("react", rubric, codeString, githubReport, testResults, finalResult);

  return finalResult;
}