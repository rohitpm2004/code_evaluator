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
  let dockerImage = options.image || (language === "python" ? "python:3.11-alpine" : (process.env.NODE_SANDBOX_IMAGE || "node:20-slim"));
  let cacheVolume = language === "python" ? "central-eval-pip-cache:/root/.cache/pip" : "central-eval-npm-cache:/root/.npm";

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
        ? "npm install --legacy-peer-deps --ignore-engines --no-audit --progress=false && npm run build"
        : "npm install --legacy-peer-deps --ignore-engines --no-audit --progress=false";
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
  const dockerCmd = `docker run --name ${containerName} --rm --memory="512m" --cpus="1.0" -v "${normalizedPath}:/app" -w /app node:20-slim node ${harnessFileName}`;

  let studentCode = "";
  try {
    studentCode = fs.readFileSync(fullTarget, "utf-8");
  } catch {}

  try {
    const { stdout } = await execPromise(dockerCmd, { timeout: 20000 });
    const match = stdout.match(/__EVAL_RESULT_START__([\s\S]*?)__EVAL_RESULT_END__/);
    const rawJson = match ? match[1].trim() : stdout.trim();
    const parsed = JSON.parse(rawJson);
    return { ...parsed, studentCode };
  } catch (err) {
    execPromise(`docker rm -f ${containerName}`).catch(() => {});
    const isTimeout = err.killed || (err.message && err.message.toLowerCase().includes('timed out'));
    if (isTimeout) {
      logger.warn(`Docker JS execution timed out in ${containerName} (infinite loop detected).`);
      return {
        results: [{
          passed: false,
          error: "Execution timed out (possible infinite loop or excessive computation). Ensure all loops have valid termination conditions."
        }],
        score: 0,
        passedCount: 0,
        totalCount: 1,
        passed: false,
        feedback: "Execution timed out after exceeding maximum time limit (20s). Check for infinite loops (e.g. while loops without exit conditions or non-terminating recursion).",
        error: "Execution timed out (possible infinite loop).",
        studentCode
      };
    }
    logger.error(`Docker JS test runner infrastructure error: ${err.message}`);
    throw new Error(`Docker JS execution failed: ${err.message}`);
  } finally {
    try { fs.unlinkSync(harnessFilePath); } catch {}
    try { fs.unlinkSync(testCasesFilePath); } catch {}
  }
}

/**
 * Runs Python algorithm / function / script test cases inside a safe Python container.
 * Supports:
 *   1. 'script' mode: console/print output capture with fuzzy matching & mock input()
 *   2. 'function' mode: unit test assertions on exported or defined functions
 *   3. 'multi-function' mode: unit tests across multiple functions
 */
export async function runPythonInDocker(repoPath, options = {}) {
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
  let inputs = options.inputs || [];
  while (typeof inputs === 'string') {
    try { inputs = JSON.parse(inputs); } catch { break; }
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

  // Find primary Python file (check explicit options.entryFile first, then auto-discover)
  let fullTarget = null;
  if (options.entryFile) {
    const candidate = path.resolve(repoPath, options.entryFile);
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) {
      fullTarget = candidate;
    }
  }
  if (!fullTarget) {
    fullTarget = findTargetFile(repoPath, f => f.endsWith(".py") && !f.includes("test") && !f.includes("spec") && !f.startsWith("."))
      || path.join(repoPath, "main.py");
  }
  const moduleName = path.basename(fullTarget, ".py");
  const relativeTarget = path.relative(repoPath, fullTarget).replace(/\\/g, "/");

  // Read student code for AST pre-flight safety & LLM feedback context
  let studentCode = "";
  try {
    studentCode = fs.readFileSync(fullTarget, "utf-8");
  } catch {}

  // Write test cases & configuration to a separate JSON file on disk
  const testCasesFileName = `.eval_py_tc_${Date.now()}_${crypto.randomBytes(3).toString('hex')}.json`;
  const testCasesFilePath = path.join(repoPath, testCasesFileName);
  fs.writeFileSync(testCasesFilePath, JSON.stringify({
    testCases: Array.isArray(testCases) ? testCases : [],
    entryFunction,
    evaluationMode,
    expectedLogs: Array.isArray(expectedLogs) ? expectedLogs : [],
    functions: Array.isArray(functions) ? functions : null,
    inputs: Array.isArray(inputs) ? inputs : (inputs !== undefined && inputs !== null ? [inputs] : [])
  }), "utf-8");

  // Create Python harness script on disk
  const harnessFileName = `.eval_harness_${Date.now()}_${crypto.randomBytes(3).toString('hex')}.py`;
  const harnessFilePath = path.join(repoPath, harnessFileName);

  const harnessPython = `
import json, sys, os, inspect, io, re, builtins

def output_result(data):
    print('__EVAL_RESULT_START__' + json.dumps(data) + '__EVAL_RESULT_END__')

# Load test cases configuration
try:
    with open('${testCasesFileName}', 'r', encoding='utf-8') as f:
        config = json.load(f)
except Exception as e:
    output_result({'error': 'Failed to load test cases configuration: ' + str(e), 'results': [], 'score': 0})
    sys.exit(0)

evaluation_mode = config.get('evaluation_mode') or config.get('evaluationMode') or 'function'
expected_logs = config.get('expectedLogs') or []
test_cases = config.get('testCases') or []
target_rel = '${relativeTarget}'

# AST Pre-flight Safety Guard
def check_ast_safety(file_path):
    import ast
    try:
        with open(file_path, 'r', encoding='utf-8') as sf:
            tree = ast.parse(sf.read(), filename=file_path)
    except Exception:
        return True, None
    FORBIDDEN_MODULES = {'subprocess', 'pty', 'socket', 'ctypes'}
    FORBIDDEN_CALL_NAMES = {'system', 'popen', 'kill', 'execfile'}
    FORBIDDEN_FROM_IMPORTS = {
        'os': {'system', 'popen', 'kill', 'remove', 'unlink', 'rmdir', 'execl', 'execv', 'spawn'},
        'shutil': {'rmtree', 'move'},
        'sys': {'exit', '_getframe'}
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split('.')[0]
                if root_pkg in FORBIDDEN_MODULES:
                    return False, f"Import of restricted module '{alias.name}' is prohibited in sandbox."
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root_pkg = node.module.split('.')[0]
                if root_pkg in FORBIDDEN_MODULES:
                    return False, f"Import from restricted module '{node.module}' is prohibited in sandbox."
                if root_pkg in FORBIDDEN_FROM_IMPORTS:
                    for alias in node.names:
                        if alias.name in FORBIDDEN_FROM_IMPORTS[root_pkg]:
                            return False, f"Importing '{alias.name}' from '{node.module}' is prohibited in sandbox."
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_CALL_NAMES:
                    return False, f"Call to '{node.func.id}' is prohibited in sandbox."
                if node.func.id == '__import__':
                    return False, "Dynamic __import__ is prohibited in sandbox."
            elif isinstance(node.func, ast.Attribute):
                if node.func.attr in FORBIDDEN_CALL_NAMES:
                    return False, f"Call to '{node.func.attr}' is prohibited in sandbox."
                if isinstance(node.func.value, ast.Name) and (node.func.value.id, node.func.attr) in {('shutil', 'rmtree'), ('os', 'system')}:
                    return False, f"Call to '{node.func.value.id}.{node.func.attr}' is prohibited in sandbox."
    return True, None

is_safe, safety_error = check_ast_safety(target_rel)
if not is_safe:
    output_result({
        'error': safety_error,
        'results': [{'passed': False, 'error': safety_error}],
        'score': 0,
        'passedCount': 0,
        'totalCount': 1,
        'passed': False
    })
    sys.exit(0)

# Setup mock interactive input queue
raw_inputs = config.get('inputs') or []
input_queue = [str(x) for x in raw_inputs]
def custom_input(prompt=None):
    if prompt is not None:
        try:
            sys.__stdout__.write(str(prompt))
        except Exception:
            pass
    if input_queue:
        return input_queue.pop(0)
    return ""

builtins.input = custom_input

# Fuzzy log-matching helper
def is_value_match(actual_line, exp_val):
    if actual_line is None:
        return False
    str_actual = str(actual_line).strip()
    str_exp = str(exp_val).strip()
    if not str_exp:
        return False

    # 1. Direct exact or case-insensitive match
    if str_actual == str_exp or str_actual.lower() == str_exp.lower():
        return True

    # 2. Delimiter extraction (e.g. "Full Name: Rohit Sharma", "Age = 25", "name - Rohit Sharma", "Course is B.Tech")
    delim_match = re.match(r'^[^:=]+(?::\\s*|=\\s*|-\\s*|\\bis\\b\\s*)(.+)$', str_actual, re.IGNORECASE)
    if delim_match:
        extracted = delim_match.group(1).strip()
        if extracted == str_exp or extracted.lower() == str_exp.lower():
            return True
        try:
            if abs(float(extracted) - float(str_exp)) < 0.0001:
                return True
        except ValueError:
            pass

    # 3. Substring match for meaningful string values (length >= 3)
    if len(str_exp) >= 3 and str_exp.lower() in str_actual.lower():
        return True

    # 4. Number token check
    try:
        num_exp = float(str_exp)
        tokens = re.split(r'[\\s,;:=-]+', str_actual)
        for token in tokens:
            try:
                if abs(float(token) - num_exp) < 0.0001:
                    return True
            except ValueError:
                pass
    except ValueError:
        pass

    # 5. Boolean token check
    if str_exp.lower() in ('true', 'false'):
        tokens = [t.lower() for t in re.split(r'[\\s,;:=-]+', str_actual)]
        if str_exp.lower() in tokens:
            return True

    return False

# ==============================================================================
# 1. SCRIPT MODE EVALUATION
# ==============================================================================
if evaluation_mode == 'script':
    try:
        with open(target_rel, 'r', encoding='utf-8') as f:
            code = f.read()

        # Check if test_cases provides per-case inputs/expected
        if test_cases and len(test_cases) > 0 and any('input' in tc or 'inputs' in tc for tc in test_cases):
            script_results = []
            for tc in test_cases:
                tc_inputs = tc.get('inputs') or (tc.get('input') if isinstance(tc.get('input'), list) else ([tc.get('input')] if tc.get('input') is not None else []))
                input_queue = [str(x) for x in tc_inputs]
                
                stdout_buf = io.StringIO()
                old_stdout = sys.stdout
                sys.stdout = stdout_buf
                run_err = None
                try:
                    ns = {'__name__': '__main__', '__file__': target_rel}
                    exec(code, ns)
                except Exception as ex:
                    run_err = str(ex)
                finally:
                    sys.stdout = old_stdout

                captured = [line.strip() for line in stdout_buf.getvalue().splitlines() if line.strip()]
                tc_expected = tc.get('expected') or tc.get('expectedLogs') or []
                if not isinstance(tc_expected, list):
                    tc_expected = [tc_expected]

                passed = False
                if run_err:
                    script_results.append({'passed': False, 'input': tc.get('input'), 'expected': tc_expected, 'error': run_err, 'captured': captured})
                elif not tc_expected:
                    script_results.append({'passed': True, 'input': tc.get('input'), 'captured': captured})
                else:
                    match_ok = any(is_value_match(c, tc_expected[0]) for c in captured) if len(tc_expected) == 1 else all(any(is_value_match(c, exp) for c in captured) for exp in tc_expected)
                    script_results.append({'passed': match_ok, 'input': tc.get('input'), 'expected': tc_expected, 'captured': captured})

            passed_count = sum(1 for r in script_results if r.get('passed'))
            total_count = len(script_results)
            score = round((passed_count / total_count) * 100) if total_count > 0 else 0
            output_result({
                'results': script_results,
                'score': score,
                'passedCount': passed_count,
                'totalCount': total_count,
                'passed': score == 100
            })
            sys.exit(0)

        # Standard expectedLogs evaluation
        stdout_buf = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = stdout_buf
        run_err = None
        try:
            ns = {'__name__': '__main__', '__file__': target_rel}
            exec(code, ns)
        except Exception as ex:
            run_err = str(ex)
        finally:
            sys.stdout = old_stdout

        captured_lines = [line.strip() for line in stdout_buf.getvalue().splitlines() if line.strip()]

        if run_err:
            output_result({
                'results': [{'passed': False, 'error': run_err}],
                'score': 0,
                'passedCount': 0,
                'totalCount': len(expected_logs) if expected_logs else 1,
                'passed': False,
                'feedback': f'Runtime error: {run_err}',
                'capturedLogs': captured_lines
            })
            sys.exit(0)

        if not expected_logs:
            output_result({
                'results': [{'passed': True, 'captured': captured_lines}],
                'score': 100,
                'passedCount': 1,
                'totalCount': 1,
                'passed': True,
                'feedback': 'Script executed successfully.',
                'capturedLogs': captured_lines
            })
            sys.exit(0)

        used_indices = set()
        match_map = {}
        for exp_idx, exp in enumerate(expected_logs):
            if exp_idx < len(captured_lines) and is_value_match(captured_lines[exp_idx], exp):
                used_indices.add(exp_idx)
                match_map[exp_idx] = exp_idx

        for exp_idx, exp in enumerate(expected_logs):
            if exp_idx not in match_map:
                for act_idx, act_line in enumerate(captured_lines):
                    if act_idx not in used_indices and is_value_match(act_line, exp):
                        used_indices.add(act_idx)
                        match_map[exp_idx] = act_idx
                        break

        details = []
        passed_count = 0
        for exp_idx, exp in enumerate(expected_logs):
            if exp_idx in match_map:
                act = captured_lines[match_map[exp_idx]]
                details.append({'passed': True, 'expected': exp, 'actual': act})
                passed_count += 1
            else:
                act = captured_lines[exp_idx] if exp_idx < len(captured_lines) else None
                details.append({'passed': False, 'expected': exp, 'actual': act, 'error': f'Expected "{exp}" not found in output'})

        total_count = len(expected_logs)
        score = round((passed_count / total_count) * 100) if total_count > 0 else 0
        output_result({
            'results': details,
            'score': score,
            'passedCount': passed_count,
            'totalCount': total_count,
            'passed': score == 100,
            'feedback': f'Passed {passed_count} of {total_count} tests',
            'capturedLogs': captured_lines
        })
        sys.exit(0)
    except Exception as e:
        output_result({'error': 'Script execution failed: ' + str(e), 'results': [], 'score': 0})
        sys.exit(0)

# ==============================================================================
# 2. FUNCTION & MULTI-FUNCTION MODES
# ==============================================================================
student_ns = {}
try:
    with open(target_rel, 'r', encoding='utf-8') as f:
        student_code_text = f.read()
    exec(student_code_text, student_ns)
except Exception as e:
    output_result({
        'error': 'Failed to compile student Python file: ' + str(e),
        'results': [{'passed': False, 'error': str(e)}],
        'score': 0,
        'passedCount': 0,
        'totalCount': len(test_cases) if test_cases else 1,
        'passed': False
    })
    sys.exit(0)

def resolve_function(name):
    if not name:
        return None
    val = student_ns.get(name)
    if callable(val):
        return val
    # Fallback to case-insensitive lookup
    for k, v in student_ns.items():
        if k.lower() == name.lower() and callable(v):
            return v
    return None

def evaluate_test_cases(fn, tc_list):
    try:
        sig = inspect.signature(fn)
        num_params = len(sig.parameters)
    except Exception:
        num_params = 1

    case_results = []
    for tc in tc_list:
        try:
            inp = tc.get('input')
            expected = tc.get('expected')
            if isinstance(inp, list) and num_params > 1:
                actual = fn(*inp)
            elif inp is not None:
                actual = fn(inp)
            else:
                actual = fn()

            passed = False
            if actual == expected:
                passed = True
            elif isinstance(actual, (int, float)) and isinstance(expected, (int, float)) and abs(actual - expected) < 0.0001:
                passed = True
            elif str(actual).strip() == str(expected).strip():
                passed = True
            else:
                try:
                    passed = json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
                except Exception:
                    pass

            case_results.append({'passed': passed, 'input': inp, 'expected': expected, 'actual': actual})
        except Exception as err:
            case_results.append({'passed': False, 'input': tc.get('input'), 'expected': tc.get('expected'), 'error': str(err)})
    return case_results

# Multi-function mode
func_list = config.get('functions')
if func_list and isinstance(func_list, list):
    all_results = []
    for fn_obj in func_list:
        f_name = fn_obj.get('name')
        f_cases = fn_obj.get('testCases') or []
        target_fn = resolve_function(f_name)
        if not target_fn:
            all_results.extend([{'passed': False, 'input': tc.get('input'), 'expected': tc.get('expected'), 'error': f'Function {f_name} not found'} for tc in f_cases])
        else:
            all_results.extend(evaluate_test_cases(target_fn, f_cases))

    passed_count = sum(1 for r in all_results if r.get('passed'))
    total_count = len(all_results)
    score = round((passed_count / total_count) * 100) if total_count > 0 else 0
    output_result({
        'results': all_results,
        'score': score,
        'passedCount': passed_count,
        'totalCount': total_count,
        'passed': score == 100,
        'feedback': f'Passed {passed_count} of {total_count} tests'
    })
    sys.exit(0)

# Single function mode
entry_fn_name = config.get('entryFunction') or 'solution'
fn = resolve_function(entry_fn_name)

# If not found by entryFunction, fallback to first defined user function in student_ns
if not fn:
    user_callables = [v for k, v in student_ns.items() if callable(v) and not k.startswith('_') and getattr(v, '__module__', None) in (None, '__main__')]
    if len(user_callables) == 1:
        fn = user_callables[0]

if not fn or not callable(fn):
    output_result({
        'error': f'Function {entry_fn_name} not found in student submission',
        'results': [],
        'score': 0,
        'passedCount': 0,
        'totalCount': len(test_cases) if test_cases else 1,
        'passed': False
    })
    sys.exit(0)

fn_results = evaluate_test_cases(fn, test_cases)
passed_count = sum(1 for r in fn_results if r.get('passed'))
total_count = len(fn_results)
score = round((passed_count / total_count) * 100) if total_count > 0 else 0
output_result({
    'results': fn_results,
    'score': score,
    'passedCount': passed_count,
    'totalCount': total_count,
    'passed': score == 100,
    'feedback': f'Passed {passed_count} of {total_count} tests'
})
`;

  fs.writeFileSync(harnessFilePath, harnessPython, "utf-8");
  const containerName = `eval-py-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`;
  // Hardened Docker execution flags: --network none, memory 512m, cpu 1.0, pids-limit 64
  const dockerCmd = `docker run --name ${containerName} --rm --network none --memory="512m" --cpus="1.0" --pids-limit=64 -v "${normalizedPath}:/app" -w /app python:3.11-alpine python ${harnessFileName}`;

  try {
    const canDocker = await isDockerAvailable();
    let stdout;
    if (canDocker) {
      const execRes = await execPromise(dockerCmd, { timeout: 15000 });
      stdout = execRes.stdout;
    } else {
      logger.info('Docker not running, executing Python test harness via local python3 process with AST safety check...');
      const pythonBin = process.platform === 'win32' ? 'python' : 'python3';
      const execRes = await execPromise(`"${pythonBin}" "${harnessFileName}"`, {
        cwd: repoPath,
        timeout: 15000,
        maxBuffer: 5 * 1024 * 1024
      });
      stdout = execRes.stdout;
    }

    const match = stdout.match(/__EVAL_RESULT_START__([\s\S]*?)__EVAL_RESULT_END__/);
    const rawJson = match ? match[1].trim() : stdout.trim();
    const parsed = JSON.parse(rawJson);
    return { ...parsed, studentCode };
  } catch (err) {
    execPromise(`docker rm -f ${containerName}`).catch(() => {});
    const isTimeout = err.killed || (err.message && err.message.toLowerCase().includes('timed out'));
    if (isTimeout) {
      logger.warn(`Python execution timed out in ${containerName} (infinite loop detected).`);
      return {
        results: [{
          passed: false,
          error: "Execution timed out (possible infinite loop or excessive computation). Ensure all loops have valid termination conditions."
        }],
        score: 0,
        passedCount: 0,
        totalCount: 1,
        passed: false,
        feedback: "Execution timed out after exceeding the 15-second time limit. Check for infinite loops (e.g. while loops without exit conditions or non-terminating recursion).",
        error: "Execution timed out (possible infinite loop).",
        studentCode
      };
    }
    logger.error(`Python test runner error: ${err.message}`);
    throw new Error(`Python execution failed: ${err.message}`);
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
  const reportFilePath = path.join(workspaceDir, "report.json");
  const scriptFilePath = path.join(workspaceDir, "run_excel.sh");

  const runScriptContent = `#!/bin/sh
set -e
RECALC=/sandbox/recalculate.py
[ -f /workspace/recalculate.py ] && RECALC=/workspace/recalculate.py
RUNNER=/sandbox/excel_runner.py
[ -f /workspace/excel_runner.py ] && RUNNER=/workspace/excel_runner.py
python3 "$RECALC" --input /workspace/original_submission.xlsx --output /workspace/recalculated_submission.xlsx --profile-dir /tmp/lo_prof > /dev/null
python3 "$RUNNER" --original /workspace/original_submission.xlsx --recalculated /workspace/recalculated_submission.xlsx --spec /workspace/test_spec.json > /workspace/report.json
`;

  try {
    fs.writeFileSync(scriptFilePath, runScriptContent.replace(/\r\n/g, "\n"), "utf-8");
  } catch (writeErr) {
    logger.warn(`Failed to write run_excel.sh: ${writeErr.message}`);
  }

  // Execute using runner script inside container - avoids OS shell quoting incompatibilities
  const dockerCmd = `docker run --name ${containerName} --entrypoint "" --rm --network none --memory="768m" --cpus="1.0" -v "${normalizedPath}:/workspace" -w /workspace ${dockerImage} sh /workspace/run_excel.sh`;

  logger.info(`🐳 Starting Excel Docker sandbox execution in ${containerName}`);
  try {
    const { stdout } = await execPromise(dockerCmd, { timeout: timeoutMs });
    if (fs.existsSync(reportFilePath)) {
      const content = fs.readFileSync(reportFilePath, 'utf8');
      return JSON.parse(content.trim());
    }
    return JSON.parse(stdout.trim());
  } catch (err) {
    execPromise(`docker rm -f ${containerName}`).catch(() => {});
    throw new Error(`Docker Excel execution failed: ${err.message}`);
  } finally {
    try {
      if (fs.existsSync(reportFilePath)) fs.unlinkSync(reportFilePath);
      if (fs.existsSync(scriptFilePath)) fs.unlinkSync(scriptFilePath);
    } catch {}
  }
}
