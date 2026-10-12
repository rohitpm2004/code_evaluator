import { Worker } from 'bullmq';
import redisConnection, { redisConfig } from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { triggerGraderWorkflow } from '../services/githubActionService.js';
import { withTimeout } from '../evaluators/react/utils/timeout.js';
import { evaluateVisualProject } from '../evaluators/visual/evaluatorService.js';
import { isDockerAvailable } from '../services/dockerSandboxService.js';
import { webhookPubSub } from '../services/webhookPubSub.js';
import fs from 'fs/promises';
import os from 'os';
import path from 'path';

let visualWorker = null;

export async function initializeVisualWorker() {
  try {
    logger.info('Initializing Visual Worker...');

    const config = queueManager.getConfig('visual');

    visualWorker = new Worker(
      'visual-evaluation',
      async (job) => {
        try {
          logger.info(`Starting Visual evaluation: ${job.id}`);
          
          const results = await withTimeout(
            (async () => {
              if (job.data.submission && job.data.submission.ideFiles) {
                logger.info(`Visual Job ${job.id} is an IDE submission. Skipping GitHub Actions.`);
                const githubReport = "IDE Submission - Build and Linter assume passed.\n\n" + job.data.submission.ideFiles.map(f => `--- ${f.name} ---\n${f.content}`).join('\n\n');
                
                // Write files to a temp directory so we can read them locally
                const tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'visual-eval-'));
                for (const file of job.data.submission.ideFiles) {
                  if (file.path && typeof file.content === 'string') {
                    const filePath = path.join(tempDir, file.path);
                    await fs.mkdir(path.dirname(filePath), { recursive: true });
                    await fs.writeFile(filePath, file.content, 'utf8');
                  }
                }
                
                try {
                   // We mock a payload structure similar to Github payload
                   const mockPayload = {
                     repoUrl: tempDir, // repoService will know to skip cloning if it's already a dir
                     rubric: job.data.rubric || job.data.rubricText,
                     assignmentId: job.data.assignmentId,
                     expectedUrl: job.data.expectedUrl,
                     studentId: job.data.submission?.studentId || job.data.studentId,
                     studentName: job.data.submission?.studentName || job.data.studentName || job.data.submission?.name,
                     entryFile: job.data.entryFile || job.data.submission?.entryFile,
                     skipCache: job.data.skipCache || job.data.reEvaluate || job.data.isReEvaluation || false
                   };
                   return await evaluateVisualProject(mockPayload, job.id, githubReport);
                } finally {
                   await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {});
                }
              }

              // Phase 0: Check if Docker Sandbox is enabled and available
              const sandboxMode = (process.env.SANDBOX_MODE || "docker").toLowerCase();
              const canUseDocker = sandboxMode === "docker" && (await isDockerAvailable());

              if (canUseDocker) {
                logger.info(`🐳 Visual Job ${job.id}: Using local Docker sandbox. Skipping GitHub Actions.`);
                const submission = job.data.submission || job.data;
                const repoUrl = submission.repoUrl || submission.submission_link;
                const payload = {
                  repoUrl,
                  rubric: job.data.rubric || job.data.rubricText,
                  assignmentId: job.data.assignmentId,
                  expectedUrl: job.data.expectedUrl,
                  studentId: submission?.studentId || job.data.studentId,
                  studentName: submission?.studentName || job.data.studentName || submission?.name,
                  entryFile: job.data.entryFile || submission?.entryFile,
                  skipCache: job.data.skipCache || job.data.reEvaluate || job.data.isReEvaluation || false
                };
                return await evaluateVisualProject(payload, job.id, "Local Execution - Visual and DOM evaluation.");
              }

              logger.info(`☁️ Visual Job ${job.id}: Docker not available or SANDBOX_MODE=github. Using GitHub Actions.`);

              // Phase 1: Wait for webhook & Dispatch to GitHub Actions
              const webhookPromise = webhookPubSub.waitForWebhook(job.id, config.timeout);
              
              const webhookUrl = `${process.env.BASE_URL}/api/webhook/github`;
              const submission = job.data.submission || job.data;
              const repoUrl = submission.repoUrl || submission.submission_link;
              
              const extraPayload = {
                rubricText: job.data.rubricText,
                expectedUrl: job.data.expectedUrl,
                assignmentId: job.data.assignmentId
              };

              await triggerGraderWorkflow(repoUrl, job.id, webhookUrl, 'run-visual-evaluation', extraPayload);
              
              // Now await the result
              const githubResult = await webhookPromise;

              // Phase 2: Handle GitHub result
              const buildFailed = githubResult.status !== 'completed' ||
                (githubResult.testOutput || '').toLowerCase().includes('syntax error');

              if (buildFailed) {
                logger.info(`Visual Job ${job.id} had syntax issues on GitHub Actions. Still running AI rubric scoring for partial credit.`);
              } else {
                logger.info(`GitHub Action completed successfully for Visual Job ${job.id}. Proceeding to AI rubric evaluation.`);
              }

              // Phase 3: Always run AI rubric scoring
              const githubReport = githubResult.testOutput || (buildFailed ? 'Syntax Check: Failed.' : 'Syntax Check: Passed.');
              
              const payload = {
                repoUrl: repoUrl,
                rubric: job.data.rubric || job.data.rubricText,
                assignmentId: job.data.assignmentId,
                expectedUrl: job.data.expectedUrl,
                studentId: submission?.studentId || job.data.studentId,
                studentName: submission?.studentName || job.data.studentName || submission?.name,
                entryFile: job.data.entryFile || submission?.entryFile,
                skipCache: job.data.skipCache || job.data.reEvaluate || job.data.isReEvaluation || false
              };

              return await evaluateVisualProject(payload, job.id, githubReport);
            })(),
            config.timeout,
            `visual-eval-job ${job.id}`
          );
          
          logger.info(`Visual Job ${job.id} completed entirely.`);
          return { success: true, results, result: results };
        } catch (err) {
          logger.error(`Visual Job ${job.id} failed`, err);
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
    visualWorker.on('completed', (job, result) => {
      logger.info(`Visual Job ${job.id} completed`, {
        duration: job.finishedOn - job.processedOn
      });
    });

    visualWorker.on('failed', (job, err) => {
      logger.error(`Visual Job ${job.id} failed`, {
        error: err.message,
        attempts: job.attemptsMade
      });
    });

    visualWorker.on('error', (err) => {
      logger.error('Visual Worker internal error:', err);
    });

    logger.info('Visual Worker initialized');
    return visualWorker;

  } catch (err) {
    logger.error('Failed to initialize visual worker:', err);
    throw err;
  }
}

export async function stopVisualWorker() {
  try {
    if (visualWorker) {
      await visualWorker.close();
    }
    logger.info('Visual worker stopped');
  } catch (err) {
    logger.error('Error stopping visual worker:', err);
  }
}

export function getVisualWorkerStatus() {
  return {
    status: visualWorker ? 'running' : 'not_initialized',
    concurrency: queueManager.getConfig('visual').concurrency
  };
}

export { visualWorker };