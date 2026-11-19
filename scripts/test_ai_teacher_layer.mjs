import { evaluatePythonRubric } from '../evaluators/python/aiFeedback.js';

async function testTeacherLayer() {
  const studentCode = `
def find_largest(numbers):
    # student mistake: initialized to 0, breaks on negative numbers
    max_val = 0
    for n in numbers:
        if n > max_val:
            max_val = n
    return max_val
`;

  const testResults = {
    passedCount: 1,
    totalCount: 2,
    score: 50,
    results: [
      { passed: true, input: [1, 5, 2], expected: 5, actual: 5 },
      { passed: false, input: [-10, -5, -20], expected: -5, actual: 0 }
    ],
    feedback: 'Failed on negative numbers input'
  };

  const jobData = {
    evaluationMode: 'function',
    entryFunction: 'find_largest',
    testCases: [
      { input: [1, 5, 2], expected: 5 },
      { input: [-10, -5, -20], expected: -5 }
    ]
  };

  console.log("==========================================================");
  console.log("       TOP-LAYER AI TEACHER VERIFICATION TEST             ");
  console.log("==========================================================\n");

  console.log("Input Student Code:\n" + studentCode);
  console.log("Automated Execution: 1/2 tests passed (50%)\n");

  const evaluation = await evaluatePythonRubric(studentCode, null, testResults, 'job-test-teacher', jobData);

  console.log("--- AI TEACHER EVALUATION RESULTS ---");
  console.log("Final Calibrated Score:", evaluation.finalScore + "/100");
  console.log("Executive Summary:\n ", evaluation.summary);
  console.log("\nStrengths:\n", JSON.stringify(evaluation.strengths, null, 2));
  console.log("\nActionable Issues & Fixes for Student:\n", JSON.stringify(evaluation.issues, null, 2));
  console.log("\nCriteria Breakdown:\n", JSON.stringify(evaluation.breakdown, null, 2));

  if (evaluation.finalScore > 0 && evaluation.issues.length > 0 && evaluation.strengths.length > 0 && evaluation.breakdown.length === 3) {
    console.log("\n✅ SUCCESS: Top AI Teacher Layer successfully evaluated student submission, populated actionable feedback, strengths, issues, and calibrated scores!");
  } else {
    console.error("\n❌ FAILED");
    process.exit(1);
  }
}

testTeacherLayer().catch(err => {
  console.error("Test error:", err);
  process.exit(1);
});
