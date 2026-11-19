import { Worker } from 'bullmq';
import fs from 'fs';
import path from 'path';
import os from 'os';
import redisConnection, { redisConfig } from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { triggerGraderWorkflow } from '../services/githubActionService.js';
import { withTimeout } from '../evaluators/react/utils/timeout.js';
import { webhookPubSub } from '../services/webhookPubSub.js';
import { generatePythonAIFeedback, evaluatePythonRubric, fetchStudentCode } from '../evaluators/python/aiFeedback.js';
import { cloneRepo, deleteRepo } from '../evaluators/react/repoService.js';
import { isDockerAvailable, runPythonInDocker } from '../services/dockerSandboxService.js';

let pythonWorker = null;

export async function initializePythonWorker() {
  try {
    logger.info('Initializing Python Worker...');

    const config = queueManager.getConfig('python');

    pythonWorker = new Worker(
      'python-evaluation',
      async (job) => {
        try {
          logger.info(`Starting Python evaluation: ${job.id}`);
          const {
            submission,
            testCases,
            entryFunction,
            entryFile,
            evaluationMode,
            expectedLogs,
            functions,
            inputs
          } = job.data;

          // Phase 0: Check if Docker Sandbox is enabled and available
          const sandboxMode = (process.env.SANDBOX_MODE || "docker").toLowerCase();
          const canUseDocker = sandboxMode === "docker" && (await isDockerAvailable());

          let results;
          const repoUrl = submission?.repoUrl || submission?.fileUrl || job.data?.fileUrl || job.data?.repoUrl;
          const directCode = submission?.code || submission?.python_text || job.data?.code;

          // Execute via Docker Sandbox (or local AST-safe process if Docker is unavailable)
          if (canUseDocker || sandboxMode === "docker" || (!process.env.GITHUB_PAT && directCode)) {
            logger.info(`🐍 Python Job ${job.id}: Using containerized / local sandbox execution.`);
            let repoPath = null;
            let didClone = false;

            if (typeof directCode === 'string' && directCode.trim().length > 0) {
              const tempDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'py-eval-'));
              await fs.promises.writeFile(path.join(tempDir, 'main.py'), directCode, 'utf8');
              repoPath = tempDir;
              didClone = true;
            } else if (submission?.ideFiles && Array.isArray(submission.ideFiles) && submission.ideFiles.length > 0) {
              const tempDir = await fs.promises.mkdtemp(path.join(os.tmpdir(), 'py-eval-'));
              for (const file of submission.ideFiles) {
                if (file.path && typeof file.content === 'string') {
                  const fp = path.join(tempDir, file.path);
                  await fs.promises.mkdir(path.dirname(fp), { recursive: true });
                  await fs.promises.writeFile(fp, file.content, 'utf8');
                }
              }
              repoPath = tempDir;
              didClone = true;
            } else if (repoUrl) {
              repoPath = await cloneRepo(repoUrl);
              didClone = true;
            } else {
              throw new Error("No Python code or repository URL provided in submission.");
            }

            try {
              results = await runPythonInDocker(repoPath, {
                testCases,
                entryFunction,
                entryFile,
                evaluationMode,
                expectedLogs,
                functions,
                inputs
              });
            } finally {
              if (didClone && repoPath) {
                await deleteRepo(repoPath);
              }
            }
          } else {
            // Cloud fallback via GitHub Actions Grader
            logger.info(`☁️ Python Job ${job.id}: Docker not available or SANDBOX_MODE=github. Using GitHub Actions Grader.`);
            results = await withTimeout(
              (async () => {
                const webhookPromise = webhookPubSub.waitForWebhook(job.id, config.timeout);
                const webhookUrl = `${process.env.BASE_URL}/api/webhook/github`;
                const extraPayload = {
                  testCases,
                  entryFunction,
                  entryFile,
                  evaluationMode,
                  expectedLogs,
                  functions,
                  inputs
                };

                await triggerGraderWorkflow(repoUrl, job.id, webhookUrl, 'run-python-evaluation', extraPayload);
                const githubResult = await webhookPromise;
                if (githubResult.status === 'completed') {
                  return githubResult.evaluation || {
                    passed: false,
                    score: 0,
                    error: "Invalid evaluation payload received from GitHub Actions"
                  };
                } else {
                  throw new Error("GitHub Actions Python job failed or was cancelled");
                }
              })(),
              config.timeout,
              `python-eval ${job.id}`
            );
          }

          let finalScore = 0;
          let passedCount = 0;
          let totalCount = 0;

          // Normalize results shape across script, function, and multi-function modes
          if (typeof results?.passedCount === 'number' && typeof results?.totalCount === 'number') {
            passedCount = results.passedCount;
            totalCount = results.totalCount;
            finalScore = typeof results?.score === 'number' ? results.score : (totalCount > 0 ? Math.round((passedCount / totalCount) * 100) : 0);
          } else if (Array.isArray(results?.results)) {
            totalCount = results.results.length;
            passedCount = results.results.filter(r => r.passed).length;
            finalScore = typeof results?.score === 'number' ? results.score : (totalCount > 0 ? Math.round((passedCount / totalCount) * 100) : 0);
          } else if (Array.isArray(results)) {
            totalCount = results.length;
            passedCount = results.filter(r => r.passed).length;
            finalScore = totalCount > 0 ? Math.round((passedCount / totalCount) * 100) : 0;
          } else if (typeof results?.score === 'number') {
            finalScore = results.score;
            const match = (results.feedback || '').match(/(\d+)\s*of\s*(\d+)/);
            if (match) {
              passedCount = parseInt(match[1], 10);
              totalCount = parseInt(match[2], 10);
            } else {
              totalCount = 100;
              passedCount = finalScore;
            }
          } else if (evaluationMode === 'script') {
            finalScore = results?.passed ? 100 : 0;
            totalCount = 1;
            passedCount = results?.passed ? 1 : 0;
          }

          // Parse rubric if present
          let rubric = null;
          const rawRubric = job.data.rubric || job.data.rubricText;
          if (rawRubric && rawRubric !== 'Standard evaluation') {
            try {
              let r = rawRubric;
              while (typeof r === 'string') {
                r = JSON.parse(r);
              }
              if (Array.isArray(r)) {
                rubric = { criteria: r };
              } else if (r && r.criteria && Array.isArray(r.criteria)) {
                rubric = r;
              }
            } catch(e) {
              logger.warn(`Failed to parse rubric for Python Job ${job.id}: ${e.message}`);
            }
          }

          const breakdown = [];
          const strengths = [];
          const issues = [];
          let studentCode = results?.studentCode || job.data?.submission?.code || '';
          if (!studentCode || typeof studentCode !== 'string' || studentCode.trim().length < 10) {
            try {
              const fetched = await fetchStudentCode(job.data);
              if (fetched && typeof fetched === 'string' && fetched.trim().length >= 10 && !fetched.startsWith("Code could not")) {
                studentCode = fetched;
              }
            } catch (e) {
              logger.warn(`Failed to fetch student Python code fallback for job ${job.id}: ${e.message}`);
            }
          }

          let summaryStr = null;
          // Top AI Teacher Layer: Always evaluates student submission code, diagnoses mistakes, and generates actionable feedback
          const rubricResult = await evaluatePythonRubric(studentCode, rubric, {
            score: finalScore,
            passedCount,
            totalCount,
            results: results?.results || results,
            feedback: results?.feedback,
            error: results?.error
          }, job.id, job.data);

          if (rubricResult) {
            finalScore = rubricResult.finalScore;
            breakdown.push(...rubricResult.breakdown);
            strengths.push(...rubricResult.strengths);
            issues.push(...rubricResult.issues);
            if (rubricResult.summary) {
              summaryStr = rubricResult.summary;
            }
          }

          // Standalone feedback fallback if needed
          if (!summaryStr) {
            logger.info(`Python Job ${job.id}: Generating fallback AI feedback...`);
            if (!job.data.submission) job.data.submission = {};
            if (studentCode) job.data.submission.code = studentCode;

            summaryStr = await generatePythonAIFeedback(job.data, results, finalScore, breakdown, rubric);
          }

          const aiFeedbackString = {
            summary: summaryStr,
            strengths: strengths.length > 0 ? strengths : (finalScore === 100 ? ["Passed all tests."] : []),
            issues: issues,
            breakdown: breakdown
          };

          return {
            success: true,
            studentId: submission?.studentId,
            studentName: submission?.studentName,
            evaluation: {
              score: finalScore,
              feedback: aiFeedbackString,
              details: results?.results || results
            }
          };
        } catch (err) {
          logger.error(`Python Job ${job.id} failed`, err);
          throw err;
        }
      },
      {
        connection: redisConfig,
        prefix: process.env.QUEUE_PREFIX || 'local',
        concurrency: Math.max(config.concurrency || 4, 4),
        drainDelay: 5,
        settings: {
          maxStalledCount: 3,
          stalledInterval: 30000,
          lockDuration: 180000,
          lockRenewTime: 30000
        }
      }
    );

    // Event handlers
    pythonWorker.on('active', (job) => {
      logger.info(`Python Job active: ${job.id}`);
    });

    pythonWorker.on('completed', (job, result) => {
      logger.info(`Python Job ${job.id} completed`, {
        duration: job.finishedOn - job.processedOn
      });
    });

    pythonWorker.on('failed', (job, err) => {
      logger.error(`Python Job ${job.id} failed`, {
        error: err.message,
        attempts: job.attemptsMade
      });
    });

    pythonWorker.on('error', (err) => {
      logger.error('Python Worker internal error:', err);
    });

    logger.info('Python Worker initialized');
    return pythonWorker;

  } catch (err) {
    logger.error('Failed to initialize Python worker:', err);
    throw err;
  }
}

export async function stopPythonWorker() {
  try {
    if (pythonWorker) {
      await pythonWorker.close();
    }
    logger.info('Python worker stopped');
  } catch (err) {
    logger.error('Error stopping Python worker:', err);
  }
}

export function getPythonWorkerStatus() {
  return {
    status: pythonWorker ? 'running' : 'not_initialized',
    concurrency: queueManager.getConfig('python').concurrency
  };
}

export { pythonWorker };
