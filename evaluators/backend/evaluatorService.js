import OpenAI from 'openai';
import logger from '../../config/logger.js';
import path from 'path';
import fs from 'fs';
import { cloneRepo, deleteRepo } from '../react/repoService.js';
import { scoreFromTestResults } from './scoringService.js';
import { generateBackendAIFeedback } from './feedbackService.js';
import { getCachedEvaluation, setCachedEvaluation } from '../../services/evaluationCache.js';
import { runInDockerSandbox } from '../../services/dockerSandboxService.js';

let client = null;

function getClient() {
  if (client) return client;
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    logger.warn('OPENAI_API_KEY not set - Backend AI feedback will fail.');
    return null;
  }
  client = new OpenAI({
    apiKey,
    baseURL: process.env.OPENAI_BASE_URL || 'https://api.deepseek.com',
  });
  return client;
}

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
        logger.warn(`[BackendEval] AI API rate-limit/error (${err.message}). Retrying in ${delay}ms (attempt ${attempt}/${maxRetries})...`);
        await new Promise(r => setTimeout(r, delay));
      } else {
        throw err;
      }
    }
  }
}

/**
 * Recursively reads relevant backend source files from a directory,
 * ignoring node_modules, dist, build, .git, etc.
 */
async function readProjectFiles(dir, fileList = []) {
  const IGNORED_DIRS = new Set(["node_modules", "dist", "build", ".git", ".next", "public", "assets", "coverage"]);
  const ALLOWED_EXTENSIONS = new Set([
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".json", ".sql", ".env.example"
  ]);

  let entries;
  try {
    entries = await fs.promises.readdir(dir);
  } catch {
    return fileList;
  }

  for (const entry of entries) {
    const filePath = path.join(dir, entry);
    const stat = await fs.promises.stat(filePath).catch(() => null);
    if (!stat) continue;

    if (stat.isDirectory()) {
      if (!IGNORED_DIRS.has(entry)) {
        await readProjectFiles(filePath, fileList);
      }
    } else if (ALLOWED_EXTENSIONS.has(path.extname(entry).toLowerCase())) {
      // Exclude package-lock.json to avoid wasting tokens
      if (entry !== 'package-lock.json') {
        fileList.push(filePath);
      }
    }
  }
  return fileList;
}

/**
 * Compresses source code to drastically reduce AI token usage.
 */
function compressCode(code) {
  let compressed = code.replace(/\/\*[\s\S]*?\*\/|\/\/.*/g, "");
  compressed = compressed.replace(/^\s*[\r\n]/gm, "");
  compressed = compressed.replace(/[ \t]{2,}/g, " ");
  return compressed;
}

/**
 * Assigns priority tier to backend source files.
 * Tier 1 (highest): Main entrypoints (server.js, app.js, index.js, src/server.*, etc.)
 * Tier 2: Routes & API endpoints (routes/**, api/**, router/**)
 * Tier 3: Controllers (controllers/**)
 * Tier 4: Database models & schemas (models/**, schemas/**, prisma/**, db/**)
 * Tier 5: Middleware & Security (middleware/**, middlewares/**, auth/**)
 * Tier 6: Services, Utils, Config (services/**, utils/**, config/**, package.json, .sql)
 * Tier 7 (lowest): Everything else
 */
function getBackendFilePriority(filePath) {
  const norm = filePath.replace(/\\/g, "/").toLowerCase();
  const base = path.basename(norm);

  // Tier 1: Main entrypoints
  if (/^(server|app|index|main)\.(jsx?|tsx?|mjs|cjs)$/i.test(base)) return 1;
  if (norm.includes("/src/server.") || norm.includes("/src/app.") || norm.includes("/src/index.")) return 1;

  // Tier 2: Routes
  if (norm.includes("/routes/") || norm.includes("/route/") || norm.includes("/api/") || norm.includes("/routers/")) return 2;

  // Tier 3: Controllers
  if (norm.includes("/controllers/") || norm.includes("/controller/") || norm.includes("/handlers/")) return 3;

  // Tier 4: Models & Database
  if (norm.includes("/models/") || norm.includes("/model/") || norm.includes("/schemas/") || norm.includes("/schema/") || norm.includes("/prisma/") || norm.includes("/db/")) return 4;

  // Tier 5: Middleware & Security
  if (norm.includes("/middleware/") || norm.includes("/middlewares/") || norm.includes("/auth/")) return 5;

  // Tier 6: Services, Utils, Config
  if (norm.includes("/services/") || norm.includes("/service/") || norm.includes("/utils/") || norm.includes("/config/") || base === "package.json" || base.endsWith(".sql")) return 6;

  // Tier 7: Everything else
  return 7;
}

/**
 * Reads relevant backend source files with priority ranking and generates a Source Manifest
 * to prevent blind truncation while remaining within LLM token budget.
 *
 * @param {string} projectPath - Path to the cloned student repo
 * @returns {Promise<{ codeString: string, manifest: { totalFiles: number, includedFiles: string[], omittedFiles: string[] }, unsupported: boolean }>}
 */
async function getBackendProjectCodeData(projectPath) {
  const srcDir = path.join(projectPath, "src");
  let allFiles = [];
  try {
    allFiles = await readProjectFiles(srcDir);
  } catch {
    // No src directory
  }

  const rootFiles = await readProjectFiles(projectPath);
  const fileSet = new Set(allFiles);
  for (const rf of rootFiles) {
    fileSet.add(rf);
  }
  allFiles = Array.from(fileSet);

  if (allFiles.length === 0) {
    return {
      codeString: "",
      manifest: { totalFiles: 0, includedFiles: [], omittedFiles: [] },
      unsupported: true,
    };
  }

  // Sort files by priority tier first, then path name
  allFiles.sort((a, b) => {
    const pA = getBackendFilePriority(a);
    const pB = getBackendFilePriority(b);
    if (pA !== pB) return pA - pB;
    return a.localeCompare(b);
  });

  const MAX_TOTAL_CHARS = 45000; // ~11,000 tokens for code
  const MAX_PER_FILE_CHARS = 6000;

  const includedFiles = [];
  const omittedFiles = [];
  let codeBody = "";

  for (const f of allFiles) {
    const relativePath = path.relative(projectPath, f).replace(/\\/g, "/");
    try {
      const content = await fs.promises.readFile(f, "utf8");
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
 * Evaluates a Backend project.
 * Tries mathematical scoring via Jest test results first.
 * Falls back to AI-based code analysis if tests are absent/failed.
 */
export async function evaluateBackendProject(payload, jobId, testResults, logs, codeContext = null) {
  const rawCriteria = payload.rubric?.criteria || [];
  const criteria = rawCriteria.map(c => ({
    name: c.name,
    weight: typeof c.weight === 'number' ? c.weight : (typeof c.score === 'number' ? c.score : 20),
    description: c.description || ""
  }));
  const rubric = { ...payload.rubric, criteria };
  const maxScore = rubric.criteria.reduce((sum, c) => sum + c.weight, 0);

  // 1. Try deterministic mathematical scoring if Jest test results are present
  const testGrading = scoreFromTestResults(rubric, testResults);
  if (testGrading) {
    logger.info(`Deterministic testResults found. Grading Backend project mathematically for Job ${jobId}`);

    const isPassed = testGrading.score >= maxScore * 0.5;
    const status = isPassed ? "passed" : "failed";
    
    // Generate concise, clean feedback summary matching react/visual style
    const feedbackText = await generateBackendAIFeedback({
      rubric_breakdown: testGrading.rubric_breakdown,
      rubric_criteria: rubric.criteria,
      per_criterion_reasons: testGrading.reasons,
      score: testGrading.score,
      warnings: testResults?.warnings || [],
      execution_logs: logs || "",
      assignmentType: "Node.js & Express Backend"
    });

    const strengths = [];
    const issues = [];
    const unifiedBreakdown = [];

    for (const c of rubric.criteria) {
      const awarded = testGrading.rubric_breakdown[c.name] ?? 0;
      const mult = testGrading.multipliers[c.name] ?? 0.0;
      const reason = testGrading.reasons[c.name] || "Criterion evaluated by automated test execution.";

      unifiedBreakdown.push({
        item: c.name,
        awarded,
        max: c.weight,
        reason
      });

      if (mult >= 1.0) {
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
      score: testGrading.score,
      rubric_breakdown: testGrading.rubric_breakdown,
      feedback: rubricFeedback,
      rubricFeedback: rubricFeedback,
      warnings: testResults?.warnings || [],
      execution_logs: logs || "",
      status,
      pass: isPassed,
      is_passed: isPassed,
    };

    return finalResult;
  }

  // 2. Fallback: AI-based code analysis (identical resilient flow to React & Visual)
  logger.info(`Test results absent or empty for Job ${jobId}. Falling back to AI code grading.`);
  
  let repoPath;
  const warnings = [];
  try {
    let codeString = codeContext;
    let manifest = null;
    if (!codeString) {
      repoPath = await cloneRepo(payload.repoUrl || payload.submission_link);
      const codeData = await getBackendProjectCodeData(repoPath);
      codeString = codeData.codeString;
      manifest = codeData.manifest;
      if (manifest?.omittedFiles?.length > 0) {
        warnings.push(
          `Evaluated top ${manifest.includedFiles.length} priority backend files. Omitted ${manifest.omittedFiles.length} secondary files (${manifest.omittedFiles.join(", ")}) due to token limits.`
        );
      }
    }

    if (!codeString) {
      logger.warn(`No source files found in student repo for Job ${jobId}`);
      const breakdown = {};
      const unifiedBreakdown = [];
      const issues = ["No source files or backend entrypoint found in the repository."];
      for (const c of rubric.criteria) {
        breakdown[c.name] = 0;
        unifiedBreakdown.push({
          item: c.name,
          awarded: 0,
          max: c.weight,
          reason: "No source files found."
        });
      }
      const rubricFeedback = {
        summary: "No source files or backend entrypoint found in the repository.",
        strengths: [],
        issues,
        breakdown: unifiedBreakdown
      };
      return {
        score: 0,
        rubric_breakdown: breakdown,
        feedback: rubricFeedback,
        rubricFeedback: rubricFeedback,
        warnings: ["No backend source files or entrypoint found in the repository."],
        execution_logs: logs || "",
        status: "unsupported_project",
        pass: false,
        is_passed: false,
      };
    }

    // Check evaluation cache: If this exact codebase + rubric was already graded, return identical result
    const skipCache = Boolean(payload.skipCache || payload.reEvaluate || payload.isReEvaluation);
    if (!skipCache) {
      const cached = await getCachedEvaluation("backend", rubric, codeString, logs, testResults);
      if (cached) {
        logger.info(`⚡ Returning cached Backend evaluation result for Job ${jobId}: score=${cached.score}/100`);
        return cached;
      }
    }

    const openai = getClient();
    if (!openai) {
      logger.error(`OpenAI client unavailable for Job ${jobId} and deterministic test results were absent.`);
      throw new Error("AI evaluation service unavailable: OPENAI_API_KEY is missing or invalid on the server.");
    }

    const criteriaList = rubric.criteria
      .map((c, i) => `${i + 1}. "${c.name}" (weight: ${c.weight} points): ${c.description || "No description provided."}`)
      .join("\n");

    const prompt = `You are an expert Backend (Node.js, Express, MongoDB, SQL) instructor grading a student's backend assignment.

## GitHub Actions / Execution Report:
${logs || "No report available."}

## Student's Compressed Source Code:
${codeString}

## Rubric Criteria to Evaluate:
${criteriaList}

## Grading Instructions:
You must be an objective, thorough backend instructor.
Read the source code carefully to determine if the functionality requested in the rubric actually exists.
Check for proper routes, controller logic, middleware, database models/queries, and error handling.
Do NOT penalize if .env file is missing (expected for security reasons).

Assign a discrete score multiplier for EACH criterion based strictly on verified code evidence:
- 1.0 = Fully meets all exact requirements for this criterion (routes, controllers, models, and middleware complete and correct).
- 0.9 = Mostly correct with only very minor syntax, naming, or minor non-critical gaps (e.g., missing empty-string trim, minor styling omission, or minor linter warning).
- 0.8 = Good implementation but missing one specific sub-feature (e.g. missing 1 HTTP status code or missing 1 validation check).
- 0.5 = Partial implementation of the required endpoint or logic (~50% of required functionality present).
- 0.2 = Bare minimum skeleton or stub only.
- 0.0 = Not attempted, completely missing, or completely unrelated project.

CRITICAL SCORING RULE:
Use ONLY the discrete multipliers: 1.0, 0.9, 0.8, 0.5, 0.2, or 0.0.
Do NOT use intermediate or arbitrary decimal values like 0.84, 0.91, 0.73, etc.

For EACH criterion write a concise 1-2 sentence explanation:
1. States specifically what was FOUND in the source code (cite route paths, function names, middleware, or queries).
2. States specifically what is MISSING or WRONG compared to the rubric requirements.

CRITICAL REQUIREMENT:
You MUST evaluate and return a score entry for ALL ${rubric.criteria.length} criteria listed above. Do not stop early. Do not omit any criterion. Keep explanations to 1-2 sentences so all ${rubric.criteria.length} items fit easily.

Output STRICTLY a JSON object (no markdown, no extra text):
{
  "scores": [
    { "name": "<exact criterion name>", "multiplier": <number 0.0, 0.2, 0.5, 0.8, 0.9, or 1.0>, "reasoning": "<1-2 sentence concise explanation>" }
  ]
}`;

    logger.info(`Sending code to AI for backend rubric scoring (Job: ${jobId})...`);

    const response = await callDeepSeekWithRetry(openai, {
      model: process.env.OPENAI_MODEL || "deepseek-chat",
      messages: [{ role: "user", content: prompt }],
      max_tokens: 3000,
      temperature: 0.0,
      seed: 42,
      response_format: { type: "json_object" },
    });

    let rawContent = response.choices[0]?.message?.content?.trim() || "";
    logger.info(`AI response received (length: ${rawContent.length}) for Job ${jobId}`);

    // Strip markdown code fences
    if (rawContent.startsWith("```")) {
      rawContent = rawContent.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
    }
    // Strip DeepSeek reasoning/think tags
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
        } catch {}
      }

      if (!parsed && rawContent.includes('"scores"')) {
        try {
          const lastObjEnd = rawContent.lastIndexOf("}");
          if (lastObjEnd !== -1) {
            const repaired = rawContent.slice(0, lastObjEnd + 1) + "]}";
            parsed = JSON.parse(repaired);
            logger.info("Successfully repaired truncated JSON.");
          }
        } catch {}
      }
    }

    // Normalize scores list from any structure the AI returned
    let rawList = [];
    if (Array.isArray(parsed)) {
      rawList = parsed;
    } else if (Array.isArray(parsed?.scores)) {
      rawList = parsed.scores;
    } else if (Array.isArray(parsed?.rubric_breakdown)) {
      rawList = parsed.rubric_breakdown;
    } else if (Array.isArray(parsed?.criteria)) {
      rawList = parsed.criteria;
    } else if (Array.isArray(parsed?.results)) {
      rawList = parsed.results;
    } else if (typeof parsed === "object" && parsed !== null) {
      rawList = Object.entries(parsed)
        .filter(([key]) => key !== "summary" && key !== "feedback" && key !== "strengths" && key !== "issues")
        .map(([name, val]) => {
          if (typeof val === "object" && val !== null) {
            return { name, ...val };
          }
          return { name, multiplier: typeof val === "number" ? val : 0 };
        });
    }

    const cleanStr = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

    const breakdown = {};
    const reasons = {};
    const multipliers = {};
    let totalScore = 0;

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
            const rawScore = scoredCriteria.score ?? scoredCriteria.awarded ?? scoredCriteria.points ?? scoredCriteria.points_awarded;
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
            scoredCriteria.reasoning || scoredCriteria.reason || scoredCriteria.comment || scoredCriteria.feedback || "Evaluated by code analysis.";
          multipliers[matchingRubric.name] = multiplier;
          totalScore += score;
        }
      });
    }

    // Check if any criteria were missed by AI and perform targeted retry
    const missingCriteria = rubric.criteria.filter((c) => breakdown[c.name] === undefined);
    if (missingCriteria.length > 0) {
      logger.warn(`AI omitted ${missingCriteria.length} criteria for Job ${jobId}. Performing targeted retry...`);
      try {
        const missingList = missingCriteria
          .map((c, i) => `${i + 1}. "${c.name}" (weight: ${c.weight} points): ${c.description || "No description provided."}`)
          .join("\n");
        const retryPrompt = `You are evaluating a student's Backend submission. The following criteria need evaluation:

## Student Source Code:
${codeString}

## Rubric Criteria:
${missingList}

Grade each criterion strictly using discrete multipliers (1.0, 0.9, 0.8, 0.5, 0.2, or 0.0). Return STRICTLY a JSON object:
{
  "scores": [
    { "name": "<exact criterion name>", "multiplier": <number 0.0, 0.2, 0.5, 0.8, 0.9, or 1.0>, "reasoning": "<1-2 sentence explanation>" }
  ]
}`;
        const retryResponse = await callDeepSeekWithRetry(openai, {
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
        logger.warn(`Retry for missing backend criteria failed: ${retryErr.message}`);
      }
    }

    // Fill in any remaining missing criteria
    for (const c of rubric.criteria) {
      if (breakdown[c.name] === undefined) {
        breakdown[c.name] = 0;
        reasons[c.name] = "Evaluation completed. Specific criteria breakdown unavailable.";
        multipliers[c.name] = 0.0;
      }
    }

    // Generate concise feedback summary
    const feedbackText = await generateBackendAIFeedback({
      rubric_breakdown: breakdown,
      rubric_criteria: rubric.criteria,
      per_criterion_reasons: reasons,
      score: totalScore,
      warnings: [],
      execution_logs: logs || "",
      assignmentType: "Node.js & Express Backend",
      codeSnippet: codeString || ""
    });

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

      if (mult >= 1.0) {
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

    const isTestFailed = (logs || "").toLowerCase().includes("test_failed") ||
      (logs || "").toLowerCase().includes("failed to compile") ||
      (logs || "").toLowerCase().includes("error: cannot find module");
    const isPassed = totalScore >= maxScore * 0.5;

    let status;
    if (isTestFailed && !isPassed) {
      status = "test_failed";
    } else if (isPassed) {
      status = "passed";
    } else {
      status = "failed";
    }

    const finalResult = {
      score: totalScore,
      rubric_breakdown: breakdown,
      feedback: rubricFeedback,
      rubricFeedback: rubricFeedback,
      warnings,
      execution_logs: logs || "",
      status,
      pass: isPassed,
      is_passed: isPassed,
    };

    // Cache result for identical submissions
    await setCachedEvaluation("backend", rubric, codeString, logs, testResults, finalResult);

    try {
      const logsDir = path.join(process.cwd(), "logs");
      if (!fs.existsSync(logsDir)) fs.mkdirSync(logsDir, { recursive: true });
      fs.writeFileSync(path.join(logsDir, "latest_backend_eval.json"), JSON.stringify({
        jobId,
        payload,
        manifest,
        finalResult
      }, null, 2));
    } catch {}

    return finalResult;

  } catch (err) {
    logger.error(`AI scoring failed for Backend Project (Job: ${jobId}):`, err.message);
    const breakdown = {};
    const unifiedBreakdown = [];
    for (const c of rubric.criteria) {
      breakdown[c.name] = 0;
      unifiedBreakdown.push({
        item: c.name,
        awarded: 0,
        max: c.weight,
        reason: `Grading note: ${err.message}`
      });
    }
    const rubricFeedback = {
      summary: "Evaluation completed with grading notice.",
      strengths: [],
      issues: [err.message],
      breakdown: unifiedBreakdown
    };
    return {
      score: 0,
      rubric_breakdown: breakdown,
      feedback: rubricFeedback,
      rubricFeedback: rubricFeedback,
      warnings: [err.message],
      execution_logs: logs || "",
      status: "failed",
      pass: false,
      is_passed: false,
    };
  } finally {
    if (repoPath) {
      await deleteRepo(repoPath);
    }
  }
}

/**
 * Evaluates a Backend project locally using Docker Sandbox.
 * Runs dependency installation and automated tests or syntax verification inside an isolated container.
 */
export async function evaluateBackendProjectWithDocker(payload, jobId) {
  let repoPath;
  try {
    const repoUrl = payload.repoUrl || payload.submission_link;
    logger.info(`🐳 Backend Docker evaluation started for job ${jobId}: ${repoUrl}`);

    // Step 1: Clone the student's repo locally (or use local path)
    repoPath = await cloneRepo(repoUrl);
    logger.info(`Cloned backend repo to: ${repoPath}`);

    // Step 2: Determine appropriate test or build command
    const packageJsonPath = path.join(repoPath, "package.json");
    let containerCmd = null;
    if (fs.existsSync(packageJsonPath)) {
      try {
        const pkg = JSON.parse(fs.readFileSync(packageJsonPath, "utf-8"));
        const testScript = pkg.scripts?.test || "";
        if (testScript && !testScript.includes("no test specified")) {
          containerCmd = "npm install --prefer-offline --no-audit --progress=false && npm test";
        }
      } catch {}
    }

    // Step 3: Run execution safely inside Docker container
    const dockerResult = await runInDockerSandbox(repoPath, { command: containerCmd });

    // Step 4: Evaluate backend project against rubric using real execution report
    const dockerPayload = {
      ...payload,
      repoUrl: repoPath
    };
    const finalResult = await evaluateBackendProject(dockerPayload, jobId, null, dockerResult.testOutput);

    logger.info(`✅ Backend Docker evaluation completed for job ${jobId}: score=${finalResult.score} (${dockerResult.durationMs}ms container run)`);

    return finalResult;
  } finally {
    if (repoPath) {
      await deleteRepo(repoPath);
    }
  }
}