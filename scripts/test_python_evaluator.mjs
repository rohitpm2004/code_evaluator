import path from 'path';
import fs from 'fs';
import os from 'os';
import { runPythonInDocker } from '../services/dockerSandboxService.js';
import { evaluatePythonRubric, generatePythonAIFeedback } from '../evaluators/python/aiFeedback.js';

let passedTests = 0;
let totalTests = 0;

function assert(condition, message) {
  totalTests++;
  if (!condition) {
    console.error(`❌ FAIL: ${message}`);
    throw new Error(`Assertion failed: ${message}`);
  }
  passedTests++;
  console.log(`✅ PASS: ${message}`);
}

async function runTestSuite() {
  console.log("=================================================================");
  console.log("   CENTRAL EVALUATOR: PYTHON EVALUATOR ARCHITECTURE TEST SUITE   ");
  console.log("=================================================================\n");

  const tempRoot = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'py-test-suite-'));

  try {
    // -------------------------------------------------------------
    // TEST 1: Script Mode with Output & Delimiter Matching
    // -------------------------------------------------------------
    console.log("--- TEST 1: SCRIPT MODE (Prints & Fuzzy Delimiter Match) ---");
    const scriptDir = path.join(tempRoot, 'test1_script');
    await fs.promises.mkdir(scriptDir, { recursive: true });
    await fs.promises.writeFile(path.join(scriptDir, 'main.py'), `
name = "Rohit Sharma"
score = 95.5
passed = True
print(f"Candidate Name: {name}")
print(f"Total Score = {score}")
print(f"Status is {passed}")
`, 'utf8');

    const res1 = await runPythonInDocker(scriptDir, {
      evaluationMode: 'script',
      expectedLogs: ["Rohit Sharma", 95.5, true]
    });

    assert(res1.score === 100, `Script mode should score 100% (got ${res1.score})`);
    assert(res1.passedCount === 3, `Script mode passed 3/3 tests (got ${res1.passedCount})`);
    assert(res1.results.length === 3, `Expected 3 test result entries`);

    // -------------------------------------------------------------
    // TEST 2: Interactive input() Mocking (No Hangs / No EOFError)
    // -------------------------------------------------------------
    console.log("\n--- TEST 2: INTERACTIVE input() PROMPTING ---");
    const inputDir = path.join(tempRoot, 'test2_input');
    await fs.promises.mkdir(inputDir, { recursive: true });
    await fs.promises.writeFile(path.join(inputDir, 'main.py'), `
user_name = input("Enter your name: ")
user_age = int(input("Enter your age: "))
next_year = user_age + 1
print(f"Welcome {user_name}!")
print(f"Next year you will be: {next_year}")
`, 'utf8');

    const res2 = await runPythonInDocker(inputDir, {
      evaluationMode: 'script',
      inputs: ["Saniya", "22"],
      expectedLogs: ["Welcome Saniya!", 23]
    });

    assert(res2.score === 100, `Interactive input should score 100% (got ${res2.score})`);
    assert(res2.passedCount === 2, `Interactive input passed 2/2 tests (got ${res2.passedCount})`);

    // -------------------------------------------------------------
    // TEST 3: Function Mode (Unit Test Assertions & Types)
    // -------------------------------------------------------------
    console.log("\n--- TEST 3: FUNCTION MODE (Unit Tests & Multi-Param) ---");
    const fnDir = path.join(tempRoot, 'test3_fn');
    await fs.promises.mkdir(fnDir, { recursive: true });
    await fs.promises.writeFile(path.join(fnDir, 'solution.py'), `
def calculate_grade(marks, total=100):
    pct = (marks / total) * 100
    if pct >= 90:
        return {"grade": "A", "percentage": pct}
    elif pct >= 75:
        return {"grade": "B", "percentage": pct}
    return {"grade": "C", "percentage": pct}
`, 'utf8');

    const res3 = await runPythonInDocker(fnDir, {
      entryFunction: 'calculate_grade',
      evaluationMode: 'function',
      testCases: [
        { input: [95, 100], expected: { grade: "A", percentage: 95.0 } },
        { input: [80, 100], expected: { grade: "B", percentage: 80.0 } },
        { input: [50, 100], expected: { grade: "C", percentage: 50.0 } }
      ]
    });

    assert(res3.score === 100, `Function mode should score 100% (got ${res3.score})`);
    assert(res3.passedCount === 3, `Function mode passed 3/3 cases`);

    // -------------------------------------------------------------
    // TEST 4: Multi-Function Mode
    // -------------------------------------------------------------
    console.log("\n--- TEST 4: MULTI-FUNCTION MODE ---");
    const multiDir = path.join(tempRoot, 'test4_multi');
    await fs.promises.mkdir(multiDir, { recursive: true });
    await fs.promises.writeFile(path.join(multiDir, 'main.py'), `
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
`, 'utf8');

    const res4 = await runPythonInDocker(multiDir, {
      evaluationMode: 'multi-function',
      functions: [
        {
          name: 'add',
          testCases: [
            { input: [10, 20], expected: 30 },
            { input: [-5, 5], expected: 0 }
          ]
        },
        {
          name: 'multiply',
          testCases: [
            { input: [4, 5], expected: 20 },
            { input: [3, 0], expected: 0 }
          ]
        }
      ]
    });

    assert(res4.score === 100, `Multi-function mode should score 100% (got ${res4.score})`);
    assert(res4.passedCount === 4, `Multi-function mode passed 4/4 test cases`);

    // -------------------------------------------------------------
    // TEST 5: AST Pre-flight Safety Guard (Prohibited System Calls)
    // -------------------------------------------------------------
    console.log("\n--- TEST 5: AST PRE-FLIGHT SAFETY GUARD ---");
    const evilDir = path.join(tempRoot, 'test5_evil');
    await fs.promises.mkdir(evilDir, { recursive: true });
    await fs.promises.writeFile(path.join(evilDir, 'main.py'), `
import subprocess
subprocess.call(["ls", "-la"])
`, 'utf8');

    const res5 = await runPythonInDocker(evilDir, {
      evaluationMode: 'script',
      expectedLogs: ["hello"]
    });

    assert(res5.score === 0, `Prohibited subprocess import must score 0% (got ${res5.score})`);
    assert(res5.passed === false, `Passed flag must be false`);
    assert(typeof res5.error === 'string' && res5.error.includes("restricted module"), `Error must mention restricted module`);

    // -------------------------------------------------------------
    // TEST 6: AI Teacher Rubric Evaluation
    // -------------------------------------------------------------
    console.log("\n--- TEST 6: AI TEACHER RUBRIC EVALUATION ---");
    const samplePythonCode = `
def process_data(items):
    # Calculate sum and filtered items
    total = sum(x for x in items if x > 0)
    positives = [x for x in items if x > 0]
    return {"total": total, "count": len(positives)}
`;

    const sampleRubric = {
      criteria: [
        { name: "Function Definition & Syntax", weight: 30, description: "Correct function definition and signature" },
        { name: "List Comprehension & Filtering", weight: 40, description: "Correctly filters positive numbers using list comprehension" },
        { name: "Dictionary Aggregation", weight: 30, description: "Returns dictionary with total and count" }
      ]
    };

    const rubricRes = await evaluatePythonRubric(samplePythonCode, sampleRubric, {
      passedCount: 3,
      totalCount: 3,
      score: 100,
      feedback: "All test cases passed."
    }, "job-test-6", null);

    assert(rubricRes.finalScore === 100, `Full pass rubric should award 100 (got ${rubricRes.finalScore})`);
    assert(rubricRes.breakdown.length === 3, `Rubric breakdown should contain 3 criteria`);
    assert(rubricRes.strengths.length > 0, `Should include strengths for student`);
    assert(typeof rubricRes.summary === 'string' && rubricRes.summary.length > 10, `Should include executive summary`);

    console.log("\n=================================================================");
    console.log(`   TEST RESULTS: ${passedTests}/${totalTests} TESTS PASSED (100%)   `);
    console.log("=================================================================\n");
  } finally {
    try {
      await fs.promises.rm(tempRoot, { recursive: true, force: true });
    } catch {}
  }
}

runTestSuite().catch(err => {
  console.error("Test execution failed:", err);
  process.exit(1);
});
