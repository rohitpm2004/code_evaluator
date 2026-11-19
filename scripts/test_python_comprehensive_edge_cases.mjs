import path from 'path';
import fs from 'fs';
import os from 'os';
import { runPythonInDocker } from '../services/dockerSandboxService.js';
import { evaluatePythonRubric, generatePythonAIFeedback } from '../evaluators/python/aiFeedback.js';

let passedCount = 0;
let totalCount = 0;
const failures = [];

function check(testName, condition, details = "") {
  totalCount++;
  if (condition) {
    passedCount++;
    console.log(`  ✅ [PASS] ${testName}`);
  } else {
    failures.push({ testName, details });
    console.error(`  ❌ [FAIL] ${testName}: ${details}`);
  }
}

async function runEdgeCaseMatrix() {
  console.log("==========================================================================");
  console.log("      PYTHON EVALUATOR: EXHAUSTIVE REAL-WORLD EDGE CASE SUITE             ");
  console.log("==========================================================================\n");

  const tempRoot = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'py-edge-cases-'));

  try {
    // -------------------------------------------------------------------------
    // SUITE 1: SYNTAX, EMPTY FILES & COMPILATION ERRORS
    // -------------------------------------------------------------------------
    console.log("👉 SUITE 1: Syntax, Indentation & Empty Submissions");

    // Case 1.1: Completely empty submission
    const dir1_1 = path.join(tempRoot, 'case1_1_empty');
    await fs.promises.mkdir(dir1_1, { recursive: true });
    await fs.promises.writeFile(path.join(dir1_1, 'main.py'), '', 'utf8');
    const res1_1 = await runPythonInDocker(dir1_1, { evaluationMode: 'script', expectedLogs: ['hello'] });
    check("1.1 Empty file script mode scores 0 without crashing", res1_1.score === 0 && res1_1.passed === false);

    const res1_1_fn = await runPythonInDocker(dir1_1, { evaluationMode: 'function', entryFunction: 'solution', testCases: [{ input: 1, expected: 2 }] });
    check("1.1b Empty file function mode cleanly flags missing function", res1_1_fn.score === 0 && (res1_1_fn.error || '').includes('not found'));

    // Case 1.2: Invalid syntax (unclosed paren / invalid token)
    const dir1_2 = path.join(tempRoot, 'case1_2_syntax');
    await fs.promises.mkdir(dir1_2, { recursive: true });
    await fs.promises.writeFile(path.join(dir1_2, 'main.py'), 'def calculate(\n    return 42', 'utf8');
    const res1_2 = await runPythonInDocker(dir1_2, { evaluationMode: 'function', entryFunction: 'calculate', testCases: [{ input: null, expected: 42 }] });
    check("1.2 SyntaxError handled gracefully without server exception", res1_2.score === 0 && res1_2.passed === false);

    // Case 1.3: IndentationError (very common beginner mistake)
    const dir1_3 = path.join(tempRoot, 'case1_3_indent');
    await fs.promises.mkdir(dir1_3, { recursive: true });
    await fs.promises.writeFile(path.join(dir1_3, 'main.py'), 'def solution(x):\nreturn x * 2', 'utf8');
    const res1_3 = await runPythonInDocker(dir1_3, { evaluationMode: 'function', entryFunction: 'solution', testCases: [{ input: 2, expected: 4 }] });
    check("1.3 IndentationError returns 0 with compilation error", res1_3.score === 0 && res1_3.passed === false);

    // -------------------------------------------------------------------------
    // SUITE 2: RUNTIME EXCEPTIONS (ZeroDivision, NameError, TypeError, Recursion)
    // -------------------------------------------------------------------------
    console.log("\n👉 SUITE 2: Student Runtime Exceptions");

    // Case 2.1: ZeroDivisionError
    const dir2_1 = path.join(tempRoot, 'case2_1_zerodiv');
    await fs.promises.mkdir(dir2_1, { recursive: true });
    await fs.promises.writeFile(path.join(dir2_1, 'main.py'), 'def divide(a, b):\n    return a / b\n', 'utf8');
    const res2_1 = await runPythonInDocker(dir2_1, {
      entryFunction: 'divide',
      evaluationMode: 'function',
      testCases: [
        { input: [10, 2], expected: 5.0 },
        { input: [10, 0], expected: 0 } // triggers ZeroDivisionError
      ]
    });
    check("2.1 ZeroDivisionError in 1 test case awards partial score (50%)", res2_1.score === 50 && res2_1.passedCount === 1);
    check("2.1b Failed test case details specify error", res2_1.results[1]?.error !== undefined);

    // Case 2.2: NameError (referencing undefined variable)
    const dir2_2 = path.join(tempRoot, 'case2_2_nameerror');
    await fs.promises.mkdir(dir2_2, { recursive: true });
    await fs.promises.writeFile(path.join(dir2_2, 'main.py'), 'print(total_score)\n', 'utf8');
    const res2_2 = await runPythonInDocker(dir2_2, { evaluationMode: 'script', expectedLogs: [100] });
    check("2.2 NameError in script mode returns 0 without crashing runner", res2_2.score === 0 && (res2_2.feedback || '').includes('Runtime error'));

    // Case 2.3: RecursionError (Infinite recursion)
    const dir2_3 = path.join(tempRoot, 'case2_3_recursion');
    await fs.promises.mkdir(dir2_3, { recursive: true });
    await fs.promises.writeFile(path.join(dir2_3, 'main.py'), 'def recurse(n):\n    return recurse(n + 1)\n', 'utf8');
    const res2_3 = await runPythonInDocker(dir2_3, { entryFunction: 'recurse', evaluationMode: 'function', testCases: [{ input: 1, expected: 100 }] });
    check("2.3 Infinite RecursionError handled without hanging runner", res2_3.score === 0 && res2_3.passed === false);

    // -------------------------------------------------------------------------
    // SUITE 3: COMPLEX TYPES, NESTED STRUCTURES & FLOATS
    // -------------------------------------------------------------------------
    console.log("\n👉 SUITE 3: Data Structures, Types & Precision");

    // Case 3.1: Float epsilon tolerance (e.g. 0.1 + 0.2 == 0.3)
    const dir3_1 = path.join(tempRoot, 'case3_1_float');
    await fs.promises.mkdir(dir3_1, { recursive: true });
    await fs.promises.writeFile(path.join(dir3_1, 'main.py'), 'def add_float(a, b):\n    return a + b\n', 'utf8');
    const res3_1 = await runPythonInDocker(dir3_1, { entryFunction: 'add_float', evaluationMode: 'function', testCases: [{ input: [0.1, 0.2], expected: 0.3 }] });
    check("3.1 Float precision discrepancy (0.30000000000000004 vs 0.3) matches", res3_1.score === 100 && res3_1.passed === true);

    // Case 3.2: Nested Dictionary & List Comparison
    const dir3_2 = path.join(tempRoot, 'case3_2_nested');
    await fs.promises.mkdir(dir3_2, { recursive: true });
    await fs.promises.writeFile(path.join(dir3_2, 'main.py'), `
def transform_user(user):
    return {
        "id": user["id"],
        "name": user["name"].title(),
        "tags": sorted(user.get("tags", [])),
        "active": True
    }
`, 'utf8');
    const res3_2 = await runPythonInDocker(dir3_2, {
      entryFunction: 'transform_user',
      evaluationMode: 'function',
      testCases: [{
        input: { id: 101, name: "saniya barabari", tags: ["sql", "python", "ai"] },
        expected: { id: 101, name: "Saniya Barabari", tags: ["ai", "python", "sql"], active: true }
      }]
    });
    check("3.2 Deep nested dict & list comparison passes", res3_2.score === 100 && res3_2.passed === true);

    // Case 3.3: Student returns boolean True vs numeric 1
    const dir3_3 = path.join(tempRoot, 'case3_3_bool');
    await fs.promises.mkdir(dir3_3, { recursive: true });
    await fs.promises.writeFile(path.join(dir3_3, 'main.py'), 'def is_even(n):\n    return n % 2 == 0\n', 'utf8');
    const res3_3 = await runPythonInDocker(dir3_3, {
      entryFunction: 'is_even',
      evaluationMode: 'function',
      testCases: [
        { input: 4, expected: true },
        { input: 5, expected: false }
      ]
    });
    check("3.3 Boolean return types match expected JSON booleans", res3_3.score === 100);

    // -------------------------------------------------------------------------
    // SUITE 4: SCRIPT MODE NOISY LOGS & FORMATTING TOLERANCE
    // -------------------------------------------------------------------------
    console.log("\n👉 SUITE 4: Noisy Script Outputs & Delimiter Parsing");

    // Case 4.1: Out-of-order logs with banner / header noise
    const dir4_1 = path.join(tempRoot, 'case4_1_noisy');
    await fs.promises.mkdir(dir4_1, { recursive: true });
    await fs.promises.writeFile(path.join(dir4_1, 'main.py'), `
print("************************************")
print("*     STUDENT REPORT SYSTEM        *")
print("************************************")
print("Total Students: 45")
print("Average Marks = 82.5")
print("Pass Rate is 94.2")
print("Debug: calculations completed.")
`, 'utf8');
    const res4_1 = await runPythonInDocker(dir4_1, {
      evaluationMode: 'script',
      expectedLogs: [45, 82.5, 94.2]
    });
    check("4.1 Banner headers and debug noise ignored, numeric values matched", res4_1.score === 100 && res4_1.passedCount === 3);

    // Case 4.2: Delimiters with hyphens and colons ("Item - Laptop", "Price: 54000")
    const dir4_2 = path.join(tempRoot, 'case4_2_delims');
    await fs.promises.mkdir(dir4_2, { recursive: true });
    await fs.promises.writeFile(path.join(dir4_2, 'main.py'), `
print("Product - MacBook Pro M2")
print("Price : 120000")
print("In Stock : True")
`, 'utf8');
    const res4_2 = await runPythonInDocker(dir4_2, {
      evaluationMode: 'script',
      expectedLogs: ["MacBook Pro M2", 120000, true]
    });
    check("4.2 Hyphen, colon, and spacing delimiters extracted cleanly", res4_2.score === 100 && res4_2.passedCount === 3);

    // -------------------------------------------------------------------------
    // SUITE 5: INTERACTIVE INPUT() MULTIPLE EDGE CASES
    // -------------------------------------------------------------------------
    console.log("\n👉 SUITE 5: Interactive input() Prompts");

    // Case 5.1: Multiple sequential inputs without prompts + empty fallback
    const dir5_1 = path.join(tempRoot, 'case5_1_inputs');
    await fs.promises.mkdir(dir5_1, { recursive: true });
    await fs.promises.writeFile(path.join(dir5_1, 'main.py'), `
val1 = input()
val2 = input()
val3 = input() # exhausted queue -> returns empty string
print(f"Val1={val1}, Val2={val2}, Val3={val3}")
`, 'utf8');
    const res5_1 = await runPythonInDocker(dir5_1, {
      evaluationMode: 'script',
      inputs: ["First", "Second"],
      expectedLogs: ["Val1=First, Val2=Second, Val3="]
    });
    check("5.1 Exhausted input queue gracefully yields empty string (no EOFError)", res5_1.score === 100);

    // Case 5.2: Student calls input() inside a function mode test
    const dir5_2 = path.join(tempRoot, 'case5_2_fn_input');
    await fs.promises.mkdir(dir5_2, { recursive: true });
    await fs.promises.writeFile(path.join(dir5_2, 'main.py'), `
def greet(name):
    extra = input("Optional note: ")
    return f"Hello {name} ({extra})"
`, 'utf8');
    const res5_2 = await runPythonInDocker(dir5_2, {
      entryFunction: 'greet',
      evaluationMode: 'function',
      testCases: [{ input: "Arjun", expected: "Hello Arjun ()" }]
    });
    check("5.2 Function unexpectedly calling input() does not hang or crash", res5_2.score === 100);

    // -------------------------------------------------------------------------
    // SUITE 6: ADVERSARIAL AST ATTACKS
    // -------------------------------------------------------------------------
    console.log("\n👉 SUITE 6: Adversarial Security & Injection Attempts");

    // Case 6.1: from os import system
    const dir6_1 = path.join(tempRoot, 'case6_1_importfrom');
    await fs.promises.mkdir(dir6_1, { recursive: true });
    await fs.promises.writeFile(path.join(dir6_1, 'main.py'), 'from os import system\nsystem("echo pwned")\n', 'utf8');
    const res6_1 = await runPythonInDocker(dir6_1, { evaluationMode: 'script', expectedLogs: ['test'] });
    check("6.1 'from os import system' blocked by AST guard", res6_1.score === 0 && (res6_1.error || '').includes("prohibited"));

    // Case 6.2: shutil.rmtree
    const dir6_2 = path.join(tempRoot, 'case6_2_shutil');
    await fs.promises.mkdir(dir6_2, { recursive: true });
    await fs.promises.writeFile(path.join(dir6_2, 'main.py'), 'import shutil\nshutil.rmtree("/tmp")\n', 'utf8');
    const res6_2 = await runPythonInDocker(dir6_2, { evaluationMode: 'script', expectedLogs: ['test'] });
    check("6.2 'shutil.rmtree' call blocked by AST guard", res6_2.score === 0 && (res6_2.error || '').includes("prohibited"));

    // -------------------------------------------------------------------------
    // SUITE 7: RUBRIC PEDAGOGY (Zero Contradiction & Partial Calibrations)
    // -------------------------------------------------------------------------
    console.log("\n👉 SUITE 7: Pedagogical Rubric Grading Fidelity");

    const sampleRubric = {
      criteria: [
        { name: "Syntax and Structure", weight: 30 },
        { name: "Algorithm Logic", weight: 40 },
        { name: "Output Formatting", weight: 30 }
      ]
    };

    // Case 7.1: Zero-score submission (completely wrong)
    const res7_1 = await evaluatePythonRubric("# No code here", sampleRubric, {
      passedCount: 0,
      totalCount: 3,
      score: 0,
      feedback: "All test cases failed."
    }, "job-zero", null);
    check("7.1 Zero score submission does not get praised as 'Great job'", !res7_1.summary.toLowerCase().includes("great job") && res7_1.finalScore <= 15);

    // Case 7.2: Partial credit submission (demonstrates functions and logic, but failed 1 test)
    const studentPartial = `
def calculate_discount(price, member=False):
    if member:
        return price * 0.8
    return price
`;
    const res7_2 = await evaluatePythonRubric(studentPartial, sampleRubric, {
      passedCount: 2,
      totalCount: 3,
      score: 67,
      feedback: "Failed test case with non-member price"
    }, "job-partial", null);
    check("7.2 Partial credit awards proportional marks and lists strengths & issues", res7_2.finalScore > 50 && res7_2.strengths.length > 0 && res7_2.issues.length > 0);

    // -------------------------------------------------------------------------
    // SUMMARY
    // -------------------------------------------------------------------------
    console.log("\n==========================================================================");
    console.log(`   EDGE CASE MATRIX COMPLETED: ${passedCount}/${totalCount} TESTS PASSED`);
    console.log("==========================================================================");

    if (failures.length > 0) {
      console.error("\n❌ FAILED TESTS SUMMARY:");
      failures.forEach(f => console.error(`  - ${f.testName}: ${f.details}`));
      process.exit(1);
    } else {
      console.log("🎉 ALL EDGE CASES PASSED WITH 100% SUCCESS RATE!\n");
    }
  } finally {
    try {
      await fs.promises.rm(tempRoot, { recursive: true, force: true });
    } catch {}
  }
}

runEdgeCaseMatrix().catch(err => {
  console.error("Test execution fatal error:", err);
  process.exit(1);
});
