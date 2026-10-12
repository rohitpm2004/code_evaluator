import { Worker } from 'bullmq';
import redisConnection, { redisConfig } from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { triggerGraderWorkflow } from '../services/githubActionService.js';
import { withTimeout } from '../evaluators/react/utils/timeout.js';
import { evaluateBackendProject, evaluateBackendProjectWithDocker } from '../evaluators/backend/evaluatorService.js';
import { isDockerAvailable } from '../services/dockerSandboxService.js';
import { webhookPubSub } from '../services/webhookPubSub.js';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';

let backendWorker = null;

export async function initializeBackendWorker() {
  try {
    logger.info('Initializing Backend Worker...');

    const config = queueManager.getConfig('backend');

    backendWorker = new Worker(
      'backend-evaluation',
      async (job) => {
        try {
          logger.info(`Starting Backend evaluation: ${job.id}`);
          
          const results = await withTimeout(
            (async () => {
              const ideFiles = job.data.ideFiles || job.data.submission?.ideFiles;
              if (ideFiles && Array.isArray(ideFiles) && ideFiles.length > 0) {
                logger.info(`Backend Job ${job.id} is an IDE submission. Materializing ${ideFiles.length} files into temp sandbox.`);
                const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'backend-ide-'));
                try {
                  for (const file of ideFiles) {
                    const relativeFilePath = file.path || file.name;
                    if (relativeFilePath && typeof file.content === 'string') {
                      const safeRelativePath = path.normalize(relativeFilePath).replace(/^(\.\.[\/\\])+/, '');
                      const fullFilePath = path.resolve(tempDir, safeRelativePath);
                      if (!fullFilePath.startsWith(path.resolve(tempDir))) {
                        logger.warn(`Security: Skipping suspicious file path outside sandbox: ${relativeFilePath}`);
                        continue;
                      }
                      await fs.mkdir(path.dirname(fullFilePath), { recursive: true });
                      await fs.writeFile(fullFilePath, file.content, 'utf8');
                    }
                  }
                  const idePayload = {
                    ...job.data,
                    repoUrl: tempDir
                  };
                  return await evaluateBackendProject(idePayload, job.id, null, "IDE Submission - Files verified locally.");
                } finally {
                  await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {});
                }
              }

              // Phase 0: Check if Docker Sandbox is enabled and available
              const sandboxMode = (process.env.SANDBOX_MODE || "docker").toLowerCase();
              const canUseDocker = sandboxMode === "docker" && (await isDockerAvailable());

              if (canUseDocker) {
                logger.info(`🐳 Backend Job ${job.id}: Using local Docker sandbox. Skipping GitHub Actions.`);
                return await evaluateBackendProjectWithDocker(job.data, job.id);
              }

              logger.info(`☁️ Backend Job ${job.id}: Docker not available or SANDBOX_MODE=github. Using GitHub Actions.`);

              // Phase 1: Wait for webhook & Dispatch to GitHub Actions
              // We set up the listener FIRST to avoid race conditions if GitHub returns instantly
              const webhookPromise = webhookPubSub.waitForWebhook(job.id, config.timeout);
              
              const webhookUrl = `${process.env.BASE_URL}/api/webhook/github`;
              const repoUrl = job.data.repoUrl || job.data.submission_link;
              const specFile = job.data.rubric?.specFile || null;
              await triggerGraderWorkflow(repoUrl, job.id, webhookUrl, 'run-backend-evaluation', {
                rubric: job.data.rubric,
                specFile
              });
              
              // Now await the result
              const githubResult = await webhookPromise;

              // Phase 2: Handle GitHub result
              const buildFailed = githubResult.status !== 'completed' || !githubResult.testResults;

              if (buildFailed) {
                logger.info(`Backend Job ${job.id} had run failure on GitHub Actions. Still running AI rubric scoring for partial credit.`);
              } else {
                logger.info(`GitHub Action completed successfully for Backend Job ${job.id}. Proceeding to rubric evaluation.`);
              }

              // Phase 3: Evaluate with Jest test results or fallback to AI scoring
              const githubReport = githubResult.testOutput || (buildFailed ? 'Run: Failed. The test execution encountered an error.' : 'Run Completed.');
              return await evaluateBackendProject(job.data, job.id, githubResult.testResults, githubReport);
            })(),
            config.timeout,
            `backend-eval-job ${job.id}`
          );
          
          logger.info(`Backend Job ${job.id} completed entirely.`);
          return { success: true, results, result: results };
        } catch (err) {
          logger.error(`Backend Job ${job.id} failed`, err);
          if (!err.failedReason) {
            err.failedReason = `[infrastructure_failed] ${err.message}`;
          }
          throw err;
        }
      },
      {
        connection: redisConfig,
        prefix: process.env.QUEUE_PREFIX || 'local',
        concurrency: config.concurrency,
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
    backendWorker.on('completed', (job, result) => {
      logger.info(`Backend Job ${job.id} completed`, {
        duration: job.finishedOn - job.processedOn
      });
    });

    backendWorker.on('failed', (job, err) => {
      logger.error(`Backend Job ${job.id} failed`, {
        error: err.message,
        attempts: job.attemptsMade
      });
    });

    backendWorker.on('error', (err) => {
      logger.error('Backend Worker internal error:', err);
    });

    logger.info('Backend Worker initialized');
    return backendWorker;

  } catch (err) {
    logger.error('Failed to initialize Backend worker:', err);
    throw err;
  }
}

export async function stopBackendWorker() {
  try {
    if (backendWorker) {
      await backendWorker.close();
    }
    logger.info('Backend worker stopped');
  } catch (err) {
    logger.error('Error stopping Backend worker:', err);
  }
}

export function getBackendWorkerStatus() {
  return {
    status: backendWorker ? 'running' : 'not_initialized',
    concurrency: queueManager.getConfig('backend').concurrency
  };
}

export { backendWorker };