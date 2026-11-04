import { Worker } from 'bullmq';
import { redisConfig } from '../config/redis.js';
import queueManager from '../config/queueManager.js';
import logger from '../config/logger.js';
import { withTimeout } from '../utils/timeout.js';
import { evaluateSqlSubmission, sweepOrphanedSchemas } from '../evaluators/sql/evaluatorService.js';

let sqlWorker = null;

/**
 * Create and return the SQL queue worker with configured concurrency and evaluation timeouts.
 * Follows the same lifecycle pattern as excelWorker.js.
 */
export async function initializeSqlWorker() {
  try {
    logger.info('Initializing SQL Worker...');

    // Sweep orphaned schemas on startup and every 15 minutes periodically
    sweepOrphanedSchemas().catch((err) => logger.warn('[SchemaClean] Startup sweep error:', err.message));
    setInterval(() => {
      sweepOrphanedSchemas().catch((err) => logger.warn('[SchemaClean] Periodic sweep error:', err.message));
    }, 15 * 60 * 1000).unref();

    const config = queueManager.getConfig('sql') || {
      concurrency: 4,
      timeout: 120000,
      attempts: 3
    };

    const envConcurrency = parseInt(process.env.SQL_CONCURRENCY, 10);
    const effectiveConcurrency = !isNaN(envConcurrency)
      ? envConcurrency
      : (config.concurrency || 4);

    logger.info(`SQL Worker concurrency set to ${effectiveConcurrency}`);

    sqlWorker = new Worker(
      'sql-evaluation',
      async (job) => {
        try {
          logger.info(`Starting SQL evaluation for Job ${job.id}`);

          const results = await withTimeout(
            evaluateSqlSubmission(job.data, job.id),
            config.timeout,
            `sql-eval-job ${job.id}`
          );

          logger.info(`SQL Job ${job.id} completed with score: ${results.score ?? 0}`);

          // Return the result so BullMQ persists it as job.returnvalue.
          // async-lms polls GET /jobs/sql/:jobId and reads job.returnvalue
          // via syncEvaluationStatus — there is no outbound webhook.
          return results;
        } catch (err) {
          logger.error(`SQL Job ${job.id} failed: ${err.message}`);
          throw err;
        }
      },
      {
        connection: redisConfig,
        prefix: process.env.QUEUE_PREFIX || 'local',
        concurrency: effectiveConcurrency,
        drainDelay: 5,
        settings: {
          maxStalledCount: 3,
          stalledInterval: 30000,
          lockDuration: 180000,
          lockRenewTime: 30000
        }
      }
    );

    sqlWorker.on('completed', (job) => {
      logger.info(`SQL Job ${job.id} finished successfully`);
    });

    sqlWorker.on('failed', (job, err) => {
      logger.error(`SQL Job ${job?.id} failed with error:`, err);
    });

    sqlWorker.on('error', (err) => {
      logger.error('SQL Worker connection error:', err);
    });

    logger.info('SQL Worker initialized successfully');
    return sqlWorker;
  } catch (err) {
    logger.error('Failed to initialize SQL Worker:', err);
    throw err;
  }
}

/**
 * Wait for the SQL worker to close and clear the stored worker reference.
 */
export async function stopSqlWorker() {
  if (sqlWorker) {
    logger.info('Stopping SQL Worker...');
    await sqlWorker.close();
    sqlWorker = null;
    logger.info('SQL Worker stopped');
  }
}

/**
 * Return the current SQL worker, or null before initialization or after shutdown.
 */
export function getSqlWorker() {
  return sqlWorker;
}
