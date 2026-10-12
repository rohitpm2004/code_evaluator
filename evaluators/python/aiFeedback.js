import OpenAI from "openai";
import logger from "../../config/logger.js";

let client = null;

function getClient() {
  if (client) return client;

  const apiKey = process.env.OPENAI_API_KEY || process.env.GROQ_API_KEY;
  if (!apiKey) {
    logger.warn("[PythonFeedback] Neither OPENAI_API_KEY nor GROQ_API_KEY is set — AI feedback will be skipped.");
    return null;
  }

  client = new OpenAI({
    apiKey,
    baseURL: process.env.OPENAI_BASE_URL || 'https://api.deepseek.com',
  });

  logger.info("OpenAI/DeepSeek client initialised for Python Evaluator.");
  return client;
}

/**
 * Attempts to fetch student code from jobData memory or by downloading from GitHub.
 */
async function fetchStudentCode(jobData) {
  // 1. Direct in-memory code
  const directCode =
    jobData?.submission?.code ||
    jobData?.submission?.source_code ||
    jobData?.submission?.codeSnippet ||
    jobData?.code;

  if (typeof directCode === 'string' && directCode.trim().length > 0) {
    return directCode.slice(0, 3000);
  }

  const repoUrl = jobData?.submission?.repoUrl || jobData?.submission?.submission_link;
  if (!repoUrl) return "No repository URL or code provided.";

  try {
    let cleanUrl = repoUrl.trim().replace(/\.git\/?$/, '').replace(/\/+$/, '');

    // Case A: Direct file URL (blob or raw)
    if (cleanUrl.includes("github.com") && (cleanUrl.includes("/blob/") || cleanUrl.includes("/raw/"))) {
      const rawUrl = cleanUrl
        .replace("github.com", "raw.githubusercontent.com")
        .replace("/blob/", "/");
      const res = await fetchWithTimeout(rawUrl);
      if (res) return res.slice(0, 3000);
    }

    // Case B: Repo root URL (e.g. https://github.com/user/repo)
    const match = cleanUrl.match(/github\.com\/([^\/]+)\/([^\/]+)/);
    if (match) {
      const [, owner, repo] = match;
      const candidates = [
        'main/solution.py',
        'main/main.py',
        'main/app.py',
        'main/index.py',
        'master/solution.py',
        'master/main.py',
        'master/app.py',
        'master/index.py',
      ];

      for (const candidate of candidates) {
        const rawUrl = `https://raw.githubusercontent.com/${owner}/${repo}/${candidate}`;
        const content = await fetchWithTimeout(rawUrl);
        if (content && !content.startsWith('<!DOCTYPE') && !content.startsWith('<html')) {
          return content.slice(0, 3000);
        }
      }
    }

    return "Code could not be automatically downloaded from repository.";
  } catch (err) {
    logger.warn(`Failed to fetch student Python code: ${err.message}`);
    return "Code could not be automatically fetched.";
  }
}

async function fetchWithTimeout(url, timeoutMs = 25000) {
  try {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), timeoutMs);
    const response = await fetch(url, { signal: controller.signal });
    clearTimeout(timeoutId);
    if (response.ok) {
      return await response.text();
    }
  } catch {
    // Ignore fetch errors
  }
  return null;
}

/**
 * Generates AI-assisted feedback for a student's Python submission.
 *
 * @param {Object} jobData      - Raw BullMQ job data
 * @param {Object} githubResult - Output from grader { passed, score, feedback, error, details }
 * @param {number} finalScore   - Total score achieved (0-100)
 * @param {Array}  [breakdown]  - Optional rubric breakdown items
 * @param {Object} [rubric]     - Optional parsed rubric object
 * @returns {Promise<string>}   - Direct, actionable student feedback
 */
export async function generatePythonAIFeedback(jobData, githubResult, finalScore, breakdown = [], rubric = null) {
  if (finalScore === 100) {
    return "Excellent work! All Python test cases and requirements passed successfully with a perfect score. Your code is clean, idiomatic, and fully functional.";
  }

  const openai = getClient();
  if (!openai) {
    return buildPythonFallback(finalScore, jobData?.evaluationMode, jobData?.entryFunction);
  }

  const studentCode = await fetchStudentCode(jobData);

  const evaluationMode = jobData?.evaluationMode || 'script';
  const entryFunction = jobData?.entryFunction || '';

  let requirements = "No specific requirements provided.";
  if (evaluationMode === "function") {
    requirements = `Function Mode: Required entry function '${entryFunction}'.\nTest Cases:\n${JSON.stringify(jobData?.testCases || [], null, 2)}`;
  } else {
    requirements = `Script Mode: Expected Console Print Outputs:\n${JSON.stringify(jobData?.expectedLogs || [], null, 2)}`;
  }

  // Format execution result and error details
  let resultDetails = "";
  if (typeof githubResult?.feedback === 'string' && githubResult.feedback.trim()) {
    resultDetails += `Grader Feedback: ${githubResult.feedback}\n`;
  }
  if (githubResult?.error) {
    resultDetails += `Runtime Error: ${typeof githubResult.error === 'string' ? githubResult.error : JSON.stringify(githubResult.error)}\n`;
  }
  if (githubResult?.details) {
    resultDetails += `Test Details: ${JSON.stringify(githubResult.details, null, 2).slice(0, 1000)}\n`;
  }
  if (!resultDetails) {
    resultDetails = JSON.stringify(githubResult, null, 2).slice(0, 1000);
  }

  let breakdownText = "";
  if (Array.isArray(breakdown) && breakdown.length > 0) {
    breakdownText = breakdown.map(b => `- [${b.item || b.name}]: Awarded ${b.awarded}/${b.max} pts (${b.reason || ''})`).join('\n');
  }

  const prompt = `
You are a senior Python instructor and software engineer reviewing a student's code submission.

## Assignment Details:
- Evaluation Mode: ${evaluationMode} (${evaluationMode === 'function' ? `Target Function: '${entryFunction}'` : 'Console Print Output'})
${requirements}

## Evaluation Results:
- Total Score: ${finalScore}/100
- Grader Execution Output & Test Failures:
${resultDetails}

${breakdownText ? `## Rubric Breakdown:\n${breakdownText}\n` : ''}

## Student's Python Code Excerpt:
\`\`\`python
${studentCode}
\`\`\`

## Instructions:
1. Write 2 to 3 concise, encouraging, and direct sentences explaining what the student implemented and exactly why test cases failed.
2. Directly reference specific lines of their Python code, function names, variables, loops, conditional checks, indentation, or print formatting that caused the failure.
3. Provide ONE clear, actionable suggestion on how to fix their Python logic (e.g. check return value vs print, handle type casting like int(x), fix off-by-one range index, handle edge cases).
4. CRITICAL: Never contradict the score. If the score is low (e.g., < 60) or tests failed, do NOT say "Great work, all tests passed" or "fully functional".
5. Return plain text only. Do NOT use markdown headers, bullet points, or quotes around the response.
`.trim();

  try {
    const response = await openai.chat.completions.create({
      model: process.env.OPENAI_MODEL || "deepseek-chat",
      messages: [{ role: "user", content: prompt }],
      max_tokens: 250,
      temperature: 0.0,
      seed: 42,
    });

    const feedback = response.choices[0]?.message?.content?.trim();
    return feedback || buildPythonFallback(finalScore, evaluationMode, entryFunction);
  } catch (err) {
    logger.error(`[PythonFeedback] OpenAI/DeepSeek call failed: ${err.message}`);
    return buildPythonFallback(finalScore, evaluationMode, entryFunction);
  }
}

function buildPythonFallback(finalScore, evaluationMode, entryFunction) {
  if (finalScore >= 90) {
    return "Great work! All Python test cases and requirements passed successfully.";
  }
  const modeContext = evaluationMode === 'function' && entryFunction
    ? `for function '${entryFunction}'`
    : 'in your script output';
  return `Your submission scored ${finalScore}/100. Some automated test cases failed ${modeContext}. Please review your return statements, variable types, and expected print output format to ensure your implementation satisfies all test specifications.`;
}
