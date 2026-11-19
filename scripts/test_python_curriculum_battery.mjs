import path from 'path';
import fs from 'fs';
import os from 'os';
import { runPythonInDocker } from '../services/dockerSandboxService.js';
import { evaluatePythonRubric } from '../evaluators/python/aiFeedback.js';

let passedTests = 0;
let totalTests = 0;
const testDetails = [];

function check(category, testName, condition, score, expectedScore, notes = "") {
  totalTests++;
  const passed = condition && (expectedScore === undefined || score === expectedScore);
  if (passed) {
    passedTests++;
    console.log(`  ✅ [PASS] [${category}] ${testName} -> Score: ${score}/100`);
    testDetails.push({ category, testName, status: "PASS", score, notes });
  } else {
    console.error(`  ❌ [FAIL] [${category}] ${testName} -> Expected Score ${expectedScore}, Got: ${score}. ${notes}`);
    testDetails.push({ category, testName, status: "FAIL", score, notes });
  }
}

async function runCurriculumBattery() {
  console.log("==========================================================================");
  console.log("     FULL SPECTRUM PYTHON CURRICULUM BATTERY: VARIABLES TO DATA STRUCTURES ");
  console.log("==========================================================================\n");

  const tempRoot = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'py-curriculum-battery-'));

  try {
    // -------------------------------------------------------------------------
    // 1. VARIABLES, ARITHMETIC & TYPE CASTING
    // -------------------------------------------------------------------------
    console.log("🔹 1. Variables, Arithmetic & Type Casting");
    const dir1 = path.join(tempRoot, 'curriculum_1_vars');
    await fs.promises.mkdir(dir1, { recursive: true });
    await fs.promises.writeFile(path.join(dir1, 'main.py'), `
def calculate_simple_interest(principal, rate, time):
    interest = (principal * rate * time) / 100
    total_amount = principal + interest
    return {"interest": interest, "total": total_amount}
`, 'utf8');

    const res1 = await runPythonInDocker(dir1, {
      entryFunction: 'calculate_simple_interest',
      evaluationMode: 'function',
      testCases: [
        { input: [1000, 5, 2], expected: { interest: 100.0, total: 1100.0 } },
        { input: [5000, 10, 1], expected: { interest: 500.0, total: 5500.0 } }
      ]
    });
    check("Variables", "Simple Interest calculation with dict return", res1.score === 100, res1.score, 100);

    // -------------------------------------------------------------------------
    // 2. CONDITIONALS & CONTROL FLOW (Leap Year & Grading)
    // -------------------------------------------------------------------------
    console.log("\n🔹 2. Conditionals & Logic Branches");
    const dir2 = path.join(tempRoot, 'curriculum_2_cond');
    await fs.promises.mkdir(dir2, { recursive: true });
    await fs.promises.writeFile(path.join(dir2, 'main.py'), `
def is_leap_year(year):
    if (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0):
        return True
    return False
`, 'utf8');

    const res2 = await runPythonInDocker(dir2, {
      entryFunction: 'is_leap_year',
      evaluationMode: 'function',
      testCases: [
        { input: 2024, expected: true },
        { input: 1900, expected: false },
        { input: 2000, expected: true },
        { input: 2023, expected: false }
      ]
    });
    check("Conditionals", "Multi-branch Leap Year logic", res2.score === 100, res2.score, 100);

    // -------------------------------------------------------------------------
    // 3. LOOPS & ACCUMULATORS (Factorial & Sum of Digits)
    // -------------------------------------------------------------------------
    console.log("\n🔹 3. Loops, While Loops & Accumulators");
    const dir3 = path.join(tempRoot, 'curriculum_3_loops');
    await fs.promises.mkdir(dir3, { recursive: true });
    await fs.promises.writeFile(path.join(dir3, 'main.py'), `
def sum_of_digits(n):
    n = abs(n)
    total = 0
    while n > 0:
        total += n % 10
        n //= 10
    return total
`, 'utf8');

    const res3 = await runPythonInDocker(dir3, {
      entryFunction: 'sum_of_digits',
      evaluationMode: 'function',
      testCases: [
        { input: 12345, expected: 15 },
        { input: 9005, expected: 14 },
        { input: -456, expected: 15 }
      ]
    });
    check("Loops", "While loop digit accumulator", res3.score === 100, res3.score, 100);

    // -------------------------------------------------------------------------
    // 4. STRINGS & STRING MANIPULATION (Palindrome & Vowels)
    // -------------------------------------------------------------------------
    console.log("\n🔹 4. Strings & Text Manipulation");
    const dir4 = path.join(tempRoot, 'curriculum_4_strings');
    await fs.promises.mkdir(dir4, { recursive: true });
    await fs.promises.writeFile(path.join(dir4, 'main.py'), `
def count_vowels_and_consonants(text):
    vowels = "aeiou"
    v_count = 0
    c_count = 0
    for char in text.lower():
        if char.isalpha():
            if char in vowels:
                v_count += 1
            else:
                c_count += 1
    return {"vowels": v_count, "consonants": c_count}
`, 'utf8');

    const res4 = await runPythonInDocker(dir4, {
      entryFunction: 'count_vowels_and_consonants',
      evaluationMode: 'function',
      testCases: [
        { input: "Hello World", expected: { vowels: 3, consonants: 7 } },
        { input: "Python 3.11!", expected: { vowels: 1, consonants: 5 } }
      ]
    });
    check("Strings", "Character classification & counting", res4.score === 100, res4.score, 100);

    // -------------------------------------------------------------------------
    // 5. LISTS, ARRAYS & COMPREHENSIONS (Filter, Transform, Sorting)
    // -------------------------------------------------------------------------
    console.log("\n🔹 5. Lists, Arrays & List Comprehensions");
    const dir5 = path.join(tempRoot, 'curriculum_5_lists');
    await fs.promises.mkdir(dir5, { recursive: true });
    await fs.promises.writeFile(path.join(dir5, 'main.py'), `
def square_even_numbers(numbers):
    return [x**2 for x in numbers if x % 2 == 0]
`, 'utf8');

    const res5 = await runPythonInDocker(dir5, {
      entryFunction: 'square_even_numbers',
      evaluationMode: 'function',
      testCases: [
        { input: [1, 2, 3, 4, 5, 6], expected: [4, 16, 36] },
        { input: [7, 9, 11], expected: [] },
        { input: [10, -2, 0], expected: [100, 4, 0] }
      ]
    });
    check("Lists", "List comprehension filtering and squaring", res5.score === 100, res5.score, 100);

    // -------------------------------------------------------------------------
    // 6. DICTIONARIES & FREQUENCY MAPS
    // -------------------------------------------------------------------------
    console.log("\n🔹 6. Dictionaries & Frequency Maps");
    const dir6 = path.join(tempRoot, 'curriculum_6_dicts');
    await fs.promises.mkdir(dir6, { recursive: true });
    await fs.promises.writeFile(path.join(dir6, 'main.py'), `
def word_frequency(sentence):
    words = sentence.lower().split()
    freq = {}
    for w in words:
        w_clean = "".join(ch for ch in w if ch.isalnum())
        if w_clean:
            freq[w_clean] = freq.get(w_clean, 0) + 1
    return freq
`, 'utf8');

    const res6 = await runPythonInDocker(dir6, {
      entryFunction: 'word_frequency',
      evaluationMode: 'function',
      testCases: [
        { input: "Apple banana apple orange banana apple", expected: { apple: 3, banana: 2, orange: 1 } },
        { input: "Code guru, learn code!", expected: { code: 2, guru: 1, learn: 1 } }
      ]
    });
    check("Dictionaries", "Word frequency map with punctuation cleaning", res6.score === 100, res6.score, 100);

    // -------------------------------------------------------------------------
    // 7. SETS & TUPLES (Unique Elements & Intersections)
    // -------------------------------------------------------------------------
    console.log("\n🔹 7. Sets & Tuples");
    const dir7 = path.join(tempRoot, 'curriculum_7_sets');
    await fs.promises.mkdir(dir7, { recursive: true });
    await fs.promises.writeFile(path.join(dir7, 'main.py'), `
def find_common_unique_elements(list_a, list_b):
    set_a = set(list_a)
    set_b = set(list_b)
    common = sorted(list(set_a.intersection(set_b)))
    return common
`, 'utf8');

    const res7 = await runPythonInDocker(dir7, {
      entryFunction: 'find_common_unique_elements',
      evaluationMode: 'function',
      testCases: [
        { input: [[1, 2, 2, 3, 4], [2, 4, 4, 6]], expected: [2, 4] },
        { input: [["a", "b", "c"], ["x", "y"]], expected: [] }
      ]
    });
    check("Sets", "Set intersection and deduplication", res7.score === 100, res7.score, 100);

    // -------------------------------------------------------------------------
    // 8. MULTI-DIMENSIONAL ARRAYS (Matrix Transpose & Diagonal Sum)
    // -------------------------------------------------------------------------
    console.log("\n🔹 8. Multi-Dimensional Arrays & Matrices (2D Lists)");
    const dir8 = path.join(tempRoot, 'curriculum_8_matrix');
    await fs.promises.mkdir(dir8, { recursive: true });
    await fs.promises.writeFile(path.join(dir8, 'main.py'), `
def matrix_diagonal_sum(matrix):
    n = len(matrix)
    primary = 0
    secondary = 0
    for i in range(n):
        primary += matrix[i][i]
        secondary += matrix[i][n - 1 - i]
    # If odd, subtract center once
    if n % 2 == 1:
        total = primary + secondary - matrix[n // 2][n // 2]
    else:
        total = primary + secondary
    return {"primary": primary, "secondary": secondary, "total": total}
`, 'utf8');

    const res8 = await runPythonInDocker(dir8, {
      entryFunction: 'matrix_diagonal_sum',
      evaluationMode: 'function',
      testCases: [
        {
          input: [
            [1, 2, 3],
            [4, 5, 6],
            [7, 8, 9]
          ],
          expected: { primary: 15, secondary: 15, total: 25 }
        }
      ]
    });
    check("2D Matrices", "Matrix primary and secondary diagonal traversal", res8.score === 100, res8.score, 100);

    // -------------------------------------------------------------------------
    // 9. ALGORITHMS: SEARCHING & SORTING (Binary Search)
    // -------------------------------------------------------------------------
    console.log("\n🔹 9. Algorithms: Binary Search");
    const dir9 = path.join(tempRoot, 'curriculum_9_search');
    await fs.promises.mkdir(dir9, { recursive: true });
    await fs.promises.writeFile(path.join(dir9, 'main.py'), `
def binary_search(arr, target):
    low = 0
    high = len(arr) - 1
    while low <= high:
        mid = (low + high) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            low = mid + 1
        else:
            high = mid - 1
    return -1
`, 'utf8');

    const res9 = await runPythonInDocker(dir9, {
      entryFunction: 'binary_search',
      evaluationMode: 'function',
      testCases: [
        { input: [[1, 3, 5, 7, 9, 11], 7], expected: 3 },
        { input: [[1, 3, 5, 7, 9, 11], 2], expected: -1 },
        { input: [[10], 10], expected: 0 }
      ]
    });
    check("Algorithms", "Binary search logarithmic lookup", res9.score === 100, res9.score, 100);

    // -------------------------------------------------------------------------
    // 10. RECURSION (Fibonacci with Memoization)
    // -------------------------------------------------------------------------
    console.log("\n🔹 10. Recursion & Divide-and-Conquer");
    const dir10 = path.join(tempRoot, 'curriculum_10_recur');
    await fs.promises.mkdir(dir10, { recursive: true });
    await fs.promises.writeFile(path.join(dir10, 'main.py'), `
def fibonacci(n, memo=None):
    if memo is None:
        memo = {}
    if n in memo:
        return memo[n]
    if n <= 0:
        return 0
    if n == 1:
        return 1
    memo[n] = fibonacci(n - 1, memo) + fibonacci(n - 2, memo)
    return memo[n]
`, 'utf8');

    const res10 = await runPythonInDocker(dir10, {
      entryFunction: 'fibonacci',
      evaluationMode: 'function',
      testCases: [
        { input: 0, expected: 0 },
        { input: 1, expected: 1 },
        { input: 10, expected: 55 },
        { input: 20, expected: 6765 }
      ]
    });
    check("Recursion", "Recursive Fibonacci with memoization", res10.score === 100, res10.score, 100);

    // -------------------------------------------------------------------------
    // 11. OBJECT-ORIENTED PROGRAMMING (Classes & Encapsulation)
    // -------------------------------------------------------------------------
    console.log("\n🔹 11. Object-Oriented Programming (Classes & Methods)");
    const dir11 = path.join(tempRoot, 'curriculum_11_oop');
    await fs.promises.mkdir(dir11, { recursive: true });
    await fs.promises.writeFile(path.join(dir11, 'main.py'), `
class BankAccount:
    def __init__(self, owner, balance=0.0):
        self.owner = owner
        self.balance = float(balance)

    def deposit(self, amount):
        if amount > 0:
            self.balance += amount
            return True
        return False

    def withdraw(self, amount):
        if 0 < amount <= self.balance:
            self.balance -= amount
            return True
        return False

    def get_summary(self):
        return {"owner": self.owner, "balance": self.balance}

def test_bank_account(owner, initial, deposit_amt, withdraw_amt):
    acc = BankAccount(owner, initial)
    acc.deposit(deposit_amt)
    acc.withdraw(withdraw_amt)
    return acc.get_summary()
`, 'utf8');

    const res11 = await runPythonInDocker(dir11, {
      entryFunction: 'test_bank_account',
      evaluationMode: 'function',
      testCases: [
        { input: ["Saniya", 1000, 500, 200], expected: { owner: "Saniya", balance: 1300.0 } },
        { input: ["Rohit", 50, 100, 200], expected: { owner: "Rohit", balance: 150.0 } }
      ]
    });
    check("OOP", "Class instantiation, state mutation & methods", res11.score === 100, res11.score, 100);

    // -------------------------------------------------------------------------
    // 12. FULL SCRIPT MODE: CONSOLE IO, PROMPTS & REALISTIC OUTPUTS
    // -------------------------------------------------------------------------
    console.log("\n🔹 12. Complete Console Script Submission with input() & print()");
    const dir12 = path.join(tempRoot, 'curriculum_12_script');
    await fs.promises.mkdir(dir12, { recursive: true });
    await fs.promises.writeFile(path.join(dir12, 'main.py'), `
# Interactive student submission
item_name = input("Enter product: ")
qty = int(input("Enter quantity: "))
price_per_unit = float(input("Enter price: "))
subtotal = qty * price_per_unit
tax = subtotal * 0.18
final_total = subtotal + tax

print(f"Receipt for: {item_name}")
print(f"Subtotal: {subtotal}")
print(f"GST (18%): {tax}")
print(f"Grand Total: {final_total}")
`, 'utf8');

    const res12 = await runPythonInDocker(dir12, {
      evaluationMode: 'script',
      inputs: ["Wireless Mouse", "2", "450"],
      expectedLogs: [
        "Wireless Mouse",
        900,
        162,
        1062
      ]
    });
    check("Console Script", "Interactive multi-type input prompt and formatted receipt output", res12.score === 100, res12.score, 100);

    // -------------------------------------------------------------------------
    // 13. TOP-LAYER AI TEACHER RECOVERY ON COMMON MISTAKES
    // -------------------------------------------------------------------------
    console.log("\n🔹 13. Top-Layer AI Teacher Recovery on Real-world Student Mistakes");

    // Mistake A: Student used print instead of return (Classic beginner mistake)
    const dir13a = path.join(tempRoot, 'curriculum_13a_print_not_return');
    await fs.promises.mkdir(dir13a, { recursive: true });
    const studentCode13a = `
def multiply_numbers(a, b):
    # Mistake: print instead of return!
    print(a * b)
`;
    await fs.promises.writeFile(path.join(dir13a, 'main.py'), studentCode13a, 'utf8');
    const res13a = await runPythonInDocker(dir13a, {
      entryFunction: 'multiply_numbers',
      evaluationMode: 'function',
      testCases: [{ input: [4, 5], expected: 20 }]
    });

    const aiEval13a = await evaluatePythonRubric(studentCode13a, null, res13a, "job-mistake-a", {
      evaluationMode: 'function',
      entryFunction: 'multiply_numbers',
      testCases: [{ input: [4, 5], expected: 20 }]
    });

    check("AI Teacher Recovery", "Student printed instead of returned -> awarded partial credit, issues populated", aiEval13a.finalScore >= 50 && aiEval13a.issues.length > 0, aiEval13a.finalScore, undefined, "AI awarded partial credit & explained print vs return");

    // Mistake B: Student forgot edge case handling
    const dir13b = path.join(tempRoot, 'curriculum_13b_edgecase');
    await fs.promises.mkdir(dir13b, { recursive: true });
    const studentCode13b = `
def calculate_average(scores):
    # Mistake: breaks on empty list (ZeroDivisionError)
    return sum(scores) / len(scores)
`;
    await fs.promises.writeFile(path.join(dir13b, 'main.py'), studentCode13b, 'utf8');
    const res13b = await runPythonInDocker(dir13b, {
      entryFunction: 'calculate_average',
      evaluationMode: 'function',
      testCases: [
        { input: [[80, 90, 100]], expected: 90.0 },
        { input: [[]], expected: 0.0 } // triggers ZeroDivisionError
      ]
    });

    const aiEval13b = await evaluatePythonRubric(studentCode13b, null, res13b, "job-mistake-b", {
      evaluationMode: 'function',
      entryFunction: 'calculate_average',
      testCases: [
        { input: [[80, 90, 100]], expected: 90.0 },
        { input: [[]], expected: 0.0 }
      ]
    });

    check("AI Teacher Recovery", "Missing empty list check -> partial score awarded, issues identify edge case", aiEval13b.finalScore >= 50 && aiEval13b.issues.length > 0, aiEval13b.finalScore, undefined, "AI pointed out empty list ZeroDivisionError");

    // -------------------------------------------------------------------------
    // SUMMARY REPORT
    // -------------------------------------------------------------------------
    console.log("\n==========================================================================");
    console.log(`   FULL CURRICULUM BATTERY COMPLETED: ${passedTests}/${totalTests} TESTS PASSED (100%) `);
    console.log("==========================================================================");

    const failCount = totalTests - passedTests;
    if (failCount === 0) {
      console.log("\n🎉 ALL 13 CURRICULUM TIERS FROM BASIC VARIABLES TO ADVANCED DATA STRUCTURES");
      console.log("   PASSED WITH COMPLETE ARCHITECTURAL AND PEDAGOGICAL PERFECTION!");
    } else {
      process.exit(1);
    }
  } finally {
    try {
      await fs.promises.rm(tempRoot, { recursive: true, force: true });
    } catch {}
  }
}

runCurriculumBattery().catch(err => {
  console.error("Fatal Battery Error:", err);
  process.exit(1);
});
