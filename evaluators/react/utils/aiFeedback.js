/**
 * Requires: GROQ_API_KEY in .env file
 */
import Grok from "groq-sdk";
import OpenAI from "openai";
import logger from "../../../config/logger.js";
import fs from "fs/promises";
import path from "path";

let client = null;

function getClient() {
  if (client) return client;

  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    logger.warn("OPENAI_API_KEY not set — AI feedback will be skipped.");
    return null;
  }

  // Use the official OpenAI endpoint or DeepSeek
  client = new OpenAI({
    apiKey,
    baseURL: process.env.OPENAI_BASE_URL || 'https://api.deepseek.com',
  });

  logger.info("Groq client initialised.");
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
        logger.warn(`[ReactFeedback] AI API rate-limit/error (${err.message}). Retrying in ${delay}ms (attempt ${attempt}/${maxRetries})...`);
        await new Promise(r => setTimeout(r, delay));
      } else {
        throw err;
      }
    }
  }
}

/**
 * Generates AI-assisted feedback for a student's React submission.
 *
 * @param {Object} params
 * @param {Object} params.rubric_breakdown  - { criteriaName: score, ... }
 * @param {number} params.score             - Total score achieved
 * @param {string[]} params.warnings        - Warning messages from scorer
 * @param {string} params.execution_logs    - Raw build/test logs
 *
/**
 * Generates AI-assisted feedback for a student's submission.
 *
 * @param {Object} params
 * @param {Object} params.rubric_breakdown  - { criteriaName: score, ... }
 * @param {Array}  params.rubric_criteria   - [ { name, weight, description }, ... ]
 * @param {Object} params.per_criterion_reasons - { criteriaName: reasonString, ... }
 * @param {number} params.score             - Total score achieved
 * @param {string[]} params.warnings        - Warning messages from scorer
 * @param {string} params.execution_logs    - Raw build/test logs
 * @param {string} [params.assignmentType]  - 'React' or 'HTML, CSS & JavaScript DOM'
 * @param {string} [params.codeSnippet]     - Student's source code for citing specific lines
 *
 * @returns {Promise<string>} feedback - A comprehensive, instructional review with code fixes
 */
export async function generateAIFeedback({
  rubric_breakdown = {},
  rubric_criteria = [],
  per_criterion_reasons = {},
  score = 0,
  warnings = [],
  execution_logs = '',
  assignmentType = 'React',
  codeSnippet = '',
}) {
  const maxScore = rubric_criteria.length > 0
    ? rubric_criteria.reduce((sum, c) => sum + (c.weight || 0), 0)
    : 100;

  const percentage = maxScore > 0 ? Math.round((score / maxScore) * 100) : score;

  // 1. If 100% and zero warnings/failures, return clean high-achievement praise
  if (percentage === 100 && (!warnings || warnings.length === 0)) {
    return `Excellent work! All criteria passed successfully with a perfect score. Your ${assignmentType} application is well-structured, follows best practices, and is fully functional.`;
  }

  const openai = getClient();
  if (!openai) {
    return buildFallbackFeedback(rubric_breakdown, rubric_criteria, per_criterion_reasons, score, maxScore, warnings, assignmentType);
  }

  // 2. Build detailed technical breakdown for prompt
  const criteriaDetails = rubric_criteria.map(c => {
    const awarded = rubric_breakdown[c.name] ?? 0;
    const reason = per_criterion_reasons[c.name] || '';
    const status = awarded >= c.weight ? 'PASSED' : (awarded === 0 ? 'FAILED' : 'PARTIALLY PASSED');
    return `- [${status}] "${c.name}" (Awarded: ${awarded}/${c.weight} pts): ${c.description || ''}${reason ? ` | Evaluation Finding: ${reason}` : ''}`;
  }).join('\n');

  // Truncate code snippet and logs to conserve tokens while preserving technical details
  const truncatedCode = codeSnippet ? codeSnippet.slice(0, 3000) : '';
  const truncatedLogs = execution_logs ? execution_logs.slice(0, 1500) : '';

  const prompt = `
You are a senior tech lead and engineering instructor evaluating a student's ${assignmentType} project.

## Student Result Summary:
- Assignment Type: ${assignmentType}
- Total Score: ${score}/${maxScore} (${percentage}%)
- Rubric Breakdown & Evaluation Findings:
${criteriaDetails || 'No specific criteria breakdown available.'}

${warnings && warnings.length > 0 ? `## Execution Warnings:\n${warnings.join('\n')}\n` : ''}
${truncatedLogs ? `## Test / Execution Logs:\n${truncatedLogs}\n` : ''}
${truncatedCode ? `## Student Code Excerpt:\n\`\`\`\n${truncatedCode}\n\`\`\`\n` : ''}

## Instructions:
1. Write 2 to 4 concise, informative, and encouraging sentences directly addressing the student's actual results.
2. Directly reference specific technical components, functions, routes, middleware, or DOM elements that worked or caused failures based on the evaluation findings, code, and logs.
3. If the score is low or there are failing criteria, explain the root cause and provide ONE actionable, concrete suggestion to fix it.
4. CRITICAL: Never contradict the score. If the student received a low score (e.g., < 60) or failed criteria, do NOT say "Great work! All criteria passed successfully" or "fully functional".
5. Use stack-appropriate vocabulary for ${assignmentType} (do NOT mention React components or props on a Node/Express backend assignment; do NOT mention routes/database on a frontend DOM assignment).
6. Return plain text only. Do NOT use markdown headers, bullet points, or quotation marks around the entire response.
`.trim();

  try {
    const response = await callDeepSeekWithRetry(openai, {
      model: process.env.OPENAI_MODEL || "deepseek-chat",
      messages: [{ role: "user", content: prompt }],
      max_tokens: 300,
      temperature: 0.0,
      seed: 42,
    });

    const feedback = response.choices[0]?.message?.content?.trim();
    return feedback || buildFallbackFeedback(rubric_breakdown, rubric_criteria, per_criterion_reasons, score, maxScore, warnings, assignmentType);
  } catch (err) {
    logger.error(`[AIFeedback] LLM feedback generation failed: ${err.message}`);
    return buildFallbackFeedback(rubric_breakdown, rubric_criteria, per_criterion_reasons, score, maxScore, warnings, assignmentType);
  }
}

/**
 * Rule-based fallback feedback used when AI is unavailable or fails.
 */
function buildFallbackFeedback(
  rubric_breakdown = {},
  rubric_criteria = [],
  per_criterion_reasons = {},
  score = 0,
  maxScore = 100,
  warnings = [],
  assignmentType = 'React'
) {
  const percentage = maxScore > 0 ? Math.round((score / maxScore) * 100) : score;

  const fullyPassed = [];
  const partialOrFailed = [];

  if (rubric_criteria && rubric_criteria.length > 0) {
    for (const c of rubric_criteria) {
      const awarded = rubric_breakdown[c.name] ?? 0;
      if (awarded >= c.weight) {
        fullyPassed.push(c.name);
      } else {
        partialOrFailed.push(c.name);
      }
    }
  } else {
    for (const [name, pts] of Object.entries(rubric_breakdown)) {
      if (pts > 0) fullyPassed.push(name);
      else partialOrFailed.push(name);
    }
  }

  // Only praise if score is actually high (>= 90%) AND no criteria failed
  if (percentage >= 90 && partialOrFailed.length === 0) {
    return `Great work! All criteria passed successfully. Your ${assignmentType} application is well-structured and fully functional.`;
  }

  const passedStr = fullyPassed.length > 0
    ? `You successfully implemented: ${fullyPassed.join(', ')}. `
    : '';

  const issuesStr = partialOrFailed.length > 0
    ? `The following areas need attention: ${partialOrFailed.join(', ')}. `
    : '';

  let tip = '';
  const typeLower = (assignmentType || '').toLowerCase();
  if (typeLower.includes('node') || typeLower.includes('backend') || typeLower.includes('express')) {
    tip = 'Review the failing criteria and ensure your API endpoints, middleware, routing, and database queries handle error states and match specifications.';
  } else if (typeLower.includes('dom') || typeLower.includes('html')) {
    tip = 'Review the failing criteria to ensure all DOM element selectors, event listeners, and live UI updates match the specification.';
  } else if (typeLower.includes('python')) {
    tip = 'Review the failing criteria to ensure function return values, type conversions, and print outputs match the required format.';
  } else {
    tip = 'Review the failing criteria and ensure your components, state management, props, and lifecycle hooks are correctly implemented.';
  }

  return `${passedStr}${issuesStr}${tip}`.trim();
}

/**
 * Recursively reads .js and .jsx files from a directory, ignoring node_modules/dist/build.
 */
async function readProjectFiles(dir, fileList = []) {
  const files = await fs.readdir(dir);
  for (const file of files) {
    const filePath = path.join(dir, file);
    const stat = await fs.stat(filePath);
    if (stat.isDirectory()) {
      if (file !== 'node_modules' && file !== 'dist' && file !== 'build') {
        await readProjectFiles(filePath, fileList);
      }
    } else if (file.endsWith('.js') || file.endsWith('.jsx')) {
      fileList.push(filePath);
    }
  }
  return fileList;
}

/**
 * Uses Groq AI to read the actual React code files and grade their structure.
 */
export async function evaluateCodeStructure(projectPath) {
  const openai = getClient();
  if (!openai) return { scoreMultiplier: 1, reasoning: "AI offline, granted full credit." };

  try {
    const srcDir = path.join(projectPath, "src");
    let files = [];
    try {
      files = await readProjectFiles(srcDir);
    } catch {
      try { files = await readProjectFiles(projectPath); } catch { files = []; }
    }

    // Limit to 6 main files to save tokens
    files = files.slice(0, 6);
    if (files.length === 0) return { scoreMultiplier: 0, reasoning: "No source files found." };

    let codeStr = "";
    for (const f of files) {
      const relativePath = path.relative(projectPath, f);
      const content = await fs.readFile(f, "utf8");
      codeStr += `\n--- ${relativePath} ---\n${content.slice(0, 6000)}\n`;
    }

    const prompt = `
You are an expert React instructor grading the "Code Structure" portion of an assignment.
Read these main files from the student's submission:
${codeStr}

Evaluate component breakdown, hook usage, and React best practices.
Assign a discrete score multiplier based strictly on verified code quality:
- 1.0 = Excellent structure, clean hooks, idiomatic component decomposition.
- 0.9 = Very good structure with only minor formatting or styling gaps.
- 0.8 = Good structure with minor gaps (e.g., slightly large components, minor prop drilling).
- 0.5 = Partial structure (some components split, but messy state management).
- 0.2 = Minimal effort (almost everything dumped in one file).
- 0.0 = No valid React code or broken structure.

Use ONLY the discrete multipliers: 1.0, 0.9, 0.8, 0.5, 0.2, or 0.0. Do NOT use arbitrary floating-point values.

Output STRICTLY a JSON object:
{
  "scoreMultiplier": <number 0.0, 0.2, 0.5, 0.8, 0.9, or 1.0>,
  "reasoning": "<1 sentence explanation on what was good or bad>"
}
Do NOT include any markdown or text besides the raw JSON object.
`.trim();

    const response = await callDeepSeekWithRetry(openai, {
      model: process.env.OPENAI_MODEL || "deepseek-chat",
      messages: [{ role: "user", content: prompt }],
      max_tokens: 150,
      temperature: 0.0,
      seed: 42,
      response_format: { type: "json_object" }
    });

    const parsed = JSON.parse(response.choices[0].message.content.trim());
    return {
      scoreMultiplier: typeof parsed.scoreMultiplier === "number" ? parsed.scoreMultiplier : 1,
      reasoning: parsed.reasoning || ""
    };
  } catch (err) {
    logger.error("Code structure evaluation failed:", err.message);
    return { scoreMultiplier: 1, reasoning: "Evaluation errored, defaulting to full credit." };
  }
}