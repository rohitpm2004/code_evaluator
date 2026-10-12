import OpenAI from "openai";
import dotenv from "dotenv";
import logger from "../../config/logger.js";

dotenv.config();

let client = null;

function getOpenAIClient() {
  if (client) return client;
  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    logger.warn("[FullstackFeedback] OPENAI_API_KEY is not set.");
    return null;
  }
  client = new OpenAI({
    apiKey,
    baseURL: process.env.OPENAI_BASE_URL || 'https://api.deepseek.com',
  });
  return client;
}

/**
 * Generates structured, educational AI feedback for a fullstack submission using deepseek-v4-flash.
 *
 * Supports both options object:
 *   generateFullstackFeedback({ score, maxScore, rubric_breakdown, rubric_criteria, per_criterion_reasons, testDetails, codeSnippet, execution_logs, warnings })
 * and legacy arguments:
 *   generateFullstackFeedback(backendResultsOrTestDetails, frontendResultsOrRubric, rubric)
 *
 * @param {Object|Array} optionsOrTestDetails
 * @param {Object} [frontendResultsOrRubric]
 * @param {Object} [rubricParam]
 * @returns {Promise<string>}
 */
export async function generateFullstackFeedback(optionsOrTestDetails, frontendResultsOrRubric, rubricParam) {
  let score = 0;
  let maxScore = 100;
  let rubric_breakdown = {};
  let rubric_criteria = [];
  let per_criterion_reasons = {};
  let testDetails = [];
  let codeSnippet = '';
  let execution_logs = '';
  let warnings = [];

  // Check if called with options object
  if (
    optionsOrTestDetails &&
    typeof optionsOrTestDetails === 'object' &&
    !Array.isArray(optionsOrTestDetails) &&
    ('score' in optionsOrTestDetails || 'rubric_criteria' in optionsOrTestDetails || 'rubric_breakdown' in optionsOrTestDetails || 'testDetails' in optionsOrTestDetails)
  ) {
    const opts = optionsOrTestDetails;
    score = typeof opts.score === 'number' ? opts.score : 0;
    rubric_criteria = opts.rubric_criteria || opts.rubric?.criteria || [];
    maxScore = opts.maxScore || (rubric_criteria.length > 0 ? rubric_criteria.reduce((s, c) => s + (c.weight || 0), 0) : 100);
    rubric_breakdown = opts.rubric_breakdown || {};
    per_criterion_reasons = opts.per_criterion_reasons || opts.reasons || {};
    testDetails = Array.isArray(opts.testDetails) ? opts.testDetails : [];
    codeSnippet = opts.codeSnippet || '';
    execution_logs = opts.execution_logs || opts.logs || '';
    warnings = opts.warnings || [];
  } else {
    // Legacy signatures
    if (Array.isArray(optionsOrTestDetails)) {
      testDetails = optionsOrTestDetails;
      rubric_criteria = frontendResultsOrRubric?.criteria || rubricParam?.criteria || [];
    } else {
      const backendResults = optionsOrTestDetails || { test_details: [] };
      const frontendResultsObj = frontendResultsOrRubric || { test_details: [] };
      const bTests = (backendResults.test_details || []).map(t => ({ ...t, layer: 'Backend' }));
      const fTests = (frontendResultsObj.test_details || []).map(t => ({ ...t, layer: 'Frontend' }));
      testDetails = [...bTests, ...fTests];
      rubric_criteria = rubricParam?.criteria || frontendResultsOrRubric?.criteria || [];
    }
    maxScore = rubric_criteria.length > 0 ? rubric_criteria.reduce((s, c) => s + (c.weight || 0), 0) : 100;
    score = Math.round(maxScore * 0.5); // Fallback estimate if not supplied
  }

  const finalMax = maxScore > 0 ? maxScore : 100;
  const percentage = Math.round((score / finalMax) * 100);

  // Extract test failures
  const testFailures = testDetails.filter(t => t.status === 'fail' || t.status === 'failed');
  const failureContext = testFailures
    .map(f => `[${f.layer || 'Test'}] ${f.name || f.title || 'Assertion'}: ${f.error || f.failureMessage || 'Test failed'}`.slice(0, 300))
    .join('\n');

  // Check if criteria failed
  const failedCriteria = rubric_criteria.filter(c => {
    const awarded = rubric_breakdown[c.name];
    return awarded !== undefined && awarded < c.weight;
  });

  // 1. Strict Praise Gate: Only praise if high score (>= 90%) AND no test failures AND no failed criteria
  if (percentage >= 90 && testFailures.length === 0 && failedCriteria.length === 0 && (!warnings || warnings.length === 0)) {
    return "Excellent work! Both your frontend interface and backend API passed all criteria with a high score. Your fullstack application is well-architected, handles cross-origin client-server communication reliably, and manages data flow and state effectively.";
  }

  const openai = getOpenAIClient();
  if (!openai) {
    return buildFallbackFeedback({ score, maxScore: finalMax, percentage, rubric_criteria, rubric_breakdown, per_criterion_reasons, testFailures, failureContext });
  }

  // 2. Build detailed criteria breakdown
  const criteriaDetails = rubric_criteria.map(c => {
    const awarded = rubric_breakdown[c.name] ?? 0;
    const reason = per_criterion_reasons[c.name] || '';
    const status = awarded >= c.weight ? 'PASSED' : (awarded === 0 ? 'FAILED' : 'PARTIALLY PASSED');
    return `- [${status}] [${c.layer || 'Fullstack'}] "${c.name}" (Awarded: ${awarded}/${c.weight} pts): ${c.description || ''}${reason ? ` | Finding: ${reason}` : ''}`;
  }).join('\n');

  const truncatedCode = codeSnippet ? codeSnippet.slice(0, 3000) : '';
  const truncatedLogs = execution_logs ? execution_logs.slice(0, 1500) : '';

  const prompt = `
You are a senior fullstack engineering instructor evaluating a student's fullstack web assignment (React frontend + Node/Express backend + REST API/Database).

## Student Result Summary:
- Assignment Type: Full Stack (React & Node.js/Express)
- Total Score: ${score}/${finalMax} (${percentage}%)
- Rubric Breakdown & Evaluation Findings:
${criteriaDetails || 'No specific criteria breakdown available.'}

${failureContext ? `## Automated Test Failures:\n${failureContext}\n` : ''}
${warnings && warnings.length > 0 ? `## Warnings:\n${warnings.join('\n')}\n` : ''}
${truncatedLogs ? `## Execution Logs:\n${truncatedLogs}\n` : ''}
${truncatedCode ? `## Student Code Excerpt:\n\`\`\`\n${truncatedCode}\n\`\`\`\n` : ''}

## Instructions:
1. Write 2 to 4 concise, helpful sentences directly evaluating what was built across both frontend and backend.
2. Address both layers specifically:
   - Frontend: cite specific React components, hooks (useState, useEffect), props, or client-side fetch/axios requests.
   - Backend: cite specific Express routes (e.g. GET /api/..., POST /api/...), controller methods, HTTP status codes (200, 201, 400, 500), CORS configuration, or database queries.
3. If the score is low (< 70%) or there are failing criteria / test failures, explain the technical root cause and provide ONE concrete, actionable recommendation to fix it.
4. CRITICAL: Never contradict the score. If the student received a low score or failed criteria/tests, do NOT say "Great work! All criteria passed successfully" or "fully functional".
5. Return plain text only. Do NOT use markdown headers, bullet points, or quotation marks around the entire response.
`.trim();

  try {
    const response = await openai.chat.completions.create({
      model: process.env.OPENAI_MODEL || "deepseek-chat",
      messages: [{ role: "user", content: prompt }],
      max_tokens: 350,
      temperature: 0.0,
      seed: 42,
    });

    const feedback = response.choices[0]?.message?.content?.trim();
    return feedback || buildFallbackFeedback({ score, maxScore: finalMax, percentage, rubric_criteria, rubric_breakdown, per_criterion_reasons, testFailures, failureContext });
  } catch (err) {
    logger.error(`[FullstackFeedback] LLM feedback generation failed: ${err.message}`);
    return buildFallbackFeedback({ score, maxScore: finalMax, percentage, rubric_criteria, rubric_breakdown, per_criterion_reasons, testFailures, failureContext });
  }
}

/**
 * Score-aware fallback feedback if LLM is unavailable or fails.
 */
function buildFallbackFeedback({ score, maxScore, percentage, rubric_criteria = [], rubric_breakdown = {}, per_criterion_reasons = {}, testFailures = [], failureContext = '' }) {
  const partialOrFailed = [];

  for (const c of rubric_criteria) {
    const awarded = rubric_breakdown[c.name] ?? 0;
    if (awarded < c.weight) {
      const reason = per_criterion_reasons[c.name] ? ` (${per_criterion_reasons[c.name]})` : '';
      partialOrFailed.push(`"${c.name}" [${awarded}/${c.weight} pts]${reason}`);
    }
  }

  if (percentage >= 90 && partialOrFailed.length === 0 && testFailures.length === 0) {
    return `Great work! Your fullstack project scored ${score}/${maxScore} (${percentage}%). Both your backend API routes and frontend interface pass nearly all project criteria.`;
  }

  if (percentage >= 70 && partialOrFailed.length <= 1) {
    const issueText = partialOrFailed.length > 0 ? ` Review ${partialOrFailed[0]} for minor adjustments.` : '';
    return `Good effort! Your fullstack implementation scored ${score}/${maxScore} (${percentage}%).${issueText} Ensure client-server data flow is consistent.`;
  }

  if (partialOrFailed.length > 0) {
    const topIssues = partialOrFailed.slice(0, 2).join('; ');
    return `Your fullstack project scored ${score}/${maxScore} (${percentage}%). Attention is needed on: ${topIssues}. Verify your Express route handlers, CORS middleware configuration, and ensure your React frontend properly handles asynchronous responses and error states.`;
  }

  if (failureContext) {
    return `Your fullstack project scored ${score}/${maxScore} (${percentage}%). Automated tests encountered errors during execution. Review your backend route endpoints, server startup logs, and frontend API integration to resolve test failures.`;
  }

  return `Your fullstack submission received a score of ${score}/${maxScore} (${percentage}%). Review the rubric breakdown above to identify areas for improvement in your client components, server routing, and database integration.`;
}
