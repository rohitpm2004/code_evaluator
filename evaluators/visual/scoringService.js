import { generateVisualAIFeedback } from "./feedbackService.js";
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

/**
 * Recursively reads .html, .css, .js files from a directory,
 * ignoring node_modules, .git, and common asset folders.
 */
async function readProjectFiles(dir, fileList = []) {
  const IGNORED_DIRS = new Set(["node_modules", ".git", "public", "assets", "images", "img"]);
  const ALLOWED_EXTENSIONS = new Set([".html", ".css", ".js"]);

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
 * Compresses source code to reduce AI token usage.
 */
function compressCode(code) {
  let compressed = code.replace(/\/\*[\s\S]*?\*\/|\/\/.*/g, "");
  compressed = compressed.replace(/^\s*[\r\n]/gm, "");
  compressed = compressed.replace(/[ \t]{2,}/g, " ");
  return compressed;
}

/**
 * Reads all relevant source files from the student project and returns
 * a concatenated string for AI analysis.
 */
async function getProjectCodeString(projectPath) {
  let files = await readProjectFiles(projectPath);

  // Cap at 15 files to avoid token overflow
  files = files.slice(0, 15);

  if (files.length === 0) {
    return "";
  }

  let codeStr = "";
  for (const f of files) {
    const relativePath = path.relative(projectPath, f);
    try {
      const content = await fs.readFile(f, "utf8");
      const compressed = compressCode(content);
      // Cap each file at 6000 compressed chars to prevent truncation
      codeStr += `\n--- ${relativePath} ---\n${compressed.slice(0, 6000)}\n`;
    } catch {
      // Skip unreadable files
    }
  }

  return codeStr;
}

export function parseRubric(rubricData) {
  if (typeof rubricData === 'string') {
    try {
      rubricData = JSON.parse(rubricData);
    } catch (e) {
      // It's a raw string, we'll try to parse it as text
      const lines = rubricData.split(/\r?\n/).map(l => l.trim()).filter(l => l.length > 3);
      const items = [];
      for (const line of lines) {
        const weightMatch = line.match(/\((\d+)\s*(?:pts?|points?|marks?|%)\)/i) ||
                            line.match(/\[(\d+)\s*(?:pts?|points?|marks?|%)\]/i) ||
                            line.match(/[-:]\s*(\d+)\s*(?:pts?|points?|marks?|%)\s*$/i) ||
                            line.match(/(\d+)\s*(?:pts?|points?|marks?)\s*[-:]/i) ||
                            line.match(/(\d+)\s*(?:pts?|points?|marks?|%)\s*$/i);
        const weight = weightMatch ? parseInt(weightMatch[1], 10) : 20;
        let name = line.replace(/^\d+[\.\)]\s*/, '').replace(/^[-*•]\s*/, '').trim();
        name = name.replace(/\((\d+)\s*(?:pts?|points?|marks?|%)\)/i, '')
                   .replace(/\[(\d+)\s*(?:pts?|points?|marks?|%)\]/i, '')
                   .replace(/[-:]\s*(\d+)\s*(?:pts?|points?|marks?|%)\s*$/i, '')
                   .replace(/(\d+)\s*(?:pts?|points?|marks?)\s*[-:]/i, '')
                   .replace(/(\d+)\s*(?:pts?|points?|marks?|%)\s*$/i, '')
                   .trim();
        if (name) items.push({ name, weight, description: name });
      }
      return items.length > 0 ? { criteria: items } : { criteria: [
        { name: 'HTML Structure & Markup', weight: 25, description: 'HTML layout and structure' },
        { name: 'CSS Styling & Design', weight: 25, description: 'CSS stylesheet styling' },
        { name: 'JavaScript Functionality', weight: 50, description: 'JavaScript DOM interactivity' }
      ]};
    }
  }
  
  // Handle already parsed object
  if (rubricData && rubricData.criteria) return rubricData;
  if (rubricData && Array.isArray(rubricData)) return { criteria: rubricData };
  return { criteria: [] };
}

/**
 * Scores a Visual (HTML/CSS/JS) submission using AI-based code analysis.
 */
export default async function scoreSubmission(rawRubric, projectPath, githubReport, options = {}) {
  const warnings = [];
  const rubric = parseRubric(rawRubric);
  const skipCache = Boolean(options?.skipCache || options?.reEvaluate || options?.isReEvaluation);

  const codeString = await getProjectCodeString(projectPath);

  let breakdown = {};
  let reasons = {};
  let multipliers = {};
  let totalScore = 0;

  if (!codeString) {
    logger.warn("No source files found in student repo.");
    const zeroMaxScore = rubric.criteria.reduce((sum, c) => sum + (c.weight || 0), 0) || 100;
    for (const c of rubric.criteria) {
      breakdown[c.name] = 0;
    }
    const feedbackText = await generateVisualAIFeedback({
      rubric_breakdown: breakdown,
      rubric_criteria: rubric.criteria,
      score: 0,
      maxScore: zeroMaxScore,
      warnings: ["No source files found in the repository."],
      execution_logs: githubReport || "",
    });
    const zeroFeedback = {
      summary: feedbackText,
      strengths: [],
      issues: ["No source files found in the repository."],
      breakdown: rubric.criteria.map(c => ({
        item: c.name,
        awarded: 0,
        max: c.weight,
        reason: "No source files found."
      }))
    };
    return {
      score: 0,
      rubric_breakdown: breakdown,
      feedback: zeroFeedback,
      rubricFeedback: zeroFeedback,
      warnings: ["No source files found in the repository."],
      execution_logs: githubReport || "",
      status: "fail",
    };
  }

  // Check evaluation cache: If this exact codebase + rubric was already graded, return identical result
  if (!skipCache) {
    const cached = await getCachedEvaluation("visual", rubric, codeString, githubReport);
    if (cached) {
      logger.info(`⚡ Returning cached Visual evaluation result: score=${cached.score}/100`);
      return cached;
    }
  }

  const criteriaList = rubric.criteria
    .map((c, i) => `${i + 1}. "${c.name}" (weight: ${c.weight} points): ${c.description || "No description provided."}`)
    .join("\n");

  const groq = getGroqClient();

  if (!groq) {
    logger.error("Groq client unavailable — OPENAI_API_KEY is not set.");
    throw new Error("AI grading client unavailable. Please check your API keys.");
  } else {
    const prompt = `You are an expert web development instructor grading a student's HTML/CSS/JS assignment.

## GitHub Actions Syntax Report:
${githubReport || "No report available."}

## Student's Compressed Source Code:
${codeString}

## Rubric Criteria to Evaluate:
${criteriaList}

## Grading Instructions:
You must be an extremely strict, objective, and unforgiving grader. 
Read the source code carefully to determine if the functionality requested in the rubric actually exists.
Pay close attention to whether the HTML actually links to the CSS (<link>) and JS (<script>). If they are not linked, the student cannot receive full marks for functionality that depends on them.

Assign a discrete score multiplier for EACH criterion based strictly on verified code evidence:
- 1.0 = Fully meets all exact requirements for this criterion (code is fully functional, complete, and linked correctly).
- 0.9 = Mostly correct with only very minor syntax, styling, or formatting flaws (e.g. minor CSS defect or minor spacing flaw).
- 0.8 = Good implementation but missing one specific sub-feature (e.g. missing 2-digit zero-padding or missing a secondary class).
- 0.5 = Partial implementation (core logic started but missing major aspects).
- 0.2 = Bare minimum skeleton or stub only.
- 0.0 = Not attempted, completely missing, or completely unrelated code.

CRITICAL SCORING RULE:
Use ONLY the discrete multipliers: 1.0, 0.9, 0.8, 0.5, 0.2, or 0.0.
Do NOT use intermediate or arbitrary decimal values like 0.84, 0.91, 0.73, etc.

For EACH criterion write a concise 1-2 sentence explanation:
1. States specifically what was FOUND in the source code files (cite code, tags, or selectors).
2. States specifically what is MISSING or WRONG compared to the rubric requirements.

CRITICAL REQUIREMENT:
You MUST evaluate and return a score entry for ALL ${rubric.criteria.length} criteria listed above. Do not stop early. Do not omit any criterion. Keep explanations to 1-2 sentences so all ${rubric.criteria.length} items fit easily.

Output STRICTLY a JSON object (no markdown, no extra text):
{
  "scores": [
    { "name": "<exact criterion name>", "multiplier": <number 0.0, 0.2, 0.5, 0.8, 0.9, or 1.0>, "reasoning": "<1-2 sentence concise explanation>" }
  ]
}`;

    try {
      logger.info("Sending code to Groq AI for visual rubric scoring...");

      const response = await groq.chat.completions.create({
        model: process.env.OPENAI_MODEL || "deepseek-chat",
        messages: [{ role: "user", content: prompt }],
        max_tokens: 3000,
        temperature: 0.0,
        seed: 42,
        response_format: { type: "json_object" },
      });

      let rawContent = response.choices[0]?.message?.content?.trim() || "";
      logger.info(`AI response received (length: ${rawContent.length}): ${rawContent.slice(0, 150)}...`);

      // Strip markdown code fences if returned
      if (rawContent.startsWith("```")) {
        rawContent = rawContent.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();
      }
      // Strip any DeepSeek reasoning/think tags
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
          const retryPrompt = `You are evaluating a student's submission. The following criteria need evaluation:

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
          const retryResponse = await groq.chat.completions.create({
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
          warnings.push(`AI did not return a valid score for "${c.name}" — defaulting to ${defaultScore}.`);
          breakdown[c.name] = defaultScore;
          reasons[c.name] = "Evaluation completed. Specific criteria breakdown unavailable.";
          multipliers[c.name] = 0.0;
        }
      }
    } catch (err) {
      logger.error(`AI scoring encountered error: ${err.message}`);
      // Fallback: don't crash the entire job, give baseline evaluation
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

  const maxScore = rubric.criteria.reduce((sum, c) => sum + c.weight, 0);
  // Clamp: Math.round() on each criterion can cause the accumulated total to
  // slightly exceed maxScore (e.g. 112 instead of 100). Cap it here.
  if (totalScore > maxScore) {
    logger.warn(`totalScore (${totalScore}) exceeded maxScore (${maxScore}) due to rounding — clamping to ${maxScore}.`);
    totalScore = maxScore;
  }
  const status = totalScore >= maxScore * 0.5 ? "pass" : "fail";

  logger.info(`Generating visual feedback for total score: ${totalScore}/${maxScore}`);
  const feedbackText = await generateVisualAIFeedback({
    rubric_breakdown: breakdown,
    rubric_criteria: rubric.criteria,
    per_criterion_reasons: reasons,
    score: totalScore,
    maxScore,
    warnings,
    execution_logs: githubReport || "",
    codeSnippet: codeString || "",
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

    if (mult >= 0.8 || awarded === c.weight) {
      strengths.push(`[${c.name}] ${reason} (earned ${awarded}/${c.weight} marks)`);
    } else {
      issues.push(`[${c.name}] ${reason} (earned ${awarded}/${c.weight} marks)`);
    }
  }

  if (strengths.length === 0 && totalScore === maxScore) {
    strengths.push("All criteria passed successfully.");
  }
  if (issues.length === 0 && totalScore < maxScore) {
    issues.push("Some criteria received partial marks.");
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
  };

  // Cache result for identical submissions
  await setCachedEvaluation("visual", rubric, codeString, githubReport, null, finalResult);

  return finalResult;
}
