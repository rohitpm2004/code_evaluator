import OpenAI from "openai";
import dotenv from "dotenv";
import logger from "../../config/logger.js";

dotenv.config();

let client = null;

function getClient() {
  if (client) return client;

  const apiKey = process.env.OPENAI_API_KEY;
  if (!apiKey) {
    logger.warn("[VisualFeedback] OPENAI_API_KEY not set — AI feedback will be skipped.");
    return null;
  }

  client = new OpenAI({
    apiKey,
    baseURL: process.env.OPENAI_BASE_URL || 'https://api.deepseek.com',
  });

  return client;
}

/**
 * Generates AI-assisted feedback for a student's Visual (HTML/CSS/JS DOM) project.
 *
 * @param {Object} params
 * @param {Object} params.rubric_breakdown   - { criteriaName: score, ... }
 * @param {Array}  params.rubric_criteria    - [ { name, weight, description }, ... ]
 * @param {Object} params.per_criterion_reasons - { criteriaName: reasonString, ... }
 * @param {number} params.score              - Total score achieved
 * @param {number} [params.maxScore]         - Max possible score
 * @param {string[]} [params.warnings]       - Warning messages
 * @param {string} [params.execution_logs]   - Syntax / build / action logs
 * @param {string} [params.codeSnippet]      - Compressed HTML/CSS/JS source code
 *
 * @returns {Promise<string>} feedback string
 */
export async function generateVisualAIFeedback({
  rubric_breakdown = {},
  rubric_criteria = [],
  per_criterion_reasons = {},
  score = 0,
  maxScore = null,
  warnings = [],
  execution_logs = '',
  codeSnippet = '',
}) {
  const calculatedMax = (rubric_criteria && rubric_criteria.length > 0)
    ? rubric_criteria.reduce((sum, c) => sum + (c.weight || 0), 0)
    : (maxScore || 100);

  const finalMax = calculatedMax > 0 ? calculatedMax : 100;
  const percentage = Math.round((score / finalMax) * 100);

  // 1. If 100% and zero warnings/failures, return clean high-achievement praise
  if (percentage === 100 && (!warnings || warnings.length === 0)) {
    return "Excellent work! All visual and functional criteria passed successfully with a perfect score. Your HTML semantic structure is clean, CSS styling is responsive and well-organized, and your JavaScript DOM manipulation functions flawlessly.";
  }

  const openai = getClient();
  if (!openai) {
    return buildFallbackFeedback(rubric_breakdown, rubric_criteria, per_criterion_reasons, score, finalMax, warnings);
  }

  // 2. Build detailed technical breakdown for prompt
  const criteriaDetails = (rubric_criteria || []).map(c => {
    const awarded = rubric_breakdown[c.name] ?? 0;
    const reason = per_criterion_reasons[c.name] || '';
    const status = awarded >= c.weight ? 'PASSED' : (awarded === 0 ? 'FAILED' : 'PARTIALLY PASSED');
    return `- [${status}] "${c.name}" (Awarded: ${awarded}/${c.weight} pts): ${c.description || ''}${reason ? ` | Evaluation Finding: ${reason}` : ''}`;
  }).join('\n');

  // Truncate code snippet and logs to conserve tokens while preserving technical details
  const truncatedCode = codeSnippet ? codeSnippet.slice(0, 3000) : '';
  const truncatedLogs = execution_logs ? execution_logs.slice(0, 1500) : '';

  const prompt = `
You are a senior frontend instructor evaluating a student's Vanilla Web project (HTML5, CSS3, and JavaScript DOM manipulation).

## Student Result Summary:
- Assignment Type: HTML, CSS & JavaScript DOM
- Total Score: ${score}/${finalMax} (${percentage}%)
- Rubric Breakdown & Evaluation Findings:
${criteriaDetails || 'No specific criteria breakdown available.'}

${warnings && warnings.length > 0 ? `## Execution Warnings:\n${warnings.join('\n')}\n` : ''}
${truncatedLogs ? `## Syntax / Linter Report:\n${truncatedLogs}\n` : ''}
${truncatedCode ? `## Student Code Excerpt:\n\`\`\`\n${truncatedCode}\n\`\`\`\n` : ''}

## Instructions:
1. Write 2 to 4 concise, informative, and encouraging sentences directly addressing the student's actual results.
2. Directly reference specific HTML tags/elements, CSS selectors/layouts (Flexbox, Grid, media queries), and JavaScript DOM methods (e.g. querySelector, addEventListener, innerText/innerHTML, classList) that worked or caused failures.
3. Pay special attention to whether HTML properly links to CSS (<link rel="stylesheet">) and JS (<script src="...">), and whether event handlers actually trigger state changes.
4. If the score is low (< 60%) or there are failing criteria, explain the root cause and provide ONE actionable, concrete suggestion to fix it.
5. CRITICAL: Never contradict the score. If the student received a low score or failed criteria, do NOT say "Great work! All criteria passed successfully" or "fully functional".
6. Return plain text only. Do NOT use markdown headers, bullet points, or quotation marks around the entire response.
`.trim();

  try {
    const response = await openai.chat.completions.create({
      model: process.env.OPENAI_MODEL || "deepseek-chat",
      messages: [{ role: "user", content: prompt }],
      max_tokens: 300,
      temperature: 0.0,
      seed: 42,
    });

    const feedback = response.choices[0]?.message?.content?.trim();
    return feedback || buildFallbackFeedback(rubric_breakdown, rubric_criteria, per_criterion_reasons, score, finalMax, warnings);
  } catch (err) {
    logger.error(`[VisualFeedback] LLM feedback generation failed: ${err.message}`);
    return buildFallbackFeedback(rubric_breakdown, rubric_criteria, per_criterion_reasons, score, finalMax, warnings);
  }
}

/**
 * Score-aware fallback feedback when AI is unavailable or fails.
 */
function buildFallbackFeedback(
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
        const reason = per_criterion_reasons[c.name] ? ` (${per_criterion_reasons[c.name]})` : '';
        partialOrFailed.push(`"${c.name}" [${awarded}/${c.weight} pts]${reason}`);
      }
    }
  }

  if (percentage >= 90 && partialOrFailed.length === 0) {
    return `Excellent work! All HTML, CSS, and DOM criteria passed with a score of ${score}/${maxScore} (${percentage}%). Your page layout and interactivity are well-implemented.`;
  }

  if (percentage >= 70 && partialOrFailed.length <= 1) {
    const issueText = partialOrFailed.length > 0 ? ` Review ${partialOrFailed[0]} for minor improvements.` : '';
    return `Good job! Your HTML, CSS, and DOM implementation scored ${score}/${maxScore} (${percentage}%).${issueText}`;
  }

  if (partialOrFailed.length > 0) {
    const topIssues = partialOrFailed.slice(0, 2).join('; ');
    return `Your submission earned ${score}/${maxScore} (${percentage}%). Attention is needed on: ${topIssues}. Verify that your HTML semantic tags are properly structured, external CSS and JS files are linked correctly in <head> and <body>, and DOM event listeners are registered properly.`;
  }

  return `Your submission received a score of ${score}/${maxScore} (${percentage}%). Review the rubric breakdown above to identify areas for improvement in your HTML structure, CSS styling, and JavaScript interactivity.`;
}
