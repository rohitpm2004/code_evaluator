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
export async function fetchStudentCode(jobData) {
  // 1. Direct in-memory code
  const directCode =
    jobData?.submission?.code ||
    jobData?.submission?.source_code ||
    jobData?.submission?.codeSnippet ||
    jobData?.code;

  if (typeof directCode === 'string' && directCode.trim().length > 0) {
    return directCode.slice(0, 4000);
  }

  // 1b. Direct ideFiles
  if (jobData?.submission?.ideFiles && Array.isArray(jobData.submission.ideFiles)) {
    const mainFile = jobData.submission.ideFiles.find(f => f.path && (f.path.endsWith('.py') || f.path === 'main.py'));
    if (mainFile && typeof mainFile.content === 'string' && mainFile.content.trim().length > 0) {
      return mainFile.content.slice(0, 4000);
    }
  }

  const repoUrl = jobData?.submission?.repoUrl || jobData?.submission?.submission_link || jobData?.submission?.fileUrl || jobData?.fileUrl;
  if (!repoUrl) return "No repository URL or code provided.";

  try {
    let cleanUrl = repoUrl.trim().replace(/\.git\/?$/, '').replace(/\/+$/, '');

    // Case A: Direct file URL (blob or raw)
    if (cleanUrl.includes("github.com") && (cleanUrl.includes("/blob/") || cleanUrl.includes("/raw/"))) {
      const rawUrl = cleanUrl
        .replace("github.com", "raw.githubusercontent.com")
        .replace("/blob/", "/");
      const res = await fetchWithTimeout(rawUrl);
      if (res) return res.slice(0, 4000);
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
          return content.slice(0, 4000);
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

class AsyncSemaphore {
  constructor(max) {
    this.max = max;
    this.running = 0;
    this.queue = [];
  }

  async acquire() {
    if (this.running < this.max) {
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

function analyzePythonFeatures(code) {
  if (!code || typeof code !== 'string') return { codeLength: 0 };
  const c = code;
  return {
    codeLength: c.trim().length,
    hasFunctions: /\bdef\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\(/.test(c),
    hasClasses: /\bclass\s+[a-zA-Z_][a-zA-Z0-9_]*/.test(c),
    hasLoops: /\b(?:for|while)\b/.test(c),
    hasConditionals: /\b(?:if|elif|else)\b/.test(c),
    hasPrint: /\bprint\s*\(/.test(c),
    hasInput: /\binput\s*\(/.test(c),
    hasReturn: /\breturn\b/.test(c),
    hasLists: /\[[\s\S]*?\]|\.(?:append|extend|pop|insert|remove)\s*\(/.test(c),
    hasDicts: /\{[\s\S]*?:[\s\S]*?\}|\.get\s*\(|\.keys\s*\(|\.values\s*\(/.test(c),
    hasListComp: /\[\s*.+?\s+for\s+.+?\s+in\s+.+?\]/.test(c),
    hasImports: /\b(?:import\s+[a-zA-Z_]|from\s+[a-zA-Z_].+?import)/.test(c)
  };
}

function buildFallbackRubric(rubric, passRatio, studentCode) {
  const feat = analyzePythonFeatures(studentCode);
  const breakdown = [];
  const strengths = [];
  const issues = [];
  let totalScore = 0;

  for (const c of rubric.criteria) {
    const maxPts = typeof c.weight === 'number' ? c.weight : 0;
    const cName = (c.name || '').toLowerCase();

    let multiplier = passRatio;
    // Check if code features support partial credit even if tests failed
    if (feat.codeLength > 20) {
      if ((cName.includes('syntax') || cName.includes('structure')) && (feat.hasFunctions || feat.codeLength > 20)) {
        multiplier = Math.max(multiplier, 0.85);
      } else if ((cName.includes('algorithm') || cName.includes('logic')) && (feat.hasConditionals || feat.hasLoops || passRatio >= 0.5)) {
        multiplier = Math.max(multiplier, 0.8);
      } else if ((cName.includes('function') || cName.includes('def')) && feat.hasFunctions) {
        multiplier = Math.max(multiplier, 0.85);
      } else if ((cName.includes('loop') || cName.includes('iteration')) && feat.hasLoops) {
        multiplier = Math.max(multiplier, 0.8);
      } else if ((cName.includes('condition') || cName.includes('if')) && feat.hasConditionals) {
        multiplier = Math.max(multiplier, 0.8);
      } else if ((cName.includes('print') || cName.includes('output')) && feat.hasPrint) {
        multiplier = Math.max(multiplier, 0.8);
      } else if ((cName.includes('input') || cName.includes('prompt')) && feat.hasInput) {
        multiplier = Math.max(multiplier, 0.8);
      } else if ((cName.includes('list') || cName.includes('array')) && feat.hasLists) {
        multiplier = Math.max(multiplier, 0.8);
      } else if ((cName.includes('dict') || cName.includes('dictionary') || cName.includes('hash')) && feat.hasDicts) {
        multiplier = Math.max(multiplier, 0.8);
      } else {
        multiplier = Math.max(multiplier, passRatio >= 0.5 ? 0.65 : 0.4);
      }
    }

    const awarded = Math.round(maxPts * multiplier);
    totalScore += awarded;
    const reason = multiplier >= 0.75
      ? `Implemented core Python logic corresponding to this criterion.`
      : `Partial implementation identified; verify test cases and output specifications.`;

    breakdown.push({
      item: c.name,
      awarded,
      max: maxPts,
      reason
    });

    if (multiplier >= 0.7) {
      strengths.push(`[${c.name}] Good implementation. Met criteria specifications (earned ${awarded}/${maxPts} marks).`);
    } else {
      issues.push(`[${c.name}] Incomplete implementation or test failure (earned ${awarded}/${maxPts} marks).`);
    }
  }

  // Ensure strengths & issues are populated appropriately
  if (strengths.length === 0 && totalScore > 0) {
    strengths.push(`Demonstrated valid Python syntax and partial implementation.`);
  }
  if (issues.length === 0 && totalScore < 100) {
    issues.push(`Some automated test cases failed; review requirements and edge cases.`);
  }

  const summary = totalScore >= 80
    ? `Great job! Your Python solution scored ${totalScore}/100 and meets the rubric expectations.`
    : `Your submission scored ${totalScore}/100. Review the detailed rubric breakdown and test cases to address any incomplete logic.`;

  return {
    finalScore: totalScore,
    breakdown,
    strengths,
    issues,
    summary
  };
}

/**
 * Evaluates student Python code against a detailed rubric criteria breakdown
 * in a SINGLE unified DeepSeek LLM call. Acts as the top-layer pedagogical teacher.
 */
export async function evaluatePythonRubric(studentCode, rubric, testResults, jobId = '', jobData = null) {
  // If no custom rubric is provided, synthesize standard 3-tier pedagogical criteria
  let activeRubric = rubric;
  if (!activeRubric || !Array.isArray(activeRubric.criteria) || activeRubric.criteria.length === 0) {
    activeRubric = {
      criteria: [
        { name: "Code Structure & Syntax", weight: 25, description: "Valid Python syntax, proper indentation, idiomatic naming, and clean code organization." },
        { name: "Core Logic Implementation", weight: 50, description: "Correctness of algorithmic steps, loops, conditionals, and core calculation logic." },
        { name: "Output & Edge Case Handling", weight: 25, description: "Accurate output format, return values, and resilience against test cases and edge cases." }
      ]
    };
  }

  // If studentCode is not provided or empty, attempt to fetch it directly from jobData
  if ((!studentCode || typeof studentCode !== 'string' || studentCode.trim().length < 10) && jobData) {
    try {
      const fetched = await fetchStudentCode(jobData);
      if (fetched && typeof fetched === 'string' && fetched.trim().length >= 10 && !fetched.startsWith("Code could not")) {
        studentCode = fetched;
      }
    } catch (e) {
      logger.warn(`Failed to fallback-fetch student code in evaluatePythonRubric for job ${jobId}: ${e.message}`);
    }
  }

  const passRatio = (testResults?.totalCount > 0)
    ? (testResults.passedCount / testResults.totalCount)
    : (typeof testResults?.score === 'number' ? testResults.score / 100 : 0);

  // If student passed 100% of tests, award full credit for every criterion
  if ((passRatio >= 0.99 || testResults?.score === 100) && studentCode && studentCode.trim().length > 10) {
    const breakdown = activeRubric.criteria.map(c => ({
      item: c.name,
      awarded: c.weight,
      max: c.weight,
      reason: "Fully passed all automated tests and meets rubric criteria."
    }));
    return {
      finalScore: 100,
      breakdown,
      strengths: activeRubric.criteria.map(c => `[${c.name}] Excellent implementation. Passed all requirements (earned ${c.weight}/${c.weight} marks)`),
      issues: [],
      summary: "Excellent work! All Python test cases and requirements passed successfully with a perfect score. Your solution is clean, robust, and fully functional."
    };
  }

  const openai = getClient();
  if (!openai) {
    return buildFallbackRubric(activeRubric, passRatio, studentCode);
  }

  try {
    const criteriaList = activeRubric.criteria
      .map((c, i) => `${i + 1}. "${c.name}" (weight: ${c.weight} points): ${c.description || "Evaluate Python code implementation."}`)
      .join("\n");

    const evalMode = jobData?.evaluationMode || 'function';
    const entryFn = jobData?.entryFunction || '';
    const assignmentContext = evalMode === 'function' && entryFn
      ? `Function Mode (Target Function: '${entryFn}')`
      : `Script Mode (Console/Print output matching)`;

    const prompt = `You are a strict, fair, and encouraging senior Python instructor and software engineer grading a student's submission against an evaluation rubric.

## Assignment Specifications:
- Evaluation Mode: ${assignmentContext}
- Expected Specifications: ${JSON.stringify(jobData?.testCases || jobData?.expectedLogs || []).slice(0, 1000)}

## Student's Source Code:
\`\`\`python
${studentCode ? studentCode.slice(0, 4000) : "No source code available."}
\`\`\`

## Automated Test Execution Results:
- Passed: ${testResults?.passedCount || 0} / ${testResults?.totalCount || 0} test cases (${Math.round(passRatio * 100)}%)
- Grader Output / Feedback: ${testResults?.feedback || 'N/A'}
- Execution Error (if any): ${JSON.stringify(testResults?.error || null)}

## Rubric Criteria to Evaluate:
${criteriaList}

## Grading Instructions:
1. Review the student's code like a real teacher:
   - For code-level criteria (syntax, variables, loops, conditionals, list/dict usage, functions): Award marks based on what the student actually attempted and wrote. Even if automated tests failed, if the student wrote genuine logic, award calibrated partial credit (e.g., 0.5 to 0.8).
   - For output criteria: Calibrate based on test outcomes and execution trace.
2. In 'issues', pinpoint the exact mistakes the student made:
   - Reference the exact line of code, function name, or variable where the mistake occurred.
   - Provide ONE concrete, actionable fix so the student can immediately correct their code and resubmit (e.g. "Change line 4 from 'print(ans)' to 'return ans'", "Add check for negative numbers with if x < 0: return 0").
3. In 'strengths', highlight 1 to 3 specific positive aspects of their code (e.g. good indentation, clean variable names, correct loop construct).
4. Assign a discrete multiplier for each criterion: 1.0, 0.9, 0.8, 0.5, 0.2, or 0.0.
5. Provide a 2 to 3 sentence concise, encouraging summary acknowledging what the student correctly implemented and what remains to be fixed.
6. CRITICAL: Never contradict the score. If the score is low (e.g., < 60), do NOT say "Great work, all tests passed" or "fully functional".

Output STRICTLY a JSON object with this shape:
{
  "summary": "<2-3 sentence executive feedback summary for the student>",
  "strengths": ["<specific strength 1 with code evidence>", "<specific strength 2 with code evidence>"],
  "issues": ["<specific mistake 1 citing line/function and exact actionable fix>", "<specific mistake 2 citing line/function and exact actionable fix>"],
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
    const aiStrengths = Array.isArray(parsed.strengths) ? parsed.strengths.filter(s => typeof s === 'string' && s.trim()) : [];
    const aiIssues = Array.isArray(parsed.issues) ? parsed.issues.filter(s => typeof s === 'string' && s.trim()) : [];

    const breakdown = [];
    const strengths = [...aiStrengths];
    const issues = [...aiIssues];
    let totalScore = 0;

    const cleanStr = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]/g, "");

    for (const c of activeRubric.criteria) {
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

      if (multiplier >= 0.7 && !aiStrengths.length) {
        strengths.push(`[${c.name}] ${reason} (earned ${awarded}/${maxPts} marks)`);
      } else if (multiplier < 0.7 && !aiIssues.length) {
        issues.push(`[${c.name}] ${reason} (earned ${awarded}/${maxPts} marks)`);
      }
    }

    const finalSummary = aiSummary || (
      totalScore >= 80
        ? `Great job! Your Python implementation scored ${totalScore}/100 and meets the rubric requirements well.`
        : `Your submission scored ${totalScore}/100. Review the detailed rubric breakdown and test cases to address any incomplete logic.`
    );

    return {
      finalScore: totalScore,
      breakdown,
      strengths: strengths.length > 0 ? strengths : (totalScore === 100 ? ["Passed all tests successfully."] : ["Demonstrated basic code structure."]),
      issues: issues.length > 0 ? issues : (totalScore < 100 ? ["Review failing test cases and adjust logic accordingly."] : []),
      summary: finalSummary
    };
  } catch (err) {
    logger.warn(`AI Rubric evaluation failed for Job ${jobId}: ${err.message}. Using structured fallback.`);
    return buildFallbackRubric(activeRubric, passRatio, studentCode);
  }
}

/**
 * Generates AI-assisted feedback for a student's Python submission when no rubric is provided.
 */
export async function generatePythonAIFeedback(jobData, githubResult, finalScore, breakdown = [], rubric = null) {
  if (finalScore === 100) {
    return "Excellent work! All Python test cases and requirements passed successfully with a perfect score. Your code is clean, idiomatic, and fully functional.";
  }

  const openai = getClient();
  if (!openai) {
    return buildPythonFallback(finalScore, jobData?.evaluationMode, jobData?.entryFunction, githubResult?.error);
  }

  const studentCode = await fetchStudentCode(jobData);

  const evaluationMode = jobData?.evaluationMode || 'function';
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
${studentCode ? studentCode.slice(0, 4000) : "No source code available."}
\`\`\`

## Instructions:
1. Write 2 to 3 concise, encouraging, and direct sentences explaining what the student implemented and exactly why test cases failed.
2. Directly reference specific lines of their Python code, function names, variables, loops, conditional checks, indentation, or print formatting that caused the failure.
3. Provide ONE clear, actionable suggestion on how to fix their Python logic (e.g. check return value vs print, handle type casting like int(x), fix off-by-one range index, handle edge cases).
4. CRITICAL: Never contradict the score. If the score is low (e.g., < 60) or tests failed, do NOT say "Great work, all tests passed" or "fully functional".
5. Return plain text only. Do NOT use markdown headers, bullet points, or quotes around the response.
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
    return feedback || buildPythonFallback(finalScore, evaluationMode, entryFunction, githubResult?.error);
  } catch (err) {
    logger.error(`[PythonFeedback] OpenAI/DeepSeek call failed: ${err.message}`);
    return buildPythonFallback(finalScore, evaluationMode, entryFunction, githubResult?.error);
  }
}

function buildPythonFallback(finalScore, evaluationMode, entryFunction, error = null) {
  if (finalScore >= 90) {
    return "Great work! All Python test cases and requirements passed successfully.";
  }
  if (error) {
    return `Your submission scored ${finalScore}/100. A runtime error occurred during test execution: ${typeof error === 'string' ? error : JSON.stringify(error)}. Please review your implementation and fix the error before resubmitting.`;
  }
  const modeContext = evaluationMode === 'function' && entryFunction
    ? `for function '${entryFunction}'`
    : 'in your script output';
  return `Your submission scored ${finalScore}/100. Some automated test cases failed ${modeContext}. Please review your return statements, variable types, and expected print output format to ensure your implementation satisfies all test specifications.`;
}
