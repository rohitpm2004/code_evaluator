import logger from '../../config/logger.js';

/**
 * Calculates the final score based on the rubric criteria and test results.
 * @param {Object} rubric The rubric.json defining weights and criteria.
 * @param {Object} testResults Output from runApiTests containing passed/failed counts.
 * @returns {Object} Structured evaluation result.
 */
export const evaluateResults = (rubric, testResults) => {
    // Basic logic mapping test percentage pass/fail to the rubric's "All API endpoints work" criterion
    // In a production system, tests would map individually to specific rubric categories.
    const rubricCriteria = rubric.criteria || [];

    let totalScore = 0;
    let maxScore = 0;
    const rubric_breakdown = [];

    // Simple ratio scaling for the MVP
    const testDetails = testResults.test_details || [];
    const globalPassRatio = testResults.passedCount / (testResults.totalTests || 1);

    // Performance Monitoring
    const PERFORMANCE_THRESHOLD_MS = 500;
    const httpTests = testDetails.filter(t => t.duration !== undefined);
    const slowTests = httpTests.filter(t => t.duration > PERFORMANCE_THRESHOLD_MS);
    
    rubricCriteria.forEach((criterion) => {
        const possiblePoints = criterion.weight;
        maxScore += possiblePoints;

        const nameLower = criterion.name.toLowerCase();
        const isPerformanceCriterion = nameLower.includes('performance') || nameLower.includes('speed') || nameLower.includes('efficiency');

        let achievedPoints;
        if (isPerformanceCriterion) {
            // Performance logic: percentage of tests that stayed under threshold
            const fastTests = httpTests.length - slowTests.length;
            const perfRatio = httpTests.length > 0 ? (fastTests / httpTests.length) : 1;
            achievedPoints = Math.round(possiblePoints * perfRatio);
            logger.debug(`[Scoring] Performance Criterion "${criterion.name}": ${fastTests}/${httpTests.length} tests under ${PERFORMANCE_THRESHOLD_MS}ms. Score: ${achievedPoints}/${possiblePoints}`);
        } else {
            // Standard Logic: Find tests belonging to this criterion
            const matchingTests = testDetails.filter(t => t.criterion === criterion.name);
            if (matchingTests.length > 0) {
                const critPassed = matchingTests.filter(t => t.status === 'pass').length;
                const critRatio = critPassed / matchingTests.length;
                achievedPoints = Math.round(possiblePoints * critRatio);
            } else {
                achievedPoints = Math.round(possiblePoints * globalPassRatio);
            }
        }

        totalScore += achievedPoints;
        rubric_breakdown.push({
            name: criterion.name,
            points_achieved: achievedPoints,
            total_points: possiblePoints
        });
    });

    // Add Performance warnings to the final output
    if (slowTests.length > 0) {
        const slowList = slowTests.map(t => `${t.name} (${t.duration}ms)`).join(', ');
        const truncated = slowList.length > 200 ? `${slowList.slice(0, 200)}...` : slowList;
        testResults.warnings = testResults.warnings || [];
        testResults.warnings.push(`Performance Warning: ${slowTests.length} tests exceeded the ${PERFORMANCE_THRESHOLD_MS}ms threshold: ${truncated}`);
    }

    // We consider "Pass" to be 70% or greater overall.
    const passPercent = maxScore > 0 ? (totalScore / maxScore) * 100 : 0;
    const isPass = passPercent >= 70;

    let feedback = `Your API passed ${testResults.passedCount} out of ${testResults.totalTests} tests.`;

    if (isPass) {
        feedback += " Great job! Your backend meets the requirements.";
    } else {
        feedback += " Please review the errors and warnings to improve the robustness of your API.";
    }

    return {
        score: totalScore,
        maxScore,
        rubric_breakdown,
        feedback,
        ai_feedback: null, // Placeholder for LLM feedback
        test_details: testResults.test_details,
        warnings: testResults.warnings,
        execution_logs: testResults.execution_logs,
        pass: isPass
    };
};

/**
 * Matches an assertion title to a rubric criterion using a resilient multi-tier strategy.
 */
function matchAssertionToCriterion(title, criteria) {
  const normTitle = (title || "").toLowerCase();

  // Tier 1: Explicit tag, e.g. "[User Registration]" or "@criterion:User Registration"
  for (const c of criteria) {
    const normName = (c.name || "").toLowerCase();
    if (normTitle.includes(`[${normName}]`) || normTitle.includes(`@criterion:${normName}`)) {
      return c.name;
    }
  }

  // Sort criteria by name length descending so more specific names match first
  const sortedByLength = [...criteria].sort((a, b) => (b.name || "").length - (a.name || "").length);

  // Tier 2: Exact word or prefix match of criterion name
  for (const c of sortedByLength) {
    const normName = (c.name || "").trim().toLowerCase();
    if (!normName) continue;
    if (normTitle.startsWith(normName)) {
      return c.name;
    }
    // Only match as whole words, avoiding substring false positives (e.g. "auth" inside "author")
    const escaped = normName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const regex = new RegExp(`(^|[^a-z0-9])${escaped}([^a-z0-9]|$)`, 'i');
    if (regex.test(normTitle)) {
      return c.name;
    }
  }

  // Tier 3: Keyword matching from criterion name and description
  let bestMatch = null;
  let maxScore = 0;
  for (const c of criteria) {
    const words = `${c.name} ${c.description || ""}`
      .toLowerCase()
      .replace(/[^a-z0-9\s]/g, " ")
      .split(/\s+/)
      .filter(w => w.length > 3 && !["with", "from", "that", "this", "should", "must", "when", "test"].includes(w));

    let count = 0;
    for (const w of words) {
      if (normTitle.includes(w)) count++;
    }
    if (count > maxScore && count >= 2) {
      maxScore = count;
      bestMatch = c.name;
    }
  }

  return bestMatch;
}

/**
 * Helper to compute evaluation score from Jest JSON report.
 */
export function scoreFromTestResults(rubric, testResults) {
  if (!testResults || testResults.error || !Array.isArray(testResults.testResults)) {
    return null;
  }
  const assertions = Array.isArray(testResults.testResults)
    ? testResults.testResults.flatMap(suite => suite.assertionResults || [])
    : (Array.isArray(testResults.assertionResults) ? testResults.assertionResults : []);
  if (assertions.length === 0) {
    return null;
  }

  const breakdown = {};
  const reasons = {};
  const multipliers = {};
  let totalScore = 0;

  // Group tests by rubric name prefix or keywords
  const categoryStats = {};
  const criteria = rubric.criteria || [];
  for (const c of criteria) {
    categoryStats[c.name] = { passed: 0, total: 0, failedDetails: [] };
  }

  for (const assertion of assertions) {
    const title = assertion.title || "";
    const status = assertion.status; // "passed" or "failed"
    
    // Find matching rubric
    const matchedCriterion = matchAssertionToCriterion(title, criteria);

    if (matchedCriterion && categoryStats[matchedCriterion]) {
      categoryStats[matchedCriterion].total++;
      if (status === "passed") {
        categoryStats[matchedCriterion].passed++;
      } else {
        const cleanedError = (assertion.failureMessages?.[0] || "Test assertion failed")
          .split('\n')[0] // Only get the first line of the error to keep it clean
          .replace(/\x1B\[\d+m/g, ""); // Strip ANSI colors
        categoryStats[matchedCriterion].failedDetails.push(cleanedError);
      }
    }
  }
  
  const passedAssertions = assertions.filter(a => a.status === "passed").length;
  const overallPassRatio = assertions.length > 0 ? passedAssertions / assertions.length : 0;
  const matchedCriteriaCount = Object.values(categoryStats).filter(s => s.total > 0).length;

  if (matchedCriteriaCount < criteria.length) {
    logger.info(`Jest test suite covers ${matchedCriteriaCount}/${criteria.length} criteria. Falling back to AI code analysis so all criteria are evaluated on actual code evidence.`);
    return null;
  }

  // Calculate scores for fully matched criteria
  for (const c of criteria) {
    const stats = categoryStats[c.name];
    const multiplier = stats.passed / stats.total;
    const score = Math.round(multiplier * c.weight);
    breakdown[c.name] = score;
    multipliers[c.name] = Math.round(multiplier * 10) / 10;
    
    if (stats.failedDetails.length === 0) {
      reasons[c.name] = `Passed all ${stats.total} unit tests.`;
    } else {
      reasons[c.name] = `Failed ${stats.failedDetails.length}/${stats.total} tests. Errors: ${stats.failedDetails.join('; ')}`;
    }
    totalScore += breakdown[c.name];
  }

  return {
    score: totalScore,
    rubric_breakdown: breakdown,
    reasons,
    multipliers
  };
}