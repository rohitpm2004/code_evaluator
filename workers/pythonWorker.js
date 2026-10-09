import { Worker } from 'bullmq';
import redisConnection from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { triggerGraderWorkflow } from '../services/githubActionService.js';
import { withTimeout } from '../evaluators/react/utils/timeout.js';
import { webhookPubSub } from '../services/webhookPubSub.js';
import { generatePythonAIFeedback } from '../evaluators/python/aiFeedback.js';
import { cloneRepo, deleteRepo } from '../evaluators/react/repoService.js';
import { isDockerAvailable, runPythonInDocker } from '../services/dockerSandboxService.js';

let pythonWorker = null;

export async function initializePythonWorker() {
  try {
    logger.info('Initializing python Worker...');

    const config = queueManager.getConfig('python');

    pythonWorker = new Worker(
      'python-evaluation',
      async (job) => {
          try {
            logger.info(`Starting Python evaluation: ${job.id}`);
            const { submission, testCases, evaluationMode, entryFunction, expectedLogs } = job.data;
            const repoUrl = submission.repoUrl;

            // Phase 0: Check if Docker Sandbox is enabled and available
            const sandboxMode = (process.env.SANDBOX_MODE || "docker").toLowerCase();
            const canUseDocker = sandboxMode === "docker" && (await isDockerAvailable());

            let results;
            if (canUseDocker) {
              logger.info(`🐳 Python Job ${job.id}: Using local Docker sandbox. Skipping GitHub Actions.`);
              const repoPath = await cloneRepo(repoUrl);
              try {
                results = await runPythonInDocker(repoPath, { testCases, entryFunction, evaluationMode });
              } finally {
                await deleteRepo(repoPath);
              }
            } else {
              logger.info(`☁️ Python Job ${job.id}: Docker not available or SANDBOX_MODE=github. Using GitHub Actions.`);
              results = await withTimeout(
                (async () => {
                  const webhookPromise = webhookPubSub.waitForWebhook(job.id, config.timeout);
                  
                  const webhookUrl = `${process.env.BASE_URL}/api/webhook/github`;
                  const extraPayload = { testCases, evaluationMode, entryFunction, expectedLogs };
                  await triggerGraderWorkflow(repoUrl, job.id, webhookUrl, 'run-python-evaluation', extraPayload);
                  
                  const githubResult = await webhookPromise;
                  
                  if (githubResult.status === 'completed') {
                    return githubResult.results || githubResult.evaluation || [];
                  } else {
                    throw new Error("GitHub Actions job failed or cancelled");
                  }
                })(),
                config.timeout,
                `python-eval ${job.id}`
              );
            }
            
            let finalScore = 0;
            let passedCount = 0;
            let totalCount = 0;

            if (Array.isArray(results)) {
              totalCount = results.length;
              passedCount = results.filter(r => r.passed).length;
              finalScore = totalCount > 0 ? (passedCount / totalCount) * 100 : 0;
            } else if (typeof results?.score === 'number') {
              finalScore = results.score;
              const match = (results.feedback || '').match(/(\d+)\s*of\s*(\d+)/);
              if (match) {
                passedCount = parseInt(match[1], 10);
                totalCount = parseInt(match[2], 10);
              }
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
                 logger.warn(`Failed to parse rubricText for Python Job ${job.id}: ${e.message}`);
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

            logger.info(`Python Job ${job.id} completed via GitHub Actions. Generating AI feedback...`);
            
            const summaryStr = await generatePythonAIFeedback(job.data, results, finalScore, breakdown, rubric);
            
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
            logger.error(`Python Job ${job.id} failed`, err);
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