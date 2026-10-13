import { Worker } from 'bullmq';
import redisConnection, { redisConfig } from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { withTimeout } from '../utils/timeout.js';
import { evaluateExcelSubmission } from '../evaluators/excel/evaluatorService.js';

let excelWorker = null;

/**
 * Create and return the Excel queue worker with configured concurrency and evaluation timeouts.
 */
export async function initializeExcelWorker() {
  try {
    logger.info('Initializing Excel Worker...');

    const config = queueManager.getConfig('excel') || {
      concurrency: 2,
      timeout: 60000,
      attempts: 2
    };

    excelWorker = new Worker(
      'excel-evaluation',
      async (job) => {
        try {
          logger.info(`Starting Excel evaluation for Job ${job.id}`);

          const results = await withTimeout(
            evaluateExcelSubmission(job.data, job.id),
            config.timeout,
            `excel-eval-job ${job.id}`
          );

          logger.info(`Excel Job ${job.id} completed with status: ${results.evaluation_status}`);
          return results;
        } catch (err) {
          logger.error(`Excel Job ${job.id} failed: ${err.message}`);
          throw err;
        }
      },
      {
        connection: redisConfig,
        prefix: process.env.QUEUE_PREFIX || 'local',
        concurrency: config.concurrency || 2,
        drainDelay: 5,
        settings: {
          maxStalledCount: 3,
          stalledInterval: 10000,
          lockDuration: 60000,
          lockRenewTime: 20000
        }
      }
    );

    excelWorker.on('completed', (job) => {
      logger.info(`Excel Job ${job.id} finished successfully`);
    });

    excelWorker.on('failed', (job, err) => {
      logger.error(`Excel Job ${job?.id} failed with error:`, err);
    });

    excelWorker.on('error', (err) => {
      logger.error('Excel Worker connection error:', err);
    });

    logger.info('Excel Worker initialized successfully');
    return excelWorker;
  } catch (err) {
    logger.error('Failed to initialize Excel Worker:', err);
    throw err;
  }
}

/**
 * Wait for the Excel worker to close and clear the stored worker reference.
 */
export async function stopExcelWorker() {
  if (excelWorker) {
    logger.info('Stopping Excel Worker...');
    await excelWorker.close();
    excelWorker = null;
    logger.info('Excel Worker stopped');
  }
}

/**
 * Return the current Excel worker, or null before initialization or after shutdown.
 */
export function getExcelWorker() {
  return excelWorker;
}
