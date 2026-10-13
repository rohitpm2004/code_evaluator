import { exec } from "child_process";
import util from "util";
import path from "path";
import fs from "fs";
import crypto from "crypto";
import logger from "../config/logger.js";

const execPromise = util.promisify(exec);

let dockerAvailableCache = null;

/**
 * Checks if Docker daemon is running and reachable.
 * Once verified positive, retains cached status to prevent concurrent timeouts under load.
 */
export async function isDockerAvailable() {
  if (dockerAvailableCache === true) {
    return true;
  }

  try {
    await execPromise("docker info", { timeout: 30000 });
    dockerAvailableCache = true;
    return true;
  } catch (err) {
    logger.warn(`Docker not reachable: ${err.message}. Falling back to local execution.`);
    dockerAvailableCache = false;
    return false;
  }
}

/**
 * Executes a student project build/test safely inside an isolated Docker container.
 *
 * Security & Performance features:
 * - Isolated filesystem (mounted workspace)
 * - Memory & CPU capped (--memory=1g --cpus=1.5)
 * - Persistent cache named volume
 * - Hard execution timeout (45s) to kill infinite loops
 *
 * @param {string} repoPath - Local path to the cloned repository
 * @param {Object} options - { timeoutMs, command, language }
 * @returns {Promise<{ success: boolean, testOutput: string, durationMs: number }>}
 */
export async function runInDockerSandbox(repoPath, options = {}) {
  const startTime = Date.now();
  const timeoutMs = options.timeoutMs || 60000;
  const language = options.language || "node";
  
  // Normalize Windows paths for Docker volume mounting (e.g. C:/Users/...:/app)
  const normalizedPath = path.resolve(repoPath).replace(/\\/g, "/");

  let containerCmd = options.command;
  let dockerImage = "node:18-alpine";
  let cacheVolume = "central-eval-npm-cache:/root/.npm";

  if (language === "python") {
    dockerImage = "python:3.11-alpine";
    cacheVolume = "central-eval-pip-cache:/root/.cache/pip";
    if (!containerCmd) {
      const hasReqs = fs.existsSync(path.join(repoPath, "requirements.txt"));
      containerCmd = hasReqs
        ? "pip install -r requirements.txt --prefer-binary && pytest || python -m unittest"
        : "python -m unittest || pytest";
    }
  } else {
    // Node / React / Backend / Fullstack / Visual default
    if (!containerCmd) {
      const packageJsonPath = path.join(repoPath, "package.json");
      let hasBuildScript = true;
      if (fs.existsSync(packageJsonPath)) {
        try {
          const pkg = JSON.parse(fs.readFileSync(packageJsonPath, "utf-8"));
          hasBuildScript = Boolean(pkg.scripts?.build);
        } catch {
          // Ignore JSON parse errors
        }
      }

      containerCmd = hasBuildScript
        ? "npm install --prefer-offline --no-audit --progress=false && npm run build"
        : "npm install --prefer-offline --no-audit --progress=false";
    }
  }

  const containerName = `eval-sandbox-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`;
  const dockerCmd = `docker run --name ${containerName} --rm --memory="1g" --cpus="1.5" -v "${normalizedPath}:/app" -v /app/node_modules -v "${cacheVolume}" -w /app ${dockerImage} sh -c "${containerCmd}"`;

  logger.info(`🐳 Starting Docker sandbox (${language}) execution for: ${normalizedPath} (container: ${containerName})`);

  try {
    const { stdout, stderr } = await execPromise(dockerCmd, {
      timeout: timeoutMs,
      maxBuffer: 10 * 1024 * 1024,
    });

    const durationMs = Date.now() - startTime;
    logger.info(`✅ Docker sandbox build PASSED in ${durationMs}ms`);

    const outputLog = [
      "Build: Success.",
      stdout ? `\nBuild Output:\n${stdout.slice(0, 3000)}` : "",
      stderr ? `\nBuild Notices:\n${stderr.slice(0, 1000)}` : ""
    ].join("");

    return {
      success: true,
      testOutput: outputLog,
      durationMs
    };
  } catch (err) {
    execPromise(`docker rm -f ${containerName}`).catch(() => {});
    const durationMs = Date.now() - startTime;
    logger.warn(`❌ Docker sandbox build FAILED in ${durationMs}ms: ${err.message}`);

    const isTimeout = err.killed || (err.message && err.message.toLowerCase().includes("timed out"));
    const errorOutput = [
      isTimeout
        ? "Build Notice: Build verification timed out during sandbox dependency resolution. Proceeding with static code evaluation."
        : "Build: Failed. The application could not be compiled.",
      err.stdout ? `\nStdout:\n${err.stdout.slice(0, 2000)}` : "",
      err.stderr ? `\nStderr:\n${err.stderr.slice(0, 2000)}` : "",
      `\nError:\n${err.message}`
    ].join("");

    return {
      success: false,
      testOutput: errorOutput,
      durationMs
    };
  }
}

const PRIORITY_ENTRY_FILES = [
  'index.js',
  'script.js',
  'main.js',
  'app.js',
  'solution.js',
  'assignment.js',
  'task.js'
];

const CONFIG_FILE_PATTERNS = [
  /\.config\.(js|cjs|mjs)$/i,
  /^(webpack|vite|babel|postcss|tailwind|jest|karma|rollup|grunt|gulp)\./i,
  /\.min\.js$/i
];

function isConfigFile(filename) {
  return CONFIG_FILE_PATTERNS.some(pat => pat.test(filename));
}

/**
 * Recursively finds target file in a directory matching a predicate, excluding node_modules/git.
 * Prioritizes standard entry files (index.js, script.js, main.js, etc.) and ignores tooling configs.
 */
function findTargetFile(dir, predicate, maxDepth = 3, currentDepth = 0) {
  if (currentDepth > maxDepth || !fs.existsSync(dir)) return null;

  // 1. Check for priority entry files directly in this directory first
  for (const prio of PRIORITY_ENTRY_FILES) {
    const directPrio = path.join(dir, prio);
    if (fs.existsSync(directPrio) && fs.statSync(directPrio).isFile()) {
      return directPrio;
    }
  }

  const entries = fs.readdirSync(dir, { withFileTypes: true });
  entries.sort((a, b) => a.name.localeCompare(b.name));

  // 2. Check all direct files in current directory matching predicate (excluding configs)
  for (const entry of entries) {
    if (entry.isFile() && !entry.name.startsWith('.') && !isConfigFile(entry.name) && predicate(entry.name)) {
      return path.join(dir, entry.name);
    }
  }

  // 3. Traverse into subdirectories
  for (const entry of entries) {
    if (entry.isDirectory()) {
      if (entry.name.startsWith('.') || entry.name === 'node_modules' || entry.name === 'coverage' || entry.name === 'test' || entry.name === 'tests' || entry.name === 'dist' || entry.name === 'build') continue;
      const fullPath = path.join(dir, entry.name);
      const nested = findTargetFile(fullPath, predicate, maxDepth, currentDepth + 1);
      if (nested) return nested;
    }
  }
  return null;
}

/**
 * Runs JavaScript algorithm / function test cases inside a safe Node container.
 */
export async function runJsInDocker(repoPath, options = {}) {
  const normalizedPath = path.resolve(repoPath).replace(/\\/g, "/");

  let testCases = options.testCases || [];
  while (typeof testCases === 'string') {
    try { testCases = JSON.parse(testCases); } catch { break; }
  }
  let expectedLogs = options.expectedLogs || [];
  while (typeof expectedLogs === 'string') {
    try { expectedLogs = JSON.parse(expectedLogs); } catch { break; }
  }
  let functions = options.functions || null;
  while (typeof functions === 'string') {
    try { functions = JSON.parse(functions); } catch { break; }
  }

  // Auto-detect evaluationMode if not provided
  let evaluationMode = options.evaluationMode;
  if (!evaluationMode) {
    if (Array.isArray(expectedLogs) && expectedLogs.length > 0 && (!Array.isArray(testCases) || testCases.length === 0)) {
      evaluationMode = 'script';
    } else if (Array.isArray(functions) && functions.length > 0) {
      evaluationMode = 'multi-function';
    } else {
      evaluationMode = 'function';
    }
  }

  let entryFunction = options.entryFunction || null;
  
  // Find primary JS file (check explicit options.entryFile first, then auto-discover)
  let fullTarget = null;
  if (options.entryFile) {
    const candidate = path.resolve(repoPath, options.entryFile);
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      fullTarget = candidate;
    }
  }
  if (!fullTarget) {
    fullTarget = findTargetFile(repoPath, f => f.endsWith(".js") && !f.includes("test") && !f.includes("spec") && !f.startsWith("."))
      || path.join(repoPath, "index.js");
  }
  const relativeTarget = path.relative(repoPath, fullTarget).replace(/\\/g, "/");

  // Write test cases & configuration to a separate JSON file on disk
  const testCasesFileName = `.eval_tc_${Date.now()}_${crypto.randomBytes(3).toString('hex')}.json`;
  const testCasesFilePath = path.join(repoPath, testCasesFileName);
  fs.writeFileSync(testCasesFilePath, JSON.stringify({
    testCases: Array.isArray(testCases) ? testCases : [],
    entryFunction,
    evaluationMode,
    expectedLogs: Array.isArray(expectedLogs) ? expectedLogs : [],
    functions: Array.isArray(functions) ? functions : null
  }), "utf-8");

  // Create harness script on disk
  const harnessFileName = `.eval_harness_${Date.now()}_${crypto.randomBytes(3).toString('hex')}.mjs`;
  const harnessFilePath = path.join(repoPath, harnessFileName);

  const harnessCode = `
import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { pathToFileURL } from 'url';
import { createRequire } from 'module';

(async () => {
  const config = JSON.parse(fs.readFileSync(path.resolve('./${testCasesFileName}'), 'utf8'));
  const targetRel = './${relativeTarget}';
  const targetAbs = path.resolve(targetRel);

  let capturedLogs = [];
  const logPush = (...args) => {
    const line = args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ');
    capturedLogs.push(line);
  };
  const customConsole = {
    log: logPush,
    info: logPush,
    warn: logPush,
    error: logPush
  };

  const mockPrompt = (msg) => {
    logPush('[prompt: ' + (msg || '') + ']');
    return '';
  };
  const mockAlert = (...args) => logPush(...args);
  const mockConfirm = () => true;
  const mockWindow = {
    alert: mockAlert,
    prompt: mockPrompt,
    confirm: mockConfirm,
    console: customConsole,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval
  };
  const mockDocument = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    write: (...args) => logPush(...args),
    writeln: (...args) => logPush(...args),
    createElement: () => ({ style: {}, appendChild: () => {}, addEventListener: () => {} }),
    body: { appendChild: () => {} }
  };

  const createSafeSandbox = (extra = {}) => {
    const base = {
      console: customConsole,
      process,
      require: createRequire(import.meta.url),
      prompt: mockPrompt,
      alert: mockAlert,
      confirm: mockConfirm,
      window: mockWindow,
      document: mockDocument,
      setTimeout,
      clearTimeout,
      setInterval,
      clearInterval,
      ...extra
    };
    base.global = base;
    base.globalThis = base;
    return base;
  };

  const outputResult = (data) => {
    console.log('__EVAL_RESULT_START__' + JSON.stringify(data) + '__EVAL_RESULT_END__');
  };

  // 1. Script mode evaluation
  if (config.evaluationMode === 'script') {
    try {
      const code = fs.readFileSync(targetAbs, 'utf8');
      const sandbox = createSafeSandbox();
      vm.createContext(sandbox);
      vm.runInContext(code, sandbox, { timeout: 30000 });
      
      const expected = config.expectedLogs || [];
      const cleanCaptured = capturedLogs.flatMap(l => String(l).split('\\n')).map(s => s.trim()).filter(s => s.length > 0);
      
      const isValueMatch = (actualLine, expVal) => {
        if (actualLine === undefined || actualLine === null) return false;
        const strActual = String(actualLine).trim();
        const strExp = String(expVal).trim();
        if (!strExp) return false;
        
        // 1. Direct exact or case-insensitive match
        if (strActual === strExp || strActual.toLowerCase() === strExp.toLowerCase()) return true;

        // 2. Delimiter extraction: e.g. "Full Name: Rohit Sharma", "Age = 25", "name - Rohit Sharma", "Course is B.Tech"
        const delimiterMatch = strActual.match(/^[^:=]+(?::\s*|=\s*|-\s*|\bis\b\s*)(.+)$/i);
        if (delimiterMatch) {
          const extracted = delimiterMatch[1].trim();
          if (extracted === strExp || extracted.toLowerCase() === strExp.toLowerCase()) return true;
          const numExtracted = parseFloat(extracted);
          const numExp = parseFloat(strExp);
          if (!isNaN(numExtracted) && !isNaN(numExp) && Math.abs(numExtracted - numExp) < 0.0001) return true;
        }

        // 3. Substring match for meaningful string values (length >= 3)
        // e.g. expected is "Rohit Sharma", student prints "Full Name: Rohit Sharma, Age: 20" or "My name is Rohit Sharma"
        if (strExp.length >= 3 && strActual.toLowerCase().includes(strExp.toLowerCase())) {
          return true;
        }

        // 4. Number token check (e.g. expected is 20 or 11.6, actual line contains token 20 or 11.6)
        const numExp = parseFloat(strExp);
        if (!isNaN(numExp) && !isNaN(Number(strExp))) {
          const tokens = strActual.split(/[\s,;:=-]+/);
          for (const token of tokens) {
            const numToken = parseFloat(token);
            if (!isNaN(numToken) && Math.abs(numToken - numExp) < 0.0001) return true;
          }
        }

        // 5. Boolean token check
        if (strExp.toLowerCase() === 'true' || strExp.toLowerCase() === 'false') {
          const tokens = strActual.toLowerCase().split(/[\s,;:=-]+/);
          if (tokens.includes(strExp.toLowerCase())) return true;
        }

        return false;
      };

      // Two-pass matching:
      // Pass 1: Direct 1-to-1 index matching
      // Pass 2: Unused lines search for any remaining test cases (handles out-of-order logs, debug logs, or banners)
      const usedIndices = new Set();
      const matchMap = new Map();

      expected.forEach((exp, expIdx) => {
        if (cleanCaptured[expIdx] !== undefined && isValueMatch(cleanCaptured[expIdx], exp)) {
          usedIndices.add(expIdx);
          matchMap.set(expIdx, expIdx);
        }
      });

      expected.forEach((exp, expIdx) => {
        if (!matchMap.has(expIdx)) {
          for (let i = 0; i < cleanCaptured.length; i++) {
            if (!usedIndices.has(i) && isValueMatch(cleanCaptured[i], exp)) {
              usedIndices.add(i);
              matchMap.set(expIdx, i);
              break;
            }
          }
        }
      });

      let passedCount = 0;
      const details = expected.map((exp, expIdx) => {
        if (matchMap.has(expIdx)) {
          passedCount++;
          const actualIdx = matchMap.get(expIdx);
          return { passed: true, expected: String(exp).trim(), actual: cleanCaptured[actualIdx] };
        } else {
          const fallbackActual = cleanCaptured[expIdx] !== undefined ? cleanCaptured[expIdx] : '';
          return { passed: false, expected: String(exp).trim(), actual: fallbackActual };
        }
      });
      const passRatio = expected.length > 0 ? passedCount / expected.length : 1;
      const score = Math.round(passRatio * 100);
      outputResult({ 
        passed: score === 100, 
        score, 
        results: details, 
        logs: cleanCaptured, 
        passedCount, 
        totalCount: expected.length,
        feedback: 'Passed ' + passedCount + ' of ' + expected.length + ' test cases'
      });
      process.exit(0);
    } catch(e) {
      outputResult({ 
        passed: false, 
        score: 0, 
        error: e.message, 
        logs: capturedLogs, 
        passedCount: 0, 
        totalCount: (config.expectedLogs || []).length || 1,
        results: []
      });
      process.exit(0);
    }
  }

  // 2. Load module via ES import, CommonJS require, or VM fallback
  let mod = {};
  try {
    const fileUrl = pathToFileURL(targetAbs).href;
    mod = await import(fileUrl);
  } catch(e) {
    try {
      const require = createRequire(import.meta.url);
      mod = require(targetAbs);
    } catch(e2) {
      try {
        const code = fs.readFileSync(targetAbs, 'utf8');
        const sandbox = createSafeSandbox({ module: { exports: {} }, exports: {} });
        vm.createContext(sandbox);
        vm.runInContext(code, sandbox, { timeout: 30000 });
        mod = { ...sandbox, ...(sandbox.module?.exports || {}), ...(sandbox.exports || {}) };
      } catch(e3) {
        outputResult({ error: e3.message, results: [] });
        process.exit(0);
      }
    }
  }

  const resolveFn = (name) => {
    if (name && typeof mod[name] === 'function') return mod[name];
    if (name && mod.default && typeof mod.default[name] === 'function') return mod.default[name];
    if (mod.default && typeof mod.default === 'function') return mod.default;
    if (typeof mod === 'function') return mod;

    // Smart auto-discovery if explicit name not found or default 'solution' was used
    const builtins = new Set(['console', 'process', 'require', 'module', 'exports', 'path', 'fs']);
    for (const key of Object.keys(mod)) {
      if (!builtins.has(key) && typeof mod[key] === 'function') return mod[key];
    }
    if (mod.default && typeof mod.default === 'object' && mod.default !== null) {
      for (const key of Object.keys(mod.default)) {
        if (!builtins.has(key) && typeof mod.default[key] === 'function') return mod.default[key];
      }
    }
    return null;
  };

  const evaluateCases = (fn, cases) => {
    return cases.map(tc => {
      try {
        const inp = tc.input;
        let actual;
        if (Array.isArray(inp) && fn.length > 1) {
          actual = fn(...inp);
        } else if (inp !== undefined) {
          actual = fn(inp);
        } else {
          actual = fn();
        }
        
        let passed = false;
        if (actual === tc.expected) {
          passed = true;
        } else if (typeof actual === 'number' && typeof tc.expected === 'number' && Math.abs(actual - tc.expected) < 0.0001) {
          passed = true;
        } else if (String(actual).trim() === String(tc.expected).trim()) {
          passed = true;
        } else {
          try {
            passed = JSON.stringify(actual) === JSON.stringify(tc.expected);
          } catch {}
        }
        return { passed, input: tc.input, expected: tc.expected, actual };
      } catch(err) {
        return { passed: false, input: tc.input, expected: tc.expected, error: err.message };
      }
    });
  };

  // Multi-function mode
  if (config.functions && Array.isArray(config.functions)) {
    let allResults = [];
    for (const fnObj of config.functions) {
      const fn = resolveFn(fnObj.name);
      if (!fn) {
        allResults.push(...(fnObj.testCases || []).map(tc => ({ passed: false, input: tc.input, expected: tc.expected, error: 'Function ' + fnObj.name + ' not found' })));
      } else {
        allResults.push(...evaluateCases(fn, fnObj.testCases || []));
      }
    }
    const passedCount = allResults.filter(r => r.passed).length;
    const totalCount = allResults.length;
    const score = totalCount > 0 ? Math.round((passedCount / totalCount) * 100) : 0;
    outputResult({ results: allResults, score, passedCount, totalCount, passed: score === 100 });
    process.exit(0);
  }

  // Single function mode
  const entryFunction = config.entryFunction || 'solution';
  const fn = resolveFn(entryFunction);
  if (typeof fn !== 'function') {
    outputResult({ error: 'Function ' + (entryFunction || 'target') + ' not found', results: [] });
    process.exit(0);
  }

  const results = evaluateCases(fn, config.testCases || []);
  const passedCount = results.filter(r => r.passed).length;
  const totalCount = results.length;
  const score = totalCount > 0 ? Math.round((passedCount / totalCount) * 100) : 0;
  outputResult({ results, score, passedCount, totalCount, passed: score === 100 });
})();
`;

  fs.writeFileSync(harnessFilePath, harnessCode, "utf-8");
  const containerName = `eval-js-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`;
  const dockerCmd = `docker run --name ${containerName} --rm --memory="512m" --cpus="1.0" -v "${normalizedPath}:/app" -w /app node:18-alpine node ${harnessFileName}`;

  let studentCode = "";
  try {
    studentCode = fs.readFileSync(fullTarget, "utf-8");
  } catch {}

  try {
    const { stdout } = await execPromise(dockerCmd, { timeout: 60000 });
    const match = stdout.match(/__EVAL_RESULT_START__([\s\S]*?)__EVAL_RESULT_END__/);
    const rawJson = match ? match[1].trim() : stdout.trim();
    const parsed = JSON.parse(rawJson);
    return { ...parsed, studentCode };
  } catch (err) {
    execPromise(`docker rm -f ${containerName}`).catch(() => {});
    logger.error(`Docker JS test runner infrastructure error: ${err.message}`);
    throw new Error(`Docker JS execution failed: ${err.message}`);
  } finally {
    try { fs.unlinkSync(harnessFilePath); } catch {}
    try { fs.unlinkSync(testCasesFilePath); } catch {}
  }
}

/**
 * Runs Python algorithm / function test cases inside a safe Python container.
 */
export async function runPythonInDocker(repoPath, { testCases = [], entryFunction = "solution", evaluationMode = "function" }) {
  const normalizedPath = path.resolve(repoPath).replace(/\\/g, "/");

  // Find primary Python file (root or src/)
  const fullTarget = findTargetFile(repoPath, f => f.endsWith(".py") && !f.includes("test") && !f.includes("spec") && !f.startsWith("."))
    || path.join(repoPath, "main.py");
  const moduleName = path.basename(fullTarget, ".py");
  const relativeTarget = path.relative(repoPath, fullTarget).replace(/\\/g, "/");

  const testCasesFileName = `.eval_py_tc_${Date.now()}_${crypto.randomBytes(3).toString('hex')}.json`;
  const testCasesFilePath = path.join(repoPath, testCasesFileName);
  fs.writeFileSync(testCasesFilePath, JSON.stringify(Array.isArray(testCases) ? testCases : []), "utf-8");

  const harnessFileName = `.eval_harness_${Date.now()}_${crypto.randomBytes(3).toString('hex')}.py`;
  const harnessFilePath = path.join(repoPath, harnessFileName);

  const harnessPython = `
import json, sys, os, inspect

try:
    with open('${testCasesFileName}', 'r', encoding='utf-8') as f:
        test_cases = json.load(f)
except Exception as e:
    print(json.dumps({'error': 'Failed to load test cases: ' + str(e), 'results': []}))
    sys.exit(0)

fn = None
try:
    import ${moduleName} as student_module
    fn = getattr(student_module, '${entryFunction}', None)
    if fn is None and hasattr(student_module, 'default'):
        fn = student_module.default
except Exception:
    pass

def output_result(data):
    print('__EVAL_RESULT_START__' + json.dumps(data) + '__EVAL_RESULT_END__')

if fn is None:
    try:
        with open('${relativeTarget}', 'r', encoding='utf-8') as f:
            code = f.read()
        ns = {}
        exec(code, ns)
        fn = ns.get('${entryFunction}')
    except Exception as e:
        output_result({'error': 'Failed to load function: ' + str(e), 'results': []})
        sys.exit(0)

if not callable(fn):
    output_result({'error': 'Function ${entryFunction} not found', 'results': []})
    sys.exit(0)

try:
    sig = inspect.signature(fn)
    num_params = len(sig.parameters)
except Exception:
    num_params = 1

results = []
for tc in test_cases:
    try:
        inp = tc.get('input')
        expected = tc.get('expected')
        if isinstance(inp, list) and num_params > 1:
            actual = fn(*inp)
        elif inp is not None:
            actual = fn(inp)
        else:
            actual = fn()
        passed = (actual == expected)
        results.append({'passed': passed, 'input': inp, 'expected': expected, 'actual': actual})
    except Exception as err:
        results.append({'passed': False, 'input': tc.get('input'), 'expected': tc.get('expected'), 'error': str(err)})

output_result({'results': results})
`;

  fs.writeFileSync(harnessFilePath, harnessPython, "utf-8");
  const containerName = `eval-py-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`;
  const dockerCmd = `docker run --name ${containerName} --rm --memory="512m" --cpus="1.0" -v "${normalizedPath}:/app" -w /app python:3.11-alpine python ${harnessFileName}`;

  try {
    const { stdout } = await execPromise(dockerCmd, { timeout: 15000 });
    const match = stdout.match(/__EVAL_RESULT_START__([\s\S]*?)__EVAL_RESULT_END__/);
    const rawJson = match ? match[1].trim() : stdout.trim();
    const parsed = JSON.parse(rawJson);
    return parsed.results || [];
  } catch (err) {
    execPromise(`docker rm -f ${containerName}`).catch(() => {});
    logger.error(`Docker Python test runner infrastructure error: ${err.message}`);
    throw new Error(`Docker Python execution failed: ${err.message}`);
  } finally {
    try { fs.unlinkSync(harnessFilePath); } catch {}
    try { fs.unlinkSync(testCasesFilePath); } catch {}
  }
}

/**
 * Runs Excel workbook recalculation & assertion evaluation inside Docker sandbox.
 */
export async function runExcelInDocker(workspaceDir, options = {}) {
  const timeoutMs = options.timeoutMs || 45000;
  const normalizedPath = path.resolve(workspaceDir).replace(/\\/g, "/");
  const containerName = `eval-excel-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`;
  const dockerImage = process.env.EXCEL_SANDBOX_IMAGE || "eval-excel-sandbox";

  const dockerCmd = `docker run --name ${containerName} --entrypoint "" --rm --network none --memory="768m" --cpus="1.0" -v "${normalizedPath}:/workspace" -w /workspace ${dockerImage} sh -c "python3 /sandbox/recalculate.py --input /workspace/original_submission.xlsx --output /workspace/recalculated_submission.xlsx --profile-dir /tmp/lo_prof && python3 /sandbox/excel_runner.py --original /workspace/original_submission.xlsx --recalculated /workspace/recalculated_submission.xlsx --spec /workspace/test_spec.json"`;

  logger.info(`🐳 Starting Excel Docker sandbox execution in ${containerName}`);
  try {
    const { stdout } = await execPromise(dockerCmd, { timeout: timeoutMs });
    return JSON.parse(stdout.trim());
  } catch (err) {
    execPromise(`docker rm -f ${containerName}`).catch(() => {});
    throw new Error(`Docker Excel execution failed: ${err.message}`);
  }
}
