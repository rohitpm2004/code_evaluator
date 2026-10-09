export default function scoreFullstack(backendResults, frontendResults, rubric) {
  const criteria = rubric?.criteria ?? [];

  let totalScore = 0;
  let maxScore = 0;
  const rubric_breakdown = [];

  const backendPassRatio = ratio(backendResults.passed, backendResults.total);
  const frontendPassRatio = ratio(frontendResults.passed, frontendResults.total);

  for (const criterion of criteria) {
    const { name, weight, layer } = criterion;
    maxScore += weight;

    let achievedPoints;

    if (layer === "backend") {
      const match = backendResults.test_details.find((t) => t.name === name);
      achievedPoints = match
        ? match.status === "pass" ? weight : 0
        : Math.round(weight * backendPassRatio);
    } else if (layer === "frontend") {
      const match = frontendResults.test_details.find((t) => t.name === name);
      achievedPoints = match
        ? match.status === "pass" ? weight : 0
        : Math.round(weight * frontendPassRatio);
    } else {
      // No layer specified — use average pass ratio
      const avgRatio = (backendPassRatio + frontendPassRatio) / 2;
      achievedPoints = Math.round(weight * avgRatio);
    }

    totalScore += achievedPoints;
    rubric_breakdown.push({
      name,
      layer: layer ?? "general",
      points_achieved: achievedPoints,
      total_points: weight,
    });
  }

  const passPercent = maxScore > 0 ? (totalScore / maxScore) * 100 : 0;
  const status = passPercent >= 70 ? "pass" : "fail";

  return {
    score: totalScore,
    maxScore,
    passPercent: Math.round(passPercent),
    status,
    rubric_breakdown,
    backend: {
      passed: backendResults.passed,
      total: backendResults.total,
      test_details: backendResults.test_details,
    },
    frontend: {
      passed: frontendResults.passed,
      total: frontendResults.total,
      test_details: frontendResults.test_details,
    },
  };
}

function ratio(passed, total) {
  if (!total) return 0;
  return passed / total;
}

/**
 * Helper to compute evaluation score from Playwright JSON report.
 */
export function scoreFromTestResults(rubric, testResults) {
  if (!testResults || testResults.error || !Array.isArray(testResults.suites)) {
    return null;
  }

  // Flatten all tests from the Playwright suites
  const assertions = [];
  
  function recurseSuites(suite) {
    if (Array.isArray(suite.suites)) {
      suite.suites.forEach(recurseSuites);
    }
    if (Array.isArray(suite.specs)) {
      suite.specs.forEach(spec => {
        const title = spec.title || "";
        const testRun = spec.tests?.[0];
        const result = testRun?.results?.[0];
        const status = result?.status || "failed";
        const failureMessage = result?.error?.message || "Test failed";
        
        assertions.push({
          title,
          status: status === "passed" ? "passed" : "failed",
          failureMessage
        });
      });
    }
  }

  testResults.suites.forEach(recurseSuites);

  if (assertions.length === 0) {
    return null;
  }

  const breakdown = {};
  const reasons = {};
  const multipliers = {};
  let totalScore = 0;

  // Group tests by rubric name prefix
  const categoryStats = {};
  const criteria = rubric.criteria || [];
  for (const c of criteria) {
    categoryStats[c.name] = { passed: 0, total: 0, failedDetails: [] };
  }

  for (const assertion of assertions) {
    const title = assertion.title || "";
    const status = assertion.status;
    
    // Find matching rubric
    let matchedCriterion = null;
    for (const c of criteria) {
      if (title.toLowerCase().startsWith(c.name.toLowerCase())) {
        matchedCriterion = c.name;
        break;
      }
    }

    if (matchedCriterion) {
      categoryStats[matchedCriterion].total++;
      if (status === "passed") {
        categoryStats[matchedCriterion].passed++;
      } else {
        const cleanedError = (assertion.failureMessage || "Test failed")
          .split('\n')[0]
          .replace(/\x1B\[\d+m/g, ""); // Strip ANSI colors
        categoryStats[matchedCriterion].failedDetails.push(cleanedError);
      }
    }
  }

  const totalCriteria = criteria.length;
  const matchedCriteriaCount = Object.values(categoryStats).filter(s => s.total > 0).length;

  if (matchedCriteriaCount === 0 || matchedCriteriaCount < totalCriteria) {
    logger.info(`Playwright test suite only matched ${matchedCriteriaCount}/${totalCriteria} criteria. Falling back to AI code grading.`);
    return null;
  }

  // Calculate scores
  for (const c of criteria) {
    const stats = categoryStats[c.name];
    if (stats && stats.total > 0) {
      const multiplier = stats.passed / stats.total;
      const score = Math.round(multiplier * c.weight);
      breakdown[c.name] = score;
      multipliers[c.name] = Math.round(multiplier * 10) / 10;
      
      if (stats.failedDetails.length === 0) {
        reasons[c.name] = `Passed all ${stats.total} Playwright checks.`;
      } else {
        reasons[c.name] = `Failed ${stats.failedDetails.length}/${stats.total} checks. Errors: ${stats.failedDetails.join('; ')}`;
      }
    } else {
      breakdown[c.name] = 0;
      multipliers[c.name] = 0.0;
      reasons[c.name] = "No tests found or executed for this criterion.";
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
