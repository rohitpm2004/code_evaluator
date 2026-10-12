import OpenAI from 'openai';
import dotenv from 'dotenv';
import logger from '../../config/logger.js';

dotenv.config();

let _client;
function getClient() {
  if (!_client) {
    const apiKey = process.env.OPENAI_API_KEY || process.env.GROQ_API_KEY;
    if (!apiKey) {
      logger.warn('[BackendFeedback] Neither OPENAI_API_KEY nor GROQ_API_KEY is set — AI feedback will be skipped.');
      return null;
    }
    const baseURL = process.env.OPENAI_BASE_URL || 'https://api.deepseek.com';
    _client = new OpenAI({
      apiKey,
      baseURL,
    });
  }
  return _client;
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
        logger.warn(`[BackendFeedback] AI API rate-limit/error (${err.message}). Retrying in ${delay}ms (attempt ${attempt}/${maxRetries})...`);
        await new Promise(r => setTimeout(r, delay));
      } else {
        throw err;
      }
    }
  }
}

/**
 * Generates AI-assisted feedback specifically tailored for Node.js & Express Backend submissions.
 *
 * @param {Object} params
 * @param {Object} params.rubric_breakdown  - { criteriaName: score, ... }
 * @param {Array}  params.rubric_criteria   - [ { name, weight, description }, ... ]
 * @param {Object} params.per_criterion_reasons - { criteriaName: reasonString, ... }
 * @param {number} params.score             - Total score achieved
 * @param {string[]} params.warnings        - Warning messages from scorer
 * @param {string} params.execution_logs    - Raw build/test/execution logs
 * @param {string} [params.assignmentType]  - 'Node.js & Express Backend'
 * @param {string} [params.codeSnippet]     - Student's backend source code
 *
 * @returns {Promise<string>} feedback - A concise, instructional review grounded in backend architecture
 */
export async function generateBackendAIFeedback({
  rubric_breakdown = {},
  rubric_criteria = [],
  per_criterion_reasons = {},
  score = 0,
  warnings = [],
  execution_logs = '',
  assignmentType = 'Node.js & Express Backend',
  codeSnippet = '',
}) {
  const maxScore = rubric_criteria.length > 0
    ? rubric_criteria.reduce((sum, c) => sum + (c.weight || 0), 0)
    : 100;

  const percentage = maxScore > 0 ? Math.round((score / maxScore) * 100) : score;

  // If 100% and zero warnings/failures, return clean high-achievement praise
  if (percentage === 100 && (!warnings || warnings.length === 0)) {
    return `Excellent work! All backend API endpoints, database operations, and middleware passed all tests with a perfect score. Your application architecture is robust, clean, and production-ready.`;
  }

  const openai = getClient();
  if (!openai) {
    return buildBackendFallback(rubric_breakdown, rubric_criteria, per_criterion_reasons, score, maxScore, warnings);
  }

  const criteriaDetails = rubric_criteria.map(c => {
    const awarded = rubric_breakdown[c.name] ?? 0;
    const reason = per_criterion_reasons[c.name] || '';
    const status = awarded >= c.weight ? 'PASSED' : (awarded === 0 ? 'FAILED' : 'PARTIALLY PASSED');
    return `- [${status}] "${c.name}" (Awarded: ${awarded}/${c.weight} pts): ${c.description || ''}${reason ? ` | Evaluation Findings: ${reason}` : ''}`;
  }).join('\n');

  const truncatedCode = codeSnippet ? codeSnippet.slice(0, 3000) : '';
  const truncatedLogs = execution_logs ? execution_logs.slice(0, 1500) : '';

  const prompt = `
You are a senior backend engineering lead and instructor conducting a code review on a student's ${assignmentType} project.

## Student Result Summary:
- Total Score: ${score}/${maxScore} (${percentage}%)
- Rubric Breakdown & Test Findings:
${criteriaDetails || 'No specific criteria breakdown available.'}

${warnings && warnings.length > 0 ? `## Execution Warnings:\n${warnings.join('\n')}\n` : ''}
${truncatedLogs ? `## Test / Execution Logs:\n${truncatedLogs}\n` : ''}
${truncatedCode ? `## Student Backend Source Code:\n\`\`\`javascript\n${truncatedCode}\n\`\`\`\n` : ''}

## Instructions:
1. Write 2 to 4 concise, informative, and encouraging sentences directly addressing the student's backend code and results.
2. Directly reference specific backend elements that worked or failed (e.g., Express route paths, HTTP status codes like 400/404/500, middleware, database CRUD queries, authentication, request validation).
3. If the score is low or criteria failed, diagnose the technical root cause and provide ONE clear, actionable suggestion on how to fix it.
4. CRITICAL: Never contradict the score. If the student received a low score (e.g. < 60) or failed criteria, do NOT say "Great work" or "fully functional".
5. Use strictly backend terminology. Do NOT mention React components, JSX, props, or browser DOM manipulation.
6. Return plain text only. No markdown headers, bullet points, or enclosing quotes.
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
    return feedback || buildBackendFallback(rubric_breakdown, rubric_criteria, per_criterion_reasons, score, maxScore, warnings);
  } catch (err) {
    logger.error(`[BackendFeedback] LLM feedback generation failed: ${err.message}`);
    return buildBackendFallback(rubric_breakdown, rubric_criteria, per_criterion_reasons, score, maxScore, warnings);
  }
}

/**
 * Rule-based fallback feedback when AI is unreachable.
 */
function buildBackendFallback(
  rubric_breakdown = {},
  rubric_criteria = [],
  per_criterion_reasons = {},
  score = 0,
  maxScore = 100,
  warnings = []
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

  if (percentage >= 90 && partialOrFailed.length === 0) {
    return 'Great work! All criteria passed successfully. Your Node.js & Express backend application is well-structured and fully functional.';
  }

  const passedStr = fullyPassed.length > 0
    ? `You successfully implemented: ${fullyPassed.join(', ')}. `
    : '';

  const issuesStr = partialOrFailed.length > 0
    ? `The following areas need attention: ${partialOrFailed.join(', ')}. `
    : '';

  const tip = 'Review the failing criteria and ensure your API routes, request validation, middleware, and database operations handle error states properly.';

  return `${passedStr}${issuesStr}${tip}`.trim();
}

/**
 * Backwards-compatible helper for testDetails array input.
 */
export default async function getAiFeedback(testDetails, rubric) {
  if (!Array.isArray(testDetails) || testDetails.length === 0) {
    return "No tests could be run for this submission, so there's no pass/fail signal to give feedback on — see the warnings above for why.";
  }

  const failures = testDetails.filter(t => t.status === 'fail');
  if (failures.length === 0) {
    return "Amazing work! Your implementation matches the requirements perfectly. No major technical improvements needed—keep maintaining this standard of excellence!";
  }

  const failureContext = failures.map(f => `
Test Name: ${f.name}
Error: ${f.error?.slice(0, 300) || 'Unknown error'}
  `).join('\n');

  const prompt = `
You are an encouraging Senior Backend Developer performing a code review for a student.
Below are the results of an automated test suite. Some tests failed.

### Failed Tests:
${failureContext}

### Rubric:
${(rubric?.criteria || []).map(c => `- ${c.name} (weight: ${c.weight})`).join('\n')}

### Instructions:
1. Provide a brief (max 3-4 sentences) technical advice to the student.
2. Maintain an encouraging but professional "Senior Developer" tone.
3. Be specific about the potential root cause (e.g., missing middleware, bad validation, disk vs DB).
4. Do not just say "check the logs"; give them a hint on *how* to fix it.
5. Do not include any PII or sensitive system data.

Your response:
`.trim();

  const openai = getClient();
  if (!openai) {
    return "Evaluation completed. Please review the errors and warnings above to improve the robustness of your API.";
  }

  try {
    const chatCompletion = await openai.chat.completions.create({
      messages: [{ role: 'user', content: prompt }],
      model: process.env.OPENAI_MODEL || 'deepseek-chat',
      max_tokens: 300,
      temperature: 0.0,
      seed: 42,
    });

    return chatCompletion.choices[0]?.message?.content?.trim() || "Evaluation completed. Some tests failed.";
  } catch (error) {
    logger.error('Error fetching AI feedback:', error.message);
    return "We couldn't generate specific AI advice at this moment, but please check the detailed test logs above for clues.";
  }
}