import { Worker } from 'bullmq';
import redisConnection, { redisConfig } from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { triggerGraderWorkflow } from '../services/githubActionService.js';
import { withTimeout } from '../evaluators/react/utils/timeout.js';
import { evaluateFullstackProject, evaluateFullstackProjectWithDocker } from "../evaluators/fullstack/evaluatorService.js";
import { isDockerAvailable } from '../services/dockerSandboxService.js';
import { webhookPubSub } from '../services/webhookPubSub.js';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';

let fullstackWorker = null;

export async function initializeFullstackWorker() {
  try {
    logger.info('Initializing Fullstack Worker...');

    const config = queueManager.getConfig('fullstack');

    fullstackWorker = new Worker(
      'fullstack-evaluation',
      async (job) => {
        try {
          logger.info(`Starting Fullstack evaluation: ${job.id}`);

          const ideFiles = job.data.ideFiles || job.data.submission?.ideFiles;
          if (ideFiles && Array.isArray(ideFiles) && ideFiles.length > 0) {
            logger.info(`Fullstack Job ${job.id} is an IDE submission. Materializing ${ideFiles.length} files into temp sandbox.`);
            const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'fullstack-ide-'));
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
              const results = await evaluateFullstackProject(idePayload, job.id, null, "IDE Submission - Files verified locally.");
              return { success: true, results, result: results };
            } finally {
              await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {});
            }
          }

          // Phase 0: Check if Docker Sandbox is enabled and available
          const sandboxMode = (process.env.SANDBOX_MODE || "docker").toLowerCase();
          const canUseDocker = sandboxMode === "docker" && (await isDockerAvailable());

          if (canUseDocker) {
            logger.info(`🐳 Fullstack Job ${job.id}: Using local Docker sandbox. Skipping GitHub Actions.`);
            const results = await evaluateFullstackProjectWithDocker(job.data, job.id);
            return { success: true, results, result: results };
          }

          logger.info(`☁️ Fullstack Job ${job.id}: Docker not available or SANDBOX_MODE=github. Using GitHub Actions.`);
          
          // Phase 1: Dispatch to GitHub Actions
          const githubResult = await withTimeout(
            (async () => {
              const webhookPromise = webhookPubSub.waitForWebhook(job.id, config.timeout);
              
              const webhookUrl = `${process.env.BASE_URL}/api/webhook/github`;
              const repoUrl = job.data.repoUrl || job.data.submission_link;
              const specFile = job.data.rubric?.specFile || null;
              await triggerGraderWorkflow(repoUrl, job.id, webhookUrl, 'run-fullstack-evaluation', {
                rubric: job.data.rubric,
                specFile
              });
              
              return await webhookPromise;
            })(),
            config.timeout,
            `fullstack-eval-github ${job.id}`
          );

          // Phase 2: Handle GitHub result
          const buildFailed = githubResult.status !== 'completed' || !githubResult.testResults;
          if (buildFailed) {
            logger.info(`Fullstack Job ${job.id} had run failure on GitHub Actions. Still running AI rubric scoring for partial credit.`);
          } else {
            logger.info(`GitHub Action completed successfully for Fullstack Job ${job.id}. Proceeding to rubric evaluation.`);
          }

          // Phase 3: Evaluate with Playwright test results or fallback to AI scoring
          const githubReport = githubResult.testOutput || (buildFailed ? 'Run: Failed. The test execution encountered an error.' : 'Run Completed.');
          logger.info(`RAW PLAYWRIGHT RESULTS FOR JOB ${job.id}: ${JSON.stringify(githubResult.testResults)}`);
          const results = await evaluateFullstackProject(job.data, job.id, githubResult.testResults, githubReport);
          
          logger.info(`Fullstack Job ${job.id} completed entirely.`);
          return { success: true, results, result: results };
        } catch (err) {
          logger.error(`Fullstack Job ${job.id} failed`, err);
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
    fullstackWorker.on('completed', (job, result) => {
      logger.info(`Fullstack Job ${job.id} completed`, {
        duration: job.finishedOn - job.processedOn
      });
    });

    fullstackWorker.on('failed', (job, err) => {
      logger.error(`FullStack Job ${job.id} failed`, {
        error: err.message,
        attempts: job.attemptsMade
      });
    });

    fullstackWorker.on('error', (err) => {
      logger.error('Fullstack Worker internal error:', err);
    });

    logger.info('Fullstack Worker initialized');
    return fullstackWorker;

  } catch (err) {
    logger.error('Failed to initialize Fullstack worker:', err);
    throw err;
  }
}

export async function stopFullstackWorker() {
  try {
    if (fullstackWorker) {
      await fullstackWorker.close();
    }
    logger.info('Fullstack worker stopped');
  } catch (err) {
    logger.error('Error stopping Fullstack worker:', err);
  }
}

export function getFullstackWorkerStatus() {
  return {
    status: fullstackWorker ? 'running' : 'not_initialized',
    concurrency: queueManager.getConfig('fullstack').concurrency
  };
}

export { fullstackWorker };