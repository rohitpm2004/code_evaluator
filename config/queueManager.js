// src/queues/queueManager.js
/**
 * QUEUE MANAGER - Handles all evaluation queues
 * 
 * This integrates with your existing:
 * - evaluationRouter.js
 * - worker files (visualWorker, jsWorker, etc)
 * - Redis connection
 */

import os from 'os';
import crypto from 'crypto';
import { Queue } from 'bullmq';
import redisConnection, { redisConfig } from '../config/redis.js';
import logger from './logger.js';

/**
 * Queue Configuration
 * 
 * Each evaluator has:
 * - Queue name (matches worker subscription)
 * - Job name (for logging)
 * - Concurrency (how many jobs in parallel)
 * - Timeout (max job duration)
 * - Retries (attempt count)
 */

const QUEUE_CONFIG = {
  react: {
    jobName: 'react-job',
    concurrency: 2,
    timeout: 45000,       // 45s hard ceiling
    description: 'React Component Evaluation'
  },
  
  backend: {
    jobName: 'backend-job',
    concurrency: 2,
    timeout: 45000,       // 45s hard ceiling
    description: 'Backend API Evaluation'
  },
  
  visual: {
    jobName: 'visual-job',
    concurrency: parseInt(process.env.VISUAL_CONCURRENCY) || 2,
    timeout: 90000,       // 90s ceiling (covers git clone + multi-page Playwright DOM + AI reconciliation)
    description: 'Visual/UI Evaluation'
  },
  
  javascript: {
    jobName: 'javascript-job',
    concurrency: parseInt(process.env.JS_CONCURRENCY) || 4,
    timeout: 30000,
    description: 'JavaScript Evaluation'
  },
  
  python: {
    jobName: 'python-job',
    concurrency: parseInt(process.env.PYTHON_CONCURRENCY) || 4,
    timeout: 25000,       // 25s Python execution ceiling
    description: 'Python Evaluation'
  },
  
  fullstack: {
    jobName: 'fullstack-job',
    concurrency: 2,
    timeout: 45000,
    description: 'Full Stack Evaluation'
  },

  excel: {
    jobName: 'excel-job',
    concurrency: Math.min(Math.max(1, parseInt(process.env.EXCEL_CONCURRENCY, 10) || 5), 5),
    timeout: 35000,       // 35s Excel ceiling
    description: 'Excel Spreadsheet Evaluation'
  },

  sql: {
    jobName: 'sql-job',
    concurrency: parseInt(process.env.SQL_CONCURRENCY, 10) || 5,
    timeout: 30000,       // 30s SQL ceiling
    description: 'SQL Database Evaluation'
  }
};

/**
 * Queue Manager Class
 * 
 * Manages all queues, scheduling, and monitoring
 */
class QueueManager {
  constructor() {
    this.queues = {};
    this.isInitialized = false;
  }

  /**
   * Initialize all queues
   * Called once at application startup
   */
  async initialize() {
    if (this.isInitialized) {
      logger.warn('QueueManager already initialized');
      return;
    }

    try {
      logger.info('Initializing Queue Manager...');

      // Use shared Redis connection across all queues to minimize client connections
      const sharedConnection = redisConnection.connect();

      for (const [type, config] of Object.entries(QUEUE_CONFIG)) {
        this.queues[type] = new Queue(type + '-evaluation', {
          connection: sharedConnection,
          prefix: process.env.QUEUE_PREFIX || 'local',
          
          // Default job options: strictly cap retained jobs to prevent Redis Cloud 30MB OOM
          defaultJobOptions: {
            attempts: 1,
            backoff: {
              type: 'exponential',
              delay: 2000
            },
            removeOnComplete: {
              count: 100,
              age: 3600 // 1 hour
            },
            removeOnFail: {
              count: 50,
              age: 7200 // 2 hours
            }
          }
        });

        // Add error listener
        this.setupQueueListeners(type);

        logger.info(`Queue created: ${type}-evaluation (concurrency: ${config.concurrency})`);
      }

      // Run proactive cleanup to immediately free Redis memory from previous runs
      await this.pruneStaleRedisJobs();

      // Run periodic cleanup every 3 minutes
      setInterval(() => {
        this.pruneStaleRedisJobs().catch(() => {});
      }, 3 * 60 * 1000);

      this.isInitialized = true;
      logger.info('All queues initialized successfully');

    } catch (err) {
      logger.error('Failed to initialize queues:', err);
      throw err;
    }
  }

  /**
   * Proactively unlinks stale completed and failed job data from Redis to stay within memory limits.
   */
  async pruneStaleRedisJobs() {
    const conn = redisConnection.connect();
    const prefix = process.env.QUEUE_PREFIX || 'local';
    logger.info('🧹 Running Redis memory cleanup to prevent OOM...');

    try {
      // Log current memory usage
      try {
        const memInfo = await conn.info('memory');
        const usedMatch = memInfo.match(/used_memory_human:([^\r\n]+)/);
        const maxMatch = memInfo.match(/maxmemory_human:([^\r\n]+)/);
        if (usedMatch) {
          logger.info(`📊 Redis Memory Usage: ${usedMatch[1]} (Max limit: ${maxMatch ? maxMatch[1] : 'unlimited'})`);
        }
      } catch {}

      for (const type of Object.keys(QUEUE_CONFIG)) {
        const queueName = `${type}-evaluation`;
        const completedKey = `${prefix}:${queueName}:completed`;
        const failedKey = `${prefix}:${queueName}:failed`;
        const eventsKey = `${prefix}:${queueName}:events`;

        // 1. Remove completed jobs keeping at most 100
        try {
          const completedJobIds = await conn.zrange(completedKey, 0, -101);
          if (completedJobIds.length > 0) {
            const keysToDelete = [];
            for (const id of completedJobIds) {
              keysToDelete.push(prefix + ':' + queueName + ':' + id);
              keysToDelete.push(prefix + ':' + queueName + ':' + id + ':logs');
            }
            await conn.unlink(...keysToDelete).catch(() => {});
            await conn.zremrangebyrank(completedKey, 0, -101).catch(() => {});
          }
        } catch {}

        // 2. Remove failed jobs keeping at most 50
        try {
          const failedJobIds = await conn.zrange(failedKey, 0, -51);
          if (failedJobIds.length > 0) {
            const keysToDelete = [];
            for (const id of failedJobIds) {
              keysToDelete.push(prefix + ':' + queueName + ':' + id);
              keysToDelete.push(prefix + ':' + queueName + ':' + id + ':logs');
            }
            await conn.unlink(...keysToDelete).catch(() => {});
            await conn.zremrangebyrank(failedKey, 0, -51).catch(() => {});
          }
        } catch {}

        // 3. Trim events stream to last 10 entries
        try {
          await conn.xtrim(eventsKey, 'MAXLEN', '~', 10);
        } catch {}
      }

      // Also unlink any orphaned logs or legacy keys if Redis is tight on memory
      try {
        const legacyKeys = await conn.keys(`${prefix}:*:logs`);
        if (legacyKeys.length > 20) {
          await conn.unlink(...legacyKeys.slice(0, legacyKeys.length - 10)).catch(() => {});
        }
      } catch {}

      logger.info('✅ Redis memory cleanup complete.');
    } catch (err) {
      logger.warn(`Redis memory cleanup warning: ${err.message}`);
    }
  }

  /**
   * Setup event listeners for a queue
   */
  setupQueueListeners(type) {
    const queue = this.queues[type];

    // Error handling
    queue.on('error', (err) => {
      logger.error(`Queue error (${type}):`, err);
    });
  }

  /**
   * Add a job to the appropriate queue
   * 
   * Usage:
   * const job = await queueManager.addEvaluation('visual', {...payload});
   */
  async addEvaluation(type, payload) {
    if (!this.queues[type]) {
      throw new Error(`Invalid evaluator type: ${type}`);
    }

    try {
      const config = QUEUE_CONFIG[type];
      const queue = this.queues[type];

      // Derive a unique jobId for every evaluation run.
      // Previously this was a pure content-hash, which caused BullMQ to silently
      // skip re-evaluations of the same student (same hash → same jobId → job
      // already exists → BullMQ returns the old job without queuing a new one →
      // GitHub Action never fires → score stays "hardcoded" from the last run).
      // Adding Date.now() ensures every call produces a fresh job.
      let jobId = payload.jobId;
      if (jobId) {
        if (payload.reEvaluate || payload.isReEvaluation || payload.skipCache) {
          const rand = crypto.randomBytes(4).toString('hex');
          jobId = `${jobId}-${Date.now()}-${rand}`;
        }
      } else {
        const submissionData =
          payload.submission ||
          payload.submissions ||
          payload.repoUrl ||
          payload.submission_link ||
          payload.ideFiles ||
          '';
        const rubricData = payload.rubric || payload.rubricText || '';
        const idPayload = JSON.stringify({
          type,
          submission: submissionData,
          rubric: rubricData,
          testCases: payload.testCases || '',
          expectedUrl: payload.expectedUrl || '',
          assignmentId: payload.assignmentId || '',
        });
        const hash = crypto
          .createHash('sha256')
          .update(idPayload)
          .digest('hex')
          .slice(0, 16);
        // Prefix with timestamp and random entropy to guarantee uniqueness across batch/re-evaluations
        const rand = crypto.randomBytes(4).toString('hex');
        jobId = `${type}-${Date.now()}-${rand}-${hash}`;
      }

      // Add job to queue with deterministic jobId for deduplication and retry resilience
      const jobOptions = {
        jobId: String(jobId),
        attempts: config.attempts || 2,
        backoff: {
          type: 'exponential',
          delay: 5000,
        },
        removeOnComplete: { count: 2000, age: 86400 },
        removeOnFail: { count: 2000, age: 86400 }
      };

      // Only assign explicit positive numeric priority if explicitly requested.
      // Omit priority otherwise so BullMQ pushes jobs to standard FIFO wait list
      // without stalling behind prioritized ZSET markers.
      if (typeof payload.priority === 'number' && payload.priority > 0) {
        jobOptions.priority = payload.priority;
      }

      const job = await queue.add(
        config.jobName,
        payload,
        jobOptions
      );

      logger.info(`Job added: ${type}/${job.id}`, {
        evaluator: type,
        jobId: job.id,
        queueLength: (await queue.getJobCounts()).waiting
      });

      return job;

    } catch (err) {
      logger.error(`Failed to add job to ${type} queue:`, err);
      throw err;
    }
  }

  /**
   * Get job status
   */
  async getJobStatus(type, jobId) {
    try {
      const queue = this.queues[type];
      const job = await queue.getJob(jobId);
      if (!job) {
        return null;
      }
      logger.debug(`Job found: ${type}/${jobId}`); // V-33: no full-job/secret dump

      return {
        id: job.id,
        state: await job.getState(),
        progress: job.progress ?? 0,
        data: job.data,
        result: job.returnvalue,
        failedReason: job.failedReason,
        stacktrace: job.stacktrace
      };

    } catch (err) {
      logger.error(`Failed to get job status: ${type}/${jobId}`, err);
      return null;
    }
  }

  /**
   * Get statuses for a batch of jobs in parallel using a single batch request.
   * @param {string} [type] - Optional evaluator queue type
   * @param {string[]} jobIds - Array of job ID strings
   * @returns {Promise<Array<object>>}
   */
  async getBatchJobStatus(type, jobIds) {
    if (!Array.isArray(jobIds) || jobIds.length === 0) return [];

    return Promise.all(
      jobIds.map(async (id) => {
        try {
          // Priority 1: If job ID has an explicit queue prefix (e.g. 'visual-123' -> 'visual', 'excel-123' -> 'excel'),
          // trust the ID prefix over any mismatched type passed by caller!
          let targetQueue = null;
          if (typeof id === 'string' && id.includes('-')) {
            const prefix = id.split('-')[0].toLowerCase();
            const normalized = prefix === 'js' ? 'javascript' : prefix;
            targetQueue = this.queues[normalized];
          }
          if (!targetQueue && type) {
            targetQueue = this.queues[type];
          }

          if (targetQueue) {
            const job = await targetQueue.getJob(id);
            if (job) {
              return {
                id: job.id,
                state: await job.getState(),
                progress: job.progress ?? 0,
                result: job.returnvalue,
                failedReason: job.failedReason
              };
            }
          }

          // Fallback: search all queues
          for (const [qType, q] of Object.entries(this.queues)) {
            if (q === targetQueue) continue;
            try {
              const job = await q.getJob(id);
              if (job) {
                return {
                  id: job.id,
                  type: qType,
                  state: await job.getState(),
                  progress: job.progress ?? 0,
                  result: job.returnvalue,
                  failedReason: job.failedReason
                };
              }
            } catch {}
          }

          return { id, state: 'not_found' };
        } catch (e) {
          return { id, state: 'error', error: e.message };
        }
      })
    );
  }

  /**
   * Remove a job from queue
   */
  async removeJob(type, jobId) {
    try {
      const queue = this.queues[type];
      const job = await queue.getJob(jobId);
      
      if (job) {
        await job.remove();
        logger.info(`Job removed: ${type}/${jobId}`);
        return true;
      }
      
      return false;

    } catch (err) {
      logger.error(`Failed to remove job: ${type}/${jobId}`, err);
      throw err;
    }
  }

  /**
   * Get queue statistics
   */
  async getQueueStats(type) {
    try {
      const queue = this.queues[type];
      const counts = await queue.getJobCounts('waiting', 'active', 'completed', 'failed', 'delayed', 'paused', 'prioritized');

      return {
        evaluator: type,
        config: QUEUE_CONFIG[type],
        stats: {
          waiting: counts.waiting || 0,
          active: counts.active || 0,
          completed: counts.completed || 0,
          failed: counts.failed || 0,
          delayed: counts.delayed || 0,
          paused: counts.paused || 0,
          prioritized: counts.prioritized || 0
        },
        timestamp: new Date().toISOString()
      };

    } catch (err) {
      logger.error(`Failed to get stats for ${type}:`, err);
      throw err;
    }
  }

  /**
   * Get all queues statistics
   */
  async getAllStats() {
    const stats = {};

    for (const type of Object.keys(QUEUE_CONFIG)) {
      stats[type] = await this.getQueueStats(type);
    }

    return stats;
  }

  /**
   * Get queue health status
   */
  async getHealthStatus() {
    try {
      const stats = await this.getAllStats();
      
      // Check for backed up queues
      const backedUp = Object.entries(stats)
        .filter(([_, data]) => data.stats.waiting > 50)
        .map(([type, _]) => type);

      // Check for failed jobs
      const hasFailed = Object.entries(stats)
        .some(([_, data]) => data.stats.failed > 0);

      return {
        status: backedUp.length > 0 ? 'degraded' : 'healthy',
        redisConnected: redisConnection.status === 'ready',
        backedUpQueues: backedUp,
        hasFailed: hasFailed,
        stats: stats
      };

    } catch (err) {
      logger.error('Failed to get health status:', err);
      return { status: 'error', error: err.message };
    }
  }

  /**
   * Clear all jobs from a queue (DANGEROUS - use carefully)
   */
  async clearQueue(type) {
    try {
      const queue = this.queues[type];
      
      // Remove last 1000 jobs
      await queue.clean(0, 1000);
      
      logger.warn(`⚠️  Queue cleared: ${type}`);

    } catch (err) {
      logger.error(`Failed to clear queue ${type}:`, err);
      throw err;
    }
  }

  /**
   * Pause a queue (stop processing new jobs)
   */
  async pauseQueue(type) {
    try {
      const queue = this.queues[type];
      await queue.pause();
      logger.warn(`Queue paused: ${type}`);

    } catch (err) {
      logger.error(`Failed to pause queue ${type}:`, err);
      throw err;
    }
  }

  /**
   * Resume a paused queue
   */
  async resumeQueue(type) {
    try {
      const queue = this.queues[type];
      await queue.resume();
      logger.info(`Queue resumed: ${type}`);

    } catch (err) {
      logger.error(`Failed to resume queue ${type}:`, err);
      throw err;
    }
  }

  /**
   * Disconnect all queues (cleanup)
   */
  async disconnect() {
    try {
      logger.info('Disconnecting all queues...');

      for (const queue of Object.values(this.queues)) {
        await queue.close();
      }

      this.queues = {};
      this.isInitialized = false;

      logger.info('All queues disconnected');

    } catch (err) {
      logger.error('Error disconnecting queues:', err);
    }
  }

  /**
   * Get queue object directly (for workers)
   */
  getQueue(type) {
    if (!this.queues[type]) {
      throw new Error(`Queue not found: ${type}`);
    }
    return this.queues[type];
  }

  /**
   * Get all queue types
   */
  getQueueTypes() {
    return Object.keys(QUEUE_CONFIG);
  }

  /**
   * Get config for a queue type
   */
  getConfig(type) {
    return QUEUE_CONFIG[type];
  }
}

// Create singleton instance
const queueManager = new QueueManager();

export default queueManager;
export { QueueManager, QUEUE_CONFIG };