import { Worker } from 'bullmq';
import redisConnection, { redisConfig } from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { triggerGraderWorkflow } from '../services/githubActionService.js';
import { withTimeout } from '../evaluators/react/utils/timeout.js';
import { generateJSAIFeedback, evaluateJSRubric, fetchStudentCode } from '../evaluators/js/aiFeedback.js';
import { cloneRepo, deleteRepo } from '../evaluators/react/repoService.js';
import { isDockerAvailable, runJsInDocker } from '../services/dockerSandboxService.js';
import { webhookPubSub } from '../services/webhookPubSub.js';

let jsWorker = null;

export async function initializeJsWorker() {
  try {
    logger.info('Initializing JavaScript Worker...');

    const config = queueManager.getConfig('javascript');

    jsWorker = new Worker(
      'javascript-evaluation',
      async (job) => {
        try {
          logger.info(`Starting JS evaluation: ${job.id}`);
          const {
            submission,
            testCases,
            entryFunction,
            entryFile,
            evaluationMode,
            expectedLogs,
            functions
          } = job.data;

          // Phase 0: Check if Docker Sandbox is enabled and available
          const sandboxMode = (process.env.SANDBOX_MODE || "docker").toLowerCase();
          const canUseDocker = sandboxMode === "docker" && (await isDockerAvailable());

          let results;
          if (canUseDocker) {
            logger.info(`🐳 JS Job ${job.id}: Using local Docker sandbox. Skipping GitHub Actions.`);
            const repoPath = await cloneRepo(submission.repoUrl);
            try {
              results = await runJsInDocker(repoPath, { testCases, entryFunction, entryFile, evaluationMode, expectedLogs, functions });
            } finally {
              await deleteRepo(repoPath);
            }
          } else {
            logger.info(`☁️ JS Job ${job.id}: Docker not available or SANDBOX_MODE=github. Using GitHub Actions.`);
            results = await withTimeout(
              (async () => {
                const webhookPromise = webhookPubSub.waitForWebhook(job.id, config.timeout);
                const webhookUrl = `${process.env.BASE_URL}/api/webhook/github`;
                const repoUrl = submission.repoUrl;
                const extraPayload = {
                  testCases,
                  entryFunction,
                  entryFile,
                  evaluationMode,
                  expectedLogs,
                  functions
                };
                
                await triggerGraderWorkflow(repoUrl, job.id, webhookUrl, 'run-js-evaluation', extraPayload);
                const githubResult = await webhookPromise;
                if (githubResult.status === 'completed') {
                  return githubResult.evaluation || {
                    passed: false,
                    score: 0,
                    error: "Invalid evaluation payload received from GitHub Actions"
                  };
                } else {
                  throw new Error("GitHub Actions job failed or cancelled");
                }
              })(),
              config.timeout,
              `js-eval ${job.id}`
            );
        }
          
        let finalScore = 0;
          let passedCount = 0;
          let totalCount = 0;

          // Handle both structured object ({ passedCount, totalCount, score, results }) and legacy array shapes
          if (typeof results?.passedCount === 'number' && typeof results?.totalCount === 'number') {
            passedCount = results.passedCount;
            totalCount = results.totalCount;
            finalScore = typeof results?.score === 'number' ? results.score : (totalCount > 0 ? (passedCount / totalCount) * 100 : 0);
          } else if (Array.isArray(results?.results)) {
            totalCount = results.results.length;
            passedCount = results.results.filter(r => r.passed).length;
            finalScore = typeof results?.score === 'number' ? results.score : (totalCount > 0 ? (passedCount / totalCount) * 100 : 0);
          } else if (Array.isArray(results)) {
            // function mode — array of per-test-case results (legacy fallback)
            totalCount = results.length;
            passedCount = results.filter(r => r.passed).length;
            finalScore = totalCount > 0 ? (passedCount / totalCount) * 100 : 0;
          } else if (typeof results?.score === 'number') {
            // New script/function mode — single result object with a numeric score
            finalScore = results.score;
            // Attempt to extract passed/total from the feedback string (e.g. "Passed 3 of 4 tests")
            const match = (results.feedback || '').match(/(\d+)\s*of\s*(\d+)/);
            if (match) {
              passedCount = parseInt(match[1], 10);
              totalCount = parseInt(match[2], 10);
            } else {
              totalCount = 100;
              passedCount = finalScore;
            }
          } else if (evaluationMode === 'script') {
            // fallback: treat passed as binary 0/100
            finalScore = results?.passed ? 100 : 0;
            totalCount = 1;
            passedCount = results?.passed ? 1 : 0;
          }

          const globalPassRatio = finalScore / 100;

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
               logger.warn(`Failed to parse rubric for JS Job ${job.id}: ${e.message}`);
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
              logger.warn(`Failed to fetch student code fallback for job ${job.id}: ${e.message}`);
            }
          }

          let summaryStr = null;
          if (rubric && rubric.criteria && rubric.criteria.length > 0) {
            const rubricResult = await evaluateJSRubric(studentCode, rubric, {
              score: finalScore,
              passedCount,
              totalCount,
              results: results?.results || results,
              feedback: results?.feedback
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
          }

          // Only generate standalone feedback if rubric evaluation didn't already provide a summary
          if (!summaryStr) {
            logger.info(`JS Job ${job.id}: Generating standalone AI feedback...`);
            if (!job.data.submission) job.data.submission = {};
            if (studentCode) job.data.submission.code = studentCode;

            summaryStr = await generateJSAIFeedback(job.data, results, finalScore, breakdown, rubric);
          }
          
          const aiFeedbackString = {
            summary: summaryStr,
            strengths: strengths.length > 0 ? strengths : (finalScore === 100 ? ["Passed all tests."] : []),
            issues: issues,
            breakdown: breakdown
          };

          return {
            success: true,
            studentId: submission.studentId,
            studentName: submission.studentName,
            evaluation: {
              score: finalScore,
              feedback: aiFeedbackString,
              details: results?.results || results
            }
          };
        } catch (err) {
          logger.error(`JS Job ${job.id} failed`, err);
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
          stalledInterval: 10000,
          lockDuration: 60000,
          lockRenewTime: 20000
        }
      }
    );

    // Event handlers
    jsWorker.on('active', (job) => {
      logger.info(`JS Job active: ${job.id}`);
    });

    jsWorker.on('completed', (job, result) => {
      logger.info(`JS Job ${job.id} completed`, {
        duration: job.finishedOn - job.processedOn
      });
    });

    jsWorker.on('failed', (job, err) => {
      logger.error(`JS Job ${job.id} failed`, {
        error: err.message,
        attempts: job.attemptsMade
      });
    });

    jsWorker.on('error', (err) => {
      logger.error('JS Worker internal error:', err);
    });

    logger.info('JavaScript Worker initialized');
    return jsWorker;

  } catch (err) {
    logger.error('Failed to initialize JS worker:', err);
    throw err;
  }
}

export async function stopJsWorker() {
  try {
    if (jsWorker) {
      await jsWorker.close();
    }
    logger.info('JS worker stopped');
  } catch (err) {
    logger.error('Error stopping JS worker:', err);
  }
}

export function getJsWorkerStatus() {
  return {
    status: jsWorker ? 'running' : 'not_initialized',
    concurrency: queueManager.getConfig('javascript').concurrency
  };
}

export { jsWorker };
