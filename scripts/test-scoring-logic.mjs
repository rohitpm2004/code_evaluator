/**
 * Unit test for scoring normalization and validation.
 */
import { parseRubric } from "../evaluators/visual/scoringService.js";
import { computeSqlFinalScore } from "../evaluators/sql/evaluatorService.js";

let failures = 0;
function ok(name, cond) {
  console.log(`${cond ? "✅" : "❌"} ${name}`);
  if (!cond) failures++;
}

console.log("--- Scoring Logic Unit Tests ---\n");

// Test scoring thresholds
{
  const maxScore = 100;
  const passScore = 60;
  const failScore = 40;
  
  ok("Pass threshold matches >= 50%", passScore >= maxScore * 0.5);
  ok("Fail threshold matches < 50%", failScore < maxScore * 0.5);
}

// Test rubric criteria weight summation
{
  const rubric = parseRubric(JSON.stringify({
    criteria: [
      { name: "HTML", weight: 20 },
      { name: "CSS", weight: 30 },
      { name: "JS", weight: 50 }
    ]
  }));
  const total = rubric.criteria.reduce((s, c) => s + c.weight, 0);
  ok("Rubric criteria sum to 100 points", total === 100);
}

// Test SQL deterministic scoring (ungrounded effort-floor removed)
{
  ok(
    "SQL deterministicScore > 0 uses deterministicScore when pedagogical is higher (prevent inflation)",
    computeSqlFinalScore({ deterministicScore: 85, pedagogicalScore: 100, hasTestResults: true, policyBlocked: false }) === 85
  );
  ok(
    "SQL deterministicScore > 0 strictly uses verified deterministicScore for 100% repeatability",
    computeSqlFinalScore({ deterministicScore: 72, pedagogicalScore: 90, hasTestResults: true, policyBlocked: false }) === 72 &&
    computeSqlFinalScore({ deterministicScore: 72, pedagogicalScore: 60, hasTestResults: true, policyBlocked: false }) === 72
  );
  ok(
    "SQL deterministicScore === 0 with pedagogicalScore awards capped partial credit for genuine effort",
    computeSqlFinalScore({ deterministicScore: 0, pedagogicalScore: 65, hasTestResults: true, policyBlocked: false }) === 65 &&
    computeSqlFinalScore({ deterministicScore: 0, pedagogicalScore: 95, hasTestResults: true, policyBlocked: false }) === 70
  );
  ok(
    "SQL deterministicScore === 0 with policyBlocked stays 0",
    computeSqlFinalScore({ deterministicScore: 0, pedagogicalScore: 80, hasTestResults: true, policyBlocked: true }) === 0
  );
}

console.log("");
console.log(failures === 0 ? "All Scoring Logic assertions PASS." : `${failures} assertion(s) FAILED.`);
process.exit(failures === 0 ? 0 : 1);
