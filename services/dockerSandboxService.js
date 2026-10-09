import { exec } from "child_process";
import util from "util";
import path from "path";
import fs from "fs";
import crypto from "crypto";
import logger from "../config/logger.js";

const execPromise = util.promisify(exec);

let dockerAvailableCache = null;
let lastCheckTime = 0;

/**
 * Checks if Docker daemon is running and reachable.
 * Caches the result for 30 seconds to avoid running `docker info` repeatedly.
 */
export async function isDockerAvailable() {
  const now = Date.now();
  if (dockerAvailableCache !== null && now - lastCheckTime < 30000) {
    return dockerAvailableCache;
  }

  try {
    await execPromise("docker info", { timeout: 4000 });
    dockerAvailableCache = true;
    lastCheckTime = now;
    return true;
  } catch (err) {
    logger.warn(`Docker not reachable: ${err.message}. Will fall back to GitHub Actions.`);
    dockerAvailableCache = false;
    lastCheckTime = now;
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
  const timeoutMs = options.timeoutMs || 45000;
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

/**
 * Recursively finds target file in a directory matching a predicate, excluding node_modules/git.
 * Checks files in the current root directory first before traversing subdirectories to ensure deterministic precedence.
 */
function findTargetFile(dir, predicate, maxDepth = 3, currentDepth = 0) {
  if (currentDepth > maxDepth || !fs.existsSync(dir)) return null;
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  // Sort entries alphabetically for deterministic traversal
  entries.sort((a, b) => a.name.localeCompare(b.name));

  // 1. Check all direct files in current directory first
  for (const entry of entries) {
    if (entry.isFile() && !entry.name.startsWith('.') && predicate(entry.name)) {
      return path.join(dir, entry.name);
    }
  }

  // 2. Traverse into subdirectories
  for (const entry of entries) {
    if (entry.isDirectory()) {
      if (entry.name.startsWith('.') || entry.name === 'node_modules' || entry.name === 'coverage' || entry.name === 'test' || entry.name === 'tests') continue;
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
export async function runJsInDocker(repoPath, { testCases = [], entryFunction = "solution", evaluationMode = "function", expectedLogs = [], functions = null }) {
  const normalizedPath = path.resolve(repoPath).replace(/\\/g, "/");
  
  // Find primary JS file (root or src/)
  const fullTarget = findTargetFile(repoPath, f => f.endsWith(".js") && !f.includes("test") && !f.includes("spec") && !f.startsWith("."))
    || path.join(repoPath, "index.js");
  const relativeTarget = path.relative(repoPath, fullTarget).replace(/\\/g, "/");

  // Write test cases & configuration to a separate JSON file on disk to prevent host shell injection
  const testCasesFileName = `.eval_tc_${Date.now()}_${crypto.randomBytes(3).toString('hex')}.json`;
  const testCasesFilePath = path.join(repoPath, testCasesFileName);
  fs.writeFileSync(testCasesFilePath, JSON.stringify({
    testCases: Array.isArray(testCases) ? testCases : [],
    entryFunction: entryFunction || "solution",
    evaluationMode: evaluationMode || "function",
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
  const customConsole = {
    log: (...args) => capturedLogs.push(args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')),
    info: (...args) => capturedLogs.push(args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')),
    warn: (...args) => capturedLogs.push(args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' ')),
    error: (...args) => capturedLogs.push(args.map(a => typeof a === 'object' ? JSON.stringify(a) : String(a)).join(' '))
  };

  const outputResult = (data) => {
    console.log('__EVAL_RESULT_START__' + JSON.stringify(data) + '__EVAL_RESULT_END__');
  };

  // 1. Script mode evaluation
  if (config.evaluationMode === 'script') {
    try {
      const code = fs.readFileSync(targetAbs, 'utf8');
      const sandbox = { console: customConsole, process, require: createRequire(import.meta.url) };
      vm.createContext(sandbox);
      vm.runInContext(code, sandbox, { timeout: 5000 });
      
      const expected = config.expectedLogs || [];
      const passed = expected.every((exp, idx) => {
        const actual = capturedLogs[idx] || '';
        return actual.trim() === String(exp).trim();
      });
      outputResult({ passed, score: passed ? 100 : 0, logs: capturedLogs });
      process.exit(0);
    } catch(e) {
      outputResult({ passed: false, score: 0, error: e.message, logs: capturedLogs });
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
      // VM fallback for unexported functions
      try {
        const code = fs.readFileSync(targetAbs, 'utf8');
        const sandbox = { console: customConsole, module: { exports: {} }, exports: {}, require: createRequire(import.meta.url) };
        vm.createContext(sandbox);
        vm.runInContext(code, sandbox, { timeout: 5000 });
        mod = { ...sandbox, ...(sandbox.module?.exports || {}), ...(sandbox.exports || {}) };
      } catch(e3) {
        outputResult({ error: e3.message, results: [] });
        process.exit(0);
      }
    }
  }

  const resolveFn = (name) => {
    if (typeof mod[name] === 'function') return mod[name];
    if (mod.default && typeof mod.default[name] === 'function') return mod.default[name];
    if (mod.default && typeof mod.default === 'function') return mod.default;
    if (typeof mod === 'function') return mod;
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
        const passed = JSON.stringify(actual) === JSON.stringify(tc.expected);
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
    outputResult({ results: allResults });
    process.exit(0);
  }

  // Single function mode
  const entryFunction = config.entryFunction || 'solution';
  const fn = resolveFn(entryFunction);
  if (typeof fn !== 'function') {
    outputResult({ error: 'Function ' + entryFunction + ' not found', results: [] });
    process.exit(0);
  }

  const results = evaluateCases(fn, config.testCases || []);
  outputResult({ results });
})();
`;

  fs.writeFileSync(harnessFilePath, harnessCode, "utf-8");
  const containerName = `eval-js-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`;
  const dockerCmd = `docker run --name ${containerName} --rm --memory="512m" --cpus="1.0" -v "${normalizedPath}:/app" -w /app node:18-alpine node ${harnessFileName}`;

  try {
    const { stdout } = await execPromise(dockerCmd, { timeout: 15000 });
    const match = stdout.match(/__EVAL_RESULT_START__([\s\S]*?)__EVAL_RESULT_END__/);
    const rawJson = match ? match[1].trim() : stdout.trim();
    const parsed = JSON.parse(rawJson);
    return parsed.results || (parsed.passed !== undefined ? parsed : []);
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
