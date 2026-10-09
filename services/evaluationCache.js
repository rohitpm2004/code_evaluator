import crypto from "crypto";
import redisConnection from "../config/redis.js";
import logger from "../config/logger.js";

// In-memory fallback cache (LRU-like cap of 1000 items)
const memoryCache = new Map();
const MAX_MEMORY_ITEMS = 1000;
const DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60; // 7 days

/**
 * Computes a deterministic SHA-256 hash based on the evaluation inputs:
 * Evaluator type, normalized rubric, student code content, build report, and test results.
 */
export function computeEvaluationHash(type, rubric, codeString, buildReport = "", testResults = null) {
  // Normalize rubric criteria so ordering differences don't cause cache misses
  const normalizedCriteria = (rubric?.criteria || [])
    .map(c => ({
      name: (c.name || "").trim().toLowerCase(),
      weight: c.weight,
      description: (c.description || "").trim()
    }))
    .sort((a, b) => a.name.localeCompare(b.name));

  const normalizedPayload = {
    type: (type || "").trim().toLowerCase(),
    criteria: normalizedCriteria,
    code: (codeString || "").trim(),
    buildReport: (buildReport || "").trim(),
    testResults: testResults ? JSON.stringify(testResults) : ""
  };

  return crypto
    .createHash("sha256")
    .update(JSON.stringify(normalizedPayload))
    .digest("hex");
}

/**
 * Retrieves a cached evaluation result if the exact same submission was previously evaluated.
 *
 * @param {string} type - Evaluator type ('react', 'backend', 'js', etc.)
 * @param {Object} rubric - Rubric object
 * @param {string} codeString - Compressed or raw source code of the student repo
 * @param {string} buildReport - Build or linter report
 * @param {Object} testResults - Automated test results (if any)
 * @returns {Promise<Object|null>} Cached result or null
 */
export async function getCachedEvaluation(type, rubric, codeString, buildReport = "", testResults = null) {
  try {
    const hash = computeEvaluationHash(type, rubric, codeString, buildReport, testResults);
    const cacheKey = `eval_cache:${type}:${hash}`;

    // 1. Check in-memory cache first
    if (memoryCache.has(cacheKey)) {
      const item = memoryCache.get(cacheKey);
      if (item.expiresAt > Date.now()) {
        logger.info(`⚡ Evaluation cache HIT (Memory) [${type}]: hash=${hash.slice(0, 12)}... (identical code & rubric)`);
        return JSON.parse(JSON.stringify(item.data));
      } else {
        memoryCache.delete(cacheKey);
      }
    }

    // 2. Check Redis cache
    const redisClient = redisConnection.getClient();
    if (redisClient && redisConnection.status === "ready") {
      const raw = await redisClient.get(cacheKey);
      if (raw) {
        logger.info(`⚡ Evaluation cache HIT (Redis) [${type}]: hash=${hash.slice(0, 12)}... (identical code & rubric)`);
        const data = JSON.parse(raw);
        // Populate local memory cache
        memoryCache.set(cacheKey, {
          data,
          expiresAt: Date.now() + DEFAULT_TTL_SECONDS * 1000
        });
        return JSON.parse(JSON.stringify(data));
      }
    }
  } catch (err) {
    logger.warn(`Error reading evaluation cache: ${err.message}`);
  }

  return null;
}

/**
 * Stores an evaluation result in both Redis and Memory cache.
 *
 * @param {string} type - Evaluator type
 * @param {Object} rubric - Rubric object
 * @param {string} codeString - Source code
 * @param {string} buildReport - Build report
 * @param {Object} testResults - Test results
 * @param {Object} result - Final evaluation result to cache
 * @param {number} ttlSeconds - Cache TTL in seconds (default 7 days)
 */
export async function setCachedEvaluation(
  type,
  rubric,
  codeString,
  buildReport = "",
  testResults = null,
  result,
  ttlSeconds = DEFAULT_TTL_SECONDS
) {
  try {
    if (!result || typeof result.score !== "number") return;

    // Do not cache infrastructure failures, AI-error fallbacks, or transient errors
    const isAiErrorFallback = 
      Boolean(result.error) ||
      result.status === "infrastructure_failed" ||
      result.status === "error" ||
      result.fallback === true ||
      result.manualCorrection === true ||
      (Array.isArray(result.warnings) && result.warnings.some(w => /timeout|api error|rate limit|econnreset|503|unreachable|groq|openai|deepseek|ai scoring failed/i.test(String(w)))) ||
      (Array.isArray(result.feedback?.issues) && result.feedback.issues.some(issue => /AI scoring failed|rate limit|timeout|503/i.test(String(issue))));

    if (isAiErrorFallback) {
      logger.info(`Skipping cache for AI-error fallback/failed evaluation [${type}]: score=${result.score}`);
      return;
    }

    const hash = computeEvaluationHash(type, rubric, codeString, buildReport, testResults);
    const cacheKey = `eval_cache:${type}:${hash}`;

    // 1. Save to in-memory cache
    if (memoryCache.size >= MAX_MEMORY_ITEMS) {
      // Evict oldest item
      const firstKey = memoryCache.keys().next().value;
      if (firstKey) memoryCache.delete(firstKey);
    }
    memoryCache.set(cacheKey, {
      data: result,
      expiresAt: Date.now() + ttlSeconds * 1000
    });

    // 2. Save to Redis
    const redisClient = redisConnection.getClient();
    if (redisClient && redisConnection.status === "ready") {
      await redisClient.set(cacheKey, JSON.stringify(result), "EX", ttlSeconds);
      logger.info(`💾 Evaluation cached to Redis [${type}]: hash=${hash.slice(0, 12)}... (score=${result.score})`);
    }
  } catch (err) {
    logger.warn(`Error saving to evaluation cache: ${err.message}`);
  }
}
