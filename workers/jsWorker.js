import { Worker } from 'bullmq';
import redisConnection from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { triggerGraderWorkflow } from '../services/githubActionService.js';
import { withTimeout } from '../evaluators/react/utils/timeout.js';
import { generateJSAIFeedback } from '../evaluators/js/aiFeedback.js';
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
              results = await runJsInDocker(repoPath, { testCases, entryFunction, evaluationMode, expectedLogs, functions });
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

          // GitHub Actions sends back evaluation as either:
          //   - An object  { passed, score, feedback }  (script mode)
          //   - An array   [{ passed, score }, ...]     (function mode, one entry per test case)
          // Handle both shapes so we never silently return 0.
          if (Array.isArray(results)) {
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
          if (job.data.rubricText && job.data.rubricText !== 'Standard evaluation') {
            try {
              let r = job.data.rubricText;
              while (typeof r === 'string') {
                r = JSON.parse(r);
              }
              if (Array.isArray(r)) {
                rubric = { criteria: r };
              } else if (r && r.criteria && Array.isArray(r.criteria)) {
                rubric = r;
              }
            } catch(e) {
               logger.warn(`Failed to parse rubricText for JS Job ${job.id}: ${e.message}`);
            }
          }

          const breakdown = [];
          const strengths = [];
          const issues = [];

          if (rubric && rubric.criteria && rubric.criteria.length > 0) {
            finalScore = 0; // Recalculate based on rubric weights
            for (const c of rubric.criteria) {
               const maxPts = typeof c.weight === 'number' ? c.weight : (typeof c.score === 'number' ? c.score : 0);
               const awardedPts = Math.round(maxPts * globalPassRatio);
               finalScore += awardedPts;

               const reason = totalCount > 0 
                  ? `Passed ${passedCount}/${totalCount} test cases.` 
                  : "No test cases run.";

               breakdown.push({
                 item: c.name,
                 awarded: awardedPts,
                 max: maxPts,
                 reason
               });

               if (globalPassRatio >= 1.0) {
                 strengths.push(`[${c.name}] ${reason} (earned ${awardedPts}/${maxPts} marks)`);
               } else if (globalPassRatio >= 0.5) {
                 strengths.push(`[${c.name}] ${reason} (earned ${awardedPts}/${maxPts} marks)`);
                 issues.push(`[${c.name}] Missed some test cases. (earned ${awardedPts}/${maxPts} marks)`);
               } else {
                 issues.push(`[${c.name}] ${reason} (earned ${awardedPts}/${maxPts} marks)`);
               }
            }
          }

          logger.info(`JS Job ${job.id} completed via GitHub Actions. Generating AI feedback...`);
          
          const summaryStr = await generateJSAIFeedback(job.data, results, finalScore, breakdown, rubric);
          
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
              details: results
            }
          };
        } catch (err) {
          logger.error(`JS Job ${job.id} failed`, err);
          throw err;
        }
      },
      {
        connection: redisConnection.getClient(),
        concurrency: config.concurrency,
        settings: {
          maxStalledCount: 2,
          lockDuration: 30000,
          lockRenewTime: 15000
        }
      }
    );

    // Event handlers
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
