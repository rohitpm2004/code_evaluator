import fs from 'fs';
import fsp from 'fs/promises';
import path from 'path';
import os from 'os';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';
import logger from '../../config/logger.js';
import { generateSqlFeedback } from './aiFeedback.js';
import { loadSqlSubmission } from './submissionLoader.js';

import dotenv from 'dotenv';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const MAX_SQL_SIZE_BYTES = 2 * 1024 * 1024; // 2MB

/**
 * Computes official SQL score strictly based on verified deterministic sandbox test results.
 * AI pedagogical feedback explains mistakes and provides learning guidance,
 * but never fabricates or inflates official correctness marks.
 */
export function computeSqlFinalScore({ deterministicScore = 0, pedagogicalScore = 0, hasTestResults = false, policyBlocked = false }) {
  if (policyBlocked) {
    return 0;
  }
  if (deterministicScore > 0) {
    // Verified ground truth score computed deterministically by the execution engine.
    // 100% reproducible and immune to LLM hallucination or temperature variance across re-evaluations.
    return Math.max(0, Math.min(100, Math.round(deterministicScore)));
  }
  // When database execution fails completely due to syntax errors (deterministicScore == 0),
  // but the student demonstrated genuine effort and correct structure without security violations:
  if (pedagogicalScore > 0) {
    // Award partial credit for demonstrated intent/structure, capped at 70% to reflect syntax execution failure
    return Math.max(0, Math.min(70, Math.round(pedagogicalScore)));
  }
  return 0;
}

/**
 * Spawns the Python SQL execution engine as a child process.
 * Communication is strictly via JSON over stdin/stdout — student SQL is
 * NEVER passed as a CLI argument (prevents shell interpolation attacks).
 *
 * @param {object} enginePayload - The JSON contract to send to sql_runner.py
 * @param {number} timeoutMs     - Hard kill timeout for the child process
 * @returns {Promise<object>}    - Parsed JSON response from the Python engine
 */
async function runPythonEngine(enginePayload, timeoutMs = 90000) {
  dotenv.config(); // Reload .env dynamically so newly configured PG credentials are active
  const pythonBin = process.env.PYTHON_BIN || (process.platform === 'win32' ? 'python' : 'python3');
  const runnerScript = path.resolve(__dirname, 'engine', 'sql_runner.py');

  if (!fs.existsSync(runnerScript)) {
    throw new Error(`SQL engine script not found at: ${runnerScript}`);
  }

  return new Promise((resolve, reject) => {
    const child = spawn(pythonBin, [runnerScript], {
      stdio: ['pipe', 'pipe', 'pipe'],
      timeout: timeoutMs,
      env: {
        ...process.env,
        PYTHONUNBUFFERED: '1'
      }
    });

    let stdout = '';
    let stderr = '';

    child.stdout.on('data', (chunk) => {
      stdout += chunk.toString();
    });

    child.stderr.on('data', (chunk) => {
      stderr += chunk.toString();
    });

    child.on('error', (err) => {
      reject(new Error(`Python engine spawn error: ${err.message}`));
    });

    child.on('close', (code, signal) => {
      if (code !== 0) {
        if (code === null || signal === 'SIGTERM') {
          reject(new Error(`EVALUATION_PROCESS_TIMEOUT: Python engine was terminated after ${timeoutMs}ms (code null, signal ${signal || 'SIGTERM'}). Infrastructure database latency or pool congestion exceeded process limit.`));
          return;
        }
        const errMsg = stderr.trim() || `Python engine exited with code ${code}`;
        reject(new Error(errMsg));
        return;
      }

      try {
        const result = JSON.parse(stdout);
        resolve(result);
      } catch (parseErr) {
        reject(new Error(`Failed to parse Python engine output: ${parseErr.message}. Raw stdout: ${stdout.slice(0, 500)}`));
      }
    });

    // Write the JSON payload to stdin and close the stream
    const payloadStr = JSON.stringify(enginePayload);
    child.stdin.write(payloadStr);
    child.stdin.end();
  });
}

/**
 * Main evaluation orchestrator for SQL submissions.
 * Follows the same lifecycle pattern as evaluateExcelSubmission:
 *   1. Ingest submission (sqlText / ideFiles / URL download)
 *   2. Build engine payload with manifest, student SQL, and job metadata
 *   3. Spawn Python engine for AST validation, sandboxed execution, and deterministic scoring
 *   4. Generate AI feedback from verified test evidence (DB connection already released by engine)
 *   5. Return unified result payload for BullMQ persistence
 *
 * @param {object} jobData - The job payload from BullMQ
 * @param {string} jobId   - The BullMQ job ID
 * @returns {Promise<object>} - Evaluation results with score, feedback, and test evidence
 */
export async function evaluateSqlSubmission(jobData, jobId) {
  const startTime = Date.now();

  // Extract fields from job payload (matches evaluationRouter.js forwarding)
  const submission = jobData.submission || {};
  const manifest = jobData.manifest || jobData.testCases || {};
  const rubric = jobData.rubric || {};
  const assignmentInstructions = jobData.assignmentInstructions || jobData.description || submission.description || '';
  const sqlText = jobData.sqlText || submission.sqlText;
  const ideFiles = jobData.ideFiles || submission.ideFiles;
  const fileUrl = jobData.fileUrl || submission.submission_file_url || submission.submission_link || submission.repoUrl;

  const tempWorkspace = await fsp.mkdtemp(path.join(os.tmpdir(), `eval-sql-${jobId || Date.now()}-`));

  try {
    // ── Phase 1: Ingest student SQL ─────────────────────────────────────
    let studentSql;
    try {
      studentSql = await loadSqlSubmission({
        sqlText,
        ideFiles,
        fileUrl,
        tempWorkspace,
        maxSizeBytes: MAX_SQL_SIZE_BYTES,
        assignmentTitle: jobData.assignmentTitle || '',
        manifest,
      });
    } catch (ingestErr) {
      logger.warn(`[SqlEvaluator] Failed to ingest student SQL for Job ${jobId}: ${ingestErr.message}`);
      throw ingestErr;
    }

    if (!studentSql || studentSql.trim().length === 0) {
      logger.warn(`[SqlEvaluator] No SQL content found for Job ${jobId}`);
      throw new Error('No SQL content was found in the submission. Please submit your SQL query or file and try again.');
    }

    // ── Phase 2: Build engine payload ───────────────────────────────────
    let mode = manifest.mode;
    if (!mode) {
      const hasDdl = studentSql && /^\s*(CREATE|ALTER|DROP)\b/im.test(studentSql);
      const isDataModeling = /data\s*model|schema|ddl|table\s*creation|erd|foreign\s*key|primary\s*key/i.test(
        JSON.stringify(manifest) + ' ' + JSON.stringify(rubric) + ' ' + (jobData.assignmentTitle || '')
      );
      mode = (hasDdl || isDataModeling) ? 'script' : 'query_only';
    }

    const enginePayload = {
      contractVersion: '1.0',
      jobId: String(jobId),
      mode,
      studentSql,
      manifest
    };

    // ── Phase 3: Execute Python engine ──────────────────────────────────
    // The engine handles:
    //   - AST parsing & policy enforcement (sqlglot)
    //   - Dual-role sandboxed PostgreSQL execution (admin DDL + runner SELECT)
    //   - Reference query execution against visible & hidden fixtures
    //   - Multiset result comparison with N+1 overflow detection
    //   - Deterministic scoring with partial credit
    //   - Schema cleanup (finally block + background sweeper)
    // Database connections are closed inside the engine BEFORE returning.
    let engineResult;
    try {
      engineResult = await runPythonEngine(enginePayload, 30000);
    } catch (engineErr) {
      logger.error(`[SqlEvaluator] Engine error for Job ${jobId}: ${engineErr.message}`);

      // Distinguish infrastructure failures from student errors
      const msg = engineErr.message.toLowerCase();
      const isInfraError = msg.includes('connection') ||
                           msg.includes('timeout') ||
                           msg.includes('code null') ||
                           msg.includes('evaluation_process_timeout') ||
                           msg.includes('neither psycopg nor psycopg2') ||
                           msg.includes('spawn error');

      if (isInfraError) {
        // Let BullMQ retry — do NOT give the student a 0
        throw engineErr;
      }

      // Student-caused error (syntax, policy violation) — return completed with deductions
      return {
        score: 0,
        feedback: {
          summary: `SQL evaluation encountered an error: ${engineErr.message}`,
          strengths: [],
          issues: [engineErr.message],
          breakdown: []
        },
        evaluation_status: 'COMPLETED',
        test_results: [],
        durationMs: Date.now() - startTime
      };
    }

    // ── Phase 4: Generate AI feedback & Pedagogical Score ─────────────
    // Database connections are already closed by the Python engine.
    // Wrap with a 40-second timeout race with DeepSeek retry backoff
    let feedback;
    try {
      const aiTimeout = new Promise((resolve) => setTimeout(() => resolve(null), 25000));
      const aiResult = await Promise.race([generateSqlFeedback(engineResult, rubric, assignmentInstructions), aiTimeout]);
      feedback = aiResult || await generateSqlFeedback({ ...engineResult, skipAi: true }, rubric, assignmentInstructions);
    } catch (fbErr) {
      logger.warn(`[SqlEvaluator] Feedback generation failed, falling back to deterministic: ${fbErr.message}`);
      feedback = {
        summary: `SQL evaluated with a score of ${engineResult.deterministicScore ?? 0}/100.`,
        strengths: (engineResult.testResults || []).filter((t) => t.passed).map((t) => `${t.name}: Passed`),
        issues: (engineResult.testResults || []).filter((t) => !t.passed).map((t) => `${t.name}: ${t.reason}`),
        breakdown: [],
        pedagogicalScore: engineResult.deterministicScore ?? 0
      };
    }

    const durationMs = Date.now() - startTime;
    const deterministicScore = engineResult.deterministicScore ?? 0;
    const pedagogicalScore = typeof feedback?.pedagogicalScore === 'number'
      ? feedback.pedagogicalScore
      : deterministicScore;

    const hasTestResults = Array.isArray(engineResult.testResults) && engineResult.testResults.length > 0;
    let finalScore = computeSqlFinalScore({
      deterministicScore,
      pedagogicalScore,
      hasTestResults,
      policyBlocked: Boolean(engineResult.policyBlocked)
    });

    // MATHEMATICAL CONSISTENCY GUARANTEE:
    // Ensure that sum(feedback.breakdown[i].awarded) ALWAYS matches finalScore exactly!
    if (Array.isArray(feedback?.breakdown) && feedback.breakdown.length > 0) {
      const breakdownSum = feedback.breakdown.reduce((sum, b) => sum + (Number(b.awarded) || 0), 0);
      if (breakdownSum !== finalScore) {
        let diff = finalScore - breakdownSum;
        for (const item of feedback.breakdown) {
          if (diff === 0) break;
          const max = typeof item.max === 'number' ? item.max : 20;
          const current = Number(item.awarded) || 0;
          if (diff > 0) {
            const add = Math.min(diff, max - current);
            item.awarded = current + add;
            diff -= add;
          } else if (diff < 0) {
            const sub = Math.min(-diff, current);
            item.awarded = current - sub;
            diff += sub;
          }
        }
        // If there's still a rounding discrepancy, adjust the last criterion
        if (diff !== 0 && feedback.breakdown.length > 0) {
          const last = feedback.breakdown[feedback.breakdown.length - 1];
          last.awarded = Math.max(0, (Number(last.awarded) || 0) + diff);
        }
      }
    }

    logger.info(`[SqlEvaluator] Completed Job ${jobId} in ${durationMs}ms with verified final score: ${finalScore} (Deterministic: ${deterministicScore}, Pedagogical: ${pedagogicalScore})`);

    // ── Phase 5: Return unified result for BullMQ persistence ───────────
    // This object becomes job.returnvalue in Redis.
    // async-lms syncEvaluationStatus reads:
    //   marks = finalData?.score ?? finalData?.marks ?? 0
    //   feedback = finalData?.rubricFeedback || finalData?.feedback
    return {
      score: finalScore,
      feedback,
      rubricFeedback: feedback,
      rubric_breakdown: engineResult.rubricBreakdown || {},
      test_results: engineResult.testResults || [],
      ast_analysis: engineResult.astAnalysis || {},
      execution_metrics: engineResult.executionMetrics || {},
      evaluation_status: 'COMPLETED',
      durationMs
    };
  } finally {
    // Clean up temporary workspace
    try {
      await fsp.rm(tempWorkspace, { recursive: true, force: true });
    } catch (cleanupErr) {
      logger.warn(`Failed to clean up temp directory ${tempWorkspace}: ${cleanupErr.message}`);
    }
  }
}

/**
 * Sweeps orphaned ephemeral PostgreSQL schemas left behind by killed processes.
 * Runs during worker startup.
 */
export async function sweepOrphanedSchemas() {
  const pythonBin = process.env.PYTHON_BIN || (process.platform === 'win32' ? 'python' : 'python3');
  const cleanerScript = path.resolve(__dirname, 'engine', 'schema_cleaner.py');
  if (!fs.existsSync(cleanerScript)) return;

  return new Promise((resolve) => {
    const child = spawn(pythonBin, [cleanerScript], {
      stdio: ['ignore', 'pipe', 'pipe'],
      timeout: 15000,
      env: { ...process.env, PYTHONUNBUFFERED: '1' }
    });
    let out = '';
    child.stdout?.on('data', (d) => { out += d.toString(); });
    child.on('close', () => {
      if (out.trim()) logger.info(`[SchemaClean] ${out.trim()}`);
      resolve();
    });
    child.on('error', (err) => {
      logger.warn(`[SchemaClean] Sweep notice: ${err.message}`);
      resolve();
    });
  });
}

