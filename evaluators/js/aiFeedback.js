import OpenAI from "openai";
import logger from "../../config/logger.js";

let client = null;

function getClient() {
  if (client) return client;

  const apiKey = process.env.OPENAI_API_KEY || process.env.GROQ_API_KEY;
  if (!apiKey) {
    logger.warn("[JSFeedback] Neither OPENAI_API_KEY nor GROQ_API_KEY is set — AI feedback will be skipped.");
    return null;
  }

  client = new OpenAI({
    apiKey,
    baseURL: process.env.OPENAI_BASE_URL || 'https://api.deepseek.com',
  });

  logger.info("OpenAI/DeepSeek client initialised for JS Evaluator.");
  return client;
}

/**
 * Attempts to fetch student code from jobData memory or by downloading from GitHub.
 */
export async function fetchStudentCode(jobData) {
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
        'main/index.js',
        'main/script.js',
        'main/solution.js',
        'main/main.js',
        'main/app.js',
        'master/index.js',
        'master/script.js',
        'master/solution.js',
        'master/main.js',
        'master/app.js',
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
    logger.warn(`Failed to fetch student JS code: ${err.message}`);
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

class AsyncSemaphore {
  constructor(maxConcurrent = 2) {
    this.maxConcurrent = maxConcurrent;
    this.running = 0;
    this.queue = [];
  }

  async acquire() {
    if (this.running < this.maxConcurrent) {
      this.running++;
      return;
    }
    await new Promise(resolve => this.queue.push(resolve));
    this.running++;
  }

  release() {
    this.running--;
    if (this.queue.length > 0) {
      const next = this.queue.shift();
      next();
    }
  }

  async run(fn) {
    await this.acquire();
    try {
      return await fn();
    } finally {
      this.release();
    }
  }
}

const deepseekLimiter = new AsyncSemaphore(parseInt(process.env.DEEPSEEK_CONCURRENCY, 10) || 2);

async function callDeepSeekWithRetry(openai, params, maxRetries = 3) {
  return deepseekLimiter.run(async () => {
    let attempt = 0;
    while (attempt < maxRetries) {
      try {
        return await openai.chat.completions.create(params);
      } catch (err) {
        attempt++;
        if (attempt < maxRetries) {
          const delay = (attempt * 2000) + Math.floor(Math.random() * 1000);
          logger.warn(`DeepSeek API rate-limit/error (${err.message}). Retrying in ${delay}ms (attempt ${attempt}/${maxRetries})...`);
          await new Promise(r => setTimeout(r, delay));
        } else {
          throw err;
        }
      }
    }
  });
}

/**
 * Generates AI-assisted feedback for a student's JavaScript submission.
 *
 * @param {Object} jobData      - Raw BullMQ job data
 * @param {Object} githubResult - Output from grader { passed, score, feedback, error, details }
 * @param {number} finalScore   - Total score achieved (0-100)
 * @param {Array}  [breakdown]  - Optional rubric breakdown items
 * @param {Object} [rubric]     - Optional parsed rubric object
 * @returns {Promise<string>}   - Direct, actionable student feedback
 */
export async function generateJSAIFeedback(jobData, githubResult, finalScore, breakdown = [], rubric = null) {
  if (finalScore === 100) {
    return "Excellent work! All JavaScript test cases and requirements passed successfully with a perfect score. Your solution is clean, robust, and fully functional.";
  }

  const openai = getClient();
  if (!openai) {
    return buildJSFallback(finalScore, jobData?.evaluationMode, jobData?.entryFunction);
  }

  const studentCode = await fetchStudentCode(jobData);

  const evaluationMode = jobData?.evaluationMode || 'script';
  const entryFunction = jobData?.entryFunction || '';

  let requirements = "No specific requirements provided.";
  if (evaluationMode === "function") {
    requirements = `Function Mode: Required entry function '${entryFunction}'.\nTest Cases:\n${JSON.stringify(jobData?.testCases || [], null, 2)}`;
  } else {
    requirements = `Script Mode: Expected Console Logs:\n${JSON.stringify(jobData?.expectedLogs || [], null, 2)}`;
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
You are a senior JavaScript instructor and engineer reviewing a student's code submission.

## Assignment Details:
- Evaluation Mode: ${evaluationMode} (${evaluationMode === 'function' ? `Target Function: '${entryFunction}'` : 'Console Script Output'})
${requirements}

## Evaluation Results:
- Total Score: ${finalScore}/100
- Grader Execution Output & Test Failures:
${resultDetails}

${breakdownText ? `## Rubric Breakdown:\n${breakdownText}\n` : ''}

## Student's Code Excerpt:
\`\`\`javascript
${studentCode}
\`\`\`

## Instructions:
1. Write 2 to 3 concise, encouraging, and direct sentences explaining what the student implemented and exactly why test cases failed.
2. Directly reference specific lines of their code, function names, variables, loops, conditional checks, or output formatting that caused the failure.
3. Provide ONE clear, actionable suggestion on how to fix their logic (e.g. check return value vs console.log, fix array boundary, handle type coercion, or format expected strings).
4. CRITICAL: Never contradict the score. If the score is low (e.g., < 60) or tests failed, do NOT say "Great work, all tests passed" or "fully functional".
5. Return plain text only. Do NOT use markdown headers, bullet points, or quotes around the response.
`.trim();

  try {
    const response = await callDeepSeekWithRetry(openai, {
      model: process.env.OPENAI_MODEL || "deepseek-chat",
      messages: [{ role: "user", content: prompt }],
      max_tokens: 250,
      temperature: 0.0,
      seed: 42,
    });

    const feedback = response.choices[0]?.message?.content?.trim();
    return feedback || buildJSFallback(finalScore, evaluationMode, entryFunction, githubResult?.error);
  } catch (err) {
    logger.error(`[JSFeedback] OpenAI/DeepSeek call failed: ${err.message}`);
    return buildJSFallback(finalScore, evaluationMode, entryFunction, githubResult?.error);
  }
}

function buildJSFallback(finalScore, evaluationMode, entryFunction, error = null) {
  if (finalScore >= 90) {
    return "Great work! All test cases and requirements passed successfully.";
  }
  if (error) {
    return `Execution stopped due to error: ${typeof error === 'object' ? JSON.stringify(error) : error}. Please check your syntax, variable references, and unclosed statements.`;
  }
  const modeContext = evaluationMode === 'function' && entryFunction
    ? `for function '${entryFunction}'`
    : 'in your script output';
  return `Your submission scored ${finalScore}/100. Some automated test cases failed ${modeContext}. Please review the expected return values and console output format to ensure your implementation satisfies all test specifications.`;
}

/**
 * Evaluates a JavaScript submission against a rubric using AI code analysis,
 * returning the total score, itemized breakdown, strengths, issues, and executive summary
 * in a SINGLE unified DeepSeek LLM call.
 */
export async function evaluateJSRubric(studentCode, rubric, testResults, jobId = '', jobData = null) {
  if (!rubric || !Array.isArray(rubric.criteria) || rubric.criteria.length === 0) {
    return null;
  }

  // If studentCode is not provided or empty, attempt to fetch it directly from jobData
  if ((!studentCode || typeof studentCode !== 'string' || studentCode.trim().length < 10) && jobData) {
    try {
      const fetched = await fetchStudentCode(jobData);
      if (fetched && typeof fetched === 'string' && fetched.trim().length >= 10 && !fetched.startsWith("Code could not")) {
        studentCode = fetched;
      }
    } catch (e) {
      logger.warn(`Failed to fallback-fetch student code in evaluateJSRubric for job ${jobId}: ${e.message}`);
    }
  }

  const passRatio = (testResults?.totalCount > 0)
    ? (testResults.passedCount / testResults.totalCount)
    : (typeof testResults?.score === 'number' ? testResults.score / 100 : 0);

  // If student passed 100% of tests, award full credit for every criterion
  if ((passRatio >= 0.99 || testResults?.score === 100) && studentCode && studentCode.trim().length > 10) {
    const breakdown = rubric.criteria.map(c => ({
      item: c.name,
      awarded: c.weight,
      max: c.weight,
      reason: "Fully passed all automated tests and meets rubric criteria."
    }));
    return {
      finalScore: 100,
      breakdown,
      strengths: rubric.criteria.map(c => `[${c.name}] Excellent implementation. Passed all requirements (earned ${c.weight}/${c.weight} marks)`),
      issues: [],
      summary: "Excellent work! All JavaScript test cases and requirements passed successfully with a perfect score. Your solution is clean, robust, and fully functional."
    };
  }

  const openai = getClient();
  if (!openai) {
    return buildFallbackRubric(rubric, passRatio, studentCode);
  }

  try {
    const criteriaList = rubric.criteria
      .map((c, i) => `${i + 1}. "${c.name}" (weight: ${c.weight} points): ${c.description || "Evaluate code implementation."}`)
      .join("\n");

    const prompt = `You are a strict, fair, and encouraging JavaScript instructor grading a student's submission against an evaluation rubric.

## Student's Source Code:
\`\`\`javascript
${studentCode ? studentCode.slice(0, 4000) : "No source code available."}
\`\`\`

## Automated Test Results:
- Passed: ${testResults?.passedCount || 0} / ${testResults?.totalCount || 0} test cases (${Math.round(passRatio * 100)}%)
- Grader Feedback: ${testResults?.feedback || 'N/A'}

## Rubric Criteria to Evaluate:
${criteriaList}

## Grading Instructions:
1. Examine the student's source code against each rubric criterion independently:
   - For code-level criteria (e.g. declarations, data structures, algorithms, functions, logic, classes, syntax): Evaluate whether the required concepts and structures are present in their code. Award proportional marks (e.g., 0.8 to 1.0) based on what the student actually implemented in their source code, even if automated output tests failed or if other sections were incomplete.
   - For output or test-execution criteria: Evaluate based on the automated test results and console output matching.
2. Assign a discrete score multiplier for EACH criterion based on verified code evidence:
   - 1.0 = Fully implemented and correct in source code.
   - 0.9 = Mostly correct with only minor syntax or formatting gap.
   - 0.8 = Good implementation with minor omission.
   - 0.5 = Partial implementation (~50% of logic present).
   - 0.2 = Minimal effort or stub only.
   - 0.0 = Completely missing or not attempted.
3. Use ONLY discrete multipliers: 1.0, 0.9, 0.8, 0.5, 0.2, or 0.0.
4. For EACH criterion write a concise 1-sentence reasoning citing the specific code constructs, identifiers, or logic found in their submission.
5. Provide a 2 to 3 sentence concise, encouraging summary acknowledging what the student correctly implemented and what remains to be completed.

Output STRICTLY a JSON object with this shape:
{
  "summary": "<2-3 sentence executive feedback summary for the student>",
  "scores": [
    { "name": "<exact criterion name>", "multiplier": <number 0.0, 0.2, 0.5, 0.8, 0.9, or 1.0>, "reasoning": "<1 sentence concise explanation>" }
  ]
}`;

    const response = await callDeepSeekWithRetry(openai, {
      model: process.env.OPENAI_MODEL || "deepseek-chat",
      messages: [{ role: "user", content: prompt }],
      max_tokens: 2000,
      temperature: 0.0,
      seed: 42,
      response_format: { type: "json_object" },
    });

    let raw = response.choices[0]?.message?.content?.trim() || "";
    if (raw.startsWith("```")) {
      raw = raw.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
    }
    raw = raw.replace(/<think>[\s\S]*?<\/think>/gi, "").trim();

    const parsed = JSON.parse(raw);
    const scoreList = Array.isArray(parsed) ? parsed : (parsed.scores || parsed.criteria || []);
    const aiSummary = typeof parsed.summary === 'string' && parsed.summary.trim() ? parsed.summary.trim() : null;

    const breakdown = [];
    const strengths = [];
    const issues = [];
    let totalScore = 0;

    const cleanStr = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

    for (const c of rubric.criteria) {
      const match = scoreList.find(s => cleanStr(s.name) === cleanStr(c.name) || cleanStr(s.name).includes(cleanStr(c.name)) || cleanStr(c.name).includes(cleanStr(s.name)));
      let multiplier = typeof match?.multiplier === 'number' ? Math.max(0, Math.min(1, match.multiplier)) : passRatio;
      let reason = match?.reasoning || `Evaluated against code implementation.`;
      
      const maxPts = typeof c.weight === 'number' ? c.weight : 0;
      const awarded = Math.round(maxPts * multiplier);
      totalScore += awarded;
      
      breakdown.push({
        item: c.name,
        awarded,
        max: maxPts,
        reason
      });

      if (multiplier >= 0.8) {
        strengths.push(`[${c.name}] ${reason} (earned ${awarded}/${maxPts} marks)`);
      } else {
        issues.push(`[${c.name}] ${reason} (earned ${awarded}/${maxPts} marks)`);
      }
    }

    const finalSummary = aiSummary || (
      totalScore >= 80
        ? `Great job! Your JavaScript implementation scored ${totalScore}/100 and meets the rubric requirements well.`
        : `Your submission scored ${totalScore}/100. Review the detailed rubric breakdown and test cases to address any incomplete logic.`
    );

    return {
      finalScore: totalScore,
      breakdown,
      strengths,
      issues,
      summary: finalSummary
    };
  } catch (err) {
    logger.warn(`AI Rubric evaluation failed for Job ${jobId}: ${err.message}. Using structured fallback.`);
    return buildFallbackRubric(rubric, passRatio, studentCode);
  }
}

function analyzeCodeFeatures(code) {
  if (!code || typeof code !== 'string') return { codeLength: 0 };
  const c = code;
  return {
    codeLength: c.trim().length,
    hasVariables: /(?:let|const|var)\s+[a-zA-Z_$][a-zA-Z0-9_$]*/.test(c),
    hasVariableTypes: /(?:typeof\s+|(['"`].*?['"`]|\b\d+(?:\.\d+)?\b|\b(?:true|false)\b))/.test(c),
    hasArithmetic: /[+\-*/%]/.test(c),
    hasComparison: /(?:===|!==|==|!=|[><]=?)/.test(c),
    hasConditionals: /\b(?:if|else|switch|case)\b|\?.*?:/.test(c),
    hasLoops: /\b(?:for|while|do)\b/.test(c),
    hasFunctions: /\b(?:function\b|\b=>\b)/.test(c),
    hasArrays: /\[[\s\S]*?\]|\.(?:push|pop|shift|unshift|map|filter|forEach|reduce|slice|splice|indexOf|includes)\b/.test(c),
    hasObjects: /\{[\s\S]*?\}|\bObject\./.test(c),
    hasConsoleLog: /console\.(?:log|info|warn|error)\s*\(/.test(c)
  };
}

function buildFallbackRubric(rubric, passRatio, studentCode) {
  const features = analyzeCodeFeatures(studentCode);
  const hasCode = features.codeLength > 20;
  const breakdown = [];
  const strengths = [];
  const issues = [];
  let totalScore = 0;

  for (const c of rubric.criteria) {
    const maxPts = typeof c.weight === 'number' ? c.weight : 0;
    const nameLower = (c.name || '').toLowerCase();
    const descLower = (c.description || '').toLowerCase();
    const text = nameLower + ' ' + descLower;

    const isOutputCriterion = text.includes('output') || text.includes('test') || text.includes('result') || text.includes('expected');

    let mult = passRatio;
    let rationale = '';

    if (isOutputCriterion) {
      if (features.hasConsoleLog && passRatio === 0) {
        mult = 0.5;
        rationale = 'Console output statements exist in code, but output values differed from test specification.';
      } else {
        mult = passRatio;
        rationale = `Passed ${Math.round(passRatio * 100)}% of output test specifications.`;
      }
    } else if (hasCode) {
      if (text.includes('variable') || text.includes('declaration')) {
        mult = features.hasVariables ? 1.0 : (features.hasVariableTypes ? 0.8 : 0.5);
        rationale = features.hasVariables ? 'Variable declarations verified in code.' : 'Partial variable usage detected.';
      } else if (text.includes('data type') || text.includes('type') || text.includes('primitive')) {
        mult = features.hasVariableTypes ? 1.0 : 0.8;
        rationale = 'Data types and literal values verified in code.';
      } else if (text.includes('operator') || text.includes('arithmetic') || text.includes('calculation')) {
        mult = features.hasArithmetic ? 1.0 : (features.hasComparison ? 0.8 : 0.5);
        rationale = features.hasArithmetic ? 'Arithmetic operations implemented in code.' : 'Basic operators detected.';
      } else if (text.includes('condition') || text.includes('if') || text.includes('branch')) {
        mult = features.hasConditionals ? 1.0 : 0.5;
        rationale = features.hasConditionals ? 'Conditional branching logic verified.' : 'Partial conditional structure.';
      } else if (text.includes('loop') || text.includes('iteration')) {
        mult = features.hasLoops ? 1.0 : 0.5;
        rationale = features.hasLoops ? 'Loop constructs verified in code.' : 'Partial loop structure.';
      } else if (text.includes('function') || text.includes('method')) {
        mult = features.hasFunctions ? 1.0 : 0.5;
        rationale = features.hasFunctions ? 'Function declarations and return logic verified.' : 'Function structure needs improvement.';
      } else if (text.includes('array')) {
        mult = features.hasArrays ? 1.0 : 0.5;
        rationale = features.hasArrays ? 'Array operations verified in code.' : 'Partial array usage.';
      } else if (text.includes('object')) {
        mult = features.hasObjects ? 1.0 : 0.5;
        rationale = features.hasObjects ? 'Object properties verified in code.' : 'Partial object usage.';
      } else {
        mult = Math.max(passRatio, 0.8);
        rationale = 'Code structure satisfies core assignment requirements.';
      }
    } else {
      mult = passRatio;
      rationale = `Evaluated based on automated tests (${Math.round(passRatio * 100)}%).`;
    }

    const awarded = Math.round(maxPts * mult);
    totalScore += awarded;
    const reason = rationale || `Passed ${Math.round(mult * 100)}% of requirements for this criterion.`;

    breakdown.push({ item: c.name, awarded, max: maxPts, reason });
    if (mult >= 0.8) {
      strengths.push(`[${c.name}] ${reason} (earned ${awarded}/${maxPts} marks)`);
    } else {
      issues.push(`[${c.name}] ${reason} (earned ${awarded}/${maxPts} marks)`);
    }
  }

  const summary = totalScore >= 80
    ? `Great job! Your submission scored ${totalScore}/100. Core code constructs and concepts were verified in your implementation.`
    : `Your submission scored ${totalScore}/100. Key code constructs were identified in your solution, but some requirements or test cases did not pass. Check the breakdown for specific areas to improve.`;

  return { finalScore: totalScore, breakdown, strengths, issues, summary };
}

