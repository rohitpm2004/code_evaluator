import OpenAI from 'openai';
import logger from '../../config/logger.js';
import path from 'path';
import fs from 'fs';
import { cloneRepo, deleteRepo } from '../react/repoService.js';
import { scoreFromTestResults } from './scoringService.js';
import { generateFullstackFeedback } from './feedbackService.js';
import { runInDockerSandbox } from '../../services/dockerSandboxService.js';
import { getCachedEvaluation, setCachedEvaluation } from '../../services/evaluationCache.js';

let client = null;

function getClient() {
  if (client) return client;
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    logger.warn('OPENAI_API_KEY not set - Fullstack AI feedback fallback will fail.');
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
        const delay = attempt * 1500 + Math.floor(Math.random() * 500);
        logger.warn(
          `[FullstackEval] AI API rate-limit/error (${err.message}). Retrying in ${delay}ms (attempt ${attempt}/${maxRetries})...`,
        );
        await new Promise((r) => setTimeout(r, delay));
      } else {
        throw err;
      }
    }
  }
}

const IGNORED_DIRS = new Set([
  'node_modules',
  'dist',
  'build',
  '.git',
  '.next',
  'public',
  'assets',
  'coverage',
  '.vscode',
  '.idea',
]);

const ALLOWED_EXTENSIONS = new Set([
  '.js',
  '.jsx',
  '.ts',
  '.tsx',
  '.mjs',
  '.cjs',
  '.css',
  '.html',
  '.json',
  '.sql',
  '.env.example',
]);

/**
 * Recursively reads relevant fullstack source files from a directory,
 * ignoring build artifacts, node_modules, and git internals.
 */
async function readProjectFiles(dir, fileList = []) {
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
      if (!IGNORED_DIRS.has(entry.toLowerCase())) {
        await readProjectFiles(filePath, fileList);
      }
    } else if (ALLOWED_EXTENSIONS.has(path.extname(entry).toLowerCase())) {
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
  let compressed = code.replace(/\/\*[\s\S]*?\*\/|\/\/.*/g, '');
  compressed = compressed.replace(/^\s*[\r\n]/gm, '');
  compressed = compressed.replace(/[ \t]{2,}/g, ' ');
  return compressed;
}

/**
 * Categorizes and ranks fullstack source files into Frontend, Backend, and Shared/Root.
 * Guarantees balanced evaluation so neither client nor server is starved of token budget.
 */
function getFullstackFileClassification(filePath) {
  const norm = filePath.replace(/\\/g, '/').toLowerCase();
  const base = path.basename(norm);

  const isBackend =
    norm.includes('/server/') ||
    norm.includes('/backend/') ||
    norm.includes('/api/') ||
    norm.includes('/routes/') ||
    norm.includes('/controllers/') ||
    norm.includes('/models/') ||
    norm.includes('/prisma/') ||
    norm.includes('/db/');

  const isFrontend =
    norm.includes('/client/') ||
    norm.includes('/frontend/') ||
    norm.includes('/src/components/') ||
    norm.includes('/src/pages/') ||
    norm.includes('/src/views/') ||
    norm.includes('/src/hooks/') ||
    norm.includes('/src/context/') ||
    norm.includes('/src/store/') ||
    /\.(jsx|tsx)$/i.test(base);

  if (isBackend) {
    // Backend priority:
    // Tier 1: Main server entrypoints
    if (/^(server|app|index|main)\.(jsx?|tsx?|mjs|cjs)$/i.test(base)) return { layer: 'backend', priority: 1 };
    // Tier 2: Routes & API endpoints
    if (norm.includes('/routes/') || norm.includes('/api/')) return { layer: 'backend', priority: 2 };
    // Tier 3: Controllers & Handlers
    if (norm.includes('/controllers/') || norm.includes('/handlers/')) return { layer: 'backend', priority: 3 };
    // Tier 4: Database models, schemas, SQL
    if (norm.includes('/models/') || norm.includes('/schemas/') || norm.includes('/prisma/') || norm.includes('/db/') || norm.endsWith('.sql')) return { layer: 'backend', priority: 4 };
    // Tier 5: Middleware & Authentication
    if (norm.includes('/middleware/') || norm.includes('/auth/')) return { layer: 'backend', priority: 5 };
    // Tier 6: Services, Utils, Config
    if (norm.includes('/services/') || norm.includes('/utils/') || norm.includes('/config/')) return { layer: 'backend', priority: 6 };
    return { layer: 'backend', priority: 7 };
  }

  if (isFrontend) {
    // Frontend priority:
    // Tier 1: Main React entrypoints
    if (/^(app|main|index)\.(jsx?|tsx?)$/i.test(base) || base === 'index.html') return { layer: 'frontend', priority: 1 };
    // Tier 2: Components & Pages
    if (norm.includes('/pages/') || norm.includes('/components/') || norm.includes('/views/')) return { layer: 'frontend', priority: 2 };
    // Tier 3: State & Data Fetching (Hooks, API clients, Context, Store)
    if (norm.includes('/services/') || norm.includes('/api/') || norm.includes('/hooks/') || norm.includes('/context/') || norm.includes('/store/')) return { layer: 'frontend', priority: 3 };
    // Tier 4: Utils & Styles
    return { layer: 'frontend', priority: 4 };
  }

  // Shared / Root files
  if (base === 'package.json') return { layer: 'shared', priority: 1 };
  if (/^(docker-compose|dockerfile|\.env\.example)$/i.test(base)) return { layer: 'shared', priority: 2 };
  if (/\.(jsx|tsx|css|html)$/i.test(base)) return { layer: 'frontend', priority: 5 };
  return { layer: 'backend', priority: 7 };
}

/**
 * Reads project files with dual-layer priority ranking and generates a Source Manifest.
 * Prevents arbitrary cut-offs and guarantees balanced inclusion of both client and server files.
 */
async function getProjectCodeData(projectPath) {
  const allFiles = await readProjectFiles(projectPath);

  if (allFiles.length === 0) {
    return {
      codeString: '',
      manifest: { totalFiles: 0, includedFiles: [], omittedFiles: [] },
    };
  }

  const backendFiles = [];
  const frontendFiles = [];
  const sharedFiles = [];

  for (const f of allFiles) {
    const classification = getFullstackFileClassification(f);
    if (classification.layer === 'backend') {
      backendFiles.push({ file: f, priority: classification.priority });
    } else if (classification.layer === 'frontend') {
      frontendFiles.push({ file: f, priority: classification.priority });
    } else {
      sharedFiles.push({ file: f, priority: classification.priority });
    }
  }

  // Sort each group by priority tier first, then alphabetical path
  const sortFn = (a, b) => (a.priority !== b.priority ? a.priority - b.priority : a.file.localeCompare(b.file));
  backendFiles.sort(sortFn);
  frontendFiles.sort(sortFn);
  sharedFiles.sort(sortFn);

  const MAX_TOTAL_CHARS = 46000;
  const MAX_PER_FILE_CHARS = 5500;
  const includedFiles = [];
  const omittedFiles = [];
  let codeBody = '';

  const appendFile = async (f, budget) => {
    const relativePath = path.relative(projectPath, f).replace(/\\/g, '/');
    try {
      const content = await fs.promises.readFile(f, 'utf8');
      const compressed = compressCode(content);
      const sliceSize = Math.min(compressed.length, MAX_PER_FILE_CHARS);
      if (codeBody.length + sliceSize <= budget) {
        includedFiles.push(relativePath);
        codeBody += `\n--- ${relativePath} ---\n${compressed.slice(0, sliceSize)}\n`;
        return true;
      } else {
        omittedFiles.push(relativePath);
        return false;
      }
    } catch {
      omittedFiles.push(relativePath);
      return false;
    }
  };

  // 1. Shared / Root files first (package.json, config)
  for (const item of sharedFiles.slice(0, 3)) {
    await appendFile(item.file, MAX_TOTAL_CHARS);
  }

  // 2. Interleave top backend and top frontend files in balanced proportions
  const maxPairs = Math.max(backendFiles.length, frontendFiles.length);
  for (let i = 0; i < maxPairs; i++) {
    if (i < backendFiles.length) {
      await appendFile(backendFiles[i].file, MAX_TOTAL_CHARS);
    }
    if (i < frontendFiles.length) {
      await appendFile(frontendFiles[i].file, MAX_TOTAL_CHARS);
    }
    if (codeBody.length >= MAX_TOTAL_CHARS - 1000) break;
  }

  // Mark any remaining files as omitted
  for (const item of [...backendFiles, ...frontendFiles, ...sharedFiles]) {
    const rel = path.relative(projectPath, item.file).replace(/\\/g, '/');
    if (!includedFiles.includes(rel) && !omittedFiles.includes(rel)) {
      omittedFiles.push(rel);
    }
  }

  let manifestHeader = `[SOURCE MANIFEST: Discovered ${allFiles.length} files. Included: ${includedFiles.length}. Omitted: ${omittedFiles.length}]\n`;
  manifestHeader += `Included files: ${includedFiles.join(', ')}\n`;
  if (omittedFiles.length > 0) {
    manifestHeader += `Omitted files (due to token budget cap): ${omittedFiles.join(', ')}\n`;
  }
  manifestHeader += `========================================\n`;

  return {
    codeString: manifestHeader + codeBody,
    manifest: {
      totalFiles: allFiles.length,
      includedFiles,
      omittedFiles,
    },
  };
}

/**
 * Evaluates a Full Stack project.
 * Tries mathematical scoring via Playwright test results first.
 * Falls back to AI-based code analysis if test results are absent/failed.
 */
export async function evaluateFullstackProject(payload, jobId, testResults, logs, codeContext = null) {
  const rawCriteria = payload.rubric?.criteria || [];
  const criteria = rawCriteria.map((c) => ({
    name: c.name,
    weight: typeof c.weight === 'number' ? c.weight : (typeof c.score === 'number' ? c.score : 20),
    description: c.description || '',
    layer: c.layer || 'general',
  }));
  const rubric = { ...payload.rubric, criteria };
  const maxScore = rubric.criteria.reduce((sum, c) => sum + c.weight, 0);

  const skipCache = Boolean(payload.skipCache || payload.reEvaluate || payload.isReEvaluation);

  // 1. Try mathematical scoring if Playwright test results are present
  const testGrading = scoreFromTestResults(rubric, testResults);
  if (testGrading) {
    logger.info(`Deterministic testResults found. Grading Fullstack project mathematically for Job ${jobId}`);

    const status = testGrading.score >= maxScore * 0.5 ? 'pass' : 'fail';

    // Parse Playwright spec details to flat array
    const testDetails = [];
    function recurseSuites(suite) {
      if (Array.isArray(suite.suites)) suite.suites.forEach(recurseSuites);
      if (Array.isArray(suite.specs)) {
        suite.specs.forEach((spec) => {
          const testRun = spec.tests?.[0];
          const result = testRun?.results?.[0];
          testDetails.push({
            name: spec.title || '',
            status: result?.status === 'passed' ? 'pass' : 'fail',
            error: result?.error?.message || null,
          });
        });
      }
    }
    if (testResults.suites) {
      testResults.suites.forEach(recurseSuites);
    }

    // Generate technical AI review paragraph
    const feedbackText = await generateFullstackFeedback({
      score: testGrading.score,
      maxScore,
      rubric_breakdown: testGrading.rubric_breakdown,
      rubric_criteria: rubric.criteria,
      per_criterion_reasons: testGrading.reasons,
      testDetails,
      execution_logs: logs || '',
    });

    // Format output to match other evaluators
    const strengths = [];
    const issues = [];
    const unifiedBreakdown = [];

    for (const c of rubric.criteria) {
      const awarded = testGrading.rubric_breakdown[c.name] ?? 0;
      const mult = testGrading.multipliers[c.name] ?? 0.0;
      const reason = testGrading.reasons[c.name] || 'Criterion evaluated by Playwright checks.';

      unifiedBreakdown.push({
        item: c.name,
        awarded,
        max: c.weight,
        reason,
      });

      if (mult >= 0.8 || awarded === c.weight) {
        strengths.push(`[${c.name}] ${reason} (earned ${awarded}/${c.weight} marks)`);
      } else {
        issues.push(`[${c.name}] ${reason} (earned ${awarded}/${c.weight} marks)`);
      }
    }

    if (strengths.length === 0 && testGrading.score === maxScore) {
      strengths.push('All criteria passed successfully.');
    }
    if (issues.length === 0 && testGrading.score < maxScore) {
      issues.push('Some criteria received partial marks.');
    }

    const rubricFeedback = {
      summary: feedbackText || 'Evaluation completed successfully.',
      strengths,
      issues,
      breakdown: unifiedBreakdown,
    };

    const finalResult = {
      score: testGrading.score,
      rubric_breakdown: testGrading.rubric_breakdown,
      feedback: rubricFeedback,
      rubricFeedback: rubricFeedback,
      warnings: [],
      execution_logs: logs || '',
      status,
    };

    if (!skipCache) {
      await setCachedEvaluation('fullstack', rubric, '', logs, testResults, finalResult);
    }

    return finalResult;
  }

  // 2. Fallback: AI-based code analysis (Dual-layer, deterministic scoring)
  logger.info(`Test results absent or empty for Job ${jobId}. Falling back to AI code grading.`);

  let repoPath;
  try {
    let codeString = codeContext;
    if (!codeString) {
      repoPath = await cloneRepo(payload.repoUrl || payload.submission_link, { preserveRoot: true });
      const codeData = await getProjectCodeData(repoPath);
      codeString = codeData.codeString;
    }

    if (!codeString) {
      logger.warn(`No source files found in student repo for Job ${jobId}`);
      const breakdown = {};
      const unifiedBreakdown = [];
      const issues = ['No source files found in the repository.'];
      for (const c of rubric.criteria) {
        breakdown[c.name] = 0;
        unifiedBreakdown.push({
          item: c.name,
          awarded: 0,
          max: c.weight,
          reason: 'No source files found.',
        });
      }
      const rubricFeedback = {
        summary: 'No source files found in the repository.',
        strengths: [],
        issues,
        breakdown: unifiedBreakdown,
      };
      return {
        score: 0,
        rubric_breakdown: breakdown,
        feedback: rubricFeedback,
        rubricFeedback: rubricFeedback,
        warnings: ['No source files found in the repository.'],
        execution_logs: logs || '',
        status: 'fail',
      };
    }

    // Check evaluation cache for deterministic, stable scoring across re-evaluations
    if (!skipCache) {
      const cached = await getCachedEvaluation('fullstack', rubric, codeString, logs, testResults);
      if (cached) {
        logger.info(`⚡ Returning cached Fullstack evaluation result for Job ${jobId}: score=${cached.score}/${maxScore}`);
        return cached;
      }
    }

    const openai = getClient();
    if (!openai) {
      logger.error(`OpenAI client unavailable for Fullstack Job ${jobId} and deterministic test results were absent.`);
      throw new Error('AI evaluation service unavailable: OPENAI_API_KEY is missing or invalid on the server.');
    }

    const assignmentTitle = rubric.title || rubric.assignmentTitle || payload.title || 'Fullstack Web Application';
    const assignmentContext = rubric.description || rubric.assignmentDescription || payload.description || '';

    const criteriaList = rubric.criteria
      .map((c, i) => `${i + 1}. "${c.name}" (weight: ${c.weight} points, layer: ${c.layer || 'general'}): ${c.description || 'No description provided.'}`)
      .join('\n');

    const prompt = `You are an expert Full Stack (React, Node/Express, MongoDB/SQL) instructor grading a student's fullstack web application.

## Assignment: ${assignmentTitle}
${assignmentContext ? `## Assignment Requirements & Context:\n${assignmentContext}\n` : ''}
## GitHub Actions / Execution Report:
${logs || 'No report available.'}

## Student's Source Code & Manifest:
${codeString}

## Rubric Criteria to Evaluate:
${criteriaList}

## Grading Instructions:
You must be an objective, thorough instructor.
Read the source code carefully across BOTH frontend (React components, state, hooks, API calls) and backend (Express routes, controllers, middleware, database models) to determine if the functionality requested in the rubric actually exists.

CRITICAL RULES:
1. ASSIGNMENT RELEVANCE CHECK:
   - Verify that the submission actually attempts the assignment defined in the rubric criteria.
   - If the student submitted a completely unrelated project (e.g. an empty template, counter app, or unrelated homework), they MUST receive 0 marks for all functional criteria (Setup can get max 2 marks, all other criteria must be strictly 0).
   - Do NOT give credit for code that does not implement the required features.

2. DUAL-LAYER BALANCED EVALUATION:
   - For Frontend criteria ("Frontend UI", "State & Data Fetching"): Evaluate the React JSX components, props, hooks, and fetch/axios requests.
   - For Backend criteria ("Backend API", "Backend Logic & Database"): Evaluate the Express route handlers, controllers, queries, and database models.
   - For Integration criteria ("Fullstack Integration"): Verify that the frontend actually calls the backend API endpoints and handles the responses.
   - Note the [SOURCE MANIFEST] at the top: If secondary files are listed as omitted due to token budget, do NOT penalize the student for them if the core implementation is present in the included files.
   - Do NOT penalize if .env file is missing (expected for security reasons).

Assign a discrete score multiplier for EACH criterion based strictly on verified code evidence:
- 1.0 = Fully meets all exact requirements for this criterion (components/routes/logic complete and correct).
- 0.9 = Mostly correct with only very minor syntax, naming, or styling gaps.
- 0.8 = Good implementation but missing one specific sub-feature or edge case.
- 0.5 = Partial implementation (~50% of required functionality present).
- 0.2 = Bare minimum skeleton, stub, or boilerplate only.
- 0.0 = Not attempted, completely missing, or completely unrelated project.

CRITICAL SCORING RULE:
Use ONLY the discrete multipliers: 1.0, 0.9, 0.8, 0.5, 0.2, or 0.0.
Do NOT use intermediate or arbitrary decimal values like 0.84, 0.91, 0.73, etc.

For EACH criterion write a concise 1-2 sentence explanation:
1. States specifically what was FOUND in the source code (cite actual component, route, or query names).
2. States specifically what is MISSING or WRONG compared to the rubric requirements.

CRITICAL REQUIREMENT:
You MUST evaluate and return a score entry for ALL ${rubric.criteria.length} criteria listed above. Do not stop early. Do not omit any criterion. Keep explanations to 1-2 sentences.

Output STRICTLY a JSON object (no markdown, no extra text):
{
  "scores": [
    { "name": "<exact criterion name>", "multiplier": <number 0.0, 0.2, 0.5, 0.8, 0.9, or 1.0>, "reasoning": "<1-2 sentence concise explanation>" }
  ]
}`;

    logger.info(`Sending balanced fullstack code to AI for rubric scoring (Job: ${jobId})...`);

    const response = await callDeepSeekWithRetry(openai, {
      model: process.env.OPENAI_MODEL || 'deepseek-chat',
      messages: [{ role: 'user', content: prompt }],
      max_tokens: 3000,
      temperature: 0.0,
      seed: 42,
      response_format: { type: 'json_object' },
    });

    let rawContent = response.choices[0]?.message?.content?.trim() || '';
    logger.info(`AI response received (length: ${rawContent.length}) for Job ${jobId}`);

    // Strip markdown code fences
    if (rawContent.startsWith('```')) {
      rawContent = rawContent.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '').trim();
    }
    // Strip DeepSeek reasoning/think tags
    rawContent = rawContent.replace(/<think>[\s\S]*?<\/think>/gi, '').trim();

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
          const lastObjEnd = rawContent.lastIndexOf('}');
          if (lastObjEnd !== -1) {
            const repaired = rawContent.slice(0, lastObjEnd + 1) + ']}';
            parsed = JSON.parse(repaired);
            logger.info('Successfully repaired truncated JSON.');
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
    } else if (typeof parsed === 'object' && parsed !== null) {
      rawList = Object.entries(parsed)
        .filter(([key]) => key !== 'summary' && key !== 'feedback' && key !== 'strengths' && key !== 'issues')
        .map(([name, val]) => {
          if (typeof val === 'object' && val !== null) {
            return { name, ...val };
          }
          return { name, multiplier: typeof val === 'number' ? val : 0 };
        });
    }

    const cleanStr = (s) => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');

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
            if (typeof rawScore === 'number') {
              rawMult = rawScore > 1 ? rawScore / matchingRubric.weight : rawScore;
            }
          }

          const multiplier =
            typeof rawMult === 'number'
              ? Math.max(0, Math.min(1, rawMult))
              : 0.5;

          const score = Math.round(matchingRubric.weight * multiplier);
          breakdown[matchingRubric.name] = score;
          reasons[matchingRubric.name] =
            scoredCriteria.reasoning || scoredCriteria.reason || scoredCriteria.comment || scoredCriteria.feedback || 'Evaluated by code analysis.';
          multipliers[matchingRubric.name] = multiplier;
          totalScore += score;
        }
      });
    }

    // Check if any criteria were missed by AI and perform targeted retry
    const missingCriteria = rubric.criteria.filter((c) => breakdown[c.name] === undefined);
    if (missingCriteria.length > 0) {
      logger.warn(`AI omitted ${missingCriteria.length} criteria for Fullstack Job ${jobId}. Performing targeted retry...`);
      try {
        const missingList = missingCriteria
          .map((c, i) => `${i + 1}. "${c.name}" (weight: ${c.weight} points): ${c.description || 'No description provided.'}`)
          .join('\n');
        const retryPrompt = `You are evaluating a student's Fullstack submission. The following criteria need evaluation:

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
          model: process.env.OPENAI_MODEL || 'deepseek-chat',
          messages: [{ role: 'user', content: retryPrompt }],
          max_tokens: 1500,
          temperature: 0.0,
          seed: 42,
          response_format: { type: 'json_object' },
        });
        let retryRaw = retryResponse.choices[0]?.message?.content?.trim() || '';
        if (retryRaw.startsWith('```')) {
          retryRaw = retryRaw.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '').trim();
        }
        retryRaw = retryRaw.replace(/<think>[\s\S]*?<\/think>/gi, '').trim();
        let retryParsed = null;
        try {
          retryParsed = JSON.parse(retryRaw);
        } catch {
          const m = retryRaw.match(/\{[\s\S]*\}/);
          if (m) try { retryParsed = JSON.parse(m[0]); } catch {}
        }

        const retryList = Array.isArray(retryParsed)
          ? retryParsed
          : (Array.isArray(retryParsed?.scores) ? retryParsed.scores : []);

        retryList.forEach((scored) => {
          if (!scored) return;
          const sName = cleanStr(scored.name);
          const match = missingCriteria.find((c) => cleanStr(c.name) === sName || cleanStr(c.name).includes(sName));
          if (match && breakdown[match.name] === undefined) {
            const mult = typeof scored.multiplier === 'number' ? Math.max(0, Math.min(1, scored.multiplier)) : 0.5;
            const score = Math.round(match.weight * mult);
            breakdown[match.name] = score;
            reasons[match.name] = scored.reasoning || scored.reason || 'Evaluated by code analysis.';
            multipliers[match.name] = mult;
            totalScore += score;
          }
        });
      } catch (retryErr) {
        logger.warn(`Retry for missing fullstack criteria failed: ${retryErr.message}`);
      }
    }

    // Fill in any remaining missing criteria
    for (const c of rubric.criteria) {
      if (breakdown[c.name] === undefined) {
        breakdown[c.name] = 0;
        reasons[c.name] = 'Evaluation completed. Specific criteria breakdown unavailable.';
        multipliers[c.name] = 0.0;
      }
    }

    // Generate concise feedback summary
    const feedbackText = await generateFullstackFeedback({
      score: totalScore,
      maxScore,
      rubric_breakdown: breakdown,
      rubric_criteria: rubric.criteria,
      per_criterion_reasons: reasons,
      codeSnippet: codeString || '',
      execution_logs: logs || '',
    });

    const strengths = [];
    const issues = [];
    const unifiedBreakdown = [];

    for (const c of rubric.criteria) {
      const awarded = breakdown[c.name] ?? 0;
      const mult = multipliers[c.name] ?? 0.0;
      const reason = reasons[c.name] || 'Criterion evaluated by code analysis.';

      unifiedBreakdown.push({
        item: c.name,
        awarded,
        max: c.weight,
        reason,
      });

      if (mult >= 0.8 || awarded === c.weight) {
        strengths.push(`[${c.name}] ${reason} (earned ${awarded}/${c.weight} marks)`);
      } else {
        issues.push(`[${c.name}] ${reason} (earned ${awarded}/${c.weight} marks)`);
      }
    }

    if (strengths.length === 0 && totalScore === maxScore) {
      strengths.push('All criteria passed successfully.');
    }
    if (issues.length === 0 && totalScore < maxScore) {
      issues.push('Some criteria received partial marks.');
    }

    const rubricFeedback = {
      summary: feedbackText || 'Evaluation completed successfully.',
      strengths,
      issues,
      breakdown: unifiedBreakdown,
    };

    const finalResult = {
      score: totalScore,
      rubric_breakdown: breakdown,
      feedback: rubricFeedback,
      rubricFeedback: rubricFeedback,
      warnings: [],
      execution_logs: logs || '',
      status: totalScore >= maxScore * 0.5 ? 'pass' : 'fail',
    };

    // Cache the deterministic score in Redis/Memory
    if (!skipCache && totalScore !== undefined) {
      await setCachedEvaluation('fullstack', rubric, codeString, logs, testResults, finalResult);
    }

    return finalResult;
  } catch (err) {
    logger.error(`AI scoring failed for Fullstack Project (Job: ${jobId}):`, err.message);
    const breakdown = {};
    const unifiedBreakdown = [];
    for (const c of rubric.criteria) {
      breakdown[c.name] = 0;
      unifiedBreakdown.push({
        item: c.name,
        awarded: 0,
        max: c.weight,
        reason: `Grading note: ${err.message}`,
      });
    }
    const rubricFeedback = {
      summary: 'Evaluation completed with grading notice.',
      strengths: [],
      issues: [err.message],
      breakdown: unifiedBreakdown,
    };
    return {
      score: 0,
      rubric_breakdown: breakdown,
      feedback: rubricFeedback,
      rubricFeedback: rubricFeedback,
      warnings: [err.message],
      execution_logs: logs || '',
      status: 'fail',
    };
  } finally {
    if (repoPath) {
      await deleteRepo(repoPath);
    }
  }
}

/**
 * Runs fullstack project build and test verification inside an isolated container,
 * then scores the project using real container execution feedback.
 */
export async function evaluateFullstackProjectWithDocker(payload, jobId) {
  let repoPath;
  try {
    const repoUrl = payload.repoUrl || payload.submission_link;
    logger.info(`🐳 Fullstack Docker evaluation started for job ${jobId}: ${repoUrl}`);

    // Step 1: Clone the student's repo locally (or use local path)
    repoPath = await cloneRepo(repoUrl, { preserveRoot: true });
    logger.info(`Cloned fullstack repo to: ${repoPath}`);

    // Step 2: Run build / test safely inside local Docker container
    const dockerResult = await runInDockerSandbox(repoPath);

    // Step 3: Evaluate project against rubric using real execution report
    const dockerPayload = {
      ...payload,
      repoUrl: repoPath,
    };
    const finalResult = await evaluateFullstackProject(dockerPayload, jobId, null, dockerResult.testOutput);

    logger.info(
      `✅ Fullstack Docker evaluation completed for job ${jobId}: score=${finalResult.score} (${dockerResult.durationMs}ms container run)`,
    );

    return finalResult;
  } finally {
    if (repoPath) {
      await deleteRepo(repoPath);
    }
  }
}
