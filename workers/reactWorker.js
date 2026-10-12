import { Worker } from 'bullmq';
import redisConnection, { redisConfig } from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { triggerGraderWorkflow } from '../services/githubActionService.js';
import { withTimeout } from '../evaluators/react/utils/timeout.js';
import { evaluateReactProject, evaluateReactProjectWithDocker } from '../evaluators/react/evaluatorService.js';
import { isDockerAvailable } from '../services/dockerSandboxService.js';
import { webhookPubSub } from '../services/webhookPubSub.js';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';

let reactWorker = null;

export async function initializeReactWorker() {
  try {
    logger.info('Initializing React Worker...');

    const config = queueManager.getConfig('react');

    reactWorker = new Worker(
      'react-evaluation',
      async (job) => {
        try {
          logger.info(`Starting React evaluation: ${job.id}`);
          
          const results = await withTimeout(
            (async () => {
              const ideFiles = job.data.ideFiles || job.data.submission?.ideFiles;
              if (ideFiles && Array.isArray(ideFiles) && ideFiles.length > 0) {
                logger.info(`React Job ${job.id} is an IDE submission. Materializing ${ideFiles.length} files into temp sandbox.`);
                const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'react-ide-'));
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
                  return await evaluateReactProject(idePayload, job.id, "IDE Submission - Files verified locally.");
                } finally {
                  await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {});
                }
              }

              // Phase 0: Check if Docker Sandbox is enabled and available
              const sandboxMode = (process.env.SANDBOX_MODE || "docker").toLowerCase();
              const canUseDocker = sandboxMode === "docker" && (await isDockerAvailable());

              if (canUseDocker) {
                logger.info(`🐳 React Job ${job.id}: Using local Docker sandbox. Skipping GitHub Actions.`);
                return await evaluateReactProjectWithDocker(job.data, job.id);
              }

              logger.info(`☁️ React Job ${job.id}: Docker not available or SANDBOX_MODE=github. Using GitHub Actions.`);

              // Phase 1: Wait for webhook & Dispatch to GitHub Actions
              const webhookPromise = webhookPubSub.waitForWebhook(job.id, config.timeout);
              
              const webhookUrl = `${process.env.BASE_URL}/api/webhook/github`;
              const repoUrl = job.data.repoUrl || job.data.submission_link;
              await triggerGraderWorkflow(repoUrl, job.id, webhookUrl, 'run-react-evaluation');
              
              // Now await the result
              const githubResult = await webhookPromise;

              // Phase 2: Handle GitHub result
              const buildFailed = githubResult.status !== 'completed' ||
                (githubResult.testOutput || '').toLowerCase().includes('build: failed') ||
                (githubResult.testOutput || '').toLowerCase().includes('failed to compile');

              if (buildFailed) {
                logger.info(`React Job ${job.id} had build failure on GitHub Actions. Still running AI rubric scoring for partial credit.`);
              } else {
                logger.info(`GitHub Action completed successfully for React Job ${job.id}. Proceeding to AI rubric evaluation.`);
              }

              // Phase 3: Always run AI rubric scoring (build report is passed as context)
              // Build failures penalise relevant criteria (UI/CSS), but students still
              // get credit for state management, function logic, etc that can be
              // assessed from the source code alone.
              const githubReport = githubResult.testOutput || (buildFailed ? 'Build: Failed. The application could not be compiled.' : 'Build and Linter Passed.');
              return await evaluateReactProject(job.data, job.id, githubReport, githubResult.testResults);
            })(),
            config.timeout,
            `react-eval-job ${job.id}`
          );
          
          logger.info(`React Job ${job.id} completed entirely.`);
          return { success: true, results, result: results };
        } catch (err) {
          logger.error(`React Job ${job.id} failed`, err);
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
    reactWorker.on('completed', (job, result) => {
      logger.info(`React Job ${job.id} completed`, {
        duration: job.finishedOn - job.processedOn
      });
    });

    reactWorker.on('failed', (job, err) => {
      logger.error(`React Job ${job.id} failed`, {
        error: err.message,
        attempts: job.attemptsMade
      });
    });

    reactWorker.on('error', (err) => {
      logger.error('React Worker internal error:', err);
    });

    logger.info('React Worker initialized');
    return reactWorker;

  } catch (err) {
    logger.error('Failed to initialize React worker:', err);
    throw err;
  }
}

export async function stopReactWorker() {
  try {
    if (reactWorker) {
      await reactWorker.close();
    }
    logger.info('React worker stopped');
  } catch (err) {
    logger.error('Error stopping React worker:', err);
  }
}

export function getJsWorkerStatus() {
  return {
    status: reactWorker ? 'running' : 'not_initialized',
    concurrency: queueManager.getConfig('react').concurrency
  };
}

export { reactWorker };